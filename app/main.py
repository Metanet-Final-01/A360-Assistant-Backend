import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


app = FastAPI(title="A360 Assistant Backend", version="0.1.0")

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

# 요청을 가로채 모든 HTTP 호출을 AOP 스타일로 기록한다 (각 라우트 코드는 손대지 않음).
# /debug, /api/rag/logs/recent는 디버그 콘솔이 1.5초마다 스스로 폴링하는 경로라 제외한다
# (그러지 않으면 로그가 자기 자신의 폴링 기록으로 도배된다).
_SKIP_HTTP_LOG_PREFIXES = ("/api/rag/logs/recent", "/debug")


@app.middleware("http")
async def log_http_requests(request: Request, call_next):
    from app.rag.observability import log_event, new_request_id

    if request.url.path.startswith(_SKIP_HTTP_LOG_PREFIXES):
        return await call_next(request)

    new_request_id()  # 이 HTTP 요청 안의 모든 파이프라인 로그(embed_query 등)를 이 id로 묶는다
    started_at = datetime.now(timezone.utc)
    start = time.perf_counter()
    common = {
        "method": request.method,
        "path": request.url.path,
        "query": str(request.url.query),
        "client": request.client.host if request.client else None,
        "started_at": started_at.isoformat(),
    }
    try:
        response = await call_next(request)
    except Exception as exc:
        log_event(
            "http_request",
            **common,
            status="error",
            error_type=type(exc).__name__,
            error_message=str(exc),
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
            ended_at=datetime.now(timezone.utc).isoformat(),
        )
        raise

    log_event(
        "http_request",
        **common,
        status_code=response.status_code,
        status="ok" if response.status_code < 400 else "error",
        duration_ms=round((time.perf_counter() - start) * 1000, 2),
        ended_at=datetime.now(timezone.utc).isoformat(),
    )
    return response


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


@app.get("/api/rag/search")
def rag_search(q: str, limit: int = 5, mode: str = "hybrid_rerank") -> dict:
    """A360 패키지/액션 지식 하이브리드(RRF) + Voyage Reranker 검색.

    app/rag 파이프라인으로 적재된 데이터를 사용한다.
    mode: vector(기존 방식) / hybrid(RRF만) / hybrid_rerank(기본, RRF+Voyage Reranker)
    """
    from app.rag.store import db, opensearch_client
    from app.rag.retrieval.hybrid_search import search as hybrid_search

    # request_id는 log_http_requests 미들웨어가 이미 생성해둠 — 여기서 다시 만들지 않는다
    try:
        conn = db.connect()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DB 연결 실패: {e}")
    try:
        os_client = opensearch_client.connect()
        try:
            results = hybrid_search(conn, os_client, q, limit=limit, mode=mode)
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
    finally:
        conn.close()
    return {"query": q, "results": results}


@app.get("/api/rag/logs/recent")
def rag_logs_recent(limit: int = 100) -> dict:
    """검색/리랭커 파이프라인 최근 로그 — /debug 페이지가 폴링해서 실시간처럼 보여준다."""
    from app.rag import config

    log_files = sorted(config.LOG_DIR.glob("rag-*.jsonl")) if config.LOG_DIR.exists() else []
    if not log_files:
        return {"logs": []}

    lines: list[str] = []
    for path in reversed(log_files):
        lines = path.read_text(encoding="utf-8").splitlines() + lines
        if len(lines) >= limit:
            break

    records = [json.loads(line) for line in lines[-limit:]]
    records.reverse()  # 최신 순
    return {"logs": records}


_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/debug", StaticFiles(directory=_STATIC_DIR, html=True), name="debug")
