# -*- coding: utf-8 -*-
"""RAG 캐시 Redis 백엔드 (RPA-274) — fakeredis로 실 Redis 없이 검증한다.

루트 conftest가 REDIS_URL을 빈 문자열로 격리하므로(공유 캐시 오염 방지), 여기서는
자기 setenv + 팩토리 패치로 Redis 모드를 명시적으로 켠다 — test_alerts·test_rag_config와
같은 "격리는 기본, 검증 대상만 자기 손으로 다시 켠다" 패턴이다.

검증 축:
- 라운드트립이 **동일 값**인가 (캐시 투명성 불변식 — 같은 입력=같은 출력)
- 기존 스킵 규칙(빈 결과·저하)이 Redis 모드에서도 그대로인가
- Redis 장애 시 fail-open + 백오프(hot path가 타임아웃을 반복 대기하지 않는가)
- Redis 지정 시 인프로세스로 **폴백하지 않는가** (두 백엔드 분기 방지)
- bust_search가 검색 키만 지우고 임베딩 키는 남기는가
"""

import fakeredis
import pytest

from app.services import rag_cache


class _Params:
    """search_key가 getattr로 읽는 파라미터 5종의 최소 스텁."""

    candidate_pool_size = 50
    rerank_candidates = 20
    rrf_k = 60
    vector_weight = 0.7
    bm25_weight = 0.3


