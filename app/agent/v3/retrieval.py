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
    """검색 구현이 따라야 하는 계약. score 내림차순으로 최대 limit개를 반환한다.

    source_types를 주면 그 소스 타입만 반환한다 (예: ["action_schema", "bot_example"] —
    shortlist가 문서 페이지·패키지 개요를 걸러 액션 후보만 받을 때 쓴다). None이면 전체.
    """

    def search(
        self, query: str, limit: int = 4, source_types: list[str] | None = None
    ) -> list[dict]:
        """query로 최대 limit개 후보를 score 내림차순 반환한다. source_types로 소스 타입 제한."""
        ...


def _make_retriever() -> Retriever:
    """실제 하이브리드 검색기를 만든다. 테스트는 conftest가 이 함수를 스텁으로 patch한다.

    지연 임포트 — 검색기를 실제로 쓸 때만 백엔드 서비스(→ pgvector·OpenSearch)에
    의존하게 한다. get_retriever가 이 모듈 전역을 호출하므로, 사용처의 from-import
    참조를 건드리지 않고 이 함수만 갈아끼우면 된다.

    ## pushdown=True인 이유 (RPA-298)

    후단 필터(기본값)는 source_types를 **검색에 안 내려보내고** 융합·재정렬이 끝난 뒤
    파이썬에서 거른다. 그래서 필터가 실제로 보는 창은 `min(k*3, rerank_candidates=20)`인데,
    이 코퍼스는 **doc_page가 92%**(16,164/17,838행)라 그 창을 doc_page가 독식한다.
    실측(2026-07-28): 질의 20건을 쏘고도 고유 액션 후보가 16개뿐이었고, 그 결과 메뉴에
    업무 액션이 부족해 composer가 빈 Step으로 자리를 때우고 데이터 추출을 클릭으로
    대신했다 — 검수 규칙을 아무리 붙여도 **메뉴에 없는 액션은 못 쓴다**.

    push-down은 v4용으로 만들어졌고 v4 폐기 후 켜는 버전이 하나도 없었다. 기능·테스트는
    그대로 살아 있으므로 v3가 이어받는다. 캐시 키는 `search_key(pushdown=)`가 네임스페이스를
    가르므로 후단 필터 경로의 캐시와 섞이지 않는다.

    ## collapse_by_action=True인 이유

    코퍼스는 긴 문서를 조각내 싣는다 — `action_schema` 1,375행 = 고유 액션 1,200 + 이어짐
    조각 175. `Email/Send`는 4조각(설명 / Subject·Attachment / From address / Tenant ID)이라
    "이메일 첨부 발송" 질의가 상위 5칸 중 4칸을 **한 액션**으로 채운다 — 실효 k가 2가 된다.
    행이 중복이라서가 아니라 조각이라서 생기는 낭비이므로 **데이터를 지우면 안 되고**
    (파라미터 문서가 사라진다) 검색 결과에서 접는다. 메뉴는 RAG 본문이 아니라
    `catalog.get_action_schema()`로 만들므로(research.py) 이 접기는 무손실이다.

    ⚠ v1·v2는 **얼린 기준선이라 둘 다 끈다** — 검색 폭이 같이 움직이면 버전 간 비교가
    무의미해진다(tests/test_rag_pushdown.py가 이 분리를 지킨다).
    """
    from app.services.agent_retriever import get_hybrid_retriever

    return get_hybrid_retriever(pushdown=True, collapse_by_action=True)


def get_retriever() -> Retriever:
    """graph·orchestrator가 쓰는 실제 하이브리드 검색기를 반환한다."""
    return _make_retriever()
