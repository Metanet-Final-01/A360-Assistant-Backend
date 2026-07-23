"""RAG 검색 경로 진단 — debug·admin이 공유하는 단일 판정 소스 (RPA-232/RPA-270).

두 층위:
- service_status(): 설정·인프라 도달성 요약. **외부 임베딩 API 실호출 없음**(비용 X).
- run_live_probe(): 검색 critical path를 실제로 태운다 — 임베딩 실호출(캐시 우회)·pgvector 쿼리·
  OpenSearch 도달성. 실패는 **error_type으로 분류**해 반환하고 **원문 예외는 서버 로그로만** 남긴다
  (응답에 Secret·전체 DSN·host·credential 미노출 — codex 리뷰).

debug 라우터와 admin 엔드포인트가 이 함수들을 **공유**해 판정이 갈리지 않게 한다.
"""

import logging

logger = logging.getLogger(__name__)


def _classify_embedding_error(exc: Exception) -> str:
    """임베딩 실호출 실패를 응답용 error_type으로 분류(원문 미노출)."""
    import httpx

    if isinstance(exc, RuntimeError) and "API_KEY" in str(exc):
        return "api_key_missing"
    if isinstance(exc, httpx.TimeoutException):
        return "connection_timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connection_failed"
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return "authentication_failed"
        if code == 429:
            return "rate_limited"
        if code >= 500:
            return "provider_error"
        return "http_error"
    return "unknown"


def _classify_db_error(exc: Exception) -> str:
    """pgvector 연결/쿼리 실패를 error_type으로 분류(원문 미노출)."""
    msg = str(exc).lower()
    try:
        import psycopg

        if isinstance(exc, psycopg.errors.UndefinedTable):
            return "schema_missing"
        if isinstance(exc, psycopg.errors.DataError):
            return "dimension_mismatch"
        if isinstance(exc, psycopg.OperationalError):
            return "connection_timeout" if "timeout" in msg else "connection_failed"
    except ImportError:
        pass
    if "does not match" in msg or "dimension" in msg:
        return "dimension_mismatch"
    if "timeout" in msg:
        return "connection_timeout"
    return "unknown"


def _classify_os_error(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "timeout" in name or "timeout" in msg:
        return "connection_timeout"
    if "auth" in name or "401" in msg or "403" in msg:
        return "authentication_failed"
    if "connection" in name or "connect" in msg:
        return "connection_failed"
    return "unknown"


def _opensearch_status() -> dict:
    """OpenSearch 도달성 — 실패해도 error_type만(host 미노출)."""
    from app.rag.store import opensearch_client

    try:
        client = opensearch_client.connect()
        health = client.cluster.health(request_timeout=3)
        return {"reachable": True, "cluster_status": health.get("status")}
    except Exception as e:  # noqa: BLE001
        logger.warning("rag diag: OpenSearch 도달 실패", exc_info=True)
        return {"reachable": False, "error_type": _classify_os_error(e)}


def service_status() -> dict:
    """외부 임베딩 API 실호출 없이 설정·인프라 도달성만 본다(GET 용).

    ⚠️ 임베딩은 키 설정 여부(api_key_configured)만 본다 — 키가 있어도 무효·egress 차단이면
    검색은 죽는다. 실제 도달성은 run_live_probe()로 확인한다(RPA-232 "가짜 초록불").
    host·DSN 같은 배포 세부는 응답에 넣지 않는다.
    """
    from app.rag import config
    from app.rag.store import db

    status: dict = {}
    try:
        conn = db.connect(connect_timeout=5)
        conn.close()
        status["database"] = {"reachable": True}
    except Exception as e:  # noqa: BLE001
        logger.warning("rag diag: DB 연결 실패", exc_info=True)
        status["database"] = {"reachable": False, "error_type": _classify_db_error(e)}

    status["opensearch"] = _opensearch_status()
    status["embedding"] = {
        "provider": config.EMBEDDING_PROVIDER,
        "model": config.EMBEDDING_MODEL,
        "api_key_configured": bool(
            config.VOYAGE_API_KEY if config.EMBEDDING_PROVIDER == "voyage" else config.OPENAI_API_KEY
        ),
        "live_check": "키 설정 여부만 — 실제 도달성은 POST /api/admin/rag/probe",
    }
    status["reranker"] = {
        "model": config.RERANK_MODEL,
        "api_key_configured": bool(config.VOYAGE_API_KEY),
    }
    return status


def run_live_probe(*, timeout: float = 5.0) -> dict:
    """검색 critical path 실호출 — 임베딩(캐시 우회)→pgvector, + OpenSearch 도달성.

    SEARCH_UNAVAILABLE의 원인 단계를 격리한다(RPA-232). 임베딩이 죽으면 벡터쿼리는 skip.
    실패는 error_type만 반환하고 원문 예외는 서버 로그로만 남긴다(응답에 credential/DSN/host 미노출).
    """
    from app.rag import config
    from app.rag.retrieval.embed import embed_query_live
    from app.rag.store import db

    result: dict = {"embedding_provider": config.EMBEDDING_PROVIDER}

    probe_vec = None
    try:
        probe_vec = embed_query_live("rag search healthcheck probe", timeout=timeout)
        result["embedding"] = {"reachable": True, "dim": len(probe_vec)}
    except Exception as e:  # noqa: BLE001 — 원문은 로그, 응답엔 분류만
        logger.warning("rag probe: 임베딩 실호출 실패", exc_info=True)
        result["embedding"] = {"reachable": False, "error_type": _classify_embedding_error(e)}

    if probe_vec is None:
        result["vector_query"] = {"skipped": "embedding_unreachable"}
    else:
        try:
            conn = db.connect(connect_timeout=5)
            try:
                hits = db.search(conn, probe_vec, limit=1)
            finally:
                conn.close()
            result["vector_query"] = {"reachable": True, "hits": len(hits)}
        except Exception as e:  # noqa: BLE001
            logger.warning("rag probe: pgvector 쿼리 실패", exc_info=True)
            result["vector_query"] = {"reachable": False, "error_type": _classify_db_error(e)}

    result["opensearch"] = _opensearch_status()
    return result
