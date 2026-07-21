# -*- coding: utf-8 -*-
"""관측 이벤트 배치 큐 검증 (RPA-221).

여기서 막는 것은 **두 가지**다.
1. 적재가 다시 핫패스로 돌아오는 회귀 — _write_log가 Neon 왕복을 기다리면 검색당
   ~2,327ms가 되돌아오고, 비동기 경로에서는 이벤트 루프가 그만큼 멈춘다.
2. 큐 때문에 관측이 조용히 나빠지는 것 — 마스킹 누락, 무음 유실, 내용 변질.

성능 수치는 환경마다 달라 고정하지 않는다. 대신 "기다렸는가 / 세션을 몇 개 썼는가 /
버린 걸 셌는가"라는 관찰 가능한 사실을 검증한다.
"""

import json
import threading
import time

import pytest

from app.rag import event_queue as eq
from app.rag import observability as obs


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """테스트마다 워커·카운터를 초기화하고 JSONL은 임시 디렉터리로."""
    monkeypatch.setattr(obs.config, "LOG_DIR", tmp_path)
    eq.reset_for_tests()
    yield
    eq.reset_for_tests()


class _FakeSession:
    """observability_sessionmaker가 주는 세션 스텁 — 세션 수와 커밋 수를 센다."""

    def __init__(self, sink, opened):
        self.sink, self.opened = sink, opened
        self.commits = 0

    def __enter__(self):
        self.opened.append(self)
        return self

    def __exit__(self, *a):
        return False

    def add(self, row):
        self.sink.append(row)

    def commit(self):
        self.commits += 1


def _install_fake_db(monkeypatch):
    """관측 DB를 스텁으로 갈아끼우고 (적재된 행, 열린 세션들)을 돌려준다."""
    rows, opened = [], []
    monkeypatch.setattr(
        "app.core.observability_db.observability_sessionmaker",
        lambda: (lambda: _FakeSession(rows, opened)),
    )
    return rows, opened


# --- 핫패스에서 빠졌는가 ---

def test_write_log_does_not_wait_for_persist(monkeypatch):
    """🔴 _write_log는 적재 완료를 기다리지 않는다.

    적재가 1초 걸리게 만들고 _write_log가 그보다 훨씬 빨리 반환하는지 본다.
    동기로 되돌아가면 이 테스트가 1초 이상 걸려 실패한다.
    """
    started = threading.Event()

    def _slow(records):
        started.set()
        time.sleep(1.0)

    eq.reset_for_tests()
    eq.configure(_slow)
    try:
        t = time.perf_counter()
        obs._write_log({"request_id": "r1", "event": "embed_query"})
        elapsed = time.perf_counter() - t
        assert elapsed < 0.2, f"_write_log가 적재를 기다렸다 ({elapsed:.2f}s)"
        assert started.wait(timeout=3.0), "워커가 적재를 시작하지 않았다"
    finally:
        eq.reset_for_tests()
        eq.configure(obs._persist_rag_events)


def test_enqueue_never_blocks_when_full():
    """큐가 가득 차도 enqueue는 즉시 반환한다 — 이벤트 루프에서 불릴 수 있다."""
    eq.configure(lambda records: time.sleep(5.0))  # 워커를 묶어둔다
    monkey_max = eq._MAXSIZE
    try:
        eq._MAXSIZE = 2
        eq.reset_for_tests()
        eq.configure(lambda records: time.sleep(5.0))
        t = time.perf_counter()
        for i in range(50):
            eq.enqueue({"event": f"e{i}"})
        elapsed = time.perf_counter() - t
        assert elapsed < 0.5, f"포화 상태에서 블로킹했다 ({elapsed:.2f}s)"
        assert eq.dropped_count() > 0, "버렸는데 세지 않았다"
    finally:
        eq._MAXSIZE = monkey_max
        eq.reset_for_tests()
        eq.configure(obs._persist_rag_events)


# --- 배치로 나가는가 ---

