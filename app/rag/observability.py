"""검색/리랭커 파이프라인 각 단계를 가로채 JSON Lines로 기록하는 AOP 스타일 데코레이터.

hybrid_search.py/rerank.py 등 핵심 로직 안에 로깅 코드를 직접 흩뿌리지 않고,
@log_call(event)를 함수에 붙이는 것만으로 호출마다 시작/종료 시각·소요시간·
성공 여부·예외를 기록한다. 같은 검색 요청 안의 모든 단계(임베딩→벡터 검색→
BM25 검색→RRF→Reranker→API 응답)는 request_id로 묶여 나중에 하나의 흐름으로
재구성할 수 있다.
"""

import contextvars
import functools
import inspect
import json
import threading
import time
import uuid
from datetime import datetime, timezone

from . import config, event_queue

_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
_write_lock = threading.Lock()  # 동시 요청(3개 모드 동시 검색 등)이 같은 로그 파일에 겹쳐 쓰지 않도록


def new_request_id() -> str:
    """새 검색 요청의 시작점에서 호출 — 이후 같은 호출 흐름의 모든 로그가 이 id를 공유한다."""
    request_id = uuid.uuid4().hex[:12]
    _request_id_var.set(request_id)
    return request_id


def get_request_id() -> str | None:
    return _request_id_var.get()


# hybrid_search 이벤트에 함께 남길 RAG 설정 스냅샷 — "이 검색이 어떤 설정으로 돌았나"
# (chunk_size·모델·RRF 등)를 관측에서 바로 보게 한다. 값은 config가 env로 결정.
def _rag_config_snapshot() -> dict:
    return {
        "chunk_size": config.CHUNK_SIZE,
        "chunk_overlap": config.CHUNK_OVERLAP,
        "embedding_model": config.EMBEDDING_MODEL,
        "embedding_dim": config.EMBEDDING_DIM,
        "rerank_model": config.RERANK_MODEL,
        "rrf_k": config.RRF_K,
        "candidate_pool": config.HYBRID_CANDIDATE_POOL_SIZE,
        "rerank_candidates": config.HYBRID_RERANK_CANDIDATES,
    }


def _mask_record(record: dict) -> dict:
    """자유 텍스트(검색어 preview·error_message)를 마스킹한 복사본 반환 (RPA-128).

    JSONL·관측 DB **둘 다에 쓰기 전에** 공통 적용 — 한쪽만 마스킹하면 다른 쪽에 원문이
    남는다(CodeRabbit #188). 마스킹 모듈을 못 불러오면 **fail-closed**: 원문 유지가
    아니라 자유 텍스트 필드를 통째로 대체해 유출 가능성을 차단한다(관측성보다 안전 우선).
    """
    import copy

    r = copy.deepcopy(record)
    try:
        from app.core.masking import mask_pii
    except Exception:  # noqa: BLE001 — 마스킹 불가 시 자유 텍스트를 아예 제거(fail-closed)
        mask_pii = lambda _t: "[REDACTED]"  # noqa: E731
    args = r.get("args")
    if isinstance(args, dict):
        for v in args.values():
            if isinstance(v, dict) and isinstance(v.get("preview"), str):
                v["preview"] = mask_pii(v["preview"])
    if isinstance(r.get("error_message"), str):
        r["error_message"] = mask_pii(r["error_message"])
    return r


def _rag_event_row(record: dict):
    """마스킹된 record → RagEvent 행. 단건·배치 적재가 **같은 것**을 쓰게 하는 단일 출처.

    두 벌로 갈리면 컬럼 절단(40/120/20자)이나 config 스냅샷이 경로마다 달라진다.
    """
    from app import models

    detail = {k: v for k, v in record.items()
              if k not in ("request_id", "event", "function", "status", "duration_ms")}
    if record.get("event") == "hybrid_search":
        detail["config"] = _rag_config_snapshot()

    return models.RagEvent(
        request_id=record.get("request_id"),
        event=str(record.get("event"))[:40],
        function=(record.get("function") or None) and str(record["function"])[:120],
        status=(record.get("status") or None) and str(record["status"])[:20],
        duration_ms=record.get("duration_ms"),
        detail=json.dumps(detail, ensure_ascii=False, default=str),
    )


def _persist_rag_events(records: list[dict]) -> None:
    """관측 DB(Neon)에 RAG 이벤트를 **배치로** 적재 — 세션 1개 + COMMIT 1회 (RPA-221).

    건당 세션을 열면 pre_ping→INSERT→COMMIT→리셋으로 왕복이 4번 든다. 원격 Neon
    기준 실측 7건 2,327ms → 배치 858ms. INSERT 문장 수는 같고, 줄어드는 건 세션
    부대비용이다. 적재 실패가 검색을 죽이면 안 되므로 예외는 삼킨다.
    """
    if not records:
        return
    try:
        from app.core.observability_db import observability_sessionmaker

        with observability_sessionmaker()() as db:
            for record in records:
                db.add(_rag_event_row(record))
            db.commit()
    except Exception:  # noqa: BLE001 — 관측 적재 실패가 검색을 죽이면 안 됨
        pass


