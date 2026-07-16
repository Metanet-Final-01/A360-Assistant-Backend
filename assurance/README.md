# A360 AI 보조 개발 보증

> 이 디렉터리는 AI가 만든 변경과 AI가 생성한 제품 출력을 그대로 신뢰하지 않고, 독립된 검증과
> 사람의 운영 결정을 거쳐야만 다음 단계로 진행시키기 위한 A360의 보증 체계를 기록한다.

## 왜 이 기록을 남기는가

A360은 개발 과정에서 AI를 적극적으로 사용했다. 그래서 결과물만 보여주는 대신, AI가 틀릴 수
있다는 전제에서 어떤 결함을 발견했고, 어떤 주장을 철회했으며, 어떤 통제를 추가했는지까지 남긴다.

이 기록의 목적은 문서의 양을 늘리는 것이 아니다.

- 설계 결정과 그 이유를 팀원이 다시 추적할 수 있게 한다.
- 같은 AI 또는 다른 AI가 낸 결과를 독립 검토한 흔적을 보존한다.
- 통과한 시험과 아직 증명하지 못한 운영 통제를 명확히 분리한다.
- 이후 CI, Backend 저장 경계, 감사로그 구현이 어떤 계약을 따라야 하는지 고정한다.

## 5분 읽기 순서

| 순서 | 문서 | 확인할 내용 |
|---|---|---|
| 1 | [Phase 0 v1.10 개요](phase0/v1.10/README.md) | 무엇을 결정했고 무엇은 아직 미구현인지 |
| 2 | [버전별 설계 변화](phase0/v1.10/VERSION_HISTORY.md) | v1.1부터 v1.10까지 왜 반복 수정했는지 |
| 3 | [채택 결정](phase0/v1.10/decisions/adoption.json) | 사람이 확정한 경계와 rollout 상태 |
| 4 | [동결 후 사후 검토](phase0/v1.10/evidence/post-freeze-review.md) | 외부 AI 리뷰 28건의 독립 판정과 구현 차단 조건 |
| 5 | [재검증 결과](phase0/v1.10/evidence/verification.md) | 무엇을 어느 환경에서 재현했는지 |
| 6 | [증거 안내](phase0/v1.10/evidence/README.md) | 동결 원본과 검증 코드의 역할 및 읽는 법 |
| 7 | [RPA-179 교정 참조](reference/rpa179/README.md) | 동결 원본을 보존하며 28건을 어떻게 교정·회귀하는지 |
| 8 | [RPA-180 Change Assurance](change/README.md) | 실제 PR diff를 Observe하고 증거를 남기는 첫 제품 연결 |

## 세 가지 하네스

| 하네스 | 질문 | 최종 차단 지점 |
|---|---|---|
| Change Assurance | AI가 만든 코드 변경을 병합해도 되는가? | 보호된 CI와 브랜치 정책 |
| Backend Output Boundary Assurance | Agent 공개 출력과 사용자 편집 후보를 저장·조회·내보내도 되는가? | Backend 저장·latest·history·export 경계 |
| Evidence & Governance | 그 판정이 어떤 입력·정책·실행 환경에서 나왔는가? | 단일 writer가 발행하는 영수증과 감사 증거 |

제품 Agent 내부의 prompt, graph, model, tool, repair, retry, eval은 Agent 담당자의 소유 영역이다.
Backend 하네스는 Agent의 공개 계약을 소비하고 저장 경계를 감싸며 `app/agent/**` 내부 구현을
복제하거나 수정하지 않는다.

## 현재 상태

