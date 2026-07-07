"""RAG 검색 API — A360 패키지/액션 지식 검색 (FR-07)."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/rag", tags=["rag"])


@router.get("/search")
def rag_search(
    q: str = Query(min_length=1, max_length=500, description="검색어"),
    limit: int = Query(5, ge=1, le=50, description="반환 개수"),
    mode: Literal["vector", "hybrid", "hybrid_rerank"] = "hybrid_rerank",
) -> dict:
    """A360 패키지/액션 지식 하이브리드(RRF) + Voyage Reranker 검색.

    app/rag 파이프라인으로 적재된 데이터를 사용한다.
    mode: vector(기존 방식) / hybrid(RRF만) / hybrid_rerank(기본, RRF+Voyage Reranker)
    검증 제약(q 길이·limit 범위·mode 값)은 과도한 검색/리랭킹 비용을 막는다.
    """
    from app.rag.store import db, opensearch_client
    from app.rag.retrieval.hybrid_search import search as hybrid_search

    # request_id는 log_http_requests 미들웨어가 이미 생성해둠 — 여기서 다시 만들지 않는다
    try:
        conn = db.connect()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DB 연결 실패: {e}")
    try:
        os_client = opensearch_client.connect()
        try:
            results = hybrid_search(conn, os_client, q, limit=limit, mode=mode)
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
    finally:
        conn.close()
    return {"query": q, "results": results}
