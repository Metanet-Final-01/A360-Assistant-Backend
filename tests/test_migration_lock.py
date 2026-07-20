"""기동 시 마이그레이션 직렬화 (RPA-223) — advisory lock 계약.

실제 Postgres의 pg_advisory_lock으로 검증한다 — 락 직렬화는 mock으로 증명되지 않는다
(가드가 읽는 것과 동작이 읽는 것이 같아야 한다는 원칙의 테스트판). conftest가
APP_DATABASE_URL을 비워 DATABASE_*(로컬 docker Postgres)로 폴백한 상태를 전제한다.
"""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

import app.db as app_db


def _autocommit_conn():
    """락 보유용 별도 세션 — NullPool이라 close 시 연결이 끊겨 락도 확실히 풀린다."""
    engine = create_engine(app_db._database_url(), poolclass=NullPool)
    return engine, engine.connect().execution_options(isolation_level="AUTOCOMMIT")


def test_lock_free_after_successful_run():
    """run_migrations 정상 완료 후 락이 풀려 있다.

    안 풀리면 다음 기동(배포·스케일아웃)마다 타임아웃(120초)을 다 기다린 뒤에야 뜬다 —
    "락을 잡는다"만 검증하고 "놓는다"를 빼먹으면 그게 새 배포 장애가 된다.
    """
    app_db.run_migrations()
    engine, conn = _autocommit_conn()
    try:
        got = conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": app_db.APP_MIGRATION_LOCK_KEY}
        ).scalar()
        assert got is True, "run_migrations가 advisory lock을 해제하지 않았다"
        conn.execute(
            text("SELECT pg_advisory_unlock(:k)"), {"k": app_db.APP_MIGRATION_LOCK_KEY}
        )
    finally:
        conn.close()
        engine.dispose()


def test_run_waits_then_fails_while_lock_held(monkeypatch):
    """다른 세션이 락을 쥐고 있으면 lock_timeout까지 대기 후 실패한다 — 무한 대기 금지.

    실패는 조용히 사라지지 않는다: lifespan이 migrations_ok=False로 남겨 /health/live가
    503을 주고(RPA-222) ASG가 인스턴스를 교체한다. 타임아웃을 500ms로 줄여 실측한다.
    """
    monkeypatch.setattr(app_db, "_LOCK_TIMEOUT", "500ms")
    engine, conn = _autocommit_conn()
    try:
        got = conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": app_db.APP_MIGRATION_LOCK_KEY}
        ).scalar()
        assert got is True, "테스트 전제 실패: 락을 선점하지 못함"
        with pytest.raises(Exception) as ei:
            app_db.run_migrations()
        assert "lock timeout" in str(ei.value).lower()
    finally:
        conn.execute(
            text("SELECT pg_advisory_unlock(:k)"), {"k": app_db.APP_MIGRATION_LOCK_KEY}
        )
        conn.close()
        engine.dispose()


def test_non_postgres_url_passes_without_lock(tmp_path):
    """advisory lock이 없는 방언(sqlite)은 락 없이 통과 — 로컬·테스트 호환.

    연결 시도 자체가 없어야 한다: 파일이 생기면 어딘가에 접속했다는 뜻이다.
    """
    db_file = tmp_path / "no-such.db"
    with app_db.pg_advisory_lock(f"sqlite:///{db_file}", 1):
        pass
    assert not db_file.exists()
