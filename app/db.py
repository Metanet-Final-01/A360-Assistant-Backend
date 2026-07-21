"""SQLAlchemy 엔진·세션·Base. 앱 전역에서 이 모듈을 통해서만 DB에 접근한다.

로컬에서 5432 포트가 점유된 경우 .env의 DATABASE_PORT=5433을 사용한다 (docker-compose 참고).
"""

import logging
import os
from urllib.parse import quote

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

load_dotenv()

logger = logging.getLogger(__name__)


def _normalize_sqlalchemy_url(url: str) -> str:
    """libpq 형식(postgresql://)을 SQLAlchemy psycopg 드라이버 형식으로 맞춘다.

    Neon 콘솔이 주는 문자열은 `postgresql://`라 그대로 붙이면 SQLAlchemy가 psycopg2를 찾는다.
    이미 드라이버가 명시된 URL(`postgresql+psycopg://`)은 건드리지 않는다.
    """
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _database_url() -> str:
    # 공유 앱 DB 토글 (RPA-186) — 설정 시 아래 DATABASE_* 조각 env를 통째로 대체한다.
    # **미설정이 기본**이라 로컬 단독 개발은 기존과 동일하게 동작한다.
    # ⚠️ 이 URL은 import 시점에 engine에 굳는다(아래 create_engine). 즉 테스트가 fixture로
    #    env를 지워도 이미 늦다 — 격리는 tests/conftest.py **최상단**에서 import 전에 한다.
    shared = os.getenv("APP_DATABASE_URL", "").strip()
    if shared:
        return _normalize_sqlalchemy_url(shared)

    host = os.getenv("DATABASE_HOST", "localhost")
    port = os.getenv("DATABASE_PORT", "5432")
    name = os.getenv("DATABASE_NAME", "a360")
    user = os.getenv("DATABASE_USERNAME", "a360_admin")
    password = os.getenv("DATABASE_PASSWORD", "")
    # 자격증명은 URL 인코딩한다 — RDS 자동생성 비밀번호의 '%'·'@'·':'·'/' 등이
    # URL을 깨뜨리는 것(호스트 오파싱·인증 실패)을 막는다 (RPA-51).
    return f"postgresql+psycopg://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{name}"


class Base(DeclarativeBase):
    pass


engine = create_engine(_database_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db():
    """FastAPI 의존성: 요청 단위 세션."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_migrations(*, allow_shared: bool = False) -> None:
    """Alembic 마이그레이션을 head까지 적용한다 (스키마의 단일 진실 공급원).

    신규 DB는 전체 스키마가 생성되고, 최신 DB는 no-op이다. 앱 기동 시 호출한다.

    ⚠️ **공유 앱 DB(APP_DATABASE_URL)에는 자동으로 적용하지 않는다** (RPA-186).

    이건 함수 안에 둔다 — 호출부(main.py lifespan)에 두면 다른 호출자가 그냥 우회한다.

    왜: 이 함수는 **앱 기동 때마다** 돈다(app/main.py). 공유 DB에서 그대로 두면 팀원이 자기
    브랜치로 서버를 띄우는 것만으로 공유 스키마가 그 브랜치 head로 올라간다 — 아무도 "지금
    올리겠다"고 결정하지 않았는데도. 그러면:
      - 두 사람이 각자 리비전을 만들면 head가 둘 → `Multiple head revisions`로 전원 기동 불가
      - NOT NULL 컬럼이 추가되면 그 컬럼을 모르는 팀원 코드의 INSERT가 깨진다
      - 리뷰에서 까인 마이그레이션도 이미 공유 DB에 적용돼 되돌리기 어렵다

    "스키마 작업 중엔 토글을 끈다"는 **규약만으로는 못 막는다** — 사람이 결정해서 실행하는
    동작이 아니라 기동 시 자동으로 일어나기 때문이다. 그래서 코드로 강제한다.

    공유 DB에 적용하는 건 `dev` 머지 후 **한 명이 명시적으로**:
        python scripts/migrate_shared_db.py
    (그 스크립트만 allow_shared=True로 부른다.)

    ⚠️ 아래 가드와 마이그레이션 **대상**(_database_url())은 **둘 다 호출 시점** env를 읽는다 —
       의도한 것이고, 그래서 서로 항상 일치한다. 모듈 전역 `engine`(import 시점 고정)과는
       갈릴 수 있지만 이 함수는 engine을 쓰지 않는다. 이 성질에 통합 테스트가 의존한다:
       `mp.setenv("DATABASE_NAME", "a360_test")` 후 호출하면 그 DB로 간다(tests/integration).

    ⚠️ "공유"의 판정을 host 기반(원격이면 차단)으로 바꾸지 말 것 — **프로덕션 RDS도 원격**이라
       배포 시 마이그레이션이 막힌다. "원격"과 "팀 공유 개발 DB"는 다르고, 후자를 가리키는 건
       APP_DATABASE_URL뿐이다. (프로덕션은 DATABASE_* 조각을 쓰고 이 값을 설정하지 않는다.)
    """
    shared = os.getenv("APP_DATABASE_URL", "").strip()
    if shared and not allow_shared:
        logger.warning(
            "공유 앱 DB(APP_DATABASE_URL)라 자동 마이그레이션을 건너뜁니다 — 스키마가 코드보다 "
            "낡았다면 기동 후 쿼리에서 터집니다. dev 머지 후 한 명이 "
            "`python scripts/migrate_shared_db.py`로 적용하세요 (RPA-186)."
        )
        return

    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    # alembic.ini 파일에 의존하지 않고 script_location을 직접 지정한다 — 배포 이미지에
    # ini가 안 복사됐거나 실행 위치가 달라도 "No 'script_location' key found" 없이 동작한다.
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    # URL은 attributes(순수 dict)에 담는다 — set_main_option()은 configparser에 저장되는데,
    # 기본 보간(%-interpolation)이 '%'를 특수문자로 취급해 비밀번호에 '%'가 섞이면 죽는다.
    cfg.attributes["sqlalchemy_url"] = _database_url()
    command.upgrade(cfg, "head")


def schema_is_current() -> bool:
    """DB의 현재 alembic 리비전이 코드 head와 일치하나 (RPA-222 헬스 판정).

    `/health/live`의 `migrations_ok`를 '`run_migrations`가 예외 없이 리턴했다'(대리 지표)가
    아니라 **스키마의 실제 상태**로 판정하기 위함이다 (Qodo 리뷰). `run_migrations`는 공유
    DB(APP_DATABASE_URL)에서 마이그레이션을 적용하지 않고 early-return하므로, 그것만으로
    True로 두면 스키마가 낡은 인스턴스도 200을 줘 타겟그룹에 들어간다. 여기서 실제 리비전이
    코드 head와 같은지 봐서, 낡았으면 False → /health/live 503으로 진입을 막는다.

    run_migrations와 **같은 URL**(호출 시점 _database_url)로 본다 — 모듈 전역 engine
    (import 시점 고정)은 공유 DB 토글·테스트 env 전환과 갈릴 수 있다.
    """
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, inspect, text

    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    heads = set(ScriptDirectory.from_config(cfg).get_heads())

    eng = create_engine(_database_url())
    try:
        if not inspect(eng).has_table("alembic_version"):
            return False  # 스키마 자체가 없다 — 준비 안 됨
        with eng.connect() as c:
            current = c.execute(
                text("select version_num from alembic_version")
            ).scalar_one_or_none()
    finally:
        eng.dispose()
    return current is not None and current in heads