@pytest.fixture
def fake_redis(monkeypatch):
    """rag_cache를 fakeredis 백엔드로 켠다. 반환값은 검사용 fake 클라이언트."""
    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setenv("RAG_CACHE_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "redis://fake-for-test:6379/0")
    monkeypatch.setattr(rag_cache, "_make_redis_client", lambda url: fake)
    # 모듈 싱글톤 초기화 — 이전 테스트가 남긴 클라이언트·백오프 상태를 물려받지 않는다
    monkeypatch.setattr(rag_cache, "_redis_client", None)
    monkeypatch.setattr(rag_cache, "_redis_url_cached", None)
    monkeypatch.setattr(rag_cache, "_redis_down_until", 0.0)
    rag_cache.bust_all()
    return fake


def test_embedding_roundtrip_is_identical(fake_redis):
    key = rag_cache.embedding_key("구글시트에서 값 읽기", "voyage-3", 1024)
    vector = [0.125, -0.5, 3.0e-7, 1.0]
    rag_cache.put_embedding(key, vector)

    assert fake_redis.exists("rag:emb:" + key) == 1  # 실제로 Redis에 갔는가
    assert rag_cache.get_embedding(key) == vector

    s = rag_cache.stats()
    assert s["backend"] == "redis"
    assert s["embed_hit"] == 1


def test_search_roundtrip_is_identical_and_isolated(fake_redis):
    key = rag_cache.search_key("이메일 보내기", 5, None, _Params(), "voyage-3", "rerank-2.5-lite")
    results = [
        {"id": 1, "title": "Send Email — 한글·유니코드 ✓", "score": 0.987654321,
         "meta": {"nested": [1, 2.5, None, True]}, "content": "본문"},
        {"id": 2, "title": "b", "score": 0.5, "bm25_available": True},
    ]
    assert rag_cache.put_search(key, results) is None
    got = rag_cache.get_search(key)
    assert got == results

    # 반환값을 호출부가 변형해도 캐시 원본은 오염되지 않아야 한다 (#290 리뷰와 동일 계약)
    got[0]["title"] = "오염"
    assert rag_cache.get_search(key) == results


def test_skip_rules_still_apply_in_redis_mode(fake_redis):
    key = rag_cache.search_key("q", 5, None, _Params(), "e", "r")
    assert rag_cache.put_search(key, []) == "empty"
    assert rag_cache.put_search(key, [{"id": 1, "bm25_available": False}]) == "degraded"
    assert rag_cache.get_search(key) is None          # 아무것도 저장 안 됨
    assert fake_redis.keys("rag:search:*") == []


def test_unserializable_results_are_skipped_not_mangled(fake_redis):
    """JSON에 못 담는 값이면 저장을 포기한다 — 변형해서 저장하면 '같은 입력=같은 출력'이 깨진다."""
    key = rag_cache.search_key("q2", 5, None, _Params(), "e", "r")
    weird = [{"id": 1, "tags": {"a", "b"}}]  # set은 JSON 직렬화 불가
    assert rag_cache.put_search(key, weird) == "unserializable"
    assert rag_cache.get_search(key) is None
    assert rag_cache.stats()["skip_unserializable"] == 1


class _DownRedis:
    """모든 연산이 접속 오류를 던지는 스텁 — 장애 fail-open·백오프 검증용."""

    def __init__(self):
        self.calls = 0

    def _boom(self, *a, **k):
        self.calls += 1
        raise ConnectionError("redis down")

    get = set = scan_iter = delete = exists = _boom


def test_redis_failure_fails_open_with_backoff(monkeypatch):
    down = _DownRedis()
    monkeypatch.setenv("RAG_CACHE_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "redis://down:6379/0")
    monkeypatch.setattr(rag_cache, "_make_redis_client", lambda url: down)
    monkeypatch.setattr(rag_cache, "_redis_client", None)
    monkeypatch.setattr(rag_cache, "_redis_url_cached", None)
    monkeypatch.setattr(rag_cache, "_redis_down_until", 0.0)
    rag_cache.bust_all()  # 카운터 초기화 — 이 호출 자체가 down을 한 번 때리므로 아래서 리셋
    monkeypatch.setattr(rag_cache, "_redis_down_until", 0.0)
    down.calls = 0

    key = rag_cache.embedding_key("q", "m", 8)
    assert rag_cache.get_embedding(key) is None       # 죽어도 miss로 살아간다
    assert down.calls == 1
    # 백오프 창 안에서는 클라이언트를 다시 때리지 않는다 — hot path 보호
    assert rag_cache.get_embedding(key) is None
    assert down.calls == 1
    assert rag_cache.stats()["redis_error"] >= 1


def test_no_silent_fallback_to_inprocess_when_redis_down(monkeypatch):
    """Redis가 지정돼 있으면 다운이어도 인프로세스에 쓰지 않는다 — 두 백엔드가 갈라지면
    한쪽만 bust되는 순간 '같은 입력=같은 출력'이 깨진다."""
    down = _DownRedis()
    monkeypatch.setenv("RAG_CACHE_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "redis://down:6379/0")
    monkeypatch.setattr(rag_cache, "_make_redis_client", lambda url: down)
    monkeypatch.setattr(rag_cache, "_redis_client", None)
    monkeypatch.setattr(rag_cache, "_redis_url_cached", None)
    monkeypatch.setattr(rag_cache, "_redis_down_until", 0.0)
    monkeypatch.setattr(rag_cache, "_embedding_cache", None)
    monkeypatch.setattr(rag_cache, "_search_cache", None)

    rag_cache.put_embedding(rag_cache.embedding_key("q", "m", 8), [1.0])
    key = rag_cache.search_key("q", 5, None, _Params(), "e", "r")
    rag_cache.put_search(key, [{"id": 1}])
    # 인프로세스 캐시가 생성조차 되지 않았어야 한다
    assert rag_cache._embedding_cache is None
    assert rag_cache._search_cache is None


def test_bust_search_clears_search_but_keeps_embeddings(fake_redis):
    ek = rag_cache.embedding_key("q", "m", 8)
    rag_cache.put_embedding(ek, [1.0, 2.0])
    sk = rag_cache.search_key("q", 5, None, _Params(), "e", "r")
    rag_cache.put_search(sk, [{"id": 1}])

    busted = rag_cache.bust_search()

    assert busted >= 1
    assert rag_cache.get_search(sk) is None
    assert rag_cache.get_embedding(ek) == [1.0, 2.0]  # 임베딩은 코퍼스와 무관 — 유지


def test_memory_backend_when_url_unset(monkeypatch):
    """REDIS_URL 미설정(conftest 기본)이면 기존 인프로세스 동작 그대로."""
    monkeypatch.setenv("RAG_CACHE_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "")
    rag_cache.bust_all()
    key = rag_cache.embedding_key("q", "m", 8)
    rag_cache.put_embedding(key, [0.5])
    assert rag_cache.get_embedding(key) == [0.5]
    assert rag_cache.stats()["backend"] == "memory"
