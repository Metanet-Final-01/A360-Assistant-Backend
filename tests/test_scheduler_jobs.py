"""스케줄러 잡이 **각자 독립적으로** 켜지는지 (RPA-189).

⚠️ 이 파일이 생긴 이유: `start_scheduler()`가 `METRICS_ROLLUP_ENABLED`로 **함수 전체를
   early-return**해서, **롤업만 끄려 해도 헬스 감시까지 같이 꺼졌다**(CodeRabbit #263).
   둘은 서로 다른 관심사다 — 롤업은 집계(비용), 헬스는 가용성 감시(안전). 하나를 끄려고
   다른 하나를 잃으면 안 된다.

실제 스케줄러를 띄우지 않고 등록된 job id만 본다 — 스레드·타이머를 테스트에 들이지 않는다.
"""

import pytest

import app.core.scheduler as sched

HOOK = "https://hooks.slack.com/services/T000/B000/fake"


@pytest.fixture
def registered(monkeypatch):
    """start_scheduler()가 등록한 job id 집합을 돌려준다 (실제 실행 없음)."""
    jobs: list[str] = []

    class _FakeScheduler:
        def __init__(self, *a, **kw): pass
        def add_job(self, func, trigger=None, **kw): jobs.append(kw.get("id", "?"))
        def start(self): pass
        def shutdown(self, wait=False): pass

    monkeypatch.setattr(
        "apscheduler.schedulers.background.BackgroundScheduler", _FakeScheduler)
    monkeypatch.setattr(sched, "_scheduler", None)

    def _run() -> set[str]:
        jobs.clear()
        sched.start_scheduler()
        return set(jobs)

    return _run


def test_both_on_registers_all(registered, monkeypatch):
    monkeypatch.setenv("METRICS_ROLLUP_ENABLED", "true")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", HOOK)

    assert registered() == {"metrics_rollup", "rollup_catchup", "health_watch"}


def test_rollup_off_keeps_health_watch(registered, monkeypatch):
    """🔴 롤업을 꺼도 **헬스 감시는 살아 있어야** 한다 — 이게 이 파일의 존재 이유다.

    예전엔 METRICS_ROLLUP_ENABLED=false가 함수 전체를 early-return시켜 헬스도 같이 죽었다.
    "집계는 필요 없지만 서버가 죽었는지는 알고 싶다"가 완전히 정상적인 운영 요구다.
    """
    monkeypatch.setenv("METRICS_ROLLUP_ENABLED", "false")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", HOOK)

    jobs = registered()
    assert "health_watch" in jobs, "롤업을 껐더니 헬스 감시까지 꺼졌다"
    assert "metrics_rollup" not in jobs and "rollup_catchup" not in jobs


def test_no_webhook_keeps_rollup(registered, monkeypatch):
    """반대도 마찬가지 — 웹훅이 없어도 롤업(집계)은 돌아야 한다."""
    monkeypatch.setenv("METRICS_ROLLUP_ENABLED", "true")
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)

    jobs = registered()
    assert "metrics_rollup" in jobs
    assert "health_watch" not in jobs, "알릴 곳이 없는데 헬스 감시를 등록했다 — 판정만 하고 버린다"


def test_both_off_starts_nothing(registered, monkeypatch):
    monkeypatch.setenv("METRICS_ROLLUP_ENABLED", "false")
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)

    assert registered() == set()
    assert sched.start_scheduler() is False
