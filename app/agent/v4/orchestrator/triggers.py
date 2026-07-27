"""트리거/스케줄 제안 (A-2, RPA-206 소비부) — 업무의 '언제'를 실행 방식 제안으로 잇는다.

업무정의서의 시점 표현("매일 아침 9시", "메일이 오면", "폴더에 파일이 생기면")은 흐름도
액션이 아니라 실행 방식(트리거 패키지 or Control Room 스케줄)의 영역인데, 지금까지는
추천안에 그 개념 자체가 없어 버려졌다. 여기서 FlowSpec·원문을 결정론 키워드 게이트로
보고, 시점 의도가 있을 때만 트리거 메뉴를 실은 LLM 1콜로 하나를 고른다.

## 메뉴는 전량, 검색은 순서 (RPA-298 Phase 3)

예전 주석은 "검색이 아니라 전량 메뉴로 다룬다 — 희귀 소스타입은 상위 k에서 굶는다
(후단 필터 실측 0-hit)"였다. **그 전제가 무너졌다.** 소스 타입 push-down
(`search_actions(pushdown=True)`, v4 검색기만) 이후 `trigger_schema`는 12질의 전부 0건이던
것이 12질의 전부 포화(누적 0 → 48히트)가 됐다 — 실측표는 `knowledge/channels.py` 참조.

그래서 검색을 **켰지만 메뉴를 좁히는 데 쓰지 않는다.** 트리거 카탈로그는 7패키지·9문서라
전량을 실어도 토큰이 싸고, 좁히는 순간 정답이 상위 k 밖으로 밀릴 위험만 생긴다(폐쇄 어휘가
9줄인데 검색으로 거를 이유가 없다). 검색이 주는 건 **순서**다: `channels.TRIGGER`로 의도
표현을 던져 관련도 높은 문서를 메뉴 위로 올린다. LLM 메뉴는 위치 편향이 크고, 같은 패키지에
문서가 여럿일 때 어느 문서가 근거(title·url)로 붙을지도 이 순서가 정한다.

검색기가 없거나(사용자 제공 카탈로그) 검색이 실패하면 카탈로그 순서 그대로 — **기능이
줄지 않는다.** 트리거 행이 없거나(구 카탈로그) 의도가 없으면 None — 기능이 조용히 쉬는
하위호환도 그대로다.
"""

import logging
import re
from pathlib import Path

from pydantic import BaseModel

from app.agent.knowledge import channels
from app.schemas.recommendation import RagSource, TriggerRecommendation

from ..retrieval import get_retriever
from ..verify.catalog import get_catalog
from .jsonio import chat_json

logger = logging.getLogger(__name__)

_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "trigger_pick.md").read_text(encoding="utf-8")

# 시점 의도 게이트 — 이 표현이 없으면 LLM을 부르지 않는다 (결정론, 비용 0).
_INTENT = re.compile(
    r"매일|매주|매월|매시간|아침마다|저녁마다|정기|주기적|스케줄|자동\s*(?:으로)?\s*실행"
    r"|도착하면|수신하면|오면|생기면|생성되면|들어오면|받으면|올라오면|변경되면|눌렀을 때|누르면|단축키|핫키"
)

_MENU_CONTENT_CHARS = 200  # 메뉴 항목당 본문 발췌 길이 — 9건 전량이라 짧게 실어도 총량이 작다

# 원문을 문장으로 자르는 기준. 요구사항·목표는 이미 조각이라 자를 필요가 없다.
_SENTENCE_SPLIT = re.compile(r"[\n.!?。]+")

# 순위 질의에 실을 의도 조각 수·길이 상한. 왜 자르나: `_intent_text`는 원문 1500자를 통째로
# 붙인 게이트용 텍스트다. 그대로 검색에 던지면 "언제 실행하나"라는 신호가 업무 서술에 묻혀
# 임베딩이 흐려진다 — 순위를 얻으려고 던지는 질의라 신호 밀도가 전부다.
_MAX_QUERY_FRAGMENTS = 3
_MAX_QUERY_CHARS = 200


class _TriggerPick(BaseModel):
    none: bool = False
    kind: str = "trigger"
    package: str | None = None
    title: str = ""
    reason: str = ""
    setup_hint: str = ""


