import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.api.documents import router as documents_router

load_dotenv()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # DB 없이도 앱은 기동돼야 한다 (프론트 로컬 개발 등) — 실패는 경고만 남긴다
    try:
        from app.db import init_db

        init_db()
        logger.info("DB 테이블 초기화 완료")
    except Exception as e:  # noqa: BLE001
        logger.warning("DB 초기화 실패 (앱은 계속 기동): %s", e)
    yield


app = FastAPI(title="A360 Assistant Backend", version="0.1.0", lifespan=lifespan)

app.include_router(documents_router)

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


class HttpDebugRequest(BaseModel):
    method: str = "GET"
    url: str
    headers: dict[str, str] = {}
    body: str | None = None
    timeout_seconds: float = 20.0
    follow_redirects: bool = False


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


@app.post("/api/debug/http-request")
async def debug_http_request(payload: HttpDebugRequest, request: Request) -> dict:
    """Debug page helper: send an arbitrary HTTP request from the backend process."""
    import httpx

    debug_enabled = os.getenv("DEBUG_HTTP_CLIENT_ENABLED", "").lower() == "true"
    local_env = os.getenv("APP_ENV", "development").lower() in {"development", "local", "test"}
    if not (debug_enabled or local_env):
        raise HTTPException(status_code=403, detail="Debug HTTP client is disabled for this environment.")

    method = payload.method.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        raise HTTPException(status_code=400, detail=f"Unsupported method: {payload.method}")

    url = payload.url.strip()
    if url.startswith("/"):
        url = str(request.base_url).rstrip("/") + url
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="URL must be absolute or start with '/'.")

    headers = {
        str(key): str(value)
        for key, value in payload.headers.items()
        if key.lower() not in {"host", "content-length"}
    }
    timeout = max(1.0, min(payload.timeout_seconds, 60.0))
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=payload.follow_redirects) as client:
            response = await client.request(
                method,
                url,
                headers=headers,
                content=payload.body if payload.body and method not in {"GET", "HEAD"} else None,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"HTTP request failed: {exc}")

    content_type = response.headers.get("content-type", "")
    try:
        response_body = response.json() if "application/json" in content_type else response.text
    except Exception:
        response_body = response.text

    return {
        "request": {
            "method": method,
            "url": url,
            "headers": headers,
            "body": payload.body if method not in {"GET", "HEAD"} else None,
        },
        "response": {
            "status_code": response.status_code,
            "reason_phrase": response.reason_phrase,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "headers": dict(response.headers),
            "body": response_body,
        },
    }


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


class RerankDebugRequest(BaseModel):
    query: str
    documents: list[str]
    top_k: int = 5


@app.get("/api/rag/debug/embed")
def debug_embed(text: str) -> dict:
    """임베딩 단계만 단독 실행 (벡터 전체는 너무 커서 차원 수 + 앞부분만 반환)."""
    from app.rag.retrieval.embed import embed_query

    try:
        vector = embed_query(text)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"text": text, "dim": len(vector), "preview": vector[:8]}


@app.get("/api/rag/debug/vector-search")
def debug_vector_search(q: str, limit: int = 5) -> dict:
    """pgvector 코사인 유사도 검색 단계만 단독 실행 (RRF/rerank 없음)."""
    from app.rag.retrieval.embed import embed_query
    from app.rag.store import db

    try:
        query_embedding = embed_query(q)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    try:
        conn = db.connect()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DB 연결 실패: {e}")
    try:
        results = db.search(conn, query_embedding, limit=limit)
    finally:
        conn.close()
    return {"query": q, "results": results}


@app.get("/api/rag/debug/bm25-search")
def debug_bm25_search(q: str, size: int = 5) -> dict:
    """OpenSearch BM25 검색 단계만 단독 실행 (RRF/rerank 없음)."""
    from app.rag.store import opensearch_client

    try:
        client = opensearch_client.connect()
        results = opensearch_client.keyword_search(client, q, size=size)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"OpenSearch 오류: {e}")
    return {"query": q, "results": results}


