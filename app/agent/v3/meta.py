"""v3 버전 메타 — registry.available_versions()가 읽는 경량 정보.

전체 에이전트 스택을 import하지 않고 목록/셀렉터에 쓸 표시값만 담는다(무거운 import 금지).
"""

VERSION_META = {
    "label": "v3 · Quality Loop (다중 후보+심판)",
    "description": (
        "요구 정형화(FlowSpec)→선행 조사→전문가 관점 다중 후보→계층 검증(정적·데이터플로우·"
        "시맨틱·시뮬레이션)→심판→EditOps 패치 refine. 미확정 값은 질문 카드로 사후 수집."
    ),
}
