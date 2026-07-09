"""정적 체커 (R1~R8) — 추천 액션 트리를 카탈로그 스펙으로 검사한다.

트리 1회 순회, LLM·DB 없음. 위반은 위치(location)·규칙(rule)과 함께 반환하며,
recommend의 check 노드가 이를 confidence 산정·repair 트리거·notes 후보로 쓴다.

규칙:
  R1 (package, action)이 카탈로그에 존재            — 골드셋 직결(문자열 매칭). 최우선.
  R2 parameters[].name이 액션 스펙에 존재
  R3 required(NOT_EMPTY) 파라미터에 값 존재          — 없으면 needs_input 후보
  R4 RADIO/SELECT 값이 options enum 안
  R5 NUMBER/BOOLEAN 값의 형식
  R6 children은 컨테이너 액션에만                      — 트리 구조 정합

R1~R6은 액션 하나의 '문법' 검사라 단계별(run_checks)로 돈다. R1 실패(스펙 부재)면
그 액션의 R2~R5는 검사하지 않는다(스펙이 없으면 판정 불가).

  R7 세션을 열기 전에/닫은 뒤에 쓰지 않는가          — 세션 순서(실행 흐름 dryrun)
  R8 연 세션을 끝까지 닫는가                          — 세션 미종료(리소스 누수)

R7~R8은 액션들이 '순서상 말이 되는가'를 보는 심볼릭 dryrun이라 단계 경계를 넘어
전체 흐름도(run_session_checks)를 실행 순서로 순회한다. 변수 흐름은 흐름도 모델에
액션↔변수 연결 정보가 없어 이 범위에서 제외한다(스키마 확장이 선행돼야 함).
"""

from collections.abc import Iterator
from dataclasses import dataclass, field

from .catalog import CatalogLookup

# 본문(children)을 가질 수 있는 컨테이너 액션. A360에는 임의 병합점이 없어
# 분기/반복 블록이 끝나면 다음 형제로 이어진다 — 컨테이너만 children을 갖는다.
CONTAINER_ACTIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("Loop", "loop.commands.start"),
        ("If", "if"),
        ("If", "else"),
        ("If", "elseIf"),
        ("Step", "step"),
        ("ErrorHandler", "try"),
        ("ErrorHandler", "catch"),
        ("ErrorHandler", "finally"),
    }
)

# RADIO/SELECT처럼 값이 정해진 선택지 안에 있어야 하는 타입 (R4 대상).
_ENUM_TYPES = {"RADIO", "SELECT"}

# --- 세션 생명주기 (R7~R8) ---
# 세션을 여는/닫는 액션. 세션 이름은 아래 SESSION_PARAM_NAMES 파라미터 값으로 식별한다.
# 세션 파라미터로 이름을 갖는 대표 패키지만 추적한다(Email 등 이름 없는 연결은 제외).
# 실제 카탈로그에 세션 패키지가 늘면 여기에 (package, action)을 추가한다.
SESSION_OPENERS: frozenset[tuple[str, str]] = frozenset(
    {
        ("Excel_MS", "OpenSpreadsheet"),
        ("Excel_MS", "CreateSpreadsheet"),
        ("WebAutomation", "StartSessionWebAutomation"),
    }
)
SESSION_CLOSERS: frozenset[tuple[str, str]] = frozenset(
    {
        ("Excel_MS", "CloseSpreadsheet"),
        ("WebAutomation", "EndSessionWebAutomation"),
    }
)
# 세션 이름을 담는 파라미터 이름 (패키지별 표기 차이).
SESSION_PARAM_NAMES = ("session", "sessionName")


@dataclass
class Violation:
    """검수 위반 한 건. repair 프롬프트가 rule·location·message·spec_excerpt를 쓴다."""

    rule: str  # "R1"~"R8"
    location: str  # 트리 경로, 예: "actions[1].children[0]"
    message: str
    package: str | None = None
    action: str | None = None
    param: str | None = None
    step_id: str | None = None  # R7~R8은 단계 경계를 넘으므로 위반 액션의 단계를 싣는다
    spec_excerpt: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "location": self.location,
            "message": self.message,
            "package": self.package,
            "action": self.action,
            "param": self.param,
            "step_id": self.step_id,
        }


