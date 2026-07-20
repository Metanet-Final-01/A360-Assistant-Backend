"""RAG 검색 API — A360 패키지/액션 지식 검색 (FR-07)."""

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rag", tags=["rag"])


@router.get("/search")
async def rag_search(
    q: str = Query(min_length=1, max_length=500, description="검색어"),
    limit: int = Query(5, ge=1, le=50, description="반환 개수"),
    mode: Literal["vector", "hybrid", "hybrid_rerank"] = "hybrid_rerank",
) -> dict:
    """A360 패키지/액션 지식 하이브리드(RRF) + Voyage Reranker 검색.

    app/rag 파이프라인으로 적재된 데이터를 사용한다.
    mode: vector(기존 방식) / hybrid(RRF만) / hybrid_rerank(기본, RRF+Voyage Reranker)
    검증 제약(q 길이·limit 범위·mode 값)은 과도한 검색/리랭킹 비용을 막는다.

    비동기 라우트 + 재사용 커넥션 풀. 원래 동기(def) 라우트를 비동기로만 바꿨을 때는
    부하테스트(k6, 50 VU)로 재보니 개선이 없었다 — 진짜 병목은 스레드풀 대기가 아니라
    요청마다 새 Postgres 연결·새 httpx 클라이언트를 맺고 버리던 것이었다(평시 몇 ms인
    pgvector 쿼리가 부하 중 평균 1.6초까지 늘어난 게 단서). app/rag/store/pool.py의
    앱 전역 풀/클라이언트(lifespan에서 한 번만 열고 재사용)로 바꿔 이 병목을 없앤다.
    적재(ingest)·debug 엔드포인트는 이 문제의 대상이 아니었으므로 그대로 동기.
    """
    from app.rag.retrieval.hybrid_search import search_async as hybrid_search_async
    from app.rag.store import pool as rag_pool
    from app.services.retrieval_params import load_active_params

    # request_id는 log_http_requests 미들웨어가 이미 생성해둠 — 여기서 다시 만들지 않는다
    # 공개 엔드포인트라 내부 예외 문자열은 클라이언트에 노출하지 않는다(정보 누출 방지) —
    # 표준 {code,message}만 돌려주고 원인은 서버 로그로 남긴다.
    pg_pool = rag_pool.get_pg_pool()
    if pg_pool is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "SEARCH_STORE_UNAVAILABLE", "message": "검색 저장소에 연결할 수 없습니다."},
        )
    try:
        async with pg_pool.connection() as conn:
            try:
                results = await hybrid_search_async(
                    conn, rag_pool.get_opensearch_client(), q, limit=limit, mode=mode,
                    http_client=rag_pool.get_external_client(),
                    params=load_active_params(),  # DB 오버라이드/.env 폴백 — 무중단 튜닝 (RPA-149)
                )
            except Exception as e:  # RuntimeError뿐 아니라 OpenSearch/psycopg 등 어떤 예외도 표준화
                logger.exception("RAG 하이브리드 검색 실패")
                raise HTTPException(
                    status_code=503,
                    detail={"code": "SEARCH_UNAVAILABLE", "message": "검색 처리 중 오류가 발생했습니다."},
                ) from e
    except HTTPException:
        raise
    except Exception as e:  # 풀에서 커넥션을 못 받아온 경우(고갈·DB 다운 등)
        logger.exception("RAG 검색 DB 연결 실패")
        raise HTTPException(
            status_code=503,
            detail={"code": "SEARCH_STORE_UNAVAILABLE", "message": "검색 저장소에 연결할 수 없습니다."},
        ) from e
    return {"query": q, "results": results}
