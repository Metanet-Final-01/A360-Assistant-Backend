"""RAG 저장소 DB 연결 분리 테스트 (RPA-132).

RAG_DATABASE_URL이 있으면 RAG 코퍼스를 앱 DB와 분리된 전용 공유 DB로 보내고,
없으면 기존 DATABASE_*(앱 DB)로 폴백한다. RAG store는 raw psycopg라 libpq URL만 받으므로
SQLAlchemy 드라이버 접미사(+psycopg)는 벗겨야 한다.
"""

import app.rag.config as config


def test_dsn_falls_back_to_app_database_when_unset(monkeypatch):
    """RAG_DATABASE_URL 미설정이면 기존 DATABASE_*(libpq 키워드 형식)로 폴백."""
    monkeypatch.delenv("RAG_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_HOST", "db.example")
    monkeypatch.setenv("DATABASE_NAME", "a360")
    dsn = config.database_dsn()
    assert dsn.startswith("host=")          # 키워드 형식(=폴백)
    assert "host=db.example" in dsn and "dbname=a360" in dsn


def test_dsn_prefers_rag_database_url(monkeypatch):
    """RAG_DATABASE_URL이 있으면 그걸 그대로 쓴다(앱 DB와 분리)."""
    monkeypatch.setenv("RAG_DATABASE_URL", "postgresql://u:p@ep-x-pooler.neon.tech/neondb?sslmode=require")
    monkeypatch.setenv("DATABASE_HOST", "localhost")  # 무시돼야 함
    dsn = config.database_dsn()
    assert dsn == "postgresql://u:p@ep-x-pooler.neon.tech/neondb?sslmode=require"


def test_dsn_strips_sqlalchemy_driver_suffix(monkeypatch):
    """관측 URL 형식(postgresql+psycopg://)을 복붙해도 psycopg가 읽도록 접미사를 벗긴다."""
    monkeypatch.setenv("RAG_DATABASE_URL", "postgresql+psycopg://u:p@ep-x.neon.tech/db?sslmode=require")
    assert config.database_dsn() == "postgresql://u:p@ep-x.neon.tech/db?sslmode=require"
    # psycopg2 접미사도 동일하게 처리
    monkeypatch.setenv("RAG_DATABASE_URL", "postgresql+psycopg2://u:p@h/db")
    assert config.database_dsn() == "postgresql://u:p@h/db"


def test_empty_rag_database_url_falls_back(monkeypatch):
    """빈 값(RAG_DATABASE_URL=)은 미설정과 동일하게 앱 DB로 폴백 — 빈 채 커밋된 .env 대응."""
    monkeypatch.setenv("RAG_DATABASE_URL", "")
    monkeypatch.setenv("DATABASE_HOST", "fallback.host")
    dsn = config.database_dsn()
    assert dsn.startswith("host=") and "fallback.host" in dsn
