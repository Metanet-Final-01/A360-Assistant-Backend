# -*- coding: utf-8 -*-
"""교정 라운드 관측 — 무엇을 시도했고 왜 그렇게 끝났나 (RPA-298).

## 무엇을 막는가 (실측, 2026-07-27)

산출물에 R17(세션 핸들 패키지 불일치) blocker가 남았는데, 그 수리에 필요한 `Email` 어휘는
수리 메뉴에 이미 들어가 있었다. 그런데 **원인을 특정할 수 없었다**:

  - surgeon이 그 노드를 건드리려 시도조차 안 했나?
  - 연산을 냈는데 적용이 실패했나?
  - 적용됐는데 가중합이 안 줄어 폐기됐나?

라운드 정보가 `turn_events`에 아예 없었고 컨테이너 로그는 재시작으로 날아간다. 원인을 못
짚으면 고칠 수도 없다. 이 파일은 라운드마다 **결정론으로** 남기는 기록을 고정한다.
"""

import pytest

from app.agent.v4.orchestrator import harness
from app.agent.v4.orchestrator.edit_ops import EditOp, EditOps
from app.agent.v4.verify.findings import Finding


@pytest.fixture
def events(monkeypatch):
    """refine 경로가 흘리는 이벤트를 모은다 — emit은 harness가 직접 참조한다."""
    seen: list[dict] = []
    monkeypatch.setattr(harness, "emit", lambda ev: seen.append(ev))
    monkeypatch.setattr(harness, "emit_flow_frame", lambda *a, **k: None)
    return seen


def rounds(events):
    return [e for e in events if e.get("stage") == "refining" and "round" in (e.get("data") or {})]


def summary(events):
    return [e for e in events if e.get("stage") == "refining" and "rounds_used" in (e.get("data") or {})]


class _Catalog:
    def get_action_schema(self, pkg, act):
        return {"package": pkg, "action": act, "parameters": []}

    def iter_action_schemas(self):
        return iter([{"package": "Excel advanced", "action": "Open", "parameters": []}])


def _flow():
    return {"steps": [{"step_id": "step-1", "label": "s", "actions": [
        {"package": "Excel advanced", "action": "Open", "label": "열기", "children": []},
    ]}]}


def _stub(monkeypatch, *, ops, violations_seq):
    """surgeon 출력과 라운드별 재검증 결과를 고정한다."""
    monkeypatch.setattr(harness, "chat_json", lambda *a, **k: EditOps(operations=ops))
    seq = list(violations_seq)
    monkeypatch.setattr(harness, "collect_violations", lambda *a, **k: seq.pop(0) if seq else [])


def _viol(rule, loc="actions[0]"):
    return {"rule": rule, "location": loc, "message": f"{rule} 위반", "step_id": "step-1"}


# ── 라운드별 기록 ────────────────────────────────────────────────────────────

def test_폐기된_라운드가_기록된다(events, monkeypatch):
    """🔴 폐기는 '고쳤는데 되돌렸다'는 뜻 — 무엇을 시도했고 가중합이 어떻게 움직였는지가 남아야 한다."""
    _stub(monkeypatch,
          ops=[EditOp(op="update", target="n1", package="Email", action_name="Send")],
          violations_seq=[[_viol("R1")], [_viol("R1"), _viol("R2")]])  # 초기 → 라운드1(악화)

    harness.refine_flow(_flow(), _Catalog(), max_rounds=1)

    (r,) = rounds(events)
    assert r["data"]["outcome"] == "discarded"
    assert r["data"]["weight_before"] < r["data"]["weight_after"], "악화가 숫자로 보여야 한다"
    assert r["data"]["ops"] == [{"op": "update", "target": "n1", "to": "Email/Send"}]


def test_채택된_라운드가_기록된다(events, monkeypatch):
    _stub(monkeypatch,
          ops=[EditOp(op="remove", target="n1")],
          violations_seq=[[_viol("R1")], []])

    harness.refine_flow(_flow(), _Catalog(), max_rounds=1)

    (r,) = rounds(events)
    assert r["data"]["outcome"] == "accepted"
    assert r["data"]["weight_after"] == 0
    assert r["data"]["remaining"] == {}


