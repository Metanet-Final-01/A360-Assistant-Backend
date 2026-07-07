"""Agent 진입점의 UsageCallbackHandler 부착 검증 (RPA-35).

LLM/DB 없이 배선만 검증한다: run_agent/stream_agent가 그래프 config에 콜백을 싣는지,
그리고 콜백이 '요청 스레드'에서 생성돼 usage_context를 정확히 스냅샷하는지.
(토큰 추출·기록 자체는 백엔드 UsageCallbackHandler의 몫으로 RPA-33에서 검증됨.)
"""
import asyncio
import uuid

from app.agent import graph
from app.core.llm import UsageCallbackHandler, usage_context


def _handler_from(config) -> UsageCallbackHandler:
    handlers = (config or {}).get("callbacks", [])
    return next(h for h in handlers if isinstance(h, UsageCallbackHandler))


def test_run_agent_attaches_usage_callback_snapshotting_context(monkeypatch):
    captured = {}

    class _FakeGraph:
        def invoke(self, state, config=None):
            captured["config"] = config
            return {"answer": "ok", "docs": []}

    monkeypatch.setattr(graph, "_get_graph", lambda: _FakeGraph())
    monkeypatch.setattr(graph, "_require_api_key", lambda: None)

    uid = uuid.uuid4()
    with usage_context(component="agent", user_id=uid):
        result = graph.run_agent("질문")

    assert result.answer == "ok"
    handler = _handler_from(captured["config"])
    # 생성 시점(요청 스레드)의 context가 스냅샷됐는가 → component=agent, user 귀속
    assert handler._ctx.component == "agent"
    assert handler._ctx.user_id == uid
    assert handler._ctx.actor_type == "user"


def test_run_agent_default_context_is_system(monkeypatch):
    """usage_context 밖에서 호출하면 기본 system 귀속(사용자 무관)."""
    captured = {}

    class _FakeGraph:
        def invoke(self, state, config=None):
            captured["config"] = config
            return {"answer": "ok", "docs": []}

    monkeypatch.setattr(graph, "_get_graph", lambda: _FakeGraph())
    monkeypatch.setattr(graph, "_require_api_key", lambda: None)

    graph.run_agent("질문")

    handler = _handler_from(captured["config"])
    assert handler._ctx.actor_type == "system"
    assert handler._ctx.user_id is None


def test_stream_agent_attaches_usage_callback_and_keeps_stream_mode(monkeypatch):
    captured = {}

    class _FakeGraph:
        def astream(self, state, *, stream_mode=None, config=None):
            captured["stream_mode"] = stream_mode
            captured["config"] = config

            async def _gen():
                for _ in ():  # 빈 async 제너레이터 (토큰 없음)
                    yield

            return _gen()

    monkeypatch.setattr(graph, "_get_graph", lambda: _FakeGraph())
    monkeypatch.setattr(graph, "_require_api_key", lambda: None)

    uid = uuid.uuid4()

    async def _drive():
        with usage_context(component="agent", user_id=uid):
            async for _ in graph.stream_agent("질문"):
                pass

    asyncio.run(_drive())

    # 콜백을 얹어도 스트리밍 규약(stream_mode="messages")은 유지돼야 한다
    assert captured["stream_mode"] == "messages"
    handler = _handler_from(captured["config"])
    assert handler._ctx.component == "agent"
    assert handler._ctx.user_id == uid
