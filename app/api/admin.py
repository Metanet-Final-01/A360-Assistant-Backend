"""운영·모니터링 집계 조회 API — llm_usage / audit_logs를 읽는다 (감사 D, RPA-81).

기록(관측성 미들웨어·usage_context)은 있는데 읽을 창구가 없어 모니터링·데이터 시각화(KDH)가
막혀 있던 것을 연다. 접근은 ADMIN_EMAILS 화이트리스트(환경변수)로 게이트한다 — 전 사용자의
감사로그·사용량이 노출되는 API라 "로그인만"으로는 부족(CodeRabbit). DB 롤(users.is_admin)
기반 세밀한 권한은 스키마 확장이 필요해 후속으로 두되, 그 전까지 이 게이트가 격리를 보장한다.
"""

import logging
import os
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app import models
from app.api.auth import get_optional_user
from app.core.observability_db import get_obs_db
from app.db import get_db
from app.rag.retrieval.params import RetrievalParams
from app.schemas.budget import BudgetLimitsUpdate
from app.schemas.retrieval import RetrievalParamsUpdate
from app.services import budget as budget_service
from app.services import retrieval_params as rp_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _service_key_ok(request: Request) -> bool:
    """X-API-Key가 OPS_API_KEY와 일치하나 — 머신(M2M) 신원. 사람 로그인 재사용을 대체한다.

    OPS_API_KEY 미설정/빈값이면 이 경로는 비활성(fail-closed) — 빈 키가 우연히 통과하지 않게.
    compare_digest로 타이밍 공격을 막는다.
    """
    ops_key = os.getenv("OPS_API_KEY", "").strip()
    if not ops_key:
        return False
    provided = request.headers.get("X-API-Key", "")
    return bool(provided) and secrets.compare_digest(provided, ops_key)


def require_admin(
    request: Request,
    user: models.User | None = Depends(get_optional_user),
) -> models.User | None:
    """관리자 게이트 — 둘 중 하나면 통과, 아니면 403 (RPA-118).

    ① 서비스 API 키(X-API-Key == OPS_API_KEY): ops-server 같은 머신 신원. 사람 계정을
       빌려 로그인하는 안티패턴 대신 전용 자격을 쓴다.
    ② is_admin=True 사용자의 JWT: 인가를 문자열 화이트리스트가 아니라 서버 속성으로 판정
       (개방 가입으로 화이트리스트 이메일을 선점해도 is_admin이 없으면 무의미).
    """
    if _service_key_ok(request):
        return None  # 머신 신원 — 사용자 객체 없음(엔드포인트는 user를 게이트로만 씀)
    if user is not None and user.is_admin:
        return user
    raise HTTPException(
        403, detail={"code": "FORBIDDEN", "message": "관리자만 접근할 수 있습니다."}
    )


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
    "session": models.LlmUsage.session_id,  # 세션별 비용 리포트(대시보드 #6)
}


