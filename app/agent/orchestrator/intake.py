"""intake — 요청 라우팅 노드 (LLM 4분류 + 결정론 가드).

턴의 첫 LLM 호출이며 purpose="intake"로 기록된다 — 백엔드 링 게이지가 이 호출의
usage.prompt_tokens를 측정한다(계약). 그래서 프롬프트 구성이 곧 측정 계약이다:
history 전체 + 이전 compact 전문을 절삭 없이 싣고(대화 누적분이 그대로 반영),
문서·분석·흐름도는 존재 신호 한 줄만 싣는다(고정 크기 — 게이지 왜곡 방지).

가드가 LLM 오분류의 하한을 보장한다: 미지 라우트/파싱 실패 → qa 폴백(턴이 항상
답을 내게), 흐름도 없는 edit → generate 강등(수정 대상이 없으면 신규 산출).
"""

import logging
from pathlib import Path

from pydantic import BaseModel

from ..recommend.stream import emit
from .jsonio import chat_json
from .render import context_signals, render_compact, render_history
from .state import ROUTE_EDIT, ROUTE_GENERATE, ROUTE_QA, ROUTES, TurnState

logger = logging.getLogger(__name__)

_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "intake.md").read_text(encoding="utf-8")


class IntakeOutput(BaseModel):
    route: str = ROUTE_QA
    reason: str = ""


def _build_messages(state: TurnState) -> list[dict]:
    user_content = (
        f"[세션 컨텍스트]\n{context_signals(state)}\n\n"
        f"[이전 대화 압축 요약]\n{render_compact(state.get('compact'))}\n\n"
        f"[대화 이력]\n{render_history(state.get('history'))}\n\n"
        f"[사용자 요청]\n{state.get('message', '')}"
    )
    return [
        {"role": "system", "content": _PROMPT},
        {"role": "user", "content": user_content},
    ]


def intake_node(state: TurnState) -> dict:
    """라우트를 판정한다. 인프라 오류(RuntimeError)만 위로 올리고 나머지는 qa 폴백."""
    emit({"event": "stage", "stage": "routing", "message": "요청 분석 중"})
    try:
        out = chat_json(_build_messages(state), purpose="intake", model_cls=IntakeOutput)
        route, reason = out.route, out.reason
    except ValueError as e:  # 교정 후에도 파싱 실패 — 답변 브랜치로 폴백
        logger.warning("intake 분류 실패, qa 폴백: %s", e)
        route, reason = ROUTE_QA, "라우팅 실패 — 일반 답변으로 폴백"

    if route not in ROUTES:
        logger.warning("intake 미지 라우트 %r, qa 폴백", route)
        route, reason = ROUTE_QA, f"미지 라우트({route}) — 일반 답변으로 폴백"
    if route == ROUTE_EDIT and not (state.get("recommendation") or {}).get("steps"):
        route, reason = ROUTE_GENERATE, "수정 요청이지만 흐름도가 없어 신규 산출로 전환"

    # reason은 LLM이 만든 근거라 사용자 발화를 인용할 수 있어(PII 축적 방지) 길이를 제한해
    # 로깅한다. route가 주 신호이고 reason은 보조 맥락이다. 원문 전체는 route_reason으로 반환.
    logger.info("intake route=%s (%s)", route, reason[:120])
    return {"route": route, "route_reason": reason}
