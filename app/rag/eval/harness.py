"""검색 평가 하네스 — 골드셋을 검색 함수에 흘려 순위 품질을 집계한다 (RPA-131).

evaluate()는 search_fn을 주입받는다: search_fn(query) -> list[dict](검색 결과 문서, 상위
순서대로, package_name/action_name 포함). 검색 구현·파라미터·DB를 알지 못하므로,
어떤 검색 구성이든 함수로 감싸 같은 잣대로 비교할 수 있다 — 하이퍼파라미터 탐색(RPA-132)이
이 위에 올라탄다. DB 없이 fake search_fn으로 단위 테스트된다.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from . import metrics
from .goldset import GoldQuery, doc_key

SearchFn = Callable[[str], list[dict]]


@dataclass(frozen=True)
class QueryEval:
    """쿼리 하나의 평가 결과 — 집계 전 원자료(디버깅·오답 분석용)."""

    query: str
    rank: int | None          # 첫 정답 순위(없으면 None)
    reciprocal_rank: float
    recall_at_k: float
    hit_at_k: float
    retrieved: list[str]      # 검색된 상위 문서 키("pkg/action") — 왜 놓쳤나 재구성용


@dataclass(frozen=True)
class EvalReport:
    """골드셋 전체 집계 + 쿼리별 상세."""

    k: int
    num_queries: int
    mrr: float
    recall_at_k: float
    hit_at_k: float
    per_query: list[QueryEval] = field(default_factory=list)


def _key_str(key: tuple[str, str] | None) -> str:
    return f"{key[0]}/{key[1]}" if key else "?"


def evaluate(gold: list[GoldQuery], search_fn: SearchFn, k: int = 5) -> EvalReport:
    """골드셋의 각 쿼리를 search_fn으로 검색해 MRR·recall@k·hit@k를 집계한다.

    검색 결과 문서를 (package_name, action_name)로 환원해 정답 집합과 맞춘다. 순위는
    검색이 돌려준 순서를 그대로 쓴다(hits[0]=1위). RR은 전체 순위 기준, recall/hit는
    상위 k 기준 — 관심사가 다르므로 잘라내는 시점을 구분한다.
    """
    per_query: list[QueryEval] = []
    for gq in gold:
        docs = search_fn(gq.query)
        keys = [doc_key(d) for d in docs]
        hits = [key in gq.relevant for key in keys]
        # recall은 '고유 정답 키'를 세야 한다: 같은 (package, action) 문서가 여러 번
        # 반환되면(청크 분할 등) hits가 모두 True라 정답 수보다 많이 집계돼 recall>1이 된다.
        # 첫 등장만 True로 표시해 상위 k 내 '서로 다른 정답'만 센다. RR·hit은 중복에 무해하므로
        # 원래 hits를 그대로 쓴다(첫 정답 순위·존재 여부는 중복이 바꾸지 않는다).
        seen_relevant: set = set()
        recall_hits: list[bool] = []
        for key, is_relevant in zip(keys, hits):
            recall_hits.append(is_relevant and key not in seen_relevant)
            if is_relevant:
                seen_relevant.add(key)
        rank = metrics.first_relevant_rank(hits)
        per_query.append(QueryEval(
            query=gq.query,
            rank=rank,
            reciprocal_rank=metrics.reciprocal_rank(hits),
            recall_at_k=metrics.recall_at_k(recall_hits, len(gq.relevant), k),
            hit_at_k=metrics.hit_at_k(hits, k),
            retrieved=[_key_str(key) for key in keys[:k]],
        ))

    return EvalReport(
        k=k,
        num_queries=len(per_query),
        mrr=metrics.mean([q.reciprocal_rank for q in per_query]),
        recall_at_k=metrics.mean([q.recall_at_k for q in per_query]),
        hit_at_k=metrics.mean([q.hit_at_k for q in per_query]),
        per_query=per_query,
    )