def _is_empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def _check_parameters(action: dict, spec: dict, location: str) -> list[Violation]:
    """R2~R5: 파라미터 name·필수·enum·형식 검사. spec이 있는 액션에만 호출된다."""
    violations: list[Violation] = []
    pkg, act = action.get("package"), action.get("action")
    spec_params = {p["name"]: p for p in spec.get("parameters", [])}
    given = {p.get("name"): p for p in action.get("parameters", []) if p.get("name")}

    # R2: 준 파라미터 이름이 스펙에 있는가
    for name in given:
        if name not in spec_params:
            violations.append(
                Violation(
                    "R2", location,
                    f"파라미터 '{name}'은(는) {pkg}/{act} 스펙에 없습니다.",
                    package=pkg, action=act, param=name,
                    spec_excerpt={"valid_params": list(spec_params)},
                )
            )

    for name, pspec in spec_params.items():
        provided = given.get(name)
        # R3: 필수인데 값이 없거나 안 줌
        if pspec.get("required"):
            if provided is None or _is_empty(provided.get("value")):
                violations.append(
                    Violation(
                        "R3", location,
                        f"필수 파라미터 '{name}'({pspec.get('label') or name})에 값이 없습니다.",
                        package=pkg, action=act, param=name,
                        spec_excerpt={"type": pspec.get("type"), "required": True},
                    )
                )
        if provided is None or _is_empty(provided.get("value")):
            continue

        value = provided.get("value")
        # R4: enum 타입은 값이 options 안에 있어야
        if pspec.get("type") in _ENUM_TYPES and "options" in pspec:
            allowed = {o.get("value") for o in pspec["options"]} | {o.get("label") for o in pspec["options"]}
            if value not in allowed:
                violations.append(
                    Violation(
                        "R4", location,
                        f"'{name}' 값 '{value}'은(는) 허용된 선택지가 아닙니다.",
                        package=pkg, action=act, param=name,
                        spec_excerpt={"options": [o.get("value") for o in pspec["options"]]},
                    )
                )
        # R5: NUMBER/BOOLEAN 형식 (경량)
        elif pspec.get("type") == "NUMBER" and not _is_number(value):
            violations.append(
                Violation(
                    "R5", location, f"'{name}'은(는) 숫자여야 하는데 '{value}'입니다.",
                    package=pkg, action=act, param=name,
                )
            )
        elif pspec.get("type") == "BOOLEAN" and not isinstance(value, bool):
            violations.append(
                Violation(
                    "R5", location, f"'{name}'은(는) 참/거짓이어야 합니다.",
                    package=pkg, action=act, param=name,
                )
            )
    return violations


def _is_number(value) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    try:
        float(str(value))
        return True
    except (TypeError, ValueError):
        return False


def _check_action(action: dict, catalog: CatalogLookup, location: str) -> list[Violation]:
    violations: list[Violation] = []
    pkg, act = action.get("package"), action.get("action")
    children = action.get("children") or []

    spec = catalog.get_action_schema(pkg, act) if pkg and act else None

    # R1: 카탈로그 존재 (골드셋 직결)
    if spec is None:
        violations.append(
            Violation(
                "R1", location,
                f"'{pkg}/{act}'은(는) 카탈로그에 없는 액션입니다.",
                package=pkg, action=act,
            )
        )
        # 스펙이 없으면 파라미터 판정 불가 — R2~R5 스킵, children은 계속 순회
    else:
        violations.extend(_check_parameters(action, spec, location))

    # R6: children은 컨테이너 액션에만
    if children and (pkg, act) not in CONTAINER_ACTIONS:
        violations.append(
            Violation(
                "R6", location,
                f"'{pkg}/{act}'은(는) 컨테이너가 아닌데 children이 있습니다.",
                package=pkg, action=act,
            )
        )

    for i, child in enumerate(children):
        violations.extend(_check_action(child, catalog, f"{location}.children[{i}]"))
    return violations


