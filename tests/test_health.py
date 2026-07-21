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


def _patch(monkeypatch, app_ok=True, obs_ok=True, os_ok=True):
    monkeypatch.setattr("app.db.SessionLocal", _factory(app_ok))
    monkeypatch.setattr(
        "app.core.observability_db.observability_sessionmaker", lambda: _factory(obs_ok)
    )
    # OpenSearch 도달성 체크는 실제 네트워크(RPA-156)라 테스트에선 스텁 — conftest가 host를
    # localhost로 격리하므로 스텁 안 하면 항상 fail로 잡힌다.
    monkeypatch.setattr("app.main._check_opensearch", lambda: os_ok)


def test_health_all_ok(monkeypatch):
    _patch(monkeypatch)
    with TestClient(app) as c:
        r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert body["checks"] == {
        "database": "ok",
        "observability_database": "ok",
        "opensearch": "ok",
    }


def test_health_opensearch_down_degraded_but_200(monkeypatch):
    """OpenSearch(BM25)만 죽으면 dense 검색은 산다 — UP이되 degraded로 구분 (RPA-156)."""
    _patch(monkeypatch, os_ok=False)
    with TestClient(app) as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"
    assert r.json()["checks"]["opensearch"] == "fail"


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


def test_health_live_ok_when_schema_current(monkeypatch):
    """/health/live — 부팅 성공 + 스키마 최신이면 200 (ALB 타겟그룹 계약, RPA-222).

    유닛은 DB 없이 돈다 — run_migrations·schema_is_current를 스텁한다. schema_is_current의
    실제 리비전 대조는 tests/integration이 실 Postgres로 검증한다(test_alerts와 같은 원칙).
    """
    monkeypatch.setattr("app.db.run_migrations", lambda: None)
    monkeypatch.setattr("app.db.schema_is_current", lambda: True)
    with TestClient(app) as c:
        r = c.get("/health/live")
    assert r.status_code == 200
    assert r.json()["status"] == "alive"


def test_health_live_migration_failure_503(monkeypatch):
    """마이그레이션 실패(예외) 인스턴스는 503 — 타겟그룹 진입 차단 (RPA-222)."""

    def _boom():
        raise RuntimeError("migration failed")

    monkeypatch.setattr("app.db.run_migrations", _boom)
    with TestClient(app) as c:
        r = c.get("/health/live")
    assert r.status_code == 503
    assert r.json()["status"] == "boot_failed"


def test_health_live_stale_schema_503(monkeypatch):
    """run_migrations가 예외 없이 리턴해도 스키마가 낡으면 503 (RPA-222 Qodo 반영).

    공유 DB(APP_DATABASE_URL) 구성에서 run_migrations는 마이그레이션을 적용하지 않고
    early-return한다 — 그것만으로 200을 주면 스키마 낡은 인스턴스가 타겟그룹에 들어간다.
    migrations_ok는 '리턴했다'(대리 지표)가 아니라 schema_is_current(실제 상태)로 판정한다.
    """
    monkeypatch.setattr("app.db.run_migrations", lambda: None)  # 성공/스킵
    monkeypatch.setattr("app.db.schema_is_current", lambda: False)  # 스키마 낡음
    with TestClient(app) as c:
        r = c.get("/health/live")
    assert r.status_code == 503
    assert r.json()["status"] == "boot_failed"


def test_health_paths_skip_observability(monkeypatch):
    """헬스 경로는 JSONL·request_metrics에 안 쌓인다 (RPA-222).

    ALB(30초×AZ2)+docker(30초) 프로브가 인스턴스당 하루 8,640회라, 스킵이 빠지면
    관측이 자기 프로브 기록으로 도배된다. 대조군(/api/message)으로 스킵이 과하게
    넓지 않음도 같이 고정한다.
    """
    _patch(monkeypatch)
    events, metrics = [], []
    monkeypatch.setattr(
        "app.rag.observability.log_event", lambda *a, **k: events.append(a)
    )
    monkeypatch.setattr(
        "app.core.http_logging._record_metric", lambda *a, **k: metrics.append(a)
    )
    with TestClient(app) as c:
        c.get("/health")
        c.get("/api/health")
        c.get("/health/live")
        assert events == [] and metrics == []
        c.get("/api/message")  # 대조군 — 일반 경로는 여전히 기록돼야 한다
    assert len(events) == 1 and len(metrics) == 1


def test_health_reports_shared_flag(monkeypatch):
    """OBSERVABILITY_DATABASE_URL 설정 여부가 응답에 드러난다 (로컬 폴백 구분)."""
    _patch(monkeypatch)
    monkeypatch.setenv("OBSERVABILITY_DATABASE_URL", "postgresql://shared/obs")
    with TestClient(app) as c:
        assert c.get("/health").json()["observability_shared"] is True
    monkeypatch.delenv("OBSERVABILITY_DATABASE_URL")
    with TestClient(app) as c:
        assert c.get("/health").json()["observability_shared"] is False
