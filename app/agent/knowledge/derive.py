"""카탈로그에서 어휘를 유도한다 — 수기 상수를 폴백으로만 남긴다 (RPA-298).

## 왜 유도인가

v3는 세션 opener/closer를 수기 (package, action) 쌍으로 들고 있었다. 카탈로그가 llm_agent
소싱으로 바뀌며 그 4쌍이 **전부 부재**가 됐고, 실패가 침묵이 아니라 더 나쁜 형태로 나타났다:

    openers = 공집합, closers = 3건 생존 → 열린 적 없는 세션을 닫는 것처럼 보여 R7 대량 오탐

그 오탐이 confidence 감점(`harness._PENALTY_RULES`)과 불필요한 surgeon 수리 라운드로
직결된다. 유도로 바꾸면 재적재를 따라가고, 유도가 실패하면 **검사를 통째로 침묵**시킨다.

## 폴백 정책 — "한쪽이라도 비면 검사하지 않는다"

`SessionRegistry.usable`이 False면 호출부는 R7/R8을 건너뛴다. **'검사 안 함'이
'틀리게 검사함'보다 낫다** — 지금 v3가 정확히 후자다. 건너뛴 사실은 `source`로 기계
판독 가능하게 노출하고 로그를 남긴다.

수기 상수는 지우지 않고 `fallback_*` 인자로 받는다. 유도가 전부 비면 지금 수준을 유지한다.

## 유도 규칙 (실측 근거)

로컬 카탈로그(1,200액션) 실측:

| 신호 | 건수 |
|---|---|
| `spec.session_role` | **0** |
| `spec.return_type == "SESSION"` | **0** |
| SESSION 타입 파라미터 보유 액션 | 821 (60패키지) |

명시 신호가 전무하므로 **세션 패키지 게이팅 + 액션명 정규식**이 주력이다. 게이팅을 빼면
`File/Open`·`Folder/Open`·`Application/Open program/file`·`Analyze/Open` 4건이 opener로
오검출된다 — 전부 세션을 만들지 않는 액션이다. 반드시 AND로 건다.

게이팅 적용 시: opener 57액션/51패키지, closer 48액션/47패키지.
"""

import logging
import re
import weakref
from collections.abc import Iterator
from dataclasses import dataclass, field

from .lexicon import CONTAINER_PACKAGES, eh_role, loop_signal_role

logger = logging.getLogger(__name__)

# 세션을 여는/닫는 액션의 이름 패턴. 액션명 **앞머리**만 본다 — 중간 일치를 허용하면
# "Close spreadsheet after reopen" 같은 서술형 이름이 양쪽에 다 걸린다.
# 앞머리로 못 잡는 슬러그 표기(`excelAdvancedPackageCloseAction`)는 아래 ③ 닫기 보강이
# **opener 보유 패키지로 좁혀** 담당한다.
_OPENER_RE = re.compile(r"^\s*(open|connect|start\s+session|log\s?in)\b", re.IGNORECASE)
_CLOSER_RE = re.compile(r"^\s*(close|disconnect|end\s+session|log\s?out)\b", re.IGNORECASE)

# 파라미터 타입이 이 값이면 그 액션은 세션을 참조한다 → 소속 패키지를 세션 패키지로 본다.
_SESSION_PARAM_TYPE = "SESSION"


@dataclass(frozen=True)
class SessionRegistry:
    """세션 opener/closer 유도 결과 + 출처.

    `source`는 관측용이다 — 평가에서 'v4가 v3보다 나쁘다'가 나왔을 때 원인이 유도 실패인지
    설계 문제인지 즉시 갈라야 한다.
    """

    openers: frozenset[tuple[str, str]] = frozenset()
    closers: frozenset[tuple[str, str]] = frozenset()
    session_packages: frozenset[str] = frozenset()
    source: str = "empty"  # "derived" | "constants" | "mixed" | "empty"
    derived_count: int = 0

    @property
    def usable(self) -> bool:
        """세션 검사(R7/R8)를 돌려도 되는가.

        **한쪽이라도 비면 False**다. opener 없이 closer만 있으면 모든 닫기가 "열린 적 없는
        세션을 닫는다"로 잡히고, 반대면 모든 열기가 미종료로 잡힌다 — 어느 쪽이든 전량 오탐이다.
        """
        return bool(self.openers) and bool(self.closers)


