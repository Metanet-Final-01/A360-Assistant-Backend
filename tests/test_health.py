"""/health 의존성 체크 테스트 (RPA-117) — 백오피스 생존 감시 probe의 계약.

앱 DB 실패=503(DOWN), 관측 DB만 실패=200 degraded("반쯤 죽은" 상태 구분).
"""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import app


def _factory(ok=True):
    class _S:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, stmt):
            if not ok:
                raise RuntimeError("db down")
            return SimpleNamespace()

    return _S


def _patch(monkeypatch, app_ok=True, obs_ok=True):
    monkeypatch.setattr("app.db.SessionLocal", _factory(app_ok))
    monkeypatch.setattr(
        "app.core.observability_db.observability_sessionmaker", lambda: _factory(obs_ok)
    )


def test_health_all_ok(monkeypatch):
    _patch(monkeypatch)
    with TestClient(app) as c:
        r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert body["checks"] == {"database": "ok", "observability_database": "ok"}


def test_health_app_db_down_503(monkeypatch):
    """앱 DB가 죽으면 서비스 불능 — probe가 DOWN으로 판단해야 하므로 503."""
    _patch(monkeypatch, app_ok=False)
    with TestClient(app) as c:
        r = c.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "unhealthy"
    assert r.json()["checks"]["database"] == "fail"


def test_health_obs_factory_creation_failure_degraded_not_500(monkeypatch):
    """관측 DB 엔진/팩토리 생성 자체가 터져도 500이 아니라 degraded (CodeRabbit #177)."""

    def _boom():
        raise RuntimeError("engine init failed")

    monkeypatch.setattr("app.db.SessionLocal", _factory(True))
    monkeypatch.setattr("app.core.observability_db.observability_sessionmaker", _boom)
    with TestClient(app) as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"


def test_health_obs_db_down_degraded_but_200(monkeypatch):
    """관측 DB만 죽으면 본 기능은 산다 — UP이되 degraded로 구분."""
    _patch(monkeypatch, obs_ok=False)
    with TestClient(app) as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"
    assert r.json()["checks"]["observability_database"] == "fail"


def test_health_reports_shared_flag(monkeypatch):
    """OBSERVABILITY_DATABASE_URL 설정 여부가 응답에 드러난다 (로컬 폴백 구분)."""
    _patch(monkeypatch)
    monkeypatch.setenv("OBSERVABILITY_DATABASE_URL", "postgresql://shared/obs")
    with TestClient(app) as c:
        assert c.get("/health").json()["observability_shared"] is True
    monkeypatch.delenv("OBSERVABILITY_DATABASE_URL")
    with TestClient(app) as c:
        assert c.get("/health").json()["observability_shared"] is False
