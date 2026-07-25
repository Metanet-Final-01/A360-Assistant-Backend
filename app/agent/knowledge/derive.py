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
from collections.abc import Iterator
from dataclasses import dataclass, field

from .lexicon import CONTAINER_PACKAGES, eh_role, loop_signal_role

logger = logging.getLogger(__name__)

# 세션을 여는/닫는 액션의 이름 패턴. 액션명 **앞머리**만 본다 — 중간 일치를 허용하면
# "Close spreadsheet after reopen" 같은 서술형 이름이 양쪽에 다 걸린다.
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


# 프로세스 캐시 — catalog 객체 id 기준. BackendCatalog가 이미 TTL 캐시라 이중 비용이 없다.
_CACHE: dict[tuple[int, str], object] = {}


def clear_cache() -> None:
    """유도 캐시를 비운다 — 테스트·재적재용."""
    _CACHE.clear()


def _cached(catalog, key: str, build):
    ck = (id(catalog), key)
    if ck not in _CACHE:
        _CACHE[ck] = build()
    return _CACHE[ck]


def derive_session_registry(
    catalog,
    *,
    fallback_openers: frozenset[tuple[str, str]] = frozenset(),
    fallback_closers: frozenset[tuple[str, str]] = frozenset(),
) -> SessionRegistry:
    """세션 opener/closer를 카탈로그에서 유도한다. 유도가 비면 폴백 상수를 쓴다.

    게이팅이 핵심이다 — 액션명 정규식만 쓰면 세션과 무관한 `File/Open`이 opener로 잡힌다.
    """

    def build() -> SessionRegistry:
        specs = list(_iter_specs(catalog))
        if not specs:
            return SessionRegistry(
                openers=fallback_openers,
                closers=fallback_closers,
                source="constants" if (fallback_openers or fallback_closers) else "empty",
            )

        session_packages = {
            spec.get("package")
            for spec in specs
            if spec.get("package") and _has_session_param(spec)
        }

        openers: set[tuple[str, str]] = set()
        closers: set[tuple[str, str]] = set()
        for spec in specs:
            pkg, act = spec.get("package"), spec.get("action")
            if not pkg or not act or pkg not in session_packages:
                continue
            if _OPENER_RE.match(act):
                openers.add((pkg, act))
            elif _CLOSER_RE.match(act):
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
