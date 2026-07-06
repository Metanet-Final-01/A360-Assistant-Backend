"""LangGraph 오케스트레이터: retrieve(KB 검색) → generate(근거 기반 답변) 2노드 그래프.

검색은 retrieval.Retriever에 위임한다 — 현재는 스텁(FakeRetriever)이고, 실제
pgvector·하이브리드 검색은 RAG 담당 모듈 완성 후 retrieval.py에서 교체한다.
구조화 추천·검수 루프·멀티턴 메모리는 후속 이슈 범위다.
"""

from collections.abc import AsyncIterator
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from app.schemas import RagSource

from . import config
from .retrieval import get_retriever
from .schemas import AgentResult

_SYSTEM_PROMPT = """\
당신은 Automation Anywhere Automation 360(A360) 작업 추천 어시스턴트다.
사용자의 업무 설명에 맞는 A360 패키지·액션과 구성 방법을 안내한다.

규칙:
- 아래 [근거 문서]에 있는 내용만 사실로 사용한다. 근거에 없는 패키지·액션·옵션 이름을 지어내지 않는다.
- 근거 문서로 답할 수 없는 질문에는 현재 지식베이스로 확인할 수 없다고 밝힌 뒤, 일반적인 방향만 조심스럽게 제안한다.
- 답변에 사용한 근거 문서의 제목을 함께 언급한다.
- 한국어로 간결하게 답한다."""


class AgentState(TypedDict):
    """그래프 상태. 노드가 늘어나면 중간 산출물 필드를 여기에 추가한다."""

    message: str
    docs: list[dict]
    answer: str


def _retrieve(state: AgentState) -> dict:
    docs = get_retriever().search(state["message"])
    return {"docs": docs}


def _format_context(docs: list[dict]) -> str:
    if not docs:
        return "[근거 문서]\n(검색된 문서 없음 — 근거 없이 단정하지 말 것)"
    blocks = [
        f"{i}. {doc['title']} (패키지: {doc.get('package_name') or '-'})\n{doc['content']}"
        for i, doc in enumerate(docs, start=1)
    ]
    return "[근거 문서]\n" + "\n\n".join(blocks)


def _generate(state: AgentState) -> dict:
    llm = ChatOpenAI(model=config.OPENAI_MODEL, api_key=config.OPENAI_API_KEY)
    response = llm.invoke(
        [
            SystemMessage(content=f"{_SYSTEM_PROMPT}\n\n{_format_context(state['docs'])}"),
            HumanMessage(content=state["message"]),
        ]
    )
    return {"answer": response.text}


def build_graph():
    """retrieve → generate 그래프를 컴파일해 반환한다."""
    graph = StateGraph(AgentState)
    graph.add_node("retrieve", _retrieve)
    graph.add_node("generate", _generate)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "generate")
    graph.add_edge("generate", END)
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


def _to_sources(docs: list[dict]) -> list[RagSource]:
    return [
        RagSource(
            source_type=doc["source_type"],
            title=doc["title"],
            url=doc.get("url"),
            score=doc["score"],
        )
        for doc in docs
    ]


def run_agent(message: str) -> AgentResult:
    """비스트리밍 진입점: 근거(sources)를 포함한 완성 답변을 반환한다. 키 미설정 시 RuntimeError."""
    _require_api_key()
    result = _get_graph().invoke({"message": message})
    return AgentResult(answer=result["answer"], sources=_to_sources(result["docs"]))


async def stream_agent(message: str) -> AsyncIterator[str]:
    """SSE용 async 진입점: LLM 답변 토큰을 생성되는 대로 yield한다. 키 미설정 시 RuntimeError.

    stream_mode="messages"가 노드 안 LLM 호출의 토큰을 콜백으로 흘려주므로
    그래프·노드는 run_agent와 동일한 것을 그대로 쓴다. retrieve 노드는 LLM을
    호출하지 않아 스트림에 섞이지 않는다. sources는 토큰 스트림에 싣지 않는다
    (SSE 이벤트 설계는 후속 이슈에서 백엔드와 협의).
    """
    _require_api_key()
    async for chunk, _meta in _get_graph().astream(
        {"message": message}, stream_mode="messages"
    ):
        if chunk.text:
            yield chunk.text
