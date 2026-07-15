# app/agent — 서비스 에이전트 (버전별 모듈)

업무정의서를 분석하고 A360 자동화 흐름도를 생성·수정하는 LangGraph 에이전트다.
**에이전트는 stateless** — 저장·버전·이력은 전부 백엔드가 한다(INTERFACES §1). 백엔드는
`POST /api/sessions/{id}/turn`에서 공개 진입점 `stream_agent_turn` **하나만** import한다.

## 버전 구조 (RPA-167)

서비스 에이전트는 깃 이력상 두 아키텍처를 거쳤고, 이를 **버전 폴더로 완전 분리(벤더링)** 했다.
각 버전은 orchestrator·recommend·verify·prompts까지 **자기 사본**을 가진 독립 구현이라, 한
버전을 고쳐도 다른 버전에 영향이 없다.

```
app/agent/
├── __init__.py   # 공개 디스패처 — stream_agent_turn/analyze/recommend 를 버전별로 위임
├── registry.py   # vN 자동탐색 + available_versions()/default_version()/resolve_version()
├── README.md
├── v1/           # 단계분해 → 액션 1:1 매핑 (plan→shortlist→compose→assemble→check). 레거시
│   ├── meta.py   #   registry가 읽는 경량 메타(label/description)
│   └── __init__.py analysis.py config.py retrieval.py orchestrator/ recommend/ verify/ prompts/
└── v2/           # agentic ReAct 루프 (compose_agent ⇄ tools → verify → finalize). 현재 기본
    ├── meta.py
    └── __init__.py analysis.py config.py retrieval.py orchestrator/ recommend/ verify/ prompts/
```

## 버전 선택 (요청 → 디스패치)

버전은 진입점 시그니처를 바꾸지 않고 **기존 `context` dict의 새 키 `agent_version`**으로 흘러온다
(`operation`/`compact`와 같은 "확장 제안분 — 없으면 기본값" 패턴). 백엔드가 `AgentTurnRequest`로
받은 값을 `context`에 실어주면, 디스패처가 그 버전 구현으로 위임한다.

```python
from app.agent import stream_agent_turn
async for event in stream_agent_turn(message, context):  # context["agent_version"] = "v1" | "v2" | None
    ...
```

- `agent_version` 없음 → env `AGENT_VERSION`(없으면 `v2`) 기본으로 동작.
- 미지 버전을 명시 요청하면 `ValueError`(백엔드 엔드포인트가 `available_versions()`로 사전 검증).

> **HTTP 표면은 백엔드가 제공한다(이 PR 범위 밖).** `AgentTurnRequest.agent_version` 필드와
> `GET /api/agent/versions` 엔드포인트는 백엔드 담당이 구현한다(작업요청서). 이 패키지는
> `context["agent_version"]` 소비와 `available_versions()`/`default_version()` 헬퍼만 제공한다.

## 버전 추가 (v3 이후 — 프론트·백엔드 무변경)

`app/agent/v3/` 폴더를 **드롭하는 것만으로** 끝난다:

1. `v3/` 안에 그 버전의 완결된 에이전트(진입점 `stream_agent_turn`/`analyze`/`recommend` re-export)와 `v3/meta.py`(`VERSION_META = {"label", "description"}`)를 둔다.
2. registry가 자동 발견 → 백엔드 `GET /api/agent/versions`(= `available_versions()` 노출)에 자동 반영 → **FE 셀렉터 자동 갱신·BE 검증 자동 수용(양쪽 코드 0 수정)**.
3. v3를 기본으로 승격하려면 코드가 아니라 env `AGENT_VERSION=v3`만 바꾼다(통제형 거버넌스).

## 계약 (버전 불변)

`stream_agent_turn(message, context)` 시그니처와 `done.data` 판별 유니온(type ∈
answer/analysis/recommendation/compact), SSE `ProgressEvent` 규약은 **모든 버전에서 동일**하다.
상세는 `docs/INTERFACES.md` §3·§4. 버전이 다른 것은 흐름도를 **어떻게 만드는가**(내부 그래프)일 뿐,
백엔드·프론트가 보는 입출력 계약은 같다.

## 환경변수 (.env)

| 키 | 필수 | 설명 |
|---|---|---|
| `OPENAI_API_KEY` | ✅ | OpenAI API 키 |
| `OPENAI_MODEL` | — | 챗 모델명 (기본 `gpt-5.4-mini`) |
| `AGENT_VERSION` | — | 요청에 `agent_version`이 없을 때의 기본 버전 (기본 `v2`) |

## 로컬 동작 확인

```bash
# 발견된 버전 목록 (레지스트리 자동탐색 확인)
python -c "from app.agent import available_versions; print(available_versions())"
# → [{'id':'v1','label':'v1 · 단계분해 매핑',...,'default':False},
#    {'id':'v2','label':'v2 · Agentic (ReAct)',...,'default':True}]
```

버전 선택 API·라이브 스모크(각 버전으로 `/turn` 호출)는 `docs/INTERFACES.md` §3와 RPA-167 PR 참조.
