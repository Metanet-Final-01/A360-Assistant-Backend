"""세션 단위 라우터 — 세션 생성, 추천안(흐름도) 버전 CRUD, 에이전트 단일 진입점(/turn).

분석/질문/흐름도 수정은 모두 POST /{id}/turn 하나로 처리한다 (app/agent의 stream_agent_turn).
레거시 개별 엔드포인트(/analyze, /recommend, /api/agent/chat)는 /turn으로 흡수돼 제거됐다 (RPA-67).
"""

import asyncio
import contextlib
import json
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import models
from app.api.auth import assert_session_owner, get_current_user, get_optional_user
from app.core import config
from app.core.llm import usage_context
from app.core.masking import mask_fields, mask_pii
from app.db import get_db
from app.schemas import ProgressEvent, Recommendation
from app.services import alerts, budget
from app.services.assurance_evidence import persist_output_receipt
from app.services.output_assurance import (
    OutputBoundaryContext,
    build_unassured_observation,
    finalize_persistence_observation,
    observe_recommendation_candidate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.post("", status_code=201)
def create_session(
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_optional_user),
) -> dict:
    """빈 세션을 만든다 — 문서 없이 멀티턴 챗을 시작할 때 쓴다.

    문서 업로드도 세션을 자동 생성하지만, 표준 챗봇처럼 문서가 없는 경우엔 이걸로
    session_id를 먼저 받아 챗(session_id 전달)의 대화 이력을 이 세션에 쌓는다.
    """
    session = models.AnalysisSession(title="채팅", user_id=user.id if user else None)
    db.add(session)
    db.commit()
    return {"session_id": str(session.id), "created_at": session.created_at.isoformat() if session.created_at else None}


# ─────────────────────────────────────────────────────────────────────────────
# 세션 조회·관리 — 목록/상세/삭제/대화이력/분석재조회 (FR-20, RPA-78)
# 데이터는 DB에 쌓이지만 읽을 API가 없어 프론트(아카이브·재조회)가 막혀 있던 것을 연다.
# ─────────────────────────────────────────────────────────────────────────────


def _session_out(session: models.AnalysisSession) -> dict:
    return {
        "id": str(session.id),
        "title": session.title,
        "solution": session.solution,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
    }


def _analysis_out(row: models.Analysis) -> dict:
    return {
        "id": str(row.id),
        "document_id": str(row.document_id) if row.document_id else None,
        "status": row.status,
        "model": row.model,
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }


@router.get("")
def list_sessions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """로그인 사용자의 세션 목록(최신 활동순). 익명 세션(user_id NULL)은 소유자 식별이
    안 되므로 목록 대상이 아니다 — 그래서 인증을 요구한다(get_current_user → 미인증 401)."""
    rows = db.execute(
        select(models.AnalysisSession)
        .where(models.AnalysisSession.user_id == user.id)
        .order_by(models.AnalysisSession.updated_at.desc())
        .limit(limit).offset(offset)
    ).scalars().all()
    return {"sessions": [_session_out(s) for s in rows]}


@router.get("/{session_id}")
def get_session(
    session_id: str,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_optional_user),
) -> dict:
    """세션 상세(메타)."""
    return _session_out(_owned_session_or_404(session_id, db, user))


@router.delete("/{session_id}", status_code=204)
def delete_session(
    session_id: str,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_optional_user),
) -> None:
    """세션 삭제 — 문서/분석/추천/대화/compact가 FK ON DELETE CASCADE로 함께 삭제된다.

    Core delete를 쓴다: ORM db.delete(session)는 관계(documents/recommendations)의 자식 FK를
    NULL로 만들려다 NOT NULL 제약에 걸린다. 원시 DELETE는 DB CASCADE가 모든 자식을 처리한다.
    """
    session = _owned_session_or_404(session_id, db, user)  # 소유권 검사
    db.execute(delete(models.AnalysisSession).where(models.AnalysisSession.id == session.id))
    db.commit()


@router.get("/{session_id}/chat-messages")
def list_chat_messages(
    session_id: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_optional_user),
) -> dict:
    """세션의 대화 이력(시간순) — 프론트가 아카이브/새로고침에서 재구성한다."""
    session = _owned_session_or_404(session_id, db, user)
    rows = db.execute(
        select(models.ChatMessage)
        .where(models.ChatMessage.session_id == session.id)
        .order_by(models.ChatMessage.created_at.asc())
        .limit(limit).offset(offset)
    ).scalars().all()
    return {
        "messages": [
            {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "recommendation_version": m.recommendation_version,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in rows
        ]
    }


@router.get("/{session_id}/analyses")
def list_analyses(
    session_id: str,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_optional_user),
) -> dict:
    """세션의 분석 목록(메타, 최신순). 전체 result는 /analyses/latest로 받는다."""
    session = _owned_session_or_404(session_id, db, user)
    rows = db.execute(
        select(models.Analysis)
        .where(models.Analysis.session_id == session.id)
        .order_by(models.Analysis.created_at.desc())
    ).scalars().all()
    return {"analyses": [_analysis_out(r) for r in rows]}


