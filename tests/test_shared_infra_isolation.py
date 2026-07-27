"""공유 RAG 인프라 격리 회귀 테스트 (RPA-157).

conftest의 _isolate_rag_shared_infra가 실제로 동작해, 개발자 .env에 공유 크레덴셜
(RAG_DATABASE_URL=Neon, OPENSEARCH_HOST=Bonsai)이 있어도 테스트가 그걸로 연결하지
않고 명시적인 로컬 URL을 쓰는지 못박는다. 이 격리가 깨지면(누가 fixture를 지우면) 로컬 pytest가
팀 공유 코퍼스를 오염시킬 수 있으므로 여기서 fail한다.
"""

import app.rag.config as config


def test_rag_dsn_is_explicit_local_not_shared_neon():
    """RAG_DATABASE_URL이 공유 Neon이 아닌 테스트 전용 로컬 URL로 명시된다."""
    dsn = config.database_dsn()
    assert "neon.tech" not in dsn  # 공유 Neon 코퍼스 아님
    assert dsn.startswith("postgresql://") and dsn.endswith("/a360_rag")


def test_opensearch_host_isolated_from_bonsai():
    """OPENSEARCH_HOST가 Bonsai가 아니라 localhost로 저하됐는지."""
    assert "bonsai" not in config.OPENSEARCH_HOST.lower()
    assert config.OPENSEARCH_HOST == "http://localhost:9200"
    assert config.OPENSEARCH_USERNAME == ""  # Bonsai 인증 정보 비활성


def test_rag_config_test_can_still_override(monkeypatch):
    """격리 fixture가 있어도 RAG_DATABASE_URL 파싱을 검증하는 테스트는 setenv로 재지정 가능
    (function-scoped monkeypatch가 fixture 뒤에 덮어씀 — test_rag_config.py 패턴 보호)."""
    monkeypatch.setenv("RAG_DATABASE_URL", "postgresql://u:p@ep-x.neon.tech/db?sslmode=require")
    assert config.database_dsn() == "postgresql://u:p@ep-x.neon.tech/db?sslmode=require"
