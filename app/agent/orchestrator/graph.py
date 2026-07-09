"""오케스트레이터 그래프 배선과 단일 진입점 stream_agent_turn (RPA-65 / 백엔드 RPA-64 계약).

    START ─(operation)─┬─ compact ────────────────────────────────▶ END
                       └─ intake ─┬─ analyze ─┬─(route=generate)─▶ generate ─▶ END
                                  │           └──────────────────────────────▶ END
                                  ├─ generate(분석본 있음) ────────────────────▶ END
                                  ├─ edit ────────────────────────────────────▶ END
                                  └─ qa ──────────────────────────────────────▶ END

analyze→generate는 직렬 파이프라인이고 intake의 route가 종착점을 정한다.
turn_type은 각 브랜치 노드가 실행 사실로 찍으므로(LLM 무관) 백엔드 저장 분기와
항상 일치한다. 진행 이벤트는 노드들이 emit(custom 스트림)으로 흘리고, 이 진입점이
ProgressEvent로 감싸 yield한 뒤 마지막에 done(data=판별 유니온)을 싣는다.
"""

import logging
from collections.abc import AsyncIterator

from langgraph.graph import END, START, StateGraph

from app.core.llm import UsageCallbackHandler
from app.schemas import ProgressEvent

from .. import config
from .compact import compact_node
from .edit import edit_node
from .generate import analyze_node, generate_node
from .intake import intake_node
from .qa import qa_node
from .state import (
    ROUTE_ANALYZE,
    ROUTE_GENERATE,
    TYPE_ANSWER,
    TurnState,
)

logger = logging.getLogger(__name__)

# 그래프 전체 동시 LLM 호출 상한 (recommend 서브그래프 병렬 단계 포함, rate limit 방어).
_MAX_CONCURRENCY = 3


def _route_entry(state: TurnState) -> str:
    """compact 버튼(operation)은 결정론 요청 — LLM 라우터를 우회한다."""
    return "compact" if state.get("operation") == "compact" else "intake"


def _route_from_intake(state: TurnState) -> str:
    route = state.get("route") or "qa"
    if route == ROUTE_ANALYZE:
        return "analyze"
    if route == ROUTE_GENERATE:
        # 분석본 없는 산출 요청은 analyze를 경유해 한 턴에 이어 달린다
        # (analysis_out도 함께 반환 — 백엔드가 분석본을 유실하지 않게).
        return "generate" if (state.get("analysis") or {}).get("steps") else "analyze"
    return route  # "edit" | "qa" — 노드 이름과 동일


def _route_after_analyze(state: TurnState) -> str:
    """intake가 generate를 원했으면 이어 달리고, analyze가 종착이면 끝낸다."""
    return "generate" if state.get("route") == ROUTE_GENERATE else END


def build_graph():
    g = StateGraph(TurnState)
    g.add_node("intake", intake_node)
    g.add_node("analyze", analyze_node)
    g.add_node("generate", generate_node)
    g.add_node("edit", edit_node)
    g.add_node("qa", qa_node)
    g.add_node("compact", compact_node)

    g.add_conditional_edges(START, _route_entry, ["intake", "compact"])
    g.add_conditional_edges("intake", _route_from_intake, ["analyze", "generate", "edit", "qa"])
    g.add_conditional_edges("analyze", _route_after_analyze, ["generate", END])
    g.add_edge("generate", END)
    g.add_edge("edit", END)
    g.add_edge("qa", END)
    g.add_edge("compact", END)
    return g.compile()


_graph = None


def _get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def _to_inputs(message: str, context: dict) -> TurnState:
    """백엔드 context(RPA-64) → 그래프 입력. operation/compact는 확장 제안분 — 없으면 기본값."""
    return {
        "message": message,
        "solution": context.get("solution") or "a360",
        "operation": context.get("operation") or "chat",
        "history": context.get("history") or [],
        "compact": context.get("compact"),
        "analysis": context.get("analysis"),
        "recommendation": context.get("recommendation"),
        "parsed_doc": context.get("parsed_doc"),
    }


def _done_data(state: dict) -> dict:
    """최종 상태 → done 이벤트 data (판별 유니온 — 백엔드가 type으로 저장 분기)."""
    return {
        "type": state.get("turn_type") or TYPE_ANSWER,
        "answer": state.get("answer") or "",
        "sources": state.get("sources") or [],
        "analysis_result": state.get("analysis_out"),
        "updated_recommendation": state.get("recommendation_out"),
        "change_summary": state.get("change_summary"),
        "compact": state.get("compact_out"),
        "violations": state.get("violations") or [],
    }


async def stream_agent_turn(message: str, context: dict) -> AsyncIterator[ProgressEvent]:
    """단일 진입점: 한 메시지로 분석/산출/수정/질문/압축을 처리한다 (백엔드 RPA-64 계약).

    context: {solution, history, analysis, recommendation, parsed_doc}
             (+ 확장 제안분 operation, compact — 없으면 일반 대화로 동작).
    yield: stage/partial/token → done(data={type, answer, sources, analysis_result,
           updated_recommendation, change_summary, ...}). 실패는 error 이벤트로 흘린다.

    Agent는 stateless — 저장·버전·이력은 백엔드 몫. LLM 사용량은 core.llm.chat(구조화
    호출, intake는 purpose="intake"로 링 게이지 측정 대상)과 UsageCallbackHandler
    (qa·edit의 ChatOpenAI 호출)가 빠짐없이 기록한다.
    """
    if not config.OPENAI_API_KEY:
        yield ProgressEvent(event="error", message="OPENAI_API_KEY 환경변수가 필요합니다")
        return

    final_state: dict = {}
    try:
        async for mode, chunk in _get_graph().astream(
            _to_inputs(message, context),
            stream_mode=["custom", "values"],
            config={
                "callbacks": [UsageCallbackHandler(purpose="turn")],
                "max_concurrency": _MAX_CONCURRENCY,
            },
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
