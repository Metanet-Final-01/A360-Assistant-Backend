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


# ─────────────────────────────────────────────────────────────────────────────
# solution PATCH·자동 확정 (RPA-285 2단계)
# ─────────────────────────────────────────────────────────────────────────────

def test_patch_solution_updates_session():
    """타 솔루션 모드 자동 확정의 되돌리기 수단 — 오탐이 사용자를 가두면 안 된다."""
    session = _session(user_id=UID)
    db = FakeDB(session=session)
    _override(db, optional_user=SimpleNamespace(id=UID))
    with TestClient(app) as c:
        r = c.patch(f"/api/sessions/{SID}", json={"solution": "uipath"})
    assert r.status_code == 200
    assert r.json()["solution"] == "uipath"
    assert session.solution == "uipath" and db.committed


def test_patch_solution_normalizes_and_reverts_to_a360():
    session = _session(user_id=UID)
    session.solution = "uipath"
    _override(FakeDB(session=session), optional_user=SimpleNamespace(id=UID))
    with TestClient(app) as c:
        r = c.patch(f"/api/sessions/{SID}", json={"solution": "  A360 "})
    assert r.status_code == 200
    assert session.solution == "a360"  # 공백 제거 + 소문자화


@pytest.mark.parametrize("bad", ["", "   ", "x" * 60, "drop table;", "한글솔루션"])
def test_patch_solution_rejects_bad_values(bad):
    """자유 문자열이 세션에 굳으면 생성 경로가 통째로 갈린다 — 형식을 좁게 막는다."""
    session = _session(user_id=UID)
    _override(FakeDB(session=session), optional_user=SimpleNamespace(id=UID))
    with TestClient(app) as c:
        r = c.patch(f"/api/sessions/{SID}", json={"solution": bad})
    assert r.status_code == 422
    assert session.solution == "a360"  # 원본 불변


def test_apply_detected_solution_respects_existing_choice(monkeypatch):
    """사용자가 정한 값이 감지보다 우선 — 오탐이 선택을 덮으면 되돌려도 다시 뒤집힌다."""
    already = SimpleNamespace(id=SID, solution="uipath")

    class _Ctx:
        def __enter__(self_inner):
            return SimpleNamespace(get=lambda m, k: already, commit=lambda: None)

        def __exit__(self_inner, *a):
            return False

    monkeypatch.setattr("app.db.SessionLocal", lambda: _Ctx())
    assert sessions_api._apply_detected_solution(SID, "power automate") is None
    assert already.solution == "uipath"  # 건드리지 않는다


def test_apply_detected_solution_sets_a360_session(monkeypatch):
    fresh = SimpleNamespace(id=SID, solution="a360")

    class _Ctx:
        def __enter__(self_inner):
            return SimpleNamespace(get=lambda m, k: fresh, commit=lambda: None)

        def __exit__(self_inner, *a):
            return False

    monkeypatch.setattr("app.db.SessionLocal", lambda: _Ctx())
    assert sessions_api._apply_detected_solution(SID, "uipath") == "uipath"
    assert fresh.solution == "uipath"


def test_apply_detected_solution_rejects_malformed_and_survives_db_failure(monkeypatch):
    """확정은 부가 기능 — 형식 오류나 DB 실패로 턴 산출을 잃으면 안 된다."""
    assert sessions_api._apply_detected_solution(SID, "'; DROP TABLE x;--") is None

    def boom():
        raise RuntimeError("DB 다운")

    monkeypatch.setattr("app.db.SessionLocal", boom)
    assert sessions_api._apply_detected_solution(SID, "uipath") is None
