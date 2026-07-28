import uuid

from app.services import session_title


def test_normalize_title_keeps_subject_and_intent():
    assert session_title.normalize_title("  보험금 지급 자동화 업무 요청  ") == "보험금 지급 자동화 업무 요청"


def test_normalize_title_rejects_keep_marker_and_empty_value():
    assert session_title.normalize_title("__KEEP__") is None
    assert session_title.normalize_title("   ") is None


def test_suggest_session_title_uses_only_the_two_latest_user_messages(monkeypatch):
    captured = {}

    def fake_chat(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return "보험금 지급 자동화 업무 요청"

    monkeypatch.setattr(session_title.llm, "chat", fake_chat)

    result = session_title.suggest_session_title(
        ["이전 메시지", "보험금 지급 자동화하고 싶어", "추가 메시지"], uuid.uuid4()
    )

    assert result == "보험금 지급 자동화 업무 요청"
    assert "추가 메시지" in captured["messages"][1]["content"]
    assert "이전 메시지" not in captured["messages"][1]["content"]
    assert captured["kwargs"]["purpose"] == "session_title"
