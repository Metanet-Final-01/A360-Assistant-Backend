"""LangGraph 오케스트레이터: retrieve(KB 검색) → generate(근거 기반 답변) 2노드 그래프.

검색은 retrieval.Retriever에 위임한다 — 현재는 스텁(FakeRetriever)이고, 실제
pgvector·하이브리드 검색은 RAG 담당 모듈 완성 후 retrieval.py에서 교체한다.
멀티턴은 백엔드가 이력을 저장하고 호출 시 history로 주입하며 Agent는 stateless를
유지한다(run_agent/stream_agent의 history 파라미터). 구조화 추천·검수 루프는 후속 이슈 범위다.
"""

from collections.abc import AsyncIterator
from typing import TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
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
    history: list[dict]  # 이전 대화 턴 [{"role": "user"|"assistant", "content": str}]
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


# 백엔드가 주입하는 대화 이력의 role → LangChain 메시지 타입.
_ROLE_TO_MESSAGE = {"user": HumanMessage, "assistant": AIMessage}


def _history_messages(history: list[dict] | None) -> list:
    """백엔드가 넘긴 이력([{"role", "content"}])을 LLM 메시지로 변환한다.

    Agent는 stateless라 이력을 보관하지 않고 호출 시 받은 것만 그대로 쓴다.
    아는 role(user/assistant)만 싣고 나머지는 건너뛴다 — 잘못된 입력에 죽지 않도록.
    """
    messages = []
    for turn in history or []:
        message_cls = _ROLE_TO_MESSAGE.get(turn.get("role"))
        if message_cls is None:
            continue
        messages.append(message_cls(content=turn.get("content", "")))
    return messages


def _build_messages(state: AgentState) -> list:
    """LLM 입력 메시지: 시스템(프롬프트+근거) → 이전 턴 → 현재 질문 순."""
    return [
        SystemMessage(content=f"{_SYSTEM_PROMPT}\n\n{_format_context(state['docs'])}"),
        *_history_messages(state.get("history")),
        HumanMessage(content=state["message"]),
    ]


def _generate(state: AgentState) -> dict:
    llm = ChatOpenAI(model=config.OPENAI_MODEL, api_key=config.OPENAI_API_KEY)
    response = llm.invoke(_build_messages(state))
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


def run_agent(message: str, history: list[dict] | None = None) -> AgentResult:
    """비스트리밍 진입점: 근거(sources)를 포함한 완성 답변을 반환한다. 키 미설정 시 RuntimeError.

    history: 백엔드가 주입하는 이전 대화 턴 [{"role": "user"|"assistant", "content": str}].
    없으면 단발 질의응답(기존 동작)과 동일하다 — Agent는 이력을 저장하지 않는다(stateless).
    """
    _require_api_key()
    result = _get_graph().invoke({"message": message, "history": history or []})
    return AgentResult(answer=result["answer"], sources=_to_sources(result["docs"]))


async def stream_agent(
    message: str, history: list[dict] | None = None
) -> AsyncIterator[str]:
    """SSE용 async 진입점: LLM 답변 토큰을 생성되는 대로 yield한다. 키 미설정 시 RuntimeError.

    history는 run_agent와 동일한 형태이며, 없으면 단발 질의응답과 동일하다.

    stream_mode="messages"가 노드 안 LLM 호출의 토큰을 콜백으로 흘려주므로
    그래프·노드는 run_agent와 동일한 것을 그대로 쓴다. retrieve 노드는 LLM을
    호출하지 않아 스트림에 섞이지 않는다. sources는 토큰 스트림에 싣지 않는다
    (SSE 이벤트 설계는 후속 이슈에서 백엔드와 협의).
    """
    _require_api_key()
    async for chunk, _meta in _get_graph().astream(
        {"message": message, "history": history or []}, stream_mode="messages"
    ):
        if chunk.text:
            yield chunk.text
