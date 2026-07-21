"""인증 API — 이메일/비밀번호 회원가입·로그인, JWT 발급 (RPA-23).

get_current_user 의존성을 다른 라우터가 import해 보호 대상 엔드포인트에 건다.
"""

import os
import uuid

from fastapi import APIRouter, Body, Cookie, Depends, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, text, update
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


# 쿠키명·경로는 발급과 삭제가 **반드시 같아야** 한다 — 속성이 하나라도 다르면 브라우저가
# 별개 쿠키로 취급해 삭제되지 않는다. 그래서 상수로 묶어 두 곳이 같은 값을 읽게 한다.
REFRESH_COOKIE_NAME = "a360_refresh_token"
# 인증 엔드포인트에만 실리도록 범위를 좁힌다 — /api/rag 등 무관한 요청에 갱신 토큰을 보내지 않는다.
REFRESH_COOKIE_PATH = "/api/auth"


def _cookie_security() -> tuple[bool, str]:
    """(secure, samesite) — 둘은 **한 토글로 함께** 정해야 한다 (RPA-216).

    `SameSite=None`은 `Secure`를 강제하고, 브라우저는 `Secure` 없는 `SameSite=None`을
    **거부**한다. 두 값을 따로 두면 "로컬만 secure를 껐는데 쿠키가 아예 안 실리는" 조합이 나온다.

    기본값이 secure인 이유: 운영은 프론트(Vercel)와 백엔드가 다른 도메인이라 cross-site이고,
    설정을 빠뜨렸을 때 **로컬이 불편한 쪽**이 **운영 보안이 조용히 꺼지는 쪽**보다 낫다.
    로컬(http, 둘 다 localhost)은 same-site라 `lax`로도 정상 전달된다.
    """
    secure = os.getenv("SECURE_COOKIES", "true").strip().lower() not in ("false", "0", "no", "off")
    return (True, "none") if secure else (False, "lax")


def _set_refresh_cookie(response: Response, token: str, max_age_seconds: int) -> None:
    secure, samesite = _cookie_security()
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        token,
        max_age=max_age_seconds,
        httponly=True,  # 페이지 JS(XSS 포함)가 값을 읽지 못하게 — 이 작업의 핵심
        secure=secure,
        samesite=samesite,
        path=REFRESH_COOKIE_PATH,
    )


def _clear_refresh_cookie(response: Response) -> None:
    """발급 때와 **동일한** 속성으로 지운다 — 다르면 브라우저가 다른 쿠키로 보고 남겨 둔다."""
    secure, samesite = _cookie_security()
    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=secure,
        samesite=samesite,
    )


def _issue_tokens(
    user_id: uuid.UUID,
    db: Session,
    *,
    response: Response,
    family_id: uuid.UUID | None = None,
) -> TokenResponse:
    """액세스+리프레시 쌍을 발급하고 리프레시는 **해시로만** 저장한다 (RPA-200).

    원문은 응답으로 한 번 나가고 서버엔 남지 않는다 — DB가 유출돼도 세션을 복원할 수 없다.
    family_id를 주면 그 계열을 잇고(회전), 없으면 새 계열을 연다(신규 로그인).

    리프레시 토큰은 httpOnly 쿠키로 내려간다 (RPA-216). `response`를 **필수 인자**로 둔 이유는
    발급과 쿠키 설정이 갈라지지 않게 하기 위함이다 — 호출부에서 따로 부르게 두면 언젠가
    빠뜨리고, 그때 증상은 "가끔 로그인이 안 풀린다"처럼 재현이 어렵게 나타난다.

    ⚠️ 쿠키 만료는 DB의 `expires_at`과 **같은 `expire_days`**에서 파생시킨다. 따로 읽으면
    한쪽만 바뀌었을 때 서버는 유효하다는데 브라우저엔 쿠키가 없는 상태가 된다.

    응답 바디의 `refresh_token`은 과도기 하위호환으로 **함께** 내려간다 — 프론트가 쿠키 기반으로
    전환을 마친 뒤 별도 배포에서 제거한다(RPA-205). 그전까지는 XSS 노출 범위가 줄지 않는다.
    """
    from datetime import datetime, timedelta, timezone

    refresh = create_refresh_token(user_id)
    expire_days = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "14"))
    db.add(
        models.RefreshToken(
            user_id=user_id,
            family_id=family_id or uuid.uuid4(),
            token_hash=hash_token(refresh),
            expires_at=datetime.now(timezone.utc) + timedelta(days=expire_days),
        )
    )
    db.commit()
    _set_refresh_cookie(response, refresh, expire_days * 24 * 60 * 60)
    return TokenResponse(access_token=create_access_token(user_id), refresh_token=refresh)


