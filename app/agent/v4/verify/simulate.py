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

⚠ **점수축과 결함축을 가른다** (RPA-298): `error` 경로는 Error handler가 있는 흐름도에만
생기고 통과가 가장 어렵다. 통과율에 섞으면 예외 처리를 넣은 후보만 벌점을 받는다 —
그래서 `nominal_pass_rate`(happy·alt)만 점수로 쓰고, 예외 경로 결함은 finding으로 낸다.
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


# 정상 경로(점수축)와 예외 경로(결함축)를 가르는 경계. `error`는 Error handler가 있는
# 흐름도에만 생기므로 통과율에 섞으면 **예외 처리를 넣을수록 점수가 깎인다**(아래 참조).
_ERROR_TRACE = "error"


class SimulationReport(BaseModel):
    """L3 판정 결과 — 정상 경로 통과율(점수축)과 예외 경로 판정(결함축)을 **분리**한다."""

    verdicts: list[TraceVerdict] = Field(default_factory=list)

    @property
    def nominal_pass_rate(self) -> float:
        """`happy`·`alt` 통과율 — deterministic_score·flow_confidence가 쓰는 값.

        ## 왜 `error`를 빼는가 (실측, 2026-07-28)

            00:02  예외처리 없음 → 트레이스 2개 → 통과율 1.00
            00:05  예외처리 없음 → 트레이스 2개 → 통과율 0.50
            00:07  예외처리 있음 → 트레이스 3개 → 통과율 0.00

        `error` 트레이스는 **Error handler가 있는 흐름도에만** 만들어진다(build_traces의
        `has_eh` 게이트). 그런데 그 경로는 통과가 가장 어렵다 — Catch가 실제로 수습하고,
        Finally가 정리하고, 오류 뒤 후속 단계가 안 돌아야 한다. 통과율에 섞으면
        **예외 처리를 넣은 후보만 심사를 하나 더 받고 점수가 깎인다.**

        그 벌점이 다른 신호들과 같은 방향으로 겹쳐 있었다: R12(예외 처리 없음)는 warning이라
        교정 목적 함수에서 빠지고, 회귀 가드는 Try/Catch wrap을 '가중합 무변화'로 항상
        폐기한다. 즉 시스템 전체가 예외 처리를 억제하고 있었고, 산출물이 평면으로 나왔다.

        예외 경로의 결함이 사라지는 것은 아니다 — `from_simulation`이 major finding으로
        내보내 surgeon에게 전달한다. 점수에서 빼고 **결함으로 옮긴** 것이다.
        """
        nominal = [v for v in self.verdicts if v.trace_id != _ERROR_TRACE]
        if not nominal:
            return 1.0
        return sum(1 for v in nominal if v.ok) / len(nominal)

    @property
    def error_path_ok(self) -> bool | None:
        """예외 경로 판정. None은 '판정 안 함'이다 — 예외 처리가 없어 경로 자체가 없었다는 뜻.

        False와 None을 구별하는 이유: 전자는 "예외 처리가 있는데 수습을 못 한다", 후자는
        "예외 처리가 아예 없다"로 처방이 다르다. 하나로 뭉치면 예외 처리가 없는 흐름도가
        '예외 경로 통과'로 보인다(모름 → 침묵 원칙).
        """
        for v in self.verdicts:
            if v.trace_id == _ERROR_TRACE:
                return v.ok
        return None


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
    # 판정 무결성: 트레이서가 만들지 않은 경로 판정은 버리고, 판정이 누락된 경로는
    # 보수적으로 실패 처리한다 — 누락을 빼고 나누면 통과율이 부푼다(전량 누락 시 1.0).
    report.verdicts = [v for v in report.verdicts if v.trace_id in traces]
    judged = {v.trace_id for v in report.verdicts}
    for tid in traces:
        if tid not in judged:
            report.verdicts.append(TraceVerdict(
                trace_id=tid, ok=False,
                issues=["판정관이 이 경로를 판정하지 않음(누락) — 보수적으로 실패 처리"],
            ))
    return report
