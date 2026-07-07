# app/agent — Agent 오케스트레이터 (LangGraph)

업무정의서 질문에 답하는 LangGraph 기반 오케스트레이터.
현재는 **retrieve(KB 검색) → generate(근거 기반 답변) 2노드 그래프**다 (RPA-11).
검색은 스텁(`FakeRetriever`, 하드코딩 카탈로그 + 키워드 매칭)이며, 실제
pgvector·하이브리드 검색은 RAG 담당 모듈 완성 후 교체한다 (아래 [검색 스텁 교체](#검색-스텁-교체-rag-담당-가이드) 참조).
멀티턴은 백엔드가 대화 이력을 저장하고 호출 시 `history`로 주입하며 Agent는 stateless를 유지한다 (RPA-25).
구조화 추천·검수 루프는 후속 이슈에서 진행한다.

## 공개 인터페이스 (백엔드 호출 계약)

agent는 FastAPI 엔드포인트를 소유하지 않는다. 백엔드가 아래 함수를 import해서 라우트에 붙인다.

```python
from app.agent import run_agent, stream_agent, AgentResult

def run_agent(message: str, history: list[dict] | None = None) -> AgentResult: ...            # 완성 답변 한 번에
async def stream_agent(message: str, history: list[dict] | None = None) -> AsyncIterator[str]: ...  # SSE용 토큰 스트림

class AgentResult(BaseModel):
    answer: str               # LLM 답변
    sources: list[RagSource]  # 답변 근거 (검색 결과 없으면 빈 리스트)
```

출처 모델은 공유 도메인 스키마 `app.schemas.RagSource`를 그대로 쓴다
(`source_type`/`title`/`url`/`score`) — 추천안(`Recommendation`)의 액션 출처와 같은 형태라
프론트가 한 가지 모델만 다루면 된다.

- `message`: 사용자 질문 문자열
- `history`(선택): 이전 대화 턴 `[{"role": "user"|"assistant", "content": str}]`. 백엔드가 세션 이력을 저장하고 호출 시 주입한다 — **Agent는 이력을 보관하지 않는다(stateless)**. 생략하면 단발 질의응답(기존 동작)과 동일하고, 현재 챗 라우트와 하위호환된다. `retrieve`는 최신 `message` 기준으로 동작한다(이력 기반 질의 재작성은 후속).
- `run_agent`: `AgentResult` 반환 — `result.answer`로 답변 텍스트, `result.sources`로 근거 문서 접근. **동기 함수**라 `async def` 라우트에서 직접 부르면 이벤트 루프를 막는다 — 일반 `def` 라우트에서 쓰면 FastAPI가 스레드풀로 처리해서 안전.
- `stream_agent`: async 제너레이터 — LLM이 생성하는 대로 답변 토큰(str)을 yield. SSE 응답에 이걸 쓴다. **sources는 토큰 스트림에 실리지 않는다** (`done` 이벤트의 `data`로 실을지는 후속 이슈에서 협의).
- 둘 다 `OPENAI_API_KEY`가 없으면 `RuntimeError`를 던진다 (설정 오류 — 503으로 매핑 권장)

### 문서 분석 — `analyze` (FR-05)

```python
from app.agent import analyze
from app.schemas import AnalysisResult

def analyze(parsed_doc: dict) -> AnalysisResult: ...   # parsed_content → 업무 단계 분석
```

- `parsed_doc`: 백엔드 파서 산출물(`documents.parsed_content`) — 마스킹 적용된 상태로 전달.
- 반환 `AnalysisResult`(공유 스키마): `document_title / summary / steps[WorkStep] / ambiguities`. 저장은 백엔드(`analyses.result`).
- LLM은 `app.core.llm.chat(purpose="analyze", response_format={"type":"json_object"})` 경유 — 사용량은 `usage_context`로 귀속(백엔드가 심음). 출력은 Pydantic으로 검증하고 실패 시 1회 교정한다.
- `OPENAI_API_KEY` 미설정 등은 `RuntimeError`(503 매핑), 교정 후에도 스키마 불일치면 `ValueError`.
- `vision_text`(이미지 자동 추출) 기반 불확실 항목은 `ambiguities`로 분리된다. `step_id`/`order`는 결정론적으로 재부여되어 안정적이다(recommend가 참조).

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

# 멀티턴 — 백엔드가 이전 대화 턴을 history로 주입 (Agent는 stateless)
history = [
    {"role": "user", "content": "엑셀 읽는 방법 알려줘"},
    {"role": "assistant", "content": "Excel advanced 패키지의 Open / Get multiple cells를 씁니다."},
]
result = run_agent("방금 읽은 걸 다른 시트에 쓰려면?", history=history)
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

SSE 스트리밍 — 이벤트 형식은 팀 규약인 `app.schemas.ProgressEvent`를 쓴다
(`docs/INTERFACES.md` §5, 프론트는 `event` 필드로 분기):

```python
from fastapi.responses import StreamingResponse

from app.agent import stream_agent
from app.schemas import ProgressEvent


@app.post("/api/agent/chat/stream")
async def agent_chat_stream(payload: AgentChatRequest) -> StreamingResponse:
    async def sse():
        try:
            async for token in stream_agent(payload.message):
                yield ProgressEvent(event="token", message=token).to_sse()
            yield ProgressEvent(event="done").to_sse()
        except RuntimeError as e:
            yield ProgressEvent(event="error", message=f"Agent 설정 오류: {e}").to_sse()

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
| `__init__.py` | 공개 진입점 export (`run_agent`, `stream_agent`, `AgentResult`) |
| `config.py` | 환경변수 로딩 (`OPENAI_API_KEY`, `OPENAI_MODEL`) |
| `graph.py` | LangGraph `StateGraph` 정의·컴파일 (retrieve → generate), `run_agent`/`stream_agent` 구현 |
| `retrieval.py` | 검색 인터페이스(`Retriever`)와 스텁 구현(`FakeRetriever`) |
| `schemas.py` | pydantic 입출력 계약 (`AgentResult` — 출처는 공유 `app.schemas.RagSource`) |

## 검색 스텁 교체 (RAG 담당 가이드)

agent는 `retrieval.Retriever` 인터페이스에만 의존한다:

```python
class Retriever(Protocol):
    def search(self, query: str, limit: int = 4) -> list[dict]: ...
```

반환 dict 스키마는 `/api/rag/search`(`app.rag.store.db.search`)의 행과 동일하다:
`id, source_type, package_name, action_name, title, url, content, score`.

실제 검색 모듈(pgvector·하이브리드)이 완성되면 `retrieval.py`의 `get_retriever()`가
그 구현을 반환하도록 교체하면 된다 — `graph.py` 등 나머지 agent 코드는 수정 불필요.
