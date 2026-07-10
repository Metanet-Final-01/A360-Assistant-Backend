"""세션 단위 라우터 — 세션 생성, 추천안(흐름도) 버전 CRUD, 에이전트 단일 진입점(/turn).

분석/질문/흐름도 수정은 모두 POST /{id}/turn 하나로 처리한다 (app/agent의 stream_agent_turn).
레거시 개별 엔드포인트(/analyze, /recommend, /api/agent/chat)는 /turn으로 흡수돼 제거됐다 (RPA-67).
"""

import logging
import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import models
from app.api.auth import assert_session_owner, get_current_user, get_optional_user
from app.core.llm import usage_context
from app.db import get_db
from app.schemas import ProgressEvent, Recommendation

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


def _save_recommendation(
    session_id: uuid.UUID, analysis_id: uuid.UUID, payload: dict,
    source: str, parent_version: int | None, change_summary: str | None = None,
) -> dict:
    """새 추천안 버전을 저장한다 (version은 세션 내 max+1). 새 세션 사용(스트리밍 후에도 안전).

    동시 저장으로 같은 version이 계산되면 uq_recommendations_session_version 제약에
    걸리므로(더블클릭 등), IntegrityError 시 version을 재계산해 몇 번 재시도한다.
    """
    from sqlalchemy.exc import IntegrityError

    from app.db import SessionLocal

    for attempt in range(3):
        try:
            with SessionLocal() as s:
                last = s.execute(
                    select(func.max(models.RecommendationVersion.version))
                    .where(models.RecommendationVersion.session_id == session_id)
                ).scalar()
                version = (last or 0) + 1
                row = models.RecommendationVersion(
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
                return _recommendation_out(row)
        except IntegrityError:
            if attempt == 2:  # 마지막 시도까지 충돌하면 상위에서 처리
                raise
            logger.warning("추천안 version 충돌 — 재계산 재시도 (session=%s)", session_id)


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
        raise HTTPException(
            400, detail={"code": "INVALID_RECOMMENDATION", "message": f"추천안 형식이 올바르지 않습니다: {e.error_count()}건"}
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


class AgentTurnRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000, description="사용자 메시지 (버튼이면 프론트가 합성)")
    # compact 버튼발 결정론 요청 — LLM 라우터를 우회하는 명시 신호 (기본 "chat")
    operation: str = Field("chat", pattern="^(chat|compact)$", description="chat | compact")


def _assemble_turn_context(
    session: models.AnalysisSession, db: Session, operation: str = "chat"
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
    함께 오고, 흐름도의 step_id가 그 분석본을 참조하므로 분석이 저장 안 되면 참조가 끊긴다.
    이때 흐름도는 이번 턴에 새로 저장한 분석의 id에 귀속시켜 무결성을 지킨다.
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
        )
        out.update(saved)  # id, version, parent_version, source, change_summary, created_at
        out["recommendation"] = rec
        saved_version = saved["version"]

    _persist_chat_turn(session_id, user_message, answer, recommendation_version=saved_version)
    return out


_GAUGE_WARN_RATIO = 0.7  # intake 토큰이 이 비율을 넘으면 compact 권장 신호


def _read_intake_gauge(session_id: uuid.UUID) -> dict | None:
    """이번 세션 최신 intake 호출의 prompt_tokens로 대화 누적 게이지를 만든다.

    정준환의 intake 태깅(purpose="intake", RPA-73) 기준 — 이 호출엔 history+compact가 절삭
    없이 실리므로 input_tokens가 "대화 누적분"의 충실한 대리값이다. 임계는 env로 조정한다.
    """
    from app.db import SessionLocal

    with SessionLocal() as s:
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
        limit = int(os.getenv("TURN_GAUGE_LIMIT_TOKENS", "100000"))
    except ValueError:
        limit = 0
    if limit <= 0:
        limit = 100000
    ratio = round(tokens / limit, 4)
    return {
        "intake_tokens": int(tokens),
        "limit_tokens": limit,
        "ratio": ratio,
        "compact_recommended": bool(ratio >= _GAUGE_WARN_RATIO),  # 소프트: 프론트가 압축 유도
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

    tiktoken이 있으면 실제 인코딩으로 세고(정확), 없으면 문자 기반으로 폴백한다.
    안전망 성격상 과소추정이 더 위험(스파이크를 놓침)하므로 폴백은 1문자≈1토큰(len)로
    잡는다 — cl100k_base 실측: 한글 ≈0.9~1.0 tok/char라 len//2는 절반 과소추정이었고
    (CodeRabbit #134), len은 한글과 거의 일치·영문(≈0.15 tok/char)엔 과대추정이라 가드로 안전.
    """
    if not text:
        return 0
    enc = _token_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:  # noqa: BLE001 — 인코딩 실패 시 문자 폴백
            pass
    return max(1, len(text))


def _needs_auto_compact(gauge: dict, message: str) -> bool:
    """자동 compact가 필요한가 — 후행 게이지 + 이번 입력 선행(look-ahead) 반영 (RPA-84/86).

    게이지의 compact_required는 '직전 intake 기준'이라 지금 들어온 대형 입력을 못 본다.
    이번 message의 토큰 추정치를 더한 '예상 비율'이 하드 임계를 넘으면 미리 압축한다 —
    게이지 0.99에서 초대형 단일 입력이 당턴을 넘치게 하는 갭(RPA-86)을 닫는다.
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


@router.post("/{session_id}/turn")
async def agent_turn(
    session_id: str,
    payload: AgentTurnRequest,
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
            compact_ctx = _assemble_turn_context(session, db, operation="compact")["agent_context"]
            try:
                with usage_context(
                    component="agent", actor_type="user", user_id=user_id, session_id=session_key
                ):
                    auto_compacted = await _run_internal_compact(
                        session_key, message, stream_turn, compact_ctx
                    )
            except Exception:  # noqa: BLE001 — 자동 압축 실패가 사용자 턴을 막지 않게
                logger.warning("자동 compact 실패 (무시): session=%s", session_key, exc_info=True)

    ctx = _assemble_turn_context(session, db, operation=payload.operation)
    agent_context = ctx["agent_context"]
    rec_analysis_id, document_id = ctx["rec_analysis_id"], ctx["document_id"]

    async def sse():
        result_data = None
        saw_error = False
        try:
            if auto_compacted:
                yield ProgressEvent(
                    event="stage", stage="compacting", message="대화가 길어 자동 압축했습니다"
                ).to_sse()
            # async 제너레이터라 usage_context가 yield를 넘어도 안전 (같은 태스크 컨텍스트).
            with usage_context(
                component="agent", actor_type="user", user_id=user_id, session_id=session_key
            ):
                async for event in stream_turn(message, agent_context):
                    if event.event == "done":
                        result_data = event.data or {}
                        continue  # done은 저장 후 최종 메타와 함께 다시 낸다
                    if event.event == "error":
                        saw_error = True  # 에이전트가 이미 error를 흘림 — 중복 error 안 냄
                    yield event.to_sse()
            if result_data is not None:
                final = _persist_turn_result(
                    session_key, rec_analysis_id, document_id, message, result_data
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
                yield ProgressEvent(event="done", stage="agent", message="완료", data=final).to_sse()
            elif not saw_error:
                yield ProgressEvent(
                    event="error", stage="agent", message="응답을 생성하지 못했습니다"
                ).to_sse()
        except RuntimeError as e:  # OPENAI_API_KEY 미설정 등 구성 오류
            yield ProgressEvent(event="error", stage="agent", message=f"에이전트 구성 오류: {e}").to_sse()
        except Exception:  # noqa: BLE001 — 미포착 예외가 스트림을 끊지 않도록 error로 흘린다
            logger.exception("에이전트 턴 실패: session=%s", session_key)
            yield ProgressEvent(
                event="error", stage="agent", message="응답 생성 중 오류가 발생했습니다"
            ).to_sse()

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
