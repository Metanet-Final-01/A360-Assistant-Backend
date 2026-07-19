# Evidence & Governance Observe 첫 슬라이스

> Jira: `RPA-182`
> 현재 범위: Backend Output receipt 저장과 관리자 read-only API
> 현재 모드: `Observe`

## 왜 일반 로그와 분리하는가

`llm_usage`, `turn_events`, `audit_logs` 같은 관측 로그는 장애 시 제품 요청을 살리기 위해
best-effort로 기록한다. 보증 영수증은 이후 저장·latest·history·export 결정을 설명하는 근거가 될
수 있으므로 같은 유실 허용 경로에 둘 수 없다. 따라서 `assurance_receipts`는 제품 DB에 저장한다.

영수증은 추천 payload 원문이나 사용자 메시지를 복제하지 않는다. 추천 내용은 SHA-256 digest로
결속하고, 판정·검사 버전·catalog digest·누락 증거·마스킹된 finding만 보존한다.

## 한 건의 Output 영수증이 답하는 질문

| 질문 | 필드 |
|---|---|
| 어떤 후보를 검사했는가 | `candidate_id`, `payload_digest`, `recommendation_id`, `recommendation_version` |
| 어떤 요청과 세션이었는가 | `request_id`, `session_id` |
| 어떤 검사와 정책을 썼는가 | `validator_version`, `policy_digest`, `catalog_digest` |
| 입력 관측값이 변조되지 않았는가 | `source_observation_id`, `evidence_valid`, `integrity_valid` |
| 필수 증거가 모두 있었는가 | `completeness_status`, `missing_evidence` |
| 검사와 저장 결과는 무엇인가 | `decision`, `assurance_verdict`, `business_persisted` |
| Agent 버전을 실제로 알 수 있었는가 | `requested_agent_version`, `resolved_agent_version` |

`assurance_verdict=observed`는 Observe 검사가 완전하게 기록됐다는 뜻이지 승인이나 검증 완료가
아니다. 증거가 누락되거나 source observation 무결성이 깨지면 `refused`로 기록한다. 명시적 계약
위반은 `deny`다. Agent/chat 후보는 RPA-184의 공개 `resolved_agent_version` 증거가 들어오기 전까지
버전을 추측하지 않고 `refused`가 정상이다.

## 쓰기와 무결성 계약

1. 추천 버전을 제품 DB에 먼저 commit한다.
2. 확정된 추천 ID·버전과 Output observation으로 최소 영수증을 만든다.
3. canonical JSON의 SHA-256을 `receipt_digest`로 사용한다.
4. 별도 제품 DB transaction으로 영수증을 INSERT한다.
5. 같은 digest의 재시도만 idempotent 성공으로 인정한다.
6. UPDATE/DELETE는 DB trigger가 거부한다.

Observe에서는 영수증 DB 쓰기 실패가 이미 성공한 추천 저장을 되돌리지 않는다. 대신 응답의
`assurance_receipt.status`를 `refused`로 반환해 증거가 없는 성공을 숨기지 않는다.

## 관리자 조회 계약

- `GET /api/admin/assurance-receipts`: 목록과 증분 수집. `harness`, `decision`,
  `assurance_verdict`, `request_id`, `session_id`, `since`, `cursor`, `limit` 필터를 지원한다.
  첫 응답의 `next_cursor`를 다음 요청의 `cursor`로 보내 같은 시각에 생성된 행도 빠짐없이 읽는다.
- `GET /api/admin/assurance-receipts/{receipt_digest}`: 마스킹된 상세 payload와 digest 재계산 결과를
  반환한다.
- POST/PUT/PATCH/DELETE API는 제공하지 않는다.
- 기존 `require_admin` 권한을 사용한다.

## 현재 한계와 다음 단계

- 이 슬라이스가 직접 쓰는 것은 Output receipt뿐이다. Change Assurance 파일 artifact를 제품 DB로
  전달하는 보호된 CI writer transport는 별도 구현·검토가 필요하다.
- Backoffice 화면은 아직 연결하지 않았다. 위 read-only API가 화면의 계약이다.
- 보존기간은 별도 사람 결정 전 삭제하지 않는 append-only 정책이다. 법무·개인정보 정책이 바뀌면
  무결성 연쇄와 삭제 증명 방식을 먼저 결정해야 한다.
- DB 관리자 권한까지 막는 외부 서명·WORM 저장소는 아직 없다. 현재 무결성은 내용 주소와 앱 경로
  변조 탐지, DB trigger 수준이다.
- `Warn`이나 `Enforce` 전환, latest/history/export 차단은 이 구현만으로 허용되지 않는다.

## English Summary

RPA-182 stores PII-minimized, content-addressed Output assurance receipts in the primary product
database because best-effort observability storage is not authoritative enough for later policy
decisions. The table is append-only at the database layer and exposed only through admin read-only
APIs. This is an Observe-mode evidence slice, not an approval, signature, WORM store, or completed
Change Assurance ingestion path.
