"""recommend v4 — 품질 루프 흐름도 생성 (설계 §2).

v2의 "compose ReAct 1후보 → 정적 검수 → 국소 교정"을 다음으로 대체한다:

    spec(FlowSpec)                          … 채점 기준 (orchestrator spec_builder가 선행)
      → research (이중 질의 Dossier)         … 조사 선행·공유 — 후보들이 같은 근거에서 경쟁
      → compose ×N (A360 전문가 페르소나)     … 모범 사례 / 운영 안정성 / 문서 충실, 병렬
      → verify 스택 (후보별)                 … L0/L1 정적 → L2 시맨틱 → L3 시뮬레이션
      → judge (루브릭·하드 게이트)            … 승자 + 패자 장점 이식 지시
      → refine (surgeon EditOps 패치 루프)    … 회귀 가드, ≤3라운드
      → finalize                             … sources·confidence 합성·질문 카드·flow_confidence

구현은 내부 StateGraph 없이 순수 async 파이프라인이다 — emit()은 부모(오케스트레이터)
그래프의 스트림 컨텍스트를 그대로 탄다. 후보 하나의 실패는 N 강등일 뿐 턴 실패가 아니다
(부분 실패 격리). 전 후보 실패만 RuntimeError로 끊는다.
"""

import asyncio
import copy
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import ValidationError

from app.core.llm import UsageCallbackHandler
from app.schemas import ProgressEvent, Recommendation

from .. import config
from .stream import (
    emit,
    emit_candidates_frame,
    emit_draft_frame,
    emit_flow_frame,
    emit_refine_frame,
    emit_scorecard_frame,
    emit_verdict_frame,
)

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
_BASE_PROMPT = (_PROMPT_DIR / "compose_agent.md").read_text(encoding="utf-8")
_ADDENDUM = (_PROMPT_DIR / "compose_v4_addendum.md").read_text(encoding="utf-8")
_PERSONAS: list[tuple[str, str, str, bool]] = [
    # (candidate_id, 표시명, 프롬프트 파일, 문서 필요 여부)
    ("A", "모범 사례 전문가", "persona_best_practice.md", False),
    ("B", "운영 안정성 전문가", "persona_ops.md", False),
    ("C", "문서 충실 재현", "persona_doc_faithful.md", True),
]

# 초안 흐름도를 단계별로 '드러내는' 프레임 사이 지연(초) — v2와 동일한 인지적 페이싱.
_REVEAL_DELAY = 0.18
# 후보별 compose 예산: escape hatch 툴콜 ≤2회 + 최초 출력 1회 + 파싱 재출력 ≤2회 = 5.
_COMPOSE_MAX_TURNS = 5
_ESCAPE_HATCH_ROUNDS = 2
_COMPOSE_PARSE_RETRIES = 2
# 출력 턴에 거는 JSON mode. ⚠ OpenAI는 json_object 모드에서 메시지에 'json' 문자열을 요구한다 —
# 사용자 메시지의 "Recommendation JSON"이 그 조건을 채우고 있다(프롬프트를 손대면 400).
_JSON_RESPONSE_FORMAT = {"type": "json_object"}
# 툴 바인딩 + JSON mode 병용은 **미확인 조합**이라 켜지 않는다. 잘못되면 모든 compose 첫
# 호출이 400이고, 툴이 억제되면 표기 환각이 늘어 R1 → 사전 검증 폐기로 교정 라운드를 태운다.
_JSON_MODE_WITH_TOOLS = False
# compose 전용 purpose. "turn_generate"는 spec 빌더·심판·surgeon(라운드당 1회, 최대 8회)이
# 함께 쓴다 — surgeon이 표본을 지배해 "compose 실패가 후보 부피와 함께 가는가"가 희석된다.
_COMPOSE_PURPOSE = "turn_generate_compose"
# recommend 검색은 액션 후보 메뉴용 — 문서 페이지·패키지 개요 오염을 막는다.
SEARCH_SOURCE_TYPES = ["action_schema", "bot_example"]

# 에이전트가 흔히 슬립하는 enum 필드의 허용값 — 벗어나면 안전값으로 강등한다.
_VALID_VALUE_SOURCE = {"schema_default", "llm", "user"}
_VALID_DIRECTION = {"input", "output", "local"}

# 2상 정밀화 예산 (설계 §6.3 "실패·타임아웃: 잠금 해제 + 초안 유지 + 사유 안내").
#
# ⚠️ 이 숫자들은 백엔드의 턴 상한(`app/api/sessions.py` TURN_MAX_DURATION_SEC, 기본 900초)
#    **아래**에 있어야 한다. 정밀화가 턴 상한까지 끌면 sessions.py가 스트림을 error로 끊고
#    그러면 **초안조차 저장되지 않는다** — 2상으로 쪼갠 목적(먼저 준 것은 지킨다)이 통째로
#    무너지는 최악의 실패다. 그래서 여기서 먼저 접고 초안을 확정한다.
#  - _TURN_RESERVE_SEC: 정밀화가 끝난 **뒤에** 남아 있는 일(finalize·검증 요약·trigger·
#    백엔드 저장)의 몫. 실제 턴 데드라인을 알 때 거기서 이만큼 떼고 쓴다.
#  - _TURN_SOFT_BUDGET_SEC: 데드라인을 **모를 때만** 쓰는 폴백(900 - 60). 이 값은 "초안
#    생성 시작"부터 재는 것이라 그 앞의 intake·analyze·spec 생성을 못 센다 —
#    그래서 데드라인을 아는 경로(운영)에서는 쓰지 않는다.
#  - _REFINE_TIMEOUT_SEC: 1상이 아무리 빨라도 정밀화에 이보다 더 주지는 않는다.
#  - _REFINE_MIN_BUDGET_SEC: 남은 예산이 이보다 적으면 시작조차 안 한다 — 어차피 못 끝낼
#    정밀화를 켜서 잠금만 걸었다 푸는 것은 사용자에게 손해만 준다.
_TURN_RESERVE_SEC = 60.0
_TURN_SOFT_BUDGET_SEC = 840.0
_REFINE_TIMEOUT_SEC = 420.0
_REFINE_MIN_BUDGET_SEC = 30.0
# 교정 루프가 **새 라운드를 시작하지 않고 접는** 여유분. 라운드 하나는 surgeon 1회 + 재검수라
# 이보다 짧은 잔여로 시작하면 하드 컷에 걸려 그 라운드가 통째로 버려진다 — 그리고 하드 컷은
# 2상 결과 전체(그때까지 채택된 라운드 포함)를 버리므로 손실이 라운드 하나로 끝나지 않는다.
_REFINE_ROUND_RESERVE_SEC = 45.0
# 취소·타임아웃 확인 주기. 탈출구 버튼의 체감 반응 속도가 이 값이다(0.5초면 즉시로 느껴진다).
_REFINE_POLL_SEC = 0.5


