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
import time

from app.db import SessionLocal
from app.models import RetrievalParamOverride
from app.rag.retrieval.params import RetrievalParams

logger = logging.getLogger(__name__)

# DB 조회 주기 상한(초). 값 변경은 bust_cache()로 즉시 반영되므로, 이 TTL은 "PUT을 안 거친
# 경로(직접 SQL 등)로 바뀐 값을 늦게라도 반영"하는 안전망 겸 부하 방어일 뿐이다.
_CACHE_TTL_SEC = 30.0

# (monotonic 시각, 파라미터) — None이면 미로드. monotonic이라 시스템 시계 변경에 안 흔들린다.
_cache: tuple[float, RetrievalParams] | None = None


def load_active_params() -> RetrievalParams:
    """현재 활성 검색 파라미터. DB 오버라이드가 있으면 그걸, 없으면 .env 기본값을 준다.

    TTL 내 재호출은 캐시를 돌려준다. 검색 hot path에서 불리므로 DB 왕복을 최소화한다.
    """
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _CACHE_TTL_SEC:
        return _cache[1]
    params = _read_override() or RetrievalParams.from_config()
    _cache = (now, params)
    return params


def bust_cache() -> None:
    """캐시 무효화 — admin PUT 직후 호출해 다음 load_active_params()가 DB를 다시 읽게 한다."""
    global _cache
    _cache = None


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
