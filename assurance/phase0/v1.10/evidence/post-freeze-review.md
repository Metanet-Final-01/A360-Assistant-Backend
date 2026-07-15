# PR #255 동결 후 사후 검토

> 판정일: 2026-07-15
>
> 대상: Phase 0 v1.10 동결 원본과 저장소 편입 구조
>
> 결론: 설계 결정은 유지하되 참조 구현은 `corrective-action-required`

## 1. 왜 이 문서가 필요한가

Phase 0 v1.10 원본을 동결한 뒤 PR #255에서 CodeRabbit 리뷰가 수행됐다. 첫 라운드는 인라인 19건,
리뷰 본문 5건을 제시했고, 구조 정리 커밋을 본 두 번째 라운드는 새로운 인라인 4건을 제시했다.
중복 지적을 제외하면 고유 항목은 28건이다.

CodeRabbit는 결함 탐색 도구이지 보증 권위가 아니다. Codex가 각 항목을 동결 원본, JSON Schema,
Git diff와 결정론적 입력으로 다시 확인한 뒤 아래처럼 처분했다. 동결 원본은 당시 판단의 증거이므로
한 줄도 고치지 않았고, 잘못된 주장과 후속 차단 조건을 이 정리층에 남긴다.

| 처분 | 수 | 뜻 |
|---|---:|---|
| C: 교정 필요 | 22 | 현재 원본에서 실제 결손을 확인했으며 RPA-179 회귀와 함께 고쳐야 함 |
| Q: 범위 한정 후 교정 | 3 | 지적의 핵심은 맞지만 영향 또는 제안 수정 범위를 좁혀야 함 |
| H: 역사 기록·정리층 처리 | 3 | 원본은 보존하고 현재 안내에서 모순·경로·기준선을 명시함 |
| 기각 | 0 | 근거 없이 무시한 항목 없음 |

## 2. 현재 결정

- `approve_for_human_decision`은 D-16·D-17·D-21~D-24의 설계 결정을 검토할 수 있다는 판정이다.
- 동결 Python 원본을 생산 하네스 구현으로 복사하거나 CI 강제에 연결하는 것은 RPA-179 전까지 금지한다.
- 제품 코드, `app/agent/**`, 운영 DB, Jira 상태는 이 사후 검토에서 변경하지 않았다.
- 다음 허용 단계는 교정된 fixture에서의 재검증이며, 제품 연결은 그 뒤에도 `Observe`부터 시작한다.

## 3. 독립 확인에서 나온 핵심 증거

다음 결과는 리뷰 설명을 그대로 옮긴 것이 아니라 현재 원본에서 다시 확인한 값이다.

- `git diff --name-only ed1a1b0..50687 -- app/agent`는 비어 있지 않고 8개 파일을 반환했다.
- v1.5 회귀 파일의 `closed(...)` 실행은 #1, #2, #3, #4, #6, #7, #8, #9의 8건뿐이다.
- `fnmatch("bootstrap.sh", "**/*.sh")`는 `False`라 루트 스크립트가 검사 범위에서 빠진다.
- `Current-Confirmed + evidence_kind:none`, `rule_versions` 누락, 관측 불가 상태의 문자열 버전,
  정책 최상위 오타, `risk_profile` 동반 필드 누락은 모두 현 스키마 오류 0건으로 통과했다.
- 잘못된 UTF-8 artifact는 `ArtifactError`가 아니라 `UnicodeDecodeError`로 빠져나갔다.
- 90일 23시간 waiver와 해석되지 않는 `approval_ref` 조합이 `_resolve_waiver()`에서 허용됐다.
- `from importlib import import_module as load; load("app.agent.verify")`는 HL-06을 통과했다.
- `ChangeReceiptStore.put()`은 `{"receipt_id":"CR-poison"}`만 있는 객체를 저장한 뒤 조회 시 실패했다.
- assured bundle의 `fail` 상태는 스키마가 거부했고 `skipped`는 enum 자체가 거부했다. 따라서 CR-10의
  “즉시 critical 우회”는 재현되지 않았지만, HL-15 단독 규칙의 비통과 상태 미검사는 교정 대상으로 남긴다.