def _read_refresh_token(cookie_token: str | None, payload: "RefreshRequest | None") -> str | None:
    """쿠키를 먼저 보고, 없으면 요청 바디로 폴백한다 (RPA-216).

    🔴 **바디 폴백을 남긴 이유**: 백엔드가 프론트보다 먼저 배포된다. 요청까지 즉시 쿠키 전용으로
    바꾸면 프론트가 전환을 마치기 전까지 **모든 갱신·로그아웃이 401**이 된다(= 전원 재로그인).
    응답만 병행하고 요청을 즉시 끊으면 앞뒤가 맞지 않는다.

    쿠키를 우선하는 이유: 전환기에는 둘 다 올 수 있는데, 쿠키 쪽이 회전을 실제로 반영한 최신
    값이다(바디는 프론트가 localStorage에 들고 있던 옛 값일 수 있다).

    프론트 전환 완료 후 바디 경로를 제거한다(RPA-205 후속).
    """
    if cookie_token:
        return cookie_token
    return payload.refresh_token if payload is not None else None


def _lock_family(family_id: uuid.UUID, db: Session) -> None:
    """이 **계열**에 대한 refresh·logout을 직렬화한다 (자문 잠금, transaction 종료 시 자동 해제).

    ⚠️ **행 잠금(`FOR UPDATE`)으로는 부족하다** — 실측으로 확인했다.
    제시된 토큰의 행만 잠그면, logout이 회전된 **옛 토큰**(t1)을, refresh가 현재 토큰(t2)을
    제시할 때 서로 **다른 행**을 잠근다(멀티탭·모바일 캐시·재시도에서 흔하다). logout의 계열
    UPDATE는 t2에서 잠깐 막히지만, 풀린 뒤에도 그 사이 INSERT된 t3는 **자기 스냅샷에 없어
    폐기하지 못한다** — 사용자는 204를 받았는데 세션이 살아남는다.
    (별도 프로브로 검증: 행 잠금에 막혔던 UPDATE는 그동안 삽입된 행을 보지 못한다.)

    계열 식별자 자체를 잠그면 어느 토큰을 제시하든 순서가 강제된다.
    회귀: tests/integration/test_refresh_token_concurrency.py::
          test_logout_with_stale_token_still_kills_session

    SQLite(유닛 테스트)에는 이 함수가 없어 조용히 넘어간다 — 단일 커넥션이라 이 경합이
    성립하지 않는다. 실제 검증은 통합 테스트(Postgres)가 한다.
    """
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return
    # uuid(128비트) → 부호 있는 bigint 하나. 2인자 형식은 int4 두 개라 bigint가 안 들어간다.
    # 서로 다른 계열이 같은 키로 접혀도 결과는 '불필요한 직렬화'일 뿐 정확성은 깨지지 않는다.
    key = (family_id.int >> 64) - (1 << 63)
    db.execute(text("select pg_advisory_xact_lock(cast(:key as bigint))"), {"key": key})


def _get_refresh_token(token_hash: str, db: Session) -> models.RefreshToken | None:
    """해시로 토큰 행을 찾는다 (원문은 저장돼 있지 않다).

    잠금은 여기서 걸지 않는다 — 계열 단위여야 하므로 family_id를 안 뒤 `_lock_family`로 건다.
    """
    return db.scalar(
        select(models.RefreshToken).where(models.RefreshToken.token_hash == token_hash)
    )


