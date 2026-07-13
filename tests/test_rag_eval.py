"""RAG 검색 평가 하네스 테스트 (RPA-131).

metrics는 순위 정답 리스트(hits)만으로, harness는 fake search_fn으로 검증한다 —
DB·검색 구현 없이 '측정자'의 정확성만 격리해서 확인한다. goldset 로더는 실제
data/eval 파일이 형식·코퍼스 계약을 지키는지도 함께 본다.
"""

import pytest

from app.rag.eval import metrics
from app.rag.eval.goldset import GoldQuery, doc_key, load_goldset
from app.rag.eval.harness import evaluate


# --- metrics (순수 함수) ---

def test_reciprocal_rank_uses_first_relevant():
    assert metrics.reciprocal_rank([False, False, True, True]) == pytest.approx(1 / 3)
    assert metrics.reciprocal_rank([True, False]) == 1.0
    assert metrics.reciprocal_rank([False, False]) == 0.0  # 정답 없음


def test_recall_at_k_counts_within_k_over_total():
    # 정답 2개 중 상위 3 안에 1개 → recall@3 = 0.5
    assert metrics.recall_at_k([False, True, False, True], total_relevant=2, k=3) == 0.5
    # k 밖의 정답은 제외
    assert metrics.recall_at_k([False, False, False, True], total_relevant=1, k=3) == 0.0
    assert metrics.recall_at_k([True], total_relevant=0, k=3) == 0.0  # 0 나눗셈 방지


def test_hit_at_k_is_binary():
    assert metrics.hit_at_k([False, True, False], k=2) == 1.0
    assert metrics.hit_at_k([False, False, True], k=2) == 0.0  # k 밖


def test_mean_handles_empty():
    assert metrics.mean([]) == 0.0
    assert metrics.mean([1.0, 0.0]) == 0.5


# --- doc_key ---

def test_doc_key_requires_both_package_and_action():
    assert doc_key({"package_name": "Excel_MS", "action_name": "SetCell"}) == ("Excel_MS", "SetCell")
    assert doc_key({"package_name": "Excel_MS", "action_name": None}) is None  # 패키지 개요 등


# --- harness (fake search_fn) ---

def _doc(pkg, act):
    return {"package_name": pkg, "action_name": act, "title": f"{pkg}/{act}"}


def test_evaluate_aggregates_mrr_recall_hit():
    gold = [
        GoldQuery("열기", frozenset({("Excel_MS", "OpenSpreadsheet")})),
        GoldQuery("셀 설정", frozenset({("Excel_MS", "SetCell")})),
    ]

    def fake_search(q):
        if q == "열기":
            return [_doc("Excel_MS", "OpenSpreadsheet"), _doc("Excel", "SetCell")]  # 1위 정답 → RR=1
        return [_doc("File", "downloadTo"), _doc("Excel_MS", "SetCell")]            # 2위 정답 → RR=0.5

    report = evaluate(gold, fake_search, k=5)
    assert report.num_queries == 2
    assert report.mrr == pytest.approx((1.0 + 0.5) / 2)   # (1 + 1/2)/2 = 0.75
    assert report.hit_at_k == 1.0                          # 둘 다 상위 5 안에 정답
    assert report.per_query[1].rank == 2


def test_evaluate_counts_miss_as_zero():
    gold = [GoldQuery("없는 것", frozenset({("Nope", "nope")}))]
    report = evaluate(gold, lambda q: [_doc("Excel_MS", "SetCell")], k=5)
    assert report.mrr == 0.0 and report.hit_at_k == 0.0
    assert report.per_query[0].rank is None


def test_evaluate_respects_k_for_recall_and_hit():
    """정답이 k 밖(6위)에 있으면 hit@5=0이지만 rank·RR은 전체 순위로 잡힌다."""
    gold = [GoldQuery("깊은 정답", frozenset({("Excel_MS", "ProtectWorkbook")}))]
    docs = [_doc("X", str(i)) for i in range(5)] + [_doc("Excel_MS", "ProtectWorkbook")]
    report = evaluate(gold, lambda q: docs, k=5)
    assert report.hit_at_k == 0.0                      # 상위 5 안엔 없음
    assert report.per_query[0].rank == 6
    assert report.mrr == pytest.approx(1 / 6)          # RR은 전체 순위 기준


# --- 실제 골드셋 파일 계약 ---

def test_shipped_goldset_loads_and_is_wellformed():
    gold = load_goldset()
    assert len(gold) >= 10                              # 봇 기반 시드
    for gq in gold:
        assert gq.query and gq.relevant                # 빈 쿼리·정답 없음
        for pkg, act in gq.relevant:
            assert pkg and act


def test_load_goldset_rejects_empty_relevant(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"query": "x", "relevant": []}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="relevant"):
        load_goldset(bad)
