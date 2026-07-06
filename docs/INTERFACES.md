# 백엔드 ↔ Agent 인터페이스 계약

백엔드 담당과 Agent/LLM 담당이 병렬로 개발하기 위한 경계 정의.
**HTTP·DB·파일은 백엔드, LLM 프롬프트·추론 흐름은 Agent, 두 영역은 이 문서의 함수 계약에서만 만난다.**

## 1. 소유권

| 자산 | 소유 | 규칙 |
|---|---|---|
| `app/agent/` | Agent 담당 | 백엔드는 공개 진입점만 import |
| `app/schemas/` (도메인 JSON 스키마) | 백엔드 | **변경은 양측 합의 후** 백엔드가 커밋 |
| `app/models.py`, `app/db.py` (DB) | 백엔드 | Agent 코드는 DB 직접 접근 금지 |
| FastAPI 라우트·SSE 스트림 | 백엔드 | Agent는 엔드포인트를 만들지 않는다 |
| 프롬프트 (`app/agent/prompts/` 권장) | Agent 담당 | git으로 튜닝 이력 관리 |

상태 관리 원칙: **Agent는 stateless.** 추천안 저장·버전 증가·대화 이력은 전부 백엔드가 한다.
Agent 함수는 "입력 → 산출물"만 책임진다.

## 2. 공유 도메인 스키마 (`app/schemas/`)

| 모델 | 용도 | 저장 위치 |
|---|---|---|
| `AnalysisResult` / `WorkStep` | 업무 흐름 분석 산출물 (FR-05) | `analyses.result` (JSONB) |
| `Recommendation` / `StepRecommendation` / `RecommendedAction` | 추천안 (FR-09~12) — 내보내기·골드셋 채점 대상 | `recommendations.payload` (JSONB) |
| `ProgressEvent` | SSE 진행 이벤트 규약 | (저장 안 함) |

`package`/`action`/파라미터 `name`은 **RAG 카탈로그(docs/RAG_CATALOG.md) 표기를 따른다**
(예: `Excel_MS` / `GoToCell` / `cellOption`). 골드셋 채점이 문자열 매칭 기반이므로 표기 일치가 중요하다.

## 3. 현행 계약 v0 (RPA-7에서 Agent 팀이 구현 — 동작 중)

```python
from app.agent import run_agent, stream_agent, AgentResult

def run_agent(message: str) -> AgentResult                    # AgentResult(answer: str)
async def stream_agent(message: str) -> AsyncIterator[str]    # 토큰 스트림
```

백엔드는 이 계약으로 첫 챗봇 엔드포인트를 연결한다 (연동 예시는 `app/agent/README.md`).

## 4. 목표 계약 v1 (제안 — Agent 담당과 합의 후 확정)

FR-05/09/13을 지원하는 3개 진입점. **v0에서 점진 확장**하며, 각 함수 시그니처가 곧 후속 이슈다.

```python
from app.schemas import AnalysisResult, Recommendation, ProgressEvent

# ① 문서 분석 (FR-05) — Agent 구현
def analyze(parsed_doc: dict) -> AnalysisResult:
    """parsed_doc: 백엔드 파서의 구조화 출력 (Document.parsed_content).
    마스킹은 백엔드가 이미 적용한 상태로 전달한다."""

# ② 추천 생성 (FR-09~12) — Agent 구현. 진행 이벤트를 yield하고 마지막에 done으로 산출물 전달
async def recommend(
    analysis: AnalysisResult,
    constraints: list[str] | None = None,   # 사용자 제약 (FR-14)
) -> AsyncIterator[ProgressEvent]:
    """yield 순서 예: stage(searching) → partial(step별 결과) → ... → done(data={"recommendation": ...})
    백엔드 SSE 엔드포인트가 이벤트를 그대로 흘려보낸다."""

# ③ 챗봇 수정 (FR-13~16) — Agent 구현
async def chat_refine(
    current: Recommendation,
    analysis: AnalysisResult,
    user_message: str,
    history: list[dict],                    # [{"role": "user"|"assistant", "content": str}]
) -> AsyncIterator[ProgressEvent]:
    """답변 텍스트는 token 이벤트로, 수정된 추천안은 done(data={"recommendation": ..., "change_summary": str})로.
    추천안 변경이 없는 단순 질의응답이면 done.data.recommendation = None."""
```

### 백엔드가 Agent에 제공하는 tool

```python
from app.services.rag import search_actions   # (후속 이슈에서 app/ingest 검색을 서비스로 승격)

def search_actions(
    query: str,
    k: int = 5,
    source_types: list[str] | None = None,   # 예: ["action_schema"]
) -> list[dict]:
    """pgvector 의미 검색. 반환 항목: source_type/package_name/title/content/score/url.
    현재는 GET /api/rag/search 로도 노출되어 있다."""
```

LLM 호출은 백엔드가 제공할 사용량 기록 래퍼(후속 이슈: `app/core/llm.py`)를 통과하는 것을 목표로 한다
(토큰/비용 모니터링이 전 호출을 커버해야 하므로).

## 5. SSE 이벤트 규약 (`ProgressEvent`)

`data:` 라인에 JSON 한 건. 프론트는 `event` 필드로 분기한다.

| event | 의미 | 필드 |
|---|---|---|
| `stage` | 처리 단계 진입 | `stage`(기계용), `message`(사람용) |
| `partial` | 중간 산출물 | `data` (예: 단계 1개 분석/추천 완료분) |
| `token` | LLM 텍스트 조각 | `message` |
| `done` | 완료 | `data` (최종 산출물) |
| `error` | 실패 | `message` (사용자용 문구) |

```
data: {"event":"stage","stage":"searching","message":"관련 A360 액션 검색 중"}
data: {"event":"partial","data":{"step_id":"step-1","actions":[...]}}
data: {"event":"done","data":{"recommendation":{...}}}
```

## 6. 추천안 버전 관리 (백엔드 내부 — Agent는 몰라도 됨)

모든 수정(챗봇/드래그/피드백)은 `recommendations` 테이블에 **새 버전 INSERT**.
Agent의 `chat_refine`은 수정된 `Recommendation` 전체를 반환하기만 하면 되고,
버전 번호·저장·이력은 백엔드가 처리한다.

## 7. 골드셋 채점 인터페이스 (요구사항 8.2)

평가 하네스(후속 이슈)는 `Recommendation.steps[].actions[]`의 `(package, action)` 쌍을
골드셋과 대조한다. **retrieval hit rate(검색 품질 — 백엔드 책임)와 최종 매핑 정확도(프롬프트 품질 — Agent 책임)를
분리 측정**해서 개선 지점을 특정한다. 결과는 `eval_runs` 테이블에 기록.
