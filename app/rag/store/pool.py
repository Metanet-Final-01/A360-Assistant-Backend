"""/api/rag/search 전용 재사용 커넥션 — pgvector 커넥션 풀 + 재사용 httpx 클라이언트.

k6 부하테스트로 확인한 진짜 병목: 요청마다 새 Postgres 연결(psycopg.AsyncConnection.
connect)과 새 httpx.AsyncClient를 맺었다 버려서, 동시 요청이 늘면 연결 수립 자체가
직렬화되며 지연시간이 쌓였다(비동기 전환만으로는 안 풀림 — 스레드풀 대기가 원인이
아니었다). 여기서 앱 생명주기 동안 딱 한 번 열고 재사용한다.

lifespan(app/main.py)에서 open_pools()/close_pools()를 호출한다. 적재(ingest)·
debug 엔드포인트는 이 문제의 대상이 아니었으므로 여전히 요청별 sync connect를 쓴다.
"""

import logging
import ssl

import httpx
from psycopg_pool import AsyncConnectionPool

from .. import config
from .opensearch_client import connect_async as _connect_opensearch_async

logger = logging.getLogger(__name__)

# httpx.AsyncClient(verify=True)(기본값)는 매번 ssl.create_default_context()를 새로
# 만드는데, 이게 Windows에서 인증서 스토어를 읽느라 매 호출 700ms~1s씩 걸린다(실측 —
# 커넥션 풀링 효과를 테스트 스위트에서 검증하다 발견). 프로세스당 한 번만 만들어서
# httpx.AsyncClient(verify=...)에 넘기면 이후 생성은 1ms 미만.
_SSL_CONTEXT = ssl.create_default_context()


def get_ssl_context() -> ssl.SSLContext:
    """프로세스 공용 SSL 컨텍스트 — 동기 경로(app/rag/retrieval/embed.py)도 같이 쓴다 (RPA-219).

    위 주석의 700ms~1s는 특정 환경(개발 PC) 수치다. 2026-07-20 재측정에서는 새
    프로세스 최초 호출도 약 10ms였다 — 환경 편차가 크므로 이 비용을 성능 근거로
    삼지 말 것. 공유하는 이유는 "어느 환경에서든 중복 생성할 이유가 없어서"다.
    """
    return _SSL_CONTEXT

_pg_pool: AsyncConnectionPool | None = None
_opensearch_client: httpx.AsyncClient | None = None
_external_client: httpx.AsyncClient | None = None  # Voyage/OpenAI (임베딩·리랭크)


async def open_pools() -> None:
    global _pg_pool, _opensearch_client, _external_client
    try:
        _pg_pool = AsyncConnectionPool(config.database_dsn(), min_size=2, max_size=20, open=False)
        await _pg_pool.open()
    except Exception:  # noqa: BLE001 — DB 없이도 앱은 기동돼야 한다(기존 lifespan 관례)
        logger.warning("RAG pgvector 커넥션 풀 기동 실패", exc_info=True)
        _pg_pool = None

    _opensearch_client = _connect_opensearch_async()
    _external_client = httpx.AsyncClient(timeout=60.0, verify=_SSL_CONTEXT)


async def close_pools() -> None:
    global _pg_pool, _opensearch_client, _external_client
    if _pg_pool is not None:
        await _pg_pool.close()
        _pg_pool = None
    if _opensearch_client is not None:
        await _opensearch_client.aclose()
        _opensearch_client = None
    if _external_client is not None:
        await _external_client.aclose()
        _external_client = None


def get_pg_pool() -> AsyncConnectionPool | None:
    return _pg_pool


def get_opensearch_client() -> httpx.AsyncClient | None:
    return _opensearch_client


def get_external_client() -> httpx.AsyncClient | None:
    return _external_client
