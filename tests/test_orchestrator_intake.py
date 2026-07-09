"""orchestrator/intake.py 단위 테스트 (RPA-65).

chat_json을 몽키패치해 LLM 없이 검증한다: 정상 분류, 결정론 가드(흐름도 없는
edit→generate 강등, 미지 라우트→qa), 파싱 실패 시 qa 폴백, 인프라 오류 전파.
"""

import pytest

from app.agent.orchestrator import intake as intake_mod
from app.agent.orchestrator.intake import IntakeOutput, intake_node

_FLOW = {"schema_version": "1.0", "steps": [{"step_id": "step-1", "actions": []}]}


def _state(**over):
    base = {"message": "안녕", "solution": "a360", "history": [], "compact": None,
            "analysis": None, "recommendation": None, "parsed_doc": None}
    base.update(over)
    return base


def test_intake_returns_llm_route(monkeypatch):
    monkeypatch.setattr(intake_mod, "chat_json",
                        lambda *a, **k: IntakeOutput(route="generate", reason="산출 요청"))
    out = intake_node(_state(message="흐름도 만들어줘"))
    assert out["route"] == "generate"
    assert out["route_reason"] == "산출 요청"


def test_intake_downgrades_edit_without_flow(monkeypatch):
    monkeypatch.setattr(intake_mod, "chat_json",
                        lambda *a, **k: IntakeOutput(route="edit", reason="수정 요청"))
    out = intake_node(_state(message="3번 단계 바꿔줘", recommendation=None))
    assert out["route"] == "generate"  # 수정 대상이 없으면 신규 산출로


def test_intake_keeps_edit_with_flow(monkeypatch):
    monkeypatch.setattr(intake_mod, "chat_json",
                        lambda *a, **k: IntakeOutput(route="edit", reason="수정 요청"))
    out = intake_node(_state(message="3번 단계 바꿔줘", recommendation=_FLOW))
    assert out["route"] == "edit"


def test_intake_falls_back_to_qa_on_unknown_route(monkeypatch):
    monkeypatch.setattr(intake_mod, "chat_json",
                        lambda *a, **k: IntakeOutput(route="deploy", reason="?"))
    out = intake_node(_state())
    assert out["route"] == "qa"


def test_intake_falls_back_to_qa_on_parse_failure(monkeypatch):
    def boom(*a, **k):
        raise ValueError("파싱 실패")

    monkeypatch.setattr(intake_mod, "chat_json", boom)
    out = intake_node(_state())
    assert out["route"] == "qa"


def test_intake_propagates_infra_error(monkeypatch):
    """인프라 오류(키/인증)는 폴백하지 않고 올린다 — 진입점이 error 이벤트로 처리."""

    def boom(*a, **k):
        raise RuntimeError("OPENAI_API_KEY 환경변수가 필요합니다")

    monkeypatch.setattr(intake_mod, "chat_json", boom)
    with pytest.raises(RuntimeError):
        intake_node(_state())


def test_intake_prompt_carries_full_history(monkeypatch):
    """링 게이지 계약: intake 프롬프트에 history 전체가 절삭 없이 실린다."""
    captured = {}

    def capture(messages, **kwargs):
        captured["user"] = messages[-1]["content"]
        return IntakeOutput(route="qa", reason="")

    monkeypatch.setattr(intake_mod, "chat_json", capture)
    history = [{"role": "user", "content": f"메시지-{i}"} for i in range(30)]
    intake_node(_state(history=history))
    for i in range(30):
        assert f"메시지-{i}" in captured["user"]


def test_intake_prompt_has_signals_not_payloads(monkeypatch):
    """문서·흐름도는 존재 신호만 — 원문(파싱 블록·액션 트리)은 싣지 않는다."""
    captured = {}

    def capture(messages, **kwargs):
        captured["user"] = messages[-1]["content"]
        return IntakeOutput(route="qa", reason="")

    monkeypatch.setattr(intake_mod, "chat_json", capture)
    parsed = {"page_count": 3, "pages": [{"page": 1, "blocks": [{"type": "text", "text": "문서원문내용"}]}]}
    flow = {"steps": [{"step_id": "step-1", "actions": [{"order": 1, "package": "Excel_MS", "action": "GoToCell"}]}]}
    intake_node(_state(parsed_doc=parsed, recommendation=flow))
    assert "문서원문내용" not in captured["user"]
    assert "GoToCell" not in captured["user"]
    assert "3페이지" in captured["user"]
    assert "1단계" in captured["user"]
