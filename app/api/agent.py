"""Agent 챗 API (FR-13 대화형). app/agent의 공개 함수를 라우트에 붙인다.

agent는 엔드포인트를 소유하지 않는다(app/agent/README.md 계약) — 백엔드가 여기서
run_agent/stream_agent를 호출하고, 출처(RagSource)·SSE 규약(ProgressEvent)은
공유 도메인 스키마를 그대로 쓴다.

범위: 현재 agent는 stateless(run_agent(message))라 멀티턴 메모리·세션 영속은
후속 이슈다. 이 라우터는 무상태 질의응답만 제공한다.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent import run_agent, stream_agent
from app.schemas import ProgressEvent

router = APIRouter(prefix="/api/agent", tags=["agent"])


class AgentChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000, description="사용자 질문")


@router.post("/chat")
def agent_chat(payload: AgentChatRequest) -> dict:
    """비스트리밍 질의응답 — 완성된 답변과 근거(sources)를 한 번에 반환한다.

    run_agent는 동기 함수라 def 라우트로 둔다 (FastAPI가 스레드풀에서 실행해
    이벤트 루프를 막지 않음).
    """
    try:
        result = run_agent(payload.message)
    except RuntimeError as e:  # OPENAI_API_KEY 미설정 등 설정 오류
        raise HTTPException(
            status_code=503,
            detail={"code": "AGENT_UNAVAILABLE", "message": f"Agent 설정 오류: {e}"},
        )
    return {
        "answer": result.answer,
        "sources": [source.model_dump() for source in result.sources],
    }


@router.post("/chat/stream")
async def agent_chat_stream(payload: AgentChatRequest) -> StreamingResponse:
    """스트리밍 질의응답 — LLM 토큰을 ProgressEvent(token→done) SSE로 흘린다.

    sources는 토큰 스트림에 실리지 않는다(app/agent 계약). 프론트는 근거가 필요하면
    비스트리밍 /chat을 쓰거나 후속 이슈에서 done.data로 싣는 협의를 따른다.
    """

    async def sse():
        try:
            async for token in stream_agent(payload.message):
                yield ProgressEvent(event="token", stage="chat", message=token).to_sse()
            yield ProgressEvent(event="done", stage="chat").to_sse()
        except RuntimeError as e:
            yield ProgressEvent(event="error", stage="chat", message=f"Agent 설정 오류: {e}").to_sse()

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
