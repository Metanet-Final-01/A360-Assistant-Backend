"""앱 조립 — CORS, lifespan, 미들웨어, 라우터 등록, 정적 콘솔 마운트.

라우트 로직은 app/api/*, 미들웨어는 app/core/*에 둔다 (이 파일은 조립만).
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Response
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
    # 관리자 부트스트랩(RPA-118) — ADMIN_EMAILS 시드 계정을 is_admin으로 백필(멱등).
    # migration 직후 재로그인을 기다리지 않고 여기서 승격 (DB 미가동이면 경고만).
    try:
        from app.api.auth import backfill_seed_admins

        promoted = backfill_seed_admins()
        if promoted:
            logger.info("관리자 부트스트랩: %d개 계정 승격", promoted)
    except Exception as e:  # noqa: BLE001
        logger.warning("관리자 부트스트랩 실패 (앱은 계속 기동): %s", e)
    # tiktoken 인코더 워밍업(RPA-86) — 최초 get_encoding은 원격 BPE 다운로드라 요청 경로에서
    # 부르면 이벤트 루프를 막는다. 백그라운드 스레드로 미리 로드 (실패 시 문자 폴백으로 동작).
    import threading

    from app.api.sessions import warmup_token_encoder

    threading.Thread(target=warmup_token_encoder, daemon=True).start()
    # 관측 전용 DB(RPA-90) — OBSERVABILITY_DATABASE_URL 설정 시 테이블 보장 (실패해도 기동)
    from app.core.observability_db import ensure_observability_schema

    ensure_observability_schema()
    # 일별 롤업 스케줄러(RPA-104) — metrics_daily·usage_daily 재집계 + retention (실패해도 기동)
    from app.core.scheduler import start_scheduler, stop_scheduler

    start_scheduler()
    # /api/rag/search 전용 커넥션 풀(부하테스트로 확인된 요청별-신규연결 병목 대응)
    from app.rag.store.pool import close_pools, open_pools

    await open_pools()
    yield
    stop_scheduler()
    await close_pools()


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


def _check_db(open_session) -> bool:
    """SELECT 1 왕복 성공 여부 — 실패 원인은 로그로만 (health 응답은 ok/fail 단순 유지).

    open_session은 세션을 '만들어서' 반환하는 콜러블 — 팩토리/엔진 생성 실패까지
    try 안에서 잡혀야 /health가 500이 아니라 degraded로 응답한다 (CodeRabbit #177).
    """
    try:
        from sqlalchemy import text

        with open_session() as s:
            s.execute(text("SELECT 1"))
        return True
    except Exception as e:  # noqa: BLE001 — 어떤 실패든 "fail"로 보고하는 게 목적
        logger.warning("health 체크 실패: %s", e)
        return False


def _check_opensearch() -> bool:
    """OpenSearch(BM25) 도달성 — 얕은 _cluster/health 핑(짧은 타임아웃, RPA-156).

    BM25는 보강 신호라 실패해도 degraded(본 검색은 dense로 동작)다. 신선 요청으로 '인프라
    도달성'을 본다 — 앱-전역 클라이언트가 죽어 dense-only로 저하되는 건 여기선 못 잡지만
    (그건 검색 경로의 BM25 실패 로그가 잡는다), Bonsai 자체가 내려간 건 이걸로 드러난다.
    """
    import httpx

    import app.rag.config as rag_config

    host = rag_config.OPENSEARCH_HOST
    auth = (
        (rag_config.OPENSEARCH_USERNAME, rag_config.OPENSEARCH_PASSWORD)
        if rag_config.OPENSEARCH_USERNAME
        else None
    )
    try:
        r = httpx.get(
            f"{host.rstrip('/')}/_cluster/health",
            auth=auth,
            timeout=3.0,
            verify=host.startswith("https"),
        )
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001 — 어떤 실패든 "fail"로 보고
        logger.warning("opensearch health 체크 실패: %s", e)
        return False


@app.get("/api/health")
@app.get("/health")
def health(response: Response) -> dict:
    """의존성 체크 포함 헬스 (RPA-117) — 백오피스 생존 감시 probe의 대상.

    - 앱 DB 실패 → 503 unhealthy: 서비스가 사실상 동작 불가라 probe가 DOWN으로 봐야 한다.
    - 관측 DB 실패 → 200 degraded: 본 기능은 살아 있으니 UP이되, "반쯤 죽은" 상태를 구분.
    """
    import app.db as app_db
    from app.core.observability_db import observability_sessionmaker

    checks = {
        "database": "ok" if _check_db(lambda: app_db.SessionLocal()) else "fail",
        "observability_database": "ok" if _check_db(lambda: observability_sessionmaker()()) else "fail",
        # BM25(OpenSearch) 도달성 — 실패해도 dense 검색은 살아 degraded (RPA-156)
        "opensearch": "ok" if _check_opensearch() else "fail",
    }
    if checks["database"] == "fail":
        status = "unhealthy"
        response.status_code = 503
    elif checks["observability_database"] == "fail" or checks["opensearch"] == "fail":
        status = "degraded"
    else:
        status = "healthy"
    return {
        "status": status,
        "checks": checks,
        # 관측 DB가 공유(Neon)인지 로컬 폴백인지 — 폴백이면 위 체크는 앱 DB와 동일 대상
        "observability_shared": bool(os.getenv("OBSERVABILITY_DATABASE_URL")),
    }


_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/debug", StaticFiles(directory=_STATIC_DIR, html=True), name="debug")
