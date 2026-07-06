"""Agent용 실제 검색기 — app/agent/retrieval.py의 Retriever 계약 구현.

FakeRetriever(키워드 매칭 스텁)를 대체한다. app.services.rag.search_actions()가
이미 계약 스키마(id, source_type, package_name, action_name, title, url, content,
score)를 반환하므로 얇게 감싸기만 한다.

app/agent/retrieval.py의 get_retriever()가 AGENT_RETRIEVER=hybrid일 때 이걸 쓴다.
"""

from app.services.rag import search_actions


class HybridRetriever:
    """pgvector + OpenSearch 하이브리드(RRF) + Voyage Reranker 기반 실제 검색기."""

    def search(self, query: str, limit: int = 4) -> list[dict]:
        return search_actions(query, k=limit)


def get_hybrid_retriever() -> HybridRetriever:
    return HybridRetriever()
