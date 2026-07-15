"""Agent 오케스트레이터 (LangGraph). 백엔드는 이 패키지의 공개 진입점만 import한다.

    from app.agent import stream_agent_turn          # 단일 진입점 (RPA-64/65)
    async for event in stream_agent_turn(message, context):  # done.data.type으로 저장 분기
        ...

    from app.agent import analyze, recommend
    analysis = analyze(parsed_doc)                  # 문서 분석 (FR-05)
    async for event in recommend(analysis):         # 추천 스트림 (FR-09~12)
        ...

구 챗 진입점 run_agent/stream_agent(retrieve→generate)는 orchestrator의 qa 노드로
대체돼 삭제됐다 (RPA-76). 대화형 상호작용은 전부 stream_agent_turn 하나로 들어온다.
"""

from .analysis import analyze
from .orchestrator import stream_agent_turn
from .recommend import recommend

__all__ = [
    "analyze",
    "recommend",
    "stream_agent_turn",
]
