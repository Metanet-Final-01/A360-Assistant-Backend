"""RAG 검색 API 에러 응답 형식 테스트 (RPA-72).

공개 엔드포인트라 에러는 표준 {code,message}로 나가야 하고, 내부 예외 문자열은
클라이언트에 노출되면 안 된다(정보 누출 방지). 원인은 서버 로그로만 남는다.
"""

from fastapi.testclient import TestClient

from app.main import app


def test_search_db_failure_returns_code_message(monkeypatch):
    import app.rag.store.db as db

    def _boom():
        raise RuntimeError("connection refused at 10.0.0.5:5432")

    monkeypatch.setattr(db, "connect", _boom)
    with TestClient(app) as c:
        r = c.get("/api/rag/search", params={"q": "엑셀"})

    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["code"] == "SEARCH_STORE_UNAVAILABLE"  # raw 문자열 아님
    assert "message" in detail
    assert "connection refused" not in detail["message"]  # 내부 예외 비노출


def test_search_hybrid_failure_returns_code_message(monkeypatch):
    import app.rag.store.db as db
    import app.rag.store.opensearch_client as osc
    import app.rag.retrieval.hybrid_search as hs

    monkeypatch.setattr(db, "connect", lambda: _FakeConn())
    monkeypatch.setattr(osc, "connect", lambda: object())

    def _boom(*a, **k):
        raise RuntimeError("opensearch index missing")

    monkeypatch.setattr(hs, "search", _boom)
    with TestClient(app) as c:
        r = c.get("/api/rag/search", params={"q": "엑셀"})

    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["code"] == "SEARCH_UNAVAILABLE"
    assert "opensearch index missing" not in detail["message"]


def test_search_opensearch_connect_failure_standardized(monkeypatch):
    """OpenSearch 연결 실패도 표준 {code,message} + conn은 닫힌다 (CodeRabbit)."""
    import app.rag.store.db as db
    import app.rag.store.opensearch_client as osc

    conn = _FakeConn()
    monkeypatch.setattr(db, "connect", lambda: conn)

    def _boom():
        raise RuntimeError("opensearch unreachable at 10.0.0.9:9200")

    monkeypatch.setattr(osc, "connect", _boom)
    with TestClient(app) as c:
        r = c.get("/api/rag/search", params={"q": "엑셀"})

    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["code"] == "SEARCH_STORE_UNAVAILABLE"
    assert "opensearch unreachable" not in detail["message"]
    assert conn.closed  # 열어둔 DB 커넥션은 반드시 닫힌다


def test_search_non_runtime_error_standardized(monkeypatch):
    """hybrid_search가 RuntimeError 외 예외(psycopg/OpenSearch 등)를 던져도 표준화된다."""
    import app.rag.store.db as db
    import app.rag.store.opensearch_client as osc
    import app.rag.retrieval.hybrid_search as hs

    monkeypatch.setattr(db, "connect", lambda: _FakeConn())
    monkeypatch.setattr(osc, "connect", lambda: object())

    def _boom(*a, **k):
        raise ValueError("unexpected non-RuntimeError")  # 비-RuntimeError

    monkeypatch.setattr(hs, "search", _boom)
    with TestClient(app) as c:
        r = c.get("/api/rag/search", params={"q": "엑셀"})

    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "SEARCH_UNAVAILABLE"


class _FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True
