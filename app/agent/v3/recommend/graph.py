"""recommend v3 — 품질 루프 흐름도 생성 (설계 §2).

    spec(FlowSpec)                          … 채점 기준 (orchestrator spec_builder가 선행)
      → research (이중 질의 Dossier)         … 동작 단위 검색 → 액션 후보 메뉴
      → compose (4단)                        … 구조 → 능력 요청 → 구조 게이트 → 값
      → verify 스택                          … L0/L1 정적 → L2 커버리지 → L3 시뮬레이션
      → judge (루브릭·하드 게이트)            … 채점과 refine 지시
      → refine (surgeon EditOps 패치 루프)    … 회귀 가드, ≤3라운드
      → finalize                             … sources·confidence 합성·질문 카드·flow_confidence

**넓이가 아니라 깊이로 간다.** 페르소나 3개를 병렬로 뽑아 심판이 고르던 방식을 접고, 관점
하나를 단계로 쪼개 각 단계를 검수로 거른다(_STANCE_FILE 위 주석의 근거 참고). 각 단계의
산출은 다음 단계가 **줄일 수 없다** — 구조 수리는 회귀 가드가 막고, 값 채우기는 아예 구조를
받지 않는다(노드 id별 값 패치라 줄일 구조가 출력에 없다).

구현은 내부 StateGraph 없이 순수 async 파이프라인이다 — emit()은 부모(오케스트레이터)
그래프의 스트림 컨텍스트를 그대로 탄다. 구조 단계 실패만 RuntimeError로 끊고, 그 뒤 단계의
실패는 직전 산출로 진행한다(값이 비면 R3가 질문 카드로 올린다).
"""

import asyncio
import copy
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from app.core.llm import UsageCallbackHandler
from app.schemas import ProgressEvent, Recommendation

from .. import config
from ..orchestrator import edit_ops as _edit_ops
# 메뉴 렌더는 조사 단계와 **같은 함수**를 써야 한다 — 능력 요청으로 덧붙이는 액션이
# 본 메뉴와 다른 모양이면 모델이 두 목록을 다른 것으로 읽는다.
from .research import _menu_block
from .stream import (
    emit,
    emit_candidates_frame,
    emit_flow_frame,
    emit_scorecard_frame,
    emit_verdict_frame,
)

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
_BASE_PROMPT = (_PROMPT_DIR / "compose_agent.md").read_text(encoding="utf-8")
_ADDENDUM = (_PROMPT_DIR / "compose_v3_addendum.md").read_text(encoding="utf-8")
# 구조/값 2단 분리 (2026-07-29) — 한 호출이 고르기·순서·값을 동시에 하다 뒤쪽에서 힘이
# 빠져 "골격만 있고 안이 빈" 흐름도가 4턴 연속 나왔다. outline은 출력 계약만 바꾸고
# 앞의 설계 원칙·어휘 규칙은 그대로 쓴다.
_OUTLINE_CONTRACT = (_PROMPT_DIR / "compose_outline.md").read_text(encoding="utf-8")
_FILL_PROMPT = (_PROMPT_DIR / "compose_fill.md").read_text(encoding="utf-8")
# 설계 관점은 하나다 (2026-07-29). 페르소나 3개로 넓게 뽑아 심판이 고르던 방식을 접고,
# **단계를 나눠 각 단계를 검수로 거르는** 쪽으로 바꿨다.
#
# 근거: 3단 분리(구조/능력요청/값)만으로 구조 위반 가중이 481 → 최대 14로 떨어졌다.
# 깊이가 넓이보다 효과가 컸다. 반면 후보 3개는 (a) 어느 후보가 왜 이겼는지 관측에 안 남고
# (partial 이벤트라 미저장), (b) 심판 루브릭이 2축뿐이며, (c) 세 후보가 설계 관점보다
# 잡음으로 갈리는 경우가 많았다. 후보를 줄여 아낀 예산을 구조 게이트·값 분할에 쓴다.
#
# judge/CandidateReport 배선은 남긴다 — 흐름도 하나를 채점해 verdict·refine 지시를 만드는
# 경로가 그대로 필요하고, `judge_candidates`는 보고가 하나면 LLM 없이 통과시킨다.
_STANCE_FILE = "design_stance.md"
_CANDIDATE_ID = "A"
_CANDIDATE_LABEL = "표준 설계"

# 초안 흐름도를 단계별로 '드러내는' 프레임 사이 지연(초) — v2와 동일한 인지적 페이싱.
_REVEAL_DELAY = 0.18
# 단계(구조·값) 하나의 왕복 상한 — 첫 출력 + 파싱 재출력 1회 + JSON mode 강등 1회.
# 툴 왕복은 없앴다(escape hatch → needs 능력 요청).
_STAGE_MAX_TURNS = 3
_MAX_NEEDS = 4            # 구조 단계가 요청할 수 있는 능력 수
_MAX_NEED_ACTIONS = 12    # 능력 요청 검색으로 메뉴에 덧붙일 액션 수
_NEED_SEARCH_LIMIT = 5    # 능력 요청 1건이 받아올 액션 수

# 구조 게이트에서 판정하는 규칙 — 파라미터·변수 연결 없이도 결론이 나는 것만.
# 빠진 것(R2~R5 파라미터, R9~R11 변수 흐름)은 값 단계 이후 전체 검수가 본다.
_GATE_RULES = frozenset({
    "R1", "R6", "R7", "R8", "R12", "R13", "R14", "R15", "R16", "R17", "R18", "R19",
})
# recommend 검색은 액션 후보 메뉴용 — 문서 페이지·패키지 개요 오염을 막는다.
SEARCH_SOURCE_TYPES = ["action_schema", "bot_example"]

# 추론 강도로 인정하는 값. 오타가 그대로 API에 실려 400을 내지 않게 여기서 거른다
# (모르는 값이면 인자를 아예 안 보낸다 = 공급자 기본값 none).
_REASONING_LEVELS = frozenset({"none", "low", "medium", "high", "xhigh"})

# 출력이 길이 한도에서 잘렸을 때의 재출력 지시. 형식 오류용 문구("다시 출력하라")를 그대로
# 쓰면 같은 길이가 또 나와 **반드시** 재실패한다 — 실측에서 후보 3개가 전부 재시도까지
# 소진하고 탈락했다. 무엇을 줄일지와 **무엇을 줄이면 안 되는지**를 함께 못 박는다:
# 흐름을 잘라 짧게 만드는 건 요구 누락이라 잘린 것보다 나쁘다.
_TRUNCATED_RETRY = (
    "직전 출력이 **길이 한도에서 잘렸다** — JSON이 끝까지 나오지 못했다. "
    "같은 흐름도를 더 짧은 JSON으로 다시 출력하라:\n"
    "- rationale은 액션당 한 구(15자 내외)로 줄인다.\n"
    "- step description은 한 문장으로 줄인다.\n"
    "- **액션·파라미터·children 구조는 줄이지 마라** — 줄일 것은 설명 텍스트뿐이다. "
    "흐름을 잘라 짧게 만들면 요구를 빠뜨린 것이라 더 나쁘다.\n"
    "코드펜스·설명 없이 JSON 객체 하나만 출력하라."
)

# 값 단계용 절단 재출력 지시. 위 문구를 그대로 쓰면 이 단계에 **없는 것**(흐름도·children·
# 액션 구조)을 지키라고 말하게 되고, 정작 줄이면 안 되는 것(지정된 노드 수)은 안 말한다.
_FILL_TRUNCATED_RETRY = (
    "직전 출력이 **길이 한도에서 잘렸다** — JSON이 끝까지 나오지 못했다. "
    "같은 노드들을 더 짧은 JSON으로 다시 출력하라:\n"
    "- `rationale`은 꼭 필요한 것(사용자 입력 필요 사유)만 남기고 지운다.\n"
    "- **[이번에 채울 노드]의 id는 하나도 빼지 마라** — 노드를 줄여 짧게 만들면 그 액션이 "
    "값 없이 남는다. 줄일 것은 설명 텍스트뿐이다.\n"
    "코드펜스·설명 없이 JSON 객체 하나만 출력하라."
)

# 구조 게이트 수리 라운드 상한. 값 단계 전의 조기 여과라 얕게 간다 — 남은 것은 값 단계
# 이후의 전체 refine이 이어받는다.
_GATE_REPAIR_ROUNDS = 2
# 게이트 수리에만 붙는 지시. 이 시점의 흐름도는 **파라미터가 아직 없는 구조**이고, 지적을
# 삭제로 해소하는 길을 막아야 한다 — 실측에서 수리가 커버리지 지적을 받고 그 요구를 담당하던
# 단계를 통째로 지운 뒤 notes에 "자동화 불가"로 적어 낸 적이 있다.
_GATE_REPAIR_NOTE = (
    "[이번 수리의 범위 — 구조만]\n"
    "- 이 흐름도는 **파라미터를 아직 채우지 않은 구조**다. `set_params`로 값을 채우지 마라 — "
    "값은 다음 단계가 채운다. 비어 있는 파라미터는 결함이 아니다.\n"
    "- **액션을 지워서 지적을 해소하지 마라.** remove는 지적이 '이 액션이 잘못됐다'고 "
    "말할 때만 쓰고, 그때도 대신할 액션을 함께 insert한다.\n"
    "- [커버리지] 지적은 그 요구가 흐름에 **없다**는 뜻이다 — 답은 insert다."
)

# 에이전트가 흔히 슬립하는 enum 필드의 허용값 — 벗어나면 안전값으로 강등한다.
_VALID_VALUE_SOURCE = {"schema_default", "llm", "user"}
_VALID_DIRECTION = {"input", "output", "local"}


# ─────────────────────────────────────────────────────────────────────────────
# 헬퍼 (v2 계승 — _coerce/_parse/_attach_sources는 실측 검증 자산)
# ─────────────────────────────────────────────────────────────────────────────

def _make_llm(*, json_mode: bool | None = None, reasoning: str | None = None):
    """compose용 ChatOpenAI 클라이언트를 만든다(사용량 스트리밍 on).

    json_mode를 명시하면 config를 무시하고 그 값을 쓴다 — 호출부가 JSON mode 비호환을
    만났을 때 끄고 다시 만들기 위한 통로다. reasoning도 같은 이유로 인자다.

    출력 상한을 명시한다 — 미지정이면 provider 기본 천장에 걸려 흐름도 JSON이 중간에서
    잘린다(config.COMPOSE_MAX_TOKENS 주석의 실측 참고). ⚠ 추론 토큰도 출력으로 세므로
    이 상한을 함께 쓴다 — 추론이 길면 본문이 잘릴 수 있다.

    JSON mode는 `model_kwargs`로 넘긴다 — langchain_openai 1.3의 ChatOpenAI에는
    `response_format` 전용 필드가 없다. 여기서 ChatOpenAI 인스턴스를 그대로 돌려줘야
    호출부의 `bind_tools`가 성립하므로 `.bind()`를 쓰지 않는다.
    `reasoning_effort`는 1급 필드라 그대로 넣는다.
    """
    from langchain_openai import ChatOpenAI

    kwargs: dict = {
        "model": config.OPENAI_MODEL,
        "api_key": config.OPENAI_API_KEY,
        "stream_usage": True,
    }
    if config.COMPOSE_MAX_TOKENS > 0:
        kwargs["max_tokens"] = config.COMPOSE_MAX_TOKENS
    if config.COMPOSE_JSON_MODE if json_mode is None else json_mode:
        kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}
    if reasoning in _REASONING_LEVELS:
        kwargs["reasoning_effort"] = reasoning
    return ChatOpenAI(**kwargs)


