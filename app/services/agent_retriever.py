"""Agent용 실제 검색기 — app/agent/retrieval.py의 Retriever 계약 구현.

app.services.rag.search_actions()가 이미 계약 스키마(id, source_type, package_name,
action_name, title, url, content, score)를 반환하므로 얇게 감싸기만 한다.

app/agent/retrieval.py의 get_retriever()가 이걸 반환한다(테스트는 conftest가 스텁 주입).
"""

from app.services.rag import search_actions


class HybridRetriever:
    """pgvector + OpenSearch 하이브리드(RRF) + Voyage Reranker 기반 실제 검색기."""

    def search(self, query: str, limit: int = 4) -> list[dict]:
        return search_actions(query, k=limit)


def get_hybrid_retriever() -> HybridRetriever:
    return HybridRetriever()
