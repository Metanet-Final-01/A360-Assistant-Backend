"""하이브리드 검색: pgvector 코사인 유사도 + OpenSearch BM25를 RRF로 융합 후 Voyage로 재정렬한다.

mode:
  vector        — 기존 동작 그대로 (pgvector 코사인 유사도 단독)
  hybrid        — 벡터 + BM25를 RRF로 융합
  hybrid_rerank — hybrid 결과를 Voyage rerank-2.5-lite로 재정렬 (기본값)
"""

import asyncio
import logging

from ..observability import log_call
from ..store import db, opensearch_client
from .embed import embed_query, embed_query_async
from .params import RetrievalParams
from .rerank import rerank as voyage_rerank
from .rerank import rerank_async as voyage_rerank_async

logger = logging.getLogger(__name__)


def reciprocal_rank_fusion(
    rank_lists: list[list[str]], k: int, weights: list[float] | None = None
) -> dict[str, float]:
    """rank_lists: 각 검색 방식(branch)의 결과 id를 순위(1위부터)대로 나열한 리스트들.

    score(d) = sum over branches containing d of weight_branch / (k + rank_in_branch(d)).
    특정 branch 결과에 없는 문서는 그 branch의 항을 0으로 취급한다(페널티 없음).
    weights 미지정 시 모든 branch를 1.0으로 동일 가중(기존 동작). 지정 시 branch 수와
    길이가 같아야 한다 — 어긋나면 zip이 조용히 잘라 신호가 유실되므로 명시적으로 막는다.
    """
    if weights is None:
        weights = [1.0] * len(rank_lists)
    elif len(weights) != len(rank_lists):
        raise ValueError(
            f"weights 길이({len(weights)})가 rank_lists 개수({len(rank_lists)})와 다릅니다"
        )
    scores: dict[str, float] = {}
    for ids, weight in zip(rank_lists, weights):
        for index, doc_id in enumerate(ids):
            rank = index + 1
            scores[doc_id] = scores.get(doc_id, 0.0) + weight / (k + rank)
    return scores


def _fuse_candidates(
    vector_hits: list[dict], bm25_hits: list[dict], bm25_error: str | None, params: RetrievalParams
) -> list[dict]:
    """RRF 융합 + 후보 리스트 조립 — sync/async 두 검색 경로가 공유하는 순수 로직(I/O 없음)."""
    vector_ids = [h["id"] for h in vector_hits]
    bm25_ids = [h["id"] for h in bm25_hits]
    rrf_scores = reciprocal_rank_fusion(
        [vector_ids, bm25_ids], k=params.rrf_k,
        weights=[params.vector_weight, params.bm25_weight],
    )

    dense_rank = {doc_id: i + 1 for i, doc_id in enumerate(vector_ids)}
    bm25_rank = {doc_id: i + 1 for i, doc_id in enumerate(bm25_ids)}

    # 콘텐츠 조회용 lookup: 두 branch 모두에서 온 필드를 합치되, pgvector 쪽이 스키마 전체(parent_id 등)를 갖고 있으니 우선한다.
    lookup: dict[str, dict] = {h["id"]: h for h in bm25_hits}
    lookup.update({h["id"]: h for h in vector_hits})

    def _retrieval_source(doc_id: str) -> str:
        if doc_id in dense_rank and doc_id in bm25_rank:
            return "hybrid_both"
        if doc_id in dense_rank:
            return "hybrid_dense_only"
        return "hybrid_bm25_only"

    fused_ids = sorted(rrf_scores.keys(), key=lambda d: (-rrf_scores[d], d))
    fused_ids = [d for d in fused_ids if d in lookup][: params.rerank_candidates]
    return [
        {
            **lookup[doc_id],
            "dense_rank": dense_rank.get(doc_id),
            "bm25_rank": bm25_rank.get(doc_id),
            "rrf_score": rrf_scores[doc_id],
            "retrieval_source": _retrieval_source(doc_id),
            "bm25_available": bm25_error is None,
            **({"bm25_error": bm25_error} if bm25_error else {}),
        }
        for doc_id in fused_ids
    ]


def _bm25_health(results: list[dict]) -> bool | None:
    """BM25 **호출 성공** 집계 — 필드가 없는 결과(vector 모드)는 None으로 구분한다 (RPA-211).

    True=예외 없음 / False=저하(OpenSearch 실패) / None=해당 없음(BM25를 부르지 않음).
    셋을 뭉개면 "저하율"이 거짓말을 한다.

    ⚠️ 이건 "예외가 없었다"이지 "검색에 기여했다"가 아니다 — 색인이 비어 200 OK로 0건이
    와도 True다. 실제 기여는 _bm25_contributed()로 따로 본다 (RPA-249).
    """
    flags = [r["bm25_available"] for r in results if "bm25_available" in r]
    return all(flags) if flags else None


