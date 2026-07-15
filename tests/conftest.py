"""공용 테스트 픽스처 (RPA-69).

프로덕션 경로는 항상 실제 RAG(HybridRetriever/BackendCatalog)를 쓰지만, CI(tests.yml)는
pgvector·OpenSearch·DB 없이 pytest만 돌린다. 아래 autouse fixture가 app/agent의 검색기·
카탈로그 팩토리를 인메모리 스텁으로 주입해, 인프라 의존 테스트가 실제 서비스를 때리지
않게 한다. 개별 테스트는 필요하면 자기 페이크로 다시 monkeypatch하면 된다.

팩토리(_make_retriever/_make_catalog)만 patch하므로, 사용처의 `from ..retrieval import
get_retriever` 참조는 그대로 두어도 최신 스텁을 받는다(get_retriever/get_catalog가
호출 시점에 모듈 전역 팩토리를 부르기 때문).
"""

import pytest

from app.agent.v2 import retrieval as retrieval_mod
from app.agent.v2.verify import catalog as catalog_mod

from tests.agent_stubs import FakeCatalog, FakeRetriever


@pytest.fixture(autouse=True)
def _stub_agent_rag(monkeypatch):
    """모든 테스트에서 agent 검색기·카탈로그를 인메모리 스텁으로 대체한다."""
    fake_retriever = FakeRetriever()
    fake_catalog = FakeCatalog()
    monkeypatch.setattr(retrieval_mod, "_make_retriever", lambda: fake_retriever)
    monkeypatch.setattr(catalog_mod, "_make_catalog", lambda: fake_catalog)


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
