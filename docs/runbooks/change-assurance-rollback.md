# Change Assurance Enforce rollback / break-glass

이 문서는 Enforce 승격 후 검사기 장애가 실제 배포·복구를 막는 경우에만 사용하는 절차다. 일반적인
`deny`나 코드 결함을 우회하는 수단이 아니다.

## 사전 조건

- `RPA-N` 장애 이슈와 영향 범위가 기록되어 있다.
- 요청자와 다른 승인자 2명이 break-glass artifact를 승인했다.
- artifact는 `dev` 또는 `main` 한 브랜치, 시작·만료 시각(최대 2시간), 사유에 결합된다.
- 최근 30일 안에 성공한 rollback drill의 절차 참조와 SHA-256 증거가 있다.
- `python -m assurance.change.rollout`의 승격 상태와 장애 실행의 receipt를 보존했다.

## 실행

1. GitHub ruleset에서 사전에 지정된 break-glass 주체만 만료 시각까지 임시 bypass한다.
2. 필요한 복구 PR만 병합하고 다른 PR에는 bypass를 사용하지 않는다.
3. 검사기·정책·workflow를 마지막 정상 커밋으로 되돌리는 별도 PR을 만든다.
4. Change Assurance를 trusted base에서 Warn으로 실행해 receipt를 남긴다.
5. Enforce 고정 결함 fixture가 다시 차단되는지 확인한 다음 required check를 복원한다.

## 종료 검증

- 임시 bypass를 즉시 제거한다.
- ruleset의 승인 수 2, code-owner review, required Change Assurance check가 원래 값인지 다시 조회한다.
- 복구 PR, Jira, 승인자, 시작·종료 시각, 영향받은 PR, rollback receipt digest를 사후 기록한다.
- 2시간 만료가 지나도 bypass가 남아 있으면 break-glass 실패로 취급한다.

현재 저장소 ruleset에는 bypass actor가 없으므로 이 절차는 활성화되어 있지 않다. Enforce 승격 PR과 별도로
보호된 운영 설정에서 만료 가능한 주체와 제거 자동화를 검증하기 전에는 break-glass 완료를 주장하지 않는다.
