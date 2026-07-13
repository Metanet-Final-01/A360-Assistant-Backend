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
    assert "[NUM]" in masked["error_message"]
    assert record["args"]["query"]["preview"] == "문의 user@ex.com 관련"  # 원본 불변(깊은 복사)


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


def test_write_log_masks_jsonl_and_calls_persist(tmp_path, monkeypatch):
    """_write_log는 JSONL을 마스킹해 기록하고(원문 유출 방지) 관측 적재도 호출한다."""
    monkeypatch.setattr(obs.config, "LOG_DIR", tmp_path)
    called = {}
    monkeypatch.setattr(obs, "_persist_rag_event", lambda rec: called.setdefault("rec", rec))
    obs._write_log({"request_id": "r1", "event": "embed_query",
                    "args": {"q": {"len": 5, "preview": "메일 a@b.com"}}})
    text = list(tmp_path.glob("*.jsonl"))[0].read_text(encoding="utf-8")
    assert "r1" in text and "[EMAIL]" in text and "a@b.com" not in text  # JSONL도 마스킹
    assert called["rec"]["args"]["q"]["preview"] == "메일 [EMAIL]"        # persist엔 마스킹본 전달
