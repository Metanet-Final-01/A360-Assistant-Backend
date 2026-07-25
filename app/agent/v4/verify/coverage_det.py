"""결정론 요구 커버리지 — "담당 액션이 하나도 없는 요구"를 LLM 없이 판정한다 (설계 §5.2-A·B).

**왜 필요한가.** 검수 스택 전체가 '잘못 넣은 것'만 본다(R1 환각은 blocker인데, 있어야 할
액션을 안 넣으면 어떤 규칙도 발화하지 않는다 — 설계 §5.1-③). 그래서 모델의 합리적 전략이
"확신 없으면 빼기"가 되고, 교정 루프의 목적 함수(정적 위반 가중합)까지 삭제를 보상한다.
실측 서명: 정밀도 0.295→0.430(상승) vs 재현율 0.109→0.151(정체). 누락을 환각과 **동급
blocker**로 올려 넣기/빼기의 무게를 대칭화하는 것이 이 모듈의 목적이다.

**왜 LLM이 아니라 결정론인가.** §5.2-B의 2단 판정 중 1단이다 — "슬롯 미배정"은 문자열
대조로 끝나는 확정 누락이라 오탐이 0이다. "배정됐지만 부적합"은 판단이 필요해 L2
시맨틱(semantic.py, LLM)이 major로 본다. 오탐 0인 신호만 blocker로 쓴다.

**침묵 원칙.** 흐름도 액션에 req_id가 **하나도** 없으면 빈 목록을 낸다. 앵커 미기재는
'요구를 안 지켰다'가 아니라 '연결 정보가 없다'이므로, 검사하면 전량 오탐이 된다.
checker의 R9/R10이 produces 명시가 하나도 없으면 침묵하는 것과 같은 원칙이다.
"""

from .findings import Finding


def _assigned_req_ids(flow: dict) -> set[str]:
    """흐름도의 모든 액션(children 재귀 포함)이 들고 있는 req_id 집합."""
    found: set[str] = set()

    def walk(actions) -> None:
        for a in actions or []:
            if not isinstance(a, dict):
                continue
            rid = a.get("req_id")
            if isinstance(rid, str) and rid.strip():
                found.add(rid.strip())
            walk(a.get("children"))

    for step in flow.get("steps") or []:
        if isinstance(step, dict):
            walk(step.get("actions"))
    return found


def _spec_requirements(spec: dict) -> list[dict]:
    return [r for r in (spec or {}).get("requirements") or [] if isinstance(r, dict) and r.get("req_id")]


def missing_requirements(flow: dict, spec: dict) -> list[str]:
    """요구 중 담당 액션이 하나도 없는 것 — LLM 없이 판정한다(오탐 0).

    반환 순서는 spec의 요구 순서를 유지한다(사람이 읽는 순서 = 업무 순서).
    흐름도에 req_id 배정이 전무하면 빈 목록 — 모듈 docstring의 침묵 원칙.
    """
    reqs = _spec_requirements(spec)
    if not reqs:
        return []
    assigned = _assigned_req_ids(flow)
    if not assigned:
        return []
    return [r["req_id"] for r in reqs if r["req_id"] not in assigned]


def coverage_findings(flow: dict, spec: dict) -> list[Finding]:
    """미배정 must 요구 → blocker Finding. severity는 findings.Finding 규약을 따른다.

    must 미배정 = blocker(=R1 환각과 동급, 가중치 100), should 미배정 = minor.
    from_coverage(L2 LLM 채점)의 등급 규약과 같은 축을 쓴다 — 두 신호가 같은 회귀 가드
    가중합에 섞여도 등급이 어긋나지 않게.

    fix_hint는 surgeon에게 **삭제가 아니라 삽입**을 지시한다. 위반 있는 액션을 지우는
    가장 싼 경로를 막는 것이 결정 A의 요지이므로, 힌트에서도 그 경로를 닫아 둔다.
    """
    by_id = {r["req_id"]: r for r in _spec_requirements(spec)}
    out: list[Finding] = []
    for rid in missing_requirements(flow, spec):
        req = by_id[rid]
        text = str(req.get("text") or "").strip()
        priority = req.get("priority") or "must"
        out.append(
            Finding(
                layer="L2",
                severity="minor" if priority == "should" else "blocker",
                req_id=rid,
                message=f"[{rid}] 미배정: 요구 '{text}'를 담당하는 액션이 흐름도에 없습니다.",
                fix_hint=(
                    f"이 요구를 실현하는 액션을 삽입(insert/wrap)하고 그 액션에 req_id='{rid}'를 "
                    f"부여하라. 기존 액션을 지워서 해결하려 하지 말 것."
                ),
            )
        )
    return out
