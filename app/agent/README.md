# app/agent — Agent 오케스트레이터 (LangGraph)

업무정의서 질문에 답하는 LangGraph 기반 오케스트레이터.
현재는 **retrieve(KB 검색) → generate(근거 기반 답변) 2노드 그래프**다 (RPA-11).
검색은 스텁(`FakeRetriever`, 하드코딩 카탈로그 + 키워드 매칭)이며, 실제
pgvector·하이브리드 검색은 RAG 담당 모듈 완성 후 교체한다 (아래 [검색 스텁 교체](#검색-스텁-교체-rag-담당-가이드) 참조).
구조화 추천·검수 루프·멀티턴 메모리는 후속 이슈에서 진행한다.

## 공개 인터페이스 (백엔드 호출 계약)

agent는 FastAPI 엔드포인트를 소유하지 않는다. 백엔드가 아래 함수를 import해서 라우트에 붙인다.

```python
from app.agent import run_agent, stream_agent, AgentResult, Source

def run_agent(message: str) -> AgentResult: ...            # 완성 답변 한 번에
async def stream_agent(message: str) -> AsyncIterator[str]: ...  # SSE용 토큰 스트림

class Source(BaseModel):
    id: str
    title: str
    package_name: str | None
    action_name: str | None
    url: str | None
    score: float

class AgentResult(BaseModel):
    answer: str            # LLM 답변
    sources: list[Source]  # 답변 근거 문서 메타데이터 (검색 결과 없으면 빈 리스트)
```

- `message`: 사용자 질문 문자열
- `run_agent`: `AgentResult` 반환 — `result.answer`로 답변 텍스트, `result.sources`로 근거 문서 접근. **동기 함수**라 `async def` 라우트에서 직접 부르면 이벤트 루프를 막는다 — 일반 `def` 라우트에서 쓰면 FastAPI가 스레드풀로 처리해서 안전.
- `stream_agent`: async 제너레이터 — LLM이 생성하는 대로 답변 토큰(str)을 yield. SSE 응답에 이걸 쓴다. **sources는 토큰 스트림에 실리지 않는다** (SSE 이벤트 설계는 후속 이슈에서 협의).
- 둘 다 `OPENAI_API_KEY`가 없으면 `RuntimeError`를 던진다 (설정 오류 — 503으로 매핑 권장)

## 호출 예시

```python
from app.agent import run_agent, stream_agent

# 비스트리밍
result = run_agent("A360에서 엑셀 데이터를 읽는 방법 알려줘")
print(result.answer)
for source in result.sources:
    print(f"- 근거: {source.title} (score={source.score})")

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
    return {
        "answer": result.answer,
        "sources": [source.model_dump() for source in result.sources],
    }
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
# venv 활성화 후 — 비스트리밍 (KB에 있는 주제: 근거와 함께 답변)
python -c "
from app.agent import run_agent

result = run_agent('A360에서 엑셀 데이터를 읽는 방법 알려줘')
print(result.answer)
print([s.title for s in result.sources])
"

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

엑셀 관련 답변과 근거 제목 리스트가 출력되면 정상. KB에 없는 주제(예: "주식 추천해줘")를
물으면 sources가 비고, 지식베이스로 확인할 수 없다는 답변이 나와야 한다.
`RuntimeError: OPENAI_API_KEY 환경변수가 필요합니다`가 나오면 `.env`를 확인한다.

## 구조

| 파일 | 역할 |
|---|---|
| `__init__.py` | 공개 진입점 export (`run_agent`, `stream_agent`, `AgentResult`, `Source`) |
| `config.py` | 환경변수 로딩 (`OPENAI_API_KEY`, `OPENAI_MODEL`) |
| `graph.py` | LangGraph `StateGraph` 정의·컴파일 (retrieve → generate), `run_agent`/`stream_agent` 구현 |
| `retrieval.py` | 검색 인터페이스(`Retriever`)와 스텁 구현(`FakeRetriever`) |
| `schemas.py` | pydantic 입출력 계약 (`AgentResult`, `Source`) |

## 검색 스텁 교체 (RAG 담당 가이드)

agent는 `retrieval.Retriever` 인터페이스에만 의존한다:

```python
class Retriever(Protocol):
    def search(self, query: str, limit: int = 4) -> list[dict]: ...
```

반환 dict 스키마는 `/api/rag/search`(`app.ingest.db.search`)의 행과 동일하다:
`id, source_type, package_name, action_name, title, url, content, score`.

실제 검색 모듈(pgvector·하이브리드)이 완성되면 `retrieval.py`의 `get_retriever()`가
그 구현을 반환하도록 교체하면 된다 — `graph.py` 등 나머지 agent 코드는 수정 불필요.
