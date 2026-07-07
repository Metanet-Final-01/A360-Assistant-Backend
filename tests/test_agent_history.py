"""app/agent 멀티턴 이력(history) 배선 단위 테스트 (RPA-25).

LLM 호출·앱 부팅 없이 메시지 조립과 시그니처만 검증한다 — app.agent만 import하므로
OPENAI_API_KEY나 (auth 등) 다른 앱 의존성 없이 돌아간다.
"""
import inspect

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent import graph, run_agent, stream_agent


def test_signatures_are_backward_compatible():
    """message는 필수 첫 인자 그대로, history는 뒤에 붙는 optional(default None)."""
    for fn in (run_agent, stream_agent):
        params = inspect.signature(fn).parameters
        assert list(params) == ["message", "history"]
        assert params["message"].default is inspect.Parameter.empty
        assert params["history"].default is None


def test_build_messages_without_history_matches_single_turn():
    state = {"message": "엑셀 읽는 법", "history": [], "docs": [], "answer": ""}
    msgs = graph._build_messages(state)
    assert [type(m) for m in msgs] == [SystemMessage, HumanMessage]
    assert msgs[-1].content == "엑셀 읽는 법"


def test_build_messages_orders_history_between_system_and_current():
    state = {
        "message": "방금 읽은 걸 다른 시트에 쓰려면?",
        "history": [
            {"role": "user", "content": "엑셀 읽는 법"},
            {"role": "assistant", "content": "Open / Get multiple cells를 씁니다."},
        ],
        "docs": [{"title": "T-TITLE", "content": "C-CONTENT", "package_name": "P"}],
        "answer": "",
    }
    msgs = graph._build_messages(state)
    assert [type(m) for m in msgs] == [SystemMessage, HumanMessage, AIMessage, HumanMessage]
    assert msgs[1].content == "엑셀 읽는 법"
    assert msgs[2].content == "Open / Get multiple cells를 씁니다."
    assert msgs[3].content == "방금 읽은 걸 다른 시트에 쓰려면?"
    # 근거 문서는 시스템 메시지에 주입된다
    assert "T-TITLE" in msgs[0].content
    assert "C-CONTENT" in msgs[0].content


def test_history_messages_maps_roles_and_skips_unknown():
    msgs = graph._history_messages(
        [
            {"role": "system", "content": "무시"},  # 모르는 role → skip
            {"role": "user", "content": "질문"},
            {"role": "assistant", "content": "답변"},
        ]
    )
    assert [type(m) for m in msgs] == [HumanMessage, AIMessage]
    assert [m.content for m in msgs] == ["질문", "답변"]


def test_history_messages_defaults_missing_content_to_empty():
    (msg,) = graph._history_messages([{"role": "user"}])
    assert isinstance(msg, HumanMessage)
    assert msg.content == ""


def test_history_messages_none_is_empty():
    assert graph._history_messages(None) == []


def test_build_messages_tolerates_missing_history_key():
    """history 키 없이 message만 넘어와도(구 호출부 호환) 안전해야 한다."""
    msgs = graph._build_messages({"message": "q", "docs": []})
    assert [type(m) for m in msgs] == [SystemMessage, HumanMessage]
