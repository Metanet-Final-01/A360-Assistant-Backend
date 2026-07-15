# Phase 0 보증 계약 v1.10

> 상태: `approve_for_human_decision` 이후 사람 결정을 반영한 구현 기준
>
> 범위: 계약·정책·결정론적 fixture의 원본 보존과 재검증
>
> 비범위: 제품 코드 수정, 운영 DB 쓰기, 실제 LLM 호출, required CI, 운영 차단

## 1. 세 줄 요약

1. AI가 만든 코드와 AI 출력이 틀릴 수 있다는 전제로 서로 다른 세 경계에 하네스를 설계했다.
2. v1.1부터 v1.10까지 독립 검토에서 발견된 fail-open 결함을 반복 보정하고 Phase 0를 종료했다.
3. 현재 저장된 것은 **계약과 참조 구현**이며 실제 Backend·CI 강제는 후속 Jira에서 구현한다.

처음 보는 독자는 [버전별 설계 변화](VERSION_HISTORY.md)를 먼저 읽으면 왜 문서와 검증 원본이
이 정도로 상세한지 빠르게 이해할 수 있다.

## 2. 무엇이 실제로 들어 있는가

| 구분 | 경로 | 역할 | 운영 코드 여부 |
|---|---|---|---|
| 현재 설명 | `README.md` | 상태, 경계, 다음 구현 순서 | 아니오 |
| 변화 기록 | `VERSION_HISTORY.md` | v1.1~v1.10 발견·보정·판정 요약 | 아니오 |
| 채택 결정 | `decisions/adoption.json` | 사람 결정과 rollout의 기계 판독 원본 | 정책 입력 후보 |
| 결정 스키마 | `decisions/adoption.schema.json` | 잘못된 채택 상태를 거부 | 검증 계약 |
| 검증 기록 | `evidence/verification.md` | 실행 환경, 명령, 결과와 한계 | 아니오 |
| 동결 원본 | `evidence/frozen/` | Claude 계약, Codex 리뷰, 참조 코드와 fixture | 참조 구현 |
| 무결성 목록 | `evidence/artifact-manifest.sha256` | 동결 원본과 채택 결정의 SHA-256 | 증거 |

`evidence/frozen/` 아래의 파일명과 내부 코드는 당시 검토한 원본을 보존하기 위해 고치지 않는다.
사람이 읽는 이름과 구조는 바깥 계층에서 정리하고, 원본의 역할은
[증거 안내](evidence/README.md)에 한국어로 설명한다.

## 3. 기준선과 사실 라벨

| 구분 | commit | 의미 |
|---|---|---|
| 역사적 감사 기준선 | `ed1a1b0d62452f75212da4f78f3e8b9f990da42e` | 초기 감사에서 직접 재현한 사실 |
| 계약 작성 기준선 | `d66fce1840c1dcf907c9d303bbc64654c7c41857` | v1.10 계약이 평가한 당시 `origin/dev` |
| 저장소 편입 기준선 | `7d2561819eba107a6a5ffcc32f5420a955dd7028` | RPA-178 시작 시점의 `origin/dev` |

- **Baseline-Confirmed**: 위 고정 SHA 중 명시된 기준선에서 재현했다.
- **Current-Confirmed**: 저장소 편입 시점에 다시 직접 확인했다.
- **Not-tested**: 아직 실행하거나 관찰하지 않았다.
- **Decision-needed**: 기술만으로 정하지 않고 사람에게 남겼다.

`d66fce1` 이후 `7d25618`까지 PR #239, #242, #243이 병합됐다. 예산 가드레일과 런타임
오버라이드 관련 변경은 읽기 전용으로 대조했지만, 해당 제품 동작을 이 패키지에서 실행 검증했다고
주장하지 않는다. 미병합 PR #249의 Agent v3도 `Planned`일 뿐 현재 사실이 아니다.

## 4. 세 하네스의 책임

| 하네스 | 검사 대상 | 검출 예시 | 목표 차단 지점 |
|---|---|---|---|
| Change Assurance | AI 보조 코드 변경 | 누락된 테스트, 의존성·계약 drift, 자기신고형 증거 | 보호된 GitHub Actions와 병합 정책 |
| Backend Output Boundary | 공개 Agent 출력과 사용자 편집 후보 | schema·catalog·버전·증거 불일치 | 저장·latest·history·export 공통 경계 |
| Evidence & Governance | 앞선 판정의 입력과 실행 맥락 | 영수증 위조, 정책 변경, 증거 불완전성 | 보호된 단일 writer와 읽기 전용 감사 조회 |

세 하네스는 하나의 거대한 AI 검토기가 아니다. 서로 다른 실패 경계에 독립적인 결정론적 통제를
두고, 강제 단계에서는 필요한 증거가 없으면 진행을 허용하지 않는 fail-close를 목표로 한다.

## 5. 사람이 채택한 운영 결정

기계 판독 정본은 [decisions/adoption.json](decisions/adoption.json)이다.

| ID | 채택 내용 | 이유 | 구현 Jira |
|---|---|---|---|
| D-16 | 보호된 GitHub Actions를 영수증 단일 writer로 사용, 서명은 후순위 | 애플리케이션이 스스로 통과 영수증을 만들지 못하게 함 | RPA-183 |
| D-17 | 정책 경로에 CODEOWNERS, 브랜치 보호, 2인 리뷰 | 코드와 판정 기준의 동시 약화를 막음 | RPA-183 |
| D-21 | 공개 응답에 `resolved_agent_version` 요청 | Backend가 Agent 내부를 읽지 않고 실제 실행 버전을 확인 | RPA-184 |
| D-22 | 승인 버전 목록은 빈 상태로 시작 | 발견 가능한 버전과 운영 승인 버전을 분리 | RPA-184 |
| D-23 | Observe는 기록, Enforce는 저장·조회·export 차단 | 측정 단계와 최종 fail-close를 구분 | RPA-181 |
| D-24 | 버전은 세션 고정이 아니라 매 턴 선택 | RPA-176·177의 v1/v2/v3 사용자 선택 계약 보존 | RPA-184 |

