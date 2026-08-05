"""v2 버전 메타 — registry.available_versions()가 읽는 경량 정보.

전체 에이전트 스택을 import하지 않고 목록/셀렉터에 쓸 표시값만 담는다(무거운 import 금지).
"""

VERSION_META = {
    # 어느 버전이 기본인지는 여기 적지 않는다 — env `AGENT_VERSION`으로 정해지고
    # `available_versions()`가 버전별 `default` 불리언으로 이미 내려준다. 설명에 박아두면
    # env를 바꿀 때마다 어긋난다(실제로 AGENT_VERSION=v3인데 "현재 기본"이 남아 있었다).
    "label": "v2 · Agentic (ReAct)",
    "description": "에이전트가 KB 도구로 직접 조사하며 흐름도 전체를 설계하는 ReAct 루프.",
}
