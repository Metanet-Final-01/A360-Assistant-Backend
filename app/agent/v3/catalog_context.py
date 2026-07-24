"""액션 어휘의 출처 — 파이프라인에 주입되는 카탈로그 컨텍스트 (RPA-285).

v3는 원래 `solution` 값으로 **파이프라인 자체를 갈랐다**:

    if solution == "a360":  <spec→research→3후보→judge→verify→simulate→cards>
    else:                   <LLM 단발 호출 + R1~R6>

그 결과 평행 파이프라인 둘이 생겼고 한쪽만 발전했다 — 품질 루프 전체가 a360 가지에만
쌓여 타 솔루션은 v1 수준으로 화석화됐다. 근본 원인은 분기 기준이 틀렸다는 것이다.
솔루션이 달라서 달라지는 건 파이프라인이 아니라 **액션 어휘를 어디서 얻는가** 하나뿐이다.

그래서 분기를 주입으로 바꾼다. 파이프라인은 하나이고, 이 컨텍스트가 어휘 출처를 나른다:

- a360        → BackendCatalog(DB 적재) + 하이브리드 검색기. 어휘가 수천 개라 검색으로 좁힌다.
- 타 솔루션    → UserCatalog(대화에서 추출) + 검색기 없음. 어휘가 수십 개라 전량이 곧 메뉴다.

`searchable`이 이 둘을 가른다 — 검색기가 없으면 research가 검색 대신 카탈로그 전량을
메뉴로 쓰고, compose의 KB 검색 툴도 바인딩하지 않는다(검색할 KB가 없으므로).
"""

from dataclasses import dataclass

from .retrieval import Retriever, get_retriever
from .verify.catalog import CatalogLookup, get_catalog

A360 = "a360"


@dataclass(frozen=True)
class CatalogContext:
    """이번 턴이 쓸 액션 어휘의 출처. 파이프라인 전체가 이걸 받아 돈다."""

    catalog: CatalogLookup
    retriever: Retriever | None
    solution: str = A360

    @property
    def searchable(self) -> bool:
        """검색으로 어휘를 좁혀야 하는가 — 아니면 카탈로그 전량이 곧 메뉴다."""
        return self.retriever is not None

    @property
    def is_a360(self) -> bool:
        """A360 전용 기능(트리거 제안·KB 문서 검색)을 켤지 판정한다."""
        return self.solution == A360


def a360_context() -> CatalogContext:
    """기본 컨텍스트 — DB 적재 카탈로그 + 하이브리드 검색기."""
    return CatalogContext(catalog=get_catalog(), retriever=get_retriever(), solution=A360)


def user_catalog_context(catalog: CatalogLookup, solution: str) -> CatalogContext:
    """사용자가 대화로 준 카탈로그 컨텍스트 — 검색기 없음(전량이 메뉴)."""
    return CatalogContext(catalog=catalog, retriever=None, solution=solution)
