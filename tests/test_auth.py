"""인증(JWT) 테스트 (RPA-23) — DB는 인메모리 SQLite로 격리."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.core import security
from app.db import Base, get_db
from app.main import app


@pytest.fixture()
def client(monkeypatch):
    # 테스트용 인메모리 DB (users 테이블만 있으면 충분)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.User.__table__.create(engine)
    TestingSession = sessionmaker(bind=engine)

    def _override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_register_returns_token_and_hashes_password(client):
    r = client.post("/api/auth/register", json={"email": "A@Ex.com", "password": "pw12345678"})
    assert r.status_code == 201
    assert r.json()["access_token"]
    # /me로 토큰이 실제로 동작하는지
    token = r.json()["access_token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "a@ex.com"  # 정규화(소문자)


def test_register_duplicate_email_conflict(client):
    client.post("/api/auth/register", json={"email": "dup@ex.com", "password": "pw12345678"})
    r = client.post("/api/auth/register", json={"email": "DUP@ex.com", "password": "other12345"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "EMAIL_TAKEN"


def test_register_rejects_short_password(client):
    r = client.post("/api/auth/register", json={"email": "x@ex.com", "password": "short"})
    assert r.status_code == 422  # Pydantic min_length


def test_register_rejects_invalid_email(client):
    r = client.post("/api/auth/register", json={"email": "not-an-email", "password": "pw12345678"})
    assert r.status_code == 422


def test_login_success(client):
    client.post("/api/auth/register", json={"email": "u@ex.com", "password": "pw12345678"})
    r = client.post("/api/auth/login", json={"email": "u@ex.com", "password": "pw12345678"})
    assert r.status_code == 200 and r.json()["access_token"]


def test_login_wrong_password_generic_401(client):
    client.post("/api/auth/register", json={"email": "u2@ex.com", "password": "pw12345678"})
    r = client.post("/api/auth/login", json={"email": "u2@ex.com", "password": "wrongpass123"})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "INVALID_CREDENTIALS"


def test_login_unknown_email_same_error(client):
    # 미가입 이메일도 동일한 코드/메시지 (사용자 열거 방지)
    r = client.post("/api/auth/login", json={"email": "ghost@ex.com", "password": "pw12345678"})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "INVALID_CREDENTIALS"


def test_me_requires_token(client):
    assert client.get("/api/auth/me").status_code == 401


def test_me_rejects_garbage_token(client):
    r = client.get("/api/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


# --- security 모듈 단위 테스트 ---

def test_password_hash_roundtrip_and_secrecy():
    h = security.hash_password("s3cret-pw")
    assert h != "s3cret-pw" and h.startswith("$2b$")  # 평문 아님, bcrypt
    assert security.verify_password("s3cret-pw", h)
    assert not security.verify_password("wrong", h)


def test_long_password_not_truncated_at_72_bytes():
    # bcrypt 72바이트 한계 우회 확인: 72바이트 이후만 다른 두 비밀번호가 구분돼야 한다
    base = "a" * 72
    h = security.hash_password(base + "TAIL_ONE")
    assert not security.verify_password(base + "TAIL_TWO", h)
    assert security.verify_password(base + "TAIL_ONE", h)


def test_jwt_production_requires_secret(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        security.create_access_token("some-id")


def test_expired_token_rejected(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_MINUTES", "-1")  # 이미 만료된 토큰
    token = security.create_access_token("uid-1")
    assert security.decode_access_token(token) is None


# --- 관리자 부트스트랩 (RPA-118) ---

def _mem_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.User.__table__.create(engine)
    return engine


def test_register_seed_email_promoted_to_admin(monkeypatch):
    """ADMIN_EMAILS 시드 이메일로 가입하면 is_admin=True, 아니면 False (승격은 시드에만)."""
    engine = _mem_engine()
    TestingSession = sessionmaker(bind=engine)

    def _get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("ADMIN_EMAILS", "Boss@Gmail.com")  # 대소문자 무시 확인
    try:
        with TestClient(app) as c:
            c.post("/api/auth/register", json={"email": "boss@gmail.com", "password": "pw12345678"})
            c.post("/api/auth/register", json={"email": "peon@gmail.com", "password": "pw12345678"})
        with TestingSession() as s:
            boss = s.scalar(select(models.User).where(models.User.email == "boss@gmail.com"))
            peon = s.scalar(select(models.User).where(models.User.email == "peon@gmail.com"))
            assert boss.is_admin is True
            assert peon.is_admin is False
    finally:
        app.dependency_overrides.clear()


def test_backfill_seed_admins_promotes_existing_idempotent(monkeypatch):
    """기동 백필 — 시드 이메일 기존 계정을 승격, 재실행 시 0(멱등)."""
    from app.api import auth as auth_mod

    engine = _mem_engine()
    TestingSession = sessionmaker(bind=engine)
    with TestingSession() as s:
        s.add(models.User(email="boss@gmail.com", password_hash="x", is_admin=False))
        s.add(models.User(email="peon@gmail.com", password_hash="x", is_admin=False))
        s.commit()
    monkeypatch.setattr("app.db.SessionLocal", TestingSession)
    monkeypatch.setenv("ADMIN_EMAILS", "boss@gmail.com")

    assert auth_mod.backfill_seed_admins() == 1  # boss만 승격
    with TestingSession() as s:
        assert s.scalar(select(models.User).where(models.User.email == "boss@gmail.com")).is_admin is True
        assert s.scalar(select(models.User).where(models.User.email == "peon@gmail.com")).is_admin is False
    assert auth_mod.backfill_seed_admins() == 0  # 멱등 — 다시 승격할 것 없음


def test_backfill_no_seed_is_noop(monkeypatch):
    """ADMIN_EMAILS 미설정이면 아무도 승격하지 않는다 (DB 접근도 안 함)."""
    from app.api import auth as auth_mod

    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    # SessionLocal을 건드리면 실패하도록 — 시드 없으면 호출 전에 반환해야 함
    monkeypatch.setattr("app.db.SessionLocal", lambda: (_ for _ in ()).throw(AssertionError("DB 접근 금지")))
    assert auth_mod.backfill_seed_admins() == 0
