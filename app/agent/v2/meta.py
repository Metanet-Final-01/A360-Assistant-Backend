"""v2 버전 메타 — registry.available_versions()가 읽는 경량 정보.

전체 에이전트 스택을 import하지 않고 목록/셀렉터에 쓸 표시값만 담는다(무거운 import 금지).
"""

VERSION_META = {
    "label": "v2 · Agentic (ReAct)",
    "description": "에이전트가 KB 도구로 직접 조사하며 흐름도 전체를 설계하는 ReAct 루프. 현재 기본.",
}
