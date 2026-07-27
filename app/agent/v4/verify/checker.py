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

RPA-298 신설:
  R17 세션 핸들 패키지 일관성 — 세션을 연 패키지와 다른 패키지가 그 핸들을 소비  (blocker)
  R18 비실행 구획이 요구 담당 — Step/Comment가 req_id를 달고 액션 자리에 있음    (major)

R9~R11의 원료는 스키마 확장 필드 produces/consumes(app/schemas/recommendation.py의
VarRef)다. composer 명시가 1차이고 `$var$` 파싱이 교차 보정한다 — 흐름도에 produces
명시가 하나도 없으면 R9/R10은 침묵한다(정보 없이 검사하면 전부 오탐이므로).
세션 opener/closer와 컨테이너 예외는 공용 지식층(app/agent/knowledge)이 카탈로그에서
유도한다 — 수기 상수는 폴백으로만 남는다. 유도가 한쪽이라도 비면 R7/R8은 침묵한다
('검사 안 함'이 '틀리게 검사함'보다 낫다 — RPA-298).

같은 체커가 타 솔루션 흐름도(대화에서 추출한 UserCatalog)도 검수한다. 어휘는 이식되지만
**구조·세션 모델은 이식되지 않아** R6/R7/R8/R12/R13/R14는 A360에서만 돈다 —
`run_flow_checks(..., is_a360=False)`가 그 게이트다(근거는 해당 docstring).
"""

import logging
import re
from dataclasses import dataclass, field

from app.agent.knowledge import derive as knowledge_derive
from app.agent.knowledge import lexicon

from .catalog import CatalogLookup

logger = logging.getLogger(__name__)

# 본문(children)을 가질 수 있는 컨테이너 패키지 — 공용 지식층에서 온다 (RPA-298).
# 카탈로그 재적재로 거짓이 되지 않는 A360 언어 수준 어휘라 리터럴로 유지된다.
CONTAINER_PACKAGES: frozenset[str] = lexicon.CONTAINER_PACKAGES

# v3가 수기로 들고 있던 '본문 없는 컨테이너 액션' 3쌍. **현행 카탈로그에 전부 부재**라
# Loop/Break가 컨테이너로 오판된다 — 이제 카탈로그에서 유도하고(derive_container_exceptions)
# 이 상수는 유도가 빈 결과를 낼 때의 폴백으로만 남는다.
NON_CONTAINER_ACTIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("Loop", "loopPackageBreakAction"),
        ("Loop", "loopPackageContinueAction"),
        ("Error handler", "errorHandlerThrow"),
    }
)


def container_exceptions(catalog: CatalogLookup | None) -> frozenset[tuple[str, str]]:
    """카탈로그에서 유도한 '본문 없는 컨테이너 액션'. 유도가 비면 수기 상수 폴백."""
    if catalog is None:
        return NON_CONTAINER_ACTIONS
    derived = knowledge_derive.derive_container_exceptions(catalog)
    return derived or NON_CONTAINER_ACTIONS


def is_container(
    package: str | None,
    action: str | None,
    *,
    non_container: frozenset[tuple[str, str]] = frozenset(),
) -> bool:
    """이 액션이 children(본문)을 가질 수 있는 컨테이너인지 판정한다 — R6 기준.

    `non_container`를 안 주면 예외 없이 패키지만 본다. 카탈로그를 아는 호출부는
    `container_exceptions(catalog)` 결과를 넘겨 Break·Continue·Throw를 제외시킨다.
    """
    return lexicon.is_container(package, action, non_container=non_container)

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
        """위반을 repair 프롬프트·관측 로그용 dict로 직렬화한다.

        `param_type`은 `spec_excerpt["type"]`(카탈로그 파라미터 타입)의 사본이다. R3를
        '동작 옵션(결함)'과 '업무 데이터(질문 카드)'로 가르는 판별축인데(설계 §5.2-E),
        실사용 경로는 harness의 dict 셔틀(`from_violations_dicts`)을 지나므로 as_dict가
        떨어뜨리면 findings 쪽에서 타입을 영영 볼 수 없어 **전부 카드로** 흘러버린다.
        `spec_excerpt` 전체를 싣지 않는 이유는 R2의 `valid_params`(액션당 수십 건)가
        SSE 흐름도 프레임에 매 위반마다 실려 payload를 부풀리기 때문이다 — 판별에 필요한
        스칼라 한 개만 승격한다.
        """
        return {
            "rule": self.rule,
            "location": self.location,
            "message": self.message,
            "package": self.package,
            "action": self.action,
            "param": self.param,
            "param_type": self.spec_excerpt.get("type"),
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


def spec_param_names(spec: dict | None) -> frozenset[str] | None:
    """액션 스펙의 파라미터 이름 집합. None이면 **판정 근거 없음**(스펙 부재 또는 파라미터 미상).

    R2가 '스펙에 없는 파라미터'를 가르는 바로 그 집합이다(_check_parameters). 표기를 갈아끼운
    뒤 옛 파라미터를 걷어내는 쪽(edit_ops._retarget_params)이 여기서 **같은 집합**을 받아 쓴다 —
    두 곳이 각자 스펙을 읽으면 "걷어냈는데 R2가 남는다 / 안 걷어냈는데 R2가 없다"가 언젠가 생긴다.

    None을 흘리는 것도 계약이다: params_unknown 행에서 R2가 침묵하듯 걷어내는 쪽도 침묵한다.
    ⚠ parameters가 **리스트가 아니면** None이다. frozenset()은 '파라미터 없는 액션 확정'이라는
    정당한 의미를 이미 갖고 있어(그 경우 전량 삭제가 맞다) '모름'과 표현을 공유하면 안 된다 —
    dict·문자열 슬립에서 빈 집합을 돌려주면 노드 파라미터가 통째로 지워진다.
    """
    if not isinstance(spec, dict):
        return None
    params = spec.get("parameters")
    if not isinstance(params, list):
        return None
    return frozenset(p["name"] for p in params if isinstance(p, dict) and p.get("name"))


def _check_parameters(action: dict, spec: dict, location: str) -> list[Violation]:
    """R2~R5: 파라미터 name·필수·enum·형식 검사. spec이 있는 액션에만 호출된다."""
    violations: list[Violation] = []
    pkg, act = action.get("package"), action.get("action")
    if spec.get("parameters") is None:
        # 파라미터 스펙 미상(BackendCatalog params_unknown 행 — schema 없는 v2 문서 카탈로그
        # 행) — 존재(R1)만 성립하고 R2~R5는 판정 근거가 없다. R3의 required tri-state와 같은
        # '모름 → 침묵' 원칙. 빈 목록([])은 '파라미터 없음' 확정이므로 아래로 진행해 R2가 잡는다.
        return violations
    # ⚠ 계약 변경(2026-07-27): 이름 없는/비-dict 스펙 행을 조용히 건너뛴다. 이전에는
    # p["name"]이 KeyError를, 비-dict가 TypeError를 던져 run_flow_checks가 **통째로** 죽었다 —
    # 카탈로그 한 행의 흠이 검수 전체를 무력화하는 쪽이 더 나쁘다. spec["parameters"] 직접
    # 접근은 위의 `is None` 조기 반환에 기대고 있다(빼면 KeyError가 돌아온다).
    spec_params = {
        p["name"]: p
        for p in spec["parameters"]
        if isinstance(p, dict) and p.get("name")
    }
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
    non_container: frozenset[tuple[str, str]] = frozenset(),
    check_containers: bool = True,
) -> list[Violation]:
    """액션 하나를 R1(카탈로그 존재)·R2~R5(파라미터)·R6(children 컨테이너)로 검사하고 children을 재귀한다.

    `check_containers=False`면 R6를 끈다 — 컨테이너 어휘(CONTAINER_PACKAGES)가 A360
    전용이라 타 솔루션 흐름에서는 전량 오탐이 된다(설계 §6.6). 상세는 run_flow_checks 참조.
    """
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
    if check_containers and children and not is_container(pkg, act, non_container=non_container):
        violations.append(
            Violation(
                "R6", location,
                f"'{pkg}/{act}'은(는) 컨테이너가 아닌데 children이 있습니다.",
                package=pkg, action=act,
            )
        )

    for i, child in enumerate(children):
        violations.extend(
            _check_action(
                child, catalog, f"{location}.children[{i}]", non_container, check_containers
            )
        )
    return violations


def run_checks(
    actions: list[dict],
    catalog: CatalogLookup,
    non_container: frozenset[tuple[str, str]] | None = None,
    *,
    check_containers: bool = True,
) -> list[Violation]:
    """액션 트리(한 단계의 actions[])를 R1~R6로 검사해 위반 목록을 반환한다.

    actions: RecommendedAction.model_dump() 리스트 또는 동형 dict 리스트.
    check_containers=False면 R6를 끈다(타 솔루션 — run_flow_checks의 is_a360 게이트).
    """
    exc = container_exceptions(catalog) if non_container is None else non_container
    violations: list[Violation] = []
    for i, action in enumerate(actions):
        violations.extend(_check_action(action, catalog, f"actions[{i}]", exc, check_containers))
    return violations


# ─────────────────────────────────────────────────────────────────────────────
# 세션 레지스트리 유도 (v3) — 수기 상수 + 카탈로그 메타
# ─────────────────────────────────────────────────────────────────────────────

def derive_session_registry(catalog=None) -> knowledge_derive.SessionRegistry:
    """세션 opener/closer를 카탈로그에서 유도한다 — 공용 지식층에 위임 (RPA-298).

    **반환 타입이 v3와 다르다**: 튜플이 아니라 `SessionRegistry`다. `usable`(양쪽 다
    비어 있지 않음)과 `source`(derived/constants/mixed/empty)를 함께 실어, 호출부가
    "유도가 실패했으니 검사를 건너뛴다"를 판단하고 그 사실을 관측에 남길 수 있게 한다.

    v3의 수기 상수(SESSION_OPENERS/CLOSERS)는 지우지 않고 폴백으로 넘긴다. 다만 그 4쌍은
    현행 카탈로그에 전부 부재라, 유도가 되는 환경에서는 섞이지 않는다.

    왜 v3의 유도 규칙을 안 쓰는가: v3는 `session_role`·`return_type=SESSION`에 의존하는데
    현행 카탈로그 실측 결과 **둘 다 0건**이다. 그래서 opener가 공집합이 되고 closer만
    이름 휴리스틱으로 3건 남아, 열린 적 없는 세션을 닫는 것처럼 보여 R7이 대량 오탐을 냈다.
    지식층은 SESSION 타입 파라미터 보유 패키지로 게이팅한 뒤 이름 패턴을 본다(실측 opener
    51 / closer 48 / 세션패키지 60).
    """
    return knowledge_derive.derive_session_registry(
        catalog,
        fallback_openers=SESSION_OPENERS,
        fallback_closers=SESSION_CLOSERS,
    )


def _is_session_param(param: object) -> bool:
    """세션 이름을 담는 파라미터인지 판정한다. **타입이 있으면 타입이 1순위** (RPA-298).

    이름 부분 일치("session" 포함)만 보면 세션 이름이 **아닌** 파라미터가 걸린다. 카탈로그
    실측 21건:

        Session Token (CREDENTIAL)   ← 비밀값
        Session type (SELECT)        ← enum 선택지
        Session variable (VARIABLE)
        Run bot runner session on Control Room (BOOLEAN)   ← 플래그

    이걸 세션 이름으로 읽으면 BOOLEAN·CREDENTIAL 값이 세션 키가 되어 R7/R8 추적이 어긋난다.

    ⚠️ **현재 호출 경로에서는 아직 이 개선이 발동하지 않는다.** 흐름도의 `ActionParameter`
    스키마가 `{name, label, value, value_source}`라 `type`을 안 싣기 때문이다(카탈로그 스펙에만
    있다). 그래서 지금은 이름 폴백으로 떨어져 v3와 동일하게 동작한다 — 회귀는 없지만 위 21건도
    아직 안 고쳐진다.

    제대로 고치려면 `_session_name`이 카탈로그 스펙을 조회해 SESSION 타입 파라미터의 name을
    알아낸 뒤 흐름도에서 그 이름의 값을 읽어야 한다. `run_session_checks`에 catalog를 흘려야
    해서 별도 작업으로 남긴다. 이 함수는 타입이 실리는 순간 자동으로 옳게 동작한다.
    """
    if isinstance(param, str):
        return "session" in param.replace(" ", "").lower()
    if not isinstance(param, dict):
        return False
    ptype = str(param.get("type") or "").strip().upper()
    if ptype:
        return ptype == "SESSION"
    name = param.get("name")
    return isinstance(name, str) and "session" in name.replace(" ", "").lower()


def _session_name(action: dict) -> str | None:
    """액션의 세션 파라미터 값을 세션 이름으로 반환. 없으면 None.

    'Default'도 유효한 세션 이름이다 — A360에서 Default 세션도 명시적으로 열어야 한다.
    """
    for p in action.get("parameters", []):
        if _is_session_param(p):
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
    registry: knowledge_derive.SessionRegistry | tuple[frozenset, frozenset] | None = None,
    *,
    emit_r12: bool = False,
) -> list[Violation]:
    """전체 흐름도를 분기 인지 심볼릭 실행으로 순회하며 세션 생명주기(R7~R8, 선택 R12)를 검사한다.

    steps: Recommendation.steps[] (각 {step_id, actions[]}).
    registry: `SessionRegistry` 또는 (openers, closers) 튜플. 없으면 수기 상수.

    **유도가 한쪽이라도 비면 검사를 통째로 건너뛴다** (RPA-298). opener 없이 closer만
    있으면 모든 닫기가 "열린 적 없는 세션"으로 잡히고, 반대면 모든 열기가 미종료로 잡힌다 —
    어느 쪽이든 전량 오탐이다. '검사 안 함'이 '틀리게 검사함'보다 낫다: 오탐은 confidence
    감점과 불필요한 surgeon 수리 라운드로 직결되므로 침묵보다 해롭다.
    """
    if isinstance(registry, knowledge_derive.SessionRegistry):
        if not registry.usable:
            logger.info(
                "세션 어휘 유도 불가(source=%s) — R7/R8 건너뜀", registry.source
            )
            return []
        openers, closers = registry.openers, registry.closers
    else:
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


def run_structure_checks(
    steps: list[dict],
    non_container: frozenset[tuple[str, str]] = frozenset(),
) -> list[Violation]:
    """제어 흐름 구조 정합 검사 (R13~R14).

    R13 (Error handler 구조, error):
      - Try의 바로 다음 형제가 Catch/Finally가 아님 — A360에서 Try 다음엔 Catch가 와야
        하며, 사이에 낀 일반 액션은 보호도 안 되고 구조도 깨진다 → Try의 children으로
        옮기라는 수리 지시가 된다.
      - Catch/Finally가 Try 블록에 붙어 있지 않음 (직전 형제가 Try/Catch가 아님).
      - Try 본문(children)이 비어 있음.
    R14 (Loop 구조):
      - Continue/Break가 Loop 본문(조상) 밖에서 사용됨 (error) — Continue를 '반복 처리'로
        오용하면 반복 없이 지나간다.
      - Loop 컨테이너의 본문이 비어 있음 — 반복할 액션이 밖에 있다는 신호.

    ## 빈 본문은 warning이 아니다 (RPA-298, 실측)

    둘 다 원래 `severity="warning"`이었다. 그런데 warning은 `_error_findings`가 걸러
    **교정 목적 함수에 아예 안 들어간다** — surgeon이 손댈 이유가 없다는 뜻이다.
    실측 산출물에서 Error handler Try/Catch/Finally 세 칸이 본문 없이 떠 있고 본업 전체가
    그 밖에 있었는데, 위반은 warning 하나로 조용히 지나갔다. **예외 처리가 있다고 표시된
    채로 실제로는 아무것도 보호하지 않는다** — 장식이 아니라 실행 의미가 깨진 상태다.
    빈 Loop도 같다: 반복할 액션이 밖에 있으면 N번 돌 일이 한 번만 돈다.

    규칙 기본값(major)을 그대로 쓰게 두어 수리 대상에 넣는다. 중간 상태로 빈 컨테이너가
    잠깐 생기는 것을 벌하지 않느냐 — surgeon의 `wrap`은 감쌀 형제를 함께 지정해 본문이
    처음부터 채워지므로, 빈 컨테이너는 '수리 중간'이 아니라 '수리가 안 끝난' 상태다.
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
                            package=pkg, action=act, step_id=step_id,
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
                elif is_container(pkg, act, non_container=non_container) and not children:
                    violations.append(Violation(
                        "R14", loc,
                        "Loop 본문(children)이 비어 있습니다 — 반복할 액션들을 Loop 안에 넣으세요.",
                        package=pkg, action=act, step_id=step_id,
                    ))

            child_depth = loop_depth + (
                1 if pkg == "Loop" and is_container(pkg, act, non_container=non_container) else 0
            )
            walk(children, f"{loc}.children", step_id, child_depth)

    for step in steps:
        walk(step.get("actions") or [], "actions", step.get("step_id"), 0)
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
# R17~R18 (RPA-298) — 실행하면 확실히 깨지는데 기존 규칙이 전부 통과시키던 두 가지
# ─────────────────────────────────────────────────────────────────────────────

