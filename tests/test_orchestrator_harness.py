"""orchestrator/harness.py 단위 테스트 (RPA-65).

FakeCatalog(개발용 축약 카탈로그) 기준으로 R1~R6 수집과 repair 채택 규칙을 검증한다:
교정본은 위반이 줄었을 때만 채택하고, 못 줄이거나 파싱 실패면 원본을 유지한다.
"""

from app.agent.orchestrator import harness as harness_mod
from app.agent.orchestrator.harness import collect_violations, verify_and_repair
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
