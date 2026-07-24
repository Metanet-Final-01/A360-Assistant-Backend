"""edit — 흐름도 수정 브랜치 노드 (RPA-65, 연산 기반 재설계).

산출(generate)이 결정론 shortlist 파이프라인을 쓰는 것과 달리, 수정은 요청이 국소적이라
KB 접근을 LLM 재량의 툴 호출로 둔다: 새 액션 표기가 필요하면 search_kb, 파라미터 스펙이
필요하면 get_action_schema를 불러 확인한다.

★ 근본 재설계: LLM이 '수정된 흐름도 전체'를 다시 출력하지 않는다. 큰 흐름도를 한 글자도 안
틀리고 재출력하라는 요구는 (1) 원본을 그대로 되뱉는 게으른 에코(change_summary만 그럴듯)와
(2) 스크립트 파라미터의 따옴표·개행 오이스케이프로 인한 JSON 파손을 필연적으로 부른다.
대신 LLM은 노드 id를 참조하는 **작은 수정 연산(EditOps)만** 내고, edit_ops가 현재 흐름도에
결정론적으로 적용한다 — 손대지 않은 노드는 원본 그대로라 파라미터·value_source가 자동
보존되고(예전엔 프롬프트 규칙에만 의존), 에코할 원본이 없어 무변경 저장이 원천 차단된다.

LLM이 '수정 없음'(operations=[])을 택하거나 연산이 흐름도를 실제로 바꾸지 못하면(존재하지
않는 id 등) type="answer"로 정직하게 저하한다 — 가짜 성공으로 무변경 버전을 저장하지 않는다.
"""

import copy
import json
import logging
import re
from collections import defaultdict
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from app.core.llm import UsageCallbackHandler

from .. import config
from ..recommend.graph import _coerce_flow
from ..recommend.stream import emit, emit_flow_frame
from .edit_ops import EditOps, annotate_ids, apply_edit_ops, render_outline, renumber, strip_ids
from .generate import resolve_catalog_context
from .harness import attach_confidence, verify_and_repair
from .render import render_compact, render_history
from .state import TYPE_ANSWER, TYPE_RECOMMENDATION, TurnState
from .tools import build_kb_tools, describe_tool_calls, execute_tool_calls, sink_to_sources, tool_calls_data

logger = logging.getLogger(__name__)

_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "edit.md").read_text(encoding="utf-8")

# 도구 호출 왕복 상한 — 국소 수정에 충분하고 폭주를 막는다.
_MAX_TOOL_ROUNDS = 4

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")

# 연산이 흐름도를 실제로 바꾸지 못했을 때(무효과/무변경) 정직하게 내보내는 답변.
_CANT_APPLY = (
    "요청하신 변경을 흐름도에 실제로 반영하지 못했어요. 어느 단계의 무엇을 어떻게 바꿀지 "
    "조금 더 구체적으로 알려주시면 다시 시도할게요."
)


def _make_llm() -> ChatOpenAI:
    """이 노드용 ChatOpenAI를 만든다.

    stream_usage=True: UsageCallbackHandler(진입점 config)가 토큰을 집계하게 한다.
    """
    return ChatOpenAI(model=config.OPENAI_MODEL, api_key=config.OPENAI_API_KEY, stream_usage=True)


def _parse_ops(text: str) -> EditOps:
    """LLM 출력을 EditOps로 관대하게 파싱한다 — 최외곽 { } 추출 + strict=False.

    연산의 set_params 값 등에 스크립트(개행·따옴표)가 실릴 수 있어 제어문자를 허용한다.
    흐름도 전체가 아니라 작은 연산 목록이라 파손 위험 자체가 작다.
    """
    stripped = _FENCE.sub("", text.strip())
    start, end = stripped.find("{"), stripped.rfind("}")
    raw = stripped[start : end + 1] if start != -1 and end > start else stripped
    return EditOps.model_validate(json.loads(raw, strict=False))


