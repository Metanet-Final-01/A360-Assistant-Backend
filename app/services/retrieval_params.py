"""활성 검색 파라미터 로더 — 검색 경로가 재시작 없이 튜닝값을 읽게 한다 (RPA-149).

config는 import 시점에 모듈 상수로 고정돼 .env를 바꿔도 재시작 전엔 안 먹는다. 이 모듈은
앱 DB의 retrieval_params 최신 행을 읽어 RetrievalParams를 만들고, 행이 없으면
RetrievalParams.from_config()(.env 기본값)로 폴백한다 — 그래서 오버라이드가 없는 로컬/데모는
기존과 100% 동일하게 동작한다.

매 검색마다 DB를 때리지 않도록 짧은 TTL 캐시를 둔다. admin API가 값을 바꾸면 bust_cache()로
즉시 무효화해 무중단 반영한다(다음 검색부터 새 값). DB 조회가 실패해도 검색을 죽이지 않고
config 폴백으로 저하시킨다 — 튜닝 저장소 장애가 검색 가용성을 깨면 안 되기 때문.
"""

import logging
import threading
import time

from app.db import SessionLocal
from app.services import config_bus
from app.models import RetrievalParamOverride
from app.rag.retrieval.params import RetrievalParams

logger = logging.getLogger(__name__)

# DB 조회 주기 상한(초). 값 변경은 bust_cache()로 즉시 반영되므로, 이 TTL은 "PUT을 안 거친
# 경로(직접 SQL 등)로 바뀐 값을 늦게라도 반영"하는 안전망 겸 부하 방어일 뿐이다.
_CACHE_TTL_SEC = 30.0

# (monotonic 시각, 파라미터) — None이면 미로드. monotonic이라 시스템 시계 변경에 안 흔들린다.
_cache: tuple[float, RetrievalParams] | None = None

# 캐시 무효화 세대 — bust_cache()가 올린다. 조회 스레드는 "읽기 시작 시점의 세대"와 저장 직전
# 세대가 같을 때만 캐시에 쓴다 (RPA-175, budget.py와 동일 기법).
#
# 왜 필요한가: 조회가 _read_override()로 옛 행을 읽는 동안 admin PUT이 새 값을 쓰고 bust_cache()를
# 부르면, 조회 스레드가 그 뒤에 `_cache = (now, 옛값)`을 실행해 **무효화를 되돌린다** — 최대 TTL
# 30초 동안 변경 전 파라미터가 검색에 적용된다. sync 검색 라우트는 스레드풀에서 병렬로 도므로 실재.
# 왜 락 직렬화가 아닌가: 락을 DB 조회 전체에 걸면 그동안 모든 검색이 대기한다(hot path). 세대
# 비교는 짧은 임계구역만 잠그고 조회는 병렬로 둔다 — 최악의 경우 캐시를 한 번 못 채울 뿐이다.
_lock = threading.Lock()
_generation = 0


def load_active_params() -> RetrievalParams:
    """현재 활성 검색 파라미터. DB 오버라이드가 있으면 그걸, 없으면 .env 기본값을 준다.

    TTL 내 재호출은 캐시를 돌려준다. 검색 hot path에서 불리므로 DB 왕복을 최소화한다.
    """
    now = time.monotonic()
    with _lock:
        cached = _cache
        gen_at_read = _generation  # 이 조회가 "시작된" 세대 — 저장 직전에 다시 비교한다
    if cached is not None and now - cached[0] < _CACHE_TTL_SEC:
        return cached[1]
    params = _read_override() or RetrievalParams.from_config()  # DB 왕복 — 락 밖에서
    _store_if_current(gen_at_read, now, params)
    return params


def _store_if_current(gen_at_read: int, now: float, params: RetrievalParams) -> None:
    """읽는 동안 무효화가 없었을 때만 캐시에 쓴다 — 무효화를 되돌리지 않기 위해 (RPA-175)."""
    global _cache
    with _lock:
        if gen_at_read == _generation:
            _cache = (now, params)
        else:
            # 내가 읽는 사이 admin PUT이 값을 바꿨다 — 내 값은 이미 낡았으니 캐시에 넣지 않는다.
            # 다음 호출이 새 값을 다시 읽는다(한 번의 DB 왕복 손해일 뿐).
            logger.debug("retrieval_params 캐시 저장 생략 — 조회 중 무효화됨")


def bust_cache_local() -> None:
    """이 프로세스의 캐시만 비운다 — **전파하지 않는다**.

    세대를 올려, **지금 조회 중인 스레드가 옛 값을 캐시에 되돌려놓는 것**도 함께 막는다.

    ⚠️ 전파 메시지를 **수신**했을 때 부르는 것이 이 함수다. 거기서 `bust_cache()`를 부르면
    수신할 때마다 다시 발행해 무한 루프가 된다 (RPA-275).
    """
    global _cache, _generation
    with _lock:
        _cache = None
        _generation += 1


def bust_cache() -> None:
    """캐시 무효화 — admin PUT 직후 호출해 다음 load_active_params()가 DB를 다시 읽게 한다.

    로컬을 먼저 비우고, 다른 인스턴스에도 알린다 (RPA-275). ASG가 2대면 PUT을 받지 않은
    인스턴스는 전파가 없을 때 TTL(30초)까지 옛 파라미터로 검색한다.

    전파는 최적화이고 TTL이 최종 방어다 — publish가 실패해도 이 함수는 조용히 성공한다.
    """
    bust_cache_local()
    config_bus.publish("retrieval_params")


def _read_override() -> RetrievalParams | None:
    """retrieval_params 최신 행을 RetrievalParams로. 행 없거나 조회 실패면 None(→config 폴백).

    RetrievalParams(...) 생성이 곧 __post_init__ 검증이라, DB에 어쩌다 잘못된 값이 들어가 있어도
    (직접 SQL 등) 여기서 걸러 config 폴백으로 저하시킨다 — 깨진 값으로 검색이 무너지지 않게.
    """
    db = SessionLocal()
    try:
        row = (
            db.query(RetrievalParamOverride)
            .order_by(RetrievalParamOverride.id.desc())
            .first()
        )
        if row is None:
            return None
        return RetrievalParams(
            candidate_pool_size=row.candidate_pool_size,
            rerank_candidates=row.rerank_candidates,
            rrf_k=row.rrf_k,
            vector_weight=row.vector_weight,
            bm25_weight=row.bm25_weight,
        )
    except Exception:
        logger.warning("retrieval_params 오버라이드 조회 실패 — .env 기본값으로 폴백", exc_info=True)
        return None
    finally:
        db.close()
