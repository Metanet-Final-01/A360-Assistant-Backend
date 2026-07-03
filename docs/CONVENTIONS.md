# 협업 컨벤션

> 심사위원이 리포지토리를 직접 확인하는 것을 전제로, 히스토리·PR·이슈가 "읽히는 기록"이 되도록 관리한다.

## 1. 브랜치 전략

4주 단기 프로젝트이므로 **GitHub Flow** (main + 작업 브랜치)를 사용한다. develop 브랜치는 두지 않는다.

```
main ← 항상 배포 가능한 상태 (브랜치 보호, 직접 push 금지)
 └── feat/RPA-12-rag-summary-chunking   ← 작업 브랜치 (PR로만 머지)
```

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
- 브랜치는 머지 후 삭제한다 (리포 설정에서 자동 삭제 활성화).

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

- **PR 제목은 커밋 컨벤션과 동일한 형식** — Squash merge 시 PR 제목이 main의 커밋 메시지가 되므로 제목 규칙이 곧 main 히스토리 품질이다.
- 머지는 **Squash merge만** 사용한다 (main 히스토리 = PR 단위의 깔끔한 한 줄).
- 본문은 PR 템플릿(`.github/PULL_REQUEST_TEMPLATE.md`)을 따른다.
- **리뷰어 최소 1인 승인** 후 머지. 본인 승인으로 본인 PR 머지 금지.
- 리뷰 요청 전 스스로 diff를 한 번 훑는다 (디버그 코드, 주석, .env 잔재 제거).
- 작업 중 공유가 필요하면 **Draft PR**로 올린다.
- PR 크기는 리뷰 가능한 수준으로 유지 (대략 ±500줄 이내 권장, 넘으면 분할 고려).

## 4. 이슈·라벨

- 이슈 트래킹의 원본(source of truth)은 **Jira**. GitHub 이슈는 Jira Automation으로 동기화된다 (`docs/JIRA_GITHUB.md` 참고).
- 라벨 세트:

| 라벨 | 용도 |
|---|---|
| `area:backend` `area:rag` `area:agent` `area:frontend` `area:infra` | 담당 영역 |
| `priority:P0` `priority:P1` `priority:P2` | 우선순위 (P0=필수 연계, P1=가점 효율, P2=차별화) |
| `type:feat` `type:bug` `type:docs` | 성격 |
| `from-jira` | Jira Automation이 생성한 이슈 |

## 5. 담당 영역

| 영역 | 폴더 | 담당 |
|---|---|---|
| 백엔드 API·DB·RAG | `app/` (agent 제외), `app/ingest/` | 백엔드 담당 |
| Agent·LLM | `app/agent/` (예정) | AI 담당 |
| 인프라·CI/CD | `infra/`, `.github/workflows/backend-deploy.yml` | DevOps 담당 |

다른 사람 영역을 수정해야 하면 PR에 해당 담당자를 리뷰어로 지정한다.

## 6. 금지 사항

- `main` 직접 push
- 시크릿(API 키, 비밀번호) 커밋 — `.env`는 절대 커밋하지 않고 `.env.example`만 갱신
- `--force` push (본인 작업 브랜치에서 rebase 후는 `--force-with-lease` 허용)
- 리뷰 없는 머지
