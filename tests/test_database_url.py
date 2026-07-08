"""DB URL 구성 테스트 (RPA-51) — 특수문자 자격증명이 URL을 깨뜨리지 않아야 한다."""

from sqlalchemy.engine import make_url

from app.db import _database_url


def _url_with(monkeypatch, **env):
    # _database_url()은 호출 시 os.getenv를 읽으므로 reload 없이 env만 바꾸면 된다
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return _database_url()


def test_special_char_password_is_url_encoded(monkeypatch):
    url = _url_with(
        monkeypatch,
        DATABASE_HOST="myhost",
        DATABASE_PORT="5432",
        DATABASE_NAME="db",
        DATABASE_USERNAME="admin",
        DATABASE_PASSWORD="pa%ss@wo:rd/x",  # RDS 자동생성 비밀번호 유형
    )
    u = make_url(url)
    # 인코딩 안 하면 host가 'wo:rd/x@myhost' 등으로 오파싱된다
    assert u.host == "myhost"
    assert u.username == "admin"
    assert u.password == "pa%ss@wo:rd/x"  # 디코드 후 원문 복원
    assert u.database == "db"


def test_plain_password_unchanged(monkeypatch):
    url = _url_with(
        monkeypatch,
        DATABASE_HOST="localhost",
        DATABASE_PORT="5433",
        DATABASE_NAME="a360",
        DATABASE_USERNAME="a360_admin",
        DATABASE_PASSWORD="a360_local_password",
    )
    u = make_url(url)
    assert u.password == "a360_local_password"  # 특수문자 없으면 그대로
    assert u.host == "localhost" and str(u.port) == "5433"
