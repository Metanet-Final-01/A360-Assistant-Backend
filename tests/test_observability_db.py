"""관측 전용 DB 분리 테스트 (RPA-90).

핵심 계약:
- OBSERVABILITY_DATABASE_URL 미설정 → unavailable (서비스 DB 폴백 금지)
- 설정 → 별도 엔진의 세션 팩토리 (앱 DB와 분리)
- 관측 DB 장애·미설정이 쓰기 호출(record_usage/_record_audit)을 실패시키지 않음
"""

from types import SimpleNamespace

import pytest

import app.core.observability_db as obs


def _reset_singleton():
    obs._engine = None
    obs._sessionmaker = None
    obs._url_cached = None


def test_unset_url_never_falls_back_to_app_database(monkeypatch):
    """URL 미설정은 명시적 unavailable이며 앱 SessionLocal을 절대 참조하지 않는다."""
    monkeypatch.delenv("OBSERVABILITY_DATABASE_URL", raising=False)
    _reset_singleton()
    with pytest.raises(obs.ObservabilityUnavailableError):
        obs.observability_sessionmaker()


def test_admin_dependency_returns_503_when_unconfigured(monkeypatch):
    """관리자 관측 조회는 빈 결과나 서비스 DB 데이터 대신 표준 503을 반환한다."""
    from app.core.errors import AppError

    monkeypatch.setenv("OBSERVABILITY_DATABASE_URL", "")
    _reset_singleton()
    dependency = obs.get_obs_db()
    with pytest.raises(AppError) as exc:
        next(dependency)
    assert exc.value.status_code == 503
    assert exc.value.code == "OBSERVABILITY_UNAVAILABLE"


def test_admin_dependency_returns_503_when_connection_fails(monkeypatch):
    """URL은 있어도 DB가 내려간 경우 raw 500이나 드라이버 오류를 노출하지 않는다."""
    from app.core.errors import AppError

    closed = []

    class _Session:
        def connection(self):
            raise RuntimeError("driver detail must stay private")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(obs, "observability_sessionmaker", lambda: lambda: _Session())
    dependency = obs.get_obs_db()
    with pytest.raises(AppError) as exc:
        next(dependency)
    assert exc.value.status_code == 503
    assert exc.value.code == "OBSERVABILITY_UNAVAILABLE"
    assert closed == [True]


def test_separate_engine_when_url_set(monkeypatch):
    """URL 설정이면 앱 SessionLocal이 아닌 관측 전용 세션 팩토리를 만든다."""
    monkeypatch.setenv("OBSERVABILITY_DATABASE_URL", "sqlite:///:memory:")
    _reset_singleton()
    try:
        sm = obs.observability_sessionmaker()
        import app.db as app_db

        assert sm is not app_db.SessionLocal
        assert str(sm.kw["bind"].url) == "sqlite:///:memory:"
        # 같은 URL이면 엔진 재사용 (싱글톤)
        assert obs.observability_sessionmaker() is sm
    finally:
        _reset_singleton()


def test_engine_rebuilt_on_url_change(monkeypatch):
    """URL이 바뀌면 엔진을 재생성한다 (테스트·환경 전환 대응)."""
    monkeypatch.setenv("OBSERVABILITY_DATABASE_URL", "sqlite:///:memory:")
    _reset_singleton()
    try:
        first = obs.observability_sessionmaker()
        monkeypatch.setenv("OBSERVABILITY_DATABASE_URL", "sqlite://")
        second = obs.observability_sessionmaker()
        assert first is not second
    finally:
        _reset_singleton()


def test_ensure_schema_noop_when_unset(monkeypatch):
    """URL 미설정이면 스키마 보장은 no-op(False) — 앱 DB는 Alembic이 관리한다."""
    monkeypatch.delenv("OBSERVABILITY_DATABASE_URL", raising=False)
    _reset_singleton()
    assert obs.ensure_observability_schema() is False


def test_ensure_schema_survives_bad_url(monkeypatch):
    """관측 DB에 연결 못 해도 예외를 올리지 않는다 — 앱 기동을 막으면 안 됨."""
    monkeypatch.setenv(
        "OBSERVABILITY_DATABASE_URL",
        "postgresql+psycopg://x:x@127.0.0.1:1/none",  # 닫힌 포트
    )
    _reset_singleton()
    try:
        assert obs.ensure_observability_schema() is False
    finally:
        _reset_singleton()


def test_ensure_schema_uses_short_best_effort_lock_timeout(monkeypatch):
    """관측 DB 락 경합이 앱 기동을 앱 마이그레이션 제한(120초)만큼 막지 않는다."""
    from app.db import OBS_SCHEMA_LOCK_KEY

    monkeypatch.setenv("OBSERVABILITY_DATABASE_URL", "sqlite:///:memory:")
    _reset_singleton()
    lock_calls = []

    class _Lock:
        def __enter__(self):
            return None

        def __exit__(self, *args):
            return False

    def _capture_lock(url, key, *, timeout=None):
        lock_calls.append((url, key, timeout))
        return _Lock()

    metadata = SimpleNamespace(create_all=lambda bind: None)
    monkeypatch.setattr("app.db.pg_advisory_lock", _capture_lock)
    monkeypatch.setattr(obs, "_observability_metadata", lambda: metadata)
    monkeypatch.setattr(obs, "_apply_observability_indexes", lambda *args: None)
    try:
        assert obs.ensure_observability_schema() is True
        assert lock_calls == [
            (
                "sqlite:///:memory:",
                OBS_SCHEMA_LOCK_KEY,
                "5s",
            )
        ]
    finally:
        _reset_singleton()


def test_obs_metadata_strips_foreign_keys():
    """관측 테이블 사본은 FK 제약이 없어야 한다 — 관측 DB엔 부모 테이블이 없다."""
    meta = obs._observability_metadata()
    assert set(meta.tables) == {
        "audit_logs", "llm_usage", "request_metrics", "metrics_daily", "usage_daily",
        "turn_events", "rag_events", "alert_state",
    }
    for table in meta.tables.values():
        assert not any(c.foreign_keys for c in table.columns)


def test_record_usage_uses_observability_session(monkeypatch):
    """record_usage가 관측 세션 팩토리를 통해 기록하는지 (라우팅 검증)."""
    from app.core.llm import record_usage

    saved = []

    class _S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def add(self, row): saved.append(row)
        def commit(self): pass

    monkeypatch.setattr(obs, "observability_sessionmaker", lambda: _S)
    monkeypatch.setattr("app.models.LlmUsage", lambda **kw: SimpleNamespace(**kw))
    record_usage(purpose="intake", model="m", input_tokens=10, output_tokens=2)
    assert len(saved) == 1 and saved[0].purpose == "intake"


def test_record_usage_is_best_effort_without_observability_url(monkeypatch):
    """관측 미설정은 기록만 유실시키고 LLM 호출자나 서비스 DB에 영향을 주지 않는다."""
    from app.core.llm import record_usage

    monkeypatch.setenv("OBSERVABILITY_DATABASE_URL", "")
    _reset_singleton()
    monkeypatch.setattr(
        "app.db.SessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("service DB fallback used")),
    )
    record_usage(purpose="intake", model="m", input_tokens=10, output_tokens=2)
