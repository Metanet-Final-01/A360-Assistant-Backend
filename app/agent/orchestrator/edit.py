"""edit — 흐름도 수정 브랜치 노드 (RPA-65).

산출(generate)이 결정론 shortlist 파이프라인을 쓰는 것과 달리, 수정은 요청이
국소적이라 KB 접근을 LLM 재량의 툴 호출로 둔다: 새 액션 표기가 필요하면 search_kb,
파라미터 스펙이 필요하면 get_action_schema를 불러 확인한 뒤 수정안을 낸다.

value_source="user" 값 보존은 프롬프트 규칙으로 지시한다(경로 기반 구조 대조는
액션 이동·삭제와 구분이 안 돼 결정론 강제가 오히려 위험 — 프롬프트 + 검수로 방어).
LLM이 "수정 없음"(recommendation=null)을 선택하면 type="answer"로 내려간다 —
intake가 qa로 거르지 못한 경계 케이스의 안전망이자 type 정확성 원칙의 이행.
"""

import json
import logging
import re
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from app.core.llm import UsageCallbackHandler
from app.schemas import Recommendation

from .. import config
from ..recommend.stream import emit
from ..verify.catalog import get_catalog
from .generate import UserCatalog, extract_user_catalog
from .harness import verify_and_repair
from .render import dump_json, render_compact, render_history
from .state import TYPE_ANSWER, TYPE_RECOMMENDATION, TurnState
from .tools import build_kb_tools, describe_tool_calls, execute_tool_calls, sink_to_sources

logger = logging.getLogger(__name__)

_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "edit.md").read_text(encoding="utf-8")

# 도구 호출 왕복 상한 — 국소 수정에 충분하고 폭주를 막는다.
_MAX_TOOL_ROUNDS = 4

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


class EditOutput(BaseModel):
    recommendation: Recommendation | None = None
    change_summary: str = ""
    answer: str = ""


def _make_llm() -> ChatOpenAI:
    """이 노드용 ChatOpenAI를 만든다.

    stream_usage=True: UsageCallbackHandler(진입점 config)가 토큰을 집계하게 한다.
    """
    return ChatOpenAI(model=config.OPENAI_MODEL, api_key=config.OPENAI_API_KEY, stream_usage=True)


def _parse_output(text: str) -> EditOutput:
    """LLM 출력(코드펜스 허용)을 EditOutput으로 파싱한다."""
    return EditOutput.model_validate(json.loads(_FENCE.sub("", text.strip())))


def _build_messages(state: TurnState) -> list:
    """현재 흐름도·압축·이력·수정 요청을 담은 edit 프롬프트 메시지를 만든다."""
    user_content = (
        f"[현재 흐름도]\n{dump_json(state.get('recommendation'))}\n\n"
        f"[이전 대화 압축 요약]\n{render_compact(state.get('compact'))}\n\n"
        f"[대화 이력]\n{render_history(state.get('history'))}\n\n"
        f"[수정 요청]\n{state.get('message', '')}"
    )
    return [SystemMessage(content=_PROMPT), HumanMessage(content=user_content)]


async def edit_node(state: TurnState) -> dict:
    """기존 흐름도 수정 브랜치 — 툴로 카탈로그를 확인하며 국소 수정안을 낸다.

    LLM이 '수정 없음'(recommendation=null)을 택하면 type="answer"로 저하한다.
    반환: {turn_type, recommendation_out?, change_summary?, violations, answer, sources}.
    """
    emit({"event": "stage", "stage": "refining", "message": "흐름도 수정 중"})
    is_a360 = (state.get("solution") or "a360") == "a360"
    sink: list[dict] = []
    tools = build_kb_tools(sink) if is_a360 else []
    llm = _make_llm()
    runnable = llm.bind_tools(tools) if tools else llm

    # 이 노드의 LLM 호출을 purpose="turn_edit"로 기록한다(RPA-73). 노드 진입 시점에
    # 생성해 귀속 context를 스냅샷하고 모든 ainvoke config에 얹는다(최상위 콜백 없음).
    usage_config = {"callbacks": [UsageCallbackHandler(purpose="turn_edit")]}

    messages = _build_messages(state)
    response = await runnable.ainvoke(messages, config=usage_config)
    rounds = 0
    while getattr(response, "tool_calls", None) and rounds < _MAX_TOOL_ROUNDS:
        rounds += 1
        emit({"event": "stage", "stage": "searching",
              "message": describe_tool_calls(response.tool_calls)})
        messages.append(response)
        messages.extend(execute_tool_calls(tools, response))
        # 마지막 라운드는 도구 없이 강제 마무리 — 무한 검색을 끊는다.
        target = llm if rounds == _MAX_TOOL_ROUNDS else runnable
        response = await target.ainvoke(messages, config=usage_config)

    try:
        out = _parse_output(response.text)
    except (json.JSONDecodeError, ValidationError) as first_error:
        logger.warning("edit 첫 출력 파싱 실패, 1회 교정: %s", first_error)
        messages.append(response)
        messages.append(HumanMessage(content=(
            f"위 출력이 지정한 JSON 형식을 만족하지 못했습니다. 오류:\n{first_error}\n"
            "설명 없이, 형식에 맞는 JSON 객체만 다시 출력하세요."
        )))
        response = await llm.ainvoke(messages, config=usage_config)  # 교정 턴은 도구 없이
        try:
            out = _parse_output(response.text)
        except (json.JSONDecodeError, ValidationError) as second_error:
            # 수정안 산출 실패 — 턴을 죽이지 않고 답변으로 저하 (실제로 수정 안 했으니 type도 answer)
            logger.warning("edit 출력 파싱 실패(교정 후에도): %s", second_error)
            return {
                "turn_type": TYPE_ANSWER,
                "answer": "요청하신 수정안을 만들지 못했어요. 어느 단계의 무엇을 바꿀지 조금 더 구체적으로 알려주시겠어요?",
                "sources": sink_to_sources(sink),
            }

    if out.recommendation is None:  # 수정 없음 — 질문 답변/되묻기
        return {
            "turn_type": TYPE_ANSWER,
            "answer": out.answer or "흐름도를 바꾸지는 않았어요.",
            "sources": sink_to_sources(sink),
        }

    flow = out.recommendation.model_dump()
    if is_a360:
        result = verify_and_repair(flow, get_catalog())
    else:
        # 타 솔루션: generate와 동일하게 대화(메시지+이력+compact.verbatim)에서 UserCatalog를
        # 재추출해 검수한다. 카탈로그를 못 찾으면 검수 기준이 없어 생략한다.
        extraction = extract_user_catalog(state)
        if extraction.actions:
            specs = [a.as_spec() for a in extraction.actions]
            result = verify_and_repair(flow, UserCatalog(specs))
        else:
            result = {"flow": flow, "violations": [], "repaired": False}

    answer = out.answer or (out.change_summary or "요청하신 대로 흐름도를 수정했어요.")
    if result["violations"]:
        answer += f" (검수에서 해소하지 못한 위반 {len(result['violations'])}건이 있어요.)"
    return {
        "turn_type": TYPE_RECOMMENDATION,
        "recommendation_out": result["flow"],
        "change_summary": out.change_summary or None,
        "violations": result["violations"],
        "answer": answer,
        "sources": sink_to_sources(sink),
    }
