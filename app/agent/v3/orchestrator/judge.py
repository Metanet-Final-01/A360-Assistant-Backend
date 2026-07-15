"""judge — 다중 후보 루브릭 채점·승자 선정·이식 지시 (v3 설계 §2-[5]).

병합이 아니라 "승자 + 개선 지시"다: 두 트리의 자동 병합은 세션·변수 정합을 깨는
고위험 연산이라, 패자의 장점은 findings(이식 지시)로 내려 refine이 EditOps로 안전하게
반영한다. 루브릭의 절반 이상은 **결정론 신호**(위반 가중합·must 커버리지·시뮬레이션
통과율)로 앵커링해 LLM 심판의 분산·장황 편향을 줄인다. must 요구 미충족(missing)은
가중합에 묻히지 않는 하드 게이트다.
"""

import copy
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from ..verify.findings import Finding, weight
from .edit_ops import annotate_ids, render_outline
from .jsonio import chat_json

logger = logging.getLogger(__name__)

_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "judge.md").read_text(encoding="utf-8")


class CandidateReport(BaseModel):
    """후보 하나의 검증 스택 요약 — judge 입력이자 verdict 프레임의 원료."""

    candidate_id: str
    persona: str = ""
    flow: dict = Field(default_factory=dict)
    violations: list[dict] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    must_coverage: float | None = None
    gate_failures: list[str] = Field(default_factory=list)  # must인데 missing인 req_id들
    sim_pass_rate: float | None = None
    coverage_by_step: dict[str, str] = Field(default_factory=dict)
    coverage_by_req: dict[str, str] = Field(default_factory=dict)

    def deterministic_score(self) -> float:
        """결정론 앵커 점수 (0~1) — 커버리지·위반 가중합·시뮬레이션의 합성."""
        cov = self.must_coverage if self.must_coverage is not None else 0.5
        w = weight([f for f in self.findings if f.severity != "warning"])
        viol_factor = 1.0 / (1.0 + w / 20.0)  # 가중합 0→1.0, 20→0.5, 100→~0.17
        sim = self.sim_pass_rate if self.sim_pass_rate is not None else 0.7
        return round(0.5 * cov + 0.3 * viol_factor + 0.2 * sim, 3)


class _JudgeScore(BaseModel):
    candidate_id: str
    robustness: float = Field(0.5, ge=0.0, le=1.0, description="예외·세션·경계 상황 대비")
    simplicity: float = Field(0.5, ge=0.0, le=1.0, description="불필요한 복잡도 없음")
    note: str = ""


class _JudgeTransplant(BaseModel):
    to_location: str = Field("", description="승자 흐름도의 위치(step_id 또는 노드 설명)")
    instruction: str = Field("", description="이식할 구조·이유 (surgeon이 실행할 지시)")


class _JudgeOutput(BaseModel):
    scores: list[_JudgeScore] = Field(default_factory=list)
    winner: str = ""
    reason: str = ""
    transplants: list[_JudgeTransplant] = Field(default_factory=list)


def _outline_of(flow: dict) -> str:
    return render_outline(annotate_ids(copy.deepcopy(flow)))


def _render_candidate(r: CandidateReport) -> str:
    err = sum(1 for f in r.findings if f.severity in ("blocker", "major"))
    warn = sum(1 for f in r.findings if f.severity == "warning")
    cov = f"{r.must_coverage:.0%}" if r.must_coverage is not None else "미측정"
    sim = f"{r.sim_pass_rate:.0%}" if r.sim_pass_rate is not None else "미측정"
    gates = ", ".join(r.gate_failures) or "없음"
    top = "\n".join(
        f"  - [{f.severity}·{f.rule or f.req_id or f.layer}] {f.message}"
        for f in sorted(r.findings, key=lambda f: 0 if f.severity == "blocker" else 1)[:6]
    )
    return (
        f"### 후보 {r.candidate_id} ({r.persona})\n"
        f"- 결정론 신호: must 커버리지 {cov} · 오류 위반 {err}건 · 경고 {warn}건 · 시뮬레이션 {sim}\n"
        f"- 하드 게이트 실패(must missing): {gates}\n"
        f"- 주요 발견:\n{top or '  (없음)'}\n"
        f"- 아웃라인:\n{_outline_of(r.flow)}"
    )


