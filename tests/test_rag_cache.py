# -*- coding: utf-8 -*-
"""RAG 캐싱 2층 검증 (RPA-211).

여기서 막는 것은 **성능이 아니라 정확성**이다. 캐시는 투명해야 한다 — 같은 입력이면
바이트 단위로 같은 결과. 다르면 캐싱이 아니라 에이전트 동작 변경이다.
"""

import pytest

from app.services import rag_cache


class _Params:
    """RetrievalParams 스텁 — 키에 들어가야 하는 5개 필드."""

    def __init__(self, pool=50, rc=20, k=60, vw=1.0, bw=1.0):
        self.candidate_pool_size = pool
        self.rerank_candidates = rc
        self.rrf_k = k
        self.vector_weight = vw
        self.bm25_weight = bw


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("RAG_CACHE_ENABLED", "true")
    rag_cache.bust_all()
    yield
    rag_cache.bust_all()


def _key(params=None, **kw):
    return rag_cache.search_key(
        kw.get("query", "send email"), kw.get("k", 5), kw.get("source_types"),
        params or _Params(), kw.get("embed_model", "voyage-3"), kw.get("rerank_model", "rerank-2.5-lite"),
    )


# --- 토글: 미설정이면 기존 동작 그대로 ---

def test_disabled_by_default(monkeypatch):
    """미설정=비활성 — 켜지 않은 배포의 동작을 바꾸지 않는다."""
    monkeypatch.delenv("RAG_CACHE_ENABLED", raising=False)
    assert rag_cache.enabled() is False
    rag_cache.put_search(_key(), [{"id": "a", "bm25_available": True}])
    assert rag_cache.get_search(_key()) is None  # 저장도 조회도 안 한다


def test_roundtrip_returns_identical_results():
    """🔴 miss 후 hit이 **완전히 같은 값**이어야 한다 — 다르면 에이전트가 다른 걸 본다."""
    results = [{"id": "a", "score": 0.9, "bm25_available": True},
               {"id": "b", "score": 0.7, "bm25_available": True}]
    rag_cache.put_search(_key(), results)
    got = rag_cache.get_search(_key())
    assert got == results


def test_cached_copy_is_isolated():
    """호출부가 결과를 변형해도 캐시 원본이 오염되면 안 된다 (다음 호출이 다른 값을 받는다)."""
    rag_cache.put_search(_key(), [{"id": "a", "score": 0.9, "bm25_available": True}])
    first = rag_cache.get_search(_key())
    first[0]["score"] = 999          # 호출부가 score를 덮어쓰는 실제 코드가 있다
    assert rag_cache.get_search(_key())[0]["score"] == 0.9


# --- 🔴 키가 결과를 좌우하는 모든 입력을 포함하는가 ---

@pytest.mark.parametrize("field,changed", [
    ("candidate_pool_size", _Params(pool=80)),
    ("rerank_candidates", _Params(rc=10)),
    ("rrf_k", _Params(k=30)),
    ("vector_weight", _Params(vw=2.0)),
    ("bm25_weight", _Params(bw=0.5)),
])
def test_runtime_params_change_the_key(field, changed):
    """🔴 파라미터는 RPA-149로 **런타임에 바뀐다**(백오피스 슬라이더).

    키에 없으면 "튜닝했는데 안 먹는다"가 된다 — 캐시가 옛 결과를 계속 준다.
    """
    assert _key(_Params()) != _key(changed), f"{field}가 키에 반영되지 않는다"


def test_models_change_the_key():
    """모델 교체 시 옛 결과·벡터를 쓰면 안 된다."""
    assert _key(embed_model="voyage-3") != _key(embed_model="voyage-3-large")
    assert _key(rerank_model="rerank-2.5-lite") != _key(rerank_model="rerank-2.5")
    assert rag_cache.embedding_key("q", "voyage-3", 1024) != rag_cache.embedding_key("q", "voyage-3", 512)


def test_request_shape_changes_the_key():
    assert _key(k=5) != _key(k=10)
    assert _key(source_types=None) != _key(source_types=("action_schema",))


def test_key_uses_raw_query_without_normalization():
    """🔴 질의를 **정규화하지 않고 그대로** 키에 쓴다 (#290 리뷰).

    키만 공백을 합치면, 실제 임베딩·BM25 호출에는 **원본 질의가 그대로 가므로**
    `"send  email"`과 `"send email"`이 같은 키를 공유하면서 실제 결과는 다를 수 있다 —
    캐시가 결과를 바꾸는 것이고, 이 캐시가 지키겠다고 한 불변식을 스스로 깨는 것이다.
    적중률을 조금 잃더라도 원본을 쓴다.
    """
    assert _key(query="send  email") != _key(query="send email")   # 공백 변형도 다른 키
    assert _key(query="Send Email") != _key(query="send email")
    assert rag_cache.embedding_key("send  email", "m", 1) != rag_cache.embedding_key("send email", "m", 1)


