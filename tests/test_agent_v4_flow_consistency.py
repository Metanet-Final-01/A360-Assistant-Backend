# -*- coding: utf-8 -*-
"""R17 세션 핸들 패키지 일관성 · R18 비실행 구획 (RPA-298, 2026-07-27 실사용 결함).

## 왜 이 두 규칙이 생겼나 — 실측

사용자가 과제 업무정의서로 v4를 돌린 산출물에서 나온 결함이다:

    3.3  Excel advanced      / Open              produces sExcelSession
    3.5  Microsoft 365 Excel / Paste cell        consumes sExcelSession   ← 실행 시 여기서 멈춤
    3.7  Excel advanced      / Format table cell consumes sExcelSession

    3.2  Step / Step  req_id=req-7  "엑셀 시작 위치 결정"   ← 실행되지 않는 구획
    3.6  Step / Step  req_id=req-8  "테두리 적용 범위 식별" ← 실행되지 않는 구획

**기존 검수는 전부 통과시켰다.** 그래서 이 파일이 지키는 건 "이 두 형태가 다시
조용히 통과하지 않는다"이고, 동시에 **오탐이 안 나는 경계**를 못 박는다 —
데이터 변수는 패키지를 건너다녀도 정상이고, children 있는 Step은 진짜 구획이다.
"""

import pytest

from app.agent.knowledge.derive import SessionRegistry
from app.agent.v4.verify.checker import run_scaffold_checks, run_session_package_checks

# 실측 산출물과 같은 표기. opener 유도는 카탈로그가 필요하므로 테스트는 레지스트리를
# 직접 만든다 — DB 없이 돌아야 한다.
_REG = SessionRegistry(
    openers=frozenset({("Excel advanced", "Open"), ("Email", "Connect")}),
    closers=frozenset({("Excel advanced", "Close"), ("Email", "Disconnect")}),
    session_packages=frozenset({"Excel advanced", "Email", "Microsoft 365 Excel"}),
    source="derived",
    derived_count=4,
)


def _steps(*actions):
    return [{"step_id": "s1", "actions": list(actions)}]


def _a(pkg, act, *, produces=(), consumes=(), req_id=None, children=None, label=None):
    a = {"package": pkg, "action": act,
         "produces": [{"name": n} for n in produces],
         "consumes": [{"name": n} for n in consumes]}
    if req_id:
        a["req_id"] = req_id
    if children is not None:
        a["children"] = children
    if label:
        a["label"] = label
    return a


# ── R17 — 세션 핸들이 패키지를 건너면 blocker ────────────────────────────────

def test_실측_결함이_잡힌다():
    """🔴 이 파일의 존재 이유 — 사용자 산출물에서 실제로 나온 그 형태."""
    steps = _steps(
        _a("Excel advanced", "Open", produces=["sExcelSession"]),
        _a("Excel advanced", "Switch to sheet", consumes=["sExcelSession"]),
        _a("Microsoft 365 Excel", "Paste cell", consumes=["sExcelSession", "tGoldPrices"]),
        _a("Excel advanced", "Format table cell", consumes=["sExcelSession"]),
    )
    v = run_session_package_checks(steps, _REG)
    assert len(v) == 1, "불일치한 그 한 자리만 잡아야 한다"
    assert v[0].rule == "R17"
    assert v[0].location == "actions[2]"
    assert "Microsoft 365 Excel" in v[0].message and "Excel advanced" in v[0].message


def test_같은_패키지가_받으면_침묵한다():
    steps = _steps(
        _a("Excel advanced", "Open", produces=["s"]),
        _a("Excel advanced", "Switch to sheet", consumes=["s"]),
        _a("Excel advanced", "Close", consumes=["s"]),
    )
    assert run_session_package_checks(steps, _REG) == []


def test_데이터_변수는_패키지를_건너도_정상이다():
    """🔴 오탐 경계. `tGoldPrices` 같은 데이터는 Data Table로 만들어 메일에 싣는 게 정상이다.

    세션 핸들만 대상으로 삼는 근거가 "producer가 opener인가"라는 것을 고정한다 —
    이 조건이 빠지면 모든 교차 패키지 변수 전달이 결함으로 찍힌다.
    """
    steps = _steps(
        _a("Recorder", "Structured data extraction", produces=["tGoldPrices"]),
        _a("Microsoft 365 Excel", "Paste cell", consumes=["tGoldPrices"]),
        _a("Email", "Send", consumes=["tGoldPrices"]),
    )
    assert run_session_package_checks(steps, _REG) == []