def test_events_are_persisted_in_one_batch(monkeypatch):
    """🔴 이벤트 N건에 세션이 N개가 아니어야 한다 — 절감의 원천이 세션 부대비용이므로."""
    rows, opened = _install_fake_db(monkeypatch)
    eq.reset_for_tests()
    eq.configure(obs._persist_rag_events)

    for i in range(7):  # 검색 1회분
        obs._write_log({"request_id": "r1", "event": f"stage{i}", "status": "ok"})

    assert eq.flush(timeout=5.0), "큐가 시간 안에 비지 않았다"
    time.sleep(0.2)

    assert len(rows) == 7, f"적재된 행이 7개가 아니다: {len(rows)}"
    assert len(opened) < 7, f"세션이 건당 하나씩 열렸다 ({len(opened)}개) — 배치가 아니다"
    assert sum(s.commits for s in opened) < 7


def test_burst_is_drained_in_few_batches(monkeypatch):
    """트래픽 급증 시 배치가 커져야 한다 — 배치 비용이 크기에 대해 평평하기 때문.

    이게 회귀하면(배치 크기가 작아지면) 처리량 상한이 내려가 큐가 쌓이고 결국 드롭한다.
    """
    rows, opened = _install_fake_db(monkeypatch)
    eq.reset_for_tests()
    eq.configure(obs._persist_rag_events)

    burst = 200
    for i in range(burst):
        obs._write_log({"request_id": "r1", "event": f"e{i}", "status": "ok"})

    assert eq.flush(timeout=10.0), "큐가 시간 안에 비지 않았다"
    time.sleep(0.3)

    assert len(rows) == burst, f"적재 누락: {len(rows)}/{burst}"
    # 200건이 건당 세션이면 200개. 배치가 살아 있으면 한 자릿수여야 한다.
    assert len(opened) <= 10, f"세션이 {len(opened)}개 — 배치가 잘게 쪼개졌다"


# --- 내용이 그대로인가 ---

def test_queued_record_is_masked_before_enqueue(monkeypatch):
    """🔴 큐에 원문 PII가 앉으면 안 된다 (fail-closed, #188).

    마스킹을 큐 투입 뒤로 옮기면 이 테스트가 실패한다.
    """
    captured = []
    eq.reset_for_tests()
    eq.configure(lambda records: captured.extend(records))

    try:
        obs._write_log({"request_id": "r1", "event": "embed_query",
                        "args": {"q": {"len": 5, "preview": "메일 a@b.com"}}})
        assert eq.flush(timeout=5.0)
        time.sleep(0.2)
        assert captured, "워커가 레코드를 받지 못했다"
        assert captured[0]["args"]["q"]["preview"] == "메일 [EMAIL]"
        assert "a@b.com" not in json.dumps(captured[0], ensure_ascii=False)
    finally:
        eq.reset_for_tests()
        eq.configure(obs._persist_rag_events)


def test_batch_row_matches_single_row(monkeypatch):
    """배치 적재의 행이 단건 적재와 동일해야 한다 — 컬럼 절단·config 스냅샷 포함."""
    record = {"request_id": "abc123", "event": "hybrid_search", "function": "search",
              "status": "ok", "duration_ms": 1726.68, "result": {"count": 5}}
    row = obs._rag_event_row(dict(record))
    assert row.request_id == "abc123" and row.event == "hybrid_search"
    detail = json.loads(row.detail)
    assert "config" in detail  # hybrid_search엔 설정 스냅샷
    assert detail["result"] == {"count": 5}


def test_long_values_are_truncated():
    """컬럼 길이 계약(40/120/20자)이 배치 경로에서도 지켜진다."""
    row = obs._rag_event_row({
        "event": "e" * 100, "function": "f" * 300, "status": "s" * 50,
    })
    assert len(row.event) == 40 and len(row.function) == 120 and len(row.status) == 20


# --- 실패해도 서비스를 안 죽이는가 ---