@app.post("/api/rag/debug/rerank")
def debug_rerank(payload: RerankDebugRequest) -> dict:
    """Voyage Reranker 단계만 단독 실행 — 임의의 문서 목록을 직접 넣어 재정렬 결과를 확인."""
    from app.rag.retrieval.rerank import rerank

    try:
        reranked = rerank(payload.query, payload.documents, top_k=payload.top_k)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {
        "query": payload.query,
        "results": [
            {"index": item["index"], "relevance_score": item["relevance_score"], "document": payload.documents[item["index"]]}
            for item in reranked
        ],
    }


# 공통 필수 필드 + source_type별로만 의미 있는 필드 — doc_page/bot_example은
# 특정 패키지·액션 하나에 매인 문서가 아니라서 package_name/action_name이
# 원래부터 NULL이다(DB 컬럼 자체가 nullable, normalize.py가 그렇게 만듦).
# 그걸 "누락"으로 잘못 표시하지 않도록 source_type별 필수 필드를 따로 둔다.
_COMMON_REQUIRED_FIELDS = ["id", "source_type", "title", "content", "score"]
_REQUIRED_FIELDS_BY_SOURCE_TYPE = {
    "action_schema": ["package_name", "action_name"],
    "package_overview": ["package_name"],
}


@app.get("/api/rag/debug/search-actions")
def debug_search_actions(q: str, k: int = 5, source_types: str | None = None) -> dict:
    """docs/INTERFACES.md 계약 함수 app.services.rag.search_actions()를 그대로 호출한다.

    Agent 담당의 app/agent/retrieval.py가 FakeRetriever를 이걸로 교체했을 때 받게 될
    결과와 100% 동일하다. source_type별 필수 필드가 실제로 채워졌는지
    _missing_contract_fields로 같이 알려준다 (url은 어느 source_type이든 선택 필드).
    """
    from app.services.rag import search_actions

    types = [t.strip() for t in source_types.split(",") if t.strip()] if source_types else None
    try:
        results = search_actions(q, k=k, source_types=types)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    checked = []
    for r in results:
        required = _COMMON_REQUIRED_FIELDS + _REQUIRED_FIELDS_BY_SOURCE_TYPE.get(r.get("source_type"), [])
        checked.append({**r, "_missing_contract_fields": [f for f in required if r.get(f) is None]})
    return {"query": q, "k": k, "source_types": types, "results": checked}


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

    records = []
    for line in lines[-limit:]:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 쓰는 도중 읽어서 잘린 마지막 줄 등 — 건너뛰고 계속 (요청 전체를 실패시키지 않음)
    records.reverse()  # 최신 순
    return {"logs": records}


@app.get("/api/rag/debug/status")
def rag_debug_status() -> dict:
    """DB/OpenSearch/임베딩/리랭커 실시간 연결 상태 — 로컬 개발 중 코드가 실제로 각 서비스에
    잘 붙어 있는지 한눈에 점검하기 위한 디버그 전용 엔드포인트."""
    from app.rag import config
    from app.rag.store import db, opensearch_client

    status: dict = {}

    try:
        conn = db.connect()
        conn.close()
        status["database"] = {"reachable": True}
    except Exception as e:
        status["database"] = {"reachable": False, "error": str(e)}

    try:
        client = opensearch_client.connect()
        health = client.cluster.health(request_timeout=3)
        status["opensearch"] = {
            "reachable": True,
            "host": config.OPENSEARCH_HOST,
            "cluster_status": health.get("status"),
        }
    except Exception as e:
        status["opensearch"] = {
            "reachable": False,
            "host": config.OPENSEARCH_HOST,
            "error": str(e),
        }

    status["embedding"] = {
        "provider": config.EMBEDDING_PROVIDER,
        "model": config.EMBEDDING_MODEL,
        "api_key_configured": bool(config.VOYAGE_API_KEY if config.EMBEDDING_PROVIDER == "voyage" else config.OPENAI_API_KEY),
    }
    status["reranker"] = {
        "model": config.RERANK_MODEL,
        "api_key_configured": bool(config.VOYAGE_API_KEY),
    }
    return status


_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/debug", StaticFiles(directory=_STATIC_DIR, html=True), name="debug")
