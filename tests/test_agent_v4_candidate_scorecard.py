# -*- coding: utf-8 -*-
"""후보별 채점 결과가 관측에 남는다 (RPA-298).

## 무엇을 막는가 (실측, 2026-07-27)

같은 업무를 5분 간격으로 두 번 돌렸는데 1상 산출물이 갈렸다:

    13:00:42  ea4d22b1   초기 가중합  196  →  교정 후 103   (Recorder/Click, Try 안에 업무 본체)
    13:05:38  84f545a5   초기 가중합 1319  →  교정 후 256   (Browser/Open ×5, Try 안에 메일만)

교정은 1319를 256까지만 끌어내렸다 — **나쁜 출발점은 교정으로 복구되지 않는다.** 그러니
재현성의 지렛대는 refine이 아니라 1상인데, 정작 다음 질문에 답할 수 없었다:

    후보 셋이 다 나빴나?  아니면 좋은 후보가 있는데 심판이 다른 걸 골랐나?

두 원인은 처방이 정반대다(전자는 조사·compose, 후자는 심판). 그런데 후보 요약과 심판
점수판은 `partial` 이벤트라 `sessions._tev`가 **의도적으로** turn_events에서 제외한다
(흐름도 트리가 통째로 실리므로 옳은 결정이다). 그래서 스칼라만 stage로 한 번 더 낸다.
"""

import pytest

from app.agent.v4.orchestrator.judge import CandidateReport
from app.agent.v4.recommend import graph as g
from app.agent.v4.verify.findings import Finding


def _report(cid: str, *, weight_findings: list[Finding], cov=1.0, sim=1.0, gates=()):
    return CandidateReport(
        candidate_id=cid, persona=f"페르소나 {cid}", flow={"steps": []},
        findings=weight_findings, must_coverage=cov, sim_pass_rate=sim,
        gate_failures=list(gates),
    )


def _f(severity: str) -> Finding:
    return Finding(layer="L0", severity=severity, rule="R7", message="x")


