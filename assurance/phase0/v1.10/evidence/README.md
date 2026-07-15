# Phase 0 증거 안내

> 이 디렉터리는 보증 계약을 만들면서 사용한 원본, 독립 검토, 재실행 결과를 보존한다.
> `frozen/`의 Python 코드는 제품 Backend에 import되는 운영 코드가 아니라 결정론적 참조 구현이다.

## 1. 왜 원본까지 저장하는가

최종 결론만 남기면 왜 특정 통제가 생겼는지, AI가 어떤 잘못된 주장을 했는지, 다른 검토자가
무엇을 실제로 재현했는지 확인하기 어렵다. 그래서 다음 세 층을 분리해 보존한다.

| 층 | 파일 | 목적 |
|---|---|---|
| 설명 | 상위 `README.md`, `VERSION_HISTORY.md` | 사람이 빠르게 이해하는 한국어 중심 안내 |
| 결정 | `../decisions/` | 현재 채택된 경계와 rollout의 기계 판독 정본 |
| 증거 | `verification.md`, `artifact-manifest.sha256`, `frozen/` | 당시 원본과 재현 가능한 검사 |

원본을 많이 저장했다는 사실 자체가 보증은 아니다. 중요한 것은 원본의 역할과 한계를 표시하고,
실제 하네스 구현이 이 계약을 실행 경로에서 소비하도록 만드는 것이다.

## 2. 파일 지도

| 경로 | 작성·검토 주체 | 역할 |
|---|---|---|
| `verification.md` | Codex 재검증 | 실행 환경, 통과·실패·Not-tested 기록 |
| `artifact-manifest.sha256` | 저장소 채택 단계 | 최종 원본과 결정 파일의 내용 무결성 |
| `frozen/claude-contract-v1.10.md` | Claude | 최종 계약, 참조 코드, 당시 실행 로그, 한국어 요약 |
| `frozen/codex-independent-review-v1.10.md` | Codex | 최종 독립 판정과 주장 한계, 한국어 버전 |
| `frozen/codex-independent-check-v1.10.py` | Codex | 공개 수렴 조건을 별도 사례로 재검사 |
| `frozen/claude-post-contract-impact-v1.md` | Claude | 후속 병합과 Agent v3 계획이 방향을 바꾸는지 대조 |
| `frozen/phase0-v1.10-src/` | Claude, Codex 검토 | 스키마·정책·참조 attestor·fixture·회귀 검사 원본 |

`frozen/` 아래의 기존 파일은 SHA-256 검토 대상이므로 내용과 내부 파일명을 소급 정리하지 않는다.
대신 이 안내서에서 역할과 읽는 순서를 제공한다.

## 3. 참조 구현의 구조

`frozen/phase0-v1.10-src/`는 다음 책임을 가진다.

| 파일·폴더 | 역할 | 먼저 읽을 필요 |
|---|---|---|
| `control-registry.yaml` | control ID, trigger, detector, block point, evidence 계약 | 높음 |
| `schemas/` | manifest, report, receipt, runtime, evidence 등의 JSON Schema | 높음 |
| `core.py` | 정본 바이트, digest, Git subject, 정책 artifact 모델 | 구현자 |
| `rules.py` | HL 계열 fail-close 규칙과 정책 결정 | 구현자 |
| `attest.py` | Change·Output 판정 오케스트레이션과 runner authority | 구현자 |
| `store.py` | 경계·영수증·관측 저장소 adapter 계약 | 구현자 |
| `policies/` | 기본 미승인 정책과 operation·severity·interface 규칙 | 정책 검토자 |
| `policies_approved_fixture/` | positive test 전용 승인 정책 | 테스트만 |
| `contract_self_test.py` | 66개 fixture 기반 계약 검사와 coverage | 재검증 시작점 |
| `vertical_paths.py` | Change와 Output의 positive·negative 수직 경로 | 재검증 시작점 |
| `selfattack.py` | 공개 주장에 대한 자기공격과 D-16 잔여 위험 | 보안 검토자 |
| `codex_v15_regression.py`~`codex_v19_regression.py` | 이전 독립 검토 결함의 재발 방지 | 회귀 조사 시 |
| `SHA256SUMS.txt` | 위 원본 45개의 내용 해시 | 실행 전후 |
| `.out/` | 실행 중 생성되는 임시 fixture와 보고서 | Git 제외 |

