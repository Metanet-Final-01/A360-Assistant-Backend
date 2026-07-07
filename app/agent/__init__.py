"""Agent 오케스트레이터 (LangGraph). 백엔드는 이 패키지의 공개 진입점만 import한다.

    from app.agent import run_agent, stream_agent

    result = run_agent("질문")           # 완성 답변 한 번에
    async for token in stream_agent("질문"):  # SSE용 토큰 스트림
        ...

    from app.agent import analyze, recommend
    analysis = analyze(parsed_doc)                  # 문서 분석 (FR-05)
    async for event in recommend(analysis):         # 추천 스트림 (FR-09~12)
        ...
"""

from .analysis import analyze
from .graph import run_agent, stream_agent
from .recommend import recommend
from .schemas import AgentResult

__all__ = ["AgentResult", "analyze", "recommend", "run_agent", "stream_agent"]
