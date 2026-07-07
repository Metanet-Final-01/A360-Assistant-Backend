"""인증 API — 이메일/비밀번호 회원가입·로그인, JWT 발급 (RPA-23).

get_current_user 의존성을 다른 라우터가 import해 보호 대상 엔드포인트에 건다.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_dummy,
    verify_password,
)
from app.db import get_db

router = APIRouter(prefix="/api/auth", tags=["auth"])

_bearer = HTTPBearer(auto_error=False)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128, description="8자 이상")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: str
    email: str


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> models.User:
    """Authorization: Bearer <JWT>를 검증해 현재 사용자를 반환한다. 보호 라우트의 의존성."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _error(401, "NOT_AUTHENTICATED", "인증 토큰이 필요합니다.")
    user_id = decode_access_token(credentials.credentials)
    try:
        user_key = uuid.UUID(user_id) if user_id else None
    except (ValueError, TypeError):
        user_key = None
    user = db.get(models.User, user_key) if user_key else None
    if user is None:
        raise _error(401, "INVALID_TOKEN", "토큰이 유효하지 않거나 만료되었습니다.")
    return user


def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> models.User | None:
    """토큰이 있으면 사용자를, 없거나 유효하지 않으면 None을 반환한다 (예외 없음).

    아직 로그인 강제하지 않는 라우트(챗·비전 등)에서 사용량 귀속용 user_id를
    얻기 위한 의존성. 프론트 연동 후 필수 인증으로 전환할 수 있다.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        return None
    user_id = decode_access_token(credentials.credentials)
    try:
        user_key = uuid.UUID(user_id) if user_id else None
    except (ValueError, TypeError):
        return None
    return db.get(models.User, user_key) if user_key else None


def assert_session_owner(session: "models.AnalysisSession", user: models.User | None) -> None:
    """세션에 소유자가 있으면 요청자와 일치하는지 검사한다.

    익명 세션(user_id NULL)은 누구나 접근 허용(하위호환) — 소유자가 지정된 세션만
    타인 접근을 403으로 막는다. 세션 UUID만 알면 남의 문서를 분석·조회하던 허점을 차단.
    """
    owner_id = getattr(session, "user_id", None)
    if owner_id is not None and (user is None or user.id != owner_id):
        raise HTTPException(
            403, detail={"code": "FORBIDDEN", "message": "이 세션에 접근할 권한이 없습니다."}
        )


@router.post("/register", response_model=TokenResponse, status_code=201)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """회원가입 후 바로 로그인 상태가 되도록 토큰을 발급한다."""
    email = _normalize_email(payload.email)
    exists = db.scalar(select(models.User).where(models.User.email == email))
    if exists is not None:
        raise _error(409, "EMAIL_TAKEN", "이미 가입된 이메일입니다.")

    user = models.User(email=email, password_hash=hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return TokenResponse(access_token=create_access_token(user.id))


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """이메일/비밀번호로 로그인. 실패 사유는 열거 공격 방지를 위해 구분하지 않는다."""
    email = _normalize_email(payload.email)
    user = db.scalar(select(models.User).where(models.User.email == email))
    if user is None:
        verify_dummy()  # 사용자 미존재 시에도 검증과 유사한 시간을 소모 (타이밍 차이 제거)
        raise _error(401, "INVALID_CREDENTIALS", "이메일 또는 비밀번호가 올바르지 않습니다.")
    if not verify_password(payload.password, user.password_hash):
        raise _error(401, "INVALID_CREDENTIALS", "이메일 또는 비밀번호가 올바르지 않습니다.")
    return TokenResponse(access_token=create_access_token(user.id))


@router.get("/me", response_model=UserOut)
def me(current: models.User = Depends(get_current_user)) -> UserOut:
    """현재 로그인한 사용자 정보 (토큰 유효성 확인용으로도 쓰인다)."""
    return UserOut(id=str(current.id), email=current.email)
