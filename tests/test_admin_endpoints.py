"""운영·모니터링 집계 조회 API 테스트 (RPA-81)."""

import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.api.admin as admin_api
from app.core.observability_db import get_obs_db
from app.db import get_db
from app.main import app


class FakeDB:
    def __init__(self, agg_rows=None, scalar_rows=None):
        self.agg_rows = agg_rows or []
        self.scalar_rows = scalar_rows or []

    def execute(self, stmt):
        return SimpleNamespace(
            all=lambda: self.agg_rows,
            scalars=lambda: SimpleNamespace(all=lambda: self.scalar_rows),
            scalar_one_or_none=lambda: (self.scalar_rows[0] if self.scalar_rows else None),
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


# --- 관리자 게이트 (RPA-118: is_admin 속성 + 서비스 API 키) ---

def _as_user(is_admin: bool):
    """get_optional_user를 지정 사용자로 오버라이드 (require_admin이 이 의존성을 씀)."""
    app.dependency_overrides[admin_api.get_optional_user] = (
        lambda: SimpleNamespace(id=uuid.uuid4(), email="u@test.com", is_admin=is_admin)
    )


def test_non_admin_user_403(monkeypatch):
    """is_admin=False 로그인 사용자는 403 — 전 사용자 데이터 열람 차단."""
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    app.dependency_overrides[get_obs_db] = lambda: FakeDB()
    _as_user(is_admin=False)
    with TestClient(app) as c:
        r = c.get("/api/admin/llm-usage/stats")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "FORBIDDEN"


def test_anonymous_denied(monkeypatch):
    """토큰도 API 키도 없으면 차단 (fail-closed)."""
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    app.dependency_overrides[get_obs_db] = lambda: FakeDB()
    app.dependency_overrides[admin_api.get_optional_user] = lambda: None
    with TestClient(app) as c:
        r = c.get("/api/admin/audit-logs")
    assert r.status_code == 403


def test_is_admin_user_passes(monkeypatch):
    """is_admin=True 사용자는 통과 — 인가는 서버 속성으로 판정."""
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    app.dependency_overrides[get_obs_db] = lambda: FakeDB()
    _as_user(is_admin=True)
    with TestClient(app) as c:
        r = c.get("/api/admin/llm-usage/stats")
    assert r.status_code == 200


def test_service_api_key_passes(monkeypatch):
    """유효한 X-API-Key(머신 신원)는 사용자 토큰 없이 통과."""
    monkeypatch.setenv("OPS_API_KEY", "s3cr3t-ops-key")
    app.dependency_overrides[get_obs_db] = lambda: FakeDB()
    app.dependency_overrides[admin_api.get_optional_user] = lambda: None  # JWT 없음
    with TestClient(app) as c:
        r = c.get("/api/admin/llm-usage/stats", headers={"X-API-Key": "s3cr3t-ops-key"})
    assert r.status_code == 200


def test_service_api_key_wrong_403(monkeypatch):
    """틀린 API 키는 403."""
    monkeypatch.setenv("OPS_API_KEY", "s3cr3t-ops-key")
    app.dependency_overrides[get_obs_db] = lambda: FakeDB()
    app.dependency_overrides[admin_api.get_optional_user] = lambda: None
    with TestClient(app) as c:
        r = c.get("/api/admin/audit-logs", headers={"X-API-Key": "wrong"})
    assert r.status_code == 403


def test_service_api_key_disabled_when_unset(monkeypatch):
    """OPS_API_KEY 미설정이면 어떤 X-API-Key도 통과 못 함 (빈 키 우연 통과 방지)."""
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    app.dependency_overrides[get_obs_db] = lambda: FakeDB()
    app.dependency_overrides[admin_api.get_optional_user] = lambda: None
    with TestClient(app) as c:
        r = c.get("/api/admin/audit-logs", headers={"X-API-Key": ""})
    assert r.status_code == 403


def _receipt_row():
    row_id = uuid.uuid4()
    recommendation_id = uuid.uuid4()
    session_id = uuid.uuid4()
    candidate_id = "sha256:" + "a" * 64
    payload_digest = "sha256:" + "b" * 64
    policy_digest = "sha256:" + "c" * 64
    catalog_digest = "sha256:" + "d" * 64
    observation_id = "sha256:" + "e" * 64
    payload = {
        "schema_version": "1.0",
        "harness": "output",
        "record_kind": "output_observation",
        "writer_authority": "backend_boundary_observer",
        "source": "drag",
        "source_observation_id": observation_id,
        "subject": {
            "recommendation_id": str(recommendation_id),
            "recommendation_version": 2,
            "session_id": str(session_id),
            "request_id": "request-1",
            "candidate_id": candidate_id,
            "payload_digest": payload_digest,
        },
        "decision": "deny",
        "assurance_verdict": "deny",
        "assurance_status": "unassured_observe",
        "evidence_valid": True,
        "completeness": {"status": "complete", "missing": []},
        "provenance": {
            "validator_version": "v1",
            "policy_digest": policy_digest,
            "catalog_digest": catalog_digest,
            "requested_agent_version": None,
            "resolved_agent_version": None,
        },
        "enforcement": {"mode": "observe", "effect": "none"},
        "business_outcome": {"persisted": True},
    }
    from app.services.assurance_evidence import digest

    return SimpleNamespace(
        id=row_id, receipt_digest=digest(payload), schema_version="1.0", harness="output",
        record_kind="output_observation", writer_authority="backend_boundary_observer",
        source="drag", request_id="request-1", session_id=session_id,
        recommendation_id=recommendation_id, recommendation_version=2,
        candidate_id=candidate_id, payload_digest=payload_digest,
        source_observation_id=observation_id,
        evidence_valid=True, completeness_status="complete", decision="deny",
        assurance_verdict="deny", assurance_status="unassured_observe", rollout_mode="observe",
        enforcement_effect="none", business_persisted=True, validator_version="v1",
        policy_digest=policy_digest, catalog_digest=catalog_digest,
        requested_agent_version=None, resolved_agent_version=None,
        receipt_payload=payload, created_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
    )


def test_assurance_receipts_are_admin_only_and_read_only(monkeypatch):
    row = _receipt_row()
    app.dependency_overrides[get_db] = lambda: FakeDB(scalar_rows=[row])
    _auth()
    with TestClient(app) as c:
        listed = c.get("/api/admin/assurance-receipts")
        detail = c.get(f"/api/admin/assurance-receipts/{row.receipt_digest}")
        write = c.post("/api/admin/assurance-receipts", json={"decision": "allow_candidate"})

    assert listed.status_code == 200
    assert listed.json()["receipts"][0]["integrity_valid"] is True
    cursor_time, cursor_id = admin_api._decode_assurance_cursor(listed.json()["next_cursor"])
    assert cursor_time == row.created_at and cursor_id == row.id
    assert "receipt_payload" not in listed.json()["receipts"][0]
    assert detail.status_code == 200
    assert detail.json()["receipt_payload"]["decision"] == "deny"
    assert write.status_code == 405


def test_assurance_receipts_reject_non_admin(monkeypatch):
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    app.dependency_overrides[get_db] = lambda: FakeDB()
    _as_user(is_admin=False)
    with TestClient(app) as c:
        r = c.get("/api/admin/assurance-receipts")
    assert r.status_code == 403


def test_assurance_receipts_invalid_session_id_400():
    app.dependency_overrides[get_db] = lambda: FakeDB()
    _auth()
    with TestClient(app) as c:
        r = c.get("/api/admin/assurance-receipts", params={"session_id": "not-a-uuid"})
    assert r.status_code == 400


def test_assurance_receipts_invalid_composite_cursor_400():
    app.dependency_overrides[get_db] = lambda: FakeDB()
    _auth()
    with TestClient(app) as c:
        r = c.get("/api/admin/assurance-receipts", params={"cursor": "not-base64"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_CURSOR"


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


def test_llm_usage_stats_group_by_session(monkeypatch):
    """세션별 비용 축(대시보드 #6, RPA-124) — session_id도 UUID라 문자열화."""
    sid = uuid.uuid4()
    db = FakeDB(agg_rows=[_agg(sid, 4, 400, 80, 0.02)])
    app.dependency_overrides[get_obs_db] = lambda: db
    _auth()
    with TestClient(app) as c:
        r = c.get("/api/admin/llm-usage/stats", params={"group_by": "session"})
    assert r.status_code == 200
    assert r.json()["group_by"] == "session"
    assert str(sid) in [b["key"] for b in r.json()["breakdown"]]


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
        r = c.get("/api/admin/llm-usage/stats", params={"group_by": "purpose"})  # 화이트리스트 밖
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


def test_rag_events_returns_rows():
    """RAG 파이프라인 단계 로그 조회(RPA-128) — request_id로 필터."""
    rows = [SimpleNamespace(id=3, request_id="abc123", event="hybrid_search", function="search",
                            status="ok", duration_ms=1726.68, detail='{"result":{"count":5}}',
                            created_at=None)]
    app.dependency_overrides[get_obs_db] = lambda: FakeDB(scalar_rows=rows)
    _auth()
    with TestClient(app) as c:
        r = c.get("/api/admin/rag-events", params={"request_id": "abc123"})
    assert r.status_code == 200
    ev = r.json()["events"][0]
    assert ev["event"] == "hybrid_search" and ev["duration_ms"] == 1726.68 and ev["request_id"] == "abc123"


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
