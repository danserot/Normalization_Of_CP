import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .document_limits import FORMATS, MAX_BYTES, DocumentError
from .openai_model import model_available
from .openai_client import configured_model
from .pipeline import PipelinePool, QueueFullError
from .annotations import create_annotation_router, initialize_annotations
from .errors import ProviderError
from .proposal_models import ProposalData

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "./backend/data/readdocument.sqlite3"))
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
APP_SECRET = os.getenv("APP_SECRET") or secrets.token_hex(32)
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
SESSION_COOKIE = "readdocument_session"
SESSION_LIFETIME = 8 * 60 * 60
pipeline = PipelinePool()


class ProposalSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal: ProposalData
    sources: list[dict[str, object]] = Field(max_length=50)


class LoginRequest(BaseModel):
    password: str = Field(max_length=500)

@asynccontextmanager
async def lifespan(app):
    yield
    pipeline.stop()


app = FastAPI(title="ReadDocument сервис extraction API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
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
        initialize_annotations(connection)


initialize_db()


def session_signature(value: str) -> str:
    return hmac.new(APP_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()


def authenticated(request: Request) -> bool:
    if not APP_PASSWORD:
        return True
    token = request.cookies.get(SESSION_COOKIE, "")
    try:
        expires, nonce, signature = token.split(".", 2)
        unsigned = f"{expires}.{nonce}"
        return int(expires) > int(time.time()) and hmac.compare_digest(signature, session_signature(unsigned))
    except (ValueError, TypeError):
        return False


def require_auth(request: Request) -> None:
    if not authenticated(request):
        raise HTTPException(status_code=401, detail="Войдите, чтобы продолжить")


app.include_router(create_annotation_router(connect_db, require_auth, pipeline))


@app.get("/api/health")
async def health() -> dict:
    available = await asyncio.to_thread(model_available)
    return {"status": "ok", "database": "sqlite", "provider": "openai", "model": configured_model(),
            "configured": available, "device": "api", "apiConnectionVerified": False,
            "busy": pipeline.busy, "queued": max(0, pipeline.pending - pipeline.active),
            "workers": len(pipeline.workers), "active": pipeline.active,
            "queueLimit": pipeline.max_pending, "extractionMode": "model_direct",
            "localParsers": False, "ruleFallback": False,
            "architecture": "model-direct-v4"}


@app.get("/api/models")
async def get_models(_: None = Depends(require_auth)) -> dict:
    configured = await asyncio.to_thread(model_available)
    return {"models": [{"id": "openai", "name": configured_model(),
        "description": "Исходный файл читает модель и возвращает готовое КП. Python проверяет формат и арифметику. Значения и цитаты требуют сверки с оригиналом.",
        "size": "API · без локальных моделей", "recommended": True,
        "configured": configured, "installed": configured}]}


@app.get("/api/session")
async def get_session(request: Request) -> dict:
    return {"authenticated": authenticated(request), "passwordRequired": bool(APP_PASSWORD)}


@app.post("/api/login")
async def login(credentials: LoginRequest, response: Response) -> dict:
    if not APP_PASSWORD:
        return {"authenticated": True, "passwordRequired": False}
    if not hmac.compare_digest(credentials.password, APP_PASSWORD):
        raise HTTPException(status_code=401, detail="Неверный пароль")
    expires = int(time.time()) + SESSION_LIFETIME
    unsigned = f"{expires}.{secrets.token_urlsafe(24)}"
    response.set_cookie(
        key=SESSION_COOKIE,
        value=f"{unsigned}.{session_signature(unsigned)}",
        max_age=SESSION_LIFETIME,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        path="/",
    )
    return {"authenticated": True, "passwordRequired": True}


@app.post("/api/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=COOKIE_SECURE, samesite="strict")
    return {"authenticated": not bool(APP_PASSWORD)}


@app.post("/api/extract")
async def extract(file: UploadFile = File(...), models: str = Form(default=""), _: None = Depends(require_auth)) -> dict:
    filename = Path(file.filename or "document").name
    if Path(filename).suffix.lower() not in FORMATS:
        raise HTTPException(status_code=415, detail="Неподдерживаемый формат файла")
    if models:
        try:
            if json.loads(models) != ["openai"]:
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="Доступен только Сервис распознавания")
    if not model_available():
        await file.close()
        raise HTTPException(status_code=503, detail="Сервис распознавания не настроен. Задайте OPENAI_API_KEY в .env и перезапустите backend.")
    try:
        content = await file.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise HTTPException(status_code=413, detail=f"Файл должен быть не больше {MAX_BYTES // 1024**2} МБ")
        return await pipeline.run(content, filename)
    except QueueFullError as error:
        raise HTTPException(status_code=429, detail=str(error), headers={"Retry-After": "5"}) from error
    except ProviderError as error:
        return JSONResponse(status_code=error.http_status,
            content={"detail": str(error), "error": error.public_info()})
    except DocumentError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        await file.close()


@app.post("/api/proposals")
async def save_proposal(payload: ProposalSubmission, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), _: None = Depends(require_auth)) -> dict:
    key = idempotency_key or str(UUID(bytes=os.urandom(16), version=4))
    try:
        key = str(UUID(key))
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Некорректный ключ отправки") from error
    identifier = str(UUID(bytes=os.urandom(16), version=4))
    created_at = datetime.now(timezone.utc).isoformat()
    serialized = json.dumps(payload.model_dump(mode="json", exclude_unset=True), ensure_ascii=False, allow_nan=False)
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
async def get_proposal(proposal_id: str, _: None = Depends(require_auth)) -> dict:
    try:
        normalized_id = str(UUID(proposal_id))
    except ValueError as error:
        raise HTTPException(status_code=404, detail="Предложение не найдено") from error
    with connect_db() as connection:
        row = connection.execute("SELECT id, created_at, payload FROM proposals WHERE id = ?", (normalized_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Предложение не найдено")
    return {"id": row["id"], "createdAt": row["created_at"], "payload": json.loads(row["payload"])}
