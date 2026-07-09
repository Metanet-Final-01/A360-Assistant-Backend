"""운영·모니터링 집계 조회 API 테스트 (RPA-81)."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.api.admin as admin_api
from app.db import get_db
from app.main import app


class FakeDB:
    def __init__(self, agg_rows=None, audit_rows=None):
        self.agg_rows = agg_rows or []
        self.audit_rows = audit_rows or []

    def execute(self, stmt):
        return SimpleNamespace(
            all=lambda: self.agg_rows,
            scalars=lambda: SimpleNamespace(all=lambda: self.audit_rows),
        )


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


def _auth(user=SimpleNamespace(id=uuid.uuid4())):
    app.dependency_overrides[admin_api.get_current_user] = (
        (lambda: (_ for _ in ()).throw(user)) if isinstance(user, Exception) else (lambda: user)
    )


def _agg(key, calls, i, o, cost):
    return SimpleNamespace(key=key, calls=calls, input_tokens=i, output_tokens=o, cost_usd=cost)


# --- llm-usage/stats ---

def test_llm_usage_stats_aggregates():
    db = FakeDB(agg_rows=[_agg("agent", 10, 1000, 200, 0.05), _agg("vision", 3, 300, 0, 0.01)])
    app.dependency_overrides[get_db] = lambda: db
    _auth()
    with TestClient(app) as c:
        r = c.get("/api/admin/llm-usage/stats", params={"group_by": "component", "days": 7})
    assert r.status_code == 200
    body = r.json()
    assert body["group_by"] == "component" and body["period_days"] == 7
    assert body["total"]["calls"] == 13 and body["total"]["input_tokens"] == 1300
    assert round(body["total"]["cost_usd"], 3) == 0.06
    assert body["breakdown"][0]["key"] == "agent"


def test_llm_usage_stats_user_group_stringifies_uuid():
    uid = uuid.uuid4()
    db = FakeDB(agg_rows=[_agg(uid, 5, 500, 100, 0.02), _agg(None, 2, 50, 0, 0.0)])
    app.dependency_overrides[get_db] = lambda: db
    _auth()
    with TestClient(app) as c:
        r = c.get("/api/admin/llm-usage/stats", params={"group_by": "user"})
    keys = [b["key"] for b in r.json()["breakdown"]]
    assert str(uid) in keys and None in keys  # UUID는 문자열로, 시스템(NULL)은 None


def test_llm_usage_stats_requires_auth():
    app.dependency_overrides[get_db] = lambda: FakeDB()
    _auth(HTTPException(401, detail={"code": "UNAUTHORIZED", "message": "x"}))
    with TestClient(app) as c:
        r = c.get("/api/admin/llm-usage/stats")
    assert r.status_code == 401


def test_llm_usage_stats_invalid_group_by_422():
    app.dependency_overrides[get_db] = lambda: FakeDB()
    _auth()
    with TestClient(app) as c:
        r = c.get("/api/admin/llm-usage/stats", params={"group_by": "session"})
    assert r.status_code == 422


# --- audit-logs ---

def test_audit_logs_returns_rows():
    rows = [SimpleNamespace(request_id="abc123", user_id=None, method="POST",
                            path="/api/sessions", status_code=201, latency_ms=12, created_at=None)]
    app.dependency_overrides[get_db] = lambda: FakeDB(audit_rows=rows)
    _auth()
    with TestClient(app) as c:
        r = c.get("/api/admin/audit-logs", params={"method": "post", "limit": 50})
    assert r.status_code == 200
    log = r.json()["logs"][0]
    assert log["method"] == "POST" and log["status_code"] == 201 and log["request_id"] == "abc123"


def test_audit_logs_invalid_user_id_400():
    app.dependency_overrides[get_db] = lambda: FakeDB()
    _auth()
    with TestClient(app) as c:
        r = c.get("/api/admin/audit-logs", params={"user_id": "not-a-uuid"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_ID"