def _revoke_family(family_id: uuid.UUID, db: Session) -> int:
    """그 **계열**의 유효한 리프레시 토큰을 전부 폐기하고 건수를 반환한다.

    계열이 단위인 이유(두 방향 모두 실패를 막는다):
    - 제시된 토큰만 끊으면 **경합 중 회전으로 발급된 후손이 살아남는다** — 로그아웃이 204를
      돌려줘도 세션이 유지된다(#273 리뷰에서 지적된 실제 구멍).
    - 사용자의 토큰을 전부 끊으면 무관한 **다른 기기까지 로그아웃**된다.

    로그아웃과 재사용 탐지가 같은 함수를 쓴다 — 둘 다 "이 로그인 계열을 끝낸다"는 같은 의미다.
    """
    from datetime import datetime, timezone

    result = db.execute(
        update(models.RefreshToken)
        .where(
            models.RefreshToken.family_id == family_id,
            models.RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(timezone.utc))
    )
    db.commit()
    return result.rowcount


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
def register(
    payload: RegisterRequest, response: Response, db: Session = Depends(get_db)
) -> TokenResponse:
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
    return _issue_tokens(user.id, db, response=response)


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest, response: Response, db: Session = Depends(get_db)
) -> TokenResponse:
    """이메일/비밀번호로 로그인. 실패 사유는 열거 공격 방지를 위해 구분하지 않는다."""
    email = _normalize_email(payload.email)
    user = db.scalar(select(models.User).where(models.User.email == email))
    if user is None:
        verify_dummy()  # 사용자 미존재 시에도 검증과 유사한 시간을 소모 (타이밍 차이 제거)
        raise _error(401, "INVALID_CREDENTIALS", "이메일 또는 비밀번호가 올바르지 않습니다.")
    if not verify_password(payload.password, user.password_hash):
        raise _error(401, "INVALID_CREDENTIALS", "이메일 또는 비밀번호가 올바르지 않습니다.")
    return _issue_tokens(user.id, db, response=response)


@router.post("/refresh", response_model=TokenResponse)
def refresh(
    response: Response,
    payload: RefreshRequest | None = Body(default=None),
    cookie_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
    db: Session = Depends(get_db),
) -> TokenResponse:
    """리프레시 토큰으로 새 토큰 쌍을 받는다 — 재로그인 없이 세션을 잇는다 (RPA-200).

    **회전(rotation)**: 쓴 토큰은 즉시 폐기하고 새로 발급한다. 그래서 원문이 한 번만 유효하다.
    **재사용 탐지**: 이미 폐기된 토큰이 오면 원문이 두 곳에 존재한다는 뜻이고 어느 쪽이 공격자인지
    알 수 없으므로, 그 사용자의 토큰을 **전부** 끊고 재로그인을 요구한다(OAuth 2.0 Security BCP).

    실패 사유는 401 하나로 합친다 — 서명 위조·만료·폐기를 구분해 알려주면 공격자에게
    토큰 상태를 알려주는 오라클이 된다.
    """
    from datetime import datetime, timezone

    invalid = _error(401, "INVALID_REFRESH_TOKEN", "다시 로그인해 주세요.")

    token = _read_refresh_token(cookie_token, payload)
    if token is None:  # 쿠키도 바디도 없다 — 사유는 구분하지 않는다(오라클 방지)
        raise invalid

    # ① 서명·만료·용도(typ=refresh) 검증 — 액세스 토큰은 여기서 걸러진다
    user_id = decode_refresh_token(token)
    if user_id is None:
        raise invalid
    try:
        user_uuid = uuid.UUID(user_id)
    except (ValueError, TypeError):
        raise invalid

    # ② 서버가 아는 토큰인지 (해시로 조회 — 원문은 저장돼 있지 않다)
    row = _get_refresh_token(hash_token(token), db)
    if row is None:
        raise invalid

    # 계열 잠금 — 여기부터 발급까지가 logout과 직렬화된다. 잠금을 기다리는 동안
    # logout이 이 계열을 끊었을 수 있으므로 낡은 스냅샷을 버리고 다시 읽는다.
    _lock_family(row.family_id, db)
    db.refresh(row)

    # ③ 재사용 탐지 — 폐기된 토큰이 다시 왔다. 다만 **방금** 폐기된 것이면 탈취가 아니라 경합이다.
    #
    # 실 동시성 테스트에서 확인한 것: 같은 토큰으로 동시 요청이 오면 승자가 폐기를 커밋한 뒤
    # 나머지가 조회하므로, 진 요청들이 전부 여기로 들어와 **방금 발급된 정상 토큰까지 폐기**했다
    # (유효 토큰 0개). 클라이언트의 더블클릭·네트워크 재시도가 전 기기 로그아웃이 되는 셈이다.
    # 그래서 짧은 유예창 안의 재제시는 경합으로 보고 401만 돌려준다(계열 유지).
    # 창 밖의 재제시는 원문이 오래 살아 있었다는 뜻이므로 그대로 탈취로 처리한다.
    if row.revoked_at is not None:
        revoked_at = row.revoked_at
        if revoked_at.tzinfo is None:
            revoked_at = revoked_at.replace(tzinfo=timezone.utc)
        grace = int(os.getenv("REFRESH_REUSE_GRACE_SECONDS", "10"))
        if (datetime.now(timezone.utc) - revoked_at).total_seconds() > grace:
            _revoke_family(row.family_id, db)
        raise invalid

    now = datetime.now(timezone.utc)
    expires_at = row.expires_at
    if expires_at.tzinfo is None:  # 드라이버가 naive로 주는 경우 UTC로 간주
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        # 만료는 탈취가 아니다 — 계열을 끊지 않는다(오래 쉰 사용자가 다른 기기까지 잃지 않게).
        raise invalid

    # ④ 회전 — 부모 폐기와 후손 INSERT를 같은 transaction으로 commit한다. 위 계열 잠금이
    # 동시 refresh와 logout을 직렬화하므로 중간에 계열 폐기가 빠져나갈 틈이 없다.
    row.revoked_at = now
    # 계열을 잇고 함께 commit — 회전된 새 토큰이 Set-Cookie로 다시 내려간다
    return _issue_tokens(user_uuid, db, response=response, family_id=row.family_id)


