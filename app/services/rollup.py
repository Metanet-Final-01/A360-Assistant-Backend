"""일별 롤업 (RPA-104) — 관측 raw를 피벗 가능한 집계 테이블로.

request_metrics → metrics_daily (일자×method×path: 호출수·에러·p50/p95),
llm_usage       → usage_daily   (일자×component×purpose×model: 호출수·토큰·비용).

설계:
- **멱등**: 해당 일자의 집계 행을 DELETE 후 INSERT — 몇 번을 다시 돌려도 결과 동일.
  그래서 "오늘"을 주기적으로 재집계해 대시보드가 신선하게 유지된다.
- **원자성/경쟁**: DELETE→INSERT는 세션 하나에서 commit 한 번 — 단일 트랜잭션이라
  중간 상태가 노출되지 않는다. 동시 실행은 ① 프로세스 내엔 스케줄러 단일 워커+
  max_instances=1로 차단(core/scheduler), ② 프로세스 간엔 PK(day,…)가 한 승자를
  보장하고 패자 트랜잭션은 통째로 롤백 → 다음 주기에 멱등 재집계된다.
- **집계는 Python 순수 함수**: percentile 등을 SQL 방언(percentile_cont)에 안 기대고
  파이썬으로 계산 — DB 무관·단위 테스트 가능. 일 단위 행 수(수천)면 충분히 싸다.
- **retention**: raw(request_metrics)는 N일 지나면 삭제, 집계본은 장기 보관.
- 전부 best-effort — 롤업 실패가 앱/스케줄러를 죽이면 안 된다(경고 로그만).
"""

import logging
import os
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import delete, select

from app import models

logger = logging.getLogger(__name__)


# ── 순수 계산 (테스트 대상) ───────────────────────────────────────────────

