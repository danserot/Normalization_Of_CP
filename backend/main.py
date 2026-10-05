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
from uuid import UUID

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from .extraction import FORMATS, MAX_BYTES, DocumentError
from .local_model import MODEL_NAME, model_available
from .pipeline import Pipeline
from .annotations import create_annotation_router, initialize_annotations

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "./backend/data/readdocument.sqlite3"))
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
APP_SECRET = os.getenv("APP_SECRET") or secrets.token_hex(32)
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
SESSION_COOKIE = "readdocument_session"
SESSION_LIFETIME = 8 * 60 * 60
pipeline = Pipeline()


class AdditionalField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(max_length=200)
    value: str = Field(max_length=2000)


class CostComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(max_length=500)
    unitPrice: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    lineTotal: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ProposalItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=500)
    quantity: float = Field(gt=0, allow_inf_nan=False)
    unit: str = Field(max_length=50)
    unitPrice: float = Field(ge=0, allow_inf_nan=False)
    lineTotal: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    components: list[CostComponent] = Field(default_factory=list, max_length=10)
    additionalFields: list[AdditionalField] = Field(default_factory=list, max_length=30)


class ProposalData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=500)
    client: str = Field(max_length=500)
    clientContact: str = Field(max_length=500)
    validUntil: str = Field(max_length=200)
    supplier: str = Field(max_length=500)
    currency: str = Field(max_length=30)
    vat: str = Field(max_length=200)
    discount: str = Field(max_length=200)
    delivery: str = Field(max_length=500)
    paymentTerms: str = Field(max_length=1000)
    deliveryTerms: str = Field(max_length=1000)
    warranty: str = Field(max_length=500)
    documentNumber: str = Field(max_length=200)
    documentDate: str = Field(max_length=200)
    documentTotal: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    notes: str = Field(max_length=500_000)
    items: list[ProposalItem] = Field(max_length=2000)
    additionalFields: list[AdditionalField] = Field(default_factory=list, max_length=100)


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


app = FastAPI(title="ReadDocument local extraction API", lifespan=lifespan)
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
    return {"status": "ok", "database": "sqlite", "provider": "local", "model": MODEL_NAME,
            "configured": available, "busy": pipeline.busy}


@app.get("/api/models")
async def get_models(_: None = Depends(require_auth)) -> dict:
    return {"models": [{"id": "local", "name": "Локальное извлечение",
        "description": "Модель определяет структуру КП, проверяет источник и полноту; при недоступности — резервный разбор",
        "size": "CPU · без внешних API", "recommended": True,
        "installed": await asyncio.to_thread(model_available)}]}


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
            if json.loads(models) != ["local"]:
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="Доступен только локальный конвейер")
    if pipeline.busy:
        raise HTTPException(status_code=429, detail="Уже обрабатывается документ. Повторите после завершения", headers={"Retry-After": "5"})
    pipeline.busy = True
    try:
        content = await file.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise HTTPException(status_code=413, detail="Файл должен быть не больше 25 МБ")
        return await pipeline.run(content, filename)
    except DocumentError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        pipeline.busy = False
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
