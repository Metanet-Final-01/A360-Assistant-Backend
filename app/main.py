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
from app.api.agent import router as agent_router
from app.api.assurance_writer import router as assurance_writer_router
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
    # 단 그 결과는 상태로 남긴다(RPA-222): /health/live가 이 플래그로 503을 줘서,
    # 마이그레이션이 실패한 인스턴스가 ALB 타겟그룹에 들어가는 걸 막는다. 이전엔
    # 경고만 남기고 정상 부팅해 스키마 깨진 채 트래픽을 받았다.
    # (매 기동마다 False에서 시작 — 재기동 시 직전 성공이 남아 있으면 안 된다.)
    app.state.migrations_ok = False
    # Alembic으로 스키마를 head까지 올린다 (신규 DB는 전체 생성, 최신 DB는 no-op).
    try:
        from app.db import run_migrations

        run_migrations()
        app.state.migrations_ok = True
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
app.include_router(agent_router)
app.include_router(assurance_writer_router)


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
def compute_health() -> dict:
    """의존성을 실제로 찔러 헬스를 판정한다 (RPA-117). 순수 함수 — 응답과 무관.

    - 앱 DB 실패 → unhealthy: 서비스가 사실상 동작 불가.
    - 관측 DB·OpenSearch 실패 → degraded: 본 기능은 살아 있으나 "반쯤 죽은" 상태.

    ⚠️ 라우트에서 분리한 이유 (RPA-189): 알림 잡도 이 판정이 필요한데, **복사하면 반드시
    갈린다** — 한쪽에 체크를 추가하고 다른 쪽을 잊으면 "/health는 degraded인데 알림은 조용"이
    된다. 가드가 읽는 것과 동작이 읽는 것은 같은 표현식이어야 한다(CONVENTIONS §9).
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


@app.get("/health")
def health(response: Response) -> dict:
    """의존성 체크 포함 헬스 (RPA-117) — 백오피스 생존 감시 probe의 대상.

    판정은 compute_health()가 한다(알림 잡과 공유). 여기선 HTTP 상태코드만 매핑한다:
    앱 DB가 죽으면 503 — probe가 DOWN으로 봐야 한다. degraded는 200(UP이되 반쯤 죽음).
    ⚠️ ALB 헬스체크는 여기가 아니라 /health/live다 (RPA-222) — 이유는 그쪽 docstring.
    """
    result = compute_health()
    if result["status"] == "unhealthy":
        response.status_code = 503
    return result


@app.get("/health/live")
def health_live(response: Response) -> dict:
    """ALB 타겟그룹·컨테이너 헬스체크 전용 — **인스턴스-로컬 판정만** 한다 (RPA-222).

    깊은 체크(/health)를 ALB에 물리면 양방향으로 틀린다:
    - 공유 의존성(Neon·OpenSearch) 장애는 전 인스턴스가 **동시에** 실패하는데,
      HealthCheckType: ELB라 ASG가 멀쩡한 인스턴스를 전부 교체한다 — 장애는 그대로에
      재생성 루프만 얹힌다. 인스턴스 교체로 고칠 수 있는 문제만 봐야 한다.
    - 의존성 3개 동기 호출은 실측 4.6초로 헬스체크 타임아웃 5초와 여유가 400ms다.

    그래서 여기선 "이 인스턴스의 부팅이 성공했나"만 본다: 마이그레이션이 실패한
    인스턴스는 이후 쿼리에서 터지므로 503으로 타겟그룹 진입을 막는다.
    """
    ok = bool(getattr(app.state, "migrations_ok", False))
    if not ok:
        response.status_code = 503
    return {"status": "alive" if ok else "boot_failed"}


_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/debug", StaticFiles(directory=_STATIC_DIR, html=True), name="debug")
