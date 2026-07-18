"""인증(JWT) 테스트 (RPA-23) — DB는 인메모리 SQLite로 격리."""

import uuid

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
    models.RefreshToken.__table__.create(engine)  # RPA-200 — 갱신 토큰 폐기 상태 저장
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
    models.RefreshToken.__table__.create(engine)  # register가 발급 시 기록한다 (RPA-200)
    return engine


def test_register_never_grants_admin_even_for_seed_email(monkeypatch):
    """공개 가입은 시드 이메일이어도 is_admin을 부여하지 않는다 — 시드 선점 권한상승 차단
    (CodeRabbit #179). 승격은 운영자 기동 백필로만."""
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
    monkeypatch.setenv("ADMIN_EMAILS", "Boss@Gmail.com")  # 시드에 있어도
    try:
        with TestClient(app) as c:
            c.post("/api/auth/register", json={"email": "boss@gmail.com", "password": "pw12345678"})
        with TestingSession() as s:
            boss = s.scalar(select(models.User).where(models.User.email == "boss@gmail.com"))
            assert boss.is_admin is False  # 가입만으로는 절대 관리자가 아니다
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


# --- 리프레시 토큰 (RPA-200) ---

def _login(client, email="ref@example.com", password="pw12345678"):
    """가입 후 토큰 쌍을 돌려준다."""
    r = client.post("/api/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return r.json()


def test_register_and_login_issue_refresh_token(client):
    """가입·로그인 모두 갱신 토큰을 함께 준다 — 60분마다 재로그인하던 문제의 해소 지점."""
    body = _login(client)
    assert body["refresh_token"]
    r = client.post("/api/auth/login", json={"email": "ref@example.com", "password": "pw12345678"})
    assert r.json()["refresh_token"]
    assert r.json()["access_token"]  # 기존 필드도 그대로 (하위호환)


def test_refresh_returns_new_usable_access_token(client):
    """만료를 기다리지 않고도 갱신이 동작하고, 받은 액세스 토큰이 실제로 통한다."""
    body = _login(client)
    r = client.post("/api/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert r.status_code == 200, r.text
    new = r.json()
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {new['access_token']}"})
    assert me.status_code == 200 and me.json()["email"] == "ref@example.com"


def test_refresh_token_is_rejected_as_access_token(client):
    """🔴 타입 혼동 차단 — 리프레시 토큰으로 보호 API에 접근할 수 없어야 한다.

    같은 시크릿으로 서명되므로 typ 검증이 없으면 그냥 통과한다. 그러면 수명이 긴 토큰이
    액세스 토큰처럼 쓰여 60분 만료 정책이 통째로 무의미해진다.
    """
    body = _login(client)
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['refresh_token']}"})
    assert r.status_code == 401


def test_access_token_is_rejected_as_refresh_token(client):
    """역방향도 성립해야 한다 — 액세스 토큰으로는 갱신할 수 없다."""
    body = _login(client)
    r = client.post("/api/auth/refresh", json={"refresh_token": body["access_token"]})
    assert r.status_code == 401


def test_refresh_rotates_old_token_becomes_invalid(client):
    """회전 — 한 번 쓴 갱신 토큰은 즉시 무효가 된다(원문이 한 번만 유효)."""
    body = _login(client)
    old = body["refresh_token"]
    first = client.post("/api/auth/refresh", json={"refresh_token": old})
    assert first.status_code == 200
    assert first.json()["refresh_token"] != old  # 새 토큰이 나왔다
    again = client.post("/api/auth/refresh", json={"refresh_token": old})
    assert again.status_code == 401  # 옛 토큰 재사용 불가


def test_reuse_of_revoked_token_revokes_whole_family(client, monkeypatch):
    """재사용 탐지 — 폐기된 토큰이 (유예창 밖에서) 다시 오면 탈취로 보고 계열 전체를 끊는다.

    회전만 있고 이 방어가 없으면, 탈취자가 먼저 갱신했을 때 정상 사용자의 새 토큰은
    그대로 살아 있어 공격이 조용히 지속된다.
    """
    monkeypatch.setenv("REFRESH_REUSE_GRACE_SECONDS", "0")  # 경합 유예 없음 = 즉시 탈취 판정
    body = _login(client)
    stolen = body["refresh_token"]
    rotated = client.post("/api/auth/refresh", json={"refresh_token": stolen}).json()["refresh_token"]
    # 공격자가 옛 토큰을 다시 제시 → 계열 전체 폐기
    assert client.post("/api/auth/refresh", json={"refresh_token": stolen}).status_code == 401
    # 회전으로 받은 정상 토큰까지 무효여야 한다
    assert client.post("/api/auth/refresh", json={"refresh_token": rotated}).status_code == 401


def test_immediate_resubmit_within_grace_keeps_family_alive(client):
    """🔴 유예창 안의 재제시는 탈취가 아니라 경합이다 — 계열을 끊지 않는다.

    실 동시성 테스트(tests/integration/test_refresh_token_concurrency.py)에서 드러난 결함:
    동시 요청의 진 쪽들이 '폐기된 토큰 재사용'으로 분류돼 **방금 발급된 정상 토큰까지** 폐기했다.
    클라이언트 더블클릭·네트워크 재시도가 전 기기 로그아웃이 되면 안 된다.
    """
    body = _login(client, email="grace@example.com")
    old = body["refresh_token"]
    rotated = client.post("/api/auth/refresh", json={"refresh_token": old}).json()["refresh_token"]
    # 곧바로 옛 토큰을 다시 제출(재시도 시뮬레이션) — 거절은 하되 계열은 살려둔다
    assert client.post("/api/auth/refresh", json={"refresh_token": old}).status_code == 401
    assert client.post("/api/auth/refresh", json={"refresh_token": rotated}).status_code == 200


def test_logout_revokes_refresh_token(client):
    """로그아웃 후에는 그 토큰으로 갱신할 수 없다 — 서버가 무효를 안다."""
    body = _login(client)
    assert client.post("/api/auth/logout", json={"refresh_token": body["refresh_token"]}).status_code == 204
    assert client.post("/api/auth/refresh", json={"refresh_token": body["refresh_token"]}).status_code == 401
    # 멱등 — 이미 폐기된 토큰이어도 204, 존재 여부를 알려주지 않는다
    assert client.post("/api/auth/logout", json={"refresh_token": body["refresh_token"]}).status_code == 204


def test_refresh_token_stored_only_as_hash(client):
    """DB에 원문이 남지 않는다 — 유출돼도 세션을 복원할 수 없어야 한다."""
    body = _login(client)
    from app.db import get_db
    from app.main import app

    gen = app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        rows = db.scalars(select(models.RefreshToken)).all()
        assert rows, "발급 기록이 있어야 한다"
        for row in rows:
            assert row.token_hash != body["refresh_token"]        # 원문 아님
            assert row.token_hash == security.hash_token(body["refresh_token"]) or row.revoked_at
            assert len(row.token_hash) == 64                       # SHA-256 hex
    finally:
        gen.close()


def test_unknown_or_garbage_refresh_token_rejected(client):
    """서버가 모르는 토큰·쓰레기 문자열은 401 (사유를 구분해 알려주지 않는다)."""
    assert client.post("/api/auth/refresh", json={"refresh_token": "garbage"}).status_code == 401
    # 서명은 유효하지만 DB에 없는 토큰 (다른 배포에서 발급된 것 등)
    orphan = security.create_refresh_token(uuid.uuid4())
    assert client.post("/api/auth/refresh", json={"refresh_token": orphan}).status_code == 401


def test_expired_refresh_token_rejected(client, monkeypatch):
    """만료된 갱신 토큰은 거부 — 무기한 세션이 되지 않는다."""
    monkeypatch.setenv("REFRESH_TOKEN_EXPIRE_DAYS", "0")
    body = _login(client, email="exp@example.com")
    r = client.post("/api/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert r.status_code == 401


def test_concurrent_refresh_yields_only_one_new_pair(client, monkeypatch):
    """🔴 동시 갱신 경합 — 같은 토큰으로 두 요청이 오면 **한 건만** 성공해야 한다 (CodeRabbit #273).

    조회 후 갱신으로 나뉘어 있으면 둘 다 '아직 유효'로 보고 각각 새 쌍을 발급받아
    회전(1회용)이 깨진다. 그러면 재사용 탐지도 무의미해진다 — 둘 다 폐기 전 상태였으므로
    탐지가 걸리지 않는다.

    경쟁을 결정론적으로 재현한다: 라우트가 선점을 부르기 직전에 **다른 요청이 이미 선점한**
    상황을 만든다(선점 함수를 한 번 가로채 먼저 실행).
    """
    from app.api import auth as auth_mod

    body = _login(client, email="race@example.com")
    token = body["refresh_token"]

    real_claim = auth_mod._claim_refresh_token
    state = {"raced": False}

    def racing_claim(token_hash, db):
        if not state["raced"]:
            state["raced"] = True
            real_claim(token_hash, db)  # 경쟁자가 먼저 선점 (동시 요청 시뮬레이션)
        return real_claim(token_hash, db)

    monkeypatch.setattr(auth_mod, "_claim_refresh_token", racing_claim)
    r = client.post("/api/auth/refresh", json={"refresh_token": token})
    assert r.status_code == 401  # 진 쪽은 새 쌍을 못 받는다

    # 경합은 탈취가 아니다 — 계열을 끊지 않아야 한다(더블클릭이 전 기기 로그아웃이 되면 안 됨).
    monkeypatch.undo()
    with TestClient(app):
        pass
    assert client.post("/api/auth/login",
                       json={"email": "race@example.com", "password": "pw12345678"}).status_code == 200


def test_claim_is_one_shot(client):
    """선점 프리미티브 자체 — 같은 토큰을 두 번 선점할 수 없다(DB가 승자를 하나로 만든다)."""
    from app.api import auth as auth_mod
    from app.core.security import hash_token
    from app.db import get_db
    from app.main import app as fastapi_app

    body = _login(client, email="claim@example.com")
    h = hash_token(body["refresh_token"])
    gen = fastapi_app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        assert auth_mod._claim_refresh_token(h, db) is True   # 첫 선점 성공
        assert auth_mod._claim_refresh_token(h, db) is False  # 두 번째는 실패
    finally:
        gen.close()


def test_logout_wins_over_later_refresh(client):
    """로그아웃이 원자적이라, 그 뒤 갱신은 새 토큰을 못 받는다."""
    body = _login(client, email="lo@example.com")
    assert client.post("/api/auth/logout", json={"refresh_token": body["refresh_token"]}).status_code == 204
    assert client.post("/api/auth/refresh", json={"refresh_token": body["refresh_token"]}).status_code == 401
