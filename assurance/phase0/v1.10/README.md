# Phase 0 보증 계약 v1.10

> 상태: `approve_for_human_decision` 이후 사람 결정을 반영한 추적 기준
>
> 범위: 계약·정책·결정론적 fixture의 원본 보존 및 재검증
>
> 비범위: 제품 코드 수정, 운영 DB 쓰기, 실제 LLM 호출, CI required check, 운영 차단

## 1. 기준선과 증거 구분

| 구분 | commit | 의미 |
|---|---|---|
| 역사적 감사 기준선 | `ed1a1b0d62452f75212da4f78f3e8b9f990da42e` | 초기 감사에서 직접 재현한 사실. 최신 코드의 사실로 재표기하지 않는다. |
| 계약 작성 기준선 | `d66fce1840c1dcf907c9d303bbc64654c7c41857` | v1.10 계약과 실행 원본이 평가한 당시 `origin/dev`. |
| 현재 구현 기준선 | `7d2561819eba107a6a5ffcc32f5420a955dd7028` | 이 패키지를 리포에 고정할 때의 `origin/dev`. |

- **Baseline-Confirmed**: 위 고정 SHA 중 명시된 기준선에서 재현된 사실이다.
- **Current-Confirmed**: 현재 구현 기준선에서 이번 작업이 직접 재현한 사실이다.
- **Not-tested**: 아직 실행하거나 관찰하지 않았다.
- **Decision-needed**: 기술 검증만으로 정할 수 없어 사람이 결정해야 한다.

`d66fce1` 이후 `7d25618`까지 PR #239, #242, #243이 병합됐고 예산 가드레일, 게이지 임계값,
런타임 예산 오버라이드 관련 16개 파일이 변경됐다. 이 병합 사실과 파일 범위는
Current-Confirmed다. `post-contract-impact-claude-v1.md`의 읽기 전용 영향 검토에서도 세 병합이
`app/agent/**`를 바꾸지 않았고 3중 하네스 방향과 D-002 경계를 바꾸지 않는다고 판정했다. 이 판정은
코드 판독 기반 advisory evidence이며 런타임 PASS가 아니다. 과거 계약 파일을 수정해 현재 사실처럼
보이게 하지 않는다.

현재 기준선의 Agent registry는 `v1`과 `v2`를 발견하며 `v3` 구현 폴더는 아직 없다. 따라서 아래
D-24의 v1/v2/v3 선택은 v3가 공개·검증·승인됐을 때도 유지해야 할 **목표 계약**이다. 현재 제품이
v3를 이미 제공한다는 Current-Confirmed 주장이 아니다. 현재 코드에서 알 수 없는 `AGENT_VERSION`
기본값은 경고 후 v2로 폴백하므로 D-23의 silent fallback 금지도 아직 미구현이다.

## 2. 보존한 원본과 채택 overlay

| 파일 | SHA-256 | 역할 |
|---|---|---|
| `contract.md` | `05c1bb974193e973025ac818235a9df511c0f566bba4d40f6b270dcaed531128` | Claude가 작성한 영문·한글 v1.10 계약과 당시 실행 증거 |
| `independent-review.md` | `4f5e1aef48821ae00bd1c41b4c85f217a33a54d71b25d92af0a15c8540858274` | Codex 독립 검토와 주장 한계 |
| `independent-check.py` | `3a8fa7ee2f54007f6b51a8ca54c54034e73e52134830e9717758990c6cc6b36a` | 독립 수렴 조건 재검사 |
| `phase0-v1.10-src/SHA256SUMS.txt` | `d0ad43c2a705d0522cba8e287b31fe040c953bb2ba89a5d3d9788cc0b07a666f` | 실행 원본 45개 파일의 무결성 목록 |
| `post-contract-impact-claude-v1.md` | `2235c5942414603e52487794787a6c2ca21358485fcd46cddc1cfbc092ce5999` | `d66fce1..7d25618`과 미병합 PR #249의 읽기 전용 영향 검토 |
| `ADOPTION.json` | `6192e83017884ca0c983fa648a916a420798dc315f32c857fc25cc9ae99bb7dc` | 이후 사람 결정과 현재 rollout을 담은 기계 판독 overlay |
| `adoption.schema.json` | `c3e31e1aba57175c974549a2503fae17d55f98ff2b25d71d1a1849943ca0afc6` | overlay의 필수 결정과 금지 상태를 검증하는 스키마 |

해시는 `ARTIFACTS.sha256`에도 기계 판독 형식으로 기록했다. 실행 중 생성되는 `.out/`은 계약
원본이 아니며 Git에서 제외한다. 동결 원본의 과거 `Decision-needed` 문구는 당시 상태를 보존하기
위해 고치지 않고, 이후 확정 사항을 이 문서에서 덧붙인다.

