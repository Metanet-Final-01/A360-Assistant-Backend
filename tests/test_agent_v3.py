"""agent v3 단위 테스트 — 검증 계층(R7 분기·R8 루프·R9~R12)·세션 레지스트리 유도·
step 연산·질문 카드·confidence 합성·TaskPlan 가드·심판 결정론 경로.

LLM이 필요한 지점(judge)은 chat_json을 실패시켜 결정론 폴백 경로를 검증한다 —
LLM 정상 경로는 E2E(AGENT_VERSION=v3) 검증 범위다.
"""

import pytest

from app.agent.v3.orchestrator import cards as cards_mod
from app.agent.v3.orchestrator import edit_ops
from app.agent.v3.recommend.graph import _coerce_flow, _recover_identity
from app.schemas import Recommendation
from app.agent.v3.orchestrator.harness import (
    attach_confidence,
    compute_flow_confidence,
    from_violations_dicts,
    repair_spec_excerpts,
)
from app.agent.v3.orchestrator.intake import _guard_plan
from app.agent.v3.orchestrator.judge import CandidateReport, judge_candidates
from app.agent.v3.verify import checker
from tests.agent_stubs import FakeCatalog


def _act(pkg, act, params=None, children=None, **extra):
    return {
        "package": pkg, "action": act, "order": 1,
        "parameters": params or [], "children": children or [], **extra,
    }


def _param(name, value):
    return {"name": name, "value": value, "value_source": "llm"}


# ─────────────────────────────────────────────────────────────────────────────
# 세션 레지스트리 유도
# ─────────────────────────────────────────────────────────────────────────────

def test_derive_session_registry_from_catalog():
    openers, closers = checker.derive_session_registry(FakeCatalog())
    # return_type=SESSION → opener 유도 (수기 상수 밖 패키지)
    assert ("WebAutomation", "StartSessionWebAutomation") in openers
    # opener 보유 패키지의 close/end-session 이름 → closer 유도
    assert ("WebAutomation", "EndSessionWebAutomation") in closers
    # 수기 상수는 그대로 포함
    assert ("Excel advanced", "cloudExcelOpen") in openers
    assert ("Excel advanced", "excelAdvancedPackageCloseAction") in closers


def test_derive_session_registry_without_iter_falls_back():
    class NoIter:
        def get_action_schema(self, p, a):
            return None

    openers, closers = checker.derive_session_registry(NoIter())
    assert openers == checker.SESSION_OPENERS
    assert closers == checker.SESSION_CLOSERS


# ─────────────────────────────────────────────────────────────────────────────
# R7/R8 — 분기 인지 심볼릭 실행
# ─────────────────────────────────────────────────────────────────────────────

def test_r7_branch_mismatch_detected():
    """If 한쪽 분기에서만 세션을 열면 병합점 불일치 위반이 나온다 (else 없음 = 암묵 경로)."""
    steps = [{"step_id": "s1", "actions": [
        _act("If", "ifPackageIfAction", children=[
            _act("Excel advanced", "cloudExcelOpen", params=[_param("sessionName", "S")]),
        ]),
        _act("Excel advanced", "excelAdvancedPackageCloseAction", params=[_param("sessionName", "S")]),
    ]}]
    violations = checker.run_session_checks(steps)
    rules = [v.rule for v in violations]
    assert "R7" in rules
    assert any("분기" in v.message for v in violations if v.rule == "R7")
    # maybe-열림의 닫기는 관대 수용 — double-close 오탐이 없어야 한다
    assert not any("닫으려" in v.message for v in violations)
    assert "R8" not in rules  # 누수 오탐 없음


def test_r7_both_branches_open_is_consistent():
    """If/Else 양쪽 다 열면 불일치가 아니다 — 병합 후 사용·닫기도 정상."""
    steps = [{"step_id": "s1", "actions": [
        _act("If", "ifPackageIfAction", children=[
            _act("Excel advanced", "cloudExcelOpen", params=[_param("sessionName", "S")]),
        ]),
        _act("If", "ifPackageElseAction", children=[
            _act("Excel advanced", "cloudExcelOpen", params=[_param("sessionName", "S")]),
        ]),
        _act("Excel advanced", "excelAdvancedPackageSaveWorkbookAction", params=[_param("sessionName", "S")]),
        _act("Excel advanced", "excelAdvancedPackageCloseAction", params=[_param("sessionName", "S")]),
    ]}]
    violations = checker.run_session_checks(steps)
    assert violations == []


