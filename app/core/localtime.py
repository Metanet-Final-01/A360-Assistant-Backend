"""서비스 표준 시간대(KST) — 날짜 경계의 단일 진실 공급원.

일/월 경계가 UTC면 한국 팀 기준 "오늘"이 **오전 9시에 리셋**된다 — 일 예산이 아침에 풀리고,
"오늘 비용" 알림·일별 집계가 어제 오후 3시(KST)부터를 한 날로 묶는다. 실측으로도 왜곡됐다:
완결일 최대 비용 산출이 UTC 창(09:00~09:00 KST) 기준이라 임계의 근거 자체가 틀어져 있었다.

⚠️ 날짜 경계를 읽는 **모든 곳이 이 모듈 하나를 읽어야 한다** — budget(기간 상한)·rollup(일별
   집계)·alerts(당일 판정)·캘리브레이션 스크립트. 한 곳이라도 자체 계산하면 "예산은 KST인데
   집계는 UTC"로 갈린다(가드가 읽는 것 == 동작이 읽는 것, CONVENTIONS §9).

고정 오프셋(+9)을 쓴다:
- 한국은 DST가 없어 고정 오프셋이 항상 정확하다.
- `ZoneInfo("Asia/Seoul")`은 tzdata가 없는 배포 이미지(slim 계열)에서 죽을 수 있다 —
  의존성 0인 쪽을 택한다.
- SQL 쪽 대응 산식은 `(created_at + interval '9 hours')::date` — 파이썬과 같은 계산이어야
  스크립트 집계와 서비스 판정이 일치한다(AT TIME ZONE 'Asia/Seoul'도 결과는 같지만,
  파이썬이 고정 오프셋인 이상 SQL도 고정 오프셋으로 맞춰 산식을 하나로 둔다).

저장은 그대로 UTC(timestamptz)다 — **바뀌는 건 "하루의 경계"를 어디에 긋느냐뿐**이다.
"""

from datetime import date, datetime, time, timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")

# SQL에서 같은 경계를 쓸 때 붙일 표현식 조각 (budget_calibration_report 등).
SQL_LOCAL_DATE = "(created_at + interval '9 hours')::date"


def local_date(now: datetime) -> date:
    """이 시각이 속하는 KST 날짜. (aware datetime 전제 — 서비스 코드는 전부 aware다.)"""
    return now.astimezone(KST).date()


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """KST 기준 그 날짜의 [시작, 끝) — aware라 UTC 컬럼과의 비교·저장에 그대로 쓴다.

    예: 2026-07-16 → [2026-07-15T15:00:00Z, 2026-07-16T15:00:00Z)
    """
    start = datetime.combine(day, time.min, tzinfo=KST)
    return start, start + timedelta(days=1)


def day_start(now: datetime) -> datetime:
    """이 시각이 속한 KST 날의 시작(자정, aware)."""
    return day_bounds(local_date(now))[0]


def month_start(now: datetime) -> datetime:
    """이 시각이 속한 KST 달의 시작(1일 자정, aware)."""
    l = now.astimezone(KST)
    return datetime(l.year, l.month, 1, tzinfo=KST)
