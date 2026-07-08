"""세션 단위 파이프라인 라우터 — 분석(FR-05). 추천·챗 수정은 후속 이슈.

분석 실행 주체는 app/agent의 analyze()다 (INTERFACES.md §4 계약, Agent 담당 구현).
아직 랜딩 전이므로 lazy import 스위치를 둔다: 없으면 503 AGENT_UNAVAILABLE을 돌려주고,
Agent가 analyze를 내보내는 순간 이 라우트는 코드 변경 없이 활성화된다
(RPA-24의 get_retriever() 스위치와 같은 패턴).
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.api.auth import assert_session_owner, get_optional_user
from app.core.llm import usage_context
from app.db import get_db
from app.schemas import AnalysisResult, ProgressEvent, Recommendation

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


def _get_agent_analyze():
    """app/agent의 analyze()를 lazy import한다. 미구현이면 None."""
    try:
        from app.agent import analyze  # noqa: PLC0415

        return analyze
    except ImportError:
        return None


@router.post("/{session_id}/analyze")
def analyze_session(
    session_id: str,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_optional_user),
) -> StreamingResponse:
    """세션의 최신 파싱 완료 문서를 Agent로 분석한다 (FR-05) — SSE 스트림.

    이벤트: stage(analyzing) → done(data=AnalysisResult JSON) / error.
    LLM 호출로 수십 초 걸릴 수 있어 SSE로 진행 상황을 흘린다 (규약: INTERFACES.md §5).
    """
    try:
        session_key = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(
            400, detail={"code": "INVALID_ID", "message": "세션 ID 형식이 올바르지 않습니다."}
        ) from None

    session = db.get(models.AnalysisSession, session_key)
    if session is None:
        raise HTTPException(404, detail={"code": "SESSION_NOT_FOUND", "message": "세션을 찾을 수 없습니다."})
    assert_session_owner(session, user)  # 남의 세션 분석 차단

    # 세션의 가장 최근 문서 기준 (다중 업로드 시 마지막 업로드가 분석 대상)
    document = db.execute(
        select(models.Document)
        .where(models.Document.session_id == session_key)
        .order_by(models.Document.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if document is None:
        raise HTTPException(404, detail={"code": "NO_DOCUMENT", "message": "세션에 업로드된 문서가 없습니다."})
    if document.status != "parsed" or document.parsed_content is None:
        raise HTTPException(
            409,
            detail={"code": "NOT_PARSED", "message": f"파싱이 완료되지 않았습니다 (현재 상태: {document.status})."},
        )

    analyze = _get_agent_analyze()
    if analyze is None:
        # 스트림을 열기 전에 판정 — 프론트가 일반 HTTP 에러로 잡아 목업 폴백 등 분기하기 쉽게
        raise HTTPException(
            503,
            detail={"code": "AGENT_UNAVAILABLE", "message": "분석 엔진이 아직 준비되지 않았습니다."},
        )

    doc_id, session_key_v, parsed = document.id, session.id, document.parsed_content
    user_id = user.id if user else None

    def sse():
        try:
            yield ProgressEvent(
                event="stage", stage="analyzing", message="업무 단계를 분석하고 있습니다"
            ).to_sse()
            # 주의: usage_context를 yield 너머로 걸치면 안 된다 — 동기 제너레이터는
            # StreamingResponse가 next()마다 다른 스레드 컨텍스트에서 재개하므로
            # ContextVar가 전파되지 않고 reset도 ValueError로 터진다.
            # with 블록은 yield 없이 한 재개 구간 안에서 열고 닫는다.
            with usage_context(
                component="agent", actor_type="user", user_id=user_id, session_id=session_key_v
            ):
                result = analyze(parsed)  # AnalysisResult (INTERFACES §4)
            result_json = result.model_dump()

            # 요청 스코프 db 세션은 스트리밍 시작 전에 닫히므로 저장은 새 세션으로 (FastAPI 0.106+)
            from app.db import SessionLocal

            analysis_id = None
            with SessionLocal() as s:
                row = models.Analysis(
                    session_id=session_key_v,
                    document_id=doc_id,
                    status="completed",
                    result=result_json,
                )
                s.add(row)
                s.commit()
                analysis_id = str(row.id)

            yield ProgressEvent(
                event="done",
                stage="analyzing",
                message="분석 완료",
                data={"analysis_id": analysis_id, **result_json},
            ).to_sse()
        except RuntimeError as e:  # OPENAI_API_KEY 미설정 등 구성 오류
            # 구성 오류도 시도된 분석의 실패이므로 이력에 남긴다 (계약: 실패도 영속화)
            _record_failure(session_key_v, doc_id, "분석 엔진 구성 오류")
            yield ProgressEvent(event="error", stage="analyzing", message=f"분석 엔진 구성 오류: {e}").to_sse()
        except Exception:  # noqa: BLE001
            logger.exception("분석 실패: session=%s document=%s", session_key_v, doc_id)
            _record_failure(session_key_v, doc_id, "분석 중 오류가 발생했습니다")
            yield ProgressEvent(
                event="error", stage="analyzing", message="분석 중 오류가 발생했습니다"
            ).to_sse()

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _record_failure(session_id: uuid.UUID, document_id: uuid.UUID, error: str) -> None:
    """분석 실패를 Analysis 행으로 남긴다 (실패해도 스트림 에러 이벤트는 나가야 하므로 best-effort)."""
    try:
        from app.db import SessionLocal

        with SessionLocal() as s:
            s.add(
                models.Analysis(
                    session_id=session_id,
                    document_id=document_id,
                    status="failed",
                    error=error,
                )
            )
            s.commit()
    except Exception:  # noqa: BLE001
        logger.exception("분석 실패 기록마저 실패: session=%s", session_id)


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
    """새 추천안 버전을 저장한다 (version은 세션 내 max+1). 새 세션 사용(스트리밍 후에도 안전)."""
    from app.db import SessionLocal

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


@router.post("/{session_id}/recommend")
async def recommend_session(
    session_id: str,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_optional_user),
) -> StreamingResponse:
    """세션의 최신 분석으로 A360 추천안(흐름도)을 생성하고 v1로 저장한다 (FR-09~12) — SSE.

    이벤트: stage/partial(recommend가 흘리는 진행) → done(data={recommendation, version...}).
    recommend()는 async라 async 제너레이터로 소비한다.
    """
    from app.agent import recommend

    session = _owned_session_or_404(session_id, db, user)
    analysis = db.execute(
        select(models.Analysis)
        .where(models.Analysis.session_id == session.id, models.Analysis.status == "completed")
        .order_by(models.Analysis.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if analysis is None or analysis.result is None:
        raise HTTPException(
            409, detail={"code": "NO_ANALYSIS", "message": "먼저 문서 분석(analyze)을 완료해 주세요."}
        )
    analysis_result = AnalysisResult.model_validate(analysis.result)
    session_key, analysis_id, user_id = session.id, analysis.id, (user.id if user else None)

    async def sse():
        rec_payload = None
        try:
            # recommend는 async 제너레이터 — 콜백이 이 블록 안에서 생성돼 귀속 스냅샷을 잡는다.
            # (async 제너레이터는 같은 태스크 컨텍스트에서 재개되므로 usage_context가 yield를
            #  넘어도 안전하다 — 동기 제너레이터의 ContextVar 문제와 다름.)
            with usage_context(
                component="agent", actor_type="user", user_id=user_id, session_id=session_key
            ):
                async for event in recommend(analysis_result):
                    if event.event == "done":
                        rec_payload = (event.data or {}).get("recommendation")
                        continue  # done은 저장 후 버전정보와 함께 다시 낸다
                    yield event.to_sse()
            if rec_payload is None:
                raise ValueError("recommend가 추천안을 반환하지 않았습니다")
            saved = _save_recommendation(session_key, analysis_id, rec_payload, source="agent", parent_version=None)
            yield ProgressEvent(
                event="done", stage="recommending", message="추천 완료",
                data={**saved, "recommendation": rec_payload},
            ).to_sse()
        except RuntimeError as e:  # OPENAI_API_KEY 미설정 등
            yield ProgressEvent(event="error", stage="recommending", message=f"추천 엔진 구성 오류: {e}").to_sse()
        except Exception:  # noqa: BLE001
            logger.exception("추천 생성 실패: session=%s", session_key)
            yield ProgressEvent(
                event="error", stage="recommending", message="추천 생성 중 오류가 발생했습니다"
            ).to_sse()

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
