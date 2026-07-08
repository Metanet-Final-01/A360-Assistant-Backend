"""app/agent/recommend/graph.py end-to-end 테스트 (RPA-27).

llm.chat과 retriever를 몽키패치해 인프라 없이 recommend() 스트림 전체를 구동한다:
plan → Send(단계 병렬) → step(shortlist→compose→check) → assemble → done.
그래프가 유효한 Recommendation을 산출하고, 변수 통합·이벤트 순서·에러 경로가 맞는지 본다.
"""

import asyncio
import json

from app.agent.recommend import graph as graph_mod
from app.agent.recommend import compose as compose_mod
from app.agent.recommend import shortlist as shortlist_mod


class _FakeRetriever:
    """카탈로그 표기(package_name/action_name)로 히트를 돌려주는 스텁 — 하이드레이션 성공용."""

    def search(self, query, limit=8):
        return [
            {"source_type": "action_schema", "package_name": "WebAutomation",
             "action_name": "openpage", "title": "Open Page", "url": None, "score": 0.9, "content": ""},
            {"source_type": "action_schema", "package_name": "Excel_MS",
             "action_name": "CreateSpreadsheet", "title": "생성", "url": None, "score": 0.8, "content": ""},
        ]


def _fake_chat(messages, **kwargs):
    """단계 이름에 따라 웹/엑셀 액션을 돌려준다. 변수 produce/consume로 통합 로직도 태운다."""
    user = messages[-1]["content"]
    if "엑셀" in user or "가공" in user:
        return json.dumps({
            "step_id": "x",
            "actions": [{"order": 1, "package": "Excel_MS", "action": "CreateSpreadsheet",
                         "label": "통합 문서 생성",
                         "parameters": [{"name": "filePath", "value": "C:/out.xlsx", "value_source": "llm"},
                                        {"name": "session", "value": "Default", "value_source": "schema_default"}],
                         "children": [], "rationale": "엑셀 생성"}],
            "variables_used": [{"name": "goldPrices", "type": "TABLE", "role": "consume", "description": "시세 표"}],
            "needs_input": [], "gaps": [], "notes_candidates": ["Knox 발송은 Email 서버 기준"],
        })
    return json.dumps({
        "step_id": "x",
        "actions": [{"order": 1, "package": "WebAutomation", "action": "openpage",
                     "label": "페이지 열기",
                     "parameters": [{"name": "url", "value": "https://finance.naver.com", "value_source": "llm"},
                                    {"name": "sessionName", "value": "S", "value_source": "llm"}],
                     "children": [], "rationale": "접속"}],
        "variables_used": [{"name": "goldPrices", "type": "TABLE", "role": "produce", "description": "시세 표"}],
        "needs_input": [], "gaps": [], "notes_candidates": [],
    })


def _drive(analysis, constraints=None):
    async def go():
        return [ev async for ev in graph_mod.recommend(analysis, constraints)]

    return asyncio.run(go())


def _wire(monkeypatch):
    monkeypatch.setattr(graph_mod.config, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(compose_mod.llm, "chat", _fake_chat)
    monkeypatch.setattr(shortlist_mod, "get_retriever", lambda: _FakeRetriever())


_ANALYSIS = {
    "schema_version": "1.0", "document_title": "금 시세 조회", "summary": "",
    "steps": [
        {"step_id": "step-1", "order": 1, "name": "네이버 접속", "description": "1. 네이버 접속",
         "inputs": [], "outputs": [], "systems": ["Edge"], "branching": None},
        {"step_id": "step-3", "order": 3, "name": "엑셀 가공", "description": "1. 엑셀에 넣기",
         "inputs": [], "outputs": [], "systems": ["Excel"], "branching": "최근 3일치"},
    ],
    "ambiguities": ["Knox 발송 방식 미확정"],
}


def test_recommend_produces_valid_recommendation(monkeypatch):
    _wire(monkeypatch)
    events = _drive(_ANALYSIS)

    kinds = [e.event for e in events]
    assert kinds[0] == "stage"          # plan
    assert "partial" in kinds           # step별 산출
    assert kinds[-1] == "done"          # 마지막은 완료
    assert "error" not in kinds

    rec = events[-1].data["recommendation"]
    step_ids = [s["step_id"] for s in rec["steps"]]
    assert step_ids == ["step-1", "step-3"]   # order로 재정렬됨(병렬 도착 무관)
    # 액션이 실려 있고 카탈로그 검수를 통과(위반 없음)해 confidence가 높다
    web = rec["steps"][0]["actions"][0]
    assert web["package"] == "WebAutomation" and web["action"] == "openpage"
    assert web["confidence"] == 0.9
    # sources가 shortlist source_map에서 부착됨
    assert web["sources"] and web["sources"][0]["title"] == "Open Page"


def test_recommend_unifies_cross_step_variable(monkeypatch):
    _wire(monkeypatch)
    rec = _drive(_ANALYSIS)[-1].data["recommendation"]
    gold = [v for v in rec["variables"] if v["name"] == "goldPrices"]
    assert len(gold) == 1
    # step-1이 produce, step-3이 consume → 단계 간 통로라 local
    assert gold[0]["direction"] == "local"
    assert gold[0]["type"] == "TABLE"


def test_recommend_merges_notes_and_ambiguities(monkeypatch):
    _wire(monkeypatch)
    rec = _drive(_ANALYSIS)[-1].data["recommendation"]
    assert "Knox 발송은 Email 서버 기준" in rec["notes"]
    assert "Knox 발송 방식 미확정" in rec["notes"]


def test_recommend_empty_steps_yields_empty_recommendation(monkeypatch):
    _wire(monkeypatch)
    empty = {**_ANALYSIS, "steps": [], "ambiguities": []}
    events = _drive(empty)
    assert events[-1].event == "done"
    assert events[-1].data["recommendation"]["steps"] == []


def test_recommend_missing_api_key_emits_error(monkeypatch):
    monkeypatch.setattr(graph_mod.config, "OPENAI_API_KEY", "")
    events = _drive(_ANALYSIS)
    assert len(events) == 1 and events[0].event == "error"
    assert "OPENAI_API_KEY" in events[0].message