# ─────────────────────────────────────────────────────────────────────────────
# 헬퍼 (v2 계승 — _coerce/_parse/_attach_sources는 실측 검증 자산)
# ─────────────────────────────────────────────────────────────────────────────

def _make_llm():
    """compose용 ChatOpenAI 클라이언트를 만든다(사용량 스트리밍 on)."""
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=config.OPENAI_MODEL, api_key=config.OPENAI_API_KEY, stream_usage=True)


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


# compose 파싱 실패의 원인 구분 (RPA-298 항목 B). 처방이 정반대라 뭉치면 안 된다:
# 잘림에는 "부피를 줄여 다시" + 깨진 본문 제거가 맞고, 문법 오류에는 "네가 쓴 걸 보고 고쳐라"가
# 맞다 — 문법 오류에 본문을 지우면 모델이 자기 실수를 볼 수 없어 **현행보다 나빠진다**.
PARSE_TRUNCATED = "truncated"   # 상한·예산 소진으로 중간에서 끊김
PARSE_SYNTAX = "syntax"         # 끝까지 나왔는데 문법이 깨짐(미이스케이프 따옴표 등)
PARSE_SHAPE = "shape"           # 파싱은 됐는데 최상위 모양이 아님(steps 없음)
PARSE_NO_JSON = "no_json"       # 본문에 JSON 객체가 아예 없음


class ComposeParseError(ValueError):
    """compose 출력 파싱 실패 — `kind`로 원인을 구분한다.

    ValueError를 상속하는 것이 계약이다: 호출부의 `except ValueError`는 `_coerce_flow`가
    예상 못 한 형태에 내는 ValueError도 함께 받아야 한다. 좁히면 그게 asyncio.gather 밖으로
    전파돼 **후보 하나의 실패가 턴 전체를 죽인다**(부분 실패 격리의 붕괴).
    """

    def __init__(self, message: str, kind: str = PARSE_SYNTAX):
        super().__init__(message)
        self.kind = kind


def _scan_json(text: str) -> tuple[int, bool]:
    """텍스트를 한 번 훑어 (미닫힌 괄호 수, 문자열 안에서 끝났는가)를 돌려준다.

    `rfind("}")`나 예외 문구 매칭으로는 잘림과 문법 오류를 못 가른다 — 값 안에 중괄호가
    섞이면 문자열 한가운데를 문서 끝으로 잡고, 오류 문구는 파이썬·모델 버전에 따라 달라진다.
    문자열 안에서 끝났다는 것은 **따옴표가 안 닫혔다**는 뜻이라, 미닫힌 괄호가 있어도
    잘림이 아니라 문법 오류다(이스케이프 안 된 `"` 하나가 뒤 전체를 문자열로 뒤집는다).
    """
    depth = 0
    in_str = False
    escaped = False
    for ch in text:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
    return depth, in_str


def _classify_parse_failure(text: str, finish_reason: str | None) -> str:
    """왜 깨졌나. finish_reason이 1순위다 — 괄호 균형만 보면 상한 절단을 놓친다."""
    if finish_reason == "length":
        return PARSE_TRUNCATED
    if not text.strip():
        return PARSE_NO_JSON
    depth, in_str = _scan_json(text)
    return PARSE_TRUNCATED if (depth > 0 and not in_str) else PARSE_SYNTAX


# 잘린 출력에서 '설계 뼈대'만 뽑는 정규식 — 값은 짧게 잘라 담고, 개행·이스케이프가 든 값은
# 애초에 안 잡는다(문자열 경계를 다시 추측하지 않기 위해).
_SKELETON_KEYS = re.compile(r'"(step_id|label|package|action)"\s*:\s*"([^"\\\n]{0,60})"')
_MAX_SKELETON_ENTRIES = 120


def _salvage_skeleton(text: str) -> str:
    """잘린 출력에서 단계·액션 표기만 등장 순서대로 뽑아 한 줄씩 렌더한다.

    잘림 재시도에 **깨진 12KB 본문 대신** 이걸 싣는다. 본문을 통째로 지우면 모델 문맥에
    '그대로 둘 설계'가 없어져 "설계는 유지하고 부피만 줄여라"가 무의미해지고(첫 시도와 같은
    조건), 본문을 그대로 되돌려주면 긴 출력을 예시로 각인시키면서 입력비까지 문다.

    ⚠ 이 결과물은 **후보로 승격하지 않는다.** req_id를 든 액션이 잘려 나간 복구본은 요구당
    blocker(100)를 만들어 회귀 가드의 비교축을 오염시키고, `_recover_identity`가 만든
    Step 스캐폴드는 사전 검증(drop_unknown_action_ops)에 걸려 구조적으로 수리 불가다.
    여기서는 오직 '모델에게 자기 설계를 상기시키는 메모'로만 쓴다.
    """
    lines: list[str] = []
    for key, value in _SKELETON_KEYS.findall(text):
        lines.append(f"{key}={value}")
        if len(lines) >= _MAX_SKELETON_ENTRIES:
            lines.append("… (이하 생략)")
            break
    return " / ".join(lines)