class _FlowParseError(ValueError):
    """흐름도 JSON 파싱 실패 — 진단용으로 원문과 실패 위치를 함께 나른다.

    메시지(str)는 **재시도 프롬프트에 그대로 실리므로** 짧게 두고, 원문은 별도 속성에만
    담는다(프롬프트를 원문으로 부풀리지 않는다). ValueError를 상속해 기존 except 절과
    호출부 계약을 그대로 유지한다.
    """

    def __init__(self, message: str, *, raw: str = "", pos: int | None = None):
        super().__init__(message)
        self.raw = raw
        self.pos = pos


# 실패 지점 발췌 반경(문자). 진단에 필요한 만큼만 — 로그가 흐름도 원문 덤프가 되면
# 안 된다(jsonio._error_digest가 같은 이유로 입력 값을 안 되비춘다).
_EXCERPT_RADIUS = 120


def _parse_excerpt(raw: str, pos: int | None) -> str:
    """파싱이 깨진 지점 앞뒤를 잘라 한 줄 발췌로 만든다 — ⟪…⟫가 실패 위치다.

    이게 없으면 "Expecting ',' delimiter: line 1 column 8094"만 남아 **절단인지 문법
    오류인지, 어느 필드에서 깨졌는지** 알 방법이 없다(실측에서 실제로 그랬다).
    """
    if not raw:
        return ""
    at = len(raw) if pos is None else max(0, min(pos, len(raw)))
    head = raw[max(0, at - _EXCERPT_RADIUS):at]
    tail = raw[at:at + _EXCERPT_RADIUS]
    return f"…{head}⟪여기⟫{tail}…".replace("\n", "\\n")


def _finish_reason(ai) -> str:
    """LLM 응답의 종료 사유 — provider·래퍼 표기 차이를 흡수해 소문자로."""
    meta = getattr(ai, "response_metadata", None) or {}
    return str(meta.get("finish_reason") or meta.get("stop_reason") or "").lower()


def _looks_truncated(ai) -> bool:
    """출력이 길이 한도에서 잘렸는가 — 파싱 실패 원인을 '잘림'과 '형식 오류'로 가른다.

    1차 신호는 finish_reason이고, 안 실려 오는 경로(스트리밍·프록시)를 대비해 본문 꼬리로
    보완한다 — 완결된 JSON은 코드펜스를 걷어내면 '}'로 끝난다. 이 판정은 **파싱이 이미
    실패한 뒤에만** 쓰이므로 오판의 대가는 재시도 문구가 달라지는 것뿐이다.
    """
    if _finish_reason(ai) in ("length", "max_tokens"):
        return True
    text = str(getattr(ai, "content", "") or "").strip()
    return bool(text) and not text.removesuffix("```").strip().endswith("}")


def _to_dict(analysis: Any) -> dict:
    """AnalysisResult(Pydantic) 또는 dict를 상태용 dict로 정규화한다."""
    if hasattr(analysis, "model_dump"):
        return analysis.model_dump()
    return dict(analysis)


# 컨테이너 패키지 → 카탈로그 canonical (package, action). null-action 복원용.
# compose가 깊은 Loop 안 leaf의 action을 비운 채 내보내면(정준환 실측 probe10) package는
# 살아있으므로 여기서 되살린다 — research.py _STRUCTURAL_CANDIDATES와 같은 표기.
#
# ⚠ **분기·실패경로를 action이 결정하는 컨테이너(If·Error handler)는 여기 넣지 않는다.**
# If의 action은 {if, elseIf, else}, Error handler는 {errorHandlerTry, errorHandlerCatch,
# errorHandlerFinally} 중 하나로, action이 비면 elseIf를 if로·catch를 try로 되살려 제어흐름을
# 뒤집는다(새 조건 분기가 열리거나, 오류 처리 구간이 보호 구간으로 둔갑). 형제를 포함한 문맥
# 없이는 어느 변형인지 판별 불가하므로, 이 모호 컨테이너는 아래에서 Step 스캐폴드로 보수적
# 강등한다 — children·label을 보존한 채 순차 실행되는 안전한 과실행이, 틀린 분기 주장보다 낫다.
_CONTAINER_CANON: dict[str, tuple[str, str]] = {
    "loop": ("Loop", "cloudUsingLoopAction"),
    "step": ("Step", "stepAction"),
}


def _recover_identity(a: dict) -> None:
    """package/action이 빈(=검증 폭파) 노드를 되살린다 — action이 흐름 의미를 고정하지 않는
    단일의미 컨테이너(Loop)만 canonical action으로, 그 외(모호 컨테이너 If/Error handler 포함)는
    Step 스캐폴드로. label·children은 보존한다(자식의 실 액션 유실 방지).

    RecommendedAction.package/action은 필수 str이다. compose가 leaf 하나의 action을 null로
    흘리면 model_validate가 통째 거부돼 흐름도 전체가 빈 폴백으로 붕괴한다(정준환 실측 probe10:
    깊은 If 중첩 8곳 null → steps=[]). 정상 노드(둘 다 채워짐)는 손대지 않는다.

    분기(If: if/elseIf/else)·실패경로(Error handler: try/catch/finally) 컨테이너는 action이
    비면 어느 변형인지 판별 불가하므로 canonical로 되살리지 않고(→_CONTAINER_CANON 미포함)
    Step으로 강등한다 — elseIf를 if로, catch를 try로 잘못 복원해 제어흐름을 뒤집지 않기 위함.
    """
    act_ok = isinstance(a.get("action"), str) and a["action"].strip()
    pkg_ok = isinstance(a.get("package"), str) and a["package"].strip()
    if act_ok and pkg_ok:
        return
    canon = _CONTAINER_CANON.get(a["package"].strip().lower()) if pkg_ok else None
    a["package"], a["action"] = canon or ("Step", "stepAction")


def _coerce_action(a: dict, order: int = 1) -> None:
    """액션 dict의 흔한 타입/enum 슬립을 제자리 보정한다 (재귀) — v2 계승 + v3 필드.

    value_source 강등(0단계 버그 방지)·order 폴백에 더해, v3의 produces/consumes가
    dict 리스트가 아니면 버린다(검증 깨짐 방지 — 미기재와 동일한 자연 강등).
    """
    _recover_identity(a)  # null package/action 복원 — 검증 붕괴(빈 흐름도) 방지
    if not isinstance(a.get("order"), int):
        a["order"] = order
    params = a.get("parameters")
    if not isinstance(params, list):
        a["parameters"] = params = []
    for p in params:
        if isinstance(p, dict) and p.get("value_source") not in _VALID_VALUE_SOURCE:
            p["value_source"] = "llm"
    for key in ("produces", "consumes"):
        refs = a.get(key)
        if not isinstance(refs, list):
            a[key] = []
        else:
            a[key] = [
                r if isinstance(r, dict) else {"name": str(r)}
                for r in refs
                if (isinstance(r, dict) and r.get("name")) or (isinstance(r, str) and r.strip())
            ]
    children = a.get("children")
    if not isinstance(children, list):
        a["children"] = children = []
    for i, c in enumerate(children):
        if isinstance(c, dict):
            _coerce_action(c, i + 1)


def _coerce_flow(obj: dict) -> dict:
    """Recommendation 검증 직전, 에이전트 출력의 슬립을 제자리 보정한다 (v2 계승)."""
    steps = obj.get("steps")
    if not isinstance(steps, list):
        obj["steps"] = steps = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        if not isinstance(step.get("step_id"), str) or not step.get("step_id"):
            step["step_id"] = f"step-{i + 1}"
        if not isinstance(step.get("label"), str) or not step.get("label"):
            step["label"] = step.get("step_id") or f"step-{i + 1}"
        actions = step.get("actions")
        if not isinstance(actions, list):
            step["actions"] = actions = []
        for j, a in enumerate(actions):
            if isinstance(a, dict):
                _coerce_action(a, j + 1)
    variables = obj.get("variables")
    if not isinstance(variables, list):
        obj["variables"] = variables = []
    for v in variables:
        if isinstance(v, dict) and v.get("direction") not in _VALID_DIRECTION:
            v["direction"] = "local"
    notes = obj.get("notes")
    if isinstance(notes, list):
        obj["notes"] = " · ".join(str(n) for n in notes) or None
    elif notes is not None and not isinstance(notes, str):
        obj["notes"] = str(notes)
    return obj


