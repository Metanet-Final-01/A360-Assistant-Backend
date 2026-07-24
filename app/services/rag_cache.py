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
- 키가 결과를 좌우하는 **모든** 입력을 포함한다(질의·k·파라미터 5종·모델).
- 값은 strict JSON(allow_nan=False) + **라운드트립 동일성 검사** — json.dumps가 값을
  조용히 바꾸는 경우(tuple→list, int 키→str)도 위반이므로 저장을 포기한다.
- 읽기도 문법(JSON)만이 아니라 **계약(타입·형태)**까지 검증한다 — 통과 못 하면
  그 키를 지우고 miss로 산다.
가드가 읽는 것 == 동작이 읽는 것(CONVENTIONS §9).

## 무효화는 삭제가 아니라 세대(generation) 전환이다 (RPA-274)

SCAN+DELETE 방식은 레이스가 있다: 요청 A가 구 코퍼스로 검색하는 **도중** ingest가
삭제를 끝내면, A가 늦게 끝나며 구 결과를 다시 SET한다 — 무효화가 조용히 되돌아간다.
그래서 검색 키에 **코퍼스 세대**를 리터럴 세그먼트로 넣는다:

    a360:{env}:rag:v{schema}:search:g{gen}:{digest}

- 요청은 **키를 만드는 순간** 세대를 한 번 캡처하고, get/compute/put 전 구간에서 같은
  키 문자열을 쓴다(호출부는 키를 불투명하게 취급). 늦게 끝난 구 요청의 SET은 구 세대
  키에만 저장돼 새 세대 독자에게 보이지 않는다.
- ingest 성공 시 세대 키를 INCR — 원자적 전환. **구 세대 삭제는 정합성 조건이 아니다**
  (TTL이 치우고, cleanup_old_generations()는 하이진일 뿐).
- 임베딩 층은 세대 무관(질의→벡터는 코퍼스에 안 의존) — 세대 세그먼트 없음.
- 세대를 못 읽으면(장애) 그 요청은 캐싱 자체를 건너뛴다 — 모르는 세대에 저장하는 것보다
  안전하다.

## hot path와 control path는 실패 정책이 다르다

- **hot path**(get/put, 검색 경로): fail-open. 짧은 타임아웃 + 백오프(서킷) — 죽은
  Redis를 반복 대기하지 않고 miss로 산다. 캐시는 정본이 아니다.
- **control path**(ingest의 mark_ingest_pending/publish_generation): fail-closed.
  hot path 서킷과 **무관하게**(별도 클라이언트) 유한 재시도 후 CacheInvalidationError를
  던진다 — "무효화했다고 믿었는데 안 됐다"가 0건 성공으로 숨으면 안 된다. pipeline이
  non-zero exit로 운영자에게 알린다.

## 부분 적재는 pending 마커가 막는다

ingest는 스토어를 만지기 **전에** pending 마커를 세운다. 마커가 있는 동안 put_search는
저장을 건너뛴다(사유 "ingest_pending") — PG만 성공하고 OpenSearch가 실패한 부분 코퍼스
결과가 TTL 동안 캐싱되는 것을 막는다. 성공 publish가 마커를 지운다. 실패하면 마커가
남아(만료 한도 RAG_CACHE_PENDING_TTL_SECONDS) 재실행 전까지 새 캐싱을 차단한다.
⚠️ 남은 한계: 마커 만료 후에도 부분 상태가 방치돼 있으면 캐싱이 재개된다 — 마커는
시간 한도가 있는 안전장치이지 blue-green publish가 아니다(그건 범위 밖, RPA-274 본문).

## 백엔드

`REDIS_URL` 미설정(기본) = 인프로세스 TTLCache(RPA-211 그대로 — 세대 전환은 프로세스
로컬로만 동작하고, ingest 별도 프로세스와 단일 워커 한계는 그때와 동일). 설정 시 공유
Redis: ① ingest 즉시 무효화 ② 인스턴스 간 공유 ③ 재배포 생존.
- Redis 지정 시 인프로세스로 폴백하지 않는다 — 두 백엔드가 갈라지면 한쪽만 무효화되는
  순간 투명성 불변식이 깨진다.
- TTL 기본은 1시간. Redis + ingest 배선이 확인된 배포에서만 86400으로 명시적으로 올릴
  것(적중률 실측 13%→24%) — REDIS_URL 유무로 TTL이 암묵 변경되면 안 된다.
