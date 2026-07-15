"""LLM 예산 가드레일 — 계층적 상한(주체별+전역, 일/월) 검사 (RPA-171).

**왜 있나**: 관측이 수집·집계·조회에서 멈춰 있었다. 실무 LLM 운영은 "meter before you manage" —
계측은 전제고 그 다음이 본편(enforce → route → cache → alert)이다. 이 모듈이 enforce다.

**왜 직접 만드나**: 기성 관측 도구는 보여주기만 하고 막지 않는다. Datadog LLM Observability 문서:
*"provides cost dashboards and alerting, but does not block requests when budgets are exceeded or
apply hierarchical budget logic at the request layer"*. LiteLLM은 막지만 팀 단위 강제는 Enterprise
유료. 즉 **계층적 예산 강제는 사서 해결되지 않는다.**

**주체(subject)**: 로그인이면 user_id, 익명이면 session_id. `/turn`이 get_optional_user라 익명을
허용하므로(llm_usage.user_id도 nullable — 실측 감사에서 52%가 익명), user_id 상한만 두면 익명
요청이 그대로 뚫린다. session_id는 /turn 경로에 항상 있어 익명까지 덮는다.

**어디서 읽나**: llm_usage는 관측 DB(RPA-90)에 쌓이므로 **같은 곳에서 읽는다** — 앱 DB에서 읽으면
관측 DB 설정 시 항상 0이 나와 상한이 통째로 무력해진다(게이지 `_read_intake_gauge`와 같은 규칙).

**검사 시점**: 턴 진입 시 1회. 매 LLM 호출마다 검사하면 턴 중간에 죽어 부분 상태가 남고 응답
경로에 오버헤드가 붙는다.

⚠️ **알려진 한계 — soft cap이고, 초과분은 "한 턴"이 아니라 "동시 요청 수"에 비례한다** (#239
리뷰). 이 검사는 읽기 전용이라, 동시 요청 N개가 같은 (상한 미만) 사용량을 읽고 **모두 통과**할
수 있다. 비용은 턴이 **끝난 뒤** llm_usage에 기록되므로, 검사를 직렬화해도 못 고친다 — 직렬화된
두 검사 모두 "턴 전 사용량"을 보기 때문이다. 실제 최대 초과분 ≈ (동시 요청 수 × 턴당 비용).

지금 안 고치는 이유: 제대로 막으려면 **진입 시 예상비용 예약 → 종료 후 실제비용 정산**이
필요한데(예약 테이블·만료·정산·크래시 복구), 현재 규모(단일 인스턴스·소수 사용자·턴당 수 센트)
에서 초과분이 상한의 수 % 수준이라 비용 대비 이득이 없다. 상용 도구도 대부분 같은 성질의
eventually-consistent soft cap이다. 정확한 강제가 필요해지면 예약 방식으로 후속한다.
"""

import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app import models

logger = logging.getLogger(__name__)

# 상한 이름 → env 키. 이 순서·이름이 DB 컬럼(budget_limits)과 1:1로 대응한다.
_LIMIT_ENV = {
    "subject_daily": "BUDGET_SUBJECT_DAILY_USD",
    "subject_monthly": "BUDGET_SUBJECT_MONTHLY_USD",
    "global_daily": "BUDGET_GLOBAL_DAILY_USD",
    "global_monthly": "BUDGET_GLOBAL_MONTHLY_USD",
}

# DB 조회 주기 상한(초). 변경은 bust_cache()로 즉시 반영되므로, 이 TTL은 PUT을 안 거친 경로로
# 바뀐 값을 늦게라도 반영하는 안전망 겸 부하 방어다 (RPA-149와 동일 규칙).
_CACHE_TTL_SEC = 30.0

# (monotonic 시각, {상한이름: 값|None}) — None이면 미로드. monotonic이라 시계 변경에 안 흔들린다.
_cache: tuple[float, dict[str, float | None]] | None = None


@dataclass(frozen=True)
class Subject:
    """상한을 매길 대상. kind는 429 사유 문구를 가르는 데도 쓴다."""

    kind: str  # "user" | "session"
    id: uuid.UUID


