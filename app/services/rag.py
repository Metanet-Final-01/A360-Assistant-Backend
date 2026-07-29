"""Agent 담당에 노출하는 RAG 검색 서비스 진입점 (docs/INTERFACES.md 계약).

app/rag/의 실제 구현(하이브리드 RRF + Voyage Reranker, RPA-9)을 감싸는 얇은
wrapper다. Agent 쪽(app/agent/retrieval.py의 Retriever 구현체)은 이 함수 하나만
알면 되고, app/rag/ 내부 구조가 바뀌어도 이 시그니처만 유지되면 영향받지 않는다.
"""

from app.rag import config
from app.rag.observability import log_event
from app.rag.retrieval.hybrid_search import search as _hybrid_search
from app.rag.store import db, opensearch_client
from app.services import rag_cache
from app.services.retrieval_params import load_active_params


# collapse_by_action의 과다인출 배수. 코퍼스는 액션 1건당 머리 1행 + 이어짐 조각 N행이라
# (action_schema 1,375행 = 고유 1,200 + 조각 175), k개를 그대로 뽑으면 한 액션의 조각이
# 여러 칸을 먹어 실효 k가 줄어든다(실측 최악: Email/Send 4조각·Terminal Emulator/Connect 4조각).
# 3배면 최악의 4조각짜리가 섞여도 k개 고유 액션이 남는다. 리랭커 입력이 rerank_candidates(20)로
# 잘리므로 이보다 더 키워도 얻는 게 없다.
_COLLAPSE_OVERFETCH = 3


def search_actions(
    query: str,
    k: int = 5,
    source_types: list[str] | None = None,
    pushdown: bool = False,
    collapse_by_action: bool = False,
) -> list[dict]:
    """pgvector + OpenSearch 하이브리드(RRF) + Voyage Reranker 검색.

    반환 항목(최소): id, source_type, package_name, action_name, title, url, content, score.
    score는 재정렬됐으면 rerank_score, 아니면 rrf_score/코사인 유사도 순으로 채워진다
    (여러 검색 단계의 진단 필드 dense_rank/bm25_rank/retrieval_source 등도 함께 담겨 있다).

    source_types를 주면 (예: ["action_schema"]) 그 소스 타입만 남기고 상위 k개를 반환한다.
    거르는 **시점**이 pushdown으로 갈린다:

      pushdown=False (기본, v1~v3) — 후단 필터. k*3을 뽑아 융합·재정렬까지 끝낸 뒤
        파이썬에서 거른다. 실제로 필터가 보는 창은 min(k*3, rerank_candidates=20)이라,
        코퍼스 비중이 작은 타입은 k를 올려도 0건이 된다(doc_page가 90%를 차지한다).
      pushdown=True (v4) — 검색 SQL·BM25 질의에 타입을 내려보낸다. 후보 20건이 전부
        해당 타입이므로 창 낭비가 없고, k*3 과다인출 보정도 필요 없다.

    ⚠️ pushdown은 source_types가 있을 때만 의미가 있다. 없으면 무시하고 기본 경로로
    간다 — 캐시 키까지 기본과 같게 유지해야 v4가 전체 검색을 할 때 v1~v3와 캐시를 공유한다.

    collapse_by_action=True면 같은 (package_name, action_name)의 행을 **최고 점수 하나로
    접어** 상위 k개가 서로 다른 k개 액션이 되게 한다. 코퍼스가 긴 문서를 조각내 싣기
    때문이다 — `Email/Send`는 4조각(설명 / Subject·Attachment / From address / Tenant ID)이라
    "이메일 첨부 발송" 질의가 5칸 중 4칸을 한 액션으로 채운다. **행이 중복이라서가 아니라
    조각이라서** 접는 것이므로, 데이터를 지우면 파라미터 문서가 사라진다(접기는 검색
    결과에만 적용된다). package/action이 없는 행(doc_page 등)은 접지 않고 그대로 통과시킨다
    — 키가 (None, None)로 같아 하나만 남게 되면 문서 검색이 망가진다.
    """
    push = bool(source_types) and pushdown
    collapse = bool(collapse_by_action)
    # 캐시 (RPA-211) — 에이전트가 타는 경로다. 턴당 9회 불리고 회당 5.5초라 턴 시간의 68%를 먹는다.
    # 키에 활성 파라미터를 넣으므로 RPA-149 런타임 튜닝이 **즉시** 반영된다(별도 무효화 불필요).
    # 🔴 push는 같은 (query, k, source_types)로 **다른 결과**를 낸다 — 키를 안 가르면 두 방식이
    #    서로의 결과를 읽는다. search_key(pushdown=)가 다이제스트 네임스페이스를 분리한다.
    params = load_active_params()
    cache_key = rag_cache.search_key(
        query, k, tuple(source_types) if source_types else None,
        params, config.EMBEDDING_MODEL, config.RERANK_MODEL,
        pushdown=push, collapse=collapse,
    )
    cached = rag_cache.get_search(cache_key)
    if cached is not None:
        return cached

    # 연결은 풀·공용 클라이언트에서 빌린다 (RPA-219) — 검색마다 새 PG 연결·새 OpenSearch
    # 클라이언트를 만들던 것이 팬아웃 시 연결 수립 직렬화의 원인이었다.
    os_client = opensearch_client.get_shared_client()
    # k*3 과다인출은 후단 필터가 버려질 것을 감안한 보정이다. push면 버릴 게 없으므로 k면 된다.
    # collapse는 조각이 접히며 줄어드는 만큼을 따로 벌어야 하므로 push 여부와 무관하게 배수를 쓴다.
    if collapse:
        fetch_limit = k * _COLLAPSE_OVERFETCH
    else:
        fetch_limit = k if push else (k * 3 if source_types else k)
    with db.connection() as conn:
        results = _hybrid_search(
            conn, os_client, query, limit=fetch_limit, params=params,
            source_types=list(source_types) if push else None,
        )

    # push여도 후단 필터를 남긴다 — 비용이 0이고(이미 전부 해당 타입), 아래 계층 중
    # 하나라도 필터를 흘리면(예: 색인 매핑 변경으로 terms가 무력화) 여기서 잡힌다.
    if source_types:
        results = [r for r in results if r.get("source_type") in source_types]

    # 조각 접기 — results는 이미 점수 내림차순이라 첫 등장이 그 액션의 최고 점수 조각이다.
    if collapse:
        seen: set[tuple] = set()
        collapsed: list[dict] = []
        for r in results:
            key = (r.get("package_name"), r.get("action_name"))
            if key[0] and key[1]:
                if key in seen:
                    continue
                seen.add(key)
            collapsed.append(r)  # 키가 없는 행(doc_page 등)은 접지 않는다
        results = collapsed

    for r in results:
        r["score"] = r.get("rerank_score", r.get("rrf_score", r.get("score", 0.0)))

    final = results[:k]
    # 저하(BM25 실패)·빈 결과는 저장하지 않는다 — 장애가 캐시에 얼어붙으면 복구 후에도
    # TTL 동안 반쪽 결과가 나간다. 건너뛴 사유는 관측에 남긴다.
    skipped = rag_cache.put_search(cache_key, final)
    if skipped:
        log_event("rag_cache_skip", reason=skipped, query_len=len(query), k=k)
    return final
