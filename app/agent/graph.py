"""LangGraph 최소 오케스트레이터: 단일 노드(입력 메시지 → LLM 호출 → 답변).

RAG 검색 연동·멀티 노드 분기·프롬프트 고도화는 후속 이슈 범위다.
"""

from collections.abc import AsyncIterator
from typing import TypedDict

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from . import config
from .schemas import AgentResult


class AgentState(TypedDict):
    """그래프 상태. 노드가 늘어나면 중간 산출물(검색 결과 등) 필드를 여기에 추가한다."""

    message: str
    answer: str


def _call_llm(state: AgentState) -> dict:
    llm = ChatOpenAI(model=config.OPENAI_MODEL, api_key=config.OPENAI_API_KEY)
    response = llm.invoke([HumanMessage(content=state["message"])])
    return {"answer": response.text}


def build_graph():
    """단일 노드 그래프를 컴파일해 반환한다."""
    graph = StateGraph(AgentState)
    graph.add_node("llm", _call_llm)
    graph.add_edge(START, "llm")
    graph.add_edge("llm", END)
    return graph.compile()


_graph = None


def _get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def _require_api_key() -> None:
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY 환경변수가 필요합니다")


def run_agent(message: str) -> AgentResult:
    """비스트리밍 진입점: 완성된 답변을 한 번에 반환한다. 키 미설정 시 RuntimeError."""
    _require_api_key()
    result = _get_graph().invoke({"message": message})
    return AgentResult(answer=result["answer"])


async def stream_agent(message: str) -> AsyncIterator[str]:
    """SSE용 async 진입점: LLM 답변 토큰을 생성되는 대로 yield한다. 키 미설정 시 RuntimeError.

    stream_mode="messages"가 노드 안 LLM 호출의 토큰을 콜백으로 흘려주므로
    그래프·노드는 run_agent와 동일한 것을 그대로 쓴다.
    """
    _require_api_key()
    async for chunk, _meta in _get_graph().astream(
        {"message": message}, stream_mode="messages"
    ):
        if chunk.text:
            yield chunk.text
