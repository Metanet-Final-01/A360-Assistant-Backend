# Change Assurance Observe MVP

> Jira: `RPA-180`
>
> 현재 모드: `Observe`
>
> 병합 차단 효과: 없음

## 왜 만들었나

A360은 AI 보조 개발 비중이 높다. 사람이나 AI가 작성한 설명과 체크박스를 증거로 믿지 않고, 보호된 CI가 실제 Git diff에서
사실을 다시 계산해야 한다. 이 MVP는 다음과 같은 실패를 병합 전에 관측하기 위한 첫 번째 수직 슬라이스다.

- 존재하지 않거나 승인되지 않은 패키지를 AI가 그럴듯하게 추가한 경우
- 요구사항 버전과 실행 환경의 버전이 다른 경우
- 실제로 해석되지 않는 모듈·심볼을 import한 경우
- 검토된 고정 취약점 snapshot에서 임계치 이상의 advisory가 발견된 경우
- 허용 정책 밖의 라이선스를 가진 경우
- 테스트, workflow, 보증 정책, 다른 담당자의 `app/agent/**`를 함께 바꾼 경우
- 검사를 통과시키려고 코드 안에서 패키지를 자동 설치하는 우회 경로를 추가한 경우

## 동작 흐름

```text
GitHub PR event의 base/head SHA
        |
        v
Git object에서 diff와 변경 경로 재계산
        |
        +-- change-manifest.json
        +-- 경로별 risk profile
        +-- 의존성·import·취약점·license 판정
        +-- test/workflow/policy/Agent 소유 경로 변경 감지
        +-- checkout SHA·clean 상태 결합
        |
        v
assurance-report.json + SHA256SUMS + evidence artifact
        |
        v
allow_candidate | deny | unassured

Observe에서는 위 판정과 무관하게 기존 병합 결과를 바꾸지 않음
```

## 판정 의미

| 판정 | 의미 | Observe에서의 효과 |
|---|---|---|
| `allow_candidate` | 적용된 control이 모두 통과했고 증거가 완전함 | 기록만 함 |
| `deny` | 가짜 import, 미승인 dependency, 고위험 advisory, 금지 license 등 명시적 위반 | 기록만 함 |
| `unassured` | snapshot 누락·오래됨, detector 오류, 보호 경로 변경 등으로 안전을 증명하지 못함 | 기록만 함 |

`unassured`는 성공이 아니다. detector 오류나 증거 누락을 `PASS`로 바꾸지 않는다. 다만 RPA-180은 측정 단계이므로
`enforcement.blocks_merge=false`를 고정한다.

## Control

| Control | 지금 검사하는 것 | 차단 지점 목표 |
|---|---|---|
| `CH-01` | Git에서 change manifest를 재생성하고 계약 검증 | CI required check |
| `CH-02` | 실제 변경 경로를 risk profile로 라우팅 | CI required check |
| `CH-04` | allowlist, exact pin, import symbol, 취약점, license, 자동 설치 우회 | CI required check |
| `CH-06` | test/workflow/policy/Agent 소유 경로 변경 | 별도 owner 승인 |
| `CH-11` | 보고서 대상 SHA와 실제 clean checkout 결합 | 보호 runner |
| `CH-12` | 모든 증거 URI와 SHA-256 무결성 확인 | 단일 보호 writer |

## 의존성 정책

정책 파일은 [`policy/dependency-policy.json`](policy/dependency-policy.json)이다.

1. PR head와 base SHA의 merge-base에 있는 `requirements.txt`·`requirements-dev.txt` exact pin을 신뢰 기준
   allowlist로 사용한다.
2. 새 패키지나 버전 변경은 `approved_additions`에 정확한 버전과 승인 참조가 없으면 `deny`다.
3. 현재 `policy_decision_state=decision_needed`이며 전역 license 목록과 `high` 임계치는 Observe 측정용 제안값이지 사람 승인 정책이 아니다.
   단, `license_policy.approved_exceptions`는 Jira 등 명시적 승인 근거에 결합된 package/version/license 고정 예외다.
   세 값 중 하나라도 달라지면 예외를 적용하지 않는다.
4. `approved_additions`와 운영 취약점 package snapshot은 의도적으로 비어 있다.
5. 정책 승인 전이거나 snapshot이 없거나 30일 freshness를 넘으면 `unassured`이며 통과가 아니다.
6. 검사기 자체는 패키지를 설치하거나 네트워크 fallback을 하지 않는다. GitHub Actions는 검사기 실행 전에 PR 코드가 아닌
   trusted base SHA의 `requirements.txt`·`requirements-dev.txt`만 설치해 실제 import 검사 환경을 준비한다.
7. 패키지 제거는 HEAD 전체의 Python import를 다시 조사한다. 참조가 남으면 `deny`, 안정적인 import-root
   매핑으로 제거를 증명하면 해당 version·취약점·license 검사는 적용하지 않는다.

CI는 PR head를 분석 대상 checkout으로만 두고, 별도 checkout의 정확한 base SHA에서 검사기와 정책을 실행한다.
allowlist 기준선은 merge-base이고, 검사 실행 환경은 trusted base SHA의 고정 requirements를 설치한 환경이다. PR head의
요구사항이나 코드는 설치하지 않으므로 신규·변경 의존성이 trusted 환경에 없거나 실제 import를 증명할 수 없으면 보수적으로
`deny` 또는 `unassured`가 된다. 결정론적 정상 fixture는 test-only adapter로 검사 로직을 검증한다. 향후 보호된 사전 구축
runner와 검토된 offline snapshot은 `RPA-183` 승격 준비에서 다룬다.

RPA-180을 처음 추가하는 이 PR의 base에는 검사기가 없다. 이 부트스트랩 실행은 PR head의 검사기를 신뢰하는 대신
`BOOTSTRAP_UNASSURED.md`만 남긴다. RPA-180이 dev에 병합된 다음 PR부터 base의 검사기가 실행된다. workflow 자체의 변경 보호와
required check는 아직 RPA-183 범위이므로, 이 구조만으로 보호된 writer나 Enforce 경계를 주장하지 않는다.

## 실행과 산출물

```powershell
python -m assurance.change.cli `
  --repo . `
  --base-sha <40자 PR base SHA> `
  --head-sha <40자 PR head SHA> `
  --repository Metanet-Final-01/A360-Assistant-Backend `
  --output .artifacts/change-assurance
```

| 파일 | 내용 |
|---|---|
| `change-manifest.json` | Git에서 파생한 변경 목록, risk profile, 대상 SHA와 diff digest |
| `dependency-evidence.json` | rule별 판정과 package/import 상세 |
| `protected-change-evidence.json` | test/workflow/policy/Agent 소유 경로 및 민감 패턴 |
| `runtime-environment.json` | Python, OS, Git, 설치 distribution 목록과 dependency digest |
| `subject-evidence.json` | event head와 checkout HEAD, clean 상태, merge-base |
| `evidence-integrity.json` | 보고서가 참조하는 증거 URI와 digest 검증 결과 |
| `assurance-report.json` | control별 판정, 이유 코드, 증거 참조, 최종 보증 판정 |
| `evidence-index.json`, `SHA256SUMS` | artifact 전체 목록과 파일 무결성 |

출력 디렉터리는 기존에 알려진 하네스 산출물만 교체할 수 있다. 알 수 없는 파일·디렉터리나 심볼릭 링크가 있으면
검사기는 증거 혼입 가능성을 `AssuranceError`로 보고한다. GitHub Actions에서는 PR checkout 밖의
`${{ runner.temp }}` 아래에 실행별 새 디렉터리를 사용한다.

## 코드 구조

| 모듈 | 책임 |
|---|---|
| `foundation.py` | 공통 계약, Git object 읽기, 격리된 설치환경 검사 |
| `dependency_checks.py` | 요구사항·import·취약점·license·보호 경로 판정 |
| `evidence.py` | manifest/report 계약 검증과 증거 파일 무결성 작성 |
| `checker.py` | control 결과 조합, 최종 판정, Observe 오류 기록 |
| `cli.py` | GitHub Actions와 로컬 실행 진입점 |

## 검증

```powershell
python -m pytest -q tests/test_change_assurance.py
```

결정론적 fixture는 정상 import, 가짜 dependency, 가짜 symbol, 고위험 취약점, 허용되지 않은 license를 포함한다.
결함 주입은 `tests/change_assurance_adapter.py`에서만 일어나며 production endpoint나 공격용 flag는 없다.

## 아직 보장하지 않는 것

- 이 workflow는 아직 required check가 아니며 branch protection을 설정하지 않는다.
- base에 검사기가 없는 최초 도입 PR은 `bootstrap-unassured`이며 완전한 assurance report를 만들지 않는다.
- `deny`와 정상적으로 기록된 `unassured`는 CLI 성공으로 끝나지만, 오류 기록조차 만들지 못한 실행기 고장은 CI 실패로
  드러난다. required check가 아니므로 현재 단계에서 자동 병합 차단으로 승격되지는 않는다.
- `CODEOWNERS`와 정책 2인 승인은 아직 연결되지 않았다.
- `RPA-207`은 기본 브랜치 `workflow_run` publisher와 전용 writer API를 연결하지만, GitHub Environment의
  승인자·secret·배포 URL이 실제로 보호됐는지는 운영 설정에서 별도 검증해야 한다.
- 취약점 snapshot의 운영 갱신 주기와 승인자는 아직 확정되지 않았다.
- 외부 패키지를 실행하는 완전 격리·네트워크 차단 runner는 아직 없다.

정책 보호와 Observe 승격 근거는 후속 운영 검증에서 다룬다. 이 MVP와 `RPA-207` 전송 코드만으로 보안 인증이나
`Warn`·`Enforce` 승격을 주장하면 안 된다.

## 사람 리뷰 후속 판정

보호 대상 변경은 PR 생성 직후 CH-06 `PROTECTED_ORACLE_REVIEW_REQUIRED`로 기록된다. 이후 별도 사람이
현재 PR HEAD를 승인하면 `pull_request_review` 이벤트가 하네스를 다시 실행하고
`PROTECTED_ORACLE_REVIEW_VERIFIED` 후속 기록을 append-only로 추가한다. 최초 기록은 수정하지 않는다.
승인 뒤 새 커밋이 올라오거나 승인이 취소되면 현재 HEAD에는 기존 승인을 적용하지 않고 다시 검토 필요로 판정한다.

## English Summary

RPA-180 derives a manifest and assurance evidence from trusted Git objects, checks dependency and import closure,
flags protected-oracle changes, binds evidence to the exact PR head, and emits digest-addressed artifacts. It runs
in Observe mode only: `deny` and `unassured` are real assurance decisions, but neither changes the merge outcome.
The checker never installs PR dependencies or uses a network fallback. RPA-207 adds a disabled-by-default,
default-branch publisher and dedicated Backend writer API. Protected environment configuration, policy ownership,
branch protection, and promotion to Warn or Enforce still require operational verification.
