"""Agent 담당에 노출하는 RAG 검색 서비스 진입점 (docs/INTERFACES.md 계약).

app/rag/의 실제 구현(하이브리드 RRF + Voyage Reranker, RPA-9)을 감싸는 얇은
wrapper다. Agent 쪽(app/agent/retrieval.py의 Retriever 구현체)은 이 함수 하나만
알면 되고, app/rag/ 내부 구조가 바뀌어도 이 시그니처만 유지되면 영향받지 않는다.
"""

from app.rag import config
from app.rag.observability import log_event
from app.rag.retrieval.hybrid_search import search as _hybrid_search
from app.rag.store import db, opensearch_client
from app.services import rag_cache
from app.services.retrieval_params import load_active_params


def search_actions(
    query: str,
    k: int = 5,
    source_types: list[str] | None = None,
) -> list[dict]:
    """pgvector + OpenSearch 하이브리드(RRF) + Voyage Reranker 검색.

    반환 항목(최소): id, source_type, package_name, action_name, title, url, content, score.
    score는 재정렬됐으면 rerank_score, 아니면 rrf_score/코사인 유사도 순으로 채워진다
    (여러 검색 단계의 진단 필드 dense_rank/bm25_rank/retrieval_source 등도 함께 담겨 있다).

    source_types를 주면 (예: ["action_schema"]) 그 소스 타입만 남기고 상위 k개를 반환한다 —
    현재는 하이브리드 검색 자체에 필터를 내려보내지 않고, 후보를 넉넉히 뽑은 뒤
    결과 단계에서 걸러내는 방식이라 필터를 걸면 후보 풀이 좁아질 수 있다.
    """
    # 캐시 (RPA-211) — 에이전트가 타는 경로다. 턴당 9회 불리고 회당 5.5초라 턴 시간의 68%를 먹는다.
    # 키에 활성 파라미터를 넣으므로 RPA-149 런타임 튜닝이 **즉시** 반영된다(별도 무효화 불필요).
    params = load_active_params()
    cache_key = rag_cache.search_key(
        query, k, tuple(source_types) if source_types else None,
        params, config.EMBEDDING_MODEL, config.RERANK_MODEL,
    )
    cached = rag_cache.get_search(cache_key)
    if cached is not None:
        return cached

    conn = db.connect()
    try:
        os_client = opensearch_client.connect()
        fetch_limit = k * 3 if source_types else k
        results = _hybrid_search(conn, os_client, query, limit=fetch_limit, params=params)
    finally:
        conn.close()

    if source_types:
        results = [r for r in results if r.get("source_type") in source_types]

    for r in results:
        r["score"] = r.get("rerank_score", r.get("rrf_score", r.get("score", 0.0)))

    final = results[:k]
    # 저하(BM25 실패)·빈 결과는 저장하지 않는다 — 장애가 캐시에 얼어붙으면 복구 후에도
    # TTL 동안 반쪽 결과가 나간다. 건너뛴 사유는 관측에 남긴다.
    skipped = rag_cache.put_search(cache_key, final)
    if skipped:
        log_event("rag_cache_skip", reason=skipped, query_len=len(query), k=k)
    return final
