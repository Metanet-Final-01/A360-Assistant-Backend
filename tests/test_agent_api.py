"""Agent 챗 API 및 실제 검색기 연결 테스트 (RPA-24).

run_agent/stream_agent는 OpenAI를 부르므로 모킹한다 (엔드포인트 배선만 검증).
"""

import json

import pytest
from fastapi.testclient import TestClient

import app.api.agent as agent_api
from app.main import app
from app.schemas import RagSource


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_chat_returns_answer_and_sources(client, monkeypatch):
    from app.agent import AgentResult

    def _fake_run(message):
        assert message == "엑셀 읽는 법"
        return AgentResult(
            answer="Excel advanced 패키지를 쓰세요",
            sources=[RagSource(source_type="action_schema", title="Excel: Open", score=0.9)],
        )

    monkeypatch.setattr(agent_api, "run_agent", _fake_run)
    r = client.post("/api/agent/chat", json={"message": "엑셀 읽는 법"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "Excel advanced 패키지를 쓰세요"
    assert body["sources"][0]["title"] == "Excel: Open"


def test_chat_runtime_error_maps_to_503(client, monkeypatch):
    def _boom(message):
        raise RuntimeError("OPENAI_API_KEY 환경변수가 필요합니다")

    monkeypatch.setattr(agent_api, "run_agent", _boom)
    r = client.post("/api/agent/chat", json={"message": "안녕"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "AGENT_UNAVAILABLE"


def test_chat_rejects_empty_message(client):
    assert client.post("/api/agent/chat", json={"message": ""}).status_code == 422


def test_chat_stream_emits_token_then_done(client, monkeypatch):
    async def _fake_stream(message):
        for tok in ["엑", "셀"]:
            yield tok

    monkeypatch.setattr(agent_api, "stream_agent", _fake_stream)
    with client.stream("POST", "/api/agent/chat/stream", json={"message": "질문"}) as r:
        events = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
    assert [e["event"] for e in events] == ["token", "token", "done"]
    assert "".join(e["message"] for e in events if e["event"] == "token") == "엑셀"


def test_chat_stream_error_event_on_runtime_error(client, monkeypatch):
    async def _boom_stream(message):
        raise RuntimeError("키 없음")
        yield  # pragma: no cover — 제너레이터로 만들기 위한 형식상 yield

    monkeypatch.setattr(agent_api, "stream_agent", _boom_stream)
    with client.stream("POST", "/api/agent/chat/stream", json={"message": "질문"}) as r:
        events = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
    assert events[-1]["event"] == "error"


# --- 검색기 연결 ---

def test_hybrid_retriever_delegates_to_search_actions(monkeypatch):
    import app.services.agent_retriever as ar

    captured = {}

    def _fake_search_actions(query, k=5, source_types=None):
        captured["query"], captured["k"] = query, k
        return [{"id": "1", "source_type": "action_schema", "title": "t", "content": "c", "score": 0.5}]

    monkeypatch.setattr(ar, "search_actions", _fake_search_actions)
    results = ar.HybridRetriever().search("엑셀", limit=3)
    assert captured == {"query": "엑셀", "k": 3}
    assert results[0]["source_type"] == "action_schema"


def test_get_retriever_switches_by_env(monkeypatch):
    from app.agent import retrieval
    from app.agent.retrieval import FakeRetriever

    monkeypatch.setenv("AGENT_RETRIEVER", "fake")
    assert isinstance(retrieval.get_retriever(), FakeRetriever)

    monkeypatch.setenv("AGENT_RETRIEVER", "hybrid")
    from app.services.agent_retriever import HybridRetriever

    assert isinstance(retrieval.get_retriever(), HybridRetriever)
