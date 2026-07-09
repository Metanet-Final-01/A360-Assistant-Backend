"""RAG 검색 API — A360 패키지/액션 지식 검색 (FR-07)."""

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

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
    # 공개 엔드포인트라 내부 예외 문자열은 클라이언트에 노출하지 않는다(정보 누출 방지) —
    # 표준 {code,message}만 돌려주고 원인은 서버 로그로 남긴다.
    try:
        conn = db.connect()
    except Exception as e:
        logger.exception("RAG 검색 DB 연결 실패")
        raise HTTPException(
            status_code=503,
            detail={"code": "SEARCH_STORE_UNAVAILABLE", "message": "검색 저장소에 연결할 수 없습니다."},
        ) from e
    try:
        try:
            os_client = opensearch_client.connect()
        except Exception as e:  # OpenSearch 연결 실패도 표준 포맷으로 (outer try엔 except가 없었음)
            logger.exception("RAG 검색 OpenSearch 연결 실패")
            raise HTTPException(
                status_code=503,
                detail={"code": "SEARCH_STORE_UNAVAILABLE", "message": "검색 저장소에 연결할 수 없습니다."},
            ) from e
        try:
            results = hybrid_search(conn, os_client, q, limit=limit, mode=mode)
        except Exception as e:  # RuntimeError뿐 아니라 OpenSearch/psycopg 등 어떤 예외도 표준화
            logger.exception("RAG 하이브리드 검색 실패")
            raise HTTPException(
                status_code=503,
                detail={"code": "SEARCH_UNAVAILABLE", "message": "검색 처리 중 오류가 발생했습니다."},
            ) from e
    finally:
        conn.close()
    return {"query": q, "results": results}
