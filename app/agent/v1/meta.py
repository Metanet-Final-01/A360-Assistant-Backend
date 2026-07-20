"""v1 버전 메타 — registry.available_versions()가 읽는 경량 정보.

전체 에이전트 스택을 import하지 않고 목록/셀렉터에 쓸 표시값만 담는다(무거운 import 금지).
"""

VERSION_META = {
    "label": "v1 · 단계분해 매핑",
    "description": "업무 단계를 분해해 단계별로 액션을 1:1 매핑하는 결정론 파이프라인(레거시).",
}
