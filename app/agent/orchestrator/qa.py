"""qa — 일반 질문 답변 브랜치 노드 (툴 호출 에이전트, RPA-65).

고정 retrieve→generate(기존 graph.py)를 대체한다: LLM이 필요하다고 판단할 때만
search_kb/get_action_schema를 호출한다 — 인사말·방금 만든 흐름도에 대한 질문은
검색 없이 세션 컨텍스트로 답한다. 유일하게 답변을 token 이벤트로 스트리밍하는
노드다(그래프 전체 messages 스트림을 쓰면 intake·compose 토큰까지 새므로, 이
노드가 자체 astream으로 방출을 통제한다).
"""

import logging
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.core.llm import UsageCallbackHandler

from .. import config
from ..graph import _history_messages
from ..recommend.stream import emit
from .render import analysis_brief, context_signals, flow_outline, render_compact
from .state import TYPE_ANSWER, TurnState
from .tools import build_kb_tools, execute_tool_calls, sink_to_sources

logger = logging.getLogger(__name__)

_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "qa.md").read_text(encoding="utf-8")

# 도구 호출 왕복 상한. 초과 시 도구 없이 마무리 답변을 강제한다.
_MAX_TOOL_ROUNDS = 4


def _make_llm() -> ChatOpenAI:
    # stream_usage=True: 스트리밍 응답에도 usage를 실어 UsageCallbackHandler가 집계하게 한다.
    return ChatOpenAI(model=config.OPENAI_MODEL, api_key=config.OPENAI_API_KEY, stream_usage=True)


def _build_system(state: TurnState) -> str:
    return (
        f"{_PROMPT}\n\n"
        f"[세션 정보]\n{context_signals(state)}\n\n"
        f"[이전 대화 압축 요약]\n{render_compact(state.get('compact'))}\n\n"
        f"[업무 분석 요약]\n{analysis_brief(state.get('analysis'))}\n\n"
        f"[현재 흐름도 개요]\n{flow_outline(state.get('recommendation'))}"
    )


async def qa_node(state: TurnState) -> dict:
    sink: list[dict] = []
    # KB는 A360 전용 — 타 솔루션 세션엔 툴을 바인딩하지 않는다 (컨텍스트로만 답변).
    tools = build_kb_tools(sink) if (state.get("solution") or "a360") == "a360" else []
    llm = _make_llm()
    runnable = llm.bind_tools(tools) if tools else llm

    # 이 노드의 LLM 호출을 purpose="turn_qa"로 기록한다(RPA-73). 노드 진입 시점(async,
    # usage_context 활성)에 생성해 귀속 context를 정확히 스냅샷하고, 각 astream 호출
    # config에 얹는다 — 최상위 콜백은 제거돼 있어 이중 기록되지 않는다.
    usage_config = {"callbacks": [UsageCallbackHandler(purpose="turn_qa")]}

    messages = [
        SystemMessage(content=_build_system(state)),
        *_history_messages(state.get("history")),
        HumanMessage(content=state.get("message", "")),
    ]

    parts: list[str] = []
    for round_no in range(_MAX_TOOL_ROUNDS + 1):
        # 상한 도달 시 도구 없이 마무리 — 무한 검색을 끊는다.
        target = llm if round_no == _MAX_TOOL_ROUNDS else runnable
        gathered = None
        async for chunk in target.astream(messages, config=usage_config):
            gathered = chunk if gathered is None else gathered + chunk
            if chunk.text:  # 도구 호출 턴은 텍스트가 없어 토큰이 새지 않는다
                parts.append(chunk.text)
                emit({"event": "token", "stage": "chat", "message": chunk.text})
        if not getattr(gathered, "tool_calls", None):
            break
        emit({"event": "stage", "stage": "searching", "message": "지식베이스 확인 중"})
        messages.append(gathered)
        messages.extend(execute_tool_calls(tools, gathered))

    return {
        "turn_type": TYPE_ANSWER,
        "answer": "".join(parts),
        "sources": sink_to_sources(sink),
    }