리포의 `.gitattributes`는 이 디렉터리에 `-text`를 적용한다. 로컬 `core.autocrlf` 설정과 무관하게
검토한 바이트를 그대로 저장해 SHA-256이 checkout마다 달라지는 것을 막기 위한 조치다.

`ADOPTION.json`은 이후 사람 결정과 현재 구현 기준선을 기계 판독 가능하게 기록한 overlay이며,
`adoption.schema.json`으로 검증한다. 이는 동결된 v1.10 정책 원본을 소급 수정하지 않는다.

## 3. 이후 사람이 확정한 운영 결정

| ID | 확정 내용 | 이유와 적용 시점 |
|---|---|---|
| D-16 | 보호된 GitHub Actions를 보증 결과와 영수증의 단일 writer로 사용한다. 서명은 후순위다. | 애플리케이션 프로세스가 스스로 `allow` 영수증을 만들 수 없게 권한을 분리한다. 실제 보호 설정 전에는 조건부 설계다. |
| D-17 | 정책 경로에 `CODEOWNERS`, 브랜치 보호, 2인 리뷰를 적용한다. | 코드 작성자나 AI가 같은 PR에서 판정 기준을 약화하는 것을 막는다. 보호 설정은 RPA-183에서 검증한다. |
| D-21 | Agent 담당자에게 공개 응답의 `resolved_agent_version` 제공을 요청한다. | Backend는 공개 필드만 소비하며 `app/agent/**` 내부를 읽거나 수정하지 않는다. RPA-184의 팀 간 계약이다. |
| D-22 | 승인 버전 목록은 빈 상태로 시작하고, 검증된 버전만 사람이 추가한다. | 발견 가능한 버전과 운영 승인 버전을 분리한다. 빈 목록은 아직 운영 차단을 켤 수 없다는 정직한 상태다. |
| D-23 | `Observe`에서는 미승인·불명 버전을 기록하고, `Enforce`에서는 저장·latest·history·export를 차단한다. | 초기 오탐과 계약 누락을 측정하면서도 최종 강제 단계에서는 우회를 남기지 않는다. |
| D-24 | 버전은 세션 고정이 아니라 **매 턴 v1/v2/v3 중 선택**한다. | RPA-176·RPA-177의 사용자 선택 계약을 보존한다. 다음 턴에는 다른 버전을 선택할 수 있다. |

D-24의 세부 불변식은 다음과 같다.

1. 각 턴은 `requested_agent_version`을 명시하거나 공개 기본값 규칙을 따른다.
2. 같은 턴의 자동 compact와 본 처리는 동일한 요청 버전을 사용한다.
3. 다음 턴은 v1, v2, v3 중 다른 버전을 다시 선택할 수 있다.
4. 요청 버전과 실제 `resolved_agent_version`을 턴 및 산출물에 함께 기록한다.
5. 알 수 없거나 미승인인 버전으로 조용히 대체하는 silent fallback은 허용하지 않는다.

현재 구현에서는 발견된 v1/v2만 선택할 수 있다. v3는 공개 버전으로 실제 제공되고 D-22 검증을
통과한 뒤 선택지에 포함한다.

담당자 실명, 시행일, 승인 버전의 실제 목록, 서명 도입 시점은 여전히 Decision-needed다.

### 3.1 새 Decision-needed: 런타임 오버라이드

PR #239·#243으로 예산 상한은 사용자 요청을 429로 차단하는 실제 런타임 정책이 됐고,
`budget_limits`와 기존 `retrieval_params`는 저장소 밖에서 admin API로 변경된다. 이 값들은 D-17의
CODEOWNERS 정책 트리에 포함되지 않으므로 다음 경계를 사람이 확정해야 한다.

> 제품 런타임 튜닝 정책을 assurance 정책 권위 안에 둘 것인가, 명시적으로 밖에 둘 것인가?

현재 권장안은 **D-17의 allow/deny 권위에서는 분리하되 Evidence & Governance 관측 범위에는 포함**하는
것이다. 즉 런타임 튜닝이 보증 영수증을 발급하지는 못하게 하면서, 사용자 차단 값에는 구체적인
service principal, 변경 전후 값, 사유, 승인 또는 긴급 예외, 만료·rollback 정보를 남긴다. 이 권장안은
사람 승인 전까지 확정 정책으로 취급하지 않는다.

## 4. 소유권 경계

- Backend 측에서 변경 가능한 범위: 공개 요청·응답 스키마, 저장 전 경계 검증, 판정 영속화,
  latest/history/export 공통 차단, 증거 조회.
