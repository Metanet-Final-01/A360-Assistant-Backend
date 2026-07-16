# 협업 컨벤션

> 심사위원이 리포지토리를 직접 확인하는 것을 전제로, 히스토리·PR·이슈가 "읽히는 기록"이 되도록 관리한다.

## 1. 브랜치 전략

**main + dev 2단 구조**를 사용한다.

```
main ← 배포(릴리스) 브랜치. dev에서 검증된 것만 머지 (직접 push 금지)
 └── dev ← 통합 브랜치. 모든 작업 PR의 대상 (직접 push 금지)
      └── feat/RPA-12-rag-summary-chunking ← 작업 브랜치 (dev에서 분기)
```

- 작업 브랜치는 **dev에서 분기**하고, PR도 **dev로** 보낸다.
- `dev → main` 머지는 배포 시점에 DevOps 담당과 협의하여 진행한다.

### 브랜치 네이밍

```
<type>/<Jira키>-<영문-요약>
```

| 예시 | 용도 |
|---|---|
| `feat/RPA-12-rag-summary-chunking` | 기능 개발 |
| `fix/RPA-31-pdf-parse-order` | 버그 수정 |
| `refactor/RPA-40-split-routers` | 리팩터링 |
| `docs/RPA-7-api-spec` | 문서 |
| `chore/RPA-3-labels-setup` | 설정/잡무 |

- Jira 키가 브랜치명에 있으면 Jira 개발 패널에 자동 연결된다 (GitHub for Jira 앱 설치 시).
- 브랜치는 머지 후에도 삭제하지 않고 남겨둔다 (진행 이력 보존).

## 2. 커밋 컨벤션