def _intent_text(spec: dict, document: str | None) -> str:
    reqs = " ".join(r.get("text") or "" for r in spec.get("requirements") or [])
    return " ".join(filter(None, [spec.get("goal") or "", reqs, (document or "")[:1500]]))


def _intent_fragments(spec: dict, document: str | None) -> list[str]:
    """의도 표현이 **실제로 들어 있는** 조각만 뽑는다 (순위 질의 재료).

    조각 단위로 거르는 이유: 게이트가 통과한 문서라도 시점 문장은 보통 한두 줄이고 나머지는
    업무 절차다. 전체를 던지면 그 한두 줄이 희석돼 순위가 업무 어휘 쪽으로 끌려간다.
    """
    pieces: list[str] = [spec.get("goal") or ""]
    pieces += [(r.get("text") or "") for r in spec.get("requirements") or []]
    pieces += _SENTENCE_SPLIT.split((document or "")[:1500])

    out: list[str] = []
    seen: set[str] = set()
    for piece in pieces:
        fragment = " ".join(piece.split())
        if not fragment or fragment in seen or not _INTENT.search(fragment):
            continue
        seen.add(fragment)
        out.append(fragment)
        if len(out) >= _MAX_QUERY_FRAGMENTS:
            break
    return out


def _rank_query(spec: dict, document: str | None) -> str:
    """메뉴 순위용 질의 한 건. 조각을 못 찾으면 목표 문장으로 물러선다.

    폴백이 필요한 이유: 게이트는 조각들을 공백으로 이어 붙인 텍스트에서 검사하므로
    경계에 걸친 매칭("…자동" + "실행…")으로 통과할 수 있다. 그때 조각은 0건인데 의도는
    있는 상태라, 순위를 포기하기보다 목표 문장으로 던지는 편이 낫다.
    """
    fragments = _intent_fragments(spec, document) or [" ".join((spec.get("goal") or "").split())]
    return " ".join(f for f in fragments if f)[:_MAX_QUERY_CHARS]


def _hit_key(hit: dict) -> tuple:
    """검색 히트를 카탈로그 행과 잇는 키 — (패키지, 제목).

    id로는 못 잇는다: 카탈로그(`list_trigger_schemas`)는 청킹된 31행을 (package, title)로
    접어 9행으로 주고, 검색은 청크 행을 그대로 돌려준다. 같은 문서의 2·3번째 청크가 따로
    순위를 먹지 않도록 접는 키이기도 하다.
    """
    return (hit.get("package_name"), hit.get("title"))


def _ranked_menu(rows: list[dict], query: str) -> list[dict]:
    """검색 순위로 메뉴를 재정렬한다 — **거르지 않는다**. 실패하면 카탈로그 순서 그대로.

    반환 목록은 입력과 같은 집합이다. 이 계약이 깨지면 폐쇄 어휘 검사(메뉴 밖 패키지 거부)가
    검색이 놓친 트리거를 '지어낸 것'으로 오판한다.
    """
    if len(rows) < 2 or not query:
        return rows  # 순서를 매길 게 없다 — 검색을 아예 던지지 않는다(구 카탈로그 포함)

    try:
        retriever = get_retriever()
    except Exception as e:  # noqa: BLE001 — 검색기 부재가 트리거 제안을 막지 않게
        logger.debug("트리거 순위 검색기 확보 실패 (카탈로그 순서 유지): %s", e)
        retriever = None
    if retriever is None:
        return rows

    # search_channel이 실패를 빈 결과로 강등하고 굶주림 집계까지 맡는다(채널 계약).
    hits = channels.search_channel(retriever, channels.TRIGGER, query)
    if not hits:
        return rows
    ranked = channels.merge_by_rank([hits], channels.TRIGGER, key_fn=_hit_key)

    by_pair: dict[tuple, int] = {}
    by_package: dict[str | None, int] = {}
    for i, row in enumerate(rows):
        by_pair.setdefault((row.get("package"), row.get("title")), i)
        by_package.setdefault(row.get("package"), i)

    order: list[int] = []
    used: set[int] = set()
    for hit in ranked:
        # 제목은 같은 컬럼에서 오지만 재적재 세대가 어긋나면 전부 미스가 나고, 그러면 재정렬이
        # **통째로 조용히** 죽는다. 7패키지·9문서라 패키지만으로도 사실상 유일 매칭이다.
        i = by_pair.get(_hit_key(hit), by_package.get(hit.get("package_name")))
        if i is None or i in used:
            continue
        used.add(i)
        order.append(i)
    order += [i for i in range(len(rows)) if i not in used]
    return [rows[i] for i in order]


