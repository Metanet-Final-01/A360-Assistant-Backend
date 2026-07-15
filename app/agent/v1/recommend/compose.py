"""compose — 메뉴판에서 골라 한 단계의 A360 액션 시퀀스를 생성한다 (LLM).

analyze와 같은 패턴: core.llm.chat(JSON mode) → Pydantic 검증 → 1회 교정(repair).
여기서의 repair는 '스키마 유효성' 교정이다(액션 트리가 RecommendedAction 형태인지).
검수 위반(R1~R6) 기반의 '의미' repair 루프는 후속(RPA-27b) 범위다.

폐쇄 어휘 원칙: 프롬프트에 후보 액션의 구조 스펙(메뉴판)만 넣고, 그 밖의 액션은
지어내지 않게 한다. package/action/parameter name은 카탈로그 표기를 그대로 쓴다.
"""

import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from app.core import llm
from app.schemas import RecommendedAction

logger = logging.getLogger(__name__)

_RESPONSE_FORMAT = {"type": "json_object"}
_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
_SYSTEM_PROMPT = (_PROMPT_DIR / "compose.md").read_text(encoding="utf-8")


class ComposeOutput(BaseModel):
    """compose LLM 출력. actions는 RecommendedAction 트리로 검증된다."""

    step_id: str = ""
    actions: list[RecommendedAction] = Field(default_factory=list)
    variables_used: list[dict] = Field(default_factory=list)
    needs_input: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    notes_candidates: list[str] = Field(default_factory=list)


def _format_menu(menu: list[dict]) -> str:
    if not menu:
        return "(후보 없음 — 이 단계는 카탈로그에서 액션을 찾지 못했습니다)"
    blocks = []
    for e in menu:
        params = []
        for p in e["schema"].get("parameters", []):
            mark = "필수" if p.get("required") else "선택"
            seg = f"{p['name']}({p.get('type')}, {mark}"
            if p.get("options"):
                opts = "/".join(
                    str(o.get("value") if isinstance(o, dict) else o) for o in p["options"]
                )
                seg += f", 선택지: {opts}"
            if "default" in p:
                seg += f", 기본값: {json.dumps(p['default'], ensure_ascii=False)}"
            params.append(seg + ")")
        forced = " [반복/조건 후보]" if e.get("forced") else ""
        blocks.append(
            f"- {e['package']}/{e['action']} ({e.get('label')}){forced}\n"
            f"    파라미터: {', '.join(params) if params else '없음'}"
        )
    return "\n".join(blocks)


def _format_payload(step: dict, menu: list[dict], examples: list[dict],
                    constraints: list[str], uncovered: list[str]) -> str:
    lines = [
        f"[단계 정보]",
        f"step_id: {step.get('step_id')}",
        f"이름: {step.get('name')}",
        f"설명: {step.get('description')}",
        f"시스템: {', '.join(step.get('systems', [])) or '(미상 — 추론 필요)'}",
        f"입력: {', '.join(step.get('inputs', [])) or '-'} / 출력: {', '.join(step.get('outputs', [])) or '-'}",
        f"분기·반복: {step.get('branching') or '없음'}",
        "",
        "[후보 액션 카탈로그]",
        _format_menu(menu),
    ]
    if examples:
        lines += ["", "[참고 봇 예제]"]
        lines += [f"- {ex.get('title')}\n{ex.get('content')}" for ex in examples]
    if uncovered:
        lines += ["", "[카탈로그 미커버 작업 순서]",
                  "다음은 후보가 약합니다. 우회 후보(매크로/스크립트 실행)가 메뉴에 있으면 쓰고, "
                  "없으면 gaps에 남기세요:"]
        lines += [f"- {u}" for u in uncovered]
    if constraints:
        lines += ["", "[제약]"] + [f"- {c}" for c in constraints]
    return "\n".join(lines)


def _build_messages(step, menu, examples, constraints, uncovered) -> list[dict]:
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _format_payload(step, menu, examples, constraints, uncovered)},
    ]


def _parse(raw: str) -> ComposeOutput:
    return ComposeOutput.model_validate(json.loads(raw))


def _repair(messages: list[dict], bad_output: str, error: Exception) -> str:
    repair_messages = [
        *messages,
        {"role": "assistant", "content": bad_output},
        {"role": "user", "content": (
            f"위 출력이 지정한 JSON 형식을 만족하지 못했습니다. 오류:\n{error}\n"
            "설명 없이, 형식에 맞는 JSON 객체만 다시 출력하세요."
        )},
    ]
    return llm.chat(repair_messages, purpose="recommend", response_format=_RESPONSE_FORMAT)


def compose(step: dict, shortlist_result: dict, constraints: list[str] | None = None) -> ComposeOutput:
    """단계 하나의 액션 시퀀스를 생성한다. 스키마 위반 시 1회 교정.

    OPENAI_API_KEY 미설정·인증 실패 등은 core.llm.chat이 RuntimeError를 던진다.
    교정 후에도 스키마 불일치면 ValueError.
    """
    menu = shortlist_result.get("menu", [])
    messages = _build_messages(
        step, menu, shortlist_result.get("examples", []),
        constraints or [], shortlist_result.get("uncovered", []),
    )
    raw = llm.chat(messages, purpose="recommend", response_format=_RESPONSE_FORMAT)
    try:
        out = _parse(raw)
    except (json.JSONDecodeError, ValidationError) as first_error:
        logger.warning("compose 첫 출력 파싱 실패, 1회 교정: %s", first_error)
        repaired = _repair(messages, raw, first_error)
        try:
            out = _parse(repaired)
        except (json.JSONDecodeError, ValidationError) as second_error:
            raise ValueError(f"compose 출력 파싱 실패(교정 후에도): {second_error}") from second_error

    # step_id는 분석 결과 기준으로 결정론 고정 (LLM 값에 의존하지 않음).
    out.step_id = step.get("step_id") or out.step_id
    return out