@router.get("/{session_id}/analyses/latest")
def get_latest_analysis(
    session_id: str,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_optional_user),
) -> dict:
    """최신 완료 분석의 전체 result — SSE done을 놓쳤을 때 재조회한다."""
    session = _owned_session_or_404(session_id, db, user)
    row = db.execute(
        select(models.Analysis)
        .where(models.Analysis.session_id == session.id, models.Analysis.status == "completed")
        .order_by(models.Analysis.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, detail={"code": "NO_ANALYSIS", "message": "완료된 분석이 없습니다."})
    return {**_analysis_out(row), "result": row.result}


# ─────────────────────────────────────────────────────────────────────────────
# 추천안(흐름도) 생성·저장·버전 편집 (FR-09~18, RPA-61)
# 흐름도 = Recommendation 트리(steps→actions→children). 프론트가 이 트리를 블록으로
# 렌더·드래그 편집하고, 편집본을 새 버전으로 저장한다(수정=UPDATE 아닌 새 버전 INSERT).
# ─────────────────────────────────────────────────────────────────────────────


def _owned_session_or_404(session_id: str, db: Session, user) -> models.AnalysisSession:
    try:
        key = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(
            400, detail={"code": "INVALID_ID", "message": "세션 ID 형식이 올바르지 않습니다."}
        ) from None
    session = db.get(models.AnalysisSession, key)
    if session is None:
        raise HTTPException(404, detail={"code": "SESSION_NOT_FOUND", "message": "세션을 찾을 수 없습니다."})
    assert_session_owner(session, user)
    return session


def _recommendation_out(row: models.RecommendationVersion) -> dict:
    return {
        "id": str(row.id),
        "version": row.version,
        "parent_version": row.parent_version,
        "source": row.source,
        "change_summary": row.change_summary,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _current_request_id() -> str | None:
    from app.rag.observability import get_request_id

    return get_request_id()


def _agent_registry_snapshot() -> dict | None:
    registry = _get_agent_versions()
    if registry is None:
        return None
    available_versions, default_version = registry
    try:
        return {"versions": available_versions(), "default": default_version()}
    except Exception:  # 공개 registry 관측 실패는 Output Boundary가 미관측으로 기록한다.
        logger.warning("Agent 공개 버전 registry 관측 실패", exc_info=True)
        return None


def _log_output_observation(observation: dict) -> None:
    logger.info(
        "output_boundary_observe %s",
        json.dumps(observation, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def _save_recommendation(
    session_id: uuid.UUID, analysis_id: uuid.UUID, payload: dict,
    source: str, parent_version: int | None, change_summary: str | None = None,
    request_id: str | None = None, requested_agent_version: str | None = None,
    resolved_agent_version: str | None = None, agent_registry_snapshot: Any = None,
    producer_advisory: Any = None,
) -> dict:
    """새 추천안 버전을 저장한다 (version은 세션 내 max+1). 새 세션 사용(스트리밍 후에도 안전).

    동시 저장으로 같은 version이 계산되면 uq_recommendations_session_version 제약에
    걸리므로(더블클릭 등), IntegrityError 시 version을 재계산해 몇 번 재시도한다.
    """
    from sqlalchemy.exc import IntegrityError

    from app.db import SessionLocal

    boundary_context = OutputBoundaryContext(
        session_id=str(session_id),
        request_id=request_id,
        source=source,
        requested_agent_version=requested_agent_version,
        resolved_agent_version=resolved_agent_version,
        agent_registry_snapshot=agent_registry_snapshot,
        producer_advisory=producer_advisory,
    )
    try:
        observation = observe_recommendation_candidate(payload, boundary_context)
    except Exception as exc:
        # Observe mode records detector failure without changing the existing save outcome.
        logger.warning("Output Boundary Observe 관측 실패", exc_info=True)
        observation = build_unassured_observation(
            boundary_context, error_type=type(exc).__name__
        )
    for attempt in range(3):
        try:
            with SessionLocal() as s:
                last = s.execute(
                    select(func.max(models.RecommendationVersion.version))
                    .where(models.RecommendationVersion.session_id == session_id)
                ).scalar()
                version = (last or 0) + 1
                row = models.RecommendationVersion(
                    id=uuid.uuid4(),
                    session_id=session_id,
                    analysis_id=analysis_id,
                    version=version,
                    parent_version=parent_version if parent_version is not None else last,
                    source=source,
                    payload=payload,
                    change_summary=change_summary,
                )
                s.add(row)
                s.commit()
        except IntegrityError as exc:
            if attempt == 2:  # 마지막 시도까지 충돌하면 상위에서 처리
                failed = finalize_persistence_observation(
                    observation, persisted=False, error_type=type(exc).__name__
                )
                _log_output_observation(failed)
                raise
            logger.warning("추천안 version 충돌 — 재계산 재시도 (session=%s)", session_id)
        except Exception as exc:
            failed = finalize_persistence_observation(
                observation, persisted=False, error_type=type(exc).__name__
            )
            _log_output_observation(failed)
            raise
        else:
            finalized = finalize_persistence_observation(observation, persisted=True)
            _log_output_observation(finalized)
            receipt = persist_output_receipt(
                finalized,
                recommendation_id=row.id,
                recommendation_version=row.version,
            )
            return {
                **_recommendation_out(row),
                "output_assurance": finalized,
                "assurance_receipt": receipt,
            }


@router.get("/{session_id}/recommendations")
def list_recommendations(
    session_id: str,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_optional_user),
) -> dict:
    """세션의 추천안 버전 목록(메타) — undo·이력 UI용. 최신 버전이 먼저."""
    session = _owned_session_or_404(session_id, db, user)
    rows = db.execute(
        select(models.RecommendationVersion)
        .where(models.RecommendationVersion.session_id == session.id)
        .order_by(models.RecommendationVersion.version.desc())
    ).scalars().all()
    return {"versions": [_recommendation_out(r) for r in rows]}


@router.get("/{session_id}/recommendations/latest")
def get_latest_recommendation(
    session_id: str,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_optional_user),
) -> dict:
    """최신 추천안 트리 전체 — 프론트가 흐름도로 렌더한다."""
    session = _owned_session_or_404(session_id, db, user)
    row = db.execute(
        select(models.RecommendationVersion)
        .where(models.RecommendationVersion.session_id == session.id)
        .order_by(models.RecommendationVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, detail={"code": "NO_RECOMMENDATION", "message": "저장된 추천안이 없습니다."})
    return {**_recommendation_out(row), "recommendation": row.payload}


class SaveRecommendationRequest(BaseModel):
    recommendation: dict = Field(description="편집된 Recommendation 트리 (흐름도)")
    parent_version: int | None = Field(None, description="이 편집의 기준 버전 (없으면 현재 최신)")
    source: str = Field("drag", pattern="^(drag|chat|feedback)$", description="편집 출처")
    change_summary: str | None = Field(None, max_length=500)


@router.post("/{session_id}/recommendations", status_code=201)
def save_edited_recommendation(
    session_id: str,
    payload: SaveRecommendationRequest,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_optional_user),
) -> dict:
    """프론트에서 블록 드래그로 편집한 추천안을 새 버전으로 저장한다 (FR-18).

    수정은 UPDATE가 아니라 새 버전 INSERT — undo·수정 이력이 여기서 나온다.
    페이로드는 Recommendation 스키마로 검증한다(카탈로그 액션명 검증은 후속).
    """
    session = _owned_session_or_404(session_id, db, user)
    try:
        Recommendation.model_validate(payload.recommendation)  # 스키마 검증 (구조 깨짐 방지)
    except ValidationError as e:
        # 개수만 주면 프론트가 어디를 고쳐야 할지 알 수 없다 — 필드 경로·사유까지 돌려준다.
        # err["input"]은 트리 전체가 실릴 수 있어 제외(응답 비대·원문 반향 방지), 상한 10건.
        errors = [
            {"field": ".".join(str(p) for p in err["loc"]) or "(root)", "reason": err["msg"]}
            for err in e.errors()[:10]
        ]
        raise HTTPException(
            400,
            detail={
                "code": "INVALID_RECOMMENDATION",
                "message": f"추천안 형식이 올바르지 않습니다: {e.error_count()}건",
                "errors": errors,
            },
        ) from None
    # 기준 버전(부모)에서 analysis_id를 이어받는다 — 편집본도 같은 분석에 속한다
    base = db.execute(
        select(models.RecommendationVersion)
        .where(models.RecommendationVersion.session_id == session.id)
        .order_by(models.RecommendationVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if base is None:
        raise HTTPException(
            409, detail={"code": "NO_RECOMMENDATION", "message": "편집할 기준 추천안이 없습니다 (먼저 recommend)."}
        )
    saved = _save_recommendation(
        session.id, base.analysis_id, payload.recommendation,
        source=payload.source, parent_version=payload.parent_version if payload.parent_version is not None else base.version,
        change_summary=payload.change_summary,
        request_id=_current_request_id(),
    )
    return saved


@router.get("/{session_id}/recommendations/{version}/export")
def export_recommendation(
    session_id: str,
    version: int,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_optional_user),
) -> JSONResponse:
    """확정된 추천안(흐름도)을 다운로드용 JSON으로 내보낸다 (FR-17).

    지정 버전의 Recommendation 페이로드를 메타 봉투에 담아 attachment로 반환한다.
    라우트는 4세그먼트라 /recommendations·/recommendations/latest와 충돌하지 않는다.
    """
    session = _owned_session_or_404(session_id, db, user)
    row = db.execute(
        select(models.RecommendationVersion).where(
            models.RecommendationVersion.session_id == session.id,
            models.RecommendationVersion.version == version,
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            404, detail={"code": "NO_RECOMMENDATION", "message": "해당 버전의 추천안이 없습니다."}
        )
    envelope = {
        "schema_version": "1.0",
        "session_id": str(session.id),
        "recommendation_version": row.version,
        "source": row.source,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "recommendation": row.payload,
    }
    filename = f"recommendation-{session.id}-v{row.version}.json"
    return JSONResponse(
        content=envelope,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ─────────────────────────────────────────────────────────────────────────────
# 에이전트 단일 진입점 — analyze/ask/흐름도 수정 통합 (RPA-64)
# intent는 두지 않는다: 에이전트 라우터가 message로 브랜치를 판단하므로, 백엔드는
# 어느 브랜치로 갈지 모른 채 매 턴 full context(solution/operation/history/compact/analysis/
# recommendation/parsed_doc)를 조립해 넘긴다. 에이전트는 stateless(DB 안 붙음)를 유지한다.
# 반환 type("answer"|"analysis"|"recommendation"|"compact")으로 백엔드가 저장을 분기한다.
# compact는 대화 압축(RPA-66): operation="compact" 버튼발 요청은 라우터를 우회해 압축 노드 직행.
# ─────────────────────────────────────────────────────────────────────────────

_VALID_TURN_TYPES = {"answer", "analysis", "recommendation", "compact"}  # 에이전트 결과 type 계약


def _get_agent_turn():
    """app/agent의 통합 진입점 stream_agent_turn을 lazy import한다 (미구현이면 None).

    기대 계약 (Agent 담당 구현):
        async def stream_agent_turn(message: str, context: dict) -> AsyncIterator[ProgressEvent]
      context = {solution, operation, history, compact, analysis, recommendation, parsed_doc}
        - operation: "chat" | "compact" (버튼발 결정론 신호 — compact면 LLM 라우터 우회, 압축 노드 직행)
        - compact: 이전 압축본(CompactContext) | None — 매 턴 프롬프트 주입 + 재압축 입력
        - history: 최신 압축 이후 대화 (그 이전은 compact가 대체)
      종료 done 이벤트의 data = {
          "type": "answer" | "analysis" | "recommendation" | "compact",
          "answer": str, "sources": [RagSource, ...],
          "analysis_result": {...} | None,          # type=="analysis"
          "updated_recommendation": {...} | None,   # type=="recommendation"
          "change_summary": str | None,             # type=="recommendation"
          "compact": {...} | None,                  # type=="compact" (고정 섹션 JSON)
      }
    agent가 stream_agent_turn을 export하는 순간 이 라우트는 코드 변경 없이 활성화된다.
    """
    try:
        from app.agent import stream_agent_turn  # noqa: PLC0415

        return stream_agent_turn
    except ImportError:
        return None


def _get_agent_versions():
    """app/agent의 버전 레지스트리를 lazy import한다 (미구현이면 None).

    기대 계약 (Agent 담당 구현, RPA-167):
        available_versions() -> [{"id": "v2", "label": "...", "description": "...", "default": True}, ...]
        default_version() -> str            # env AGENT_VERSION 반영
      버전 선택은 stream_agent_turn의 context에 "agent_version" 키로 전달한다
      (operation/compact와 같은 패턴 — 있으면 그 버전, 없으면 서버 기본).

    _get_agent_turn과 같은 lazy 스위치다 — agent가 export하는 순간 코드 변경 없이 활성화된다.
    ⚠️ 버전 목록을 여기서 하드코딩(Literal["v1","v2"] 등)하면 안 된다: v3는 app/agent/v3/ 폴더만
    생기고 registry가 자동 포함하므로, 동적으로 물어봐야 백엔드·프론트가 0 수정이 된다.
    """
    try:
        from app.agent import available_versions, default_version  # noqa: PLC0415

        return available_versions, default_version
    except ImportError:
        return None


class AgentTurnRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000, description="사용자 메시지 (버튼이면 프론트가 합성)")
    # compact/fill_cards 버튼발 결정론 요청 — LLM 라우터를 우회하는 명시 신호 (기본 "chat")
    operation: str = Field(
        "chat", pattern="^(chat|compact|fill_cards)$", description="chat | compact | fill_cards"
    )
    # fill_cards 전용 — {card_id: 값}. 에이전트(v3)가 targets 좌표로 결정론 적용한다.
    # v3 미지원 버전이 fill_cards를 받으면 에이전트가 미지 operation으로 안전 처리한다.
    card_values: dict[str, Any] | None = Field(
        None, description="operation=fill_cards일 때 질문 카드 응답 {card_id: value}"
    )
    agent_version: str | None = Field(
        None, description="에이전트 버전 (없으면 서버 기본). 유효값은 GET /api/agent/versions"
    )

    @field_validator("agent_version")
    @classmethod
    def _known_version(cls, v: str | None) -> str | None:
        """레지스트리에 실제로 있는 버전인지 동적으로 검증한다 (v3 추가 시 코드 무변경)."""
        if v is None:  # 서버 기본으로 처리 — 레지스트리가 없어도 기존 동작 그대로다
            return v
        registry = _get_agent_versions()
        if registry is None:
            raise ValueError("에이전트 버전 선택을 아직 쓸 수 없습니다 (버전 레지스트리 미배포)")
        available_versions, _ = registry
        if v not in {x["id"] for x in available_versions()}:
            raise ValueError(f"알 수 없는 에이전트 버전: {v}")
        return v


def _assemble_turn_context(
    session: models.AnalysisSession, db: Session, operation: str = "chat",
    agent_version: str | None = None,
) -> dict:
    """단일 턴에 필요한 full context를 세션에서 조립한다 (intent 없으니 재료를 다 준다).

    agent에 넘길 컨텍스트와, 백엔드 저장에 필요한 참조(analysis_id/document_id)를 함께 담아
    반환한다 — 요청 스코프 db가 살아있는 지금(스트리밍 시작 전) 모두 평문 값으로 스냅샷한다.
    최신 압축본(compact)을 주입하고, history는 그 압축 이후 대화만(절삭 없이) 넘긴다 —
    압축 이전은 compact가 대체하며, 게이지가 대화 누적을 충실히 반영하게 한다.
    """
    compact = db.execute(
        select(models.SessionCompact)
        .where(models.SessionCompact.session_id == session.id)
        .order_by(models.SessionCompact.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    # history는 절삭 없이 넘긴다 — 압축이 있으면 그 이후 대화만, 없으면 전체. 상한을 두면
    # (1) 첫 compact 때 오래된 대화가 SessionCompact에도 안 들어가 영구 유실되고(CodeRabbit),
    # (2) intake 토큰이 대화 누적을 충실히 반영 못 해 게이지가 왜곡된다. 폭주는 임계 초과
    # 자동 compact(안전망, 후속)로 막는다.
    hist_q = select(models.ChatMessage).where(models.ChatMessage.session_id == session.id)
    if compact is not None:
        hist_q = hist_q.where(models.ChatMessage.created_at > compact.created_at)  # 그 이전은 compact가 대체
    history_rows = db.execute(
        hist_q.order_by(models.ChatMessage.created_at.asc())
    ).scalars().all()
    history = [{"role": m.role, "content": m.content} for m in history_rows]

    analysis = db.execute(
        select(models.Analysis)
        .where(models.Analysis.session_id == session.id, models.Analysis.status == "completed")
        .order_by(models.Analysis.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    rec = db.execute(
        select(models.RecommendationVersion)
        .where(models.RecommendationVersion.session_id == session.id)
        .order_by(models.RecommendationVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()

    # 파싱 완료 문서만 — 최신 업로드가 uploaded/failed이면 parsed_doc가 비거나 분석이
    # 잘못된 document_id에 묶일 수 있으므로 status="parsed"로 거른다 (CodeRabbit).
    document = db.execute(
        select(models.Document)
        .where(models.Document.session_id == session.id, models.Document.status == "parsed")
        .order_by(models.Document.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    return {
        "agent_context": {
            "solution": session.solution,
            "operation": operation,
            # None이면 에이전트가 서버 기본(env AGENT_VERSION)으로 처리한다 (RPA-167 계약)
            "agent_version": agent_version,
            "history": history,
            "compact": compact.payload if compact else None,
            "analysis": analysis.result if analysis else None,
            "recommendation": rec.payload if rec else None,
            "parsed_doc": document.parsed_content if document else None,
        },
        # 저장용 참조 (agent엔 안 보냄)
        "document_id": document.id if document else None,
        # 추천 새 버전은 기준(base) 버전의 analysis_id를 잇고, 없으면 최신 분석의 id를 쓴다
        # (recommendations.analysis_id는 NOT NULL).
        "rec_analysis_id": (rec.analysis_id if rec else (analysis.id if analysis else None)),
    }


def _save_analysis(session_id: uuid.UUID, document_id: uuid.UUID, result: dict) -> uuid.UUID:
    """분석 결과를 Analysis 행으로 저장하고 새 id를 반환한다 (새 세션 — 스트리밍 후에도 안전)."""
    from app.db import SessionLocal

    with SessionLocal() as s:
        row = models.Analysis(
            session_id=session_id, document_id=document_id, status="completed", result=result
        )
        s.add(row)
        s.commit()
        return row.id


# compact 고정 섹션 계약(RPA-66) — 저장 경계에서 강제한다. 값 타입까지 확인해
# dict 아닌 payload·빈 dict·섹션 누락이 session_compacts에 새는 것을 막는다.
_COMPACT_SECTIONS = {
    "task_overview": str,
    "decisions": list,
    "flow_journal": list,
    "open_questions": list,
    "verbatim": list,
}


def _validate_compact_payload(cp) -> None:
    """compact payload가 고정 섹션 JSON 계약을 지키는지 검증한다 (위반 시 ValueError → SSE error)."""
    if not isinstance(cp, dict):
        raise ValueError("compact payload가 객체(dict)가 아닙니다")
    missing = [k for k in _COMPACT_SECTIONS if k not in cp]
    if missing:
        raise ValueError(f"compact 필수 섹션 누락: {', '.join(missing)}")
    for key, expected in _COMPACT_SECTIONS.items():
        if not isinstance(cp[key], expected):
            raise ValueError(f"compact 섹션 타입 오류: {key}는 {expected.__name__}이어야 합니다")
    # verbatim은 유실-critical(카탈로그 원문) — 항목이 {kind, content} 형태인지까지 확인.
    # (빈 리스트/빈 문자열 자체는 정상이라 강제하지 않는다; 내용 완결성은 에이전트 union 보정 몫)
    for item in cp["verbatim"]:
        if not (isinstance(item, dict) and "kind" in item and "content" in item):
            raise ValueError("compact verbatim 항목은 {kind, content} 객체여야 합니다")


def _save_compact(session_id: uuid.UUID, payload: dict) -> str:
    """대화 압축본을 session_compacts에 저장한다 (append-only — 최신 행이 다음 턴에 주입된다)."""
    from app.db import SessionLocal

    with SessionLocal() as s:
        row = models.SessionCompact(
            session_id=session_id,
            schema_version=str(payload.get("schema_version", "1.0")),
            payload=payload,
        )
        s.add(row)
        s.commit()
        return str(row.id)


def _persist_chat_turn(
    session_id: uuid.UUID, user_msg: str, assistant_msg: str,
    recommendation_version: int | None = None,
) -> None:
    """이번 턴을 chat_messages에 저장한다 (best-effort — 실패해도 응답을 막지 않는다).

    이 턴이 추천 버전을 만들었으면 assistant 메시지에 그 버전 번호를 남긴다
    (ChatMessage.recommendation_version 계약 — 어느 대화가 어느 버전을 만들었는지).
    """
    try:
        from app.db import SessionLocal

        with SessionLocal() as s:
            s.add(models.ChatMessage(session_id=session_id, role="user", content=user_msg))
            s.add(models.ChatMessage(
                session_id=session_id, role="assistant", content=assistant_msg,
                recommendation_version=recommendation_version,
            ))
            s.commit()
    except Exception:  # noqa: BLE001 — 대화 저장 실패가 응답을 막으면 안 됨
        logger.warning("대화 턴 저장 실패 (무시): session=%s", session_id, exc_info=True)


def _persist_turn_result(
    session_id: uuid.UUID, rec_analysis_id: uuid.UUID | None, document_id: uuid.UUID | None,
    user_message: str, result: dict,
) -> dict:
    """반환 결과를 저장하고, 프론트에 줄 최종 done.data를 만든다.

    산출물(analysis_result/updated_recommendation)은 **type과 무관하게 non-null이면 모두 저장**한다:
    "분석 없이 바로 흐름도" 턴은 type="recommendation"이지만 분석을 선행 수행해 analysis_result도
    함께 오고, 추천 버전은 그 분석본에 **버전 단위 FK(analysis_id)로 귀속**(provenance)되므로 분석이
    저장 안 되면 그 FK가 끊긴다. 이때 흐름도는 이번 턴에 새로 저장한 분석의 id에 귀속시켜 무결성을
    지킨다. (흐름도 step_id는 이제 분석을 참조하지 않는 지역 id다 — 귀속은 오직 버전 단위.)
    계약 위반(알 수 없는 type, 선언한 산출물 누락, 저장할 문서 없음)은 성공 done으로 내보내지
    않고 ValueError로 올려 상위 SSE 핸들러가 error로 매핑한다 (CodeRabbit). 주 산출물 저장 실패도
    상위로 전파하고, 대화 기록만 best-effort로 삼킨다.
    """
    rtype = result.get("type")
    if rtype not in _VALID_TURN_TYPES:
        raise ValueError(f"에이전트 결과 type이 올바르지 않습니다: {rtype!r}")

    answer = result.get("answer") or ""

    # compact — 압축본을 저장하고 여기서 끝낸다. 압축은 이력을 대체하므로 이 턴 자체는
    # chat_messages에 남기지 않는다 (압축 이후 이력을 다시 늘리지 않도록).
    if rtype == "compact":
        cp = result.get("compact")
        _validate_compact_payload(cp)  # dict + 고정 섹션 계약을 저장 경계에서 강제
        _save_compact(session_id, cp)
        return {
            "type": "compact", "answer": answer,
            "sources": result.get("sources") or [], "session_id": str(session_id),
            "compact": cp,
        }

    ar = result.get("analysis_result")
    rec = result.get("updated_recommendation")

    # 선언한 type의 산출물이 실제로 있어야 한다 (없으면 성공 done 대신 error)
    if rtype == "analysis" and ar is None:
        raise ValueError("type=analysis인데 analysis_result가 없습니다")
    if rtype == "recommendation" and rec is None:
        raise ValueError("type=recommendation인데 updated_recommendation이 없습니다")

    out = {
        "type": rtype,
        "answer": answer,
        "sources": result.get("sources") or [],
        "session_id": str(session_id),
    }

    # 분석본 — non-null이면 저장 (type 무관). 저장할 파싱 완료 문서가 없으면 계약 위반.
    new_analysis_id = None
    if ar is not None:
        if document_id is None:
            raise ValueError("분석 결과를 저장할 파싱 완료 문서가 없습니다")
        new_analysis_id = _save_analysis(session_id, document_id, ar)
        out["analysis_id"] = str(new_analysis_id)
        out["analysis_result"] = ar

    # 흐름도 — non-null이면 새 버전 저장 (type 무관). 이번 턴에 분석을 새로 냈으면 그 id에
    # 귀속(참조 무결성), 아니면 기존 base/최신 분석 id를 잇는다. 귀속할 분석이 없으면 계약 위반.
    saved_version = None
    if rec is not None:
        analysis_id = new_analysis_id or rec_analysis_id
        if analysis_id is None:
            raise ValueError("흐름도를 귀속할 분석이 없습니다 (분석 선행 필요)")
        saved = _save_recommendation(
            session_id, analysis_id, rec, source="chat",
            parent_version=None, change_summary=result.get("change_summary"),
            request_id=result.get("_backend_request_id"),
            requested_agent_version=result.get("_backend_requested_agent_version"),
            resolved_agent_version=result.get("resolved_agent_version"),
            agent_registry_snapshot=_agent_registry_snapshot(),
            producer_advisory=result.get("violations"),
        )
        out.update(saved)  # id, version, parent_version, source, change_summary, created_at
        out["recommendation"] = rec
        saved_version = saved["version"]

    _persist_chat_turn(session_id, user_message, answer, recommendation_version=saved_version)
    return out


def _save_turn_events(session_id: uuid.UUID, request_id: str | None, rows: list[dict]) -> None:
    """턴 하나의 노드 타임라인을 관측 DB에 일괄 적재한다 (RPA-105, best-effort).

    스트리밍 중 매 이벤트마다 DB를 치지 않고 버퍼를 턴 종료 시 한 번에 쓴다 —
    관측이 스트림 지연에 얹히지 않게. 실패는 경고만(턴 응답에 영향 없음).
    """
    if not rows:
        return
    try:
        from app.core.observability_db import observability_sessionmaker

        with observability_sessionmaker()() as s:
            for r in rows:
                s.add(models.TurnEvent(session_id=session_id, request_id=request_id, **r))
            s.commit()
    except Exception:  # noqa: BLE001
        logger.warning("turn_events 적재 실패 (턴은 정상): session=%s", session_id, exc_info=True)


# 대화 누적 게이지 하드 임계(토큰) — 실측 기반 (RPA-172, 2026-07-15).
#
# 이전 기본값 100000은 **하드 도달에 ~527턴이 필요해 자동 compact가 절대 발동하지 않았다**
# (RPA-89가 진단해 .env.example에 경고까지 적어뒀으나 값은 그대로였다). 실측(관측 DB 6,237행):
#   - 대화 누적 Δ ≈ 188 토큰/턴, 첫 턴 base ≈ 582 토큰
#   - 세션당 턴 수: 중앙 1턴 / p90 3턴 / 최대 7턴
#
# 6000을 고른 이유 — 두 제약이 아래에서 위로, 위에서 아래로 조인다:
#   ① 아래 경계(~5,100 이상이어야): 히스토리가 거의 없는 초반 턴에 큰 입력 하나가 들어와도
#      오발동하지 않아야 한다 — 압축할 history가 없는데 compact 비용($0.00843/콜)만 나간다.
#      ⚠️ 이 경계를 **문자 수로 잡으면 안 된다**. `AgentTurnRequest.message`의 max_length=4000은
#      **문자** 상한이고, 게이지가 세는 건 **토큰**이다. cl100k_base 실측 tok/char:
#        영문 0.12 / 한글 산문 1.08 / 태국어 1.0 / 희귀 CJK 2.0 / 이모지 3.0
#      즉 4,000자짜리 스키마 유효 입력의 토큰 수는 **500 ~ 12,000**으로 24배 벌어진다.
#      현실 입력(한국어 산문)의 최악은 4,000자 × 1.08 ≈ **4,309토큰**이고, base(582)를 더하면
#      **4,891**이다. 6000은 이 위라 여유 1,109를 남긴다 — 현실 입력으로는 초반 오발동이 없다.
#      (이모지 도배 같은 병리적 입력은 12,582까지 가능해 이 경계를 넘지만, 그건 **선행 가드가
#       잡는 게 맞는** 진짜 초대형 입력이다 — _needs_auto_compact 참고.)
#      ⚠️ base는 리포트가 계산하는 **첫 턴 intake 중앙값(582)**이다. 한때 여기 '~772'라고 적었는데
#         출처 없는 숫자였다(리포트·테스트 어디에도 없음). 근거 있는 값만 쓸 것.
#   ② 위 경계(작을수록 발동): 6000이면 짧은 메시지 기준 하드가 29턴째(경고 25턴째). 100000의
#      527턴에서 18배 당겨져 실사용 긴 대화에서 실제로 동작하고, 게이지 ratio도 눈에 보이게 움직인다.
#      ⚠️ "29턴"은 Δ=188(짧은 메시지) 가정이다. **긴 메시지를 쓰면 history가 턴당 ~2,000씩 뛰어
#      4턴이면 발동한다**(live 실측). 즉 발동 시점은 턴 수가 아니라 **입력 길이**가 지배한다.
#   ③ 경제성: compact는 콜당 $0.00843인데, LIMIT=6000에서 압축하면 ~5,418토큰/턴을 아껴
#      **2턴이면 본전**이다. 낮출수록 나빠진다(3000=5턴, 2000=8턴) — 일찍 터질수록 압축할 게 적다.
#
# ⚠️ 관측 표본은 대부분 테스트/시뮬 트래픽(최대 7턴)이라 이 값으로도 관측 트래픽에선 안 터진다.
#    실사용/데모 대화가 쌓이면 scripts/gauge_calibration_report.py로 재확인할 것.
_GAUGE_LIMIT_DEFAULT = 6000

# compact 권장(소프트) 임계 비율 기본값 — LIMIT=6000에서 "하드 4턴 전 경고"가 되는 값.
# 계산: 1 − 4×Δ/LIMIT = 1 − 4×188/6000 ≈ 0.87 (RPA-89 리포트의 headroom 공식).
# 이전 0.7은 LIMIT=6000 기준 하드 10턴 전에 경고해 과잉 알림이 된다.
_GAUGE_WARN_RATIO_DEFAULT = 0.87


def _gauge_warn_ratio() -> float:
    """compact 권장(소프트) 임계 비율. env로 조정, 비정상값은 기본값 (RPA-89).

    RPA-89 캘리브레이션 리포트가 실데이터에서 권장값을 산출하면 이 env로 적용한다
    (코드 수정 없이 사람이 승인해 갱신 — 통제형 거버넌스).
    """
    try:
        v = float(os.getenv("TURN_GAUGE_WARN_RATIO", str(_GAUGE_WARN_RATIO_DEFAULT)))
    except ValueError:
        return _GAUGE_WARN_RATIO_DEFAULT
    return v if 0 < v <= 1 else _GAUGE_WARN_RATIO_DEFAULT


def _read_intake_gauge(session_id: uuid.UUID) -> dict | None:
    """이번 세션 최신 intake 호출의 prompt_tokens로 대화 누적 게이지를 만든다.

    정준환의 intake 태깅(purpose="intake", RPA-73) 기준 — 이 호출엔 history+compact가 절삭
    없이 실리므로 input_tokens가 "대화 누적분"의 충실한 대리값이다. 임계는 env로 조정한다.
    llm_usage는 관측 DB(RPA-90)에 쌓이므로 같은 곳에서 읽는다 (미설정 시 앱 DB 폴백).
    """
    from app.core.observability_db import observability_sessionmaker

    with observability_sessionmaker()() as s:
        tokens = s.execute(
            select(models.LlmUsage.input_tokens)
            .where(
                models.LlmUsage.session_id == session_id,
                models.LlmUsage.purpose == "intake",
            )
            .order_by(models.LlmUsage.id.desc())
            .limit(1)
        ).scalar_one_or_none()
    if tokens is None:
        return None
    # env가 비정상(non-numeric)·0·음수면 기본값으로 폴백 — 게이지를 통째로 끄거나
    # ZeroDivision을 내지 않고 항상 의미 있는 값을 낸다.
    try:
        limit = int(os.getenv("TURN_GAUGE_LIMIT_TOKENS", str(_GAUGE_LIMIT_DEFAULT)))
    except ValueError:
        limit = 0
    if limit <= 0:
        limit = _GAUGE_LIMIT_DEFAULT
    ratio = round(tokens / limit, 4)
    return {
        "intake_tokens": int(tokens),
        "limit_tokens": limit,
        "ratio": ratio,
        "compact_recommended": bool(ratio >= _gauge_warn_ratio()),  # 소프트: 프론트가 압축 유도
        "compact_required": bool(ratio >= _gauge_hard_ratio()),   # 하드: 백엔드가 자동 compact
    }


def _gauge_hard_ratio() -> float:
    """자동 compact 하드 임계 비율 — 기본 1.0(=LIMIT 도달). env로 조정, 비정상값은 1.0."""
    try:
        v = float(os.getenv("TURN_GAUGE_HARD_RATIO", "1.0"))
    except ValueError:
        v = 1.0
    return v if v > 0 else 1.0


# tiktoken 인코더 — 요청 경로에서 로드하지 않는다 (CodeRabbit #134).
# get_encoding()은 캐시 미준비 시 원격 BPE 다운로드를 하므로 async 라우트 안에서 부르면
# 이벤트 루프를 막을 수 있고, lru_cache로 감싸면 일시 실패(None)까지 영구 고정된다.
# → 앱 시작 시(lifespan) 백그라운드 스레드로 한 번 워밍업하고, 요청은 준비된 것만 쓴다.
_TOKEN_ENCODER = None


def warmup_token_encoder() -> None:
    """tiktoken 인코더를 미리 로드한다 — lifespan이 백그라운드 스레드로 호출.

    실패(미설치·오프라인 등)해도 앱은 계속 뜨고, 추정은 문자 폴백(len)으로 동작한다.
    """
    global _TOKEN_ENCODER
    try:
        import tiktoken

        _TOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
    except Exception:  # noqa: BLE001 — tiktoken 미설치/BPE 다운로드 실패 등
        logger.warning("tiktoken 인코더 로드 실패 — 문자 기반 폴백으로 동작", exc_info=True)


def _token_encoder():
    """준비된 인코더를 돌려준다 (워밍업 전/실패면 None → 호출부가 문자 폴백)."""
    return _TOKEN_ENCODER


def _estimate_message_tokens(text: str) -> int:
    """이번 입력 message의 토큰 수 추정 — 선행 compact 판단용 (RPA-86).

    tiktoken이 있으면 실제 인코딩으로 세고(정확), 없으면 **UTF-8 바이트 수**로 폴백한다.

    안전망이므로 폴백은 **과소추정하면 안 된다**(스파이크를 놓쳐 가드가 뚫린다). 바이트 수는
    그 조건을 **증명 가능하게** 만족한다: cl100k_base는 byte-level BPE라 모든 토큰이 최소
    1바이트를 소비하므로 항상 `tokens ≤ bytes`다. 실측으로도 전 구간에서 성립한다(4,000자 기준):

        영문 4,000B/500tok(8.0x) · 한글 9,540B/4,309tok(2.2x) · 태국어 12,000B/4,000tok(3.0x)
        희귀CJK 12,000B/8,000tok(1.5x) · **이모지 16,000B/12,000tok(1.3x)**

    가장 위험한 입력(이모지)에서 여유가 가장 타이트해 상한으로서 낭비가 적다.

    ⚠️ 한때 `len(text)`(문자 수)를 썼다 — **상한이 아니다.** 이모지는 3.0, 희귀 CJK는 2.0
       tok/char라 2~3배 **과소**추정했고, 스키마를 통과하는 이모지 4,000자(12,000토큰)를 4,000
       으로 세서 선행 가드를 그냥 통과시켰다(RPA-172). 문자 수는 토큰 상한을 주지 않는다.

    트레이드오프: 폴백은 과대추정 쪽이라(한글 2.2x) 긴 한국어 입력에 compact를 과발동시킬 수
    있다. 폴백은 tiktoken 로드 실패(미설치·오프라인) 시에만 도는 **저하 모드**이고, 그 상태에선
    비용($0.00843/콜) < 컨텍스트 초과 위험이라 이 방향을 택했다.
    """
    if not text:
        return 0
    enc = _token_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:  # noqa: BLE001 — 인코딩 실패 시 바이트 폴백
            pass
    return max(1, len(text.encode("utf-8")))


def _needs_auto_compact(gauge: dict, message: str) -> bool:
    """자동 compact가 필요한가 — 후행 게이지 + 이번 입력 선행(look-ahead) 반영 (RPA-84/86).

    게이지의 compact_required는 '직전 intake 기준'이라 지금 들어온 대형 입력을 못 본다.
    이번 message의 토큰 추정치를 더한 '예상 비율'이 하드 임계를 넘으면 미리 압축한다.

    이 함수가 잡는 시나리오는 둘이다:
      ⓐ **누적 history + 큰 입력** — 실사용에서 압도적으로 흔한 쪽. live 실측: 4턴째 발동.
      ⓑ **초대형 단일 입력** — RPA-86의 원래 문구. 스키마(max_length=4000)는 **문자**만 막고
         토큰은 안 막으므로 여전히 가능하다: 이모지는 cl100k_base에서 3 tok/char라 4,000자짜리
         **스키마 유효** 입력이 12,000토큰(LIMIT의 2배)이 된다.

    ⚠️ ⓑ를 "스키마상 불가능"이라고 적었던 적이 있다(RPA-172). 오진이었다 — max_length를 토큰
       상한으로 착각한 것이다(문자↔토큰 혼동). RPA-86 원문이 옳았다. 문자 상한은 토큰 상한을
       주지 않는다는 걸 여기 남겨둔다: 아래 경계를 문자 수로 재계산하려는 다음 사람을 위해.

    LIMIT을 현실 입력의 최악(base 582 + 한글 4,000자 ≈ 4,309 = **4,891**) 아래로 낮추면 평범한
    긴 한국어 입력만으로 초반 오발동이 생긴다 — 위 _GAUGE_LIMIT_DEFAULT의 '아래 경계'가 그 이유다.

    또 하나: 게이지는 **그 세션의 직전 intake**를 읽으므로 **1턴째엔 gauge=None이라 호출 자체가
    안 된다**(호출부에서 `if gauge and ...`). 세션 첫 입력은 아무리 커도 자동 compact가 안 걸린다.
    """
    if gauge.get("compact_required"):
        return True
    limit = gauge.get("limit_tokens") or 0
    if limit <= 0:
        return False
    projected = (gauge.get("intake_tokens", 0) + _estimate_message_tokens(message)) / limit
    return projected >= _gauge_hard_ratio()


async def _run_internal_compact(session_id: uuid.UUID, message: str, stream_turn, compact_ctx: dict) -> bool:
    """대화 누적이 하드 임계를 넘었을 때 실제 턴 전에 자동으로 한 번 압축한다 (RPA-84).

    compact 결과를 session_compacts에 저장해, 이어지는 실제 턴의 컨텍스트가 새 압축본을 집게 한다.
    best-effort — 압축이 실패하면 False를 돌려 원본 이력 그대로 진행한다(사용자 턴을 막지 않음).
    """
    result = None
    async for ev in stream_turn(message, compact_ctx):
        if ev.event == "done":
            result = ev.data or {}
        elif ev.event == "error":
            return False
    if not (result and result.get("type") == "compact" and result.get("compact")):
        return False
    try:
        _validate_compact_payload(result["compact"])
        _save_compact(session_id, result["compact"])
        return True
    except Exception:  # noqa: BLE001 — 자동 압축 저장 실패가 사용자 턴을 막지 않게
        logger.warning("자동 compact 저장 실패 (무시): session=%s", session_id, exc_info=True)
        return False


# SSE heartbeat (RPA-233) — 에이전트가 다음 이벤트를 내기까지 조용한 구간이 길면(LLM 긴 생성 등)
# CloudFront OriginReadTimeout(60초)에 연결이 끊긴다. 침묵이 이어지면 주석 프레임을 흘려 연결을
# 살려둔다. 60초보다 넉넉히 짧게 둬 idle마다 최소 한 번은 반드시 들어가게 한다.
_SSE_HEARTBEAT_SEC = 15.0
_SSE_HEARTBEAT_FRAME = ": keepalive\n\n"  # SSE 주석 — EventSource는 무시하고 연결만 유지한다
_HEARTBEAT = object()  # 침묵 구간 신호 (실제 이벤트와 구분되는 sentinel)

_TURN_MAX_DEFAULT_SEC = 900.0


def _turn_max_sec() -> float:
    """Registry 값을 요청 시점에 읽고 비정상 상한은 안전한 기본값으로 복구한다."""
    try:
        value = float(config.TURN_MAX_DURATION_SEC)
    except (TypeError, ValueError):
        logger.warning("TURN_MAX_DURATION_SEC가 숫자가 아니어서 기본값 %.0f초를 사용합니다", _TURN_MAX_DEFAULT_SEC)
        return _TURN_MAX_DEFAULT_SEC
    if not math.isfinite(value) or value < 0:
        logger.warning("TURN_MAX_DURATION_SEC가 유효 범위를 벗어나 기본값 %.0f초를 사용합니다", _TURN_MAX_DEFAULT_SEC)
        return _TURN_MAX_DEFAULT_SEC
    return value


class _TurnTimeout(Exception):
    """전체 턴 상한 초과 — hung 턴을 error로 끊는다 (RPA-235)."""


async def _iter_with_heartbeat(agen, interval: float, max_total: float | None = None):
    """`agen`의 이벤트를 흘리되, `interval`초 이상 조용하면 `_HEARTBEAT` sentinel을 낸다.
    `max_total`초를 넘도록 진행이 전혀 없으면 `_TurnTimeout`을 올린다 (RPA-235, hung 턴 방어).

    다음 이벤트를 기다리는 `__anext__`를 `asyncio.shield`로 감싸 타임아웃이 하위 async
    제너레이터를 취소·훼손하지 않게 한다 — 취소하면 진행 중이던 LLM 호출·상태가 깨진다.
    타임아웃은 shield 래퍼만 취소하고 실제 대기는 다음 반복에서 그대로 이어진다.
    """
    it = agen.__aiter__()
    loop = asyncio.get_running_loop()
    deadline = None if not max_total or max_total <= 0 else loop.time() + max_total

    async def _next():
        # StopAsyncIteration을 태스크 경계로 넘기지 않는다 — __anext__를 직접 태스크화하면
        # 종료 신호 전파가 깨져(무한 대기) heartbeat만 영원히 나간다. 코루틴 안에서 잡아
        # (값, 종료여부) 튜플로 바꾼다.
        try:
            return await it.__anext__(), False
        except StopAsyncIteration:
            return None, True

    pending: asyncio.Future | None = None
    try:
        while True:
            wait_timeout = interval
            if deadline is not None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise _TurnTimeout
                wait_timeout = min(interval, remaining)
            if pending is None:
                pending = asyncio.ensure_future(_next())
            try:
                event, done = await asyncio.wait_for(asyncio.shield(pending), wait_timeout)
            except asyncio.TimeoutError:
                # 전체 상한을 넘도록 진행이 없으면 hung으로 보고 끊는다 (RPA-235) — 그냥 두면
                # heartbeat만 무한히 나가며 요청 세션·워커를 영영 붙잡는다. finally가 하위를 정리한다.
                if deadline is not None and loop.time() >= deadline:
                    raise _TurnTimeout
                yield _HEARTBEAT  # 아직 다음 이벤트 없음 — 연결만 살린다
                continue
            pending = None
            if done:
                return
            yield event
            if getattr(event, "event", None) == "done":
                return
    finally:
        # 조기 종료(클라 끊김 등)면 진행 중이던 __anext__를 정리한다 — 진행 중 호출 1건만 취소.
        # 우리가 유발한 CancelledError만 삼킨다 — BaseException을 통째로 삼키면 바깥 취소까지
        # 먹어 asyncio 협조적 취소가 깨진다(RPA-233 Qodo).
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending
        # 하위 제너레이터도 명시적으로 닫는다 — __anext__를 수동 구동해서 wrapper의 GeneratorExit이
        # it로 자동 전파되지 않는다. pending을 먼저 취소했으니 "generator already running" 없이
        # 하위(graph.astream)의 finally 정리가 확실히 돈다. 정리 중 하위 예외는 삼키되,
        # CancelledError(바깥 취소)는 전파한다 — 취소가 최우선이다(RPA-233 Qodo).
        aclose = getattr(it, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):  # noqa: BLE001 — 정리 예외만, 취소는 전파
                await aclose()


@router.post("/{session_id}/turn")
async def agent_turn(
    session_id: str,
    payload: AgentTurnRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_optional_user),
) -> StreamingResponse:
    """에이전트 단일 진입점 — 한 메시지로 분석/질문/흐름도 수정을 모두 처리한다 (SSE).

    백엔드가 세션에서 full context를 조립해 에이전트에 넘기고, 에이전트가 solution으로
    그래프를 골라 intent(analyze/edit/ask)를 판단한다. 반환 type으로 저장을 분기한다.
    대화 누적이 하드 임계를 넘으면(chat 턴) 실제 턴 전에 자동 compact를 먼저 돌린다(RPA-84).
    이벤트: [자동압축 stage] → (에이전트가 흘리는) stage/partial/token → done(data={type, ...}).
    """
    session = _owned_session_or_404(session_id, db, user)
    stream_turn = _get_agent_turn()
    if stream_turn is None:
        # 스트림 열기 전 판정 — 프론트가 일반 HTTP 에러로 잡아 폴백 분기하기 쉽게
        raise HTTPException(
            503, detail={"code": "AGENT_UNAVAILABLE", "message": "에이전트가 아직 준비되지 않았습니다."}
        )

    # 예산 가드레일 (RPA-171) — 스트림 열기 전에 판정한다. 턴 중간에 끊으면 부분 상태가 남고,
    # SSE는 이미 200으로 열린 뒤라 프론트가 HTTP 에러로 잡지 못한다(위 503과 같은 이유).
    # 상한 미설정이면 no-op이라 기존 배포의 동작은 그대로다.
    verdict = await run_in_threadpool(
        budget.check_budget, budget.subject_of(user, session.id)
    )
    if verdict.exceeded:
        logger.warning(
            "예산 초과로 턴 차단 — scope=%s period=%s session=%s",
            verdict.scope, verdict.period, session.id,
        )
        # 사람에게 알린다 (RPA-189) — 여기가 enforce가 실제로 사용자를 막는 순간이다.
        # 로그만 남기면 아무도 안 본다: 예산을 넘겨 서비스를 끊었는데 대시보드를 열어봐야만 알았다.
        #
        # ⚠️ 요청 경로다. notify()는 슬랙 POST(최대 5초)를 하므로 run_in_threadpool로 이벤트
        #    루프를 막지 않는다(check_budget과 같은 패턴). 이 턴의 429 응답은 그만큼 늦어질 수
        #    있으나, 쿨다운(기본 60분) 때문에 실제 발송은 드물고 이미 차단된 요청이다.
        #
        # ⚠️ try가 **빌더까지** 감싼다. notify()는 자체 fail-open이지만 budget_exceeded()는
        #    그 바깥이라, 거기서 예외가 나면 429가 500으로 바뀐다 — 알림이 본체를 죽인다.
        #    "관측 실패는 서비스를 죽이지 않는다"는 이 블록 전체에 적용돼야 한다.
        try:
            subject = budget.subject_of(user, session.id)
            await run_in_threadpool(
                alerts.notify, alerts.budget_exceeded(verdict, f"{subject.kind}:{subject.id}")
            )
        except Exception:  # noqa: BLE001 — 알림 실패가 429를 500으로 바꾸면 안 된다
            logger.warning("예산 초과 알림 실패 (차단은 정상 진행)", exc_info=True)
        raise HTTPException(429, detail=budget.exceeded_detail(verdict))

    session_key = session.id
    user_id, message = (user.id if user else None), payload.message

    # 하드 자동 compact: chat 턴이고 대화 누적이 임계를 넘으면 실제 턴 전에 먼저 압축한다.
    # 요청 db가 살아있는 지금(제너레이터 전) 처리하고, 이후 컨텍스트 조립이 새 compact를 집는다.
    # 게이지(후행)는 직전 intake 기준이라 이번 대형 입력을 못 보므로, _needs_auto_compact가
    # 이번 message 토큰을 더한 선행(look-ahead) 판단까지 겸한다 (RPA-86).
    auto_compacted = False
    if payload.operation == "chat":
        try:
            gauge = _read_intake_gauge(session_key)
        except Exception:  # noqa: BLE001 — 게이지 조회 실패는 자동 압축을 건너뛴다
            gauge = None
        if gauge and _needs_auto_compact(gauge, message):
            # agent_version을 여기도 넘긴다 — 자동 압축이 사용자가 고른 버전과 다른 버전으로
            # 돌면 버전 혼선이 생긴다 (RPA-167 요청서 명시 회귀 주의사항).
            compact_ctx = _assemble_turn_context(
                session, db, operation="compact", agent_version=payload.agent_version
            )["agent_context"]
            try:
                with usage_context(
                    component="agent", actor_type="user", user_id=user_id, session_id=session_key
                ):
                    auto_compacted = await _run_internal_compact(
                        session_key, message, stream_turn, compact_ctx
                    )
            except Exception:  # noqa: BLE001 — 자동 압축 실패가 사용자 턴을 막지 않게
                logger.warning("자동 compact 실패 (무시): session=%s", session_key, exc_info=True)

    ctx = _assemble_turn_context(
        session, db, operation=payload.operation, agent_version=payload.agent_version
    )
    agent_context = ctx["agent_context"]
    if payload.operation == "fill_cards":
        # 카드 응답 원본을 그대로 넘긴다 — 적용은 에이전트(v3 cards.apply_card_values)가
        # targets 좌표로 결정론 수행한다 (백엔드는 흐름도 구조를 해석하지 않는다).
        agent_context["card_values"] = payload.card_values or {}
    rec_analysis_id, document_id = ctx["rec_analysis_id"], ctx["document_id"]

    # 턴 노드 타임라인 관측(RPA-105) — 스트림을 지나는 stage/error/done을 버퍼링해 턴
    # 종료 시 일괄 적재. request_id는 미들웨어가 심은 ContextVar에서 (같은 요청 묶음 키).
    from app.rag.observability import get_request_id

    turn_request_id = get_request_id()

    async def sse():
        result_data = None
        saw_error = False
        disconnected = False
        turn_max_sec = _turn_max_sec()
        turn_t0 = time.perf_counter()
        tev: list[dict] = []

        def _tev(kind: str, stage: str | None = None, message: str | None = None, data: dict | None = None):
            # detail은 항상 "유효한 JSON"이어야 한다 — 문자열을 그대로 자르면 중간에서 끊겨
            # 파싱 불가 JSON이 저장된다(CodeRabbit #164). 과대 payload는 preview 마커 객체로
            # 대체해 잘라도 유효 JSON을 유지한다.
            detail = None
            if data:
                # 사용자 업무 텍스트가 섞일 수 있는 자유 텍스트 키만 PII 마스킹(RPA-123) —
                # reason(라우팅 근거)·query(검색어). step_id·route 등 구조값은 그대로.
                detail = json.dumps(mask_fields(data, ("reason", "query")), ensure_ascii=False, default=str)
                if len(detail) > 4000:
                    detail = json.dumps(
                        {"_truncated": True, "size": len(detail), "preview": detail[:2000]},
                        ensure_ascii=False,
                    )
            tev.append({
                "seq": len(tev), "kind": kind, "stage": stage,
                "message": mask_pii((message or "")[:512]) or None,
                "detail": detail,
                "elapsed_ms": int((time.perf_counter() - turn_t0) * 1000),
            })

        try:
            if auto_compacted:
                _tev("stage", "compacting", "대화가 길어 자동 압축했습니다")
                yield ProgressEvent(
                    event="stage", stage="compacting", message="대화가 길어 자동 압축했습니다"
                ).to_sse()
            # async 제너레이터라 usage_context가 yield를 넘어도 안전 (같은 태스크 컨텍스트).
            with usage_context(
                component="agent", actor_type="user", user_id=user_id, session_id=session_key
            ):
                async for event in _iter_with_heartbeat(
                    stream_turn(message, agent_context), _SSE_HEARTBEAT_SEC, turn_max_sec
                ):
                    # 조용한 구간엔 heartbeat만 흘려 연결을 살린다 (RPA-233, CloudFront 60초 idle).
                    # 끊긴 클라이언트엔 보내지 않고 즉시 턴을 중단한다 — 계속 소비하면 비용만 나간다.
                    if event is _HEARTBEAT:
                        if await request.is_disconnected():
                            logger.info("클라이언트 끊김 — 턴 중단(heartbeat): session=%s", session_key)
                            _tev("error", "agent", "클라이언트 연결 끊김 — 턴 중단")
                            disconnected = True
                            break
                        yield _SSE_HEARTBEAT_FRAME
                        continue
                    # done은 끊김 체크보다 먼저 잡는다 (CodeRabbit #164) — 같은 반복에서
                    # 끊김이 감지되면 이미 완료된 작업(전체 LLM 비용 지출됨)이 저장 없이
                    # 버려지는 누락이 생긴다. done을 먼저 확보하면 그 케이스가 "done 후
                    # 끊김 = 저장 + 전송만 생략" 경로로 안전하게 합류한다.
                    if event.event == "done":
                        result_data = event.data or {}
                        continue  # done은 저장 후 최종 메타와 함께 다시 낸다
                    # 클라이언트 끊김 체크 (RPA-106) — 탭 닫기·네트워크 단절 후에도 에이전트
                    # 스트림을 계속 소비하면 이후 노드 LLM 호출이 허공에 계속 나간다(비용 낭비).
                    # 이터레이션을 멈추면 astream이 더 진행하지 않으므로 진행 중이던 호출 1건만
                    # 완료되고 끝. BaseHTTPMiddleware 조합에선 Starlette 자동 취소가 보장되지
                    # 않아 이벤트 경계마다 명시적으로 확인한다.
                    # done 전 끊김 → 미완성 턴 폐기(저장 안 함). done 후 끊김 → 저장 + 전송만 생략.
                    if await request.is_disconnected():
                        logger.info("클라이언트 끊김 — 턴 중단: session=%s", session_key)
                        _tev("error", "agent", "클라이언트 연결 끊김 — 턴 중단")
                        disconnected = True
                        break
                    if event.event == "error":
                        saw_error = True  # 에이전트가 이미 error를 흘림 — 중복 error 안 냄
                    if event.event in ("stage", "error"):  # token/partial은 볼륨 때문에 제외
                        _tev(event.event, event.stage, event.message, event.data)
                    yield event.to_sse()
            # done이 마지막 이벤트면 루프가 끊김 체크 없이 끝난다(선처리 continue) — 최종
            # 전송 전에 한 번 더 확인해, 죽은 클라이언트로의 yield(전송 예외 → turn_events
            # 적재 누락)를 막는다 (CodeRabbit #165). 저장은 그대로 진행된다.
            if not disconnected and await request.is_disconnected():
                logger.info("클라이언트 끊김 — 응답 전송 생략: session=%s", session_key)
                _tev("error", "agent", "클라이언트 연결 끊김 — 전송 생략")
                disconnected = True
            if result_data is not None:
                persistence_result = dict(result_data)
                # Backend가 관측한 값으로 덮어써 Agent payload가 provenance를 자기신고하지 못하게 한다.
                persistence_result["_backend_request_id"] = turn_request_id
                persistence_result["_backend_requested_agent_version"] = payload.agent_version
                final = _persist_turn_result(
                    session_key, rec_analysis_id, document_id, message, persistence_result
                )
                # 대화 누적 게이지 — compact 턴은 intake가 없어 갱신 안 함(다음 대화 턴에서 압축값 반영).
                # best-effort: 게이지 조회 실패가 정상 응답을 error로 바꾸지 않게 한다.
                if final.get("type") != "compact":
                    try:
                        gauge = _read_intake_gauge(session_key)
                        if gauge is not None:
                            final["usage_gauge"] = gauge
                    except Exception:  # noqa: BLE001
                        logger.warning("게이지 조회 실패 (무시): session=%s", session_key, exc_info=True)
                _tev("done", "agent", "완료", {"type": final.get("type")})
                if not disconnected:  # 끊긴 클라이언트엔 전송 생략 (저장·관측은 위에서 완료)
                    yield ProgressEvent(event="done", stage="agent", message="완료", data=final).to_sse()
            elif not saw_error and not disconnected:
                _tev("error", "agent", "응답을 생성하지 못했습니다")
                yield ProgressEvent(
                    event="error", stage="agent", message="응답을 생성하지 못했습니다"
                ).to_sse()
        except _TurnTimeout:
            # 전체 상한 초과 — hung 턴을 error로 끊는다 (RPA-235). 하위 정리는 wrapper finally가 했다.
            logger.warning("턴 시간 초과(%.0fs) — 중단: session=%s", turn_max_sec, session_key)
            _tev("error", "agent", "처리 시간이 너무 길어 중단했습니다")
            yield ProgressEvent(
                event="error", stage="agent", message="처리 시간이 너무 길어 중단했습니다"
            ).to_sse()
        except RuntimeError as e:  # OPENAI_API_KEY 미설정 등 구성 오류
            _tev("error", "agent", f"에이전트 구성 오류: {e}")
            yield ProgressEvent(event="error", stage="agent", message=f"에이전트 구성 오류: {e}").to_sse()
        except Exception:  # noqa: BLE001 — 미포착 예외가 스트림을 끊지 않도록 error로 흘린다
            logger.exception("에이전트 턴 실패: session=%s", session_key)
            _tev("error", "agent", "응답 생성 중 오류가 발생했습니다")
            yield ProgressEvent(
                event="error", stage="agent", message="응답 생성 중 오류가 발생했습니다"
            ).to_sse()
        # 타임라인 일괄 적재 — 스트림이 정상 종료된 뒤 한 번 (best-effort, threadpool).
        # 클라이언트가 중간에 끊으면 여기 못 오지만, 끊김 처리는 별도 과제(HIGH todo).
        await run_in_threadpool(_save_turn_events, session_key, turn_request_id, tev)

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
