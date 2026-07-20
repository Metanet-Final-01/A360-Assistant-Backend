"""트리거/스케줄 제안 (A-2, RPA-206 소비부) — 업무의 '언제'를 실행 방식 제안으로 잇는다.

업무정의서의 시점 표현("매일 아침 9시", "메일이 오면", "폴더에 파일이 생기면")은 흐름도
액션이 아니라 실행 방식(트리거 패키지 or Control Room 스케줄)의 영역인데, 지금까지는
추천안에 그 개념 자체가 없어 버려졌다. 여기서 FlowSpec·원문을 결정론 키워드 게이트로
보고, 시점 의도가 있을 때만 트리거 메뉴를 실은 LLM 1콜로 하나를 고른다.

트리거 카탈로그(trigger_schema)는 7패키지·9문서 규모라 검색이 아니라 **전량 메뉴**로
다룬다 — 희귀 소스타입은 하이브리드 검색 상위 k에서 굶는다(후단 필터 실측 0-hit).
트리거 행이 없거나(구 카탈로그) 의도가 없으면 None — 기능이 조용히 쉬는 하위호환.
"""

import logging
import re
from pathlib import Path

from pydantic import BaseModel

from app.schemas.recommendation import RagSource, TriggerRecommendation

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


def recommend_trigger(spec: dict, document: str | None) -> dict | None:
    """FlowSpec·원문에서 실행 시점 의도를 감지해 TriggerRecommendation dict를 반환한다.

    의도 없음 / 트리거 카탈로그 없음+비시간 의도 / LLM 실패 → None (추천은 그대로 진행).
    """
    text = _intent_text(spec, document)
    if not _INTENT.search(text):
        return None

    catalog = get_catalog()
    list_fn = getattr(catalog, "list_trigger_schemas", None)
    rows: list[dict] = list_fn() if callable(list_fn) else []

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