@dataclass(frozen=True)
class BudgetVerdict:
    """검사 결과. exceeded면 라우터가 429로 끊는다."""

    exceeded: bool
    scope: str | None = None  # "subject" | "global" — 누구 상한에 걸렸나
    period: str | None = None  # "daily" | "monthly"
    spent_usd: float | None = None
    limit_usd: float | None = None
    resets_at: str | None = None  # ISO8601 — 프론트가 "언제 풀리나"를 보여줄 수 있게


def subject_of(user, session_id: uuid.UUID) -> Subject:
    """로그인 사용자면 user, 익명이면 session에 상한을 건다."""
    if user is not None:
        return Subject("user", user.id)
    return Subject("session", session_id)


def _limit(name: str) -> float | None:
    """env 상한을 읽는다. 미설정·비정상·0 이하면 None(=그 상한 비활성).

    미설정 시 비활성이 기본이다 — 예산 기능을 켜지 않은 배포의 기존 동작을 바꾸지 않는다.
    비정상값(non-numeric)에 기본값을 씌우면 의도치 않게 서비스를 막을 수 있으므로, 게이지와
    달리 폴백하지 않고 끈다(fail-open) — 상한은 '켠 사람만' 적용받는다.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s 값이 숫자가 아니라 이 상한을 비활성화한다: %r", name, raw)
        return None
    return value if value > 0 else None


def _env_limits() -> dict[str, float | None]:
    """.env 기반 상한 — DB 오버라이드가 없을 때의 폴백."""
    return {key: _limit(env) for key, env in _LIMIT_ENV.items()}


def _read_override() -> dict[str, float | None] | None:
    """budget_limits 최신 행 → 상한 dict. **행이 없으면 None**.

    ⚠️ 조회 실패는 None이 아니라 **예외를 올린다** — RPA-149의 retrieval_params는 둘을 None으로
    합쳐도 됐지만(둘 다 config 폴백이 정답), 예산은 다르다: '행 없음'은 env 폴백이 정답이고,
    '조회 실패'는 **관리자가 설정한 상한을 조용히 무력화**하는 것이라 구분해야 한다.
    """
    from app.db import SessionLocal

    with SessionLocal() as db:
        row = (
            db.query(models.BudgetLimitOverride)
            .order_by(models.BudgetLimitOverride.id.desc())
            .first()
        )
        if row is None:
            return None
        # 0·음수가 어쩌다 들어가 있어도(직접 SQL 등) None으로 저하 — env와 같은 규칙
        return {
            "subject_daily": _positive_or_none(row.subject_daily_usd),
            "subject_monthly": _positive_or_none(row.subject_monthly_usd),
            "global_daily": _positive_or_none(row.global_daily_usd),
            "global_monthly": _positive_or_none(row.global_monthly_usd),
        }


def _positive_or_none(v: float | None) -> float | None:
    return float(v) if v is not None and v > 0 else None


def active_limits() -> dict[str, float | None]:
    """현재 활성 상한. DB 오버라이드가 있으면 그걸, 없으면 .env를 준다 (RPA-173).

    TTL 내 재호출은 캐시. /turn hot path에서 불리므로 DB 왕복을 최소화한다.

    **조회 실패 시 이전 값을 지킨다** — retrieval_params는 실패 시 config로 저하해도 검색이
    조금 덜 최적일 뿐이지만, 예산에서 같은 저하는 **관리자가 건 상한이 사라져 비용이 새는** 것이다.
    캐시가 있으면 그 값을 유지하고(TTL 연장으로 DB 재시도 폭주 방지), 한 번도 못 읽었으면 env로
    간다. env는 배포에 명시된 값이라 '아무 상한 없음'보다 안전한 기본이다.
    """
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _CACHE_TTL_SEC:
        return _cache[1]
    try:
        override = _read_override()
    except Exception:
        if _cache is not None:
            logger.warning("예산 상한 오버라이드 조회 실패 — 직전 값을 유지한다", exc_info=True)
            _cache = (now, _cache[1])
            return _cache[1]
        logger.warning("예산 상한 오버라이드 조회 실패 — .env 값으로 폴백", exc_info=True)
        override = None
    limits = override if override is not None else _env_limits()
    _cache = (now, limits)
    return limits


def bust_cache() -> None:
    """캐시 무효화 — admin PUT 직후 호출해 다음 검사가 DB를 다시 읽게 한다(무중단 반영)."""
    global _cache
    _cache = None


def _period_start(period: str, now: datetime) -> datetime:
    """일별=UTC 자정, 월별=UTC 월초."""
    if period == "daily":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _period_end(period: str, start: datetime) -> datetime:
    """다음 리셋 시각 — 429 응답에 실어 "언제 풀리나"를 알려준다."""
    if period == "daily":
        return start + timedelta(days=1)
    # 월초 + 4일씩 더해 다음 달로 넘긴 뒤 1일로 — 28/29/30/31일 말일에 무관하게 정확하다
    return (start + timedelta(days=32)).replace(day=1)


def _spent(session, since: datetime, subject: Subject | None) -> float:
    """since 이후 누적 비용(USD). subject=None이면 전역.

    cost_usd가 NULL인 행(단가 미상 모델)은 0으로 친다 — 상한을 조용히 넘기는 것보다는 낫고,
    NULL 자체는 RPA-97(모델별 단가)에서 대부분 해소됐다.
    """
    stmt = select(func.coalesce(func.sum(models.LlmUsage.cost_usd), 0.0)).where(
        models.LlmUsage.created_at >= since
    )
    if subject is not None:
        column = models.LlmUsage.user_id if subject.kind == "user" else models.LlmUsage.session_id
        stmt = stmt.where(column == subject.id)
    return float(session.execute(stmt).scalar() or 0.0)


def check_budget(subject: Subject, now: datetime | None = None) -> BudgetVerdict:
    """주체별·전역 상한을 일/월로 검사한다. 하나라도 걸리면 exceeded.

    상한이 하나도 설정 안 됐으면 DB를 아예 안 읽는다 — 기능을 끈 배포에 쿼리 비용을 물리지 않는다.
    """
    limits = active_limits()  # DB 오버라이드 우선, 없으면 .env (RPA-173)
    checks = [
        ("subject", "daily", limits["subject_daily"], subject),
        ("subject", "monthly", limits["subject_monthly"], subject),
        ("global", "daily", limits["global_daily"], None),
        ("global", "monthly", limits["global_monthly"], None),
    ]
    active = [c for c in checks if c[2] is not None]
    if not active:
        return BudgetVerdict(exceeded=False)

    now = now or datetime.now(timezone.utc)
    from app.core.observability_db import observability_sessionmaker

    with observability_sessionmaker()() as s:
        for scope, period, limit, subj in active:
            start = _period_start(period, now)
            spent = _spent(s, start, subj)
            if spent >= limit:
                return BudgetVerdict(
                    exceeded=True, scope=scope, period=period,
                    spent_usd=round(spent, 6), limit_usd=limit,
                    resets_at=_period_end(period, start).isoformat(),
                )
    return BudgetVerdict(exceeded=False)


def exceeded_detail(v: BudgetVerdict) -> dict:
    """429 detail — 사유를 주체/전역으로 구분한다.

    전역 초과를 "당신이 많이 썼다"로 오해시키면 안 된다: 남의 사용량 때문에 막힌 것이므로
    문구를 나누고, 전역일 땐 사용량 수치를 노출하지 않는다(다른 사용자 합계가 새어나간다).
    """
    if v.scope == "global":
        return {
            "code": "BUDGET_EXCEEDED",
            "message": "서비스 전체 LLM 예산을 초과해 잠시 요청을 받을 수 없습니다. 관리자에게 문의하세요.",
            "scope": "global", "period": v.period, "resets_at": v.resets_at,
        }
    return {
        "code": "BUDGET_EXCEEDED",
        "message": (f"LLM 사용 예산을 초과했습니다 "
                    f"({v.period}: ${v.spent_usd:.4f} / ${v.limit_usd:.2f})."),
        "scope": "subject", "period": v.period,
        "spent_usd": v.spent_usd, "limit_usd": v.limit_usd, "resets_at": v.resets_at,
    }
