"""pgvector 저장소. docker-compose의 pgvector/pgvector:pg16 컨테이너를 그대로 사용한다."""

import json

import psycopg

from .. import config
from ..observability import log_call

_DDL = f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS rag_documents (
    id            text PRIMARY KEY,
    source_type   text NOT NULL,
    package_name  text,
    action_name   text,
    locale        text,
    title         text NOT NULL,
    url           text,
    content       text NOT NULL,
    metadata      jsonb NOT NULL DEFAULT '{{}}',
    embedding     vector({config.EMBEDDING_DIM}),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
-- 이미 존재하는 테이블에도 청킹 컬럼이 반영되도록 ADD COLUMN IF NOT EXISTS로 추가 (별도 마이그레이션 도구 없이)
ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS parent_id text;
ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS chunk_index integer NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_rag_documents_package ON rag_documents (package_name);
CREATE INDEX IF NOT EXISTS idx_rag_documents_source ON rag_documents (source_type);
CREATE INDEX IF NOT EXISTS idx_rag_documents_parent ON rag_documents (parent_id);
"""


def connect() -> psycopg.Connection:
    return psycopg.connect(config.database_dsn())


def ensure_schema(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(_DDL)
    conn.commit()


def clear_all(conn: psycopg.Connection) -> None:
    """rag_documents 전체 삭제. upsert는 새 build에 없는 옛 row를 절대 지우지 않으므로,
    RAG 구조를 크게 바꿔 재적재할 때(예: 패키지/액션 스키마 커버리지 범위 변경) 쓰는
    명시적 초기화 — 자동으로 호출되지 않고 `ingest --clean`에서만 실행된다."""
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE rag_documents")
    conn.commit()


def upsert_documents(conn: psycopg.Connection, documents: list[dict], embeddings: list | None) -> int:
    sql = """
        INSERT INTO rag_documents
            (id, source_type, package_name, action_name, locale, title, url, content, metadata,
             parent_id, chunk_index, embedding, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, now())
        ON CONFLICT (id) DO UPDATE SET
            source_type = EXCLUDED.source_type,
            package_name = EXCLUDED.package_name,
            action_name = EXCLUDED.action_name,
            locale = EXCLUDED.locale,
            title = EXCLUDED.title,
            url = EXCLUDED.url,
            content = EXCLUDED.content,
            metadata = EXCLUDED.metadata,
            parent_id = EXCLUDED.parent_id,
            chunk_index = EXCLUDED.chunk_index,
            embedding = COALESCE(EXCLUDED.embedding, rag_documents.embedding),
            updated_at = now()
    """
    with conn.cursor() as cur:
        for i, doc in enumerate(documents):
            vector = None
            if embeddings is not None:
                vector = "[" + ",".join(f"{x:.7f}" for x in embeddings[i]) + "]"
            cur.execute(
                sql,
                (
                    doc["id"],
                    doc["source_type"],
                    doc.get("package_name"),
                    doc.get("action_name"),
                    doc.get("locale"),
                    doc["title"],
                    doc.get("url"),
                    doc["content"],
                    json.dumps(doc.get("metadata", {}), ensure_ascii=False),
                    doc.get("parent_id", doc["id"]),
                    doc.get("chunk_index", 0),
                    vector,
                ),
            )
    conn.commit()
    return len(documents)


@log_call("vector_search", capture_args=("limit",), capture_result=lambda r: {"count": len(r)})
def search(conn: psycopg.Connection, query_embedding: list[float], limit: int = 5) -> list[dict]:
    vector = "[" + ",".join(f"{x:.7f}" for x in query_embedding) + "]"
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, source_type, package_name, action_name, title, url, content,
                   parent_id, chunk_index,
                   1 - (embedding <=> %s::vector) AS score
            FROM rag_documents
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (vector, vector, limit),
        )
        columns = [d.name for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
