# -*- coding: utf-8 -*-
"""must 요구의 입도를 분석 단계에 고정한다 (RPA-298).

## 무엇을 막는가 (실측, 2026-07-27 — 같은 PDF로 3회 실행)

    13:54   분석 7단계 → must 5   (step-4·5를 한 요구로 뭉침)
    13:56   분석 7단계 → must 7   (1:1)
    13:59   분석 7단계 → must 5   (뭉침)

**업무 분석은 세 번 다 7단계로 같았다.** 흔들린 것은 spec 빌더의 재량이다.

채점은 "must 요구 중 몇 개가 충족됐나"라서 분모가 달라지면 실행 간 비교가 성립하지 않는다:

    must 5에서 1건 누락 → 0.8        must 7에서 1건 누락 → 0.857

그리고 must_coverage는 심판 결정론 점수의 50% 가중치·하드 게이트·flow_confidence에 전부
들어간다. **요구를 뭉친 실행이 같은 결함으로도 더 높은 점수를 받는다** — 이 상태로는 어떤
개선을 넣어도 측정할 수 없다. 그래서 개수를 모델 재량에서 빼 결정론으로 닫는다.
"""

import pytest

from app.agent.v4.orchestrator.spec import MAX_ANCHORED_MUSTS, anchor_musts_to_analysis


def _analysis(*names: str) -> dict:
    return {"summary": "s", "steps": [
        {"step_id": f"step-{i}", "name": n, "description": f"{n} 설명"}
        for i, n in enumerate(names, 1)
    ]}


def _spec(*reqs: dict) -> dict:
    return {"goal": "g", "requirements": list(reqs)}


def _must(rid: str, text: str, step_id: str | None = None) -> dict:
    return {"req_id": rid, "text": text, "priority": "must", "source": "doc", "step_id": step_id}


def _musts(spec: dict) -> list[tuple[str, str]]:
    return [(r["req_id"], r.get("step_id")) for r in spec["requirements"]
            if r.get("priority", "must") == "must"]


# ── 보정 ─────────────────────────────────────────────────────────────────────

def test_뭉친_단계가_요구로_채워진다():
    """🔴 실측 13:54·13:59의 재현 — 분석 7단계인데 must 5개."""
    analysis = _analysis("네이버 접속", "증권 클릭", "국내 금 클릭",
                         "일별 시세 표 확인", "엑셀에 입력", "테두리 설정", "메일 발송")
    spec = _spec(
        _must("req-1", "네이버에 접속한다", "step-1"),
        _must("req-2", "증권을 클릭한다", "step-2"),
        _must("req-3", "국내 금을 클릭한다", "step-3"),
        _must("req-4", "표에서 3일치를 엑셀에 반영한다", "step-4"),  # step-5를 뭉쳤다
        _must("req-5", "테두리를 설정한다", "step-6"),
        _must("req-6", "메일로 발송한다", "step-7"),
    )
    added = anchor_musts_to_analysis(spec, analysis)

    assert added == ["step-5"]
    assert len(_musts(spec)) == 7, "분석 단계 수와 must 요구 수가 같아진다"


def test_채운_요구는_분석_단계에서_가져온다():
    """창작이 아니다 — 분석은 이미 같은 입력에서 뽑은 사실이라 뒤로 나르는 것뿐이다."""
    spec = _spec()
    anchor_musts_to_analysis(spec, _analysis("엑셀에 입력"), from_document=True)

    (r,) = spec["requirements"]
    assert "엑셀에 입력" in r["text"] and "설명" in r["text"]
    assert (r["priority"], r["source"], r["step_id"]) == ("must", "doc", "step-1")


def test_문서가_없으면_출처가_chat이다():
    """분석의 출처를 따른다 — 없는 근거를 doc이라고 표시하면 안 된다."""
    spec = _spec()
    anchor_musts_to_analysis(spec, _analysis("x"), from_document=False)

    assert spec["requirements"][0]["source"] == "chat"


def test_이미_1대1이면_아무것도_안_한다():
    """실측 13:56의 형태 — 보정이 필요 없는 정상 경로에서 요구를 늘리지 않는다."""
    spec = _spec(_must("req-1", "a", "step-1"), _must("req-2", "b", "step-2"))
    before = len(spec["requirements"])

    assert anchor_musts_to_analysis(spec, _analysis("a", "b")) == []
    assert len(spec["requirements"]) == before


def test_should_요구는_단계를_덮지_않는다():
    """🔴 채점 분모는 must만 센다. should가 단계를 가리면 그 단계는 분모에서 사라진다."""
    spec = _spec({"req_id": "req-1", "text": "a", "priority": "should",
                  "source": "inferred", "step_id": "step-1"})

    assert anchor_musts_to_analysis(spec, _analysis("a")) == ["step-1"]


