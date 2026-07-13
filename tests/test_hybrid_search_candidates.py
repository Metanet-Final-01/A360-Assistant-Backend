"""hybrid_search 관측 후보 요약 테스트 (RPA-129) — 실제로 뭐가 뽑혔나를 로그에 남긴다."""

from app.rag.retrieval.hybrid_search import _candidate_summary


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
