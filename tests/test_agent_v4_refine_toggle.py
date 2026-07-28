# -*- coding: utf-8 -*-
"""교정을 끄는 스위치 (RPA-298).

## 왜 필요한가 (실측, 2026-07-28 00:46)

교정 라운드 1이 가중합을 200 → **0**으로 만들었는데 산출물은 이랬다:

    Browser/Call a JavaScript function  «증권 클릭»     ← 버튼 클릭을 JS 호출로
    Loop action for data iteration      «3일 반복»  └ 본문은 Step/Step(비실행)
    Microsoft 365 Excel/Paste cell      «표 붙여넣기»    ← 복사한 적이 없다
    Email/Forward                       «메일 보내기»    ← 전달할 원본 메일이 없다

**데이터를 읽는 액션이 하나도 없다.** 교정의 목적 함수가 '정적 위반 + req_id 배정'뿐이라
req_id를 단 채 아무 액션으로 갈아끼우면 만점이 된다. 그런데 compose 후보 자체는 그보다
나았다(승자 커버리지 0.57 · 정상경로 0.50).

즉 **"교정이 순이득인가"가 열린 질문**이다. 코드를 지우기 전에 스위치로 잰다 —
`V4_REFINE_MAX_ROUNDS=0`이면 초안이 그대로 확정되고, 되돌리기는 값 하나다.
"""

import pytest

from app.agent.v4.orchestrator import harness
from app.agent.v4.orchestrator.edit_ops import EditOp, EditOps
from app.agent.v4.verify.findings import Finding


class _Catalog:
    def get_action_schema(self, pkg, act):
        return {"package": pkg, "action": act, "parameters": []}

    def iter_action_schemas(self):
        return iter([{"package": "String", "action": "assign", "parameters": []}])


def _flow():
    return {"steps": [{"step_id": "step-1", "label": "s", "actions": [
        {"order": 1, "package": "String", "action": "assign", "label": "지정",
         "req_id": "req-1", "parameters": [], "children": []},
    ]}]}


def _spec():
    return {"goal": "g", "requirements": [
        {"req_id": "req-1", "text": "하나", "priority": "must"},
        {"req_id": "req-2", "text": "둘", "priority": "must"},   # 미해결 — 자리표시자 대상
    ]}


@pytest.fixture
def bench(monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(harness, "emit", lambda ev: events.append(ev))
    monkeypatch.setattr(harness, "emit_flow_frame", lambda *a, **k: None)
    monkeypatch.setattr(harness, "collect_violations",
                        lambda *a, **k: [{"rule": "R7", "location": "actions[0]",
                                          "message": "R7", "step_id": "step-1"}])

    def _boom(*a, **k):
        raise AssertionError("교정이 꺼졌는데 surgeon을 불렀다")

    monkeypatch.setattr(harness, "chat_json", _boom)
    run = lambda **kw: harness.refine_flow(_flow(), _Catalog(), spec=_spec(), **kw)  # noqa: E731
    run.events = events
    return run


# ── 끄기 ─────────────────────────────────────────────────────────────────────

def test_0라운드면_surgeon을_부르지_않는다(bench):
    """🔴 스위치의 요점 — LLM 호출도, 흐름도 변형도 없어야 한다(fixture가 호출 시 실패시킨다)."""
    out = bench(max_rounds=0)

    assert out["repaired"] is False
    assert out["violations"], "검수 결과는 표시용으로 그대로 실린다"


def test_0라운드면_교정_중이라고_말하지_않는다(bench):
    """stage message는 프론트가 그대로 화면에 쌓는다 — 하지 않은 일을 했다고 하면 안 된다."""
    bench(max_rounds=0)
    msgs = " ".join(e.get("message", "") for e in bench.events)

    assert "교정 중" not in msgs and "교정 종료" not in msgs
    assert "교정 없이 확정" in msgs


def test_0라운드에도_미해결_요구는_자리표시자로_남는다(bench):
    """🔴 교정을 껐다고 누락을 숨기면 안 된다 — 조용히 빠뜨리는 것은 선택지가 아니다."""
    out = bench(max_rounds=0)
    ids = [s.get("step_id") for s in out["flow"]["steps"]]

    assert any(str(i).startswith(harness.PLACEHOLDER_STEP_PREFIX) for i in ids)


def test_끈_사실이_관측에_남는다(bench):
    """산출물이 나빠졌을 때 '교정이 꺼져 있었나'를 사후에 알 수 있어야 한다."""
    bench(max_rounds=0)
    (ev,) = [e for e in bench.events if (e.get("data") or {}).get("refine_disabled")]

    assert ev["data"]["violations"] == 1


# ── env 토글 ─────────────────────────────────────────────────────────────────

def test_env로_끌_수_있다(monkeypatch, bench):
    """운영에서 코드 수정 없이 끄고 켤 수 있어야 한다 — 되돌리기가 값 하나."""
    monkeypatch.setenv("V4_REFINE_MAX_ROUNDS", "0")

    assert harness._default_max_rounds() == 0
    assert bench()["repaired"] is False   # max_rounds 미지정 → env를 읽는다


def test_env_기본은_8이다(monkeypatch):
    """스위치를 넣었다고 기본 동작이 바뀌면 안 된다."""
    monkeypatch.delenv("V4_REFINE_MAX_ROUNDS", raising=False)
    assert harness._default_max_rounds() == 8


@pytest.mark.parametrize("bad", ["", "여덟", "-3"])
def test_잘못된_값은_생성을_막지_않는다(monkeypatch, bad):
    """설정 사고가 턴 실패가 되면 안 된다 — 음수는 0으로 눌러 '끔'으로 읽는다."""
    monkeypatch.setenv("V4_REFINE_MAX_ROUNDS", bad)
    assert harness._default_max_rounds() >= 0


def test_명시_인자가_env를_이긴다(monkeypatch, bench):
    """평가 하네스·테스트가 라운드 수를 직접 고정할 수 있어야 한다."""
    monkeypatch.setenv("V4_REFINE_MAX_ROUNDS", "8")
    assert bench(max_rounds=0)["repaired"] is False


# ── 켜져 있을 때는 그대로 ────────────────────────────────────────────────────

def test_켜져_있으면_기존_경로다(monkeypatch):
    """회귀 가드 — 스위치는 끌 때만 동작을 바꾼다."""
    events: list[dict] = []
    monkeypatch.setattr(harness, "emit", lambda ev: events.append(ev))
    monkeypatch.setattr(harness, "emit_flow_frame", lambda *a, **k: None)
    seq = [[{"rule": "R7", "location": "actions[0]", "message": "R7", "step_id": "step-1"}], []]
    monkeypatch.setattr(harness, "collect_violations", lambda *a, **k: seq.pop(0) if seq else [])
    monkeypatch.setattr(harness, "chat_json",
                        lambda *a, **k: EditOps(operations=[EditOp(op="update", target="n1", label="x")]))

    out = harness.refine_flow(_flow(), _Catalog(), spec=None, max_rounds=1,
                              extra_findings=[Finding(layer="judge", message="x")])

    assert out["repaired"] is True
    assert any("교정 라운드" in e.get("message", "") for e in events)