def test_embedding_cached_copy_is_isolated():
    """임베딩도 복사본을 저장·반환한다 — 호출부가 벡터를 변형해도 캐시 원본이 오염되면 안 된다."""
    ek = rag_cache.embedding_key("q", "voyage-3", 1024)
    original = [0.1, 0.2, 0.3]
    rag_cache.put_embedding(ek, original)
    original[0] = 999                       # 저장 뒤 호출부가 원본 리스트를 변형
    got = rag_cache.get_embedding(ek)
    assert got == [0.1, 0.2, 0.3]
    got[0] = -1                             # 받은 쪽이 변형해도
    assert rag_cache.get_embedding(ek) == [0.1, 0.2, 0.3]


# --- 🔴 저하된 결과는 캐싱하지 않는다 ---

def test_degraded_result_is_not_cached():
    """OpenSearch가 죽어 dense-only로 저하된 결과를 캐싱하면, 복구 후에도 TTL 동안
    반쪽 결과가 나간다 — 성능 최적화가 장애를 숨기는 장치가 되면 안 된다 (RPA-156)."""
    degraded = [{"id": "a", "bm25_available": False}, {"id": "b", "bm25_available": False}]
    assert rag_cache.put_search(_key(), degraded) == "degraded"
    assert rag_cache.get_search(_key()) is None
    assert rag_cache.stats()["skip_degraded"] == 1


def test_partially_degraded_is_not_cached():
    """항목 하나라도 저하면 저장하지 않는다 — 섞인 결과를 굳히면 원인 추적이 불가능해진다."""
    mixed = [{"id": "a", "bm25_available": True}, {"id": "b", "bm25_available": False}]
    assert rag_cache.put_search(_key(), mixed) == "degraded"
    assert rag_cache.get_search(_key()) is None


def test_empty_result_is_not_cached():
    """빈 결과는 '저하로 0건'인지 '정말 없음'인지 **구분할 수 없다** — 굳히지 않는다."""
    assert rag_cache.put_search(_key(), []) == "empty"
    assert rag_cache.get_search(_key()) is None
    assert rag_cache.stats()["skip_empty"] == 1


# --- 무효화 ---

def test_bust_search_keeps_embeddings():
    """적재 후 검색 결과는 낡지만 **질의 임베딩은 그대로 유효하다**(문서와 무관)."""
    ek = rag_cache.embedding_key("q", "voyage-3", 1024)
    rag_cache.put_embedding(ek, [0.1, 0.2])
    rag_cache.put_search(_key(), [{"id": "a", "bm25_available": True}])

    assert rag_cache.bust_search() == 1
    assert rag_cache.get_search(_key()) is None
    assert rag_cache.get_embedding(ek) == [0.1, 0.2]   # 임베딩은 살아 있어야 한다


def test_ttl_expiry(monkeypatch):
    """TTL이 지나면 만료된다 — **적재(ingest)는 별도 프로세스라 캐시를 못 비우므로**,
    코퍼스 변경 후 낡은 결과가 나가는 창을 묶는 건 TTL뿐이다. 그래서 기본을 짧게(1시간) 잡았다.
    """
    import time

    monkeypatch.setenv("RAG_CACHE_TTL_SECONDS", "1")   # 최소값(0은 1로 클램프됨)
    rag_cache.bust_all()
    rag_cache.put_search(_key(), [{"id": "a", "bm25_available": True}])
    assert rag_cache.get_search(_key()) is not None    # 아직 유효
    time.sleep(1.2)
    assert rag_cache.get_search(_key()) is None        # 만료


def test_stats_report_hit_rate_and_skips():
    rag_cache.put_search(_key(), [{"id": "a", "bm25_available": True}])
    rag_cache.get_search(_key())          # hit
    rag_cache.get_search(_key(k=99))      # miss
    s = rag_cache.stats()
    assert s["search_hit"] == 1 and s["search_miss"] == 1
    assert s["search_hit_rate"] == 0.5
    assert s["enabled"] is True


# --- BM25 저하 지표: 3상태 구분 (#290 리뷰) ---

def test_bm25_health_distinguishes_three_states():
    """🔴 True=정상 / False=저하 / None=해당 없음(BM25를 부르지 않음)을 구분한다.

    `mode="vector"`는 BM25를 **아예 호출하지 않아** 결과에 이 필드가 없다. 기본값 True로
    집계하면 "호출도 안 했는데 정상"으로 기록돼 저하율이 거짓말을 한다.
    """
    from app.rag.retrieval.hybrid_search import _bm25_health

    assert _bm25_health([{"bm25_available": True}, {"bm25_available": True}]) is True
    assert _bm25_health([{"bm25_available": True}, {"bm25_available": False}]) is False
    assert _bm25_health([{"id": "a"}, {"id": "b"}]) is None      # vector 모드 — 필드 없음
    assert _bm25_health([]) is None                              # 결과 없음
