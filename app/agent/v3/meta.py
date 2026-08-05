"""v3 버전 메타 — registry.available_versions()가 읽는 경량 정보.

전체 에이전트 스택을 import하지 않고 목록/셀렉터에 쓸 표시값만 담는다(무거운 import 금지).
"""

VERSION_META = {
    # 다후보·심판은 RPA-357에서 삭제됐다 — 후보가 하나면 고를 것이 없다. 여기 문자열은
    # `GET /api/agent/versions`로 FE 셀렉터에 그대로 뜨므로, 없는 기능을 광고하면 사용자가
    # 보는 설명과 실제 산출이 갈린다. 표기는 recommend/graph.py 상단 파이프라인 도식을 따른다.
    "label": "v3 · Quality Loop (단계 분할+계층 검증)",
    "description": (
        "요구 정형화(FlowSpec)→선행 조사→4단 생성(구조·능력 요청·구조 게이트·값)→계층 검증"
        "(정적·데이터플로우·시맨틱·시뮬레이션)→EditOps 패치 refine. 미확정 값은 질문 카드로 사후 수집."
    ),
}
