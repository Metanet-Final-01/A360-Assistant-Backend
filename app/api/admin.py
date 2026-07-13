"""운영·모니터링 집계 조회 API — llm_usage / audit_logs를 읽는다 (감사 D, RPA-81).

기록(관측성 미들웨어·usage_context)은 있는데 읽을 창구가 없어 모니터링·데이터 시각화(KDH)가
막혀 있던 것을 연다. 접근은 ADMIN_EMAILS 화이트리스트(환경변수)로 게이트한다 — 전 사용자의
감사로그·사용량이 노출되는 API라 "로그인만"으로는 부족(CodeRabbit). DB 롤(users.is_admin)
기반 세밀한 권한은 스키마 확장이 필요해 후속으로 두되, 그 전까지 이 게이트가 격리를 보장한다.
"""

import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app import models
from app.api.auth import get_current_user
from app.core.observability_db import get_obs_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_admin(user: models.User = Depends(get_current_user)) -> models.User:
    """ADMIN_EMAILS(쉼표 구분) 화이트리스트 게이트 — 미설정이면 전부 차단(fail-closed).

    임의의 로그인 사용자가 다른 사용자의 user_id·요청 경로·사용량을 열람하지 못하게 한다.
    """
    allowed = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
    if user.email.lower() not in allowed:
        raise HTTPException(
            403, detail={"code": "FORBIDDEN", "message": "관리자만 접근할 수 있습니다."}
        )
    return user


def _parse_since(value: str) -> datetime:
    """증분 수집 커서(ISO8601) 파싱 — naive면 UTC로 간주, 형식 오류는 400.

    백오피스 수집기가 '마지막으로 받은 created_at'을 그대로 되돌려주는 용도라
    응답의 isoformat()과 왕복 가능해야 한다.
    """
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            400, detail={"code": "INVALID_SINCE", "message": "since는 ISO8601 형식이어야 합니다."}
        ) from None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


# group_by 화이트리스트 — 임의 컬럼 주입을 막고 3축(component/model/user)만 허용
_GROUP_COLS = {
    "component": models.LlmUsage.component,
    "model": models.LlmUsage.model,
    "user": models.LlmUsage.user_id,
}


