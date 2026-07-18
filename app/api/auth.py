"""인증 API — 이메일/비밀번호 회원가입·로그인, JWT 발급 (RPA-23).

get_current_user 의존성을 다른 라우터가 import해 보호 대상 엔드포인트에 건다.
"""

import os
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
    hash_password,
    hash_token,
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
    # RPA-200에서 추가. 기존 클라이언트는 이 필드를 무시하면 되므로 하위호환이 유지된다.
    refresh_token: str | None = None
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=4096)


class UserOut(BaseModel):
    id: str
    email: str


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _issue_tokens(user_id: uuid.UUID, db: Session) -> TokenResponse:
    """액세스+리프레시 쌍을 발급하고 리프레시는 **해시로만** 저장한다 (RPA-200).

    원문은 응답으로 한 번 나가고 서버엔 남지 않는다 — DB가 유출돼도 세션을 복원할 수 없다.
    """
    from datetime import datetime, timedelta, timezone

    refresh = create_refresh_token(user_id)
    expire_days = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "14"))
    db.add(
        models.RefreshToken(
            user_id=user_id,
            token_hash=hash_token(refresh),
            expires_at=datetime.now(timezone.utc) + timedelta(days=expire_days),
        )
    )
    db.commit()
    return TokenResponse(access_token=create_access_token(user_id), refresh_token=refresh)


def _revoke_all_for_user(user_id: uuid.UUID, db: Session) -> int:
    """그 사용자의 유효한 리프레시 토큰을 전부 폐기하고 건수를 반환한다.

    재사용 탐지 시 호출한다 — 폐기된 토큰이 다시 왔다는 건 원문이 두 곳에 있다는 뜻이고,
    어느 쪽이 공격자인지 서버는 알 수 없다. 그래서 **계열 전체를 끊고** 재로그인을 요구한다.
    """
    from datetime import datetime, timezone

    rows = db.scalars(
        select(models.RefreshToken).where(
            models.RefreshToken.user_id == user_id,
            models.RefreshToken.revoked_at.is_(None),
        )
    ).all()
    now = datetime.now(timezone.utc)
    for row in rows:
        row.revoked_at = now
    db.commit()
    return len(rows)


def admin_seed_emails() -> set[str]:
    """ADMIN_EMAILS(쉼표 구분) — is_admin을 부여할 부트스트랩 시드.

    **운영자 신뢰 경계**: 이 값은 외부 입력이 아니라 운영자가 서버 설정으로 지정하는
    프로비저닝 소스다. 승격은 오직 이 시드로만, 그것도 '서버 기동 시 백필'로만 일어난다
    (공개 /register 같은 외부 요청으로는 절대 승격하지 않는다 — CodeRabbit #179).
    운영자는 여기 넣는 이메일 계정을 본인이 직접 등록해 소유를 보장해야 한다."""
    import os

    return {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}


def backfill_seed_admins() -> int:
    """앱 기동 시 시드 이메일 기존 계정을 is_admin으로 백필(멱등). 승격 건수 반환.

    **is_admin을 부여하는 유일한 경로**다(공개 가입은 부여하지 않는다). ADMIN_EMAILS는
    운영자 설정이므로 외부 입력이 아니다 — 운영자가 신뢰하는 계정만 나열한다는 전제.
    migration 직후 is_admin이 전부 False라 재로그인을 기다리지 않고 여기서 메운다.
    DB 미가동이어도 앱 기동은 계속돼야 하므로 호출부에서 예외를 삼킨다.
    """
    from app.db import SessionLocal

    seed = admin_seed_emails()
    if not seed:
        return 0
    with SessionLocal() as db:
        users = db.scalars(
            select(models.User).where(models.User.email.in_(seed), models.User.is_admin.is_(False))
        ).all()
        for u in users:
            u.is_admin = True
        if users:
            db.commit()
        return len(users)


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
    # 공개 가입은 관리자 권한을 절대 부여하지 않는다 — 시드 이메일을 선점한 공격자가
    # 관리자가 되는 권한 상승을 막기 위함(CodeRabbit #179). 승격은 운영자 설정
    # ADMIN_EMAILS 기반 기동 백필로만 일어난다(backfill_seed_admins).
    return _issue_tokens(user.id, db)


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
    return _issue_tokens(user.id, db)


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """리프레시 토큰으로 새 토큰 쌍을 받는다 — 재로그인 없이 세션을 잇는다 (RPA-200).

    **회전(rotation)**: 쓴 토큰은 즉시 폐기하고 새로 발급한다. 그래서 원문이 한 번만 유효하다.
    **재사용 탐지**: 이미 폐기된 토큰이 오면 원문이 두 곳에 존재한다는 뜻이고 어느 쪽이 공격자인지
    알 수 없으므로, 그 사용자의 토큰을 **전부** 끊고 재로그인을 요구한다(OAuth 2.0 Security BCP).

    실패 사유는 401 하나로 합친다 — 서명 위조·만료·폐기를 구분해 알려주면 공격자에게
    토큰 상태를 알려주는 오라클이 된다.
    """
    from datetime import datetime, timezone

    invalid = _error(401, "INVALID_REFRESH_TOKEN", "다시 로그인해 주세요.")

    # ① 서명·만료·용도(typ=refresh) 검증 — 액세스 토큰은 여기서 걸러진다
    user_id = decode_refresh_token(payload.refresh_token)
    if user_id is None:
        raise invalid
    try:
        user_uuid = uuid.UUID(user_id)
    except (ValueError, TypeError):
        raise invalid

    # ② 서버가 아는 토큰인지 (해시로 조회 — 원문은 저장돼 있지 않다)
    row = db.scalar(
        select(models.RefreshToken).where(
            models.RefreshToken.token_hash == hash_token(payload.refresh_token)
        )
    )
    if row is None:
        raise invalid

    # ③ 재사용 탐지 — 폐기된 토큰이 다시 왔다 = 탈취 신호
    if row.revoked_at is not None:
        _revoke_all_for_user(row.user_id, db)
        raise invalid

    now = datetime.now(timezone.utc)
    expires_at = row.expires_at
    if expires_at.tzinfo is None:  # 드라이버가 naive로 주는 경우 UTC로 간주
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        raise invalid

    # ④ 회전 — 옛 토큰 폐기 후 새 쌍 발급
    row.revoked_at = now
    db.commit()
    return _issue_tokens(user_uuid, db)


@router.post("/logout", status_code=204)
def logout(payload: RefreshRequest, db: Session = Depends(get_db)) -> None:
    """리프레시 토큰을 폐기한다. 서버가 무효를 알아야 로그아웃이 주장이 아니라 사실이 된다.

    이미 없거나 폐기된 토큰이어도 204 — 멱등하게 두고 토큰 존재 여부를 알려주지 않는다.
    액세스 토큰은 짧은 수명(기본 60분)이라 그대로 만료를 기다린다(무상태 JWT의 트레이드오프).
    """
    from datetime import datetime, timezone

    row = db.scalar(
        select(models.RefreshToken).where(
            models.RefreshToken.token_hash == hash_token(payload.refresh_token)
        )
    )
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        db.commit()


@router.get("/me", response_model=UserOut)
def me(current: models.User = Depends(get_current_user)) -> UserOut:
    """현재 로그인한 사용자 정보 (토큰 유효성 확인용으로도 쓰인다)."""
    return UserOut(id=str(current.id), email=current.email)