def recommend_trigger(spec: dict, document: str | None) -> dict | None:
    """FlowSpec·원문에서 실행 시점 의도를 감지해 TriggerRecommendation dict를 반환한다.

    의도 없음 / 트리거 카탈로그 없음+비시간 의도 / LLM 실패 → None (추천은 그대로 진행).

    ⚠️ 시그니처는 고정이다 — `generate.py`가 `asyncio.to_thread(recommend_trigger, spec,
    document)`로 위치 인자 2개만 넘긴다. 검색기는 인자로 받지 않고 여기서 확보한다:
    호출부(a360·오버레이)의 검색기와 트리거 채널에서는 결과가 같다(오버레이는 액션 계열
    소스 타입에만 커스텀 액션을 얹고 `trigger_schema`에는 손대지 않는다).
    """
    text = _intent_text(spec, document)
    if not _INTENT.search(text):
        return None

    catalog = get_catalog()
    list_fn = getattr(catalog, "list_trigger_schemas", None)
    rows: list[dict] = list_fn() if callable(list_fn) else []
    # 전량 메뉴 유지 + 검색 순위로 재정렬. LLM은 package만 고르므로, 같은 패키지에 문서가
    # 여럿이면 아래 `next(...)`가 집는 '첫 행'이 곧 근거 문서다 — 재정렬이 그 첫 행을
    # 질의에 가장 가까운 문서로 바꾼다.
    rows = _ranked_menu(rows, _rank_query(spec, document))

    menu_lines = [
        f"- [{r['package']}] {r['title']}: {(r['content'] or '')[:_MENU_CONTENT_CHARS]}"
        for r in rows
    ]
    # 스케줄은 트리거 패키지가 아니라 Control Room 기능 — 시간 기반 의도를 위해 상시 포함한다.
    menu_lines.append(
        "- [스케줄] Control Room 예약 실행: 시간 기반(매일/매주/특정 시각) 실행은 "
        "Control Room > Activity에서 봇을 예약한다 (트리거 패키지 아님)"
    )

    user = (
        f"[업무 목표]\n{spec.get('goal') or ''}\n\n"
        f"[요구사항 발췌]\n" + "\n".join(f"- {r.get('text')}" for r in (spec.get("requirements") or [])[:8]) + "\n\n"
        f"[실행 방식 메뉴]\n" + "\n".join(menu_lines)
    )
    try:
        pick = chat_json(
            [{"role": "system", "content": _PROMPT}, {"role": "user", "content": user}],
            purpose="recommend",
            model_cls=_TriggerPick,
        )
    except ValueError as e:
        logger.warning("트리거 제안 실패 (생략): %s", e)
        return None
    if pick.none or not pick.title:
        return None

    kind = "schedule" if pick.kind == "schedule" or pick.package is None else "trigger"
    if kind == "trigger":
        # 폐쇄어휘 유지 — 메뉴에 없는 패키지명은 채택하지 않는다(지어내기 방지).
        row = next((r for r in rows if r["package"] == pick.package), None)
        if row is None:
            logger.info("트리거 pick이 메뉴 밖 패키지(%s) — 생략", pick.package)
            return None
        sources = [RagSource(source_type="trigger_schema", title=row["title"], url=row.get("url"))]
    else:
        row, sources = None, []

    return TriggerRecommendation(
        kind=kind,
        package=pick.package if kind == "trigger" else None,
        # 트리거면 title도 카탈로그 canonical 값 — LLM 문구를 그대로 쓰면 표기 1:1 계약이 깨진다.
        title=row["title"] if kind == "trigger" else pick.title,
        reason=pick.reason or None,
        setup_hint=pick.setup_hint or None,
        sources=sources,
    ).model_dump()
