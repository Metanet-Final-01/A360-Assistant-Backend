"""app/agent/recommend/check.py — 액션별 RAG 근거 기반 신뢰도 산정 테스트.

confidence가 (1) 검색 score로 액션별 차등화되고, (2) 컨테이너/근거없음 액션을
구분하며, (3) 자식까지 각자 산정되고, (4) sources가 부착되는지 검증한다.
정확한 산출 근거(검색 score↔검수 결합)는 팀 합의 후 확정 예정 — 이 테스트는
1차 산식(검색 근거 기반)의 계약을 고정한다. LLM·DB 없이 FakeCatalog로 돈다.
"""

from app.agent.recommend.check import _CONF_STRUCTURAL, _CONF_UNGROUNDED, check
from tests.agent_stubs import FakeCatalog

CAT = FakeCatalog()


def _src(title, score, url=None):
    return {"title": title, "url": url, "score": score}


def test_confidence_from_search_score():
    actions = [{"package": "WebAutomation", "action": "openpage",
                "parameters": [{"name": "url", "value": "http://x"},
                               {"name": "sessionName", "value": "S"}]}]
    result = check(actions, CAT, {"WebAutomation/openpage": _src("Open Page", 0.82)})
    assert result["actions"][0]["confidence"] == 0.82


def test_confidence_differs_per_action():
    actions = [
        {"package": "WebAutomation", "action": "openpage",
         "parameters": [{"name": "url", "value": "http://x"}, {"name": "sessionName", "value": "S"}]},
        {"package": "Excel_MS", "action": "CreateSpreadsheet",
         "parameters": [{"name": "filePath", "value": "C:/o.xlsx"}, {"name": "session", "value": "Default"}]},
    ]
    source_map = {
        "WebAutomation/openpage": _src("Open Page", 0.9),
        "Excel_MS/CreateSpreadsheet": _src("생성", 0.6),
    }
    confs = [a["confidence"] for a in check(actions, CAT, source_map)["actions"]]
    assert confs == [0.9, 0.6]   # 액션별로 검색 근거가 다르면 신뢰도도 다르다


def test_container_action_is_structural_confidence():
    # Loop은 결정론 주입 컨테이너 — 검색 히트가 없어도 구조적으로 확실
    actions = [{"package": "Loop", "action": "loop.commands.start",
                "parameters": [{"name": "iteratorType", "value": "N times"}], "children": []}]
    assert check(actions, CAT, {})["actions"][0]["confidence"] == _CONF_STRUCTURAL


def test_ungrounded_action_is_low_confidence():
    # 검색 히트도 없고 컨테이너도 아닌 액션 — LLM이 KB 근거 없이 고른 것
    actions = [{"package": "String", "action": "assign",
                "parameters": [{"name": "value", "value": "x"}]}]
    assert check(actions, CAT, {})["actions"][0]["confidence"] == _CONF_UNGROUNDED


def test_children_get_own_confidence_not_parent_copy():
    # 컨테이너(구조적)의 자식은 자기 검색 근거로 산정한다 — 부모 값 복제가 아님
    actions = [{
        "package": "Loop", "action": "loop.commands.start",
        "parameters": [{"name": "iteratorType", "value": "N times"}],
        "children": [{"package": "Excel_MS", "action": "SetCell",
                      "parameters": [{"name": "cellAddress", "value": "A1"},
                                     {"name": "session", "value": "S"}]}],
    }]
    loop = check(actions, CAT, {"Excel_MS/SetCell": _src("셀 설정", 0.7)})["actions"][0]
    assert loop["confidence"] == _CONF_STRUCTURAL
    assert loop["children"][0]["confidence"] == 0.7   # 자식은 자기 score로


def test_sources_attached_from_source_map():
    actions = [{"package": "WebAutomation", "action": "openpage",
                "parameters": [{"name": "url", "value": "http://x"}, {"name": "sessionName", "value": "S"}]}]
    src = check(actions, CAT, {"WebAutomation/openpage": _src("Open Page", 0.5, url="http://doc")})
    got = src["actions"][0]["sources"]
    assert got and got[0]["title"] == "Open Page" and got[0]["score"] == 0.5


def test_score_none_treated_as_ungrounded():
    actions = [{"package": "WebAutomation", "action": "openpage",
                "parameters": [{"name": "url", "value": "http://x"}, {"name": "sessionName", "value": "S"}]}]
    result = check(actions, CAT, {"WebAutomation/openpage": _src("Open Page", None)})
    assert result["actions"][0]["confidence"] == _CONF_UNGROUNDED


def test_score_clamped_to_unit_interval():
    actions = [{"package": "String", "action": "assign", "parameters": [{"name": "value", "value": "x"}]}]
    assert check(actions, CAT, {"String/assign": _src("assign", 1.7)})["actions"][0]["confidence"] == 1.0


def test_step_confidence_is_min_of_actions():
    actions = [
        {"package": "WebAutomation", "action": "openpage",
         "parameters": [{"name": "url", "value": "http://x"}, {"name": "sessionName", "value": "S"}]},
        {"package": "String", "action": "assign", "parameters": [{"name": "value", "value": "x"}]},  # ungrounded
    ]
    result = check(actions, CAT, {"WebAutomation/openpage": _src("Open Page", 0.9)})
    assert result["confidence"] == _CONF_UNGROUNDED   # min(0.9, 0.3) — 가장 약한 액션 기준


def test_existing_confidence_is_preserved():
    actions = [{"package": "WebAutomation", "action": "openpage", "confidence": 0.11,
                "parameters": [{"name": "url", "value": "http://x"}, {"name": "sessionName", "value": "S"}]}]
    result = check(actions, CAT, {"WebAutomation/openpage": _src("Open Page", 0.9)})
    assert result["actions"][0]["confidence"] == 0.11   # 이미 지정된 값은 덮지 않는다


def test_empty_actions_yields_zero_step_confidence():
    assert check([], CAT, {})["confidence"] == 0.0
