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

import json

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
    # ⚠ 모든 표기에 `parameters: []`를 준다 = "파라미터 없는 액션 확정". 표기를 갈아끼우는
    # update가 오면 _retarget_params가 그 노드의 파라미터를 **전부** 걷는다. 아래 픽스처는
    # 파라미터가 없어 무해하지만, 파라미터 있는 흐름도를 새로 쓸 거면 스텁을 함께 고쳐야 한다.
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
    # 키 이름이 `params`가 아니라 `params_sent`인 이유: 예전 이 필드는 "모델이 보냈지만
    # update가 무시한 이름"이었고 지금은 "실제로 병합된 이름"이다(RPA-298 항목 A).
    # 같은 이름을 유지하면 과거·신규 turn_events를 같은 질의로 읽을 때 조용히 틀린다.
    assert digest[0]["params_sent"] == ["URL"], "이름만 남긴다 — 무엇을 건드렸나는 알아야 한다"


def test_연산_수에_상한이_있다():
    """detail이 4,000자를 넘으면 sessions._tev가 통째로 preview 마커로 바꿔 구조가 사라진다."""
    ops = [EditOp(op="remove", target=f"n{i}") for i in range(50)]
    assert len(harness._op_digest(ops)) == harness._MAX_LOGGED_OPS


def test_라운드_기록이_총_길이_예산_안에_들어간다(events, monkeypatch):
    """🔴 필드별 상한([:120]·[:5])만으로는 **총량**을 못 막는다.

    sessions._tev는 detail JSON이 4,000자를 넘으면 잘라 붙이는 게 아니라 통째로
    {_truncated, size, preview}로 대체한다 — ops·dropped·errors·param_prune이 한 라운드에
    다 실리면 진단 근거가 **한꺼번에** 사라진다.
    """
    harness._emit_round(
        1, "discarded", weight_before=100, weight_after=200,
        ops=[{"op": "update", "target": f"n{i}", "to": "패키지이름/액션이름" * 5} for i in range(12)],
        dropped=["x" * 120] * 5, errors=["y" * 120] * 5,
        param_prune_reverted=[{"node": f"n{i}", "to": "P/A", "dropped": ["Title"]} for i in range(6)],
        remaining={"R2": 15},
    )

    (e,) = [x for x in events if "round" in (x.get("data") or {})]
    assert len(json.dumps(e["data"], ensure_ascii=False)) <= harness._ROUND_DETAIL_BUDGET
    assert e["data"]["outcome"] == "discarded", "종료 사유는 마지막까지 남는다"
    assert e["data"]["weight_after"] == 200, "가중합 이동도 마지막까지 남는다"


def test_예산_절단은_잘린_키를_밝힌다(events):
    """조용한 절단 금지 — 무엇이 빠졌는지 안 밝히면 '그건 없었다'로 읽힌다."""
    harness._emit_round(1, "discarded", ops=[{"op": "x", "pad": "가" * 400}] * 12,
                        errors=["e" * 120] * 5, remaining={"R2": 1})

    (e,) = [x for x in events if "round" in (x.get("data") or {})]
    assert e["data"]["_budget_trimmed"], "무엇을 버렸는지 남긴다"


# ── 예산 인지 종료 (RPA-298 항목 D) ──────────────────────────────────────────

def test_예산이_다하면_채택된_현재본을_들고_정상_종료한다(events, monkeypatch):
    """🔴 바깥 하드 컷(generate_flow_two_phase)은 타임아웃 시 2상 결과를 **통째로 버리고**
    초안을 확정한다 — 라운드 1~7이 채택한 성과까지 함께 사라진다.

    지금까지 안 터진 이유는 _STOP_AFTER_NO_IMPROVE(2)가 2라운드 만에 빼줬기 때문이고,
    update가 파라미터를 적용하게 되면서 라운드가 생산적이 되어 그 전제가 깨졌다.
    루프가 **라운드를 시작하기 전에** 접으면 현재본을 들고 정상 종료한다.
    """
    def boom(*a, **k):
        raise AssertionError("예산이 없으면 surgeon을 부르지 않는다")

    monkeypatch.setattr(harness, "chat_json", boom)
    monkeypatch.setattr(harness, "collect_violations", lambda *a, **k: [_viol("R1")])

    out = harness.refine_flow(_flow(), _Catalog(), max_rounds=8, deadline_mono=0.0)

    (r,) = rounds(events)
    assert r["data"]["outcome"] == "budget_exhausted"
    assert out["flow"]["steps"], "현재본을 들고 나온다 — 빈 흐름도가 아니다"
    (s,) = summary(events)
    assert s["data"]["rounds_used"] == 0, "돌지 않은 라운드를 썼다고 세지 않는다"
