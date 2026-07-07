"""인증 보안 프리미티브 — 비밀번호 해싱과 JWT 발급/검증.

설계:
- 비밀번호는 bcrypt로 해싱한다. bcrypt는 72바이트 초과 입력을 잘라내므로,
  입력을 먼저 SHA-256으로 축약(→base64)해 길이를 정규화한다 (긴 비밀번호도
  안전하게 전체가 반영되는 잘 알려진 패턴).
- 평문 비밀번호는 저장·로그·응답 어디에도 남기지 않는다.
- JWT 시크릿은 환경변수(JWT_SECRET)에서 읽고, 프로덕션에서 미설정이면 기동을
  거부한다 (약한 개발용 기본값이 운영에 새어나가지 않게).
"""

import base64
import hashlib
import os
import time
import uuid

import bcrypt
import jwt

_JWT_ALGORITHM = "HS256"
# 사용자 없음일 때 login이 비교에 쓰는 더미 해시 — 존재 여부에 따른 타이밍 차이를 없앤다
_DUMMY_HASH = "$2b$12$Wb7yPq3f8Z6xN0qA1sE2iOe5tU9vW3xY7zB1cD4eF6gH8iJ0kL2m"


def _prepare(password: str) -> bytes:
    """bcrypt 72바이트 한계를 회피하기 위해 SHA-256으로 길이를 정규화한다."""
    return base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prepare(password), password_hash.encode("ascii"))
    except ValueError:
        return False  # 손상된 해시 형식 등


def verify_dummy() -> None:
    """사용자 미존재 시 호출 — 실제 검증과 유사한 연산 시간을 소모해 열거 공격을 막는다."""
    verify_password("__nonexistent__", _DUMMY_HASH)


def _secret() -> str:
    secret = os.getenv("JWT_SECRET", "").strip()
    if secret:
        return secret
    if os.getenv("APP_ENV", "development").lower() == "production":
        raise RuntimeError("JWT_SECRET 환경변수가 프로덕션에서 반드시 필요합니다")
    return "dev-only-insecure-secret-do-not-use-in-prod"  # 로컬 개발 전용


def create_access_token(user_id: uuid.UUID | str) -> str:
    expire_minutes = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + expire_minutes * 60,
    }
    return jwt.encode(payload, _secret(), algorithm=_JWT_ALGORITHM)


def decode_access_token(token: str) -> str | None:
    """유효하면 user_id(sub)를, 만료·위조·형식오류면 None을 반환한다."""
    try:
        payload = jwt.decode(token, _secret(), algorithms=[_JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    return payload.get("sub")
