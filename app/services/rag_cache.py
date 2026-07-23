# -*- coding: utf-8 -*-
"""RAG 캐싱 2층 — 질의 임베딩 · 검색 결과 (RPA-211, Redis 백엔드 RPA-274).

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

## 백엔드 (RPA-274)

`REDIS_URL` 미설정(기본) = 기존 인프로세스 TTLCache 그대로. 설정하면 **공유 Redis**가
백엔드가 되어 인프로세스의 한계 세 가지가 풀린다:
  ① ingest(별도 프로세스)가 `bust_search()`로 캐시를 실제로 무효화할 수 있다
  ② 인스턴스 2대가 캐시를 공유한다 — 한 대의 miss가 다른 대의 hit
  ③ 재배포에도 캐시가 살아남는다 (배포 직후 콜드 스타트 제거)

- **fail-open**: Redis 접속 실패는 miss로 동작하고(짧은 타임아웃 + 30초 백오프),
  검색은 캐시 없이 계속된다. 캐시는 정본이 아니다.
- Redis가 설정돼 있으면 인프로세스로 **폴백하지 않는다** — 두 백엔드가 서로 다른
  내용을 들면(한쪽만 bust되는 등) "같은 입력=같은 출력" 불변식이 깨진다.
- 값은 strict JSON — 직렬화 불가면 저장을 건너뛴다(불변식이 적중률보다 우선).
- TTL 기본은 백엔드와 무관하게 1시간이다. Redis + ingest bust가 배선된 배포에서만
  `RAG_CACHE_TTL_SECONDS=86400`으로 **명시적으로** 올릴 것(적중률 실측 13%→24%) —
  REDIS_URL 유무로 TTL이 암묵 변경되면 "설정 안 바꿨는데 낡음 창이 24배"가 된다.

## ⚠️ 남은 한계 (정직하게)

- 인프로세스 모드(REDIS_URL 미설정)는 여전히: ingest가 무효화 못 함(→TTL 1시간 유지),
  단일 워커 전제, 재기동 시 전멸. 이 모드의 한계는 RPA-211 때와 동일하다.
- hit/miss 카운터(stats)는 Redis 모드에서도 **프로세스별**이다 — 전역 적중률은
  인스턴스별 stats를 합산해서 봐야 한다.
"""

import hashlib
import json
import os
import threading
import time
from typing import Any

from cachetools import TTLCache

_DEFAULT_TTL_SEC = 3600      # 1시간 — 적재 후 낡는 창을 짧게 (적중률 13% 수준)
_DEFAULT_MAXSIZE = 2048

_lock = threading.Lock()     # TTLCache는 스레드 안전하지 않다. 동기 라우트는 워커 스레드풀에서 돈다
_embedding_cache: TTLCache | None = None
_search_cache: TTLCache | None = None

# 관측용 카운터 — "적중률이 왜 낮은가"에 답하려면 건너뛴 이유까지 세야 한다
_stats = {"embed_hit": 0, "embed_miss": 0, "search_hit": 0, "search_miss": 0,
          "skip_degraded": 0, "skip_empty": 0, "skip_unserializable": 0,
          "redis_error": 0, "redis_decode_error": 0}

# ── Redis 백엔드 (RPA-274) ───────────────────────────────────────────────────

_REDIS_NS_EMB = "rag:emb:"
_REDIS_NS_SEARCH = "rag:search:"
# 캐시 조회가 검색 hot path에 있다 — Redis가 죽었을 때 매 검색이 타임아웃을 기다리면
# 캐시가 성능 장치가 아니라 지연 장치가 된다. 실패 시 이 시간 동안 시도 자체를 끊는다.
_REDIS_DOWN_BACKOFF_SEC = 30.0
# 같은 VPC/로컬 전제의 캐시 조회다 — 0.5초에 못 받으면 캐시로서 의미가 없다(miss가 낫다)
_REDIS_TIMEOUT_SEC = 0.5

