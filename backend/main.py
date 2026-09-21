import json
import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile

import fitz
import httpx
from docx import Document
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

MAX_FILE_SIZE = 25 * 1024 * 1024
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5vl:7b")

app = FastAPI(title="ReadDocument extraction API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["POST"],
    allow_headers=["*"],
)


def extract_pdf(path: str) -> str:
    document = fitz.open(path)
    text = "\n\n".join(page.get_text("text") for page in document).strip()
    document.close()
    return text


def extract_docx(path: str) -> str:
    document = Document(path)
    parts = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            parts.append("\t".join(cell.text.strip() for cell in row.cells))
    return "\n".join(parts).strip()


def extract_text(path: str, suffix: str) -> str:
    if suffix == ".pdf":
        return extract_pdf(path)
    if suffix == ".docx":
        return extract_docx(path)
    raise ValueError("Поддерживаются только PDF и DOCX")


def response_json(content: str) -> dict:
    cleaned = content.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=502, detail="Qwen вернула некорректный JSON") from error
    if not isinstance(value, dict):
        raise HTTPException(status_code=502, detail="Qwen вернула JSON не того формата")
    return value


async def extract_with_qwen(text: str, source_name: str) -> dict:
    prompt = """Извлеки данные коммерческого предложения из текста.
Верни ТОЛЬКО валидный JSON без markdown и дополнительных пояснений:
{
  "title": "string",
  "client": "string",
  "clientContact": "string",
  "validUntil": "string",
  "notes": "полный исходный текст",
  "items": [{"name": "string", "quantity": 0, "unit": "string", "unitPrice": 0}]
}
Не придумывай значения. Если поле не найдено, верни пустую строку, для items верни [].
Числа quantity и unitPrice должны быть числами без валютных символов."""
    async with httpx.AsyncClient(timeout=180) as client:
        try:
            response = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "stream": False,
                    "format": "json",
                    "messages": [{"role": "user", "content": f"{prompt}\n\nФайл: {source_name}\n\n{text}"}],
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise HTTPException(
                status_code=503,
                detail=f"Ollama недоступна. Запустите модель {OLLAMA_MODEL}",
            ) from error
    payload = response.json()
    return response_json(payload.get("message", {}).get("content", ""))


@app.post("/api/extract")
async def extract(file: UploadFile = File(...)) -> dict:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise HTTPException(status_code=415, detail="AI pipeline поддерживает PDF и DOCX")

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="Файл не должен превышать 25 МБ")

    temporary_path = ""
    try:
        with NamedTemporaryFile(delete=False, suffix=suffix) as temporary:
            temporary.write(content)
            temporary_path = temporary.name
        text = extract_text(temporary_path, suffix)
        if not text:
            raise HTTPException(status_code=422, detail="В документе не найден текстовый слой")
        proposal = await extract_with_qwen(text, file.filename or "document")
        return {
            "proposal": proposal,
            "metadata": {
                "sourceName": file.filename,
                "parser": f"Qwen2.5-VL-7B/{suffix[1:].upper()}",
                "status": "parsed",
                "confidence": 0.9,
                "warnings": ["Проверьте данные перед подтверждением"],
            },
        }
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=422, detail="Не удалось обработать документ") from error
    finally:
        if temporary_path:
            Path(temporary_path).unlink(missing_ok=True)
