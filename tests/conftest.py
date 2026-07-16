"""공용 테스트 픽스처 (RPA-69).

프로덕션 경로는 항상 실제 RAG(HybridRetriever/BackendCatalog)를 쓰지만, CI(tests.yml)는
pgvector·OpenSearch·DB 없이 pytest만 돌린다. 아래 autouse fixture가 app/agent의 검색기·
카탈로그 팩토리를 인메모리 스텁으로 주입해, 인프라 의존 테스트가 실제 서비스를 때리지
않게 한다. 개별 테스트는 필요하면 자기 페이크로 다시 monkeypatch하면 된다.

팩토리(_make_retriever/_make_catalog)만 patch하므로, 사용처의 `from ..retrieval import
get_retriever` 참조는 그대로 두어도 최신 스텁을 받는다(get_retriever/get_catalog가
호출 시점에 모듈 전역 팩토리를 부르기 때문).
"""

import os

# ⚠️ 이 줄은 **아래 import보다 먼저** 실행돼야 한다 (RPA-186). 순서를 바꾸지 말 것.
#
# 앱 DB는 관측·RAG DB와 달리 `app/db.py`가 **import 시점에 engine을 만든다**. 즉 .env에
# APP_DATABASE_URL(공유 Neon)이 있으면 `app.db`가 import되는 순간 공유 DB에 커넥션이 열리고,
# fixture로 env를 지워도 이미 늦다. 아래 `from app.agent...` import가 app.db를 끌고 오므로
# 그 전에 무력화해야 로컬 폴백으로 굳는다.
#
# ⚠️⚠️ **pop이 아니라 빈 문자열이어야 한다.** `app/db.py`는 import 시점에 `load_dotenv()`를
# 부르는데, 그게 .env를 다시 읽어 **pop한 키를 되살린다**. 반면 load_dotenv(override=False,
# 기본값)는 **이미 os.environ에 있는 키는 건드리지 않는다** — 빈 문자열도 "있는" 것이므로
# 살아남고, `_database_url()`의 `.strip()`이 falsy로 보고 로컬 조각 env로 폴백한다.
# (실측 2026-07-15: pop으로 짰다가 .env에 실제 URL이 들어온 순간 623개가 전부 원격을 봤다.
#  셸 env로만 시험해서 통과했던 것 — 그땐 .env에 키가 없어 load_dotenv가 되살릴 게 없었다.)
#
# 왜 중요한가: 테스트 63개 중 45개가 TestClient로 실제 SessionLocal을 쓴다. 격리가 없으면
# 매 pytest 실행이 팀 공유 DB에 세션·문서·추천을 쓴다 — 2026-07-15 관측 DB 오염(47행)의
# 확대판이다. 아래 _assert_app_db_is_local이 이 메커니즘이 깨졌는지 **확인**한다(바라지 않고).
_SHARED_APP_DB_URL = os.environ.get("APP_DATABASE_URL")
os.environ["APP_DATABASE_URL"] = ""

import pytest

from app.agent.v2 import retrieval as retrieval_mod
from app.agent.v2.verify import catalog as catalog_mod
from app.agent.v3 import retrieval as retrieval_mod_v3
from app.agent.v3.verify import catalog as catalog_mod_v3

from tests.agent_stubs import FakeCatalog, FakeRetriever


_LOCAL_DB_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


@pytest.fixture(scope="session", autouse=True)
def _assert_app_db_is_local():
    """테스트가 공유 앱 DB를 절대 못 보게 **확인**한다 (RPA-186) — fail-closed.

    위 `os.environ.pop`이 격리 **메커니즘**이고, 이 가드는 그게 실제로 먹었는지 **검증**한다.
    둘을 나눈 이유: 메커니즘은 import 순서에 의존해서 조용히 깨질 수 있다(누가 conftest 위쪽에
    import를 추가하거나, 플러그인이 app.db를 먼저 끌어오면). 깨지면 팀 DB가 오염되므로
    바라지 말고 확인한다 — `scripts/check_smoke_isolation.py`와 같은 철학이다.

    ⚠️ engine.url.host를 본다. env를 보면 안 된다 — engine은 이미 import 시점에 굳었고,
       "env가 비었다"와 "engine이 로컬을 본다"는 다른 명제다. 실제로 물어야 할 건 후자다.
    """
    import app.db

    host = (app.db.engine.url.host or "").lower()
    assert host in _LOCAL_DB_HOSTS, (
        f"테스트가 원격 앱 DB({host})에 연결돼 있다 — 팀 공유 DB를 오염시킨다.\n"
        f"conftest 최상단의 APP_DATABASE_URL pop이 app.db import보다 늦게 실행됐을 수 있다"
        f"{' (APP_DATABASE_URL이 .env에 설정돼 있음)' if _SHARED_APP_DB_URL else ''}.\n"
        f"import 순서를 확인할 것 — 이 가드가 깨진 채 돌면 매 pytest가 공유 DB에 쓴다."
    )


