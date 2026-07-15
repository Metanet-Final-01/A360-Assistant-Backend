"""assemble — 단계별 산출을 하나의 Recommendation으로 묶는다.

병렬(Send)로 도착한 step_results를 order로 재정렬해 steps를 만들고, 각 단계가 신고한
variables_used를 통합해 변수 통로를 세우며, needs_input·gaps·notes_candidates와 분석
ambiguities를 notes로 병합한다. 변수 direction은 produce/consume 관계로 정한다:
생산·소비 모두 있으면 local, 소비만이면 input(사용자 지정 필요), 생산만이면 output.

세션 브래킷 봉합·단계 간 dryrun(R7~R8)은 후속(RPA-27b) 범위 — 여기서는 코어
시퀀스 조립까지(11p 기대 산출물 수준)만 한다.
"""

from app.schemas import BotVariable, Recommendation, StepRecommendation

from .state import RecommendState
from .stream import emit


def _build_variables(step_results: list[dict]) -> list[BotVariable]:
    produced: set[str] = set()
    consumed: set[str] = set()
    meta: dict[str, dict] = {}
    for sr in step_results:
        for v in sr.get("variables_used", []):
            name = v.get("name")
            if not name:
                continue
            meta.setdefault(name, v)
            role = (v.get("role") or "").lower()
            if role == "produce":
                produced.add(name)
            elif role == "consume":
                consumed.add(name)
            else:  # 역할 불명은 양쪽에 넣지 않고 local로 취급
                produced.add(name)

    variables: list[BotVariable] = []
    for name, v in meta.items():
        if name in consumed and name not in produced:
            direction = "input"  # 생산자 없이 소비 → 실행 전 사용자가 채워야
        elif name in produced and name not in consumed:
            direction = "output"
        else:
            direction = "local"
        variables.append(
            BotVariable(
                name=name,
                type=v.get("type", "STRING"),
                direction=direction,
                description=v.get("description"),
            )
        )
    return variables


def _build_notes(step_results: list[dict], ambiguities: list[str]) -> str | None:
    lines: list[str] = []
    for sr in step_results:
        for key in ("needs_input", "gaps", "notes_candidates"):
            lines.extend(sr.get(key, []))
    lines.extend(ambiguities)
    # 중복 제거(순서 보존)
    seen: set[str] = set()
    unique = [x for x in lines if x and not (x in seen or seen.add(x))]
    return " · ".join(unique) if unique else None


def assemble_node(state: RecommendState) -> dict:
    """step_results → Recommendation.model_dump()를 recommendation 상태에 쓴다."""
    emit({"event": "stage", "stage": "recommending", "message": "추천안 조립·검수 중"})

    step_results = sorted(state.get("step_results", []), key=lambda s: s.get("order", 0))
    steps = [
        StepRecommendation(step_id=sr["step_id"], actions=sr.get("actions", []))
        for sr in step_results
    ]
    ambiguities = state.get("analysis", {}).get("ambiguities", [])
    recommendation = Recommendation(
        steps=steps,
        variables=_build_variables(step_results),
        notes=_build_notes(step_results, ambiguities),
    )
    return {"recommendation": recommendation.model_dump()}
