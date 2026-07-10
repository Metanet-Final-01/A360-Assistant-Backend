"""운영·모니터링 집계 조회 API — llm_usage / audit_logs를 읽는다 (감사 D, RPA-81).

기록(관측성 미들웨어·usage_context)은 있는데 읽을 창구가 없어 모니터링·데이터 시각화(KDH)가
막혀 있던 것을 연다. 접근은 ADMIN_EMAILS 화이트리스트(환경변수)로 게이트한다 — 전 사용자의
감사로그·사용량이 노출되는 API라 "로그인만"으로는 부족(CodeRabbit). DB 롤(users.is_admin)
기반 세밀한 권한은 스키마 확장이 필요해 후속으로 두되, 그 전까지 이 게이트가 격리를 보장한다.
"""

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

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
    db: Session = Depends(get_obs_db),
    user: models.User = Depends(require_admin),
) -> dict:
    """최근 감사 로그(변경성 요청, 최신순) — method/status_code/user_id 필터. forensics·모니터링용."""
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