@router.post("/logout", status_code=204)
def logout(
    response: Response,
    payload: RefreshRequest | None = Body(default=None),
    cookie_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
    db: Session = Depends(get_db),
) -> None:
    """리프레시 토큰을 폐기한다. 서버가 무효를 알아야 로그아웃이 주장이 아니라 사실이 된다.

    이미 없거나 폐기된 토큰이어도 204 — 멱등하게 두고 토큰 존재 여부를 알려주지 않는다.
    액세스 토큰은 짧은 수명(기본 60분)이라 그대로 만료를 기다린다(무상태 JWT의 트레이드오프).

    **계열 전체를 끊는다.** 제시된 토큰만 폐기하면, 동시에 진행되던 갱신이 먼저 회전해
    발급한 후손이 살아남는다 — 사용자는 204를 받았는데 그 토큰을 쥔 쪽의 세션은 유지된다
    (#273 리뷰 지적). 로그아웃은 "이 로그인을 끝낸다"는 뜻이므로 계열이 단위여야 한다.
    다른 기기(다른 계열)는 영향받지 않는다.
    """
    # 쿠키는 **무조건** 지운다. 토큰이 이미 폐기됐거나 서버가 모르는 값이어도 마찬가지다 —
    # 브라우저에 남겨 두면 로그아웃했는데 다음 요청에 죽은 쿠키가 계속 실린다.
    _clear_refresh_cookie(response)

    token = _read_refresh_token(cookie_token, payload)
    if token is None:
        return  # 멱등 — 지울 게 없어도 204

    row = _get_refresh_token(hash_token(token), db)
    if row is not None:
        # refresh와 같은 잠금 — 이게 없으면 우리 UPDATE가 지나간 뒤 refresh가
        # 새 행을 INSERT해 살아남는다(옛 토큰으로 로그아웃할 때 실제로 발생).
        _lock_family(row.family_id, db)
        _revoke_family(row.family_id, db)


@router.get("/me", response_model=UserOut)
def me(current: models.User = Depends(get_current_user)) -> UserOut:
    """현재 로그인한 사용자 정보 (토큰 유효성 확인용으로도 쓰인다)."""
    return UserOut(id=str(current.id), email=current.email)