def test_r8_loop_leak_warning():
    """Loop 본문에서 열고 본문에서 닫지 않으면 반복 누수 경고 + 미종료 R8."""
    steps = [{"step_id": "s1", "actions": [
        _act("Loop", "cloudUsingLoopAction", children=[
            _act("Excel advanced", "cloudExcelOpen", params=[_param("sessionName", "S")]),
        ]),
    ]}]
    violations = checker.run_session_checks(steps)
    r8 = [v for v in violations if v.rule == "R8"]
    assert len(r8) == 2  # 루프 누수 경고 1 + 순회 종료 미닫힘 1
    assert any(v.severity == "warning" and "Loop" in v.message for v in r8)


def test_r12_closer_outside_finally():
    """Error handler가 있는 흐름에서 닫기가 Finally 밖이면 R12 경고."""
    steps = [{"step_id": "s1", "actions": [
        _act("Error handler", "errorHandlerTry", children=[
            _act("Excel advanced", "cloudExcelOpen", params=[_param("sessionName", "S")]),
        ]),
        _act("Error handler", "errorHandlerCatch"),
        _act("Excel advanced", "excelAdvancedPackageCloseAction", params=[_param("sessionName", "S")]),
    ]}]
    violations = checker.run_session_checks(steps, emit_r12=True)
    assert any(v.rule == "R12" and v.severity == "warning" for v in violations)


# ─────────────────────────────────────────────────────────────────────────────
# R9~R11 — 변수 데이터플로우
# ─────────────────────────────────────────────────────────────────────────────

def _dataflow_flow():
    return {
        "steps": [{"step_id": "s1", "actions": [
            # tData를 정의 전에 소비 (R9)
            _act("String", "assign", params=[_param("value", "$tData$")],
                 consumes=[{"name": "tData"}]),
            # tOut을 생산하지만 아무도 소비 안 함 (R10)
            _act("Excel_MS", "writeDataTableToWorksheet",
                 params=[_param("dataTable", "x"), _param("cellAddress", "A1"), _param("session", "Default")],
                 produces=[{"name": "tOut"}]),
        ]}],
        "variables": [
            {"name": "tData", "type": "TABLE", "direction": "local"},
            {"name": "sName", "type": "STRING", "direction": "local"},
        ],
    }


def test_r9_def_before_use_and_r10_dead_output():
    violations = checker.run_dataflow_checks(_dataflow_flow(), FakeCatalog())
    rules = [v.rule for v in violations]
    assert "R9" in rules
    assert "R10" in rules
    assert all(v.severity == "warning" for v in violations if v.rule == "R10")


def test_r9_silent_without_declared_produces():
    """produces/consumes 명시가 전혀 없으면 R9/R10은 침묵한다 (오탐 방지 게이트)."""
    flow = _dataflow_flow()
    for step in flow["steps"]:
        for a in step["actions"]:
            a.pop("produces", None)
            a.pop("consumes", None)
    violations = checker.run_dataflow_checks(flow, FakeCatalog())
    assert not any(v.rule in ("R9", "R10") for v in violations)


def test_r11_type_mismatch():
    """NUMBER 기대 파라미터에 STRING 변수 단일 참조 → R11."""
    flow = {
        "steps": [{"step_id": "s1", "actions": [
            _act("Email", "emailConnect",
                 params=[_param("host", "smtp"), _param("port", "$sName$")]),
        ]}],
        "variables": [{"name": "sName", "type": "STRING", "direction": "local"}],
    }
    violations = checker.run_dataflow_checks(flow, FakeCatalog())
    assert [v.rule for v in violations] == ["R11"]


# ─────────────────────────────────────────────────────────────────────────────
# EditOps — split_step / merge_step / set_params 변수 연결
# ─────────────────────────────────────────────────────────────────────────────

def _three_action_flow():
    return {"steps": [{"step_id": "s1", "label": "일괄", "actions": [
        _act("String", "assign"), _act("String", "assign"), _act("String", "assign"),
    ]}]}


def test_split_and_merge_step():
    flow = _three_action_flow()
    edit_ops.annotate_ids(flow)  # n1, n2, n3
    op = edit_ops.EditOp(op="split_step", step_id="s1", anchor="n2", label="후반부")
    applied, errors = edit_ops.apply_edit_ops(flow, [op])
    assert applied == 1 and not errors
    assert [s["step_id"] for s in flow["steps"]] == ["s1", "s1-b"]
    assert len(flow["steps"][0]["actions"]) == 1
    assert len(flow["steps"][1]["actions"]) == 2

    applied, errors = edit_ops.apply_edit_ops(
        flow, [edit_ops.EditOp(op="merge_step", step_id="s1-b")]
    )
    assert applied == 1
    assert len(flow["steps"]) == 1
    assert len(flow["steps"][0]["actions"]) == 3


