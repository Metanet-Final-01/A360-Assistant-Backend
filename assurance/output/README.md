# Backend Output Boundary Assurance Observe MVP

> Jira: `RPA-181`
> 현재 모드: `Observe`
> 저장·조회·내보내기 차단 효과: 없음

## 목적

Agent 공개 출력과 사용자의 흐름도 편집 결과를 믿고 바로 저장하지 않고, Backend 저장 경계에서
독립적으로 다시 검사한다. 검사는 `app/services/output_assurance.py`가 소유하며 `app/agent/**`의
checker, prompt, graph, repair 구현을 import하거나 복제하지 않는다.

## 현재 검사

1. `strict_schema`: Pydantic strict 검증과 알 수 없는 필드 탐지
2. `catalog_closure`: digest를 만든 동일 Backend catalog snapshot에서 모든 `package/action`을 확인
3. payload, request, session, source, catalog, validator를 digest로 결속
4. Agent의 `producer_advisory`와 Backend의 `boundary_findings`를 별도 필드로 유지

정상 후보는 `allow_candidate`, 명시적 위반은 `deny`, detector 오류나 catalog 증거 누락은
`unassured`다. 어떤 결과도 현재는 `validated`가 아니며 `assurance_status=unassured_observe`로
표시한다. `deny`와 `unassured`도 기존 추천안 저장을 막지 않는다.

## 저장·latest·history·export 상태 정책

| 모드 | 저장 | latest/history | export |
|---|---|---|---|
| Observe | 기존 저장 유지, 판정만 반환·로그 | 기존 동작 유지 | 기존 동작 유지 |
| Warn | 별도 승인 전 사용 금지 | 별도 승인 전 사용 금지 | 별도 승인 전 사용 금지 |
| Enforce | `deny`·`unassured` 활성 저장 차단 | 검증된 활성 버전만 노출 | 검증된 버전만 허용 |

RPA-182의 첫 Backend 슬라이스는 저장이 성공한 추천 버전마다 내용 주소형 receipt를 제품 DB의
`assurance_receipts`에 append-only로 남긴다. receipt 저장이 실패하면 기존 추천 저장은 유지하되
API 응답에 `assurance_receipt.status=refused`를 명시한다. Observe 단계이므로 이 receipt는 통과
증명이나 배포 승인이 아니며 latest/history/export의 노출 정책도 아직 바꾸지 않는다.

상세 계약과 관리자 조회 방법은 [Evidence & Governance](../evidence/README.md)를 참고한다.

## 남은 의존성

- RPA-184: Agent 공개 응답의 `resolved_agent_version` 계약
- RPA-182: Change receipt 수집 경로와 Backoffice 화면 연결. Backend Output receipt 및 read-only
  API는 첫 슬라이스 구현
- RPA-183: 보호 writer, 정책 보호, 승격 기준

위 의존성과 사람 승인이 완료되기 전에는 `Warn`이나 `Enforce`로 승격하지 않는다.
