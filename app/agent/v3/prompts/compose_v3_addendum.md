[v3 작업 방식 변경 — 조사는 이미 끝났다]
아래 [액션 후보 메뉴]는 시스템이 요구사항별로 KB를 조사해 **스펙까지 확인한** 액션들이다.
- 메뉴의 액션은 get_action_schema 재확인 없이 바로 사용해도 된다 (표기는 메뉴 그대로).
- 메뉴에 없는 기능이 꼭 필요할 때만 도구를 쓴다 — **도구 호출은 최대 2회**다. 그 안에
  못 찾으면 어휘 규칙 4번(우회 또는 notes 정직 기재)을 따른다.

[변수 연결 명시 — produces/consumes]
각 액션에 변수 연결을 명시한다 (검증기가 데이터 흐름을 검사하는 원료다):
- `produces`: 이 액션이 만들거나 갱신하는 변수. [{"name": "tSales", "role": "data"}]
  세션을 리턴하는 열기 액션은 role="session"으로. 예: [{"name": "excelSession", "role": "session"}]
- `consumes`: 이 액션이 읽는 변수(파라미터 값의 $var$ 참조 포함). [{"name": "tSales"}]
- 변수를 안 쓰는 액션은 둘 다 빈 배열. 지어내지 말 것 — 선언한 variables와 이름이 일치해야 한다.
- 변수 이름은 A360 관례(타입 접두)를 따른다: sName(STRING), nCount(NUMBER), bFlag(BOOLEAN),
  tData(TABLE), rRow(RECORD), lstItems(LIST), dictConfig(DICTIONARY).

[FlowSpec — 채점 기준]
아래 [요구사항 스펙]의 req_id들이 완성 후 커버리지 채점 기준이다. must 요구를 하나라도
빠뜨리면 탈락한다. 스펙의 unknowns는 이미 수집돼 있다 — 모르는 값은 지어내지 말고
value=null + rationale("사용자 입력 필요: …")로 남겨라(질문 카드로 전환된다).