@router.get("/llm-usage/stats")
def llm_usage_stats(
    days: int = Query(30, ge=1, le=365, description="집계 기간(일)"),
    group_by: str = Query("component", pattern="^(component|model|user|session)$"),
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


@router.get("/rag-events")
def rag_events(
    request_id: str | None = Query(None),
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_obs_db),
    user: models.User = Depends(require_admin),
) -> dict:
    """RAG 파이프라인 단계 로그(RPA-128) 조회 — request_id로 한 검색 흐름의 단계별 소요·설정.
    사건 추적 대시보드가 embed/search/rerank 병목을 보이는 데 쓴다."""
    q = select(models.RagEvent).order_by(
        models.RagEvent.created_at.desc(), models.RagEvent.id.desc()
    )
    if request_id:
        q = q.where(models.RagEvent.request_id == request_id)
    rows = db.execute(q.limit(limit)).scalars().all()
    return {
        "events": [
            {
                "id": r.id,
                "request_id": r.request_id,
                "event": r.event,
                "function": r.function,
                "status": r.status,
                "duration_ms": r.duration_ms,
                "detail": r.detail,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


def _params_payload(p: RetrievalParams) -> dict:
    """RetrievalParams → 응답 dict (필드명은 .env·DB·API가 동일하게 쓴다)."""
    return {
        "candidate_pool_size": p.candidate_pool_size,
        "rerank_candidates": p.rerank_candidates,
        "rrf_k": p.rrf_k,
        "vector_weight": p.vector_weight,
        "bm25_weight": p.bm25_weight,
    }


@router.get("/retrieval-params")
def get_retrieval_params(
    db: Session = Depends(get_db),
    user: models.User = Depends(require_admin),
) -> dict:
    """현재 활성 검색 파라미터 (RPA-149). DB 오버라이드가 있으면 그 값(source=db), 없으면
    .env 기본값(source=config)을 준다 — ops 슬라이더가 이 값을 프리필한다.

    검색 경로가 실제로 쓰는 값과 일치시키려고 DB 조회는 이 라우트가 직접 한다(캐시 우회) —
    관리자가 방금 바꾼 값을 캐시 TTL 때문에 옛 값으로 보여주면 혼란스럽기 때문.
    """
    row = (
        db.query(models.RetrievalParamOverride)
        .order_by(models.RetrievalParamOverride.id.desc())
        .first()
    )
    if row is None:
        return {
            "source": "config",
            **_params_payload(RetrievalParams.from_config()),
            "updated_by": None,
            "updated_at": None,
        }
    return {
        "source": "db",
        "candidate_pool_size": row.candidate_pool_size,
        "rerank_candidates": row.rerank_candidates,
        "rrf_k": row.rrf_k,
        "vector_weight": row.vector_weight,
        "bm25_weight": row.bm25_weight,
        "updated_by": row.updated_by,
        "updated_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.put("/retrieval-params")
def put_retrieval_params(
    body: RetrievalParamsUpdate,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(require_admin),
) -> dict:
    """검색 파라미터 갱신 (RPA-149) — 재시작 없이 다음 검색부터 반영.

    검증은 새로 짜지 않고 RetrievalParams(**body) 생성에 위임한다(단일 진실) — 범위(1 이상)·
    nan/inf·음수 위반은 __post_init__이 ValueError를 던지고, 여기서 400으로 변환한다. append-only로
    한 행을 추가하고(감사 이력) 캐시를 무효화해 즉시 반영한다. updated_by는 사람 관리자면 이메일,
    ops-server(X-API-Key)면 user가 None이라 "service"로 남긴다.
    """
    try:
        RetrievalParams(**body.model_dump())  # 값 검증 재사용 — 통과해야만 저장
    except ValueError as e:
        raise HTTPException(
            400, detail={"code": "INVALID_PARAMS", "message": str(e)}
        ) from None

    row = models.RetrievalParamOverride(
        **body.model_dump(),
        updated_by=user.email if user is not None else "service",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    rp_service.bust_cache()  # 다음 load_active_params()가 DB를 다시 읽게 — 무중단 반영
    logger.info("검색 파라미터 갱신 by=%s params=%s", row.updated_by, _params_payload(RetrievalParams(**body.model_dump())))
    return {
        "source": "db",
        **_params_payload(RetrievalParams(**body.model_dump())),
        "updated_by": row.updated_by,
        "updated_at": row.created_at.isoformat() if row.created_at else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# LLM 예산 상한 런타임 조정 (RPA-173) — retrieval-params(RPA-149)와 같은 패턴
# ─────────────────────────────────────────────────────────────────────────────

_BUDGET_COLUMNS = ("subject_daily_usd", "subject_monthly_usd",
                   "global_daily_usd", "global_monthly_usd")


@router.get("/budget-limits")
def get_budget_limits(
    db: Session = Depends(get_db),
    user: models.User = Depends(require_admin),
) -> dict:
    """현재 활성 예산 상한 (RPA-173). DB 오버라이드가 있으면 그 값(source=db), 없으면
    .env 기본값(source=config)을 준다 — ops 화면이 이 값을 프리필한다.

    /turn이 실제로 쓰는 값과 일치시키려고 DB 조회는 이 라우트가 직접 한다(캐시 우회) —
    관리자가 방금 바꾼 값을 캐시 TTL 때문에 옛 값으로 보여주면 혼란스럽기 때문(RPA-149 규칙).
    """
    row = (
        db.query(models.BudgetLimitOverride)
        .order_by(models.BudgetLimitOverride.id.desc())
        .first()
    )
    if row is None:
        env = budget_service._env_limits()
        return {
            "source": "config",
            "subject_daily_usd": env["subject_daily"],
            "subject_monthly_usd": env["subject_monthly"],
            "global_daily_usd": env["global_daily"],
            "global_monthly_usd": env["global_monthly"],
            "updated_by": None,
            "updated_at": None,
        }
    return {
        "source": "db",
        **{c: getattr(row, c) for c in _BUDGET_COLUMNS},
        "updated_by": row.updated_by,
        "updated_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.put("/budget-limits")
def put_budget_limits(
    body: BudgetLimitsUpdate,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(require_admin),
) -> dict:
    """예산 상한 갱신 (RPA-173) — 재배포 없이 다음 턴부터 반영.

    값 검증은 BudgetLimitsUpdate가 한다(0·음수 거부, 월<일 거부) — Pydantic이 422로 떨군다.
    append-only로 한 행을 추가하고(감사 이력) 캐시를 무효화해 즉시 반영한다. updated_by는 사람
    관리자면 이메일, ops-server(X-API-Key)면 user가 None이라 "service".

    ⚠️ 이 API는 **서비스를 막는 값**을 바꾼다 — 잘못 낮추면 정상 사용자가 429를 맞는다.
    근거 없는 변경을 막으려면 scripts/budget_calibration_report.py로 실측 권장값을 먼저 뽑을 것.
    """
    row = models.BudgetLimitOverride(
        **body.model_dump(),
        updated_by=user.email if user is not None else "service",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    budget_service.bust_cache()  # 다음 check_budget()이 DB를 다시 읽게 — 무중단 반영
    logger.info("예산 상한 갱신 by=%s limits=%s", row.updated_by, body.model_dump())
    return {
        "source": "db",
        **{c: getattr(row, c) for c in _BUDGET_COLUMNS},
        "updated_by": row.updated_by,
        "updated_at": row.created_at.isoformat() if row.created_at else None,
    }
