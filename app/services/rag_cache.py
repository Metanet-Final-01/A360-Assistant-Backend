# -*- coding: utf-8 -*-
"""RAG 캐싱 2층 — 질의 임베딩 · 검색 결과 (RPA-211).

## 왜 여기인가 (실측 근거)

턴 하나의 시간 구성(관측 DB 251턴): **RAG 41.9초 / LLM 20.0초** — RAG가 68%다.
그 RAG 5.5초의 내역(웜업 후 12회 측정):

    임베딩(Voyage) 1,991ms 36% | 벡터(pgvector) 873ms 16%
    BM25(OpenSearch) 558ms 10% | 리랭커(Voyage) 2,068ms 38%

**74%가 외부 API 왕복**이다. DB 튜닝이 아니라 왕복 제거가 지렛대다.

## 무엇을 캐싱하지 않는가

- **한 턴 안 중복은 0%**(2,462건 중 10건) — 에이전트가 매번 다른 질의를 만든다.
  캐시는 턴·사용자 **사이**에서만 먹는다. 그래서 TTL이 짧으면 의미가 없다
  (실측: 5분 6% / 60분 13% / 24시간 24%).
- 프롬프트 조립·질의응답 전체는 캐싱하지 않는다. 전자는 절감이 미미하고,
  후자는 추천이 세션 맥락에 의존해 같은 질의라도 답이 달라야 한다.

## 🔴 캐시는 투명해야 한다

같은 입력이면 **바이트 단위로 같은 결과**여야 한다. 다르면 캐싱이 아니라 동작 변경이다.
그래서 키가 **결과를 좌우하는 모든 입력**을 포함한다 — 특히 검색 파라미터는 RPA-149로
런타임에 바뀌므로(백오피스 슬라이더), 키에 없으면 "튜닝했는데 안 먹는다"가 된다.
가드가 읽는 것 == 동작이 읽는 것(CONVENTIONS §9).

## ⚠️ 한계 (정직하게)

- **적재(ingest)는 별도 프로세스**라 인프로세스 캐시를 무효화하지 못한다. 코퍼스가 바뀐 뒤
  최대 TTL 동안 옛 결과가 나갈 수 있다 — 그래서 기본 TTL을 1시간으로 짧게 잡았다
  (24시간이면 적중률 24%지만 적재 후 하루 동안 낡은다). 적재 직후 데모라면 서버를
  재기동하거나 `bust_search()`를 부를 것.
- **단일 워커 전제**다. 다중 인스턴스면 인스턴스마다 따로 데워지고 일관된 무효화가 안 된다
  → 스케일아웃 시 Redis로 옮긴다(`retrieval_params`·`budget`와 같은 판단).
"""

import hashlib
import os
import threading
from typing import Any

from cachetools import TTLCache

_DEFAULT_TTL_SEC = 3600      # 1시간 — 적재 후 낡는 창을 짧게 (적중률 13% 수준)
_DEFAULT_MAXSIZE = 2048

_lock = threading.Lock()     # TTLCache는 스레드 안전하지 않다. 동기 라우트는 워커 스레드풀에서 돈다
_embedding_cache: TTLCache | None = None
_search_cache: TTLCache | None = None

# 관측용 카운터 — "적중률이 왜 낮은가"에 답하려면 건너뛴 이유까지 세야 한다
_stats = {"embed_hit": 0, "embed_miss": 0, "search_hit": 0, "search_miss": 0,
          "skip_degraded": 0, "skip_empty": 0}