| 항목 | 상태 |
|---|---|
| Phase 0 계약 검토 | 종료, `approve_for_human_decision` |
| 사람의 핵심 결정 | D-16, D-17, D-21~D-24 채택 |
| 동결 후 참조 구현 검토 | 28건 처분 완료, `corrective-action-required` |
| RPA-179 교정 참조 | 실행 대상 25/25, 독립 실행 2회 결정론 검증 PASS |
| RPA-180 Change Assurance | Observe 검사기·결정론적 결함 fixture·비차단 CI 연결 |
| 참조 구현의 제품·운영 사용 | 금지, RPA-179도 fixture 참조만 제공 |
| 제품 코드 연결 | RPA-180이 PR CI에 `Observe`로만 연결 |
| 현재 허용 rollout | `Observe`만 허용 |
| `Warn`·`Enforce` | 별도 증거와 사람 승인 전 금지 |
| 운영 보안 인증 | 아님 |

Phase 0와 RPA-179는 **검토 가능한 계약과 결정론적 참조 구현**이며 생산 사용 승인이 아니다.
RPA-180은 실제 PR 경로에 연결되는 첫 구현이지만 아직 비차단 `Observe`다. required check, 보호 writer,
정책 소유권을 갖춘 운영 하네스 승격은 아니며 후속 구현은 Jira `RPA-181`부터 `RPA-184`까지 추적한다.

## 디렉터리 구조

```text
assurance/
├─ README.md                         # 전체 목적과 읽기 순서
├─ phase0/v1.10/
   ├─ README.md                      # 현재 계약과 구현 경계
   ├─ VERSION_HISTORY.md             # v1.1~v1.10 고민과 보정 과정
   ├─ decisions/
   │  ├─ adoption.json               # 사람이 채택한 기계 판독 결정
   │  └─ adoption.schema.json        # 결정 파일 검증 스키마
   └─ evidence/
      ├─ README.md                   # 증거 분류와 파일 역할
      ├─ post-freeze-review.md       # CodeRabbit 지적의 독립 판정과 처분
      ├─ verification.md             # 재실행 환경과 결과
      ├─ artifact-manifest.sha256    # 동결 원본 무결성
      └─ frozen/                     # 당시 원본, 내용 수정 금지
├─ change/
   ├─ README.md                      # RPA-180 목적, 실행법, 한계
   ├─ foundation.py                  # 공통 계약, Git·실행환경 증거 수집
   ├─ dependency_checks.py           # 의존성·import·보호 경로 판정
   ├─ evidence.py                    # 증거 계약 검증과 무결성 작성
   ├─ checker.py                     # control 조합과 최종 판정
   ├─ cli.py                         # 판정 비차단 Observe CLI
   ├─ policy/                        # 의존성·취약점·license 정책
   └─ schemas/                       # manifest·report JSON Schema
└─ reference/rpa179/
   ├─ README.md                      # 교정 구조, 실행법과 한계
   ├─ corrections.patch              # 동결 원본 대비 교정만 담은 diff
   ├─ finding-matrix.yaml            # 검토 28건과 회귀 case 연결
   ├─ materialize.py                 # 무결성 확인 후 임시 교정본 조립
   ├─ regression.py                  # 실행 대상 25건 회귀
   ├─ verify.py                      # 독립 조립·실행 2회 결정론 검증
   ├─ verification-result.schema.json # 검증 evidence의 정확한 계약
   └─ evidence/
      ├─ verification.json           # 마지막 독립 검증 결과
      └─ verification.sha256         # 결과 파일의 sha256 sidecar
```

## 판정 언어

- **Baseline-Confirmed**: 명시된 과거 commit에서 재현된 사실이다.
- **Current-Confirmed**: 현재 구현 기준선에서 직접 확인한 사실이다.
- **Not-tested**: 아직 실행하거나 관찰하지 않았다.
- **Decision-needed**: 기술 검증만으로 정할 수 없어 사람이 결정해야 한다.

AI 간 교차 검토는 결함을 찾는 방법이지 최종 승인이나 보안 인증이 아니다. 최종 rollout 권한은
보호된 정책과 사람의 결정에 있다.

## English Summary

This directory records how A360 treats AI-produced code and product output as untrusted until
independent checks and human policy decisions permit progression. Start with the Phase 0 overview,
then read the Korean-first version history, adopted machine-readable decisions, verification record,
and frozen evidence guide. The frozen Python package is an executable reference contract, not a
production harness or security certification.
