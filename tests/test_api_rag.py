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


class _FakeConn:
    def close(self):
        pass