- hit/miss 카운터는 Redis 모드에서도 프로세스별이다(전역은 인스턴스 합산으로).
"""

import hashlib
import json
import logging
import math
import os
import random
import re
import threading
import time
from typing import Any

from cachetools import TTLCache

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SEC = 3600          # 1시간 — 적재 후 낡는 창을 짧게 (적중률 13% 수준)
_DEFAULT_MAXSIZE = 2048
_DEFAULT_PENDING_TTL_SEC = 21600  # 6시간 — 실패한 ingest의 캐싱 차단 한도(재실행 유예)

# 캐시 값 직렬화 계약 버전 — 값의 형태(필드·타입)가 바뀌면 올린다. 롤링 배포 중 신·구
# 코드가 같은 Redis를 봐도 서로의 값을 읽지 않게 네임스페이스를 가른다.
_SCHEMA_VERSION = 1

# 검색 결과 캐시 값의 최소 계약 — services/rag.py가 저장 직전 보장하는 필드들.
# 여기 없는 값이 오면 "유효한 JSON이지만 우리 값이 아니다" → 지우고 miss (계약 위반 metric).
_SEARCH_REQUIRED_FIELDS = frozenset({"id", "content", "score"})

_lock = threading.Lock()         # _stats·인프로세스 TTLCache 보호
_embedding_cache: TTLCache | None = None
_search_cache: TTLCache | None = None
_local_generation = 0            # 인프로세스 모드의 코퍼스 세대 (publish_generation이 올림)

# 관측용 카운터 — "적중률이 왜 낮은가"에 답하려면 건너뛴 이유까지 세야 한다
_stats = {"embed_hit": 0, "embed_miss": 0, "search_hit": 0, "search_miss": 0,
          "skip_degraded": 0, "skip_empty": 0, "skip_unserializable": 0,
          "skip_ingest_pending": 0, "skip_no_generation": 0,
          "redis_error": 0, "redis_decode_error": 0, "redis_contract_error": 0}


class CacheInvalidationError(RuntimeError):
    """control path(ingest 무효화·세대 전환) 실패 — 호출부가 non-zero exit로 알려야 한다."""


# ── Redis 클라이언트·서킷 상태 (RPA-274) ────────────────────────────────────
# ⚠️ 아래 4개 전역은 반드시 _state_lock 아래에서만 읽고 쓴다 — 스레드풀(동기 라우트)과
#    async 경로가 동시에 접근한다. 클라이언트 교체·서킷 판정이 원자적이어야
#    "옛 클라이언트의 실패가 새 URL의 서킷을 여는" 경합이 안 생긴다.

_state_lock = threading.Lock()
_redis_client: Any = None
_redis_url_cached: str | None = None
_redis_down_until = 0.0          # 서킷: 이 시각 전에는 hot path가 Redis를 시도하지 않는다

# hot path 파라미터 — 캐시 조회가 검색 경로에 있으므로 실패는 빨라야 한다
_REDIS_TIMEOUT_SEC = 0.5         # 같은 VPC/로컬 전제 — 0.5초에 못 받으면 miss가 낫다
_REDIS_DOWN_BACKOFF_SEC = 30.0   # 서킷 유지 시간(지터 ±20% 적용)
_REDIS_PROBE_WINDOW_SEC = 2.0    # half-open: 만료 후 한 스레드만 프로브, 나머지는 이 창 동안 대기

# control path 파라미터 — 정확성이 걸린 경로라 조금 더 기다리고, 유한 재시도 후 실패를 던진다
_CONTROL_TIMEOUT_SEC = 2.0
_CONTROL_ATTEMPTS = 3
_CONTROL_RETRY_BASE_SEC = 0.5    # 0.5 → 1.0 → 2.0


def _redis_url() -> str:
    """호출 시점 읽기 — conftest가 빈 문자열로 격리한다(관측 DB와 같은 패턴)."""
    return os.getenv("REDIS_URL", "").strip()


def _make_redis_client(url: str, *, timeout: float = _REDIS_TIMEOUT_SEC) -> Any:
    """redis.Redis 생성 — 테스트가 fakeredis로 갈아끼우는 팩토리(검색기 팩토리 선례)."""
    import redis  # noqa: PLC0415 — 지연 import: 인프로세스 모드는 redis 패키지를 안 탄다

    return redis.Redis.from_url(
        url,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
        decode_responses=True,
    )


def _hot_client() -> Any | None:
    """hot path용 클라이언트 스냅샷, 또는 None(미설정·서킷 열림·생성 실패).

    - URL 변경 감지가 서킷 체크보다 먼저다: 서킷은 "그 대상이 죽어 있다"는 지식이므로
      대상이 바뀌면 무효다(Qodo #365). 교체 시 이전 클라이언트는 close(연결 풀 정리).
    - half-open: 서킷 만료 후 **한 스레드만** 프로브한다 — 만료 순간 스레드 전부가
      죽었을 수 있는 Redis로 몰리면 타임아웃 폭주가 재발한다. 프로브 창(2초) 동안
      나머지는 계속 miss, 프로브가 성공하면 _note_redis_ok가 서킷을 닫는다.
    """
    global _redis_client, _redis_url_cached, _redis_down_until
    url = _redis_url()
    if not url:
        return None
    old_client = None
    with _state_lock:
        if url != _redis_url_cached:
            old_client = _redis_client
            _redis_client = None
            _redis_url_cached = url   # 생성 실패해도 유지 — 같은(잘못된) URL 재시도는 서킷을 탄다
            _redis_down_until = 0.0
        now = time.monotonic()
        if now < _redis_down_until:
            client = None
        else:
            if _redis_down_until:  # 서킷이 열려 있었다 → 이 스레드가 프로브 슬롯을 잡는다
                _redis_down_until = now + _REDIS_PROBE_WINDOW_SEC
            if _redis_client is None:
                try:
                    _redis_client = _make_redis_client(url)
                except Exception:  # noqa: BLE001 — 캐시는 정본이 아니다: 생성 실패는 miss로 산다
                    _redis_client = None
                    _open_circuit_locked()
            client = _redis_client
    if old_client is not None:
        try:
            old_client.close()
        except Exception:  # noqa: BLE001
            pass
    return client


def _open_circuit_locked() -> None:
    """서킷 열기 — _state_lock 아래에서만 부른다. 지터로 만료 동시 폭주를 흩는다."""
    global _redis_down_until
    _redis_down_until = time.monotonic() + _REDIS_DOWN_BACKOFF_SEC * random.uniform(0.8, 1.2)
    with _lock:
        _stats["redis_error"] += 1


def _mark_redis_down(client: Any) -> None:
    """hot path 연산 실패 → 서킷 열기. **그 클라이언트가 아직 현역일 때만** —
    URL이 이미 교체됐다면 옛 대상의 실패가 새 대상의 서킷을 열면 안 된다."""
    with _state_lock:
        if client is _redis_client:
            _open_circuit_locked()
        else:
            with _lock:
                _stats["redis_error"] += 1


def _note_redis_ok(client: Any) -> None:
    """연산 성공 → (그 클라이언트가 현역이면) 서킷·프로브 창을 닫는다."""
    global _redis_down_until
    with _state_lock:
        if client is _redis_client and _redis_down_until:
            _redis_down_until = 0.0


def _control_client() -> Any:
    """control path 전용 클라이언트 — hot path 서킷과 **무관**하게 새로 만든다.

    서킷은 hot path의 지연 방어일 뿐, ingest의 무효화가 그것 때문에 조용히 건너뛰면
    "적재했는데 캐시는 구 세대"가 성공 로그 뒤에 숨는다. 여기 실패는 위로 던진다.
    """
    url = _redis_url()
    if not url:
        raise CacheInvalidationError("REDIS_URL이 비어 있다 — control path를 부르면 안 되는 상태")
    return _make_redis_client(url, timeout=_CONTROL_TIMEOUT_SEC)


# 메시지 안의 **어떤** URL이든 userinfo 비밀번호를 지우는 일반 패턴 — 현재 REDIS_URL과
# 무관하게(회전된 옛 크레덴셜, 다른 인코딩 표현) URL 형태로 나타나면 걸린다 (Qodo #380 3차).
_URL_CRED_RE = re.compile(r"(://[^/@\s:]+:)[^@\s]+(@)")


def _safe_err(exc: Exception) -> str:
    """예외를 운영자용 문자열로 — **URL 크레덴셜은 마스킹**한다 (Qodo #380).

    타입만 남기면(기존) 인증 실패·DNS·타임아웃이 구분이 안 되고, 메시지를 통째로 남기면
    REDIS_URL의 비밀번호가 오류 문자열에 실려 나올 수 있다(SLACK 웹훅 유출 선례, PR#263·#288
    에서 타입 중심으로 줄였던 이유). 절충: 메시지를 포함하되 크레덴셜을 지운다. 3겹:

    ① 일반 URL userinfo 패턴 — 현재 설정과 무관한 URL(회전 전 크레덴셜 포함)도 URL 형태면 마스킹
    ② 현재 URL 비밀번호의 **디코드형**(urlsplit이 %XX를 푼 값 — redis-py 오류가 이 형태를 실음)
    ③ 현재 URL 비밀번호의 **원문형**(퍼센트 인코딩 그대로 — 원문 URL이 통째로 실리는 경우)

    한계(정직하게): 회전된 **옛** 비밀번호가 URL 문맥 없이 단독 문자열로 실리는 경우는 못
    가린다 — 그 값을 알 방법이 없다. ①이 URL 형태는 덮으므로 실질 표면은 그 좁은 틈뿐이다.

    ⚠️ 마스킹이 절단(300자)보다 **먼저**다 (Qodo #380 2차): 순서가 반대면 경계에 걸친
       비밀번호의 잘린 조각이 replace에 안 걸린다 — 자른 뒤에는 "전체 비밀번호"가 존재하지
       않으므로 마스킹이 원리적으로 불가능하다.
    """
    msg = f"{type(exc).__name__}: {exc}"
    msg = _URL_CRED_RE.sub(r"\1***\2", msg)
    url = _redis_url()
    if url:
        candidates: set[str] = set()
        try:
            from urllib.parse import unquote, urlsplit  # noqa: PLC0415

            # ⚠️ urlsplit().password는 퍼센트 인코딩을 **풀지 않은 원문**이다 — 디코드형은
            #    redis-py가 unquote한 값으로 오류에 실리므로 둘 다 후보에 넣는다.
            raw_pw = urlsplit(url).password
            if raw_pw:
                candidates.add(raw_pw)
                candidates.add(unquote(raw_pw))
        except ValueError:
            pass
        if "://" in url and "@" in url:
            userinfo = url.split("://", 1)[1].rsplit("@", 1)[0]
            if ":" in userinfo:
                candidates.add(userinfo.split(":", 1)[1])
        for secret in candidates:
            if secret:
                msg = msg.replace(secret, "***")
    return msg[:300]


def _control_call(op_name: str, fn) -> Any:
    """유한 재시도(0.5s→1s→2s) 후 실패면 CacheInvalidationError. 성공/실패 모두 로그."""
    last_exc: Exception | None = None
    for attempt in range(1, _CONTROL_ATTEMPTS + 1):
        try:
            client = _control_client()
            try:
                result = fn(client)
            finally:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
            return result
        except CacheInvalidationError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("rag_cache control '%s' 시도 %d/%d 실패: %s",
                           op_name, attempt, _CONTROL_ATTEMPTS, _safe_err(exc))
            if attempt < _CONTROL_ATTEMPTS:
                time.sleep(_CONTROL_RETRY_BASE_SEC * (2 ** (attempt - 1)))
    raise CacheInvalidationError(
        f"rag_cache control '{op_name}' {_CONTROL_ATTEMPTS}회 실패 "
        f"(마지막: {_safe_err(last_exc)}) — 캐시 세대가 코퍼스와 어긋났을 수 있다"
    ) from last_exc


# ── 설정·키 구성 ─────────────────────────────────────────────────────────────

def enabled() -> bool:
    """미설정=비활성. 켜지 않은 배포의 기존 동작을 바꾸지 않는다(데모 중 즉시 원복 가능)."""
    return os.getenv("RAG_CACHE_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _ttl() -> int:
    try:
        return max(int(os.getenv("RAG_CACHE_TTL_SECONDS", _DEFAULT_TTL_SEC)), 1)
    except ValueError:
        return _DEFAULT_TTL_SEC


def _pending_ttl() -> int:
    try:
        return max(int(os.getenv("RAG_CACHE_PENDING_TTL_SECONDS", _DEFAULT_PENDING_TTL_SEC)), 60)
    except ValueError:
        return _DEFAULT_PENDING_TTL_SEC


def _maxsize() -> int:
    try:
        return max(int(os.getenv("RAG_CACHE_MAXSIZE", _DEFAULT_MAXSIZE)), 1)
    except ValueError:
        return _DEFAULT_MAXSIZE


def _ns() -> str:
    """네임스페이스 루트 — 환경(dev/staging/prod 공유 Redis 혼입 방지)과 값 스키마 버전
    (롤링 배포 중 신·구 코드 혼입 방지)을 리터럴 세그먼트로 박는다."""
    env = os.getenv("APP_ENV", "development").strip() or "development"
    return f"a360:{env}:rag:v{_SCHEMA_VERSION}"


def _gen_key() -> str:
    return f"{_ns()}:gen"


def _pending_key() -> str:
    return f"{_ns()}:ingest_pending"


def _current_generation() -> int | None:
    """현재 코퍼스 세대. Redis 모드에서 못 읽으면 None — 그 요청은 캐싱을 건너뛴다."""
    if not _redis_url():
        return _local_generation
    r = _hot_client()
    if r is None:
        return None
    try:
        raw = r.get(_gen_key())
        _note_redis_ok(r)
    except Exception:  # noqa: BLE001
        _mark_redis_down(r)
        return None
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        # 세대 키가 오염됐다 — 임의 해석은 위험하므로 이 요청은 캐싱 스킵
        with _lock:
            _stats["redis_contract_error"] += 1
        return None


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


# ── 값 검증 (쓰기: strict 직렬화 / 읽기: 계약 검증) ─────────────────────────

def _dump_transparent(value: Any) -> str | None:
    """strict JSON 직렬화 + 라운드트립 동일성 확인. 어긋나면 None(저장 포기).

    - allow_nan=False: NaN/Infinity는 유효 JSON이 아니고, 통과시키면 읽는 쪽 파서에
      따라 값이 달라진다.
    - 라운드트립 비교: json.dumps는 tuple→list, int 키→str처럼 **예외 없이 값을 바꾸는**
      경우가 있다 — "같은 입력=같은 출력" 위반이므로 적중률보다 투명성을 지킨다.
    """
    try:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return None
    if json.loads(payload) != value:
        return None
    return payload


def _valid_embedding(value: Any, expected_dim: int | None) -> bool:
    """임베딩 계약: 비어 있지 않은 list[유한 수치], (알면) 기대 차원 일치."""
    if not isinstance(value, list) or not value:
        return False
    if expected_dim is not None and len(value) != expected_dim:
        return False
    for x in value:
        # bool은 int의 서브클래스 — 벡터 성분으로는 계약 위반이다
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
            return False
    return True


def _valid_search(value: Any) -> bool:
    """검색 결과 계약: 비어 있지 않은 list[dict], 각 항목에 최소 필드(id·content·score)."""
    if not isinstance(value, list) or not value:
        return False
    return all(isinstance(r, dict) and _SEARCH_REQUIRED_FIELDS <= r.keys() for r in value)


def _load_validated(raw: str, r: Any, full_key: str, validator) -> Any | None:
    """Redis 값 디코드 + 계약 검증 — 실패는 전부 miss로 산다(fail-open은 데이터 오류에도).

    문법 오류(JSON 아님)와 계약 오류(유효 JSON이지만 타입·형태가 우리 값이 아님)를
    별도 카운터로 가른다 — 전자는 오염/부분 기록, 후자는 스키마 드리프트 신호라 대응이
    다르다. 어느 쪽이든 키를 지워(best-effort) 반복 실패를 막는다. 서버 자체는 건강하므로
    서킷은 열지 않는다.
    """
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        with _lock:
            _stats["redis_decode_error"] += 1
        _evict(r, full_key)
        return None
    if not validator(value):
        with _lock:
            _stats["redis_contract_error"] += 1
        _evict(r, full_key)
        return None
    return value


def _evict(r: Any, full_key: str) -> None:
    try:
        r.delete(full_key)
    except Exception:  # noqa: BLE001
        _mark_redis_down(r)


# ── 질의 임베딩 층 ───────────────────────────────────────────────────────────

def embedding_key(query: str, model: str, dim: int | None) -> str:
    """모델·차원이 키에 들어간다 — 모델을 갈면 자동으로 다른 키가 되어 옛 벡터를 안 쓴다.

    임베딩은 코퍼스와 무관하므로 **세대 세그먼트가 없다**(ingest가 무효화하지 않는 층).
    반환 키는 불투명 문자열이다 — 호출부는 그대로 get/put에 넘긴다.
    """
    d = _digest("emb", model, dim, _norm(query))
    return f"{_ns()}:emb:{d}"


def get_embedding(key: str, *, expected_dim: int | None = None) -> list[float] | None:
    """복사본/새 객체를 돌려준다 — 호출부가 벡터를 변형해도 캐시 원본이 오염되면 안 된다
    (#290 리뷰; Redis 경로는 json.loads가 매번 새 객체라 내재).

    expected_dim을 주면 값의 차원까지 검증한다(키의 dim과 값의 실제 길이는 다른 명제다 —
    잘못 기록된 값이 파이프라인 깊숙이 들어가기 전에 여기서 걸러진다).
    """
    if not enabled():
        return None
    if _redis_url():
        r = _hot_client()
        value = None
        if r is not None:
            try:
                raw = r.get(key)
                _note_redis_ok(r)
            except Exception:  # noqa: BLE001
                _mark_redis_down(r)
                raw = None
            if raw is not None:
                value = _load_validated(raw, r, key,
                                        lambda v: _valid_embedding(v, expected_dim))
        with _lock:  # 디코드·검증 **후** 집계 — 오염값이 hit로 세이면 적중률이 거짓말한다
            _stats["embed_hit" if value is not None else "embed_miss"] += 1
        return value
    with _lock:
        v = _get("embed").get(key)
        _stats["embed_hit" if v is not None else "embed_miss"] += 1
        return list(v) if v is not None else None


def put_embedding(key: str, vector: list[float]) -> None:
    if not enabled() or not vector:
        return
    if _redis_url():
        payload = _dump_transparent(vector)
        if payload is None:
            with _lock:
                _stats["skip_unserializable"] += 1
            return
        r = _hot_client()
        if r is None:
            return
        try:
            r.set(key, payload, ex=_ttl())
            _note_redis_ok(r)
        except Exception:  # noqa: BLE001
            _mark_redis_down(r)
        return
    with _lock:
        _get("embed")[key] = list(vector)   # 저장도 복사본으로 — 호출부가 나중에 바꿔도 무관하게


# ── 검색 결과 층 ─────────────────────────────────────────────────────────────

def search_key(query: str, k: int, source_types: tuple[str, ...] | None, params: Any,
               embed_model: str, rerank_model: str) -> str:
    """결과를 좌우하는 **모든** 입력 + **현재 코퍼스 세대**로 키를 만든다.

    파라미터 5종은 RPA-149로 런타임에 바뀐다 — 키에 넣으면 변경 즉시 다른 키가 되므로
    별도 무효화 없이도 "튜닝이 바로 먹는다". 무효화보다 키 포함이 실수에 강하다.

    🔴 세대는 **여기서 한 번** 캡처된다. 호출부(services/rag.py)가 이 키로 get→계산→put을
    끝까지 진행하므로, 도중에 ingest가 세대를 올려도 이 요청의 put은 구 세대 키로 간다 —
    늦은 SET이 새 세대 독자에게 보이지 않는 것이 무효화의 정합성 조건이다(SCAN+DELETE의
    레이스를 이렇게 없앤다). 세대를 못 읽으면 'nogen' 키가 되고 get/put이 캐싱을 건너뛴다.
    """
    d = _digest(
        "search", _norm(query), k, tuple(source_types or ()),
        getattr(params, "candidate_pool_size", None),
        getattr(params, "rerank_candidates", None),
        getattr(params, "rrf_k", None),
        getattr(params, "vector_weight", None),
        getattr(params, "bm25_weight", None),
        embed_model, rerank_model,
    )
    # 캐시 OFF면 세대 조회(=Redis GET)를 하지 않는다 (Qodo #380): 호출부(rag.py)는 키를
    # 무조건 만들므로, 여기서 I/O를 타면 REDIS_URL만 설정된 캐시-off 배포의 검색이
    # Redis 지연·장애의 영향을 받는다 — "끄면 기존 동작 그대로" 계약 위반이다.
    # 키 세그먼트는 뭐가 되든 무관하다(get/put이 enabled()에서 이미 no-op).
    if not enabled():
        return f"{_ns()}:search:off:{d}"
    gen = _current_generation()
    seg = f"g{gen}" if gen is not None else "nogen"
    return f"{_ns()}:search:{seg}:{d}"


def _is_nogen(key: str) -> bool:
    return ":search:nogen:" in key


def get_search(key: str) -> list[dict] | None:
    if not enabled():
        return None
    if _redis_url():
        value = None
        if _is_nogen(key):
            with _lock:
                _stats["skip_no_generation"] += 1
                _stats["search_miss"] += 1
            return None
        r = _hot_client()
        if r is not None:
            try:
                raw = r.get(key)
                _note_redis_ok(r)
            except Exception:  # noqa: BLE001
                _mark_redis_down(r)
                raw = None
            if raw is not None:
                value = _load_validated(raw, r, key, _valid_search)
        with _lock:  # 디코드·검증 **후** 집계 — get_embedding과 같은 이유
            _stats["search_hit" if value is not None else "search_miss"] += 1
        return value
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

    Redis 모드 추가 스킵: ingest pending 마커(부분 적재 결과의 장기 캐싱 방지),
    nogen 키(세대 미상), 직렬화 투명성 위반.
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
    if _redis_url():
        if _is_nogen(key):
            with _lock:
                _stats["skip_no_generation"] += 1
            return "generation_unavailable"
        payload = _dump_transparent(results)
        if payload is None:
            with _lock:
                _stats["skip_unserializable"] += 1
            return "unserializable"
        r = _hot_client()
        if r is None:
            return None
        try:
            # pending 마커 확인 → SET. 마커 확인에 실패하면 "적재 중이 아님"을 증명할 수
            # 없으므로 저장하지 않는다(보수적 fail-open — miss일 뿐 오답은 아니다).
            if r.exists(_pending_key()):
                _note_redis_ok(r)
                with _lock:
                    _stats["skip_ingest_pending"] += 1
                return "ingest_pending"
            r.set(key, payload, ex=_ttl())
            _note_redis_ok(r)
        except Exception:  # noqa: BLE001
            _mark_redis_down(r)
        return None
    with _lock:
        _get("search")[key] = [dict(r_) for r_ in results]
    return None


def _get(which: str) -> TTLCache:
    global _embedding_cache, _search_cache
    if which == "embed":
        if _embedding_cache is None:
            _embedding_cache = TTLCache(maxsize=_maxsize(), ttl=_ttl())
        return _embedding_cache
    if _search_cache is None:
        _search_cache = TTLCache(maxsize=_maxsize(), ttl=_ttl())
    return _search_cache


# ── control path: ingest 무효화 프로토콜 (RPA-274) ──────────────────────────
# 순서: mark_ingest_pending() → 스토어 변경(PG→OpenSearch→refresh) → publish_generation()
# 실패 시 CacheInvalidationError — pipeline이 non-zero exit로 운영자에게 알린다.

def mark_ingest_pending() -> None:
    """적재 시작 마커 — 스토어를 만지기 **전에** 부른다.

    마커가 있는 동안 put_search가 저장을 건너뛰므로, 부분 적재 상태(PG만 성공 등)의
    검색 결과가 TTL 동안 캐싱되는 것을 막는다. 만료(_pending_ttl)는 실패한 ingest가
    영원히 캐싱을 죽이지 않게 하는 한도다 — 그 안에 재실행하는 것이 운영 계약.

    인프로세스 모드: no-op — ingest는 별도 프로세스라 서버의 로컬 캐시에 손댈 방법이
    없다(RPA-211 한계 그대로, TTL이 방어).
    """
    if not _redis_url():
        return
    _control_call("mark_ingest_pending",
                  lambda c: c.set(_pending_key(), str(int(time.time())), ex=_pending_ttl()))
    logger.info("rag_cache: ingest pending 마커 설정 (한도 %ds)", _pending_ttl())


def publish_generation() -> int:
    """새 코퍼스 세대를 원자적으로 공개하고(INCR) pending 마커를 지운다.

    ingest가 **모든 스토어 반영(refresh 포함)을 끝낸 뒤에만** 불러야 한다. INCR가
    성공하면 새 세대는 공개된 것 — 마커 삭제 실패는 경고로 낮춘다(마커는 만료로도
    풀리고, 남아 있는 동안 캐싱이 안 될 뿐 오답은 없다).

    인프로세스 모드: 로컬 세대만 올린다(이 프로세스 한정 — ingest CLI에선 사실상 no-op).
    """
    global _local_generation
    if not _redis_url():
        with _lock:
            _local_generation += 1
            gen = _local_generation
        _clear_local_search()  # 로컬 모드는 세대 전환+즉시 비움이 같은 뜻(메모리도 회수)
        return gen
    gen = int(_control_call("publish_generation", lambda c: c.incr(_gen_key())))
    try:
        _control_call("clear_ingest_pending", lambda c: c.delete(_pending_key()))
    except CacheInvalidationError:
        logger.warning("rag_cache: pending 마커 삭제 실패 — 만료(%ds)까지 신규 캐싱이 멈춘다 "
                       "(세대 전환 자체는 g%d로 완료)", _pending_ttl(), gen)
    return gen


def cleanup_old_generations() -> int:
    """구 세대 검색 키 정리 — **정합성 조건이 아니라 하이진**이다(TTL이 어차피 치운다).

    UNLINK(논블로킹)를 쓰고, 실패해도 조용히 0을 돌려준다 — 이게 실패한다고 ingest를
    실패시키면 안 된다(publish는 이미 끝났고 정합성은 세대 키가 보장한다).
    """
    if not _redis_url():
        return 0
    gen = _current_generation()
    if gen is None:
        return 0
    prefix = f"{_ns()}:search:g"
    current_seg = f"{prefix}{gen}:"
    removed = 0
    r = _hot_client()
    if r is None:
        return 0
    try:
        batch: list[str] = []
        for k in r.scan_iter(match=prefix + "*", count=500):
            if not k.startswith(current_seg):
                batch.append(k)
            if len(batch) >= 500:
                removed += _unlink(r, batch)
                batch = []
        if batch:
            removed += _unlink(r, batch)
        _note_redis_ok(r)
    except Exception:  # noqa: BLE001
        _mark_redis_down(r)
    return removed


def _unlink(r: Any, keys: list[str]) -> int:
    try:
        return int(r.unlink(*keys))
    except Exception:  # noqa: BLE001 — 구버전 서버 등 unlink 미지원이면 delete로
        return int(r.delete(*keys))


def _clear_local_search() -> None:
    global _search_cache
    with _lock:
        _search_cache = None


# ── 테스트·관측 ──────────────────────────────────────────────────────────────

def bust_all() -> None:
    """전체 초기화 — 테스트 픽스처용. Redis 모드에선 우리 네임스페이스만 지운다(best-effort)."""
    global _embedding_cache, _search_cache, _local_generation
    if _redis_url():
        r = _hot_client()
        if r is not None:
            try:
                batch = list(r.scan_iter(match=_ns() + ":*", count=500))
                if batch:
                    r.delete(*batch)
                _note_redis_ok(r)
            except Exception:  # noqa: BLE001
                _mark_redis_down(r)
    with _lock:
        _embedding_cache = None
        _search_cache = None
        _local_generation = 0
        for k in _stats:
            _stats[k] = 0


def stats() -> dict:
    """적중·미스·건너뜀 카운터. 건너뛴 사유를 함께 봐야 '적중률이 왜 낮은가'에 답할 수 있다.

    Redis 모드에서도 카운터는 프로세스별이다(전역 적중률은 인스턴스 합산으로 볼 것).
    backend는 **설정된 저장소**이고, 지금 실제로 붙을 수 있는지는 redis_circuit_open이
    말한다 — "URL이 있다"와 "쓸 수 있다"는 다른 명제다.
    """
    with _lock:
        s = dict(_stats)
    for layer in ("embed", "search"):
        hit, miss = s[f"{layer}_hit"], s[f"{layer}_miss"]
        s[f"{layer}_hit_rate"] = round(hit / (hit + miss), 3) if (hit + miss) else None
    s["enabled"] = enabled()
    s["ttl_seconds"] = _ttl()
    configured = bool(_redis_url())
    s["backend"] = "redis" if configured else "memory"
    with _state_lock:
        s["redis_configured"] = configured
        s["redis_circuit_open"] = bool(configured and time.monotonic() < _redis_down_until)
    s["generation"] = _current_generation()
    return s
