"""설정 캐시 무효화 전파 (RPA-275) — 인스턴스 간 pub/sub.

무엇을 보는가:
- 인스턴스 A의 `bust_cache()`가 **B의 로컬 캐시까지** 비우는가(핵심 계약)
- `REDIS_URL` 미설정이면 **도입 전과 동일**한가(전파 없음, 예외 없음)
- 전파(publish)가 실패해도 admin PUT 경로가 죽지 않는가 — 전파는 최적화, TTL이 최종 방어
- 수신 핸들러가 **다시 발행하지 않는가**(무한 루프 방지)
- 자기가 보낸 메시지는 건너뛰는가

실 Redis 없이 fakeredis로 돈다 — rag_cache/turn_stream 테스트와 같은 방식이다.
"""

import json
import threading
import time

import fakeredis
import pytest

import app.services.budget as budget
import app.services.config_bus as config_bus
import app.services.retrieval_params as rp


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    config_bus.reset_for_tests()


@pytest.fixture
def redis_on(monkeypatch):
    """config_bus를 fakeredis로 켠다. 같은 FakeServer를 발행·구독이 공유한다."""
    server = fakeredis.FakeServer()
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setattr(
        config_bus, "_make_client",
        lambda url: fakeredis.FakeRedis(server=server, decode_responses=True),
    )
    return server


def _publish_from_other(server, target: str) -> None:
    """**다른 인스턴스**가 보낸 것처럼 원문 페이로드를 직접 발행한다.

    ⚠️ `config_bus._INSTANCE_ID`를 monkeypatch하면 안 된다 — 발행부와 수신부가 같은 모듈
    전역을 읽으므로 origin이 항상 일치해 "자기 메시지"로 걸러진다(실제로 그렇게 테스트 5건이
    조용히 헛돌았다). 페이로드를 직접 만들어야 외부 인스턴스를 진짜로 흉내낸다.
    """
    payload = json.dumps({"target": target, "origin": "other-instance"}, ensure_ascii=False)
    fakeredis.FakeRedis(server=server, decode_responses=True).publish(config_bus._channel(), payload)


