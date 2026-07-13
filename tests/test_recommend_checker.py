"""app/agent/verify/checker.py 정적 체커 R1~R6 단위 테스트 (RPA-27).

LLM·DB 없이 FakeCatalog로 검증한다: 카탈로그 부재(R1), 파라미터 이름(R2), 필수값
누락(R3), enum 위반(R4), 형식(R5), 컨테이너 아닌데 children(R6), 그리고 트리 재귀.
"""

from app.agent.verify import run_checks
from app.agent.verify.checker import is_container

from tests.agent_stubs import FakeCatalog

CAT = FakeCatalog()


def _rules(actions):
    return sorted(v.rule for v in run_checks(actions, CAT))


def test_valid_action_has_no_violations():
    actions = [{
        "package": "Excel_MS", "action": "GoToCell",
        "parameters": [{"name": "cellOption", "value": "특정 셀"}, {"name": "session", "value": "S"}],
        "children": [],
    }]
    assert run_checks(actions, CAT) == []


def test_r1_unknown_action():
    v = run_checks([{"package": "Excel_MS", "action": "NoSuchAction", "parameters": []}], CAT)
    assert [x.rule for x in v] == ["R1"]
    assert v[0].action == "NoSuchAction"


def test_r1_skips_param_checks_when_spec_missing():
    # 스펙이 없으면 R2~R5는 판정 불가 — R1만 나와야 한다
    v = run_checks([{"package": "Zzz", "action": "zzz",
                     "parameters": [{"name": "whatever", "value": None}]}], CAT)
    assert [x.rule for x in v] == ["R1"]


def test_r2_unknown_parameter_name():
    v = run_checks([{
        "package": "WebAutomation", "action": "openpage",
        "parameters": [{"name": "url", "value": "http://x"}, {"name": "sessionName", "value": "S"},
                       {"name": "bogusParam", "value": "1"}],
    }], CAT)
    rules = [x.rule for x in v]
    assert "R2" in rules
    assert any(x.param == "bogusParam" for x in v if x.rule == "R2")


def test_r3_missing_required_value():
    # sendMail: to/subject/sendVia/message가 필수. to만 주면 나머지 3개 R3.
    v = [x for x in run_checks([{
        "package": "Email", "action": "sendMail",
        "parameters": [{"name": "to", "value": "a@b.c"}],
    }], CAT) if x.rule == "R3"]
    missing = {x.param for x in v}
    assert {"subject", "sendVia", "message"} <= missing


def test_r3_empty_string_counts_as_missing():
    v = run_checks([{
        "package": "WebAutomation", "action": "openpage",
        "parameters": [{"name": "url", "value": "   "}, {"name": "sessionName", "value": "S"}],
    }], CAT)
    assert any(x.rule == "R3" and x.param == "url" for x in v)


def test_r4_enum_value_out_of_options():
    v = run_checks([{
        "package": "Excel_MS", "action": "GoToCell",
        "parameters": [{"name": "cellOption", "value": "존재하지않는옵션"}, {"name": "session", "value": "S"}],
    }], CAT)
    assert any(x.rule == "R4" and x.param == "cellOption" for x in v)


def test_r4_enum_accepts_label_or_value():
    # 옵션 label("특정 셀")도 허용된다
    v = run_checks([{
        "package": "Excel_MS", "action": "GoToCell",
        "parameters": [{"name": "cellOption", "value": "특정 셀"}, {"name": "session", "value": "S"}],
    }], CAT)
    assert [x for x in v if x.rule == "R4"] == []


def test_r6_children_on_non_container():
    v = run_checks([{
        "package": "Email", "action": "sendMail",
        "parameters": [{"name": "to", "value": "a@b.c"}, {"name": "subject", "value": "s"},
                       {"name": "sendVia", "value": "이메일 서버"}, {"name": "message", "value": "m"}],
        "children": [{"package": "String", "action": "assign", "parameters": [{"name": "value", "value": "x"}]}],
    }], CAT)
    assert any(x.rule == "R6" for x in v)


def test_r6_children_on_container_ok():
    # 컨테이너 판정은 패키지 단위(RPA-141) — 현행 카탈로그 표기(camelCase)로도 통과해야 한다.
    assert is_container("Loop", "cloudUsingLoopAction")
    v = run_checks([{
        "package": "Loop", "action": "cloudUsingLoopAction",
        "parameters": [{"name": "iteratorType", "value": "N times"}],
        "children": [{"package": "String", "action": "assign", "parameters": [{"name": "value", "value": "x"}]}],
    }], CAT)
    assert [x for x in v if x.rule == "R6"] == []


def test_r6_container_package_any_action_name():
    # 카탈로그 표기가 바뀌어도(iterator 변형 등) 컨테이너 패키지면 children 허용 — 표기 열거로
    # 돌아가 헛위반(R6)을 만들지 않는다(구 봇 JSON 표기 열거가 8/8 불일치로 재생성을 유발했다).
    assert is_container("Loop", "cloudForEachRowInDataTable")
    assert is_container("Error handler", "errorHandlerTry")
    assert is_container("Step", "stepAction")


def test_r6_break_throw_are_not_containers():
    # 컨테이너 패키지 소속이어도 본문이 없는 제어 액션(Break/Continue/Throw)은 children 금지.
    assert not is_container("Loop", "loopPackageBreakAction")
    assert not is_container("Error handler", "errorHandlerThrow")
    v = run_checks([{
        "package": "Loop", "action": "loopPackageBreakAction", "parameters": [],
        "children": [{"package": "String", "action": "assign", "parameters": [{"name": "value", "value": "x"}]}],
    }], CAT)
    assert any(x.rule == "R6" for x in v)


def test_recurses_into_children_locations():
    v = run_checks([{
        "package": "Loop", "action": "cloudUsingLoopAction",
        "parameters": [{"name": "iteratorType", "value": "N times"}],
        "children": [{"package": "Nope", "action": "nope", "parameters": []}],
    }], CAT)
    child_r1 = [x for x in v if x.rule == "R1"]
    assert child_r1 and child_r1[0].location == "actions[0].children[0]"
