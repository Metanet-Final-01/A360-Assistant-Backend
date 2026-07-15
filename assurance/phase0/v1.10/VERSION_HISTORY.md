# Phase 0 버전별 설계 변화

> 이 문서는 v1.1부터 v1.10까지 Claude가 계약을 수정하고 Codex가 독립 검토한 과정을 한국어로
> 재구성한 후향 요약이다. 초기 v1.1~v1.3 Claude 계약 초안과 일부 검토 원문에는 동등한 한국어
> 설명이 없었기 때문에, 최종 판정과 당시 발견 사항을 바꾸지 않고 이 문서에서 보완한다.

## 1. 왜 열 번이나 수정했는가

Phase 0의 목적은 그럴듯한 설계 문서를 만드는 것이 아니라, AI가 만든 보증 장치 자체도 틀릴 수
있다는 사실을 실제 부정 사례로 확인하는 것이었다. 각 버전은 이전 버전의 통과 결과를 그대로
믿지 않고, 다른 관점의 fixture와 공격 사례를 추가해 다음 질문을 반복했다.

- 검사 대상과 실제 Git·런타임 대상이 정말 같은가?
- 보고서가 스스로 주장한 값을 권위 있는 사실로 오인하지 않는가?
- 실패·누락·판정 불가가 조용히 통과하지 않는가?
- 정책과 영수증을 검사 대상 프로세스가 스스로 바꿀 수 없는가?
- Agent 내부를 침범하지 않고 공개 경계만으로 검증할 수 있는가?

## 2. 한눈에 보는 변화

| 버전 | 독립 판정 | 그 버전에서 확보한 것 | 다음 버전이 필요했던 핵심 이유 |
|---|---|---|---|
| v1.1 | `revision_required` | 세 하네스와 Agent/Backend 소유권 경계의 초안 | 실패한 control과 누락 증거가 `validated`·`assured`로 통과 |
| v1.2 | `revision_required` | JSON Schema와 부정 fixture 확대 | 실제 diff·HEAD 대신 manifest 자기신고를 신뢰, 빈 coverage 허용 |
| v1.3 | `revision_required` | applicability·evidence 결속 규칙 보강 | trusted context, 정책 권위, 참조 무결성이 여전히 호출자 입력 |
| v1.4 | `revision_required` | 실행 원본 공개, 한국어 병기, Observe와 차단 효과 분리 | self-test가 실제 attestor가 아니고 authority·store가 얕게 검증됨 |
| v1.5 | `revision_required` | 실제 attestor, 정책 resolver, 저장 adapter 골격 | Git 대상 분리, 실패 gate의 `attested`, 성공 불가능한 Output 경로 |
| v1.6 | `revision_required` | Change와 Output의 positive 경로 및 adapter 결속 | 보고서 digest가 실제 Change 승인 증명은 아니며 query 의미가 약함 |
| v1.7 | `revision_required` | raw-byte 결정, 영수증 단일화 시도, 8/10 수렴 | 테스트 승인 정책 경로 우회와 영수증 ID의 복수 출처·충돌 |
| v1.8 | `revision_required` | 기준선 재대조, Agent 버전 정책과 canonical receipt | 버전 provenance가 기록만 되고 판정에 미사용, runner 권위 우회 |
| v1.9 | `revision_required` | Agent 정책 연결, 내용 주소, Git parser 보정 | 정책 집합 완전성, registry 관측 결속, caller-controlled root 잔존 |
| v1.10 | `approve_for_human_decision` | 권위 선택 입력 제거, 정확한 정책 집합, 관측 결속, 결정론적 Git | 동결 후 리뷰에서 참조 구현 28건을 추가 처분, RPA-179 교정 전 생산 사용 금지 |

## 3. 버전별 고민과 보정

### v1.1: 하네스의 모양을 처음 고정