def test_step_id가_없는_must는_단계를_덮지_않는다():
    """모델이 step_id를 안 붙이면 어느 단계를 담당하는지 알 수 없다 — 모르는 것을 덮었다고
    치면 그 단계가 조용히 분모에서 빠진다."""
    spec = _spec(_must("req-1", "a", None))

    assert anchor_musts_to_analysis(spec, _analysis("a")) == ["step-1"]


# ── 경계 ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("analysis", [None, {}, {"steps": []}, {"steps": [{"name": "id 없음"}]}])
def test_분석이_없으면_손대지_않는다(analysis):
    """edit 경로·최소 스펙처럼 분석이 없는 자리에서 요구를 지어내면 안 된다."""
    spec = _spec(_must("req-1", "a"))

    assert anchor_musts_to_analysis(spec, analysis) == []
    assert len(spec["requirements"]) == 1


def test_단계가_너무_많으면_보정하지_않는다():
    """🔴 분석이 비정상적으로 잘게 쪼개졌을 때(파싱 사고 등) 요구가 수십 건으로 부풀면
    채점 기준이 통째로 무너진다 — 고치려던 문제를 더 크게 만든다."""
    analysis = _analysis(*[f"단계 {i}" for i in range(MAX_ANCHORED_MUSTS + 1)])
    spec = _spec()

    assert anchor_musts_to_analysis(spec, analysis) == []
    assert spec["requirements"] == []


def test_이름도_설명도_없는_단계는_건너뛴다():
    """나를 사실이 없다 — 빈 요구를 만들면 영원히 못 채우는 blocker가 된다."""
    spec = _spec()
    added = anchor_musts_to_analysis(
        spec, {"steps": [{"step_id": "step-1"}, {"step_id": "step-2", "name": "실체"}]}
    )

    assert added == ["step-2"]


def test_긴_설명은_잘라_담는다():
    """요구 문장은 프롬프트·카드·채점에 그대로 실린다 — 한 줄이 예산을 먹으면 안 된다."""
    spec = _spec()
    anchor_musts_to_analysis(spec, {"steps": [{"step_id": "step-1", "name": "x" * 500}]})

    assert len(spec["requirements"][0]["text"]) <= 300


# ── 배선 ─────────────────────────────────────────────────────────────────────

def test_스펙_스칼라가_관측에_남는다(monkeypatch):
    """🔴 spec 프레임은 partial이라 turn_events에서 빠진다 — 채점 분모를 재는 숫자가
    관측에 없으면 '요구가 5였나 7이었나'를 사후에 알 수 없다(실측에서 실제로 겪었다).

    `anchored`가 곧 프롬프트 규칙이 먹히는지를 재는 축이다 — 0으로 수렴해야 자리를 잡은 것.
    """
    from app.agent.v4.orchestrator import spec as spec_mod

    events: list[dict] = []
    monkeypatch.setattr(spec_mod, "emit", lambda ev: events.append(ev))
    monkeypatch.setattr(spec_mod, "chat_json", lambda *a, **k: spec_mod._SpecDraft(
        goal="g", requirements=[
            {"req_id": "req-1", "text": "a", "priority": "must", "source": "doc",
             "step_id": "step-1"},
        ],
    ))

    spec_mod.build_flow_spec({"analysis": _analysis("a", "b"), "message": "m"}, None)

    (ev,) = [e for e in events if "musts" in (e.get("data") or {})]
    assert ev["event"] == "stage", "partial이면 관측에서 걸러진다"
    assert ev["data"]["analysis_steps"] == 2
    assert ev["data"]["musts"] == 2, "보정 뒤의 수 — 분모가 그 값이다"
    assert ev["data"]["anchored"] == ["step-2"]


def test_보정된_요구도_req_id를_받는다(monkeypatch):
    """req_id는 L2 커버리지·심판·질문 카드의 공유 앵커다 — 비어 있으면 그 요구는
    어느 채점에도 안 잡히고, 담당 액션을 달아 줄 방법도 없다."""
    from app.agent.v4.orchestrator import spec as spec_mod

    monkeypatch.setattr(spec_mod, "emit", lambda ev: None)
    monkeypatch.setattr(spec_mod, "emit_spec_frame", lambda *a, **k: None)
    monkeypatch.setattr(spec_mod, "chat_json", lambda *a, **k: spec_mod._SpecDraft(goal="g"))

    out = spec_mod.build_flow_spec({"analysis": _analysis("a", "b"), "message": "m"}, None)

    ids = [r["req_id"] for r in out["requirements"]]
    assert all(ids) and len(set(ids)) == len(ids)
