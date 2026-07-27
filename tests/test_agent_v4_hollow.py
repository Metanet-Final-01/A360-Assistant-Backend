# -*- coding: utf-8 -*-
"""빈껍데기 — 요구를 담당한다고 주장하는 자리가 아무것도 실행하지 않는 경우 (RPA-298).

## 무엇을 막는가 (실측, 2026-07-27)

같은 산출물에서 두 지표가 정면으로 어긋났다:

    verifying | 검수 위반 20건 · 요구 누락 0건 · 요구 뭉갬 0건
    R18 major | '3일치 시세를 순서대로 기록'이 요구 req-4를 담당한다고 돼 있는데
                Step은 실행되지 않는 구획입니다

req-4는 **must**("최근 3일치 일별 시세를 엑셀에 입력한다")인데 담당이 `Step` 하나뿐이라
실행되는 것이 없다. 그런데 커버리지는 req_id가 붙어 있다는 사실만 보고 "누락 0건"이라
답했다 — 액션을 지우면 blocker가 나지만 **이름만 남긴 구획으로 바꾸면 무신호**였다.
검수를 통과하는 삭제 경로가 하나 열려 있었다는 뜻이다.
"""

import pytest

from app.agent.v4.verify.checker import performs_work, run_scaffold_checks
from app.agent.v4.verify.coverage_det import (
    completeness_findings,
    hollow_findings,
    hollow_requirements,
    missing_requirements,
)
from app.agent.v4.verify.findings import weight


def _spec(*reqs):
    return {"goal": "g", "requirements": [
        {"req_id": f"req-{i}", "text": t, "priority": p}
        for i, (t, p) in enumerate(reqs, 1)
    ]}


def _flow(*actions):
    return {"steps": [{"step_id": "step-1", "label": "s", "actions": list(actions)}]}


def _act(pkg, act, req_id=None, children=None, label=None):
    a = {"package": pkg, "action": act, "label": label or act}
    if req_id:
        a["req_id"] = req_id
    if children is not None:
        a["children"] = children
    return a


_SPEC1 = _spec(("최근 3일치 일별 시세를 엑셀에 입력한다.", "must"))


# ── 실측 재현 ────────────────────────────────────────────────────────────────

def test_실측_결함이_잡힌다():
    """🔴 이 파일의 존재 이유 — Step 하나가 must를 담당하면 아무 일도 일어나지 않는다."""
    flow = _flow(_act("Step", "Step", "req-1", label="3일치 시세를 순서대로 기록"))

    assert [h["req_id"] for h in hollow_requirements(flow, _SPEC1)] == ["req-1"]
    assert [f.severity for f in hollow_findings(flow, _SPEC1)] == ["blocker"]


def test_커버리지가_더는_만족이라_답하지_않는다():
    """🔴 모순의 핵심. 예전엔 여기서 '누락 0건'만 나오고 끝이었다."""
    flow = _flow(_act("Step", "Step", "req-1"))

    # 배정은 있으므로 '누락'은 여전히 0 — 그래서 별도 항이 필요했다
    assert missing_requirements(flow, _SPEC1) == []
    # 그러나 완성도 축에는 blocker가 잡힌다
    assert weight(completeness_findings(flow, _SPEC1)) >= 100


def test_실행되는_액션이면_침묵한다():
    flow = _flow(_act("Excel advanced", "Set cell", "req-1"))
    assert hollow_requirements(flow, _SPEC1) == []


# ── 오탐 경계 ────────────────────────────────────────────────────────────────

def test_같은_요구를_실행_액션도_들고_있으면_침묵한다():
    """🔴 실측에서 req-7이 이 경우였다 — Step과 `Browser/Get source code`가 함께 담당.

    구획이 묶고 액션이 하는 것은 정상이다. 이걸 잡으면 잘 만든 흐름도가 발화한다.
    """
    flow = _flow(
        _act("Step", "Step", "req-1", label="행 식별 로직"),
        _act("Browser", "Get source code", "req-1"),
    )
    assert hollow_requirements(flow, _SPEC1) == []


def test_본문이_실행되는_구획은_정상이다():
    """Step이 실제 액션을 묶고 있으면 본문이 실행한다."""
    flow = _flow(_act("Step", "Step", "req-1",
                      children=[_act("Excel advanced", "Set cell")]))
    assert hollow_requirements(flow, _SPEC1) == []


def test_구획_안에_구획만_있으면_여전히_빈껍데기다():
    """🔴 children 유무만 보면 이 중첩을 놓친다 — Step > Step은 여전히 아무것도 안 한다."""
    flow = _flow(_act("Step", "Step", "req-1",
                      children=[_act("Step", "Step"), _act("Comment", "Comment")]))
    assert [h["req_id"] for h in hollow_requirements(flow, _SPEC1)] == ["req-1"]


def test_req_id가_없는_구획은_침묵한다():
    """순수 구획 표시는 정상이다 — 요구를 담당한다고 주장할 때만 결함이다."""
    flow = _flow(_act("Step", "Step"), _act("Excel advanced", "Set cell", "req-1"))
    assert hollow_requirements(flow, _SPEC1) == []


def test_모르는_패키지는_실행되는_것으로_본다():
    """이 모듈의 계약은 '오탐 0인 신호만' — 모름은 침묵이지 고발이 아니다."""
    flow = _flow(_act("낯선 패키지", "낯선 액션", "req-1"))
    assert hollow_requirements(flow, _SPEC1) == []