def test_split_step_at_first_action_is_invalid():
    flow = _three_action_flow()
    edit_ops.annotate_ids(flow)
    applied, errors = edit_ops.apply_edit_ops(
        flow, [edit_ops.EditOp(op="split_step", step_id="s1", anchor="n1")]
    )
    assert applied == 0 and errors  # 원 단계가 비어버리는 분할은 거부


def test_set_params_updates_var_refs():
    flow = _three_action_flow()
    edit_ops.annotate_ids(flow)
    op = edit_ops.EditOp(
        op="set_params", target="n1",
        parameters=[{"name": "value", "value": "$tData$"}],
        consumes=[{"name": "tData"}], produces=[],
    )
    applied, _ = edit_ops.apply_edit_ops(flow, [op])
    assert applied == 1
    node = flow["steps"][0]["actions"][0]
    assert node["consumes"] == [{"name": "tData"}]
    assert node["produces"] == []


def test_editop_coerces_string_action_spec():
    """surgeon의 "패키지/액션" 문자열 슬립을 dict로 코얼스한다 (골드셋 평가 3회 실측 회귀).

    검증 거부는 교정 라운드 통째 폐기로 이어지므로, 의도가 명백한 문자열은 살리고
    패키지 불명 문자열만 None(무연산)으로 강등한다.
    """
    op = edit_ops.EditOp(op="insert", anchor="n1", position="after",
                         action="Excel advanced/cloudExcelOpen")
    assert op.action == {"package": "Excel advanced", "action": "cloudExcelOpen"}

    # 패키지를 특정할 수 없는 축약 — 배치를 깨지 않고 해당 스펙만 무연산 강등
    assert edit_ops.EditOp(op="insert", anchor="n1", action="messageBox").action is None

    wrap = edit_ops.EditOp(
        op="wrap", targets=["n1"], container="Error handler/errorHandlerTry",
        siblings_after=["Error handler/errorHandlerCatch", "junk",
                        {"package": "Error handler", "action": "errorHandlerFinally"}],
    )
    assert wrap.container == {"package": "Error handler", "action": "errorHandlerTry"}
    assert wrap.siblings_after == [
        {"package": "Error handler", "action": "errorHandlerCatch"},
        {"package": "Error handler", "action": "errorHandlerFinally"},
    ]


def test_coerce_flow_recovers_null_action_nodes():
    """null-action 노드(package는 살아있음)를 되살려 leaf 하나가 Recommendation 검증을
    통째로 폭파(→빈 흐름도)시키는 회귀를 막는다 (정준환 실측 probe10: 깊은 If 중첩 8곳 null).

    단일의미 컨테이너(Loop)만 canonical action으로 복원, 그 외(모호 분기 컨테이너 If·비컨테이너·
    null/null)는 Step 스캐폴드로 강등하되 label·children은 보존한다. 정상 노드는 불변.
    """
    flow = {
        "steps": [{
            "step_id": "s1", "label": "정산",
            "actions": [
                # Loop도 action을 비워 canonical 복원 경로를 실제로 태운다(단일의미 컨테이너).
                _act("Loop", None, children=[
                    {"package": "If", "action": None, "label": "임계 초과 판정",
                     "parameters": [], "children": [
                         {"package": "Excel advanced", "action": None, "label": "값 기록",
                          "parameters": [], "children": []},
                     ]},
                    {"package": None, "action": None, "label": "빈 껍데기",
                     "parameters": [], "children": []},
                ]),
            ],
        }],
        "variables": [],
    }
    # 이전엔 여기서 ValidationError → 상위 폴백도 실패 → steps=[] 붕괴였다.
    rec = Recommendation.model_validate(_coerce_flow(flow))

    loop = rec.steps[0].actions[0]
    # null-action Loop → canonical(Loop/cloudUsingLoopAction)으로 복원(복구 로직이 없으면 실패).
    assert (loop.package, loop.action) == ("Loop", "cloudUsingLoopAction")  # 단일의미 컨테이너 canonical
    if_node = loop.children[0]
    # If는 action(if/elseIf/else)이 비면 어느 분기인지 모르므로 canonical(If/if)로 되살리지
    # 않고 Step으로 보수적 강등 — elseIf/else를 if로 둔갑시켜 제어흐름을 뒤집지 않는다.
    assert (if_node.package, if_node.action) == ("Step", "stepAction")
    assert if_node.label == "임계 초과 판정"                                  # label 보존
    assert len(if_node.children) == 1                                       # children 보존
    biz = if_node.children[0]
    assert (biz.package, biz.action) == ("Step", "stepAction")             # 비컨테이너 → Step 바닥
    assert biz.label == "값 기록"                                            # label 보존
    assert (loop.children[1].package, loop.children[1].action) == ("Step", "stepAction")  # null/null


