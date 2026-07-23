"""/api/rag/debug/status?probe=1 실제 도달성 프로브 테스트 (RPA-232).

기본(probe=false)은 키 설정 여부만 본다 — 키가 있어도 무효·egress 차단이면 검색은 죽는데
초록으로 보이는 "가짜 초록불"이라, probe=1은 실제 임베딩 호출 + pgvector 쿼리까지 태워
SEARCH_UNAVAILABLE의 원인 단계(임베딩 vs 벡터쿼리)를 격리한다.
"""

from fastapi.testclient import TestClient

from app.main import app


class _FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeCluster:
    def health(self, request_timeout=None):
        return {"status": "green"}


class _FakeOSClient:
    cluster = _FakeCluster()


def _patch_service_checks(monkeypatch):
    """상단의 DB/OpenSearch 연결 체크가 실제 인프라를 때리지 않게 고정한다."""
    import app.rag.store.db as db
    import app.rag.store.opensearch_client as osc

    monkeypatch.setattr(db, "connect", lambda: _FakeConn())
    monkeypatch.setattr(osc, "connect", lambda: _FakeOSClient())


def test_status_without_probe_only_reports_key_configured(monkeypatch):
    _patch_service_checks(monkeypatch)
    with TestClient(app) as c:
        body = c.get("/api/rag/debug/status").json()

    emb = body["embedding"]
    assert "api_key_configured" in emb
    assert "live_check" in emb          # "키만 봤다"는 명시
    assert "reachable" not in emb       # 실제 호출은 안 함
    assert "vector_query" not in body   # 프로브 미실행


def test_probe_reports_embedding_and_vector_reachable(monkeypatch):
    _patch_service_checks(monkeypatch)
    import app.rag.retrieval.embed as embed_mod
    import app.rag.store.db as db

    monkeypatch.setattr(embed_mod, "embed_query", lambda q: [0.1] * 8)
    monkeypatch.setattr(db, "search", lambda conn, vec, limit=1: [{"id": "doc-1"}])

    with TestClient(app) as c:
        body = c.get("/api/rag/debug/status", params={"probe": "1"}).json()

    assert body["embedding"]["reachable"] is True
    assert body["embedding"]["dim"] == 8
    assert body["vector_query"] == {"reachable": True, "hits": 1}


def test_probe_embedding_failure_surfaces_error_and_skips_vector(monkeypatch):
    _patch_service_checks(monkeypatch)
    import app.rag.retrieval.embed as embed_mod
    import app.rag.store.db as db

    def _boom(q):
        raise RuntimeError("VOYAGE egress blocked")

    monkeypatch.setattr(embed_mod, "embed_query", _boom)
    # 임베딩이 죽으면 벡터쿼리는 아예 시도하면 안 된다 — 호출되면 실패시켜 그 계약을 고정.
    monkeypatch.setattr(db, "search", lambda *a, **k: (_ for _ in ()).throw(AssertionError("불려선 안 됨")))

    with TestClient(app) as c:
        body = c.get("/api/rag/debug/status", params={"probe": "1"}).json()

    assert body["embedding"]["reachable"] is False
    assert "VOYAGE egress blocked" in body["embedding"]["error"]
    assert body["vector_query"] == {"skipped": "임베딩 실패로 pgvector 쿼리 생략"}


def test_probe_vector_query_failure_surfaces_error(monkeypatch):
    _patch_service_checks(monkeypatch)
    import app.rag.retrieval.embed as embed_mod
    import app.rag.store.db as db

    monkeypatch.setattr(embed_mod, "embed_query", lambda q: [0.1] * 8)

    def _boom(*a, **k):
        raise RuntimeError("dimension 8 does not match column vector(1024)")

    monkeypatch.setattr(db, "search", _boom)

    with TestClient(app) as c:
        body = c.get("/api/rag/debug/status", params={"probe": "1"}).json()

    assert body["embedding"]["reachable"] is True
    assert body["vector_query"]["reachable"] is False
    assert "does not match" in body["vector_query"]["error"]
