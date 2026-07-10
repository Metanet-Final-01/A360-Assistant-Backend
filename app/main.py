"""앱 조립 — CORS, lifespan, 미들웨어, 라우터 등록, 정적 콘솔 마운트.

라우트 로직은 app/api/*, 미들웨어는 app/core/*에 둔다 (이 파일은 조립만).
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.debug import router as debug_router
from app.api.documents import router as documents_router
from app.api.rag import router as rag_router
from app.api.sessions import router as sessions_router
from app.core.errors import install_error_handlers
from app.core.http_logging import register_http_logging

load_dotenv()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # DB 없이도 앱은 기동돼야 한다 (프론트 로컬 개발 등) — 실패는 경고만 남긴다.
    # Alembic으로 스키마를 head까지 올린다 (신규 DB는 전체 생성, 최신 DB는 no-op).
    try:
        from app.db import run_migrations

        run_migrations()
        logger.info("DB 마이그레이션 완료 (alembic head)")
    except Exception as e:  # noqa: BLE001
        logger.warning("DB 마이그레이션 실패 (앱은 계속 기동): %s", e)
    # tiktoken 인코더 워밍업(RPA-86) — 최초 get_encoding은 원격 BPE 다운로드라 요청 경로에서
    # 부르면 이벤트 루프를 막는다. 백그라운드 스레드로 미리 로드 (실패 시 문자 폴백으로 동작).
    import threading

    from app.api.sessions import warmup_token_encoder

    threading.Thread(target=warmup_token_encoder, daemon=True).start()
    yield


app = FastAPI(title="A360 Assistant Backend", version="0.1.0", lifespan=lifespan)

frontend_origins = [
    origin.strip()
    for origin in os.getenv(
        "FRONTEND_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]

# Vercel gives every deployment a new hash-suffixed URL (e.g.
# a360-assistant-frontend-<hash>-a360-assistant.vercel.app), so a fixed
# FRONTEND_ORIGINS entry breaks on each redeploy. Allow any deployment of
# this Vercel project via regex instead of chasing the hash by hand.
frontend_origin_regex = os.getenv(
    "FRONTEND_ORIGIN_REGEX",
    r"https://a360-assistant-frontend-.*-a360-assistant\.vercel\.app",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=frontend_origins,
    allow_origin_regex=frontend_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_http_logging(app)
install_error_handlers(app)

app.include_router(auth_router)
app.include_router(documents_router)
app.include_router(rag_router)
app.include_router(debug_router)
app.include_router(sessions_router)
app.include_router(admin_router)


class EchoRequest(BaseModel):
    message: str


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "A360 Assistant Backend",
        "environment": os.getenv("APP_ENV", "development"),
        "status": "ok",
    }


@app.get("/api/message")
def message() -> dict[str, str]:
    return {
        "service": "A360 Assistant Backend",
        "environment": os.getenv("APP_ENV", "development"),
        "message": "Frontend and backend are connected.",
    }


@app.post("/api/echo")
def echo(payload: EchoRequest) -> dict[str, str]:
    return {
        "received": payload.message,
        "reply": f"Backend received: {payload.message}",
    }


@app.get("/api/health")
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy"}


_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/debug", StaticFiles(directory=_STATIC_DIR, html=True), name="debug")