def test_role이_session이면_opener_목록에_없어도_잡는다():
    """카탈로그 유도가 놓친 여는 액션도 스키마가 말해 주면 쓴다(두 신호의 합집합)."""
    steps = _steps(
        {"package": "SAP", "action": "Start", "produces": [{"name": "h", "role": "session"}],
         "consumes": []},
        _a("Terminal Emulator", "Send text", consumes=["h"]),
    )
    v = run_session_package_checks(steps, _REG)
    assert len(v) == 1 and v[0].rule == "R17"


def test_패키지명_표기_흔들림은_결함이_아니다():
    """'Excel advanced'와 'Excel advanced 패키지'는 같은 패키지다."""
    steps = _steps(
        _a("Excel advanced", "Open", produces=["s"]),
        _a("Excel advanced 패키지", "Switch to sheet", consumes=["s"]),
    )
    assert run_session_package_checks(steps, _REG) == []


def test_컨테이너_안의_액션도_본다():
    """Loop 본문에서 패키지가 갈리는 게 오히려 흔하다 — 순회가 children을 놓치면 안 된다."""
    steps = _steps(
        _a("Excel advanced", "Open", produces=["s"]),
        _a("Loop", "For each row in table", children=[
            _a("Microsoft 365 Excel", "Paste cell", consumes=["s"]),
        ]),
    )
    v = run_session_package_checks(steps, _REG)
    assert len(v) == 1
    assert "children" in v[0].location


def test_레지스트리를_못_믿으면_통째로_침묵한다():
    """'검사 안 함'이 '틀리게 검사함'보다 낫다 — 이 파일의 기존 방침(R7/R8과 동일)."""
    empty = SessionRegistry(openers=frozenset(), closers=frozenset(),
                            session_packages=frozenset(), source="empty")
    steps = _steps(
        _a("Excel advanced", "Open", produces=["s"]),
        _a("Microsoft 365 Excel", "Paste cell", consumes=["s"]),
    )
    assert not empty.usable
    assert run_session_package_checks(steps, empty) == []


def test_produces가_없으면_침묵한다():
    """핸들을 특정할 근거가 없다 — 정보 없이 검사하면 전부 오탐이다."""
    steps = _steps(
        _a("Excel advanced", "Open"),
        _a("Microsoft 365 Excel", "Paste cell"),
    )
    assert run_session_package_checks(steps, _REG) == []


def test_R17은_blocker다():
    """실행이 **확실히** 멈추는 결함이라 R1(환각)과 동급이다.

    major면 교정 루프가 다른 위반과 저울질하다 그냥 남길 수 있다.
    """
    from app.agent.v4.verify.findings import from_violations

    steps = _steps(
        _a("Excel advanced", "Open", produces=["s"]),
        _a("Microsoft 365 Excel", "Paste cell", consumes=["s"]),
    )
    findings, _ = from_violations(run_session_package_checks(steps, _REG))
    assert [f.severity for f in findings] == ["blocker"]


# ── R18 — 실행되지 않는 구획이 요구를 담당 ──────────────────────────────────

def test_요구를_담당하는_Step은_결함이다():
    """커버리지는 이걸 못 잡는다 — req_id가 있으니 '담당 액션 있음'으로 계산된다."""
    steps = _steps(
        _a("Step", "Step", req_id="req-7", label="엑셀 시작 위치 결정", produces=["sStartCell"]),
        _a("Step", "Step", req_id="req-8", label="테두리 적용 범위 식별"),
    )
    v = run_scaffold_checks(steps)
    assert len(v) == 2
    assert all(x.rule == "R18" for x in v)
    assert "엑셀 시작 위치 결정" in v[0].message


def test_children이_있으면_진짜_구획이다():
    """하위를 묶는 용도의 Step은 정상이다 — 오탐 경계."""
    steps = _steps(
        _a("Step", "Step", req_id="req-1", children=[_a("Excel advanced", "Open")]),
    )
    assert run_scaffold_checks(steps) == []


def test_req_id가_없으면_침묵한다():
    """순수 구획 표시는 정상 — 요구를 담당한다고 **주장할 때만** 결함이다."""
    steps = _steps(_a("Step", "Step", label="엑셀 작업"))
    assert run_scaffold_checks(steps) == []


@pytest.mark.parametrize("pkg", ["Step", "step", "Comment", "comment"])
def test_표기가_흔들려도_구획으로_읽는다(pkg):
    assert len(run_scaffold_checks(_steps(_a(pkg, "x", req_id="req-1")))) == 1


