# AGENTS.md — AI 에이전트 작업 가이드

이 리포는 "업무정의서 기반 A360 작업 추천 AI 플랫폼"의 백엔드다 (FastAPI + PostgreSQL/pgvector + RAG).
AI 도구(Claude Code, Cursor 등)로 이 리포에서 작업할 때 아래 규칙을 따른다.
상세 규칙: [docs/CONVENTIONS.md](docs/CONVENTIONS.md), Jira 연동: [docs/JIRA_GITHUB.md](docs/JIRA_GITHUB.md)

## Git 작업 규칙 (필수)

1. **main·dev에 직접 커밋 금지.** 작업 시작 전 현재 브랜치를 확인하고, main/dev면 반드시 **dev에서 새 브랜치를 분기**한 뒤 작업한다. PR은 **dev를 대상으로** 만든다 (main은 배포용).
2. **브랜치 이름**: `<type>/<Jira키>-<영문-요약>` (예: `feat/RPA-12-rag-summary-chunking`)
   - **Jira 키(RPA-N)를 모르면 임의로 지어내지 말고 사용자에게 물어본다.** 이슈 트래킹의 원본은 Jira이며, 브랜치는 Jira 이슈가 먼저 있어야 한다.
3. **커밋 메시지**: `<type>(<scope>): <한글 제목> (<Jira키>)`
   - type: `feat` `fix` `refactor` `docs` `test` `chore` `ci` `perf` `style`
   - scope(선택): `api` `rag` `ingest` `db` `agent` `parser` `chat` `export` `infra` `ci`
   - 제목 50자 이내, 본문에는 "왜"를 쓴다. 1 커밋 = 1 논리적 변경.
4. **커밋 전 자가 점검** (어긋나면 스스로 고친 뒤 진행):
   - [ ] 메시지가 위 형식을 따르는가? Jira 키가 포함됐는가?
   - [ ] diff에 시크릿(API 키, 비밀번호, 토큰)이나 `.env` 파일이 없는가? (`.env.example`만 허용)
   - [ ] 이번 변경과 무관한 파일이 섞여 있지 않은가?
   - [ ] 디버그 코드·임시 주석이 남아 있지 않은가?
5. **PR**: 제목은 커밋 컨벤션과 동일 형식(Squash merge 시 main 커밋 메시지가 됨).
   본문은 `.github/PULL_REQUEST_TEMPLATE.md` 구조를 따르고, GitHub 미러 이슈가 있으면 `Closes #번호`를 넣는다.
   미러 이슈 번호는 GitHub Issues에서 `[RPA-N]`으로 시작하는 이슈를 찾으면 된다.
6. **push 전 확인**: PR Title Lint와 Secret Scan 워크플로가 통과할 수 있는 상태인지 점검한다.

## 하지 말 것

- `main` 직접 push, `--force` push (작업 브랜치의 `--force-with-lease`만 허용)
- `.env` 커밋, 시크릿 하드코딩 (환경변수는 `.env.example`에 키 이름만 추가)
- 사용자 확인 없이 커밋·push·PR 생성 — **커밋/PR 직전에 컨벤션 준수 여부를 요약해서 보여주고 진행한다**
- Jira 키 임의 생성, 컨벤션에 안 맞는 브랜치/커밋을 "일단" 만들기

## 담당 영역 (다른 사람 폴더 건드리지 않기)

| 폴더 | 담당 | AI 작업 가능 여부 |
|---|---|---|
| `app/` (agent 제외), `app/ingest/` | 백엔드·DB·RAG | ✅ 주 작업 영역 |
| `app/agent/` (예정) | Agent/LLM 담당 팀원 | ⚠️ 인터페이스 합의 없이 수정 금지 |
| `infra/`, `.github/workflows/backend-deploy.yml` | DevOps 담당 팀원 | ⚠️ 수정 시 담당자 리뷰 필수 |
| `.github/` (배포 워크플로 제외), `docs/` | 공용 (컨벤션 관리) | ✅ |

## 개발 환경 참고

- 로컬 DB: `docker compose up -d db` — pgvector Postgres. 로컬에서 5432 포트가 점유된 경우 `DATABASE_PORT=5433` 사용
- RAG 수집 파이프라인: `app/ingest/` (Fluid Topics API 기반, 상세는 해당 폴더 README)
- Python 3.11 / FastAPI. 의존성 추가 시 `requirements.txt` 갱신