@pytest.fixture
def events(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(g, "emit", lambda ev: seen.append(ev))
    return seen


def _card(events) -> dict:
    (e,) = [x for x in events if "candidates" in (x.get("data") or {})]
    return e["data"]


def test_후보별_가중합이_남는다(events):
    """🔴 '후보가 다 나빴나 / 심판이 잘못 골랐나'를 가르는 유일한 축."""
    reports = [
        _report("A", weight_findings=[_f("blocker")] * 13),   # 1300
        _report("B", weight_findings=[_f("major")] * 2),      # 20
    ]
    verdict = {"winner": reports[0],
               "verdict": {"winner": "A", "scores": [{"candidate_id": "A", "total": 0.9},
                                                     {"candidate_id": "B", "total": 0.4}]}}
    g._emit_candidate_scorecard(reports, verdict, attempted=3)

    data = _card(events)
    by_id = {c["id"]: c for c in data["candidates"]}
    assert by_id["A"]["weight"] == 1300 and by_id["B"]["weight"] == 20
    assert by_id["A"]["blockers"] == 13
    assert data["weight_spread"] == [20, 1300]


def test_승자가_가중합_최소가_아니면_드러난다(events):
    """🔴 실측에서 답을 못 얻은 그 질문. 승자가 최경량이 아니라면 심판이 다른 축을
    우선했다는 뜻이고, 그 판단이 옳았는지를 이 한 줄로 되짚을 수 있다."""
    reports = [
        _report("A", weight_findings=[_f("blocker")] * 13),
        _report("B", weight_findings=[_f("major")] * 2),
    ]
    verdict = {"winner": reports[0], "verdict": {"winner": "A", "scores": []}}
    g._emit_candidate_scorecard(reports, verdict, attempted=3)

    assert _card(events)["winner_is_lightest"] is False


def test_승자가_최경량이면_그렇다고_남는다(events):
    reports = [
        _report("A", weight_findings=[_f("major")]),
        _report("B", weight_findings=[_f("blocker")]),
    ]
    verdict = {"winner": reports[0], "verdict": {"winner": "A", "scores": []}}
    g._emit_candidate_scorecard(reports, verdict, attempted=2)

    assert _card(events)["winner_is_lightest"] is True


def test_몇_개를_내보내_몇_개가_살아왔는지_남는다(events):
    """후보 하나만 살면 심판은 고를 것이 없고 confidence의 합의 항도 통째로 꺼진다 —
    '그 턴에 경쟁이 없었다'는 사실이 기록에 남아야 재현성 논의가 성립한다."""
    reports = [_report("A", weight_findings=[])]
    verdict = {"winner": reports[0], "verdict": {"winner": "A", "scores": []}}
    g._emit_candidate_scorecard(reports, verdict, attempted=3)

    data = _card(events)
    assert (data["attempted"], data["survived"]) == (3, 1)


def test_심판_점수와_결정론_점수가_함께_남는다(events):
    """둘이 갈리는 지점이 곧 '심판이 결정론 신호를 뒤집은 자리'다."""
    reports = [_report("A", weight_findings=[_f("major")], cov=0.5, sim=0.8)]
    verdict = {"winner": reports[0],
               "verdict": {"winner": "A", "scores": [{"candidate_id": "A", "total": 0.77}]}}
    g._emit_candidate_scorecard(reports, verdict, attempted=1)

    (row,) = _card(events)["candidates"]
    assert row["judge"] == 0.77
    assert row["det"] == reports[0].deterministic_score()
    assert (row["must_coverage"], row["sim_pass_rate"]) == (0.5, 0.8)


def test_흐름도나_이유_문장은_싣지_않는다(events):
    """`partial`이 관측에서 빠진 이유가 부피다 — 여기서 트리를 다시 실으면 같은 문제를
    되살리고, 사용자 업무 내용이 관측 DB로 새는 경로도 함께 연다."""
    reports = [_report("A", weight_findings=[])]
    reports[0].flow = {"steps": [{"step_id": "s", "label": "증권 버튼 클릭", "actions": []}]}
    verdict = {"winner": reports[0],
               "verdict": {"winner": "A", "reason": "내부시스템 고객 12345 처리가 낫다",
                           "scores": []}}
    g._emit_candidate_scorecard(reports, verdict, attempted=1)

    blob = repr(_card(events))
    assert "증권 버튼 클릭" not in blob
    assert "내부시스템" not in blob


def test_stage_이벤트라_관측에_남는다(events):
    """partial이면 sessions._tev가 걸러 낸다 — 이 이벤트의 존재 이유가 그것이다."""
    reports = [_report("A", weight_findings=[])]
    verdict = {"winner": reports[0], "verdict": {"winner": "A", "scores": []}}
    g._emit_candidate_scorecard(reports, verdict, attempted=1)

    (e,) = [x for x in events if "candidates" in (x.get("data") or {})]
    assert e["event"] == "stage"


def test_리포트_모양이_달라도_턴을_죽이지_않는다(events):
    """🔴 관측이 산출 경로를 죽이면 안 된다 — 필드 하나가 비어서 그 턴 전체가 실패하는 것보다
    그 칸만 비는 편이 낫다(core.llm._log_llm_failure와 같은 원칙).

    실제로 2상 테스트의 대역이 must_coverage·deterministic_score 없는 셔틀을 넘겨 이 자리에서
    AttributeError가 났다 — 대역이니 망정이지 운영에서 같은 일이 나면 턴이 죽는다.
    """
    from types import SimpleNamespace

    shim = SimpleNamespace(candidate_id="A", persona="p", flow={}, violations=[], findings=[])
    g._emit_candidate_scorecard([shim], {"winner": shim, "verdict": {"winner": "A"}}, attempted=1)

    (row,) = _card(events)["candidates"]
    assert row["id"] == "A"
    assert row["det"] is None and row["must_coverage"] is None
