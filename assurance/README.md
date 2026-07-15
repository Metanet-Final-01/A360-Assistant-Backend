# A360 AI 보조 개발 보증

이 디렉터리는 A360에서 AI가 작성한 코드와 제품 AI의 공개 출력을 검증하기 위한 보증 계약의 저장소다.
현재는 **Phase 0 계약과 검증 원본을 고정한 상태**이며, 운영 차단 하네스가 완성됐다는 의미가 아니다.

## 세 가지 하네스

1. **Change Assurance**: AI가 만든 코드 변경을 병합 전에 검증한다.
2. **Backend Output Boundary Assurance**: Agent 공개 출력과 사용자 편집 후보를 Backend 저장 경계에서 검증한다.
3. **Evidence & Governance**: 앞선 판정의 입력, 결과, 정책 및 실행 환경을 연결해 재현 가능한 증거로 남긴다.

제품 Agent 내부의 prompt, graph, model, tool, repair, retry 및 eval은 Agent 담당자의 소유 영역이다.
이 저장소의 Backend 하네스는 공개 계약을 소비하고 저장 경계를 감싸며, `app/agent/**` 내부 구현을 복제하거나 수정하지 않는다.

## 현재 기준

- 확정 계약: [Phase 0 v1.10](phase0/v1.10/README.md)
- 구현 작업: Jira `RPA-178`부터 `RPA-184`까지 순차 추적
- 현재 모드: 계약 고정만 수행하며 제품 경로에는 `Observe`, `Warn`, `Enforce` 어느 모드도 연결하지 않음

모든 검증 결과는 `Baseline-Confirmed`, `Current-Confirmed`, `Not-tested`, `Decision-needed`를 구분한다.
AI 간 교차 검토는 결함 탐색 수단이지 최종 승인이나 보안 인증이 아니다.

## English Summary

This directory is the source of truth for A360's AI-assisted development assurance contract.
It currently preserves the Phase 0 contract and its reproducible evidence only; no production
enforcement is claimed. The Backend harness consumes the Agent's public contract without modifying
Agent internals, and human policy decisions remain the authority for rollout.
