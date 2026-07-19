"""임베딩 생성. Anthropic은 임베딩 API가 없어 Voyage AI(공식 권장) 또는 OpenAI를 사용한다."""

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from .. import config
from ..observability import log_call, log_event

logger = logging.getLogger(__name__)

# 한국어는 문자당 토큰 수가 많아(최대 ~2토큰/자) 보수적으로 자른다: 4000자 ≈ 최대 8k 토큰
_BATCH_SIZE = 16
_MAX_CHARS = 4000


def _record_embed_usage(data: dict) -> None:
    """임베딩 응답의 토큰을 llm_usage에 기록한다 (component=rag_embed).

    임베딩은 사용자와 무관한 인프라(적재·검색)이므로 system 사용으로 귀속한다.
    core.llm은 lazy import하고, 기록 실패가 임베딩 자체를 막지 않도록 best-effort로 삼킨다.
    """
    try:
        usage = data.get("usage") or {}
        tokens = usage.get("total_tokens") or usage.get("prompt_tokens") or 0
        if not tokens:
            return
        from app.core.llm import record_usage, usage_context

        with usage_context(component="rag_embed"):  # actor_type=system, user_id=None
            record_usage(
                purpose="embed", model=config.EMBEDDING_MODEL,
                input_tokens=int(tokens), output_tokens=0,
            )
    except Exception:  # noqa: BLE001 — 사용량 기록 실패가 임베딩을 막으면 안 됨
        logger.debug("임베딩 사용량 기록 실패 (무시)", exc_info=True)


