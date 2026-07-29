# -*- coding: utf-8 -*-
"""설정 캐시 무효화 전파 — 인스턴스 간 (RPA-275).

## 무엇을 푸는가

`retrieval_params`(30초 TTL)·`budget`(30초 TTL)의 `bust_cache()`는 **admin PUT을 받은 그
프로세스에서만** 실행된다. 백엔드 ASG는 `MaxCapacity: 2`라 나머지 한 대는 TTL이 지날 때까지
옛 설정으로 동작한다 — 예산 상한을 내렸는데 **한 서버는 최대 30초 동안 옛 상한을 허용**한다.

## 왜 지금인가

이 문제 하나로는 Redis 도입을 정당화하지 못한다(그래서 과거 PR #239에서 한 번 거절됐다).
RAG 캐시 Redis 백엔드(RPA-274)로 Redis가 이미 배포에 들어온 뒤라, 여기 붙는 건 rider다.

## 🔴 전파는 최적화이고, TTL이 최종 방어다

pub/sub을 잃어도 **현행 동작(최대 TTL 지연)으로 저하될 뿐** 정합성이 깨지지 않는다. 그래서
이 모듈의 모든 실패는 삼키고 로그만 남긴다 — 전파 실패가 admin PUT을 500으로 만들면
본말전도다. `REDIS_URL` 미설정이면 전 기능 no-op이라 로컬·CI는 도입 전과 동일하다.

## 자기 메시지는 건너뛴다

Redis pub/sub은 발행자에게도 그대로 배달된다. 발행한 인스턴스는 이미 로컬 무효화를 끝냈으므로
다시 처리할 이유가 없다 — 페이로드의 `origin`으로 자기 것을 걸러낸다. 무효화 자체는 멱등이라
안 걸러도 정합성 문제는 없지만, 불필요하게 세대를 올려 **진행 중인 조회의 캐시 저장을 무의미하게
무효화**하는 것을 피한다.
"""

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from app.core import config

logger = logging.getLogger(__name__)

# 이 프로세스 식별자 — 자기가 보낸 메시지를 걸러내는 용도(재기동마다 새로 생겨도 무방).
_INSTANCE_ID = uuid.uuid4().hex

_RECONNECT_MIN_SEC = 1.0
_RECONNECT_MAX_SEC = 30.0
_POLL_TIMEOUT_SEC = 1.0  # get_message 대기 — 종료 신호에 이 주기로 반응한다

_thread: threading.Thread | None = None
# 🔴 정지 신호는 **스레드마다 따로** 둔다 (Qodo #459). 모듈 공유 Event 하나를 쓰면, stop()이
# join 타임아웃으로 스레드를 남긴 채 참조만 지운 뒤 start()가 그 Event를 clear하는 순간
# **죽어가던 스레드가 되살아나** 구독자가 둘이 된다(핸들러 중복 호출 + 좀비).
_thread_stop: threading.Event | None = None
_lock = threading.Lock()

# 발행 전용 클라이언트 — 호출마다 새로 만들면 연결 풀이 매번 생겨 정리가 GC에 의존한다
# (Qodo #459). URL이 바뀌면 교체하고, 실패하면 버려 다음 호출이 새로 연결한다.
_pub_client: Any = None
_pub_url: str | None = None
# 구독이 실제로 붙었는지 — pub/sub은 보존이 없어 구독 전 발행은 그냥 사라진다. 기동 직후
# 발행이 유실되는 창을 호출부(주로 테스트)가 기다릴 수 있게 신호로 노출한다.
_ready = threading.Event()


def _redis_url() -> str:
    """호출 시점 읽기 — 설정 레지스트리 경유(RPA-224 래칫). 테스트가 켜고 끌 수 있어야 한다."""
    return (config.REDIS_URL or "").strip()


def enabled() -> bool:
    """전파가 켜져 있나 — `REDIS_URL` 미설정이면 publish·구독 전부 no-op."""
    return bool(_redis_url())


def _make_client(url: str) -> Any:
    """redis 클라이언트 팩토리 — 테스트가 fakeredis로 갈아끼운다(rag_cache 선례).

    ⚠️ **동기** 클라이언트를 쓴다. 발행 지점(`bust_cache`)이 동기 함수이고, 구독은 본래
    블로킹 루프라 전용 스레드가 맞다 — async로 만들 이유가 없다.
    """
    import redis  # noqa: PLC0415 — 미설정 모드는 redis 패키지를 안 탄다

    return redis.Redis.from_url(
        url, socket_connect_timeout=2.0, socket_timeout=2.0, decode_responses=True
    )


def _channel() -> str:
    env = (config.APP_ENV or "development").strip() or "development"
    return f"a360:{env}:config_bust"


def _publisher() -> Any:
    """발행 전용 클라이언트(URL 바뀌면 교체). 연결 churn을 막기 위해 재사용한다."""
    global _pub_client, _pub_url
    url = _redis_url()
    if _pub_client is None or _pub_url != url:
        old, _pub_client, _pub_url = _pub_client, _make_client(url), url
        _close_quietly(old)
    return _pub_client


def _drop_publisher() -> None:
    """발행 실패 후 캐시된 클라이언트를 버린다 — 끊긴 연결을 계속 재사용하지 않게."""
    global _pub_client, _pub_url
    old, _pub_client, _pub_url = _pub_client, None, None
    _close_quietly(old)