- Change, Backend Output Boundary, Evidence & Governance의 세 실패 경계를 분리했다.
- Agent 내부 로직을 복제하지 않고 공개 출력과 Backend 저장 경계만 검증한다는 D-002 초안을 뒀다.
- 독립 부정 fixture에서 control 실패, stage 누락, required gate 건너뜀도 통과하는 fail-open을 찾았다.
- 결론은 “방향은 맞지만 구현 정본으로 채택할 수 없다”였다.

### v1.2: 스키마가 유효하다는 것과 안전하다는 것을 분리

- 스키마 문법과 정상 예시는 통과했지만, 별도 부정 객체가 규약을 우회하는지 검사했다.
- manifest가 실제 diff applicability와 HEAD를 스스로 신고하는 구조를 문제로 확인했다.
- required-control coverage가 빈 상태로 통과하고 candidate·export·runtime evidence가 하나의 대상을
  가리킨다는 보장이 없었다.
- “JSON Schema가 valid하다”를 “업무 전이가 안전하다”로 확대 해석하지 않기로 했다.

### v1.3: 자기신고형 증거를 권위 있는 사실에서 제거

- 실행 맥락, 파생값, 정책 digest를 더 많이 결속했다.
- 그러나 manifest와 report가 서로 다른 대상을 말하는 split-brain 객체가 여전히 통과했다.
- 승인·waiver·정책 파일과 evidence chain의 권위가 호출자에게 남아 있었다.
- Observe 모드에서 보증 판정과 기존 업무 저장 결과를 분리해야 한다는 원칙을 명확히 했다.

### v1.4: 실행 가능한 원본과 한국어 설명을 함께 공개

- 이 버전부터 계약 문서에 동등한 한국어 구간을 추가했다.
- schema, registry, fixture와 self-test 원본을 함께 제공하고 해시로 묶었다.
- 업무 결과, 보증 판정, 실제 차단 효과를 각각 다른 상태로 모델링했다.
- 독립 검토에서는 공개 명령이 실제 attestor가 아니라 fixture self-test이고, 내부 객체가 얕게만
  불변이며, 승인과 저장 조회가 호출자 dictionary에 의존한다는 문제를 찾았다.

### v1.5: 문서 속 규칙을 실제 attestor 호출로 연결

- 실제 Git 대상과 schema·policy를 읽는 attestor, resolver, store adapter를 도입했다.
- dependency 파일을 읽지 못할 때 정상으로 넘기지 않는 fail-close를 보강했다.
- 독립 실행에서는 event HEAD, checkout HEAD, working-tree bytes가 서로 달라질 수 있음을 확인했다.
- 필수 gate가 실패해도 `attested`가 되고, Output의 정상 `assured` 경로는 성공할 수 없으며,
  boundary evidence를 호출자가 함께 만드는 문제가 발견됐다.

### v1.6: Change 결과와 Output 증거를 잇기 시작

- Change의 allow 사례와 Output의 assured 사례가 실제 positive path를 통과하게 했다.
- 실패 gate, 저장 adapter, query evidence의 결속을 강화했다.
- 하지만 보고서 파일의 digest는 실제 Change 하네스가 allow한 영수증이라는 증명이 아니었다.
- adapter 소유권과 조회 시간·사용자 범위가 API 수준에서 강제되지 않았고, Python 객체의 불변성을
  보안 경계처럼 표현한 문구도 철회해야 했다.

### v1.7: 불변성의 한계를 인정하고 raw byte에서 다시 결정

- 정책 결정 시 파싱 객체가 아니라 보존한 raw byte를 다시 읽어 특정 aliasing 공격을 막았다.
- Change와 Output의 positive·negative 수직 경로가 동작했고 종료 기준 10개 중 8개가 수렴했다.
- 다만 일반 생성자가 테스트 전용 승인 정책을 선택할 수 있었고 영수증 ID가 세 위치에서 달라지거나
  같은 commit의 서로 다른 실행이 충돌할 수 있었다.
- 같은 프로세스 안의 악의적 코드를 Python 불변 객체만으로 막을 수 없다는 한계를 D-16으로 분리했다.

### v1.8: 최신 기준선과 Agent 버전 provenance를 계약에 포함

