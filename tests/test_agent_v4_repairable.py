# -*- coding: utf-8 -*-
"""고칠 수 있는 위반으로 만들기 — 빈 컨테이너 승격 · surgeon 수리 어휘 (RPA-298).

## 실측 배경 (2026-07-27)

산출물 하나의 교정 목적 함수를 계산했더니 이랬다:

    R1  blocker ×1 = 100   ← Excel advanced/Format cell (환각)
    R17 blocker ×1 = 100   ← Email/Send가 엑셀 세션 핸들 수령
    R7  major   ×6 =  60 · R9 ×2 = 20 · R8 ×1 = 10 · R18 ×1 = 10 · L2 minor = 3
    ─────────────────────── 303

그리고 **목적 함수 밖**에 warning 4건이 있었다: 빈 Try 본문, 세션 누수, 미사용 변수 ×2.
동시에 surgeon이 삽입에 쓸 수 있는 어휘는 45개였는데 **전부 세션·제어 흐름**이고 업무
액션이 0개였다. 즉 위반의 상당수가 두 가지 이유로 **구조적으로 수리 불가**였다:

1. 목적 함수에 안 들어가서 (warning)
2. 갈아끼울 표기가 프롬프트에 없어서

이 파일은 그 두 구멍을 막은 것을 고정한다.
"""

import pytest

from app.agent.v4.orchestrator.harness import (
    _MAX_REPAIR_BUSINESS_ACTIONS,
    _business_repair_actions,
    repair_spec_excerpts,
)
from app.agent.v4.verify.checker import run_structure_checks
from app.agent.v4.verify.findings import from_violations, weight


def _act(pkg, act, children=None, **kw):
    a = {"package": pkg, "action": act, "label": act, **kw}
    if children is not None:
        a["children"] = children
    return a


def _steps(*actions):
    return [{"step_id": "step-1", "label": "s", "actions": list(actions)}]


# ── 빈 컨테이너는 warning이 아니다 ───────────────────────────────────────────

def test_빈_Try_본문은_수리_대상이다():
    """🔴 실측: Try/Catch/Finally 세 칸이 본문 없이 뜨고 본업 전체가 그 밖에 있었다.

    warning이면 `_error_findings`가 걸러 목적 함수에 아예 안 들어간다 — surgeon이 손댈
    이유가 없다. 예외 처리가 있다고 표시된 채 아무것도 보호하지 않는 상태로 출고된다.
    """
    v = run_structure_checks(_steps(
        _act("Error handler", "Try", children=[]),
        _act("Error handler", "Catch", children=[_act("Logging", "Log")]),
    ))
    empty = [x for x in v if x.rule == "R13" and "비어" in x.message]

    assert len(empty) == 1
    assert empty[0].severity != "warning"
    findings, _ = from_violations(empty)
    assert findings[0].severity == "major"
    assert weight(findings) == 10, "목적 함수에 무게가 실려야 수리가 시도된다"


def test_빈_Loop_본문도_같다():
    """반복할 액션이 밖에 있으면 N번 돌 일이 한 번만 돈다 — 빈 Try와 같은 종류의 결함."""
    v = run_structure_checks(_steps(_act("Loop", "Loop", children=[])))
    empty = [x for x in v if x.rule == "R14" and "비어" in x.message]

    assert len(empty) == 1
    assert empty[0].severity != "warning"


def test_본문이_차_있으면_침묵한다():
    v = run_structure_checks(_steps(
        _act("Error handler", "Try", children=[_act("Excel advanced", "Open")]),
        _act("Error handler", "Catch", children=[_act("Excel advanced", "Close")]),
    ))
    assert [x for x in v if "비어" in x.message] == []


# ── surgeon 수리 어휘 ────────────────────────────────────────────────────────