def _parse_flow(content: str) -> dict:
    """LLM 최종 출력에서 Recommendation 흐름도 dict를 뽑는다 (코드펜스 내성)."""
    text = (content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise _FlowParseError("JSON 객체를 찾지 못함", raw=text)
    sliced = text[start : end + 1]
    try:
        obj = json.loads(sliced)
    except json.JSONDecodeError as e:
        # pos는 sliced 기준이라 raw도 sliced를 싣는다 — 발췌 위치가 어긋나지 않게.
        raise _FlowParseError(f"JSON 파싱 실패: {e}", raw=sliced, pos=e.pos) from e
    if not isinstance(obj, dict) or "steps" not in obj:
        raise _FlowParseError("최상위에 steps 키가 있는 JSON 객체가 아님", raw=sliced)
    return _coerce_flow(obj)


def _json_object(content: str) -> tuple[dict, str]:
    """출력 텍스트에서 JSON 객체 하나를 뽑는다 (코드펜스 내성). 실패는 _FlowParseError."""
    text = (content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise _FlowParseError("JSON 객체를 찾지 못함", raw=text)
    sliced = text[start : end + 1]
    try:
        obj = json.loads(sliced)
    except json.JSONDecodeError as e:
        raise _FlowParseError(f"JSON 파싱 실패: {e}", raw=sliced, pos=e.pos) from e
    if not isinstance(obj, dict):
        raise _FlowParseError("JSON 객체가 아님", raw=sliced)
    return obj, sliced


def _parse_patch(content: str) -> dict:
    """값 단계 출력 → 노드별 값 패치. 구조는 담기지 않는다.

    값 단계의 출력 계약이 **흐름도 재출력**이던 시절에는, 값을 채우러 부른 호출이
    package·action·label·children·rationale을 통째로 다시 뱉어야 했다. 그래서 (a) 출력이
    길어져 뒤쪽 액션이 빈 채로 나왔고, (b) 모델이 「이 액션은 확정 흐름도에 없다」며 구조를
    줄여 내는 일까지 있었다(그래서 이식 병합이라는 방어 코드가 필요했다).

    id별 값만 받으면 둘 다 원인째 사라진다 — 구조를 안 보내니 잃을 구조가 없고, 출력은
    값에만 쓰인다. 자리 맞추기도 (index, package, action) 휴리스틱이 아니라 id 정확 일치다.
    """
    obj, sliced = _json_object(content)
    nodes = obj.get("nodes")
    if not isinstance(nodes, list):
        raise _FlowParseError("최상위에 nodes 배열이 있는 JSON 객체가 아님", raw=sliced)
    out: list[dict] = []
    for n in nodes:
        if not isinstance(n, dict) or not isinstance(n.get("id"), str):
            continue
        patch = {"id": n["id"]}
        for field in _VALUE_FIELDS:
            v = n.get(field)
            if isinstance(v, list):
                patch[field] = [x for x in v if isinstance(x, dict)]
        out.append(patch)
    adds = obj.get("variables_add")
    notes = obj.get("notes")
    return {
        "nodes": out,
        "variables_add": [v for v in adds if isinstance(v, dict)] if isinstance(adds, list) else [],
        "notes": notes if isinstance(notes, str) else "",
    }


def _attach_sources(flow: dict, sink: list[dict]) -> dict:
    """검색 히트(sink)에서 (package, action)별 최고 점수 근거를 액션에 부착한다 (FR-11)."""
    best: dict[tuple, dict] = {}
    for h in sink:
        key = (h.get("package_name"), h.get("action_name"))
        if not key[0] or not key[1]:
            continue
        cur = best.get(key)
        if cur is None or (h.get("score") or 0) > (cur.get("score") or 0):
            best[key] = h

    def walk(actions: list[dict]) -> None:
        for a in actions:
            hit = best.get((a.get("package"), a.get("action")))
            if hit and not a.get("sources"):
                a["sources"] = [{
                    "source_type": hit.get("source_type") or "action_schema",
                    "title": hit.get("title") or f"{a.get('package')}/{a.get('action')}",
                    "url": hit.get("url"),
                    "score": hit.get("score"),
                }]
            walk(a.get("children") or [])

    for step in flow.get("steps", []):
        walk(step.get("actions") or [])
    return flow


def _iter_pkg_actions(flow: dict):
    def walk(actions: list[dict]):
        for a in actions:
            if a.get("package") and a.get("action"):
                yield (a["package"], a["action"])
            yield from walk(a.get("children") or [])

    for step in flow.get("steps", []):
        yield from walk(step.get("actions") or [])


def _flow_counts(flow: dict) -> tuple[int, int]:
    steps = flow.get("steps") or []
    actions = sum(1 for _ in _iter_pkg_actions(flow))
    return len(steps), actions


def _render_spec_block(spec: dict) -> str:
    lines = [f"목표: {spec.get('goal', '')}"]
    for r in spec.get("requirements") or []:
        lines.append(f"- [{r.get('req_id')}] ({r.get('priority', 'must')}) {r.get('text', '')}")
    if spec.get("error_policy"):
        lines.append("예외 처리 기대: " + " / ".join(spec["error_policy"]))
    if spec.get("assumptions"):
        lines.append("전제(이미 확정): " + " / ".join(spec["assumptions"]))
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# compose — 페르소나 후보 생성 (Dossier 주입 + escape hatch ≤2회)
# ─────────────────────────────────────────────────────────────────────────────

def _capability_menu(needs: list[dict], sink: list[dict], ctx) -> str:
    """능력 요청(needs)을 검색해 추가 액션 메뉴 블록을 만든다.

    escape hatch(툴 왕복)를 대체한다. 툴은 **모델이 부를지 말지를 스스로 정하는데**, 실측
    (2026-07-28)에서 「엑셀 테두리」를 아무도 안 물어보고 "액션이 없다"며 must 요구를 통째로
    비웠다 — 정작 `format cell border excel`로 검색하면 0.90으로 나오는 액션이었다.
    구조 단계가 필요를 **선언**하게 하면 그 판단이 사라진다.

    질의는 outline이 직접 쓴다(중간에 LLM을 두지 않는다). 실측상 composer가 쓴 질의가
    조사 질의보다 정확했다 — 같은 액션을 0.914 대 0.430으로 찾았다. 메뉴를 본 쪽이
    카탈로그 어휘를 안다.
    """
    if not needs or ctx is None or not getattr(ctx, "searchable", False):
        return ""
    queries = [str(n.get("query") or n.get("what") or "").strip() for n in needs]
    queries = [q for q in queries if q][:_MAX_NEEDS]
    if not queries:
        return ""

    found: dict[tuple[str, str], float] = {}
    for q in queries:
        try:
            hits = ctx.retriever.search(q, limit=_NEED_SEARCH_LIMIT,
                                        source_types=SEARCH_SOURCE_TYPES)
        except Exception as e:  # noqa: BLE001 — 요청 한 건 실패가 보강 전체를 막지 않게
            logger.warning("능력 요청 검색 실패(%r): %s", q[:50], e)
            continue
        sink.extend(hits)
        for h in hits:
            pkg, act = h.get("package_name"), h.get("action_name")
            if pkg and act:
                key = (pkg, act)
                found[key] = max(found.get(key, 0.0), h.get("score") or 0.0)

    blocks: list[str] = []
    for (pkg, act), _ in sorted(found.items(), key=lambda kv: kv[1], reverse=True)[:_MAX_NEED_ACTIONS]:
        spec_dict = ctx.catalog.get_action_schema(pkg, act)
        if spec_dict is not None:  # 폐쇄 어휘 — 카탈로그에 실재하는 것만
            blocks.append(_menu_block(pkg, act, spec_dict))
    if not blocks:
        return ""
    asked = " · ".join(f"{n.get('what') or n.get('query')}" for n in needs[:_MAX_NEEDS])
    return (
        f"\n\n[추가 조사 결과 — 당신이 요청한 것: {asked}]\n"
        "요청한 기능으로 검색한 액션들이다. 여기서 골라 쓰고, 그래도 없으면 notes에 "
        "자동화 불가로 남겨라(이번이 마지막 회차다).\n" + "\n".join(blocks)
    )


def _static_gate_issues(outline: dict, ctx) -> list[str]:
    """구조만으로 판정되는 결함 (LLM 0회). 수리 전후 비교에도 쓰이므로 따로 뺐다."""
    from ..orchestrator.harness import collect_violations, from_violations_dicts

    try:
        violations = collect_violations(outline, getattr(ctx, "catalog", None))
        findings, _ = from_violations_dicts(violations)
    except Exception as e:  # noqa: BLE001 — 게이트 실패가 생성을 막지 않게
        logger.warning("구조 게이트 정적 검수 실패 — 건너뜀: %s", e)
        return []
    return [
        f"- [{f.rule}] {f.location or ''} {f.message}".strip()
        for f in findings
        if f.rule in _GATE_RULES and f.severity in ("blocker", "major")
    ]


def _gate_issues(outline: dict, spec: dict, ctx) -> tuple[list[str], list]:
    """값을 채우기 **전에** 구조만으로 잡히는 결함을 모은다.

    반환: (사람이 읽는 지시 줄, 커버리지 Finding 목록). 앞은 관측용, 뒤는 수리 입력이다 —
    정적 위반은 수리가 흐름도에서 다시 계산하므로 넘기지 않는다.

    두 갈래를 본다:
      ① 정적 구조 검수 (LLM 0회) — 파라미터가 없어도 판정되는 규칙만. R2~R5(파라미터)와
         R9~R11(변수 흐름)은 값 단계가 채워야 판정되므로 여기서 빼고 나중 전체 검수에 맡긴다.
      ② L2 커버리지 (LLM 1회) — must 요구가 구조에 실제로 들어갔는가. 요구가 통째로 빠진
         걸 **값을 다 채운 뒤에** 알면 그 비용이 버려진다.

    warning은 지시에 넣지 않는다 — 사소한 것에 재호출이 돌면 비용만 늘고, 어차피 전체
    검수와 refine이 이어받는다. 게이트는 통과 조건이 아니라 **조기 여과**다.

    ## partial도 지적한다 — 게이트와 점수가 같은 잣대를 써야 한다

    `must_coverage`는 **covered만** 센다(partial은 0점, missing과 같다). 그런데 게이트는
    오래도록 missing·violated만 지적하고 partial은 조용히 넘겼다. 그래서 게이트가 통과시킨
    구조가 최종 채점에서 2/7을 받는 일이 생긴다 — 실측(2026-07-29)에서 빈 Loop로 「최근
    3일치 반영」을, 표 생성 액션으로 「테두리 설정」을 때운 구조가 게이트를 그대로 통과했다.

    게이트는 **점수가 감점하는 것과 같은 것**을 지적해야 조기 여과로 값을 한다.
    """
    from ..verify import findings as F
    from ..verify.semantic import run_semantic_check

    issues = _static_gate_issues(outline, ctx)
    coverage_findings: list = []
    try:
        coverage = run_semantic_check(spec, outline, purpose="turn_generate")
        for entry in coverage.entries:
            if entry.priority == "must" and entry.status in ("missing", "violated", "partial"):
                note = f" ({entry.note})" if entry.note else ""
                issues.append(f"- [커버리지] 필수 요구 {entry.req_id}가 {entry.status}{note}")
        # 시나리오 공백(req_id 없는 minor)은 요구가 아니라 조언이라 수리 입력에서 뺀다 —
        # 게이트에서 그걸 고치려 들면 스펙에 없는 액션이 늘어난다(과생성).
        coverage_findings = [
            f for f in F.from_coverage(coverage)
            if f.req_id and f.severity in ("blocker", "major")
        ]
    except Exception as e:  # noqa: BLE001
        logger.warning("구조 게이트 커버리지 실패 — 건너뜀: %s", e)
    return issues, coverage_findings


def _coverage_gaps(coverage_findings: list, spec: dict) -> list[dict]:
    """커버리지 미달 요구를 **능력 요청(needs) 꼴**로 바꾼다. 검색어는 요구 문구 그대로.

    수리(surgeon)에게는 카탈로그를 검색할 수단이 없다 — `repair_spec_excerpts`는 **이미
    흐름도에 있는** 액션의 파라미터 스펙만 준다. 그래서 "이 요구를 수행할 액션이 없다"는
    지적을 받으면 아는 것 중에서 고르게 되고, 그게 매번 `Step`이다: 실측(2026-07-29)에서
    게이트 수리가 `wrap n9..n12 Step/Step`(채택되고도 같은 라벨 Step이 중첩만 됨),
    `insert Step/Step` ×2·×3(빈 Step은 R17이라 가중치 0→20·0→30)으로 답했다.
    누적 53라운드 중 채택 7의 상당 부분이 이 유형이다.

    액션이 없다는 지적은 **찾아서 넣어야** 풀린다. 그래서 검색을 가진 경로(능력 요청)로
    돌린다 — 그쪽은 이 세션에서 `Format cell` 같은 걸 실제로 찾아 넣었다.

    검색어로 요구 문구를 그대로 쓴다. 조사 단계도 한국어 질의를 보내고 있고(하이브리드
    검색 + 리랭커), 요구 문구가 그 요구를 가장 정확히 서술한 문장이다. 여기서 모델을 한 번
    더 불러 검색어를 만들게 하면 비용만 늘고 원문에서 멀어진다.
    """
    texts = {
        r.get("req_id"): (r.get("text") or "").strip()
        for r in (spec.get("requirements") or []) if isinstance(r, dict)
    }
    out: list[dict] = []
    seen: set = set()
    for f in coverage_findings:
        rid = getattr(f, "req_id", None)
        text = texts.get(rid)
        if not text or rid in seen:
            continue
        seen.add(rid)
        out.append({"what": text, "query": text, "req_id": rid})
    return out[:_MAX_NEEDS]


def _coverage_retry_user(base_user: str, gaps: list[dict]) -> str:
    """커버리지 보완 회차의 지시 — 무엇을 빠뜨렸는지 말해 주고, 때우는 길을 막는다."""
    return (
        f"{base_user}\n\n"
        "[직전 초안이 빠뜨린 필수 요구]\n"
        + "\n".join(f"- {g['what']}" for g in gaps)
        + "\n\n이 요구를 수행하는 액션을 [추가 조사 결과]에서 찾아 흐름도에 넣어라. "
        "나머지 구조는 그대로 유지한다 — 이미 담긴 요구를 빼면서 이걸 넣는 것이 아니다.\n"
        "⚠ **빈 `Step`으로 자리를 잡지 마라.** 라벨만 붙은 구획은 아무것도 실행하지 않아 "
        "요구를 이행한 시늉일 뿐이고 검수 R17이 잡는다. 정말 대응 액션이 없으면 `notes`에 "
        "자동화 불가로 정직하게 남겨라."
    )


_VALUE_FIELDS = ("parameters", "produces", "consumes")
# 값 단계가 노드를 가리키는 열쇠. surgeon이 이미 쓰는 것과 **같은 id**여야 한다 —
# 두 곳이 다른 이름표를 붙이면 같은 흐름도를 두고 서로 다른 좌표계로 말하게 된다.
_NODE_ID = _edit_ops._ID


def _subtree_size(action: dict) -> int:
    """자기 자신 + 모든 자손 액션 수."""
    return 1 + sum(
        _subtree_size(c) for c in (action.get("children") or []) if isinstance(c, dict)
    )


def _fill_units(actions: list[dict], cap: int) -> list[list[str]]:
    """액션 목록을 '한 호출이 통째로 맡을 수 있는' id 묶음들로 자른다 (순서 보존).

    subtree가 상한 이하면 그 subtree 전체가 한 단위다 — 컨테이너와 그 안의 업무가 같은
    호출에 남아야 「이 Loop가 무엇을 반복하는지」를 보며 값을 정할 수 있다. 상한을 넘으면
    컨테이너 **자신만** 떼어 한 단위로 만들고 자식들을 같은 규칙으로 다시 잰다.

    자식이 없으면 크기가 1이라 항상 상한 이하다 — 그래서 어떤 단위도 상한을 넘지 않는다.
    """
    units: list[list[str]] = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        kids = [c for c in (a.get("children") or []) if isinstance(c, dict)]
        if not kids or _subtree_size(a) <= cap:
            units.append(_subtree_ids(a))
        else:
            units.append([a.get(_NODE_ID)])
            units.extend(_fill_units(kids, cap))
    return units


def _subtree_ids(action: dict) -> list[str]:
    """subtree의 id를 pre-order로 — 프롬프트에 보여줄 순서와 같다."""
    out = [action.get(_NODE_ID)]
    for c in action.get("children") or []:
        if isinstance(c, dict):
            out.extend(_subtree_ids(c))
    return out


def _fill_chunks(outline: dict, cap: int) -> list[list[str]]:
    """id가 붙은 흐름도 → 호출별로 맡을 id 목록. 연속 단위를 상한까지 그리디로 묶는다."""
    cap = max(1, cap)
    units: list[list[str]] = []
    for s in outline.get("steps") or []:
        if isinstance(s, dict):
            units.extend(_fill_units(s.get("actions") or [], cap))

    chunks: list[list[str]] = []
    cur: list[str] = []
    for u in units:
        ids = [i for i in u if i]
        if not ids:
            continue
        if cur and len(cur) + len(ids) > cap:
            chunks.append(cur)
            cur = []
        cur.extend(ids)
    if cur:
        chunks.append(cur)
    return chunks


def _node_index(flow: dict) -> dict[str, dict]:
    """id → 액션 dict. `annotate_ids`가 붙인 전이 id 기준."""
    index: dict[str, dict] = {}

    def walk(actions: list[dict]) -> None:
        for a in actions:
            if not isinstance(a, dict):
                continue
            nid = a.get(_NODE_ID)
            if nid:
                index[nid] = a
            walk(a.get("children") or [])

    for s in flow.get("steps") or []:
        if isinstance(s, dict):
            walk(s.get("actions") or [])
    return index


def _apply_patches(
    outline: dict, patches: list[dict], allowed: set[str] | None = None
) -> tuple[int, int, int]:
    """값 패치를 id로 제자리에 넣는다. (반영, 없는 id, 범위 밖 id).

    구조는 손대지 않는다 — 패치에 구조가 없으니 손댈 수단 자체가 없다. 없는 id는 조용히
    버린다: 그 노드는 값이 빈 채로 남고, 필수 파라미터가 비면 R3가 질문 카드로 올린다.

    `allowed`는 **이번 조각이 맡은 id**다. 조각들은 흐름도 전체를 맥락으로 보므로 남의 몫을
    함께 내는 일이 생기는데, 그걸 받으면 나중에 병합된 조각이 앞 조각의 값을 덮어써 결과가
    조각 순서에 좌우된다. 각자 제 몫만 쓰게 막고, 넘어온 개수는 세어 관측에 남긴다 —
    프롬프트나 모델이 회귀하면 이 숫자가 먼저 움직인다.
    """
    index = _node_index(outline)
    applied = unknown = out_of_scope = 0
    seen: set[str] = set()
    for p in patches:
        nid = p.get("id")
        if not isinstance(nid, str):
            unknown += 1
            continue
        # **존재 여부를 먼저 본다.** 범위를 먼저 보면 지어낸 id가 전부 '남의 몫'으로 분류돼
        # unknown이 영영 0이 된다(조각의 allowed는 흐름도에서 잘라 만든 거라 그 안의 id는
        # 반드시 존재한다). 둘은 처방이 다르다 — 남의 몫은 프롬프트 범위 지시가 약한 것이고,
        # 없는 id는 모델이 id를 지어낸 것이다.
        node = index.get(nid)
        if node is None:
            unknown += 1
            continue
        if allowed is not None and nid not in allowed:
            out_of_scope += 1
            continue
        if nid in seen:
            continue          # 같은 id를 두 번 내면 첫 번째만 쓴다
        seen.add(nid)
        for field in _VALUE_FIELDS:
            if field in p:
                node[field] = p[field]
        applied += 1
    return applied, unknown, out_of_scope


def _merge_variables(outline: dict, adds: list[dict]) -> int:
    """값 단계가 새로 쓴 변수를 선언에 더한다 — 이름이 같으면 구조 단계 선언이 이긴다."""
    declared = [v for v in (outline.get("variables") or []) if isinstance(v, dict)]
    have = {v.get("name") for v in declared}
    added = 0
    for v in adds:
        name = v.get("name")
        if not name or name in have:
            continue
        declared.append(v)
        have.add(name)
        added += 1
    outline["variables"] = declared
    return added


def _emit_gate(cid: str, issues: list[str], outline: dict) -> None:
    """게이트 판정을 **turn_events에 남긴다** — 로그로는 사후 추적이 안 된다.

    실측(2026-07-29): 게이트·능력요청·값분할을 전부 logger.info로 찍었는데 컨테이너
    루트 로거가 WARNING이라 한 줄도 남지 않았다. 턴이 이상하게 나왔을 때 남은 단서가
    llm_usage의 토큰 수뿐이어서, 어느 단계가 무엇을 했는지 역산해야 했다.
    판정 지점은 로그가 아니라 관측 이벤트로 남긴다.
    """
    emit({
        "event": "stage", "stage": "verifying",
        "message": (f"구조 사전 검수 {len(issues)}건 — 수리 후 값을 채웁니다"
                    if issues else "구조 사전 검수 통과"),
        "data": {"candidate": cid, "actions": _count_actions(outline),
                 "steps": len(outline.get("steps") or []), "issues": issues[:12]},
    })


def _count_top_tries(flow: dict) -> int:
    """최상위 형제로 선 Try 덩어리 수 — 계획과 결과를 맞춰 보는 값(R13과 같은 기준)."""
    from app.agent.knowledge.lexicon import eh_role

    n = 0
    for s in flow.get("steps") or []:
        if not isinstance(s, dict):
            continue
        for a in s.get("actions") or []:
            if isinstance(a, dict) and a.get("package") == "Error handler" \
                    and eh_role(a.get("action")) == "try":
                n += 1
    return n


def _emit_plan(cid: str, plan: dict, outline: dict) -> None:
    """구조 단계가 스스로 적은 배치 계획을 관측에 남기고, 결과와 어긋나는지 본다.

    `steps`를 쓰기 시작하면 앞에 쓴 토큰이 뒤를 묶는다. 그래서 액션을 나열하기 전에 배치를
    말로 정하게 했다(compose_outline.md의 `plan`). 여기서 하는 일은 둘이다:

    ① **읽을 수 있게 남긴다.** 「왜 Try를 넷으로 나눴나」를 정황이 아니라 모델의 말로 읽는다 —
       지금까지 같은 자리에서 네 턴을 막혔는데 원인을 매번 역산해야 했다.
    ② **자기모순을 잰다.** `try_blocks`·`step_count`는 숫자라 실제와 기계적으로 비교된다.
       "Try 하나로 감싼다"고 써 놓고 넷을 쓴 것과, 애초에 넷으로 계획한 것은 처방이 다르다 —
       앞은 실행이 계획을 못 따라간 것이고, 뒤는 계획 자체가 규칙을 어긴 것이다.

    이번 회차는 **관측만 한다.** 계획을 넣은 효과와 어긋남을 되먹이는 효과가 섞이면 어느 쪽이
    듣는지 못 가른다 — 이 세션에서 이미 한 번 겪었다.
    """
    if not isinstance(plan, dict) or not plan:
        return
    actual_tries = _count_top_tries(outline)
    actual_steps = len([s for s in (outline.get("steps") or []) if isinstance(s, dict)])
    said_tries, said_steps = plan.get("try_blocks"), plan.get("step_count")
    gaps: list[str] = []
    if isinstance(said_tries, int) and said_tries != actual_tries:
        gaps.append(f"Try 덩어리 계획 {said_tries} · 실제 {actual_tries}")
    if isinstance(said_steps, int) and said_steps != actual_steps:
        gaps.append(f"step 계획 {said_steps} · 실제 {actual_steps}")
    emit({
        "event": "stage", "stage": "recommending",
        "message": ("구조 배치 계획 — " + " / ".join(gaps)) if gaps else "구조 배치 계획대로 작성됨",
        "data": {
            "candidate": cid,
            "sessions": str(plan.get("sessions") or "")[:300],
            "error_boundary": str(plan.get("error_boundary") or "")[:300],
            "step_layout": str(plan.get("step_layout") or "")[:300],
            "planned": {"try_blocks": said_tries, "steps": said_steps},
            "actual": {"try_blocks": actual_tries, "steps": actual_steps},
            "gaps": gaps,
        },
    })


def _unknown_actions(flow: dict, catalog) -> list[tuple[str, str, str]]:
    """카탈로그에 없는 (package, action)을 (위치, 패키지, 액션)으로 모은다 — R1과 같은 판정."""
    if catalog is None:
        return []
    out: list[tuple[str, str, str]] = []

    def walk(actions: list[dict], path: str) -> None:
        for i, a in enumerate(actions):
            if not isinstance(a, dict):
                continue
            pkg, act = a.get("package"), a.get("action")
            loc = f"{path}[{i}]"
            if pkg and act and catalog.get_action_schema(pkg, act) is None:
                out.append((loc, str(pkg), str(act)))
            walk(a.get("children") or [], f"{loc}.children")

    for s in flow.get("steps") or []:
        if isinstance(s, dict):
            walk(s.get("actions") or [], "actions")
    return out


def _notation_hint(pkg: str, act: str, catalog) -> str | None:
    """오염된 액션 표기에서 **카탈로그에 실재하는 이름**을 찾아 준다. 못 찾으면 None.

    메뉴 한 줄이 `- 패키지/액션 «라벨»`이라 슬래시가 두 역할을 겸한다. 실측(2026-07-29)에서
    모델이 이걸 액션 이름의 일부로 읽고 라벨까지 붙여 썼다 —
    `Structured data extraction/구조화된 데이터 추출`, `Send/보내기`.

    **고치지 않는다. 알려만 준다.** 같은 패키지에서 슬래시 앞부분이 실재할 때만 후보로 내고,
    실제 교체는 모델이 다시 출력하며 한다 — 코드가 이름을 바꾸기 시작하면 '비슷한 이름'으로
    엉뚱한 액션을 조용히 집어넣는 길이 열린다.
    """
    parts = act.split("/")
    for cut in range(len(parts) - 1, 0, -1):
        head = "/".join(parts[:cut]).strip()
        if head and catalog.get_action_schema(pkg, head) is not None:
            return head
    return None


def _vocab_retry_user(outline: dict, unknown: list[tuple[str, str, str]], catalog) -> str:
    """닫힌 어휘 재요청 문구 — 어긋난 표기를 지목하고, 메뉴 표기 규칙을 다시 못 박는다."""
    lines = []
    for _loc, pkg, act in unknown[:12]:
        lines.append(f'  {pkg} / "{act}"')
        if hint := _notation_hint(pkg, act, catalog):
            lines.append(f'      → 메뉴에 있는 표기: {pkg} / "{hint}"')
    return (
        "아래 표기가 [액션 후보 메뉴]에 없다 — 이대로면 검수 R1(카탈로그에 없는 액션)로 "
        "전부 걸려 흐름도가 무너진다.\n"
        + "\n".join(lines)
        + "\n\n⚠ 메뉴 한 줄은 `- 패키지/액션 «라벨»` 형식이다. **«» 안은 라벨이지 액션 이름이 "
        "아니다** — 액션 이름에 라벨을 이어 붙이거나 슬래시를 더 넣지 마라.\n"
        "표기만 고쳐 다시 출력하라. **액션을 지우거나 구조를 바꾸지 마라** — 메뉴에 정말 "
        "대응이 없는 것만 남겨 두고 notes에 사유를 적는다.\n\n"
        f"[현재 구조]\n{json.dumps(outline, ensure_ascii=False)}"
    )


def _gate_repair(outline: dict, coverage_findings: list, cid: str, ctx) -> dict:
    """구조 게이트가 잡은 결함을 **국소 편집(EditOps)** 으로 고친다.

    전에는 "구조 전체를 다시 출력하라"였다. 그건 3단 분리로 없앤 실패 모드 — 긴 재출력의
    뒤쪽이 빠지는 것 — 를 수리에서 되풀이하는 일이라, 실측 2턴 연속으로 수리본이 반려됐다
    (한 번은 단계 3개를 1개로 뭉갰고, 한 번은 빈 Try 지적을 빈 Step으로 덮어 결함이 늘었다).
    반려는 옳았지만 **고칠 기회를 한 번 쓰고 못 고친 것**이다.

    surgeon은 흐름도를 다시 쓰지 않고 insert/replace/move 같은 연산만 낸다 — 뭉갤 방법도,
    잃을 방법도 없다. 「Connect 없이 Disconnect」 같은 결함은 insert 한 번이면 끝난다.

    회귀 가드는 두 겹이다. refine_flow가 **게이트 규칙 가중합이 줄었을 때만** 채택하고
    (rules=_GATE_RULES — 값이 없는 구조라 파라미터 규칙까지 세면 안 된다), 그 위에
    액션·단계 수가 줄었는지 한 번 더 본다(remove 연산으로 요구가 사라지는 길 차단).
    """
    from ..orchestrator.harness import refine_flow

    catalog = getattr(ctx, "catalog", None)
    if catalog is None:
        return outline
    try:
        out = refine_flow(
            outline, catalog,
            extra_findings=coverage_findings,
            max_rounds=_GATE_REPAIR_ROUNDS,
            purpose="turn_generate",
            rules=_GATE_RULES,
            note=_GATE_REPAIR_NOTE,
            caption="구조 사전 검수 결과를 국소 수리 중",
        )
    except Exception as e:  # noqa: BLE001 — 수리 실패가 생성을 막지 않게
        logger.warning("후보 %s 구조 수리 실패 — 원래 구조로 진행: %s", cid, e)
        return outline

    repaired = out["flow"]
    if not out["repaired"]:
        return outline
    if lost := _repair_regression(outline, repaired):
        logger.warning("후보 %s 구조 수리 반려 — %s. 원래 구조로 진행", cid, lost)
        emit({"event": "stage", "stage": "verifying",
              "message": "구조 수리가 흐름을 줄여 반려했습니다 — 원안으로 진행",
              "data": {"candidate": cid, "regression": lost}})
        return outline
    emit({"event": "stage", "stage": "verifying",
          "message": "구조 사전 검수 지적을 수리했습니다",
          "data": {"candidate": cid, "actions": _count_actions(repaired),
                   "steps": len(repaired.get("steps") or [])}})
    return repaired


# 흐름 크기 판정은 **게이트 수리와 refine 루프가 같은 함수를 써야 한다** — 한쪽만 막으면
# 다른 쪽으로 같은 일이 성립한다(실측: 게이트에만 가드가 있어 refine이 뚫려 있었다).
_count_actions = _edit_ops.count_actions
_repair_regression = _edit_ops.shrink_reason


def _vocab_regression(before: dict, after: dict, catalog) -> str | None:
    """`after`가 `before`보다 **메뉴에 없는 액션을 더** 달고 있으면 그 이유를 한 줄로.

    `_repair_regression`과 같은 자리에 서는 판정이다 — 저건 "구조가 줄었나", 이건 "어휘가
    오염됐나"를 본다. 둘 다 **다시 만든 구조를 받아도 되나**에 답한다.

    왜 필요한가: `_fix_vocab`은 교정이 반려되면 **입력을 그대로 돌려준다**(코드가 이름을
    바꾸면 '비슷한 이름'으로 엉뚱한 액션이 들어가므로, 고치는 건 모델에게 맡긴 설계다).
    그래서 통과 여부를 호출부가 다시 재야 한다 — 안 재면 교정 못 한 오염이 그대로 흘러
    R1 blocker로 게이트에 들어간다(Qodo).

    0건을 요구하지 않고 **늘지 않았는가**로 본다: 초안 자체가 교정 실패로 1건을 달고 있을
    수 있고(실측 2026-07-30 턴 16fdafff: 표기 교정이 1건 → 1건으로 반려됐다), 그때 0건을
    요구하면 커버리지가 오르는 회차까지 같이 버린다. `_fix_vocab`의 자체 반려 기준과 같은
    방향이다.
    """
    n_before = len(_unknown_actions(before, catalog))
    n_after = len(_unknown_actions(after, catalog))
    if n_after > n_before:
        return f"메뉴에 없는 액션 {n_before}건 → {n_after}건"
    return None


def _action_spec_block(flow: dict, catalog) -> str:
    """확정 흐름도에 쓰인 (package, action)의 파라미터 스펙만 모아 준다 — 채우기 단계 입력.

    검색이 필요 없다. 구조가 확정됐으니 **어떤 액션의 스펙이 필요한지 이미 안다.**
    """
    if catalog is None:
        return "(카탈로그 없음 — 문서 근거가 있는 값만 신중히 채울 것)"
    seen: list[tuple[str, str]] = []

    def walk(actions: list[dict]) -> None:
        for a in actions:
            pkg, act = a.get("package"), a.get("action")
            if pkg and act and (pkg, act) not in seen:
                seen.append((pkg, act))
            walk(a.get("children") or [])

    for step in flow.get("steps") or []:
        walk(step.get("actions") or [])

    blocks = []
    for pkg, act in seen:
        spec_dict = catalog.get_action_schema(pkg, act)
        blocks.append(
            _menu_block(pkg, act, spec_dict) if spec_dict is not None
            else f"- {pkg}/{act} «스펙 없음 — 카탈로그에 없는 액션이다. 값을 지어내지 마라»"
        )
    return "\n".join(blocks) or "(액션 없음)"


def compose_system_prompt(
    persona: str, analysis: dict, spec: dict, dossier: dict, extra_menu: str = ""
) -> str:
    """구조(outline) 단계의 시스템 프롬프트 — **고정된 것부터, 이번 턴에만 있는 것은 뒤로.**

    순서가 비용을 가른다. 공급자는 프롬프트의 **공통 접두**를 캐싱해 그 구간을 할인하는데,
    호출마다 달라지는 조각이 앞에 오면 그 뒤는 전부 캐시를 못 탄다. 한 턴 안에서 이 함수는
    구조·구조(보강)·구조(수리)로 최대 세 번 불리고, 세 번 모두 `기본 지침 + 부록 + 출력 계약`
    (고정)과 `분석 + 스펙 + 메뉴`(그 턴 내내 동일)를 공유한다.

    실측(2026-07-28): 흐름도 한 턴 $0.275 중 turn_generate가 $0.222(81%)이고, 그 입력
    221k 토큰의 절반이 같은 프롬프트 재전송이었다. 캐시 적중률은 34%였다.

    ⚠ **분기 요소는 반드시 마지막이다.** 앞으로 옮기면 조용히 비용이 오른다(품질은 그대로라
    아무도 눈치채지 못한다). 지금은 뒤에 붙는 것이 설계 관점(고정)과 `extra_menu`(보강 회차에만
    생김)뿐이라, extra_menu가 persona보다 앞에 오면 보강 회차가 캐시를 잃는다.
    tests/test_agent_v3.py가 이 순서를 지킨다.
    """
    from ..orchestrator.render import analysis_brief  # 지연 임포트 — 순환 방지

    background = (
        f"\n\n[배경 지식 (공식 문서 발췌)]\n{dossier['background']}"
        if dossier.get("background") else ""
    )
    return (
        f"{_BASE_PROMPT}\n\n{_ADDENDUM}\n\n{_OUTLINE_CONTRACT}\n\n"
        f"[업무 분석]\n{analysis_brief(analysis)}\n\n"
        f"[요구사항 스펙]\n{_render_spec_block(spec)}\n\n"
        f"[액션 후보 메뉴]\n{dossier['menu']}{background}\n\n"
        f"{persona}"
        f"{extra_menu}"
    )


async def _compose_candidate(
    spec: dict,
    dossier: dict,
    analysis: dict,
    document: str | None,
    sink: list[dict],
    sem: asyncio.Semaphore,
    ctx,
) -> dict | None:
    """흐름도를 만든다 — 구조 → (능력 요청) → 구조 게이트 → 값의 4단.

    한 호출에서 **고르기·순서·값**을 동시에 정하면 출력이 깊고 길어져 뒤쪽에서 힘이 빠진다.
    실측(2026-07-29) 4턴 연속으로 예외 처리 골격만 남고 안이 빈 흐름도가 나왔고, 한 턴은
    Try·Catch·Finally를 다 갖췄는데 업무 액션이 0개였다. 단계를 쪼개면 각 출력이 작아진다.

    툴 바인딩은 없앴다 — escape hatch를 `needs`(능력 요청)로 대체했다. 부수 효과로
    JSON mode × strict 툴 충돌(2026-07-28 턴 전체 실패)도 원인째 사라진다.

    실패 시 None.
    """
    from ..orchestrator.spec import fenced_doc_block

    cid = _CANDIDATE_ID
    persona = (_PROMPT_DIR / _STANCE_FILE).read_text(encoding="utf-8")
    doc_block = fenced_doc_block(document)
    json_mode = bool(config.COMPOSE_JSON_MODE)
    # 추론이 한 번 예산을 태우면 **이 흐름도를 만드는 동안 다시 시도하지 않는다.**
    # 실측(2026-07-29): 구조 단계가 상한 16,000을 추론으로 다 쓰고 실패했는데, `_ask`마다
    # 강도가 초기화되는 바람에 보강 회차가 다시 켜 14,538토큰(96초, $0.069)을 더 태웠다.
    # 같은 프롬프트·같은 모델이라 한 번 안 되면 그 턴에는 안 된다.
    reasoning_off = False

    async def _ask(
        system: str, user: str, stage: str, *,
        reasoning: str | None = None, parse=_parse_flow,
        truncated_retry: str = _TRUNCATED_RETRY,
    ) -> dict | None:
        """한 단계를 돌려 JSON 하나를 받는다. 파싱 실패는 1회 재시도.

        `reasoning`은 이 호출에만 걸리는 추론 강도다 — `_make_llm`은 모든 단계가 공유하므로
        여기로 흘려야 **구조 단계에만** 켤 수 있다(값 채우기는 파라미터만 채우는 단순 작업이라
        추론이 필요 없고, 조각별 병렬이라 켜면 비용이 가장 크게 붙는다).

        `parse`는 출력 계약이 단계마다 다르기 때문에 있다 — 구조 단계는 흐름도(`steps`),
        값 단계는 노드별 값 패치(`nodes`)를 낸다. 절단·형식 오류 재시도는 둘이 공유한다.
        """
        nonlocal json_mode, reasoning_off
        effort = None if reasoning_off else reasoning
        llm = _make_llm(json_mode=json_mode, reasoning=effort)
        msgs: list = [SystemMessage(content=system), HumanMessage(content=user)]
        usage_config = {"callbacks": [UsageCallbackHandler(purpose="turn_generate")]}
        retried = False
        for _ in range(_STAGE_MAX_TURNS):
            try:
                async with sem:
                    ai = await llm.ainvoke(msgs, config=usage_config)
            except Exception as e:  # noqa: BLE001
                # 설정 비호환은 턴 전체를 실패시킨다 — 옵션을 하나씩 내려놓으며 다시 간다.
                # **추론을 먼저 뗀다**: JSON mode는 다른 모든 단계가 이미 쓰고 있어 검증된
                # 설정이고, 추론은 이번에 새로 켠 것이라 비호환일 가능성이 훨씬 높다.
                # 순서를 반대로 두면 원인이 아닌 쪽을 끄고 같은 이유로 또 실패한다.
                if effort:
                    logger.warning("후보 %s %s 추론(%s) 실패 — 끄고 재시도: %s",
                                   cid, stage, effort, e)
                    # 실패한 호출은 예외 경로라 사용량 콜백이 안 돌아 llm_usage에 안 남는다 —
                    # 태운 토큰이 회계에서 통째로 빠지므로 관측 이벤트로라도 남긴다.
                    emit({"event": "stage", "stage": "recommending",
                          "message": f"{stage} 단계 추론({effort})이 실패해 껐습니다",
                          "data": {"candidate": cid, "stage": stage, "effort": effort,
                                   "max_tokens": config.COMPOSE_MAX_TOKENS,
                                   "error": str(e)[:300]}})
                    reasoning_off = True   # 이 흐름도를 만드는 동안 다시 켜지 않는다
                    effort = None
                    llm = _make_llm(json_mode=json_mode, reasoning=None)
                    continue
                if json_mode:
                    logger.warning("후보 %s %s JSON mode 실패 — 끄고 재시도: %s", cid, stage, e)
                    json_mode = False
                    llm = _make_llm(json_mode=False)
                    continue
                logger.warning("후보 %s %s 호출 실패: %s", cid, stage, e)
                return None
            try:
                return parse(ai.content)
            except ValueError as e:
                truncated = _looks_truncated(ai)
                kind = "길이 한도 절단" if truncated else "형식 오류"
                excerpt = _parse_excerpt(getattr(e, "raw", ""), getattr(e, "pos", None))
                if retried:
                    logger.warning("후보 %s %s 파싱 재실패 — 탈락(%s): %s\n  실패 지점: %s",
                                   cid, stage, kind, e, excerpt)
                    return None
                logger.info("후보 %s %s 첫 파싱 실패(%s): %s\n  실패 지점: %s",
                            cid, stage, kind, e, excerpt)
                retried = True
                msgs.append(ai)
                if truncated:
                    msgs.pop()  # 잘린 거대 출력을 남기면 같은 길이를 또 만든다
                    msgs.append(HumanMessage(content=truncated_retry))
                else:
                    msgs.append(HumanMessage(content=(
                        f"출력이 올바른 JSON이 아니다({e}). "
                        "코드펜스·설명 없이 JSON 객체 하나만 다시 출력하라."
                    )))
        logger.warning("후보 %s %s 예산 소진 — 탈락", cid, stage)
        return None

    # ── 1단: 구조 ──────────────────────────────────────────────────────────
    #
    # 추론은 **이 단계에만** 켠다. 여기가 흐름도의 배치를 정하는 유일한 자리이고(이후 단계는
    # 고치거나 채울 뿐이다), 반복해 틀린 것들 — Try 경계, 세션 여닫는 순서, step 배치 — 이
    # 전부 지식이 아니라 계획의 문제였다. 값 채우기는 파라미터만 채우는 단순 작업인 데다
    # 조각별 병렬이라 켜면 비용이 가장 크게 붙는다.
    reasoning = config.COMPOSE_REASONING or None
    outline_user = (
        "위 요구사항 스펙을 달성하는 A360 흐름도의 **구조**를 당신의 설계 관점대로 설계하라. "
        "파라미터는 채우지 말고, 필요한 액션이 메뉴에 없으면 needs로 요청하라. JSON 하나만."
        f"{doc_block}"
    )
    outline = await _ask(compose_system_prompt(persona, analysis, spec, dossier),
                         outline_user, "구조", reasoning=reasoning)
    if outline is None:
        return None

    # ── 2단: 능력 요청이 있으면 검색해 한 번 더 ────────────────────────────
    needs = [n for n in (outline.get("needs") or []) if isinstance(n, dict)]
    if needs:
        queries = [str(n.get("query") or n.get("what")) for n in needs[:_MAX_NEEDS]]
        extra = await asyncio.to_thread(_capability_menu, needs, sink, ctx)
        emit({"event": "stage", "stage": "searching",
              "message": f"흐름도가 요청한 액션 {len(queries)}건 추가 조사",
              "data": {"candidate": cid, "queries": queries, "found": bool(extra)}})
        if extra:
            # 보강은 같은 '구조' 작업의 재생성이라 추론도 같이 건다 — 여기서만 끄면 첫 초안보다
            # 못한 구조가 나와 회귀 가드에 걸리고, 능력 요청으로 찾아온 액션이 버려진다.
            retry = await _ask(
                compose_system_prompt(persona, analysis, spec, dossier, extra_menu=extra),
                outline_user, "구조(보강)", reasoning=reasoning)
            # 보강 회차는 메뉴가 **더 많은** 상태의 재생성이다 — 액션이 줄었다면
            # 정보가 늘었는데 포기한 것이므로 첫 구조를 지킨다.
            if retry is not None and not (lost := _repair_regression(outline, retry)):
                outline = retry
            elif retry is not None:
                logger.warning("후보 %s 구조 보강 반려 — %s. 첫 구조로 진행", cid, lost)

    outline.pop("needs", None)  # 이후 단계에는 요청 흔적을 넘기지 않는다
    _emit_plan(cid, outline.pop("plan", None) or {}, outline)  # 배치 계획은 관측에만 남긴다

    # ── 2.5단: 닫힌 어휘 검증 — 게이트 앞에서 표기 오염을 걷어낸다 ──────────
    #
    # JSON mode도 Pydantic도 이걸 못 잡는다. `action: "Send/보내기"`는 완벽하게 유효한
    # JSON이고 스키마상으로도 그냥 str이다 — 강제되는 건 **문법**이지 **어휘**가 아니다.
    # 그래서 검수 R1이 사후에 blocker로 잡고, 그때는 이미 늦다: 실측(2026-07-29)에서 오염된
    # 이름 4개가 게이트·refine 5라운드를 태우고 업무 액션을 통째로 지운 채 끝났다
    # (신뢰도 0.05). 여기서 한 번 되물으면 그 연쇄가 시작조차 하지 않는다.
    #
    # **코드는 탐지만 하고 고치는 건 모델이다.** 이름을 코드가 바꾸기 시작하면 '비슷한 이름'
    # 으로 엉뚱한 액션이 조용히 들어가는 길이 열린다 — 지금 R1이 잡아주는 것을 잃는 셈이다.
    # ⚠ **구조를 새로 만드는 자리가 생기면 반드시 이 검증을 다시 통과시켜라.** 실측
    # (2026-07-30): 커버리지 보완 회차를 게이트 안에 넣었더니 그 재생성본이 이 단계를
    # 건너뛰어, `package="Recorder/Click"` · `action="범용 레코더로 캡처한 객체에 대해
    # 수행한"` 꼴의 오염이 R1 blocker 6건으로 게이트에 그대로 들어갔다(가중 910). 그래서
    # 함수로 빼 두 자리가 같은 것을 쓴다.
    async def _fix_vocab(flow: dict) -> dict:
        unknown = await asyncio.to_thread(_unknown_actions, flow, getattr(ctx, "catalog", None))
        if not unknown:
            return flow
        emit({"event": "stage", "stage": "verifying",
              "message": f"메뉴에 없는 액션 표기 {len(unknown)}건 — 표기 교정 요청",
              "data": {"candidate": cid,
                       "unknown": [f"{p}/{a}" for _l, p, a in unknown[:8]]}})
        fixed = await _ask(
            compose_system_prompt(persona, analysis, spec, dossier),
            _vocab_retry_user(flow, unknown, ctx.catalog) + doc_block,
            "구조(표기)")
        left = await asyncio.to_thread(_unknown_actions, fixed or {}, getattr(ctx, "catalog", None))
        if fixed is None or len(left) >= len(unknown) or _repair_regression(flow, fixed):
            logger.warning("후보 %s 표기 교정 반려 — %d건 → %d건. 원래 구조로 진행",
                           cid, len(unknown), len(left))
            return flow
        fixed.pop("needs", None)
        fixed.pop("plan", None)
        emit({"event": "stage", "stage": "verifying",
              "message": f"액션 표기를 교정했습니다 ({len(unknown)}건 → {len(left)}건)",
              "data": {"candidate": cid, "before": len(unknown), "after": len(left)}})
        return fixed

    outline = await _fix_vocab(outline)

    # ── 3단: 구조 게이트 — 값을 채우기 전에 한 번 거른다 ───────────────────
    issues, coverage_findings = await asyncio.to_thread(_gate_issues, outline, spec, ctx)
    _emit_gate(cid, issues, outline)

    # 커버리지 미달은 **수리가 아니라 조사로** 푼다 (_coverage_gaps 주석의 근거 참고).
    # 한 번만 돈다 — 못 찾았으면 두 번째도 못 찾고, 찾았는데 모델이 안 넣었으면 세 번째도 안 넣는다.
    gaps = _coverage_gaps(coverage_findings, spec) if config.COMPOSE_COVERAGE_RETRY else []
    if gaps:
        extra = await asyncio.to_thread(_capability_menu, gaps, sink, ctx)
        emit({"event": "stage", "stage": "searching",
              "message": f"빠뜨린 필수 요구 {len(gaps)}건을 조사로 보완",
              "data": {"candidate": cid, "req_ids": [g["req_id"] for g in gaps],
                       "found": bool(extra)}})
        if extra:
            retry = await _ask(
                compose_system_prompt(persona, analysis, spec, dossier, extra_menu=extra),
                _coverage_retry_user(outline_user, gaps), "구조(커버리지)", reasoning=reasoning)
            lost = _repair_regression(outline, retry) if retry is not None else "출력 실패"
            if retry is not None and not lost:
                retry.pop("needs", None)
                retry.pop("plan", None)
                # 재생성본도 닫힌 어휘를 통과해야 한다 — 안 거치면 오염이 R1 blocker로
                # 게이트에 들어간다(실측 2026-07-30: 가중 910, 수리 4라운드를 태우고 300).
                candidate = await _fix_vocab(retry)
                # 교정이 반려됐을 수 있다 — `_fix_vocab`은 그때 입력을 그대로 돌려준다.
                # 이 회차는 **선택적 개선**이라 오염이 늘었으면 직전 구조가 낫다.
                dirty = await asyncio.to_thread(
                    _vocab_regression, outline, candidate, getattr(ctx, "catalog", None))
                if dirty:
                    logger.warning("후보 %s 커버리지 보완 반려 — %s. 직전 구조로 진행", cid, dirty)
                else:
                    outline = candidate
                    # 다시 잰다 — 안 재고 옛 findings로 수리를 부르면 이미 넣은 것을 또 넣으라고 시킨다.
                    issues, coverage_findings = await asyncio.to_thread(
                        _gate_issues, outline, spec, ctx)
                    _emit_gate(cid, issues, outline)
            else:
                logger.warning("후보 %s 커버리지 보완 반려 — %s. 직전 구조로 진행", cid, lost)

    if issues:
        outline = await asyncio.to_thread(_gate_repair, outline, coverage_findings, cid, ctx)

    # ── 4단: 값 — 노드별 값 패치 ────────────────────────────────────────────
    #
    # 시스템 프롬프트는 조각이 몇 개든 **똑같이** 만든다(스펙도 흐름도 전체 기준). 조각마다
    # 자기 몫만 실으면 입력이 조금 줄지만 공통 접두가 깨져 두 번째 호출부터 캐시를 못 탄다 —
    # 캐시 입력은 정가의 1/10이라, 통째로 공유하는 쪽이 훨씬 싸다.
    fill_system = (
        f"{_FILL_PROMPT}\n\n"
        f"[요구사항 스펙]\n{_render_spec_block(spec)}\n\n"
        f"[액션별 파라미터 스펙]\n{_action_spec_block(outline, getattr(ctx, 'catalog', None))}"
    )
    # annotate_ids도 try 안이다 — 순회 중간에 터지면 일부 노드에 id가 붙은 채로 남고,
    # finally의 strip_ids가 그걸 걷어내야 스키마로 새 나가지 않는다.
    try:
        _edit_ops.annotate_ids(outline)
        chunks = _fill_chunks(outline, config.COMPOSE_FILL_CHUNK)
        if not chunks:
            return outline
        total = _count_actions(outline)
        if len(chunks) > 1:
            emit({"event": "stage", "stage": "recommending",
                  "message": f"값 채우기를 {len(chunks)}조각으로 나눠 진행합니다",
                  "data": {"candidate": cid, "actions": total,
                           "chunks": [len(c) for c in chunks],
                           "cap": config.COMPOSE_FILL_CHUNK}})
        context_block = _fill_context(outline)

        def _fill_call(i: int, ids: list[str]):
            return _ask(fill_system,
                        _fill_user(context_block, ids, doc_block),
                        f"값({i + 1}/{len(chunks)})",
                        parse=_parse_patch, truncated_retry=_FILL_TRUNCATED_RETRY)

        # 첫 조각을 **먼저 혼자** 보낸다 — 조각들이 시스템 프롬프트를 공유하는데 동시에 쏘면
        # 그 접두가 아직 캐시에 없어 전부 정가로 낸다. 실측(2026-07-29): 3조각 중 2·3번이
        # 5,850토큰 중 2,816만 캐시를 탔고(앞선 단계와 겹치는 부분뿐), 값 단계 비용이 단일
        # 호출 $0.0151 대비 $0.0208로 올랐다. 하나를 선행시키면 나머지가 접두를 1/10 가격으로
        # 받는다. 대가는 첫 조각의 지연(실측 2~3초)이고, 조각이 하나면 아무 차이가 없다.
        first = await _fill_call(0, chunks[0])
        rest = await asyncio.gather(*(
            _fill_call(i, ids) for i, ids in enumerate(chunks) if i
        )) if len(chunks) > 1 else []
        parts = [first, *rest]

        patched = unknown = failed = added = strayed = 0
        notes: list[str] = []
        for ids, part in zip(chunks, parts):
            if part is None:
                failed += 1
                continue
            a, u, o = _apply_patches(outline, part.get("nodes") or [], allowed=set(ids))
            patched += a
            unknown += u
            strayed += o
            added += _merge_variables(outline, part.get("variables_add") or [])
            note = (part.get("notes") or "").strip()
            if note:
                notes.append(note)
        if notes:
            outline["notes"] = " · ".join(filter(None, [outline.get("notes"), *notes]))

        # 이 단계가 실제로 무엇을 놓쳤는지 재는 숫자 — **패치를 하나도 못 받은 노드 수**다.
        # 필드가 비었는지로 세면 안 된다: 파라미터가 원래 없는 컨테이너(Try·Finally·Step)가
        # 섞여 들어오고, produces만 받은 노드를 '안 채워졌다'로 잘못 센다. 조각의 allowed는
        # 흐름도를 겹치지 않게 나눈 것이라 patched는 노드당 최대 1이고, total - patched가
        # 정확히 '아무도 안 건드린 노드' 수다.
        # (실측 2026-07-29: 한 호출로 19개를 맡겼을 때 Catch·Finally 4개가 통째로 비었는데,
        #  그때는 이 숫자가 없어서 검수 R3가 5건 뜬 뒤에야 알았다.)
        unpatched = total - patched
        if failed or unknown or strayed or unpatched:
            emit({"event": "stage", "stage": "recommending",
                  "message": f"값 채우기 결과 — 액션 {total}개 중 {patched}개 반영",
                  "data": {"candidate": cid, "actions": total, "patched": patched,
                           "unknown_ids": unknown, "out_of_scope_ids": strayed,
                           "unpatched": unpatched, "failed_chunks": failed,
                           "chunks": len(chunks), "variables_added": added}})
        if failed == len(chunks):
            logger.warning("후보 %s 값 채우기 전량 실패 — 구조만으로 진행", cid)
        return outline   # 값이 비어도 구조는 살린다(R3가 질문 카드로 승격한다)
    finally:
        _edit_ops.strip_ids(outline)   # 전이 id는 스키마에 남기지 않는다


def _fill_context(outline: dict) -> str:
    """값 단계가 맥락으로 받는 것 — id가 붙은 흐름도 아웃라인 + 선언된 변수.

    흐름도 JSON을 통째로 넣지 않는다. 값 단계가 볼 것은 **어떤 액션이 어디 있는지**와
    **어떤 변수 이름을 써야 하는지**뿐이고, 아웃라인이 그걸 훨씬 짧게 담는다. 그리고
    조각들이 서로를 못 보는 만큼 **공통으로 볼 것**은 넉넉히 줘야 한다 — 세션을 여는
    액션이 다른 조각에 있어도 여기서 그 패키지를 확인할 수 있어야 이름이 맞는다.
    """
    variables = [v for v in (outline.get("variables") or []) if isinstance(v, dict)]
    decl = "\n".join(
        f"- {v.get('name')} ({v.get('type') or '?'}) {v.get('description') or ''}".rstrip()
        for v in variables
    ) or "(선언된 변수 없음)"
    return (
        f"[흐름도 전체]\n{_edit_ops.render_outline(outline)}\n\n"
        f"[선언된 변수]\n{decl}"
    )


def _fill_user(context_block: str, ids: list[str], doc_block: str) -> str:
    """조각 하나의 사용자 메시지 — 맥락은 전부, 낼 것은 지정된 id만."""
    return (
        "아래 흐름도에서 **[이번에 채울 노드]에 적힌 id만** 값을 내라. "
        "지정되지 않은 노드는 다른 호출이 맡으므로 출력에 넣지 마라.\n\n"
        f"{context_block}\n\n"
        f"[이번에 채울 노드]\n{' · '.join(ids)}"
        f"{doc_block}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# verify 스택 — 후보별 L0/L1 → L2 → L3
# ─────────────────────────────────────────────────────────────────────────────

async def _verify_candidate(cid: str, persona_name: str, flow: dict, spec: dict, sem: asyncio.Semaphore, ctx):
    """후보 하나에 검증 스택을 돌려 CandidateReport를 만든다. L2/L3 실패는 신호 결측일 뿐."""
    from ..orchestrator.harness import collect_violations, from_violations_dicts
    from ..orchestrator.judge import CandidateReport
    from ..verify import findings as F
    from ..verify.semantic import run_semantic_check
    from ..verify.simulate import run_simulation

    violations = collect_violations(flow, ctx.catalog)
    fnd, _cards = from_violations_dicts(violations)

    coverage = None
    try:
        async with sem:
            coverage = await asyncio.to_thread(run_semantic_check, spec, flow)
    except Exception as e:  # noqa: BLE001
        logger.warning("후보 %s L2 채점 실패 — 커버리지 결측: %s", cid, e)
    sim = None
    try:
        async with sem:
            sim = await asyncio.to_thread(run_simulation, spec, flow)
    except Exception as e:  # noqa: BLE001
        logger.warning("후보 %s L3 시뮬레이션 실패 — 통과율 결측: %s", cid, e)

    if coverage is not None:
        fnd += F.from_coverage(coverage)
    if sim is not None:
        fnd += F.from_simulation(sim)

    cov_by_step: dict[str, str] = {}
    cov_by_req: dict[str, str] = {}
    if coverage is not None:
        # evidence 노드 id는 국소 좌표라, 단계 상태는 req 상태를 step에 근사 배정하지 않고
        # 전 단계 공통(최저 status)로 쓰기보다 req 단위로만 유지한다. 단계 매핑은 evidence
        # 기반 정밀화가 후속 — confidence의 semantic 축은 req 상태의 흐름도 전역 요약을 쓴다.
        cov_by_req = {e.req_id: e.status for e in coverage.entries}

    return CandidateReport(
        candidate_id=cid,
        persona=persona_name,
        flow=flow,
        violations=violations,
        findings=fnd,
        must_coverage=coverage.must_coverage if coverage is not None else None,
        gate_failures=[e.req_id for e in coverage.hard_gate_failures()] if coverage is not None else [],
        sim_pass_rate=sim.pass_rate if sim is not None else None,
        coverage_by_step=cov_by_step,
        coverage_by_req=cov_by_req,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 파이프라인 본체
# ─────────────────────────────────────────────────────────────────────────────

async def generate_flow(analysis: Any, document: str | None, spec: dict, ctx=None) -> dict:
    """spec → research → compose → verify → judge → refine → finalize.

    반환: {"recommendation": Recommendation dict, "violations": list[dict]}.
    구조 단계 실패 시 RuntimeError (호출부가 error 이벤트로 처리).
    """
    from ..orchestrator import cards as cards_mod
    from ..orchestrator.harness import (
        attach_confidence,
        confidence_breakdown,
        from_violations_dicts,
        refine_flow,
    )
    from ..catalog_context import a360_context
    from ..orchestrator.judge import judge_candidates
    from ..verify import findings as F
    from ..verify.semantic import run_semantic_check
    from .research import build_dossier

    # ctx 미지정은 a360 기본 — 기존 호출부(테스트 포함) 호환 (RPA-285).
    ctx = ctx or a360_context()
    analysis = _to_dict(analysis)
    sink: list[dict] = []
    sem = asyncio.Semaphore(config.MAX_LLM_CONCURRENCY)

    # [2] research — 조사 선행·공유
    dossier = await build_dossier(spec, sink, ctx)

    # [3] compose — 구조 → (능력 요청) → 구조 게이트 → 값
    cand_status = [{"id": _CANDIDATE_ID, "persona": _CANDIDATE_LABEL,
                    "status": "composing", "steps": 0, "actions": 0}]
    emit_candidates_frame(cand_status, "흐름도 설계 중")

    flow = await _compose_candidate(spec, dossier, analysis, document, sink, sem, ctx)
    if flow is None:
        cand_status[0]["status"] = "failed"
        emit_candidates_frame(cand_status, "흐름도 생성 실패")
        raise RuntimeError("흐름도를 생성하지 못했습니다")
    cand_status[0]["status"] = "verifying"
    cand_status[0]["steps"], cand_status[0]["actions"] = _flow_counts(flow)
    emit_candidates_frame(cand_status, "흐름도 초안 완성 — 검증 스택 통과 중")

    # [4] verify 스택 — L0/L1 정적 → L2 커버리지 → L3 시뮬레이션
    report = await _verify_candidate(_CANDIDATE_ID, _CANDIDATE_LABEL, flow, spec, sem, ctx)
    cand_status[0]["status"] = "done"
    emit_candidates_frame(cand_status, "검증 완료 — 채점 중")

    # [5] judge — 채점 결과와 refine 지시
    verdict = await asyncio.to_thread(judge_candidates, spec, [report])
    winner = verdict["winner"]
    emit_verdict_frame(verdict["verdict"], "설계 채점 완료")

    # 승자 트리 점진 노출 — '자라나는 흐름도' 경험은 승자 확정 이후부터 (v2 계승).
    steps = winner.flow.get("steps") or []
    for i in range(len(steps)):
        emit_flow_frame({**winner.flow, "steps": steps[: i + 1]}, None,
                        f"선택된 설계 구성 {i + 1}/{len(steps)}")
        await asyncio.sleep(_REVEAL_DELAY)
    emit_flow_frame(winner.flow, winner.violations, "선택된 초안 · 다듬기 시작")

    # [6] refine — 정적 위반 + L2/L3 발견 + 이식 지시를 surgeon 패치로
    extra = [f for f in winner.findings if f.layer in ("L2", "L3")] + verdict["transplant_findings"]
    refined = await asyncio.to_thread(
        refine_flow, winner.flow, ctx.catalog, extra_findings=extra, purpose="turn_generate"
    )
    flow, violations = refined["flow"], refined["violations"]

    # [7] finalize — 근거·confidence·질문 카드·flow_confidence
    flow = _attach_sources(flow, sink)

    coverage = None
    sim_rate = winner.sim_pass_rate
    if refined["repaired"]:  # 흐름이 바뀌었을 때만 L2/L3 재채점 (설계: 심판 시점+최종 시점 2회)
        try:
            async with sem:
                coverage = await asyncio.to_thread(run_semantic_check, spec, flow)
        except Exception as e:  # noqa: BLE001
            logger.warning("최종 L2 재채점 실패 — 승자 채점 재사용: %s", e)
        # L3도 재실행 — 교정으로 구조가 바뀌었는데 교정 전 통과율을 쓰면 신뢰도가 낡는다.
        from ..verify.simulate import run_simulation
        try:
            async with sem:
                sim_rate = (await asyncio.to_thread(run_simulation, spec, flow)).pass_rate
        except Exception as e:  # noqa: BLE001
            logger.warning("최종 L3 재실행 실패 — 승자 통과율 재사용: %s", e)
    must_cov = coverage.must_coverage if coverage is not None else winner.must_coverage

    findings_final, r3_cards = from_violations_dicts(violations)
    if coverage is not None:
        findings_final += F.from_coverage(coverage)

    # 질문 카드 — R3 + unknowns + assumptions (결정론) → 문구 다듬기 (no-fail LLM 1회)
    cards = cards_mod.build_cards(flow, spec, r3_cards, ctx.catalog)
    if cards:
        async with sem:
            cards = await asyncio.to_thread(cards_mod.polish_card_wording, cards)

    # confidence — 후보 간 합의(agreement) 신호는 후보가 하나뿐이라 없다.
    # 액션별 confidence는 검색 점수·근거만으로 매겨진다.
    attach_confidence(flow, sink, violations, agreement=None)

    blocking_cards = sum(1 for c in cards if c.get("blocking") and not c.get("resolved"))
    flow["needs_input"] = cards
    flow["spec"] = spec
    # 결과값과 항별 분해를 **한 함수에서** 받는다 — 관측용으로 산식을 베껴 쓰면 산식을
    # 고칠 때 한쪽만 고쳐져 이벤트가 조용히 거짓말을 한다(Qodo).
    breakdown = confidence_breakdown(
        must_coverage=must_cov,
        findings=findings_final,
        sim_pass_rate=sim_rate,
        blocking_cards=blocking_cards,
    )
    flow["flow_confidence"] = breakdown["confidence"]

    n_blockers = breakdown["blockers"]
    n_majors = breakdown["majors"]
    n_warnings = sum(1 for f in findings_final if f.severity == "warning")

    emit_scorecard_frame({
        "must_coverage": must_cov,
        "blockers": n_blockers,
        "warnings": n_warnings,
        "sim_pass_rate": sim_rate,
        "cards": len(cards),
        "flow_confidence": flow["flow_confidence"],
    }, "최종 검증 요약")

    # 신뢰도를 **분해해서** 관측에 남긴다. 위 scorecard는 partial 이벤트라 프론트로만 가고
    # turn_events에 저장되지 않는다 — 그래서 사후에는 결과값(flow_confidence) 하나만 남는다.
    #
    # 실측(2026-07-30): 검수 위반 0건짜리 흐름도가 0.15를 받았는데 **어느 항이 눌렀는지
    # 알 수 없었다.** 결과만 보이니 신뢰도를 개선 지표로 쓸 수가 없다 — "0.15를 올리려면
    # 무엇을 고치나"에 답이 안 나온다. 그래서 각 항이 곱한 값까지 남긴다(`factors` — 작은
    # 값이 병목이다). 카드는 감점하지 않으므로 항이 없고 개수만 싣는다(입력 대기 ≠ 결함).
    emit({
        "event": "stage", "stage": "verifying",
        "message": f"신뢰도 {flow['flow_confidence']}",
        "data": {
            "candidate": winner.candidate_id,
            # 원시 입력
            "must_coverage": must_cov, "sim_pass_rate": sim_rate,
            "warnings": n_warnings, "cards": len(cards),
            # 결과값 · 항별 분해 · clamp 여부 — 산식 소유자가 낸 것을 그대로 싣는다
            "flow_confidence": breakdown["confidence"],
            **{k: v for k, v in breakdown.items() if k != "confidence"},
        },
    })

    flow = _coerce_flow(flow)
    try:
        rec = Recommendation.model_validate(flow)
    except ValidationError as e:
        # 마지막 관문에서 전부 버리지 않는다 — 교정 전 승자 초안(직전 유효 후보)으로 강등 시도.
        logger.warning("최종 흐름도 정규화 실패 — 승자 초안으로 강등 시도: %s", e)
        try:
            rec = Recommendation.model_validate(_coerce_flow(copy.deepcopy(winner.flow)))
        except ValidationError as e2:
            logger.warning("승자 초안도 정규화 실패, 빈 추천안: %s", e2)
            rec = Recommendation(steps=[])
    rec_dict = rec.model_dump()
    emit_flow_frame(rec_dict, violations, "완료")
    return {"recommendation": rec_dict, "violations": violations}


# ─────────────────────────────────────────────────────────────────────────────
# 공개 진입점 (독립 실행용 — 오케스트레이터 밖에서도 스트리밍)
# ─────────────────────────────────────────────────────────────────────────────

async def recommend(
    analysis: Any, constraints: list[str] | None = None, parsed_doc: dict | None = None
) -> AsyncIterator[ProgressEvent]:
    """AnalysisResult → A360 추천안 스트림 (INTERFACES §4 ② — v3 품질 루프).

    단일 노드 StateGraph로 감싸 emit()의 스트림 컨텍스트를 만든다 — 파이프라인 자체는
    generate_flow와 동일하다. constraints는 spec의 assumptions로 편입된다.
    """
    if not config.OPENAI_API_KEY:
        yield ProgressEvent(event="error", message="OPENAI_API_KEY 환경변수가 필요합니다")
        return

    from langgraph.graph import END, START, StateGraph
    from typing_extensions import TypedDict

    from ..orchestrator.spec import build_flow_spec

    document: str | None = None
    if parsed_doc:
        from ..analysis import _format_document, _has_text

        if _has_text(parsed_doc):
            document = _format_document(parsed_doc)

    class _S(TypedDict, total=False):
        result: dict

    async def _run(_state: _S) -> dict:
        # 동기 LLM 호출 — 이벤트 루프 블로킹 방지 (파이프라인의 다른 LLM 호출과 동일 정책).
        spec = await asyncio.to_thread(
            build_flow_spec, {"analysis": _to_dict(analysis), "message": ""}, document
        )
        if constraints:
            spec.setdefault("assumptions", []).extend(constraints)
        return {"result": await generate_flow(analysis, document, spec)}

    g = StateGraph(_S)
    g.add_node("run", _run)
    g.add_edge(START, "run")
    g.add_edge("run", END)
    graph = g.compile()

    final_state: dict = {}
    try:
        async for mode, chunk in graph.astream(
            {}, stream_mode=["custom", "values"], config={"recursion_limit": 10},
        ):
            if mode == "custom":
                yield ProgressEvent(**chunk)
            elif mode == "values":
                final_state = chunk
    except RuntimeError as e:
        yield ProgressEvent(event="error", message=str(e))
        return
    except Exception:  # noqa: BLE001 — 예기치 못한 실패도 스트림을 죽이지 않는다
        logger.exception("recommend 실패")
        yield ProgressEvent(event="error", message="추천 생성 중 오류가 발생했습니다")
        return

    result = final_state.get("result") or {}
    yield ProgressEvent(event="done", data={"recommendation": result.get("recommendation")})