def test_빈_Loop는_여기서_보지_않는다():
    """본문이 빈 Loop도 실행되는 건 없지만 전용 규칙이 따로 본다 — 축을 넘기지 않는다."""
    flow = _flow(_act("Loop", "Loop", "req-1", children=[]))
    assert hollow_requirements(flow, _SPEC1) == []


def test_요구가_없으면_침묵한다():
    assert hollow_requirements(_flow(_act("Step", "Step", "req-1")), {"requirements": []}) == []


# ── 등급과 게임 이론 ─────────────────────────────────────────────────────────

def test_should는_minor다():
    spec = _spec(("첨부할 파일 경로를 확보한다", "should"))
    flow = _flow(_act("Step", "Step", "req-1"))
    assert [f.severity for f in hollow_findings(flow, spec)] == ["minor"]


_SPEC2 = _spec(("최근 3일치 일별 시세를 엑셀에 입력한다.", "must"),
               ("엑셀 표를 메일로 발송한다.", "must"))


def test_구획을_지우기만_해도_가중합이_줄지_않는다():
    """🔴 도피로 차단. 빈껍데기(100)를 지우면 누락(100)이 생겨 상쇄된다.

    회귀 가드는 `new_weight < current_weight`를 요구하므로 그 패치는 채택되지 않는다.
    등급을 누락보다 **낮게** 두면 지우기가 곧 이득이 되어, 이 모듈이 막으려던 삭제
    편향이 구획을 경유해 되살아난다.
    """
    hollow = _flow(_act("Step", "Step", "req-1"), _act("Email", "Send", "req-2"))
    deleted = _flow(_act("Email", "Send", "req-2"))

    assert weight(completeness_findings(hollow, _SPEC2)) == \
           weight(completeness_findings(deleted, _SPEC2)) == 100


def test_흐름도의_앵커가_전멸하면_침묵한다는_기존_계약은_그대로다():
    """⚠️ 경계 기록 — req_id가 **하나도** 없으면 완성도 항 전체가 0이 된다.

    모듈 독스트링의 침묵 원칙("앵커 미기재는 '요구를 안 지켰다'가 아니라 '연결 정보가
    없다'")이라 의도된 동작이고, 빈껍데기 항이 새로 만든 구멍이 아니다(누락도 같다).
    다만 이 경우에 한해 위 상쇄가 성립하지 않으므로 — 유일한 앵커를 지우면 100→0 —
    여기 명시해 둔다. 실제 흐름도는 요구가 여럿이라 이 조건에 닿지 않는다.
    """
    assert weight(completeness_findings(_flow(_act("Step", "Step", "req-1")), _SPEC1)) == 100
    assert weight(completeness_findings(_flow(), _SPEC1)) == 0


def test_실제로_채우면_가중합이_준다():
    """정직한 수리만 통과해야 한다."""
    hollow = _flow(_act("Step", "Step", "req-1"))
    filled = _flow(_act("Step", "Step", "req-1",
                        children=[_act("Excel advanced", "Set cell")]))

    assert weight(completeness_findings(filled, _SPEC1)) < \
           weight(completeness_findings(hollow, _SPEC1))


def test_누락과_상호배타다():
    """같은 요구가 두 항으로 이중 계상되면 가중합이 부풀어 회귀 가드가 왜곡된다."""
    flow = _flow(_act("Step", "Step", "req-1"))
    missing = set(missing_requirements(flow, _SPEC1))
    hollowed = {h["req_id"] for h in hollow_requirements(flow, _SPEC1)}
    assert not (missing & hollowed)


def test_좌표가_붙는다():
    """surgeon이 어느 자리를 고칠지 알아야 한다 — checker/Violation과 같은 경로 표기."""
    flow = {"steps": [{"step_id": "step-9", "actions": [
        _act("Error handler", "Try", children=[_act("Step", "Step", "req-1")]),
    ]}]}
    (f,) = hollow_findings(flow, _SPEC1)
    assert f.step_id == "step-9"
    assert f.location == "actions[0].children[0]"


# ── R18과의 일관성 ───────────────────────────────────────────────────────────

def test_R18도_같은_판정을_쓴다():
    """🔴 정의가 갈라지면 '한쪽은 발화하는데 다른 쪽은 만족'이라는 모순이 되돌아온다."""
    nested = _act("Step", "Step", "req-1", children=[_act("Step", "Step")])
    flow = _flow(nested)

    assert [v.rule for v in run_scaffold_checks(flow["steps"])] == ["R18"]
    assert [h["req_id"] for h in hollow_requirements(flow, _SPEC1)] == ["req-1"]


@pytest.mark.parametrize("action,expected", [
    ({"package": "Excel advanced", "action": "Set cell"}, True),
    ({"package": "Step", "action": "Step"}, False),
    ({"package": "Comment", "action": "Comment"}, False),
    ({"package": "Step", "action": "Step", "children": [{"package": "Loop", "action": "Loop"}]}, True),
    ({"package": "Step", "action": "Step", "children": [{"package": "Step", "action": "Step"}]}, False),
    ({"package": None, "action": None}, True),   # 모름 → 침묵
    ("액션이 아님", False),
])
def test_performs_work(action, expected):
    assert performs_work(action) is expected