def _iter_specs(catalog) -> Iterator[dict]:
    """카탈로그의 액션 스펙을 순회한다. 순회를 지원하지 않으면(테스트 스텁) 빈 이터레이터."""
    iter_fn = getattr(catalog, "iter_action_schemas", None)
    if not callable(iter_fn):
        return iter(())
    try:
        return iter(list(iter_fn()))
    except Exception:  # noqa: BLE001 — 유도 실패가 턴을 죽이지 않는다. 폴백이 받는다.
        logger.warning("카탈로그 순회 실패 — 어휘 유도를 건너뛴다", exc_info=True)
        return iter(())


def _has_session_param(spec: dict) -> bool:
    params = spec.get("parameters")
    if not isinstance(params, list):
        return False
    return any(
        isinstance(p, dict) and str(p.get("type") or "").strip().upper() == _SESSION_PARAM_TYPE
        for p in params
    )


# 프로세스 캐시 — catalog 객체 **약한 참조** 기준. BackendCatalog가 이미 TTL 캐시라
# 이중 비용이 없다. 카탈로그가 수거되면 그 항목도 함께 사라진다.
_CACHE: "weakref.WeakKeyDictionary[object, dict[str, object]]" = weakref.WeakKeyDictionary()


def clear_cache() -> None:
    """유도 캐시를 비운다 — 테스트·재적재용."""
    _CACHE.clear()


def _cached(catalog, key: str, build):
    """카탈로그별 유도 결과를 캐시한다.

    ⚠ 키는 **객체 자체(약한 참조)**다. 예전에는 `id(catalog)`를 썼는데, CPython은 수거된
    객체의 주소를 재사용하므로 **새 카탈로그가 죽은 카탈로그의 유도 결과를 그대로 받는다**
    (재현됨: 같은 주소를 다시 잡은 카탈로그가 남의 제어 흐름 어휘를 돌려줬다).

    이 캐시가 담는 것은 세션 opener/closer 레지스트리·제어 흐름 액션 전량이라, 오염되면
    R7/R8/R17 판정과 수리 어휘가 통째로 다른 카탈로그의 것이 된다 — 조용한 오답이라
    캐시 미스보다 훨씬 나쁘다. `OverlayCatalog`처럼 세션마다 만들어졌다 LRU에서 밀려나는
    객체가 실제로 그 조건을 만든다.

    약한 참조를 못 다는 카탈로그(__slots__에 __weakref__가 없는 등)는 캐시하지 않고 매번
    만든다 — 느려도 맞다.
    """
    try:
        bucket = _CACHE.get(catalog)
    except TypeError:  # 해시 불가 — 캐시 대상이 아니다
        return build()
    if bucket is None:
        bucket = {}
        try:
            _CACHE[catalog] = bucket
        except TypeError:  # 약한 참조 불가
            return build()
    if key not in bucket:
        bucket[key] = build()
    return bucket[key]