def percentile(sorted_values: list[int | float], q: float) -> float | None:
    """정렬된 리스트의 q(0~1) 분위수 — 선형 보간. 빈 리스트면 None."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 >= len(sorted_values):
        return float(sorted_values[-1])
    return sorted_values[lo] + (sorted_values[lo + 1] - sorted_values[lo]) * frac


def aggregate_metrics(rows: list[tuple]) -> list[dict]:
    """(method, path, status_code, latency_ms) 행들 → (method×path)별 집계 dict 목록."""
    groups: dict[tuple[str, str], dict] = {}
    for method, path, status, latency in rows:
        g = groups.setdefault((method, path), {"calls": 0, "err_4xx": 0, "err_5xx": 0, "lat": []})
        g["calls"] += 1
        if 400 <= status < 500:
            g["err_4xx"] += 1
        elif status >= 500:
            g["err_5xx"] += 1
        if latency is not None:
            g["lat"].append(latency)
    out = []
    for (method, path), g in groups.items():
        lat = sorted(g["lat"])
        out.append({
            "method": method,
            "path": path,
            "calls": g["calls"],
            "err_4xx": g["err_4xx"],
            "err_5xx": g["err_5xx"],
            "p50_ms": round(percentile(lat, 0.5)) if lat else None,
            "p95_ms": round(percentile(lat, 0.95)) if lat else None,
            "avg_ms": round(sum(lat) / len(lat)) if lat else None,
            "max_ms": max(lat) if lat else None,
        })
    return out


def aggregate_usage(rows: list[tuple]) -> list[dict]:
    """(component, purpose, model, in_tok, out_tok, cost) 행들 → 그룹별 합계 dict 목록."""
    groups: dict[tuple, dict] = {}
    for component, purpose, model, in_tok, out_tok, cost in rows:
        key = (component or "other", purpose or "other", model or "unknown")
        g = groups.setdefault(key, {"calls": 0, "in": 0, "out": 0, "cost": 0.0, "has_cost": False})
        g["calls"] += 1
        g["in"] += in_tok or 0
        g["out"] += out_tok or 0
        if cost is not None:
            g["cost"] += float(cost)
            g["has_cost"] = True
    return [
        {
            "component": k[0], "purpose": k[1], "model": k[2],
            "calls": g["calls"], "input_tokens": g["in"], "output_tokens": g["out"],
            "cost_usd": round(g["cost"], 8) if g["has_cost"] else None,
        }
        for k, g in groups.items()
    ]


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    """해당 일자(UTC)의 [시작, 끝) 타임스탬프."""
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


# ── DB 롤업 (관측 세션 사용) ─────────────────────────────────────────────

def rollup_metrics_day(session_factory, day: date) -> int:
    """해당 일자의 request_metrics를 집계해 metrics_daily에 멱등 반영. 집계 행 수 반환."""
    start, end = _day_bounds(day)
    with session_factory() as s:
        rows = s.execute(
            select(
                models.RequestMetric.method, models.RequestMetric.path,
                models.RequestMetric.status_code, models.RequestMetric.latency_ms,
            ).where(models.RequestMetric.created_at >= start, models.RequestMetric.created_at < end)
        ).all()
        aggs = aggregate_metrics([tuple(r) for r in rows])
        s.execute(delete(models.MetricsDaily).where(models.MetricsDaily.day == day))
        for a in aggs:
            s.add(models.MetricsDaily(day=day, **a))
        s.commit()
    return len(aggs)


def rollup_usage_day(session_factory, day: date) -> int:
    """해당 일자의 llm_usage를 집계해 usage_daily에 멱등 반영. 집계 행 수 반환."""
    start, end = _day_bounds(day)
    with session_factory() as s:
        rows = s.execute(
            select(
                models.LlmUsage.component, models.LlmUsage.purpose, models.LlmUsage.model,
                models.LlmUsage.input_tokens, models.LlmUsage.output_tokens, models.LlmUsage.cost_usd,
            ).where(models.LlmUsage.created_at >= start, models.LlmUsage.created_at < end)
        ).all()
        aggs = aggregate_usage([tuple(r) for r in rows])
        s.execute(delete(models.UsageDaily).where(models.UsageDaily.day == day))
        for a in aggs:
            s.add(models.UsageDaily(day=day, **a))
        s.commit()
    return len(aggs)


def _purge_old(session_factory, model, retention_days: int) -> int:
    """created_at이 retention_days 지난 raw 행을 삭제. retention_days<=0이면 영구 보관(삭제 0).

    테이블 성격별로 다른 보관기간을 준다(정책 문서 참고): 집계본이 있는 raw는 짧게,
    forensic 감사로그는 길게/영구. 삭제 행 수 반환."""
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    with session_factory() as s:
        result = s.execute(delete(model).where(model.created_at < cutoff))
        s.commit()
    return result.rowcount or 0


def purge_old_metrics(session_factory, retention_days: int) -> int:
    """retention_days 지난 request_metrics raw를 삭제한다 (집계본은 유지). 삭제 행 수 반환."""
    return _purge_old(session_factory, models.RequestMetric, retention_days)


# 테이블별 보관기간(일) — env 오버라이드. 근거는 docs/관측_운영_정책.md.
#  request_metrics: 30 (metrics_daily 집계본 있음), turn_events: 30 (고volume 디버그),
#  llm_usage: 90 (usage_daily 집계본 있음, 비용 재조회 여지), audit_logs: 365 (forensic, 0=영구).
_RETENTION = (
    ("METRICS_RETENTION_DAYS", "30", lambda: models.RequestMetric, "request_metrics"),
    ("TURN_EVENTS_RETENTION_DAYS", "30", lambda: models.TurnEvent, "turn_events"),
    ("LLM_USAGE_RETENTION_DAYS", "90", lambda: models.LlmUsage, "llm_usage"),
    ("AUDIT_RETENTION_DAYS", "365", lambda: models.AuditLog, "audit_logs"),
)


def purge_all_raw(session_factory) -> None:
    """모든 raw 관측 테이블의 보관기간 정리(멱등, best-effort). 실패는 경고만."""
    for env_key, default, model_fn, label in _RETENTION:
        try:
            days = int(os.getenv(env_key, default))
            purged = _purge_old(session_factory, model_fn(), days)
            if purged:
                logger.info("%s retention: %d행 삭제 (>%d일)", label, purged, days)
        except Exception:  # noqa: BLE001 — 한 테이블 실패가 나머지 정리를 막으면 안 됨
            logger.warning("%s retention 정리 실패 (다음 주기 재시도)", label, exc_info=True)


def run_rollup(days_back: int = 1) -> None:
    """오늘부터 days_back일 전까지 멱등 재집계 + retention. 스케줄러/시작 캐치업 공용.

    실패는 경고만 — 다음 주기에 멱등 재시도되므로 유실이 아니다.
    """
    from app.core.observability_db import observability_sessionmaker

    sf = observability_sessionmaker()
    today = datetime.now(timezone.utc).date()
    for i in range(days_back + 1):
        day = today - timedelta(days=i)
        try:
            m = rollup_metrics_day(sf, day)
            u = rollup_usage_day(sf, day)
            logger.info("롤업 완료 %s: metrics %d행, usage %d행", day, m, u)
        except Exception:  # noqa: BLE001 — 롤업 실패가 스케줄러를 죽이면 안 됨
            logger.warning("롤업 실패 %s (다음 주기에 재시도)", day, exc_info=True)
    purge_all_raw(sf)  # 테이블별 보관기간 정리 (request_metrics·turn_events·llm_usage·audit_logs)

    # 집계가 방금 갱신됐으니 지금이 임계를 볼 자리다 (RPA-189).
    # 별도 스케줄러를 두면 집계와 알림이 어긋나(집계 전 값으로 판정) 있지도 않은 급등을 알린다.
    # SLACK_WEBHOOK_URL 미설정이면 no-op — 기존 배포의 동작은 그대로다.
    try:
        from app.services import alerts

        alerts.check_daily_thresholds()
    except Exception:  # noqa: BLE001 — 알림 실패가 롤업(집계·retention)을 죽이면 안 된다
        logger.warning("임계 알림 실패 (롤업은 정상 완료)", exc_info=True)
