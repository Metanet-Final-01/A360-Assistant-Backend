"""Voyage Rerank API 클라이언트 (rerank-2.5-lite). RRF 융합 후 최종 재정렬에 사용."""

import logging

from .. import config
from ..observability import log_call
from .embed import post_with_retry

logger = logging.getLogger(__name__)

_RERANK_URL = "https://api.voyageai.com/v1/rerank"


def _record_rerank_usage(data: dict) -> None:
    """Rerank 응답의 토큰을 llm_usage에 기록한다 (component=rag_rerank).

    Voyage rerank는 응답에 usage.total_tokens를 준다. 임베딩과 마찬가지로 사용자와 무관한
    인프라(검색 재정렬)라 system 사용으로 귀속한다. core.llm은 lazy import하고, 기록 실패가
    rerank를 막지 않도록 best-effort로 삼킨다 (embed.py의 _record_embed_usage와 동일 패턴).
    """
    try:
        usage = data.get("usage") or {}
        tokens = usage.get("total_tokens") or 0
        if not tokens:
            return
        from app.core.llm import record_usage, usage_context

        with usage_context(component="rag_rerank"):  # actor_type=system, user_id=None
            record_usage(
                purpose="rerank", model=config.RERANK_MODEL,
                input_tokens=int(tokens), output_tokens=0,
            )
    except Exception:  # noqa: BLE001 — 사용량 기록 실패가 rerank를 막으면 안 됨
        logger.debug("rerank 사용량 기록 실패 (무시)", exc_info=True)


@log_call(
    "voyage_rerank",
    capture_args=("query", "documents", "top_k"),
    capture_result=lambda r: {"model": config.RERANK_MODEL, "count": len(r)},
)
def rerank(query: str, documents: list[str], top_k: int) -> list[dict]:
    """documents 리스트를 query에 대해 재정렬한다.

    반환: relevance_score 내림차순으로 정렬된 [{"index": int, "relevance_score": float}, ...].
    "index"는 입력 documents 리스트의 0-based 위치를 가리킨다.
    """
    if not config.VOYAGE_API_KEY:
        raise RuntimeError("VOYAGE_API_KEY 환경변수가 필요합니다 (rerank)")
    if not documents:
        return []

    data = post_with_retry(
        _RERANK_URL,
        {"Authorization": f"Bearer {config.VOYAGE_API_KEY}"},
        {
            "model": config.RERANK_MODEL,
            "query": query,
            "documents": documents,
            "top_k": min(top_k, len(documents)),
        },
    )
    _record_rerank_usage(data)
    results = data["data"]
    results.sort(key=lambda r: r["relevance_score"], reverse=True)
    return results