- 최신 `dev` 기준선에서 Agent 공개 경계를 다시 읽고 v1/v2 발견과 향후 v3를 구분했다.
- canonical receipt와 Agent 버전 정책을 추가했다.
- 그러나 `agent_version`과 registry digest는 영수증에 기록만 되고 실제 allow/deny 판정에 쓰이지 않았다.
- 공개 runner 생성 경로가 테스트 정책을 선택할 수 있어 권위 분리가 여전히 구조적이지 않았다.

### v1.9: Agent 버전 정책과 영수증 내용 주소를 실제 판정에 연결

- 미승인 Agent 버전을 거부하고 verdict·evidence·target·policy digest를 영수증 ID에 결속했다.
- Git porcelain의 선행 공백을 보존하도록 parser를 수정했다.
- 독립 검토는 runner가 여전히 호출자 제공 설치 root를 신뢰하며, 존재하는 정책만 확인할 뿐
  기대 정책 전체 집합을 강제하지 않고, Agent registry snapshot이 실제 관측값에 묶이지 않는다고
  확인했다.

### v1.10: Phase 0 종료와 사람 결정 단계 진입

- runner 생성 경로에서 권위를 선택할 수 있는 caller 입력을 제거했다.
- 영수증이 기대 정책 ID 집합과 정확히 일치해야만 통과하도록 했다.
- Agent registry와 공개 계약 digest를 runner 소유 observer의 관측값과 비교했다.
- 실제 Git object와 porcelain 사례를 고정 fixture로 만들어 환경 의존성을 줄였다.
- 독립 검토 5/5가 재현돼 `approve_for_human_decision`으로 종료했다.
- 보호된 writer가 실제로 구성되기 전까지 D-16은 설계 조건이며 운영 보안 인증은 아니라는 한계를
  그대로 남겼다.

## 4. 반복에서 얻은 실무 원칙

1. **Schema 통과는 업무 허용이 아니다.** 형식 검증과 정책 결정을 별도 결과로 둔다.
2. **자기신고를 증거로 쓰지 않는다.** Git, registry, 저장소와 정책은 runner 소유 관측에서 읽는다.
3. **누락은 성공이 아니다.** 필수 입력·정책·evidence가 없으면 판정 불가 또는 거부한다.
4. **테스트 fixture와 운영 권위를 구조적으로 분리한다.** 문자열 label만으로 구분하지 않는다.
5. **내용 주소는 발행 권위가 아니다.** hash와 별개로 writer와 정책 변경 권한을 보호한다.
6. **AI 교차 검토도 다시 검증한다.** Claude와 Codex의 합의 자체를 안전성 증명으로 취급하지 않는다.
7. **모르는 것은 라벨링한다.** Baseline-Confirmed, Current-Confirmed, Not-tested,
   Decision-needed를 섞지 않는다.
8. **팀 경계를 존중한다.** Backend는 Agent 공개 계약을 검증하되 내부 prompt·graph·repair를 소유하지 않는다.

## 5. 언어와 원본 보존 정책

- v1.1~v1.3의 일부 원문에는 한국어 대응 구간이 없었다.
- 이 문서가 초기 버전의 한국어 요약과 버전 간 연결 맥락을 제공한다.
- v1.4 이후 원문은 영문 뒤에 한국어 버전을 함께 두는 방식을 사용했다.
- 최종 v1.10 동결 원본은 문구와 코드를 소급 수정하지 않는다.
- 현재 설명과 사람 결정은 원본 바깥의 README와 `decisions/`에서 관리한다.

## English Summary

Phase 0 evolved through ten adversarially reviewed revisions. Early versions separated the three
harness boundaries but trusted self-reported state and allowed missing evidence to pass. Later
versions introduced executable sources, authoritative observations, positive and negative vertical
paths, content-addressed receipts, Agent version policy, and deterministic Git fixtures. Version
1.10 closed the contract-review loop by removing caller-selected authority, requiring the exact
policy set, and binding registry provenance to a runner-owned observer. Protected runner and writer
operations remain implementation work rather than a proven production security boundary.
