"""compact — 대화 컨텍스트 압축 노드 (화면 compact 버튼 → operation="compact").

버튼발 결정론 요청이라 intake(LLM 라우터)를 우회해 직행한다. 압축 이후 이전 대화는
다시 전달되지 않으므로(백엔드 UX 계약) 받은 입력 전체를 남김없이 압축하며, 유실이
영구적이라는 전제 아래 두 가지 방어를 둔다:
1. verbatim 섹션 — 요약하면 깨지는 자료(타 솔루션 카탈로그 등)는 원문 보존
2. 이월 불변식 — 이전 압축본의 decisions·verbatim은 LLM을 믿지 않고 코드로 union 보정
"""

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from ..recommend.stream import emit
from .jsonio import chat_json
from .render import render_compact, render_history
from .state import TYPE_COMPACT, TurnState

logger = logging.getLogger(__name__)

_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "compact.md").read_text(encoding="utf-8")


class VerbatimBlock(BaseModel):
    """요약 금지 원문 블록. kind: catalog|constraint|data 등 자유 슬러그."""

    kind: str = "data"
    content: str


class CompactContext(BaseModel):
    """압축 컨텍스트 — 백엔드 JSONB 저장·매 턴 재주입 대상 (계약 확장 제안분)."""

    schema_version: str = "1.0"
    task_overview: str = ""
    decisions: list[str] = Field(default_factory=list)
    flow_journal: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    verbatim: list[VerbatimBlock] = Field(default_factory=list)


def carry_over(prev: CompactContext | None, new: CompactContext) -> CompactContext:
    """이월 불변식 보정: 이전 decisions·verbatim 중 새 압축본에서 빠진 항목을 되살린다.

    문구 완전일치 기준이라 LLM이 표현을 바꾼 항목은 중복될 수 있다 — 유실보다
    중복이 안전하다는 트레이드오프(결정사항 증발은 복구 불가).
    """
    if prev is None:
        return new
    for d in prev.decisions:
        if d not in new.decisions:
            new.decisions.append(d)
    existing = {(v.kind, v.content) for v in new.verbatim}
    for v in prev.verbatim:
        if (v.kind, v.content) not in existing:
            new.verbatim.append(v)
    return new


def _build_messages(state: TurnState) -> list[dict]:
    user_content = (
        f"[이전 압축본]\n{render_compact(state.get('compact'))}\n\n"
        f"[대화 이력]\n{render_history(state.get('history'))}"
    )
    return [
        {"role": "system", "content": _PROMPT},
        {"role": "user", "content": user_content},
    ]


def compact_node(state: TurnState) -> dict:
    """이전 압축본 + 이력 전체 → 새 CompactContext. type은 무조건 "compact"."""
    emit({"event": "stage", "stage": "compacting", "message": "대화 컨텍스트 압축 중"})
    prev = CompactContext.model_validate(state["compact"]) if state.get("compact") else None
    new = chat_json(_build_messages(state), purpose="compact", model_cls=CompactContext)
    new = carry_over(prev, new)
    turns = len(state.get("history") or [])
    return {
        "turn_type": TYPE_COMPACT,
        "compact_out": new.model_dump(),
        "answer": f"이전 대화 {turns}개 메시지를 압축했어요. 이후 대화는 이 요약을 바탕으로 이어집니다.",
        "sources": [],
    }