## 4. 28건 처분표

| ID | 리뷰 위치 | 처분 | 독립 판정과 후속 처리 |
|---|---|---|---|
| CR-01 | `attest.py` | C | 정책 resolve와 digest 계산이 예외 처리 밖에 있어 typed deny 대신 중단된다. Change·Output 모두 입력 오류를 명시적 deny로 정규화한다. |
| CR-02 | `codex_v15_regression.py` | C | dirty 경로가 하나도 검사되지 않아도 통과한다. synthetic dirty repo와 non-empty 검사를 요구한다. |
| CR-03 | `codex_v15_regression.py` | C | 문서는 9건을 주장하지만 #5가 없고 실제 `closed()`는 8건이다. 누락 공격을 복원하고 개수를 단언한다. |
| CR-04 | `contract_self_test.py` | C | 작성자 Windows 절대 경로에 의존한다. 고정 fixture 또는 명시적 입력으로 바꾼다. |
| CR-05 | `control-registry.yaml` | C | `app/agent` 전체 미변경 주장은 실제 8개 변경과 모순된다. evidence scope와 재현 명령을 정확한 파일로 제한한다. |
| CR-06 | `core.py` artifact load | C | 읽기·UTF-8·JSON/YAML 오류가 `ArtifactError`로 정규화되지 않는다. 모든 비정상 artifact를 typed deny 입력으로 바꾼다. |
| CR-07 | dependency path policy | C | 재귀 glob만 있어 루트 셸·PowerShell·배치·Make 파일이 누락된다. 루트 패턴과 부정 fixture를 추가한다. |
| CR-08 | `rules.py` waiver 기간 | C | `timedelta.days`가 부분 일을 버려 90일 초과 면제를 허용한다. 전체 duration을 비교한다. |
| CR-09 | Agent version policy 승인 | Q | 승인 evidence를 검증하지 않는 문제는 맞다. 다만 `require_approved()`도 문자열 존재만 보므로 단순 재사용이 아니라 승인 artifact의 상태·역할·subject를 해석해야 한다. |
| CR-10 | `rules.py` HL-15 control 상태 | Q | HL-15가 `waived` 외 상태를 직접 거부하지 않는 것은 맞다. 현 스키마가 assured의 fail·warn·blocked와 모든 skipped를 거부해 end-to-end critical 우회는 재현되지 않았다. 방어 중복과 규칙 단독 계약을 보강한다. |
| CR-11 | `rules.py` 동적 import | C | `import_module` 별칭이 private Agent import 검사를 우회한다. alias 해석과 계산 불가 target의 fail-close fixture가 필요하다. |
| CR-12 | control registry schema | C | Confirmed evidence가 `evidence_kind:none`이어도 통과한다. Confirmed에는 실제 증거 종류를 강제한다. |
| CR-13 | evidence bundle schema | C | `rule_versions`가 optional이고 빈 배열도 허용된다. 필수·최소 1건과 실행 규칙 결속을 강제한다. |
| CR-14 | runtime provenance schema | Q | 관측 불가인데 문자열 resolved version을 허용한다. HL-20은 assured 경로에서 관측 adapter와 비교해 거부하지만, 모순 artifact 자체를 스키마에서 차단해야 한다. |
| CR-15 | waiver 승인 참조 | C | `approval_ref`와 `approver_role`을 실제 승인 artifact로 해석하지 않는다. 존재·상태·역할·subject·기간을 모두 결속한다. |
| CR-16 | `selfattack.py` 종료 상태 | C | 예상 밖 우회가 늘어도 출력만 하고 exit 0이다. 공개 D-16 잔여 목록만 허용하고 나머지는 실패 종료한다. |
| CR-17 | `SHA256SUMS.txt` 기준선 문구 | H | 동결 원본은 `d66fce1`을 현재 dev로 잘못 부른다. 현재 README와 adoption은 계약 기준선과 편입 기준선을 분리했으며 원문 모순을 이 문서에 보존한다. |
| CR-18 | 수직·v1.5 실행 경로 | C | 작성자 로컬 repo와 baseline이 하드코딩돼 다른 checkout에서 재현성이 없다. runner 입력 또는 synthetic repo로 통일한다. |
| CR-19 | 회귀 파일 Git helper | C | 하위 Git 실패의 반환 코드를 무시해 빈 문자열로 검사를 계속한다. `check=True` 상당의 명시적 실패를 강제한다. |
| CR-20 | v1.8 v404 회귀 | C | observer는 v2인데 runtime만 v404라 `unapproved`가 아니라 `not_observable`로 거부돼도 통과한다. observer와 reason까지 단언한다. |
| CR-21 | HL-12 전체 투영 | C | 주석은 full projection이라 하지만 `unreadable_scanned_paths` 비교가 없다. 필드 목록과 회귀를 보강한다. |
| CR-22 | 동결 계약의 옛 경로 | H | 원문은 당시 handoff의 `artifacts/...`를 가리킨다. 원본 해시를 보존하고 현재 `evidence/README.md`가 저장소 경로를 안내한다. |
| CR-23 | v1.9 `9/9 exit 0` 주장 | H | 같은 원문에 `FileExistsError`가 있어 모순이다. Codex 독립 리뷰가 이미 Windows 비멱등 부채로 공개했으며 현재 성공 주장에서는 제외한다. |
| CR-24 | policy schema 최상위 | C | 최상위 `additionalProperties:false`가 없어 오타가 통과한다. kind별 허용 필드와 함께 엄격하게 닫는다. |
| CR-25 | v1.7 receipt positive test | C | 새 내용의 receipt ID를 재계산하지 않고 예외도 성공처럼 삼켜 정상 신규 저장을 시험하지 않는다. 실제 put·resolve positive를 추가한다. |
| CR-26 | Git 경로 구분 | C | newline 기반 Git 출력 파싱은 특수 경로를 변형해 path policy를 우회할 수 있다. `-z`와 byte 기반 NUL 파싱을 사용한다. |
| CR-27 | applicability trigger schema | C | `risk_profile`과 `path_glob`이 동반 필드 없이 통과한다. trigger별 `if/then required`와 부정 fixture를 추가한다. |
| CR-28 | receipt 저장 전 검증 | C | `put()`이 ID만 추출해 malformed receipt로 불변 key를 선점할 수 있다. 전체 schema·content address를 검증한 뒤 저장한다. |

