"""질문 카드 (needs_input) — R3·모호성·전제를 '위반'이 아닌 1급 산출물로 승격 (v3 설계 §4).

생성 규칙 (build_cards, 결정론):
- missing_param: 잔여 R3 위반에서 기계적으로 변환 — targets는 위반 location에서 나오므로
  환각 여지가 없다. input_type·options·default는 카탈로그 스펙에서 결정론 추출한다
  (select 카드는 R4 위반이 원천 불가능해진다).
- ambiguity: FlowSpec.unknowns에서.
- assumption_confirm: FlowSpec.assumptions에서 — default가 이미 흐름도에 들어가 있고
  사용자는 승인/수정만 한다.
질문 문구만 LLM 1회로 일괄 다듬는다(polish_card_wording) — 실패해도 결정론 초안이 남는다.

채움 규칙 (apply_card_values, LLM 불개입):
- targets 좌표(step_id + node_path + param_name)로 파라미터를 직접 치환하고
  value_source="user"로 기록한다 — 이후 edit·재생성에서 사용자값 보존 규칙의 보호를 받는다.
- ambiguity의 자유 서술 응답만 edit 경로로 위임한다(구조 변경이 필요할 수 있으므로).
"""

import copy
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..recommend.stream import emit, emit_flow_frame
from ..verify.catalog import CatalogLookup
from .jsonio import chat_json

logger = logging.getLogger(__name__)

_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "question_wording.md").read_text(encoding="utf-8")

_PATH_RE = re.compile(r"(actions)\[(\d+)\]|\.children\[(\d+)\]")

# 카탈로그 파라미터 타입 → 카드 input_type (확신 있는 매핑만; 그 외 text).
_INPUT_TYPE_BY_PARAM = {
    "SELECT": "select",
    "RADIO": "select",
    "NUMBER": "number",
    "FILE": "file_path",
    "CREDENTIAL": "credential_ref",
}

_MAX_ASSUMPTION_CARDS = 3  # 카드 남발 방지 — 전제 확인은 영향 큰 순(스펙 기재 순) 상위만


def _node_at(step: dict, node_path: str) -> dict | None:
    """'actions[0].children[1]' 형태의 경로로 단계 안 액션 노드를 찾는다. 없으면 None."""
    idxs: list[int] = []
    pos = 0
    for m in _PATH_RE.finditer(node_path):
        if m.start() != pos:
            return None
        idxs.append(int(m.group(2) if m.group(2) is not None else m.group(3)))
        pos = m.end()
    if not idxs or pos != len(node_path):
        return None
    nodes = step.get("actions") or []
    node = None
    for depth, i in enumerate(idxs):
        if i >= len(nodes):
            return None
        node = nodes[i]
        nodes = node.get("children") or []
    return node


def _spec_param(catalog: CatalogLookup, package: str | None, action: str | None, param: str | None) -> dict:
    if not package or not action or not param:
        return {}
    spec = catalog.get_action_schema(package, action)
    if not spec:
        return {}
    for p in spec.get("parameters", []):
        if p.get("name") == param:
            return p
    return {}


def build_cards(flow: dict, spec: dict | None, r3_violations: list[dict], catalog: CatalogLookup) -> list[dict]:
    """잔여 R3 + FlowSpec unknowns/assumptions → QuestionCard dict 목록 (결정론)."""
    cards: list[dict] = []
    seq = 0

    def _next_id() -> str:
        nonlocal seq
        seq += 1
        return f"card-{seq}"

    # 1) R3 → missing_param (같은 파라미터의 중복 위반은 targets로 합친다)
    by_param: dict[tuple, list[dict]] = {}
    for v in r3_violations:
        key = (v.get("package"), v.get("action"), v.get("param"))
        by_param.setdefault(key, []).append(v)
    for (pkg, act, param), vs in by_param.items():
        pspec = _spec_param(catalog, pkg, act, param)
        ptype = str(pspec.get("type") or "").upper()
        input_type = _INPUT_TYPE_BY_PARAM.get(ptype, "text")
        options = None
        if input_type == "select":
            options = [
                o.get("value") if isinstance(o, dict) else o
                for o in pspec.get("options") or []
            ] or None
        default = pspec.get("default")
        label = pspec.get("label") or param
        cards.append({
            "card_id": _next_id(),
            "kind": "missing_param",
            "question": f"'{label}' 값을 알려주세요 — {pkg}/{act} 액션에 필요합니다.",
            "why": f"카탈로그상 필수 파라미터인데 문서·대화에서 값을 찾지 못했습니다.",
            "targets": [
                {"step_id": v.get("step_id") or "", "node_path": v.get("location") or "", "param_name": param}
                for v in vs if v.get("location")
            ],
            "input_type": input_type,
            "options": options,
            "default": default,
            "blocking": default in (None, ""),
            "resolved": False,
        })

    # 2) FlowSpec.unknowns → ambiguity
    for u in (spec or {}).get("unknowns") or []:
        cards.append({
            "card_id": _next_id(),
            "kind": "ambiguity",
            "question": u.get("what") or "",
            "why": u.get("why_needed"),
            "targets": [],
            "input_type": "text",
            "options": None,
            "default": None,
            "blocking": bool(u.get("blocking")),
            "resolved": False,
        })

    # 3) FlowSpec.assumptions → assumption_confirm (시안이 이미 반영돼 있음 — 승인 요청)
    for a in ((spec or {}).get("assumptions") or [])[:_MAX_ASSUMPTION_CARDS]:
        cards.append({
            "card_id": _next_id(),
            "kind": "assumption_confirm",
            "question": f"이렇게 전제하고 설계했습니다 — 맞나요? \"{a}\"",
            "why": "문서·대화에 명시가 없어 임의로 정한 전제입니다.",
            "targets": [],
            "input_type": "confirm",
            "options": None,
            "default": True,
            "blocking": False,
            "resolved": False,
        })
    return cards


