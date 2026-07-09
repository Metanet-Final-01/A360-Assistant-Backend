"""추천안 내보내기 엔드포인트 테스트 (RPA-79, FR-17)."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.api.sessions as sessions_api
from app.db import get_db
from app.main import app

SID = uuid.uuid4()


def _rec() -> dict:
    return {
        "schema_version": "1.0",
        "steps": [{"step_id": "step-1", "actions": [
            {"order": 1, "package": "Browser", "action": "openbrowser", "label": "열기",
             "parameters": [], "children": []}]}],
        "variables": [], "notes": "",
    }


class FakeDB:
    def __init__(self, session=None, row=None):
        self.session = session
        self.row = row

    def get(self, model, key):
        return self.session

    def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.row)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


def _override(db, user=None):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[sessions_api.get_optional_user] = lambda: user


def test_export_returns_download_envelope():
    session = SimpleNamespace(id=SID, user_id=None)
    row = SimpleNamespace(id=uuid.uuid4(), version=3, source="drag", payload=_rec())
    _override(FakeDB(session=session, row=row))
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/recommendations/3/export")

    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert "attachment" in cd and "v3.json" in cd  # 다운로드 헤더
    body = r.json()
    assert body["recommendation_version"] == 3 and body["source"] == "drag"
    assert body["recommendation"]["steps"][0]["step_id"] == "step-1"  # 트리 그대로
    assert "exported_at" in body


def test_export_404_missing_version():
    session = SimpleNamespace(id=SID, user_id=None)
    _override(FakeDB(session=session, row=None))
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/recommendations/99/export")
    assert r.status_code == 404


def test_export_blocks_non_owner():
    session = SimpleNamespace(id=SID, user_id=uuid.uuid4())
    _override(FakeDB(session=session, row=None), user=SimpleNamespace(id=uuid.uuid4()))
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/recommendations/1/export")
    assert r.status_code == 403


def test_export_non_int_version_422():
    """version은 int — /recommendations/latest 등과 라우트가 안 섞인다."""
    session = SimpleNamespace(id=SID, user_id=None)
    _override(FakeDB(session=session, row=None))
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/recommendations/notanint/export")
    assert r.status_code == 422
