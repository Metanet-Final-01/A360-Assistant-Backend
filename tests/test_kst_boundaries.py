"""날짜 경계가 KST 한 축인지 — budget·rollup·alerts가 같은 "오늘"을 보는지.

배경: 셋 다 UTC 자정을 경계로 써서, 한국 팀 기준 **일 예산이 오전 9시에 리셋**되고
일별 집계·"오늘 비용" 알림이 어제 오후 3시(KST)부터를 한 날로 묶였다. 임계 산출의 근거
(완결일 최대)까지 UTC 창 기준으로 왜곡됐다.

핵심 계약 둘:
1. **경계는 KST 자정이다** — KST 0~9시의 지출이 "오늘"에 속한다(예전엔 어제로 묶임).
2. **세 소비자가 전부 app.core.localtime 하나를 읽는다** — 한 곳이라도 자체 계산하면
   "예산은 KST인데 집계는 UTC"로 갈린다. 이 검증은 문자열 매칭이 아니라 **동작**으로 한다
   (getsource 매칭은 docstring에 걸려 장식이 된 전례가 있다 — #263).
"""

from datetime import date, datetime, timedelta, timezone

from app.core import localtime
from app.services import alerts, budget, rollup

# KST 07-16 01:30 = UTC 07-15 16:30 — KST 날짜와 UTC 날짜가 **다른** 시각.
# 경계 테스트는 반드시 이런 시각으로 해야 한다(둘이 같은 시각이면 UTC 구현도 통과한다).
SPLIT = datetime(2026, 7, 15, 16, 30, tzinfo=timezone.utc)


# --- localtime 자체 ---

def test_local_date_crosses_utc_midnight():
    assert SPLIT.date() == date(2026, 7, 15), "전제: UTC로는 아직 15일"
    assert localtime.local_date(SPLIT) == date(2026, 7, 16), "KST로는 이미 16일이어야 한다"


def test_day_bounds_are_kst_midnights():
    start, end = localtime.day_bounds(date(2026, 7, 16))
    assert start == datetime(2026, 7, 15, 15, tzinfo=timezone.utc)  # KST 07-16 00:00
    assert end == datetime(2026, 7, 16, 15, tzinfo=timezone.utc)
    assert start <= SPLIT < end, "KST 01:30 시각이 그 KST 날짜 창 안에 있어야 한다"


def test_month_start_crosses_utc_month():
    # UTC 06-30 16:00 = KST 07-01 01:00 → 7월의 시작
    t = datetime(2026, 6, 30, 16, tzinfo=timezone.utc)
    assert localtime.month_start(t) == datetime(2026, 6, 30, 15, tzinfo=timezone.utc)


# --- 그 버그의 회귀: 일 예산이 오전 9시에 리셋 ---

def test_budget_daily_window_covers_kst_morning():
    """KST 아침(0~9시)의 지출이 같은 KST 날의 창에 **들어와야** 한다.

    UTC 경계였으면: 창이 07-16T00:00Z(=KST 09:00)에 시작해 KST 08:00의 지출(07-15T23:00Z)이
    **창 밖** — 오전 9시에 상한이 풀리는 그 버그다.
    """
    now = datetime(2026, 7, 16, 1, tzinfo=timezone.utc)          # KST 07-16 10:00
    spend_at_kst_morning = datetime(2026, 7, 15, 23, tzinfo=timezone.utc)  # KST 07-16 08:00

    start = budget._period_start("daily", now)
    assert start <= spend_at_kst_morning, (
        "KST 아침 지출이 일 창 밖이다 — 예산이 오전 9시에 리셋되는 버그가 되살아났다")
    assert start == datetime(2026, 7, 15, 15, tzinfo=timezone.utc)  # KST 07-16 00:00


# --- 단일 진실 공급원: 세 소비자가 localtime을 실제로 부르는지 (동작 검증) ---

def test_budget_uses_localtime(monkeypatch):
    sentinel = datetime(2001, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(localtime, "day_start", lambda now: sentinel)

    assert budget._period_start("daily", SPLIT) is sentinel, (
        "budget이 localtime.day_start를 안 쓴다 — 경계를 자체 계산하면 집계와 갈린다")


def test_rollup_uses_localtime(monkeypatch):
    sentinel = ("s", "e")
    monkeypatch.setattr(localtime, "day_bounds", lambda day: sentinel)

    assert rollup._day_bounds(date(2026, 7, 16)) is sentinel, (
        "rollup이 localtime.day_bounds를 안 쓴다 — 집계 day 축이 예산 창과 갈린다")


def test_alerts_queries_kst_today(monkeypatch):
    """alerts의 '오늘' 판정이 KST 날짜로 usage_daily를 조회하는지 — 쿼리 파라미터로 확인.

    UTC 날짜로 조회하면 KST 0~9시 동안 **어제 행**을 보거나, rollup(KST day)이 쓴 오늘 행을
    못 찾아 0으로 판정한다 — 임계를 넘겨도 조용한 알림이 된다.
    """
    captured: dict = {}

    class _R:
        def scalar(self): return 0.0

    class _DB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, stmt, params=None):
            captured.update(params or {})
            return _R()
        def get(self, model, key): return None
        def add(self, row): pass
        def commit(self): pass

    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T0/B0/x")
    monkeypatch.setenv("ALERT_GLOBAL_DAILY_USD", "15")
    monkeypatch.delenv("ALERT_5XX_DAILY", raising=False)
    monkeypatch.setattr(alerts, "_obs_session", lambda: _DB())
    monkeypatch.setattr(alerts, "notify", lambda *a, **k: False)

    alerts.check_daily_thresholds(SPLIT)

    assert captured.get("d") == date(2026, 7, 16), (
        f"UTC 날짜({SPLIT.date()})로 조회했다 — rollup의 KST day 행과 어긋난다: {captured}")