@pytest.mark.parametrize("pkg", ["If", "Error handler", "errorHandler", "ERROR HANDLER"])
def test_recover_identity_downgrades_ambiguous_containers_to_step(pkg):
    """분기/실패경로 컨테이너(If={if,elseIf,else}, Error handler={try,catch,finally})는
    action이 비면 canonical로 복원하지 않고 Step으로 강등한다 — elseIf를 if로, catch를 try로
    되살려 제어흐름을 뒤집는 회귀를 막는다(CodeRabbit RPA-195 리뷰 반영).
    """
    a = {"package": pkg, "action": None, "label": "분기/실패 처리", "children": []}
    _recover_identity(a)
    assert (a["package"], a["action"]) == ("Step", "stepAction")
    assert a["label"] == "분기/실패 처리"  # label 보존


def test_recover_identity_keeps_labeled_else_catch_finally_children():
    """null-action Else/Catch/Finally가 Step으로 강등돼도 children(실 액션)은 보존한다."""
    flow = {
        "steps": [{
            "step_id": "s1", "label": "예외 처리",
            "actions": [
                {"package": "Error handler", "action": None, "label": "복구",  # catch였을 수 있음
                 "parameters": [], "children": [
                     {"package": "Message box", "action": "showMessage", "label": "오류 알림",
                      "parameters": [], "children": []},
                 ]},
            ],
        }],
        "variables": [],
    }
    rec = Recommendation.model_validate(_coerce_flow(flow))
    node = rec.steps[0].actions[0]
    assert (node.package, node.action) == ("Step", "stepAction")  # try로 둔갑 안 함
    assert node.children[0].label == "오류 알림"                    # 복구 액션 유실 없음


def test_recover_identity_leaves_valid_nodes_untouched():
    """package·action이 모두 채워진 정상 노드는 절대 건드리지 않는다 — 유효한 If/Catch 포함."""
    a = {"package": "Excel advanced", "action": "cloudExcelOpen", "label": "열기"}
    _recover_identity(a)
    assert (a["package"], a["action"]) == ("Excel advanced", "cloudExcelOpen")
    # action이 채워진 elseIf/catch는 그대로 둔다(강등 대상은 오직 null-action)
    b = {"package": "Error handler", "action": "errorHandlerCatch", "label": "복구"}
    _recover_identity(b)
    assert (b["package"], b["action"]) == ("Error handler", "errorHandlerCatch")


# ─────────────────────────────────────────────────────────────────────────────
# 질문 카드 — 생성·적용
# ─────────────────────────────────────────────────────────────────────────────

def _card_flow():
    return {
        "steps": [{"step_id": "s1", "actions": [
            _act("Email", "sendMail", params=[
                _param("subject", "리포트"), _param("message", "본문"),
                {"name": "to", "value": None, "value_source": "llm"},
                _param("sendVia", "Outlook"),
            ]),
        ]}],
        "variables": [],
    }


def _r3_violation():
    return {"rule": "R3", "location": "actions[0]", "step_id": "s1",
            "package": "Email", "action": "sendMail", "param": "to",
            "message": "필수 파라미터 'to'에 값이 없습니다."}


def test_build_cards_from_r3_and_spec():
    spec = {
        "unknowns": [{"what": "발송 대상 부서", "why_needed": "수신자 확정", "blocking": False}],
        "assumptions": ["첫 번째 시트를 대상으로 가정"],
    }
    cards = cards_mod.build_cards(_card_flow(), spec, [_r3_violation()], FakeCatalog())
    kinds = [c["kind"] for c in cards]
    assert kinds == ["missing_param", "ambiguity", "assumption_confirm"]
    mp = cards[0]
    assert mp["input_type"] == "text"  # to는 TEXT 파라미터
    assert mp["blocking"] is True  # 기본값 없음
    assert mp["targets"][0]["node_path"] == "actions[0]"
    ac = cards[2]
    assert ac["default"] is True  # 전제 확인은 승인이 기본


