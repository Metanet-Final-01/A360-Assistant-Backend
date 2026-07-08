"""recommend 그래프 배선과 공개 진입점.

    plan ──Send(단계별 병렬)──▶ step(shortlist→compose→check) ──▶ assemble ──▶ END

step은 단일 노드 안에서 shortlist→compose→check를 순차 실행한다(서브그래프 대신) —
Send로 단계마다 병렬 인스턴스가 뜨고, 결과는 operator.add로 step_results에 합쳐진다.
sync 노드라 astream이 executor로 돌려 이벤트 루프를 막지 않는다. 진행 이벤트는
get_stream_writer(custom 모드)로, 최종 Recommendation은 values 모드로 뽑아 done에 싣는다.

범위(RPA-27a): 코어 시퀀스 생성 + R1~R6 정적 검수. 검수 위반 기반 repair 루프와
단계 간 dryrun(R7~R8)은 후속(RPA-27b).
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.schemas import ProgressEvent

from .. import config
from ..verify.catalog import get_catalog
from .assemble import assemble_node
from .check import check
from .compose import compose
from .plan import fan_out, plan_node
from .shortlist import shortlist
from .state import RecommendState, new_step_result
from .stream import emit

logger = logging.getLogger(__name__)

# 동시 LLM 호출 상한 (rate limit 방어). 병렬 단계가 많아도 이만큼씩만 돈다.
_MAX_CONCURRENCY = 3


def step_node(state: dict) -> dict:
    """한 업무 단계 → 액션 시퀀스. shortlist→compose→check를 순차 실행한다.

    parse 실패(ValueError)는 그 단계만 빈 결과로 저하시키고 계속 진행한다.
    인프라 실패(RuntimeError: 키 미설정·인증·rate limit)는 그대로 올려
    recommend()가 단일 error 이벤트로 처리하게 한다.
    """
    step = state["step"]
    order = state.get("order", 0)
    constraints = state.get("constraints") or []
    step_id = step.get("step_id") or f"step-{order}"

    emit({"event": "stage", "stage": "searching",
          "message": f"[{step.get('name') or step_id}] 관련 A360 액션 검색 중"})

    sl = shortlist(step, constraints)
    try:
        out = compose(step, sl, constraints)
        actions = [a.model_dump() for a in out.actions]
        variables_used = out.variables_used
        needs_input, gaps, notes = out.needs_input, out.gaps, out.notes_candidates
    except ValueError as e:  # 스키마 파싱 실패(교정 후에도) — 이 단계만 저하
        logger.warning("step %s compose 실패: %s", step_id, e)
        actions, variables_used = [], []
        needs_input, gaps, notes = [], [f"{step_id} 추천 생성 실패: {e}"], []

    catalog = get_catalog()
    chk = check(actions, catalog, sl.get("source_map", {}))

    emit({"event": "partial", "data": {"step_id": step_id, "actions": chk["actions"]}})

    result = new_step_result(
        step_id=step_id, order=order, actions=chk["actions"],
        variables_used=variables_used, needs_input=needs_input, gaps=gaps,
        notes_candidates=notes, confidence=chk["confidence"], violations=chk["violations"],
    )
    return {"step_results": [result]}


def route_plan(state: RecommendState):
    """단계가 있으면 병렬 fan-out, 없으면 곧장 assemble로 (빈 추천안)."""
    sends = fan_out(state)
    return sends or "assemble"


def build_graph():
    g = StateGraph(RecommendState)
    g.add_node("plan", plan_node)
    g.add_node("step", step_node)
    g.add_node("assemble", assemble_node)
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", route_plan, ["step", "assemble"])
    g.add_edge("step", "assemble")
    g.add_edge("assemble", END)
    return g.compile()


_graph = None


def _get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def _to_dict(analysis: Any) -> dict:
    """AnalysisResult(Pydantic) 또는 dict를 상태용 dict로 정규화한다."""
    if hasattr(analysis, "model_dump"):
        return analysis.model_dump()
    return dict(analysis)


async def recommend(
    analysis: Any, constraints: list[str] | None = None
) -> AsyncIterator[ProgressEvent]:
    """AnalysisResult → A360 추천안(Recommendation) 스트림 (INTERFACES §4 ②).

    yield: stage(recommending/searching) → partial(step별 결과) → done(recommendation).
    OPENAI_API_KEY 미설정·인증·rate limit 등은 error 이벤트로 흘린다(백엔드가 SSE로 전달).
    Agent는 stateless — 저장·버전은 백엔드 몫이고 이 함수는 입력→산출물만 책임진다.
    """
    if not config.OPENAI_API_KEY:
        yield ProgressEvent(event="error", message="OPENAI_API_KEY 환경변수가 필요합니다")
        return

    inputs: RecommendState = {
        "analysis": _to_dict(analysis),
        "constraints": constraints or [],
    }
    final_state: dict = {}
    try:
        async for mode, chunk in _get_graph().astream(
            inputs, stream_mode=["custom", "values"],
            config={"max_concurrency": _MAX_CONCURRENCY},
        ):
            if mode == "custom":
                yield ProgressEvent(**chunk)
            elif mode == "values":
                final_state = chunk
    except RuntimeError as e:  # 인프라(키/인증/rate limit) — 사용자용 문구로
        yield ProgressEvent(event="error", message=str(e))
        return
    except Exception:  # noqa: BLE001 — 예기치 못한 실패도 스트림을 죽이지 않는다
        # 원인은 서버 로그로만 남기고, 사용자에겐 내부 정보(경로·라이브러리 메시지)를 노출하지 않는다.
        logger.exception("recommend 실패")
        yield ProgressEvent(event="error", message="추천 생성 중 오류가 발생했습니다")
        return

    yield ProgressEvent(
        event="done",
        data={"recommendation": final_state.get("recommendation")},
    )
