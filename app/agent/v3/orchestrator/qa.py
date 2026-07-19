"""qa — 일반 질문 답변 브랜치 노드 (툴 호출 에이전트, RPA-65).

고정 retrieve→generate(기존 graph.py)를 대체한다: LLM이 필요하다고 판단할 때만
search_kb/get_action_schema를 호출한다 — 인사말·방금 만든 흐름도에 대한 질문은
검색 없이 세션 컨텍스트로 답한다. 유일하게 답변을 token 이벤트로 스트리밍하는
노드다(그래프 전체 messages 스트림을 쓰면 intake·compose 토큰까지 새므로, 이
노드가 자체 astream으로 방출을 통제한다).
"""

import logging
import re
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.core.llm import UsageCallbackHandler

from .. import config
from ..recommend.stream import emit
from .render import analysis_brief, context_signals, flow_outline, render_compact
from .state import TYPE_ANSWER, TurnState
from .tools import build_kb_tools, describe_tool_calls, execute_tool_calls, sink_to_sources, tool_calls_data

logger = logging.getLogger(__name__)

_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "qa.md").read_text(encoding="utf-8")

# 도구 호출 왕복 상한. 초과 시 도구 없이 마무리 답변을 강제한다.
_MAX_TOOL_ROUNDS = 4

# 사실 조회형 질문 판정 — A360 도메인 명사와 질의/가능 마커가 함께 있으면 근거 검색을 강제한다.
# LLM 재량 검색은 사실형 질문에 검색 없이 단정하면 sources가 빈 채로 나가는데 이를 잡는 장치가
# 없었다(근거 가드). 인사말·흐름도 문맥 질문("이 단계 왜 있어?")은 명사 게이트에 걸리지 않아
# 기존처럼 검색 없이 답한다. 오탐 비용은 검색 1회(수 초), 미탐 비용은 근거 없는 단정 —
# 비대칭이라 명사 목록은 초보자 단골 사실 질문 영역(라이선스·에디션·트리거·설치, A360 조사
# §2·§10)으로 보수적으로 좁힌다.
_EVIDENCE_NOUN = re.compile(
    r"패키지|액션|라이선스|라이센스|에디션|커뮤니티|무료|요금|비용|트리거|스케줄|봇\s*에이전트|"
    r"bot\s*agent|컨트롤\s*룸|control\s*room|레코더|recorder|버전|설치|ocr",
    re.IGNORECASE,
)
_EVIDENCE_ASK = re.compile(r"있|없|돼|되나|되니|되어|가능|지원|할 수|필요|어떻|뭐|무엇|알려|설명|차이|맞아")


def _needs_evidence(message: str) -> bool:
    """이 질문이 KB 근거 없이 답하면 안 되는 사실 조회형인지 — 결정론 판정."""
    return bool(_EVIDENCE_NOUN.search(message) and _EVIDENCE_ASK.search(message))

# 백엔드가 주입하는 대화 이력의 role → LangChain 메시지 타입.
_ROLE_TO_MESSAGE = {"user": HumanMessage, "assistant": AIMessage}


def _history_messages(history: list[dict] | None) -> list:
    """백엔드가 넘긴 이력([{"role", "content"}])을 LLM 메시지로 변환한다.

    아는 role(user/assistant)만 싣고 나머지는 건너뛴다 — 잘못된 입력에 죽지 않도록.
    """
    messages = []
    for turn in history or []:
        message_cls = _ROLE_TO_MESSAGE.get(turn.get("role"))
        if message_cls is None:
            continue
        messages.append(message_cls(content=turn.get("content", "")))
    return messages


def _make_llm() -> ChatOpenAI:
    """이 노드용 ChatOpenAI를 만든다.

    stream_usage=True: 스트리밍 응답에도 usage를 실어 UsageCallbackHandler가 집계하게 한다.
    """
    return ChatOpenAI(model=config.OPENAI_MODEL, api_key=config.OPENAI_API_KEY, stream_usage=True)


def _build_system(state: TurnState) -> str:
    """세션·압축·분석·흐름도 컨텍스트를 프롬프트와 합쳐 시스템 메시지를 만든다."""
    return (
        f"{_PROMPT}\n\n"
        f"[세션 정보]\n{context_signals(state)}\n\n"
        f"[이전 대화 압축 요약]\n{render_compact(state.get('compact'))}\n\n"
        f"[업무 분석 요약]\n{analysis_brief(state.get('analysis'))}\n\n"
        f"[현재 흐름도 개요]\n{flow_outline(state.get('recommendation'))}"
    )


async def qa_node(state: TurnState) -> dict:
    """KB 근거 질문 답변 브랜치 — LLM 재량의 툴 호출 후 답변을 token으로 스트리밍한다.

    검색이 필요하면 search_kb/get_action_schema를 부르고, 그 진행을 stage 이벤트로
    알린다. 반환: {turn_type, answer, sources}.
    """
    # 진입 즉시 신호 — 인사·컨텍스트 질문은 검색 없이 바로 토큰이 흐르므로,
    # 첫 토큰 전까지 화면이 비지 않게 한다.
    emit({"event": "stage", "stage": "reading", "message": "질문 이해 중"})
    sink: list[dict] = []
    # KB는 A360 전용 — 타 솔루션 세션엔 툴을 바인딩하지 않는다 (컨텍스트로만 답변).
    tools = build_kb_tools(sink) if (state.get("solution") or "a360") == "a360" else []
    llm = _make_llm()
    runnable = llm.bind_tools(tools) if tools else llm
    # 근거 가드: 사실 조회형이면 첫 턴을 도구 호출로 강제한다(tool_choice="required").
    # 도구 턴은 텍스트가 없어 스트리밍이 오염되지 않고, 이후 턴은 재량으로 돌아간다 —
    # 답변 후 재시도 방식은 이미 흘린 토큰 위에 두 번째 답이 겹쳐 쓸 수 없어 사전 강제로 푼다.
    force_search = bool(tools) and _needs_evidence(state.get("message", ""))

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
        if round_no == _MAX_TOOL_ROUNDS:
            target = llm
        elif round_no == 0 and force_search:
            target = llm.bind_tools(tools, tool_choice="required")
        else:
            target = runnable
        gathered = None
        async for chunk in target.astream(messages, config=usage_config):
            gathered = chunk if gathered is None else gathered + chunk
            if chunk.text:  # 도구 호출 턴은 텍스트가 없어 토큰이 새지 않는다
                parts.append(chunk.text)
                emit({"event": "token", "stage": "chat", "message": chunk.text})
        if not getattr(gathered, "tool_calls", None):
            break
        emit({"event": "stage", "stage": "searching",
              "message": describe_tool_calls(gathered.tool_calls),
              "data": tool_calls_data(gathered.tool_calls)})  # data는 관측 전용(RPA-105)
        messages.append(gathered)
        messages.extend(execute_tool_calls(tools, gathered))

    return {
        "turn_type": TYPE_ANSWER,
        "answer": "".join(parts),
        "sources": sink_to_sources(sink),
    }
