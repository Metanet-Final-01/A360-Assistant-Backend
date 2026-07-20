"""관측 이벤트 적재를 핫패스에서 분리하는 배치 큐 (RPA-221).

**왜:** 검색 1회(p50 5,198ms)에 rag_events가 7건 쌓이는데, 관측 DB가 원격
Neon(ap-southeast-1)이라 적재 1건마다 왕복이 4번(pre_ping→INSERT→COMMIT→세션
리셋) 든다. 실측 검색당 ~2,327ms(45%)가 여기서 나갔다. 로컬 docker에서는 같은
적재가 2.8ms라 코드가 아니라 네트워크 왕복이 원인임이 확정됐다.

**왜 스레드 큐인가:** 생산자가 세 종류다 — asyncio.to_thread 워커(에이전트 검색),
이벤트 루프 코루틴(/api/rag/search), 평범한 동기 스레드(적재·스크립트).
asyncio.Queue는 이벤트 루프에서만 안전해 스레드 생산자를 못 받는다. queue.Queue의
put_nowait는 스레드 세이프하고 블로킹하지 않아 코루틴에서 불러도 루프를 안 막는다.
(기존 동기 호출은 코루틴 안에서 Neon 왕복 ~332ms를 기다리며 **이벤트 루프 전체를**
세우고 있었다 — 비동기 경로가 오히려 피해가 컸다.)

**왜 워커 1개인가:** 이벤트 순서를 보존한다. 여러 워커가 붙으면 같은 검색의
단계 순서가 뒤집혀 타임라인 재구성이 어긋난다.

적재 자체는 호출부가 주입한다(`configure`) — 이 모듈은 스케줄링만 알고 스키마는
모른다. app.rag.observability가 이 모듈을 임포트하므로 역방향 임포트는 없다.
"""

import logging
import os
import queue
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

# 큐 상한 — 넘치면 드롭한다. 무제한이면 관측 DB 장애가 메모리 고갈로 번진다.
_MAXSIZE = 10_000
# 한 트랜잭션에 묶을 최대 건수.
#
# 100인 이유: SQLAlchemy가 executemany(insertmanyvalues)로 묶어 **배치 크기와 무관하게
# INSERT 문장이 1개**다(로컬 계측). 즉 배치 비용이 크기에 대해 평평하다 —
# 왕복 4회(pre_ping·INSERT·COMMIT·리셋) ≈ 304ms로 20건이든 100건이든 같다.
# 그래서 처리량 상한만 9.4 → 47 검색/초로 올라간다(검색 1회 = 7건 기준).
# 대가는 배치 하나가 실패할 때 잃는 양뿐인데, rag_events는 best-effort 관측이고
# 워커는 적재 실패로 죽지 않는다. 지연은 안 늘어난다 — 100건이 창 안에 안 차면
# _FLUSH_INTERVAL_SEC에 그냥 나간다.
_BATCH_SIZE = 100
# 배치가 안 차도 이 시간 안에는 내보낸다 — 유실 창을 생산자와 무관하게 일정히 유지.
_FLUSH_INTERVAL_SEC = 1.0
# 드롭 경고 스로틀 — 포화 시 로그가 폭주하면 그 자체가 장애가 된다.
_DROP_LOG_EVERY = 100

_lock = threading.Lock()
_queue: queue.Queue | None = None
_worker: threading.Thread | None = None
_flush_fn: Callable[[list[dict]], None] | None = None
_dropped = 0
_STOP = object()  # 워커 종료 sentinel

# 워커가 배치를 **로컬에 들고 있는 동안**은 큐가 비어도 적재가 안 끝난 것이다.
# 큐가 빈 것만 보고 "flush 됐다"고 하면 종료 시 마지막 배치를 잃는다.
_idle = threading.Event()
_idle.set()
# flush() 요청 — 워커가 배치 채우기를 즉시 중단하고 내보내게 한다(종료·테스트 지연 방지).
_flush_now = threading.Event()