def post_with_retry(url: str, headers: dict, payload: dict, retries: int = 5) -> dict:
    last_status = None
    last_body = ""
    with httpx.Client(timeout=60.0) as client:
        for attempt in range(retries):
            started_at = datetime.now(timezone.utc)
            started = time.perf_counter()
            try:
                resp = client.post(url, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                log_event(
                    "external_api_attempt",
                    url=url,
                    attempt=attempt + 1,
                    retries=retries,
                    status="error",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    started_at=started_at.isoformat(),
                    ended_at=datetime.now(timezone.utc).isoformat(),
                )
                if attempt == retries - 1:
                    raise RuntimeError(f"external API request failed: {url} {type(exc).__name__}: {exc}")
                time.sleep(2**attempt)
                continue

            last_status = resp.status_code
            last_body = resp.text[:500]
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = float(resp.headers.get("retry-after", 2**attempt))
                log_event(
                    "external_api_attempt",
                    url=url,
                    attempt=attempt + 1,
                    retries=retries,
                    status="retry",
                    status_code=resp.status_code,
                    response_preview=last_body,
                    wait_seconds=wait,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    started_at=started_at.isoformat(),
                    ended_at=datetime.now(timezone.utc).isoformat(),
                )
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                log_event(
                    "external_api_attempt",
                    url=url,
                    attempt=attempt + 1,
                    retries=retries,
                    status="error",
                    status_code=resp.status_code,
                    response_preview=last_body,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    started_at=started_at.isoformat(),
                    ended_at=datetime.now(timezone.utc).isoformat(),
                )
            resp.raise_for_status()
            log_event(
                "external_api_attempt",
                url=url,
                attempt=attempt + 1,
                retries=retries,
                status="ok",
                status_code=resp.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                started_at=started_at.isoformat(),
                ended_at=datetime.now(timezone.utc).isoformat(),
            )
            return resp.json()
    detail = f"status={last_status} body={last_body}" if last_status else "no response"
    raise RuntimeError(f"external API failed after {retries} retries: {url} ({detail})")


async def post_with_retry_async(
    url: str, headers: dict, payload: dict, retries: int = 5, client: httpx.AsyncClient | None = None
) -> dict:
    """post_with_retry의 비동기 버전 — /api/rag/search 전용 경로(embed_query_async/
    rerank_async)에서 쓴다.

    client를 넘기면(정상 경로 — app/rag/store/pool.py의 앱 전역 재사용 클라이언트) 그걸
    그대로 쓰고 여기서 닫지 않는다 — 매 호출마다 새 AsyncClient를 열고 닫던 게 부하
    테스트로 확인된 진짜 병목(연결 재사용 부재) 중 하나였다. client가 없으면(단독
    테스트 등) 이 호출 범위에서만 쓰고 닫는 임시 클라이언트로 폴백한다."""
    if client is not None:
        return await _post_with_retry(client, url, headers, payload, retries)
    async with httpx.AsyncClient(timeout=60.0) as owned_client:
        return await _post_with_retry(owned_client, url, headers, payload, retries)


async def _post_with_retry(client: httpx.AsyncClient, url: str, headers: dict, payload: dict, retries: int) -> dict:
    last_status = None
    last_body = ""
    for attempt in range(retries):
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        try:
            resp = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            log_event(
                "external_api_attempt",
                url=url,
                attempt=attempt + 1,
                retries=retries,
                status="error",
                error_type=type(exc).__name__,
                error_message=str(exc),
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                started_at=started_at.isoformat(),
                ended_at=datetime.now(timezone.utc).isoformat(),
            )
            if attempt == retries - 1:
                raise RuntimeError(f"external API request failed: {url} {type(exc).__name__}: {exc}")
            await asyncio.sleep(2**attempt)
            continue

        last_status = resp.status_code
        last_body = resp.text[:500]
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = float(resp.headers.get("retry-after", 2**attempt))
            log_event(
                "external_api_attempt",
                url=url,
                attempt=attempt + 1,
                retries=retries,
                status="retry",
                status_code=resp.status_code,
                response_preview=last_body,
                wait_seconds=wait,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                started_at=started_at.isoformat(),
                ended_at=datetime.now(timezone.utc).isoformat(),
            )
            await asyncio.sleep(wait)
            continue
        if resp.status_code >= 400:
            log_event(
                "external_api_attempt",
                url=url,
                attempt=attempt + 1,
                retries=retries,
                status="error",
                status_code=resp.status_code,
                response_preview=last_body,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                started_at=started_at.isoformat(),
                ended_at=datetime.now(timezone.utc).isoformat(),
            )
        resp.raise_for_status()
        log_event(
            "external_api_attempt",
            url=url,
            attempt=attempt + 1,
            retries=retries,
            status="ok",
            status_code=resp.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            started_at=started_at.isoformat(),
            ended_at=datetime.now(timezone.utc).isoformat(),
        )
        return resp.json()
    detail = f"status={last_status} body={last_body}" if last_status else "no response"
    raise RuntimeError(f"external API failed after {retries} retries: {url} ({detail})")


def _embed_voyage(texts: list[str]) -> list[list[float]]:
    if not config.VOYAGE_API_KEY:
        raise RuntimeError("VOYAGE_API_KEY 환경변수가 필요합니다")
    data = post_with_retry(
        "https://api.voyageai.com/v1/embeddings",
        {"Authorization": f"Bearer {config.VOYAGE_API_KEY}"},
        {"model": config.EMBEDDING_MODEL, "input": texts, "input_type": "document"},
    )
    _record_embed_usage(data)
    return [item["embedding"] for item in data["data"]]


def _embed_openai(texts: list[str]) -> list[list[float]]:
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY 환경변수가 필요합니다")
    data = post_with_retry(
        "https://api.openai.com/v1/embeddings",
        {"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        {"model": config.EMBEDDING_MODEL, "input": texts},
    )
    _record_embed_usage(data)
    return [item["embedding"] for item in data["data"]]


def embed_texts(texts: list[str], on_progress=None) -> list[list[float]]:
    embed_fn = _embed_voyage if config.EMBEDDING_PROVIDER == "voyage" else _embed_openai
    vectors: list[list[float]] = []
    for start in range(0, len(texts), _BATCH_SIZE):
        batch = [t[:_MAX_CHARS] for t in texts[start : start + _BATCH_SIZE]]
        vectors.extend(embed_fn(batch))
        if on_progress:
            on_progress(min(start + _BATCH_SIZE, len(texts)), len(texts))
    return vectors


@log_call(
    "embed_query",
    capture_args=("text",),
    capture_result=lambda r: {"provider": config.EMBEDDING_PROVIDER, "dim": len(r)},
)
def embed_query(text: str) -> list[float]:
    """검색 시 질의 임베딩 (Voyage는 query/document input_type을 구분).

    캐시 적용 (RPA-211) — 이 호출이 RAG 5.5초 중 **1,991ms(36%)** 를 차지한다.
    질의→벡터는 **문서가 바뀌어도 안 변하므로**(모델에만 의존) 무효화가 필요 없다.
    모델·차원이 키에 들어가 모델 교체 시 자동으로 옛 벡터를 버린다.
    """
    from app.services import rag_cache

    key = rag_cache.embedding_key(text, config.EMBEDDING_MODEL, config.EMBEDDING_DIM)
    cached = rag_cache.get_embedding(key)
    if cached is not None:
        return cached

    if config.EMBEDDING_PROVIDER == "voyage":
        data = post_with_retry(
            "https://api.voyageai.com/v1/embeddings",
            {"Authorization": f"Bearer {config.VOYAGE_API_KEY}"},
            {"model": config.EMBEDDING_MODEL, "input": [text], "input_type": "query"},
        )
        _record_embed_usage(data)
        vector = data["data"][0]["embedding"]
    else:
        vector = _embed_openai([text])[0]
    rag_cache.put_embedding(key, vector)
    return vector


@log_call(
    "embed_query",
    capture_args=("text",),
    capture_result=lambda r: {"provider": config.EMBEDDING_PROVIDER, "dim": len(r)},
)
async def embed_query_async(text: str, client: httpx.AsyncClient | None = None) -> list[float]:
    """embed_query의 비동기 버전 — /api/rag/search 전용 경로. client는 app/rag/store/
    pool.py의 앱 전역 재사용 클라이언트(연결 재사용, 매 호출 새 클라이언트 방지)."""
    if config.EMBEDDING_PROVIDER == "voyage":
        if not config.VOYAGE_API_KEY:
            raise RuntimeError("VOYAGE_API_KEY 환경변수가 필요합니다")
        data = await post_with_retry_async(
            "https://api.voyageai.com/v1/embeddings",
            {"Authorization": f"Bearer {config.VOYAGE_API_KEY}"},
            {"model": config.EMBEDDING_MODEL, "input": [text], "input_type": "query"},
            client=client,
        )
        _record_embed_usage(data)
        return data["data"][0]["embedding"]
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY 환경변수가 필요합니다")
    data = await post_with_retry_async(
        "https://api.openai.com/v1/embeddings",
        {"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        {"model": config.EMBEDDING_MODEL, "input": [text]},
        client=client,
    )
    _record_embed_usage(data)
    return data["data"][0]["embedding"]
