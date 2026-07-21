"""갱신 토큰 httpOnly 쿠키 (RPA-216) — 발급·읽기·삭제와 전환기 하위호환.

🔴 **이 테스트가 프로덕션 모양으로 도는지 주의할 것.**
TestClient의 기본 base_url은 `http://testserver`이고, httpx 쿠키 jar는 **Secure 쿠키를
http 요청에 싣지 않는다**. 즉 기본값(SECURE_COOKIES=true) 그대로 http로 재면 쿠키가 조용히
무시되고 바디 폴백만 동작하는데, 그 상태로도 기존 테스트는 전부 초록이다 —
"쿠키를 붙였다"는 초록불이 실은 쿠키를 한 번도 안 태운 결과일 수 있다.
그래서 운영 모양은 `https://testserver`로, 로컬 모양은 http+SECURE_COOKIES=false로 각각 잰다.
"""

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.api.auth import REFRESH_COOKIE_NAME, REFRESH_COOKIE_PATH
from app.db import get_db
from app.main import app

_EMAIL = "cookie@example.com"
_PASSWORD = "pw12345678"


@contextmanager
def _client(monkeypatch, *, secure: str = "true", base_url: str = "https://testserver"):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.User.__table__.create(engine)
    models.RefreshToken.__table__.create(engine)
    TestingSession = sessionmaker(bind=engine)

    def _override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("SECURE_COOKIES", secure)
    # 비밀번호 해싱은 이 테스트의 관심사가 아니고 bcrypt가 회당 수 초를 먹는다 —
    # 쿠키 동작만 보기 위해 우회한다(회전·재사용 탐지 경로는 그대로 실제 코드를 탄다).
    monkeypatch.setattr("app.api.auth.hash_password", lambda p: f"fake:{p}")
    monkeypatch.setattr("app.api.auth.verify_password", lambda p, h: h == f"fake:{p}")
    try:
        with TestClient(app, base_url=base_url) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _register(client):
    r = client.post("/api/auth/register", json={"email": _EMAIL, "password": _PASSWORD})
    assert r.status_code == 201, r.text
    return r


# --- 발급 ---

def test_register_sets_httponly_refresh_cookie(monkeypatch):
    """가입 응답이 갱신 토큰을 httpOnly 쿠키로 내려준다 — 이 작업의 핵심."""
    with _client(monkeypatch) as client:
        r = _register(client)
        raw = r.headers["set-cookie"]
        assert REFRESH_COOKIE_NAME in raw
        assert "HttpOnly" in raw  # 페이지 JS가 읽지 못한다
        assert "Secure" in raw
        assert "samesite=none" in raw.lower()  # cross-site(Vercel↔백엔드) 전달 조건
        assert f"Path={REFRESH_COOKIE_PATH}" in raw
        # 쿠키 값은 바디의 토큰과 같아야 한다 — 다르면 한쪽이 서버가 모르는 값이 된다
        assert client.cookies.get(REFRESH_COOKIE_NAME) == r.json()["refresh_token"]


def test_cookie_max_age_follows_token_expiry(monkeypatch):
    """쿠키 만료가 DB의 expires_at과 **같은 env**에서 나온다.

    따로 읽으면 한쪽만 바뀌었을 때 "서버는 유효하다는데 브라우저엔 쿠키가 없는" 상태가 된다.
    """
    monkeypatch.setenv("REFRESH_TOKEN_EXPIRE_DAYS", "3")
    with _client(monkeypatch) as client:
        raw = _register(client).headers["set-cookie"]
        assert "Max-Age=259200" in raw  # 3일 = 3*24*60*60


def test_local_dev_uses_lax_without_secure(monkeypatch):
    """SECURE_COOKIES=false면 Secure를 빼고 SameSite=Lax로 — 로컬 http에서도 실제로 전달된다.

    ⚠️ Secure 없는 SameSite=None은 브라우저가 **거부**한다. 두 값이 한 토글로 함께
    정해지지 않으면 "secure만 껐는데 쿠키가 아예 안 실리는" 조합이 나온다.
    """
    with _client(monkeypatch, secure="false", base_url="http://testserver") as client:
        raw = _register(client).headers["set-cookie"]
        assert "Secure" not in raw
        assert "samesite=lax" in raw.lower()
        # 값이 실제로 왕복하는지 — 속성만 보고 끝내면 "전달되는가"는 아직 안 잰 것이다
        assert client.cookies.get(REFRESH_COOKIE_NAME)


# --- 읽기 ---

