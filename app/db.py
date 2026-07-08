"""SQLAlchemy 엔진·세션·Base. 앱 전역에서 이 모듈을 통해서만 DB에 접근한다.

로컬에서 5432 포트가 점유된 경우 .env의 DATABASE_PORT=5433을 사용한다 (docker-compose 참고).
"""

import os
from urllib.parse import quote

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

load_dotenv()


def _database_url() -> str:
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


def run_migrations() -> None:
    """Alembic 마이그레이션을 head까지 적용한다 (스키마의 단일 진실 공급원).

    신규 DB는 전체 스키마가 생성되고, 최신 DB는 no-op이다. 앱 기동 시 호출한다.
    """
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