def _flat_actions(steps: list[dict]):
    """(경로, step_id, 액션)을 실행 순서대로 — 컨테이너 children 포함."""

    def walk(actions, path, step_id):
        for i, a in enumerate(actions or []):
            if not isinstance(a, dict):
                continue
            loc = f"{path}[{i}]"
            yield loc, step_id, a
            yield from walk(a.get("children"), f"{loc}.children", step_id)

    for step in steps or []:
        if isinstance(step, dict):
            yield from walk(step.get("actions"), "actions", step.get("step_id"))


def _var_names(action: dict, key: str) -> list[str]:
    out = []
    for ref in action.get(key) or []:
        if isinstance(ref, dict) and ref.get("name"):
            out.append(str(ref["name"]))
        elif isinstance(ref, str) and ref.strip():
            out.append(ref.strip())
    return out


def run_session_package_checks(
    steps: list[dict], registry: knowledge_derive.SessionRegistry | None = None
) -> list[Violation]:
    """R17 — 세션 핸들을 **연 패키지와 다른 패키지**가 소비하면 blocker.

    ## 왜 필요한가 (실측)

    2026-07-27 실사용 산출물에서 나온 결함이다:

        3.3  Excel advanced      / Open              produces sExcelSession
        3.5  Microsoft 365 Excel / Paste cell        consumes sExcelSession   ← 여기서 깨진다
        3.7  Excel advanced      / Format table cell consumes sExcelSession

    세션 핸들은 그 패키지의 런타임이 발급한 것이라 **다른 패키지가 받을 수 없다.**
    사람이 그대로 Control Room에 옮기면 3.5에서 실행이 멈춘다.

    그런데 기존 규칙이 전부 통과시켰다:
      - R7/R8은 열기·닫기 **쌍**만 본다 — `Excel advanced`로 열고 닫았으니 만족이다.
      - R9(def-before-use)는 변수가 **먼저 정의됐는지**만 본다 — 정의돼 있으니 만족이다.
      - R1은 두 액션 모두 카탈로그에 **실재**하니 만족이다.
    아무도 "같은 핸들인데 패키지가 갈렸다"를 안 봤다.

    ## 오탐이 0인 이유

    세션 핸들만 본다. 판정 근거는 **카탈로그에서 유도한 opener 목록**이다 —
    `Excel advanced/Open`처럼 세션을 여는 액션이 만든 변수만 핸들로 취급한다.
    `tGoldPrices` 같은 데이터 변수는 패키지를 건너다녀도 정상이라(Data Table을 메일에
    싣는 등) 대상이 아니다. opener 유도가 실패하면(`usable`이 False) 통째로 침묵한다 —
    '검사 안 함'이 '틀리게 검사함'보다 낫다는 이 파일의 기존 방침 그대로다.

    `produces`가 없는 흐름도에서도 자연히 침묵한다(핸들을 특정할 근거가 없다).

    ⚠️ `registry`를 **반드시 넘겨라.** 생략하면 `derive_session_registry()`가 카탈로그 없이
    불려 수기 폴백 상수(opener 4건)로 떨어지고, 그 4건에 없는 패키지의 불일치는 통째로
    놓친다 — 실측으로 이 함정을 밟았다(카탈로그를 주면 51건). 프로덕션 경로
    (`run_flow_checks`)는 유도된 레지스트리를 넘기므로 정상이고, 이 기본값은 테스트·
    단독 호출 편의용이다.
    """
    reg = registry if registry is not None else derive_session_registry()
    if not getattr(reg, "usable", False):
        return []

    # 세션을 연 액션이 만든 변수 → 그 패키지. 같은 이름이 여러 번 열리면 마지막 것이 유효하다
    # (재사용 패턴: 닫고 다시 열기).
    handle_owner: dict[str, tuple[str, str]] = {}
    violations: list[Violation] = []

    for loc, step_id, a in _flat_actions(steps):
        pkg, act = a.get("package"), a.get("action")
        if not pkg or not act:
            continue
        # opener 대조는 **원표기 그대로** — 레지스트리가 카탈로그 표기를 담고 있고
        # 기존 R7/R8(_SessionWalker._process)도 같은 방식이다. 여기서만 정규화하면
        # 두 검사가 같은 액션을 다르게 읽는다.
        is_opener = (pkg, act) in reg.openers or any(
            (r.get("role") or "").lower() == "session"
            for r in a.get("produces") or [] if isinstance(r, dict)
        )
        if is_opener:
            for name in _var_names(a, "produces"):
                handle_owner[name] = (pkg, act)

        for name in _var_names(a, "consumes"):
            owner = handle_owner.get(name)
            # 패키지 비교는 정규화한다 — "Excel advanced"와 "Excel advanced 패키지"는
            # 같은 패키지고, 그 흔들림까지 결함으로 세면 오탐이 된다.
            if owner is None or lexicon.normalize_package(owner[0]) == lexicon.normalize_package(pkg):
                continue
            violations.append(Violation(
                "R17", loc,
                f"세션 '{name}'은(는) {owner[0]}/{owner[1]}이(가) 연 핸들인데 "
                f"{pkg}/{act}이(가) 받고 있습니다 — 세션 핸들은 발급한 패키지 안에서만 "
                f"유효해 실행 시 이 단계에서 멈춥니다. 같은 자원을 다루는 액션은 "
                f"{owner[0]} 패키지로 통일하세요.",
                package=pkg, action=act, step_id=step_id,
            ))
    return violations


