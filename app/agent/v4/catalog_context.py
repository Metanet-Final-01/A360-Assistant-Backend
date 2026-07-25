"""액션 어휘의 출처 — 파이프라인에 주입되는 카탈로그 컨텍스트 (RPA-285 / RPA-298).

v3는 원래 `solution` 값으로 **파이프라인 자체를 갈랐다**:

    if solution == "a360":  <spec→research→3후보→judge→verify→simulate→cards>
    else:                   <LLM 단발 호출 + R1~R6>

그 결과 평행 파이프라인 둘이 생겼고 한쪽만 발전했다 — 품질 루프 전체가 a360 가지에만
쌓여 타 솔루션은 v1 수준으로 화석화됐다. 근본 원인은 분기 기준이 틀렸다는 것이다.
솔루션이 달라서 달라지는 건 파이프라인이 아니라 **액션 어휘를 어디서 얻는가** 하나뿐이다.

그래서 분기를 주입으로 바꾼다. 파이프라인은 하나이고, 이 컨텍스트가 어휘 출처를 나른다:

- a360        → BackendCatalog(DB 적재) + 하이브리드 검색기. 어휘가 수천 개라 검색으로 좁힌다.
- a360-custom → **오버레이** — A360 카탈로그·검색을 그대로 두고 사내 커스텀 액션을 그 위에 얹는다.
- 타 솔루션    → UserCatalog(대화에서 추출) + 검색기 없음. 어휘가 수십 개라 전량이 곧 메뉴다.

`searchable`이 a360 계열과 타 솔루션을 가른다 — 검색기가 없으면 research가 검색 대신
카탈로그 전량을 메뉴로 쓰고, compose의 KB 검색 툴도 바인딩하지 않는다(검색할 KB가 없으므로).

## 왜 오버레이라는 제3의 모드가 필요했나 (설계 §6.2)

기존 두 모드는 **양자택일**이었다. 사용자가 사내 커스텀 패키지를 채팅으로 설명하면
어느 쪽으로 가도 손해였다:

- a360 모드로 남으면 → 커스텀 액션이 카탈로그에 없어 **R1이 환각으로 blocker 판정**하고
  교정 루프가 그 액션을 지운다. 사용자가 준 어휘가 산출물에서 사라진다.
- user_catalog 모드로 가면 → A360 표준 액션 수천 개와 **하이브리드 검색을 통째로 잃는다**.
  사내 패키지는 보통 A360 표준 액션과 **섞어** 쓰는데 그 절반이 사라진다.

오버레이는 둘 다 살린다: 커스텀 액션이 A360 액션과 **동등한 1급 어휘**(R1 통과)이면서
A360 카탈로그·검색·트리거 제안이 모두 유지된다.
"""

import json
import re
from dataclasses import dataclass

from .retrieval import Retriever, get_retriever
from .verify.catalog import CatalogLookup, get_catalog

A360 = "a360"

# 오버레이 모드의 세션 solution 값. 하이픈인 이유: 백엔드 `_SOLUTION_RE`
# (`^[a-z0-9][a-z0-9 ._-]{0,48}$`)가 `+`를 거부한다 — 기존 detected_solution 쓰기 경로를
# 그대로 재사용하려면 그 문법 안에 있어야 한다(백엔드 무변경).
A360_CUSTOM = "a360-custom"

# 커스텀 액션을 한 검색당 최대 몇 개까지 끼워 넣을지. research의 메뉴 상한이 14개라
# 무제한으로 넣으면 A360 액션이 통째로 밀려난다 — 절반 이하로 묶어 두 어휘가 공존하게 한다.
_OVERLAY_MAX_HITS = 6

# 근거 강도 (설계 §6.6). 사용자가 **파라미터까지 설명한** 액션은 흐름에 바로 쓸 수 있는
# 강한 근거, **이름만** 준 액션은 약한 근거다. 이 점수가 그대로 harness.attach_confidence의
# evidence 기반값이 되므로 신뢰도에 반영된다(별도 감점 장치 불필요).
_STRENGTH_DESCRIBED = 0.9
_STRENGTH_NAME_ONLY = 0.6

# 질의와 어휘가 하나도 안 겹치는 커스텀 액션에 주는 감쇄. 0으로 만들지 않는 이유:
# 사용자가 준 액션은 질의어와 표기가 달라도(영문 액션명 vs 한국어 요구) 쓰여야 한다.
# 다만 실제로 맞은 액션보다는 아래에 놓여 메뉴 상한에서 먼저 밀린다.
_NO_MATCH_FACTOR = 0.5

# 액션 후보를 찾는 검색인지 판정 — 배경 문서(doc_page) 검색에 커스텀 액션을 끼워 넣으면
# 배경 지식 자리를 액션이 잡아먹는다.
_ACTION_SOURCE_TYPES = frozenset({"action_schema", "bot_example"})

