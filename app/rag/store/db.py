"""pgvector 저장소. docker-compose의 pgvector/pgvector:pg16 컨테이너를 그대로 사용한다."""

import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import psycopg

from .. import config
from ..observability import log_call

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

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


_pool_lock = threading.Lock()
_sync_pool: "ConnectionPool | None" = None
_sync_pool_failed = False


def _get_sync_pool() -> "ConnectionPool | None":
    """검색 경로용 동기 커넥션 풀 (RPA-219). 기동 실패 시 None → 1회성 connect 폴백.

    비동기 경로는 pool.py가 이미 풀을 쓰지만 에이전트가 타는 동기 검색은 검색마다
    새 연결을 맺고 있었다. RAG_DATABASE_URL이 원격 Neon(ap-southeast-1)이라 연결
    수립 자체가 비싸다 — 2026-07-20 실측 451ms/회, 풀에서 빌리면 0.0ms. 팬아웃
    시에는 그 연결 수립이 직렬화된다(pool.py docstring의 k6 실측과 같은 현상).
    """
    global _sync_pool, _sync_pool_failed
    if _sync_pool is not None or _sync_pool_failed:
        return _sync_pool
    with _pool_lock:
        if _sync_pool is None and not _sync_pool_failed:
            pool = None
            try:
                from psycopg_pool import ConnectionPool

                pool = ConnectionPool(config.database_dsn(), min_size=1, max_size=20, open=False)
                # wait=True — DB가 죽어 있으면 여기서 즉시 실패해 폴백한다. 기다리지 않으면
                # 풀은 그냥 열리고 나중에 connection()에서 30초씩 블로킹된다(테스트가 멈춤).
                pool.open(wait=True, timeout=5.0)
                _sync_pool = pool
            except Exception:  # noqa: BLE001 — 풀 기동 실패가 검색을 막으면 안 된다
                # open()은 실패해도 이미 워커 스레드를 띄운 뒤다. _sync_pool에 담기 전이라
                # close_sync_pool()이 회수할 수 없으므로 여기서 닫는다 — 안 닫으면 DB가
                # 불안정할 때마다 유령 워커가 프로세스 종료까지 남는다.
                if pool is not None:
                    try:
                        pool.close()
                    except Exception:  # noqa: BLE001 — 정리 실패가 폴백을 막으면 안 된다
                        logger.debug("실패한 풀 정리 중 예외 (무시)", exc_info=True)
                logger.warning("RAG 동기 커넥션 풀 기동 실패 — 요청별 연결로 폴백", exc_info=True)
                _sync_pool_failed = True
    return _sync_pool


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """검색 경로용 연결 컨텍스트 — 풀에서 빌리고, 풀이 없으면 1회성 연결로 폴백한다.

    적재(ingest)·debug는 계속 connect()를 직접 쓴다(수명이 길고 풀 대상이 아님).
    """
    pool = _get_sync_pool()
    if pool is None:
        conn = connect()
        try:
            yield conn
        finally:
            conn.close()
    else:
        with pool.connection() as conn:
            yield conn


def close_sync_pool() -> None:
    """앱 종료 시 동기 풀을 닫는다 (main.py lifespan)."""
    global _sync_pool, _sync_pool_failed
    with _pool_lock:
        if _sync_pool is not None:
            _sync_pool.close()
            _sync_pool = None
        _sync_pool_failed = False


async def connect_async() -> psycopg.AsyncConnection:
    """/api/rag/search 전용 비동기 경로 — 검색 API가 동기 라우트라 요청마다 스레드풀
    슬롯을 하나씩 점유해 동시 요청 시 대기열이 쌓이던 문제(부하테스트로 확인) 대응.
    적재(ingest)·debug 엔드포인트는 그대로 동기 connect()를 쓴다(둘 다 이 문제의
    대상이 아니었음)."""
    return await psycopg.AsyncConnection.connect(config.database_dsn())


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


_SEARCH_SQL = """
    SELECT id, source_type, package_name, action_name, title, url, content,
           parent_id, chunk_index,
           1 - (embedding <=> %s::vector) AS score
    FROM rag_documents
    WHERE embedding IS NOT NULL
    ORDER BY embedding <=> %s::vector
    LIMIT %s
"""


@log_call("vector_search", capture_args=("limit",), capture_result=lambda r: {"count": len(r)})
def search(conn: psycopg.Connection, query_embedding: list[float], limit: int = 5) -> list[dict]:
    vector = "[" + ",".join(f"{x:.7f}" for x in query_embedding) + "]"
    with conn.cursor() as cur:
        cur.execute(_SEARCH_SQL, (vector, vector, limit))
        columns = [d.name for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


@log_call("vector_search", capture_args=("limit",), capture_result=lambda r: {"count": len(r)})
async def search_async(conn: psycopg.AsyncConnection, query_embedding: list[float], limit: int = 5) -> list[dict]:
    vector = "[" + ",".join(f"{x:.7f}" for x in query_embedding) + "]"
    async with conn.cursor() as cur:
        await cur.execute(_SEARCH_SQL, (vector, vector, limit))
        columns = [d.name for d in cur.description]
        rows = await cur.fetchall()
        return [dict(zip(columns, row)) for row in rows]
