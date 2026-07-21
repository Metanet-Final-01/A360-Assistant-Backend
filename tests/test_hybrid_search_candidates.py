"""hybrid_search 관측 후보 요약 테스트 (RPA-129) — 실제로 뭐가 뽑혔나를 로그에 남긴다."""

from app.rag.retrieval.hybrid_search import _bm25_contributed, _bm25_health, _candidate_summary


def test_candidate_summary_prefers_rerank_score():
    item = {"title": "Excel 열 읽기", "package": "Excel_MS", "action": "ReadColumn",
            "rrf_score": 0.0328, "rerank_score": 0.91, "retrieval_source": "hybrid_both"}
    s = _candidate_summary(item)
    assert s == {"title": "Excel 열 읽기", "package": "Excel_MS", "action": "ReadColumn",
                 "score": 0.91, "source": "hybrid_both"}  # rerank 점수 우선


def test_candidate_summary_falls_back_to_rrf_and_missing_fields():
    s = _candidate_summary({"title": "x", "rrf_score": 0.032812, "retrieval_source": "hybrid_dense_only"})
    assert s["score"] == 0.0328               # rerank 없으면 RRF, 4자리 반올림
    assert s["package"] is None and s["action"] is None  # 없는 필드는 None(안전)


def test_candidate_summary_handles_no_score():
    assert _candidate_summary({"title": "x"})["score"] is None


# --- BM25 기여 여부 (RPA-249): "예외 없음"과 "실제 기여"는 다르다 ---

def test_bm25_contributed_true_when_bm25_ranked_any_doc():
    results = [{"bm25_available": True, "bm25_rank": 3}, {"bm25_available": True, "bm25_rank": None}]
    assert _bm25_contributed(results) is True
    assert _bm25_health(results) is True


def test_bm25_available_but_zero_hits_is_not_contribution():
    """배포에서 실제로 벌어진 상태 — 색인이 비어 200 OK로 0건이 오면 available=True인 채
    dense-only 반쪽이 된다. 그때 지표가 "정상"이라고 말하면 안 된다."""
    results = [
        {"bm25_available": True, "bm25_rank": None, "retrieval_source": "hybrid_dense_only"},
        {"bm25_available": True, "bm25_rank": None, "retrieval_source": "hybrid_dense_only"},
    ]
    assert _bm25_health(results) is True          # 예외는 없었다(기존 의미 유지)
    assert _bm25_contributed(results) is False    # 그러나 한 건도 기여하지 못했다


def test_bm25_contributed_false_when_search_failed():
    """OpenSearch 실패(저하)면 기여도 당연히 없다 — 둘 다 저하로 잡힌다."""
    results = [{"bm25_available": False, "bm25_rank": None, "bm25_error": "timeout"}]
    assert _bm25_health(results) is False
    assert _bm25_contributed(results) is False


def test_bm25_contributed_none_for_vector_mode():
    """mode=vector는 BM25를 아예 부르지 않는다 — 기여 없음(False)이 아니라 해당 없음(None)."""
    results = [{"retrieval_source": "vector"}, {"retrieval_source": "vector"}]
    assert _bm25_health(results) is None
    assert _bm25_contributed(results) is None


def test_bm25_contributed_none_for_empty_results():
    assert _bm25_contributed([]) is None
