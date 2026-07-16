"""관측 전용 DB 분리 테스트 (RPA-90).

핵심 계약:
- OBSERVABILITY_DATABASE_URL 미설정 → 앱 SessionLocal 폴백 (기존 로컬 개발·테스트 호환)
- 설정 → 별도 엔진의 세션 팩토리 (앱 DB와 분리)
- 관측 DB 장애·미설정이 쓰기 호출(record_usage/_record_audit)을 실패시키지 않음
"""

from types import SimpleNamespace

import app.core.observability_db as obs


def _reset_singleton():
    obs._engine = None
    obs._sessionmaker = None
    obs._url_cached = None


def test_fallback_to_app_sessionlocal_when_unset(monkeypatch):
    """URL 미설정이면 앱 SessionLocal을 호출 시점에 참조한다 — monkeypatch 호환 계약."""
    monkeypatch.delenv("OBSERVABILITY_DATABASE_URL", raising=False)
    _reset_singleton()
    sentinel = object()
    monkeypatch.setattr("app.db.SessionLocal", sentinel)
    assert obs.observability_sessionmaker() is sentinel


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
