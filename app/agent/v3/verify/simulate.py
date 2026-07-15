"""L3 시뮬레이션 검증 — 결정론 트레이서 + LLM 판정관 (2단 분리).

LLM에게 "머릿속으로 실행해봐"라고만 하면 트레이스 자체를 환각한다. 그래서
1단(트레이서)은 파이썬이 대표 실행 경로를 결정론적으로 펼치고, 2단(판정관)은
사실이 고정된 트레이스 텍스트에 대한 판단만 한다 — 검증기의 신뢰가 검증 대상보다
높아야 한다는 원칙의 구현.

대표 경로:
  - happy   : If는 첫(참) 분기, Loop 본문 1회, Try 성공(Catch 건너뜀)+Finally
  - error   : Try 도중 실패 가정 — Try 표시 후 Catch·Finally 경로
  - alt     : If는 Else(또는 마지막) 분기, Loop 본문 0회 — '아무 일도 없는' 경로
이것은 dryrun 근사이지 실행 보증이 아니다 — 호출부가 notes/confidence에 그 성격을 남긴다.
"""

from pathlib import Path

from pydantic import BaseModel, Field

from ..orchestrator.jsonio import chat_json
from .checker import _eh_role, _if_role, _split_units

_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "simulate_judge.md").read_text(encoding="utf-8")

_MAX_TRACE_LINES = 120  # 트레이스 폭주 방지 — 초과분은 절단 표시


class TraceVerdict(BaseModel):
    """트레이스 한 경로의 판정."""

    trace_id: str
    ok: bool = True
    issues: list[str] = Field(default_factory=list, description="경로가 목적 달성에 실패하는 이유들")


class SimulationReport(BaseModel):
    """L3 판정 결과 — flow_confidence의 시뮬레이션 통과율 원료."""

    verdicts: list[TraceVerdict] = Field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        if not self.verdicts:
            return 1.0
        return sum(1 for v in self.verdicts if v.ok) / len(self.verdicts)


def _fmt_action(action: dict, note: str = "") -> str:
    label = action.get("label") or ""
    params = ", ".join(
        f"{p.get('name')}={p.get('value')!r}"
        for p in (action.get("parameters") or [])[:4]
        if p.get("value") not in (None, "")
    )
    refs = ""
    produces = [r.get("name") for r in action.get("produces") or [] if isinstance(r, dict)]
    consumes = [r.get("name") for r in action.get("consumes") or [] if isinstance(r, dict)]
    if produces:
        refs += f" → 쓰기 {produces}"
    if consumes:
        refs += f" ← 읽기 {consumes}"
    suffix = f"  ({params})" if params else ""
    return f"{action.get('package')}/{action.get('action')} «{label}»{suffix}{refs}{note}"