def test_연산_없음이_기록된다(events, monkeypatch):
    """🔴 진단상 가장 중요한 종료 사유 — '고칠 방법이 없다'는 신호다.

    남은 규칙이 곧 '수리 어휘가 부족한 지점'이라, 이게 없으면 어휘를 어디에 더 줘야 할지
    알 수 없다.
    """
    _stub(monkeypatch, ops=[], violations_seq=[[_viol("R1"), _viol("R17")]])

    harness.refine_flow(_flow(), _Catalog(), max_rounds=3)

    (r,) = rounds(events)
    assert r["data"]["outcome"] == "no_ops"
    assert r["data"]["remaining"] == {"R1": 1, "R17": 1}


def test_적용_실패가_기록된다(events, monkeypatch):
    """연산은 냈는데 대상 노드를 못 찾은 경우 — 어휘 문제가 아니라 앵커 문제다."""
    _stub(monkeypatch,
          ops=[EditOp(op="remove", target="없는노드")],
          violations_seq=[[_viol("R1")]])

    harness.refine_flow(_flow(), _Catalog(), max_rounds=1)

    (r,) = rounds(events)
    assert r["data"]["outcome"] == "apply_failed"
    assert r["data"]["proposed"] == 1
    assert r["data"]["errors"], "왜 못 붙였는지가 남아야 한다"


def test_LLM_실패가_기록된다(events, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("rate limit")

    monkeypatch.setattr(harness, "chat_json", boom)
    monkeypatch.setattr(harness, "collect_violations", lambda *a, **k: [_viol("R1")])

    harness.refine_flow(_flow(), _Catalog(), max_rounds=2)

    (r,) = rounds(events)
    assert r["data"]["outcome"] == "llm_error"
    assert "rate limit" in r["data"]["error"]


# ── 종료 요약 ────────────────────────────────────────────────────────────────

def test_종료_요약이_남는다(events, monkeypatch):
    """🔴 라운드 기록만으로는 '예산을 다 썼나, 무개선으로 일찍 빠졌나'가 안 보인다.

    둘은 처방이 정반대다 — 예산 부족이면 라운드를 늘리고, 수리 불능이면 어휘·규칙을 본다.
    """
    _stub(monkeypatch,
          ops=[EditOp(op="update", target="n1", label="x")],
          violations_seq=[[_viol("R1")], [_viol("R1")], [_viol("R1")]])

    harness.refine_flow(_flow(), _Catalog(), max_rounds=8)

    (s,) = summary(events)
    assert s["data"]["repaired"] is False
    assert s["data"]["rounds_used"] == 2, "무개선 2라운드에서 빠져나온다(_STOP_AFTER_NO_IMPROVE)"
    assert s["data"]["max_rounds"] == 8
    assert s["data"]["remaining"] == {"R1": 1}


def test_고칠_게_없으면_라운드를_안_돈다(events, monkeypatch):
    """위반이 없으면 루프 진입 전에 반환한다 — 빈 기록을 남기지 않는다."""
    monkeypatch.setattr(harness, "collect_violations", lambda *a, **k: [])
    harness.refine_flow(_flow(), _Catalog(), max_rounds=8)
    assert rounds(events) == [] and summary(events) == []


# ── 관측 안전 ────────────────────────────────────────────────────────────────

def test_연산_요약에_자유_텍스트를_싣지_않는다():
    """turn_events는 관측 DB로 나간다 — 파라미터 값·라벨에 사용자 업무 내용이 섞인다."""
    digest = harness._op_digest([EditOp(
        op="set_params", target="n1", label="증권 버튼 클릭",
        parameters=[{"name": "URL", "value": "https://내부시스템/고객/12345"}],
    )])
    blob = repr(digest)
    assert "내부시스템" not in blob and "증권 버튼 클릭" not in blob
    assert digest[0]["params"] == ["URL"], "이름만 남긴다 — 무엇을 건드렸나는 알아야 한다"


def test_연산_수에_상한이_있다():
    """detail이 4,000자를 넘으면 sessions._tev가 통째로 preview 마커로 바꿔 구조가 사라진다."""
    ops = [EditOp(op="remove", target=f"n{i}") for i in range(50)]
    assert len(harness._op_digest(ops)) == harness._MAX_LOGGED_OPS
