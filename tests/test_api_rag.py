"""RAG 검색 API 에러 응답 형식 테스트 (RPA-72).

공개 엔드포인트라 에러는 표준 {code,message}로 나가야 하고, 내부 예외 문자열은
클라이언트에 노출되면 안 된다(정보 누출 방지). 원인은 서버 로그로만 남는다.

/api/rag/search는 비동기 라우트 + 앱 전역 커넥션 풀(app/rag/store/pool.py) 사용 —
부하테스트로 확인된 "요청마다 새 연결"이 진짜 병목이라 lifespan에서 한 번만 연다.
여기서는 app.api.rag가 lazy import하는 app.rag.store.pool의 get_pg_pool을 모킹해
가짜 풀을 흉내낸다. debug 쪽(/api/rag/debug/vector-search)은 여전히 동기라
db.connect/db.search를 그대로 모킹한다.
"""

from contextlib import asynccontextmanager

from fastapi.testclient import TestClient

from app.main import app


class _FakeAsyncConn:
    def __init__(self):
        self.closed = False


class _FakePool:
    """get_pg_pool()이 반환하는 psycopg_pool.AsyncConnectionPool을 흉내낸다 —
    라우트는 `async with pool.connection() as conn:`만 쓴다."""

    def __init__(self, conn=None, connect_error: Exception | None = None):
        self._conn = conn or _FakeAsyncConn()
        self._connect_error = connect_error

    @asynccontextmanager
    async def connection(self):
        if self._connect_error is not None:
            raise self._connect_error
        yield self._conn


def test_search_db_failure_returns_code_message(monkeypatch):
    import app.rag.store.pool as rag_pool

    monkeypatch.setattr(rag_pool, "get_pg_pool", lambda: None)  # lifespan에서 풀 기동 자체가 실패한 상태
    with TestClient(app) as c:
        r = c.get("/api/rag/search", params={"q": "엑셀"})

    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["code"] == "SEARCH_STORE_UNAVAILABLE"  # raw 문자열 아님
    assert "message" in detail


def test_search_pool_connection_failure_returns_code_message(monkeypatch):
    """풀은 열려 있지만 커넥션을 못 받아오는 경우(고갈·DB 다운 등)."""
    import app.rag.store.pool as rag_pool

    monkeypatch.setattr(
        rag_pool, "get_pg_pool", lambda: _FakePool(connect_error=RuntimeError("connection refused at 10.0.0.5:5432"))
    )
    with TestClient(app) as c:
        r = c.get("/api/rag/search", params={"q": "엑셀"})

    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["code"] == "SEARCH_STORE_UNAVAILABLE"
    assert "connection refused" not in detail["message"]  # 내부 예외 비노출


def test_search_hybrid_failure_returns_code_message(monkeypatch):
    import app.rag.store.pool as rag_pool
    import app.rag.retrieval.hybrid_search as hs

    monkeypatch.setattr(rag_pool, "get_pg_pool", lambda: _FakePool())

    async def _boom(*a, **k):
        raise RuntimeError("opensearch index missing")

    monkeypatch.setattr(hs, "search_async", _boom)
    with TestClient(app) as c:
        r = c.get("/api/rag/search", params={"q": "엑셀"})

    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["code"] == "SEARCH_UNAVAILABLE"
    assert "opensearch index missing" not in detail["message"]


def test_search_non_runtime_error_standardized(monkeypatch):
    """hybrid_search가 RuntimeError 외 예외(psycopg/OpenSearch 등)를 던져도 표준화된다."""
    import app.rag.store.pool as rag_pool
    import app.rag.retrieval.hybrid_search as hs

    monkeypatch.setattr(rag_pool, "get_pg_pool", lambda: _FakePool())

    async def _boom(*a, **k):
        raise ValueError("unexpected non-RuntimeError")  # 비-RuntimeError

    monkeypatch.setattr(hs, "search_async", _boom)
    with TestClient(app) as c:
        r = c.get("/api/rag/search", params={"q": "엑셀"})

    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "SEARCH_UNAVAILABLE"


def test_debug_vector_search_db_search_failure_standardized(monkeypatch):
    """debug vector-search의 db.search 실패도 표준 {code,message} + conn 닫힘 (CodeRabbit).

    같은 함수의 connect 실패는 _error로 고쳤는데 바로 다음 db.search만 try/finally로 남아
    원시 예외가 샜던 것을 막는다. (debug는 게이트/로컬 전용이라 상세 메시지는 유지)
    debug 라우트는 여전히 동기라 기존 동기 db.connect/db.search를 모킹한다.
    """
    import app.rag.retrieval.embed as embed_mod
    import app.rag.store.db as db

    monkeypatch.setattr(embed_mod, "embed_query", lambda q: [0.1, 0.2])
    conn = _FakeConn()
    monkeypatch.setattr(db, "connect", lambda: conn)

    def _boom(*a, **k):
        raise RuntimeError("pgvector query failed")

    monkeypatch.setattr(db, "search", _boom)
    with TestClient(app) as c:
        r = c.get("/api/rag/debug/vector-search", params={"q": "엑셀"})

    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "DB_UNAVAILABLE"  # raw 예외 아님
    assert conn.closed  # 열어둔 커넥션은 닫힌다


class _FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True
