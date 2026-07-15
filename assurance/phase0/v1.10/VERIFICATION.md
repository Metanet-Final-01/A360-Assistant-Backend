# Phase 0 v1.10 재검증 기록

> 실행일: 2026-07-15 (Asia/Seoul)
>
> 작업 브랜치: `docs/RPA-178-phase0-assurance-contract`
>
> 현재 구현 기준선: `7d2561819eba107a6a5ffcc32f5420a955dd7028`

## 1. 판정 범위

이 기록의 Current-Confirmed는 **동결된 Phase 0 계약 원본이 아래 환경에서 같은 fixture 결과를
재현했다는 뜻**이다. 현재 제품 코드, 운영 CI, Agent 출력 또는 DB 경로가 보증됐다는 뜻이 아니다.
DB 쓰기, HTTP 요청, 실제 LLM 호출 및 `app/agent/**` 수정은 수행하지 않았다.

## 2. 실행 환경

### Windows 로컬 실행

| 항목 | 값 |
|---|---|
| OS | `Windows-11-10.0.26200-SP0` |
| Python | `3.13.2` (`C:\Python313\python.exe`) |
| jsonschema | `4.26.0` |
| PyYAML | `6.0.3` |
| `pip freeze --all` 항목 수 | 133 |
| 정렬된 `pip freeze --all` SHA-256 | `dbfa402b5ef1e08669a06085dae4c21654c96134661d2f57b5202ea941c66338` |
| `requirements.txt` SHA-256 | `c5321f7b9af1bb273ecb453609939893508fd6c45fdffc59166138977681aa8a` |
| `requirements-dev.txt` SHA-256 | `04ff34630f4e5dc84cc885aa4b5ae6a30969440cc9a91e75345ebfc3b4a7da0c` |
| `pyproject.toml` SHA-256 | `fb0d36dbb1cfd48e6678ca81842bf8d69d3e6f7e57c7f28a00a5b56e188a9926` |
| Git line-ending policy | 로컬 `core.autocrlf=true`; 이 패키지는 `.gitattributes`의 `-text`로 바이트 보존 |

dependency digest는 `python -m pip freeze --all` 결과를 대소문자 구분 정렬하고 LF와 마지막 LF를
적용한 UTF-8 바이트의 SHA-256이다. 전체 목록은 로컬 전역 환경의 불필요한 패키지와 경로 정보를
포함할 수 있어 커밋하지 않고 digest와 직접 사용한 패키지 버전만 기록했다.

### Python 3.11 컨테이너 확인

| 항목 | 값 |
|---|---|
| 이미지 | `a360-assistant-backend-backend:latest` |
| image digest | `sha256:6911907632d9330c7912178151c190ba3840eb5be70de2f60364671dab5ea7cd` |
| Python | `3.11.15` |
| 결과 | **Not-tested** |

컨테이너에서 `contract_self_test.py`를 시작했지만 import 단계에서
`ModuleNotFoundError: No module named 'jsonschema'`로 종료됐다. 리포의 `requirements.txt`와
`requirements-dev.txt`에도 `jsonschema` 및 `PyYAML` 선언이 없다. 따라서 이는 계약 판정 실패가
아니라 검증 도구 의존성 선언 공백이며 Jira `RPA-179` 후속 작업으로 분리한다. 이번 작업에서는
네트워크로 패키지를 임시 설치해 결과를 꾸미지 않았다.

## 3. Current-Confirmed 결과

| 검사 | 결과 | 해석 |
|---|---|---|
| 상위 동결 산출물 무결성 | 4/4 일치 | 계약, 독립 리뷰, 독립 검사, 원본 manifest가 handoff SHA와 동일 |
| 최신 변경 영향 검토 무결성 | 1/1 일치 | 별도 advisory evidence가 handoff SHA와 동일 |
| 채택 overlay 무결성 | 2/2 일치 | 사람 결정 JSON과 검증 스키마가 현재 manifest와 동일 |
| 채택 overlay schema | 정상 1/1, 부정 5/5 거부 | D-24 세션 고정, 임의 승인 버전, Enforce 조기 허용, D-21 누락, 임의 담당자 배정을 거부 |
| 실행 원본 무결성, 실행 전 | 45/45 일치 | `.out/`을 제외한 모든 동결 원본 일치 |
| `contract_self_test.py` | 66/66, mismatch 0, coverage PASS | fixture 기반 계약 검사이며 attestation이 아님 |
| `vertical_paths.py` | 양쪽 positive path 성공, negative 전건 deny | `for_testing_only`와 승인 fixture를 쓴 테스트 경로 |
| `selfattack.py` | 14건 중 4건 우회 재현 | 네 건 모두 보호 runner/writer가 필요한 D-16 잔여 위험으로 공개됨 |
| `independent-check.py` | 5/5 | 수렴 조건과 알려진 수동 정책 목록 부채 재현 |
| 실행 원본 무결성, 실행 후 | 45/45 일치 | 실행으로 동결 원본이 변하지 않음 |

검사에서 생성된 파일 33개는 `phase0-v1.10-src/.out/` 아래에만 존재하며 `.gitignore`로 제외했다.
수직 경로의 live repository 검사는 작업 트리가 dirty인 사실을 관찰하고 예상대로 `deny`했다.

현재 제품 코드의 읽기 전용 대조에서는 `app/agent/v1`, `app/agent/v2`만 발견됐고 v3 구현은 없었다.
명시한 미지 버전은 요청 스키마와 registry가 거부하지만, 환경변수 `AGENT_VERSION`의 미지 기본값은
경고 후 v2로 폴백한다. 자동 compact와 본 요청에는 같은 `payload.agent_version`이 전달된다.
D-24의 v3 선택은 목표 계약이며 D-23의 silent fallback 금지는 아직 구현 완료로 판정하지 않는다.

## 4. 미검증 및 후속 조건

- Python 3.11에서 의존성을 갖춘 재실행: Not-tested, `RPA-179`
- 최신 병합 PR #239, #242, #243의 영향: 코드 판독 검토 완료, 런타임 동작은 Not-tested
- 미병합 PR #249(v3): Planned로만 분류, 현재 구현 사실로 사용하지 않음
- 보호된 GitHub Actions 단일 writer와 정책 CODEOWNERS: 미구현, `RPA-183`
- 실제 Backend 저장·latest·history·export 경계: 미구현, `RPA-181`
- 공개 `resolved_agent_version` 계약: Agent 담당자 합의 필요, `RPA-184`
- 운영 DB, 실제 LLM, 실제 Agent 출력 검증: 이번 작업 비범위

## English Summary

The frozen v1.10 package reproduced 66/66 self-tests, both positive vertical paths, the disclosed
4/14 residual self-attacks, and 5/5 independent checks on Windows with Python 3.13.2. Source
integrity remained 45/45. Python 3.11 verification is not claimed because the existing Backend image
lacks the undeclared `jsonschema` and `PyYAML` tool dependencies; that reproducibility gap is deferred
to RPA-179.
