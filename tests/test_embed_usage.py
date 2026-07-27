"""RAG 임베딩 사용량 기록 테스트 (RPA-62)."""

import pytest

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


# ── embed_query_live: 진단 프로브 전용 라이브 호출 (RPA-232, Qodo 리뷰) ──────────────────


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _RecordingClient:
    last_timeout = None

    def __init__(self, timeout=None):
        _RecordingClient.last_timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        return _FakeResp({"data": [{"embedding": [0.9, 0.8, 0.7]}]})


def test_embed_query_live_bypasses_cache_and_uses_short_timeout(monkeypatch):
    """캐시 히트로 도달성이 가려지면 안 되므로(Qodo #2), embed_query_live는 캐시를 아예 안
    건드리고 API를 실호출한다. 짧은 timeout(Qodo #1)도 전달돼야 한다."""
    monkeypatch.setattr(embed.config, "EMBEDDING_PROVIDER", "voyage")
    monkeypatch.setattr(embed.config, "VOYAGE_API_KEY", "vk-test")
    monkeypatch.setattr(embed.httpx, "Client", _RecordingClient)

    from app.services import rag_cache

    calls = {"get": 0, "put": 0}
    monkeypatch.setattr(rag_cache, "get_embedding", lambda k: calls.__setitem__("get", calls["get"] + 1) or [9, 9, 9])
    monkeypatch.setattr(rag_cache, "put_embedding", lambda k, v: calls.__setitem__("put", calls["put"] + 1))

    vec = embed.embed_query_live("probe text", timeout=3.0)
    assert vec == [0.9, 0.8, 0.7]         # 캐시([9,9,9])가 아니라 실제 API 결과
    assert calls == {"get": 0, "put": 0}  # 캐시를 아예 안 건드림
    assert _RecordingClient.last_timeout == 3.0


def test_embed_query_live_requires_key(monkeypatch):
    monkeypatch.setattr(embed.config, "EMBEDDING_PROVIDER", "voyage")
    monkeypatch.setattr(embed.config, "VOYAGE_API_KEY", "")
    with pytest.raises(RuntimeError):
        embed.embed_query_live("x")
