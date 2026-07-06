"""하이브리드 검색: pgvector 코사인 유사도 + OpenSearch BM25를 RRF로 융합 후 Voyage로 재정렬한다.

mode:
  vector        — 기존 동작 그대로 (pgvector 코사인 유사도 단독)
  hybrid        — 벡터 + BM25를 RRF로 융합
  hybrid_rerank — hybrid 결과를 Voyage rerank-2.5-lite로 재정렬 (기본값)
"""

from .. import config
from ..observability import log_call
from ..store import db, opensearch_client
from .embed import embed_query
from .rerank import rerank as voyage_rerank


def reciprocal_rank_fusion(rank_lists: list[list[str]], k: int) -> dict[str, float]:
    """rank_lists: 각 검색 방식(branch)의 결과 id를 순위(1위부터)대로 나열한 리스트들.

    score(d) = sum over branches containing d of 1 / (k + rank_in_branch(d)).
    특정 branch 결과에 없는 문서는 그 branch의 항을 0으로 취급한다(페널티 없음).
    """
    scores: dict[str, float] = {}
    for ids in rank_lists:
        for index, doc_id in enumerate(ids):
            rank = index + 1
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


@log_call(
    "hybrid_search",
    capture_args=("query", "limit", "mode"),
    capture_result=lambda r: {
        "count": len(r),
        "retrieval_sources": [item.get("retrieval_source") for item in r],
        "reranked": any("rerank_score" in item for item in r),
    },
)
def search(pg_conn, os_client, query: str, limit: int = 5, mode: str = "hybrid_rerank") -> list[dict]:
    try:
        query_embedding = embed_query(query)
    except RuntimeError as e:
        raise RuntimeError(f"임베딩 설정 오류: {e}")

    pool = config.HYBRID_CANDIDATE_POOL_SIZE
    vector_hits = db.search(pg_conn, query_embedding, limit=pool if mode != "vector" else limit)

    if mode == "vector":
        return [{**h, "retrieval_source": "vector"} for h in vector_hits[:limit]]

    bm25_hits: list[dict] = []
    try:
        bm25_hits = opensearch_client.keyword_search(os_client, query, size=pool)
    except Exception:
        # BM25는 보강 신호이므로, OpenSearch가 응답하지 않으면 벡터 단독 검색으로 저하시킨다.
        bm25_hits = []

    vector_ids = [h["id"] for h in vector_hits]
    bm25_ids = [h["id"] for h in bm25_hits]
    rrf_scores = reciprocal_rank_fusion([vector_ids, bm25_ids], k=config.RRF_K)

    dense_rank = {doc_id: i + 1 for i, doc_id in enumerate(vector_ids)}
    bm25_rank = {doc_id: i + 1 for i, doc_id in enumerate(bm25_ids)}

    # 콘텐츠 조회용 lookup: 두 branch 모두에서 온 필드를 합치되, pgvector 쪽이 스키마 전체(parent_id 등)를 갖고 있으니 우선한다.
    lookup: dict[str, dict] = {h["id"]: h for h in bm25_hits}
    lookup.update({h["id"]: h for h in vector_hits})

    def _retrieval_source(doc_id: str) -> str:
        if doc_id in dense_rank and doc_id in bm25_rank:
            return "hybrid_both"
        if doc_id in dense_rank:
            return "hybrid_dense_only"
        return "hybrid_bm25_only"

    fused_ids = sorted(rrf_scores.keys(), key=lambda d: (-rrf_scores[d], d))
    fused_ids = [d for d in fused_ids if d in lookup][: config.HYBRID_RERANK_CANDIDATES]
    candidates = [
        {
            **lookup[doc_id],
            "dense_rank": dense_rank.get(doc_id),
            "bm25_rank": bm25_rank.get(doc_id),
            "rrf_score": rrf_scores[doc_id],
            "retrieval_source": _retrieval_source(doc_id),
        }
        for doc_id in fused_ids
    ]

    if mode == "hybrid" or not candidates:
        return candidates[:limit]

    try:
        reranked = voyage_rerank(query, [c["content"] for c in candidates], top_k=min(limit, len(candidates)))
    except RuntimeError:
        # VOYAGE_API_KEY 미설정 등 — 재정렬 없이 RRF 순서 그대로 상위 limit개 반환
        return candidates[:limit]

    return [{**candidates[item["index"]], "rerank_score": item["relevance_score"]} for item in reranked]