# 표기 분해용: CamelCase 조각·숫자·한글 덩어리. "ReadRange" → {read, range}.
_TOKEN_RE = re.compile(r"[A-Za-z][a-z0-9]+|[A-Za-z]{2,}|[0-9]{2,}|[가-힣]{2,}")


def _spec_key(spec: dict) -> tuple[str, str]:
    return (spec.get("package") or "", spec.get("action") or "")


def _spec_terms(spec: dict) -> frozenset[str]:
    """액션 스펙에서 질의 매칭용 어휘를 뽑는다 (패키지·액션·라벨·파라미터명).

    LLM을 부르지 않는 결정론 매칭이다 — 커스텀 액션을 검색 결과에 얹을지 정하는 데
    매 질의마다 임베딩을 태우는 건 얻는 것에 비해 과하다.
    """
    parts = [spec.get("package"), spec.get("action"), spec.get("label")]
    for p in spec.get("parameters") or []:
        parts.extend((p.get("name"), p.get("label")))
    terms: set[str] = set()
    for part in parts:
        if not isinstance(part, str) or not part.strip():
            continue
        low = part.strip().lower()
        terms.add(low)
        terms.update(t.lower() for t in _TOKEN_RE.findall(low))
    return frozenset(t for t in terms if len(t) >= 2)


def _relevance(query: str, terms: frozenset[str]) -> int:
    low = (query or "").lower()
    return sum(1 for t in terms if t in low)


def _strength(spec: dict) -> float:
    """파라미터를 설명해 줬으면 강한 근거, 이름만 줬으면 약한 근거 (설계 §6.6)."""
    return _STRENGTH_DESCRIBED if spec.get("parameters") else _STRENGTH_NAME_ONLY


class OverlayCatalog:
    """A360 카탈로그 위에 사용자 커스텀 액션을 얹은 합성 CatalogLookup (설계 §6.2).

    조회는 **커스텀 우선, 없으면 A360 위임**이다. 커스텀이 우선인 이유: 사내 패키지가
    A360 표준과 같은 (package, action) 표기를 쓰면 사용자가 설명한 파라미터가 진실이다
    (그쪽이 실제로 배포된 것이고, 우리 카탈로그는 표준 패키지만 안다).
    """

    def __init__(self, base: CatalogLookup, custom: list[dict]):
        self._base = base
        self._index: dict[tuple[str, str], dict] = {_spec_key(s): s for s in custom}

    def get_action_schema(self, package: str, action: str) -> dict | None:
        hit = self._index.get((package, action))
        if hit is not None:
            return hit
        return self._base.get_action_schema(package, action)

    def iter_action_schemas(self):
        """합집합 순회 — 커스텀 먼저, 그다음 중복을 뺀 A360 (BackendCatalog와 같은 계약).

        base가 순회를 지원하지 않으면(테스트 스텁 등) 커스텀만 낸다 — 순회는 라벨 룩업·
        세션 레지스트리 유도 같은 부가 기능이라 없다고 턴이 죽으면 안 된다.
        """
        yield from self._index.values()
        iter_fn = getattr(self._base, "iter_action_schemas", None)
        if not callable(iter_fn):
            return
        for spec in iter_fn():
            if _spec_key(spec) not in self._index:
                yield spec


class OverlayRetriever:
    """A360 하이브리드 검색 결과 위에 커스텀 액션을 항상 얹는 검색기 (설계 §6.2).

    왜 카탈로그만으론 부족한가: research는 **검색 결과로 액션 메뉴를 만든다**. 커스텀
    액션은 KB에 적재된 적이 없어 검색에 절대 안 잡히고, 그러면 카탈로그에 넣어 R1을
    통과시켜도 composer가 그 액션의 존재 자체를 모른다 — 쓸 수 없는 어휘가 된다.
    그래서 검색 경계에서 합집합을 만든다. 반환 dict는 백엔드 검색 행 스키마와 같은
    모양이라 research·sink·attach_sources가 그대로 소비한다.
    """

    def __init__(self, base: Retriever | None, custom: list[dict]):
        self._base = base
        self._rows = [(s, _spec_terms(s), _strength(s)) for s in custom]

    def search(
        self, query: str, limit: int = 4, source_types: list[str] | None = None
    ) -> list[dict]:
        base_hits = self._base.search(query, limit=limit, source_types=source_types) if self._base else []
        if source_types is not None and not (_ACTION_SOURCE_TYPES & set(source_types)):
            return base_hits  # 배경 문서 검색 — 액션을 끼워 넣을 자리가 아니다
        return self._overlay_hits(query) + base_hits

    def _overlay_hits(self, query: str) -> list[dict]:
        """질의 관련도 내림차순으로 상한까지. 관련도 0도 싣되 점수를 깎는다."""
        scored = sorted(
            ((spec, _relevance(query, terms), strength) for spec, terms, strength in self._rows),
            key=lambda r: (-r[1], -r[2]),  # sorted가 안정 정렬 → 동점은 사용자가 준 순서
        )
        hits: list[dict] = []
        for spec, rel, strength in scored[:_OVERLAY_MAX_HITS]:
            pkg, act = _spec_key(spec)
            params = spec.get("parameters")
            detail = (
                "파라미터: " + ", ".join(str(p.get("name")) for p in params)
                if params
                else "파라미터 설명 없음 — 사용자가 이름만 알려준 액션이다."
            )
            hits.append({
                "id": f"overlay:{pkg}/{act}",
                "source_type": "user_catalog",
                "package_name": pkg,
                "action_name": act,
                "title": f"사내 커스텀 액션 — {pkg}/{act}",
                "url": None,
                "content": f"사용자가 대화에서 제공한 사내 커스텀 액션 «{spec.get('label') or act}». {detail}",
                "score": round(strength if rel else strength * _NO_MATCH_FACTOR, 3),
            })
        return hits


