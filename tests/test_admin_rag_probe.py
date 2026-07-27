"""admin RAG 진단 엔드포인트 테스트 (RPA-270).

GET /api/admin/rag/health — 설정·도달성(외부 실호출 없음)
POST /api/admin/rag/probe — 실호출 진단. require_admin + 서버측 cooldown(30초) + error_type만.
둘 다 require_admin(OPS_API_KEY M2M 또는 is_admin JWT). 여기선 M2M(X-API-Key) 경로로 검증.
"""

import json

from fastapi.testclient import TestClient

import app.api.admin as admin_mod
from app.main import app


def _ops(monkeypatch, key="ops-secret-key"):
    monkeypatch.setenv("OPS_API_KEY", key)
    return {"X-API-Key": key}


def test_health_forbidden_without_auth(monkeypatch):
    monkeypatch.delenv("OPS_API_KEY", raising=False)  # M2M 비활성(fail-closed)
    with TestClient(app) as c:
        r = c.get("/api/admin/rag/health")
    assert r.status_code == 403


def test_health_ok_with_ops_key(monkeypatch):
    headers = _ops(monkeypatch)
    import app.services.rag_diagnostics as diag

    monkeypatch.setattr(diag, "service_status", lambda: {"embedding": {"api_key_configured": True}})
    with TestClient(app) as c:
        r = c.get("/api/admin/rag/health", headers=headers)
    assert r.status_code == 200
    assert r.json()["embedding"]["api_key_configured"] is True


def test_probe_forbidden_without_auth(monkeypatch):
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    with TestClient(app) as c:
        r = c.post("/api/admin/rag/probe")
    assert r.status_code == 403


def test_probe_ok_returns_error_type_only(monkeypatch):
    headers = _ops(monkeypatch)
    monkeypatch.setattr(admin_mod, "_probe_last_monotonic", 0.0)  # cooldown 초기화(테스트 격리)
    import app.services.rag_diagnostics as diag

    monkeypatch.setattr(
        diag, "run_live_probe",
        lambda: {"embedding": {"reachable": False, "error_type": "authentication_failed"}},
    )
    with TestClient(app) as c:
        r = c.post("/api/admin/rag/probe", headers=headers)
    assert r.status_code == 200
    emb = r.json()["embedding"]
    assert emb["error_type"] == "authentication_failed"
    assert "error" not in emb  # 원문 예외 키 없음(error_type만)


def test_probe_cooldown_blocks_rapid_second_call(monkeypatch):
    headers = _ops(monkeypatch)
    monkeypatch.setattr(admin_mod, "_probe_last_monotonic", 0.0)
    import app.services.rag_diagnostics as diag

    monkeypatch.setattr(diag, "run_live_probe", lambda: {"ok": True})
    with TestClient(app) as c:
        r1 = c.post("/api/admin/rag/probe", headers=headers)
        r2 = c.post("/api/admin/rag/probe", headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 429
    assert r2.json()["detail"]["code"] == "PROBE_COOLDOWN"
