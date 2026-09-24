import base64
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from uuid import UUID

import fitz
import httpx
from docx import Document
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

MAX_FILE_SIZE = 25 * 1024 * 1024
MAX_OCR_PAGES = 8
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5vl:7b")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "./data/readdocument.sqlite3"))
ALLOWED_SUFFIXES = {".pdf", ".docx", ".png", ".jpg", ".jpeg", ".webp"}

app = FastAPI(title="ReadDocument extraction API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Idempotency-Key"],
)


def connect_db() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_db() -> None:
    with connect_db() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS proposals (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )"""
        )


initialize_db()


def extract_pdf(content: bytes) -> tuple[str, list[str]]:
    document = fitz.open(stream=content, filetype="pdf")
    try:
        images: list[str] = []
        text_pages: list[str] = []
        for number, page in enumerate(document, start=1):
            page_text = page.get_text("text").strip()
            if page_text:
                text_pages.append(f"[Страница {number}]\n{page_text}")
            elif len(images) < MAX_OCR_PAGES:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                images.append(base64.b64encode(pixmap.tobytes("png")).decode("ascii"))
        return "\n\n".join(text_pages).strip(), images
    finally:
        document.close()


def extract_docx(content: bytes) -> str:
    document = Document(BytesIO(content))
    parts = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
    for table_index, table in enumerate(document.tables, start=1):
        parts.append(f"[ТАБЛИЦА {table_index}]")
        for row in table.rows:
            parts.append("\t".join(cell.text.strip() for cell in row.cells))
    return "\n".join(parts).strip()


def image_data(content: bytes, suffix: str) -> list[str]:
    if suffix == ".webp":
        try:
            from PIL import Image
        except ImportError as error:
            raise HTTPException(status_code=415, detail="Для WEBP требуется Pillow") from error
        image = Image.open(BytesIO(content)).convert("RGB")
        output = BytesIO()
        image.save(output, format="JPEG", quality=90)
        content = output.getvalue()
    return [base64.b64encode(content).decode("ascii")]


def response_json(content: str) -> dict:
    cleaned = content.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=502, detail="Модель вернула некорректный JSON") from error
    if not isinstance(value, dict):
        raise HTTPException(status_code=502, detail="Модель вернула данные неверного формата")
    return value


def number(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    normalized = re.sub(r"[^\d,.\-]", "", str(value or "")).replace(",", ".")
    try:
        return float(normalized)
    except ValueError:
        return 0


def normalize_proposal(value: dict, source_text: str) -> dict:
    normalized_items = []
    items = value.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name:
                normalized_items.append({
                    "name": name,
                    "quantity": number(item.get("quantity")),
                    "unit": str(item.get("unit") or "").strip(),
                    "unitPrice": number(item.get("unitPrice")),
                })
    proposal = {
        "title": str(value.get("title") or "").strip(),
        "client": str(value.get("client") or "").strip(),
        "clientContact": str(value.get("clientContact") or "").strip(),
        "validUntil": str(value.get("validUntil") or "").strip(),
        "supplier": str(value.get("supplier") or "").strip(),
        "currency": str(value.get("currency") or "").strip(),
        "vat": str(value.get("vat") or "").strip(),
        "discount": str(value.get("discount") or "").strip(),
        "delivery": str(value.get("delivery") or "").strip(),
        "paymentTerms": str(value.get("paymentTerms") or "").strip(),
        "deliveryTerms": str(value.get("deliveryTerms") or "").strip(),
        "warranty": str(value.get("warranty") or "").strip(),
        "documentNumber": str(value.get("documentNumber") or "").strip(),
        "documentDate": str(value.get("documentDate") or "").strip(),
        "documentTotal": number(value.get("documentTotal")),
        "notes": source_text,
        "items": normalized_items,
    }
    return proposal


PROMPT = """Ты аккуратно извлекаешь сведения из коммерческого предложения. Используй только то, что явно видно в документе. Не угадывай и не дополняй пропуски.
Не путай номер документа, ИНН, телефон и даты с количеством или ценой. Не объединяй строки.
Для не найденного текста верни пустую строку; для не найденного числа — 0; при сомнении не включай товарную строку.
Верни только JSON с полями title, client, clientContact, validUntil, supplier, currency, vat, discount, delivery, paymentTerms, deliveryTerms, warranty, documentNumber, documentDate, documentTotal и items.
items — массив объектов {name, quantity, unit, unitPrice}; включай только реальные строки, не заголовки и не итоги. Сохраняй оригинальные значения и валюту. Не пересчитывай цены."""


async def extract_with_qwen(text: str, source_name: str, images: list[str] | None = None) -> dict:
    message: dict = {"role": "user", "content": f"{PROMPT}\n\nФАЙЛ: {source_name}"}
    if text:
        message["content"] += f"\n\nТЕКСТ ДОКУМЕНТА:\n{text}"
    if images:
        message["images"] = images
    async with httpx.AsyncClient(timeout=240) as client:
        try:
            response = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0, "num_ctx": 16384},
                    "messages": [message],
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise HTTPException(status_code=503, detail=f"Сервис распознавания недоступен: {OLLAMA_MODEL}") from error
    payload = response.json()
    parsed = response_json(payload.get("message", {}).get("content", ""))
    return normalize_proposal(parsed, text)


def evidence_for(proposal: dict, text: str, source_name: str) -> dict:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    evidence: dict = {}
    for field in ("title", "client", "clientContact", "validUntil", "supplier", "currency",
                  "vat", "discount", "delivery", "paymentTerms", "deliveryTerms", "warranty",
                  "documentNumber", "documentDate", "documentTotal"):
        value = str(proposal.get(field) or "").strip()
        if not value:
            continue
        matched = next((line for line in lines if value.casefold() in line.casefold()), "")
        page_match = re.search(r"\[Страница (\d+)\]", "\n".join(lines[:lines.index(matched) + 1])) if matched in lines else None
        evidence[field] = {"file": source_name, "excerpt": matched or value, "verifiedInSource": bool(matched), **({"page": int(page_match.group(1))} if page_match else {})}
    evidence["items"] = [
        {"file": source_name,
         "excerpt": (matched_item := next((line for line in lines if item["name"].casefold() in line.casefold()), item["name"])),
         "verifiedInSource": matched_item != item["name"] or any(item["name"].casefold() == line.casefold() for line in lines),
         **({"page": int(page_match.group(1))} if (page_match := re.search(r"\[Страница (\d+)\]", "\n".join(lines[:lines.index(matched_item) + 1]))) and matched_item in lines else {})}
        for item in proposal["items"]
    ]
    return evidence


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "database": "sqlite", "model": OLLAMA_MODEL}


@app.post("/api/extract")
async def extract(file: UploadFile = File(...)) -> dict:
    filename = Path(file.filename or "document").name
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=415, detail="Поддерживаются PDF, DOCX, PNG, JPG и WEBP")
    content = await file.read(MAX_FILE_SIZE + 1)
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="Файл должен быть не больше 25 МБ")
    try:
        if suffix == ".pdf":
            text, images = extract_pdf(content)
        elif suffix == ".docx":
            text, images = extract_docx(content), []
        else:
            text, images = "", image_data(content, suffix)
        if not text and not images:
            raise HTTPException(status_code=422, detail="В документе не найден текстовый слой")
        proposal = await extract_with_qwen(text, filename, images)
        confidence = 0.9 if proposal["items"] and proposal["client"] else (0.72 if text else 0.62)
        warnings = ["Проверьте распознанные значения по документу"]
        if images:
            warnings.append(f"Результат OCR может требовать ручной проверки; обработано страниц: {len(images)}")
        if not proposal["items"]:
            warnings.append("Позиции не распознаны уверенно")
        return {
            "proposal": proposal,
            "metadata": {
                "sourceName": filename,
                "parser": f"{OLLAMA_MODEL}/{suffix[1:].upper()}",
                "status": "parsed",
                "confidence": confidence,
                "warnings": warnings,
                "fieldEvidence": evidence_for(proposal, text, filename),
                "ocrPages": len(images),
            },
        }
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=422, detail="Не удалось обработать документ") from error


@app.post("/api/proposals")
async def save_proposal(payload: dict, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
    proposal = payload.get("proposal")
    if not isinstance(proposal, dict):
        raise HTTPException(status_code=422, detail="Не переданы данные предложения")
    key = idempotency_key or str(UUID(bytes=os.urandom(16), version=4))
    try:
        key = str(UUID(key))
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Некорректный ключ отправки") from error
    identifier = str(UUID(bytes=os.urandom(16), version=4))
    created_at = datetime.now(timezone.utc).isoformat()
    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    try:
        with connect_db() as connection:
            connection.execute(
                "INSERT INTO proposals(id, idempotency_key, created_at, payload) VALUES (?, ?, ?, ?)",
                (identifier, key, created_at, serialized),
            )
    except sqlite3.IntegrityError:
        with connect_db() as connection:
            row = connection.execute("SELECT id, created_at FROM proposals WHERE idempotency_key = ?", (key,)).fetchone()
        return {"id": row["id"], "createdAt": row["created_at"], "status": "saved", "duplicate": True}
    except (sqlite3.Error, ValueError) as error:
        raise HTTPException(status_code=500, detail="Не удалось сохранить предложение") from error
    return {"id": identifier, "createdAt": created_at, "status": "saved", "duplicate": False}


@app.get("/api/proposals/{proposal_id}")
async def get_proposal(proposal_id: str) -> dict:
    try:
        normalized_id = str(UUID(proposal_id))
    except ValueError as error:
        raise HTTPException(status_code=404, detail="Предложение не найдено") from error
    with connect_db() as connection:
        row = connection.execute("SELECT id, created_at, payload FROM proposals WHERE id = ?", (normalized_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Предложение не найдено")
    return {"id": row["id"], "createdAt": row["created_at"], "payload": json.loads(row["payload"])}