def test_build_cards_select_options_from_catalog():
    v = dict(_r3_violation(), param="sendVia")
    cards = cards_mod.build_cards(_card_flow(), None, [v], FakeCatalog())
    assert cards[0]["input_type"] == "select"
    assert "Outlook" in cards[0]["options"]


def test_apply_card_values_sets_user_value():
    flow = _card_flow()
    flow["needs_input"] = cards_mod.build_cards(flow, None, [_r3_violation()], FakeCatalog())
    card_id = flow["needs_input"][0]["card_id"]

    applied, needs_edit, errors = cards_mod.apply_card_values(flow, {card_id: "team@corp.com"})
    assert applied == 1 and not needs_edit and not errors
    param = next(p for p in flow["steps"][0]["actions"][0]["parameters"] if p["name"] == "to")
    assert param["value"] == "team@corp.com"
    assert param["value_source"] == "user"
    assert flow["needs_input"][0]["resolved"] is True
    # 이미 해소된 카드 재적용은 거부
    _, _, errors2 = cards_mod.apply_card_values(flow, {card_id: "x"})
    assert errors2


def test_apply_card_values_routes_ambiguity_to_edit():
    flow = _card_flow()
    flow["needs_input"] = [{
        "card_id": "card-1", "kind": "ambiguity", "question": "어느 시트?",
        "targets": [], "input_type": "text", "blocking": False, "resolved": False,
    }]
    applied, needs_edit, errors = cards_mod.apply_card_values(flow, {"card-1": "두 번째 시트로"})
    assert applied == 0 and len(needs_edit) == 1 and not errors


# ─────────────────────────────────────────────────────────────────────────────
# confidence 합성 (v3)
# ─────────────────────────────────────────────────────────────────────────────

def _conf_flow():
    return {"steps": [{"step_id": "s1", "actions": [_act("Email", "sendMail")]}]}


def _sink():
    return [{"package_name": "Email", "action_name": "sendMail", "score": 0.8}]


def test_confidence_r3_not_penalized():
    flow = _conf_flow()
    attach_confidence(flow, _sink(), [dict(_r3_violation(), location="actions[0]")])
    assert flow["steps"][0]["actions"][0]["confidence"] == 0.8  # R3는 감점 없음


def test_confidence_agreement_factor():
    flow = _conf_flow()
    attach_confidence(flow, _sink(), [], agreement={("Email", "sendMail")})
    boosted = flow["steps"][0]["actions"][0]["confidence"]
    flow2 = _conf_flow()
    attach_confidence(flow2, _sink(), [], agreement=set())
    lonely = flow2["steps"][0]["actions"][0]["confidence"]
    assert boosted > 0.8 * 1.05 and lonely < 0.8  # ×1.1 / ×0.9


def test_confidence_r1_still_floors():
    flow = _conf_flow()
    attach_confidence(flow, _sink(), [{"rule": "R1", "location": "actions[0]", "step_id": "s1"}])
    assert flow["steps"][0]["actions"][0]["confidence"] == 0.2


def test_flow_confidence_composition():
    findings, _ = from_violations_dicts([{"rule": "R1", "location": "actions[0]", "step_id": "s1",
                                          "message": "", "severity": "error"}])
    full = compute_flow_confidence(must_coverage=1.0, findings=[], sim_pass_rate=1.0)
    dinged = compute_flow_confidence(must_coverage=1.0, findings=findings, sim_pass_rate=1.0)
    carded = compute_flow_confidence(must_coverage=1.0, findings=[], sim_pass_rate=1.0, blocking_cards=2)
    assert full == 1.0
    assert dinged == pytest.approx(0.8)  # blocker 1건 → ×0.8
    assert carded == pytest.approx(0.9)  # 카드 2장 → ×0.9


# ─────────────────────────────────────────────────────────────────────────────
# TaskPlan 가드 + 심판 결정론 경로
# ─────────────────────────────────────────────────────────────────────────────

def test_guard_plan_rules():
    no_flow = {"recommendation": None, "message": ""}
    assert _guard_plan(["edit"], no_flow)[0] == ["generate"]
    assert _guard_plan(["bogus"], no_flow)[0] == ["qa"]
    assert _guard_plan(["generate", "generate", "qa", "nope"], no_flow)[0] == ["generate", "qa"]

    with_flow = {"recommendation": {"steps": [{}]}, "message": "3번 단계 삭제해줘"}
    assert _guard_plan(["qa"], with_flow)[0] == ["edit"]  # 수정 명령 상향 (RPA-98 계승)
    # 질문이면 상향하지 않는다
    q = {"recommendation": {"steps": [{}]}, "message": "3번 단계는 왜 있어?"}
    assert _guard_plan(["qa"], q)[0] == ["qa"]


