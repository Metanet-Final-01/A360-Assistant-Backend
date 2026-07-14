"""BM25 실패 시 무음 저하 방지 회귀 (RPA-156).

BM25(OpenSearch)가 실패하면 하이브리드 검색은 dense-only로 저하되는데, 이전엔 그 실패를
결과 필드에만 남기고 **로그를 안 남겨** "조용히 반쪽으로 도는" 상태를 운영이 몰랐다. 이제
sync/async 두 경로 모두 logger.warning으로 남긴다 — 여기서 그걸 못박는다.
"""

import asyncio
import logging

import app.rag.retrieval.hybrid_search as hs


def _hit(doc_id):
    return {"id": doc_id, "title": doc_id, "content": doc_id}


def test_sync_bm25_failure_logs_and_degrades(monkeypatch, caplog):
    monkeypatch.setattr(hs, "embed_query", lambda q: [0.1, 0.2])
    monkeypatch.setattr(hs.db, "search", lambda conn, emb, limit: [_hit("a"), _hit("b")])

    def _boom(client, query, size):
        raise RuntimeError("connection refused to opensearch")

    monkeypatch.setattr(hs.opensearch_client, "keyword_search", _boom)

    with caplog.at_level(logging.WARNING):
        results = hs.search(None, None, "q", limit=5, mode="hybrid")

    assert results, "BM25 실패해도 dense 결과는 반환돼야 한다(전체 실패 아님)"
    assert any("BM25 검색 실패" in r.message for r in caplog.records), "무음 저하 — 로그가 남지 않음"


def test_async_bm25_failure_logs_and_degrades(monkeypatch, caplog):
    """async 경로도 동일 — pytest-asyncio 미설치라 asyncio.run으로 직접 구동(스킵 방지)."""

    async def _emb(q, client=None):
        return [0.1, 0.2]

    async def _vsearch(conn, emb, limit):
        return [_hit("a"), _hit("b")]

    async def _boom(client, query, size):
        raise RuntimeError("connection refused to opensearch")

    monkeypatch.setattr(hs, "embed_query_async", _emb)
    monkeypatch.setattr(hs.db, "search_async", _vsearch)
    monkeypatch.setattr(hs.opensearch_client, "keyword_search_async", _boom)

    with caplog.at_level(logging.WARNING):
        results = asyncio.run(hs.search_async(None, None, "q", limit=5, mode="hybrid"))

    assert results
    assert any("BM25 검색 실패(async)" in r.message for r in caplog.records)
