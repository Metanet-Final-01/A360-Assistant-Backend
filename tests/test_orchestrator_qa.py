"""orchestrator/qa.py 헬퍼 단위 테스트 (RPA-76).

_history_messages는 레거시 graph.py에서 qa로 이관됐다 — 백엔드 이력 dict를
LangChain 메시지로 변환한다. LLM·앱 부팅 없이 변환 규칙만 검증한다.
"""

from langchain_core.messages import AIMessage, HumanMessage

from app.agent.v2.orchestrator.qa import _history_messages


def test_history_messages_maps_roles_and_skips_unknown():
    msgs = _history_messages(
        [
            {"role": "system", "content": "무시"},  # 모르는 role → skip
            {"role": "user", "content": "질문"},
            {"role": "assistant", "content": "답변"},
        ]
    )
    assert [type(m) for m in msgs] == [HumanMessage, AIMessage]
    assert [m.content for m in msgs] == ["질문", "답변"]


def test_history_messages_defaults_missing_content_to_empty():
    (msg,) = _history_messages([{"role": "user"}])
    assert isinstance(msg, HumanMessage)
    assert msg.content == ""


def test_history_messages_none_is_empty():
    assert _history_messages(None) == []