def derive_session_registry(
    catalog,
    *,
    fallback_openers: frozenset[tuple[str, str]] = frozenset(),
    fallback_closers: frozenset[tuple[str, str]] = frozenset(),
) -> SessionRegistry:
    """세션 opener/closer를 카탈로그에서 유도한다. 유도가 비면 폴백 상수를 쓴다.

    신호는 두 층이고 **명시 신호가 이긴다**:

    ① 명시 — 스펙의 `session_role`("opener"/"closer")와 `return_type == "SESSION"`.
       카탈로그 빌드가 붙인 값이라 오탐 위험이 없어 **패키지 게이팅 없이** 채택한다.
    ② 이름 — `_OPENER_RE`/`_CLOSER_RE`. 이쪽은 세션 파라미터를 가진 패키지(또는 ①이 잡은
       패키지) 안에서만 본다. 게이팅이 없으면 세션과 무관한 `File/Open`이 opener가 된다.

    ①이 필요한 이유(v3에서 이식): `sessionName`을 **TEXT**로 받는 표기 세대가 있어
    SESSION 타입 파라미터만으로 게이팅하면 그 패키지가 통째로 빠진다 — 실제 카탈로그의
    `WebAutomation/StartSessionWebAutomation`(return_type=SESSION)이 그 사례다.
    """

    def build() -> SessionRegistry:
        specs = list(_iter_specs(catalog))
        if not specs:
            return SessionRegistry(
                openers=fallback_openers,
                closers=fallback_closers,
                source="constants" if (fallback_openers or fallback_closers) else "empty",
            )

        openers: set[tuple[str, str]] = set()
        closers: set[tuple[str, str]] = set()

        # ① 카탈로그가 **명시한** 역할 — 게이팅 없이 채택한다.
        # 게이팅은 이름 정규식이 `File/Open` 같은 것을 opener로 잡는 것을 막으려고 있는데,
        # 명시 신호에는 그 위험이 없다. 이 두 신호가 없으면 세션 파라미터를 SESSION 타입으로
        # 선언하지 않은 패키지(`sessionName`을 TEXT로 받는 세대)가 통째로 빠진다.
        explicit_packages: set[str] = set()
        for spec in specs:
            pkg, act = spec.get("package"), spec.get("action")
            if not pkg or not act:
                continue
            role = str(spec.get("session_role") or "").strip().lower()
            rt = str(spec.get("return_type") or "").strip().upper()
            if role == "opener" or rt == _SESSION_PARAM_TYPE:
                openers.add((pkg, act))
                explicit_packages.add(pkg)
            elif role == "closer":
                closers.add((pkg, act))
                explicit_packages.add(pkg)

        # ② 이름 규칙 — 세션을 다루는 것이 확실한 패키지 안에서만.
        session_packages = {
            spec.get("package")
            for spec in specs
            if spec.get("package") and _has_session_param(spec)
        } | explicit_packages
        for spec in specs:
            pkg, act = spec.get("package"), spec.get("action")
            if not pkg or not act or pkg not in session_packages:
                continue
            if (pkg, act) in openers or (pkg, act) in closers:
                continue  # 명시 신호가 이긴다
            if _OPENER_RE.match(act):
                openers.add((pkg, act))
            elif _CLOSER_RE.match(act):
                closers.add((pkg, act))

        # ③ 닫기 보강 — opener를 보유한 패키지 **안에서만** substring으로 본다 (v3에서 이식).
        # 앞머리 앵커는 `excelAdvancedPackageCloseAction` 같은 슬러그 표기를 구조적으로 못
        # 잡는다(이름이 패키지명으로 시작한다). 여는 것만 유도되고 닫는 것이 안 잡히면
        # R8(세션 미종료)이 **모든 흐름도에서** 헛발화한다 — 그게 더 나쁘다.
        # 게이팅이 오탐을 막는다: opener가 없는 패키지의 `File/CloseHandle`은 안 걸린다.
        opener_packages = {p for p, _ in openers}
        for spec in specs:
            pkg, act = spec.get("package"), spec.get("action")
            if not pkg or not act or pkg not in opener_packages or (pkg, act) in openers:
                continue
            low = act.lower()
            if "close" in low or ("end" in low and "session" in low):
                closers.add((pkg, act))

        derived_count = len(openers) + len(closers)
        # 유도가 한쪽이라도 비면 그쪽만 폴백으로 채운다. 둘 다 유도되면 폴백은 안 섞는다 —
        # 죽은 표기를 되살려 다시 오탐을 만들 이유가 없다.
        source = "derived"
        if not openers and fallback_openers:
            openers = set(fallback_openers)
            source = "mixed"
        if not closers and fallback_closers:
            closers = set(fallback_closers)
            source = "mixed"
        if not openers and not closers:
            source = "empty"

        registry = SessionRegistry(
            openers=frozenset(openers),
            closers=frozenset(closers),
            session_packages=frozenset(p for p in session_packages if p),
            source=source,
            derived_count=derived_count,
        )
        if not registry.usable:
            logger.warning(
                "세션 어휘 유도 실패 — R7/R8을 건너뛴다 (opener=%d closer=%d source=%s). "
                "카탈로그 표기 세대가 바뀌었을 수 있다.",
                len(registry.openers), len(registry.closers), registry.source,
            )
        else:
            logger.info(
                "세션 어휘 유도: opener=%d closer=%d 세션패키지=%d source=%s",
                len(registry.openers), len(registry.closers),
                len(registry.session_packages), registry.source,
            )
        return registry

    return _cached(catalog, "session_registry", build)


