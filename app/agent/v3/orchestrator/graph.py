"""오케스트레이터 그래프 배선과 단일 진입점 stream_agent_turn (v3 — Planner–Executor).

    START ─(operation)─┬─ compact ────────────────────────────────▶ END
                       ├─ fill_cards (카드 응답 결정론 반영) ────────▶ END
                       └─ intake (TaskPlan: LLM 1회) ─▶ supervisor ⇄ analyze/generate/edit/qa
                                                            └─(plan 소진)─▶ respond ─▶ END

v2의 "intake 4분류 1회 → 고정 배선"을 일반화했다: intake가 순서 있는 task 목록(plan)을
내고, supervisor(결정론 디스패처)가 순회한다 — 복합 요청("분석하고 만들어줘, 그리고 질문")
을 한 턴에 처리한다. 계획은 LLM이 1회, 순회는 결정론 — 매 스텝 LLM 재계획의 비결정성을
들이지 않는다. 재계획은 "선행 산출물이 후행 전제를 깨뜨린 경우"(분석 결과 단계 0건 →
generate 제거)뿐이며 그것도 결정론 규칙이다.

turn_type 불변식은 유지·일반화된다: 각 task의 산출은 artifacts에 실행 사실로 쌓이고,
최상위 type은 respond가 우선순위 규칙(recommendation > analysis > compact > answer)으로
결정론 산출한다. done data는 기존 필드를 병행 유지해(백엔드 저장 분기 무변경) 하위호환된다.
"""

import logging
from collections.abc import AsyncIterator

from langgraph.graph import END, START, StateGraph

from app.schemas import ProgressEvent

from .. import config
from .cards import fill_cards_node
from .compact import compact_node
from .edit import edit_node
from .generate import analyze_node, generate_node
from .intake import intake_node
from .qa import qa_node
from .state import (
    ROUTE_ANALYZE,
    ROUTE_GENERATE,
    TYPE_ANALYSIS,
    TYPE_ANSWER,
    TYPE_COMPACT,
    TYPE_RECOMMENDATION,
    TurnState,
)

logger = logging.getLogger(__name__)

# respond의 최상위 type 우선순위 — 복수 산출 시 저장 가치가 큰 쪽이 대표 type이 된다.
_TYPE_PRIORITY = (TYPE_RECOMMENDATION, TYPE_ANALYSIS, TYPE_COMPACT, TYPE_ANSWER)

_TASK_NODES = ("analyze", "generate", "edit", "qa")


def _route_entry(state: TurnState) -> str:
    """버튼발 결정론 요청(operation)은 LLM 라우터를 우회한다 — compact/fill_cards."""
    op = state.get("operation")
    if op == "compact":
        return "compact"
    if op == "fill_cards":
        return "fill_cards"
    return "intake"


def _snapshot_artifact(task: str, state: TurnState) -> dict:
    """방금 끝난 task의 산출을 실행 사실 그대로 스냅샷한다 (turn_type 불변식의 일반화)."""
    return {
        "task": task,
        "type": state.get("turn_type") or TYPE_ANSWER,
        "answer": state.get("answer") or "",
        "sources": state.get("sources") or [],
    }


def supervisor_node(state: TurnState) -> dict:
    """결정론 디스패처 — plan 큐를 순회하며 다음 task를 정한다 (LLM 없음).

    - 직전 task의 산출을 artifacts에 스냅샷.
    - generate인데 분석본이 없으면 plan을 소비하지 않고 analyze를 경유(v2 직렬 연결의 일반화).
    - 결정론 재계획: analyze가 실행됐는데 단계가 0건이면 generate를 계획에서 제거
      (자동화 대상이 없는데 흐름도를 억지로 만들지 않는다).
    """
    artifacts = list(state.get("artifacts") or [])
    cur = state.get("current_task")
    if cur:
        artifacts.append(_snapshot_artifact(cur, state))

    plan = list(state.get("plan") or [])
    if cur == ROUTE_ANALYZE and not (state.get("analysis") or {}).get("steps"):
        if ROUTE_GENERATE in plan:
            plan = [t for t in plan if t != ROUTE_GENERATE]
            logger.info("supervisor: 분석 단계 0건 — generate를 계획에서 제거")

    if not plan:
        return {"artifacts": artifacts, "current_task": "", "next_node": "respond", "plan": []}

    task = plan[0]
    if task == ROUTE_GENERATE and not (state.get("analysis") or {}).get("steps"):
        # 분석 선행 경유 — plan 미소비: analyze가 끝나면 다시 여기로 와 generate를 집는다.
        return {"artifacts": artifacts, "current_task": ROUTE_ANALYZE, "next_node": "analyze", "plan": plan}
    return {"artifacts": artifacts, "current_task": task, "next_node": task, "plan": plan[1:]}


def _route_from_supervisor(state: TurnState) -> str:
    return state.get("next_node") or "respond"


