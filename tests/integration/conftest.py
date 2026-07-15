"""통합 테스트 픽스처 — 실 Postgres에 실제로 쓴다 (RPA-168).

**왜 필요한가**: 나머지 스위트는 `SessionLocal`을 통째로 mock해서 실제 DB 쓰기를 한 번도
하지 않는다. 그래서 UNIQUE 제약·JSONB 왕복·CASCADE처럼 **DB가 실행하는 것**은 전혀 검증되지
않는다. CONVENTIONS §8이 "스텁/Fake로만 통과한 경로는 별도 live smoke가 필요하다"고 요구하는
바로 그 구멍이다.

**안전**: 여기가 쓰는 건 **앱 DB(각자 로컬 docker)** 뿐이다 — 공유 자원이 아니다. 게다가
루트 `tests/conftest.py`의 autouse 픽스처가 관측 DB(Neon)·RAG 공유 인프라(Bonsai)를 이미
격리하므로, 이 테스트들이 팀 공유 DB를 때리는 건 **구조적으로 불가능**하다. 그 격리를
우회하지 말 것 — 2026-07-15에 live uvicorn(격리 없음)이 실제로 공유 Neon을 오염시켰다.

**작업 DB와 분리**: 개발자가 쓰던 `a360`이 아니라 전용 `a360_test`를 만들어 쓴다(TRUNCATE로
매 테스트를 격리하므로, 작업 DB에 걸면 데이터가 날아간다).

**스키마는 Alembic으로** 만든다. `Base.metadata.create_all`을 쓰면 프로덕션(마이그레이션)과
테스트(메타데이터)가 **두 개의 스키마 정의**로 갈라져 조용히 발산한다 — §8이 겨냥하는
정합성 부채 그 자체다. 마이그레이션이 곧 검증 대상이 된다.

DB가 없으면 전부 skip한다 — docker를 안 띄운 팀원도 기존 스위트는 그대로 통과한다.
"""

import os
from urllib.parse import quote

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

TEST_DB_NAME = os.getenv("TEST_DATABASE_NAME", "a360_test")

# TRUNCATE 대상 — FK가 얽혀 있어 CASCADE로 한 번에 비운다. users는 세션 소유자라 함께.
_DOMAIN_TABLES = (
    "recommendations", "analyses", "documents", "chat_messages",
    "session_compacts", "feedback", "analysis_sessions", "users",
    "audit_logs", "request_metrics", "llm_usage",
)


def _url(dbname: str) -> str:
    """app.db._database_url()과 같은 규칙, DB 이름만 갈아끼운다."""
    host = os.getenv("DATABASE_HOST", "localhost")
    port = os.getenv("DATABASE_PORT", "5432")
    user = os.getenv("DATABASE_USERNAME", "a360_admin")
    password = os.getenv("DATABASE_PASSWORD", "")
    return (f"postgresql+psycopg://{quote(user, safe='')}:{quote(password, safe='')}"
            f"@{host}:{port}/{dbname}")


@pytest.fixture(scope="session")
def integration_engine():
    """전용 테스트 DB를 만들고 마이그레이션을 적용한 엔진. 없으면 스위트를 skip한다."""
    # 1) maintenance DB로 붙어 테스트 DB를 만든다 (CREATE DATABASE는 트랜잭션 밖이라 AUTOCOMMIT)
    try:
        admin = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT", pool_pre_ping=True)
        with admin.connect() as c:
            exists = c.execute(
                text("select 1 from pg_database where datname = :n"), {"n": TEST_DB_NAME}
            ).scalar()
            if not exists:
                c.execute(text(f'create database "{TEST_DB_NAME}"'))
        admin.dispose()
    except OperationalError as e:
        pytest.skip(
            f"통합 테스트용 Postgres에 붙지 못했습니다 — `docker compose up -d db` 후 재실행하세요. ({e.__class__.__name__})"
        )

    engine = create_engine(_url(TEST_DB_NAME), pool_pre_ping=True)

    # 2) 앱을 테스트 DB로 향하게 한 뒤 마이그레이션 — run_migrations()가 호출 시점에 env를 읽는다
    import app.db as app_db

    with pytest.MonkeyPatch.context() as mp:  # session 스코프라 function 픽스처를 못 쓴다
        mp.setenv("DATABASE_NAME", TEST_DB_NAME)
        app_db.run_migrations()

    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _point_app_at_test_db(integration_engine, monkeypatch):
    """앱의 엔진·세션 팩토리를 테스트 DB로 돌리고, 매 테스트 전에 테이블을 비운다.

    `_save_recommendation`은 주입된 `db`가 아니라 **함수 안에서 `from app.db import SessionLocal`**
    로 세션을 새로 연다(스트리밍 후에도 안전하려고). 호출 시점에 모듈 속성을 보므로 여기서
    갈아끼우면 그 경로까지 테스트 DB로 간다.
    """
    import app.db as app_db
    from app.main import app

    TestSession = sessionmaker(bind=integration_engine, expire_on_commit=False)
    monkeypatch.setattr(app_db, "engine", integration_engine)
    monkeypatch.setattr(app_db, "SessionLocal", TestSession)

    # 매 테스트를 빈 상태에서 시작한다. 롤백이 아니라 TRUNCATE인 이유: 동시성 테스트가 **진짜
    # 별도 커넥션에서 진짜 커밋**을 해야 UNIQUE 제약이 실제로 걸린다 — 바깥 트랜잭션으로 감싸면
    # 그 커밋 의미론이 통째로 가려진다.
    with integration_engine.begin() as c:
        c.execute(text(f"truncate {', '.join(_DOMAIN_TABLES)} restart identity cascade"))

    # 요청 스코프 세션도 테스트 DB로
    def _get_test_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    from app.db import get_db

    app.dependency_overrides[get_db] = _get_test_db
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def db_session(integration_engine):
    """테스트가 직접 DB를 확인할 때 쓰는 세션."""
    TestSession = sessionmaker(bind=integration_engine, expire_on_commit=False)
    s = TestSession()
    try:
        yield s
    finally:
        s.close()
