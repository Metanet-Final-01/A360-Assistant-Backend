"""orchestrator/harness.py 단위 테스트 (RPA-65).

FakeCatalog(개발용 축약 카탈로그) 기준으로 R1~R6 수집과 repair 채택 규칙을 검증한다:
교정본은 위반이 줄었을 때만 채택하고, 못 줄이거나 파싱 실패면 원본을 유지한다.
"""

from app.agent.v2.orchestrator import harness as harness_mod
from app.agent.v2.orchestrator.harness import collect_violations, verify_and_repair
from app.schemas import Recommendation

from tests.agent_stubs import FakeCatalog

_CATALOG = FakeCatalog()

# 세션 무관 clean 픽스처: String/assign은 카탈로그에 있고 value(필수) 하나뿐이라
# R1~R6은 물론 세션 검사(R7~R8) 노이즈도 없다 (세션 파라미터 없음).
_CLEAN_FLOW = {
    "schema_version": "1.0",
    "steps": [{
        "step_id": "step-1",
        "actions": [{
            "order": 1, "package": "String", "action": "assign", "label": "값 지정",
            "parameters": [{"name": "value", "value": "x", "value_source": "llm"}],
            "children": [],
        }],
    }],
    "variables": [], "notes": None,
}

_BROKEN_FLOW = {
    "schema_version": "1.0",
    "steps": [{
        "step_id": "step-1",
        "actions": [{
            "order": 1, "package": "Excel_MS", "action": "SaveWorkbook",  # R1: 없는 액션
            "label": "저장", "parameters": [], "children": [],
        }],
    }],
    "variables": [], "notes": None,
}


def test_collect_violations_attaches_step_id():
    violations = collect_violations(_BROKEN_FLOW, _CATALOG)
    assert violations and violations[0]["rule"] == "R1"
    assert violations[0]["step_id"] == "step-1"


def test_verify_event_carries_full_violation_detail(monkeypatch):
    """관측 이벤트가 위반의 7필드(message=사람이 읽는 사유 포함)를 다 남긴다 (RPA-129).
    사용자 표시 문구(이벤트 message)는 '위반 N건'으로 불변."""
    events = []
    monkeypatch.setattr(harness_mod, "emit", lambda ev: events.append(ev))
    monkeypatch.setattr(harness_mod, "chat_json",
                        lambda *a, **k: Recommendation.model_validate(_BROKEN_FLOW))  # 안 줄어듦
    verify_and_repair(_BROKEN_FLOW, _CATALOG)
    verifying = [e for e in events if e.get("stage") == "verifying" and e.get("data")]
    assert verifying, "위반 있을 때 verifying data 이벤트가 있어야"
    v = verifying[0]["data"]["violations"][0]
    assert set(v) == {"rule", "location", "message", "step_id", "package", "action", "param"}
    assert v["rule"] == "R1" and v["message"]        # 사람이 읽는 사유가 남는다
    assert "교정 중" in verifying[0]["message"]        # 표시 문구는 불변


def test_clean_flow_skips_repair(monkeypatch):
    calls = []
    monkeypatch.setattr(harness_mod, "chat_json", lambda *a, **k: calls.append(1))
    result = verify_and_repair(_CLEAN_FLOW, _CATALOG)
    assert result["violations"] == []
    assert result["repaired"] is False
    assert not calls  # 무위반이면 LLM을 부르지 않는다


def test_repair_adopted_when_violations_shrink(monkeypatch):
    monkeypatch.setattr(harness_mod, "chat_json",
                        lambda *a, **k: Recommendation.model_validate(_CLEAN_FLOW))
    result = verify_and_repair(_BROKEN_FLOW, _CATALOG)
    assert result["repaired"] is True
    assert result["violations"] == []
    assert result["flow"]["steps"][0]["actions"][0]["action"] == "assign"


def test_repair_rejected_when_violations_do_not_shrink(monkeypatch):
    monkeypatch.setattr(harness_mod, "chat_json",
                        lambda *a, **k: Recommendation.model_validate(_BROKEN_FLOW))
    result = verify_and_repair(_BROKEN_FLOW, _CATALOG)
    assert result["repaired"] is False
    assert len(result["violations"]) == 1
    assert result["flow"]["steps"][0]["actions"][0]["action"] == "SaveWorkbook"  # 원본 유지


def test_repair_parse_failure_keeps_original(monkeypatch):
    def boom(*a, **k):
        raise ValueError("파싱 실패")

    monkeypatch.setattr(harness_mod, "chat_json", boom)
    result = verify_and_repair(_BROKEN_FLOW, _CATALOG)
    assert result["repaired"] is False
    assert len(result["violations"]) == 1


# --- 국소 교정 (RPA-91) ---

_ASSIGN_ACTION = {
    "order": 1, "package": "String", "action": "assign", "label": "값 지정",
    "parameters": [{"name": "value", "value": "x", "value_source": "llm"}], "children": [],
}
_BROKEN_ACTION = {
    "order": 1, "package": "Excel_MS", "action": "SaveWorkbook",  # R1: 없는 액션
    "label": "저장", "parameters": [], "children": [],
}