# ─────────────────────────────────────────────────────────────────────────────
# R13/R14 — 제어 흐름 구조 (0374 JIRA 봇 실측 결함의 일반화)
# ─────────────────────────────────────────────────────────────────────────────

def test_r13_action_between_try_and_catch():
    """Try(빈 본문) → 일반 액션 → Catch: 인접성 error + 빈 Try warning."""
    steps = [{"step_id": "s1", "actions": [
        _act("Error handler", "errorHandlerTry"),
        _act("String", "assign"),
        _act("Error handler", "errorHandlerCatch"),
    ]}]
    violations = checker.run_structure_checks(steps)
    r13 = [v for v in violations if v.rule == "R13"]
    assert any(v.severity == "error" and "Catch" in v.message for v in r13)  # 인접성
    assert any(v.severity == "warning" and "비어" in v.message for v in r13)  # 빈 Try
    # Catch 자신도 Try에 안 붙어 있다는 위반
    assert any("붙어 있지 않습니다" in v.message for v in r13)


def test_r13_proper_try_catch_finally_passes():
    steps = [{"step_id": "s1", "actions": [
        _act("Error handler", "errorHandlerTry", children=[_act("String", "assign")]),
        _act("Error handler", "errorHandlerCatch"),
        _act("Error handler", "errorHandlerFinally"),
        _act("String", "assign"),  # 블록 밖 후속 작업은 정상
    ]}]
    assert [v for v in checker.run_structure_checks(steps) if v.severity == "error"] == []


def test_r14_continue_outside_loop_and_empty_loop():
    steps = [{"step_id": "s1", "actions": [
        _act("Loop", "loopPackageContinueAction"),        # 반복 오용 (0374 실측)
        _act("Loop", "cloudUsingLoopAction"),             # 본문 빈 Loop
    ]}]
    violations = checker.run_structure_checks(steps)
    r14 = [v for v in violations if v.rule == "R14"]
    assert any(v.severity == "error" and "Continue" in v.message for v in r14)
    assert any(v.severity == "warning" and "비어" in v.message for v in r14)


def test_r14_continue_inside_loop_is_valid():
    steps = [{"step_id": "s1", "actions": [
        _act("Loop", "cloudUsingLoopAction", children=[
            _act("String", "assign"),
            _act("Loop", "loopPackageContinueAction"),
        ]),
    ]}]
    assert checker.run_structure_checks(steps) == []


# ─────────────────────────────────────────────────────────────────────────────
# Dossier 결정론 보완 — 세션 여닫기 + 구조 액션
# ─────────────────────────────────────────────────────────────────────────────

def test_structural_complement_adds_session_and_control_flow():
    from app.agent.v3.recommend.research import structural_complement

    out = structural_complement(FakeCatalog(), {"Excel advanced", "WebAutomation"})
    # 메뉴 등장 패키지의 세션 여닫기가 자동 포함된다
    assert ("Excel advanced", "cloudExcelOpen") in out
    assert ("Excel advanced", "excelAdvancedPackageCloseAction") in out
    assert ("WebAutomation", "EndSessionWebAutomation") in out
    # 제어 흐름 구조 액션 — 카탈로그에 실재하는 것만
    assert ("Loop", "cloudUsingLoopAction") in out
    assert ("Error handler", "errorHandlerTry") in out
    # 메뉴에 없는 패키지의 세션 액션은 안 끌려온다 (Word는 메뉴 밖)
    assert not any(pkg == "Word" for pkg, _ in out)
    # 카탈로그에 없는 후보는 제외된다 (폐쇄어휘 유지)
    assert all(FakeCatalog().get_action_schema(p, a) is not None for p, a in out)


def test_repair_spec_excerpts_supplies_insertion_vocabulary():
    """surgeon 수리 메뉴 — 흐름도에 없는 opener/closer·Loop·Try 스펙을 삽입 재료로 동봉한다."""
    flow = {"steps": [{"step_id": "step-1", "actions": [
        _act("Excel advanced", "excelAdvancedPackageSaveWorkbookAction"),
    ]}]}
    menu = repair_spec_excerpts(flow, FakeCatalog(), exclude=set())
    # 흐름도에 등장한 패키지의 세션 여닫기 + 구조 액션이 스펙 형식으로 실린다
    assert "Excel advanced/cloudExcelOpen" in menu
    assert "Excel advanced/excelAdvancedPackageCloseAction" in menu
    assert "Loop/cloudUsingLoopAction" in menu
    assert "Error handler/errorHandlerTry" in menu
    assert "파라미터" in menu
    # 위반 발췌에 이미 있는 액션은 중복으로 싣지 않는다
    dedup = repair_spec_excerpts(flow, FakeCatalog(), exclude={("Loop", "cloudUsingLoopAction")})
    assert "Loop/cloudUsingLoopAction" not in dedup


