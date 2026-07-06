"""Agent 오케스트레이터 (LangGraph). 백엔드는 이 패키지의 공개 진입점만 import한다.

    from app.agent import run_agent, stream_agent

    result = run_agent("질문")           # 완성 답변 한 번에
    async for token in stream_agent("질문"):  # SSE용 토큰 스트림
        ...
"""

from .graph import run_agent, stream_agent
from .schemas import AgentResult, Source

__all__ = ["AgentResult", "Source", "run_agent", "stream_agent"]
