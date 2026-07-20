"""Rerank 사용량 기록 테스트 (RPA-74). embed.py와 동일 패턴 — component=rag_rerank."""

import app.rag.retrieval.rerank as rerank_mod

_TWO = {"data": [{"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.5}],
        "usage": {"total_tokens": 321}}


def test_rerank_records_rag_rerank_usage(monkeypatch):
    monkeypatch.setattr(rerank_mod.config, "VOYAGE_API_KEY", "vk-test")
    monkeypatch.setattr(rerank_mod.config, "RERANK_MODEL", "rerank-2.5-lite")
    monkeypatch.setattr(rerank_mod, "post_with_retry", lambda *a, **k: _TWO)

    recorded = {}
    import app.core.llm as llm
    monkeypatch.setattr(llm, "record_usage", lambda **kw: recorded.update(kw))

    out = rerank_mod.rerank("q", ["doc a", "doc b"], top_k=2)
    assert [r["index"] for r in out] == [0, 1]  # relevance 내림차순 정렬 정상
    assert recorded["purpose"] == "rerank"
    assert recorded["input_tokens"] == 321 and recorded["output_tokens"] == 0
    assert recorded["model"] == "rerank-2.5-lite"


def test_rerank_records_component_rag_rerank(monkeypatch):
    monkeypatch.setattr(rerank_mod.config, "VOYAGE_API_KEY", "vk-test")
    monkeypatch.setattr(
        rerank_mod, "post_with_retry",
        lambda *a, **k: {"data": [{"index": 0, "relevance_score": 0.9}], "usage": {"total_tokens": 50}},
    )
    seen = {}
    import app.core.llm as llm

    def _capture(**kw):
        from app.core.llm import current_usage_context
        seen["component"] = current_usage_context().component

    monkeypatch.setattr(llm, "record_usage", _capture)
    rerank_mod.rerank("q", ["d"], top_k=1)
    assert seen["component"] == "rag_rerank"


def test_rerank_no_usage_field_skips(monkeypatch):
    """usage 없는 응답(방어)엔 조용히 스킵."""
    monkeypatch.setattr(rerank_mod.config, "VOYAGE_API_KEY", "vk-test")
    monkeypatch.setattr(
        rerank_mod, "post_with_retry",
        lambda *a, **k: {"data": [{"index": 0, "relevance_score": 0.9}]},
    )
    called = {"n": 0}
    import app.core.llm as llm
    monkeypatch.setattr(llm, "record_usage", lambda **kw: called.__setitem__("n", called["n"] + 1))

    rerank_mod.rerank("q", ["d"], top_k=1)
    assert called["n"] == 0


def test_rerank_usage_failure_does_not_break(monkeypatch):
    """사용량 기록이 실패해도 rerank 결과는 정상 반환 (best-effort)."""
    monkeypatch.setattr(rerank_mod.config, "VOYAGE_API_KEY", "vk-test")
    monkeypatch.setattr(
        rerank_mod, "post_with_retry",
        lambda *a, **k: {"data": [{"index": 0, "relevance_score": 0.9}], "usage": {"total_tokens": 10}},
    )
    import app.core.llm as llm
    monkeypatch.setattr(llm, "record_usage", lambda **kw: (_ for _ in ()).throw(RuntimeError("DB 다운")))

    out = rerank_mod.rerank("q", ["d"], top_k=1)
    assert out == [{"index": 0, "relevance_score": 0.9}]  # 기록 실패에도 정상
