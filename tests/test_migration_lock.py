"""마이그레이션 advisory lock (RPA-223) — 유닛에서 볼 수 있는 부분만.

⚠️ 실 DB가 필요한 계약(락 잡음·해제·타임아웃)은 **여기 두면 안 된다** —
유닛 스위트는 DB 없이 돌아야 하고, CI의 postgres 서비스에는 `a360`이 없다
(tests/test_alerts.py 상단: 같은 함정으로 CI를 깨뜨린 선례). 그 계약은
tests/integration/test_migration_lock_pg.py 가 실 Postgres(`a360_test`)로 검증한다
(파일명이 다른 이유: 같은 basename이 두 디렉터리에 있으면 __init__.py 없는 pytest
레이아웃에선 import file mismatch로 수집이 깨진다).
여기는 접속이 필요 없는 분기만 남긴다.
"""

import app.db as app_db


def test_non_postgres_url_passes_without_lock(tmp_path):
    """advisory lock이 없는 방언(sqlite)은 락 없이 통과 — 로컬·테스트 호환.

    연결 시도 자체가 없어야 한다: 파일이 생기면 어딘가에 접속했다는 뜻이다.
    """
    db_file = tmp_path / "no-such.db"
    with app_db.pg_advisory_lock(f"sqlite:///{db_file}", 1):
        pass
    assert not db_file.exists()
