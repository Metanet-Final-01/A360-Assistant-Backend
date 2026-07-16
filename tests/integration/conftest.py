"""통합 테스트 픽스처 — 실 Postgres에 실제로 쓴다 (RPA-168).

**왜 필요한가**: 나머지 스위트는 `SessionLocal`을 통째로 mock해서 실제 DB 쓰기를 한 번도
하지 않는다. 그래서 UNIQUE 제약·JSONB 왕복·CASCADE처럼 **DB가 실행하는 것**은 전혀 검증되지
않는다. CONVENTIONS §9가 "스텁/Fake로만 통과한 경로는 별도 live smoke가 필요하다"고 요구하는
바로 그 구멍이다.

**안전**: 루트 `tests/conftest.py`가 관측 DB(Neon)·RAG 공유 인프라(Bonsai)를 autouse 픽스처로,
공유 앱 DB(`APP_DATABASE_URL`)를 **모듈 최상단에서 빈 문자열로 덮어써** 격리한다(`pop`이 아니다 —
`app/db.py`의 `load_dotenv()`가 pop한 키를 .env에서 되살린다). 그 격리를 우회하지 말 것 —
2026-07-15에 live uvicorn(격리 없음)이 실제로 공유 Neon을 오염시켰다.
여기서 만드는 `integration_engine`은 루트 가드(`app.db.engine`만 봄) 밖이라 `_assert_local_target()`이
따로 지킨다 — 매 테스트 TRUNCATE가 걸린 대상이다.

⚠️ 한때 여기 *"앱 DB는 각자 로컬 docker라 공유 자원이 아니다"*라고 적혀 있었다. **더는 사실이
아니다** — RPA-186이 `APP_DATABASE_URL` 토글을 추가해 앱 DB도 공유일 수 있다. 지금 이 파일이
안전한 이유는 "앱 DB가 로컬이라서"가 아니라 **루트 conftest가 그 env를 pop해서**다. 결론은
같지만 이유가 다르다 — 아래 `_url()`이 조각 env로만 URL을 만드는 것도 그 격리에 기대고 있다.
여기는 매 테스트 **TRUNCATE**를 하므로, 이 격리가 깨지면 팀 데이터가 통째로 날아간다.

**작업 DB와 분리**: 개발자가 쓰던 `a360`이 아니라 전용 `a360_test`를 만들어 쓴다(TRUNCATE로
매 테스트를 격리하므로, 작업 DB에 걸면 데이터가 날아간다).

**스키마는 Alembic으로** 만든다. `Base.metadata.create_all`을 쓰면 프로덕션(마이그레이션)과
테스트(메타데이터)가 **두 개의 스키마 정의**로 갈라져 조용히 발산한다 — §8이 겨냥하는
정합성 부채 그 자체다. 마이그레이션이 곧 검증 대상이 된다.

DB가 없으면 전부 skip한다 — docker를 안 띄운 팀원도 기존 스위트는 그대로 통과한다.
"""

import os
import re
from urllib.parse import quote

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

TEST_DB_NAME = os.getenv("TEST_DATABASE_NAME", "a360_test")

# 이름을 강제한다 — 아래 픽스처가 이 DB의 테이블을 **TRUNCATE**하므로, 오타 하나로 작업 DB가
# 통째로 날아갈 수 있다(#234 리뷰). `_test` 접미사를 요구하면 `a360`·`postgres` 같은 실 DB는
# 형태상 걸러진다(별도 금지목록은 그래서 불필요). 동시에 SQL 식별자로 보간해도 안전한 형태만
# 남는다.
if not re.fullmatch(r"[a-z][a-z0-9_]*_test", TEST_DB_NAME):
    raise RuntimeError(
        f"TEST_DATABASE_NAME은 전용 테스트 DB여야 합니다(소문자·`_test`로 끝남): {TEST_DB_NAME!r}. "
        "이 DB의 테이블은 매 테스트마다 TRUNCATE됩니다 — 작업 DB를 가리키면 데이터가 날아갑니다."
    )

# TRUNCATE 대상 — FK가 얽혀 있어 CASCADE로 한 번에 비운다. users는 세션 소유자라 함께.
_DOMAIN_TABLES = (
    "recommendations", "analyses", "documents", "chat_messages",
    "session_compacts", "feedback", "analysis_sessions", "users",
    "audit_logs", "request_metrics", "llm_usage",
)