@dataclass(frozen=True)
class CatalogContext:
    """이번 턴이 쓸 액션 어휘의 출처. 파이프라인 전체가 이걸 받아 돈다."""

    catalog: CatalogLookup
    retriever: Retriever | None
    solution: str = A360
    # 오버레이로 얹힌 커스텀 액션의 (package, action) — 관측·안내 문구용. 빈 튜플이면 순수 모드.
    overlay: tuple[tuple[str, str], ...] = ()

    @property
    def searchable(self) -> bool:
        """검색으로 어휘를 좁혀야 하는가 — 아니면 카탈로그 전량이 곧 메뉴다."""
        return self.retriever is not None

    @property
    def is_a360(self) -> bool:
        """A360 전용 기능(트리거 제안·KB 문서 검색·구조/세션 검사)을 켤지 판정한다.

        오버레이는 **A360이다** — 사내 커스텀 패키지도 A360 봇으로 실행되므로 컨테이너
        모델·세션 open/close 쌍 같은 A360 구조 전제가 그대로 성립한다. 어휘만 늘어난 것이다.
        """
        return self.solution in (A360, A360_CUSTOM)

    @property
    def has_overlay(self) -> bool:
        return bool(self.overlay)


def a360_context() -> CatalogContext:
    """기본 컨텍스트 — DB 적재 카탈로그 + 하이브리드 검색기."""
    return CatalogContext(catalog=get_catalog(), retriever=get_retriever(), solution=A360)


# 같은 커스텀 집합에는 같은 OverlayCatalog 인스턴스를 준다. 상한은 동시 세션 수 감각으로
# 넉넉히 — 항목 하나가 파이썬 dict 하나라 메모리 부담이 없다.
_OVERLAY_CACHE: dict[tuple, OverlayCatalog] = {}
_OVERLAY_CACHE_MAX = 64


def _overlay_catalog_for(base: CatalogLookup, custom: list[dict]) -> OverlayCatalog:
    """커스텀 집합이 같으면 **같은 인스턴스**를 돌려준다.

    왜 인스턴스를 고정해야 하나: `knowledge.derive`의 유도 캐시가 `id(catalog)`를 키로 쓴다
    (`derive._cached`). 프로덕션의 BackendCatalog는 프로세스 싱글톤이라 그 전제가 성립했는데,
    턴마다 새 래퍼를 만들면 셋이 한꺼번에 깨진다:

    1. 수천 행짜리 컨테이너·세션 레지스트리 유도를 **매 턴 처음부터** 다시 돈다.
    2. 죽은 래퍼의 id가 캐시에 영원히 남는다(누수).
    3. GC 후 id가 재사용되면 **남의 유도 결과**를 읽는다 — 조용한 오답.
    """
    key = (id(base), tuple(sorted(json.dumps(s, sort_keys=True, ensure_ascii=False) for s in custom)))
    hit = _OVERLAY_CACHE.get(key)
    if hit is None:
        if len(_OVERLAY_CACHE) >= _OVERLAY_CACHE_MAX:
            _OVERLAY_CACHE.pop(next(iter(_OVERLAY_CACHE)))  # 가장 오래된 것부터 (삽입 순서)
        hit = _OVERLAY_CACHE[key] = OverlayCatalog(base, custom)
    return hit


def overlay_context(custom: list[dict]) -> CatalogContext:
    """오버레이 컨텍스트 — A360 카탈로그·검색 유지 + 사내 커스텀 액션 합집합 (설계 §6.2).

    custom이 비면 순수 a360과 동일하다(합성 계층을 씌워도 얻는 게 없다) — 추출이 빈손이어도
    흐름 생성이 막히지 않게 여기서 흡수한다.
    """
    if not custom:
        return a360_context()
    return CatalogContext(
        catalog=_overlay_catalog_for(get_catalog(), custom),
        retriever=OverlayRetriever(get_retriever(), custom),
        solution=A360_CUSTOM,
        overlay=tuple(_spec_key(s) for s in custom),
    )


def user_catalog_context(catalog: CatalogLookup, solution: str) -> CatalogContext:
    """사용자가 대화로 준 카탈로그 컨텍스트 — 검색기 없음(전량이 메뉴)."""
    return CatalogContext(catalog=catalog, retriever=None, solution=solution)
