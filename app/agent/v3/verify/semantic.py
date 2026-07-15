"""L2 시맨틱 커버리지 검증기 (LLM) — "이 흐름도가 FlowSpec 요구를 달성하는가"를 채점한다.

정적 체커(L0/L1)가 문법을 보장하는 반면, 여기서는 요구사항 req_id별 충족 여부를
아웃라인 기반으로 채점한다. 채점만 하고 수정은 제안하지 않는다 — 발견(verifier)과
수리(surgeon)의 분리로 '고칠 수 있어 보이는 결함에 관대해지는' 편향을 차단한다.

입력은 전체 JSON이 아니라 id 붙은 아웃라인(render_outline 재사용, 토큰 ~1/5)이다.
evidence의 node_id(n1, n2…)는 refine의 EditOps target으로 직결된다.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..orchestrator import edit_ops
from ..orchestrator.jsonio import chat_json

_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "semantic_verify.md").read_text(encoding="utf-8")


class CoverageEntry(BaseModel):
    """요구사항 하나의 충족 판정."""

    req_id: str
    priority: Literal["must", "should"] = "must"
    status: Literal["covered", "partial", "missing", "violated", "unknown"] = "covered"
    evidence: list[str] = Field(default_factory=list, description="근거 노드 id (n1, n2…)")
    note: str | None = Field(None, description="partial/missing/violated의 이유 한 줄")


class CoverageReport(BaseModel):
    """L2 채점 결과 — 심판 루브릭·refine findings·flow_confidence의 원료."""

    entries: list[CoverageEntry] = Field(default_factory=list)
    scenario_gaps: list[str] = Field(
        default_factory=list, description="요구에는 없지만 실무상 비는 시나리오 ('빈 파일이면?' 등)"
    )

    @property
    def must_coverage(self) -> float:
        """must 요구 중 covered 비율 (0.0~1.0). must가 없으면 1.0."""
        musts = [e for e in self.entries if e.priority == "must"]
        if not musts:
            return 1.0
        return sum(1 for e in musts if e.status == "covered") / len(musts)

    def hard_gate_failures(self) -> list[CoverageEntry]:
        """심판 하드 게이트 — must인데 missing인 요구들."""
        return [e for e in self.entries if e.priority == "must" and e.status == "missing"]


def _render_spec(spec: dict) -> str:
    lines = [f"목표: {spec.get('goal', '')}"]
    for r in spec.get("requirements") or []:
        lines.append(f"- [{r.get('req_id')}] ({r.get('priority', 'must')}) {r.get('text', '')}")
    if spec.get("error_policy"):
        lines.append("예외 처리 기대:")
        lines += [f"  - {p}" for p in spec["error_policy"]]
    return "\n".join(lines)


def run_semantic_check(spec: dict, flow: dict, *, purpose: str = "verify_semantic") -> CoverageReport:
    """FlowSpec 요구 × 흐름도 아웃라인 → CoverageReport (LLM 1회 + jsonio 교정 1회).

    flow는 제자리 변형하지 않는다 — id 부착은 사본에서 한다.
    """
    import copy

    annotated = edit_ops.annotate_ids(copy.deepcopy(flow))
    outline = edit_ops.render_outline(annotated)
    report = chat_json(
        [
            {"role": "system", "content": _PROMPT},
            {
                "role": "user",
                "content": (
                    f"[요구사항 스펙]\n{_render_spec(spec)}\n\n"
                    f"[흐름도 아웃라인]\n{outline}\n\n"
                    "위 흐름도가 각 요구사항을 충족하는지 req_id별로 채점하세요."
                ),
            },
        ],
        purpose=purpose,
        model_cls=CoverageReport,
    )
    # 앵커 무결성: LLM이 스펙에 없는 req_id를 만들어내면 버린다 (환각 채점 차단).
    valid_ids = {r.get("req_id") for r in spec.get("requirements") or []}
    report.entries = [e for e in report.entries if e.req_id in valid_ids]
    # priority는 스펙이 진실 원천 — LLM 출력값을 스펙 값으로 덮는다.
    prio = {r.get("req_id"): r.get("priority", "must") for r in spec.get("requirements") or []}
    for e in report.entries:
        e.priority = prio.get(e.req_id, e.priority)
    return report
