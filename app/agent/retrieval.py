"""RAG 검색 인터페이스와 실제 검색기 접근자.

agent 코드는 `Retriever` 인터페이스에만 의존한다. 실제 구현은 백엔드 서비스
(`app.services.agent_retriever.HybridRetriever` — pgvector+OpenSearch RRF + Voyage
리랭커)이며, agent는 DB에 직접 붙지 않고 그 서비스만 호출한다(INTERFACES 소유권).

검색 결과 dict는 백엔드 `/api/rag/search`(`app.rag.store.db.search`)의 행 스키마를
따른다: id, source_type, package_name, action_name, title, url, content, score.

테스트는 인프라 없이 돌아야 하므로 `tests/conftest.py`의 autouse fixture가
`_make_retriever`를 인메모리 스텁(tests/agent_stubs.FakeRetriever)으로 주입한다 —
프로덕션 경로엔 스텁이 없다.
"""

from typing import Protocol


class Retriever(Protocol):
    """검색 구현이 따라야 하는 계약. score 내림차순으로 최대 limit개를 반환한다."""

    def search(self, query: str, limit: int = 4) -> list[dict]: ...


def _make_retriever() -> Retriever:
    """실제 하이브리드 검색기를 만든다. 테스트는 conftest가 이 함수를 스텁으로 patch한다.

    지연 임포트 — 검색기를 실제로 쓸 때만 백엔드 서비스(→ pgvector·OpenSearch)에
    의존하게 한다. get_retriever가 이 모듈 전역을 호출하므로, 사용처의 from-import
    참조를 건드리지 않고 이 함수만 갈아끼우면 된다.
    """
    from app.services.agent_retriever import get_hybrid_retriever

    return get_hybrid_retriever()


def get_retriever() -> Retriever:
    """graph·orchestrator가 쓰는 실제 하이브리드 검색기를 반환한다."""
    return _make_retriever()