def derive_container_exceptions(catalog) -> frozenset[tuple[str, str]]:
    """컨테이너 패키지 소속이지만 본문(children)을 갖지 않는 액션 — Break·Continue·Throw.

    v3는 이걸 수기 3쌍으로 들고 있었는데 현행 카탈로그에 전부 부재라 `Loop/Break`가
    컨테이너로 오판됐다. 카탈로그 실재 액션에서 뽑으면 표기 세대를 따라간다.
    """

    def build() -> frozenset[tuple[str, str]]:
        found = {
            (spec["package"], spec["action"])
            for spec in _iter_specs(catalog)
            if spec.get("package") in CONTAINER_PACKAGES
            and spec.get("action")
            and (loop_signal_role(spec["action"]) is not None or eh_role(spec["action"]) == "throw")
        }
        return frozenset(found)

    return _cached(catalog, "container_exceptions", build)


def derive_structural_actions(catalog) -> tuple[tuple[str, str], ...]:
    """제어 흐름 액션 전량 — 후보 메뉴 결정론 보완용.

    요구사항 문장에는 "반복한다"·"오류를 처리한다"가 명시되지 않아 검색 질의가 생성되지
    않는다. v3는 이걸 33쌍 병기 목록으로 보완했는데 `('Loop','Loop')` 같은 표기가 카탈로그와
    어긋나 정작 Loop 이터레이터가 메뉴에서 빠졌다(실제 이름은 `Loop action for data iteration`).
    카탈로그 실재 액션을 전량 열거하면 그 어긋남이 없어진다.

    반환은 (package, action) 튜플 — 패키지·액션명 순 정렬로 결정론적이다.
    """

    def build() -> tuple[tuple[str, str], ...]:
        found = [
            (spec["package"], spec["action"])
            for spec in _iter_specs(catalog)
            if spec.get("package") in CONTAINER_PACKAGES and spec.get("action")
        ]
        return tuple(sorted(set(found)))

    return _cached(catalog, "structural_actions", build)


# 액션 이름 꼬리의 "action in <패키지> package" — 같은 기능인데 패키지마다 표기가 달라
# 겹침 계산이 헛돈다(`Close action in Excel advanced package` 대 `Close`).
_ACTION_SUFFIX_RE = re.compile(r"\s*action in .*? package\s*$", re.IGNORECASE)
# 경쟁 판정 임계 — 실측(2026-07-28) 기준으로 잡았다:
#   Excel advanced ↔ Microsoft 365 Excel  겹침 24 · 47%  → 경쟁 (잡아야 함)
#   Excel advanced ↔ Excel basic          겹침 12 · 100% → 경쟁
#   Google Sheets  ↔ Microsoft 365 Excel  겹침 21 · 60%  → 경쟁
#   File           ↔ Folder               겹침  6 · 67%  → 아님 (개수로 걸러짐)
#   Browser        ↔ Recorder             겹침  0        → 아님
_COMPETE_MIN_SHARED = 8
_COMPETE_MIN_RATIO = 0.4


def _norm_action(name: str) -> str:
    return _ACTION_SUFFIX_RE.sub("", (name or "").strip().lower())


