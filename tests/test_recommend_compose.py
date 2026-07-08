"""app/agent/recommend/compose.py 단위 테스트 (RPA-27).

llm.chat을 몽키패치해 LLM 없이 검증한다: 정상 파싱, step_id 결정론 고정,
스키마 위반 1회 교정, 교정 후에도 실패 시 ValueError.
"""

import json

import pytest

from app.agent.recommend import compose as compose_mod
from app.agent.recommend.compose import compose

_MENU = {
    "menu": [{
        "package": "WebAutomation", "action": "openpage", "label": "Open Page",
        "schema": {"package": "WebAutomation", "action": "openpage",
                   "parameters": [{"name": "url", "type": "TEXT", "required": True}]},
        "source": {"title": "Open Page", "url": None, "score": 0.9}, "forced": False,
    }],
    "examples": [], "uncovered": [], "source_map": {}, "prefmap": [],
}

_STEP = {"step_id": "step-1", "name": "네이버 접속", "description": "1. 네이버 접속", "systems": ["Edge"]}


def _valid_json(step_id="ignored"):
    return json.dumps({
        "step_id": step_id,
        "actions": [{"order": 1, "package": "WebAutomation", "action": "openpage",
                     "label": "페이지 열기",
                     "parameters": [{"name": "url", "value": "https://naver.com", "value_source": "llm"}],
                     "children": [], "rationale": "접속"}],
        "variables_used": [], "needs_input": [], "gaps": [], "notes_candidates": [],
    })


def test_compose_parses_and_fixes_step_id(monkeypatch):
    monkeypatch.setattr(compose_mod.llm, "chat", lambda *a, **k: _valid_json("LLM이-준-엉뚱한-id"))
    out = compose(_STEP, _MENU)
    # step_id는 분석 결과 기준으로 결정론 고정된다 (LLM 값 무시)
    assert out.step_id == "step-1"
    assert out.actions[0].action == "openpage"
    assert out.actions[0].parameters[0].value == "https://naver.com"


def test_compose_repairs_once_on_invalid_first_output(monkeypatch):
    calls = []

    def fake_chat(*a, **k):
        calls.append(1)
        return "그냥 문장, JSON 아님" if len(calls) == 1 else _valid_json()

    monkeypatch.setattr(compose_mod.llm, "chat", fake_chat)
    out = compose(_STEP, _MENU)
    assert len(calls) == 2  # 첫 실패 → 1회 교정
    assert out.actions[0].action == "openpage"


def test_compose_raises_valueerror_if_repair_also_fails(monkeypatch):
    monkeypatch.setattr(compose_mod.llm, "chat", lambda *a, **k: "여전히 JSON 아님")
    with pytest.raises(ValueError):
        compose(_STEP, _MENU)


def test_compose_rejects_non_action_schema(monkeypatch):
    # actions가 RecommendedAction 형태가 아니면(필수 필드 없음) 검증 실패 → 교정 → 실패 시 ValueError
    bad = json.dumps({"step_id": "x", "actions": [{"label": "package·action 없음"}]})
    monkeypatch.setattr(compose_mod.llm, "chat", lambda *a, **k: bad)
    with pytest.raises(ValueError):
        compose(_STEP, _MENU)
