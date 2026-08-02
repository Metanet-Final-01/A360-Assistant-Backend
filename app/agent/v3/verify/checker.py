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
  R15 attended 함정 — 트리거 자동 실행 흐름의 대화형 액션             (warning)
  R16 플랫폼 — 대상 OS(spec.assumptions) 미지원 패키지 사용          (warning)

R9~R11의 원료는 스키마 확장 필드 produces/consumes(app/schemas/recommendation.py의
VarRef)다. composer 명시가 1차이고 `$var$` 파싱이 교차 보정한다 — 흐름도에 produces
명시가 하나도 없으면 R9/R10은 침묵한다(정보 없이 검사하면 전부 오탐이므로).
세션 opener/closer는 수기 상수에 더해 카탈로그 메타(return_type=SESSION 등)에서
유도한다(derive_session_registry) — 커버리지가 상수 3개 패키지에 갇히지 않게.
"""

import logging
import re
from dataclasses import dataclass, field

from app.agent.knowledge import derive as knowledge_derive
from app.agent.knowledge import lexicon

from .catalog import CatalogLookup

logger = logging.getLogger(__name__)

# 본문(children)을 가질 수 있는 컨테이너 액션. A360에는 임의 병합점이 없어
# 분기/반복 블록이 끝나면 다음 형제로 이어진다 — 컨테이너만 children을 갖는다.
#
# 판정은 패키지 단위다(RPA-141): 카탈로그가 llm_agent 소싱으로 바뀌며 액션명이 문서 슬러그
# 기반 camelCase가 됐고(예: Error handler/errorHandlerTry, Loop/cloudUsingLoopAction),
# Loop 패키지엔 iterator 변형이 20여 개라 (package, action) 열거는 표기가 바뀔 때마다
# 헛위반(R6)을 만든다 — 실제로 구 봇 JSON 표기(ErrorHandler/try)가 전부 불일치해
# 에이전트가 Loop/If/Try를 올바르게 써도 재생성을 유발했다. A360에서 본문을 갖는 건
# 이 제어 흐름 패키지들뿐이므로 패키지로 판정하고, 본문이 없는 게 명백한 액션만 뺀다.
# 공용 지식층에서 온다 — A360 언어 수준 어휘라 카탈로그 재적재로 거짓이 되지 않는다.
CONTAINER_PACKAGES: frozenset[str] = lexicon.CONTAINER_PACKAGES

# 컨테이너 패키지 소속이지만 본문(children)을 갖지 않는 액션 — 제어 이동/신호뿐이다.
# ⚠ 이 3쌍은 **현행 카탈로그에 전부 부재**다(llm_agent 소싱 이후 표기가 바뀌었다). 그래서
# `Loop/Break`가 컨테이너로 오판돼 "본문 없는 컨테이너"(R14) 헛위반이 났다. 이제
# `container_exceptions(catalog)`가 카탈로그에서 실재 액션으로 유도하고, 이 상수는
# 유도가 빈 결과를 낼 때(카탈로그 순회 불가·테스트 스텁)의 폴백으로만 남는다.
NON_CONTAINER_ACTIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("Loop", "loopPackageBreakAction"),
        ("Loop", "loopPackageContinueAction"),
        ("Error handler", "errorHandlerThrow"),
    }
)


def container_exceptions(catalog=None) -> frozenset[tuple[str, str]]:
    """카탈로그에서 유도한 '본문 없는 컨테이너 액션'. 유도가 비면 수기 상수 폴백."""
    if catalog is None:
        return NON_CONTAINER_ACTIONS
    return knowledge_derive.derive_container_exceptions(catalog) or NON_CONTAINER_ACTIONS


def is_container(
    package: str | None,
    action: str | None,
    *,
    non_container: frozenset[tuple[str, str]] | None = None,
) -> bool:
    """이 액션이 children(본문)을 가질 수 있는 컨테이너인지 판정한다 — R6 기준.

    `non_container`를 안 주면 수기 상수를 쓴다(하위호환). 카탈로그를 아는 호출부는
    `container_exceptions(catalog)` 결과를 넘겨 Break·Continue·Throw를 표기 세대와
    무관하게 제외시킨다.
    """
    exceptions = NON_CONTAINER_ACTIONS if non_container is None else non_container
    return lexicon.is_container(package, action, non_container=exceptions)

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

    rule: str  # "R1"~"R18"
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


def _check_action(
    action: dict,
    catalog: CatalogLookup,
    location: str,
    non_container: frozenset[tuple[str, str]] | None = None,
) -> list[Violation]:
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
    if children and not is_container(pkg, act, non_container=non_container):
        violations.append(
            Violation(
                "R6", location,
                f"'{pkg}/{act}'은(는) 컨테이너가 아닌데 children이 있습니다.",
                package=pkg, action=act,
            )
        )

    for i, child in enumerate(children):
        violations.extend(
            _check_action(child, catalog, f"{location}.children[{i}]", non_container)
        )
    return violations


def run_checks(actions: list[dict], catalog: CatalogLookup) -> list[Violation]:
    """액션 트리(한 단계의 actions[])를 R1~R6로 검사해 위반 목록을 반환한다.

    actions: RecommendedAction.model_dump() 리스트 또는 동형 dict 리스트.
    """
    violations: list[Violation] = []
    # 컨테이너 예외는 카탈로그에서 한 번만 유도해 트리 전체에 내려보낸다 — 수기 3쌍은
    # 현행 표기와 어긋나 Loop/Break를 컨테이너로 오판했다(container_exceptions 참조).
    non_container = container_exceptions(catalog)
    for i, action in enumerate(actions):
        violations.extend(_check_action(action, catalog, f"actions[{i}]", non_container))
    return violations


# ─────────────────────────────────────────────────────────────────────────────
# 세션 레지스트리 유도 (v3) — 수기 상수 + 카탈로그 메타
# ─────────────────────────────────────────────────────────────────────────────

def derive_session_registry(catalog=None) -> tuple[frozenset, frozenset]:
    """세션 opener/closer 집합을 **카탈로그에서 유도**한다. 수기 상수는 폴백이다.

    유도 본체는 공용 지식층(`app.agent.knowledge.derive`)이다 — 같은 규칙을 버전마다
    복제하면 한쪽만 고쳐질 때 "검수가 잡는 결함"과 "채점이 세는 결함"이 갈린다.
    유도 규칙(session_role 1순위 → return_type=SESSION → SESSION 파라미터 게이팅 등)은
    그쪽 독스트링을 보라.

    반환 타입은 v3 계약 그대로 (openers, closers) 튜플이다 — 지식층은 usable 플래그를 든
    SessionRegistry를 주지만, v3 호출부 전체가 튜플 언패킹을 하고 있어 경계에서 눕힌다.

    ⚠ 유도가 비면(카탈로그 순회 불가·테스트 스텁) 수기 상수 7쌍으로 떨어진다. 그 상수는
    **현행 카탈로그에 없는 세대의 표기**라 R7/R8이 한 번도 발화하지 못한 이력이 있다
    (RPA-141). 즉 폴백은 '검사가 죽지 않게' 하는 장치이지 정확한 어휘가 아니다.
    """
    registry = knowledge_derive.derive_session_registry(
        catalog,
        fallback_openers=SESSION_OPENERS,
        fallback_closers=SESSION_CLOSERS,
    )
    if not getattr(registry, "usable", False):
        logger.info("세션 레지스트리 유도 실패(source=%s) — 수기 상수 폴백",
                    getattr(registry, "source", "?"))
    return frozenset(registry.openers), frozenset(registry.closers)


def _norm_param(name: object) -> str:
    return name.replace(" ", "").lower() if isinstance(name, str) else ""


def _is_session_param(name: object) -> bool:
    """세션 **이름**을 담는 파라미터인지 — 표기 세대에 무관하게 판정한다.

    구 JAR "session"/"sessionName"과 v2 문서 라벨 "Session name", 그리고 패키지명을 앞에
    단 "Microsoft 365 Excel session"까지 잡아야 한다. 그래서 "session으로 끝나는가"로 본다.

    ⚠ "session을 **포함**하는가"로 보면 안 된다 — 세션의 *속성*을 담은 파라미터가 같이
    걸린다. 실측(2026-07-29): `Excel advanced/Open`이 `Session type='New'`와
    `Session name='$sExcelSession$'`을 함께 갖는데, 포함 판정이 파라미터 순서대로 훑다가
    `Session type`을 먼저 만나 **세션 이름을 'New'로 읽었다.** 그 결과 여는 액션은
    ("Excel advanced","New")로 등록되고, `$sExcelSession$`을 쓰는 Write·Filter는 "열려
    있지 않은 세션"(R7)이, 'New'는 "열고 안 닫은 세션"(R8)이 됐다 — 배선이 완벽한
    흐름도에서 오탐 4건. 위반 수는 신뢰도를 깎으므로 계측 자체가 오염된다.
    """
    norm = _norm_param(name)
    return norm.endswith("session") or norm.endswith("sessionname")


def _unwrap_var(value: str) -> str:
    """A360 변수 참조 표기 `$name$`를 벗겨 이름만 남긴다.

    같은 세션을 여는 쪽은 리터럴로(`outlookSession`), 쓰는 쪽은 변수 참조로(`$outlookSession$`)
    적는 일이 흔하다. 벗기지 않으면 R7/R8이 **하나의 세션을 둘로 세어** "열지 않고 닫는다"와
    "열고 닫지 않는다"를 동시에 낸다 — 짝이 맞는 흐름도에서 나오는 오탐이라 게이트 판정과
    신뢰도 감점을 함께 오염시킨다.
    """
    inner = value.strip()
    if len(inner) > 2 and inner.startswith("$") and inner.endswith("$"):
        stripped = inner[1:-1].strip()
        # `$a$-$b$` 같은 합성 표현은 이름 하나가 아니므로 건드리지 않는다.
        if stripped and "$" not in stripped:
            return stripped
    return inner


def _session_name(action: dict) -> str | None:
    """액션의 세션 파라미터 값을 세션 이름으로 반환. 없으면 None.

    'Default'도 유효한 세션 이름이다 — A360에서 Default 세션도 명시적으로 열어야 한다.

    한 액션이 세션 파라미터를 둘 이상 가지면 **"…session name"이 이긴다** — 파라미터
    순서에 기대면 카탈로그의 필드 나열 순서가 판정을 가른다.
    """
    fallback = None
    for p in action.get("parameters", []):
        name = p.get("name")
        if not _is_session_param(name):
            continue
        value = p.get("value")
        if not (isinstance(value, str) and value.strip()):
            continue
        if _norm_param(name).endswith("sessionname"):
            return _unwrap_var(value)
        if fallback is None:
            fallback = _unwrap_var(value)
    return fallback


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
    """Error handler 패키지 액션의 역할 판정 — 'try'|'catch'|'finally'|'throw'|'other'.

    공용 지식층에 위임한다. 이 함수는 knowledge 층 이관 때 남은 수기 사본이었고 'throw'를
    몰라서 Throw를 'other'로 흘려보냈다 — R18(조건 없는 Throw)이 한 건도 발화하지 못한
    원인이다. 기존 호출부는 전부 try/catch/finally와만 비교하므로 'throw' 추가는 무해하다.
    """
    return lexicon.eh_role(action_name)


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
                # ⚠ Finally 안의 **정리 가드**만 예외로 둔다 — 좁게 잡는다 (Qodo #486).
                #
                # 예외의 근거는 "병합 이후 동작이 경로에 따라 달라진다"가 Finally에서는
                # 성립하지 않는다는 것이다: 정리 구간이라 그 뒤에 세션을 쓰는 액션이 없다.
                # 그리고 `If <열렸는가> → Close`는 결함이 아니라 정확한 정리다 — Try 앞쪽에서
                # 실패하면 뒤쪽 세션은 열리지 않았고, 무조건 닫으면 정리하다 또 터진다.
                # 이 예외가 없으면 세션 정리에 합법 수가 없다(실측): 가드 없이 Finally면 L3가
                # error 경로를 떨어뜨리고, 가드를 붙이면 여기서 major가 붙고, Finally 밖으로
                # 빼면 R12+R8이 붙는다. (`Else`를 명시해도 분기 상태는 여전히 갈린다.)
                #
                # ⚠⚠ 예외는 **닫기 가드 모양에만** 준다. Finally 안이라도 분기가 세션을
                # **열어서** 상태가 갈리는 것은 정리가 아니므로 그대로 잡는다(`key in incoming`
                # 조건이 그 구분이다). 그리고 이 경우는 `maybe`에 넣지 않고 **닫힘으로 확정**
                # 한다 — maybe는 이후 사용·닫기에서 침묵하게 만드는 집합이라, 넣으면 예외가
                # 보고를 넘어 상태 추적까지 꺼 버린다. "일부 경로에서 닫혔다"의 보수적 해석은
                # '닫혔다'이다.
                #
                # ⚠ 다만 Finally 안의 이중 닫기는 이 조치로도 안 잡힌다 — Try/Catch fork가
                # `merge_lenient`로 그 세션을 이미 `maybe`에 올려 두기 때문이다(실측: 가드
                # 없이 Finally에 close를 두 번 둔 대조군도 똑같이 조용하다). 즉 그 구멍은 이
                # 예외와 무관한 **기존 동작**이고, 고치려면 catch fork의 maybe 전파를 손봐야
                # 하는데 그건 전 경로의 오탐 균형을 다시 재야 하는 별개 작업이다.
                closed_guard = in_finally and key in incoming[0] and not all(present)
                if not closed_guard:
                    merged_maybe.add(key)
                if any(present) and not closed_guard:  # 일부 분기에서만 열림/닫힘 — 상태 불일치
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
        """순회 종료 — 남은 열림을 R8로 보고한다.

        **어디에 닫기를 두라는지까지 말한다.** 실측(2026-07-29): 이 지적을 받은 수리가
        여는 액션 **바로 뒤**에 닫기를 넣어 R8을 없앴다 — 브라우저를 열자마자 닫고 그 뒤
        클릭들이 죽은 화면에서 도는 흐름도가 됐는데, 가중합은 줄었으니 채택됐다.
        자리를 안 알려주면 수리는 가장 가까운 자리에 놓는다.
        """
        for (pkg, name), stack in self.opened.items():
            shown = name if name != _ANON else "(이름 미지정)"
            for step_id, location in stack:
                self.violations.append(
                    Violation(
                        "R8", location,
                        f"세션 '{shown}'을(를) 연 뒤 닫지 않았습니다 (닫는 액션이 없습니다). "
                        "닫는 액션은 **이 세션을 쓰는 마지막 액션 뒤**에 두세요 — Error handler가 "
                        "있으면 Finally에, 없으면 흐름 끝에. 여는 액션 바로 뒤에 두면 이후 작업이 "
                        "닫힌 세션에서 돌게 됩니다.",
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

    def __init__(self, var_types: dict[str, str], catalog: CatalogLookup, check_r9: bool,
                 openers: frozenset = frozenset()) -> None:
        self.var_types = var_types  # 선언 변수 name -> type (대문자)
        self.catalog = catalog
        self.check_r9 = check_r9
        self.openers = openers      # 세션을 **만드는** 액션 — 카탈로그가 알려준다
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
        declared = set(_explicit_refs(action, "produces"))
        # 세션을 여는 액션은 그 세션을 **만든다** — 카탈로그가 opener라고 말해 주므로
        # produces 선언이 없어도 안다. 값 단계가 이 필드를 빠뜨리는 일이 실측된다
        # (2026-07-29: 조각 셋 중 하나가 Excel 블록 전체의 produces·consumes를 통째로
        # 비웠다 — 파라미터는 `$sExcelSession$`로 전부 일관됐는데도). 그때 여는 액션이
        # 자기 세션 파라미터 때문에 "정의 전 사용"으로 지목됐다. **정의하는 당사자다.**
        derived = set()
        if (pkg, act) in self.openers:
            opened = _session_name(action)
            if opened:
                derived.add(opened)
        produces = declared | derived
        # `$var$` 추론은 **이 액션이 만드는 변수**를 소비로 세지 않는다. 결과를 담을 변수를
        # 파라미터로 지정하는 액션(추출·읽기·세션 열기)은 그 자리에 `$이름$`을 적는데, 그건
        # 읽는 게 아니라 **출력 칸**이다 — 세면 "자기가 만들기 전에 자기가 쓴다"가 된다.
        # 누적 갱신(nCount = nCount + 1)은 consumes에 **명시**하므로 아래 explicit로 남는다.
        consumes = _explicit_refs(action, "consumes") + [
            n for n in _inferred_consumes(action) if n not in produces
        ]
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
        self.defined |= produces
        # R10(생산했는데 아무도 안 씀)은 **명시 선언만** 센다 — 유도한 세션까지 넣으면
        # 지금까지 안 나던 경고가 규칙 변경만으로 무더기로 생긴다.
        for name in declared:
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

    openers, _closers = derive_session_registry(catalog)
    walker = _DataflowWalker(var_types, catalog, check_r9=has_declared, openers=openers)
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
# R13~R14, R17~R18 — 제어 흐름 구조 정합 (v3, 무LLM)
# 0374 JIRA 봇 실측 결함의 일반화: Try–Catch 사이 이물 형제, Continue의 Loop 오용,
# 빈 컨테이너 본문 — 전부 surgeon의 move/wrap으로 기계 수리가 가능한 유형이다.
#
# R17·R18은 2026-07-28 실측(네이버 금 시세 12턴)에서 무검출로 새던 두 유형이다:
#   R17 — children 없는 Step 노드. Step은 순수 구획이라 자식이 없으면 **아무것도 실행하지
#         않는데**, 라벨이 업무를 주장해서("'증권' 버튼 클릭") 요구를 이행한 것처럼 보인다.
#         한 산출물에 9개까지 나왔고 그 흐름도가 신뢰도 최고점을 받았다 — 빈칸이 점수를 받는다.
#   R18 — Catch·분기 밖의 Throw. Throw는 오류를 '발생'시키므로 정상 경로에 있으면 매 실행
#         터지고 뒤 액션이 전부 죽는다. 실측에서 «성공 완료 표시», 최상위 «오류 재던지기»로
#         나왔다 — 라벨은 정상 종료를 주장하는데 액션은 정반대다.
# ─────────────────────────────────────────────────────────────────────────────

def _has_work(actions: list[dict]) -> bool:
    """Step 스캐폴드를 뚫고 **실제로 실행되는 액션**이 하나라도 있는가.

    Step은 순수 구획이라 그 자체로는 아무것도 실행하지 않는다. 그래서 "children이 있는가"로
    빈 Try를 판정하면, Try 안에 라벨만 붙은 빈 Step을 하나 넣는 것으로 규칙이 충족된 것처럼
    보인다 — 보호 대상은 여전히 0인데 지적만 사라진다. 실제 실행되는 액션을 기준으로 본다.
    """
    for a in actions:
        if not isinstance(a, dict):
            continue
        if a.get("package") == "Step":
            if _has_work(a.get("children") or []):
                return True
        else:
            return True
    return False


def _is_loop_signal(action_name: str | None) -> bool:
    """Break/Continue류 제어 신호 판정 — 표기 세대 변화(RPA-141)에 견디게 부분 문자열로."""
    low = (action_name or "").lower()
    return "break" in low or "continue" in low


def run_structure_checks(
    steps: list[dict],
    non_container: frozenset[tuple[str, str]] | None = None,
    closers: frozenset[tuple[str, str]] | None = None,
    openers: frozenset[tuple[str, str]] | None = None,
) -> list[Violation]:
    """제어 흐름 구조 정합 검사 (R13~R14).

    R13 (Error handler 구조) — 조건마다 심각도가 다르다:
      - Try의 바로 다음 형제가 Catch/Finally가 아님 (**blocker**) — 짝 없는 Try는 A360이
        저장·실행을 거부한다. R1(없는 액션 사용)과 같은 급이라 major로 두면 **실행되지 않는
        봇이 그대로 출고된다**(실측 2026-07-28: Try 3개 대 Catch 1개인 흐름도가 신뢰도
        0.13으로 나갔다). 사이에 낀 액션은 Try의 children으로 옮기라는 수리 지시가 된다.
      - Catch/Finally가 Try 블록에 붙어 있지 않음 (**blocker**, 같은 이유).
      - Try 본문에 실제 실행되는 액션이 없음 (major) — 빈 Step 스캐폴드만 있는 것도 포함.
      - 최상위 Try 블록이 둘 이상 (major) — 앞 블록의 Finally가 뒤 블록의 세션을 닫는다.
      - Error handler 블록 **뒤에 업무가 남음** (major) — 그 업무는 보호를 못 받고, Finally가
        이미 닫은 세션 위에서 돈다. `closers`를 줘야 판정한다(정리 액션과 업무를 갈라야 하므로).
      - Try·Catch·Finally가 **단계 경계로 갈림** (warning) — 실행 시퀀스로는 인접하지만
        한 덩어리로 안 그려지고 편집이 깨지기 쉽다 → merge_step 수리 지시가 된다.
    R14 (Loop 구조):
      - Continue/Break가 Loop 본문(조상) 밖에서 사용됨 (error) — Continue를 '반복 처리'로
        오용하면 반복 없이 지나간다.
      - Loop 컨테이너의 본문이 비어 있음 (warning) — 반복할 액션이 밖에 있다는 신호.
    R17 (비실행 스캐폴드, error): children 없는 Step 노드.
    R18 (조건 없는 Throw, error): Catch·분기 children 밖의 Throw.

    ## 단계 경계를 넘는 인접성

    최상위 형제 인접성은 **step 경계를 넘어** 본다. steps[]는 화면·문서의 구획일 뿐이고
    (`exportFlow.js`가 step을 마크다운 헤딩으로만 쓴다) 실행 시퀀스는 step을 가로질러
    이어지기 때문이다. 단계별로만 보면 step-2=Try / step-3=Catch / step-4=Finally가
    "붙어 있지 않다" error 3건으로 잡히는데, 실행 의미는 멀쩡하고 진짜 문제는 렌더가
    한 덩어리로 안 묶이는 것이다(프론트 `buildSegments`는 같은 step의 연속 형제만 컬럼으로
    가른다). 그래서 이 경우는 error가 아니라 **warning 1건 + merge_step 지시**로 가른다.
    children 배열은 step을 가로지를 수 없으므로 이 구분이 적용되지 않는다.
    """
    violations: list[Violation] = []

    def eh_adjacency(seq: list[dict], locs: list[str], sids: list[str | None]) -> None:
        """Error handler 형제 인접성 (R13) — seq는 같은 실행 시퀀스의 형제들이다."""
        n = len(seq)
        for idx, a in enumerate(seq):
            if a.get("package") != "Error handler":
                continue
            act = a.get("action")
            role = _eh_role(act)
            loc, sid = locs[idx], sids[idx]
            if role == "try":
                nxt = seq[idx + 1] if idx + 1 < n else None
                nxt_role = (
                    _eh_role(nxt.get("action"))
                    if nxt is not None and nxt.get("package") == "Error handler"
                    else None
                )
                if nxt_role not in ("catch", "finally"):
                    violations.append(Violation(
                        "R13", loc,
                        "Try 다음에는 Catch(또는 Finally)가 바로 와야 합니다 — 짝 없는 Try는 "
                        "A360이 저장·실행을 거부합니다. 보호할 액션은 Try의 children으로 옮기고, "
                        "Try 바로 뒤에 Catch를 두세요.",
                        package=a.get("package"), action=act, step_id=sid,
                        severity="blocker",
                    ))
            elif role in ("catch", "finally"):
                prev = seq[idx - 1] if idx > 0 else None
                prev_role = (
                    _eh_role(prev.get("action"))
                    if prev is not None and prev.get("package") == "Error handler"
                    else None
                )
                shown = "Catch" if role == "catch" else "Finally"
                # catch는 try/catch(다중 catch) 뒤, finally는 try/catch 뒤에만 유효하다.
                if prev_role not in ("try", "catch"):
                    violations.append(Violation(
                        "R13", loc,
                        f"{shown}가 Try 블록에 붙어 있지 않습니다 (직전 형제가 Try/Catch가 아님) — "
                        "짝이 깨진 예외 처리는 A360이 저장·실행을 거부합니다. "
                        "Try 바로 뒤로 옮기거나 Try와 쌍을 맞추세요.",
                        package=a.get("package"), action=act, step_id=sid,
                        severity="blocker",
                    ))
                elif sids[idx - 1] != sid:
                    violations.append(Violation(
                        "R13", loc,
                        f"{shown}가 Try와 다른 단계에 있습니다 — Try·Catch·Finally는 한 단계 안의 "
                        "연속한 형제여야 화면에 한 덩어리로 그려집니다. 이 단계를 앞 단계와 "
                        "합치세요(merge_step).",
                        package=a.get("package"), action=act, step_id=sid, severity="warning",
                    ))

    def walk(
        actions: list[dict], path: str, step_id: str | None, loop_depth: int, guarded: bool
    ) -> None:
        """노드 단위 검사 + 재귀. 형제 인접성(R13)은 eh_adjacency가 따로 본다.

        guarded: 조상에 Catch나 분기(If)가 있는가 — R18(조건 없는 Throw)의 판정 기준.
        """
        for idx, a in enumerate(actions):
            pkg, act = a.get("package"), a.get("action")
            loc = f"{path}[{idx}]"
            children = a.get("children") or []
            role = _eh_role(act) if pkg == "Error handler" else None

            if role == "try" and not _has_work(children):
                # major다(warning 아님). 실측(2026-07-29) 4턴 중 3턴이 예외 처리 골격만
                # 만들고 그 안을 비웠다 — 한 턴은 Try/Catch/Finally 셋이 다 비어 업무
                # 액션이 아예 없었다. warning(가중치 1)으로는 교정 압력이 사실상 0이라
                # refine이 손대지 않는다. 빈 Try는 "오류를 처리한다"는 요구를 이행한
                # 시늉만 낸 것이므로 결함으로 센다.
                violations.append(Violation(
                    "R13", loc,
                    "Try 본문(children)이 비어 있습니다 — 예외 처리 틀만 있고 보호하는 작업이 "
                    "없습니다. 본 업무 액션을 Try의 children으로 옮기세요(업무가 Try 밖에 "
                    "있으면 오류가 나도 Catch가 잡지 못합니다).",
                    package=pkg, action=act, step_id=step_id,
                ))

            if role == "throw" and not guarded:
                violations.append(Violation(
                    "R18", loc,
                    "Throw는 오류를 발생시키는 액션입니다 — Catch 안이나 조건 분기(If) 안이 "
                    "아니면 실행할 때마다 무조건 터져 이후 액션이 전부 실행되지 않습니다. "
                    "조건이 있으면 If children으로 감싸고, 오류 전파가 목적이면 Catch children으로 "
                    "옮기세요. 정상 완료 표시가 목적이라면 Throw가 아닌 다른 액션을 쓰세요.",
                    package=pkg, action=act, step_id=step_id,
                ))

            # R17 — Step은 순수 구획이라 children이 없으면 아무것도 실행하지 않는다.
            if pkg == "Step" and not children:
                violations.append(Violation(
                    "R17", loc,
                    "children이 없는 Step은 아무것도 실행하지 않습니다 — 라벨이 업무를 주장해도 "
                    "실제로는 빈칸입니다. 이 작업을 수행하는 실제 액션으로 교체하거나, 대응 액션이 "
                    "카탈로그에 없으면 노드를 지우고 notes에 '자동화 불가'로 남기세요.",
                    package=pkg, action=act, step_id=step_id,
                ))
            elif pkg == "Step" and len(children) == 1:
                # Step은 **여러** 액션을 논리 단위로 묶는 구획이다. 하나를 감싸면 트리만 한 겹
                # 깊어지고 얻는 게 없다 — 실측(2026-07-29)에서 업무 액션 12개에 Step 7개가 붙었고
                # 그중 다섯이 자식 하나짜리였다. 실행은 되므로 warning이다.
                violations.append(Violation(
                    "R17", loc,
                    "자식이 하나뿐인 Step은 묶는 일을 하지 않습니다 — Step은 여러 액션을 논리 "
                    "단위로 묶을 때만 쓰고, 하나면 그 액션을 Step 자리에 바로 두세요.",
                    package=pkg, action=act, step_id=step_id, severity="warning",
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
                elif is_container(pkg, act, non_container=non_container) and not children:
                    violations.append(Violation(
                        "R14", loc,
                        "Loop 본문(children)이 비어 있습니다 — 반복할 액션들을 Loop 안에 넣으세요.",
                        package=pkg, action=act, step_id=step_id, severity="warning",
                    ))

            child_depth = loop_depth + (
                1 if pkg == "Loop" and is_container(pkg, act, non_container=non_container) else 0
            )
            child_path = f"{loc}.children"
            eh_adjacency(children, [f"{child_path}[{i}]" for i in range(len(children))],
                         [step_id] * len(children))
            walk(children, child_path, step_id, child_depth,
                 guarded or role == "catch" or pkg == "If")

    # 최상위 형제 인접성은 step 경계를 넘어 하나의 시퀀스로 본다 (docstring 참고).
    top: list[dict] = []
    top_locs: list[str] = []
    top_sids: list[str | None] = []
    for step in steps:
        sid = step.get("step_id")
        for idx, a in enumerate(step.get("actions") or []):
            top.append(a)
            top_locs.append(f"actions[{idx}]")
            top_sids.append(sid)
    eh_adjacency(top, top_locs, top_sids)

    # 최상위 Try 블록은 하나다 (R13, major).
    #
    # ## 진짜 피해는 세션이 아니라 '실패를 삼킨 채 계속 도는 것'이다
    #
    # 처음엔 "앞 블록의 Finally가 뒤 블록이 쓸 세션을 닫는다"를 근거로 삼았는데, 실측
    # (2026-07-29)에서 Try 3덩어리인 흐름도가 **R7 위반 0건**으로 나왔다 — 각 블록이 자기가
    # 연 것만 닫았고 뒤에서 쓰지 않았다. 세션 피해는 R7이 이미 정확히 잡으므로 이 규칙의
    # 근거로는 약하다.
    #
    # 같은 흐름도에서 실제로 깨진 것은 이쪽이다: step-1 Try가 `tGoldRates`를 만들고 실패 시
    # Catch가 기록·안전 종료를 하는데, **Error handler 블록은 정상 종료하므로 step-2가 그대로
    # 실행된다.** step-2는 `tGoldRates`를 소비하지만 그 전제를 검사하지 않는다. 즉 앞이
    # 실패했는데 뒤가 빈 값으로 계속 돈다 — 무인 실행에서 가장 나쁜 종류의 실패다.
    #
    # 지적문이 '합치기'만 요구하면 수리가 어렵다(최상위 노드 여러 개를 옮기는 다중 연산).
    # 성공 플래그 가드로 감싸는 길을 함께 제시해 국소 편집으로도 풀 수 있게 한다.
    #
    # 반복 항목 하나의 실패를 격리하는 **중첩** Try(Loop children 안)는 정당하므로 세지 않는다
    # — 여기서 보는 것은 top-level 형제로 나란히 선 블록뿐이다.
    top_tries = [
        (loc, sid) for a, loc, sid in zip(top, top_locs, top_sids, strict=True)
        if a.get("package") == "Error handler" and _eh_role(a.get("action")) == "try"
    ]
    if len(top_tries) > 1:
        loc, sid = top_tries[1]
        violations.append(Violation(
            "R13", loc,
            f"최상위 Try 블록이 {len(top_tries)}개입니다 — 앞 블록이 실패해 Catch로 빠져도 "
            "Error handler 블록 자체는 정상 종료하므로 **뒤 블록이 그대로 실행됩니다.** "
            "뒤 블록은 앞 블록의 성공을 전제로 도는데(앞에서 만든 변수·세션을 쓰는데) 그 전제를 "
            "검사하지 않습니다. 셋 중 하나로 고치세요: ① 두 Try의 children을 하나의 Try로 합친다 "
            "(가장 깔끔), ② 앞 Try 끝에 성공 플래그를 세우고 뒤 블록을 그 플래그 If로 감싼다, "
            "③ 뒤 블록의 업무를 앞 Try의 children 끝으로 옮긴다. 반복 항목의 실패 격리가 "
            "목적이라면 형제가 아니라 Loop children 안에 중첩하세요.",
            package="Error handler", action="Try", step_id=sid,
        ))

    violations.extend(_post_block_work(top, top_locs, top_sids, closers))

    for step in steps:
        walk(step.get("actions") or [], "actions", step.get("step_id"), 0, False)
        violations.extend(_idle_session(step.get("actions") or [], "actions",
                                        step.get("step_id"), openers, closers))
    return violations


def _idle_session(
    actions: list[dict],
    path: str,
    step_id: str | None,
    openers: frozenset[tuple[str, str]] | None,
    closers: frozenset[tuple[str, str]] | None,
) -> list[Violation]:
    """여는 액션 **바로 뒤**에 닫는 액션이 오는 자리를 R8(major)로 보고한다 — 재귀.

    그 세션에서는 아무 일도 일어나지 않고, 뒤따르는 작업은 닫힌 세션 위에서 돈다.
    빈 Try(R13)·빈 Loop(R14)·빈 Step(R17)과 같은 종류의 결함이 세션에 나타난 것이다.

    실측(2026-07-29)에서 이건 **수리가 만들었다.** R8("연 뒤 닫지 않았습니다")을 받은 surgeon이
    닫기를 여는 액션 바로 뒤에 넣었고, R8이 사라져 가중합이 줄었으므로 채택됐다 — 브라우저를
    열자마자 닫고 그 뒤 클릭들이 죽은 화면에서 도는 흐름도가 그렇게 나왔다. 지적이 자리를
    말해주지 않으면 수리는 가장 가까운 자리를 고른다.

    레지스트리가 없으면 검사하지 않는다(R7/R8과 같은 침묵 원칙).
    """
    if not openers or not closers:
        return []
    out: list[Violation] = []
    for idx, a in enumerate(actions):
        if not isinstance(a, dict):
            continue
        loc = f"{path}[{idx}]"
        key = (a.get("package"), a.get("action"))
        nxt = actions[idx + 1] if idx + 1 < len(actions) else None
        if key in openers and isinstance(nxt, dict):
            nkey = (nxt.get("package"), nxt.get("action"))
            # 같은 패키지의 닫기가 바로 뒤 — 사이에 그 세션을 쓰는 액션이 하나도 없다.
            if nkey in closers and nkey[0] == key[0]:
                out.append(Violation(
                    "R8", loc,
                    f"'{key[0]}' 세션을 연 **바로 뒤**에 닫고 있습니다 — 그 사이에 아무 작업도 "
                    "없어 이 세션은 아무 일도 하지 않고, 뒤따르는 작업은 닫힌 세션에서 돌게 "
                    "됩니다. 닫는 액션을 이 세션을 쓰는 마지막 액션 뒤로(Error handler가 있으면 "
                    "Finally로) 옮기세요.",
                    package=key[0], action=key[1], step_id=step_id,
                ))
        out.extend(_idle_session(a.get("children") or [], f"{loc}.children",
                                 step_id, openers, closers))
    return out


def _post_block_work(
    top: list[dict],
    locs: list[str],
    sids: list[str | None],
    closers: frozenset[tuple[str, str]] | None,
) -> list[Violation]:
    """Error handler 블록 **뒤에 남은 업무**를 R13(major)로 보고한다.

    Try가 하나뿐이어도 그 안에 업무의 일부만 들어가는 일이 반복됐다 — 흐름도를 step 시퀀스로
    먼저 깔고 Try를 앞쪽 몇 단계에만 씌우는 형태다. 그러면 두 가지가 동시에 깨진다:
    뒤쪽 업무는 **보호를 못 받고**, 블록의 Finally가 **아직 쓸 세션을 미리 닫는다.**
    Try가 둘인 경우(위 검사)와 증상이 같은데 규칙이 못 보던 자리다.

    블록 뒤에 와도 되는 것(compose_agent.md [운영 골격]과 같은 목록):
      · 세션·리소스 정리 — `closers`로 판정한다.
      · 결과 알림 — 본 업무 성공에 의존하므로 If 가드 안에 두라고 되어 있다. If 서브트리는
        통째로 통과시킨다(안을 따지면 가드의 취지와 어긋난다).
      · Step은 구획일 뿐이라 통과시키고 안을 본다.

    `closers`가 없으면(레지스트리 유도 실패·테스트 스텁) **검사하지 않는다** — 정리 액션을
    업무로 오인해 무고한 흐름도를 흔드는 쪽이 더 나쁘다(R7/R8과 같은 침묵 원칙).
    """
    if not closers:
        return []
    last_eh = max(
        (i for i, a in enumerate(top) if a.get("package") == "Error handler"), default=-1
    )
    if last_eh < 0 or last_eh == len(top) - 1:
        return []

    found: list[tuple[str, dict]] = []

    def scan(actions: list[dict], path: str) -> None:
        for idx, a in enumerate(actions):
            if not isinstance(a, dict):
                continue
            pkg, act = a.get("package"), a.get("action")
            loc = f"{path}[{idx}]"
            if pkg == "If":            # 성공 가드 안의 후속 처리 — 허용
                continue
            if pkg == "Step":          # 구획 — 뚫고 본다
                scan(a.get("children") or [], f"{loc}.children")
                continue
            if (pkg, act) in closers:  # 정리 — 허용
                continue
            found.append((loc, a))

    for i in range(last_eh + 1, len(top)):
        scan([top[i]], locs[i].rsplit("[", 1)[0])

    if not found:
        return []
    loc, first = found[0]
    names = " · ".join(f"{a.get('package')}/{a.get('action')}" for _l, a in found[:4])
    sid = sids[min(last_eh + 1, len(sids) - 1)]
    return [Violation(
        "R13", loc,
        f"Error handler 블록 뒤에 업무 액션이 {len(found)}개 남아 있습니다({names}) — "
        "이 액션들은 오류가 나도 Catch가 잡지 못하고, 블록의 Finally가 이미 닫은 세션 위에서 "
        "실행됩니다. 업무는 Try의 children으로 옮기고, 블록 뒤에는 정리(세션 닫기)와 "
        "성공 가드(If) 안의 결과 알림만 남기세요.",
        package=first.get("package"), action=first.get("action"), step_id=sid,
    )]


def run_package_checks(steps: list[dict], catalog=None) -> list[Violation]:
    """패키지 선택 검사 — R19(경쟁 패키지 혼용, major) + R20(신규 개발 비권장, warning).

    R19 — 같은 일을 하는 경쟁 패키지를 한 흐름도에서 섞어 썼는가.

    A360에는 같은 일을 하는 패키지가 여럿이다(스프레드시트 4종, 메일 5종). **세션 모델이
    각자**라 `Excel advanced/Open`이 연 세션을 `Microsoft 365 Excel/Format cell`이 못 쓴다 —
    실행 시 "세션 없음"으로 깨진다. 실측(2026-07-28): 흐름도 18개 중 6개가 엑셀 패키지를
    섞었고 한 개는 3종을 함께 썼다.

    역할군은 `derive_competing_packages`가 카탈로그에서 유도한다. 유도가 완벽하지 않아
    (`Word ↔ PowerPoint` 같은 오탐이 있다) blocker가 아니라 major다 — 실행 불가가 확실한
    R13(짝 없는 Try)과 달리 여기는 정당한 병용이 있을 수 있다.

    **먼저 등장한 패키지를 기준으로 삼는다.** 흐름도가 이미 그쪽으로 세션을 열었을 가능성이
    높고, 수리 지시가 "나중 것을 먼저 것으로 바꿔라"로 명확해진다.
    """
    if catalog is None:
        return []
    # 두 검사는 근거가 달라 서로의 결측에 걸리지 않아야 한다 — 경쟁 역할군 유도가 비어도
    # 비권장 표시는 볼 수 있고, 그 반대도 마찬가지다.
    groups = knowledge_derive.derive_competing_packages(catalog)
    lookup = getattr(catalog, "discouraged_packages", None)
    discouraged = lookup() if callable(lookup) else {}
    if not groups and not discouraged:
        return []

    first: dict[str, tuple[str, str, str | None]] = {}  # package -> (location, action, step_id)
    order: list[str] = []

    def walk(actions: list[dict], path: str, step_id: str | None) -> None:
        for idx, a in enumerate(actions):
            pkg, act = a.get("package"), a.get("action")
            loc = f"{path}[{idx}]"
            if pkg and pkg not in first:
                first[pkg] = (loc, act or "", step_id)
                order.append(pkg)
            walk(a.get("children") or [], f"{loc}.children", step_id)

    for step in steps:
        walk(step.get("actions") or [], "actions", step.get("step_id"))

    violations: list[Violation] = []
    for group in groups:
        used = [p for p in order if p in group]
        if len(used) < 2:
            continue
        keep = used[0]
        for pkg in used[1:]:
            loc, act, sid = first[pkg]
            violations.append(Violation(
                "R19", loc,
                f"'{pkg}'와 '{keep}'는 같은 일을 하는 패키지인데 한 흐름도에서 함께 쓰였습니다 — "
                f"패키지마다 세션이 따로라 '{keep}'가 연 세션을 '{pkg}'가 이어받을 수 없습니다. "
                f"둘 중 하나로 통일하세요(필요한 액션이 한쪽에만 있으면 그 패키지 쪽으로).",
                package=pkg, action=act, step_id=sid,
            ))

    # R20 — 신규 개발 비권장 패키지 사용 (warning).
    #
    # 공식 개요 문서가 "신규 봇 개발에 권장하지 않는다"고 명시한 패키지다. 액션 스펙에는
    # 그 사실이 없어 메뉴에서는 멀쩡한 후보로 보이고, 검색 점수도 정상 패키지와 비슷하게
    # 나온다. 실행이 깨지는 결함은 아니므로(마이그레이션 봇 유지보수에서는 정당하다)
    # warning이다 — 대안이 정말 없을 수도 있어 blocker·major로 막지 않는다.
    for pkg in order:
        if pkg not in discouraged:
            continue
        loc, act, sid = first[pkg]
        violations.append(Violation(
            "R20", loc,
            f"'{pkg}'는 신규 봇 개발에 권장되지 않는 패키지입니다 — {discouraged[pkg]} "
            f"같은 일을 하는 다른 패키지의 액션이 있으면 그쪽으로 바꾸세요. "
            f"이 패키지에만 있는 액션이라 대안이 없다면 그 이유를 rationale에 남기세요.",
            package=pkg, action=act, step_id=sid, severity="warning",
        ))
    return violations


# ─────────────────────────────────────────────────────────────────────────────
# R15~R16 — 실행 환경 정합 (v3, 무LLM, warning 전용)
# ─────────────────────────────────────────────────────────────────────────────

# 사람 개입이 필수인 대화형 패키지 — 무인 실행에서 봇을 무기한 멈춘다 (표기 정규화 비교).
_ATTENDED_PACKAGES = frozenset({"messagebox", "prompt"})

# 대상 OS를 못 읽었을 때의 기본값 — 초보자 기본 환경 가정(RPA-210 취지 유지).
DEFAULT_TARGET_OS = "windows"

# 전제 문장에서 OS를 집는 키워드. spec_builder는 "실행 환경: <OS> 러너 (<근거>)" 형식을
# 쓰지만(prompts/spec_builder.md), LLM 산출이라 표기 흔들림에 관대하게 본다.
_OS_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # macOS를 먼저 본다 — "macOS"에도 "os"가 들어가듯 부분일치 충돌을 피하려면 더 구체적인
    # 쪽을 앞에 둬야 한다.
    ("macos", ("macos", "mac os", "osx", "맥os", "맥 os", "매킨토시")),
    ("windows", ("windows", "win32", "윈도우", "윈도")),
)
# 전제 목록에서 '실행 환경'을 말하는 줄인지 — 아무 줄에서나 OS 단어를 줍지 않기 위한 게이트
# ("Windows 공유폴더 경로 사용" 같은 전제를 대상 OS로 오독하지 않게).
_ENV_LINE_MARKERS = ("실행 환경", "실행환경", "러너", "runner", "os:")


def target_os(flow: dict) -> str:
    """이 흐름도가 어느 OS를 대상으로 설계됐는지 (RPA-282).

    출처는 흐름도에 동봉된 채점 기준(flow["spec"].assumptions)이다 — spec_builder가 대화·문서에서
    뽑아 항상 한 줄 남기고, 사용자가 "맥OS로 바꿔줘"라고 하면 edit의 set_flow가 그 줄을 교체한다.
    읽을 수 없으면 DEFAULT_TARGET_OS — 전제가 없던 기존 흐름도의 판정을 바꾸지 않는다.
    """
    spec = flow.get("spec")
    assumptions = (spec.get("assumptions") or []) if isinstance(spec, dict) else []
    for line in assumptions:
        if not isinstance(line, str):
            continue
        low = line.lower()
        if not any(m in low for m in _ENV_LINE_MARKERS):
            continue
        for os_key, words in _OS_KEYWORDS:
            if any(w in low for w in words):
                return os_key
    return DEFAULT_TARGET_OS


def run_environment_checks(flow: dict, catalog: CatalogLookup) -> list[Violation]:
    """실행 환경 정합 검사 (R15~R16) — 둘 다 warning: 경고하되 수리를 강제하지 않는다.

    R15 (attended 함정): 흐름도에 실행 트리거(flow.trigger)가 붙어 있으면 사실상 무인
        실행인데, Message box/Prompt 같은 대화형 액션은 사람이 없을 때 봇을 무기한
        멈춘다 (A360 attended 전용 성격 — 조사 §5.2).
    R16 (플랫폼): 카탈로그 platform 메타(등기부 로스터 실측)가 대상 OS를 지원하지 않는
        패키지는 그 러너에서 실행 불가 — 경고만 한다(차단 아님, 메타가 불완전할 수 있으므로).
        대상 OS는 흐름도의 전제(spec.assumptions)에서 읽는다 — 사용자가 "맥OS로 바꿔줘"라고
        하면 경고 방향도 함께 뒤집힌다(RPA-282). 전제가 없으면 Windows 가정(기존 동작).
    """
    violations: list[Violation] = []
    has_trigger = bool(flow.get("trigger"))
    os_key = target_os(flow)
    os_label = "macOS" if os_key == "macos" else "Windows"

    def walk(actions: list[dict], path: str, step_id: str | None) -> None:
        for idx, a in enumerate(actions):
            pkg, act = a.get("package"), a.get("action")
            loc = f"{path}[{idx}]"
            if has_trigger and pkg and pkg.replace(" ", "").lower() in _ATTENDED_PACKAGES:
                violations.append(Violation(
                    "R15", loc,
                    f"트리거로 자동 실행되는 흐름에 대화형 액션({pkg})이 있습니다 — 사람이 "
                    "없는 무인 실행에서는 이 액션에서 봇이 멈춥니다. 기록(Log)·메일 알림으로 "
                    "대체를 권장합니다.",
                    package=pkg, action=act, step_id=step_id, severity="warning",
                ))
            spec = catalog.get_action_schema(pkg, act) if pkg and act else None
            platform = (spec or {}).get("platform")
            # 대상 OS 키가 명시적으로 False일 때만 경고한다 — 키가 없으면(메타 미수집) 침묵,
            # '모름 → 침묵' 원칙(R2~R5의 params_unknown과 같은 취지).
            if isinstance(platform, dict) and platform.get(os_key) is False:
                violations.append(Violation(
                    "R16", loc,
                    f"'{pkg}' 패키지는 {os_label}를 지원하지 않습니다 — 이 흐름도의 대상 환경이 "
                    f"{os_label} 러너라 해당 액션이 실행되지 않습니다. 대상 환경이나 액션을 "
                    "확인해 주세요.",
                    package=pkg, action=act, step_id=step_id, severity="warning",
                ))
            walk(a.get("children") or [], f"{loc}.children", step_id)

    for step in flow.get("steps") or []:
        walk(step.get("actions") or [], "actions", step.get("step_id"))
    return violations


# ─────────────────────────────────────────────────────────────────────────────
# 통합 실행기 (v3) — L0(R1~R6) + L1(R7~R16) 한 번에
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
    violations.extend(run_structure_checks(steps, container_exceptions(catalog),
                                           closers=reg[1], openers=reg[0]))
    violations.extend(run_package_checks(steps, catalog))
    violations.extend(run_environment_checks(flow, catalog))

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