def test_실행되는_액션은_대상이_아니다():
    steps = _steps(_a("Excel advanced", "Open", req_id="req-1"))
    assert run_scaffold_checks(steps) == []


def test_R18_수리_힌트가_반쪽_교체를_경고한다():
    """🔴 실측 2026-07-27 — R18 5건이 4라운드 동안 한 번도 수리되지 않았다.

    surgeon은 매 라운드 시도했지만 `update`에 package만 주고 action_name을 빼서, 결과 표기가
    `Microsoft 365 Excel/Step`·`Recorder/Step`·`Browser/Step`·`Email/Step`이 됐다 — 전부 없는
    액션이라 사전 검증이 버렸다(라운드별 5·2·5·4건, 총 16건).

    규칙 이름과 메시지만으로는 그 함정이 안 보인다. 힌트는 [고칠 문제들] 줄에 그대로 붙어
    나가므로(harness._findings_lines), 여기서 함정을 짚어야 라운드가 달라진다.
    """
    from app.agent.v4.orchestrator.harness import from_violations_dicts

    findings, _ = from_violations_dicts([
        {"rule": "R18", "location": "actions[0]", "message": "구획이 요구를 담당", "step_id": "s"}
    ])
    (f,) = findings

    assert f.fix_hint and "action_name" in f.fix_hint
    assert "package" in f.fix_hint


def test_R18은_A360_밖에서도_돈다():
    """'이름만 적고 액션을 안 만들었다'는 어느 솔루션에서나 결함이라 게이트 밖이다."""
    import inspect

    from app.agent.v4.verify import checker

    src = inspect.getsource(checker.run_flow_checks)
    scaffold_line = next(ln for ln in src.splitlines() if "run_scaffold_checks(steps)" in ln)
    # 들여쓰기가 `if is_a360:` 블록 안이면 4칸이 더 붙는다
    assert scaffold_line.startswith("    violations.extend"), \
        "R18이 is_a360 게이트 안으로 들어가면 타 솔루션에서 조용해진다"


# ── 채점 축 — 이 결함을 성공으로 세지 않는다 ────────────────────────────────

def test_채점_축이_같은_결함을_센다():
    """🔴 기능 등가 축은 이 불일치를 **성공으로 센다**(둘 다 spreadsheet 도메인).

    결함을 성공으로 세는 지표 위에서는 개선을 측정할 수 없어 별도 축을 낸다.
    """
    from scripts.goldset_eval.quality_axes import flow_consistency

    flow = {"steps": _steps(
        _a("Excel advanced", "Open", produces=["sExcelSession"]),
        _a("Microsoft 365 Excel", "Paste cell", consumes=["sExcelSession"]),
        _a("Step", "Step", req_id="req-7"),
    )}
    assert flow_consistency(flow) == {"session_pkg_breaks": 1, "scaffold_claims": 1}


def test_채점_축이_등가_축과_반대_방향을_본다():
    """등가 축은 '정답 대비 관대함', 일관성 축은 '흐름 자체의 모순'이다 — 둘은 독립이다."""
    from scripts.goldset_eval.metrics import score_case

    # 정답이 Excel_MS인데 에이전트가 Microsoft 365 Excel을 썼다 → 등가 축은 성공으로 친다
    s = score_case([("Microsoft 365 Excel", "Open")], [("Excel_MS", "OpenSpreadsheet")], [])
    assert s["action_equiv"]["f1"] == 1.0, "등가 축은 대안 패키지를 인정한다"
    assert s["action"]["f1"] == 0.0, "엄격 축은 인정하지 않는다"


def test_일관성_축이_행에_항상_붙는다():
    from scripts.goldset_eval.quality_axes import L1_ROW_KEYS, l1_row

    row = l1_row({"steps": _steps(_a("Excel advanced", "Open"))})
    assert {"session_pkg_breaks", "scaffold_claims"} <= set(row) == set(L1_ROW_KEYS)


def test_opener_판정이_검수기와_같은_규칙이다():
    """두 곳이 갈리면 검수기가 잡는 결함과 채점이 세는 결함이 달라진다."""
    from app.agent.knowledge.derive import _OPENER_RE as checker_re
    from scripts.goldset_eval.quality_axes import _OPENER_RE as scorer_re

    assert checker_re.pattern == scorer_re.pattern
    # `\b` 가드가 살아 있어야 `OpenAI: Chat` 류가 opener로 오인되지 않는다
    assert not scorer_re.match("OpenAI: Chat completion")
    assert scorer_re.match("Open")
