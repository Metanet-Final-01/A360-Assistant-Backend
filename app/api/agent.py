"""Agent 챗 API (FR-13 대화형). app/agent의 공개 함수를 라우트에 붙인다.

agent는 엔드포인트를 소유하지 않는다(app/agent/README.md 계약) — 백엔드가 여기서
run_agent/stream_agent를 호출하고, 출처(RagSource)·SSE 규약(ProgressEvent)은
공유 도메인 스키마를 그대로 쓴다.

범위: 현재 agent는 stateless(run_agent(message))라 멀티턴 메모리·세션 영속은
후속 이슈다. 이 라우터는 무상태 질의응답만 제공한다.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.agent import run_agent, stream_agent
from app.api.auth import assert_session_owner, get_optional_user
from app.core.llm import usage_context
from app.db import get_db
from app.schemas import ProgressEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])

# 멀티턴 컨텍스트 상한 — 최근 N개 메시지(약 N/2턴)만 주입해 토큰 폭주를 막는다.
_HISTORY_LIMIT = 20


class AgentChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000, description="사용자 질문")
    # 주면 그 세션의 이전 대화를 이력으로 주입하고 이번 턴을 저장한다(멀티턴).
    # 없으면 무상태 단발 질의응답(하위호환).
    session_id: str | None = None


def _load_owned_session(session_id: str, db: Session, user: models.User | None):
    try:
        key = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(400, detail={"code": "INVALID_ID", "message": "세션 ID 형식이 올바르지 않습니다."}) from None
    session = db.get(models.AnalysisSession, key)
    if session is None:
        raise HTTPException(404, detail={"code": "SESSION_NOT_FOUND", "message": "세션을 찾을 수 없습니다."})
    assert_session_owner(session, user)  # 남의 세션 대화 열람·주입 차단
    return session


def _load_history(db: Session, session_id: uuid.UUID) -> list[dict]:
    """세션의 최근 대화 턴을 시간순 [{"role","content"}]로 반환 (Agent history 형식)."""
    rows = db.execute(
        select(models.ChatMessage)
        .where(models.ChatMessage.session_id == session_id)
        .order_by(models.ChatMessage.created_at.desc())
        .limit(_HISTORY_LIMIT)
    ).scalars().all()
    return [{"role": m.role, "content": m.content} for m in reversed(rows)]


def _persist_turn(session_id: uuid.UUID, user_msg: str, assistant_msg: str) -> None:
    """이번 턴(사용자+어시스턴트)을 chat_messages에 저장한다 (새 세션 — 스트리밍 후에도 안전)."""
    from app.db import SessionLocal

    with SessionLocal() as s:
        s.add(models.ChatMessage(session_id=session_id, role="user", content=user_msg))
        s.add(models.ChatMessage(session_id=session_id, role="assistant", content=assistant_msg))
        s.commit()


@router.post("/chat")
def agent_chat(
    payload: AgentChatRequest,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_optional_user),
) -> dict:
    """비스트리밍 질의응답 — 완성된 답변과 근거(sources)를 한 번에 반환한다.

    session_id를 주면 그 세션의 이전 대화를 이력으로 주입하고(멀티턴), 이번 턴을 저장한다.
    run_agent는 동기 함수라 def 라우트로 둔다 (FastAPI가 스레드풀에서 실행). usage_context로
    감싸 Agent의 LLM 사용을 귀속한다 (콜백이 이 블록 안에서 생성되며 스냅샷을 잡는다).
    """
    session = _load_owned_session(payload.session_id, db, user) if payload.session_id else None
    history = _load_history(db, session.id) if session else None
    try:
        with usage_context(
            component="agent", actor_type="user",
            user_id=user.id if user else None,
            session_id=session.id if session else None,
        ):
            result = run_agent(payload.message, history=history)
    except RuntimeError as e:  # OPENAI_API_KEY 미설정 등 설정 오류
        raise HTTPException(
            status_code=503,
            detail={"code": "AGENT_UNAVAILABLE", "message": f"Agent 설정 오류: {e}"},
        )
    except Exception:  # noqa: BLE001 — 미포착 예외를 raw 500 대신 {code,message}로
        logger.exception("챗 실패")
        raise HTTPException(
            status_code=500,
            detail={"code": "AGENT_ERROR", "message": "응답 생성 중 오류가 발생했습니다"},
        ) from None
    if session:
        _persist_turn(session.id, payload.message, result.answer)
    return {
        "answer": result.answer,
        "sources": [source.model_dump() for source in result.sources],
        "session_id": str(session.id) if session else None,
    }


@router.post("/chat/stream")
async def agent_chat_stream(
    payload: AgentChatRequest,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_optional_user),
) -> StreamingResponse:
    """스트리밍 질의응답 — LLM 토큰을 ProgressEvent(token→done) SSE로 흘린다.

    session_id를 주면 이전 대화를 이력으로 주입하고(멀티턴) 이번 턴을 done 시점에 저장한다.
    sources는 토큰 스트림에 실리지 않는다(app/agent 계약).
    """
    user_id = user.id if user else None
    # 이력 로드·소유권 검사는 요청 스코프 db 세션이 살아있는 지금 한다(스트리밍 시작 전).
    session = _load_owned_session(payload.session_id, db, user) if payload.session_id else None
    session_id = session.id if session else None
    history = _load_history(db, session_id) if session else None

    async def sse():
        answer_parts: list[str] = []
        try:
            # 콜백은 stream_agent 안(astream config)에서 이 블록 활성 중 생성되어 귀속 스냅샷을 잡는다.
            with usage_context(
                component="agent", actor_type="user", user_id=user_id, session_id=session_id
            ):
                async for token in stream_agent(payload.message, history=history):
                    answer_parts.append(token)
                    yield ProgressEvent(event="token", stage="chat", message=token).to_sse()
            if session_id:  # 대화 저장은 요청 db가 닫힌 뒤이므로 새 세션으로 (스트리밍 규약)
                _persist_turn(session_id, payload.message, "".join(answer_parts))
            yield ProgressEvent(
                event="done", stage="chat",
                data={"session_id": str(session_id)} if session_id else None,
            ).to_sse()
        except RuntimeError as e:  # OPENAI_API_KEY 미설정 등 설정 오류
            yield ProgressEvent(event="error", stage="chat", message=f"Agent 설정 오류: {e}").to_sse()
        except Exception:  # noqa: BLE001 — 미포착 예외가 스트림을 통째로 끊지(ERR_INCOMPLETE_CHUNKED_ENCODING)
            # 않도록 깔끔한 error 이벤트로 흘린다. 원인은 서버 로그로 남긴다.
            logger.exception("스트리밍 챗 실패")
            yield ProgressEvent(
                event="error", stage="chat", message="응답 생성 중 오류가 발생했습니다"
            ).to_sse()

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