def _close_quietly(resource: Any) -> None:
    if resource is None:
        return
    try:
        resource.close()
    except Exception:  # noqa: BLE001 — 정리 실패는 무시(이미 끊긴 연결 등)
        pass


def publish(target: str) -> None:
    """다른 인스턴스에 `target` 캐시를 비우라고 알린다 (실패는 삼킨다).

    호출부는 **로컬 무효화를 먼저 끝낸 뒤** 부른다 — 전파가 실패해도 이 인스턴스는 이미
    최신이고, 나머지는 TTL로 수렴한다.
    """
    if not enabled():
        return
    try:
        payload = json.dumps({"target": target, "origin": _INSTANCE_ID}, ensure_ascii=False)
        _publisher().publish(_channel(), payload)
    except Exception:  # noqa: BLE001 — 전파 실패가 admin PUT을 죽이면 안 된다
        _drop_publisher()
        logger.warning("설정 무효화 전파 실패 (무시): target=%s", target, exc_info=True)


def _handle(raw: str, handlers: Mapping[str, Callable[[], None]]) -> None:
    """수신 메시지 1건 처리 — 형식이 깨졌거나 모르는 target이면 조용히 버린다."""
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("설정 무효화 메시지 파싱 실패 (무시): %r", raw[:200])
        return
    if not isinstance(msg, dict):
        return
    if msg.get("origin") == _INSTANCE_ID:
        return  # 내가 보낸 것 — 이미 로컬에서 처리했다
    target = msg.get("target")
    handler = handlers.get(target) if isinstance(target, str) else None
    if handler is None:
        logger.debug("설정 무효화: 모르는 target 무시 (%r)", target)
        return
    try:
        handler()
        logger.info("설정 무효화 수신 — 로컬 캐시 비움: target=%s", target)
    except Exception:  # noqa: BLE001 — 한 핸들러 실패가 구독 루프를 죽이면 안 된다
        logger.warning("설정 무효화 처리 실패: target=%s", target, exc_info=True)


def _run(handlers: Mapping[str, Callable[[], None]], stop_event: threading.Event) -> None:
    """구독 루프 — 끊기면 백오프 후 재구독한다.

    구독이 영구히 죽으면 전파만 사라지고 TTL 폴백으로 돌아간다. 그래도 조용히 죽게 두지는
    않는다 — 재연결을 계속 시도하고 실패를 로그로 남긴다.
    """
    delay = _RECONNECT_MIN_SEC
    while not stop_event.is_set():
        pubsub = None
        try:
            client = _make_client(_redis_url())
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(_channel())
            _ready.set()
            logger.info("설정 무효화 구독 시작: %s", _channel())
            delay = _RECONNECT_MIN_SEC  # 연결 성공 — 백오프 리셋
            while not stop_event.is_set():
                # 블로킹 listen() 대신 짧은 타임아웃 폴링 — 종료 신호에 즉시 반응하기 위해.
                message = pubsub.get_message(timeout=_POLL_TIMEOUT_SEC)
                if message and message.get("type") == "message":
                    _handle(message.get("data") or "", handlers)
        except Exception:  # noqa: BLE001 — 연결 끊김 등. 전파는 최적화라 앱을 죽이지 않는다
            if stop_event.is_set():
                break
            _ready.clear()
            logger.warning("설정 무효화 구독 끊김 — %.0f초 후 재시도", delay, exc_info=True)
            stop_event.wait(delay)
            delay = min(delay * 2, _RECONNECT_MAX_SEC)
        finally:
            _close_quietly(pubsub)


def start(handlers: Mapping[str, Callable[[], None]]) -> bool:
    """구독 스레드를 켠다. 켰으면 True, 미설정·중복 호출이면 False.

    handlers는 `{target: 로컬 전용 무효화 함수}`다. **반드시 전파하지 않는(로컬 전용) 함수**를
    줘야 한다 — 전파하는 `bust_cache()`를 주면 수신할 때마다 다시 발행해 무한 루프가 된다.
    """
    global _thread, _thread_stop
    if not enabled():
        logger.info("설정 무효화 전파 비활성 (REDIS_URL 미설정) — TTL로만 수렴")
        return False
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        stop_event = threading.Event()
        _thread_stop = stop_event
        _ready.clear()
        _thread = threading.Thread(
            target=_run, args=(dict(handlers), stop_event),
            name="config-bust-subscriber", daemon=True,
        )
        _thread.start()
        return True


def wait_ready(timeout: float = 5.0) -> bool:
    """구독이 실제로 붙을 때까지 기다린다 (붙었으면 True).

    pub/sub은 보존이 없어 **구독 전에 발행된 메시지는 사라진다.** 기동 직후 곧바로 발행하는
    경로(테스트가 대표적)는 이걸 기다려야 결정적으로 동작한다.
    """
    return _ready.wait(timeout)


def stop(timeout: float = 3.0) -> None:
    """구독 스레드를 멈춘다 (기동 안 했으면 no-op)."""
    global _thread, _thread_stop
    with _lock:
        thread, _thread = _thread, None
        stop_event, _thread_stop = _thread_stop, None
    if stop_event is not None:
        # 이 스레드 전용 신호라, 이후 start()가 새 Event를 만들어도 **이 스레드는 되살아나지
        # 않는다** — join이 타임아웃돼도 스스로 종료된다.
        stop_event.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
    _ready.clear()


def reset_for_tests() -> None:
    """테스트 격리용 — 스레드를 멈추고 상태를 초기화한다."""
    stop(timeout=1.0)
    _ready.clear()
    _drop_publisher()