def run_scaffold_checks(steps: list[dict]) -> list[Violation]:
    """R18 — 실행되지 않는 구획(Step·Comment)이 요구를 담당한다고 주장하면 major.

    ## 왜 필요한가 (실측)

    같은 산출물에서:

        3.2  Step / Step   req_id=req-7  produces sStartCell   "엑셀 시작 위치 결정"
        3.6  Step / Step   req_id=req-8  produces sTableRange  "테두리 적용 범위 식별"

    A360에서 `Step`은 **구획(주석)**이라 아무것도 실행하지 않는다. 즉 "시작 위치를
    결정한다"는 할 일을 **이름으로만 적고 액션으로 만들지 않은** 상태다. 비전문가가
    Control Room에 옮기면 그 자리에서 멈춘다 — 무엇을 넣으라는 건지 알 수 없다.

    커버리지 검사는 이걸 **못 잡는다.** req_id가 붙어 있으니 "그 요구는 담당 액션이
    있다"로 읽혀 만족으로 계산된다. 누락이 커버리지 뒤에 숨는 정확한 형태다.

    ## 오탐 경계

    - **본문이 실행되면 구획으로 정상**이다(하위 액션을 묶는 용도) — 건너뛴다. children
      유무가 아니라 `performs_work`로 본다: 구획 안에 구획만 있으면(`Step > Step`)
      children은 있는데 실행되는 것은 여전히 없다.
    - **req_id가 없으면** 순수 구획 표시라 정상이다 — 건너뛴다. 요구를 담당한다고
      주장할 때만 결함이다.
    - `produces`가 있으면 정황이 더 짙지만(값을 만든다고 주장) 판정 조건에는 넣지
      않는다 — req_id만으로 이미 충분하고, 조건을 늘리면 놓치는 경우가 생긴다.
    """
    violations: list[Violation] = []
    for loc, step_id, a in _flat_actions(steps):
        if canon_scaffold_package(a.get("package")) is None:
            continue
        if performs_work(a):
            continue  # 하위를 묶는 진짜 구획 — 본문이 실행한다
        req_id = a.get("req_id")
        if not req_id:
            continue
        label = a.get("label") or a.get("action") or "이 단계"
        violations.append(Violation(
            "R18", loc,
            f"'{label}'이(가) 요구 {req_id}을(를) 담당한다고 돼 있는데 "
            f"{a.get('package')}은(는) 실행되지 않는 구획입니다 — 할 일이 이름으로만 "
            f"적혀 있고 액션이 없습니다. 이 자리에서 실제로 무엇을 하는지 "
            f"(값 계산·변수 할당 등) 카탈로그 액션으로 채우세요.",
            package=a.get("package"), action=a.get("action"), step_id=step_id,
        ))
    return violations


