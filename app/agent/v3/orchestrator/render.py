"""컨텍스트 → 프롬프트 텍스트 렌더러 (결정론, LLM 없음).

노드마다 "자기 일에 필요한 것만" 프롬프트에 싣는 원칙의 구현 지점:
- intake: history 전체 + compact 전문(절삭 없음 — 링 게이지가 intake prompt_tokens를
  측정하므로 대화 누적분이 그대로 반영돼야 한다) + 문서·흐름도는 존재 신호 한 줄만.
- qa/edit: compact + 흐름도 요약/원문 등 브랜치별 필요분.
"""

import json


def render_history(history: list[dict] | None) -> str:
    """대화 이력 전체를 절삭 없이 렌더한다. 비면 '(없음)'."""
    if not history:
        return "(없음)"
    lines = []
    for turn in history:
        role = "사용자" if turn.get("role") == "user" else "어시스턴트"
        lines.append(f"{role}: {turn.get('content', '')}")
    return "\n".join(lines)


def render_compact(compact: dict | None) -> str:
    """이전 압축본(CompactContext dict)을 고정 섹션 markdown으로 렌더한다."""
    if not compact:
        return "(없음)"
    lines: list[str] = []
    if compact.get("task_overview"):
        lines += ["## 업무 개요", compact["task_overview"]]
    for key, title in (
        ("decisions", "확정된 결정·제약"),
        ("flow_journal", "흐름도 작업 이력"),
        ("open_questions", "미해결 사항"),
    ):
        items = compact.get(key) or []
        if items:
            lines.append(f"## {title}")
            lines += [f"- {item}" for item in items]
    for block in compact.get("verbatim") or []:
        lines += [f"## 보존 원문 ({block.get('kind', 'data')})", block.get("content", "")]
    return "\n".join(lines) or "(없음)"


def context_signals(state: dict) -> str:
    """문서·분석·흐름도의 존재 신호 — 원문 대신 싣는 고정 크기 요약."""
    parsed = state.get("parsed_doc")
    doc = f"있음 ({parsed.get('page_count', '?')}페이지)" if parsed else "없음"
    analysis = state.get("analysis")
    ana = f"있음 ({len(analysis.get('steps', []))}단계)" if analysis else "없음"
    rec = state.get("recommendation")
    flow = f"있음 ({len(rec.get('steps', []))}단계)" if rec else "없음"
    return f"- 업로드 문서: {doc}\n- 업무 분석본: {ana}\n- 현재 흐름도: {flow}"


def flow_outline(recommendation: dict | None) -> str:
    """흐름도 트리를 단계·액션 라벨 개요로 요약한다 (qa 프롬프트용 — 원문 대신)."""
    if not recommendation or not recommendation.get("steps"):
        return "(없음)"

    def walk(actions: list[dict], depth: int) -> list[str]:
        lines = []
        for a in actions:
            label = a.get("label") or f"{a.get('package')}/{a.get('action')}"
            lines.append(f"{'  ' * depth}- [{a.get('order')}] {label} ({a.get('package')}/{a.get('action')})")
            lines.extend(walk(a.get("children") or [], depth + 1))
        return lines

    lines = []
    for step in recommendation["steps"]:
        lines.append(f"{step.get('step_id')}:")
        lines.extend(walk(step.get("actions") or [], 1))
    return "\n".join(lines)


def analysis_brief(analysis: dict | None) -> str:
    """분석본을 단계 목록으로 요약한다 (generate·qa 프롬프트용)."""
    if not analysis or not analysis.get("steps"):
        return "(없음)"
    lines = [f"요약: {analysis.get('summary', '')}"]
    for s in analysis["steps"]:
        systems = ", ".join(s.get("systems") or [])
        lines.append(f"- {s.get('step_id')} {s.get('name')} — {s.get('description', '')}"
                     + (f" [시스템: {systems}]" if systems else ""))
    if analysis.get("ambiguities"):
        lines.append("미확정: " + " / ".join(analysis["ambiguities"]))
    return "\n".join(lines)


def chat_task_brief(state: dict) -> str:
    """채팅으로 서술된 업무 설명 조립 — 문서 없는 analyze(analyze_text) 입력."""
    return (
        f"[이전 대화 압축 요약]\n{render_compact(state.get('compact'))}\n\n"
        f"[대화 이력]\n{render_history(state.get('history'))}\n\n"
        f"[현재 요청]\n{state.get('message', '')}"
    )


def dump_json(obj: dict | None) -> str:
    """프롬프트에 싣는 JSON 직렬화 (한글 보존)."""
    return json.dumps(obj, ensure_ascii=False, indent=1) if obj else "(없음)"