def run_checks(actions: list[dict], catalog: CatalogLookup) -> list[Violation]:
    """액션 트리(한 단계의 actions[])를 R1~R6로 검사해 위반 목록을 반환한다.

    actions: RecommendedAction.model_dump() 리스트 또는 동형 dict 리스트.
    """
    violations: list[Violation] = []
    for i, action in enumerate(actions):
        violations.extend(_check_action(action, catalog, f"actions[{i}]"))
    return violations


# ─────────────────────────────────────────────────────────────────────────────
# R7~R8 세션 생명주기 (전체 흐름도 심볼릭 dryrun)
# ─────────────────────────────────────────────────────────────────────────────

def _session_name(action: dict) -> str | None:
    """액션의 세션 파라미터(session/sessionName) 값을 세션 이름으로 반환. 없으면 None.

    'Default'도 유효한 세션 이름이다 — A360에서 Default 세션도 명시적으로 열어야 한다.
    """
    for p in action.get("parameters", []):
        if p.get("name") in SESSION_PARAM_NAMES:
            value = p.get("value")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _iter_execution_order(actions: list[dict], path: str) -> Iterator[tuple[dict, str]]:
    """액션 트리를 실행 순서(리스트 순서 = compose가 매긴 order)로 pre-order 순회한다.

    분기(If)/반복(Loop)은 심볼릭 근사다: children을 선언 순서대로 편다. 실제로는 분기
    한쪽만 실행되거나 반복이 여러 번 돌지만, 세션 열림/닫힘의 '선언 순서상 정합'을 보는
    데는 이 근사로 충분하다(대부분의 세션 오용은 선형 경로에서 난다).
    """
    for i, action in enumerate(actions):
        location = f"{path}[{i}]"
        yield action, location
        yield from _iter_execution_order(action.get("children") or [], f"{location}.children")


def run_session_checks(steps: list[dict]) -> list[Violation]:
    """전체 흐름도를 실행 순서로 순회하며 세션 생명주기(R7~R8)를 검사한다.

    steps: Recommendation.steps[] (각 {step_id, actions[]}). 카탈로그는 쓰지 않는다 —
    opener/closer는 (package, action) 상수로 판정하고, 세션 이름은 파라미터 값으로 좁는다.

    R7: 세션 파라미터를 쓰는 액션이 열려 있지 않은 세션을 참조(열기 전/닫은 후).
    R8: 순회가 끝났는데 닫히지 않은 채 남은 세션(리소스 누수).
    """
    opened: dict[str, tuple[str | None, str]] = {}  # session_name -> (step_id, location) 연 지점
    violations: list[Violation] = []

    for step in steps:
        step_id = step.get("step_id")
        for action, location in _iter_execution_order(step.get("actions", []), "actions"):
            key = (action.get("package"), action.get("action"))
            name = _session_name(action)
            if key in SESSION_OPENERS:
                if name:
                    opened[name] = (step_id, location)
            elif key in SESSION_CLOSERS:
                if name:
                    opened.pop(name, None)
            elif name is not None and name not in opened:
                violations.append(
                    Violation(
                        "R7", location,
                        f"세션 '{name}'이(가) 열려 있지 않은 상태에서 사용됩니다 "
                        "(여는 액션보다 먼저 오거나 닫은 뒤에 옵니다).",
                        package=action.get("package"), action=action.get("action"),
                        step_id=step_id,
                    )
                )

    for name, (step_id, location) in opened.items():
        violations.append(
            Violation(
                "R8", location,
                f"세션 '{name}'을(를) 연 뒤 닫지 않았습니다 (닫는 액션이 없습니다).",
                step_id=step_id,
            )
        )
    return violations