# 실행되지 않는 구획 패키지 — 표기 흔들림을 흡수해 판정한다.
_SCAFFOLD_PACKAGES = frozenset({"step", "comment"})


def canon_scaffold_package(package) -> str | None:
    """구획 패키지면 정규화 이름, 아니면 None."""
    if not package:
        return None
    key = re.sub(r"[\s_/.-]+", "", str(package)).lower()
    return key if key in _SCAFFOLD_PACKAGES else None


def performs_work(action: dict) -> bool:
    """이 자리(또는 그 하위)에 **실제로 실행되는 액션**이 하나라도 있는가.

    R18(이 자리가 비었다)과 coverage_det의 빈껍데기 판정(이 요구를 아무도 실행하지 않는다)이
    같은 개념을 두 번 정의하지 않도록 여기 하나만 둔다 — 구획 지식(`_SCAFFOLD_PACKAGES`)이
    사는 자리다. 정의가 갈라지면 "R18은 발화하는데 커버리지는 만족"이라는 지금 고치려는
    바로 그 모순이 다른 모양으로 되돌아온다.

    ## 판정

    구획(Step·Comment)은 **자기 자신은 실행되지 않는다** — 본문이 있으면 본문이 한다.
    그래서 하위를 재귀로 본다: 구획 안에 구획만 들어 있으면 여전히 아무것도 실행되지 않는다
    (`Step > Step`). children 유무만 보면 이 중첩을 놓친다.

    ## 왜 구획만 보는가 (오탐 경계)

    본문이 빈 `Loop`·`If`도 실행되는 것은 없지만 여기서는 실행되는 것으로 친다. 그쪽은
    이미 전용 규칙(빈 본문 검사)이 보고 있고, 무엇보다 이 판정의 소비자인 coverage_det의
    계약이 **'오탐 0인 신호만'**이다. 표기가 낯설거나 모르는 패키지도 실행되는 것으로 본다 —
    모름은 침묵이지 고발이 아니다.
    """
    if not isinstance(action, dict):
        return False
    if canon_scaffold_package(action.get("package")) is None:
        return True
    return any(performs_work(c) for c in action.get("children") or [])


