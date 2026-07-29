"""검증 계층 공통 산출 포맷 Finding — L0/L1(정적)·L2(시맨틱)·L3(시뮬레이션)의 정규화.

심판(judge)과 refine 루프가 계층 구분 없이 소비하는 단일 어휘다. severity 순서가
refine의 처리 우선순위이자 수렴 판정(가중합 단조 감소)의 축이 된다.
"""

from pydantic import BaseModel, Field

# 심각도 가중치 — refine 회귀 가드의 '위반이 줄었는가' 판정에 쓴다.
SEVERITY_WEIGHT = {"blocker": 100, "major": 10, "minor": 3, "warning": 1}


class Finding(BaseModel):
    """검증 계층 공통 발견 사항 한 건."""

    layer: str = Field(description="L0|L1|L2|L3|judge")
    severity: str = Field("major", description="blocker|major|minor|warning")
    rule: str | None = Field(None, description="R1~R18 (정적 계층일 때)")
    req_id: str | None = Field(None, description="FlowSpec 요구 id (시맨틱 계층일 때)")
    location: str | None = Field(None, description="트리 경로 또는 node_id")
    step_id: str | None = None
    message: str = ""
    fix_hint: str | None = Field(None, description="surgeon에게 줄 수리 힌트")


def weight(findings: list[Finding]) -> int:
    """심각도 가중합 — 낮을수록 좋다. refine 라운드 회귀 가드의 비교값."""
    return sum(SEVERITY_WEIGHT.get(f.severity, 10) for f in findings)


# R3는 질문 카드로 승격되므로 결함 축에서 분리한다 (설계 관찰 3 — 정보 부족 ≠ 결함).
_CARD_RULES = {"R3"}
# Violation.severity가 규칙 기본값을 덮을 수 있는 값. 기본값 "error"는 여기 없으므로
# 대부분의 위반은 규칙 기본 심각도를 그대로 쓴다(하위호환).
#
# 양방향인 이유: 한 규칙이 성격이 다른 조건들을 담는다. R13은 "Try에 짝이 없다"(A360이
# 저장·실행을 거부 → blocker)와 "Try 본문이 비었다"(장식 Try → warning)를 함께 낸다.
# warning 방향으로만 열려 있으면 실행 불가 결함이 major에 묶여 출고를 못 막는다.
_SEVERITY_OVERRIDE = frozenset({"blocker", "warning"})
# 규칙별 기본 심각도 — R1(환각)은 blocker, 구조·세션은 major, 스타일·경고는 warning.
# R13/R14(제어 흐름 구조)는 실행 의미가 깨지는 결함이라 major (warning 변형은
# Violation.severity가 덮는다 — 빈 Try/Loop 본문 등).
_RULE_SEVERITY = {
    "R1": "blocker",
    "R2": "major", "R4": "major", "R5": "minor", "R6": "major",
    "R7": "major", "R8": "major", "R9": "major", "R11": "major",
    # R13은 조건별로 갈린다 — Try 짝 부재는 Violation.severity="blocker"로 올라온다
    # (A360이 저장·실행을 거부하므로 R1(환각)과 같은 급이다). 빈 Try 본문·단계 분리는 warning.
    "R13": "major", "R14": "major",
    "R10": "warning", "R12": "warning",
    # R15(attended 함정)·R16(플랫폼)은 환경 가정에 의존하는 경고 — 수리를 강제하지 않는다.
    "R15": "warning", "R16": "warning",
    # R17(빈 Step 스캐폴드)·R18(조건 없는 Throw)은 '실행되지 않는데 실행되는 척'하는 결함이라
    # major다 — 화면에는 업무가 있는 것처럼 보이므로 사람이 눈으로 못 잡는다.
    "R17": "major", "R18": "major",
    # R19(경쟁 패키지 혼용)도 실행이 깨지지만, 역할군을 카탈로그에서 **유도**하는지라
    # 오탐 여지가 있어 blocker가 아니라 major다 (derive_competing_packages 주석 참고).
    "R19": "major",
    # R20(신규 개발 비권장 패키지)은 실행이 깨지는 결함이 아니다 — 마이그레이션 봇 유지보수
    # 에서는 정당한 선택이고, 그 패키지에만 있는 액션도 있다. 더 나은 대안을 알리는 신호다.
    "R20": "warning",
}


def from_violations(violations: list) -> tuple[list[Finding], list]:
    """checker Violation 목록 → (Finding 목록, 카드 후보 R3 위반 목록).

    Violation의 severity="warning"(Loop 누수 등)은 규칙 기본값보다 우선한다.
    """
    findings: list[Finding] = []
    card_candidates: list = []
    for v in violations:
        d = v.as_dict() if hasattr(v, "as_dict") else dict(v)
        rule = d.get("rule")
        if rule in _CARD_RULES:
            card_candidates.append(v)
            continue
        severity = _RULE_SEVERITY.get(rule, "major")
        if d.get("severity") in _SEVERITY_OVERRIDE:
            severity = d["severity"]
        findings.append(
            Finding(
                layer="L0" if rule in ("R1", "R2", "R4", "R5", "R6") else "L1",
                severity=severity,
                rule=rule,
                location=d.get("location"),
                step_id=d.get("step_id"),
                message=d.get("message", ""),
            )
        )
    return findings, card_candidates


def from_coverage(report) -> list[Finding]:
    """L2 CoverageReport → Finding 목록. must 미충족은 blocker(심판 하드 게이트 원료)."""
    findings: list[Finding] = []
    for e in report.entries:
        if e.status == "covered":
            continue
        if e.status == "unknown":  # 정보 부족 — 결함이 아니라 카드 후보(finalize가 처리)
            continue
        must = e.priority == "must"
        severity = "blocker" if (must and e.status == "missing") else ("major" if must else "minor")
        findings.append(
            Finding(
                layer="L2", severity=severity, req_id=e.req_id,
                location=(e.evidence[0] if e.evidence else None),
                message=f"[{e.req_id}] {e.status}: {e.note or ''}".strip(),
                fix_hint=e.note,
            )
        )
    for gap in report.scenario_gaps:
        findings.append(Finding(layer="L2", severity="minor", message=f"시나리오 공백: {gap}"))
    return findings


def from_simulation(report) -> list[Finding]:
    """L3 SimulationReport → Finding 목록. 실행 서사가 깨지는 경로는 major."""
    findings: list[Finding] = []
    for v in report.verdicts:
        if v.ok:
            continue
        for issue in v.issues or ["경로 판정 실패"]:
            findings.append(
                Finding(layer="L3", severity="major", message=f"[{v.trace_id}] {issue}")
            )
    return findings
