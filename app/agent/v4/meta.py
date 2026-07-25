"""v4 버전 메타 — registry.available_versions()가 읽는 경량 정보.

전체 에이전트 스택을 import하지 않고 목록/셀렉터에 쓸 표시값만 담는다(무거운 import 금지).
"""

VERSION_META = {
    "label": "v4 · Knowledge Channels (지식층 분리)",
    "description": (
        "v3 품질 루프 위에 도메인 지식층을 버전 밖으로 분리했다. 어휘를 카탈로그에서 유도하고"
        "(수기 상수는 폴백), 검색을 단계별 채널로 격리하며, 공식 문서의 자동화 용례를 "
        "생성에 주입한다. 초안·정밀화 2상 구조."
    ),
}
