"""app/agent/verify/checker.py 세션 생명주기 검사 R7~R8 단위 테스트 (RPA-71, RPA-141 현행화).

카탈로그 없이(opener/closer는 상수) 전체 흐름도의 세션 순서·미종료를 검증한다:
정상 브래킷, 열기 전 사용(R7), 닫은 후 사용(R7), 미종료(R8), 다중 세션 독립,
컨테이너(children) 내부 순회, 단계 경계를 넘는 세션, 이름 없는 열림(_ANON).

표기는 현행 카탈로그(llm_agent 소싱)를 따른다 — open 계열은 실제로는 세션 이름 파라미터
없이 세션을 리턴하지만(cloudExcelOpen), 에이전트가 sessionName을 명시한 경우도 지원한다.
"""

from app.agent.v2.verify.checker import run_session_checks


def _sess(pkg, act, name):
    return {"package": pkg, "action": act,
            "parameters": [{"name": "sessionName", "value": name}], "children": []}


def _open(name="Default"):
    return _sess("Excel advanced", "cloudExcelOpen", name)


def _use(name="Default"):
    return _sess("Excel advanced", "excelAdvancedPackageSaveWorkbookAction", name)


def _close(name="Default"):
    return _sess("Excel advanced", "excelAdvancedPackageCloseAction", name)


def _step(step_id, actions):
    return {"step_id": step_id, "actions": actions}


def _rules(steps):
    return sorted(v.rule for v in run_session_checks(steps))


def test_proper_bracket_has_no_violations():
    steps = [_step("step-1", [_open(), _use(), _close()])]
    assert run_session_checks(steps) == []


def test_r7_use_before_open():
    steps = [_step("step-1", [_use(), _open(), _close()])]
    v = run_session_checks(steps)
    assert [x.rule for x in v] == ["R7"]
    assert v[0].step_id == "step-1"
    assert "Default" in v[0].message


def test_r7_use_after_close():
    steps = [_step("step-1", [_open(), _close(), _use()])]
    v = run_session_checks(steps)
    assert [x.rule for x in v] == ["R7"]  # 닫은 뒤 사용도 '열려 있지 않음'


def test_r8_not_closed():
    steps = [_step("step-1", [_open(), _use()])]
    v = run_session_checks(steps)
    assert [x.rule for x in v] == ["R8"]
    assert "Default" in v[0].message


def test_open_and_use_without_close_is_only_r8_not_r7():
    # 열고 썼으면 사용은 정상(R7 없음), 미종료만(R8)
    steps = [_step("step-1", [_open("S"), _use("S")])]
    v = run_session_checks(steps)
    assert [x.rule for x in v] == ["R8"]


def test_multiple_sessions_tracked_independently():
    steps = [_step("step-1", [
        _open("A"), _use("A"), _close("A"),
        _open("B"), _use("B"),  # B는 안 닫음 → R8
    ])]
    v = run_session_checks(steps)
    assert [x.rule for x in v] == ["R8"]
    assert "B" in v[0].message


def test_session_spans_steps():
    # step-1에서 열고 step-3에서 쓰고 닫음 — 단계 경계를 넘어도 정상
    steps = [
        _step("step-1", [_open("S")]),
        _step("step-2", [{"package": "String", "action": "assign",
                          "parameters": [{"name": "value", "value": "x"}], "children": []}]),
        _step("step-3", [_use("S"), _close("S")]),
    ]
    assert run_session_checks(steps) == []


def test_use_in_step_before_open_across_steps_is_r7():
    steps = [
        _step("step-1", [_use("S")]),   # 아직 안 열림
        _step("step-2", [_open("S"), _close("S")]),
    ]
    v = run_session_checks(steps)
    assert [x.rule for x in v] == ["R7"]
    assert v[0].step_id == "step-1"


def test_container_children_are_traversed():
    # Loop children 안의 세션 사용도 잡힌다 (열기 전이면 R7)
    loop = {"package": "Loop", "action": "cloudUsingLoopAction",
            "parameters": [{"name": "iteratorType", "value": "N times"}],
            "children": [_use("S")]}
    steps = [_step("step-1", [loop])]
    v = run_session_checks(steps)
    assert [x.rule for x in v] == ["R7"]  # S를 연 적 없음


