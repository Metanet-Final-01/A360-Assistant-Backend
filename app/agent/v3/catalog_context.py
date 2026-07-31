"""액션 어휘의 출처 — 파이프라인에 주입되는 카탈로그 컨텍스트 (RPA-285).

v3는 원래 `solution` 값으로 **파이프라인 자체를 갈랐다**:

    if solution == "a360":  <spec→research→compose→verify→refine→cards>
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

import hashlib
import logging
from dataclasses import dataclass, field

from .retrieval import Retriever, get_retriever
from .verify.catalog import CatalogLookup, get_catalog

logger = logging.getLogger(__name__)

A360 = "a360"


class ActionVocabulary:
    """이번 턴이 확보한 액션 (package, action) — **append-only** (RPA-359).

    ## 왜 필요한가

    앞서는 단계마다 자기가 받을 어휘를 따로 정했다. 실측(2026-07-30 턴 fcd58640):

        초안(compose)  84종   검색 유래 62 + 구조·세션 보완 22
        수리(refine)   23종   위반 걸린 것 8 + 삽입 재료 15

    **61종이 수리 시점에 사라지고, 사라지는 쪽이 업무 액션이다.** 그래서 검증기가
    「`Microsoft 365 Excel/Read cell`은 카탈로그에 없다」고 지적해도 수리에는 `Get cell`이란
    이름이 없어, 실행 가능한 유일한 답이 '지우기'가 됐다(네 라운드 연속 `흐름이 줄어 반려`).

    ## 불변식 — 줄지 않는다

    턴 안에서 어휘는 **늘기만 한다.** 앞 단계가 갖고 있던 선택지를 뒷 단계가 잃는 일이
    원천 차단된다.

    ## ⚠ 순서를 보존한다 (캐시)

    프롬프트 캐시는 공통 접두에 걸린다. 새 어휘를 **뒤에 붙이면** 접두가 유지돼 캐시가
    살고, 정렬을 다시 하거나 재렌더하면 그 턴의 나머지 호출이 전부 캐시를 잃는다
    (실측: 한 턴 입력 25만 토큰의 절반이 같은 프롬프트 재전송, 캐시 적중률 34%).
    그래서 `dict`(삽입 순서 보존)로 들고 정렬하지 않는다.

    ## ⚠ 추가 지점은 호출부가 고정한다

    값 채우기는 서브트리 병렬이다. 워커 안에서 추가하면 두 워커가 같은 것을 찾아 순서가
    갈리고 캐시·재현성이 함께 깨진다. **단계 사이·라운드 사이에서만** 부른다.

    범위는 **턴 단위**다. 세션 단위로 두면 이전 턴 어휘가 다음 턴에 새어 같은 문서인데
    결과가 달라진다.
    """

    def __init__(self) -> None:
        # dict = 삽입 순서 보존 + O(1) 조회. 값은 어디서 들어왔는지(출처)로, 관측용이다.
        self._known: dict[tuple[str, str], str] = {}
        # 같은 질의로 두 번 조회하지 않기 위한 기록 — 발동 조건이 반복돼도 비용이 안 는다.
        self._fetched: set[str] = set()

    def add(self, package: str, action: str, source: str = "") -> bool:
        """하나 기록한다. 이미 있으면 False(출처는 **처음 것을 유지**한다)."""
        key = (package, action)
        if not package or not action or key in self._known:
            return False
        self._known[key] = source
        return True

    def extend(self, pairs, source: str = "") -> int:
        """여럿 기록하고 **새로 들어온 개수**를 돌려준다."""
        return sum(1 for p, a in pairs if self.add(p, a, source))

    def mark_fetched(self, token: str) -> bool:
        """이 조회를 이번 턴에 이미 했나 — 처음이면 True(그리고 기록한다)."""
        if token in self._fetched:
            return False
        self._fetched.add(token)
        return True

    def packages(self) -> set[str]:
        return {p for p, _ in self._known}

    def of_package(self, package: str) -> list[tuple[str, str]]:
        """그 패키지로 확보해 둔 액션 — 삽입 순서 그대로."""
        return [k for k in self._known if k[0] == package]

    def __contains__(self, key: object) -> bool:
        return key in self._known

    def __len__(self) -> int:
        return len(self._known)

    def __iter__(self):
        return iter(self._known)

    def digest(self) -> str:
        """이번 턴 어휘의 지문 — 턴 사이 비교용 관측값.

        정렬해서 만든다. 이건 **프롬프트에 실리지 않으므로** 캐시와 무관하고, 순서가 달라도
        같은 집합이면 같은 지문이어야 비교가 성립한다.
        """
        joined = "\n".join(f"{p}/{a}" for p, a in sorted(self._known))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class CatalogContext:
    """이번 턴이 쓸 액션 어휘의 출처. 파이프라인 전체가 이걸 받아 돈다.

    `frozen=True`는 **필드 재바인딩**만 막는다 — `vocabulary`는 가변 객체이고 그 안을
    채우는 것은 의도된 동작이다(단조 증가). 컨텍스트 자체는 턴마다 새로 만든다.
    """

    catalog: CatalogLookup
    retriever: Retriever | None
    solution: str = A360
    vocabulary: ActionVocabulary = field(default_factory=ActionVocabulary)

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