def test_refresh_works_with_cookie_only(monkeypatch):
    """바디 없이 쿠키만으로 갱신된다 — 전환을 마친 프론트의 경로."""
    with _client(monkeypatch) as client:
        _register(client)
        before = client.cookies.get(REFRESH_COOKIE_NAME)

        r = client.post("/api/auth/refresh")  # 바디 없음
        assert r.status_code == 200, r.text

        after = client.cookies.get(REFRESH_COOKIE_NAME)
        assert after and after != before  # 회전된 새 토큰이 쿠키로 다시 내려온다
        me = client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"}
        )
        assert me.status_code == 200 and me.json()["email"] == _EMAIL


def test_refresh_still_works_with_body_only(monkeypatch):
    """🔴 쿠키 없이 바디만으로도 갱신된다 — **백엔드가 프론트보다 먼저 배포**되기 때문이다.

    요청까지 즉시 쿠키 전용으로 바꾸면 프론트가 전환을 마치기 전까지 모든 갱신이 401이 된다
    (= 전원 재로그인). 전환 전 프론트는 credentials:include가 없어 쿠키를 아예 싣지 않으므로,
    쿠키를 비운 상태가 그 시점의 프로덕션 모양이다.
    """
    with _client(monkeypatch) as client:
        token = _register(client).json()["refresh_token"]
        client.cookies.clear()

        r = client.post("/api/auth/refresh", json={"refresh_token": token})
        assert r.status_code == 200, r.text
        assert r.json()["refresh_token"]


def test_cookie_wins_when_both_present(monkeypatch):
    """둘 다 오면 쿠키가 이긴다 — 쿠키가 회전을 반영한 최신 값이고, 바디는 프론트가
    localStorage에 들고 있던 옛 값일 수 있다."""
    with _client(monkeypatch) as client:
        stale = _register(client).json()["refresh_token"]
        client.post("/api/auth/refresh")  # 회전 — 쿠키만 새 값으로 갱신된다

        # 바디엔 이미 폐기된 옛 토큰, 쿠키엔 유효한 새 토큰
        r = client.post("/api/auth/refresh", json={"refresh_token": stale})
        assert r.status_code == 200, r.text


def test_refresh_without_cookie_or_body_is_401(monkeypatch):
    """둘 다 없으면 401 — 사유는 구분하지 않는다(토큰 상태 오라클 방지)."""
    with _client(monkeypatch) as client:
        _register(client)
        client.cookies.clear()
        assert client.post("/api/auth/refresh").status_code == 401


# --- 삭제 ---

def test_logout_clears_cookie_and_kills_session(monkeypatch):
    """로그아웃이 쿠키를 지우고 계열도 끊는다 — 둘 중 하나만 하면 로그아웃이 반쪽이다."""
    with _client(monkeypatch) as client:
        _register(client)
        assert client.cookies.get(REFRESH_COOKIE_NAME)

        r = client.post("/api/auth/logout")
        assert r.status_code == 204
        assert not client.cookies.get(REFRESH_COOKIE_NAME)  # 브라우저에서 사라졌다
        assert client.post("/api/auth/refresh").status_code == 401  # 서버에서도 죽었다


def test_logout_clears_cookie_even_for_unknown_token(monkeypatch):
    """서버가 모르는 토큰이어도 삭제 쿠키를 내려준다 — 남겨 두면 죽은 쿠키가 계속 실린다.

    여기서는 클라이언트 jar 상태가 아니라 **서버가 보낸 Set-Cookie**를 본다. jar에 쿠키를
    직접 심으면 httpx가 서버발 쿠키와 다른 키로 저장해 삭제와 매칭되지 않는데, 그건 실제
    브라우저엔 없는 상황이라 코드가 아니라 테스트 설정을 재게 된다. 서버의 책임은
    "삭제 지시를 보냈는가"까지다.
    """
    with _client(monkeypatch) as client:
        _register(client)
        client.cookies.clear()

        r = client.post("/api/auth/logout", json={"refresh_token": "not-a-real-token"})
        assert r.status_code == 204
        raw = r.headers.get("set-cookie", "")
        assert REFRESH_COOKIE_NAME in raw
        assert "Max-Age=0" in raw  # 즉시 만료 = 삭제
        assert f"Path={REFRESH_COOKIE_PATH}" in raw  # 발급 때와 같은 경로여야 지워진다


def test_logout_without_any_token_is_idempotent(monkeypatch):
    """쿠키도 바디도 없으면 지울 게 없어도 204 — 토큰 존재 여부를 알려주지 않는다."""
    with _client(monkeypatch) as client:
        _register(client)
        client.cookies.clear()
        assert client.post("/api/auth/logout").status_code == 204
