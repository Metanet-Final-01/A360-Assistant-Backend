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
그 액션의 R2~R5는 검사하지 않는다(스펙이 없으면 판정 불가). 스펙이 있어도 파라미터가
미상이면(params_unknown 행 — parameters 키 부재) R2~R5를 건너뛴다(모름 → 침묵).

  R7 세션을 열기 전에/닫은 뒤에 쓰지 않는가          — 세션 순서(실행 흐름 dryrun)
  R8 연 세션을 끝까지 닫는가                          — 세션 미종료(리소스 누수)

R7~R8은 액션들이 '순서상 말이 되는가'를 보는 심볼릭 dryrun이라 단계 경계를 넘어
전체 흐름도를 실행 순서로 순회한다. v3에서 분기 인지로 정밀화됐다: If 분기는 상태를
fork해 병합점(다음 형제)에서 비교하고, Loop 본문은 반복 누수를 검사한다.

v3 신설 (run_flow_checks가 통합 실행):
  R9  def-before-use — consumes 변수가 실행 경로상 앞서 produces/입력 선언되지 않음
  R10 dead output — produces 됐으나 아무도 consumes 안 함            (warning)
  R11 변수 타입 정합 — $var$ 참조 파라미터의 기대 타입과 변수 타입 불일치
  R12 표준 골격 적합성 — 예외 처리 부재·Finally 밖 세션 닫기          (warning)