D-24의 목표 불변식은 다음과 같다.

1. 각 턴은 요청 버전 또는 공개 기본값 규칙을 따른다.
2. 같은 턴의 자동 compact와 본 처리는 같은 요청 버전을 사용한다.
3. 다음 턴에는 다른 승인 버전을 다시 선택할 수 있다.
4. 요청 버전과 실제 `resolved_agent_version`을 함께 기록한다.
5. 알 수 없거나 미승인인 버전으로 조용히 대체하지 않는다.

현재 기준선에서 발견된 Agent는 v1과 v2다. v3는 공개 계약이 실제 병합되고 검증·승인된 뒤에만
선택지에 포함한다. 담당자 실명, 시행일, 실제 승인 버전과 서명 도입 시점은 별도 결정이 필요하다.

## 6. Agent 담당 영역과의 경계

- Backend가 변경할 수 있는 범위: 공개 요청·응답 스키마, 저장 전 검증, 판정 영속화,
  latest/history/export 공통 차단, 증거 조회.
- Agent 담당자에게 요청할 범위: 공개 `resolved_agent_version` 필드와 버전 계약 합의.
- 변경하지 않는 범위: `app/agent/**`의 prompt, graph, model, tool, repair, retry, eval.
- 결함 주입은 deterministic fixture와 test-only adapter로만 수행한다.
- production 공격용 endpoint나 flag는 만들지 않는다.

## 7. 검증된 것과 검증되지 않은 것

동결된 계약 패키지에서 다음 결과를 재현했다.

| 검사 | 결과 | 의미 |
|---|---|---|
| 계약 self-test | 66/66, coverage PASS | fixture 기반 계약 검사 |
| 실행 원본 무결성 | 45/45 | 실행 전후 원본이 동일 |
| 수직 경로 | positive 양쪽 성공, negative 전건 deny | 참조 경로의 성공·거부 동작 |
| 공개 자기공격 | 14건 중 4건 우회 재현 | 모두 보호 runner/writer가 필요한 D-16 잔여 위험 |
| 독립 수렴 검사 | 5/5 두 차례 | Phase 0 종료 조건 재현 |

이 결과는 운영 보안 인증이 아니다. 다음 항목은 아직 제품에서 증명되지 않았다.

- 보호된 Actions writer와 실제 CODEOWNERS·브랜치 보호
- Backend 저장·latest·history·export 공통 경계
- Agent의 공개 `resolved_agent_version`
- 운영 DB, 실제 LLM, 실제 Agent 출력의 end-to-end 검증
- Python 3.11 환경의 참조 검사 의존성 재현

세부 실행 환경과 실패까지 포함한 기록은 [evidence/verification.md](evidence/verification.md)에 있다.

## 8. 남은 결정과 구현 순서

런타임의 `budget_limits`와 `retrieval_params`를 assurance 정책 권위에 포함할지는 아직
`Decision-needed`다. 현재 권장안은 allow/deny 권위에서는 분리하고 Evidence & Governance의
관측 범위에는 포함하는 것이다.

| 순서 | Jira | 산출물 | 허용 모드 |
|---|---|---|---|
| 1 | RPA-179 | 결정론적 회귀 안정화, 정책 registry 단일화, 의존성 선언 | fixture |
| 2 | RPA-180 | Change Assurance MVP | Observe |
| 3 | RPA-181 | Backend Output Boundary MVP | Observe |
| 4 | RPA-184 | 공개 Agent 버전 계약과 Backend 소비 | Observe |
| 5 | RPA-182 | 영수증·감사로그·Backoffice 읽기 전용 조회 | Observe |
| 6 | RPA-183 | 보호 writer, 정책 보호, 승격 기준 | Observe 후 평가 |

`Warn`과 `Enforce`는 정상 fixture, 결함 fixture, 우회 시험, 오탐 측정, 보호 writer 확인과 사람 승인을
모두 만족한 control만 승격한다.

## 9. 재검증

PowerShell에서 저장소 루트를 기준으로 실행한다.

```powershell
$env:PYTHONIOENCODING = 'utf-8'
Set-Location assurance/phase0/v1.10/evidence/frozen/phase0-v1.10-src
python -B contract_self_test.py
python -B vertical_paths.py
python -B selfattack.py
Set-Location ..
python -B codex-independent-check-v1.10.py
```

실행 전후 `phase0-v1.10-src/SHA256SUMS.txt`의 45개 원본 해시가 모두 같아야 한다. 실행 시 OS,
Python 버전, dependency digest, Git commit, 명령, 종료 코드와 생성 증거 경로를 함께 남긴다.

## English Summary

Phase 0 v1.10 closes the contract-review loop after ten documented revisions. It adopts protected
receipt writing, protected policy changes, public resolved Agent versioning, an initially empty
approved-version list, fail-closed enforcement targets, and per-turn version selection. The frozen
package remains an executable reference and evidence set, not a production harness. Product wiring
starts in Observe mode through RPA-179 to RPA-184 and must respect the Agent ownership boundary.
