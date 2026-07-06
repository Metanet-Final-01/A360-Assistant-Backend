import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
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
    from app.rag.observability import new_request_id
    from app.rag.store import db, opensearch_client
    from app.rag.retrieval.hybrid_search import search as hybrid_search

    new_request_id()  # 이 요청의 embed_query/vector_search/bm25_search/rerank 로그를 하나로 묶는다
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
