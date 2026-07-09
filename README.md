# A360 Assistant Backend

업무정의서(PDF/PPTX)를 AI가 분석해 **Automation 360(A360) 자동화 작업(봇) 구성을 추천**하는 플랫폼의 백엔드입니다.

```
업로드 → 파싱(+비전 보강) → LLM+RAG 분석 → A360 액션 추천 → 챗봇 수정 → 명세 내보내기
```

| 영역 | 스택 |
|---|---|
| API | FastAPI + SSE(진행 상황 스트리밍) |
| DB | PostgreSQL 16 + pgvector (임베딩 검색) |
| RAG | A360 공식 문서·패키지 스키마·봇 예제 1,700여 건 임베딩 |
| LLM | OpenAI (비전 파싱·분석·추천), 사용량 자동 기록 |
| Agent | LangGraph 오케스트레이터 ([app/agent/README.md](app/agent/README.md)) |
| 배포 | Docker · GitHub Actions → AWS (CloudFormation, [infra/](infra/)) |

## 빠른 시작

**요구사항**: Python 3.11+, Docker Desktop

```bash
git clone https://github.com/Metanet-Final-01/A360-Assistant-Backend.git
cd A360-Assistant-Backend

# 1) 환경변수 — OPENAI_API_KEY만 채우면 시작 가능
cp .env.example .env

# 2) 의존성
pip install -r requirements.txt -r requirements-dev.txt

# 3) DB (pgvector Postgres)
docker compose up -d db
#    ⚠️ 로컬 5432 포트가 사용 중이면: .env에 DATABASE_PORT=5433 설정 후 다시 실행

# 4) RAG 지식베이스 복원 (임베딩 포함 덤프 — 팀 공유 채널에서 수령)
#    절차: app/rag/TEAM_SETUP.md 의 "경로 A"

# 5) 서버 실행 (기동 시 Alembic 마이그레이션이 head까지 자동 적용됨)
uvicorn app.main:app --reload
```

> **기존 DB(Alembic 도입 전에 만든 DB)를 쓰던 팀원**은 최초 1회만:
> `alembic stamp head` (이미 테이블이 있으니 현재 상태로 표시). 그 뒤부터는 `upgrade`가 알아서 처리합니다.

확인: http://localhost:8000/docs (Swagger UI)

전체를 컨테이너로 띄우려면: `docker compose up` (backend + db)

## 데이터베이스 마이그레이션 (Alembic)

스키마의 단일 진실 공급원은 `migrations/`의 Alembic 마이그레이션이다 (앱은 부팅 시 `upgrade head` 자동 실행).

```bash
# 모델(app/models.py)을 바꾼 뒤 마이그레이션 생성 (DB에 반영 X, 파일만 생성)
alembic revision --autogenerate -m "무엇을 바꿨는지"
# 생성된 migrations/versions/*.py를 반드시 검토한 뒤 적용
alembic upgrade head        # 최신까지 적용
alembic downgrade -1        # 한 단계 되돌리기
alembic current / history   # 현재 리비전 / 이력
```

- `rag_documents`는 `app/rag`가 원시 SQL(pgvector)로 관리하므로 Alembic 대상에서 제외돼 있다 (`migrations/env.py`).
- 컬럼 추가/변경 시 `create_all`처럼 조용히 누락되지 않고, 마이그레이션 파일로 이력이 남는다.

버전 이력 (`alembic history`로도 확인):

| 리비전 | 내용 |
|---|---|
| `0001` | 도메인 8개 테이블 최초 생성 (RPA-12) |
| `0002` | users 테이블 — 인증 (RPA-23) |
| `0003` | llm_usage 귀속 컬럼 actor_type/user_id/component (RPA-33) |
| `0004` | analysis_sessions.user_id — 세션 소유자 (RPA-40) |
| `0005` | audit_logs — 감사 로그 (횡단 관심사/AOP, RPA-49) |
| `0006` | analysis_sessions.solution — 에이전트 그래프·카탈로그 라우팅 키 (RPA-64) |
| `0007` | session_compacts — 대화 압축본 (RPA-66) |

