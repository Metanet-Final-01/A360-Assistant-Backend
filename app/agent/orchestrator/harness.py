"""verify harness — 조립된 흐름도 전체에 대한 최종 검수 관문 (R1~R6 + repair 루프).

recommend 파이프라인의 단계별 check가 위반을 '측정'만 했다면(RPA-27a), 여기서는
위반을 LLM으로 '교정'까지 시도한다(RPA-27b 범위 해소). 교정본은 재검수해서
위반이 줄었을 때만 채택한다 — repair가 흐름도를 더 망치면 원본을 유지한다.

catalog는 CatalogLookup 프로토콜이면 무엇이든 된다: a360은 get_catalog(),
타 솔루션은 채팅에서 추출한 UserCatalog — 같은 checker(R1~R6)가 양쪽을 검수한다.
"""

import logging
from pathlib import Path

from app.schemas import Recommendation

from ..recommend.stream import emit
from ..verify.catalog import CatalogLookup
from ..verify.checker import run_checks, run_session_checks
from .jsonio import chat_json
from .render import dump_json

logger = logging.getLogger(__name__)

_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "repair.md").read_text(encoding="utf-8")

# 위반 교정 시도 상한. 1회면 대부분의 표기·파라미터 위반이 잡히고 비용이 바운드된다.
_MAX_REPAIRS = 1


def collect_violations(flow: dict, catalog: CatalogLookup) -> list[dict]:
    """흐름도 전체를 검사한다: R1~R6(단계별 액션 문법) + R7~R8(단계 경계를 넘는 세션 흐름).

    R1~R6은 단계별로 돌며 위반에 step_id를 부착하고, R7~R8은 전체 흐름도를 실행 순서로
    순회한다(위반 액션의 step_id는 checker가 이미 싣는다).
    """
    violations: list[dict] = []
    for step in flow.get("steps", []):
        for v in run_checks(step.get("actions", []), catalog):
            d = v.as_dict()
            d["step_id"] = step.get("step_id")
            violations.append(d)
    for v in run_session_checks(flow.get("steps", [])):
        violations.append(v.as_dict())
    return violations


def _spec_excerpts(violations: list[dict], catalog: CatalogLookup) -> str:
    """위반 액션의 스펙 발췌 — repair가 올바른 표기·파라미터를 보고 고치게 한다."""
    seen: set[tuple[str, str]] = set()
    blocks: list[str] = []
    for v in violations:
        pkg, act = v.get("package"), v.get("action")
        if not pkg or not act or (pkg, act) in seen:
            continue
        seen.add((pkg, act))
        spec = catalog.get_action_schema(pkg, act)
        if spec is None:
            continue
        params = ", ".join(
            f"{p['name']}({p.get('type')}{', 필수' if p.get('required') else ''})"
            for p in spec.get("parameters", [])
        )
        blocks.append(f"- {pkg}/{act}: 파라미터 [{params or '없음'}]")
    return "\n".join(blocks) or "(해당 액션의 스펙 없음 — R1 위반 액션은 제거 대상)"


def _repair_messages(flow: dict, violations: list[dict], catalog: CatalogLookup) -> list[dict]:
    violation_lines = "\n".join(
        f"- [{v['rule']}] {v.get('step_id')}/{v['location']}: {v['message']}" for v in violations
    )
    user_content = (
        f"[흐름도]\n{dump_json(flow)}\n\n"
        f"[검수 위반]\n{violation_lines}\n\n"
        f"[스펙 발췌]\n{_spec_excerpts(violations, catalog)}"
    )
    return [
        {"role": "system", "content": _PROMPT},
        {"role": "user", "content": user_content},
    ]


def verify_and_repair(flow: dict, catalog: CatalogLookup) -> dict:
    """흐름도를 검수하고 위반이 있으면 교정을 시도한다.

    반환: {"flow": dict, "violations": list[dict], "repaired": bool}.
    교정 LLM 출력이 스키마에 안 맞거나(ValueError) 위반이 줄지 않으면 원본을 유지한다.
    """
    violations = collect_violations(flow, catalog)
    repaired = False
    for _ in range(_MAX_REPAIRS):
        if not violations:
            break
        emit({"event": "stage", "stage": "verifying",
              "message": f"검수 위반 {len(violations)}건 교정 중"})
        try:
            fixed = chat_json(
                _repair_messages(flow, violations, catalog),
                purpose="verify", model_cls=Recommendation,
            ).model_dump()
        except ValueError as e:
            logger.warning("repair 출력 파싱 실패 — 원본 유지: %s", e)
            break
        new_violations = collect_violations(fixed, catalog)
        if len(new_violations) < len(violations):
            flow, violations, repaired = fixed, new_violations, True
        else:
            logger.info("repair가 위반을 줄이지 못함(%d→%d) — 원본 유지",
                        len(violations), len(new_violations))
            break
    return {"flow": flow, "violations": violations, "repaired": repaired}
