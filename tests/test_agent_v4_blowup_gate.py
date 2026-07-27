# -*- coding: utf-8 -*-
"""폭주 후보는 커버리지로 구제되지 않는다 (RPA-298).

## 무엇을 막는가 (실측, 2026-07-27 14:22)

심판이 **액션 108개짜리 중복 흐름도**를 골랐다.

    C  가중합 192 · 커버리지 0.857 · blocker 0 · 액션  30
    A  가중합 462 · 커버리지 1.000 · blocker 0 · 액션 108   ← 승자, R7(세션 미개방) 39건

A는 같은 `Excel advanced` 블록 48개가 통째로 두 번 반복된 흐름도였다. 그런데
`deterministic_score`의 위반 항 `1/(1+w/20)`이 포화한다:

    w=20 → 0.500    w=100 → 0.167    w=192 → 0.094    w=462 → 0.041

192와 462의 차이가 점수에서 `0.053 × 0.3 = 0.016`인데, 커버리지 1.0 vs 0.857 차이는
`0.143 × 0.5 = 0.072`로 **4.5배**다. 가중합이 100을 넘으면 위반 축이 판별을 멈추고
"요구를 (중복해서) 다 커버했다"가 이긴다. 교정 4라운드로도 R7 39건은 하나도 못 줄였다.

점수 함수를 재설계하는 대신 **자격 게이트**를 둔다 — 커버리지가 아무리 좋아도 부피가
폭주한 후보는 승자가 될 수 없다.
"""

import pytest

from app.agent.v4.orchestrator.judge import (
    CandidateReport,
    _pick_eligible,
    blowup_excluded,
    flow_action_count,
)
from app.agent.v4.verify.findings import Finding


def _flow(n_actions: int, *, nested: int = 0) -> dict:
    leaf = [{"package": "P", "action": "A", "children": []} for _ in range(n_actions)]
    if nested:
        leaf[0]["children"] = [{"package": "P", "action": "A", "children": []}
                               for _ in range(nested)]
    return {"steps": [{"step_id": "s1", "actions": leaf}]}


def _cand(cid: str, *, weight: int, actions: int = 10, cov: float = 1.0) -> CandidateReport:
    """가중합을 major(10점) 개수로 만든다 — blocker를 쓰면 다른 게이트가 먼저 걸린다."""
    return CandidateReport(
        candidate_id=cid, persona=cid, flow=_flow(actions),
        findings=[Finding(layer="L0", severity="major", rule="R7", message="x")
                  for _ in range(weight // 10)],
        must_coverage=cov, sim_pass_rate=1.0,
    )


# ── 액션 수 세기 ─────────────────────────────────────────────────────────────

def test_컨테이너_children까지_센다():
    """중복 생성은 컨테이너 안에서도 일어난다 — 최상위만 세면 108개를 30개로 읽는다."""
    assert flow_action_count(_flow(3, nested=4)) == 7


@pytest.mark.parametrize("flow", [None, {}, {"steps": None}, {"steps": [None]}])
def test_흐름도가_비어도_죽지_않는다(flow):
    assert flow_action_count(flow) == 0


# ── 폭주 판정 ────────────────────────────────────────────────────────────────

def test_실측_사고가_막힌다():
    """🔴 14:22의 재현 — 커버리지 1.0짜리 462가 커버리지 0.857짜리 192를 이기지 못한다."""
    heavy = _cand("A", weight=460, actions=108, cov=1.0)
    light = _cand("C", weight=190, actions=30, cov=0.857)

    assert "A" in blowup_excluded([heavy, light])
    assert [r.candidate_id for r in _pick_eligible([heavy, light], {})] == ["C"]


def test_액션_수만_폭주해도_잡는다():
    """가중합이 비슷해도 중복 생성은 부피로 드러난다 — 검수가 못 보는 결함이 여기 있다."""
    dup = _cand("A", weight=50, actions=100)
    normal = _cand("B", weight=50, actions=30)

    assert "중복 생성 의심" in blowup_excluded([dup, normal])["A"]


def test_비율이_안_되면_안_뺀다():
    """2배 미만은 정상 편차다 — 여기서 빼면 매 라운드 한 후보가 사라진다."""
    assert blowup_excluded([_cand("A", weight=100), _cand("B", weight=80)]) == {}


def test_작은_수의_비율은_잡음이다():
    """🔴 20 vs 50은 2.5배지만 실질 차이가 아니다 — 바닥값이 없으면 좋은 후보가 잘린다."""
    assert blowup_excluded([_cand("A", weight=50), _cand("B", weight=20)]) == {}


def test_최경량이_0이어도_바닥값이_기준을_준다():
    """무결점 후보가 있으면 비율이 무한이 된다 — 바닥값만으로 판정해야 한다."""
    assert "A" in blowup_excluded([_cand("A", weight=200), _cand("B", weight=0)])


def test_전원이_폭주면_아무도_안_뺀다():
    """비교 기준이 자기 자신이 되면 판정이 무의미하고, 어차피 하나는 골라야 한다."""
    assert blowup_excluded([_cand("A", weight=500, actions=200),
                            _cand("B", weight=480, actions=190)]) == {}


def test_후보가_하나면_판정하지_않는다():
    """상대 비교라 비교 대상이 없으면 성립하지 않는다."""
    assert blowup_excluded([_cand("A", weight=900, actions=300)]) == {}


# ── 자격 완화와의 관계 ───────────────────────────────────────────────────────

def test_폭주_게이트는_완화의_수혜자가_되지_않는다():
    """🔴 이 게이트의 요점.

    아래 층들(L2 게이트·반증 게이트)은 "그래도 하나는 골라야 한다"는 이유로 완화된다.
    폭주 후보가 그 완화를 타고 돌아오면 정확히 막으려던 사고가 재현된다 —
    커버리지가 좋다는 이유로 108액션 흐름도가 뽑히는 것.
    """
    heavy = _cand("A", weight=460, actions=108, cov=1.0)
    light = _cand("C", weight=190, actions=30, cov=0.857)
    light.gate_failures = ["req-3"]  # 가벼운 쪽이 L2 게이트에 걸렸다

    # 자격은 완화되지만 폭주 후보는 여전히 밖이다
    assert [r.candidate_id for r in _pick_eligible([heavy, light], {})] == ["C"]


def test_폭주가_없으면_기존_자격_규칙_그대로다():
    """회귀 가드 — 이 게이트는 폭주가 없을 때 아무것도 바꾸면 안 된다.

    두 번째 단언은 기존 계약 그대로다: A는 반증(LLM)에 걸렸고 B는 L2 하드 게이트(결정론)에
    걸렸는데, 둘 다 못 만족시킬 때는 **반증 게이트를 먼저 포기**한다 — 믿을 것은 결정론
    쪽이므로 L2를 통과한 A가 남는다(_pick_eligible 독스트링 ②).
    """
    a = _cand("A", weight=30)
    b = _cand("B", weight=20)
    b.gate_failures = ["req-1"]

    assert [r.candidate_id for r in _pick_eligible([a, b], {})] == ["A"]
    assert [r.candidate_id for r in _pick_eligible([a, b], {"A": True})] == ["A"]


def test_전원_폭주면_기존_규칙으로_되돌아간다():
    a = _cand("A", weight=500, actions=200)
    b = _cand("B", weight=480, actions=190)
    b.gate_failures = ["req-1"]

    assert [r.candidate_id for r in _pick_eligible([a, b], {})] == ["A"]
