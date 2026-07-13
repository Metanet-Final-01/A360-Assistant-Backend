"""Agent용 실제 검색기 — app/agent/retrieval.py의 Retriever 계약 구현.

app.services.rag.search_actions()가 이미 계약 스키마(id, source_type, package_name,
action_name, title, url, content, score)를 반환하므로 얇게 감싸기만 한다.

app/agent/retrieval.py의 get_retriever()가 이걸 반환한다(테스트는 conftest가 스텁 주입).
"""

from app.services.rag import search_actions


class HybridRetriever:
    """pgvector + OpenSearch 하이브리드(RRF) + Voyage Reranker 기반 실제 검색기."""

    def search(
        self, query: str, limit: int = 4, source_types: list[str] | None = None
    ) -> list[dict]:
        """query로 상위 limit개 액션 후보를 검색한다. source_types를 주면 그 소스 타입만."""
        return search_actions(query, k=limit, source_types=source_types)


def get_hybrid_retriever() -> HybridRetriever:
    """실제 하이브리드 검색기 인스턴스를 반환한다(agent retrieval.get_retriever가 호출)."""
    return HybridRetriever()
