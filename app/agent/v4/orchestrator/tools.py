"""qa·edit 노드가 LLM에 바인딩하는 KB 접근 툴 (RPA-65).

고정 retrieve→generate 대신 LLM이 필요할 때만 호출하는 방식 — 인사말·컨텍스트 질문에
불필요한 검색을 태우지 않는다. 검색 히트는 sources_sink에 모여 답변 근거(RagSource)로
반환된다. solution != "a360" 세션에는 바인딩하지 않는다(KB가 A360 전용).
"""

import json
import logging

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool

from app.agent.knowledge import channels

logger = logging.getLogger(__name__)

# 채널이 지정되지 않은 검색(qa — 문서까지 봐야 하는 단계)의 건수.
_SEARCH_LIMIT = 5


def build_kb_tools(
    sources_sink: list[dict],
    ctx=None,
    source_types: list[str] | None = None,
    channel: channels.SearchChannel | None = None,
):
    """KB 툴을 만든다. 검색 히트 원본은 sources_sink에 누적된다.

    검색 범위는 **채널**로 정한다 (RPA-298 — 정의는 `knowledge/channels.py` 하나뿐).
    생성 계열(compose escape hatch·edit)은 액션 어휘를 찾는 자리라 `channels.ACTION`
    (action_schema)이고, qa는 문서까지 봐야 하므로 채널 없이(전체) 검색한다.

    `source_types`는 레거시 인자다 — `recommend/graph.py`가 아직 자기 상수
    `SEARCH_SOURCE_TYPES`를 넘긴다. 넘어오면 `channel_for_source_types`가 채널로 접어
    dossier와 같은 채널을 보게 만든다. 이 접기가 없으면 dossier는 action_schema만, 툴은
    적재 0건인 bot_example까지 포함한 옛 목록을 보는 어긋남이 남는다.

    🔴 **접기에 실패해도 필터를 버리지 않는다.** 채널에 안 맞는 목록(예: 채널을 가로지르는
    ["action_schema","doc_page"])이 오면 목록 그대로 검색한다. 예전엔 None으로 떨어뜨려
    전체 코퍼스를 검색했는데, 그건 "좁은 필터"가 "필터 없음"으로 **강등**되는 것이라
    v3보다 나쁘다 — doc_page가 91%라 랭킹이 문서로 덮인다. 필터 없음은 호출부가
    source_types를 **아예 안 넘겼을 때만**(qa) 성립한다.

    ctx(CatalogContext)가 어휘 출처를 나른다(RPA-285). 검색기가 없는 경로(사용자 제공
    카탈로그)에서는 **search_kb를 아예 만들지 않는다** — 검색할 KB가 없는데 툴을 쥐여주면
    LLM이 빈 결과를 받고 "카탈로그에 없다"로 오판한다. 스펙 조회만 남긴다.
    ctx 미지정은 a360 기본 — 기존 호출부 호환.
    """
    from ..catalog_context import a360_context

    ctx = ctx or a360_context()
    retriever = ctx.retriever
    catalog = ctx.catalog
    channel = channel or channels.channel_for_source_types(source_types)
    # 채널로 안 접힌 목록은 **그대로** 필터로 쓴다 (None이 되면 전체 검색으로 강등된다).
    fallback_types = None if channel is not None else (list(source_types) if source_types else None)

    @tool
    def search_kb(query: str) -> str:
        """A360 지식베이스(패키지·액션·문서)를 의미 검색한다.

        A360 패키지/액션의 존재·용도·사용법 등 사실 확인이 필요할 때 쓴다.
        결과는 JSON 배열(제목·패키지·액션·본문·점수)이다.
        """
        if channel is not None:
            # 채널 경로는 구제 재질의·굶주림 집계를 함께 태운다 — dossier가 타는 것과
            # **같은 함수**여야 두 경로의 결과가 벌어지지 않는다.
            hits = channels.search_channel(retriever, channel, query)
        else:
            hits = retriever.search(query, limit=_SEARCH_LIMIT, source_types=fallback_types)
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
        if spec.get("parameters") is None:
            # params_unknown 행 — '스펙 미상'을 '파라미터 없음'으로 오독해 지어내지 않게 명시.
            return json.dumps(
                {**spec, "note": "액션은 존재하지만 파라미터 스펙이 카탈로그에 없습니다 — "
                                 "문서 근거가 있는 파라미터만 신중히 기입하세요."},
                ensure_ascii=False,
            )
        return json.dumps(spec, ensure_ascii=False)

    return [search_kb, get_action_schema] if ctx.searchable else [get_action_schema]


def tool_calls_data(tool_calls: list[dict]) -> dict:
    """관측 전용(RPA-105) — tool_calls를 turn_events용 구조화 data로 요약한다.

    describe_tool_calls(사람용 문구)와 별개로, 백엔드가 "무슨 검색/조회를 했나"를
    적재할 수 있게 도구 이름·질의어만 뽑는다 (표시·로직에 영향 없음).
    """
    out = []
    for tc in tool_calls or []:
        args = tc.get("args") or {}
        entry: dict = {"name": tc.get("name")}
        if args.get("query"):
            entry["query"] = str(args["query"])[:100]
        if args.get("package") and args.get("action"):
            entry["action"] = f"{args['package']}/{args['action']}"[:80]
        out.append(entry)
    return {"tools": out}


def describe_tool_calls(tool_calls: list[dict]) -> str:
    """tool_calls를 진행 표시용 한글 문구로 요약한다 (qa·edit의 stage 이벤트용).

    어떤 도구를 무슨 인자로 부르는지 사람이 읽게 풀어준다 — search_kb는 질의어를,
    get_action_schema는 패키지/액션을 노출한다. 여러 건이면 앞 2개만 싣는다.
    """
    labels: list[str] = []
    for tc in tool_calls or []:
        args = tc.get("args") or {}
        if tc.get("name") == "search_kb":
            query = (args.get("query") or "").strip()
            labels.append(f"'{query}' 검색 중" if query else "지식베이스 검색 중")
        elif tc.get("name") == "get_action_schema":
            pkg, act = args.get("package"), args.get("action")
            labels.append(f"{pkg}/{act} 스펙 확인 중" if pkg and act else "액션 스펙 확인 중")
        else:
            labels.append("지식베이스 확인 중")
    if not labels:
        return "지식베이스 확인 중"
    if len(labels) <= 2:
        return " · ".join(labels)
    return " · ".join(labels[:2]) + f" 외 {len(labels) - 2}건"


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
            except Exception:  # noqa: BLE001 — 도구 오류는 LLM에 알리고 계속
                # 예외 상세(접속 문자열 등 인프라 정보 가능)는 서버 로그에만 남긴다.
                logger.warning("도구 %s 실행 실패", tc["name"], exc_info=True)
                content = f"도구 실행 실패: {tc['name']} — 다른 질의나 방법으로 시도하세요."
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
