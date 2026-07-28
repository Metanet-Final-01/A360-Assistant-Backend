# -*- coding: utf-8 -*-
"""시뮬레이션의 점수축과 결함축을 가른다 (RPA-298).

## 무엇을 막는가 (실측, 2026-07-28 — 같은 업무로 3회)

    00:02  예외처리 없음 → 트레이스 2개(happy·alt)      → 통과율 1.00
    00:05  예외처리 없음 → 트레이스 2개                 → 통과율 0.50
    00:07  예외처리 있음 → 트레이스 3개(+error)         → 통과율 0.00

`error` 트레이스는 **Error handler가 있는 흐름도에만** 만들어진다(build_traces의 `has_eh`).
그런데 그 경로는 통과가 가장 어렵다 — Catch가 실제로 수습하고, Finally가 정리하고, 오류 뒤
후속 단계가 안 돌아야 한다. 통과율에 섞으면 **예외 처리를 넣은 후보만 심사를 하나 더 받고
점수가 깎인다.**

그 벌점은 혼자가 아니었다. R12(예외 처리 없음)는 warning이라 교정 목적 함수에서 빠지고,
회귀 가드는 Try/Catch wrap을 '가중합 무변화'로 항상 폐기한다. **시스템 전체가 예외 처리를
억제하고 있었고**, 그래서 산출물이 자꾸 평면으로 나왔다.

점수에서 빼되 **결함으로 옮긴다** — 예외 경로 실패는 major finding으로 surgeon에게 간다.
없애는 게 아니라 축을 바꾸는 것이다.

## 실측이 확인한 것 하나 더

00:07의 예외 경로 트레이스는 이랬다:

    [Catch 진입]   Excel advanced/Set cell «오류중지»
    [Finally 실행] Browser/Close «브라우저닫기»
    == STEP step-3 ==  Excel advanced/Open ...        ← 오류가 났는데 그대로 계속된다
    == STEP step-6 ==  Email/Connect → ... → Disconnect

오류가 나도 봇이 멈추지 않고 빈 메일을 보낸다. 판정관의 `ok=false`는 **옳았다** —
시뮬레이터가 과하게 엄격한 것이 아니다.
"""

import pytest

from app.agent.v4.verify.findings import from_simulation
from app.agent.v4.verify.simulate import SimulationReport, TraceVerdict


def _report(**by_trace: bool) -> SimulationReport:
    return SimulationReport(verdicts=[
        TraceVerdict(trace_id=tid, ok=ok, issues=[] if ok else [f"{tid} 실패"])
        for tid, ok in by_trace.items()
    ])


# ── 점수축: 예외 경로를 섞지 않는다 ──────────────────────────────────────────

def test_예외_경로_실패가_통과율을_깎지_않는다():
    """🔴 이 변경의 요점 — 예외 처리를 넣었다는 이유로 점수가 깎이면 안 된다."""
    assert _report(happy=True, alt=True, error=False).nominal_pass_rate == 1.0


def test_예외처리_유무가_같은_점수를_준다():
    """🔴 실측 00:02(2경로 1.00) vs 00:07(3경로 0.00)의 비대칭이 사라지는 자리.

    정상 경로 판정이 같다면 예외 경로 심사를 더 받았다는 이유만으로 뒤처지지 않는다.
    """
    without_eh = _report(happy=True, alt=True)
    with_eh = _report(happy=True, alt=True, error=False)

    assert without_eh.nominal_pass_rate == with_eh.nominal_pass_rate


def test_정상_경로_실패는_그대로_깎는다():
    """회귀 가드 — 점수축이 무뎌지면 안 된다. 실측 00:07은 happy·alt도 실패했다."""
    assert _report(happy=False, alt=True, error=False).nominal_pass_rate == 0.5
    assert _report(happy=False, alt=False, error=False).nominal_pass_rate == 0.0


def test_판정이_없으면_중립이다():
    """L3가 죽은 라운드를 0점으로 치면 인프라 실패가 품질 점수로 둔갑한다."""
    assert SimulationReport().nominal_pass_rate == 1.0


# ── 결함축: 예외 경로 판정은 사라지지 않는다 ────────────────────────────────

def test_예외_경로_실패는_finding으로_간다():
    """🔴 점수에서 뺀 대신 **결함으로 옮긴** 자리. 이 변환이 빠지면 "오류가 나도 봇이
    계속 돌아 빈 메일을 보낸다"가 통째로 사라진다."""
    (f,) = from_simulation(_report(happy=True, alt=True, error=False))

    assert f.severity == "major" and f.layer == "L3"
    assert "[error]" in f.message


def test_예외_경로_결함에는_수리_힌트가_붙는다():
    """L3 결함은 규칙 이름이 없어(rule=None) 프롬프트에서 맥락 없는 한 줄로 보인다 —
    무엇을 해야 하는지가 같이 가야 surgeon이 움직인다."""
    (f,) = from_simulation(_report(happy=True, alt=True, error=False))

    assert f.fix_hint and "Catch" in f.fix_hint


def test_정상_경로_결함에는_예외_힌트를_붙이지_않는다():
    """엉뚱한 힌트는 없는 힌트보다 나쁘다 — surgeon이 그 방향으로 라운드를 태운다."""
    (f,) = from_simulation(_report(happy=False, alt=True))

    assert f.fix_hint is None


# ── 예외 경로 판정의 3상태 ───────────────────────────────────────────────────

def test_예외처리가_없으면_판정_안_함이다():
    """🔴 None(경로 없음)과 False(수습 실패)는 처방이 다르다 — 하나로 뭉치면 예외 처리가
    아예 없는 흐름도가 '예외 경로 통과'로 보인다(모름 → 침묵)."""
    assert _report(happy=True, alt=True).error_path_ok is None


@pytest.mark.parametrize("ok", [True, False])
def test_예외처리가_있으면_판정이_남는다(ok):
    assert _report(happy=True, alt=True, error=ok).error_path_ok is ok


# ── 배선 ─────────────────────────────────────────────────────────────────────

def test_점수에_쓰이는_값이_nominal이다():
    """옛 `pass_rate`(전 경로 평균)를 지운 이유 — 이름이 같으면 호출부가 조용히
    옛 의미를 유지한다(관측 필드 `params`→`params_sent` 개명과 같은 원칙)."""
    assert not hasattr(SimulationReport(), "pass_rate")


def test_후보_보고에_예외_경로_판정이_실린다():
    """관측·심판이 '예외 처리가 없다'와 '수습을 못 한다'를 구별할 수 있어야 한다."""
    from app.agent.v4.orchestrator.judge import CandidateReport

    assert "error_path_ok" in CandidateReport.model_fields
