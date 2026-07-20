"""세션 조회·관리 엔드포인트 테스트 (RPA-78) — 목록/상세/삭제/대화이력/분석재조회."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.api.sessions as sessions_api
from app.db import get_db
from app.main import app

SID = uuid.uuid4()
UID = uuid.uuid4()


class FakeDB:
    def __init__(self, session=None, sessions=None, messages=None, analyses=None, latest=None):
        self.session = session
        self.sessions = sessions or []
        self.messages = messages or []
        self.analyses = analyses or []
        self.latest = latest
        self.deleted = 0
        self.committed = False

    def get(self, model, key):
        return self.session

    def execute(self, stmt):
        text = str(stmt).lower()
        if text.startswith("delete"):
            self.deleted += 1
            return SimpleNamespace(rowcount=1)
        if "chat_messages" in text:
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.messages))
        if "from analyses" in text or " analyses" in text:
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: self.analyses),
                scalar_one_or_none=lambda: self.latest,
            )
        if "analysis_sessions" in text:
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.sessions))
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []),
                               scalar_one_or_none=lambda: None)

    def commit(self):
        self.committed = True


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


def _override(db, *, optional_user="__unset__", current_user="__unset__"):
    app.dependency_overrides[get_db] = lambda: db
    if optional_user != "__unset__":
        app.dependency_overrides[sessions_api.get_optional_user] = lambda: optional_user
    if current_user != "__unset__":
        if isinstance(current_user, Exception):
            def _raise():
                raise current_user
            app.dependency_overrides[sessions_api.get_current_user] = _raise
        else:
            app.dependency_overrides[sessions_api.get_current_user] = lambda: current_user


def _session(user_id=None):
    return SimpleNamespace(id=SID, user_id=user_id, title="채팅", solution="a360",
                           created_at=None, updated_at=None)


# --- 목록 (인증 필수) ---

def test_list_sessions_returns_my_sessions():
    rows = [SimpleNamespace(id=uuid.uuid4(), title="t", solution="a360", created_at=None, updated_at=None)
            for _ in range(3)]
    _override(FakeDB(sessions=rows), current_user=SimpleNamespace(id=UID))
    with TestClient(app) as c:
        r = c.get("/api/sessions")
    assert r.status_code == 200
    assert len(r.json()["sessions"]) == 3


def test_list_sessions_requires_auth():
    _override(FakeDB(), current_user=HTTPException(401, detail={"code": "UNAUTHORIZED", "message": "x"}))
    with TestClient(app) as c:
        r = c.get("/api/sessions")
    assert r.status_code == 401


# --- 상세 / 삭제 ---

def test_get_session_detail():
    _override(FakeDB(session=_session()), optional_user=None)
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}")
    assert r.status_code == 200
    assert r.json()["solution"] == "a360" and r.json()["id"] == str(SID)


def test_get_session_blocks_non_owner():
    owner = uuid.uuid4()
    _override(FakeDB(session=_session(user_id=owner)), optional_user=SimpleNamespace(id=uuid.uuid4()))
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}")
    assert r.status_code == 403


def test_get_session_404_missing():
    _override(FakeDB(session=None), optional_user=None)
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}")
    assert r.status_code == 404


def test_delete_session_cascades_via_core_delete():
    db = FakeDB(session=_session())
    _override(db, optional_user=None)
    with TestClient(app) as c:
        r = c.delete(f"/api/sessions/{SID}")
    assert r.status_code == 204
    assert db.deleted == 1 and db.committed  # 원시 DELETE 실행 + 커밋


def test_delete_session_blocks_non_owner():
    db = FakeDB(session=_session(user_id=uuid.uuid4()))
    _override(db, optional_user=SimpleNamespace(id=uuid.uuid4()))
    with TestClient(app) as c:
        r = c.delete(f"/api/sessions/{SID}")
    assert r.status_code == 403
    assert db.deleted == 0  # 소유권 실패 시 삭제 안 됨


# --- 대화 이력 ---

def test_list_chat_messages():
    msgs = [
        SimpleNamespace(id=uuid.uuid4(), role="user", content="안녕", recommendation_version=None, created_at=None),
        SimpleNamespace(id=uuid.uuid4(), role="assistant", content="네", recommendation_version=2, created_at=None),
    ]
    _override(FakeDB(session=_session(), messages=msgs), optional_user=None)
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/chat-messages")
    assert r.status_code == 200
    body = r.json()["messages"]
    assert [m["role"] for m in body] == ["user", "assistant"]
    assert body[1]["recommendation_version"] == 2


# --- 분석 재조회 ---

def test_list_analyses_metadata():
    rows = [SimpleNamespace(id=uuid.uuid4(), document_id=uuid.uuid4(), status="completed",
                            model="gpt", error=None, created_at=None, completed_at=None)]
    _override(FakeDB(session=_session(), analyses=rows), optional_user=None)
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/analyses")
    assert r.status_code == 200
    assert r.json()["analyses"][0]["status"] == "completed"
    assert "result" not in r.json()["analyses"][0]  # 목록엔 result 미포함


def test_get_latest_analysis_includes_result():
    row = SimpleNamespace(id=uuid.uuid4(), document_id=uuid.uuid4(), status="completed",
                          model="gpt", error=None, created_at=None, completed_at=None,
                          result={"summary": "요약", "steps": []})
    _override(FakeDB(session=_session(), latest=row), optional_user=None)
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/analyses/latest")
    assert r.status_code == 200
    assert r.json()["result"]["summary"] == "요약"


def test_get_latest_analysis_404_when_none():
    _override(FakeDB(session=_session(), latest=None), optional_user=None)
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/analyses/latest")
    assert r.status_code == 404