> 도입 전에 만든 기존 DB는 최초 1회 `alembic stamp head --purge`로 현재 상태로 표시한다.

## 테스트

```bash
python -m pytest -q      # 또는 pytest -q
```

PR을 올리면 CI가 pytest·PR 제목 검사·시크릿 스캔·라벨링을 자동 실행합니다.

## API 개요

| 메서드/경로 | 설명 |
|---|---|
| `GET /api/health` | 헬스 체크 |
| `GET /api/rag/search?q=&limit=` | A360 액션/문서 벡터 검색 (FR-07) |
| `POST /api/documents` | 업무정의서 업로드 → 검증·저장만 하고 즉시 반환 (`status="uploaded"`). multipart `file`, 선택 `session_id` |
| `POST /api/documents/{id}/parse` | 업로드된 문서 파싱 (FR-02,04) — **SSE 스트림** (stage→done/error). 완료 시 `status="parsed"` |
| `POST /api/documents/text` | 자연어 업무 요청을 문서로 등록 — `{text, session_id?}`, `status="parsed"`로 즉시 반환(파싱 불필요) |
| `GET /api/documents/{id}` | 문서 메타·처리 상태 |
| `GET /api/documents/{id}/content` | 파싱 결과(구조화 JSON) — 분석 입력 |
| `POST /api/documents/{id}/enrich-vision` | 이미지 중심 페이지를 비전 LLM으로 보강 (FR-03) — **SSE 스트림** |
| `POST /api/sessions` | 빈 세션 생성 (문서 없이 챗 시작용) |
| `GET /api/sessions` | 내 세션 목록 (로그인 필수) |
| `GET /api/sessions/{id}` · `DELETE /api/sessions/{id}` | 세션 상세 / 삭제(CASCADE) |
| `GET /api/sessions/{id}/chat-messages` | 대화 이력 (아카이브·새로고침) |
| `GET /api/sessions/{id}/analyses` · `/analyses/latest` | 분석 목록(메타) / 최신 분석 전체 result 재조회 |
| `POST /api/sessions/{id}/turn` | **에이전트 단일 진입점** — 한 메시지로 분석/질문/흐름도 생성·수정, `operation="compact"`면 대화 압축 (반환 type으로 저장 분기) — **SSE 스트림** |
| `GET /api/sessions/{id}/recommendations` | 추천안 버전 목록 (undo·이력) |
| `GET /api/sessions/{id}/recommendations/latest` | 최신 추천안 트리 (흐름도 렌더용) |
| `POST /api/sessions/{id}/recommendations` | 편집한 추천안을 새 버전으로 저장 (FR-18 드래그 편집) |

> 전체 흐름: `POST /documents`(저장) → `/parse`(SSE) → `POST /sessions/{id}/turn`(SSE — 분석·추천(흐름도 생성)·질문·수정을 하나로) → 프론트가 트리를 블록으로 렌더·편집 → `POST /sessions/{id}/recommendations`(편집본 새 버전). 자연어는 `POST /documents/text`로 바로.
> 대화형(FR-05, 09~16): 분석·질문·흐름도 생성/수정·대화 압축을 **`POST /sessions/{id}/turn` 하나로** 처리한다 — 백엔드가 세션에서 full context(solution/operation/history/compact/analysis/recommendation/parsed_doc)를 조립해 에이전트에 넘기고, 에이전트가 `solution`으로 그래프를 골라 intent를 판단한다. 반환 `type`(answer/analysis/recommendation/compact)으로 백엔드가 저장을 분기한다. (레거시 `/analyze`·`/recommend`·`/api/agent/chat`은 이 진입점으로 흡수·제거됨, RPA-67)
> 흐름도 = `Recommendation` 트리(steps→actions→children). 수정은 UPDATE가 아니라 새 버전 INSERT라 undo·이력이 자연히 나온다.