class _WordedCard(BaseModel):
    card_id: str
    question: str
    why: str | None = None


class _WordedCards(BaseModel):
    cards: list[_WordedCard] = Field(default_factory=list)


def polish_card_wording(cards: list[dict], *, purpose: str = "cards") -> list[dict]:
    """카드 질문 문구를 LLM 1회로 일괄 다듬는다 — 실패 시 결정론 초안 그대로 (no-fail).

    구조(targets/input_type/options/default)는 절대 바꾸지 않는다 — 문구만.
    """
    if not cards:
        return cards
    drafts = "\n".join(
        f"- {c['card_id']} [{c['kind']}] Q: {c['question']} / WHY: {c.get('why') or ''}"
        for c in cards
    )
    try:
        worded = chat_json(
            [
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": f"[카드 초안]\n{drafts}"},
            ],
            purpose=purpose,
            model_cls=_WordedCards,
        )
    except (ValueError, RuntimeError) as e:
        logger.warning("카드 문구 다듬기 실패 — 결정론 초안 유지: %s", e)
        return cards
    by_id = {w.card_id: w for w in worded.cards}
    for c in cards:
        w = by_id.get(c["card_id"])
        if w and w.question.strip():
            c["question"] = w.question.strip()
            if w.why and w.why.strip():
                c["why"] = w.why.strip()
    return cards


def apply_card_values(flow: dict, values: dict[str, Any]) -> tuple[int, list[tuple[dict, str]], list[str]]:
    """카드 응답을 흐름도에 결정론 적용한다 (LLM 불개입).

    values: {card_id: 사용자 값}. 반환: (적용_수, edit_위임_목록[(card, 자유서술)], 오류들).

    - missing_param: targets 좌표로 파라미터 값 치환, value_source="user".
    - assumption_confirm: 참이면 승인(변경 없음), 자유 서술이면 edit 위임.
    - ambiguity: 항상 edit 위임(구조 변경 가능성).
    적용된 카드는 resolved=True. flow는 제자리 변형된다.
    """
    steps_by_id = {s.get("step_id"): s for s in flow.get("steps") or []}
    cards = flow.get("needs_input") or []
    by_id = {c.get("card_id"): c for c in cards}
    applied = 0
    needs_edit: list[tuple[dict, str]] = []
    errors: list[str] = []

    for card_id, value in values.items():
        card = by_id.get(card_id)
        if card is None:
            errors.append(f"{card_id}: 존재하지 않는 카드")
            continue
        if card.get("resolved"):
            errors.append(f"{card_id}: 이미 해소된 카드")
            continue
        kind = card.get("kind")

        if kind == "missing_param":
            if not card.get("targets"):  # 좌표 없는 카드 — 조용히 삼키면 응답이 증발한다
                errors.append(f"{card_id}: 적용할 대상 좌표(targets)가 없는 카드")
                continue
            ok_any = False
            for t in card.get("targets") or []:
                step = steps_by_id.get(t.get("step_id"))
                node = _node_at(step, t.get("node_path") or "") if step else None
                if node is None:
                    errors.append(f"{card_id}: 대상 노드를 못 찾음 ({t.get('step_id')}/{t.get('node_path')})")
                    continue
                params = node.setdefault("parameters", [])
                for p in params:
                    if p.get("name") == t.get("param_name"):
                        p["value"] = value
                        p["value_source"] = "user"
                        break
                else:
                    params.append({"name": t.get("param_name"), "value": value, "value_source": "user"})
                ok_any = True
            if ok_any:
                card["resolved"] = True
                applied += 1
        elif kind == "assumption_confirm":
            if value is True or (isinstance(value, str) and value.strip().lower() in ("true", "yes", "y", "확인", "네", "예")):
                card["resolved"] = True
                applied += 1
            elif isinstance(value, str) and value.strip():
                needs_edit.append((card, value.strip()))
            else:
                errors.append(f"{card_id}: 전제 거부에는 수정 내용을 함께 적어야 합니다")
        elif kind == "ambiguity":
            if isinstance(value, str) and value.strip():
                needs_edit.append((card, value.strip()))
            else:
                errors.append(f"{card_id}: 빈 응답")
        else:
            errors.append(f"{card_id}: 알 수 없는 kind '{kind}'")
    return applied, needs_edit, errors


