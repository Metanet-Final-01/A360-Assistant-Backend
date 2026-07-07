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

**분기·반복 구조**: `RecommendedAction.children`이 A360 봇 JSON의 노드 트리와 1:1 대응한다.
Loop/If/Else/Step 같은 컨테이너 액션은 본문을 children에 담고, 분기 블록이 끝나면 실행은
다음 형제 액션으로 이어진다(= 병합). A360에는 임의의 그래프 병합이 없으므로 트리로 충분하며,
흐름도 시각화(FR-18)·드래그 편집도 이 트리에서 도출한다.

### 2.1 `Recommendation` 상세 가이드 — 최종 산출물 JSON

챗봇 수정까지 끝나고 확정되면 내보내기(FR-17)로 출력되는 JSON이자 골드셋 채점 대상.

**최상위는 3덩어리:**

```
Recommendation
 ├─ steps[]        업무 단계별 액션 시퀀스 (본체)
 ├─ variables[]    봇 변수 — 단계 사이에서 데이터가 만나는(합쳐지는) 통로
 └─ notes          전제·주의사항
```

**계층 4단계:**

```
steps[]               ─ 업무 단계 (AnalysisResult.steps[].step_id 참조)
 └─ actions[]         ─ 그 단계를 자동화하는 A360 액션 (순서대로 실행)
     ├─ parameters[]  ─ 액션 설정값 (카탈로그 파라미터 name 그대로)
     └─ children[]    ─ Loop/If 컨테이너의 본문 (재귀 — A360 봇 트리와 1:1)
```

**전체 예시** — 샘플 업무정의서 "금 시세 조회 후 결과 발송"의 Task 1·3:

```json
{
  "schema_version": "1.0",
  "steps": [
    {
      "step_id": "step-1",
      "actions": [
        {
          "order": 1,
          "package": "Browser", "action": "openbrowser", "label": "브라우저 열기",
          "parameters": [
            { "name": "url", "value": "https://finance.naver.com", "value_source": "llm" }
          ],
          "rationale": "네이버 증권 접속 단계이므로 Browser 패키지로 페이지를 연다",
          "sources": [
            { "source_type": "action_schema", "title": "Browser: 열기", "score": 0.91 }
          ],
          "confidence": 0.95
        }
      ]
    },
    {
      "step_id": "step-3",
      "actions": [
        { "order": 1, "package": "Excel_MS", "action": "OpenSpreadsheet", "label": "열기",
          "parameters": [{ "name": "session", "value": "Default", "value_source": "schema_default" }] },
        {
          "order": 2,
          "package": "Loop", "action": "loop.commands.start", "label": "루프",
          "parameters": [{ "name": "loopType", "value": "Times=3", "value_source": "llm" }],
          "rationale": "'최근 3일치' 반복 요건 → Loop 3회",
          "children": [
            { "order": 1, "package": "Excel_MS", "action": "SetCell", "label": "셀 설정" }
          ]
        },
        { "order": 3, "package": "Excel_MS", "action": "SaveSpreadSheet", "label": "저장" }
      ]
    }
  ],
  "variables": [
    { "name": "goldPrices", "type": "TABLE",  "direction": "local", "description": "웹에서 추출한 시세 표" },
    { "name": "outputFile", "type": "STRING", "direction": "input", "description": "저장할 엑셀 경로" }
  ],
  "notes": "Knox Portal 메일 발송은 Email 패키지 기준으로 추천함 (SMTP 정보 필요)"
}
```

읽는 법: step-3에서 엑셀을 열고 → Loop를 3회 도는데 루프 안(`children`)에서 셀을 쓰고 →
루프가 끝나면 저장한다. 웹 데이터가 엑셀로 "합쳐지는" 것은 `variables`의
`goldPrices`(TABLE)를 통해 일어난다 — 제어 흐름은 트리(children), 데이터 흐름은 변수가 담당.

**필드 사전 (왜 있는가):**

| 필드 | 이유 |
|---|---|
| `step_id` | 분석 결과의 단계 참조 — 추천이 어느 업무 단계에서 나왔는지 (FR-09) |
| `package`/`action`/`parameters` | FR-10. **카탈로그 표기 그대로** — 골드셋 문자열 매칭 기준 |
| `parameters[].value_source` | `schema_default`(카탈로그 기본값)/`llm`(추론)/`user`(사용자 지정). 챗봇 수정 시 user 값은 보존 |
| `children` | Loop/If/Step 본문 (A360 봇 JSON 트리와 1:1) |
| `rationale`+`sources` | FR-11 추천 근거 + RAG 출처 — UI "왜 이 액션?" 펼침 재료 |
| `confidence` | FR-12 신뢰도 |
| `variables` | FR-10 입출력 변수 + 단계 간 데이터 전달 |
| `notes` | 확정 못 한 전제 기록 (챗봇 재질의 후보와 연동) |

**이 JSON 하나를 재사용하는 기능들**: 내보내기(FR-17) · 흐름도/드래그 편집(FR-18, 트리 렌더) ·
챗봇 수정(FR-13, 새 버전 저장) · 골드셋 채점(`iter_actions()` 평탄화) · 비개발자 요약본 생성.

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
from app.services.rag import search_actions   # app/rag 하이브리드 검색을 감싼 wrapper (RPA-9에서 구현 완료)

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

**스키마 강제 출력 (analyze/recommend)** — `core.llm.chat()`은 `response_format` 파라미터를 받아
OpenAI JSON mode / Structured Outputs를 지원한다. AnalysisResult·Recommendation처럼 스키마를
강제해야 하는 출력은 이걸 넘긴다 (반환은 str이며 JSON 파싱·검증은 agent가 한다):

```python
from app.core.llm import chat
# Structured Outputs (스키마 강제 — 골드셋 표기 일치에 유리)
raw = chat(messages, purpose="analyze",
           response_format={"type": "json_schema",
                            "json_schema": {"name": "AnalysisResult", "schema": {...}, "strict": True}})
# 또는 최소 JSON mode
raw = chat(messages, purpose="recommend", response_format={"type": "json_object"})
```

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

평가 하네스(후속 이슈)는 `Recommendation.iter_actions()`로 액션 트리를 평탄화한
`(package, action)` 쌍을 골드셋과 대조한다. **retrieval hit rate(검색 품질 — 백엔드 책임)와 최종 매핑 정확도(프롬프트 품질 — Agent 책임)를
분리 측정**해서 개선 지점을 특정한다. 결과는 `eval_runs` 테이블에 기록.