def _parse_flow(content: str, finish_reason: str | None = None) -> dict:
    """LLM 최종 출력에서 Recommendation 흐름도 dict를 뽑는다 (코드펜스 내성).

    실패는 ComposeParseError(ValueError)로 던지고 `kind`에 원인을 실는다 — 호출부가
    원인별로 다른 재시도를 보내야 하기 때문이다(§B).
    """
    text = (content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        # 여는 중괄호는 있는데 닫는 게 없다 = 잘림. 둘 다 없으면 애초에 JSON을 안 냈다.
        kind = PARSE_TRUNCATED if (start != -1 or finish_reason == "length") else PARSE_NO_JSON
        raise ComposeParseError("JSON 객체를 찾지 못함", kind)
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise ComposeParseError(
            f"JSON 파싱 실패: {e}", _classify_parse_failure(text, finish_reason)
        ) from e
    if not isinstance(obj, dict) or "steps" not in obj:
        raise ComposeParseError("최상위에 steps 키가 있는 JSON 객체가 아님", PARSE_SHAPE)
    return _coerce_flow(obj)


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

def _compose_retry_message(kind: str, err: str) -> str:
    """원인별 재시도 지시. 잘림과 문법 오류는 처방이 정반대다(§B)."""
    if kind == PARSE_TRUNCATED:
        return (
            "출력이 끝까지 나오지 않았다(JSON이 중간에서 끊겼다). 위에 남긴 뼈대가 네가 방금 "
            "설계한 흐름이다 — **설계는 그대로 두고 부피만 줄여** 완전한 JSON 하나로 다시 출력하라.\n"
            "줄이는 방법: rationale 40자 이내, description 50자 이내, label 20자 이내, "
            "들여쓰기·줄바꿈 없는 compact JSON.\n"
            "⚠ 단계·액션·필수 파라미터를 **빼서** 줄이지 마라. 줄이는 것은 설명 문구뿐이다."
        )
    if kind == PARSE_NO_JSON:
        return "JSON 객체를 하나도 출력하지 않았다. 코드펜스·설명 없이 Recommendation JSON 객체 하나만 출력하라."
    if kind == PARSE_SHAPE:
        return (
            f"출력이 Recommendation 모양이 아니다({err}). 최상위에 steps 배열이 있는 "
            "JSON 객체 하나만, 코드펜스·설명 없이 다시 출력하라."
        )
    return (
        f"위 출력의 JSON 문법이 깨졌다({err}). 네가 방금 쓴 출력을 보고 그 자리를 고쳐라 — "
        "문자열 값 안의 큰따옴표는 \\\" 로, 줄바꿈은 \\n 으로 이스케이프해야 한다. "
        "설계는 그대로 두고, 코드펜스·설명 없이 JSON 객체 하나만 다시 출력하라."
    )


def _emit_compose_failure(cid: str, kind: str, **data) -> None:
    """compose 후보 실패를 관측 이벤트로 남긴다.

    ⚠ message는 **중립적인 사람 말**이다. `stage` 메시지는 프론트가 assistantMessage.stages에
    쌓아 화면에 표시하므로, truncated·no_json 같은 진단 문구가 그대로 사용자에게 나간다.
    진단축은 data에만 싣는다 — 컨테이너 재시작으로 로그가 날아가도 turn_events에는 남는다.
    """
    emit({
        "event": "stage", "stage": "composing",
        "message": f"후보 {cid}를 다시 정리하는 중",
        "data": {"candidate": cid, "kind": kind, **{k: v for k, v in data.items() if v is not None}},
    })


async def _compose_candidate(
    cid: str,
    persona_file: str,
    spec: dict,
    dossier: dict,
    analysis: dict,
    document: str | None,
    sink: list[dict],
    sem: asyncio.Semaphore,
    ctx,
) -> dict | None:
    """페르소나 하나로 후보 흐름도를 생성한다. 실패 시 None (부분 실패 격리)."""
    from ..orchestrator.render import analysis_brief
    from ..orchestrator.spec import fenced_doc_block
    from ..orchestrator.tools import build_kb_tools, execute_tool_calls

    persona = (_PROMPT_DIR / persona_file).read_text(encoding="utf-8")
    llm = _make_llm()
    # 검색할 KB가 없으면(사용자 제공 카탈로그) 스펙 조회 툴만 준다 — 메뉴에 전량이 실려 있다.
    tools = build_kb_tools(sink, ctx, source_types=SEARCH_SOURCE_TYPES)
    runnable = llm.bind_tools(tools) if tools else llm
    # 출력 턴 전용 — 상한은 bind **한 곳에서만** 준다. 생성자(ChatOpenAI(max_tokens=…))와
    # 병용하면 langchain의 개명 지점이 두 곳이라 리팩터 한 번에 생성자 값이 bind를 덮는다.
    llm_json = llm.bind(response_format=_JSON_RESPONSE_FORMAT)
    usage_config = {"callbacks": [UsageCallbackHandler(purpose=_COMPOSE_PURPOSE)]}

    background = f"\n\n[배경 지식 (공식 문서 발췌)]\n{dossier['background']}" if dossier.get("background") else ""
    # 용례 블록 — 액션 '목록'이 아니라 '조합 패턴'을 보여준다 (RPA-298). 어휘(메뉴)만 주면
    # 모델은 "이 업무엔 어떤 조작들이 어떤 순서로 필요한가"를 못 고른다. 공식 문서의
    # Examples 34건에서 목표에 가까운 것을 결정론으로 골라 dossier가 채운다.
    examples = (
        f"\n\n[비슷한 업무의 공식 문서 용례 — 조합 패턴 참고용]\n{dossier['examples']}"
        if dossier.get("examples") else ""
    )
    system = (
        f"{_BASE_PROMPT}\n\n{_ADDENDUM}\n\n{persona}\n\n"
        f"[업무 분석]\n{analysis_brief(analysis)}\n\n"
        f"[요구사항 스펙]\n{_render_spec_block(spec)}\n\n"
        f"[액션 후보 메뉴]\n{dossier['menu']}{examples}{background}"
    )
    user = (
        "위 요구사항 스펙을 달성하는 A360 흐름도를 당신의 설계 관점대로 설계하고, "
        "최종 Recommendation JSON 하나만 출력하라."
        f"{fenced_doc_block(document)}"
    )
    msgs: list = [SystemMessage(content=system), HumanMessage(content=user)]
    tool_rounds = 0
    parse_attempts = 0

    for _ in range(_COMPOSE_MAX_TURNS):
        # 탐색(escape hatch)과 출력을 가른다. 예전 조건은 `tool_rounds < 2`뿐이라, 모델이 툴을
        # 한 번도 안 부르면 **재출력 턴까지 툴을 달고** 나갔다 — 재시도의 일은 출력이지
        # 조사가 아니다. 출력 턴에는 JSON mode를 건다(문법이 깨질 여지 자체를 줄인다).
        exploring = bool(tools) and tool_rounds < _ESCAPE_HATCH_ROUNDS and parse_attempts == 0
        if not exploring:
            target = llm_json
        elif _JSON_MODE_WITH_TOOLS:
            target = runnable.bind(response_format=_JSON_RESPONSE_FORMAT)
        else:
            target = runnable
        try:
            async with sem:
                ai = await target.ainvoke(msgs, config=usage_config)
        except Exception as e:  # noqa: BLE001 — 후보 하나의 인프라 실패는 N 강등
            logger.warning("후보 %s compose 호출 실패: %s", cid, e)
            _emit_compose_failure(cid, "llm_error", error=type(e).__name__)
            return None
        msgs.append(ai)
        if getattr(ai, "tool_calls", None):
            tool_rounds += 1
            msgs.extend(execute_tool_calls(tools, ai))
            continue

        finish_reason = (getattr(ai, "response_metadata", None) or {}).get("finish_reason")
        content = ai.content if isinstance(ai.content, str) else str(ai.content or "")
        try:
            return _parse_flow(content, finish_reason)
        except ValueError as e:
            # `except ValueError` 그대로 — _coerce_flow가 내는 ValueError까지 여기서 받아야
            # 후보 하나의 실패로 끝난다(ComposeParseError로 좁히면 턴 전체가 죽는다).
            kind = getattr(e, "kind", PARSE_SYNTAX)
            parse_attempts += 1
            _emit_compose_failure(
                cid, kind, attempt=parse_attempts, finish_reason=finish_reason,
                content_chars=len(content),
                output_tokens=(getattr(ai, "usage_metadata", None) or {}).get("output_tokens"),
            )
            if parse_attempts > _COMPOSE_PARSE_RETRIES:
                logger.warning("후보 %s 파싱 재실패(%s) — 탈락: %s", cid, kind, e)
                return None
            logger.info("후보 %s 파싱 실패(%s) — 재시도 %d", cid, kind, parse_attempts)
            if kind == PARSE_TRUNCATED:
                # 깨진 본문을 **설계 뼈대로 갈아끼운다** — 12KB 재전송도, 문맥 삭제도 아니다.
                msgs[-1] = AIMessage(content=(
                    "[직전 출력은 길이 초과로 끊겨 생략함. 내가 설계한 뼈대만 남긴다]\n"
                    + _salvage_skeleton(content)
                ))
            msgs.append(HumanMessage(content=_compose_retry_message(kind, str(e))))
    logger.warning("후보 %s compose 예산 소진 — 탈락", cid)
    _emit_compose_failure(cid, "budget_exhausted", attempt=parse_attempts)
    return None


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


def _emit_candidate_scorecard(reports, verdict: dict, attempted: int) -> None:
    """후보별 채점 결과를 **관측에 남는 형태**로 남긴다 (RPA-298).

    ## 왜 필요한가 (실측, 2026-07-27)

    같은 업무를 5분 간격으로 두 번 돌렸는데 1상 산출물이 가중합 196과 1319로 6.7배 갈렸다.
    교정은 1319를 256까지만 끌어내렸다 — **나쁜 출발점은 교정으로 복구되지 않는다.** 그러니
    재현성의 지렛대는 refine이 아니라 1상인데, 정작 다음 질문에 답할 수 없었다:

        후보 셋이 다 나빴나?  아니면 좋은 후보가 있는데 심판이 다른 걸 골랐나?

    두 원인은 처방이 정반대다(전자는 조사·compose, 후자는 심판). 그런데 후보 요약과 심판
    점수판은 `partial` 이벤트라 sessions._tev가 **의도적으로** turn_events에서 제외한다
    (볼륨 때문 — 흐름도 트리가 통째로 실리므로 옳은 결정이다).

    그래서 여기서 **스칼라만** stage로 한 번 더 낸다. 흐름도·근거·이유 문장을 싣지 않으므로
    부피 문제에 걸리지 않고, 한 번의 실행으로 위 질문이 닫힌다.
    """
    from ..verify.findings import weight as _weight

    totals = {
        s.get("candidate_id"): s.get("total")
        for s in (verdict.get("verdict") or {}).get("scores") or []
        if isinstance(s, dict)
    }
    win_id = getattr(verdict.get("winner"), "candidate_id", None)
    rows = []
    for r in reports:
        # getattr 기본값을 쓰는 이유: 관측이 산출 경로를 죽이면 안 된다. 리포트 모양이
        # 바뀌거나(테스트 대역·미래 필드) 한 필드가 비어도 그 턴 전체가 실패하는 것보다
        # 그 칸만 비는 편이 낫다 — core.llm._log_llm_failure와 같은 원칙.
        errs = [f for f in getattr(r, "findings", None) or [] if f.severity != "warning"]
        det = getattr(r, "deterministic_score", None)
        rows.append({
            "id": getattr(r, "candidate_id", None),
            "persona": getattr(r, "persona", ""),
            "weight": _weight(errs),
            "blockers": sum(1 for f in errs if f.severity == "blocker"),
            "must_coverage": getattr(r, "must_coverage", None),
            "sim_pass_rate": getattr(r, "sim_pass_rate", None),
            "gate_failures": len(getattr(r, "gate_failures", None) or []),
            "det": det() if callable(det) else None,
            "judge": totals.get(getattr(r, "candidate_id", None)),
            "won": getattr(r, "candidate_id", None) == win_id,
        })
    rows.sort(key=lambda x: x["weight"])
    best = rows[0] if rows else {}
    emit({
        "event": "stage", "stage": "verifying",
        "message": f"후보 {len(rows)}개 채점 — 승자 {win_id}",
        "data": {
            "candidates": rows,
            "winner": win_id,
            # 몇 개를 내보내 몇 개가 살아왔나 — 후보 하나만 살면 심판은 고를 것이 없고
            # 합의(agreement) 항도 통째로 꺼진다. 그 턴의 '경쟁'은 없었던 것이다.
            "attempted": attempted,
            "survived": len(rows),
            # 승자가 가중합 최소 후보였나. 아니라면 심판이 다른 축(커버리지·반증)을 우선했다는
            # 뜻이고, 그 판단이 옳았는지를 이 한 줄로 되짚을 수 있다.
            "winner_is_lightest": bool(best) and best.get("won") is True,
            "weight_spread": [rows[0]["weight"], rows[-1]["weight"]] if rows else [],
        },
    })


# ─────────────────────────────────────────────────────────────────────────────
# 2상 구조 — 초안(draft) / 정밀화(refine) 사이의 전달 계약 (설계 §6.3)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DraftResult:
    """1상(draft_flow)이 2상(refine_draft)에 넘기는 **유일한** 전달 계약.

    frozen·JSON 직렬화 가능으로 못 박은 이유는 지금 필요해서가 아니라 **다음 이슈 때문**이다:
    정밀화를 백그라운드 잡으로 떼어내면 이 dataclass가 그대로 잡 페이로드가 된다. 그래서
    Pydantic 객체(CandidateReport·Finding)를 객체째 들지 않고 `model_dump()`로 눕혀 담는다 —
    객체를 담으면 같은 프로세스에서는 돌지만 큐에는 못 싣는다(그때 계약을 다시 뜯게 된다).
    frozen은 2상이 1상 산출물을 제자리 변형해 재시도 시 입력이 달라지는 사고를 막는다.

    - flow      : 심판이 고른 승자 초안 (교정 전)
    - spec/dossier: 2상이 재채점·재교정에 쓰는 기준과 조사 결과
    - reports   : 후보별 CandidateReport dump — 후보 간 합의(agreement) 산출에 flow가 필요
    - verdict   : 프레임용 심판 결과 dict. `verdict["winner"]`가 승자 candidate_id
    - sink      : 검색 히트 — 근거 부착(FR-11)과 confidence의 근거 축
    - findings  : 2상 첫 라운드에 실을 개선 지시(승자 L2/L3 발견 + 심판 이식 지시) dump
    - draft_id  : 이 초안의 식별자. 후속 이슈에서 프론트가 초안↔정밀화 결과를 잇는 키
    """

    flow: dict
    spec: dict
    dossier: dict
    reports: tuple[dict, ...]
    verdict: dict
    sink: tuple[dict, ...]
    findings: tuple[dict, ...]
    draft_id: str

    def winner_report(self) -> dict:
        """verdict가 지목한 승자 후보 리포트(dump). 못 찾으면 빈 dict(신호 결측 취급)."""
        wid = self.verdict.get("winner")
        return next((r for r in self.reports if r.get("candidate_id") == wid), {})


# ─────────────────────────────────────────────────────────────────────────────
# 파이프라인 본체
# ─────────────────────────────────────────────────────────────────────────────

async def draft_flow(analysis: Any, document: str | None, spec: dict, ctx=None) -> DraftResult:
    """1상 — research → compose×N → verify → judge. 교정은 하지 않는다.

    절단선은 원래 코드에 이미 있었다: `emit_flow_frame(..., "선택된 초안 · 다듬기 시작")`이
    정확히 초안 확정 지점이다. 여기까지가 사용자에게 **먼저 보여줄 수 있는 것**이고,
    분 단위가 걸리는 교정·재채점은 2상(refine_draft)으로 넘긴다.

    설계 §5-I에 따라 초안에는 **결정론 검사 결과만 실어 표시하고 교정하지 않는다** —
    초안 지연을 안 늘리는 것이 2상으로 쪼갠 이유 자체다.

    전 후보 실패 시 RuntimeError (호출부가 error 이벤트로 처리).
    """
    from ..catalog_context import a360_context
    from ..orchestrator.judge import judge_candidates
    from .research import build_dossier

    # ctx 미지정은 a360 기본 — 기존 호출부(테스트 포함) 호환 (RPA-285).
    ctx = ctx or a360_context()
    analysis = _to_dict(analysis)
    sink: list[dict] = []
    sem = asyncio.Semaphore(config.MAX_LLM_CONCURRENCY)

    # [2] research — 조사 선행·공유
    dossier = await build_dossier(spec, sink, ctx)

    # [3] compose ×N — 문서 기반이면 문서 충실 페르소나까지 3후보
    personas = [(c, n, f) for c, n, f, needs_doc in _PERSONAS if document or not needs_doc]
    cand_status = [
        {"id": c, "persona": n, "status": "composing", "steps": 0, "actions": 0}
        for c, n, _ in personas
    ]
    emit_candidates_frame(cand_status, f"{len(personas)}가지 설계 관점으로 후보 생성 중")

    composed = await asyncio.gather(*(
        _compose_candidate(c, f, spec, dossier, analysis, document, sink, sem, ctx)
        for c, n, f in personas
    ))
    flows: list[tuple[str, str, dict]] = []
    for (c, n, _f), flow in zip(personas, composed):
        st = next(s for s in cand_status if s["id"] == c)
        if flow is None:
            st["status"] = "failed"
        else:
            st["status"] = "verifying"
            st["steps"], st["actions"] = _flow_counts(flow)
            flows.append((c, n, flow))
    emit_candidates_frame(cand_status, f"후보 {len(flows)}개 생성 — 검증 스택 통과 중")
    if not flows:
        raise RuntimeError("흐름도 후보를 하나도 생성하지 못했습니다")

    # [4] verify 스택 — 후보별 병렬
    reports = await asyncio.gather(*(
        _verify_candidate(c, n, flow, spec, sem, ctx) for c, n, flow in flows
    ))
    for r in reports:
        st = next(s for s in cand_status if s["id"] == r.candidate_id)
        st["status"] = "done"
    emit_candidates_frame(cand_status, "후보 검증 완료 — 심판 채점 중")

    # [5] judge — 승자 + 이식 지시.
    # 문서 원문을 함께 넘긴다: 심판의 맹목 기대는 "spec+문서만 보고 독립 생성"이 전제라
    # 문서가 빠지면 요구사항 요약만 보고 세운 기대가 되고, 입력 비대칭(후보는 원문을 봤다)
    # 때문에 원문에만 있는 누락을 잡아내지 못한다.
    verdict = await asyncio.to_thread(judge_candidates, spec, list(reports), document=document)
    winner = verdict["winner"]
    emit_verdict_frame(verdict["verdict"], f"후보 {winner.candidate_id} 선택")
    # 심판 프레임은 partial이라 관측에 안 남는다 — 스칼라만 따로 남긴다(재현성 진단).
    try:
        _emit_candidate_scorecard(reports, verdict, attempted=len(personas))
    except Exception:  # noqa: BLE001 — 관측 실패가 산출을 죽이면 안 된다
        logger.warning("후보 채점 관측 실패 — 산출은 계속한다", exc_info=True)

    # 승자 트리 점진 노출 — '자라나는 흐름도' 경험은 승자 확정 이후부터 (v2 계승).
    steps = winner.flow.get("steps") or []
    for i in range(len(steps)):
        emit_flow_frame({**winner.flow, "steps": steps[: i + 1]}, None,
                        f"선택된 설계 구성 {i + 1}/{len(steps)}")
        await asyncio.sleep(_REVEAL_DELAY)
    emit_flow_frame(winner.flow, winner.violations, "선택된 초안 · 다듬기 시작")
    # kind="draft"를 **추가로** 흘린다(기존 flow 프레임은 그대로 둔다) — 프론트는 아직
    # kind="draft"를 모르므로 이걸 빼면 FE가 초안 캡션을 잃는다. 계약 무변경이 우선이다.
    draft_id = uuid.uuid4().hex
    emit_draft_frame(winner.flow, winner.violations, draft_id, "선택된 초안 · 다듬기 시작")

    # 2상에 넘길 개선 지시 — 승자의 L2/L3 발견 + 심판의 이식 지시. Finding 객체가 아니라
    # dump로 눕혀 담는다(DraftResult 계약: 큐에 실을 수 있어야 한다).
    extra = [f for f in winner.findings if f.layer in ("L2", "L3")] + verdict["transplant_findings"]
    return DraftResult(
        flow=winner.flow,
        spec=spec,
        dossier=dossier,
        reports=tuple(r.model_dump() for r in reports),
        verdict=verdict["verdict"],
        sink=tuple(sink),
        findings=tuple(f.model_dump() for f in extra),
        draft_id=draft_id,
    )


async def refine_draft(draft: DraftResult, ctx=None, *, deadline_mono: float | None = None) -> dict:
    """2상 — refine → finalize. 초안을 교정하고 근거·신뢰도·질문 카드를 합성한다.

    반환은 `generate_flow`의 반환 그대로다: {"recommendation": ..., "violations": ...}.
    이 상만 따로 재실행해도 되도록 draft를 **읽기만** 한다 — 교정 대상 흐름은 deepcopy로
    떠서 쓴다. 안 그러면 refine이 무개선일 때 finalize의 제자리 변형(needs_input·spec 주입)이
    draft.flow에 새어, 재시도 시 입력이 이미 오염돼 있다.

    deadline_mono를 주면 교정 루프가 **라운드를 시작하기 전에** 접는다 — 바깥 하드 컷은
    2상 결과를 통째로 버리고 초안을 확정하므로, 그때까지 채택된 라운드의 성과도 함께
    사라진다(generate_flow_two_phase의 REFINE_TIMEOUT 분기).
    """
    from ..catalog_context import a360_context
    from ..orchestrator import cards as cards_mod
    from ..orchestrator.harness import (
        attach_confidence,
        compute_flow_confidence,
        from_violations_dicts,
        refine_flow,
    )
    from ..verify import findings as F
    from ..verify.findings import Finding
    from ..verify.semantic import run_semantic_check

    ctx = ctx or a360_context()
    spec = draft.spec
    sink = list(draft.sink)
    sem = asyncio.Semaphore(config.MAX_LLM_CONCURRENCY)
    winner_report = draft.winner_report()

    # [6] refine — 정적 위반 + L2/L3 발견 + 이식 지시를 surgeon 패치로
    #
    # spec을 넘기는 게 §5-D의 발효 지점이다. 이걸 안 넘기면 회귀 가드의 비교축이 정적
    # 위반 가중합뿐이라, 위반 있는 액션을 **지우는 것**이 항상 유효한 개선 경로가 된다
    # (실측 서명: 정밀도만 오르고 재현율 정체). spec이 있으면 요구를 담당하던 액션을
    # 지울 때 누락 blocker(100)가 생겨 major(10) 제거를 압도하고 가드가 그 패치를 거부한다.
    #
    # 생성 경로에서만 넘긴다 — edit 경로는 set_spec으로 요구를 함께 지우기 전까지
    # "이 단계 빼주세요"가 blocker로 되살아나므로 harness 기본값(None)을 유지한다(§6.1).
    extra = [Finding.model_validate(f) for f in draft.findings]
    refined = await asyncio.to_thread(
        refine_flow, copy.deepcopy(draft.flow), ctx.catalog,
        extra_findings=extra, purpose="turn_generate", spec=spec,
        deadline_mono=deadline_mono,
    )
    flow, violations = refined["flow"], refined["violations"]

    # [7] finalize — 근거·confidence·질문 카드·flow_confidence
    flow = _attach_sources(flow, sink)

    coverage = None
    sim_rate = winner_report.get("sim_pass_rate")
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
    must_cov = coverage.must_coverage if coverage is not None else winner_report.get("must_coverage")

    findings_final, r3_cards = from_violations_dicts(violations)
    if coverage is not None:
        findings_final += F.from_coverage(coverage)

    # 질문 카드 — R3 + unknowns + assumptions (결정론) → 문구 다듬기 (no-fail LLM 1회)
    cards = cards_mod.build_cards(flow, spec, r3_cards, ctx.catalog)
    if cards:
        async with sem:
            cards = await asyncio.to_thread(cards_mod.polish_card_wording, cards)

    # confidence — agreement(후보 간 합의)는 후보 2개 이상일 때만.
    # 1상이 후보 flow를 reports에 담아 넘긴 건 이 합의 계산 때문이다(다른 용도 없음).
    agreement = None
    if len(draft.reports) >= 2:
        counts: dict[tuple[str, str], int] = {}
        for r in draft.reports:
            for key in set(_iter_pkg_actions(r.get("flow") or {})):
                counts[key] = counts.get(key, 0) + 1
        agreement = {k for k, v in counts.items() if v >= 2}
    attach_confidence(flow, sink, violations, agreement=agreement)

    blocking_cards = sum(1 for c in cards if c.get("blocking") and not c.get("resolved"))
    flow["needs_input"] = cards
    flow["spec"] = spec
    flow["flow_confidence"] = compute_flow_confidence(
        must_coverage=must_cov,
        findings=findings_final,
        sim_pass_rate=sim_rate,
        blocking_cards=blocking_cards,
    )

    emit_scorecard_frame({
        "must_coverage": must_cov,
        "blockers": sum(1 for f in findings_final if f.severity == "blocker"),
        "warnings": sum(1 for f in findings_final if f.severity == "warning"),
        "sim_pass_rate": sim_rate,
        "cards": len(cards),
        "flow_confidence": flow["flow_confidence"],
    }, "최종 검증 요약")

    flow = _coerce_flow(flow)
    try:
        rec = Recommendation.model_validate(flow)
    except ValidationError as e:
        # 마지막 관문에서 전부 버리지 않는다 — 교정 전 승자 초안(직전 유효 후보)으로 강등 시도.
        logger.warning("최종 흐름도 정규화 실패 — 승자 초안으로 강등 시도: %s", e)
        try:
            rec = Recommendation.model_validate(_coerce_flow(copy.deepcopy(draft.flow)))
        except ValidationError as e2:
            logger.warning("승자 초안도 정규화 실패, 빈 추천안: %s", e2)
            rec = Recommendation(steps=[])
    rec_dict = rec.model_dump()
    emit_flow_frame(rec_dict, violations, "완료")
    return {"recommendation": rec_dict, "violations": violations}


def _finalize_draft_only(draft: DraftResult) -> dict:
    """정밀화 없이 초안만으로 확정본을 만든다 — 탈출구·타임아웃·실패의 공통 출구.

    반환은 refine_draft와 같은 {"recommendation", "violations"}라 호출부가 분기 없이
    같은 자리에 꽂을 수 있다. **LLM을 한 번도 부르지 않는다** — 탈출구는 "지금 당장"이
    존재 이유라 여기서 또 기다리게 하면 탈출이 아니다.

    2상에서 오는 것 중 여기서 빠지는 것과 그 이유:
    - 교정(surgeon): 초안은 결정론 검사 결과만 실어 표시하고 교정하지 않는다(설계 §5-I).
      그래서 violations는 승자 후보의 검사 결과 **그대로**다 — 화면의 초안과 위반 표시가
      정확히 일치한다.
    - 질문 카드(needs_input): 카드 문구 다듬기가 LLM 경로다. 빈 목록으로 둔다(프론트가
      키 부재와 빈 목록을 다르게 다루지 않도록 키 자체는 채운다).
    - flow_confidence: 결정론 축(커버리지·위반·시뮬)만으로 계산한다. 카드 감쇠는 0.
    spec은 반드시 싣는다 — 이후 수정 턴의 회귀 가드가 flow["spec"]의 요구를 비교축으로
    쓰기 때문에, 여기서 빠뜨리면 초안 확정본을 고칠 때 삭제 편향이 되살아난다(§5-D).
    """
    from ..orchestrator.harness import (
        attach_confidence,
        compute_flow_confidence,
        from_violations_dicts,
    )

    winner = draft.winner_report()
    violations = list(winner.get("violations") or [])
    sink = list(draft.sink)

    flow = _coerce_flow(copy.deepcopy(draft.flow))  # 1상 산출물은 읽기만 한다(재시도 입력 보존)
    flow = _attach_sources(flow, sink)
    attach_confidence(flow, sink, violations)
    findings, _cards = from_violations_dicts(violations)
    flow["needs_input"] = []
    flow["spec"] = draft.spec
    flow["flow_confidence"] = compute_flow_confidence(
        must_coverage=winner.get("must_coverage"),
        findings=findings,
        sim_pass_rate=winner.get("sim_pass_rate"),
        blocking_cards=0,
    )

    try:
        rec = Recommendation.model_validate(flow)
    except ValidationError as e:
        logger.warning("초안 확정 정규화 실패 — 빈 추천안: %s", e)
        rec = Recommendation(steps=[])
    return {"recommendation": rec.model_dump(), "violations": violations}


async def generate_flow_two_phase(
    analysis: Any,
    document: str | None,
    spec: dict,
    ctx=None,
    *,
    session_id: str | None = None,
    refine_timeout_sec: float = _REFINE_TIMEOUT_SEC,
    turn_deadline_mono: float | None = None,
) -> dict:
    """2상 운영 진입점 — 초안을 **먼저** 내보내고, 잠금을 건 채 정밀화를 잇는다 (설계 §6.3).

    `generate_flow`의 대체가 아니라 **추가 진입점**이다. generate_flow는 시그니처·반환이
    v1~v3와 맞아야 비교(골드셋)가 성립하므로 건드리지 않았다.

    ### 왜 "백그라운드 태스크"가 아니라 같은 스트림 안인가
    이 서비스의 턴은 `POST /{id}/turn` SSE 1스트림이고, 백엔드는 **done 1회**에서만 추천
    버전을 저장한다(`app/api/sessions.py::_persist_turn_result`). 턴이 끝난 뒤에도 도는
    진짜 백그라운드 잡을 만들려면 (a) 정밀화 결과를 저장할 두 번째 경로, (b) 프론트가
    그 결과를 받아갈 채널(폴링/재연결), (c) 워커 간 잡 큐가 함께 필요하다 — 프론트가 별도
    레포라 동시 수정이 안 되는 이번 범위에서 감당할 수 없다. 그래서 **같은 스트림 안에서
    초안 프레임을 먼저 흘리고 정밀화를 잇는** 형태를 골랐다. 사용자 관점의 이득(초안을
    분 단위로 먼저 본다)은 그대로고, 남는 것은 "턴이 끝나야 새 턴을 시작할 수 있다"뿐이다.
    DraftResult가 frozen·JSON 직렬화로 못 박혀 있는 이유가 그 후속 이전을 위해서다.

    ### 관측 가능한 것
    1. 초안이 정밀화 **전에** 나간다 — draft_flow의 emit_draft_frame(kind="draft").
    2. 진행 중 상태 — emit_refine_frame(status="running", locked=...).
    3. 잠금 — session_id 범위로 레지스트리에 등록(수정 경로가 이걸 보고 거부).
    4. 탈출구 — 다른 요청이 request_refine_cancel()을 걸면 접고 초안을 확정.
    5. 실패·타임아웃 — 잠금 해제 + 초안 유지 + 사유. 턴 자체는 성공으로 끝난다.

    session_id를 안 주면(평가 스크립트·단독 실행) 잠금만 생략하고 흐름은 같다.

    turn_deadline_mono(백엔드가 이 턴을 끊는 time.monotonic() 시각)를 주면 남은 예산을
    **턴 시작 기준**으로 계산한다. 안 주면 초안 생성 시간만 빼는 종전 계산으로 폴백한다 —
    그 폴백은 intake·analyze·spec 생성을 못 세므로 운영 경로는 반드시 넘긴다.

    반환: {"recommendation", "violations", "refine": {status, reason, draft_id, elapsed_ms,
    locked}}. refine 키는 **추가**라 {"recommendation","violations"}만 읽던 호출부는 그대로다.
    """
    from ..catalog_context import a360_context
    from ..orchestrator.state import (
        REFINE_ABORTED,
        REFINE_CANCELLED,
        REFINE_DONE,
        REFINE_FAILED,
        REFINE_RUNNING,
        REFINE_TIMEOUT,
        acquire_refine_lock,
        defer_refine_lock_release,
        release_refine_lock,
    )

    ctx = ctx or a360_context()
    t_turn = time.monotonic()
    draft = await draft_flow(analysis, document, spec, ctx)  # emit_draft_frame이 여기서 나간다
    draft_elapsed = time.monotonic() - t_turn

    # 남은 턴 예산. 데드라인을 알면 **턴 시작부터의 잔여**를, 모르면 초안 생성 시간만 뺀
    # 근사를 쓴다. 이걸 안 깎으면 턴 상한에 걸려 초안까지 잃는다(위 상수 주석).
    if turn_deadline_mono is not None:
        remaining = turn_deadline_mono - time.monotonic() - _TURN_RESERVE_SEC
    else:
        remaining = _TURN_SOFT_BUDGET_SEC - draft_elapsed
    budget = min(refine_timeout_sec, remaining)
    if budget < _REFINE_MIN_BUDGET_SEC:
        # 원인이 초안 생성만은 아니다(데드라인 경로에선 intake·분석이 먹었을 수도 있다) —
        # 초안 소요는 참고로만 밝히고 결론("초안 그대로 확정")을 앞세운다.
        reason = (
            f"이번 턴에 남은 시간이 부족해 다듬기를 생략했어요(초안 생성 {int(draft_elapsed)}초) "
            "— 초안 그대로 확정했습니다."
        )
        emit_refine_frame(REFINE_TIMEOUT, draft.draft_id, "정밀화 생략 · 초안 확정",
                          reason=reason, locked=False, elapsed_ms=0)
        return {**_finalize_draft_only(draft),
                "refine": {"status": REFINE_TIMEOUT, "reason": reason,
                           "draft_id": draft.draft_id, "elapsed_ms": 0, "locked": False,
                           "persist_pending": False}}  # 잠근 적이 없으니 넘길 것도 없다

    lock = acquire_refine_lock(session_id, draft.draft_id, budget)
    emit_refine_frame(
        REFINE_RUNNING, draft.draft_id,
        "초안 확정 · 정밀화 중 (완료까지 수정이 잠깁니다)" if lock else "초안 확정 · 정밀화 중",
        locked=lock is not None,
    )

    t0 = time.monotonic()
    # 교정 루프에 데드라인을 **알려 준다**. 아래 하드 컷은 out을 None으로 두고 초안을
    # 확정하므로, 그때까지 채택된 라운드의 성과까지 통째로 버려진다. 루프가 스스로 접으면
    # 채택된 현재본을 들고 정상 종료한다 — 하드 컷은 이중 안전망으로 그대로 남는다.
    task = asyncio.create_task(
        refine_draft(draft, ctx, deadline_mono=t0 + budget - _REFINE_ROUND_RESERVE_SEC)
    )
    status, reason = REFINE_DONE, None
    out: dict | None = None
    persist_pending = False  # 잠금을 백엔드 저장까지 넘겼는가 (아래 finally에서 확정)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=_REFINE_POLL_SEC)
            if task in done:
                break
            if lock is not None and lock.cancel_requested:
                status = REFINE_CANCELLED
                reason = lock.cancel_reason or "정밀화를 중단하고 초안으로 확정했어요."
                break
            if time.monotonic() - t0 >= budget:
                status = REFINE_TIMEOUT
                reason = (
                    f"정밀화가 {int(budget)}초를 넘겨 중단했어요 — 초안 그대로 확정했습니다."
                )
                break
        if status == REFINE_DONE:
            out = task.result()
    except asyncio.CancelledError:
        # 바깥 취소(클라이언트 끊김·턴 상한)는 **삼키지 않는다** — 삼키면 asyncio 협조적
        # 취소가 깨진다. 다만 상태까지 REFINE_DONE으로 남기면, 추천 버전이 하나도 저장되지
        # 않은 턴을 재접속 클라이언트(GET /refine의 last)에게 "정밀화 정상 완료"로 보고하게
        # 된다 — 이 레포가 반복해 피하는 조용한 오답이다. 상태만 바로잡고 다시 던진다.
        status, reason = REFINE_ABORTED, "턴이 중단돼 정밀화를 끝내지 못했어요."
        raise
    except Exception as e:  # noqa: BLE001 — 정밀화 실패로 초안까지 잃지 않는다
        logger.warning("2상 정밀화 실패 — 초안으로 확정: %s", e, exc_info=True)
        status, reason = REFINE_FAILED, "정밀화 중 오류가 생겨 초안 그대로 확정했어요."
    finally:
        # 잠금은 여기서 풀지 않는다 — 추천 버전 INSERT는 한참 뒤 백엔드(`app/api/sessions.py::
        # _persist_turn_result`)에서 일어난다. 여기서 풀면 그 사이(trigger LLM·R15 재검사·
        # 그래프 마무리·DB 쓰기) 들어온 편집이 409를 안 받고 vN을 만들고, 턴 저장이 그 위를
        # 덮어쓴다. 그래서 '저장 대기'로만 표시하고 반납은 저장을 마친 백엔드가 한다.
        persist_pending = defer_refine_lock_release(lock, status, reason)
        if not persist_pending:
            # 잠금이 없거나(세션 밖 실행) 이미 새 초안에 밀려난 경우 — 종전 경로 그대로.
            release_refine_lock(lock, status, reason)
        if not task.done():
            # refine_draft가 asyncio.to_thread 안(교정·재채점)이면 **그 스레드는 끝까지 돈다**
            # — 파이썬이 스레드를 죽일 수 없기 때문이다. 진행 중이던 LLM 호출 1건의 비용은
            # 그대로 나가고 결과는 버려진다(고아 스레드는 deepcopy한 흐름만 만지므로 확정본을
            # 오염시키지는 않는다). 다만 **여기서 기다리는 시간은 0이다**: to_thread의 asyncio
            # 래퍼 future는 취소 즉시 cancelled로 확정되고 스레드 종료를 기다리지 않는다.
            # 잠금 반납이 이제 턴 저장 뒤로 밀린 만큼 이 대기가 탈출구를 늦추는지 실측했고,
            # 늦추지 않는다(3초짜리 to_thread를 취소하고 wait 한 소요 0.000초).
            task.cancel()
            # asyncio.wait는 태스크 예외를 되던지지 않는다 — 여기서 전파되는 CancelledError는
            # 오직 **바깥 취소**뿐이라 협조적 취소가 깨지지 않는다(await task를 쓰면 우리가 건
            # 취소까지 올라와 삼켜야 하고, 그러다 바깥 취소도 같이 먹는다).
            await asyncio.wait({task})
        if not task.cancelled() and task.exception() is not None:
            logger.debug("정밀화 태스크 잔여 예외 회수: %s", task.exception())

    if out is None:  # 취소·타임아웃·실패 — 정밀화는 접었고 초안이 확정본이다
        out = _finalize_draft_only(draft)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    # locked=False는 계약 그대로 둔다 — 프론트가 이 프레임을 "정밀화 구간이 끝났다"로 읽고
    # 편집 UI를 되살리는 신호다. 실제 잠금은 턴 저장까지 조금 더 남아 있지만, 그 사이 들어온
    # 편집은 조용히 덮이는 대신 409(탈출구 경로 포함)로 정확히 거절된다 — 그게 권위 있는
    # 게이트다. 남은 창을 아는 클라이언트를 위해 persist_pending을 **추가**로만 싣는다.
    emit_refine_frame(
        status, draft.draft_id,
        "정밀화 완료" if status == REFINE_DONE else "정밀화 중단 · 초안 확정",
        reason=reason, locked=False, elapsed_ms=elapsed_ms,
    )
    return {**out, "refine": {"status": status, "reason": reason, "draft_id": draft.draft_id,
                              "elapsed_ms": elapsed_ms, "locked": False,
                              "persist_pending": persist_pending}}


async def generate_flow(analysis: Any, document: str | None, spec: dict, ctx=None) -> dict:
    """spec → research → compose×N → verify → judge → refine → finalize.

    2상(draft_flow + refine_draft)의 순차 합성일 뿐, **시그니처와 반환은 그대로다** —
    백엔드가 '턴 = SSE 1스트림 = done 1회 = 추천 0~1버전' 등식 위에 서 있고 done 1회 가정이
    `app/api/sessions.py` 세 곳에 흩어져 있어서다. 이번 변경은 같은 턴·같은 스트림 안에서
    파이프라인을 두 상으로 쪼갠 **구조 작업**이고, 백그라운드 실행·done 2회는 후속 이슈다.

    잠금·탈출구·타임아웃이 붙은 운영 경로는 `generate_flow_two_phase`다. 이쪽은 그 장치가
    없는 **순수 합성**으로 남긴다 — 골드셋 평가(`recommend()`)가 타는 경로라, 여기에 시간
    예산을 걸면 평가 점수가 인프라 지연에 흔들려 버전 비교가 성립하지 않는다.

    반환: {"recommendation": Recommendation dict, "violations": list[dict]}.
    전 후보 실패 시 RuntimeError (호출부가 error 이벤트로 처리).
    """
    from ..catalog_context import a360_context

    # ctx를 여기서 한 번만 확정해 두 상이 같은 어휘 출처를 보게 한다 (RPA-285).
    ctx = ctx or a360_context()
    draft = await draft_flow(analysis, document, spec, ctx)
    return await refine_draft(draft, ctx)


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