def enabled() -> bool:
    """기본 활성. RAG_EVENT_QUEUE=0/false/off면 기존 동기 적재로 되돌린다.

    성능 변경이므로 기본을 끄면 아무 효과가 없다 — 대신 문제 시 즉시 원복할
    수 있게 토글을 남긴다(RAG_CACHE_ENABLED와 반대 방향의 기본값인 이유).
    """
    raw = os.getenv("RAG_EVENT_QUEUE", "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def configure(flush_fn: Callable[[list[dict]], None]) -> None:
    """배치 적재 함수를 등록한다. 호출부(observability)가 스키마를 소유한다."""
    global _flush_fn
    _flush_fn = flush_fn


def dropped_count() -> int:
    """큐 포화로 버린 이벤트 수 — 무음 유실 방지용(대시보드가 조용히 과소보고하면 안 된다)."""
    return _dropped


def enqueue(record: dict) -> bool:
    """이벤트를 큐에 넣는다. 넣었으면 True, 포화로 버렸으면 False.

    절대 블로킹하지 않는다 — 이벤트 루프에서 불릴 수 있다.
    """
    global _dropped
    q = _ensure_worker()
    if q is None:
        return False
    try:
        _idle.clear()  # put보다 먼저 — 그래야 flush()가 '적재 완료'를 앞질러 보고하지 않는다
        q.put_nowait(record)
        return True
    except queue.Full:
        with _lock:
            _dropped += 1
            count = _dropped
        # 관측 적재가 밀렸다는 뜻이라 log_event로 남기면 재귀·증폭이 된다. logger만 쓴다.
        if count % _DROP_LOG_EVERY == 1:
            logger.warning("관측 이벤트 큐 포화 — 누적 드롭 %d건 (적재 지연/장애 의심)", count)
        return False


def _ensure_worker() -> "queue.Queue | None":
    global _queue, _worker
    if _worker is not None and _worker.is_alive():
        return _queue
    with _lock:
        if _worker is not None and _worker.is_alive():
            return _queue
        if _flush_fn is None:
            return None  # 적재 함수 미등록 — 호출부가 동기 폴백을 쓴다
        _queue = queue.Queue(maxsize=_MAXSIZE)
        _worker = threading.Thread(
            target=_run, args=(_queue, _flush_fn), name="rag-event-writer", daemon=True
        )
        _worker.start()
    return _queue


def _collect(q: "queue.Queue") -> "tuple[list[dict], bool]":
    """다음 배치를 모은다 → (레코드들, 종료요청여부).

    첫 건은 대기해서 받고, 그 뒤 _FLUSH_INTERVAL_SEC 안에서 _BATCH_SIZE까지 채운다.
    """
    batch: list[dict] = []
    try:
        first = q.get(timeout=_FLUSH_INTERVAL_SEC)
    except queue.Empty:
        return batch, False
    if first is _STOP:
        return batch, True
    _idle.clear()  # 배치를 들고 있는 동안은 idle이 아니다
    batch.append(first)

    deadline = time.monotonic() + _FLUSH_INTERVAL_SEC
    while len(batch) < _BATCH_SIZE:
        if _flush_now.is_set():
            break  # flush 요청 — 창을 다 안 기다리고 즉시 내보낸다
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            # 창이 남아도 짧게 끊어 받는다 — 그래야 flush 요청을 곧바로 알아챈다.
            item = q.get(timeout=min(remaining, 0.05))
        except queue.Empty:
            continue
        if item is _STOP:
            return batch, True  # 남은 배치는 내보내고 종료
        batch.append(item)
    return batch, False


def _run(q: "queue.Queue", flush_fn: Callable[[list[dict]], None]) -> None:
    while True:
        batch, stopping = _collect(q)
        if batch:
            try:
                flush_fn(batch)
            except Exception:  # noqa: BLE001 — 적재 실패가 워커를 죽이면 이후 전부 유실된다
                logger.warning("관측 이벤트 배치 적재 실패 (%d건 유실)", len(batch), exc_info=True)
        if q.empty():
            _idle.set()
        if stopping:
            return


def flush(timeout: float = 5.0) -> bool:
    """대기 중인 이벤트가 **실제로 적재될 때까지** 기다린다(테스트·종료 경로).

    큐가 빈 것만 보면 안 된다 — 워커가 배치를 로컬에 들고 배치 창을 기다리는
    동안에도 큐는 비어 있다. 큐가 비었고(_queue) 워커도 손을 뗀(_idle) 상태를
    함께 본다. 동시에 _flush_now로 배치 창을 즉시 끊어 대기를 줄인다.
    """
    q = _queue
    if q is None:
        return True
    _flush_now.set()
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if q.empty() and _idle.is_set():
                return True
            time.sleep(0.01)
        return q.empty() and _idle.is_set()
    finally:
        _flush_now.clear()


def stop(timeout: float = 5.0) -> None:
    """남은 이벤트를 내보내고 워커를 정리한다 (main.py lifespan)."""
    global _queue, _worker
    with _lock:
        q, w = _queue, _worker
    if q is None or w is None:
        return
    flush(timeout=timeout)
    try:
        q.put_nowait(_STOP)
    except queue.Full:
        logger.warning("관측 이벤트 큐가 가득 차 종료 신호를 넣지 못했다")
    w.join(timeout=timeout)
    with _lock:
        _queue, _worker = None, None


def reset_for_tests() -> None:
    """테스트 격리용 — 워커를 멈추고 카운터·플래그를 초기화한다."""
    global _dropped
    stop(timeout=2.0)
    with _lock:
        _dropped = 0
    _flush_now.clear()
    _idle.set()