@pytest.fixture(autouse=True)
def _stub_agent_rag(monkeypatch):
    """모든 테스트에서 agent 검색기·카탈로그를 인메모리 스텁으로 대체한다.

    버전 패키지는 완전 벤더링이라 v2/v3 각자의 팩토리를 모두 patch한다 — 한쪽만 막으면
    다른 버전 경로의 테스트가 실제 인프라를 때린다.
    """
    fake_retriever = FakeRetriever()
    fake_catalog = FakeCatalog()
    monkeypatch.setattr(retrieval_mod, "_make_retriever", lambda: fake_retriever)
    monkeypatch.setattr(catalog_mod, "_make_catalog", lambda: fake_catalog)
    monkeypatch.setattr(retrieval_mod_v3, "_make_retriever", lambda: fake_retriever)
    monkeypatch.setattr(catalog_mod_v3, "_make_catalog", lambda: fake_catalog)


@pytest.fixture(autouse=True)
def _isolate_observability_db(monkeypatch):
    """테스트가 공유 관측 DB(Neon)를 절대 때리지 않게 격리한다 (RPA-90).

    개발자 .env에 OBSERVABILITY_DATABASE_URL이 설정돼 있으면 record_usage/_record_audit이
    실제 팀 공유 DB에 테스트 쓰레기를 쓴다 — env를 지워 앱 SessionLocal 폴백(각 테스트가
    monkeypatch하는 대상)으로 고정하고 모듈 싱글톤도 초기화한다. 관측 DB 라우팅 자체를
    검증하는 테스트(test_observability_db.py)는 자기 setenv로 다시 켠다.
    """
    import app.core.observability_db as obs

    monkeypatch.delenv("OBSERVABILITY_DATABASE_URL", raising=False)
    obs._engine = None
    obs._sessionmaker = None
    obs._url_cached = None
    # 롤업 스케줄러(RPA-104)도 테스트에선 끈다 — TestClient lifespan마다 배치가 돌면
    # 느려지고 로컬 DB에 집계 쓰레기가 쌓인다.
    monkeypatch.setenv("METRICS_ROLLUP_ENABLED", "false")


@pytest.fixture(autouse=True)
def _isolate_rag_shared_infra(monkeypatch):
    """테스트가 공유 RAG 인프라(Neon 코퍼스·Bonsai)를 절대 때리지 않게 격리한다 (RPA-157).

    관측 DB(위 fixture)만 격리돼 있고 RAG store는 안 돼 있어, .env에 RAG_DATABASE_URL·
    OPENSEARCH_HOST(공유 크레덴셜)가 있으면 lifespan의 open_pools()가 min_size=2로 **기동
    시점에 공유 Neon에 실제 커넥션을 연다** — 로컬 pytest가 팀 데모 데이터를 오염시킬 표면.
    관측 DB와 대칭으로 막는다: RAG store 연결을 전부 localhost로 저하시켜, 개별 테스트가
    모킹을 빠뜨려도 shared가 아니라 로컬(있으면 접속, 없으면 fail-open)로 떨어지게 한다.

    - RAG_DATABASE_URL: database_dsn()이 참조 시점에 읽는 env → delenv면 DATABASE_*(로컬)로 폴백.
    - OPENSEARCH_*: config 상수라 import 시점 고정 → 속성 자체를 로컬로 monkeypatch(env 삭제로는 안 됨).

    RAG 라우팅/DSN 파싱 자체를 검증하는 테스트(test_rag_config.py)는 자기 setenv/monkeypatch로
    다시 지정하므로 영향 없다(function-scoped monkeypatch라 이 fixture 뒤에 덮어씀).
    """
    import app.rag.config as rag_config

    monkeypatch.delenv("RAG_DATABASE_URL", raising=False)
    monkeypatch.setattr(rag_config, "OPENSEARCH_HOST", "http://localhost:9200")
    monkeypatch.setattr(rag_config, "OPENSEARCH_USERNAME", "")
    monkeypatch.setattr(rag_config, "OPENSEARCH_PASSWORD", "")
