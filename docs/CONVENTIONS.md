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
| 백엔드 API·DB·RAG | `app/` (agent 제외), `app/ingest/` | 백엔드 담당 |
| Agent·LLM | `app/agent/` (예정) | AI 담당 |
| 인프라·CI/CD | `infra/`, `.github/workflows/backend-deploy.yml` | DevOps 담당 |

다른 사람 영역을 수정해야 하면 PR에 해당 담당자를 리뷰어로 지정한다.

## 6. 금지 사항

- `main`·`dev` 직접 push
- 시크릿(API 키, 비밀번호) 커밋 — `.env`는 절대 커밋하지 않고 `.env.example`만 갱신
- `--force` push (본인 작업 브랜치에서 rebase 후는 `--force-with-lease` 허용)
- 리뷰 없는 머지

## 7. 작업 흐름 — 트랙 A(AI 자동) / 트랙 B(수동)

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
