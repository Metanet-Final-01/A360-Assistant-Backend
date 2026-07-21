"""schema_is_current — 실 Postgres 리비전 대조 (RPA-222, Qodo 반영).

유닛(test_health)은 schema_is_current를 mock하므로 '리비전 == head' 실제 판정은 여기서
검증한다. conftest의 integration_engine이 a360_test에 head까지 적용한 상태를 전제한다.
"""

from sqlalchemy import text

import app.db as app_db


def test_true_after_head_migration(integration_engine, monkeypatch):
    """a360_test가 head까지 적용됐으면 schema_is_current True."""
    monkeypatch.setenv("DATABASE_NAME", "a360_test")  # schema_is_current가 볼 대상
    assert app_db.schema_is_current() is True


def test_false_when_revision_stale(integration_engine, db_session, monkeypatch):
    """리비전이 낡으면 False — 공유 DB 스키마 낡음 시나리오(run_migrations는 스킵해도).

    alembic_version 전체 행을 백업/복구한다 — 멀티 head면 여러 행일 수 있어 scalar()로
    1개만 스냅샷하면 복구 시 나머지를 잃어 이후 테스트 DB를 오염시킨다(Qodo).
    """
    monkeypatch.setenv("DATABASE_NAME", "a360_test")
    orig = [r[0] for r in db_session.execute(text("select version_num from alembic_version")).all()]
    db_session.execute(text("update alembic_version set version_num = '0001'"))
    db_session.commit()
    try:
        assert app_db.schema_is_current() is False
    finally:
        db_session.execute(text("delete from alembic_version"))
        for v in orig:
            db_session.execute(
                text("insert into alembic_version (version_num) values (:v)"), {"v": v}
            )
        db_session.commit()
