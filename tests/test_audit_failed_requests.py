"""감사 로그가 실패한 변경 요청도 기록하는지 (RPA-82).

_record_audit(req_id, user_id, method, path, status_code, latency_ms)를 가로채
호출 여부·인자를 확인한다 (DB 없이 미들웨어 로직만 검증).
"""

import app.core.http_logging as hl
import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


def test_records_failed_mutation(monkeypatch):
    """존재하지 않는 경로로의 POST(404)도 감사에 남는다 — 성공만이 아니라 시도 전체."""
    calls = []
    monkeypatch.setattr(hl, "_record_audit", lambda *a: calls.append(a))
    with TestClient(app) as c:
        r = c.post("/api/this-route-does-not-exist", json={})
    assert r.status_code == 404
    assert calls, "실패한 변경 요청이 감사에 기록되지 않음"
    _rid, _uid, method, path, status_code, _lat = calls[0]
    assert method == "POST" and status_code == 404


def test_skips_get_requests(monkeypatch):
    """조회(GET)는 여전히 감사 DB에 안 남는다 (중요 이벤트만).

    ⚠️ /api/health를 쓰면 안 된다 — RPA-222부터 헬스 경로는 미들웨어 초입에서 통째로
    스킵되므로 "GET이라서 제외"가 아니라 "경로라서 제외"로 공허하게 통과한다.
    스킵 목록에 없는 GET 경로여야 이 테스트가 검증하려는 분기를 실제로 태운다.
    """
    calls = []
    monkeypatch.setattr(hl, "_record_audit", lambda *a: calls.append(a))
    monkeypatch.setattr("app.rag.observability.log_event", lambda *a, **k: None)
    monkeypatch.setattr(hl, "_record_metric", lambda *a, **k: None)
    with TestClient(app) as c:
        c.get("/api/message")
    assert calls == []


def test_records_successful_mutation(monkeypatch):
    """성공(2xx) 변경 요청도 계속 기록된다 (회귀 방지). get_db는 페이크로 대체."""
    class _FakeDB:
        def add(self, x): pass
        def commit(self): pass

    app.dependency_overrides[get_db] = lambda: _FakeDB()
    calls = []
    monkeypatch.setattr(hl, "_record_audit", lambda *a: calls.append(a))
    with TestClient(app) as c:
        r = c.post("/api/sessions")  # 세션 생성 201
    assert r.status_code == 201
    assert calls and calls[0][2] == "POST" and calls[0][4] == 201
