# -*- coding: utf-8 -*-
"""동기 검색 경로의 커넥션·클라이언트 재사용 검증 (RPA-219).

여기서 막는 것은 **자원을 매번 새로 만드는 회귀**다. RAG_DATABASE_URL이 원격
Neon(ap-southeast-1)이라 연결 수립이 실측 451ms/회인데, 에이전트가 타는 동기
검색은 검색마다 새 연결·새 httpx.Client·새 OpenSearch 클라이언트를 만들었다.
비동기 경로(/api/rag/search)는 pool.py로 이미 재사용 중이었고 이 경로만 빠져 있었다.

성능 수치 자체는 환경마다 달라 테스트로 고정하지 않는다. 대신 "자원이 몇 번
만들어졌나"라는 **원인**을 센다 — 이게 회귀하면 성능도 같이 회귀한다.
"""

import ssl
import threading

import httpx
import pytest

from app.rag.retrieval import embed
from app.rag.store import db, opensearch_client, pool


@pytest.fixture(autouse=True)
def _reset():
    """모듈 전역 캐시를 테스트마다 초기화 — 테스트 간 순서 의존을 없앤다."""
    embed.close_shared_client()
    opensearch_client.close_shared_client()
    yield
    embed.close_shared_client()
    opensearch_client.close_shared_client()


# --- SSL 컨텍스트: 프로세스당 1회 ---

def test_ssl_context_is_process_wide_singleton():
    """동기·비동기 경로가 같은 컨텍스트를 쓴다 — 경로마다 만들면 그만큼 비용이 는다."""
    assert pool.get_ssl_context() is pool.get_ssl_context()
    assert isinstance(pool.get_ssl_context(), ssl.SSLContext)


def test_shared_client_does_not_rebuild_ssl_context(monkeypatch):
    """공용 클라이언트 생성이 ssl.create_default_context()를 다시 부르지 않는다.

    컨텍스트 생성 비용은 환경 편차가 커서(측정 환경에선 ~10ms) 성능 근거는 아니다.
    다만 프로세스에 컨텍스트가 하나만 있어야 pool.py와 설정이 갈리지 않는다.
    """
    calls = []
    real = ssl.create_default_context
    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **kw: calls.append(1) or real(*a, **kw))

    for _ in range(5):
        embed.get_shared_client()

    assert calls == [], "공용 SSL 컨텍스트를 재사용해야 한다 (새로 만들면 안 됨)"


# --- httpx 클라이언트: 재사용 ---

def test_shared_http_client_is_reused():
    assert embed.get_shared_client() is embed.get_shared_client()


def test_post_with_retry_reuses_one_client(monkeypatch):
    """🔴 외부 호출 N회에 클라이언트가 1개만 만들어져야 한다."""
    created = []
    real_init = httpx.Client.__init__

    def counting_init(self, *a, **kw):
        created.append(1)
        return real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.Client, "__init__", counting_init)
    monkeypatch.setattr(embed, "log_event", lambda *a, **kw: None)

    class _Resp:
        status_code = 200
        headers: dict = {}
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    monkeypatch.setattr(httpx.Client, "post", lambda self, *a, **kw: _Resp())

    for _ in range(4):
        embed.post_with_retry("https://example.invalid/v1/x", {}, {})

    assert len(created) == 1, f"클라이언트가 {len(created)}번 생성됨 — 재사용이 깨졌다"


def test_shared_client_is_threadsafe_singleton():
    """to_thread 워커들이 동시에 처음 부를 때도 인스턴스는 하나여야 한다."""
    seen = []
    barrier = threading.Barrier(8)

    def grab():
        barrier.wait()
        seen.append(embed.get_shared_client())

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len({id(c) for c in seen}) == 1


# --- OpenSearch 클라이언트: 재사용 ---

def test_shared_opensearch_client_is_reused():
    assert opensearch_client.get_shared_client() is opensearch_client.get_shared_client()


def test_opensearch_connect_still_makes_new_clients():
    """적재(ingest)·debug가 쓰는 connect()는 계속 새 인스턴스를 준다 — 공유로 바꾸지 않았다."""
    assert opensearch_client.connect() is not opensearch_client.connect()


# --- Postgres: 풀 + 폴백 ---

def test_connection_falls_back_when_pool_unavailable(monkeypatch):
    """🔴 풀 기동에 실패해도 검색은 계속돼야 한다 — 1회성 연결로 저하될 뿐."""
    monkeypatch.setattr(db, "_sync_pool", None)
    monkeypatch.setattr(db, "_sync_pool_failed", True)  # 기동 실패 상태

    made = []

    class _Conn:
        def close(self):
            made.append("closed")

    monkeypatch.setattr(db, "connect", lambda: made.append("connected") or _Conn())

    with db.connection() as conn:
        assert isinstance(conn, _Conn)

    assert made == ["connected", "closed"], "폴백 경로는 연결을 열고 반드시 닫아야 한다"


def test_connection_borrows_from_pool_when_available(monkeypatch):
    """풀이 있으면 connect()를 부르지 않는다 — 검색마다 새 연결을 맺던 게 원인이었다."""
    from contextlib import contextmanager

    sentinel = object()

    class _Pool:
        @contextmanager
        def connection(self):
            yield sentinel

    monkeypatch.setattr(db, "_get_sync_pool", lambda: _Pool())
    monkeypatch.setattr(db, "connect", lambda: pytest.fail("풀이 있으면 새 연결을 맺으면 안 된다"))

    with db.connection() as conn:
        assert conn is sentinel
