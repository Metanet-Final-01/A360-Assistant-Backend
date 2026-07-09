"""qa·edit 노드가 LLM에 바인딩하는 KB 접근 툴 (RPA-65).

고정 retrieve→generate 대신 LLM이 필요할 때만 호출하는 방식 — 인사말·컨텍스트 질문에
불필요한 검색을 태우지 않는다. 검색 히트는 sources_sink에 모여 답변 근거(RagSource)로
반환된다. solution != "a360" 세션에는 바인딩하지 않는다(KB가 A360 전용).
"""

import json

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool

from ..retrieval import get_retriever
from ..verify.catalog import get_catalog

_SEARCH_LIMIT = 5


def build_kb_tools(sources_sink: list[dict]):
    """KB 툴 2개를 만든다. 검색 히트 원본은 sources_sink에 누적된다."""
    retriever = get_retriever()
    catalog = get_catalog()

    @tool
    def search_kb(query: str) -> str:
        """A360 지식베이스(패키지·액션·문서·봇 예제)를 의미 검색한다.

        A360 패키지/액션의 존재·용도·사용법 등 사실 확인이 필요할 때 쓴다.
        결과는 JSON 배열(제목·패키지·액션·본문·점수)이다.
        """
        hits = retriever.search(query, limit=_SEARCH_LIMIT)
        sources_sink.extend(hits)
        return json.dumps(
            [
                {k: h.get(k) for k in ("source_type", "package_name", "action_name", "title", "content", "score")}
                for h in hits
            ],
            ensure_ascii=False,
        )

    @tool
    def get_action_schema(package: str, action: str) -> str:
        """(package, action)의 정확한 카탈로그 구조 스펙을 조회한다.

        파라미터의 정확한 name·타입·필수 여부·선택지·기본값이 필요할 때 쓴다.
        표기는 카탈로그 그대로여야 한다 (예: package="Excel_MS", action="GoToCell").
        """
        spec = catalog.get_action_schema(package, action)
        if spec is None:
            return json.dumps({"error": f"'{package}/{action}'은(는) 카탈로그에 없습니다."}, ensure_ascii=False)
        return json.dumps(spec, ensure_ascii=False)

    return [search_kb, get_action_schema]


def execute_tool_calls(tools: list, ai_message) -> list[ToolMessage]:
    """AI 메시지의 tool_calls를 실행해 ToolMessage 목록으로 돌려준다.

    도구 실패는 예외 대신 오류 문자열로 LLM에 돌려준다 — 루프가 죽지 않고
    LLM이 다른 방법(다른 질의·도구 없이 답변)으로 회복할 수 있게 한다.
    """
    by_name = {t.name: t for t in tools}
    out: list[ToolMessage] = []
    for tc in ai_message.tool_calls:
        fn = by_name.get(tc["name"])
        if fn is None:
            content = f"알 수 없는 도구: {tc['name']}"
        else:
            try:
                content = fn.invoke(tc["args"])
            except Exception as e:  # noqa: BLE001 — 도구 오류는 LLM에 알리고 계속
                content = f"도구 실행 실패: {e}"
        out.append(ToolMessage(content=str(content), tool_call_id=tc.get("id") or ""))
    return out


def sink_to_sources(sources_sink: list[dict]) -> list[dict]:
    """누적된 검색 히트를 RagSource 형태 dict로 변환한다 (제목 기준 중복 제거)."""
    seen: set[str] = set()
    out: list[dict] = []
    for h in sources_sink:
        title = h.get("title") or ""
        if not title or title in seen:
            continue
        seen.add(title)
        out.append(
            {
                "source_type": h.get("source_type") or "doc_page",
                "title": title,
                "url": h.get("url"),
                "score": h.get("score"),
            }
        )
    return out