# ─────────────────────────────────────────────────────────────────────────────
# 통합 실행기 (v3) — L0(R1~R6) + L1(R7~R16) 한 번에
# ─────────────────────────────────────────────────────────────────────────────

# 구조·세션 모델에 의존해 A360에서만 유효한 규칙 — is_a360=False면 통째로 끈다 (설계 §6.6).
# 관측·문서용 상수다(게이트 자체는 아래 run_flow_checks가 호출 단위로 건다).
A360_ONLY_RULES: frozenset[str] = frozenset({"R6", "R7", "R8", "R12", "R13", "R14"})


def run_flow_checks(
    flow: dict,
    catalog: CatalogLookup,
    registry: knowledge_derive.SessionRegistry | tuple[frozenset, frozenset] | None = None,
    *,
    is_a360: bool = True,
) -> list[Violation]:
    """흐름도 전체를 L0 정적(R1~R6) + L1 데이터플로우·세션(R7~R12)으로 검사한다.

    registry를 주지 않으면 카탈로그에서 세션 레지스트리를 유도한다(derive_session_registry).
    유도가 실패하면 R7/R8은 침묵한다 — run_session_checks 참조.
    반환 위반의 step_id는 단계별 검사(R1~R6)에도 채워진다 — 국소 교정 라우팅용.

    ## is_a360 — 구조·세션 검사는 A360 전용이다 (설계 §6.6)

    이 체커는 타 솔루션 흐름도(대화에서 추출한 UserCatalog)도 같은 파이프라인으로 검수한다.
    **어휘(R1~R5)는 이식되지만 구조는 이식되지 않는다.** 조사 결과:

    | 제품 | 제어 흐름 | 세션 open/close 쌍 |
    |---|---|---|
    | Power Automate Desktop · Brity RPA | 평평한 리스트 + begin/end 마커 | 유효 (A360과 동일) |
    | UiPath | 중첩 컨테이너(Sequence/TryCatch/ForEach) | **스코프 자동 종료** — 닫는 액션이 없다 |
    | Blue Prism | 2D 플로차트 | 중첩 표현 자체가 불가능, "session"이 다른 뜻 |
    | Power Automate 클라우드 | `runAfter` DAG | 세션 개념 없음 |

    그래서 A360 어휘에 묶인 규칙을 타 솔루션에 돌리면 **올바른 자동화가 결함으로 판정**된다:

    - R6는 `CONTAINER_PACKAGES`(Loop/If/Step/Error handler/Trigger loop)에 없는 패키지에
      children이 있으면 위반이다 → UiPath의 Sequence·TryCatch·ForEach 본문이 **전부 major**가
      되고, 교정 루프가 본문을 풀거나 액션을 지운다. 교정 예산 확대(설계 §5.2-F)로 악화된다.
    - R7/R8은 open/close 쌍을 전제한다 → UiPath에서는 닫는 액션이 없는 게 정상인데 R8이
      전량 미종료로 잡는다.
    - R13/R14는 'Error handler'/'Loop' 패키지명 리터럴에 걸린다 → 타 솔루션에서는 조용하지만,
      조용한 이유가 '검사가 성립해서'가 아니라 '이름이 안 맞아서'다. 명시적으로 끈다.
    - R12a는 없는 예외 구조를 지적하며 **A360의 `Error handler` 패키지를 처방**한다 →
      surgeon이 그대로 넣으면 타 솔루션 카탈로그에 없는 액션이라 R1 blocker로 되돌아온다.

    R1~R5(어휘·파라미터)와 R9~R11(변수 데이터플로우 — 우리 스키마의 produces/consumes가
    원료라 제품 무관), R15/R16(카탈로그 메타 기반 — 메타가 없으면 자연 침묵)은 그대로 돈다.

    기본값이 True인 이유는 하위호환이다 — 호출부(harness.collect_violations 등)가 인자를
    안 넘겨도 A360 동작이 그대로 유지된다. 타 솔루션 경로는 `CatalogContext.is_a360`을
    그대로 흘려주면 된다.
    """
    steps = flow.get("steps") or []
    violations: list[Violation] = []
    # 컨테이너 예외를 한 번만 유도해 R6·R14가 같은 기준을 쓰게 한다.
    exc = container_exceptions(catalog)

    for step in steps:
        step_id = step.get("step_id")
        for v in run_checks(step.get("actions") or [], catalog, exc, check_containers=is_a360):
            v.step_id = v.step_id or step_id
            violations.append(v)

    if is_a360:
        reg = registry or derive_session_registry(catalog)
        violations.extend(run_session_checks(steps, reg, emit_r12=True))
        # R17 — 세션 핸들이 패키지를 건너는 것. A360 세션 모델 전제라 게이트 안이다
        # (UiPath 등은 스코프 자동 종료라 핸들 개념 자체가 다르다).
        violations.extend(run_session_package_checks(steps, reg))
    # R18 — 실행되지 않는 구획이 요구를 담당한다고 주장. 구조 모델과 무관하게
    # "이름만 적고 액션을 안 만들었다"는 어느 솔루션에서나 결함이라 게이트 밖이다.
    violations.extend(run_scaffold_checks(steps))
    violations.extend(run_dataflow_checks(flow, catalog))
    if is_a360:
        violations.extend(run_structure_checks(steps, exc))
    violations.extend(run_environment_checks(flow, catalog))

    # R12a: 규모 있는 흐름도에 예외 처리 구조가 아예 없음 (A360 표준 골격 위배 — warning)
    total, has_eh = _flow_stats(steps)
    if is_a360 and total >= 5 and not has_eh:
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
