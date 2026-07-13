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


def test_persist_masks_query_preview_and_adds_config(monkeypatch):
    """검색어 preview 마스킹 + hybrid_search엔 설정 스냅샷(chunk_size 등) 포함."""
    fake = _FakeSession()
    monkeypatch.setattr("app.core.observability_db.observability_sessionmaker", lambda: (lambda: fake))
    record = {
        "request_id": "abc123", "event": "hybrid_search", "function": "search",
        "status": "ok", "duration_ms": 1726.68,
        "args": {"query": {"len": 20, "preview": "문의 user@ex.com 관련"}},
        "result": {"count": 5},
    }
    obs._persist_rag_event(record)
    assert len(fake.added) == 1
    row = fake.added[0]
    assert row.request_id == "abc123" and row.event == "hybrid_search" and row.duration_ms == 1726.68
    detail = json.loads(row.detail)
    assert detail["args"]["query"]["preview"] == "문의 [EMAIL] 관련"       # 마스킹됨
    assert detail["config"]["chunk_size"] and detail["config"]["rerank_model"]  # 설정 스냅샷


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


def test_write_log_still_writes_jsonl(tmp_path, monkeypatch):
    """_write_log는 JSONL 기록을 유지하면서 관측 적재를 추가로 호출한다."""
    monkeypatch.setattr(obs.config, "LOG_DIR", tmp_path)
    called = {}
    monkeypatch.setattr(obs, "_persist_rag_event", lambda rec: called.setdefault("rec", rec))
    obs._write_log({"request_id": "r1", "event": "embed_query"})
    files = list(tmp_path.glob("*.jsonl"))
    assert files and "r1" in files[0].read_text(encoding="utf-8")  # JSONL 여전히 기록
    assert called["rec"]["request_id"] == "r1"                     # 관측 적재도 호출