def _canon_actions(actions: list) -> list:
    """액션 트리를 비교용 정규형으로 축약한다 — 검수가 채우는 휘발 필드(confidence/sources/
    rationale)는 빼고 package/action/label/order/parameters/children만 남긴다. 수정 전후를
    이 형태로 비교해 '실제로 무엇이 바뀌었는지'를 본다(무변경 판정)."""
    return [
        {
            "o": a.get("order"), "p": a.get("package"), "a": a.get("action"), "l": a.get("label"),
            "params": [(p.get("name"), p.get("value"), p.get("value_source")) for p in (a.get("parameters") or [])],
            # 변수 연결(v3)도 비교한다 — produces/consumes만 바꾸는 편집이 무변경으로 오판되지 않게.
            "prod": [(r.get("name"), r.get("role")) for r in (a.get("produces") or []) if isinstance(r, dict)],
            "cons": [(r.get("name"), r.get("role")) for r in (a.get("consumes") or []) if isinstance(r, dict)],
            "children": _canon_actions(a.get("children") or []),
        }
        for a in actions
    ]


def _is_noop_edit(out_flow: dict, in_flow: dict) -> bool:
    """수정 결과가 입력 흐름도와 실질적으로 동일한지 — 연산이 순효과 0인 경우까지 잡는다.

    단계 메타(step_id/label)·액션 트리(정규형)에 더해 흐름도 수준 notes/variables까지 본다 —
    set_flow만으로 메모·변수를 바꾸는 편집은 액션 트리가 그대로라, notes/variables를 빼면
    항상 무변경으로 오판돼 실패 경로로 저하된다."""
    def canon(flow: dict) -> list:
        return [
            {"id": s.get("step_id"), "l": s.get("label"), "actions": _canon_actions(s.get("actions") or [])}
            for s in (flow.get("steps") or [])
        ]
    return (
        canon(out_flow) == canon(in_flow)
        and out_flow.get("notes") == in_flow.get("notes")
        and out_flow.get("variables") == in_flow.get("variables")
    )


def _apply_to_flow(base: dict, ops: EditOps) -> tuple[dict, int, list[str]]:
    """base(원본)의 사본에 id를 붙이고 ops를 적용한 뒤, id 제거·정규화·order 재정렬한 흐름도를
    반환한다. (flow, 적용_수, 실패_사유들). base는 건드리지 않는다 — 재시도 때 같은 id를 다시
    붙일 수 있어야(프롬프트에 보여준 id와 일치) 하기 때문이다."""
    work = copy.deepcopy(base)
    annotate_ids(work)
    applied, errors = apply_edit_ops(work, ops.operations)
    strip_ids(work)
    work = _coerce_flow(work)  # 새 액션의 value_source 기본값·리스트 정규화
    renumber(work)             # 형제마다 order 1..N
    return work, applied, errors


def _restore_user_values(result_flow: dict, applied_flow: dict) -> None:
    """검수·교정(verify_and_repair)이 단계를 LLM으로 재생성하며 바꿨을 수 있는 사용자 지정 값을
    되돌린다 (rule 3의 이행 — value_source="user" 값은 명시적 변경이 아니면 보존).

    연산 적용 직후의 흐름도(applied_flow)는 손대지 않은 액션을 원본 그대로 보존하므로, 거기서
    user 파라미터를 모아 교정 결과(result_flow)에 다시 씌운다. 국소 교정은 위반 단계만
    재생성하므로 대부분 no-op이고, 재생성된 단계에서만 복원된다.

    키에 (step_id, package, action)의 '등장 순번(occ)'을 넣는다 — 한 단계에 같은 package/action
    액션이 둘 이상 있고 각자 다른 user 값을 가지면, 순번이 없으면 collect가 나중 값으로 앞 값을
    덮어써 restore가 서로 다른 입력을 하나로 뒤섞는다. 양쪽을 같은 전위 순회로 돌며 같은 순번을
    매기므로 각 액션 인스턴스의 값이 제 자리로만 복원된다."""
    saved: dict[tuple, object] = {}

    def collect(step_id, actions, occ):
        for a in actions:
            sig = (a.get("package"), a.get("action"))
            n = occ[sig]
            occ[sig] = n + 1
            for p in a.get("parameters") or []:
                if p.get("value_source") == "user":
                    saved[(step_id, sig[0], sig[1], n, p.get("name"))] = p.get("value")
            collect(step_id, a.get("children") or [], occ)

    for s in applied_flow.get("steps", []):
        collect(s.get("step_id"), s.get("actions") or [], defaultdict(int))
    if not saved:
        return

    def restore(step_id, actions, occ):
        for a in actions:
            sig = (a.get("package"), a.get("action"))
            n = occ[sig]
            occ[sig] = n + 1
            for p in a.get("parameters") or []:
                key = (step_id, sig[0], sig[1], n, p.get("name"))
                if key in saved:
                    p["value"], p["value_source"] = saved[key], "user"
            restore(step_id, a.get("children") or [], occ)

    for s in result_flow.get("steps", []):
        restore(s.get("step_id"), s.get("actions") or [], defaultdict(int))