[Conventional Commits](https://www.conventionalcommits.org/) 형식 + **한글 제목** (기존 히스토리 스타일 유지).

```
<type>(<scope>): <한글 제목> (<Jira키>)

<본문: 무엇을이 아니라 "왜"를 쓴다. 선택>
```

예시:

```
feat(rag): 액션 요약본 동시 청킹 저장 추가 (RPA-12)

업무 단계 서술과 액션 문서 간 어휘 격차로 검색 재현율이 낮아,
원문 청크 외에 LLM 생성 요약 청크를 별도 인덱싱한다.
```

### type

| type | 용도 |
|---|---|
| `feat` | 기능 추가 |
| `fix` | 버그 수정 |
| `refactor` | 동작 변경 없는 구조 개선 |
| `docs` | 문서만 변경 |
| `test` | 테스트 추가/수정 |
| `chore` | 빌드/설정/잡무 |
| `ci` | CI/CD 파이프라인 |
| `perf` | 성능 개선 |
| `style` | 포맷팅 (동작 무관) |

### scope (선택, 담당 영역 기준)

`api` `rag` `ingest` `db` `agent` `parser` `chat` `export` `infra` `ci`

### 규칙

- 제목 50자 이내, 마침표 없이
- 1 커밋 = 1 논리적 변경 (파싱 수정과 DB 스키마 변경을 한 커밋에 넣지 않기)
- Jira 키를 커밋 메시지에 포함하면 Jira에 자동 링크 + 스마트 커밋 사용 가능
  - 스마트 커밋 예: `fix(parser): 표 셀 순서 보정 (RPA-31) #comment 좌표 기반 정렬로 변경 #done`

## 3. PR 컨벤션

- **PR 제목은 커밋 컨벤션과 동일한 형식**으로 쓴다.
- 머지는 **Merge commit** 방식을 사용한다 — 작업 브랜치의 개별 커밋이 dev 히스토리에 그대로 보존된다. 따라서 **개별 커밋 메시지도 전부 컨벤션을 지켜야 한다** (`wip`, `수정` 같은 커밋을 남기지 않기).
- 본문은 PR 템플릿(`.github/PULL_REQUEST_TEMPLATE.md`)을 따른다.
- **리뷰어 최소 1인 승인** 후 머지. 본인 승인으로 본인 PR 머지 금지.
- 리뷰 요청 전 스스로 diff를 한 번 훑는다 (디버그 코드, 주석, .env 잔재 제거).
- 작업 중 공유가 필요하면 **Draft PR**로 올린다.
- PR 크기는 리뷰 가능한 수준으로 유지 (대략 ±500줄 이내 권장, 넘으면 분할 고려).

## 4. 이슈·라벨

- 이슈 트래킹의 원본(source of truth)은 **Jira**. GitHub 이슈는 Jira Automation으로 동기화된다 (`docs/JIRA_GITHUB.md` 참고).
- 라벨 세트 (한글 기본, 약어는 영어 유지):

| 라벨 | 용도 | 부여 방식 |
|---|---|---|
| `백엔드` `RAG` `에이전트` `DB` `인프라` `CI` `문서` `프론트` | 담당 영역 | **PR은 자동** — labeler가 변경 파일 경로 기준 부여 (`.github/labeler.yml`) |
| `P0` `P1` `P2` | 우선순위 (P0=필수 연계, P1=가점 효율, P2=차별화) | 수동 (필요 시) |
| `기능` `버그` | 이슈 성격 | 이슈 템플릿이 자동 부여 |
| `from-jira` | Jira Automation이 생성한 미러 이슈 표시. **이슈 전용 — PR에는 붙이지 않는다** (PR은 제목의 RPA-키가 이미 Jira 연결을 의미) | 자동 (Jira 규칙이 부여 — 이름 변경 시 Jira Automation 규칙도 함께 수정 필요) |

- **PR에 영역 라벨을 수동으로 붙이지 않는다** — 여러 영역을 건드리면 여러 개가 붙는 것이 정상이며, 오히려 PR 분리를 고민할 신호다.

## 5. 담당 영역

| 영역 | 폴더 | 담당 |
|---|---|---|
| 백엔드 API·DB·RAG | `app/` (agent 제외), `app/rag/` | 백엔드 담당 |
| Agent·LLM | `app/agent/` (예정) | AI 담당 |
| 인프라·CI/CD | `infra/`, `.github/workflows/backend-deploy.yml` | DevOps 담당 |

다른 사람 영역을 수정해야 하면 PR에 해당 담당자를 리뷰어로 지정한다.

## 6. 금지 사항

- `main`·`dev` 직접 push
- 시크릿(API 키, 비밀번호) 커밋 — `.env`는 절대 커밋하지 않고 `.env.example`만 갱신
- `--force` push (본인 작업 브랜치에서 rebase 후는 `--force-with-lease` 허용)
- 리뷰 없는 머지
- **공유 DB에 pytest 실행 / TRUNCATE·리셋** — 아래 §7 참고

## 7. 공유 DB 규약 (RPA-90 / RPA-132 / RPA-186)

DB가 셋이다. **공유는 팀원이 실시간으로 쓰고 있다 — 파괴적 작업 금지.**

| DB | env | 기본 |
|---|---|---|
| 앱 (계정·세션·대화·추천) | `APP_DATABASE_URL` | **미설정 = 로컬 docker** |
| 관측 (audit_logs·llm_usage) | `OBSERVABILITY_DATABASE_URL` | 공유 Neon |
| RAG 코퍼스 | `RAG_DATABASE_URL` | 공유 Neon |

### Alembic — 공유 앱 DB를 쓸 때

공유 DB는 `alembic_version` 한 줄이 **팀 전체에 하나**뿐이다. 각자 올리면 이렇게 깨진다:

- 두 사람이 각자 리비전을 만들면 head가 둘 → `Multiple head revisions`로 **전원 기동 불가**
- `NOT NULL` 컬럼이 추가되면 그 컬럼을 모르는 팀원 코드의 INSERT가 깨진다
- 리뷰에서 까인 마이그레이션도 이미 적용돼 있고, 팀원 데이터가 위에 쌓여 되돌리기 어렵다

**코드가 막는다 (규약에 기대지 않는다).** `app/db.py`의 `run_migrations()`는 앱 기동 때마다
도는데(`app/main.py`), `APP_DATABASE_URL`이 있으면 **자동 적용을 건너뛰고 경고만** 낸다.

> 왜 규약으로 안 되나: 마이그레이션은 사람이 결정해서 실행하는 게 아니라 **서버 띄우면 자동**
> 으로 일어난다. 팀원이 브랜치 바꾸고 `uvicorn` 한 번 띄우는 순간 공유 스키마가 그 브랜치
> head로 올라간다. 자동으로 벌어지는 일은 사람 규율로 못 막는다.

공유 DB에 적용하는 유일한 경로 — **`dev` 머지 후 한 명이**:

```bash
python scripts/migrate_shared_db.py          # 무엇이 적용될지 확인 (dry-run)
python scripts/migrate_shared_db.py --apply  # 적용 후 팀에 공지
```

1. 적용 전 체크아웃이 **`dev`이고 최신**인지 확인한다 (스크립트가 브랜치를 찍어준다).
2. 적용 후 **팀에 공지** — 다른 사람은 pull 해야 코드와 스키마가 맞는다.
3. **스키마를 만드는 중엔 토글을 끄고 로컬**에서 작업한다 — 로컬은 기동 시 자동 적용이라
   평소처럼 쓰면 된다.

### 오염 방지 — "바라지 않고 확인한다"

격리는 **메커니즘 + 검증**을 쌍으로 둔다. 메커니즘만 있으면 조용히 깨진다(실제로 2회 겪음).

- **pytest**: `tests/conftest.py`가 import **전에** `APP_DATABASE_URL`을 제거하고,
  `_assert_app_db_is_local`이 engine이 정말 로컬인지 fail-closed로 확인한다.
  ⚠️ 앱 DB는 `app/db.py`가 **import 시점에 engine을 만든다** — 관측·RAG처럼 fixture에서
  `delenv` 하는 방식은 **통하지 않는다**(이미 커넥션이 열린 뒤다).
- **live 기동**: `manage.ps1`이 `scripts/check_smoke_isolation.py`로 검사한다.
  env가 아니라 **`engine.url.host`를 본다** — "env가 비었다"와 "engine이 로컬을 본다"는
  다른 명제이고, 물어야 할 건 후자다.

가드를 추가·수정하면 **고의로 깨뜨려 빨간불을 확인**할 것. 안 깨지면 그건 가드가 아니다
(2026-07-15: `load_dotenv` 미실행으로 **항상 LOCAL이라 답하던 가짜 가드**를 실제로 잡았다).

## 8. 작업 흐름 — 트랙 A(AI 자동) / 트랙 B(수동)

팀원마다 Claude Code 사용 여부가 다르므로 두 트랙을 모두 지원한다.
**결과물 규칙(브랜치명·커밋·PR·리뷰)과 Jira Automation(미러 생성·상태 전환)은 두 트랙에서 완전히 동일하다.**

### 트랙 A: Claude Code 사용 (Atlassian MCP 연동)

1. Claude에게 자연어로 작업을 요청한다 (예: "RAG 검색에 하이브리드 검색 추가해줘")
2. Claude가 Jira 이슈 생성 → dev에서 브랜치 분기 → 구현·커밋 → PR 생성까지 수행한다
3. 사람은 **PR 리뷰와 머지만** 담당한다 (승인 1인 규칙 동일)

최초 1회 설정: `claude mcp add --transport http atlassian https://mcp.atlassian.com/v1/mcp` 등록 후, 대화형 세션에서 `/mcp` → atlassian → 본인 Atlassian 계정으로 인증.

### 트랙 B: 수동 작업 (Claude Code 없이)

1. Jira에서 **"작업" 이슈 생성** (담당자 본인 지정)
2. 이슈 화면 오른쪽 **개발 패널 → "브랜치 만들기"** (또는 로컬: `git checkout dev && git pull && git checkout -b feat/RPA-N-영문요약`)
3. 작업·커밋 (2장 커밋 컨벤션 준수) → push
4. GitHub에서 PR 생성 — **base: `dev`**, 템플릿 작성, 본문에 `Closes #미러이슈번호` (미러 번호는 GitHub Issues에서 `[RPA-N]`으로 검색)
5. 리뷰 승인 후 **Create a merge commit**으로 머지

Jira 상태는 손대지 않아도 된다 — 브랜치 생성 시 "진행 중", PR 머지 시 "완료"로 자동 전환된다.

### 공통 주의

- **GitHub 이슈를 직접 만들지 않는다** — Jira에서 이슈를 만들면 Automation이 GitHub 미러(`[RPA-N] ...`, `from-jira` 라벨)를 자동 생성한다
- GitHub 미러 이슈를 직접 수정·닫지 않는다 (PR의 `Closes #`로 닫히는 것은 예외)

## 9. AI 생성 코드 감사·리뷰 규약

이 저장소는 상당 부분이 AI(Claude/Cursor 등)로 작성됐다. **AI 코드는 사람과 다르게 틀린다** — 사람은 국소 오류(off-by-one, null 참조)를 내지만 AI는 **정합성(coherence) 오류**를 낸다: 그럴듯하지만 없는 API, 써놨지만 안 물린 config, 예전 설계를 설명하는 문서, 미묘하게 갈린 중복 로직, "있어 보여서" 덧댄 과잉 추상화. 따라서 리뷰는 *국소 정확성*이 아니라 **층간 정합성·죽은 참조**를 겨눈다.

### 리뷰 체크리스트 (AI가 만들거나 수정한 코드에 적용)

- [ ] **죽은 config 양방향 확인** — 이 변경이 추가/참조하는 env·설정이 (a) 실제로 코드에 읽히는가, (b) `.env.example`·infra에 문서화됐는가. *한쪽만 있으면 죽은 참조.* (실사례: `AGENT_RETRIEVER`는 `.env`·infra에 있으나 코드가 안 읽는 죽은 스위치)
- [ ] **폐기 API 부활 금지** — 문서·README·주석·예시에 **실재하지 않는 심볼/엔드포인트**가 없는가. AI는 낡은 문서를 근거로 없는 API를 재생성한다. (실사례: `app/agent/README.md`의 `run_agent`·`/api/agent/chat` — 실제 진입점은 `/api/sessions/{id}/turn`)
- [ ] **live로 증명됨** — "코드가 그렇게 보인다"가 아니라 **실제로 그렇다**를 확인했는가: 서버 띄워 호출했는가? DB에 쌓였는가? 응답이 문서와 같은가? *스텁/Fake로만 통과한 경로는 별도 live smoke가 필요하다.*
- [ ] **예외 삼킴 분류** — 새 `except`가 관측 실패 무시(로그+사유주석 필수)인지 장애 은폐인지. 조용한 `pass`·빈 `except`는 사유를 남긴다.
- [ ] **복잡도 래칫 감시** — 100줄 넘는 핵심 함수는 "AI가 계속 덧댄 중심부"일 확률이 높다. 헬퍼/서비스로 분리하되, *다른 곳으로 옮기기만 한 리팩터*(예: 큰 함수→큰 클로저)는 감소가 아니다.
- [ ] **중복-발산 확인** — 같은 개념을 재사용하지 않고 두 번 생성하지 않았는가(sync/async 쌍 등). 불가피하면 공유 코어를 두고 "한쪽 고치면 다른 쪽도"를 주석·테스트로 묶는다.
- [ ] **provenance** — 이 PR/커밋이 제목·Jira가 *주장한 것*을 실제로 구현하는가(범위 초과·미달 없이).

### 메타 규칙: AI에겐 "수정"이 아니라 "증명"을 요구한다

AI에게 감사·수정을 시킬 때는 결과 단언("고쳤습니다")을 믿지 말고 **증거**를 요구한다 — "그 API가 실제로 있나?", "서버에 띄워 호출했나?", "DB에 쌓인 걸 쿼리로 보여줘", "문서와 응답이 같은지 대조했나". 범위 밖·교차 관심사 발견은 그 PR에 섞지 말고 **별도 이슈로 분리**한다.

> 이 규약은 2026-07-14 거버넌스 감사에서 실제로 잡힌 정합성 부채(죽은 env, 폐기 심볼 문서, BM25 무음 degrade, 비용 request_id 단절)를 근거로 한다.
