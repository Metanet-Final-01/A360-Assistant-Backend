"""RAG 임베딩 사용량 기록 테스트 (RPA-62)."""

import app.rag.retrieval.embed as embed


def test_embed_openai_records_rag_embed_usage(monkeypatch):
    # 임베딩 API 응답(usage 포함) 목킹
    monkeypatch.setattr(
        embed, "post_with_retry",
        lambda url, headers, payload: {
            "data": [{"embedding": [0.1, 0.2]} for _ in payload["input"]],
            "usage": {"prompt_tokens": 123, "total_tokens": 123},
        },
    )
    monkeypatch.setattr(embed.config, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(embed.config, "EMBEDDING_MODEL", "text-embedding-3-small")

    recorded = {}
    import app.core.llm as llm
    monkeypatch.setattr(llm, "record_usage", lambda **kw: recorded.update(kw))

    vecs = embed._embed_openai(["문서1", "문서2"])
    assert len(vecs) == 2  # 임베딩은 정상 반환
    assert recorded["purpose"] == "embed"
    assert recorded["input_tokens"] == 123 and recorded["output_tokens"] == 0
    assert recorded["model"] == "text-embedding-3-small"


def test_embed_records_component_rag_embed(monkeypatch):
    """component가 rag_embed로 귀속되는지 (usage_context 경유)."""
    monkeypatch.setattr(
        embed, "post_with_retry",
        lambda *a, **k: {"data": [{"embedding": [0.0]}], "usage": {"total_tokens": 50}},
    )
    monkeypatch.setattr(embed.config, "OPENAI_API_KEY", "sk-test")

    seen = {}
    import app.core.llm as llm
    # record_usage 원본을 쓰되 DB만 가로채 component를 확인
    def _capture(**kw):
        from app.core.llm import current_usage_context
        seen["component"] = current_usage_context().component
    monkeypatch.setattr(llm, "record_usage", _capture)

    embed._embed_openai(["q"])
    assert seen["component"] == "rag_embed"


def test_embed_usage_failure_does_not_break_embedding(monkeypatch):
    """사용량 기록이 실패해도 임베딩은 정상 반환돼야 한다 (best-effort)."""
    monkeypatch.setattr(
        embed, "post_with_retry",
        lambda *a, **k: {"data": [{"embedding": [1.0]}], "usage": {"total_tokens": 10}},
    )
    monkeypatch.setattr(embed.config, "OPENAI_API_KEY", "sk-test")
    import app.core.llm as llm
    monkeypatch.setattr(llm, "record_usage", lambda **kw: (_ for _ in ()).throw(RuntimeError("DB 다운")))

    vecs = embed._embed_openai(["q"])
    assert vecs == [[1.0]]  # 기록 실패에도 임베딩은 정상


def test_no_usage_field_skips_recording(monkeypatch):
    """응답에 usage가 없으면(예외 API) 조용히 스킵."""
    monkeypatch.setattr(
        embed, "post_with_retry",
        lambda *a, **k: {"data": [{"embedding": [1.0]}]},  # usage 없음
    )
    monkeypatch.setattr(embed.config, "OPENAI_API_KEY", "sk-test")
    called = {"n": 0}
    import app.core.llm as llm
    monkeypatch.setattr(llm, "record_usage", lambda **kw: called.__setitem__("n", called["n"] + 1))

    embed._embed_openai(["q"])
    assert called["n"] == 0  # 토큰 정보 없으면 기록 안 함
