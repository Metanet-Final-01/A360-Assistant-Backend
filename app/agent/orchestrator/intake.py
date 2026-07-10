"""intake — 요청 라우팅 노드 (LLM 4분류 + 결정론 가드).

턴의 첫 LLM 호출이며 purpose="intake"로 기록된다 — 백엔드 링 게이지가 이 호출의
usage.prompt_tokens를 측정한다(계약). 그래서 프롬프트 구성이 곧 측정 계약이다:
history 전체 + 이전 compact 전문을 절삭 없이 싣고(대화 누적분이 그대로 반영),
문서·분석·흐름도는 존재 신호 한 줄만 싣는다(고정 크기 — 게이지 왜곡 방지).

가드가 LLM 오분류의 하한을 보장한다: 미지 라우트/파싱 실패 → qa 폴백(턴이 항상
답을 내게), 흐름도 없는 edit → generate 강등(수정 대상이 없으면 신규 산출).
"""

import logging
import re
from pathlib import Path

from pydantic import BaseModel

from ..recommend.stream import emit
from .jsonio import chat_json
from .render import context_signals, render_compact, render_history
from .state import ROUTE_EDIT, ROUTE_GENERATE, ROUTE_QA, ROUTES, TurnState

logger = logging.getLogger(__name__)

_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "intake.md").read_text(encoding="utf-8")

# qa→edit 보정 가드(RPA-98): 흐름도가 있는데 명백한 수정 명령이 qa로 샌 경우를 잡는다.
# 대상이 암묵적인 "…추가해줘"까지 포함하되, 질문/제안은 제외해 'qa로 되묻기' 편향을 지킨다.
_MODIFY_INTENT = re.compile(r"추가|삽입|삭제|제거|없애|변경|수정|고쳐|바꾸|바꿔|교체|대체|넣|빼|옮기|이동")
_QUESTION_MARKER = re.compile(
    r"[?？]|왜|어째서|무엇|뭐|무슨|어떻게|어떤|어때|어떨까|좋을까|나을까|될까|가능|인가|까요|나요|알려|설명"
)


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


def _looks_like_edit(state: TurnState) -> bool:
    """흐름도가 있고 메시지가 (질문이 아닌) 명백한 수정 명령이면 True (RPA-98 가드).

    intake LLM이 대상이 암묵적인 "…추가해줘" 류 수정 요청을 qa로 흘리는 경계 케이스를
    보정한다. 질문(왜/뭐/…?)이거나 흐름도가 없으면 관여하지 않아 기존 'qa로 되묻기'
    편향을 지킨다.
    """
    if not (state.get("recommendation") or {}).get("steps"):
        return False
    msg = state.get("message", "")
    return bool(_MODIFY_INTENT.search(msg)) and not _QUESTION_MARKER.search(msg)


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
    # 흐름도가 있는데 명백한 수정 명령이 qa로 샌 경우 edit로 올린다(RPA-98 — E2E 관찰 오분류).
    if route == ROUTE_QA and _looks_like_edit(state):
        route, reason = ROUTE_EDIT, "흐름도 존재 + 수정 명령 — qa에서 edit로 상향"
    if route == ROUTE_EDIT and not (state.get("recommendation") or {}).get("steps"):
        route, reason = ROUTE_GENERATE, "수정 요청이지만 흐름도가 없어 신규 산출로 전환"

    # reason은 LLM이 만든 근거라 사용자 발화를 인용할 수 있어(PII 축적 방지) 길이를 제한해
    # 로깅한다. route가 주 신호이고 reason은 보조 맥락이다. 원문 전체는 route_reason으로 반환.
    logger.info("intake route=%s (%s)", route, reason[:120])
    # 관측 전용(RPA-105): 최종 라우트 결정을 스트림에 흘린다 — message는 기존과 동일해
    # 프론트 표시 불변(같은 문구 재설정), 백엔드 turn_events가 data(route/reason)를 적재한다.
    emit({"event": "stage", "stage": "routing", "message": "요청 분석 중",
          "data": {"route": route, "reason": reason[:200]}})
    return {"route": route, "route_reason": reason}
