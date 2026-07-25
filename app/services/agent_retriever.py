"""Agent용 실제 검색기 — app/agent/retrieval.py의 Retriever 계약 구현.

app.services.rag.search_actions()가 이미 계약 스키마(id, source_type, package_name,
action_name, title, url, content, score)를 반환하므로 얇게 감싸기만 한다.

app/agent/retrieval.py의 get_retriever()가 이걸 반환한다(테스트는 conftest가 스텁 주입).
"""

from app.services.rag import search_actions


class HybridRetriever:
    """pgvector + OpenSearch 하이브리드(RRF) + Voyage Reranker 기반 실제 검색기.

    pushdown(RPA-298)은 **인스턴스 속성**이지 search()의 인자가 아니다. 인자로 두면
    Retriever 프로토콜과 모든 테스트 Fake의 시그니처를 함께 고쳐야 하고, 호출부마다
    켜고 끄는 걸 잊을 수 있다. 버전이 자기 검색기를 만들 때 한 번 정하면 "v4는
    push-down으로 검색한다"가 한 곳에만 적히므로 어긋날 여지가 없다.
    """

    def __init__(self, pushdown: bool = False):
        self.pushdown = pushdown

    def search(
        self, query: str, limit: int = 4, source_types: list[str] | None = None
    ) -> list[dict]:
        """query로 상위 limit개 액션 후보를 검색한다. source_types를 주면 그 소스 타입만."""
        return search_actions(query, k=limit, source_types=source_types, pushdown=self.pushdown)


def get_hybrid_retriever(pushdown: bool = False) -> HybridRetriever:
    """실제 하이브리드 검색기 인스턴스를 반환한다(agent retrieval.get_retriever가 호출).

    기본값 False가 v1~v3 계약이다 — 그 버전들의 retrieval.py는 인자 없이 부르므로
    후단 필터 동작이 그대로 보존된다(버전 간 비교의 전제).
    """
    return HybridRetriever(pushdown=pushdown)
