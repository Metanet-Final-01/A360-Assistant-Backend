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
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.api.auth import assert_session_owner, get_optional_user
from app.core.llm import usage_context
from app.db import get_db
from app.schemas import ProgressEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


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