- 에러 응답은 `{"detail": {"code": "...", "message": "사용자용 한글 메시지"}}` 형식
- 지원 형식: **PDF·PPTX·PPT·DOCX**. 표는 `{"type":"table","rows":[...]}`로 구조화 추출(PPTX·DOCX는 셀 단위, PDF는 pdfplumber). 레거시 `.ppt`는 LibreOffice(`soffice`)로 PPTX 변환 후 파싱 — 배포 이미지에 LibreOffice 필요(`LIBREOFFICE_PATH`로 경로 지정 가능), 없으면 파싱 단계에서 "PPTX로 저장 후 업로드" 안내
- 업로드 검증: 확장자·크기(기본 20MB)·매직바이트 위조·PDF 실행형 요소·OOXML(PPTX/DOCX) 매크로 차단

### SSE 소비 방법 (프론트)

시간이 걸리는 작업은 `ProgressEvent` 규약(stage→partial→done/error)으로 스트리밍됩니다.
POST 엔드포인트라 EventSource 대신 **fetch 스트리밍**을 사용합니다:

```js
const res = await fetch(`/api/documents/${id}/enrich-vision`, { method: "POST" });
const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
// 청크에서 "data: {...}" 라인을 파싱해 event 필드로 분기
```

이벤트 규약 상세: [docs/INTERFACES.md](docs/INTERFACES.md) §5

## 프로젝트 구조

```
app/
├── main.py          앱 조립 (CORS·lifespan·라우터)
├── api/             HTTP 라우터 (얇게 — 로직은 services로)
├── services/        비즈니스 로직: 업로드 검증·저장소(S3/로컬)·문서 파서(+비전)
├── core/            공용 인프라: LLM 래퍼 (토큰·비용·지연 → llm_usage 기록)
├── db.py, models.py DB 세션·ORM (세션/문서/분석/추천 버전/대화/피드백/사용량/평가)
├── schemas/         도메인 JSON 계약: AnalysisResult·Recommendation·ProgressEvent
├── agent/           LangGraph 오케스트레이터 (Agent 담당 영역)
└── rag/             RAG 수집·검색 (sources 수집, build 정규화, store 저장, retrieval 하이브리드 검색)
```

## 문서

| 문서 | 내용 |
|---|---|
| [docs/CONVENTIONS.md](docs/CONVENTIONS.md) | 브랜치·커밋·PR 컨벤션, 작업 흐름(AI 자동/수동 트랙) |
| [docs/INTERFACES.md](docs/INTERFACES.md) | 백엔드↔Agent 함수 계약, 산출물 JSON 스키마 가이드, SSE 규약 |
| [docs/RAG_CATALOG.md](docs/RAG_CATALOG.md) | 수집된 A360 패키지 57개·액션 368개 카탈로그 |
| [docs/JIRA_GITHUB.md](docs/JIRA_GITHUB.md) | Jira↔GitHub 자동화 연동 (이슈 미러·상태 전환) |
| [app/rag/README.md](app/rag/README.md) | RAG 수집·하이브리드 검색 사용법 |
| [AGENTS.md](AGENTS.md) | AI 코딩 도구용 작업 규칙 |

## 환경변수

`.env.example`에 전체 목록과 설명이 있습니다. 핵심만:

| 키 | 용도 |
|---|---|
| `OPENAI_API_KEY` | LLM·임베딩 (필수) |
| `DATABASE_*` | DB 접속 (로컬 기본값 제공) |
| `DOCUMENT_BUCKET` | 설정 시 업로드 파일을 S3에 저장 (미설정 시 로컬) |
| `VISION_MIN_TEXT_CHARS` / `VISION_MAX_PAGES` | 비전 파싱 비용 가드 |
| `LLM_INPUT_COST_PER_1M` / `LLM_OUTPUT_COST_PER_1M` | 설정 시 호출 비용(USD) 자동 계산 |
