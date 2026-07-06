"""디버그 전용 엔드포인트 — 로컬 개발 중 /debug 콘솔이 사용한다.

- RAG 파이프라인 단계별 단독 실행(embed/vector/bm25/rerank/search-actions)
- 파이프라인 연결 상태·최근 로그 조회
- 백엔드 프로세스에서 임의 HTTP 요청을 대신 보내는 프록시(SSRF 가드 포함)

프로덕션 노출을 의도하지 않는다. http-request 프록시는 DEBUG_HTTP_CLIENT_ENABLED로만 활성화된다.
"""

import asyncio
import ipaddress
import json
import os
import socket
import time
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(tags=["debug"])


class HttpDebugRequest(BaseModel):
    method: str = "GET"
    url: str
    headers: dict[str, str] = {}
    body: str | None = None
    timeout_seconds: float = 20.0
    follow_redirects: bool = False


class RerankDebugRequest(BaseModel):
    query: str
    documents: list[str]
    top_k: int = 5


async def _resolve_validated_ip(host: str) -> tuple[str | None, str | None]:
    """호스트를 해석하고 모든 IP가 공인망이면 (첫 IP, None), 아니면 (None, 사유)를 반환한다.

    반환된 IP를 실제 연결에 그대로 사용해야(핀 고정) DNS 리바인딩을 막을 수 있다 —
    검증 시점과 요청 시점에 호스트를 각각 해석하면 그 사이에 응답이 바뀌어 우회된다.
    조회는 blocking이라 스레드로 넘겨 이벤트 루프를 막지 않는다.
    """
    try:
        # getaddrinfo는 동기 함수 → to_thread로 async 경로를 막지 않게 한다
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except OSError:
        return None, f"호스트를 해석할 수 없습니다: {host}"
    addrs = {info[4][0] for info in infos}
    for addr in addrs:
        ip = ipaddress.ip_address(addr.split("%")[0])
        if not ip.is_global:
            return None, f"내부망 주소({addr})로의 요청은 허용되지 않습니다."
    return next(iter(addrs)), None


def _pin_url_to_ip(url: str, ip: str) -> tuple[str, str]:
    """URL의 호스트를 검증된 IP로 치환하고 (핀 URL, 원래 호스트[:포트])를 반환한다.

    치환된 URL로 연결하되 Host 헤더/SNI는 원래 호스트로 유지해 재해석을 막는다.
    """
    parsed = urlparse(url)
    netloc_host = f"[{ip}]" if ":" in ip else ip
    if parsed.port:
        netloc_host += f":{parsed.port}"
    original_host = parsed.netloc.split("@")[-1]  # userinfo 제거
    return parsed._replace(netloc=netloc_host).geturl(), original_host


@router.post("/api/debug/http-request")
async def debug_http_request(payload: HttpDebugRequest, request: Request) -> dict:
    """Debug page helper: send an arbitrary HTTP request from the backend process."""
    import httpx

    # 명시적 opt-in만 허용한다. 이전에는 APP_ENV가 development(기본값)면 열렸는데,
    # 배포 환경에서 APP_ENV를 설정하지 않으면 프로덕션에 SSRF 프록시가 열리는
    # 구조였다. 로컬 개발은 .env의 DEBUG_HTTP_CLIENT_ENABLED=true로 켠다.
    debug_enabled = os.getenv("DEBUG_HTTP_CLIENT_ENABLED", "").lower() == "true"
    if not debug_enabled:
        raise HTTPException(status_code=403, detail="Debug HTTP client is disabled for this environment.")

    method = payload.method.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        raise HTTPException(status_code=400, detail=f"Unsupported method: {payload.method}")

    url = payload.url.strip()
    request_url = url
    extensions: dict = {}
    is_self_call = url.startswith("/")
    if is_self_call:
        # 자기 자신(이 백엔드)으로의 상대경로 호출 — 디버그 콘솔의 주 용도라 허용
        url = request_url = str(request.base_url).rstrip("/") + url
    elif url.startswith("http://") or url.startswith("https://"):
        parsed = urlparse(url)
        if not parsed.hostname:
            raise HTTPException(status_code=400, detail="URL에서 호스트를 파싱할 수 없습니다.")
        ip, reason = await _resolve_validated_ip(parsed.hostname)
        if reason:
            raise HTTPException(status_code=400, detail=reason)
        # 검증된 IP로 연결을 고정(핀)해 DNS 리바인딩을 차단한다. Host 헤더와 SNI는
        # 원래 호스트로 유지해 라우팅·인증서 검증이 정상 동작하게 한다.
        request_url, original_host = _pin_url_to_ip(url, ip)
        payload.headers.setdefault("Host", original_host.split(":")[0])
        if parsed.scheme == "https":
            extensions["sni_hostname"] = parsed.hostname
    else:
        raise HTTPException(status_code=400, detail="URL must be absolute or start with '/'.")

    headers = {
        str(key): str(value)
        for key, value in payload.headers.items()
        if key.lower() not in {"content-length"}
    }
    timeout = max(1.0, min(payload.timeout_seconds, 60.0))
    started = time.perf_counter()
    try:
        # 리다이렉트는 따라가지 않는다 — 검증을 우회해 내부망으로 재유도될 수 있어
        # (공인 URL → 169.254.169.254 리다이렉트) 응답만 그대로 돌려준다.
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            req = client.build_request(
                method,
                request_url,
                headers=headers,
                content=payload.body if payload.body and method not in {"GET", "HEAD"} else None,
                extensions=extensions or None,
            )
            response = await client.send(req)
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


@router.get("/api/rag/debug/embed")
def debug_embed(text: str) -> dict:
    """임베딩 단계만 단독 실행 (벡터 전체는 너무 커서 차원 수 + 앞부분만 반환)."""
    from app.rag.retrieval.embed import embed_query

    try:
        vector = embed_query(text)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"text": text, "dim": len(vector), "preview": vector[:8]}


@router.get("/api/rag/debug/vector-search")
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


@router.get("/api/rag/debug/bm25-search")
def debug_bm25_search(q: str, size: int = 5) -> dict:
    """OpenSearch BM25 검색 단계만 단독 실행 (RRF/rerank 없음)."""
    from app.rag.store import opensearch_client

    try:
        client = opensearch_client.connect()
        results = opensearch_client.keyword_search(client, q, size=size)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"OpenSearch 오류: {e}")
    return {"query": q, "results": results}


@router.post("/api/rag/debug/rerank")
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


@router.get("/api/rag/debug/search-actions")
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


@router.get("/api/rag/logs/recent")
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


@router.get("/api/rag/debug/status")
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
