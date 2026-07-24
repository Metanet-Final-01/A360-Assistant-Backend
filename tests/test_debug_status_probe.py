"""/api/rag/debug/status 와이어링 테스트 (RPA-232/RPA-270).

판정 로직은 app/services/rag_diagnostics 공용 함수로 옮겼고(admin과 공유), 이 debug 경로는
그 함수를 호출하는 얇은 래퍼다. 여기선 래퍼 동작만 본다(공용 함수 자체는 test_rag_diagnostics).
- probe 없음 → service_status() 그대로
- probe=1 → DEBUG_RAG_PROBE_ENABLED 게이트, 통과 시 live_probe 추가
"""

from fastapi.testclient import TestClient

from app.main import app


def test_status_without_probe_returns_service_status(monkeypatch):
    import app.services.rag_diagnostics as diag

    monkeypatch.setattr(diag, "service_status", lambda: {"embedding": {"api_key_configured": True}})
    with TestClient(app) as c:
        body = c.get("/api/rag/debug/status").json()
    assert body == {"embedding": {"api_key_configured": True}}
    assert "live_probe" not in body


def test_probe_requires_gate_env(monkeypatch):
    import app.services.rag_diagnostics as diag

    monkeypatch.setattr(diag, "service_status", lambda: {"ok": True})
    monkeypatch.delenv("DEBUG_RAG_PROBE_ENABLED", raising=False)
    with TestClient(app) as c:
        r = c.get("/api/rag/debug/status", params={"probe": "1"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "PROBE_DISABLED"


def test_probe_adds_live_probe_under_gate(monkeypatch):
    import app.services.rag_diagnostics as diag

    monkeypatch.setattr(diag, "service_status", lambda: {"database": {"reachable": True}})
    monkeypatch.setattr(diag, "run_live_probe", lambda: {"embedding": {"reachable": True, "dim": 8}})
    monkeypatch.setenv("DEBUG_RAG_PROBE_ENABLED", "true")
    with TestClient(app) as c:
        body = c.get("/api/rag/debug/status", params={"probe": "1"}).json()
    assert body["database"] == {"reachable": True}
    assert body["live_probe"] == {"embedding": {"reachable": True, "dim": 8}}