def _trace_actions(actions: list[dict], mode: str, depth: int, lines: list[str]) -> None:
    """mode: 'happy'|'error'|'alt' — If/Loop/EH 전개 방식을 정한다."""
    pad = "  " * depth
    for kind, group in _split_units(actions):
        if len(lines) > _MAX_TRACE_LINES:
            return
        if kind == "if_group":
            if mode == "alt":
                # else(없으면 마지막) 분기
                idx, act = next(
                    ((i, a) for i, a in group if _if_role(a.get("action")) == "else"),
                    group[-1],
                )
                lines.append(f"{pad}[분기: {_fmt_action(act)} 경로 선택]")
                _trace_actions(act.get("children") or [], mode, depth + 1, lines)
                if not any(_if_role(a.get("action")) == "else" for _, a in group) and len(group) == 1:
                    lines.append(f"{pad}[조건 불충족 — If 본문 건너뜀]")
            else:
                idx, act = group[0]
                lines.append(f"{pad}[분기: {_fmt_action(act)} 조건 참 경로]")
                _trace_actions(act.get("children") or [], mode, depth + 1, lines)
            continue
        if kind == "eh_group":
            for _, act in group:
                role = _eh_role(act.get("action"))
                if role == "try":
                    if mode == "error":
                        lines.append(f"{pad}[Try 시작 — 도중 오류 발생 가정]")
                        _trace_actions((act.get("children") or [])[:1], mode, depth + 1, lines)
                        lines.append(f"{pad}[… Try 나머지는 실행되지 않음]")
                    else:
                        lines.append(f"{pad}[Try 시작 — 정상 실행]")
                        _trace_actions(act.get("children") or [], mode, depth + 1, lines)
                elif role == "catch":
                    if mode == "error":
                        lines.append(f"{pad}[Catch 진입]")
                        _trace_actions(act.get("children") or [], mode, depth + 1, lines)
                    else:
                        lines.append(f"{pad}[Catch 건너뜀 — 오류 없음]")
                elif role == "finally":
                    lines.append(f"{pad}[Finally 실행]")
                    _trace_actions(act.get("children") or [], mode, depth + 1, lines)
            continue
        idx, act = group[0]
        pkg = act.get("package")
        children = act.get("children") or []
        if pkg == "Loop" and children:
            if mode == "alt":
                lines.append(f"{pad}[Loop: 반복 대상 없음 — 본문 0회]")
            else:
                lines.append(f"{pad}[Loop: 본문 1회차 대표 실행] {_fmt_action(act)}")
                _trace_actions(children, mode, depth + 1, lines)
                lines.append(f"{pad}[Loop: 이후 반복은 동일 패턴]")
            continue
        lines.append(pad + _fmt_action(act))
        if children:  # Step 등
            _trace_actions(children, mode, depth + 1, lines)


def build_traces(flow: dict) -> dict[str, str]:
    """대표 경로별 실행 트레이스 텍스트를 만든다 (결정론, LLM 없음)."""
    has_eh = any(
        a.get("package") == "Error handler"
        for step in flow.get("steps") or []
        for a in _iter_tree(step.get("actions") or [])
    )
    modes = ["happy", "alt"] + (["error"] if has_eh else [])
    traces: dict[str, str] = {}
    for mode in modes:
        lines: list[str] = []
        for step in flow.get("steps") or []:
            lines.append(f"== STEP {step.get('step_id')} :: {step.get('label') or ''} ==")
            _trace_actions(step.get("actions") or [], mode, 1, lines)
        if len(lines) > _MAX_TRACE_LINES:
            lines = lines[:_MAX_TRACE_LINES] + ["… (트레이스 절단)"]
        traces[mode] = "\n".join(lines)
    return traces


def _iter_tree(actions: list[dict]):
    for a in actions:
        yield a
        yield from _iter_tree(a.get("children") or [])


def run_simulation(spec: dict, flow: dict, *, purpose: str = "verify_simulate") -> SimulationReport:
    """대표 경로 트레이스를 LLM 판정관에게 해석시킨다 (LLM 1회 — 경로들을 한 호출에 묶음)."""
    traces = build_traces(flow)
    trace_text = "\n\n".join(f"### 경로 {tid} ###\n{body}" for tid, body in traces.items())
    goal = spec.get("goal", "")
    outputs = ", ".join(spec.get("outputs") or []) or "(명시 없음)"
    report = chat_json(
        [
            {"role": "system", "content": _PROMPT},
            {
                "role": "user",
                "content": (
                    f"[봇의 목표]\n{goal}\n[기대 산출물]\n{outputs}\n\n"
                    f"[실행 트레이스]\n{trace_text}\n\n"
                    "각 경로가 목표를 달성하는지, 상태 전이가 말이 되는지 판정하세요. "
                    f"trace_id는 다음만 사용: {list(traces)}"
                ),
            },
        ],
        purpose=purpose,
        model_cls=SimulationReport,
    )
    # 판정 무결성: 트레이서가 만들지 않은 경로 판정은 버린다.
    report.verdicts = [v for v in report.verdicts if v.trace_id in traces]
    return report
