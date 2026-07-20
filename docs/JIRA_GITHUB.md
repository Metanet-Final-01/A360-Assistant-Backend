# Jira ↔ GitHub 연동 가이드

> ✅ **2026-07-03 설정 완료** — Jira: `metanetfinal.atlassian.net` / 프로젝트 키: `RPA` / 아래 규칙 A~D 전부 활성화됨.
> 이 문서는 재설정·트러블슈팅용 기록이다.

목표 동작:

1. Jira에서 이슈를 만들면 → GitHub 이슈가 자동 생성된다
2. Jira에서 이슈를 완료(Done)하면 → GitHub 이슈가 자동으로 닫힌다
3. 브랜치/커밋/PR에 Jira 키(`RPA-12`)를 넣으면 → Jira 이슈의 "개발" 패널에 자동 링크된다
4. PR이 머지되면 → Jira 이슈가 자동으로 Done으로 전환된다 (2번의 역방향)

> **선행 결정**: 이슈를 Jira/GitHub 양쪽에 두면 동기화가 어긋날 수 있다.
> 원본(source of truth)은 항상 **Jira**로 하고, GitHub 이슈는 "심사위원에게 보여주기 위한 미러"로만 취급한다.
> GitHub 이슈에서 직접 수정/닫기 하지 않는다 (PR 머지로 닫히는 것 제외).

---

## 1단계. GitHub for Jira 앱 설치 (3번·4번 동작의 기반)

1. Jira 관리자 권한으로 [Atlassian Marketplace — GitHub for Jira](https://marketplace.atlassian.com/apps/1219592/github-for-jira) 설치
2. Jira 설정 → 앱 → GitHub → **Connect GitHub organization** → `Metanet-Final-01` 조직 연결 (조직 owner 승인 필요)
3. 연결 후:
   - 브랜치명/커밋 메시지/PR 제목에 `RPA-12`가 있으면 해당 Jira 이슈의 **개발 패널**에 자동 표시
   - **스마트 커밋** 사용 가능: `fix(parser): 표 순서 보정 (RPA-31) #comment 좌표 정렬로 변경 #done`
     - `#comment <내용>` — Jira 이슈에 코멘트 추가
     - `#time 2h` — 작업 시간 기록
     - `#done` — 이슈를 Done으로 전환 (전환명이 팀 보드와 일치해야 함)

## 2단계. Jira Automation — 브랜치/PR 이벤트로 상태 자동 전환 (4번)

Jira 프로젝트 → **Project settings → Automation** 에서 규칙 2개 생성:

**규칙 A: 브랜치 생성 → In Progress**
- Trigger: `Branch created`
- Action: `Transition issue` → In Progress

**규칙 B: PR 머지 → Done**
- Trigger: `Pull request merged`
- Action: `Transition issue` → Done

(GitHub for Jira 앱이 연결되어 있어야 이 트리거들이 동작한다)

## 3단계. Jira Automation — GitHub 이슈 자동 생성/닫기 (1번·2번)

### 준비: GitHub 토큰

1. GitHub → Settings → Developer settings → **Fine-grained personal access token** 생성
   - Repository access: `Metanet-Final-01/A360-Assistant-Backend` (프론트 리포도 쓰면 함께)
   - Permissions: **Issues → Read and write**
   - 만료: 프로젝트 종료일 이후로 설정
2. 팀 봇 계정으로 만드는 것을 권장 (개인 계정 토큰이면 퇴장 시 규칙이 죽는다)

### 준비: Jira 커스텀 필드

Jira 관리 → 필드 → **커스텀 필드 추가**: `GitHub Issue Number` (숫자 또는 짧은 텍스트 — 현재 텍스트 타입으로 생성됨).
생성된 GitHub 이슈 번호를 여기 저장해서 나중에 닫을 때 사용한다.
필드 생성 후 **화면(스크린)에 연결**해야 이슈와 Automation에서 보인다.

### 규칙 C: Jira 이슈 생성 → GitHub 이슈 생성

- Trigger: `Issue created`
- Action 1: `Send web request`
  - URL: `https://api.github.com/repos/Metanet-Final-01/A360-Assistant-Backend/issues`
  - Method: `POST`
  - Headers:
    - `Authorization`: `Bearer <GitHub 토큰>`
    - `Accept`: `application/vnd.github+json`
  - Body (Custom data):
    ```json
    {
      "title": "[{{issue.key}}] {{issue.summary}}",
      "body": "Jira: {{issue.url}}\n\n{{issue.description}}",
      "labels": ["from-jira"]
    }
    ```
  - ✅ **"Delay execution of subsequent rule actions until we've received a response"** 체크
- Action 2: `Edit issue` → 필드 `GitHub Issue Number` = `{{webResponse.body.number}}`

### 규칙 D: Jira 이슈 Done → GitHub 이슈 닫기

- Trigger: `Issue transitioned` → to `Done`
- Condition: `GitHub Issue Number` 필드가 비어있지 않음
- Action: `Send web request`
  - URL: `https://api.github.com/repos/Metanet-Final-01/A360-Assistant-Backend/issues/{{issue.GitHub Issue Number}}`
  - Method: `PATCH`
  - Headers: 규칙 C와 동일
  - Body:
    ```json
    { "state": "closed", "state_reason": "completed" }
    ```

> 참고: 스마트 밸류 `{{issue.GitHub Issue Number}}`는 커스텀 필드 이름 그대로 쓰되,
> 동작하지 않으면 `{{issue.customfield_10xxx}}` (필드 ID)로 바꾼다.
> 필드 ID는 Jira 관리 → 필드 → 해당 필드 → "필드 ID 보기"에서 확인.

### (선택) 규칙 E: PR 머지로 GitHub 이슈도 닫기

PR 본문에 `Closes #<번호>`를 쓰면 GitHub이 머지 시 자동으로 이슈를 닫는다.
규칙 D와 중복 실행돼도 무해하다 (이미 닫힌 이슈 PATCH는 no-op).

---

## 운영 흐름 요약

```
Jira 이슈 생성 (RPA-12)
  └→ [규칙 C] GitHub 이슈 #34 자동 생성
브랜치 생성: feat/RPA-12-rag-summary-chunking
  └→ [규칙 A] Jira: In Progress
커밋: "feat(rag): 요약 청킹 추가 (RPA-12)"
  └→ Jira 개발 패널에 커밋 링크
PR 생성 (제목에 RPA-12, 본문에 Closes #34)
  └→ Jira 개발 패널에 PR 링크
PR 머지
  ├→ [규칙 B] Jira: Done
  ├→ [규칙 D] GitHub 이슈 #34 닫힘
  └→ GitHub 네이티브: Closes #34로도 닫힘
```

## 트러블슈팅

- **웹 요청 401/403**: 토큰 만료 또는 권한 부족 (Issues write 확인). Automation → Audit log에서 응답 코드 확인
- **개발 패널에 안 뜸**: 브랜치/커밋에 Jira 키 대소문자 정확히 (`RPA-12`, 소문자 `rpa-12`는 인식 안 될 수 있음)
- **스마트 커밋 #done 안 먹힘**: 워크플로 전환명이 "Done"인지 확인. 전환명이 "완료"면 `#완료`