_redis_client: Any = None
_redis_url_cached: str | None = None
_redis_down_until = 0.0


def _redis_url() -> str:
    """호출 시점 읽기 — conftest가 빈 문자열로 격리한다(관측 DB와 같은 패턴)."""
    return os.getenv("REDIS_URL", "").strip()


def _make_redis_client(url: str) -> Any:
    """redis.Redis 생성 — 테스트가 fakeredis로 갈아끼우는 팩토리(검색기 팩토리 선례)."""
    import redis  # noqa: PLC0415 — 지연 import: 인프로세스 모드는 redis 패키지를 안 탄다

    return redis.Redis.from_url(
        url,
        socket_connect_timeout=_REDIS_TIMEOUT_SEC,
        socket_timeout=_REDIS_TIMEOUT_SEC,
        decode_responses=True,
    )


def _mark_redis_down() -> None:
    global _redis_down_until
    with _lock:
        _stats["redis_error"] += 1
    _redis_down_until = time.monotonic() + _REDIS_DOWN_BACKOFF_SEC


def _redis() -> Any | None:
    """활성 Redis 클라이언트, 또는 None(미설정·백오프 중·생성 실패).

    URL이 바뀌면 재생성한다 — 테스트의 setenv, 운영의 엔드포인트 교체 모두 재기동 없이 반영.

    ⚠️ URL 변경 감지가 백오프 체크보다 **먼저**다(Qodo #365): 순서를 바꾸면 장애 후
    엔드포인트를 갈아끼워도 옛 대상에 걸린 백오프 30초가 새 대상까지 막는다 — 백오프는
    "그 대상이 죽어 있다"는 지식이므로 대상이 바뀌면 무효가 맞다.
    """
    global _redis_client, _redis_url_cached, _redis_down_until
    url = _redis_url()
    if not url:
        return None
    if url != _redis_url_cached:
        _redis_client = None
        _redis_url_cached = url   # 생성 실패해도 유지 — 같은(잘못된) URL 재시도는 백오프를 탄다
        _redis_down_until = 0.0
    if time.monotonic() < _redis_down_until:
        return None
    if _redis_client is None:
        try:
            _redis_client = _make_redis_client(url)
        except Exception:  # noqa: BLE001 — 캐시는 정본이 아니다: 생성 실패는 miss로 산다
            _redis_client = None
            _mark_redis_down()
            return None
    return _redis_client


def _loads(raw: str, r: Any, full_key: str) -> Any | None:
    """Redis 값 JSON 디코드 — 실패도 miss로 산다(Qodo #365): fail-open은 데이터 오류에도 적용.

    오염된 값(수동 조작·부분 기록 등)이 hot path에서 예외로 터지면 캐시가 성능 장치가
    아니라 장애 지점이 된다. 문제 키는 지워서(best-effort) 반복 실패를 막는다 —
    서버 자체는 건강하므로 백오프는 걸지 않는다(연결 오류와 구분).
    """
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        with _lock:
            _stats["redis_decode_error"] += 1
        try:
            r.delete(full_key)
        except Exception:  # noqa: BLE001
            _mark_redis_down()
        return None


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
    """질의를 **그대로** 키에 쓴다 — 정규화하지 않는다.

    🔴 처음엔 공백만 합쳤다(`" ".join(query.split())`). 그런데 **캐시 키만 정규화하고 실제
    임베딩·BM25 호출에는 원본 질의가 그대로 간다**(#290 리뷰). 그러면 `"send  email"`과
    `"send email"`이 같은 키를 공유하는데 실제 결과는 다를 수 있어, **캐시가 결과를 바꾼다** —
    내가 지키겠다고 한 불변식("같은 입력이면 같은 출력")을 캐시 자신이 깬 셈이다.

    정규화하려면 하위 호출에도 같은 질의를 넘겨야 하는데, 그건 검색 동작 자체를 바꾸는
    변경이라 캐싱 PR의 범위가 아니다. **키는 원본을 쓰고 적중률을 조금 잃는 쪽**을 택한다 —
    공백 변형은 드물고, 안전이 적중률보다 우선이다.
    """
    return query


