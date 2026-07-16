"""Slack 알림의 전이·쿨다운·fail-open (RPA-189).

이 모듈의 본질은 "슬랙에 POST한다"가 아니라 **"같은 사유로 도배하지 않는다"**이다.
/health가 30초마다 degraded면 하루 2,880개다 — 그러면 아무도 안 본다.

⚠️ 상태를 DB에서 읽는지가 핵심이다. 인메모리면 재시작·멀티워커에서 각자 "처음"이라 중복
   발송한다. 여기 테스트는 **실제 alert_state 테이블**(관측 URL 미설정 → 앱 DB 폴백,
   conftest가 로컬로 격리)을 거쳐 그 계약을 검증한다.
"""

from datetime import datetime, timedelta, timezone

import pytest

import app.services.alerts as alerts

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
HOOK = "https://hooks.slack.com/services/T000/B000/fake"


@pytest.fixture
def sent(monkeypatch):
    """웹훅을 켜고, 실제 HTTP 대신 발송 횟수를 센다.

    ⚠️ `_post`를 대신 세는 게 아니라 **`notify`가 `_post`를 부르는지**를 센다 — 도배 방지가
       듣는지는 "몇 번 보냈나"로만 증명된다.
    """
    monkeypatch.setenv("SLACK_WEBHOOK_URL", HOOK)
    monkeypatch.delenv("ALERT_COOLDOWN_MINUTES", raising=False)
    calls: list[alerts.Alert] = []
    monkeypatch.setattr(alerts, "_post", lambda a: (calls.append(a), True)[1])
    return calls


@pytest.fixture(autouse=True)
def _clean_state():
    """매 테스트를 빈 alert_state에서 시작 — 상태가 새면 전이 판정이 오염된다."""
    from app import models

    with alerts._obs_session() as db:
        db.query(models.AlertState).delete()
        db.commit()
    yield


def _alert(key="test:thing") -> alerts.Alert:
    return alerts.Alert(key=key, title="테스트", text="본문", severity="warning")


# --- 미설정 = 비활성 (회귀 없음) ---

def test_disabled_when_webhook_unset(monkeypatch):
    """`SLACK_WEBHOOK_URL`이 없으면 아무것도 안 한다 — 기존 동작 그대로.

    로컬 개발·CI는 웹훅을 안 넣는다. 여기서 뭐라도 하면 전원의 스위트가 느려지거나 깨진다.
    """
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    calls = []
    monkeypatch.setattr(alerts, "_post", lambda a: calls.append(a))

    assert alerts.notify(_alert()) is False
    assert calls == [], "비활성인데 발송을 시도했다"


def test_webhook_is_read_at_call_time(monkeypatch):
    """웹훅을 **참조 시점에** 읽는다 — import 시점에 굳으면 테스트도 운영도 못 바꾼다.

    이 프로젝트에서 import 시점 고정 때문에 여러 번 당했다(app/db.py engine, RAG config 상수).
    """
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    assert alerts.enabled() is False

    monkeypatch.setenv("SLACK_WEBHOOK_URL", HOOK)
    assert alerts.enabled() is True, "env를 켰는데 반영이 안 된다 — 값이 import 시점에 굳었다"


# --- 전이: 상태가 바뀔 때만 ---

def test_first_firing_notifies(sent):
    assert alerts.notify(_alert(), alerts.FIRING, NOW) is True
    assert len(sent) == 1


def test_repeated_firing_within_cooldown_is_silent(sent):
    """같은 firing이 이어지면 쿨다운 내엔 **한 번만**. 이게 이 모듈의 존재 이유다."""
    alerts.notify(_alert(), alerts.FIRING, NOW)
    for i in range(1, 20):  # 30초 간격 헬스체크 흉내
        alerts.notify(_alert(), alerts.FIRING, NOW + timedelta(seconds=30 * i))

    assert len(sent) == 1, f"쿨다운(기본 60분) 안에서 {len(sent)}번 보냈다 — 도배다"