# ─────────────────────────────────────────────────────────────────────────────
# 코드리뷰(PR #249) 반영 회귀 잠금
# ─────────────────────────────────────────────────────────────────────────────

def test_r8_close_only_in_catch_still_leaks():
    """catch에서만 닫는 세션 — 오류 경로 fork라 정상 경로 누수가 R8로 잡혀야 한다."""
    steps = [{"step_id": "s1", "actions": [
        _act("Error handler", "errorHandlerTry", children=[
            _act("Excel advanced", "cloudExcelOpen", params=[_param("sessionName", "S")]),
        ]),
        _act("Error handler", "errorHandlerCatch", children=[
            _act("Excel advanced", "excelAdvancedPackageCloseAction", params=[_param("sessionName", "S")]),
        ]),
    ]}]
    violations = checker.run_session_checks(steps)
    assert any(v.rule == "R8" for v in violations)


def test_r8_close_in_finally_passes():
    """표준 골격(열기 try / 닫기 finally)은 fork 후에도 깨끗해야 한다."""
    steps = [{"step_id": "s1", "actions": [
        _act("Error handler", "errorHandlerTry", children=[
            _act("Excel advanced", "cloudExcelOpen", params=[_param("sessionName", "S")]),
        ]),
        _act("Error handler", "errorHandlerCatch"),
        _act("Error handler", "errorHandlerFinally", children=[
            _act("Excel advanced", "excelAdvancedPackageCloseAction", params=[_param("sessionName", "S")]),
        ]),
    ]}]
    violations = checker.run_session_checks(steps)
    assert not any(v.rule in ("R7", "R8") for v in violations)


def test_r9_first_iteration_inside_loop():
    """Loop 본문 안 '소비 후 생산' — 1회차 미정의 사용이 R9로 잡히고, 루프 뒤 소비는 관대."""
    flow = {"steps": [{"step_id": "s1", "actions": [
        _act("Loop", "cloudUsingLoopAction", children=[
            _act("String", "assign", params=[_param("value", "$x$")], consumes=[{"name": "x"}]),
            _act("String", "assign", produces=[{"name": "x"}]),
        ]),
        _act("String", "assign", params=[_param("value", "$x$")], consumes=[{"name": "x"}]),
    ]}], "variables": []}
    violations = checker.run_dataflow_checks(flow, FakeCatalog())
    r9 = [v for v in violations if v.rule == "R9"]
    assert len(r9) == 1  # 루프 안 1회차 1건만 — 루프 뒤 소비는 maybe로 관대
    assert "children" in (r9[0].location or "")


def test_semantic_fills_missing_requirements(monkeypatch):
    """L2가 요구를 빠뜨리면 missing으로 채워 must_coverage 부풀림·게이트 누수를 막는다."""
    import app.agent.v3.verify.semantic as semantic_mod

    spec = {"goal": "g", "requirements": [
        {"req_id": "req-1", "priority": "should", "text": "a"},
        {"req_id": "req-2", "priority": "must", "text": "b"},
    ]}
    fake_report = semantic_mod.CoverageReport(entries=[
        semantic_mod.CoverageEntry(req_id="req-1", status="covered", evidence=["n1"]),
        semantic_mod.CoverageEntry(req_id="req-9", status="covered", evidence=["n2"]),  # 환각 id
    ])
    monkeypatch.setattr(semantic_mod, "chat_json", lambda *a, **k: fake_report)
    report = semantic_mod.run_semantic_check(spec, {"steps": []})
    by_id = {e.req_id: e for e in report.entries}
    assert set(by_id) == {"req-1", "req-2"}
    assert by_id["req-2"].status == "missing" and by_id["req-2"].priority == "must"
    assert report.must_coverage == 0.0
    assert [e.req_id for e in report.hard_gate_failures()] == ["req-2"]


