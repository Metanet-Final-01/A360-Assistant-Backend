"""app/agent/verify/checker.py 세션 생명주기 검사 R7~R8 단위 테스트 (RPA-71).

카탈로그 없이(opener/closer는 상수) 전체 흐름도의 세션 순서·미종료를 검증한다:
정상 브래킷, 열기 전 사용(R7), 닫은 후 사용(R7), 미종료(R8), 다중 세션 독립,
컨테이너(children) 내부 순회, 단계 경계를 넘는 세션.
"""

from app.agent.verify.checker import run_session_checks


def _sess(pkg, act, name):
    return {"package": pkg, "action": act,
            "parameters": [{"name": "session", "value": name}], "children": []}


def _open(name="Default"):
    return _sess("Excel_MS", "OpenSpreadsheet", name)


def _use(name="Default"):
    return _sess("Excel_MS", "SetCell", name)


def _close(name="Default"):
    return _sess("Excel_MS", "CloseSpreadsheet", name)


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
    # Loop children 안의 SetCell도 세션 사용으로 잡힌다 (열기 전이면 R7)
    loop = {"package": "Loop", "action": "loop.commands.start",
            "parameters": [{"name": "iteratorType", "value": "N times"}],
            "children": [_use("S")]}
    steps = [_step("step-1", [loop])]
    v = run_session_checks(steps)
    assert [x.rule for x in v] == ["R7"]  # S를 연 적 없음


def test_open_in_container_then_use_outside_is_ok_by_approximation():
    # 심볼릭 근사: children에서 연 세션도 선언 순서상 열린 것으로 본다
    loop = {"package": "Loop", "action": "loop.commands.start",
            "parameters": [{"name": "iteratorType", "value": "N times"}],
            "children": [_open("S")]}
    steps = [_step("step-1", [loop, _use("S"), _close("S")])]
    assert run_session_checks(steps) == []


def test_webautomation_session_bracket():
    steps = [_step("step-1", [
        _sess("WebAutomation", "StartSessionWebAutomation", "web"),
        {"package": "WebAutomation", "action": "openpage",
         "parameters": [{"name": "sessionName", "value": "web"}], "children": []},
        _sess("WebAutomation", "EndSessionWebAutomation", "web"),
    ])]
    # 세션 파라미터 이름이 sessionName인 패키지도 추적된다
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
    # Excel 세션 "Default"와 WebAutomation 세션 "Default"는 별개 — 서로 덮어쓰지 않는다.
    steps = [_step("step-1", [
        _open("Default"),                 # Excel_MS Default 열림
        _use("Default"),                  # Excel_MS Default 사용 (정상)
        {"package": "WebAutomation", "action": "openpage",  # Web Default — 안 열림 (R7)
         "parameters": [{"name": "sessionName", "value": "Default"}], "children": []},
    ])]
    v = run_session_checks(steps)
    assert sorted(x.rule for x in v) == ["R7", "R8"]  # Web R7(미개시) + Excel R8(미종료)
    web_r7 = next(x for x in v if x.rule == "R7")
    assert web_r7.package == "WebAutomation"
    excel_r8 = next(x for x in v if x.rule == "R8")
    assert excel_r8.package == "Excel_MS"


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