def _persist_rag_event(record: dict) -> None:
    """단건 적재 — 큐 비활성(RAG_EVENT_QUEUE=0) 시의 동기 경로.

    record는 _write_log에서 이미 마스킹된 것을 받는다.
    """
    _persist_rag_events([record])


event_queue.configure(_persist_rag_events)


def _write_log(record: dict) -> None:
    record = _mask_record(record)  # JSONL·관측 DB 쓰기 전에 공통 마스킹 (원문 유출 방지, #188)
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = config.LOG_DIR / f"rag-{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    with _write_lock:  # 여러 스레드(동시 검색 요청)가 같은 파일에 append할 때 줄이 섞이지 않게
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    # 관측 DB 적재는 큐에 넘긴다 (RPA-221) — 여기서 기다리면 원격 Neon 왕복 ~332ms가
    # 검색 시간에 얹히고, 비동기 경로에서는 이벤트 루프 전체가 그만큼 멈춘다.
    # **마스킹 뒤에** 넣는 것이 중요하다 — 큐에 원문이 앉으면 fail-closed가 깨진다(#188).
    #
    # 포화 시 동기 폴백을 하지 않는다: 큐가 찼다는 건 적재가 이미 밀린다는 뜻이라,
    # 거기서 동기로 기다리면 밀린 DB를 붙잡고 검색을 세운다 — 관측이 서비스를 끌어내리는
    # 바로 그 상황이다. 대신 드롭하고 카운터에 남긴다(event_queue.dropped_count).
    if event_queue.enabled():
        event_queue.enqueue(record)
    else:
        _persist_rag_event(record)


def log_event(event: str, **fields) -> None:
    """@log_call 데코레이터를 씌울 수 없는 곳(FastAPI 미들웨어 등)에서 직접 레코드를 남긴다.

    파일 기록 방식은 log_call과 완전히 동일해서(_write_log 공유), 검색 파이프라인
    로그와 HTTP 요청 로그가 같은 파일·같은 request_id 체계 안에 섞여 들어간다.
    """
    _write_log({"request_id": get_request_id(), "event": event, **fields})


def _summarize(value):
    if isinstance(value, str):
        return {"len": len(value), "preview": value[:80]}
    if isinstance(value, (list, tuple)):
        return {"count": len(value)}
    return value


def log_call(event: str, capture_args: tuple[str, ...] = (), capture_result=None):
    """함수 호출을 event 이름의 JSON Lines 레코드로 기록하는 데코레이터.

    capture_args: 로그에 남길 인자 이름들 (긴 문자열/리스트는 원문 대신 길이만 기록).
    capture_result: 반환값 -> 로그에 남길 요약 dict를 만드는 함수 (예: 결과 개수).
    """

    def decorator(func):
        signature = inspect.signature(func)

        def _args_summary(args, kwargs) -> dict:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            return {name: _summarize(bound.arguments.get(name)) for name in capture_args}

        # 비동기 검색 경로(app/rag/retrieval/*_async, store/*_async — RAG_ASYNC_SEARCH)에도
        # 같은 로깅을 붙이려면 async def를 그대로 삼키면 안 된다(await 없이 호출하면
        # 코루틴 객체가 result로 잡혀 capture_result가 깨진다) — 데코레이팅 시점에
        # iscoroutinefunction으로 분기해 별도 async wrapper를 반환한다.
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                started_at = datetime.now(timezone.utc)
                start = time.perf_counter()
                record = {
                    "request_id": get_request_id(),
                    "event": event,
                    "function": func.__qualname__,
                    "started_at": started_at.isoformat(),
                    "args": _args_summary(args, kwargs),
                }
                try:
                    result = await func(*args, **kwargs)
                except Exception as exc:
                    record.update(
                        status="error",
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                        duration_ms=round((time.perf_counter() - start) * 1000, 2),
                        ended_at=datetime.now(timezone.utc).isoformat(),
                    )
                    _write_log(record)
                    raise

                record.update(
                    status="ok",
                    duration_ms=round((time.perf_counter() - start) * 1000, 2),
                    ended_at=datetime.now(timezone.utc).isoformat(),
                )
                if capture_result:
                    record["result"] = capture_result(result)
                _write_log(record)
                return result

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            started_at = datetime.now(timezone.utc)
            start = time.perf_counter()

            record = {
                "request_id": get_request_id(),
                "event": event,
                "function": func.__qualname__,
                "started_at": started_at.isoformat(),
                "args": _args_summary(args, kwargs),
            }
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                record.update(
                    status="error",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    duration_ms=round((time.perf_counter() - start) * 1000, 2),
                    ended_at=datetime.now(timezone.utc).isoformat(),
                )
                _write_log(record)
                raise

            record.update(
                status="ok",
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
                ended_at=datetime.now(timezone.utc).isoformat(),
            )
            if capture_result:
                record["result"] = capture_result(result)
            _write_log(record)
            return result

        return wrapper

    return decorator
