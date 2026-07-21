"""마이그레이션 advisory lock (RPA-223) — 실 Postgres 계약.

락 직렬화는 mock으로 증명되지 않는다(가드가 읽는 것과 동작이 읽는 것이 같아야 한다는
원칙의 테스트판). 유닛 스위트에 두면 CI가 깨진다 — CI postgres 서비스에는 `a360`이 없다
(tests/test_alerts.py 상단, 같은 함정의 선례). 그래서 여기(integration): conftest가
`a360_test`를 만들고 마이그레이션까지 적용한 상태에서 실제 pg_advisory_lock으로 검증한다.
"""

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

import app.db as app_db

TEST_DB_NAME = os.getenv("TEST_DATABASE_NAME", "a360_test")


@pytest.fixture(autouse=True)
def _target_test_db(integration_engine, monkeypatch):
    """run_migrations·락 헬퍼가 테스트 DB(a360_test)를 보게 한다.

    run_migrations는 호출 시점에 조각 env를 읽는다(app/db.py docstring의 계약).
    integration_engine 의존이 DB 생성·스키마 적용·skip 판정을 대신해 준다.
    """
    monkeypatch.setenv("DATABASE_NAME", TEST_DB_NAME)


def _autocommit_conn():
    """락 보유·검사용 별도 세션 — NullPool이라 close 시 연결이 끊겨 락도 확실히 풀린다."""
    engine = create_engine(app_db._database_url(), poolclass=NullPool)
    return engine, engine.connect().execution_options(isolation_level="AUTOCOMMIT")


def test_lock_free_after_successful_run():
    """run_migrations 정상 완료 후 락이 풀려 있다.

    안 풀리면 다음 기동(배포·스케일아웃)마다 타임아웃(120초)을 다 기다린 뒤에야 뜬다 —
    "락을 잡는다"만 검증하고 "놓는다"를 빼먹으면 그게 새 배포 장애가 된다.
    """
    app_db.run_migrations()  # a360_test는 conftest가 이미 head까지 올려놔 no-op이다
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
