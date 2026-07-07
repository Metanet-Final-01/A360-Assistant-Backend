"""멀티턴 챗 백엔드 주입 테스트 (RPA-54) — chat_messages 이력 로드·주입·저장."""

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.api.agent as agent_api
from app.db import get_db
from app.main import app

SESSION_ID = uuid.uuid4()


class FakeDB:
    def __init__(self, session=None, history_rows=None):
        self._session = session
        self._rows = history_rows or []

    def get(self, model, key):
        return self._session

    def execute(self, stmt):
        # 실제 쿼리는 created_at DESC — _load_history가 reversed()로 시간순 복원하므로 흉내낸다
        rows = sorted(self._rows, key=lambda r: r.created_at, reverse=True)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


def _override(db, user=None):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[agent_api.get_optional_user] = lambda: user


def _msg(role, content, t):
    return SimpleNamespace(role=role, content=content, created_at=t)


# --- 무상태(하위호환): session_id 없으면 이력·저장 없음 ---

def test_stateless_when_no_session(monkeypatch):
    seen = {}

    def _fake_run(message, history=None):
        seen["history"] = history
        return SimpleNamespace(answer="답", sources=[])

    monkeypatch.setattr(agent_api, "run_agent", _fake_run)
    _override(FakeDB())
    with TestClient(app) as c:
        r = c.post("/api/agent/chat", json={"message": "안녕"})
    assert r.status_code == 200
    assert seen["history"] is None  # 이력 주입 안 함
    assert r.json()["session_id"] is None


# --- 멀티턴: 이력 로드·주입 + 저장 ---

def test_history_injected_and_turn_persisted(monkeypatch):
    seen = {}

    def _fake_run(message, history=None):
        seen["history"] = history
        return SimpleNamespace(answer="네 도와드릴게요", sources=[])

    persisted = []

    class _Persist:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def add(self, row): persisted.append(row)
        def commit(self): pass

    monkeypatch.setattr(agent_api, "run_agent", _fake_run)
    monkeypatch.setattr("app.db.SessionLocal", _Persist)

    session = SimpleNamespace(id=SESSION_ID, user_id=None)
    history = [_msg("user", "이전 질문", 1), _msg("assistant", "이전 답", 2)]
    _override(FakeDB(session=session, history_rows=history))

    with TestClient(app) as c:
        r = c.post("/api/agent/chat", json={"message": "이어서 질문", "session_id": str(SESSION_ID)})

    assert r.status_code == 200
    # 이력이 [{"role","content"}] 형태로 주입됐는지
    assert seen["history"] == [
        {"role": "user", "content": "이전 질문"},
        {"role": "assistant", "content": "이전 답"},
    ]
    # 이번 턴(user+assistant)이 저장됐는지 (감사 미들웨어의 AuditLog는 제외하고 ChatMessage만)
    chat_msgs = [p for p in persisted if type(p).__name__ == "ChatMessage"]
    assert [(p.role, p.content) for p in chat_msgs] == [
        ("user", "이어서 질문"),
        ("assistant", "네 도와드릴게요"),
    ]
    assert r.json()["session_id"] == str(SESSION_ID)


def test_chat_blocks_non_owner(monkeypatch):
    monkeypatch.setattr(agent_api, "run_agent", lambda *a, **k: SimpleNamespace(answer="x", sources=[]))
    owner = uuid.uuid4()
    session = SimpleNamespace(id=SESSION_ID, user_id=owner)
    _override(FakeDB(session=session), user=SimpleNamespace(id=uuid.uuid4()))  # 다른 사용자
    with TestClient(app) as c:
        r = c.post("/api/agent/chat", json={"message": "hi", "session_id": str(SESSION_ID)})
    assert r.status_code == 403


def test_unknown_session_404(monkeypatch):
    monkeypatch.setattr(agent_api, "run_agent", lambda *a, **k: SimpleNamespace(answer="x", sources=[]))
    _override(FakeDB(session=None))
    with TestClient(app) as c:
        r = c.post("/api/agent/chat", json={"message": "hi", "session_id": str(uuid.uuid4())})
    assert r.status_code == 404


# --- 스트리밍: 이력 주입 + done에 session_id + 저장 ---

def test_stream_injects_history_and_persists(monkeypatch):
    seen = {}

    async def _fake_stream(message, history=None):
        seen["history"] = history
        for t in ["도", "움"]:
            yield t

    persisted = []

    class _Persist:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def add(self, row): persisted.append(row)
        def commit(self): pass

    monkeypatch.setattr(agent_api, "stream_agent", _fake_stream)
    monkeypatch.setattr("app.db.SessionLocal", _Persist)

    session = SimpleNamespace(id=SESSION_ID, user_id=None)
    _override(FakeDB(session=session, history_rows=[_msg("user", "앞선말", 1)]))

    with TestClient(app) as c:
        with c.stream("POST", "/api/agent/chat/stream",
                      json={"message": "q", "session_id": str(SESSION_ID)}) as r:
            events = [json.loads(l[5:]) for l in r.iter_lines() if l.startswith("data:")]

    assert seen["history"] == [{"role": "user", "content": "앞선말"}]
    done = events[-1]
    assert done["event"] == "done" and done["data"]["session_id"] == str(SESSION_ID)
    # 누적된 답변("도움")이 저장됐는지 (ChatMessage만)
    chat_msgs = [p for p in persisted if type(p).__name__ == "ChatMessage"]
    assert ("assistant", "도움") in [(p.role, p.content) for p in chat_msgs]


# --- 세션 생성 엔드포인트 ---

def test_create_session_endpoint(monkeypatch):
    created = []

    class _DB:
        def add(self, obj):
            obj.id = SESSION_ID
            obj.created_at = None
            created.append(obj)
        def commit(self): pass

    app.dependency_overrides[get_db] = lambda: _DB()
    import app.api.sessions as sessions_api
    app.dependency_overrides[sessions_api.get_optional_user] = lambda: None
    try:
        with TestClient(app) as c:
            r = c.post("/api/sessions")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 201
    assert r.json()["session_id"] == str(SESSION_ID)
