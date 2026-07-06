# app/agent — Agent 오케스트레이터 (LangGraph)

업무정의서 질문에 답하는 LangGraph 기반 오케스트레이터.
현재는 **단일 노드(입력 → LLM 호출 → 답변) 골격**만 있다 (RPA-7).
RAG 검색 연동·프롬프트 고도화·평가는 후속 이슈에서 진행한다.

## 공개 인터페이스 (백엔드 호출 계약)

agent는 FastAPI 엔드포인트를 소유하지 않는다. 백엔드가 아래 함수를 import해서 라우트에 붙인다.

```python
from app.agent import run_agent, stream_agent, AgentResult

def run_agent(message: str) -> AgentResult: ...            # 완성 답변 한 번에
async def stream_agent(message: str) -> AsyncIterator[str]: ...  # SSE용 토큰 스트림

class AgentResult(BaseModel):
    answer: str  # LLM 답변. 근거 문서 등 필드는 후속 이슈에서 확장
```

- `message`: 사용자 질문 문자열
- `run_agent`: `AgentResult` 반환 — `result.answer`로 답변 텍스트 접근. **동기 함수**라 `async def` 라우트에서 직접 부르면 이벤트 루프를 막는다 — 일반 `def` 라우트에서 쓰면 FastAPI가 스레드풀로 처리해서 안전.
- `stream_agent`: async 제너레이터 — LLM이 생성하는 대로 답변 토큰(str)을 yield. SSE 응답에 이걸 쓴다.
- 둘 다 `OPENAI_API_KEY`가 없으면 `RuntimeError`를 던진다 (설정 오류 — 503으로 매핑 권장)

## 호출 예시

```python
from app.agent import run_agent, stream_agent

# 비스트리밍
result = run_agent("A360에서 엑셀 데이터를 읽는 방법 알려줘")
print(result.answer)

# 스트리밍
async for token in stream_agent("A360에서 엑셀 데이터를 읽는 방법 알려줘"):
    print(token, end="", flush=True)
```

## FastAPI 라우트 연동 예시 (백엔드 담당)

비스트리밍 — `/api/rag/search`의 `RuntimeError → 503` 처리와 같은 방식:

```python
from fastapi import HTTPException
from pydantic import BaseModel

from app.agent import run_agent


class AgentChatRequest(BaseModel):
    message: str


@app.post("/api/agent/chat")
def agent_chat(payload: AgentChatRequest) -> dict:
    try:
        result = run_agent(payload.message)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=f"Agent 설정 오류: {e}")
    return {"answer": result.answer}
```

SSE 스트리밍:

```python
import json

from fastapi.responses import StreamingResponse

from app.agent import stream_agent


@app.post("/api/agent/chat/stream")
async def agent_chat_stream(payload: AgentChatRequest) -> StreamingResponse:
    async def sse():
        try:
            async for token in stream_agent(payload.message):
                yield f"data: {json.dumps({'delta': token}, ensure_ascii=False)}\n\n"
        except RuntimeError as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")
```

## 환경변수 (.env)

| 키 | 필수 | 설명 |
|---|---|---|
| `OPENAI_API_KEY` | ✅ | OpenAI API 키 |
| `OPENAI_MODEL` | — | 챗 모델명 (기본 `gpt-5.4-mini`) |

## 로컬 동작 확인

`.env`에 `OPENAI_API_KEY`를 설정한 뒤, 리포 루트에서:

```bash
# venv 활성화 후 — 비스트리밍
python -c "from app.agent import run_agent; print(run_agent('안녕').answer)"

# 스트리밍 (토큰이 조각조각 출력되면 정상)
python -c "
import asyncio
from app.agent import stream_agent

async def main():
    async for token in stream_agent('안녕'):
        print(token, end='|', flush=True)

asyncio.run(main())
"
```

인사말 답변이 출력되면 정상. `RuntimeError: OPENAI_API_KEY 환경변수가 필요합니다`가 나오면 `.env`를 확인한다.

## 구조

| 파일 | 역할 |
|---|---|
| `__init__.py` | 공개 진입점 export (`run_agent`, `stream_agent`, `AgentResult`) |
| `config.py` | 환경변수 로딩 (`OPENAI_API_KEY`, `OPENAI_MODEL`) |
| `graph.py` | LangGraph `StateGraph` 정의·컴파일, `run_agent`/`stream_agent` 구현 |
| `schemas.py` | pydantic 입출력 계약 (`AgentResult`) |