async def fill_cards_node(state: dict) -> dict:
    """operation="fill_cards" 전용 노드 — 카드 응답을 결정론 적용한다 (라우터 우회, LLM 불개입).

    missing_param/assumption 승인은 여기서 끝난다. ambiguity·전제 거부의 자유 서술은
    구조 변경이 필요할 수 있어 edit 노드에 합성 메시지로 위임한다.
    반환은 다른 브랜치 노드와 같은 계약(turn_type 등) — 실행 사실이 type을 찍는다.
    """
    from ..verify.catalog import get_catalog
    from .harness import collect_violations
    from .state import TYPE_ANSWER, TYPE_RECOMMENDATION

    emit({"event": "stage", "stage": "refining", "message": "질문 카드 응답 반영 중"})
    base = state.get("recommendation")
    values = state.get("card_values") or {}
    if not base or not (base.get("steps")):
        return {"turn_type": TYPE_ANSWER,
                "answer": "카드를 반영할 흐름도가 없어요. 먼저 흐름도를 생성해 주세요.", "sources": []}
    if not values:
        return {"turn_type": TYPE_ANSWER,
                "answer": "반영할 카드 응답이 없어요.", "sources": []}

    flow = copy.deepcopy(base)
    applied, needs_edit, errors = apply_card_values(flow, values)

    # 자유 서술 응답(ambiguity·전제 수정)은 edit로 위임 — 카드 문맥을 합성 메시지로 만든다.
    if needs_edit:
        from .edit import edit_node  # 지연 import (순환 방지)

        lines = [f"- ({c.get('card_id')}) 질문: {c.get('question')} / 사용자 답변: {text}"
                 for c, text in needs_edit]
        synth = (
            "질문 카드에 대한 사용자 응답을 흐름도에 반영하라. 응답이 전제와 다르면 해당 "
            "구조·값을 수정하라:\n" + "\n".join(lines)
        )
        edit_out = await edit_node({**state, "message": synth, "recommendation": flow})
        if edit_out.get("turn_type") == TYPE_RECOMMENDATION:
            flow = edit_out["recommendation_out"]
            for c, _text in needs_edit:  # edit가 반영됐으니 해당 카드도 해소 처리
                for card in flow.get("needs_input") or []:
                    if card.get("card_id") == c.get("card_id"):
                        card["resolved"] = True
            applied += len(needs_edit)
        else:
            errors.append("자유 서술 응답을 흐름도에 반영하지 못했습니다: " + (edit_out.get("answer") or ""))

    if applied == 0:
        return {"turn_type": TYPE_ANSWER,
                "answer": "카드 응답을 흐름도에 반영하지 못했어요. " + " / ".join(errors[:3]),
                "sources": []}

    # L0/L1 재검증(무료) — 값이 채워지며 R3가 줄어드는 것을 확인하고 잔여 위반을 갱신한다.
    # confidence 전면 재산정은 하지 않는다: RAG sink가 없는 경로라 재산정하면 근거 점수가
    # 리셋된다. R3는 원래 감점 대상이 아니므로 카드 채움이 액션 confidence를 바꾸지 않는다.
    violations = collect_violations(flow, get_catalog())

    resolved = sum(1 for c in flow.get("needs_input") or [] if c.get("resolved"))
    total = len(flow.get("needs_input") or [])
    answer = f"질문 카드 {applied}건을 흐름도에 반영했어요. ({resolved}/{total} 해소)"
    if errors:
        answer += " 일부는 반영하지 못했어요: " + " / ".join(errors[:3])
    emit_flow_frame(flow, violations, "카드 반영 완료")
    return {
        "turn_type": TYPE_RECOMMENDATION,
        "recommendation_out": flow,
        "change_summary": f"질문 카드 {applied}건 반영",
        "violations": violations,
        "answer": answer,
        "sources": [],
    }
