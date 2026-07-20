"""RAG 파이프라인 로그 관측 DB 중앙화 테스트 (RPA-128)."""

import json
from types import SimpleNamespace

import app.rag.observability as obs


class _FakeSession:
    def __init__(self):
        self.added = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def add(self, row):
        self.added.append(row)

    def commit(self):
        pass


def test_mask_record_masks_preview_and_error():
    """검색어 preview와 error_message의 자유 텍스트를 마스킹한 복사본 반환(원본 불변)."""
    record = {
        "request_id": "r1", "event": "embed_query",
        "args": {"query": {"len": 20, "preview": "문의 user@ex.com 관련"}},
        "error_message": "실패: 010-1234-5678 응답 없음",
    }
    masked = obs._mask_record(record)
    assert masked["args"]["query"]["preview"] == "문의 [EMAIL] 관련"
    assert "user@ex.com" not in masked["args"]["query"]["preview"]   # 원문 부재까지 명시 확인
    assert "[NUM]" in masked["error_message"] and "010-1234-5678" not in masked["error_message"]
    assert record["args"]["query"]["preview"] == "문의 user@ex.com 관련"  # 원본 불변(깊은 복사)


def test_mask_record_fails_closed_without_masking_module(monkeypatch):
    """마스킹 모듈 import 실패 시 원문 유지가 아니라 자유 텍스트를 [REDACTED]로 (fail-closed)."""
    import builtins

    real_import = builtins.__import__

    def _block(name, *a, **k):
        if name == "app.core.masking" or (a and a[3] and "mask_pii" in (a[3] or ()) and "masking" in name):
            raise ImportError("blocked")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _block)
    masked = obs._mask_record({
        "event": "embed_query",
        "args": {"q": {"len": 5, "preview": "비밀 a@b.com"}},
        "error_message": "원문 오류",
    })
    assert masked["args"]["q"]["preview"] == "[REDACTED]"
    assert masked["error_message"] == "[REDACTED]"


def test_persist_adds_config_for_search(monkeypatch):
    """hybrid_search엔 설정 스냅샷(chunk_size 등) 포함, record는 이미 마스킹된 것을 받는다."""
    fake = _FakeSession()
    monkeypatch.setattr("app.core.observability_db.observability_sessionmaker", lambda: (lambda: fake))
    record = {
        "request_id": "abc123", "event": "hybrid_search", "function": "search",
        "status": "ok", "duration_ms": 1726.68, "result": {"count": 5},
    }
    obs._persist_rag_event(record)
    row = fake.added[0]
    assert row.request_id == "abc123" and row.event == "hybrid_search" and row.duration_ms == 1726.68
    detail = json.loads(row.detail)
    # 설정 스냅샷 전체 키 검증 — 일부 키 누락/오값도 잡히게 (CodeRabbit #188)
    from app.rag import config as rag_config
    assert detail["config"] == {
        "chunk_size": rag_config.CHUNK_SIZE,
        "chunk_overlap": rag_config.CHUNK_OVERLAP,
        "embedding_model": rag_config.EMBEDDING_MODEL,
        "embedding_dim": rag_config.EMBEDDING_DIM,
        "rerank_model": rag_config.RERANK_MODEL,
        "rrf_k": rag_config.RRF_K,
        "candidate_pool": rag_config.HYBRID_CANDIDATE_POOL_SIZE,
        "rerank_candidates": rag_config.HYBRID_RERANK_CANDIDATES,
    }


def test_persist_non_search_has_no_config(monkeypatch):
    """hybrid_search가 아니면 설정 스냅샷 없음(불필요)."""
    fake = _FakeSession()
    monkeypatch.setattr("app.core.observability_db.observability_sessionmaker", lambda: (lambda: fake))
    obs._persist_rag_event({"request_id": "r1", "event": "embed_query", "status": "ok", "duration_ms": 980.0})
    detail = json.loads(fake.added[0].detail)
    assert "config" not in detail


def test_persist_is_best_effort(monkeypatch):
    """관측 DB 장애가 검색 로깅으로 새지 않는다."""
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr("app.core.observability_db.observability_sessionmaker", _boom)
    obs._persist_rag_event({"request_id": "r1", "event": "embed_query"})  # 예외 없이 넘어가야


def test_write_log_masks_jsonl_and_calls_persist(tmp_path, monkeypatch):
    """_write_log는 JSONL을 마스킹해 기록하고(원문 유출 방지) 관측 적재도 호출한다.

    RAG_EVENT_QUEUE=0으로 **동기 경로**를 명시한다 (RPA-221) — 기본값은 큐 적재라
    _persist_rag_event가 워커 스레드에서 불린다. 큐 경로의 마스킹 보장은
    tests/test_rag_event_queue.py가 따로 검증한다.
    """
    monkeypatch.setenv("RAG_EVENT_QUEUE", "0")
    monkeypatch.setattr(obs.config, "LOG_DIR", tmp_path)
    called = {}
    monkeypatch.setattr(obs, "_persist_rag_event", lambda rec: called.setdefault("rec", rec))
    obs._write_log({"request_id": "r1", "event": "embed_query",
                    "args": {"q": {"len": 5, "preview": "메일 a@b.com"}}})
    text = list(tmp_path.glob("*.jsonl"))[0].read_text(encoding="utf-8")
    assert "r1" in text and "[EMAIL]" in text and "a@b.com" not in text  # JSONL도 마스킹
    assert called["rec"]["args"]["q"]["preview"] == "메일 [EMAIL]"        # persist엔 마스킹본 전달