def enabled() -> bool:
    """미설정=비활성. 켜지 않은 배포의 기존 동작을 바꾸지 않는다(데모 중 즉시 원복 가능)."""
    return os.getenv("RAG_CACHE_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _ttl() -> int:
    try:
        return max(int(os.getenv("RAG_CACHE_TTL_SECONDS", _DEFAULT_TTL_SEC)), 1)
    except ValueError:
        return _DEFAULT_TTL_SEC


def _maxsize() -> int:
    try:
        return max(int(os.getenv("RAG_CACHE_MAXSIZE", _DEFAULT_MAXSIZE)), 1)
    except ValueError:
        return _DEFAULT_MAXSIZE


def _get(which: str) -> TTLCache:
    global _embedding_cache, _search_cache
    if which == "embed":
        if _embedding_cache is None:
            _embedding_cache = TTLCache(maxsize=_maxsize(), ttl=_ttl())
        return _embedding_cache
    if _search_cache is None:
        _search_cache = TTLCache(maxsize=_maxsize(), ttl=_ttl())
    return _search_cache


def _norm(query: str) -> str:
    """질의 정규화 — 앞뒤 공백·연속 공백만 정리한다.

    ⚠️ 소문자화·형태 변환은 하지 않는다. BM25 분석기가 대소문자를 다루는 방식과 어긋나면
    "캐시 키로는 같은데 실제 검색 결과는 다른" 질의가 생겨 캐시가 결과를 바꿔버린다.
    정규화는 **결과가 확실히 같은 범위**로만 제한한다.
    """
    return " ".join(query.split())


def _digest(*parts: Any) -> str:
    """키를 해시로 축약 — 질의 원문을 메모리에 그대로 들고 있지 않기 위함(PII 방어)."""
    raw = "\x1f".join(repr(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── 질의 임베딩 층 ───────────────────────────────────────────────────────────

def embedding_key(query: str, model: str, dim: int | None) -> str:
    """모델·차원이 키에 들어간다 — 모델을 갈면 자동으로 다른 키가 되어 옛 벡터를 안 쓴다."""
    return _digest("emb", model, dim, _norm(query))


def get_embedding(key: str) -> list[float] | None:
    if not enabled():
        return None
    with _lock:
        v = _get("embed").get(key)
        _stats["embed_hit" if v is not None else "embed_miss"] += 1
        return v


def put_embedding(key: str, vector: list[float]) -> None:
    if not enabled() or not vector:
        return
    with _lock:
        _get("embed")[key] = vector


# ── 검색 결과 층 ─────────────────────────────────────────────────────────────

def search_key(query: str, k: int, source_types: tuple[str, ...] | None, params: Any,
               embed_model: str, rerank_model: str) -> str:
    """결과를 좌우하는 **모든** 입력을 넣는다.

    파라미터 5종은 RPA-149로 런타임에 바뀐다 — 키에 넣으면 변경 즉시 다른 키가 되므로
    별도 무효화 없이도 "튜닝이 바로 먹는다". 무효화보다 키 포함이 실수에 강하다.
    """
    return _digest(
        "search", _norm(query), k, tuple(source_types or ()),
        getattr(params, "candidate_pool_size", None),
        getattr(params, "rerank_candidates", None),
        getattr(params, "rrf_k", None),
        getattr(params, "vector_weight", None),
        getattr(params, "bm25_weight", None),
        embed_model, rerank_model,
    )


def get_search(key: str) -> list[dict] | None:
    if not enabled():
        return None
    with _lock:
        v = _get("search").get(key)
        _stats["search_hit" if v is not None else "search_miss"] += 1
        # 호출부가 결과를 변형해도 캐시 원본이 오염되지 않게 얕은 복사로 돌려준다
        return [dict(r) for r in v] if v is not None else None


def put_search(key: str, results: list[dict]) -> str | None:
    """저장하고, **건너뛴 경우 그 사유**를 반환한다(관측용).

    🔴 저하된 결과는 캐싱하지 않는다. OpenSearch가 죽으면 검색이 dense-only로 저하되는데
    (RPA-156에서 실제 발생), 그걸 캐싱하면 **복구 후에도 TTL 동안 반쪽 결과**가 나간다.
    성능 최적화가 장애를 숨기는 장치가 되면 안 된다.

    빈 결과도 저장하지 않는다 — 저하로 인한 0건인지 정말 없는 건지 **구분할 수 없기 때문**이다.
    """
    if not enabled():
        return None
    if not results:
        with _lock:
            _stats["skip_empty"] += 1
        return "empty"
    if not all(r.get("bm25_available", True) for r in results):
        with _lock:
            _stats["skip_degraded"] += 1
        return "degraded"
    with _lock:
        _get("search")[key] = [dict(r) for r in results]
    return None


# ── 무효화·관측 ──────────────────────────────────────────────────────────────

def bust_search() -> int:
    """검색 결과 캐시를 비운다(적재 후 등). 임베딩은 코퍼스와 무관하므로 유지한다."""
    with _lock:
        c = _get("search")
        n = len(c)
        c.clear()
        return n


def bust_all() -> None:
    global _embedding_cache, _search_cache
    with _lock:
        _embedding_cache = None
        _search_cache = None
        for k in _stats:
            _stats[k] = 0


def stats() -> dict:
    """적중·미스·건너뜀 카운터. 건너뛴 사유를 함께 봐야 '적중률이 왜 낮은가'에 답할 수 있다."""
    with _lock:
        s = dict(_stats)
    for layer in ("embed", "search"):
        hit, miss = s[f"{layer}_hit"], s[f"{layer}_miss"]
        s[f"{layer}_hit_rate"] = round(hit / (hit + miss), 3) if (hit + miss) else None
    s["enabled"] = enabled()
    s["ttl_seconds"] = _ttl()
    return s