def test_open_in_container_then_use_outside_is_ok_by_approximation():
    # 심볼릭 근사: children에서 연 세션도 선언 순서상 열린 것으로 본다
    loop = {"package": "Loop", "action": "cloudUsingLoopAction",
            "parameters": [{"name": "iteratorType", "value": "N times"}],
            "children": [_open("S")]}
    steps = [_step("step-1", [loop, _use("S"), _close("S")])]
    assert run_session_checks(steps) == []


def test_word_session_bracket():
    # Word도 open/close 상수로 추적된다 (sessionName 참조 사용 액션 포함)
    steps = [_step("step-1", [
        _sess("Word", "mswordOpenDocument", "doc"),
        _sess("Word", "mswordReplaceText", "doc"),
        _sess("Word", "mswordCloseDocument", "doc"),
    ])]
    assert run_session_checks(steps) == []


def test_r7_close_before_open():
    # 열지 않은 세션을 닫으려 함 → 닫을 대상이 없음 (R7)
    steps = [_step("step-1", [_close("S")])]
    v = run_session_checks(steps)
    assert [x.rule for x in v] == ["R7"]
    assert "닫" in v[0].message


def test_r7_double_close():
    # 정상 브래킷 뒤에 한 번 더 닫음 → 두 번째 close는 닫을 대상 없음 (R7)
    steps = [_step("step-1", [_open("S"), _use("S"), _close("S"), _close("S")])]
    v = run_session_checks(steps)
    assert [x.rule for x in v] == ["R7"]


def test_same_name_different_package_are_independent():
    # Excel 세션 "Default"와 Word 세션 "Default"는 별개 — 서로 덮어쓰지 않는다.
    steps = [_step("step-1", [
        _open("Default"),                                  # Excel advanced Default 열림
        _use("Default"),                                   # Excel advanced Default 사용 (정상)
        _sess("Word", "mswordReplaceText", "Default"),     # Word Default — 안 열림 (R7)
    ])]
    v = run_session_checks(steps)
    assert sorted(x.rule for x in v) == ["R7", "R8"]  # Word R7(미개시) + Excel R8(미종료)
    word_r7 = next(x for x in v if x.rule == "R7")
    assert word_r7.package == "Word"
    excel_r8 = next(x for x in v if x.rule == "R8")
    assert excel_r8.package == "Excel advanced"


def test_same_name_same_package_reopen_is_ok():
    # 같은 패키지·이름을 닫고 다시 열면 정상 (재사용)
    steps = [_step("step-1", [
        _open("S"), _use("S"), _close("S"),
        _open("S"), _use("S"), _close("S"),
    ])]
    assert run_session_checks(steps) == []


def test_action_without_session_param_is_ignored():
    steps = [_step("step-1", [
        {"package": "String", "action": "assign",
         "parameters": [{"name": "value", "value": "x"}], "children": []},
    ])]
    assert run_session_checks(steps) == []


# --- 이름 없는 열림 (_ANON) — 현행 open은 세션을 리턴하고 이름 파라미터가 없다 (RPA-141) ---

def _open_anon():
    """세션 이름 없이 여는 현행 open — cloudExcelOpen은 sessionName 파라미터가 없다."""
    return {"package": "Excel advanced", "action": "cloudExcelOpen",
            "parameters": [{"name": "fileSource", "value": "C:/a.xlsx"}], "children": []}


def _close_anon():
    return {"package": "Excel advanced", "action": "excelAdvancedPackageCloseAction",
            "parameters": [], "children": []}


def test_anonymous_open_covers_named_use():
    # 이름 없는 열림은 같은 패키지의 이름 참조를 관대하게 커버한다(리턴 세션을 변수로 받아
    # 쓰는 패턴이라 문자열 매칭 불가 — 오탐 방지 우선).
    steps = [_step("step-1", [_open_anon(), _use("Session1"), _close("Session1")])]
    assert run_session_checks(steps) == []


def test_anonymous_open_without_close_is_r8():
    steps = [_step("step-1", [_open_anon(), _use("S")])]
    v = run_session_checks(steps)
    assert [x.rule for x in v] == ["R8"]
    assert "이름 미지정" in v[0].message


def test_anonymous_close_pops_named_open():
    # 이름 없는 close는 그 패키지의 아무 열림이나 닫는 걸로 본다
    steps = [_step("step-1", [_open("S"), _use("S"), _close_anon()])]
    assert run_session_checks(steps) == []


def test_anonymous_bracket_ok():
    steps = [_step("step-1", [_open_anon(), _use("S"), _close_anon()])]
    assert run_session_checks(steps) == []
