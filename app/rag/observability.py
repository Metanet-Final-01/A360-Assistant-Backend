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

from . import config

_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
_write_lock = threading.Lock()  # 동시 요청(3개 모드 동시 검색 등)이 같은 로그 파일에 겹쳐 쓰지 않도록


def new_request_id() -> str:
    """새 검색 요청의 시작점에서 호출 — 이후 같은 호출 흐름의 모든 로그가 이 id를 공유한다."""
    request_id = uuid.uuid4().hex[:12]
    _request_id_var.set(request_id)
    return request_id


def get_request_id() -> str | None:
    return _request_id_var.get()


def _write_log(record: dict) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = config.LOG_DIR / f"rag-{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    with _write_lock:  # 여러 스레드(동시 검색 요청)가 같은 파일에 append할 때 줄이 섞이지 않게
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)


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

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            started_at = datetime.now(timezone.utc)
            start = time.perf_counter()

            record = {
                "request_id": get_request_id(),
                "event": event,
                "function": func.__qualname__,
                "started_at": started_at.isoformat(),
                "args": {name: _summarize(bound.arguments.get(name)) for name in capture_args},
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