def respond_node(state: TurnState) -> dict:
    """턴 요약 조립 — artifacts에서 최상위 type(우선순위 규칙)과 통합 answer를 결정론 산출."""
    artifacts = state.get("artifacts") or []
    types = {a.get("type") for a in artifacts}
    turn_type = next((t for t in _TYPE_PRIORITY if t in types), state.get("turn_type") or TYPE_ANSWER)

    answers: list[str] = []
    for a in artifacts:
        ans = (a.get("answer") or "").strip()
        if ans and ans not in answers:
            answers.append(ans)
    answer = "\n\n".join(answers) or (state.get("answer") or "")

    seen_titles: set[str] = set()
    sources: list[dict] = []
    for a in artifacts:
        for s in a.get("sources") or []:
            title = s.get("title") or ""
            if title and title in seen_titles:
                continue
            seen_titles.add(title)
            sources.append(s)

    return {"turn_type": turn_type, "answer": answer, "sources": sources, "artifacts": artifacts}


def build_graph():
    g = StateGraph(TurnState)
    g.add_node("intake", intake_node)
    g.add_node("supervisor", supervisor_node)
    g.add_node("respond", respond_node)
    g.add_node("analyze", analyze_node)
    g.add_node("generate", generate_node)
    g.add_node("edit", edit_node)
    g.add_node("qa", qa_node)
    g.add_node("compact", compact_node)
    g.add_node("fill_cards", fill_cards_node)

    g.add_conditional_edges(START, _route_entry, ["intake", "compact", "fill_cards"])
    g.add_edge("intake", "supervisor")
    g.add_conditional_edges("supervisor", _route_from_supervisor, [*_TASK_NODES, "respond"])
    for node in _TASK_NODES:
        g.add_edge(node, "supervisor")
    g.add_edge("respond", END)
    g.add_edge("compact", END)
    g.add_edge("fill_cards", END)
    return g.compile()


_graph = None


def _get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def _to_inputs(message: str, context: dict) -> TurnState:
    """백엔드 context(RPA-64) → 그래프 입력. operation/compact/card_values는 없으면 기본값."""
    return {
        "message": message,
        "solution": context.get("solution") or "a360",
        "operation": context.get("operation") or "chat",
        "history": context.get("history") or [],
        "compact": context.get("compact"),
        "analysis": context.get("analysis"),
        "recommendation": context.get("recommendation"),
        "parsed_doc": context.get("parsed_doc"),
        "card_values": context.get("card_values") or {},
    }


def _done_data(state: dict) -> dict:
    """최종 상태 → done 이벤트 data (판별 유니온 — 백엔드가 type으로 저장 분기).

    기존 필드는 그대로 유지(하위호환)하고, v3의 task별 산출 목록(artifacts)을 덧붙인다 —
    프론트·관측이 복합 턴의 내역을 볼 수 있게. artifacts의 sources는 상위 sources와
    중복이라 뺀다(payload 절약).
    """
    return {
        "type": state.get("turn_type") or TYPE_ANSWER,
        "answer": state.get("answer") or "",
        "sources": state.get("sources") or [],
        "analysis_result": state.get("analysis_out"),
        "updated_recommendation": state.get("recommendation_out"),
        "change_summary": state.get("change_summary"),
        "compact": state.get("compact_out"),
        "violations": state.get("violations") or [],
        # 세션 solution 확정 신호 (RPA-285) — 없으면 None이라 기존 소비자에 영향 없다.
        "detected_solution": state.get("detected_solution"),
        "artifacts": [
            {"task": a.get("task"), "type": a.get("type"), "answer": a.get("answer")}
            for a in (state.get("artifacts") or [])
        ],
    }


async def stream_agent_turn(message: str, context: dict) -> AsyncIterator[ProgressEvent]:
    """단일 진입점: 한 메시지로 분석/산출/수정/질문/압축/카드반영을 처리한다 (RPA-64 계약).

    context: {solution, history, analysis, recommendation, parsed_doc}
             (+ operation: "chat"|"compact"|"fill_cards", compact, card_values).
    yield: stage/partial/token → done(data={type, answer, sources, analysis_result,
           updated_recommendation, change_summary, artifacts, ...}). 실패는 error 이벤트.

    Agent는 stateless — 저장·버전·이력은 백엔드 몫. LLM 사용량 purpose는 노드별로
    나뉜다(intake="intake"로 링 게이지 측정, generate="turn_generate", qa/edit는 자체
    콜백). 이 진입점 최상위 config에는 usage 콜백을 두지 않는다(이중 기록 방지).
    """
    if not config.OPENAI_API_KEY:
        yield ProgressEvent(event="error", message="OPENAI_API_KEY 환경변수가 필요합니다")
        return

    final_state: dict = {}
    try:
        async for mode, chunk in _get_graph().astream(
            _to_inputs(message, context),
            stream_mode=["custom", "values"],
            config={"max_concurrency": config.MAX_LLM_CONCURRENCY, "recursion_limit": 30},
        ):
            if mode == "custom":
                yield ProgressEvent(**chunk)
            elif mode == "values":
                final_state = chunk
    except RuntimeError as e:  # 인프라(키/인증/rate limit) — 사용자용 문구 그대로
        yield ProgressEvent(event="error", message=str(e))
        return
    except Exception:  # noqa: BLE001 — 예기치 못한 실패도 스트림을 죽이지 않는다
        # 원인은 서버 로그로만 남기고 내부 정보(경로·라이브러리 메시지)는 노출하지 않는다.
        logger.exception("agent turn 실패")
        yield ProgressEvent(event="error", message="응답 생성 중 오류가 발생했습니다")
        return

    yield ProgressEvent(event="done", stage="agent", data=_done_data(final_state))
