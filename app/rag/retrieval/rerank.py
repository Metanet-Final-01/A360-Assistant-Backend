"""Voyage Rerank API 클라이언트 (rerank-2.5-lite). RRF 융합 후 최종 재정렬에 사용."""

from .. import config
from ..observability import log_call
from .embed import post_with_retry

_RERANK_URL = "https://api.voyageai.com/v1/rerank"


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
    results = data["data"]
    results.sort(key=lambda r: r["relevance_score"], reverse=True)
    return results
