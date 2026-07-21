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
import time
import uuid

import bcrypt
import jwt

from app.core import config

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
    secret = (config.JWT_SECRET or "").strip()
    if secret:
        return secret
    if config.APP_ENV.lower() == "production":
        raise RuntimeError("JWT_SECRET 환경변수가 프로덕션에서 반드시 필요합니다")
    return "dev-only-insecure-secret-do-not-use-in-prod"  # 로컬 개발 전용


# 토큰 용도 — 같은 시크릿으로 서명되므로 **용도를 클레임으로 구분하고 검증 때 대조**해야 한다.
# 이게 없으면 수명이 긴 리프레시 토큰이 액세스 토큰으로 통과해 60분 만료 정책이 무의미해진다
# (타입 혼동, RPA-200). 옛 토큰(typ 없음)은 access로 간주해 하위호환을 유지한다.
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"


def _create_token(user_id: uuid.UUID | str, *, typ: str, ttl_seconds: int) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "typ": typ,
        "iat": now,
        "exp": now + ttl_seconds,
        # 같은 사용자·같은 초에 발급해도 토큰이 겹치지 않게 — 리프레시는 해시가 PK라 충돌하면 안 된다
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, _secret(), algorithm=_JWT_ALGORITHM)


def create_access_token(user_id: uuid.UUID | str) -> str:
    expire_minutes = config.ACCESS_TOKEN_EXPIRE_MINUTES
    return _create_token(user_id, typ=TOKEN_TYPE_ACCESS, ttl_seconds=expire_minutes * 60)


def create_refresh_token(user_id: uuid.UUID | str) -> str:
    """장수명 갱신 토큰. 원문은 클라이언트만 갖고, 서버는 해시만 저장한다(RPA-200)."""
    expire_days = config.REFRESH_TOKEN_EXPIRE_DAYS
    return _create_token(user_id, typ=TOKEN_TYPE_REFRESH, ttl_seconds=expire_days * 86400)


def _decode(token: str, *, expected_typ: str) -> str | None:
    """서명·만료·**용도**까지 검증해 user_id(sub)를 반환한다. 하나라도 어긋나면 None."""
    try:
        payload = jwt.decode(token, _secret(), algorithms=[_JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    # typ 부재 = RPA-200 이전에 발급된 액세스 토큰 → access로만 인정(리프레시로는 못 쓴다)
    if payload.get("typ", TOKEN_TYPE_ACCESS) != expected_typ:
        return None
    return payload.get("sub")


def decode_access_token(token: str) -> str | None:
    """유효한 **액세스** 토큰이면 user_id(sub)를, 아니면 None. 리프레시 토큰은 거부한다."""
    return _decode(token, expected_typ=TOKEN_TYPE_ACCESS)


def decode_refresh_token(token: str) -> str | None:
    """유효한 **리프레시** 토큰이면 user_id(sub)를, 아니면 None. 액세스 토큰은 거부한다."""
    return _decode(token, expected_typ=TOKEN_TYPE_REFRESH)


def hash_token(token: str) -> str:
    """리프레시 토큰 저장용 해시 — DB가 유출돼도 세션을 복원할 수 없게 원문을 저장하지 않는다.

    비밀번호와 달리 bcrypt를 쓰지 않는다: 토큰은 128비트 랜덤(jti)을 포함한 고엔트로피
    문자열이라 사전 공격 대상이 아니고, 매 갱신마다 조회 키로 써야 해 **결정적 해시**여야 한다.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