def _bm25_contributed(results: list[dict]) -> bool | None:
    """BM25가 **실제로 후보를 냈나** — 융합 결과에 bm25_rank가 하나라도 있으면 True (RPA-249).

    왜 _bm25_health와 따로 두나: `bm25_available`은 예외 유무만 본다. 배포에서 OpenSearch
    인덱스가 비어 있으면 200 OK로 0건이 오고, 그러면 available=True인 채 검색은 dense-only로
    조용히 반쪽이 된다(실측). 지표가 "정상"이라고 말하는데 품질은 절반인 상태다.
    가드는 동작이 읽는 것을 읽어야 한다 — 예외가 아니라 **기여**를 본다.

    True=기여함 / False=호출했으나 0건(색인 비었거나 매칭 없음) / None=해당 없음(vector 모드).
    """
    scored = [r for r in results if "bm25_available" in r]
    if not scored:
        return None
    return any(r.get("bm25_rank") is not None for r in scored)


def _candidate_summary(item: dict) -> dict:
    """관측 로그용 후보 요약(RPA-129) — 어떤 문서가 몇 점으로 뽑혔나. 카탈로그 콘텐츠라
    PII 아님(검색어와 달리 마스킹 불필요). 점수는 rerank 우선, 없으면 RRF."""
    score = item.get("rerank_score")
    if score is None:
        score = item.get("rrf_score")
    return {
        "title": item.get("title"),
        "package": item.get("package"),
        "action": item.get("action"),
        "score": round(score, 4) if isinstance(score, (int, float)) else None,
        "source": item.get("retrieval_source"),
    }


@log_call(
    "hybrid_search",
    capture_args=("query", "limit", "mode"),
    capture_result=lambda r: {
        "count": len(r),
        "retrieval_sources": [item.get("retrieval_source") for item in r],
        # 🔴 BM25 저하 여부 (RPA-211). 이게 없어 "지난 주에 몇 번 저하됐나"를
        # 사후에 답할 수 없었다 — retrieval_sources는 문서별 출처라 저하 표시가 아니다.
        # /health는 도달성을 보고 검색은 질의 성공을 본다 — 둘은 다르다(CONVENTIONS §9).
        # ⚠️ mode="vector"는 BM25를 **아예 부르지 않아** 이 필드가 없다. 기본값 True로 집계하면
        #    "호출도 안 했는데 정상"으로 기록돼 저하 지표가 왜곡된다(#290 리뷰).
        #    필드가 하나도 없으면 None = "해당 없음"으로 남긴다.
        "bm25_available": _bm25_health(r),
        # 🔴 예외 유무(위)와 **실제 기여**는 다르다 (RPA-249) — 색인이 비면 200 OK로 0건이
        # 와서 available=True인 채 dense-only 반쪽이 된다. 배포에서 실제로 그 상태였고
        # 어떤 지표도 울리지 않았다. contributed=False가 이어지면 색인 이상으로 읽는다.
        "bm25_contributed": _bm25_contributed(r),
        "reranked": any("rerank_score" in item for item in r),
        # 실제로 뭐가 뽑혔나(상위 5) — "왜 이 스텝만 uncovered인지" 재구성·추천 근거(FR-11)
        "candidates": [_candidate_summary(item) for item in r[:5]],
    },
)
def search(
    pg_conn, os_client, query: str, limit: int = 5, mode: str = "hybrid_rerank",
    params: RetrievalParams | None = None,
) -> list[dict]:
    params = params or RetrievalParams.from_config()
    try:
        query_embedding = embed_query(query)
    except RuntimeError as e:
        raise RuntimeError(f"임베딩 설정 오류: {e}")

    pool = params.candidate_pool_size
    vector_hits = db.search(pg_conn, query_embedding, limit=pool if mode != "vector" else limit)

    if mode == "vector":
        return [{**h, "retrieval_source": "vector"} for h in vector_hits[:limit]]

    bm25_hits: list[dict] = []
    bm25_error: str | None = None
    try:
        bm25_hits = opensearch_client.keyword_search(os_client, query, size=pool)
    except Exception as e:
        # BM25는 보강 신호이므로, OpenSearch가 응답하지 않으면 벡터 단독 검색으로 저하시킨다.
        # 단, 저하 여부를 결과에 남겨야 "BM25가 원래 안 잡힌 건지 장애로 빠진 건지" 구분 가능하다.
        # 무음 저하 방지(RPA-156): 결과 필드뿐 아니라 로그로도 남겨 "조용히 dense-only로 도는" 상태를
        # 운영이 알아채게 한다 — Bonsai가 살아있어도 앱-전역 클라이언트가 죽으면 여기로 빠진다.
        logger.warning("BM25 검색 실패 — dense-only로 저하: %s", e)
        bm25_error = str(e)

    candidates = _fuse_candidates(vector_hits, bm25_hits, bm25_error, params)

    if mode == "hybrid" or not candidates:
        return [{**c, "reranked": False} for c in candidates[:limit]]

    # title을 같이 넣어야 reranker가 문맥(어느 문서 소속인지)을 보고 판단한다 —
    # 청킹 안 된 문서는 content에 title이 없어서 이걸 빼면 reranker가 맥락 없이 본문만 본다.
    rerank_inputs = [f"{c['title']}\n\n{c['content']}" for c in candidates]
    try:
        reranked = voyage_rerank(query, rerank_inputs, top_k=min(limit, len(candidates)))
    except RuntimeError as e:
        # VOYAGE_API_KEY 미설정 등 — 재정렬 없이 RRF 순서 그대로 반환하되, 폴백 여부와 사유를 명시한다
        return [{**c, "reranked": False, "rerank_fallback_reason": str(e)} for c in candidates[:limit]]

    return [
        {**candidates[item["index"]], "rerank_score": item["relevance_score"], "reranked": True}
        for item in reranked
    ]