def judge_candidates(
    spec: dict, reports: list[CandidateReport], *, purpose: str = "turn_generate"
) -> dict:
    """후보들을 채점해 승자와 이식 지시를 정한다.

    최종 순위 = 결정론 앵커 점수(deterministic_score) 60% + LLM 정성 축 40%.
    하드 게이트: gate_failures 없는 후보가 하나라도 있으면 게이트 실패 후보는 승자 자격
    박탈. LLM 심판이 실패해도 결정론 점수만으로 승자를 낸다(부분 실패 격리).

    반환: {"winner": CandidateReport, "verdict": dict(프레임용), "transplant_findings": [Finding]}.
    """
    assert reports, "후보가 없습니다"
    if len(reports) == 1:
        only = reports[0]
        return {
            "winner": only,
            "verdict": {
                "winner": only.candidate_id, "reason": "단일 후보",
                "scores": [{"candidate_id": only.candidate_id,
                            "deterministic": only.deterministic_score(), "total": only.deterministic_score()}],
            },
            "transplant_findings": [],
        }

    req_lines = "\n".join(
        f"- [{r.get('req_id')}] ({r.get('priority', 'must')}) {r.get('text', '')}"
        for r in spec.get("requirements") or []
    )
    llm_out: _JudgeOutput | None = None
    try:
        llm_out = chat_json(
            [
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": (
                    f"[목표]\n{spec.get('goal', '')}\n\n[요구사항]\n{req_lines}\n\n"
                    + "\n\n".join(_render_candidate(r) for r in reports)
                )},
            ],
            purpose=purpose, model_cls=_JudgeOutput,
        )
    except (ValueError, RuntimeError) as e:
        logger.warning("LLM 심판 실패 — 결정론 점수만으로 선정: %s", e)

    llm_scores = {s.candidate_id: s for s in (llm_out.scores if llm_out else [])}
    gate_ok = [r for r in reports if not r.gate_failures]
    eligible = gate_ok or reports  # 전원 게이트 실패면 최악 중 최선을 고른다

    rows = []
    for r in reports:
        det = r.deterministic_score()
        ls = llm_scores.get(r.candidate_id)
        qual = (ls.robustness + ls.simplicity) / 2 if ls else 0.5
        total = round(0.6 * det + 0.4 * qual, 3)
        rows.append({
            "candidate_id": r.candidate_id, "persona": r.persona,
            "deterministic": det, "qualitative": round(qual, 3), "total": total,
            "gate_failed": bool(r.gate_failures),
            "note": (ls.note if ls else ""),
        })
    by_id = {r.candidate_id: r for r in reports}
    winner_row = max(
        (row for row in rows if by_id[row["candidate_id"]] in eligible),
        key=lambda row: row["total"],
    )
    winner = by_id[winner_row["candidate_id"]]

    transplant_findings = [
        Finding(layer="judge", severity="minor", location=t.to_location or None,
                message=f"이식 지시: {t.instruction}", fix_hint=t.instruction)
        for t in (llm_out.transplants if llm_out else [])
        if t.instruction.strip()
    ][:5]

    reason = (llm_out.reason if llm_out and llm_out.winner == winner.candidate_id else "") or (
        f"결정론 신호 우세 (must 커버리지·위반·시뮬레이션 종합 {winner_row['total']})"
    )
    return {
        "winner": winner,
        "verdict": {"winner": winner.candidate_id, "reason": reason, "scores": rows},
        "transplant_findings": transplant_findings,
    }