def _url(dbname: str) -> str:
    """조각 env(DATABASE_*)로 URL을 만든다. DB 이름만 갈아끼운다.

    ⚠️ `app.db._database_url()`과 **같지 않다** — 저쪽엔 `APP_DATABASE_URL`(공유 앱 DB) 분기가
    있고 여기엔 없다(RPA-186). 일부러 그렇게 뒀다: 여기는 `a360_test`를 만들고 **TRUNCATE**
    하므로 공유 DB를 가리키면 안 된다. 조각 env만 보면 공유 URL이 어떻게 설정돼 있든 여기로는
    새지 않는다(루트 conftest의 pop과 이중 방어).

    ⚠️ 그러므로 `mp.setenv("DATABASE_NAME", ...)`로 대상을 바꾸는 아래 관용구는 **`app.db`
    쪽엔 안 통한다** — `APP_DATABASE_URL`이 있으면 그게 조각 env를 통째로 이기기 때문이다.
    루트 conftest가 pop하니 pytest 안에선 문제없지만, 이 관용구를 다른 곳에 복사하지 말 것.
    """
    host = os.getenv("DATABASE_HOST", "localhost")
    port = os.getenv("DATABASE_PORT", "5432")
    user = os.getenv("DATABASE_USERNAME", "a360_admin")
    password = os.getenv("DATABASE_PASSWORD", "")
    return (f"postgresql+psycopg://{quote(user, safe='')}:{quote(password, safe='')}"
            f"@{host}:{port}/{dbname}")


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


def _assert_local_target() -> None:
    """대상이 **로컬**인지 확인한다 — 아니면 즉시 실패 (CodeRabbit #258).

    `_url()`은 `APP_DATABASE_URL`만 안 볼 뿐 `DATABASE_HOST`는 그대로 쓴다. 루트 conftest의
    가드는 `app.db.engine`만 보고 여기서 따로 만드는 엔진은 안 본다 — 즉 `DATABASE_HOST`가
    원격을 가리키면 **원격에 DB를 만들고 TRUNCATE한다**. `_test` 접미사 강제는 DB '이름'만
    막을 뿐 '호스트'는 못 막는다.

    env가 아니라 **실제로 쓸 URL의 host**를 본다 — "env가 로컬처럼 생겼다"와 "붙을 곳이
    로컬이다"는 다른 명제이고, 물어야 할 건 후자다.
    """
    host = (make_url(_url(TEST_DB_NAME)).host or "").lower()
    if host not in _LOCAL_HOSTS:
        raise RuntimeError(
            f"통합 테스트 대상이 원격입니다 (DATABASE_HOST={host}). 이 스위트는 매 테스트마다 "
            f"'{TEST_DB_NAME}'의 테이블을 TRUNCATE합니다 — 원격/공유 DB면 데이터가 날아갑니다. "
            f"DATABASE_HOST를 로컬로 두세요."
        )


@pytest.fixture(scope="session")
def integration_engine():
    """전용 테스트 DB를 만들고 마이그레이션을 적용한 엔진. 없으면 스위트를 skip한다."""
    _assert_local_target()  # TRUNCATE 대상이 로컬인지 먼저 — skip보다 앞이다(원격이면 조용히 넘어가면 안 됨)

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
    except OperationalError:
        # CI에서 skip은 **가짜 초록**이다 — postgres 서비스가 깨지거나 자격증명이 틀어지면 통합
        # 테스트가 조용히 사라지는데 CI는 green으로 통과한다. 그러면 이 PR의 취지("mock만 통과하는
        # 상태를 끝낸다")가 소리 없이 무효가 된다. 그래서 CI에선 그대로 터뜨린다 (#234 리뷰).
        # 로컬에선 docker를 안 띄운 팀원을 막지 않도록 skip을 유지한다.
        if os.getenv("CI"):
            raise
        pytest.skip("통합 테스트용 Postgres에 붙지 못했습니다 — `docker compose up -d db` 후 재실행하세요.")

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
        """get_db 대체 — 요청 스코프 세션도 테스트 DB를 보게 한다."""
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
