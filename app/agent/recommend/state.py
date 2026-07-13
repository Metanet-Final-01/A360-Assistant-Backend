"""recommend 에이전트 그래프의 상태 정의.

에이전트형 흐름도 생성(compose_agent ⇄ tools → verify → finalize)의 공유 상태다.
messages는 add_messages 리듀서로 누적돼 ReAct 툴 루프와 검수 self-repair 대화를
이어간다. tool_rounds/repair_round는 무한 루프 방지용 예산 카운터다.
"""

from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class RecommendState(TypedDict, total=False):
    """recommend 에이전트 그래프 상태."""

    analysis: dict  # AnalysisResult.model_dump() — 단계는 '고정 경계'가 아니라 힌트로 쓴다
    constraints: list[str]

    messages: Annotated[list, add_messages]  # 에이전트 대화(system·user·AI·tool)
    tool_rounds: int  # compose_agent가 도구를 부른 왕복 수 (MAX_TOOL_ROUNDS 상한)
    repair_round: int  # verify → 재작성 반복 수 (MAX_REPAIR_ROUNDS 상한)

    flow: dict | None  # 검수 통과/교정된 흐름도 후보 (Recommendation 호환 dict)
    violations: list[dict]  # 잔여 검수 위반 (finalize까지 전달 → 사용자 표시)
    recommendation: dict | None  # 최종 Recommendation.model_dump()
