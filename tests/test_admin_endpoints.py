"""운영·모니터링 집계 조회 API 테스트 (RPA-81)."""

import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.api.admin as admin_api
from app.core.observability_db import get_obs_db
from app.main import app


class FakeDB:
    def __init__(self, agg_rows=None, scalar_rows=None):
        self.agg_rows = agg_rows or []
        self.scalar_rows = scalar_rows or []

    def execute(self, stmt):
        return SimpleNamespace(
            all=lambda: self.agg_rows,
            scalars=lambda: SimpleNamespace(all=lambda: self.scalar_rows),
        )


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


def _auth(user=SimpleNamespace(id=uuid.uuid4(), email="admin@test.com")):
    """관리자 게이트 통과 상태로 오버라이드 (게이트 자체 검증은 아래 별도 테스트)."""
    app.dependency_overrides[admin_api.require_admin] = (
        (lambda: (_ for _ in ()).throw(user)) if isinstance(user, Exception) else (lambda: user)
    )


def _agg(key, calls, i, o, cost):
    return SimpleNamespace(key=key, calls=calls, input_tokens=i, output_tokens=o, cost_usd=cost)


# --- 관리자 게이트 (ADMIN_EMAILS 화이트리스트, CodeRabbit) ---

def test_non_admin_user_403(monkeypatch):
    """화이트리스트에 없는 로그인 사용자는 403 — 전 사용자 데이터 열람 차단."""
    monkeypatch.setenv("ADMIN_EMAILS", "admin@test.com")
    app.dependency_overrides[get_obs_db] = lambda: FakeDB()
    app.dependency_overrides[admin_api.get_current_user] = (
        lambda: SimpleNamespace(id=uuid.uuid4(), email="someone@test.com")
    )
    with TestClient(app) as c:
        r = c.get("/api/admin/llm-usage/stats")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "FORBIDDEN"


def test_admin_emails_unset_denies_all(monkeypatch):
    """ADMIN_EMAILS 미설정이면 전부 차단 (fail-closed)."""
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    app.dependency_overrides[get_obs_db] = lambda: FakeDB()
    app.dependency_overrides[admin_api.get_current_user] = (
        lambda: SimpleNamespace(id=uuid.uuid4(), email="anyone@test.com")
    )
    with TestClient(app) as c:
        r = c.get("/api/admin/audit-logs")
    assert r.status_code == 403


def test_whitelisted_admin_passes(monkeypatch):
    """화이트리스트 이메일(대소문자 무시)은 통과."""
    monkeypatch.setenv("ADMIN_EMAILS", "Admin@Test.com, ops@test.com")
    app.dependency_overrides[get_obs_db] = lambda: FakeDB()
    app.dependency_overrides[admin_api.get_current_user] = (
        lambda: SimpleNamespace(id=uuid.uuid4(), email="admin@test.com")
    )
    with TestClient(app) as c:
        r = c.get("/api/admin/llm-usage/stats")
    assert r.status_code == 200


# --- llm-usage/stats ---

def test_llm_usage_stats_aggregates():
    db = FakeDB(agg_rows=[_agg("agent", 10, 1000, 200, 0.05), _agg("vision", 3, 300, 0, 0.01)])
    app.dependency_overrides[get_obs_db] = lambda: db
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
    app.dependency_overrides[get_obs_db] = lambda: db
    _auth()
    with TestClient(app) as c:
        r = c.get("/api/admin/llm-usage/stats", params={"group_by": "user"})
    keys = [b["key"] for b in r.json()["breakdown"]]
    assert str(uid) in keys and None in keys  # UUID는 문자열로, 시스템(NULL)은 None


def test_llm_usage_stats_requires_auth():
    app.dependency_overrides[get_obs_db] = lambda: FakeDB()
    _auth(HTTPException(401, detail={"code": "UNAUTHORIZED", "message": "x"}))
    with TestClient(app) as c:
        r = c.get("/api/admin/llm-usage/stats")
    assert r.status_code == 401


def test_llm_usage_stats_invalid_group_by_422():
    app.dependency_overrides[get_obs_db] = lambda: FakeDB()
    _auth()
    with TestClient(app) as c:
        r = c.get("/api/admin/llm-usage/stats", params={"group_by": "session"})
    assert r.status_code == 422


# --- audit-logs ---

def test_audit_logs_returns_rows():
    rows = [SimpleNamespace(request_id="abc123", user_id=None, method="POST",
                            path="/api/sessions", status_code=201, latency_ms=12, created_at=None)]
    app.dependency_overrides[get_obs_db] = lambda: FakeDB(scalar_rows=rows)
    _auth()
    with TestClient(app) as c:
        r = c.get("/api/admin/audit-logs", params={"method": "post", "limit": 50})
    assert r.status_code == 200
    log = r.json()["logs"][0]
    assert log["method"] == "POST" and log["status_code"] == 201 and log["request_id"] == "abc123"