def test_still_firing_after_cooldown_notifies_again(sent):
    """쿨다운이 지나면 재알림 — "아직도 터져 있다"를 알아야 한다.

    (전이만 알리면 첫 알림을 놓친 사람은 영영 모른다.)
    """
    alerts.notify(_alert(), alerts.FIRING, NOW)
    alerts.notify(_alert(), alerts.FIRING, NOW + timedelta(minutes=61))

    assert len(sent) == 2


def test_recovery_notifies_once(sent):
    """복구(firing→ok)도 알린다 — 사람이 "끝났다"를 알아야 한다. 단 한 번만."""
    alerts.notify(_alert(), alerts.FIRING, NOW)
    assert alerts.notify(_alert(), alerts.OK, NOW + timedelta(minutes=5)) is True
    assert len(sent) == 2

    # 정상이 이어지는 건 뉴스가 아니다
    for i in range(5):
        alerts.notify(_alert(), alerts.OK, NOW + timedelta(minutes=10 + i))
    assert len(sent) == 2, "정상 상태가 이어지는데 알림을 보냈다"


def test_ok_without_prior_firing_is_silent(sent):
    """처음부터 정상이면 아무 말 안 한다 — 기동할 때마다 "정상입니다" 슬랙은 소음이다."""
    alerts.notify(_alert(), alerts.OK, NOW)
    assert sent == []


def test_keys_are_independent(sent):
    """다른 key는 서로의 쿨다운에 영향받지 않는다 — 예산 알림이 헬스 알림을 막으면 안 된다."""
    alerts.notify(alerts.Alert("budget:global:daily", "예산", "초과"), alerts.FIRING, NOW)
    alerts.notify(alerts.Alert("health:degraded", "헬스", "degraded"), alerts.FIRING, NOW)

    assert len(sent) == 2


def test_cooldown_zero_means_transition_only(sent, monkeypatch):
    """ALERT_COOLDOWN_MINUTES=0이면 재알림 없이 전이 때만."""
    monkeypatch.setenv("ALERT_COOLDOWN_MINUTES", "0")
    alerts.notify(_alert(), alerts.FIRING, NOW)
    alerts.notify(_alert(), alerts.FIRING, NOW + timedelta(days=1))

    assert len(sent) == 1


# --- 상태가 DB에 있나 (인메모리면 재시작에 샌다) ---

def test_state_survives_module_reload(sent):
    """상태가 **DB**에 있어야 한다 — 모듈 전역이면 재시작·워커마다 재알림이다.

    importlib.reload로 "새 프로세스"를 흉내낸다. 인메모리 dict였다면 여기서 초기화돼
    두 번째 알림이 나간다.
    """
    import importlib

    alerts.notify(_alert(), alerts.FIRING, NOW)
    assert len(sent) == 1

    importlib.reload(alerts)
    # reload가 monkeypatch를 날렸으므로 다시 심는다 (env는 살아 있다)
    alerts._post = lambda a: (sent.append(a), True)[1]
    alerts.notify(_alert(), alerts.FIRING, NOW + timedelta(minutes=1))

    assert len(sent) == 1, "재시작 후 같은 사유로 또 보냈다 — 상태가 프로세스 안에 있다"


# --- fail-open: 알림이 서비스를 죽이면 안 된다 ---

def test_slack_failure_does_not_raise(monkeypatch):
    """슬랙이 죽어도 예외가 새면 안 된다 — 관측이 본체를 망가뜨리는 것이다."""
    monkeypatch.setenv("SLACK_WEBHOOK_URL", HOOK)

    def _boom(a):
        raise RuntimeError("slack down")

    monkeypatch.setattr(alerts, "_post", _boom)

    assert alerts.notify(_alert(), alerts.FIRING, NOW) is False  # 예외가 아니라 False


def test_state_db_failure_does_not_raise(monkeypatch):
    """상태 DB가 죽어도 마찬가지 — fail-open."""
    monkeypatch.setenv("SLACK_WEBHOOK_URL", HOOK)

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(alerts, "_obs_session", _boom)

    assert alerts.notify(_alert(), alerts.FIRING, NOW) is False
