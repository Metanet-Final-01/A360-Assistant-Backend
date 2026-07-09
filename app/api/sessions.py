"""세션 단위 라우터 — 세션 생성, 추천안(흐름도) 버전 CRUD, 에이전트 단일 진입점(/turn).

분석/질문/흐름도 수정은 모두 POST /{id}/turn 하나로 처리한다 (app/agent의 stream_agent_turn).
레거시 개별 엔드포인트(/analyze, /recommend, /api/agent/chat)는 /turn으로 흡수돼 제거됐다 (RPA-67).
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.api.auth import assert_session_owner, get_optional_user
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
    이벤트: (에이전트가 흘리는) stage/partial/token → done(data={type, ...저장 메타, answer, ...}).
    """
    session = _owned_session_or_404(session_id, db, user)
    stream_turn = _get_agent_turn()
    if stream_turn is None:
        # 스트림 열기 전 판정 — 프론트가 일반 HTTP 에러로 잡아 폴백 분기하기 쉽게
        raise HTTPException(
            503, detail={"code": "AGENT_UNAVAILABLE", "message": "에이전트가 아직 준비되지 않았습니다."}
        )

    ctx = _assemble_turn_context(session, db, operation=payload.operation)
    agent_context = ctx["agent_context"]
    session_key = session.id
    rec_analysis_id, document_id = ctx["rec_analysis_id"], ctx["document_id"]
    user_id, message = (user.id if user else None), payload.message

    async def sse():
        result_data = None
        saw_error = False
        try:
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