def test_simulation_missing_verdicts_counted_as_fail(monkeypatch):
    """판정관이 일부 경로만 판정하면 누락 경로는 실패로 채워 pass_rate 부풀림을 막는다."""
    import app.agent.v3.verify.simulate as simulate_mod

    flow = {"steps": [{"step_id": "s1", "actions": [
        _act("Message box", "messageBoxAction", params=[_param("message", "hi")]),
    ]}]}
    traces = simulate_mod.build_traces(flow)
    assert len(traces) >= 2
    fake = simulate_mod.SimulationReport(verdicts=[
        simulate_mod.TraceVerdict(trace_id=next(iter(traces)), ok=True),
    ])
    monkeypatch.setattr(simulate_mod, "chat_json", lambda *a, **k: fake)
    report = simulate_mod.run_simulation({"goal": "g"}, flow)
    assert {v.trace_id for v in report.verdicts} == set(traces)
    assert report.pass_rate == 1 / len(traces)


def test_judge_hard_gate_and_llm_failure_fallback(monkeypatch):
    """LLM 심판이 죽어도 결정론 신호로 승자를 내고, 게이트 실패 후보는 승자 자격이 없다."""
    import app.agent.v3.orchestrator.judge as judge_mod

    def _boom(*a, **k):
        raise ValueError("llm down")

    monkeypatch.setattr(judge_mod, "chat_json", _boom)

    strong_but_gated = CandidateReport(
        candidate_id="A", persona="모범", flow={"steps": []},
        must_coverage=0.9, gate_failures=["req-2"], sim_pass_rate=1.0,
    )
    weaker_but_clean = CandidateReport(
        candidate_id="B", persona="운영", flow={"steps": []},
        must_coverage=0.7, gate_failures=[], sim_pass_rate=0.8,
    )
    out = judge_candidates({"requirements": []}, [strong_but_gated, weaker_but_clean])
    assert out["winner"].candidate_id == "B"
    assert any(r["gate_failed"] for r in out["verdict"]["scores"])


# ─────────────────────────────────────────────────────────────────────────────
# params_unknown 스펙 (RPA-206 후속 — 존재 판정과 스펙 판정의 분리)
# ─────────────────────────────────────────────────────────────────────────────


class _UnknownParamsCatalog:
    """schema 없는 행의 BackendCatalog 적재 형태 — parameters 키가 아예 없다."""

    def get_action_schema(self, package, action):
        return {"package": package, "action": action, "params_unknown": True}


class _EmptyParamsCatalog:
    """파라미터가 '없음'으로 확정된 스펙 — 빈 목록은 미상과 달리 R2 판정 대상."""

    def get_action_schema(self, package, action):
        return {"package": package, "action": action, "parameters": []}


def test_params_unknown_spec_passes_r1_and_skips_r2_to_r5():
    # 존재는 성립(R1 없음), 파라미터 스펙은 미상이라 R2~R5 침묵 — '모름 → 침묵'.
    v = checker.run_checks(
        [_act("Google Drive", "Move file", params=[_param("fileId", "x")])],
        _UnknownParamsCatalog(),
    )
    assert v == []


def test_empty_param_spec_still_flags_r2():
    # 빈 목록([])은 '파라미터 없음' 확정 — 미상 스킵에 휩쓸리지 않고 R2가 잡아야 한다.
    v = checker.run_checks(
        [_act("Clipboard", "Clear", params=[_param("bogus", "x")])],
        _EmptyParamsCatalog(),
    )
    assert [x.rule for x in v] == ["R2"]


def test_qa_needs_evidence_predicate():
    # qa 근거 가드 판정 — A360 도메인 명사+질의 마커 결합일 때만 첫 턴 검색을 강제한다.
    from app.agent.v3.orchestrator.qa import _needs_evidence

    # 사실 조회형 — 강제 대상 (근거 없는 단정 방지)
    assert _needs_evidence("커뮤니티 에디션에서도 트리거 쓸 수 있어?")
    assert _needs_evidence("PDF 텍스트 추출하는 액션 있어?")
    assert _needs_evidence("봇 에이전트 설치 어떻게 해?")
    # 명사형 질의 표현도 잡는다 (CodeRabbit #286 리뷰 반영 — "인가요"/"방법"류)
    assert _needs_evidence("커뮤니티 에디션은 무료인가요?")
    assert _needs_evidence("봇 에이전트 설치 방법은?")
    assert _needs_evidence("라이선스 비용 얼마야?")
    # 인사·감사·흐름도 문맥 질문 — 기존대로 검색 없이 답한다 (강제 없음)
    assert not _needs_evidence("안녕하세요")
    assert not _needs_evidence("고마워, 잘 만들어졌네")
    assert not _needs_evidence("이 단계는 왜 있는 거야?")