R9~R11의 원료는 스키마 확장 필드 produces/consumes(app/schemas/recommendation.py의
VarRef)다. composer 명시가 1차이고 `$var$` 파싱이 교차 보정한다 — 흐름도에 produces
명시가 하나도 없으면 R9/R10은 침묵한다(정보 없이 검사하면 전부 오탐이므로).
세션 opener/closer는 수기 상수에 더해 카탈로그 메타(return_type=SESSION 등)에서
유도한다(derive_session_registry) — 커버리지가 상수 3개 패키지에 갇히지 않게.
"""

import re
from dataclasses import dataclass, field

from .catalog import CatalogLookup

# 본문(children)을 가질 수 있는 컨테이너 액션. A360에는 임의 병합점이 없어
# 분기/반복 블록이 끝나면 다음 형제로 이어진다 — 컨테이너만 children을 갖는다.
#
# 판정은 패키지 단위다(RPA-141): 카탈로그가 llm_agent 소싱으로 바뀌며 액션명이 문서 슬러그
# 기반 camelCase가 됐고(예: Error handler/errorHandlerTry, Loop/cloudUsingLoopAction),
# Loop 패키지엔 iterator 변형이 20여 개라 (package, action) 열거는 표기가 바뀔 때마다
# 헛위반(R6)을 만든다 — 실제로 구 봇 JSON 표기(ErrorHandler/try)가 전부 불일치해
# 에이전트가 Loop/If/Try를 올바르게 써도 재생성을 유발했다. A360에서 본문을 갖는 건
# 이 제어 흐름 패키지들뿐이므로 패키지로 판정하고, 본문이 없는 게 명백한 액션만 뺀다.
CONTAINER_PACKAGES: frozenset[str] = frozenset(
    {"Loop", "If", "Step", "Error handler", "Trigger loop"}
)
# 컨테이너 패키지 소속이지만 본문(children)을 갖지 않는 액션 — 제어 이동/신호뿐이다.
NON_CONTAINER_ACTIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("Loop", "loopPackageBreakAction"),
        ("Loop", "loopPackageContinueAction"),
        ("Error handler", "errorHandlerThrow"),
    }
)


def is_container(package: str | None, action: str | None) -> bool:
    """이 액션이 children(본문)을 가질 수 있는 컨테이너인지 판정한다 — R6 기준."""
    return package in CONTAINER_PACKAGES and (package, action) not in NON_CONTAINER_ACTIONS

# RADIO/SELECT처럼 값이 정해진 선택지 안에 있어야 하는 타입 (R4 대상).
_ENUM_TYPES = {"RADIO", "SELECT"}


def _opt_value(o: object) -> object:
    """옵션 원소에서 value를 뽑는다 — 파서가 dict로 정규화 못 한 문자열 옵션도 견딘다."""
    return o.get("value") if isinstance(o, dict) else o


def _opt_label(o: object) -> object:
    """옵션 원소에서 label을 뽑는다 — dict가 아닌 문자열 옵션도 견딘다."""
    return o.get("label") if isinstance(o, dict) else o

# --- 세션 생명주기 (R7~R8) ---
# 세션을 여는/닫는 액션 — 현행 카탈로그(llm_agent 소싱) 표기다(RPA-141: 구 JAR 표기
# Excel_MS/OpenSpreadsheet 등은 카탈로그에 없어 R7/R8이 한 번도 발화하지 못했다).
# 현 카탈로그의 open 계열은 세션 이름 파라미터가 없고 세션을 '리턴'한다(cloudExcelOpen 등)
# — 그래서 이름 없는 열림도 추적한다(아래 _ANON). 사용/닫기의 세션 참조는 패키지마다 다르다:
# Excel advanced·Word는 sessionName 파라미터를 받고, Browser는 세션 파라미터가 아예 없어
# (close는 target만 필수) 이름 없는 열림·닫힘이 _ANON 매칭으로 짝지어진다.
SESSION_OPENERS: frozenset[tuple[str, str]] = frozenset(
    {
        ("Excel advanced", "cloudExcelOpen"),
        ("Excel advanced", "excelAdvancedPackageCreateWorkbookAction"),
        ("Browser", "browserPackageOpenAction"),
        ("Word", "mswordOpenDocument"),
    }
)
SESSION_CLOSERS: frozenset[tuple[str, str]] = frozenset(
    {
        ("Excel advanced", "excelAdvancedPackageCloseAction"),
        ("Browser", "browserPackageCloseAction"),
        ("Word", "mswordCloseDocument"),
    }
)
# 세션 이름을 담는 파라미터 이름 (패키지별 표기 차이).
SESSION_PARAM_NAMES = ("session", "sessionName")
# 이름 없는 열림(세션을 리턴하는 open)의 세션 키 — 같은 패키지의 이름 참조를 관대하게 덮는다.
_ANON = "__anon__"


@dataclass
class Violation:
    """검수 위반 한 건. repair 프롬프트가 rule·location·message·spec_excerpt를 쓴다."""

    rule: str  # "R1"~"R12"
    location: str  # 트리 경로, 예: "actions[1].children[0]"
    message: str
    package: str | None = None
    action: str | None = None
    param: str | None = None
    step_id: str | None = None  # R7~R8은 단계 경계를 넘으므로 위반 액션의 단계를 싣는다
    spec_excerpt: dict = field(default_factory=dict)
    severity: str = "error"  # "error"|"warning" — warning은 감점·심판 앵커용(교정 강제 비대상)

    def as_dict(self) -> dict:
        """위반을 repair 프롬프트·관측 로그용 dict로 직렬화한다."""
        return {
            "rule": self.rule,
            "location": self.location,
            "message": self.message,
            "package": self.package,
            "action": self.action,
            "param": self.param,
            "step_id": self.step_id,
            "severity": self.severity,
        }


def _is_empty(value) -> bool:
    """값이 비었는지 판정한다 — None·빈 문자열(공백 포함)·빈 리스트/딕트."""
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
    if spec.get("parameters") is None:
        # 파라미터 스펙 미상(BackendCatalog params_unknown 행 — schema 없는 v2 문서 카탈로그
        # 행) — 존재(R1)만 성립하고 R2~R5는 판정 근거가 없다. R3의 required tri-state와 같은
        # '모름 → 침묵' 원칙. 빈 목록([])은 '파라미터 없음' 확정이므로 아래로 진행해 R2가 잡는다.
        return violations
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
            allowed = {_opt_value(o) for o in pspec["options"]} | {_opt_label(o) for o in pspec["options"]}
            if value not in allowed:
                violations.append(
                    Violation(
                        "R4", location,
                        f"'{name}' 값 '{value}'은(는) 허용된 선택지가 아닙니다.",
                        package=pkg, action=act, param=name,
                        spec_excerpt={"options": [_opt_value(o) for o in pspec["options"]]},
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
    """값이 숫자로 해석 가능한지 판정한다(bool은 숫자로 치지 않는다)."""
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
    """액션 하나를 R1(카탈로그 존재)·R2~R5(파라미터)·R6(children 컨테이너)로 검사하고 children을 재귀한다."""
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
    if children and not is_container(pkg, act):
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
# 세션 레지스트리 유도 (v3) — 수기 상수 + 카탈로그 메타
# ─────────────────────────────────────────────────────────────────────────────

def derive_session_registry(catalog=None) -> tuple[frozenset, frozenset]:
    """세션 opener/closer 집합을 수기 상수 + 카탈로그 메타에서 유도한다.

    - opener: 스펙 return_type이 SESSION인 액션(세션을 '리턴'하는 open 계열 — cloudExcelOpen형).
    - closer: opener를 보유한 패키지에서 액션명이 close / end+session 패턴인 액션.
      (닫기는 스펙에 구조 신호가 없어 이름 휴리스틱이다 — opener 보유 패키지로 좁혀 오탐 방지.)

    카탈로그가 순회를 지원하지 않으면(iter_action_schemas 부재 — 테스트 스텁, v2 카탈로그)
    상수만 반환한다. 실제 세션 패키지는 DLL·DataRobot·XML 등 상수 3개보다 훨씬 많아
    (RAG_CATALOG 실측) 유도가 커버리지를 넓힌다.
    """
    openers = set(SESSION_OPENERS)
    closers = set(SESSION_CLOSERS)
    iter_fn = getattr(catalog, "iter_action_schemas", None)
    if callable(iter_fn):
        try:
            specs = [s for s in iter_fn() if isinstance(s, dict)]
        except Exception:  # noqa: BLE001 — 유도 실패가 검사 자체를 막으면 안 된다(상수 폴백)
            specs = []
        for s in specs:
            pkg, act = s.get("package"), s.get("action")
            rt = s.get("return_type")
            if pkg and act and isinstance(rt, str) and rt.strip().upper() == "SESSION":
                openers.add((pkg, act))
        opener_pkgs = {p for p, _ in openers}
        for s in specs:
            pkg, act = s.get("package"), s.get("action")
            if not pkg or not act or pkg not in opener_pkgs:
                continue
            low = act.lower()
            if "close" in low or ("end" in low and "session" in low):
                closers.add((pkg, act))
    return frozenset(openers), frozenset(closers)


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


# ─────────────────────────────────────────────────────────────────────────────
# 실행 단위 분해 — If 분기 그룹 / Error handler 그룹 / Loop / 일반 액션 (v3)
# ─────────────────────────────────────────────────────────────────────────────

def _if_role(action_name: str | None) -> str:
    """If 패키지 액션의 분기 역할 판정 — 'if'|'elseif'|'else' (프론트 branchRole과 동일 계열).

    카탈로그 세대에 따라 표기가 달라(RPA-141) 정확 명칭 대신 부분 문자열로 판정한다.
    """
    low = (action_name or "").lower().replace("_", "").replace(" ", "")
    if "elseif" in low:
        return "elseif"
    if "else" in low:
        return "else"
    return "if"


def _eh_role(action_name: str | None) -> str:
    """Error handler 패키지 액션의 역할 판정 — 'try'|'catch'|'finally'|'other'."""
    low = (action_name or "").lower()
    for role in ("finally", "catch", "try"):
        if role in low:
            return role
    return "other"


def _split_units(actions: list[dict]) -> list[tuple[str, list[tuple[int, dict]]]]:
    """형제 액션 리스트를 실행 단위로 묶는다.

    반환 원소: (kind, [(sibling_index, action), ...])
      - "if_group": 원초 If + 뒤따르는 Else If/Else 형제들 (분기 fork 대상)
      - "eh_group": Try + 뒤따르는 Catch/Finally 형제들
      - "action":  그 외 단일 액션 (Loop/Step 컨테이너 포함 — 처리기에서 재귀)
    """
    units: list[tuple[str, list[tuple[int, dict]]]] = []
    i = 0
    while i < len(actions):
        a = actions[i]
        pkg, act = a.get("package"), a.get("action")
        if pkg == "If" and is_container(pkg, act):
            group = [(i, a)]
            j = i + 1
            while j < len(actions) and actions[j].get("package") == "If" and _if_role(actions[j].get("action")) in ("elseif", "else"):
                group.append((j, actions[j]))
                j += 1
            units.append(("if_group", group))
            i = j
        elif pkg == "Error handler" and _eh_role(act) == "try":
            group = [(i, a)]
            j = i + 1
            while j < len(actions) and actions[j].get("package") == "Error handler" and _eh_role(actions[j].get("action")) in ("catch", "finally"):
                group.append((j, actions[j]))
                j += 1
            units.append(("eh_group", group))
            i = j
        else:
            units.append(("action", [(i, a)]))
            i += 1
    return units


# ─────────────────────────────────────────────────────────────────────────────
# R7~R8 세션 생명주기 — 분기 인지 심볼릭 실행 (v3)
# ─────────────────────────────────────────────────────────────────────────────

class _SessionWalker:
    """세션 상태를 들고 흐름도를 실행 순서로 걷는다 — If fork·병합 비교, Loop 누수, R12.

    세션은 (package, name|_ANON) 키로 식별한다(v2와 동일 — Excel의 'Default'와 Browser의
    'Default'는 다른 세션). 분기 불일치로 '아마 열림'이 된 키는 maybe 집합에 넣어 이후
    사용/닫기에서 오탐을 내지 않는다(정밀성보다 오탐 방지 우선 — v2 _ANON 관대 매칭의 계승).
    """

    def __init__(self, openers: frozenset, closers: frozenset, *, flow_has_eh: bool, emit_r12: bool) -> None:
        self.openers = openers
        self.closers = closers
        self.flow_has_eh = flow_has_eh
        self.emit_r12 = emit_r12
        # (package, name|_ANON) -> 열림 스택 [(step_id, location)]
        self.opened: dict[tuple[str, str], list[tuple[str | None, str]]] = {}
        self.maybe: set[tuple[str, str]] = set()
        self.violations: list[Violation] = []

    # -- 상태 스냅샷/복원 (If fork용) --
    def _snapshot(self) -> tuple[dict, set]:
        return ({k: list(v) for k, v in self.opened.items()}, set(self.maybe))

    @staticmethod
    def _merge_lenient(a: tuple[dict, set], b: tuple[dict, set]) -> tuple[dict, set]:
        """두 상태의 관대 병합 — 양쪽에 다 있는 열림만 확정(짧은 스택), 나머지는 maybe."""
        opened = {k: list(min((a[0][k], b[0][k]), key=len)) for k in set(a[0]) & set(b[0])}
        maybe = a[1] | b[1] | (set(a[0]) ^ set(b[0]))
        return opened, maybe

    def _restore(self, snap: tuple[dict, set]) -> None:
        self.opened = {k: list(v) for k, v in snap[0].items()}
        self.maybe = set(snap[1])

    def _pop_open(self, pkg: str, name: str | None) -> bool:
        """닫기 대상 열림을 찾아 pop한다 — 이름 일치 → _ANON → (이름 없는 close면) 아무 열림."""
        candidates = [(pkg, name)] if name else []
        candidates.append((pkg, _ANON))
        if name is None:
            candidates.extend(k for k in self.opened if k[0] == pkg)
        for key in candidates:
            stack = self.opened.get(key)
            if stack:
                stack.pop()
                if not stack:
                    self.opened.pop(key)
                return True
        # 분기에서만 열렸을 수 있는 키는 닫기를 조용히 수용한다 (오탐 방지)
        for key in ([(pkg, name)] if name else []) + [(pkg, _ANON)] + [k for k in self.maybe if k[0] == pkg]:
            if key in self.maybe:
                self.maybe.discard(key)
                return True
        return False

    def _process(self, action: dict, location: str, step_id: str | None, in_finally: bool) -> None:
        pkg = action.get("package")
        key = (pkg, action.get("action"))
        name = _session_name(action)
        if key in self.openers:
            self.opened.setdefault((pkg, name or _ANON), []).append((step_id, location))
        elif key in self.closers:
            if self.emit_r12 and self.flow_has_eh and not in_finally:
                self.violations.append(
                    Violation(
                        "R12", location,
                        "세션 닫기가 Finally 블록 밖에 있습니다 — 예외 발생 시 세션이 누수됩니다 "
                        "(A360 표준: 정리는 Finally에서).",
                        package=pkg, action=action.get("action"), step_id=step_id,
                        severity="warning",
                    )
                )
            if not self._pop_open(pkg, name):
                shown = name or "(이름 미지정)"
                self.violations.append(
                    Violation(
                        "R7", location,
                        f"세션 '{shown}'을(를) 열지 않았는데 닫으려 합니다 (닫을 세션이 없습니다).",
                        package=pkg, action=action.get("action"), step_id=step_id,
                    )
                )
        elif (
            name is not None
            and (pkg, name) not in self.opened
            and (pkg, _ANON) not in self.opened
            and (pkg, name) not in self.maybe
            and (pkg, _ANON) not in self.maybe
        ):
            self.violations.append(
                Violation(
                    "R7", location,
                    f"세션 '{name}'이(가) 열려 있지 않은 상태에서 사용됩니다 "
                    "(여는 액션보다 먼저 오거나 닫은 뒤에 옵니다).",
                    package=pkg, action=action.get("action"), step_id=step_id,
                )
            )

    def walk(self, actions: list[dict], path: str, step_id: str | None, in_finally: bool = False) -> None:
        for kind, group in _split_units(actions):
            if kind == "if_group":
                self._walk_if_group(group, path, step_id, in_finally)
                continue
            if kind == "eh_group":
                # try는 본선, catch는 오류 경로 fork로 걷는다 — catch 안의 닫기가 본선 상태를
                # 바꾸면 'catch에서만 닫는' 결함(정상 경로 누수)이 R8에 안 잡힌다. catch fork의
                # 시작 상태는 try 전/후의 관대 병합(공통=확정, 차이=maybe) — try가 어디까지
                # 실행되고 실패했는지 모르므로 양끝 어느 쪽 상태든 오탐 없이 수용한다.
                before_try = self._snapshot()
                for idx, act in group:
                    role = _eh_role(act.get("action"))
                    if role == "try":
                        self.walk(act.get("children") or [], f"{path}[{idx}].children", step_id, in_finally)
                after_try = self._snapshot()
                fork_base = self._merge_lenient(before_try, after_try)
                catch_maybe: set = set()
                for idx, act in group:
                    if _eh_role(act.get("action")) != "catch":
                        continue
                    self._restore(fork_base)
                    self.walk(act.get("children") or [], f"{path}[{idx}].children", step_id, in_finally)
                    got = self._snapshot()
                    # catch에서 새로 연/바뀐 키는 이후 maybe로만 취급 (R7 오탐 방지)
                    catch_maybe |= (set(got[0]) - set(after_try[0])) | got[1]
                self._restore(after_try)
                self.maybe |= catch_maybe
                for idx, act in group:
                    if _eh_role(act.get("action")) == "finally":
                        self.walk(act.get("children") or [], f"{path}[{idx}].children", step_id, in_finally=True)
                continue
            idx, act = group[0]
            location = f"{path}[{idx}]"
            pkg = act.get("package")
            self._process(act, location, step_id, in_finally)
            children = act.get("children") or []
            if not children:
                continue
            if pkg == "Loop":
                self._walk_loop(act, location, step_id, in_finally)
            else:  # Step·Trigger loop·(R6 위반인 비컨테이너 children 포함) — 선형 재귀
                self.walk(children, f"{location}.children", step_id, in_finally)

    def _walk_if_group(self, group: list[tuple[int, dict]], path: str, step_id: str | None, in_finally: bool) -> None:
        """If/Else If/Else 형제들을 각자 fork로 실행하고 병합점에서 상태를 비교한다."""
        incoming = self._snapshot()
        finals: list[tuple[dict, set]] = []
        for idx, act in group:
            self._restore(incoming)
            self.walk(act.get("children") or [], f"{path}[{idx}].children", step_id, in_finally)
            finals.append(self._snapshot())
        has_else = any(_if_role(a.get("action")) == "else" for _, a in group)
        if not has_else:  # 명시 else가 없으면 '아무것도 안 한 경로'도 실존한다
            finals.append(incoming)

        # 병합: 모든 경로에서 열려 있는 키만 확정 열림, 일부 경로만이면 maybe + 불일치 위반.
        all_keys = set().union(*(set(f[0]) for f in finals)) if finals else set()
        merged_opened: dict[tuple[str, str], list] = {}
        merged_maybe: set = set().union(*(f[1] for f in finals)) if finals else set()
        first_idx, first_act = group[0]
        for key in all_keys:
            present = [key in f[0] for f in finals]
            if all(present):
                merged_opened[key] = min((f[0][key] for f in finals), key=len)
                depths = {len(f[0][key]) for f in finals}
                if len(depths) > 1:  # 전 분기 열림이지만 중첩 깊이가 다름 — 경로별 동작 상이
                    pkg, name = key
                    shown = name if name != _ANON else "(이름 미지정)"
                    self.violations.append(
                        Violation(
                            "R7", f"{path}[{first_idx}]",
                            f"If 분기 간 세션 '{shown}'({pkg})의 열림 중첩 수가 다릅니다 — "
                            "일부 분기가 같은 세션을 추가로 열어, 병합 이후 닫기 횟수가 경로에 따라 달라집니다.",
                            package=pkg, action=first_act.get("action"), step_id=step_id,
                        )
                    )
            else:
                merged_maybe.add(key)
                if any(present):  # 일부 분기에서만 열림/닫힘 — 상태 불일치
                    pkg, name = key
                    shown = name if name != _ANON else "(이름 미지정)"
                    changed_in_branch = key not in incoming[0] or not all(present)
                    if changed_in_branch:
                        self.violations.append(
                            Violation(
                                "R7", f"{path}[{first_idx}]",
                                f"If 분기 간 세션 '{shown}'({pkg}) 열림 상태가 불일치합니다 — "
                                "일부 분기에서만 열리거나 닫혀, 병합 이후 동작이 경로에 따라 달라집니다.",
                                package=pkg, action=first_act.get("action"), step_id=step_id,
                            )
                        )
        self.opened = merged_opened
        self.maybe = merged_maybe

    def _walk_loop(self, act: dict, location: str, step_id: str | None, in_finally: bool) -> None:
        """Loop 본문 1회 심볼릭 실행 + 반복 누수 검사 (본문에서 순증가한 열림 → warning)."""
        before = {k: len(v) for k, v in self.opened.items()}
        self.walk(act.get("children") or [], f"{location}.children", step_id, in_finally)
        for key, stack in self.opened.items():
            grew = len(stack) - before.get(key, 0)
            if grew > 0:
                pkg, name = key
                shown = name if name != _ANON else "(이름 미지정)"
                opener_step, opener_loc = stack[-1]
                self.violations.append(
                    Violation(
                        "R8", opener_loc,
                        f"Loop 본문에서 연 세션 '{shown}'이(가) 본문 안에서 닫히지 않습니다 — "
                        "반복마다 세션이 누적(누수)될 수 있습니다.",
                        package=pkg, step_id=opener_step or step_id, severity="warning",
                    )
                )

    def finish(self) -> None:
        """순회 종료 — 남은 열림을 R8로 보고한다."""
        for (pkg, name), stack in self.opened.items():
            shown = name if name != _ANON else "(이름 미지정)"
            for step_id, location in stack:
                self.violations.append(
                    Violation(
                        "R8", location,
                        f"세션 '{shown}'을(를) 연 뒤 닫지 않았습니다 (닫는 액션이 없습니다).",
                        package=pkg, step_id=step_id,
                    )
                )


def _flow_stats(steps: list[dict]) -> tuple[int, bool]:
    """(전체 액션 수, Error handler 존재 여부) — R12a 판정용."""
    total = 0
    has_eh = False

    def _walk(actions: list[dict]) -> None:
        nonlocal total, has_eh
        for a in actions:
            total += 1
            if a.get("package") == "Error handler":
                has_eh = True
            _walk(a.get("children") or [])

    for step in steps:
        _walk(step.get("actions") or [])
    return total, has_eh


def run_session_checks(
    steps: list[dict],
    registry: tuple[frozenset, frozenset] | None = None,
    *,
    emit_r12: bool = False,
) -> list[Violation]:
    """전체 흐름도를 분기 인지 심볼릭 실행으로 순회하며 세션 생명주기(R7~R8, 선택 R12)를 검사한다.

    steps: Recommendation.steps[] (각 {step_id, actions[]}).
    registry: (openers, closers) — 없으면 수기 상수만 사용(v2 호환 시그니처 유지).
    """
    openers, closers = registry or (SESSION_OPENERS, SESSION_CLOSERS)
    _, has_eh = _flow_stats(steps)
    walker = _SessionWalker(openers, closers, flow_has_eh=has_eh, emit_r12=emit_r12)
    for step in steps:
        walker.walk(step.get("actions") or [], "actions", step.get("step_id"))
    walker.finish()
    return walker.violations


# ─────────────────────────────────────────────────────────────────────────────
# R9~R11 변수 데이터플로우 (v3) — produces/consumes + $var$ 교차검증
# ─────────────────────────────────────────────────────────────────────────────

# 파라미터 문자열 값 안의 변수 보간 참조 — 실봇 표기 그대로 ($sUserName$ 등).
_VAR_REF_RE = re.compile(r"\$([A-Za-z_][\w-]*)\$")

# R11 타입 정합 판정 대상 — 파라미터 기대 타입별 허용 변수 타입. 확신 있는 조합만 검사해
# 오탐을 막는다(ANY·미선언·그 외 타입은 판정하지 않음).
_TYPE_COMPAT: dict[str, frozenset[str]] = {
    "NUMBER": frozenset({"NUMBER", "ANY"}),
    "BOOLEAN": frozenset({"BOOLEAN", "ANY"}),
    "TABLE": frozenset({"TABLE", "ANY"}),
    "SESSION": frozenset({"SESSION", "ANY"}),
}


def _explicit_refs(action: dict, key: str) -> list[str]:
    """액션의 produces/consumes 명시 목록에서 변수 이름들을 뽑는다 (VarRef dict/str 모두 수용)."""
    names: list[str] = []
    for ref in action.get(key) or []:
        if isinstance(ref, dict) and ref.get("name"):
            names.append(str(ref["name"]))
        elif isinstance(ref, str) and ref.strip():
            names.append(ref.strip())
    return names


def _inferred_consumes(action: dict) -> list[str]:
    """파라미터 문자열 값의 `$var$` 보간에서 소비 변수를 추론한다 (명시 consumes의 교차 보정)."""
    names: list[str] = []
    for p in action.get("parameters") or []:
        value = p.get("value")
        if isinstance(value, str):
            names.extend(_VAR_REF_RE.findall(value))
    return names


class _DataflowWalker:
    """정의 집합(definite/maybe)을 들고 흐름도를 걸으며 R9·R11을 검사하고 R10 원료를 모은다.

    세션 워커와 같은 실행 단위 분해(_split_units)를 쓴다. Loop 본문은 선(先)스캔으로
    본문 내 produces를 maybe에 미리 넣는다 — 반복 2회차에 정의되는 패턴(카운터 등)의
    오탐을 막기 위한 관대화다.
    """

    def __init__(self, var_types: dict[str, str], catalog: CatalogLookup, check_r9: bool) -> None:
        self.var_types = var_types  # 선언 변수 name -> type (대문자)
        self.catalog = catalog
        self.check_r9 = check_r9
        self.defined: set[str] = set()
        self.maybe: set[str] = set()
        self.produced_sites: dict[str, tuple[str, str | None]] = {}  # name -> (location, step_id)
        self.consumed_names: set[str] = set()
        self.violations: list[Violation] = []

    def _snapshot(self) -> tuple[set, set]:
        return (set(self.defined), set(self.maybe))

    def _restore(self, snap: tuple[set, set]) -> None:
        self.defined, self.maybe = set(snap[0]), set(snap[1])

    def _scan_produces(self, actions: list[dict]) -> set[str]:
        found: set[str] = set()
        for a in actions:
            found.update(_explicit_refs(a, "produces"))
            found.update(self._scan_produces(a.get("children") or []))
        return found

    def _process(self, action: dict, location: str, step_id: str | None) -> None:
        pkg, act = action.get("package"), action.get("action")
        consumes = _explicit_refs(action, "consumes") + _inferred_consumes(action)
        for name in consumes:
            self.consumed_names.add(name)
            if self.check_r9 and name not in self.defined and name not in self.maybe:
                self.violations.append(
                    Violation(
                        "R9", location,
                        f"변수 '{name}'이(가) 정의(produces/입력 선언)되기 전에 사용됩니다.",
                        package=pkg, action=act, step_id=step_id,
                    )
                )
                self.maybe.add(name)  # 같은 변수로 위반을 도배하지 않는다 — 최초 1회만
        self._check_types(action, location, step_id)
        for name in _explicit_refs(action, "produces"):
            self.defined.add(name)
            self.produced_sites.setdefault(name, (location, step_id))

    def _check_types(self, action: dict, location: str, step_id: str | None) -> None:
        """R11: 값이 단일 `$var$` 참조인 파라미터의 기대 타입 vs 선언 변수 타입."""
        pkg, act = action.get("package"), action.get("action")
        spec = self.catalog.get_action_schema(pkg, act) if pkg and act else None
        if spec is None:
            return
        spec_params = {p.get("name"): p for p in spec.get("parameters", [])}
        for p in action.get("parameters") or []:
            value = p.get("value")
            if not (isinstance(value, str) and value.startswith("$") and value.endswith("$")):
                continue
            refs = _VAR_REF_RE.findall(value)
            if len(refs) != 1 or value != f"${refs[0]}$":
                continue  # 보간 혼합 문자열은 STRING 문맥 — 판정 대상 아님
            pspec = spec_params.get(p.get("name"))
            expected = (pspec or {}).get("type")
            allowed = _TYPE_COMPAT.get(str(expected or "").upper())
            var_type = self.var_types.get(refs[0])
            if allowed and var_type and var_type not in allowed:
                self.violations.append(
                    Violation(
                        "R11", location,
                        f"파라미터 '{p.get('name')}'은(는) {expected} 타입을 기대하는데 "
                        f"변수 '{refs[0]}'의 선언 타입은 {var_type}입니다.",
                        package=pkg, action=act, param=p.get("name"), step_id=step_id,
                    )
                )

    def walk(self, actions: list[dict], path: str, step_id: str | None) -> None:
        for kind, group in _split_units(actions):
            if kind == "if_group":
                incoming = self._snapshot()
                finals: list[tuple[set, set]] = []
                for idx, act in group:
                    self._restore(incoming)
                    self._process(act, f"{path}[{idx}]", step_id)  # 분기 조건 자체의 소비 검사
                    self.walk(act.get("children") or [], f"{path}[{idx}].children", step_id)
                    finals.append(self._snapshot())
                if not any(_if_role(a.get("action")) == "else" for _, a in group):
                    finals.append(incoming)
                self.defined = set.intersection(*(f[0] for f in finals)) if finals else set()
                union_def = set().union(*(f[0] for f in finals)) if finals else set()
                self.maybe = (set().union(*(f[1] for f in finals)) | union_def) - self.defined
                continue
            for idx, act in group:  # eh_group은 try→catch→finally 순차, action은 단일
                location = f"{path}[{idx}]"
                self._process(act, location, step_id)
                children = act.get("children") or []
                if not children:
                    continue
                if act.get("package") == "Loop":
                    # 1회차는 진입 상태 그대로 걸어 def-before-use(R9)를 실제로 검사하고,
                    # 본문 산출은 0회전 가능성 때문에 확정(defined)이 아닌 maybe로 강등한다.
                    # (선스캔 관대화는 1회차의 진짜 미정의 사용까지 삼켰다 — 0374 실측 교훈.)
                    snap = self._snapshot()
                    self.walk(children, f"{location}.children", step_id)
                    after = self._snapshot()
                    self.defined = set(snap[0])
                    self.maybe = snap[1] | after[1] | (after[0] - snap[0])
                    continue
                self.walk(children, f"{location}.children", step_id)

    def report_dead_outputs(self, output_vars: set[str]) -> None:
        """R10: 생산됐으나 아무도 소비하지 않는 변수 (봇 출력 변수는 호출자가 소비 — 면제)."""
        for name, (location, step_id) in self.produced_sites.items():
            if name not in self.consumed_names and name not in output_vars:
                self.violations.append(
                    Violation(
                        "R10", location,
                        f"변수 '{name}'을(를) 생산했지만 이후 아무 액션도 사용하지 않습니다.",
                        step_id=step_id, severity="warning",
                    )
                )


def run_dataflow_checks(flow: dict, catalog: CatalogLookup) -> list[Violation]:
    """R9~R11: 변수 def-before-use / dead output / 타입 정합 검사.

    R9/R10은 흐름도에 produces 명시가 하나라도 있을 때만 발화한다 — 연결 정보 없이
    검사하면 전부 오탐이다(하위호환: 미기재 흐름도는 v2 수준으로 자연 강등).
    R11은 선언 변수 타입 + `$var$` 단일 참조만으로 판정 가능해 항상 검사한다.
    """
    steps = flow.get("steps") or []
    variables = flow.get("variables") or []
    var_types = {
        v.get("name"): str(v.get("type") or "").upper()
        for v in variables if isinstance(v, dict) and v.get("name")
    }
    input_vars = {v.get("name") for v in variables if isinstance(v, dict) and v.get("direction") == "input"}
    output_vars = {v.get("name") for v in variables if isinstance(v, dict) and v.get("direction") == "output"}

    has_declared = any(
        (a.get("produces") or a.get("consumes"))
        for step in steps
        for a in _iter_all_actions(step.get("actions") or [])
    )

    walker = _DataflowWalker(var_types, catalog, check_r9=has_declared)
    walker.defined |= {n for n in input_vars if n}
    for step in steps:
        walker.walk(step.get("actions") or [], "actions", step.get("step_id"))
    if has_declared:
        walker.report_dead_outputs({n for n in output_vars if n})
    return walker.violations


def _iter_all_actions(actions: list[dict]):
    for a in actions:
        yield a
        yield from _iter_all_actions(a.get("children") or [])


# ─────────────────────────────────────────────────────────────────────────────
# R13~R14 — 제어 흐름 구조 정합 (v3, 무LLM)
# 0374 JIRA 봇 실측 결함의 일반화: Try–Catch 사이 이물 형제, Continue의 Loop 오용,
# 빈 컨테이너 본문 — 전부 surgeon의 move/wrap으로 기계 수리가 가능한 유형이다.
# ─────────────────────────────────────────────────────────────────────────────

def _is_loop_signal(action_name: str | None) -> bool:
    """Break/Continue류 제어 신호 판정 — 표기 세대 변화(RPA-141)에 견디게 부분 문자열로."""
    low = (action_name or "").lower()
    return "break" in low or "continue" in low


def run_structure_checks(steps: list[dict]) -> list[Violation]:
    """제어 흐름 구조 정합 검사 (R13~R14).

    R13 (Error handler 구조, error):
      - Try의 바로 다음 형제가 Catch/Finally가 아님 — A360에서 Try 다음엔 Catch가 와야
        하며, 사이에 낀 일반 액션은 보호도 안 되고 구조도 깨진다 → Try의 children으로
        옮기라는 수리 지시가 된다.
      - Catch/Finally가 Try 블록에 붙어 있지 않음 (직전 형제가 Try/Catch가 아님).
      - Try 본문(children)이 비어 있음 (warning — 보호 대상 없는 장식 Try).
    R14 (Loop 구조):
      - Continue/Break가 Loop 본문(조상) 밖에서 사용됨 (error) — Continue를 '반복 처리'로
        오용하면 반복 없이 지나간다.
      - Loop 컨테이너의 본문이 비어 있음 (warning) — 반복할 액션이 밖에 있다는 신호.
    """
    violations: list[Violation] = []

    def walk(actions: list[dict], path: str, step_id: str | None, loop_depth: int) -> None:
        n = len(actions)
        for idx, a in enumerate(actions):
            pkg, act = a.get("package"), a.get("action")
            loc = f"{path}[{idx}]"
            children = a.get("children") or []

            if pkg == "Error handler":
                role = _eh_role(act)
                if role == "try":
                    nxt = actions[idx + 1] if idx + 1 < n else None
                    nxt_role = (
                        _eh_role(nxt.get("action"))
                        if nxt is not None and nxt.get("package") == "Error handler"
                        else None
                    )
                    if nxt_role not in ("catch", "finally"):
                        violations.append(Violation(
                            "R13", loc,
                            "Try 다음에는 Catch(또는 Finally)가 바로 와야 합니다 — 사이에 다른 "
                            "액션이 끼면 A360 구조가 깨집니다. 보호할 액션은 Try의 children으로 옮기세요.",
                            package=pkg, action=act, step_id=step_id,
                        ))
                    if not children:
                        violations.append(Violation(
                            "R13", loc,
                            "Try 본문(children)이 비어 있습니다 — 보호할 작업을 Try 안에 넣으세요.",
                            package=pkg, action=act, step_id=step_id, severity="warning",
                        ))
                elif role in ("catch", "finally"):
                    prev = actions[idx - 1] if idx > 0 else None
                    prev_role = (
                        _eh_role(prev.get("action"))
                        if prev is not None and prev.get("package") == "Error handler"
                        else None
                    )
                    # catch는 try/catch(다중 catch) 뒤, finally는 try/catch 뒤에만 유효하다.
                    if prev_role not in ("try", "catch"):
                        shown = "Catch" if role == "catch" else "Finally"
                        violations.append(Violation(
                            "R13", loc,
                            f"{shown}가 Try 블록에 붙어 있지 않습니다 (직전 형제가 Try/Catch가 아님) — "
                            "Try 바로 뒤로 옮기거나 Try와 쌍을 맞추세요.",
                            package=pkg, action=act, step_id=step_id,
                        ))

            if pkg == "Loop":
                if _is_loop_signal(act):
                    if loop_depth == 0:
                        violations.append(Violation(
                            "R14", loc,
                            "Continue/Break는 Loop 본문 안에서만 의미가 있습니다 — 반복 구조가 "
                            "필요하면 Loop(이터레이터) 컨테이너로 감싸고 반복할 액션을 그 children에 넣으세요.",
                            package=pkg, action=act, step_id=step_id,
                        ))
                elif is_container(pkg, act) and not children:
                    violations.append(Violation(
                        "R14", loc,
                        "Loop 본문(children)이 비어 있습니다 — 반복할 액션들을 Loop 안에 넣으세요.",
                        package=pkg, action=act, step_id=step_id, severity="warning",
                    ))

            child_depth = loop_depth + (1 if pkg == "Loop" and is_container(pkg, act) else 0)
            walk(children, f"{loc}.children", step_id, child_depth)

    for step in steps:
        walk(step.get("actions") or [], "actions", step.get("step_id"), 0)
    return violations


# ─────────────────────────────────────────────────────────────────────────────
# 통합 실행기 (v3) — L0(R1~R6) + L1(R7~R14) 한 번에
# ─────────────────────────────────────────────────────────────────────────────

def run_flow_checks(
    flow: dict,
    catalog: CatalogLookup,
    registry: tuple[frozenset, frozenset] | None = None,
) -> list[Violation]:
    """흐름도 전체를 L0 정적(R1~R6) + L1 데이터플로우·세션(R7~R12)으로 검사한다.

    registry를 주지 않으면 카탈로그에서 세션 레지스트리를 유도한다(derive_session_registry).
    반환 위반의 step_id는 단계별 검사(R1~R6)에도 채워진다 — 국소 교정 라우팅용.
    """
    steps = flow.get("steps") or []
    violations: list[Violation] = []

    for step in steps:
        step_id = step.get("step_id")
        for v in run_checks(step.get("actions") or [], catalog):
            v.step_id = v.step_id or step_id
            violations.append(v)

    reg = registry or derive_session_registry(catalog)
    violations.extend(run_session_checks(steps, reg, emit_r12=True))
    violations.extend(run_dataflow_checks(flow, catalog))
    violations.extend(run_structure_checks(steps))

    # R12a: 규모 있는 흐름도에 예외 처리 구조가 아예 없음 (A360 표준 골격 위배 — warning)
    total, has_eh = _flow_stats(steps)
    if total >= 5 and not has_eh:
        violations.append(
            Violation(
                "R12", "actions[0]",
                "예외 처리 구조(Error handler Try/Catch/Finally)가 없습니다 — "
                "무인 실행 표준 골격(Init→Try→Catch→Finally 정리)을 권장합니다.",
                step_id=steps[0].get("step_id") if steps else None,
                severity="warning",
            )
        )
    return violations