### 이름이 낯선 회귀 파일

`codex_v15_regression.py`의 `v15`는 제품 버전 15가 아니라 **Phase 0 계약 v1.5에서 발견된 결함**을
뜻한다. 같은 방식으로 `v16`부터 `v19`까지 이전 계약 검토의 결함을 다시 주입해 탐지 여부를 본다.
이 이름들은 동결 원본의 import와 해시에 포함되어 있어 원본 안에서는 바꾸지 않았다.

## 4. 권장 읽기 경로

### 의사결정자와 심사위원

1. 상위 [Phase 0 개요](../README.md)
2. [버전별 변화](../VERSION_HISTORY.md)
3. [재검증 결과](verification.md)
4. 필요할 때만 최종 독립 리뷰 원본

### Backend 구현자

1. `../decisions/adoption.json`
2. `control-registry.yaml`
3. `schemas/`
4. `rules.py`, `attest.py`, `store.py`
5. positive·negative 수직 경로

### 보안·감사 검토자

1. `artifact-manifest.sha256`와 `SHA256SUMS.txt`
2. Codex 독립 리뷰
3. `selfattack.py`
4. v1.5~v1.9 회귀 검사
5. `verification.md`의 Not-tested와 후속 조건

## 5. 무결성 확인

상위 산출물은 `evidence/`에서 다음과 같이 확인한다.

```powershell
Get-Content artifact-manifest.sha256 | ForEach-Object {
    $expected, $path = $_ -split '\s+', 2
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { throw "hash mismatch: $path" }
}
```

참조 원본은 `frozen/phase0-v1.10-src/`에서 `SHA256SUMS.txt`의 45개 항목을 실행 전후 비교한다.
Git checkout의 줄바꿈 변환이 원본 해시를 바꾸지 않도록 `.gitattributes`는 `frozen/**`만 `-text`로
보존한다. 현재 설명과 결정 파일은 동결 원본과 분리돼 정상적으로 수정·검토할 수 있다.

## 6. 실행 순서

```powershell
$env:PYTHONIOENCODING = 'utf-8'
Set-Location frozen/phase0-v1.10-src
python -B contract_self_test.py
python -B vertical_paths.py
python -B selfattack.py
Set-Location ..
python -B codex-independent-check-v1.10.py
```

현재 리포의 Python 3.11 의존성에는 `jsonschema`와 `PyYAML`이 선언돼 있지 않다. 이 공백은
RPA-179에서 해결한다. 임의로 전역 패키지를 설치한 결과를 공식 재현으로 표시하지 않는다.

## 7. 이 증거가 증명하지 않는 것

- 실제 GitHub branch protection과 CODEOWNERS가 작동한다는 것
- 운영 애플리케이션이 보증 영수증을 위조할 수 없다는 것
- Backend 저장·latest·history·export가 이미 차단된다는 것
- Agent v3가 현재 병합·승인됐다는 것
- 운영 DB와 실제 LLM 출력이 end-to-end로 검증됐다는 것

이 항목들은 RPA-179~RPA-184 구현과 운영 검증에서 별도 증거를 만들어야 한다.

## English Summary

The evidence directory separates curated explanations, adopted machine-readable decisions, and
byte-preserved originals. The frozen Python package is a reference contract with deterministic
fixtures and regression checks; it is not imported by the Backend and is not a production security
control. File names inside the frozen package remain unchanged to preserve hashes and imports, while
this guide provides a Korean-first map for decision makers, implementers, and security reviewers.