@router.get("/llm-usage/stats")
def llm_usage_stats(
    days: int = Query(30, ge=1, le=365, description="집계 기간(일)"),
    group_by: str = Query("component", pattern="^(component|model|user)$"),
    db: Session = Depends(get_obs_db),
    user: models.User = Depends(require_admin),
) -> dict:
    """기간 내 LLM 사용량 집계 — 총계 + group_by별 breakdown (시각화 데이터 소스)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    col = _GROUP_COLS[group_by]
    rows = db.execute(
        select(
            col.label("key"),
            func.count().label("calls"),
            func.coalesce(func.sum(models.LlmUsage.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(models.LlmUsage.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(models.LlmUsage.cost_usd), 0.0).label("cost_usd"),
        )
        .where(models.LlmUsage.created_at >= since)
        .group_by(col)
        .order_by(func.count().desc())
    ).all()

    breakdown = [
        {
            "key": str(r.key) if r.key is not None else None,
            "calls": int(r.calls),
            "input_tokens": int(r.input_tokens),
            "output_tokens": int(r.output_tokens),
            "cost_usd": round(float(r.cost_usd or 0.0), 6),
        }
        for r in rows
    ]
    total = {
        "calls": sum(b["calls"] for b in breakdown),
        "input_tokens": sum(b["input_tokens"] for b in breakdown),
        "output_tokens": sum(b["output_tokens"] for b in breakdown),
        "cost_usd": round(sum(b["cost_usd"] for b in breakdown), 6),
    }
    return {"period_days": days, "group_by": group_by, "total": total, "breakdown": breakdown}


@router.get("/audit-logs")
def audit_logs(
    limit: int = Query(100, ge=1, le=500),
    method: str | None = Query(None, description="GET/POST 등 필터"),
    status_code: int | None = Query(None),
    user_id: str | None = Query(None),
    since: str | None = Query(None, description="ISO8601 — 이 시각 이후만 (증분 수집 커서)"),
    db: Session = Depends(get_obs_db),
    user: models.User = Depends(require_admin),
) -> dict:
    """감사 로그(변경성 요청) — method/status_code/user_id 필터. forensics·모니터링용.

    since 지정 시 그 이후를 **오름차순**으로 준다 — 수집기가 마지막 created_at을
    다음 since로 쓰는 커서 방식이라, 최신순이면 limit에 걸렸을 때 중간이 유실된다.
    미지정이면 기존대로 최신순 limit건 (화면 조회용).
    """
    if since:
        q = (
            select(models.AuditLog)
            .where(models.AuditLog.created_at > _parse_since(since))
            .order_by(models.AuditLog.created_at.asc(), models.AuditLog.id.asc())
            .limit(limit)
        )
    else:
        q = select(models.AuditLog).order_by(models.AuditLog.created_at.desc()).limit(limit)
    if method:
        q = q.where(models.AuditLog.method == method.upper())
    if status_code is not None:
        q = q.where(models.AuditLog.status_code == status_code)
    if user_id:
        try:
            key = uuid.UUID(user_id)
        except ValueError:
            raise HTTPException(
                400, detail={"code": "INVALID_ID", "message": "user_id 형식이 올바르지 않습니다."}
            ) from None
        q = q.where(models.AuditLog.user_id == key)
    rows = db.execute(q).scalars().all()
    return {
        "logs": [
            {
                "request_id": r.request_id,
                "user_id": str(r.user_id) if r.user_id else None,
                "method": r.method,
                "path": r.path,
                "status_code": r.status_code,
                "latency_ms": r.latency_ms,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.get("/metrics-daily")
def metrics_daily(
    days: int = Query(7, ge=1, le=90, description="조회 기간(일)"),
    method: str | None = Query(None, description="GET/POST 등 필터"),
    path: str | None = Query(None, description="정규화된 경로 부분일치 (예: /api/sessions)"),
    db: Session = Depends(get_obs_db),
    user: models.User = Depends(require_admin),
) -> dict:
    """일별 요청 성능 롤업(RPA-104 metrics_daily) 조회 — Ops 대시보드가 raw 대신 이걸 읽는다."""
    since = date.today() - timedelta(days=days)
    q = select(models.MetricsDaily).where(models.MetricsDaily.day >= since).order_by(
        models.MetricsDaily.day.desc(), models.MetricsDaily.calls.desc()
    )
    if method:
        q = q.where(models.MetricsDaily.method == method.upper())
    if path:
        q = q.where(models.MetricsDaily.path.contains(path))
    rows = db.execute(q).scalars().all()
    return {
        "rows": [
            {
                "day": r.day.isoformat() if r.day else None,
                "method": r.method,
                "path": r.path,
                "calls": r.calls,
                "err_4xx": r.err_4xx,
                "err_5xx": r.err_5xx,
                "p50_ms": r.p50_ms,
                "p95_ms": r.p95_ms,
                "avg_ms": r.avg_ms,
                "max_ms": r.max_ms,
            }
            for r in rows
        ]
    }


@router.get("/usage-daily")
def usage_daily(
    days: int = Query(30, ge=1, le=365, description="조회 기간(일)"),
    component: str | None = Query(None),
    model: str | None = Query(None),
    db: Session = Depends(get_obs_db),
    user: models.User = Depends(require_admin),
) -> dict:
    """일별 LLM 사용량 롤업(RPA-104 usage_daily) 조회 — 비용/토큰 추이용."""
    since = date.today() - timedelta(days=days)
    q = select(models.UsageDaily).where(models.UsageDaily.day >= since).order_by(
        models.UsageDaily.day.desc()
    )
    if component:
        q = q.where(models.UsageDaily.component == component)
    if model:
        q = q.where(models.UsageDaily.model == model)
    rows = db.execute(q).scalars().all()
    return {
        "rows": [
            {
                "day": r.day.isoformat() if r.day else None,
                "component": r.component,
                "purpose": r.purpose,
                "model": r.model,
                "calls": r.calls,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cost_usd": round(float(r.cost_usd), 6) if r.cost_usd is not None else None,
            }
            for r in rows
        ]
    }


@router.get("/turn-events")
def turn_events(
    session_id: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_obs_db),
    user: models.User = Depends(require_admin),
) -> dict:
    """에이전트 턴 노드 타임라인(RPA-105 turn_events) 조회 — 어떤 노드를 얼마 만에 탔고
    어디서 실패했나. session_id를 지정하면 그 세션의 턴만, 순서(seq)대로 반환한다."""
    if session_id:
        try:
            sid = uuid.UUID(session_id)
        except ValueError:
            raise HTTPException(
                400, detail={"code": "INVALID_ID", "message": "session_id 형식이 올바르지 않습니다."}
            ) from None
        # request_id는 uuid4라 시간과 무관하다 — order_by(request_id, seq)에 바로
        # limit을 걸면 최신 턴이 아니라 사전순으로 앞선 임의의 턴만 잘릴 수 있다
        # (CodeRabbit 지적). created_at(시간축)으로 먼저 최근 limit건을 고르고,
        # 화면에 보여줄 턴 진행 순서(request_id, seq)는 바깥 쿼리에서 별도로 맞춘다.
        recent = (
            select(models.TurnEvent)
            .where(models.TurnEvent.session_id == sid)
            .order_by(models.TurnEvent.created_at.desc(), models.TurnEvent.id.desc())
            .limit(limit)
            .subquery()
        )
        recent_event = aliased(models.TurnEvent, recent)
        q = select(recent_event).order_by(recent_event.request_id, recent_event.seq)
    else:
        q = select(models.TurnEvent).order_by(models.TurnEvent.created_at.desc()).limit(limit)
    rows = db.execute(q).scalars().all()
    return {
        "events": [
            {
                "session_id": str(r.session_id) if r.session_id else None,
                "request_id": r.request_id,
                "seq": r.seq,
                "kind": r.kind,
                "stage": r.stage,
                "message": r.message,
                "detail": r.detail,
                "elapsed_ms": r.elapsed_ms,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.get("/request-metrics")
def request_metrics(
    since: str | None = Query(None, description="ISO8601 — 이 시각 이후만 (증분 수집 커서)"),
    limit: int = Query(500, ge=1, le=2000),
    method: str | None = Query(None, description="GET/POST 등 필터"),
    path: str | None = Query(None, description="정규화된 경로 부분일치 (예: /api/sessions)"),
    db: Session = Depends(get_obs_db),
    user: models.User = Depends(require_admin),
) -> dict:
    """raw 요청 메트릭(RPA-103 request_metrics) 조회 — 롤업(최대 60분 지연)을 보완하는
    "오늘 실시간" 패널용. audit-logs와 같은 커서 규칙: since 지정 시 오름차순(증분 수집),
    미지정 시 최신순 limit건. id는 수집기 중복 제거용."""
    q = select(models.RequestMetric)
    if since:
        q = q.where(models.RequestMetric.created_at > _parse_since(since)).order_by(
            models.RequestMetric.created_at.asc(), models.RequestMetric.id.asc()
        )
    else:
        q = q.order_by(models.RequestMetric.created_at.desc(), models.RequestMetric.id.desc())
    if method:
        q = q.where(models.RequestMetric.method == method.upper())
    if path:
        q = q.where(models.RequestMetric.path.contains(path))
    rows = db.execute(q.limit(limit)).scalars().all()
    return {
        "rows": [
            {
                "id": r.id,
                "request_id": r.request_id,
                "user_id": str(r.user_id) if r.user_id else None,
                "method": r.method,
                "path": r.path,
                "status_code": r.status_code,
                "latency_ms": r.latency_ms,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }
