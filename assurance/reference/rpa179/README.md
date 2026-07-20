# RPA-179 교정 참조 하네스

> 상태: `Current-Confirmed` fixture 참조, 제품 경로 미연결
>
> 목적: Phase 0 v1.10 동결 원본을 보존하면서 사후 검토 28건의 처분을 재현 가능한 교정과
> 결정론적 회귀 검사로 고정
>
> 비범위: `app/agent/**`, 제품 저장 경계, DB, 네트워크, 실제 LLM, Warn·Enforce 전환

## 한 줄 설명

이 디렉터리는 동결 원본을 복사해 새 정본처럼 보관하지 않는다. 먼저 원본 45개 파일의 SHA-256을
확인하고, 검토 가능한 `corrections.patch`를 임시 폴더에 적용한 뒤, 별도 폴더 두 곳에서 같은
결과가 나오는지 검사한다.

## 왜 이런 구조인가

PR #255의 동결 원본은 당시 고민과 검토 결과를 증명하는 역사 증거다. 그 파일을 직접 고치면
“무엇이 당시 원본이고 무엇이 사후 교정인지” 구분할 수 없다. 반대로 교정본 전체를 또 복사하면
1만 줄이 넘는 중복 코드가 생기고 이후 두 사본이 조용히 달라질 수 있다.

그래서 다음 세 층을 분리했다.

1. `assurance/phase0/v1.10/evidence/frozen/`: 변경하지 않는 역사 원본
2. `corrections.patch`: RPA-179에서 원본에 가한 교정만 보이는 변경 집합
3. `materialize.py`: 원본 무결성을 확인하고 임시 교정본을 만드는 결정론적 조립기

조립기는 상위 Git 작업 트리 안에서도 패치가 조용히 건너뛰어지지 않도록 임시 Git 루트를 만들고,
적용 후 역방향 검사까지 통과해야 성공한다. 임시 `.git`은 트리 해시 계산 전에 삭제한다.

## 파일 역할

| 파일 | 역할 |
|---|---|
| `corrections.patch` | 동결 v1.10 대비 교정 33개 파일의 검토 가능한 diff |
| `materialize.py` | 원본 45개 해시 확인, 역사 회귀 파일 5개 제외, 패치 적용, 교정 트리 해시 생성 |
| `finding-matrix.yaml` | 사후 검토 CR-01~CR-28의 처분과 회귀 검사 연결 |
| `regression.py` | 교정 대상 25건을 23개 결정론적 case로 직접 재현 |
| `verify.py` | 독립 조립 2회와 계약·수직 경로·자기공격·회귀 검사를 총괄 |
| `verification-result.schema.json` | 환경·명령·해시·결과가 빠진 검증 증거를 거부 |
| `evidence/verification.json` | 마지막 전체 재검증의 기계 판독 결과 |

## 사후 검토 28건의 처리

| 처분 | 수 | 회귀 방식 |
|---|---:|---|
| 교정 필요 | 22 | 구현과 negative fixture를 함께 보정 |
| 범위 한정 후 교정 | 3 | 실제 영향 범위로 좁힌 뒤 검사 |
| 역사 기록 | 3 | 동결 원본은 보존하고 현재 안내에서만 분리 |

회귀 대상은 교정 필요와 범위 한정 후 교정을 합친 25건이다. 역사 기록 3건은 의도적으로 실행
case가 없고 matrix에 `historical`로 남는다. 누락된 교정 대상이나 등록되지 않은 case가 있으면
`coverage_matrix`가 실패한다.

주요 교정 범주는 다음과 같다.

- 읽기·파싱·Git 실패를 typed deny로 정규화하고 실패 시 허용하지 않는다.
- NUL 구분 Git 경로, 개행 파일명, 긴 Windows 경로와 revision/pathspec 경계를 처리한다.
- 정책 authority 목록을 보호된 `policy-registry.yaml` 하나에서 해석한다.
- 정책·waiver·Agent 버전 승인을 별도 보호 artifact와 대상 digest에 결합한다.
- receipt를 저장하기 전에 전체 schema와 content address를 검증해 key poisoning을 막는다.
- 실제 실행 규칙과 `rule_versions`가 정확히 일치해야 Output 판정을 허용한다.
- `confirmed` 사실은 실제 evidence를 요구하고, 관찰 불가능한 provenance는 버전 값을 허용하지 않는다.
- 공개 자기공격 결과는 D-16 잔여 4건과 정확히 같아야 하며, 새 우회나 예상 잔여의 소실 모두 실패다.

## 전체 재검증

저장소 루트의 PowerShell에서 비어 있는 새 작업 경로를 지정한다.

```powershell
$run = [Guid]::NewGuid().ToString('N').Substring(0, 8)
$work = "assurance/reference/rpa179/.work/m-$run"
python -B assurance/reference/rpa179/verify.py `
  --work-root $work `
  --evidence assurance/reference/rpa179/evidence/verification.json
```

검증기는 시작할 때 추적 파일과 미추적 파일을 포함한 Git 작업 트리가 깨끗한지 확인한다.
먼저 구현·정책 변경을 커밋한 뒤 검증을 실행하고, 생성된 evidence와 sha256 sidecar는 별도
커밋으로 남긴다. 따라서 `baseline_commit`은 evidence 자체가 아니라 검증한 구현 커밋을 가리킨다.

검증기는 각 run에서 다음 네 명령을 순차 실행한다.

1. `contract_self_test.py`: 68개 계약 case와 15개 실행 규칙 coverage
2. `vertical_paths.py`: Change와 Output의 positive·negative 수직 경로
3. `selfattack.py`: 공개된 D-16 잔여가 정확히 4/14인지 확인
4. `regression.py`: 사후 검토의 실행 대상 25건을 모두 회귀

두 run의 교정 트리 digest, 회귀 보고서 digest와 정규화 transcript digest가 모두 같아야 최종
`pass`다. 임시 절대 경로처럼 실행별로 달라지는 값만 토큰으로 정규화하며, 원본 stdout·stderr
해시는 run별로 별도 보존한다.

Windows에서는 저장소 절대 경로가 이미 길 수 있으므로 `.work` 아래 실행 폴더명은 예시처럼
짧게 둔다. 지나치게 긴 이름은 하위 receipt fixture가 OS 경로 한계를 넘어 fail-close할 수 있다.

개별 pytest 회귀는 다음과 같다.

```powershell
python -m pytest tests/test_assurance_rpa179_reference.py -q
```

## 확인된 것과 남은 것

`Current-Confirmed`:

- Windows 11, CPython 3.13.2, `jsonschema==4.26.0`, `PyYAML==6.0.3`
- 동결 원본 무결성 45/45, 교정 트리 52개 파일
- 계약 68/68, 실행 규칙 15개 coverage PASS
- 사후 검토 실행 대상 25/25, 23개 case PASS
- 두 독립 run의 트리·회귀·정규화 출력 동일

`Not-tested` 또는 별도 구현:

- 저장소 표준 CPython 3.11에서의 재실행은 해당 런타임을 가진 CI에서 확인해야 한다.
- D-16의 보호된 단일 writer, D-17의 정책 보호와 2인 리뷰는 RPA-183 범위다.
- Backend 저장·latest·history·export 경계는 RPA-181 범위다.
- 공개 `resolved_agent_version` 계약은 Agent 담당자와 합의하는 RPA-184 범위다.
- Warn·Enforce 전환과 운영 보안 인증은 이 결과로 승인되지 않는다.

## Agent 소유권 경계

이번 교정은 Agent가 공개한 결과를 어떻게 증명하고 소비할지에 관한 바깥 계약이다.
`app/agent/**`의 prompt, graph, model, tool, repair, retry, eval을 복제하거나 수정하지 않는다.
Agent 내부에 이미 있는 하네스를 대체하지도 않는다.

## English Summary

RPA-179 preserves the immutable Phase 0 v1.10 evidence and represents corrections as an inspectable
patch. The materializer verifies all 45 frozen hashes, applies the patch in a disposable Git root,
and hashes the corrected 52-file tree. Verification performs two independent materializations and
runs contract, vertical-path, disclosed self-attack, and 25-finding regression checks. This is a
fixture-only reference, not product wiring, an Agent-internal harness, or an operational security
certification.