@log_call(
    "hybrid_search",
    capture_args=("query", "limit", "mode"),
    capture_result=lambda r: {
        "count": len(r),
        "retrieval_sources": [item.get("retrieval_source") for item in r],
        # ⚠️ mode="vector"는 BM25를 **아예 부르지 않아** 이 필드가 없다. 기본값 True로 집계하면
        #    "호출도 안 했는데 정상"으로 기록돼 저하 지표가 왜곡된다(#290 리뷰).
        #    필드가 하나도 없으면 None = "해당 없음"으로 남긴다.
        "bm25_available": _bm25_health(r),
        # 🔴 예외 유무(위)와 **실제 기여**는 다르다 (RPA-249) — 색인이 비면 200 OK로 0건이
        # 와서 available=True인 채 dense-only 반쪽이 된다. 배포에서 실제로 그 상태였고
        # 어떤 지표도 울리지 않았다. contributed=False가 이어지면 색인 이상으로 읽는다.
        "bm25_contributed": _bm25_contributed(r),
        "reranked": any("rerank_score" in item for item in r),
    },
)
async def search_async(
    pg_conn, os_client, query: str, limit: int = 5, mode: str = "hybrid_rerank",
    http_client=None, params: RetrievalParams | None = None,
) -> list[dict]:
    """search()의 비동기 버전 — /api/rag/search 전용. 벡터 검색(임베딩→pgvector)과
    BM25 검색은 서로 입력이 다른 독립 조회라(BM25는 임베딩이 필요 없다) asyncio.gather로
    동시에 돌린다 — 스레드풀 대기만 없애는 게 아니라 임베딩+BM25 두 단계의 지연시간을
    겹쳐서 실제 critical path도 줄인다(sync 버전은 완전 순차).

    http_client는 app/rag/store/pool.py의 앱 전역 재사용 httpx.AsyncClient(임베딩/
    리랭크 외부 API용) — 매 요청 새 클라이언트를 열고 닫던 게 부하테스트로 확인된
    진짜 병목이라 여기로 그대로 흘려보낸다."""
    params = params or RetrievalParams.from_config()
    pool = params.candidate_pool_size

    async def _vector_branch() -> list[dict]:
        try:
            query_embedding = await embed_query_async(query, client=http_client)
        except RuntimeError as e:
            raise RuntimeError(f"임베딩 설정 오류: {e}") from e
        return await db.search_async(pg_conn, query_embedding, limit=pool if mode != "vector" else limit)

    if mode == "vector":
        vector_hits = await _vector_branch()
        return [{**h, "retrieval_source": "vector"} for h in vector_hits[:limit]]

    async def _bm25_branch() -> tuple[list[dict], str | None]:
        try:
            return await opensearch_client.keyword_search_async(os_client, query, size=pool), None
        except Exception as e:  # noqa: BLE001 — sync 버전과 동일하게 BM25 실패는 저하만, 전체 실패 아님
            logger.warning("BM25 검색 실패(async) — dense-only로 저하: %s", e)  # 무음 저하 방지(RPA-156)
            return [], str(e)

    vector_hits, (bm25_hits, bm25_error) = await asyncio.gather(_vector_branch(), _bm25_branch())

    candidates = _fuse_candidates(vector_hits, bm25_hits, bm25_error, params)

    if mode == "hybrid" or not candidates:
        return [{**c, "reranked": False} for c in candidates[:limit]]

    rerank_inputs = [f"{c['title']}\n\n{c['content']}" for c in candidates]
    try:
        reranked = await voyage_rerank_async(
            query, rerank_inputs, top_k=min(limit, len(candidates)), client=http_client
        )
    except RuntimeError as e:
        return [{**c, "reranked": False, "rerank_fallback_reason": str(e)} for c in candidates[:limit]]

    return [
        {**candidates[item["index"]], "rerank_score": item["relevance_score"], "reranked": True}
        for item in reranked
    ]
