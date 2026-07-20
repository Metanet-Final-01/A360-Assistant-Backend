"""recommend 그래프의 상태 정의.

병렬 단계 처리(Send)의 결과는 step_results에 리듀서(operator.add)로 누적된다 —
각 단계 노드가 [draft] 하나를 반환하면 병렬 브랜치들이 합쳐진다. 도착 순서는
보장되지 않으므로 소비 측(assemble)은 step order로 재정렬한다.
"""

import operator
from typing import Annotated, Any, TypedDict


class RecommendState(TypedDict, total=False):
    """그래프 최상위 상태."""

    analysis: dict  # AnalysisResult.model_dump()
    constraints: list[str]
    step_results: Annotated[list[dict], operator.add]  # 단계별 산출 dict 누적
    recommendation: dict | None  # Recommendation.model_dump()


class StepPayload(TypedDict, total=False):
    """plan이 Send로 각 단계 노드에 넘기는 입력.

    step은 WorkStep.model_dump(), order는 재정렬용 원 순서.
    """

    step: dict
    order: int
    constraints: list[str]


def new_step_result(
    *,
    step_id: str,
    order: int,
    actions: list[dict],
    variables_used: list[dict],
    needs_input: list[str],
    gaps: list[str],
    notes_candidates: list[str],
    confidence: float,
    violations: list[dict],
) -> dict[str, Any]:
    """단계 노드가 step_results에 싣는 표준 dict를 만든다 (assemble 계약)."""
    return {
        "step_id": step_id,
        "order": order,
        "actions": actions,
        "variables_used": variables_used,
        "needs_input": needs_input,
        "gaps": gaps,
        "notes_candidates": notes_candidates,
        "confidence": confidence,
        "violations": violations,
    }
