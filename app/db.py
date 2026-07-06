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


def init_db() -> None:
    """모델 테이블 생성 (존재하면 무시). 앱 기동 시 호출된다.

    스키마 변경이 잦아지면 Alembic 마이그레이션으로 전환한다.
    """
    from app import models  # noqa: F401  # Base.metadata에 모델 등록

    Base.metadata.create_all(engine)
