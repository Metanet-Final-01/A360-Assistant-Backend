"""orchestrator/compact.py 단위 테스트 (RPA-65).

핵심은 이월 불변식: 압축 이후 이전 대화가 다시 전달되지 않으므로(영구 유실),
이전 압축본의 decisions·verbatim이 LLM 출력에서 빠져도 코드(carry_over)가 되살린다.
"""

from app.agent.v2.orchestrator import compact as compact_mod
from app.agent.v2.orchestrator.compact import CompactContext, VerbatimBlock, carry_over, compact_node


def test_carry_over_restores_dropped_decisions_and_verbatim():
    prev = CompactContext(
        decisions=["메일은 Outlook 경유", "3일치만 수집"],
        verbatim=[VerbatimBlock(kind="catalog", content="SolutionX: open/click/save")],
    )
    new = CompactContext(decisions=["3일치만 수집"], verbatim=[])  # LLM이 일부 유실
    merged = carry_over(prev, new)
    assert "메일은 Outlook 경유" in merged.decisions
    assert any(v.content == "SolutionX: open/click/save" for v in merged.verbatim)


def test_carry_over_does_not_duplicate_kept_items():
    prev = CompactContext(decisions=["결정A"], verbatim=[VerbatimBlock(kind="data", content="원문")])
    new = CompactContext(decisions=["결정A"], verbatim=[VerbatimBlock(kind="data", content="원문")])
    merged = carry_over(prev, new)
    assert merged.decisions.count("결정A") == 1
    assert len(merged.verbatim) == 1


def test_carry_over_without_prev_is_identity():
    new = CompactContext(decisions=["결정A"])
    assert carry_over(None, new) is new


def test_compact_node_stamps_type_and_applies_carry_over(monkeypatch):
    monkeypatch.setattr(compact_mod, "chat_json",
                        lambda *a, **k: CompactContext(task_overview="금 시세 자동화"))
    state = {
        "history": [{"role": "user", "content": "안녕"}] * 4,
        "compact": CompactContext(decisions=["Outlook 경유"]).model_dump(),
    }
    out = compact_node(state)
    assert out["turn_type"] == "compact"
    assert "Outlook 경유" in out["compact_out"]["decisions"]  # 이월 보정
    assert "4개" in out["answer"]


def test_compact_node_input_includes_prev_and_full_history(monkeypatch):
    """입력 전체(이전 압축본 + 이력 전부)가 압축 대상 — 최근 턴 예외 없음."""
    captured = {}

    def capture(messages, **kwargs):
        captured["user"] = messages[-1]["content"]
        return CompactContext()

    monkeypatch.setattr(compact_mod, "chat_json", capture)
    state = {
        "history": [{"role": "user", "content": f"턴-{i}"} for i in range(10)],
        "compact": CompactContext(decisions=["이전결정"]).model_dump(),
    }
    compact_node(state)
    assert "이전결정" in captured["user"]
    for i in range(10):
        assert f"턴-{i}" in captured["user"]