def _wait(predicate, timeout=5.0):
    """구독 스레드가 비동기로 처리하므로 조건이 참이 될 때까지 짧게 폴링한다."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# --- 핵심 계약: 다른 인스턴스까지 비운다 ---

def test_bust_propagates_to_other_instance(redis_on, monkeypatch):
    """인스턴스 A의 bust_cache() → B의 로컬 캐시가 비워진다 (RPA-275 완료 조건).

    전파가 없으면 B는 TTL(30초)까지 옛 값으로 동작한다 — 예산 상한을 내린 직후가 특히 위험하다.
    """
    received = []
    config_bus.start({"budget": lambda: received.append("budget")})
    assert config_bus.wait_ready(), "구독이 안 붙었다"

    _publish_from_other(redis_on, "budget")  # "인스턴스 A"가 보낸 것

    assert _wait(lambda: received == ["budget"]), "다른 인스턴스의 무효화를 못 받았다"


def test_bust_cache_busts_local_cache_of_subscriber(redis_on, monkeypatch):
    """수신 시 실제로 budget 캐시가 비워지는지 — 핸들러 호출이 아니라 **상태**로 확인한다."""
    budget._cache = (time.monotonic(), {"global_daily": 999.0})
    config_bus.start({"budget": budget.bust_cache_local})
    assert config_bus.wait_ready(), "구독이 안 붙었다"

    _publish_from_other(redis_on, "budget")

    assert _wait(lambda: budget._cache is None), "수신했는데 로컬 캐시가 남아 있다"


def test_own_message_is_skipped(redis_on):
    """자기가 보낸 메시지는 건너뛴다 — 이미 로컬에서 처리했다."""
    received = []
    config_bus.start({"budget": lambda: received.append("budget")})
    assert config_bus.wait_ready(), "구독이 안 붙었다"

    config_bus.publish("budget")            # origin == 내 INSTANCE_ID — 무시돼야 한다
    _publish_from_other(redis_on, "budget")  # 센티넬: 이게 처리되면 앞의 것도 이미 지나갔다

    # pub/sub은 채널 내 순서를 지키므로, 센티넬 1건만 남으면 자기 메시지는 걸러진 것이다.
    # (고정 sleep보다 결정적이다 — CI 부하에도 흔들리지 않는다.)
    assert _wait(lambda: received == ["budget"]), f"센티넬을 못 받았다: {received}"
    assert received == ["budget"], "자기 메시지까지 처리했다 — 불필요하게 세대를 올린다"


# --- 무한 루프 방지 ---

def test_receiving_does_not_republish(redis_on, monkeypatch):
    """수신 핸들러가 다시 발행하면 무한 루프다 — 로컬 전용 bust를 쓰는지 계약으로 고정한다."""
    published = []
    real_publish = config_bus.publish

    def _spy(target):
        published.append(target)
        real_publish(target)

    monkeypatch.setattr(config_bus, "publish", _spy)
    config_bus.start({"budget": budget.bust_cache_local})  # 로컬 전용을 넘긴다
    assert config_bus.wait_ready(), "구독이 안 붙었다"

    budget._cache = (time.monotonic(), {"global_daily": 1.0})
    _publish_from_other(redis_on, "budget")  # 스파이를 거치지 않는 외부 발행

    # 캐시가 비워진 시점 = 핸들러가 실제로 돌아간 시점. 그때 재발행이 없었어야 한다.
    assert _wait(lambda: budget._cache is None), "핸들러가 안 돌았다"
    assert published == [], "수신 처리 중 재발행이 일어났다 — 루프가 된다"


# --- Redis 미설정: 기존 동작 보존 ---

def test_disabled_without_redis(monkeypatch):
    """`REDIS_URL` 미설정이면 전파도 구독도 없고, 예외도 없다(도입 전과 동일)."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert config_bus.enabled() is False
    assert config_bus.start({"budget": lambda: None}) is False
    config_bus.publish("budget")  # no-op — 예외 없이 지나가야 한다