class _FakeCatalog:
    """Excel advanced에 읽기·쓰기가 있고, Browser·Error handler도 있는 최소 카탈로그."""

    ROWS = (
        [{"package": "Excel advanced", "action": n, "parameters": []}
         for n in ("Open", "Close", "Read column", "Write from data table", "Set cell")]
        + [{"package": "Browser", "action": n, "parameters": []} for n in ("Open", "Close")]
        + [{"package": "Error handler", "action": n, "parameters": []}
           for n in ("Try", "Catch", "Finally", "Throw")]
        + [{"package": "Gmail", "action": "Send", "parameters": []}]  # 흐름도가 안 쓰는 패키지
    )

    def iter_action_schemas(self):
        return iter(self.ROWS)

    def get_action_schema(self, pkg, act):
        for r in self.ROWS:
            if r["package"] == pkg and r["action"] == act:
                return r
        return None


_FLOW = {"steps": _steps(
    _act("Excel advanced", "Open"),
    _act("Excel advanced", "Format cell"),   # 환각 — 갈아끼울 표기가 필요하다
    _act("Browser", "Open"),
)}


def test_업무_액션이_수리_어휘에_실린다():
    """🔴 이 파일의 존재 이유 — 예전엔 45개 전부 세션·제어 흐름이고 업무 액션이 0개였다."""
    menu = repair_spec_excerpts(_FLOW, _FakeCatalog(), set())

    assert "Excel advanced/Write from data table" in menu
    assert "Excel advanced/Read column" in menu


def test_흐름도가_안_쓰는_패키지는_열지_않는다():
    """🔴 전량을 열면 surgeon이 제품을 갈아타 R17(세션 핸들 패키지 불일치)을 자작한다."""
    menu = repair_spec_excerpts(_FLOW, _FakeCatalog(), set())
    assert "Gmail/Send" not in menu


def test_위반이_걸린_패키지를_먼저_남긴다():
    """상한에 걸릴 때 어휘가 가장 아쉬운 곳은 위반이 난 패키지다."""
    rows = _business_repair_actions(
        _FLOW, _FakeCatalog(), [{"package": "Browser", "rule": "R1"}]
    )
    assert rows[0][0] == "Browser"


def test_상한이_걸려_있다():
    """패키지를 널리 건드린 흐름도에서 프롬프트가 터지지 않아야 한다."""
    class Big:
        ROWS = [{"package": "P", "action": f"A{i}", "parameters": []} for i in range(500)]

        def iter_action_schemas(self):
            return iter(self.ROWS)

        def get_action_schema(self, pkg, act):
            return {"parameters": []} if pkg == "P" else None

    flow = {"steps": _steps(_act("P", "A0"))}
    assert len(_business_repair_actions(flow, Big(), None)) == _MAX_REPAIR_BUSINESS_ACTIONS


def test_이미_발췌된_액션은_두_번_싣지_않는다():
    """위반 액션 스펙 발췌와 겹치면 같은 블록이 프롬프트에 두 번 들어간다."""
    menu = repair_spec_excerpts(_FLOW, _FakeCatalog(), {("Excel advanced", "Open")})
    assert menu.count("Excel advanced/Open") == 0


def test_구조_어휘는_그대로_실린다():
    """업무 액션을 더하면서 기존 계약(세션 여닫기·Try/Catch 공급)이 깨지면 안 된다."""
    menu = repair_spec_excerpts(_FLOW, _FakeCatalog(), set())
    for expected in ("Error handler/Try", "Error handler/Catch", "Excel advanced/Close"):
        assert expected in menu


def test_surgeon_프롬프트에_실제로_붙는다():
    """어휘를 만들어도 프롬프트에 안 실리면 아무것도 달라지지 않는다."""
    import inspect

    from app.agent.v4.orchestrator import harness

    src = inspect.getsource(harness.refine_flow)
    assert "repair_spec_excerpts(current, catalog, excerpt_keys, current_violations)" in src
    assert "[수리용 액션 스펙" in src
