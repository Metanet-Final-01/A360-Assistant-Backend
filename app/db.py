"""SQLAlchemy 엔진·세션·Base. 앱 전역에서 이 모듈을 통해서만 DB에 접근한다.

로컬에서 5432 포트가 점유된 경우 .env의 DATABASE_PORT=5433을 사용한다 (docker-compose 참고).
"""

import os

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
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"


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

    ini_path = Path(__file__).resolve().parent.parent / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", _database_url())
    command.upgrade(cfg, "head")