def test_bust_cache_works_without_redis(monkeypatch):
    """미설정 상태에서도 bust_cache()는 로컬 무효화를 정상 수행한다."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    rp._cache = (time.monotonic(), object())
    rp.bust_cache()
    assert rp._cache is None


# --- 실패해도 요청 경로를 죽이지 않는다 ---

def test_publish_failure_does_not_break_bust(monkeypatch):
    """전파 실패가 admin PUT을 500으로 만들면 본말전도다 — 로컬 무효화는 이미 끝났다."""
    monkeypatch.setenv("REDIS_URL", "redis://fake")

    def _boom(url):
        raise ConnectionError("redis down")

    monkeypatch.setattr(config_bus, "_make_client", _boom)
    budget._cache = (time.monotonic(), {"global_daily": 1.0})
    budget.bust_cache()  # 예외가 올라오면 실패
    assert budget._cache is None, "전파 실패로 로컬 무효화까지 건너뛰었다"


def test_handler_failure_does_not_kill_subscriber(redis_on, monkeypatch):
    """한 핸들러가 터져도 구독 루프는 살아 있어야 한다."""
    calls = []

    def _bad():
        calls.append("bad")
        raise RuntimeError("handler boom")

    config_bus.start({"budget": _bad, "retrieval_params": lambda: calls.append("rp")})
    assert config_bus.wait_ready(), "구독이 안 붙었다"

    _publish_from_other(redis_on, "budget")
    assert _wait(lambda: "bad" in calls)
    _publish_from_other(redis_on, "retrieval_params")
    assert _wait(lambda: "rp" in calls), "앞선 핸들러 예외로 구독이 죽었다"


def test_malformed_message_is_ignored(redis_on, monkeypatch):
    """형식이 깨진 메시지로 구독이 죽지 않는다."""
    calls = []
    config_bus.start({"budget": lambda: calls.append("budget")})
    assert config_bus.wait_ready(), "구독이 안 붙었다"

    fakeredis.FakeRedis(server=redis_on, decode_responses=True).publish(
        config_bus._channel(), "not-json"
    )
    _publish_from_other(redis_on, "budget")

    assert _wait(lambda: calls == ["budget"]), "깨진 메시지 뒤 정상 메시지를 못 받았다"


def test_unknown_target_is_ignored(redis_on, monkeypatch):
    """모르는 target은 조용히 버린다(핸들러 없는 캐시 이름 등)."""
    calls = []
    config_bus.start({"budget": lambda: calls.append("budget")})
    assert config_bus.wait_ready(), "구독이 안 붙었다"

    _publish_from_other(redis_on, "catalog")  # 핸들러 미등록 — 무시돼야 한다
    _publish_from_other(redis_on, "budget")   # 센티넬(순서 보장)
    assert _wait(lambda: calls == ["budget"]), "모르는 target 뒤 정상 메시지를 못 받았다"


def test_stop_terminates_subscriber(redis_on):
    """stop()이 구독 스레드를 실제로 끝내는가 — 좀비 스레드를 남기지 않게."""
    assert config_bus.start({"budget": lambda: None}) is True
    assert _wait(lambda: any(t.name == "config-bust-subscriber" for t in threading.enumerate()))
    config_bus.stop()
    assert _wait(
        lambda: not any(t.name == "config-bust-subscriber" and t.is_alive()
                        for t in threading.enumerate())
    ), "stop() 후에도 구독 스레드가 살아 있다"


def test_bust_cache_actually_publishes(redis_on):
    """`bust_cache()`가 **실제로 발행하는가** — 이 티켓의 핵심 계약.

    ⚠️ 이 테스트가 없으면 전파를 통째로 빼도 나머지 전부가 통과한다(실제로 그랬다). 다른
    테스트들은 `_publish_from_other`로 버스만 검증할 뿐, **호출부가 버스를 쓰는지**는 안 본다.
    가드가 보는 것과 동작이 하는 것이 갈리는 지점이라 별도로 고정한다.

    구독은 config_bus.start가 아니라 생 pubsub으로 한다 — start를 쓰면 origin 필터에 걸려
    자기 발행을 못 본다(같은 프로세스라 INSTANCE_ID가 같다).
    """
    sub = fakeredis.FakeRedis(server=redis_on, decode_responses=True).pubsub(
        ignore_subscribe_messages=True
    )
    sub.subscribe(config_bus._channel())
    assert _wait(lambda: bool(sub.subscribed)), "raw 구독이 안 붙었다"

    budget.bust_cache()
    rp.bust_cache()

    got = []
    deadline = time.time() + 3.0
    while time.time() < deadline and len(got) < 2:
        message = sub.get_message(timeout=0.5)
        if message and message.get("type") == "message":
            got.append(json.loads(message["data"])["target"])
    sub.close()
    assert sorted(got) == ["budget", "retrieval_params"], f"발행되지 않았다: {got}"


def test_restart_does_not_leave_two_subscribers(redis_on):
    """stop()이 join 타임아웃으로 스레드를 남겨도, 다음 start()가 **되살리지 않는다** (Qodo #459).

    정지 신호를 모듈 공유 Event 하나로 두면 start()의 clear가 죽어가던 스레드를 되살려
    구독자가 둘이 된다 — 핸들러가 중복 호출되고 좀비가 남는다. 신호를 스레드별로 두면
    옛 스레드는 자기 신호가 켜진 채라 스스로 끝난다.
    """
    config_bus.start({"budget": lambda: None})
    assert config_bus.wait_ready(), "구독이 안 붙었다"

    config_bus.stop(timeout=0.0)  # join 타임아웃 재현 — 참조만 지우고 스레드는 남는다
    config_bus.start({"budget": lambda: None})
    assert config_bus.wait_ready(), "재기동 구독이 안 붙었다"

    def _alive():
        return [t for t in threading.enumerate()
                if t.name == "config-bust-subscriber" and t.is_alive()]

    # 옛 스레드는 자기 신호가 켜진 채라 폴 주기(1초) 안에 스스로 끝난다. 되살아났다면 영원히 2개다.
    assert _wait(lambda: len(_alive()) == 1, timeout=6.0),         f"구독 스레드가 {len(_alive())}개 — 옛 스레드가 되살아났다"


def test_publisher_client_is_reused(redis_on, monkeypatch):
    """발행마다 클라이언트를 새로 만들지 않는다 — 연결 풀이 쌓여 정리가 GC에 의존한다 (Qodo #459)."""
    made: list[str] = []
    current = config_bus._make_client

    def _counting(url):
        made.append(url)
        return current(url)

    monkeypatch.setattr(config_bus, "_make_client", _counting)
    config_bus.publish("budget")
    config_bus.publish("budget")
    config_bus.publish("retrieval_params")
    assert len(made) == 1, f"발행 {3}회에 클라이언트를 {len(made)}개 만들었다"


def test_concurrent_publish_does_not_lose_messages(redis_on):
    """동시 발행이 서로의 클라이언트를 닫아 메시지를 잃지 않는다 (Qodo #459 2차).

    발행 실패는 설계상 삼켜지므로, 이 경합은 예외가 아니라 **전파 유실(→ TTL까지 지연)** 로
    조용히 나타난다. 그래서 "예외 안 남"이 아니라 **받은 개수**로 검증한다.
    """
    sub = fakeredis.FakeRedis(server=redis_on, decode_responses=True).pubsub(
        ignore_subscribe_messages=True
    )
    sub.subscribe(config_bus._channel())
    assert _wait(lambda: bool(sub.subscribed)), "raw 구독이 안 붙었다"

    workers = [
        threading.Thread(target=lambda: [config_bus.publish("budget") for _ in range(5)])
        for _ in range(4)
    ]
    for t in workers:
        t.start()
    for t in workers:
        t.join(timeout=10)

    got = 0
    deadline = time.time() + 3.0
    while time.time() < deadline and got < 20:
        message = sub.get_message(timeout=0.3)
        if message and message.get("type") == "message":
            got += 1
    sub.close()
    assert got == 20, f"동시 발행 20건 중 {got}건만 도착 — 경합으로 유실됐다"


def test_publish_failure_keeps_client(redis_on, monkeypatch):
    """발행이 실패해도 **클라이언트를 버리지 않는다** (Qodo #459 3차).

    버리면 ⑴ 다른 스레드가 방금 받아 간 같은 객체를 닫아 그쪽 publish가 use-after-close로
    실패하고(전파 유실), ⑵ 장애가 이어지는 동안 호출마다 새 클라이언트를 만들어 연결이 쌓인다.
    redis-py의 ConnectionPool이 끊긴 연결을 알아서 버리므로 객체를 살려 두는 게 맞다.
    """
    client = config_bus._publisher()
    monkeypatch.setattr(client, "publish", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))

    config_bus.publish("budget")  # 실패는 삼켜진다

    assert config_bus._publisher() is client, "실패했다고 클라이언트를 버렸다"


def test_subscriber_closes_its_client(redis_on, monkeypatch):
    """구독 루프가 종료될 때 **클라이언트까지** 닫는다 (Qodo #459 2차).

    재연결마다 새 클라이언트를 만드는데 pubsub만 닫으면 연결 풀이 남는다(Redis flapping 시
    누적). 누수는 기능 동작으로는 드러나지 않으므로 — 안 닫아도 테스트가 다 통과한다 —
    close 호출 자체를 관측한다.
    """
    closed_flags: list[dict] = []
    current = config_bus._make_client

    def _tracking(url):
        client = current(url)
        flag = {"closed": False}
        real_close = client.close

        def _close(*a, **k):
            flag["closed"] = True
            return real_close(*a, **k)

        client.close = _close
        closed_flags.append(flag)
        return client

    monkeypatch.setattr(config_bus, "_make_client", _tracking)
    config_bus.start({"budget": lambda: None})
    assert config_bus.wait_ready(), "구독이 안 붙었다"
    config_bus.stop()

    assert closed_flags, "구독 클라이언트가 만들어지지 않았다"
    assert _wait(lambda: closed_flags[0]["closed"]), "구독 클라이언트를 닫지 않았다 — 연결 풀이 남는다"