## 5. RPA-179 종료 조건

RPA-179는 단순히 기존 테스트를 다시 녹색으로 만드는 작업이 아니다. 다음 조건이 모두 충족돼야
이 사후 검토 상태를 해제할 수 있다.

1. 위 C 22건과 Q 3건을 corrected reference에서 재현 가능한 부정 fixture로 고정한다.
2. artifact·policy·waiver·receipt 오류는 예외 성공이나 크래시가 아니라 typed deny가 된다.
3. Git 경로, 루트 스크립트, 동적 import 별칭의 우회 사례가 차단된다.
4. 승인 참조는 문자열 존재가 아니라 보호된 승인 artifact의 역할·subject·기간에 결속된다.
5. 스키마와 실행 규칙이 같은 불변식을 각각 검증하며, 어느 한 층의 우연한 방어만 믿지 않는다.
6. 모든 회귀는 임시 synthetic repo에서 두 번 연속 실행되고 검사 0건의 vacuous pass를 거부한다.
7. 자기공격은 공개 잔여 위험 외 새로운 우회가 하나라도 생기면 non-zero로 종료한다.
8. 수정 후 독립 검토, 해시, Python·OS·dependency digest를 새 evidence bundle에 기록한다.

이 조건을 만족한 뒤에도 바로 `Enforce`하지 않는다. corrected reference를 제품 경계에 연결한 다음
정상·결함 트래픽과 오탐을 `Observe`에서 측정하고 별도 사람 승인을 받아야 한다.

## English Summary

CodeRabbit reported 28 unique findings across two review rounds. Codex independently inspected and
reproduced them instead of treating the AI review as authority: 22 require correction, three are
valid but need narrower impact or a stronger fix, and three are preserved historical or curation
issues. The Phase 0 design decisions remain available for human approval, but the frozen reference
implementation must not be copied into production or used as an enforcement control until RPA-179
produces a corrected, deterministic, independently reviewed reference.