def test_worker_survives_persist_failure(monkeypatch):
    """적재가 터져도 워커가 죽으면 안 된다 — 죽으면 이후 이벤트가 전부 유실된다."""
    calls = []

    def _flaky(records):
        calls.append(records)
        if len(calls) == 1:
            raise RuntimeError("db down")

    eq.reset_for_tests()
    eq.configure(_flaky)
    try:
        obs._write_log({"request_id": "r1", "event": "first"})
        assert eq.flush(timeout=5.0)
        time.sleep(0.2)
        obs._write_log({"request_id": "r2", "event": "second"})
        assert eq.flush(timeout=5.0)
        time.sleep(0.2)
        assert len(calls) >= 2, "첫 배치 실패 후 워커가 멈췄다"
    finally:
        eq.reset_for_tests()
        eq.configure(obs._persist_rag_events)


def test_disabled_falls_back_to_sync(monkeypatch):
    """RAG_EVENT_QUEUE=0이면 기존 동기 적재 — 문제 시 즉시 원복할 수 있어야 한다."""
    monkeypatch.setenv("RAG_EVENT_QUEUE", "0")
    called = []
    monkeypatch.setattr(obs, "_persist_rag_event", lambda rec: called.append(rec))
    obs._write_log({"request_id": "r1", "event": "embed_query"})
    assert len(called) == 1, "토글을 껐는데 동기 적재가 안 됐다"


def test_stop_counts_leftover_when_worker_times_out(caplog):
    """🔴 종료가 타임아웃되면 큐 잔여를 유실로 집계한다 (CodeRabbit #302).

    전역을 비우면 그 큐는 아무도 안 본다. 카운터에 안 잡히면 "종료 시 유실 0"이라는
    주장이 조용히 거짓이 된다 — 무음 유실 금지가 이 프로젝트의 원칙이다.
    """
    release, blocked = threading.Event(), threading.Event()
    seen = []

    def _stuck(records):
        seen.append(len(records))   # 처리 중 배치 크기
        blocked.set()               # 워커가 적재에 진입했다
        release.wait(timeout=10.0)  # 여기서 붙잡아 join이 타임아웃 나게 한다

    eq.reset_for_tests()
    eq.configure(_stuck)
    try:
        # 워커를 확실히 묶어둔다 — flush로 배치 창을 끊어 첫 배치를 내보내게 하고,
        # 워커가 _stuck에 들어간 것을 확인한 뒤에야 나머지를 넣는다(레이스 제거).
        eq.enqueue({"event": "first"})
        eq.flush(timeout=0.5)  # idle이 될 수 없으니 False를 반환한다 — 창을 끊는 게 목적
        assert blocked.wait(timeout=3.0), "워커가 적재에 진입하지 않았다"

        for i in range(5):  # 워커가 막혀 있으므로 전부 큐에 남는다
            eq.enqueue({"event": f"e{i}"})

        before = eq.dropped_count()
        with caplog.at_level("WARNING"):
            eq.stop(timeout=0.3)

        # 큐 잔여 5건 + 처리 중 배치(seen[0]건) 둘 다 세야 한다
        assert eq.dropped_count() == before + 5 + seen[0], (
            f"잔여 5 + 처리중 {seen[0]}이 집계되지 않았다 ({before} → {eq.dropped_count()})"
        )
        assert "종료되지 않았다" in caplog.text, "종료 타임아웃 경고가 없다"
    finally:
        release.set()
        eq.reset_for_tests()
        eq.configure(obs._persist_rag_events)