def _build_messages(state: TurnState, outline: str, label_hint: str = "") -> list:
    """현재 흐름도 구조(노드 id 포함)·압축·이력·수정 요청을 담은 edit 프롬프트 메시지를 만든다."""
    user_content = (
        f"[현재 흐름도 구조 — 대괄호 안이 각 액션의 참조 id]\n{outline}\n\n"
        f"{label_hint}"
        f"[이전 대화 압축 요약]\n{render_compact(state.get('compact'))}\n\n"
        f"[대화 이력]\n{render_history(state.get('history'))}\n\n"
        f"[수정 요청]\n{state.get('message', '')}"
    )
    return [SystemMessage(content=_PROMPT), HumanMessage(content=user_content)]


def _label_hint_block(message: str, is_a360: bool) -> str:
    """요청 문장 속 한국어 라벨을 카탈로그에 결정론 매칭한 힌트 블록 (v3 이중 언어 리졸버).

    이름 지목형 요청("메시지 박스 넣어줘")에서 LLM이 시맨틱 검색 없이 정확한 표기를
    바로 쓰게 한다. 매칭이 없으면 빈 문자열 — 프롬프트 불변.
    """
    if not is_a360:
        return ""
    from ..verify.catalog import label_candidates

    hits = label_candidates(message)
    if not hits:
        return ""
    lines = "\n".join(f"- «{h['label']}» = {h['package']}/{h['action']}" for h in hits)
    return (
        "[이름 지목 액션 후보 — 요청 문구와 라벨이 일치하는 카탈로그 액션 (표기 그대로 사용)]\n"
        f"{lines}\n\n"
    )


# 구조를 바꾸는 연산 — 이런 수정 뒤에만 L2 시맨틱 재채점을 태운다("라벨 바꿔줘"에
# 시맨틱 채점 수십 초는 UX 배신 — 검증 심도를 수정 규모에 연동, v3 설계 §5).
_STRUCTURAL_OPS = frozenset({"wrap", "insert", "remove", "move", "split_step", "merge_step"})