def derive_competing_packages(catalog) -> tuple[frozenset[str], ...]:
    """같은 일을 하는 패키지 묶음 — (Excel advanced, Microsoft 365 Excel, …) 같은 역할군.

    ## 왜 필요한가

    A360에는 같은 일을 하는 패키지가 여럿이다(스프레드시트 6종, 메일 5종). 이들은 **세션
    모델이 각자**라서 `Excel advanced/Open`이 연 세션을 `Microsoft 365 Excel/Format cell`이
    못 쓴다 — 섞으면 실행이 깨진다. 실측(2026-07-28) 흐름도 18개 중 6개가 엑셀 패키지를
    섞었고, 한 흐름도는 3종을 함께 썼다.

    ## 왜 유도인가

    수기 목록은 카탈로그가 바뀌면 썩는다(이 파일의 다른 유도 함수들과 같은 이유). 액션 이름
    집합의 겹침으로 판정하되, 표기 꼬리를 정규화한다 — 정규화 전에는 Excel advanced ↔
    Microsoft 365 Excel이 26%로 임계 아래였다.

    합집합-찾기로 **연결 성분**을 만든다: A↔B와 B↔C가 각각 임계를 넘으면 A·B·C가 한 군이다
    (Excel advanced ↔ Excel basic ↔ Microsoft 365 Excel이 이렇게 이어진다).

    ⚠ 완벽하지 않다 — `Word ↔ PowerPoint`처럼 파일 조작 액션이 닮아 함께 묶이는 오탐이 있다.
    그래서 이걸 쓰는 검수 규칙(R19)은 blocker가 아니라 major다.
    """

    def build() -> tuple[frozenset[str], ...]:
        by_pkg: dict[str, set[str]] = {}
        for spec in _iter_specs(catalog):
            pkg, act = spec.get("package"), spec.get("action")
            if pkg and act:
                by_pkg.setdefault(pkg, set()).add(_norm_action(act))

        names = sorted(by_pkg)
        parent = {n: n for n in names}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i, a in enumerate(names):
            for b in names[i + 1:]:
                sa, sb = by_pkg[a], by_pkg[b]
                shared = len(sa & sb)
                if shared < _COMPETE_MIN_SHARED:
                    continue
                if shared / min(len(sa), len(sb)) < _COMPETE_MIN_RATIO:
                    continue
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)

        groups: dict[str, set[str]] = {}
        for n in names:
            groups.setdefault(find(n), set()).add(n)
        return tuple(sorted(
            (frozenset(g) for g in groups.values() if len(g) > 1),
            key=lambda g: (-len(g), sorted(g)[0]),
        ))

    return _cached(catalog, "competing_packages", build)


def derive_packages(catalog) -> tuple[tuple[str, int], ...]:
    """카탈로그 패키지 목록 — (패키지명, 액션 수), 액션 수 내림차순.

    질의 설계자에게 줄 **어휘 사전**이다. 조사 질의를 만드는 LLM은 [액션 후보 메뉴]를 볼 수
    없다 — 그건 조사 *결과*라 아직 없다. 그래서 A360 어휘를 모른 채 어휘 검색용 질의를
    쓰라는 요구를 받고, 업무 문장을 그대로 내놓는다.

    실측(2026-07-28)이 그 대가를 보여준다. 같은 `Recorder/Click`을 찾는데:
        "네이버 메인에서 증권 클릭하고 국내 금 시세 조회"  → SAP/Click menu  0.315
        "click element on screen recorder"              → Recorder/Click  0.773
    패키지 이름이 든 질의는 0.66~0.91, 없는 질의는 0.31~0.49로 갈렸다. 조사 단계가 못 찾은
    액션을 composer가 escape hatch로 뒤늦게 찾아오고 있었다(같은 턴 12초 뒤, 두 배 점수).

    액션 수를 함께 주는 이유: 어휘가 많은 패키지가 그 영역의 주력이라 선택 힌트가 된다.
    """

    def build() -> tuple[tuple[str, int], ...]:
        counts: dict[str, int] = {}
        for spec in _iter_specs(catalog):
            if spec.get("package") and spec.get("action"):
                counts[spec["package"]] = counts.get(spec["package"], 0) + 1
        return tuple(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    return _cached(catalog, "packages", build)