def _one_step_fix(step_id="step-fixed"):
    """LLM repair가 돌려주는 '고쳐진 단일-단계 흐름도' 스텁 (유효 액션 하나)."""
    return Recommendation.model_validate({
        "schema_version": "1.0",
        "steps": [{"step_id": step_id, "actions": [_ASSIGN_ACTION]}],
        "variables": [], "notes": None,
    })


def test_localized_repair_only_sends_violating_step(monkeypatch):
    """위반 없는 단계는 LLM에 넘기지 않고 원본 그대로 보존한다."""
    flow = {
        "schema_version": "1.0",
        "steps": [
            {"step_id": "step-1", "actions": [_ASSIGN_ACTION]},   # clean
            {"step_id": "step-2", "actions": [_BROKEN_ACTION]},   # R1 위반
        ],
        "variables": [], "notes": None,
    }
    payloads = []

    def fake_chat_json(messages, **k):
        payloads.append(messages[-1]["content"])  # user 메시지(흐름도+위반)
        return _one_step_fix()

    monkeypatch.setattr(harness_mod, "chat_json", fake_chat_json)
    result = verify_and_repair(flow, _CATALOG)

    # 위반 단계(step-2)만 한 번 LLM에 넘어가고, clean 단계(step-1)는 페이로드에 없다
    assert len(payloads) == 1
    assert "SaveWorkbook" in payloads[0]
    assert "step-1" not in payloads[0]
    # clean 단계는 그대로, 위반 단계만 교정, step_id는 원본 보존
    assert result["flow"]["steps"][0]["step_id"] == "step-1"
    assert result["flow"]["steps"][0]["actions"][0]["action"] == "assign"
    assert result["flow"]["steps"][1]["step_id"] == "step-2"
    assert result["flow"]["steps"][1]["actions"][0]["action"] == "assign"
    assert result["violations"] == []
    assert result["repaired"] is True


def test_localized_repair_fixes_each_scattered_step(monkeypatch):
    """여러 단계에 흩어진 위반을 단계별 독립 교정으로 각각 해소한다(전역 1회 한계 제거)."""
    flow = {
        "schema_version": "1.0",
        "steps": [
            {"step_id": "step-1", "actions": [_BROKEN_ACTION]},
            {"step_id": "step-2", "actions": [_BROKEN_ACTION]},
        ],
        "variables": [], "notes": None,
    }
    calls = []

    def fake_chat_json(messages, **k):
        calls.append(1)
        return _one_step_fix()

    monkeypatch.setattr(harness_mod, "chat_json", fake_chat_json)
    result = verify_and_repair(flow, _CATALOG)

    assert len(calls) == 2  # 두 단계가 각각 독립적으로 교정된다
    assert result["repaired"] is True
    assert result["violations"] == []
    assert all(s["actions"][0]["action"] == "assign" for s in result["flow"]["steps"])


def _web_open(name):
    """열기만 하고 닫지 않는 세션 액션(R1~R6은 clean, 세션은 미종료 → R8).

    RPA-141: 세션 opener 상수가 현행 카탈로그 표기로 바뀌어 Excel advanced를 쓴다.
    """
    return {
        "order": 1, "package": "Excel advanced", "action": "cloudExcelOpen",
        "label": "통합 문서 열기", "children": [],
        "parameters": [
            {"name": "fileSource", "value": "C:/a.xlsx", "value_source": "llm"},
            {"name": "sessionName", "value": name, "value_source": "llm"},
        ],
    }


def test_global_guard_discards_repairs_that_worsen_total(monkeypatch):
    """국소 교정이 R1은 고치되 세션 누수(R8)를 더 만들어 전체가 악화되면 모두 폐기한다.

    원본: R1 1건. 국소 교정본: R1 0건이지만 미종료 세션 2개(R8 2건) → 전체 2건.
    세션 교정도 못 줄이면 전역 가드가 원본(R1 1건)을 되돌린다. (CodeRabbit RPA-91 리뷰)
    """
    # step 국소 교정과 세션 교정 모두 이 '세션 누수' 흐름도를 돌려준다.
    leak_flow = {
        "schema_version": "1.0",
        "steps": [{"step_id": "step-1", "actions": [_web_open("A"), _web_open("B")]}],
        "variables": [], "notes": None,
    }
    monkeypatch.setattr(harness_mod, "chat_json",
                        lambda *a, **k: Recommendation.model_validate(leak_flow))
    result = verify_and_repair(_BROKEN_FLOW, _CATALOG)

    assert result["repaired"] is False  # 전체가 나빠졌으므로 교정 폐기
    assert len(result["violations"]) == 1 and result["violations"][0]["rule"] == "R1"
    assert result["flow"]["steps"][0]["actions"][0]["action"] == "SaveWorkbook"  # 원본 유지
