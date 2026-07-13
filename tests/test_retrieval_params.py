"""RAG 검색 파라미터화 테스트 (RPA-130).

핵심 계약:
- RetrievalParams.from_config()는 현재 config 값을 참조 시점에 읽는다(하위호환·monkeypatch).
- 가중 RRF: 기본은 동일 가중(기존 동작), 지정 시 branch 비중 조절, 길이 불일치는 명시적 에러.
- search()에 params를 안 넘기면 config 기본값으로 기존과 동일하게 동작하고,
  넘기면 그 값(후보 풀 크기 등)이 실제 조회에 반영된다.
"""

import pytest

import app.rag.config as config
import app.rag.retrieval.hybrid_search as hs
from app.rag.retrieval.hybrid_search import _fuse_candidates, reciprocal_rank_fusion
from app.rag.retrieval.params import RetrievalParams


# --- RetrievalParams.from_config ---

def test_from_config_reads_config_values(monkeypatch):
    """config 값을 참조 시점에 읽는다 — 테스트가 값을 바꾸면 그대로 반영(스윕의 전제)."""
    monkeypatch.setattr(config, "HYBRID_CANDIDATE_POOL_SIZE", 33)
    monkeypatch.setattr(config, "HYBRID_RERANK_CANDIDATES", 11)
    monkeypatch.setattr(config, "RRF_K", 42)
    p = RetrievalParams.from_config()
    assert (p.candidate_pool_size, p.rerank_candidates, p.rrf_k) == (33, 11, 42)
    assert p.vector_weight == 1.0 and p.bm25_weight == 1.0  # 가중치 기본은 동일


# --- 가중 RRF ---

def test_rrf_default_weights_unchanged():
    """weights 미지정이면 기존과 동일 — 두 branch에 있는 문서가 한쪽에만 있는 것보다 높다."""
    scores = reciprocal_rank_fusion([["a", "b"], ["a", "c"]], k=60)
    assert scores["a"] > scores["b"] and scores["a"] > scores["c"]
    # 같은 순위(1위)라도 동일 가중이면 b와 c는 대칭
    assert scores["b"] == scores["c"]


def test_rrf_weights_scale_branch_contribution():
    """벡터 가중을 키우면 벡터 branch 상위 문서가 BM25 상위 문서보다 앞선다."""
    # b는 벡터 1위만, c는 BM25 1위만 — 동일 가중이면 동점, 벡터 가중을 키우면 b가 앞선다.
    equal = reciprocal_rank_fusion([["b"], ["c"]], k=60)
    assert equal["b"] == equal["c"]
    weighted = reciprocal_rank_fusion([["b"], ["c"]], k=60, weights=[2.0, 1.0])
    assert weighted["b"] > weighted["c"]


def test_rrf_weight_length_mismatch_raises():
    """weights 길이가 branch 수와 다르면 zip이 조용히 자르지 않고 명시적으로 막는다."""
    with pytest.raises(ValueError, match="weights 길이"):
        reciprocal_rank_fusion([["a"], ["b"]], k=60, weights=[1.0])


# --- _fuse_candidates가 params를 쓴다 ---

def _hit(doc_id):
    return {"id": doc_id, "title": doc_id, "content": doc_id}


def test_fuse_truncates_to_rerank_candidates():
    """rerank_candidates가 융합 후보 상한을 정한다(리랭커 비용 조절 노브)."""
    vector_hits = [_hit(x) for x in ("a", "b", "c", "d")]
    params = RetrievalParams(candidate_pool_size=50, rerank_candidates=2, rrf_k=60)
    fused = _fuse_candidates(vector_hits, [], None, params)
    assert len(fused) == 2  # 4개 뽑혀도 상한 2로 잘림


def test_fuse_applies_weights_to_ordering():
    """벡터 가중을 키우면 벡터 전용 문서가 BM25 전용 문서보다 앞 순위로 융합된다."""
    vector_hits = [_hit("v")]   # 벡터 1위
    bm25_hits = [_hit("b")]     # BM25 1위
    heavy_vector = RetrievalParams(
        candidate_pool_size=50, rerank_candidates=20, rrf_k=60,
        vector_weight=5.0, bm25_weight=1.0,
    )
    fused = _fuse_candidates(vector_hits, bm25_hits, None, heavy_vector)
    assert [c["id"] for c in fused] == ["v", "b"]


# --- search() 관통·하위호환 ---

def _patch_search_io(monkeypatch, capture):
    """검색 I/O(임베딩·벡터·BM25)를 스텁으로 갈아끼우고 후보 풀 크기를 기록한다."""
    monkeypatch.setattr(hs, "embed_query", lambda q: [0.1, 0.2])
    monkeypatch.setattr(hs.db, "search",
                        lambda conn, emb, limit: (capture.__setitem__("vector_limit", limit), [_hit("a")])[1])
    monkeypatch.setattr(hs.opensearch_client, "keyword_search",
                        lambda client, q, size: (capture.__setitem__("bm25_size", size), [_hit("a")])[1])


def test_search_defaults_to_config_pool(monkeypatch):
    """params 미지정 → config 후보 풀 크기가 벡터·BM25 조회에 그대로 쓰인다(하위호환)."""
    monkeypatch.setattr(config, "HYBRID_CANDIDATE_POOL_SIZE", 44)
    capture: dict = {}
    _patch_search_io(monkeypatch, capture)
    hs.search(None, None, "q", limit=5, mode="hybrid")  # hybrid: 리랭커 생략
    assert capture["vector_limit"] == 44 and capture["bm25_size"] == 44


def test_search_uses_explicit_params_pool(monkeypatch):
    """params를 넘기면 그 후보 풀 크기가 config 대신 쓰인다(동적 조절)."""
    monkeypatch.setattr(config, "HYBRID_CANDIDATE_POOL_SIZE", 44)  # 무시돼야 함
    capture: dict = {}
    _patch_search_io(monkeypatch, capture)
    params = RetrievalParams(candidate_pool_size=7, rerank_candidates=20, rrf_k=60)
    hs.search(None, None, "q", limit=5, mode="hybrid", params=params)
    assert capture["vector_limit"] == 7 and capture["bm25_size"] == 7