def _digest(*parts: Any) -> str:
    """키를 해시로 축약 — 질의 원문을 메모리에 그대로 들고 있지 않기 위함(PII 방어)."""
    raw = "\x1f".join(repr(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── 질의 임베딩 층 ───────────────────────────────────────────────────────────

def embedding_key(query: str, model: str, dim: int | None) -> str:
    """모델·차원이 키에 들어간다 — 모델을 갈면 자동으로 다른 키가 되어 옛 벡터를 안 쓴다."""
    return _digest("emb", model, dim, _norm(query))


def get_embedding(key: str) -> list[float] | None:
    """복사본을 돌려준다 — 호출부가 벡터를 변형해도 캐시 원본이 오염되면 안 된다(#290 리뷰).

    (Redis 경로는 json.loads가 매번 새 객체를 만들므로 복사가 내재돼 있다.)
    """
    if not enabled():
        return None
    r = _redis()
    if r is not None:
        try:
            raw = r.get(_REDIS_NS_EMB + key)
        except Exception:  # noqa: BLE001
            _mark_redis_down()
            raw = None
        value = _loads(raw, r, _REDIS_NS_EMB + key) if raw is not None else None
        with _lock:  # 디코드 **후** 집계 — 오염값이 hit로 세이면 적중률이 거짓말한다(Qodo #365)
            _stats["embed_hit" if value is not None else "embed_miss"] += 1
        return value
    if _redis_url():  # Redis 지정됐지만 백오프 중 — 인프로세스로 폴백하지 않는다(위 docstring)
        with _lock:
            _stats["embed_miss"] += 1
        return None
    with _lock:
        v = _get("embed").get(key)
        _stats["embed_hit" if v is not None else "embed_miss"] += 1
        return list(v) if v is not None else None


def put_embedding(key: str, vector: list[float]) -> None:
    if not enabled() or not vector:
        return
    r = _redis()
    if r is not None:
        try:
            r.set(_REDIS_NS_EMB + key, json.dumps(vector), ex=_ttl())
        except Exception:  # noqa: BLE001
            _mark_redis_down()
        return
    if _redis_url():
        return
    with _lock:
        _get("embed")[key] = list(vector)   # 저장도 복사본으로 — 호출부가 나중에 바꿔도 무관하게


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
    r = _redis()
    if r is not None:
        try:
            raw = r.get(_REDIS_NS_SEARCH + key)
        except Exception:  # noqa: BLE001
            _mark_redis_down()
            raw = None
        value = _loads(raw, r, _REDIS_NS_SEARCH + key) if raw is not None else None
        with _lock:  # 디코드 **후** 집계 — 위 get_embedding과 같은 이유
            _stats["search_hit" if value is not None else "search_miss"] += 1
        return value
    if _redis_url():
        with _lock:
            _stats["search_miss"] += 1
        return None
    with _lock:
        v = _get("search").get(key)
        _stats["search_hit" if v is not None else "search_miss"] += 1
        # 호출부가 결과를 변형해도 캐시 원본이 오염되지 않게 얕은 복사로 돌려준다
        return [dict(r_) for r_ in v] if v is not None else None


def put_search(key: str, results: list[dict]) -> str | None:
    """저장하고, **건너뛴 경우 그 사유**를 반환한다(관측용).

    🔴 저하된 결과는 캐싱하지 않는다. OpenSearch가 죽으면 검색이 dense-only로 저하되는데
    (RPA-156에서 실제 발생), 그걸 캐싱하면 **복구 후에도 TTL 동안 반쪽 결과**가 나간다.
    성능 최적화가 장애를 숨기는 장치가 되면 안 된다.

    빈 결과도 저장하지 않는다 — 저하로 인한 0건인지 정말 없는 건지 **구분할 수 없기 때문**이다.

    ⚠️ 저하는 **명시적 `False`일 때만**이다. `mode="vector"`는 BM25를 아예 부르지 않아
    필드가 없고(정상 캐싱 대상), `None`은 "해당 없음"이다 — 관측의 `_bm25_health`가 쓰는
    3상태 규약과 같은 해석을 여기서도 쓴다. 한 필드를 두 곳이 다르게 읽으면 그게 다음 버그다.
    """
    if not enabled():
        return None
    if not results:
        with _lock:
            _stats["skip_empty"] += 1
        return "empty"
    if any(r.get("bm25_available") is False for r in results):
        with _lock:
            _stats["skip_degraded"] += 1
        return "degraded"
    r = _redis()
    if r is not None:
        # strict JSON — 직렬화가 값을 바꾸면(예: datetime→str) "같은 입력=같은 출력"이 깨진다.
        # 못 담는 값이면 캐싱을 포기한다(불변식 > 적중률). 사유는 호출부가 관측에 남긴다.
        try:
            payload = json.dumps(results, ensure_ascii=False)
        except (TypeError, ValueError):
            with _lock:
                _stats["skip_unserializable"] += 1
            return "unserializable"
        try:
            r.set(_REDIS_NS_SEARCH + key, payload, ex=_ttl())
        except Exception:  # noqa: BLE001
            _mark_redis_down()
        return None
    if _redis_url():
        return None
    with _lock:
        _get("search")[key] = [dict(r_) for r_ in results]
    return None


# ── 무효화·관측 ──────────────────────────────────────────────────────────────

def bust_search() -> int:
    """검색 결과 캐시를 비운다(적재 후 등). 임베딩은 코퍼스와 무관하므로 유지한다.

    Redis 모드에서는 크로스 프로세스로 동작한다 — `pipeline ingest`(별도 프로세스)가
    적재 직후 불러 "적재했는데 옛 결과가 나온다" 창을 없앤다(RPA-274). enabled()와
    무관하게 시도한다: ingest 프로세스에는 서버의 캐시 토글이 없을 수 있다.
    """
    n = 0
    r = _redis()
    if r is not None:
        try:
            batch: list[str] = []
            for k in r.scan_iter(match=_REDIS_NS_SEARCH + "*", count=500):
                batch.append(k)
                if len(batch) >= 500:
                    n += r.delete(*batch)
                    batch = []
            if batch:
                n += r.delete(*batch)
        except Exception:  # noqa: BLE001
            _mark_redis_down()
    # 로컬 캐시도 함께 비운다 — 백엔드 전환 직후 남아 있을 수 있는 잔재까지 정리
    with _lock:
        c = _get("search")
        n += len(c)
        c.clear()
    return n


def bust_all() -> None:
    r = _redis()
    if r is not None:
        try:
            for ns in (_REDIS_NS_EMB, _REDIS_NS_SEARCH):
                batch = list(r.scan_iter(match=ns + "*", count=500))
                if batch:
                    r.delete(*batch)
        except Exception:  # noqa: BLE001
            _mark_redis_down()
    global _embedding_cache, _search_cache
    with _lock:
        _embedding_cache = None
        _search_cache = None
        for k in _stats:
            _stats[k] = 0


def stats() -> dict:
    """적중·미스·건너뜀 카운터. 건너뛴 사유를 함께 봐야 '적중률이 왜 낮은가'에 답할 수 있다.

    Redis 모드에서도 카운터는 프로세스별이다(전역 적중률은 인스턴스 합산으로 볼 것).
    """
    with _lock:
        s = dict(_stats)
    for layer in ("embed", "search"):
        hit, miss = s[f"{layer}_hit"], s[f"{layer}_miss"]
        s[f"{layer}_hit_rate"] = round(hit / (hit + miss), 3) if (hit + miss) else None
    s["enabled"] = enabled()
    s["ttl_seconds"] = _ttl()
    s["backend"] = "redis" if _redis_url() else "memory"
    return s
