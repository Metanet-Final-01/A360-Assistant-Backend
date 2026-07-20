"""공유 앱 DB 토글(APP_DATABASE_URL)과 테스트 격리 (RPA-186).

이 이슈의 본질은 "env 하나 추가했다"가 아니라 **"공유로 켜도 pytest는 절대 공유 DB를 못 본다"**
이다. 앱 DB는 관측·RAG DB와 달리 `app/db.py`가 import 시점에 engine을 만들어서, 저쪽의
`delenv → 참조 시점 폴백` 패턴이 통하지 않는다.

⚠️ 이 파일은 격리 **메커니즘**을 검증한다. 격리가 실제로 걸렸는지에 대한 런타임 확인은
   conftest의 `_assert_app_db_is_local`(session autouse)이 매 실행마다 한다.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import app.db as db_mod

NEON = "postgresql://u:p@ep-fake-pooler.ap-northeast-2.aws.neon.tech/neondb?sslmode=require"


def _url_with_env(monkeypatch, **env) -> str:
    """주어진 env에서 `_database_url()`이 만들어내는 URL."""
    for k in ("APP_DATABASE_URL", "DATABASE_HOST", "DATABASE_PORT",
              "DATABASE_NAME", "DATABASE_USERNAME", "DATABASE_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return db_mod._database_url()


# --- 토글: 미설정이 기본이고, 기존 동작을 바꾸지 않는다 ---

def test_unset_toggle_keeps_existing_local_behavior(monkeypatch):
    """APP_DATABASE_URL이 없으면 기존 DATABASE_* 조각 env 그대로 — 회귀 없음."""
    url = _url_with_env(monkeypatch, DATABASE_HOST="localhost", DATABASE_PORT="5433",
                        DATABASE_NAME="a360", DATABASE_USERNAME="a360_admin",
                        DATABASE_PASSWORD="pw")
    assert url == "postgresql+psycopg://a360_admin:pw@localhost:5433/a360"


def test_toggle_overrides_the_fragment_env_entirely(monkeypatch):
    """APP_DATABASE_URL이 있으면 DATABASE_* 조각을 **통째로** 대체한다.

    반쯤 섞이면(호스트만 공유, 이름은 로컬 등) 어디에 쓰는지 아무도 모르게 된다.
    """
    url = _url_with_env(monkeypatch, APP_DATABASE_URL=NEON, DATABASE_HOST="localhost",
                        DATABASE_NAME="a360", DATABASE_USERNAME="a360_admin")
    assert "neon.tech" in url
    assert "localhost" not in url and "a360_admin" not in url


def test_libpq_url_is_normalized_for_sqlalchemy(monkeypatch):
    """Neon 콘솔이 주는 `postgresql://`를 psycopg 드라이버 형식으로 맞춘다.

    안 하면 SQLAlchemy가 psycopg2를 찾다가 기동에서 죽는다 — 붙여넣기 그대로 동작해야 한다.
    """
    url = _url_with_env(monkeypatch, APP_DATABASE_URL=NEON)
    assert url.startswith("postgresql+psycopg://")
    assert "sslmode=require" in url, "쿼리스트링(sslmode)이 유실되면 Neon 접속이 깨진다"


def test_explicit_driver_url_is_left_alone(monkeypatch):
    """이미 드라이버가 명시된 URL은 건드리지 않는다."""
    explicit = "postgresql+psycopg://u:p@ep-x.neon.tech/db"
    assert _url_with_env(monkeypatch, APP_DATABASE_URL=explicit) == explicit


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_toggle_falls_back_to_local(monkeypatch, blank):
    """빈 문자열·공백은 '미설정'으로 취급한다.

    PowerShell에서 `$env:APP_DATABASE_URL=""`는 변수를 지우지만, .env에 `APP_DATABASE_URL=`로
    남는 경우가 흔하다 — 그때 빈 URL로 engine을 만들려다 죽으면 원인 찾기가 어렵다.
    """
    url = _url_with_env(monkeypatch, APP_DATABASE_URL=blank, DATABASE_HOST="localhost")
    assert "localhost" in url


# --- 자동 마이그레이션 가드: 규약이 아니라 코드로 막는다 ---

def test_startup_migration_skips_shared_db(monkeypatch):
    """공유 DB면 기동 시 자동 마이그레이션을 **건너뛴다** (RPA-186).

    `run_migrations()`는 app/main.py가 **기동 때마다** 부른다. 공유 DB에서 그대로 두면 팀원이
    자기 브랜치로 서버를 띄우는 것만으로 공유 스키마가 그 브랜치 head로 올라간다 — 아무도
    결정하지 않았는데도. head가 둘이 되면 `Multiple head revisions`로 전원 기동 불가다.

    "스키마 작업 중엔 토글을 끈다"는 규약으로는 못 막는다 — 사람이 실행하는 동작이 아니라
    기동 시 자동으로 일어나기 때문이다.
    """
    called = []
    monkeypatch.setattr("alembic.command.upgrade", lambda *a, **k: called.append(a))
    monkeypatch.setenv("APP_DATABASE_URL", NEON)

    db_mod.run_migrations()

    assert called == [], "공유 DB에 자동 마이그레이션이 돌았다 — 팀 전체 스키마가 올라간다"


def test_startup_migration_still_runs_on_local(monkeypatch):
    """로컬 DB에선 기존대로 자동 적용된다 — 가드가 기능을 죽이면 안 된다.

    (이 테스트가 없으면 "공유에 안 돈다"가 **함수 전체가 죽어서** 참일 수도 있다.)
    """
    called = []
    monkeypatch.setattr("alembic.command.upgrade", lambda *a, **k: called.append(a))
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)

    db_mod.run_migrations()

    assert len(called) == 1, "로컬 자동 마이그레이션이 사라졌다"


def test_explicit_script_can_override_the_guard(monkeypatch):
    """`allow_shared=True`(migrate_shared_db.py 전용)는 가드를 넘는다.

    가드가 **탈출구 없는 금지**면 공유 DB를 영영 마이그레이션 못 한다. 자동은 막되 명시적
    실행은 열어둬야 규약이 성립한다.
    """
    called = []
    monkeypatch.setattr("alembic.command.upgrade", lambda *a, **k: called.append(a))
    monkeypatch.setenv("APP_DATABASE_URL", NEON)
    # advisory lock(RPA-223)은 스텁 — NEON은 가짜 URL이라 락이 실제 접속을 시도하면
    # 여기서 죽는다. 이 테스트의 관심사는 가드 탈출구이고, 락 계약(잡음·해제·타임아웃)은
    # tests/test_migration_lock.py가 실제 Postgres로 검증한다.
    from contextlib import nullcontext

    monkeypatch.setattr(db_mod, "pg_advisory_lock", lambda *a, **k: nullcontext())

    db_mod.run_migrations(allow_shared=True)

    assert len(called) == 1, "명시적 경로마저 막혔다 — 공유 DB를 마이그레이션할 방법이 없다"


def test_guard_is_inside_the_function_not_the_caller():
    """가드가 **함수 안**에 있어야 한다 — 호출부에 두면 다른 호출자가 그냥 우회한다.

    app/main.py 말고도 tests/integration/conftest.py가 run_migrations()를 부른다. 앞으로
    호출자가 더 늘어도 각자 가드를 복사하게 두면 언젠가 하나가 빠진다.
    """
    import inspect

    src = inspect.getsource(db_mod.run_migrations)
    assert "APP_DATABASE_URL" in src, (
        "run_migrations 안에 공유 DB 가드가 없다 — 호출부로 옮겼다면 되돌릴 것")


# --- 격리: 공유로 켜도 pytest는 로컬을 본다 ---

def test_conftest_blanked_the_shared_url_before_app_db_import():
    """conftest가 APP_DATABASE_URL을 **빈 문자열로** 무력화했다 (RPA-186).

    이게 격리의 **메커니즘**이다. `pop`이 아니라 빈 문자열이어야 한다 — `app/db.py`가
    import 시점에 `load_dotenv()`를 부르고, 그게 .env를 읽어 **pop한 키를 되살리기** 때문이다.
    load_dotenv(override=False, 기본)는 이미 있는 키를 안 건드리므로 빈 문자열은 살아남는다.

    ⚠️ 키가 아예 없으면(=pop) .env에 값이 있을 때 되살아난다 — 그래서 "없음"이 아니라
       "있지만 빈값"을 요구한다. 이 구분이 실제로 623개 테스트를 갈랐다.
    """
    assert "APP_DATABASE_URL" in os.environ, (
        "APP_DATABASE_URL 키 자체가 없다 — load_dotenv()가 .env에서 되살릴 수 있는 상태다. "
        "pop 대신 빈 문자열을 넣어야 한다")
    assert os.environ["APP_DATABASE_URL"] == "", (
        "conftest가 APP_DATABASE_URL을 비우지 못했다 — 테스트가 공유 DB에 쓸 수 있다")


def test_load_dotenv_cannot_resurrect_the_blanked_url():
    """`load_dotenv()`를 다시 불러도 빈 문자열이 되살아나지 않는다 — 격리의 핵심 계약.

    이 테스트가 없으면 "빈 문자열이 낫다"가 그냥 내 주장이다. 실제로 dotenv가 덮어쓰지
    않는지 확인한다(라이브러리 동작에 기대는 방어라, 버전이 올라 override 기본값이 바뀌면
    조용히 뚫린다 — 그때 여기서 잡힌다).
    """
    from dotenv import load_dotenv

    load_dotenv()  # app/db.py가 import 시점에 하는 것과 같은 호출

    assert os.environ.get("APP_DATABASE_URL") == "", (
        "load_dotenv()가 빈 APP_DATABASE_URL을 .env 값으로 덮어썼다 — 격리가 뚫렸다. "
        "python-dotenv의 override 기본값이 바뀌었는지 확인할 것")


def test_engine_points_at_local_even_though_env_had_shared():
    """.env에 공유 URL이 있었더라도 engine은 로컬을 본다 (RPA-186 핵심 계약).

    conftest가 pop한 원본은 `_SHARED_APP_DB_URL`에 보관돼 있다 — 그게 None이 아니면
    "설정돼 있었는데 격리됐다"는 뜻이고, 이 테스트가 그 격리를 확인한다.
    """
    from tests.conftest import _LOCAL_DB_HOSTS

    host = (db_mod.engine.url.host or "").lower()
    assert host in _LOCAL_DB_HOSTS, f"engine이 원격({host})을 본다 — 격리 실패"


def test_toggle_really_connects_shared_outside_pytest():
    """토글이 **실제 프로세스에선 정말 공유 DB를 본다** — 격리 증명의 대조군.

    위 두 테스트만 있으면 "engine이 늘 로컬"이 **토글이 고장나서** 참일 수도 있다(그러면 격리를
    증명한 게 아니라 기능이 죽은 것). pytest 밖의 깨끗한 프로세스에서 토글이 살아 있음을 보여야
    "pytest 안에서 로컬 = 격리가 일한 것"이 성립한다.

    ⚠️ 서브프로세스로 도는 이유: 이 검증은 `app.db`를 **새로 import**해야 하는데, 같은
       프로세스에서 importlib.reload를 쓰면 `Base`가 재정의돼 SQLAlchemy 모델 레지스트리가
       깨지고 **다른 테스트 4개가 무너진다**(실측으로 잡음 — 전역을 건드리는 테스트는 격리할 것).
    """
    env = {**os.environ, "APP_DATABASE_URL": NEON}
    proc = subprocess.run(
        [sys.executable, "-c", "import app.db; print(app.db.engine.url.host)"],
        capture_output=True, text=True, env=env, cwd=str(Path(__file__).resolve().parent.parent),
    )

    assert proc.returncode == 0, f"서브프로세스 실패: {proc.stderr[-500:]}"
    assert "neon.tech" in proc.stdout, (
        f"토글을 켠 새 프로세스가 공유 DB를 보지 않는다(host={proc.stdout.strip()}) — "
        f"토글이 죽었다면 pytest의 '로컬' 결과는 격리의 증거가 아니다")
