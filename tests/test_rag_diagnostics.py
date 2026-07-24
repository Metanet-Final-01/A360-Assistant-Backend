"""RAG 진단 공용 함수 테스트 (RPA-270) — debug·admin이 공유하는 판정.

service_status()는 외부 임베딩 API를 실호출하지 않고, run_live_probe()는 실호출하되 실패를
error_type으로만 분류해 반환한다(원문·DSN·credential 미노출).
"""

import json

import httpx

import app.services.rag_diagnostics as diag


class _FakeConn:
    def close(self):
        pass


class _FakeCluster:
    def health(self, request_timeout=None):
        return {"status": "green"}


class _FakeOS:
    cluster = _FakeCluster()


def _patch_infra(monkeypatch):
    import app.rag.store.db as db
    import app.rag.store.opensearch_client as osc

    monkeypatch.setattr(db, "connect", lambda *a, **k: _FakeConn())
    # _opensearch_status는 공유 클라이언트를 쓴다(churn 방지) → get_shared_client를 모킹
    monkeypatch.setattr(osc, "get_shared_client", lambda: _FakeOS())


# ── 분류기 (원문 대신 error_type) ──────────────────────────────────────────────
def test_classify_embedding_api_key_missing():
    assert diag._classify_embedding_error(RuntimeError("VOYAGE_API_KEY 환경변수가 필요합니다")) == "api_key_missing"


def test_classify_embedding_timeout():
    assert diag._classify_embedding_error(httpx.ConnectTimeout("x")) == "connection_timeout"


def test_classify_embedding_connect_failed():
    assert diag._classify_embedding_error(httpx.ConnectError("x")) == "connection_failed"


def test_classify_embedding_auth_and_rate_limit():
    req = httpx.Request("POST", "https://api.example/v1/embeddings")
    for code, expected in [(401, "authentication_failed"), (403, "authentication_failed"), (429, "rate_limited"), (500, "provider_error")]:
        resp = httpx.Response(code, request=req)
        exc = httpx.HTTPStatusError("x", request=req, response=resp)
        assert diag._classify_embedding_error(exc) == expected


def test_classify_db_dimension_and_timeout():
    assert diag._classify_db_error(RuntimeError("dimension 8 does not match column vector(1024)")) == "dimension_mismatch"
    assert diag._classify_db_error(RuntimeError("connection timeout expired")) == "connection_timeout"


# ── service_status: 외부 임베딩 실호출 없음 ────────────────────────────────────
def test_service_status_makes_no_external_embedding_call(monkeypatch):
    _patch_infra(monkeypatch)
    import app.rag.retrieval.embed as embed_mod

    called = {"n": 0}
    monkeypatch.setattr(embed_mod, "embed_query_live", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or [0.1])

    s = diag.service_status()
    assert called["n"] == 0  # 외부 임베딩 API 실호출 없음
    assert s["database"]["reachable"] is True
    assert s["opensearch"]["reachable"] is True
    assert "api_key_configured" in s["embedding"]


def test_service_status_whitespace_key_is_unconfigured(monkeypatch):
    _patch_infra(monkeypatch)
    import app.rag.config as rag_config

    monkeypatch.setattr(rag_config, "EMBEDDING_PROVIDER", "voyage")
    monkeypatch.setattr(rag_config, "VOYAGE_API_KEY", "   ")  # 공백-only 키는 무효

    s = diag.service_status()
    assert s["embedding"]["api_key_configured"] is False
    assert s["reranker"]["api_key_configured"] is False


# ── run_live_probe: 단계별 판정 + 정제 ────────────────────────────────────────
def test_run_live_probe_all_reachable(monkeypatch):
    _patch_infra(monkeypatch)
    import app.rag.retrieval.embed as embed_mod
    import app.rag.store.db as db

    monkeypatch.setattr(embed_mod, "embed_query_live", lambda text, **k: [0.1] * 8)
    monkeypatch.setattr(db, "search", lambda conn, vec, limit=1: [{"id": "d1"}])

    r = diag.run_live_probe()
    assert r["embedding"] == {"reachable": True, "dim": 8}
    assert r["vector_query"] == {"reachable": True, "hits": 1}
    assert r["opensearch"]["reachable"] is True


def test_run_live_probe_embedding_failure_redacted_and_skips_vector(monkeypatch):
    _patch_infra(monkeypatch)
    import app.rag.retrieval.embed as embed_mod
    import app.rag.store.db as db

    def _boom(text, **k):
        raise RuntimeError("VOYAGE_API_KEY 환경변수가 필요합니다")

    monkeypatch.setattr(embed_mod, "embed_query_live", _boom)
    monkeypatch.setattr(db, "search", lambda *a, **k: (_ for _ in ()).throw(AssertionError("불려선 안 됨")))

    r = diag.run_live_probe()
    assert r["embedding"] == {"reachable": False, "error_type": "api_key_missing"}
    assert r["vector_query"] == {"skipped": "embedding_unreachable"}
    # 원문(환경변수 이름 등)이 응답에 새지 않는다 — error_type만
    assert "VOYAGE_API_KEY" not in json.dumps(r, ensure_ascii=False)


def test_run_live_probe_vector_failure_error_type(monkeypatch):
    _patch_infra(monkeypatch)
    import app.rag.retrieval.embed as embed_mod
    import app.rag.store.db as db

    monkeypatch.setattr(embed_mod, "embed_query_live", lambda text, **k: [0.1] * 8)

    def _boom(*a, **k):
        raise RuntimeError("dimension 8 does not match column vector(1024)")

    monkeypatch.setattr(db, "search", _boom)

    r = diag.run_live_probe()
    assert r["embedding"]["reachable"] is True
    assert r["vector_query"] == {"reachable": False, "error_type": "dimension_mismatch"}
    assert "does not match" not in json.dumps(r, ensure_ascii=False)  # 원문 미노출
