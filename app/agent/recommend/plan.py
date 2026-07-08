"""plan 노드 — 단계별 검색 계획을 세우고 병렬 처리를 fan-out한다.

두 가지 결정론 신호를 뽑아 shortlist가 메뉴판을 조립할 때 쓴다:
1. 컨테이너 강제 후보: 반복/조건 서술이 보이면 Loop/If 스키마를 검색 운에 맡기지
   않고 후보에 강제 포함한다(단, 선택은 compose가 예제 근거로 한다 — 강제 사용 아님).
2. 패키지 프리맵: 시스템/키워드로 우선 검색할 패키지를 힌트한다.

신호 규칙은 문자열 매칭이라 오탐이 있을 수 있으므로 '후보 주입'까지만 하고
확정하지 않는다 — 실제 채택 여부와 값은 compose+check가 판단한다.
"""

import re

from langgraph.types import Send

from .state import RecommendState, StepPayload

# --- 컨테이너 신호: 반복/조건 서술 → (package, action) 강제 후보 ---
_LOOP_PATTERNS = re.compile(r"반복|각각|마다|모든|every|each|최근\s*\d+|상위\s*\d+|\d+\s*(일치|건|개|회|번)")
_IF_PATTERNS = re.compile(r"인\s*경우|일\s*때|이면|없으면|실패\s*시|조건|만약|if\b|경우에\s*따라")

_LOOP_CANDIDATE = ("Loop", "loop.commands.start")
_IF_CANDIDATE = ("If", "if")

# --- 패키지 프리맵: 시스템/키워드 → 우선 검색 패키지 ---
_PREFMAP_RULES: list[tuple[re.Pattern, list[str]]] = [
    (re.compile(r"edge|chrome|브라우저|browser|웹|url|사이트|접속|클릭|페이지"), ["WebAutomation", "Browser"]),
    (re.compile(r"엑셀|excel|스프레드시트|시트|셀|xlsx|테두리|서식|매크로"), ["Excel_MS"]),
    (re.compile(r"메일|이메일|email|발송|outlook|knox|smtp"), ["Email"]),
    (re.compile(r"pdf"), ["PDF"]),
    (re.compile(r"데이터\s*테이블|필터|정렬|집계|가공"), ["Excel_MS"]),
]


def detect_container_candidates(step: dict) -> list[tuple[str, str]]:
    """단계의 branching/description에서 컨테이너 강제 후보를 뽑는다."""
    text = " ".join(
        str(step.get(k) or "")
        for k in ("branching", "description", "name")
    ) + " ".join(step.get("inputs", []) + step.get("outputs", []))
    candidates: list[tuple[str, str]] = []
    if _LOOP_PATTERNS.search(text):
        candidates.append(_LOOP_CANDIDATE)
    if _IF_PATTERNS.search(text):
        candidates.append(_IF_CANDIDATE)
    return candidates


def prefmap_packages(step: dict) -> list[str]:
    """단계의 systems/name/description에서 우선 검색 패키지를 힌트한다."""
    haystack = " ".join(
        str(step.get(k) or "") for k in ("name", "description", "branching")
    ) + " " + " ".join(step.get("systems", []))
    haystack = haystack.lower()
    packages: list[str] = []
    for pattern, pkgs in _PREFMAP_RULES:
        if pattern.search(haystack):
            for p in pkgs:
                if p not in packages:
                    packages.append(p)
    return packages


def plan_node(state: RecommendState) -> dict:
    """진행 이벤트만 방출하고 상태는 바꾸지 않는다 (실질 fan-out은 조건부 엣지)."""
    from .stream import emit

    steps = state.get("analysis", {}).get("steps", [])
    emit({"event": "stage", "stage": "recommending",
          "message": f"{len(steps)}개 업무 단계별 추천 계획 수립 중"})
    return {}


def fan_out(state: RecommendState) -> list[Send]:
    """분석의 각 단계를 병렬 step 노드로 보낸다 (Send)."""
    constraints = state.get("constraints") or []
    sends: list[Send] = []
    for i, step in enumerate(state.get("analysis", {}).get("steps", []), start=1):
        payload: StepPayload = {"step": step, "order": i, "constraints": constraints}
        sends.append(Send("step", payload))
    return sends
