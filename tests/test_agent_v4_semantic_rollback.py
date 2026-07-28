# -*- coding: utf-8 -*-
"""교정이 '봇이 업무를 하는가'를 나쁘게 만들면 되돌린다 (RPA-298).

## 무엇을 막는가 (실측, 2026-07-28 00:46)

교정 라운드 1이 가중합을 200 → **0**으로 만들었다. 그런데 산출물은 이랬다:

    Browser/Call a JavaScript function  «증권 클릭»     ← 버튼 클릭을 JS 호출로
    Browser/Call a JavaScript function  «국내 금 클릭»
    Loop action for data iteration      «3일 반복»
      └ Step/Step                                      ← 본문이 비실행 구획
    Microsoft 365 Excel/Paste cell      «표 붙여넣기»    ← 복사한 적이 없다
    Email/Forward                       «메일 보내기»    ← 전달할 원본 메일이 없다

**데이터를 읽는 액션이 하나도 없다**(Get table·Get multiple cells·Extract 전무). 시세를
수집하지 않는 시세 수집 봇이다. surgeon이 R1(없는 표기 `Recorder/Click on cell by text`)을
만나자 "카탈로그에 실재하고 필수 파라미터가 적은 아무 액션"으로 갈아끼워 정적 위반을 0으로
만들었다.

라운드별 회귀 가드는 이걸 못 막는다 — 그 축이 **정적 위반 + req_id 배정**뿐이라 req_id를 단 채
액션만 바꾸면 둘 다 만족한다. "이 액션이 그 요구를 실제로 수행하는가"는 어느 축에도 없다.

그래서 **전체 단위**로 한 번 더 잰다. 교정 뒤 L2/L3 재채점은 이미 돌고 있어(신뢰도 갱신용)
LLM 추가 비용이 0이다 — 표시에만 쓰던 결과를 판정에도 쓴다.
"""

import pytest

from app.agent.v4.recommend.graph import _SEMANTIC_REGRESSION_EPS, semantic_regressions


def _before(cov=1.0, sim=1.0) -> dict:
    return {"must_coverage": cov, "sim_pass_rate": sim}


# ── 판정 ─────────────────────────────────────────────────────────────────────

def test_요구_충족이_떨어지면_회귀다():
    """🔴 실측 00:46 — 정적 위반은 0이 됐지만 요구를 실제로 수행하지 않게 됐다."""
    assert semantic_regressions(_before(cov=0.857), 0.571, 1.0) == ["must_coverage"]


def test_정상경로_통과율이_떨어지면_회귀다():
    assert semantic_regressions(_before(sim=1.0), 1.0, 0.5) == ["nominal_pass_rate"]


def test_둘_다_떨어지면_둘_다_보고한다():
    """어느 축이 무너졌는지가 다음 진단의 출발점이다 — 하나로 뭉치면 원인을 못 짚는다."""
    assert semantic_regressions(_before(0.9, 1.0), 0.5, 0.0) == [
        "must_coverage", "nominal_pass_rate"
    ]


def test_좋아지면_회귀가_아니다():
    assert semantic_regressions(_before(0.5, 0.5), 1.0, 1.0) == []


def test_같으면_회귀가_아니다():
    """정적 수리만 하고 의미는 그대로인 정상 교정 — 여기서 물면 모든 교정이 무효가 된다."""
    assert semantic_regressions(_before(0.857, 0.5), 0.857, 0.5) == []


def test_부동소수_오차는_무시한다():
    """0.857142…처럼 나눗셈에서 온 값이 재계산으로 미세하게 흔들려도 되돌리지 않는다."""
    assert semantic_regressions(_before(cov=5 / 7), 5 / 7 - _SEMANTIC_REGRESSION_EPS / 2, 1.0) == []


def test_요구_한_건_하락은_확실히_문다():
    """실제 눈금 — must 7건이면 한 건 하락이 0.143이라 허용치(0.01)보다 훨씬 크다."""
    assert semantic_regressions(_before(cov=7 / 7), 6 / 7, 1.0) == ["must_coverage"]


# ── 모름 → 침묵 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("before,after", [
    ({"must_coverage": None, "sim_pass_rate": 1.0}, (None, 1.0)),
    ({"must_coverage": 1.0, "sim_pass_rate": None}, (1.0, None)),
    ({}, (None, None)),
])
def test_비교값이_없으면_판정하지_않는다(before, after):
    """🔴 교정 전 L2/L3가 실패해 값이 없는 라운드에서 되돌리면, **인프라 실패가 교정을
    무효화**한다. 모르는 것을 나빠졌다고 치면 안 된다."""
    assert semantic_regressions(before, *after) == []


def test_한쪽만_알아도_그_축은_본다():
    """전부 아니면 전무가 아니다 — 아는 축은 판정한다."""
    assert semantic_regressions({"must_coverage": 1.0, "sim_pass_rate": None}, 0.5, None) == [
        "must_coverage"
    ]


# ── 배선 ─────────────────────────────────────────────────────────────────────

def test_되돌리면_교정_전_상태로_돌아간다(monkeypatch):
    """🔴 '교정이 한 라운드도 채택되지 않은 경로'와 **정확히 같은 상태**여야 한다 —
    흐름도만 되돌리고 커버리지·통과율은 교정 후 값을 남기면, 사용자가 보는 신뢰도가
    화면에 없는 흐름도의 것이 된다."""
    from app.agent.v4.recommend import graph as g

    events: list[dict] = []
    monkeypatch.setattr(g, "emit", lambda ev: events.append(ev))

    before = {"must_coverage": 0.857, "sim_pass_rate": 1.0}
    assert semantic_regressions(before, 0.571, 1.0) == ["must_coverage"]
    # 되돌린 뒤 쓰이는 값은 교정 **전** 값이다(호출부가 winner_report에서 다시 읽는다).
    assert before["must_coverage"] == 0.857 and before["sim_pass_rate"] == 1.0


def test_회귀_판정이_관측_가능한_이름을_쓴다():
    """이벤트·로그에 그대로 실리는 문자열이라, 바꾸면 관측 질의가 조용히 깨진다."""
    assert set(semantic_regressions(_before(1.0, 1.0), 0.0, 0.0)) == {
        "must_coverage", "nominal_pass_rate"
    }