- Agent 담당자에게 요청할 범위: 공개 `resolved_agent_version` 필드와 버전 계약 합의.
- 변경 금지 범위: `app/agent/**`의 prompt, graph, model, tool, repair, retry, eval 및 내부 verifier.
- deterministic fixture와 test-only adapter만 사용하며 production 공격용 endpoint나 flag를 만들지 않는다.

## 5. 검증 상태와 주장 한계

동결 문서에 기록된 Baseline-Confirmed 결과는 다음과 같다.

- 계약 self-test: 66/66, mismatch 0, coverage PASS
- 실행 원본 무결성: 45/45
- 독립 수렴 조건: 5/5를 두 차례 재현
- 공개 자기공격: 14건 중 4건 우회 재현, 모두 보호 runner/writer가 필요한 D-16으로 분류

이는 제품 하네스가 운영에서 안전하다는 인증이 아니다. 특히 D-16 보호 설정, Backend 권위 저장
경계, Agent 공개 버전 필드, 실제 CI/운영 증거는 아직 구현·검증되지 않았다. `approved_versions`도
의도적으로 비어 있으므로 현재 패키지로 운영 결과를 `assured`라고 판정해서는 안 된다.

알려진 실행 원본 부채는 Jira `RPA-179`에서 별도로 처리한다.

1. `codex_v19_regression.py`가 고정 `.out/v19-porcelain-subject`를 사용해 Windows에서 연속 재실행 시 충돌한다.
2. `contract_self_test.py`가 정책 ID 일부를 수동 중복 선언한다.
3. 계약 실행에 필요한 `jsonschema`와 `PyYAML`이 리포의 개발 의존성에 선언돼 있지 않다.

RPA-178에서는 동결 원본을 수정하지 않는다.

## 6. 구현 순서

| Jira | 작업 | 이 단계에서 허용되는 모드 |
|---|---|---|
| RPA-178 | 이 계약과 원본을 SSOT로 고정 | 제품 미연결 |
| RPA-179 | 재실행 안정성·정책 registry 부채 제거 | fixture 검증 |
| RPA-180 | Change Assurance MVP | Observe |
| RPA-181 | Backend Output Boundary MVP | Observe |
| RPA-182 | 영수증·감사로그·백오피스 읽기 전용 조회 | Observe |
| RPA-183 | 보호 writer·정책 보호·승격 기준 | Observe에서 Warn/Enforce 후보 평가 |
| RPA-184 | Agent 공개 버전 계약과 Backend 소비 | Agent 담당자 합의 후 Observe |

`Warn`과 `Enforce`는 정상 fixture 통과, 결함 fixture 탐지, 우회 시험, 오탐 측정, 보호 writer 확인 및
사람 승인까지 충족한 control만 승격한다.

미병합 PR #249(v3)의 공개 계약이 바뀔 수 있으므로 다음 순서를 추가한다.

- RPA-181의 validator와 Observe 배선은 v2 기준으로 시작할 수 있지만 strict schema 고정은 #249 병합과
  RPA-184 공개 버전 계약 이후에 한다.
- RPA-181은 `fill_cards`를 `llm_invoked=false`인 추천 변경 경로로 모델링한다.
- RPA-182의 완결성 기대값은 `resolved_agent_version`으로 키잉하고, 예산 429로 LLM/turn 증거가 0건인
  경우를 정상 차단 결과로 표현한다.
- RPA-182는 `credential_ref`와 `file_path`의 실제 카드 값을 증거에 저장하지 않는다.
- RPA-183은 위 런타임 오버라이드의 포함·제외 경계를 인수 기준에 명시한다.

## 7. 재검증 명령

PowerShell에서는 UTF-8 출력을 고정한 뒤 실행한다.

```powershell
$env:PYTHONIOENCODING = 'utf-8'
Set-Location assurance/phase0/v1.10/phase0-v1.10-src
python -B contract_self_test.py
python -B vertical_paths.py
python -B selfattack.py
Set-Location ..
python -B independent-check.py
```

각 실행 전후 `SHA256SUMS.txt`의 45개 원본 해시가 모두 일치해야 한다. 검증은 OS, Python 버전,
dependency 목록 digest, Git commit, 명령, 종료 코드와 생성 증거 경로를 함께 기록한다.
이번 재검증의 환경과 결과는 `VERIFICATION.md`에 기록했다.

## English Summary

Phase 0 v1.10 is preserved byte-for-byte as the reviewed contract package. Historical evidence is
kept separate from the current implementation baseline. Human decisions now select a protected
single writer, protected policy changes, a public resolved-version contract, an initially empty
approved-version list, fail-closed enforcement, and per-turn v1/v2/v3 selection. No production
enforcement or security certification is claimed by this package.
