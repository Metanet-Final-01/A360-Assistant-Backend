# -*- coding: utf-8 -*-
"""RAG 캐시 Redis 백엔드 — **실 Redis** 라이브 검증 (RPA-274).

fakeredis가 못 잡는 것을 실 서버로 확인한다: 실제 TTL 만료, 프로세스 경계를 넘는
가시성(모듈 상태를 리셋해 '새 프로세스'를 시뮬레이트), 장애 시 hot path 지연 상한.

로컬 Redis(기본 redis://localhost:6379/15, TEST_REDIS_URL로 변경)가 없으면 스스로 skip —
통합 테스트 suite와 같은 관례라 CI·팀원 로컬을 막지 않는다. db 15 + 전용 APP_ENV
네임스페이스만 쓰고 시작·종료에 그 네임스페이스만 지운다(FLUSHALL 금지).

⚠️ 남은 수동 확인 항목(자동화 불가): Redis 재시작 후 fail-open 복귀, compose 네트워크의
   서비스 DNS(redis://redis:6379) 해석 — docs/rds-migration 롤아웃 체크리스트에 있다.
"""

import statistics
import time

import pytest

from app.services import rag_cache

_LIVE_URL = None


def _live_url() -> str | None:
    global _LIVE_URL
    if _LIVE_URL is not None:
        return _LIVE_URL or None
    import os

    url = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")
    try:
        import redis

        client = redis.Redis.from_url(url, socket_connect_timeout=0.3, socket_timeout=0.3)
        client.ping()
        client.close()
        _LIVE_URL = url
    except Exception:  # noqa: BLE001 — 없으면 skip이 정답
        _LIVE_URL = ""
    return _LIVE_URL or None


pytestmark = pytest.mark.skipif(_live_url() is None,
                                reason="로컬 Redis 없음 — docker compose --profile redis up -d redis")


@pytest.fixture
def live(monkeypatch):
    """실 Redis + 전용 네임스페이스로 rag_cache를 배선하고, 끝나면 그 네임스페이스만 지운다."""
    monkeypatch.setenv("RAG_CACHE_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", _live_url())
    monkeypatch.setenv("APP_ENV", "pytest-live")   # 네임스페이스 격리 — 남의 키에 손 안 댐
    monkeypatch.setattr(rag_cache, "_redis_client", None)
    monkeypatch.setattr(rag_cache, "_redis_url_cached", None)
    monkeypatch.setattr(rag_cache, "_redis_down_until", 0.0)
    rag_cache.bust_all()
    yield
    rag_cache.bust_all()


class _Params:
    candidate_pool_size = 50
    rerank_candidates = 20
    rrf_k = 60
    vector_weight = 0.7
    bm25_weight = 0.3


def _sk(q="q"):
    return rag_cache.search_key(q, 5, None, _Params(), "e-model", "r-model")


def _results():
    return [{"id": 1, "content": "본문", "score": 0.9}]


def test_cross_process_visibility(live, monkeypatch):
    """한 '프로세스'가 쓴 캐시·세대를 다른 '프로세스'가 본다 — 모듈 상태 리셋으로 경계 시뮬레이트."""
    key = _sk("공유 질의")
    rag_cache.put_search(key, _results())
    gen_before = rag_cache.stats()["generation"]

    # '새 프로세스': 클라이언트·서킷 상태가 전부 초기값 (Redis만 공유)
    monkeypatch.setattr(rag_cache, "_redis_client", None)
    monkeypatch.setattr(rag_cache, "_redis_url_cached", None)
    monkeypatch.setattr(rag_cache, "_redis_down_until", 0.0)

    assert rag_cache.get_search(_sk("공유 질의")) == _results()   # 다른 프로세스의 hit
    assert rag_cache.stats()["generation"] == gen_before

    new_gen = rag_cache.publish_generation()                      # ingest 프로세스의 세대 전환
    monkeypatch.setattr(rag_cache, "_redis_client", None)         # 또 다른 서버 프로세스
    assert rag_cache.stats()["generation"] == new_gen
    assert rag_cache.get_search(_sk("공유 질의")) is None         # 구 세대는 안 보인다


def test_real_ttl_expiry(live, monkeypatch):
    monkeypatch.setenv("RAG_CACHE_TTL_SECONDS", "1")
    key = _sk("ttl 질의")
    rag_cache.put_search(key, _results())
    assert rag_cache.get_search(key) == _results()
    time.sleep(1.3)
    assert rag_cache.get_search(key) is None       # 실 서버 TTL로 실제 만료


def test_latency_hit_vs_circuit_open(live, monkeypatch):
    """지연 상한 — 캐시가 성능 장치인지 확인한다. p50/p95를 출력하고 상한만 느슨히 단언."""
    key = rag_cache.embedding_key("지연 측정", "m", 4)
    rag_cache.put_embedding(key, [1.0, 2.0, 3.0, 4.0])

    def _measure(n=100):
        xs = []
        for _ in range(n):
            t0 = time.perf_counter()
            rag_cache.get_embedding(key, expected_dim=4)
            xs.append((time.perf_counter() - t0) * 1000)
        xs.sort()
        return xs[len(xs) // 2], xs[int(len(xs) * 0.95)]

    hit_p50, hit_p95 = _measure()

    miss_key = rag_cache.embedding_key("없는 질의", "m", 4)
    t0 = time.perf_counter()
    for _ in range(100):
        rag_cache.get_embedding(miss_key, expected_dim=4)
    miss_avg = (time.perf_counter() - t0) * 10  # ms/call

    monkeypatch.setattr(rag_cache, "_redis_down_until", time.monotonic() + 60)  # 서킷 열림
    open_p50, open_p95 = _measure()

    print(f"\n[latency ms] hit p50={hit_p50:.2f} p95={hit_p95:.2f} | miss avg={miss_avg:.2f} "
          f"| circuit-open p50={open_p50:.3f} p95={open_p95:.3f}")
    assert hit_p95 < 50            # 로컬 Redis hit — 검색 5.5초 대비 무시 가능해야 한다
    assert open_p95 < 5            # 서킷 열림 = 네트워크 대기 0 — 장애가 지연 장치가 되면 안 된다
    assert statistics.median([open_p50, open_p95]) < hit_p95 + 50  # 방어적 sanity