def test_stop_counts_inflight_batch_when_queue_empty(caplog):
    """🔴 큐가 비어 있고 적재만 멈춘 경우에도 처리 중 배치가 집계돼야 한다 (CodeRabbit #302).

    _collect가 꺼낸 배치는 이미 큐에서 빠져 qsize()에 안 잡힌다. 큐 잔여만 세면
    이 상황에서 leftover == 0이 되어 "유실 없음"을 거짓으로 보고하게 된다.
    """
    release, blocked = threading.Event(), threading.Event()
    seen = []

    def _stuck(records):
        seen.append(len(records))
        blocked.set()
        release.wait(timeout=10.0)

    eq.reset_for_tests()
    eq.configure(_stuck)
    try:
        for i in range(3):
            eq.enqueue({"event": f"e{i}"})

        # 워커가 전부 배치로 가져가 큐가 빌 때까지 기다린다 — 그래야 '큐는 비었는데
        # 처리 중 배치만 있는' 상황이 성립한다.
        deadline = time.time() + 3.0
        while not eq._writer.q.empty() and time.time() < deadline:
            time.sleep(0.01)
        eq.flush(timeout=0.5)  # 배치 창을 끊어 적재를 시작시킨다
        assert blocked.wait(timeout=3.0), "워커가 적재에 진입하지 않았다"
        assert eq._writer.q.empty(), "이 테스트는 큐가 빈 상태를 전제한다"
        assert seen[0] >= 1

        before = eq.dropped_count()
        with caplog.at_level("WARNING"):
            eq.stop(timeout=0.3)

        assert eq.dropped_count() == before + seen[0], (
            f"처리 중 배치 {seen[0]}건이 집계되지 않았다 ({before} → {eq.dropped_count()})"
        )
    finally:
        release.set()
        eq.reset_for_tests()
        eq.configure(obs._persist_rag_events)


def test_overlapping_workers_do_not_clobber_inflight(caplog):
    """🔴 종료 타임아웃으로 워커가 겹쳐도 유실 집계가 서로 덮어써지지 않는다 (CodeRabbit #302).

    처리중 건수가 모듈 전역이면, 살아남은 이전 워커의 finally가 새 워커의 값을 0으로
    덮어써 새 워커도 타임아웃될 때 그 배치가 집계에서 빠진다. 상태를 _Writer
    인스턴스에 두는 이유가 이것이다.
    """
    releases: list[threading.Event] = []
    entered: list[int] = []

    def _stuck(records):
        ev = threading.Event()
        releases.append(ev)
        entered.append(len(records))
        ev.wait(timeout=10.0)

    def _wait(pred, why, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return
            time.sleep(0.01)
        raise AssertionError(why)

    eq.reset_for_tests()
    eq.configure(_stuck)
    try:
        # 워커 A — 1건을 들고 적재에서 멈춘다
        eq.enqueue({"event": "a"})
        eq.flush(timeout=0.5)
        _wait(lambda: len(entered) >= 1, "워커 A가 적재에 진입하지 않았다")
        eq.stop(timeout=0.3)  # 타임아웃 → A는 살아 있고 전역에서만 분리된다
        assert eq.dropped_count() >= 1

        # 워커 B — 새로 만들어져 3건을 들고 멈춘다
        for i in range(3):
            eq.enqueue({"event": f"b{i}"})
        _wait(lambda: eq._writer is not None and eq._writer.q.empty(),
              "워커 B가 큐를 비우지 않았다")
        eq.flush(timeout=0.5)
        _wait(lambda: len(entered) >= 2, "워커 B가 적재에 진입하지 않았다")
        b_size = entered[1]
        assert b_size >= 1

        # 여기서 A를 풀어준다 — A의 finally가 돈다. 전역 상태였다면 B의 값이 0이 된다.
        releases[0].set()
        time.sleep(0.3)

        before = eq.dropped_count()
        with caplog.at_level("WARNING"):
            eq.stop(timeout=0.3)

        assert eq.dropped_count() == before + b_size, (
            f"이전 워커가 새 워커의 처리중 집계({b_size}건)를 덮어썼다 "
            f"({before} → {eq.dropped_count()})"
        )
    finally:
        for ev in releases:
            ev.set()
        eq.reset_for_tests()
        eq.configure(obs._persist_rag_events)


def test_stop_flushes_remaining(monkeypatch):
    """정상 종료(lifespan)에서는 유실이 0이어야 한다."""
    rows, _ = _install_fake_db(monkeypatch)
    eq.reset_for_tests()
    eq.configure(obs._persist_rag_events)

    for i in range(5):
        obs._write_log({"request_id": "r1", "event": f"e{i}"})
    eq.stop(timeout=5.0)

    assert len(rows) == 5, f"종료 시 flush가 빠뜨렸다: {len(rows)}/5"