async def _rescore_if_structural(flow: dict, ops: EditOps) -> None:
    """구조 연산이 있었고 spec이 동봉돼 있으면 L2 재채점으로 flow_confidence를 갱신한다.

    실패는 조용히 무시(신호 결측일 뿐) — 수정 자체의 성패와 무관하다. 제자리 갱신.
    """
    import asyncio

    spec = flow.get("spec")
    if not spec or not any(op.op in _STRUCTURAL_OPS for op in ops.operations):
        return
    try:
        from ..verify.semantic import run_semantic_check
        from .harness import compute_flow_confidence, from_violations_dicts

        emit({"event": "stage", "stage": "verifying", "message": "구조 변경 — 요구 커버리지 재채점"})
        coverage = await asyncio.to_thread(run_semantic_check, spec, flow)
        findings, _ = from_violations_dicts([])
        from ..verify import findings as F

        cov_findings = F.from_coverage(coverage)
        blocking_cards = sum(
            1 for c in flow.get("needs_input") or [] if c.get("blocking") and not c.get("resolved")
        )
        flow["flow_confidence"] = compute_flow_confidence(
            must_coverage=coverage.must_coverage,
            findings=findings + cov_findings,
            sim_pass_rate=None,
            blocking_cards=blocking_cards,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("edit 후 L2 재채점 실패 (무시): %s", e)


def _retry_message(errors: list[str], outline: str) -> str:
    """연산이 무효과였을 때, 유효 id를 다시 보여주며 실제 반영을 강제하는 재요청."""
    reason = (" — 사유: " + "; ".join(errors[:4])) if errors else ""
    return (
        f"직전 연산이 흐름도를 실제로 바꾸지 못했다{reason}. 아래 구조에 있는 노드 id만 정확히 "
        "참조해 요청한 변경을 이루는 operations를 다시 출력하라. wrap의 targets는 같은 부모의 "
        "'연속된 형제'여야 한다. 예: '엑셀 열기를 try로 감싸라'면 그 액션 id를 targets에 넣고 "
        "container=Error handler/errorHandlerTry, siblings_after에 Catch·Finally를 둔다. "
        f"설명·코드펜스 없이 JSON 하나만 출력하라.\n\n[현재 흐름도 구조]\n{outline}"
    )


async def edit_node(state: TurnState) -> dict:
    """기존 흐름도 수정 브랜치 — 툴로 카탈로그를 확인하고, 연산(EditOps)을 받아 결정론 적용한다.

    LLM이 '수정 없음'을 택하거나 연산이 흐름도를 못 바꾸면 type="answer"로 저하한다.
    반환: {turn_type, recommendation_out?, change_summary?, violations, answer, sources}.
    """
    emit({"event": "stage", "stage": "refining", "message": "흐름도 수정 중"})
    # 어휘 출처를 정한다 — a360 카탈로그 또는 대화에서 추출한 사용자 카탈로그 (RPA-285).
    # 카탈로그를 못 찾으면(None) 검수 기준이 없으니 검수를 생략하고 편집만 적용한다.
    ctx = await resolve_catalog_context(state)
    is_a360 = ctx is not None and ctx.is_a360
    sink: list[dict] = []
    tools = build_kb_tools(sink, ctx) if ctx is not None else []
    llm = _make_llm()
    runnable = llm.bind_tools(tools) if tools else llm

    # 이 노드의 LLM 호출을 purpose="turn_edit"로 기록한다(RPA-73). 노드 진입 시점에
    # 생성해 귀속 context를 스냅샷하고 모든 ainvoke config에 얹는다(최상위 콜백 없음).
    usage_config = {"callbacks": [UsageCallbackHandler(purpose="turn_edit")]}

    # 현재 흐름도에 임시 id를 붙여 아웃라인을 만든다 — LLM이 이 id로 수정 대상을 지목한다.
    base = copy.deepcopy(state.get("recommendation") or {"steps": []})
    annotated = copy.deepcopy(base)
    annotate_ids(annotated)
    outline = render_outline(annotated)

    messages = _build_messages(state, outline, _label_hint_block(state.get("message", ""), is_a360))
    response = await runnable.ainvoke(messages, config=usage_config)
    rounds = 0
    while getattr(response, "tool_calls", None) and rounds < _MAX_TOOL_ROUNDS:
        rounds += 1
        emit({"event": "stage", "stage": "searching",
              "message": describe_tool_calls(response.tool_calls),
              "data": tool_calls_data(response.tool_calls)})  # data는 관측 전용(RPA-105)
        messages.append(response)
        messages.extend(execute_tool_calls(tools, response))
        # 마지막 라운드는 도구 없이 강제 마무리 — 무한 검색을 끊는다.
        target = llm if rounds == _MAX_TOOL_ROUNDS else runnable
        response = await target.ainvoke(messages, config=usage_config)

    try:
        ops = _parse_ops(response.text)
    except (json.JSONDecodeError, ValidationError) as first_error:
        logger.warning("edit 연산 파싱 실패, 1회 교정: %s", first_error)
        messages.append(response)
        messages.append(HumanMessage(content=(
            f"위 출력이 지정한 연산 JSON 형식을 만족하지 못했습니다. 오류:\n{first_error}\n"
            "설명 없이, operations 배열을 담은 JSON 객체만 다시 출력하세요."
        )))
        response = await llm.ainvoke(messages, config=usage_config)  # 교정 턴은 도구 없이
        try:
            ops = _parse_ops(response.text)
        except (json.JSONDecodeError, ValidationError) as second_error:
            logger.warning("edit 연산 파싱 실패(교정 후에도): %s", second_error)
            return {
                "turn_type": TYPE_ANSWER,
                "answer": "요청하신 수정안을 만들지 못했어요. 어느 단계의 무엇을 바꿀지 조금 더 구체적으로 알려주시겠어요?",
                "sources": sink_to_sources(sink),
            }

    if not ops.operations:  # 수정 없음 — 질문 답변/되묻기 (type 정확성 원칙)
        return {
            "turn_type": TYPE_ANSWER,
            "answer": ops.answer or "흐름도를 바꾸지는 않았어요.",
            "sources": sink_to_sources(sink),
        }

    # 연산을 현재 흐름도에 적용한다. 아무것도 적용되지 않았거나(존재하지 않는 id 등) 순효과가
    # 없으면(무변경) 유효 id를 다시 보여주며 1회 재요청하고, 그래도 안 바뀌면 정직하게 저하한다.
    # 연산 일부만 적용되고 나머지가 실패한(errors 비어있지 않은) 경우도 '미완결'로 본다 —
    # 실패한 연산을 삼키고 applied>0을 성공으로 처리하면 사용자가 요청한 수정 일부가 조용히
    # 누락된다. 유효 id를 재안내해 1회 재요청하고, 재요청도 완결되지 않으면 정직하게 저하한다.
    flow, applied, errors = _apply_to_flow(base, ops)
    if applied == 0 or errors or _is_noop_edit(flow, base):
        logger.info("edit 연산 미완결(적용 %d, 실패 %s) — 유효 id 재안내 후 1회 재요청", applied, errors[:3])
        messages.append(response)
        messages.append(HumanMessage(content=_retry_message(errors, outline)))
        response = await llm.ainvoke(messages, config=usage_config)  # 재요청은 도구 없이
        try:
            ops = _parse_ops(response.text)
        except (json.JSONDecodeError, ValidationError) as e:
            logger.warning("edit 재요청 연산 파싱 실패: %s", e)
            return {"turn_type": TYPE_ANSWER, "answer": _CANT_APPLY, "sources": sink_to_sources(sink)}
        if not ops.operations:
            return {"turn_type": TYPE_ANSWER, "answer": ops.answer or _CANT_APPLY, "sources": sink_to_sources(sink)}
        flow, applied, errors = _apply_to_flow(base, ops)
        if applied == 0 or errors or _is_noop_edit(flow, base):
            logger.warning("edit 재요청 후에도 미완결(적용 %d, 실패 %s) — 답변으로 저하", applied, errors[:3])
            return {"turn_type": TYPE_ANSWER, "answer": _CANT_APPLY, "sources": sink_to_sources(sink)}

    # 라이브 렌더: 적용된 수정안을 즉시 프레임으로 흘려보낸다(추천 흐름도 상세 패널이 트리로 표시).
    emit_flow_frame(flow, None, "수정안 구성")
    if ctx is not None:
        result = verify_and_repair(flow, ctx.catalog)
    else:
        # 타 솔루션 세션인데 대화에서 카탈로그를 못 찾았다 — 검수 기준이 없어 생략한다
        # (A360 카탈로그로 검수하면 사용자가 준 액션이 전부 R1 위반으로 찍힌다).
        result = {"flow": flow, "violations": [], "repaired": False}

    # 교정이 위반 단계를 재생성하며 바꿨을 수 있는 사용자 지정 값(value_source="user")을 복원한다.
    _restore_user_values(result["flow"], flow)
    # 액션별 신뢰도(FR-12/RPA-116)를 RAG 소스 점수·위반으로 산정해 채운다 — 수정 경로도 동일.
    attach_confidence(result["flow"], sink, result["violations"])
    # 구조 연산이었으면 L2 재채점으로 flow_confidence 갱신 (검증 심도 ∝ 수정 규모, v3).
    await _rescore_if_structural(result["flow"], ops)
    # 라이브 렌더: 검수·교정 반영한 최종본을 "완료" 프레임으로 흘려보낸다(done 직전).
    emit_flow_frame(result["flow"], result["violations"], "완료")
    answer = ops.answer or (ops.change_summary or "요청하신 대로 흐름도를 수정했어요.")
    if ctx is None:
        # 검수를 아예 못 돌린 채 violations=[]로 내보내면 사용자는 '검수 통과'로 읽는다 —
        # 이 PR이 없애려던 조용한 오답과 같은 부류다(Qodo 리뷰). 미검수임을 밝힌다.
        logger.info("카탈로그 부재 — 검수 생략 상태로 수정안 반환")
        answer += (
            " 다만 사용 중인 솔루션의 액션 카탈로그를 찾지 못해 **이번 수정은 검수하지 "
            "않았어요** — 액션 표기나 파라미터가 실제와 맞는지 확인이 필요합니다. "
            "카탈로그를 알려주시면 검수까지 해드릴게요."
        )
    if result["violations"]:
        answer += f" (검수에서 해소하지 못한 위반 {len(result['violations'])}건이 있어요.)"
    return {
        "turn_type": TYPE_RECOMMENDATION,
        "recommendation_out": result["flow"],
        "change_summary": ops.change_summary or None,
        "violations": result["violations"],
        "answer": answer,
        "sources": sink_to_sources(sink),
    }
