"""마이그레이션 advisory lock (RPA-223) — 유닛에서 볼 수 있는 부분만.

⚠️ 실 DB가 필요한 계약(락 잡음·해제·타임아웃)은 **여기 두면 안 된다** —
유닛 스위트는 DB 없이 돌아야 하고, CI의 postgres 서비스에는 `a360`이 없다
(tests/test_alerts.py 상단: 같은 함정으로 CI를 깨뜨린 선례). 그 계약은
tests/integration/test_migration_lock_pg.py 가 실 Postgres(`a360_test`)로 검증한다
(파일명이 다른 이유: 같은 basename이 두 디렉터리에 있으면 __init__.py 없는 pytest
레이아웃에선 import file mismatch로 수집이 깨진다).
여기는 접속이 필요 없는 분기만 남긴다.
"""

import logging

import pytest
import sqlalchemy

import app.db as app_db


class _ScalarResult:
    def __init__(self, value=True):
        self.value = value

    def scalar(self):
        return self.value


class _FakeConnection:
    def __init__(self, *, fail_unlock=False):
        self.calls = []
        self.fail_unlock = fail_unlock

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execution_options(self, **_kwargs):
        return self

    def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params))
        if self.fail_unlock and "pg_advisory_unlock" in sql:
            raise RuntimeError("unlock failed")
        return _ScalarResult()


class _FakeEngine:
    def __init__(self, connection):
        self.connection = connection
        self.disposed = False

    def connect(self):
        return self.connection

    def dispose(self):
        self.disposed = True


def test_non_postgres_url_passes_without_lock(tmp_path):
    """advisory lock이 없는 방언(sqlite)은 락 없이 통과 — 로컬·테스트 호환.

    연결 시도 자체가 없어야 한다: 파일이 생기면 어딘가에 접속했다는 뜻이다.
    """
    db_file = tmp_path / "no-such.db"
    with app_db.pg_advisory_lock(f"sqlite:///{db_file}", 1):
        pass
    assert not db_file.exists()


def test_postgres_lock_timeout_uses_bound_set_config(monkeypatch):
    connection = _FakeConnection()
    engine = _FakeEngine(connection)
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *_args, **_kwargs: engine)

    with app_db.pg_advisory_lock("postgresql+psycopg://db/test", 7, timeout="500ms"):
        pass

    timeout_sql, timeout_params = connection.calls[0]
    assert timeout_sql == "SELECT set_config('lock_timeout', :timeout, false)"
    assert timeout_params == {"timeout": "500ms"}
    assert "500ms" not in timeout_sql
    assert engine.disposed is True


def test_unlock_error_does_not_mask_original_error(monkeypatch, caplog):
    connection = _FakeConnection(fail_unlock=True)
    engine = _FakeEngine(connection)
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *_args, **_kwargs: engine)

    with caplog.at_level(logging.ERROR), pytest.raises(ValueError, match="migration failed"):
        with app_db.pg_advisory_lock("postgresql+psycopg://db/test", 7):
            raise ValueError("migration failed")

    assert "advisory lock 명시적 해제 실패" in caplog.text
    assert engine.disposed is True