def test_audit_logs_invalid_user_id_400():
    app.dependency_overrides[get_obs_db] = lambda: FakeDB()
    _auth()
    with TestClient(app) as c:
        r = c.get("/api/admin/audit-logs", params={"user_id": "not-a-uuid"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_ID"


# --- metrics-daily ---

def test_metrics_daily_returns_rows():
    rows = [SimpleNamespace(day=date(2026, 7, 11), method="GET", path="/api/sessions/:id",
                            calls=42, err_4xx=1, err_5xx=0, p50_ms=80, p95_ms=200, avg_ms=95, max_ms=500)]
    app.dependency_overrides[get_obs_db] = lambda: FakeDB(scalar_rows=rows)
    _auth()
    with TestClient(app) as c:
        r = c.get("/api/admin/metrics-daily", params={"method": "get", "days": 7})
    assert r.status_code == 200
    row = r.json()["rows"][0]
    assert row["day"] == "2026-07-11" and row["path"] == "/api/sessions/:id" and row["calls"] == 42
    assert row["p95_ms"] == 200


def test_metrics_daily_requires_auth():
    app.dependency_overrides[get_obs_db] = lambda: FakeDB()
    _auth(HTTPException(401, detail={"code": "UNAUTHORIZED", "message": "x"}))
    with TestClient(app) as c:
        r = c.get("/api/admin/metrics-daily")
    assert r.status_code == 401


# --- usage-daily ---

def test_usage_daily_returns_rows():
    rows = [SimpleNamespace(day=date(2026, 7, 11), component="agent", purpose="recommend",
                            model="claude-sonnet-5", calls=5, input_tokens=1000, output_tokens=200, cost_usd=0.05)]
    app.dependency_overrides[get_obs_db] = lambda: FakeDB(scalar_rows=rows)
    _auth()
    with TestClient(app) as c:
        r = c.get("/api/admin/usage-daily", params={"component": "agent", "days": 30})
    assert r.status_code == 200
    row = r.json()["rows"][0]
    assert row["day"] == "2026-07-11" and row["component"] == "agent" and row["cost_usd"] == 0.05


def test_usage_daily_requires_auth():
    app.dependency_overrides[get_obs_db] = lambda: FakeDB()
    _auth(HTTPException(401, detail={"code": "UNAUTHORIZED", "message": "x"}))
    with TestClient(app) as c:
        r = c.get("/api/admin/usage-daily")
    assert r.status_code == 401


# --- turn-events ---

def test_turn_events_returns_rows():
    sid = uuid.uuid4()
    rows = [SimpleNamespace(session_id=sid, request_id="req1", seq=0, kind="stage",
                            stage="agent", message="시작", detail=None, elapsed_ms=10, created_at=None)]
    app.dependency_overrides[get_obs_db] = lambda: FakeDB(scalar_rows=rows)
    _auth()
    with TestClient(app) as c:
        r = c.get("/api/admin/turn-events", params={"session_id": str(sid)})
    assert r.status_code == 200
    ev = r.json()["events"][0]
    assert ev["session_id"] == str(sid) and ev["kind"] == "stage" and ev["seq"] == 0


def test_turn_events_invalid_session_id_400():
    app.dependency_overrides[get_obs_db] = lambda: FakeDB()
    _auth()
    with TestClient(app) as c:
        r = c.get("/api/admin/turn-events", params={"session_id": "not-a-uuid"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_ID"


def test_turn_events_requires_auth():
    app.dependency_overrides[get_obs_db] = lambda: FakeDB()
    _auth(HTTPException(401, detail={"code": "UNAUTHORIZED", "message": "x"}))
    with TestClient(app) as c:
        r = c.get("/api/admin/turn-events")
    assert r.status_code == 401


# --- request-metrics (RPA-117) ---

def test_request_metrics_returns_rows():
    from datetime import datetime, timezone

    rows = [SimpleNamespace(id=7, request_id="abc123", user_id=None, method="GET",
                            path="/api/sessions/:id", status_code=200, latency_ms=45,
                            created_at=datetime(2026, 7, 13, 3, 0, tzinfo=timezone.utc))]
    app.dependency_overrides[get_obs_db] = lambda: FakeDB(scalar_rows=rows)
    _auth()
    with TestClient(app) as c:
        r = c.get("/api/admin/request-metrics", params={"method": "get", "limit": 100})
    assert r.status_code == 200
    row = r.json()["rows"][0]
    assert row["id"] == 7 and row["path"] == "/api/sessions/:id" and row["latency_ms"] == 45
    assert row["created_at"].startswith("2026-07-13T03:00")  # 수집기가 다음 since로 되돌려주는 값


def test_request_metrics_accepts_since_roundtrip():
    """응답의 created_at(isoformat)을 그대로 since로 되돌려도 400이 안 난다 (커서 왕복)."""
    app.dependency_overrides[get_obs_db] = lambda: FakeDB()
    _auth()
    with TestClient(app) as c:
        r = c.get("/api/admin/request-metrics", params={"since": "2026-07-13T03:00:00+00:00"})
    assert r.status_code == 200


def test_request_metrics_invalid_since_400():
    app.dependency_overrides[get_obs_db] = lambda: FakeDB()
    _auth()
    with TestClient(app) as c:
        r = c.get("/api/admin/request-metrics", params={"since": "어제쯤"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_SINCE"


def test_request_metrics_requires_auth():
    app.dependency_overrides[get_obs_db] = lambda: FakeDB()
    _auth(HTTPException(401, detail={"code": "UNAUTHORIZED", "message": "x"}))
    with TestClient(app) as c:
        r = c.get("/api/admin/request-metrics")
    assert r.status_code == 401


def test_audit_logs_invalid_since_400():
    """audit-logs의 증분 커서도 같은 파서를 탄다 (RPA-117)."""
    app.dependency_overrides[get_obs_db] = lambda: FakeDB()
    _auth()
    with TestClient(app) as c:
        r = c.get("/api/admin/audit-logs", params={"since": "not-a-date"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_SINCE"
