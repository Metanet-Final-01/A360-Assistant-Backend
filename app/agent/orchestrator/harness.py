"""verify harness — 조립된 흐름도 전체에 대한 최종 검수 관문 (R1~R8 + 2단계 국소 repair).

recommend 파이프라인의 단계별 check가 위반을 '측정'만 했다면(RPA-27a), 여기서는
위반을 LLM으로 '교정'까지 시도한다. 교정은 2단계다(RPA-91):
1. 단계별 문법 위반(R1~R6)은 그 단계의 actions[] 서브트리만 LLM에 넘겨 국소 교정한다
   — 흐름도 전체를 재작성하지 않으므로 위반 없는 단계는 그대로 보존되고, 단계마다
   독립 교정이라 여러 단계에 흩어진 위반도 각각 시도된다.
2. 남은 세션 생명주기 위반(R7~R8)만 흐름도 전체 관점에서 한 번 교정한다(단계 경계를
   넘어야 고칠 수 있으므로).
어느 단계든 교정본은 재검수해 위반이 줄었을 때만 채택한다 — repair가 흐름도를 더
망치면 원본을 유지한다.

catalog는 CatalogLookup 프로토콜이면 무엇이든 된다: a360은 get_catalog(),
타 솔루션은 채팅에서 추출한 UserCatalog — 같은 checker가 양쪽을 검수한다.
"""

import logging
from pathlib import Path

from app.schemas import Recommendation

from ..recommend.stream import emit, emit_flow_frame
from ..verify.catalog import CatalogLookup
from ..verify.checker import run_checks, run_session_checks
from .jsonio import chat_json
from .render import dump_json

logger = logging.getLogger(__name__)

_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "repair.md").read_text(encoding="utf-8")

# 세션 생명주기(R7~R8)는 단계 경계를 넘어(예: step-1에서 열고 step-3에서 닫음) 교정해야
# 하므로 단계별 국소 교정 대상이 아니다 — 잔여 세션 위반만 흐름도 전체로 한 번 교정한다.
_SESSION_RULES = frozenset({"R7", "R8"})


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


def attach_confidence(flow: dict, sink: list[dict], violations: list[dict]) -> None:
    """액션별 신뢰도(FR-12)를 RAG 소스 점수 + 검수 위반으로 산정해 제자리에 채운다 (RPA-116).

    compose는 confidence를 내지 않고 '검수가 채운다'는 계약의 이행 — best 소스 유사도를
    기준값으로, 위반이면 감점한다(특히 R1=카탈로그 부재는 환각 가능성이라 아주 낮게).
    sink는 검색 히트(package_name/action_name/score), violations는 step_id+location을 싣는다.
    생성(finalize)·수정(edit) 양쪽이 이 함수로 신뢰도를 채운다.
    """
    best: dict[tuple, float] = {}
    for h in sink:
        pkg, act = h.get("package_name"), h.get("action_name")
        if pkg and act:
            best[(pkg, act)] = max(best.get((pkg, act), 0.0), h.get("score") or 0.0)
    viol: dict[tuple, str | None] = {}
    for v in violations:
        loc = (v.get("step_id"), v.get("location"))
        if loc[1] and (loc not in viol or v.get("rule") == "R1"):  # R1(액션 부재)을 우선
            viol[loc] = v.get("rule")

    def _conf(score: float | None, rule: str | None) -> float:
        if rule == "R1":  # 카탈로그에 없는 액션 — 환각 가능
            return 0.2
        base = score if score else 0.4  # 소스 못 찾음 → 중하
        if rule:  # R2~R6 등 파라미터·구조 위반
            base *= 0.75
        return round(min(1.0, max(0.05, base)), 2)

    def _walk(actions: list[dict], sid, base: str) -> None:
        for i, a in enumerate(actions):
            path = f"{base}[{i}]" if base else f"actions[{i}]"  # 위반 location 포맷과 일치
            a["confidence"] = _conf(best.get((a.get("package"), a.get("action"))), viol.get((sid, path)))
            _walk(a.get("children") or [], sid, f"{path}.children")

    for step in flow.get("steps", []):
        _walk(step.get("actions") or [], step.get("step_id"), "")


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
    """검수 위반 목록·스펙 발췌를 담은 repair LLM용 (system, user) 메시지를 만든다."""
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


def _repair_one_step(step: dict, catalog: CatalogLookup) -> tuple[list[dict], bool]:
    """한 단계의 문법 위반(R1~R6)만 국소 교정한다.

    그 단계의 actions[]만 담은 단일-단계 흐름도를 LLM에 넘기고(흐름도 전체 아님),
    교정 후 그 단계의 위반이 줄었을 때만 채택한다. 반환: (actions, repaired).
    세션 검사(R7~R8)는 단계 경계를 넘으므로 여기서 다루지 않는다.
    """
    actions = step.get("actions", [])
    violations = run_checks(actions, catalog)
    if not violations:
        return actions, False

    step_id = step.get("step_id")
    # label/description도 실어 교정 LLM이 단계 문맥을 보고, 출력에 그대로 되싣게 한다
    # (검증 안정성 + 교정 정확도). 채택 시엔 원본 step에 new_actions만 병합하므로 원본
    # label이 보존된다.
    mini_flow = {"steps": [{
        "step_id": step_id, "label": step.get("label"),
        "description": step.get("description"), "actions": actions,
    }]}
    violation_dicts = []
    for v in violations:
        d = v.as_dict()
        d["step_id"] = step_id  # run_checks는 step_id를 싣지 않으므로 프롬프트용으로 부착
        violation_dicts.append(d)

    try:
        fixed = chat_json(
            _repair_messages(mini_flow, violation_dicts, catalog),
            purpose="verify", model_cls=Recommendation,
        ).model_dump()
    except ValueError as e:
        logger.warning("step %s repair 출력 파싱 실패 — 원본 유지: %s", step_id, e)
        return actions, False

    fixed_steps = fixed.get("steps") or []
    if len(fixed_steps) != 1:  # 한 단계를 줬는데 다른 개수를 돌려주면 신뢰 불가 — 원본 유지
        logger.info("step %s repair가 단계 %d개 반환 — 원본 유지", step_id, len(fixed_steps))
        return actions, False

    new_actions = fixed_steps[0].get("actions", [])
    if len(run_checks(new_actions, catalog)) < len(violations):
        return new_actions, True
    logger.info("step %s repair가 위반을 줄이지 못함 — 원본 유지", step_id)
    return actions, False


def _repair_sessions(
    flow: dict, violations: list[dict], catalog: CatalogLookup
) -> tuple[dict, list[dict], bool]:
    """세션 생명주기 위반(R7~R8)을 흐름도 전체 관점에서 1회 교정한다.

    세션은 단계 경계를 넘어 열고 닫으므로 국소 교정이 아니라 흐름도 전체를 넘긴다.
    교정 후 전체 위반이 줄었을 때만 채택한다. 반환: (flow, violations, repaired).
    """
    try:
        fixed = chat_json(
            _repair_messages(flow, violations, catalog),
            purpose="verify", model_cls=Recommendation,
        ).model_dump()
    except ValueError as e:
        logger.warning("세션 repair 출력 파싱 실패 — 원본 유지: %s", e)
        return flow, violations, False

    new_violations = collect_violations(fixed, catalog)
    if len(new_violations) < len(violations):
        return fixed, new_violations, True
    logger.info("세션 repair가 위반을 줄이지 못함(%d→%d) — 원본 유지",
                len(violations), len(new_violations))
    return flow, violations, False


def verify_and_repair(flow: dict, catalog: CatalogLookup) -> dict:
    """흐름도를 검수하고 위반이 있으면 2단계로 교정을 시도한다.

    1단계: 단계별 문법 위반(R1~R6)을 그 단계 서브트리만 국소 교정한다(단계마다 독립 —
    흩어진 위반도 각각 시도되고, 위반 없는 단계는 그대로 보존된다).
    2단계: 남은 세션 위반(R7~R8)만 흐름도 전체 관점에서 1회 교정한다.
    각 교정은 자기 기준(단계별 R1~R6 / 세션별 전체)으로 위반이 줄 때만 채택하고, 마지막에
    전역 회귀 가드로 전체 위반이 원본보다 늘었으면 모든 교정을 폐기한다 — repair가 흐름도를
    악화시키지 않는다.

    반환: {"flow": dict, "violations": list[dict], "repaired": bool}.
    """
    emit({"event": "stage", "stage": "verifying", "message": "흐름도 최종 검수 중"})
    violations = collect_violations(flow, catalog)
    if not violations:
        return {"flow": flow, "violations": violations, "repaired": False}

    # 국소 교정(1단계)은 단계별 R1~R6만 보고 채택하므로 액션 수정이 단계 경계를 넘는
    # R7~R8을 새로 만들 수 있다. 전역 회귀 가드용으로 원본을 붙잡아 둔다.
    original_flow, original_violations = flow, violations

    emit({"event": "stage", "stage": "verifying",
          "message": f"검수 위반 {len(violations)}건 교정 중",
          # 관측 전용(RPA-105/129): "위반 N건"의 실체. 표시 문구는 위 message 그대로 불변 —
          # data엔 Violation.as_dict()가 이미 만드는 7필드(message=사람이 읽는 사유 포함)를
          # 다 남긴다(예전엔 rule/location 2개만 남겨 "무엇이 왜 틀렸나"를 재구성 못 했음).
          "data": {"violations": [
              {k: v.get(k) for k in ("rule", "location", "message", "step_id", "package", "action", "param")}
              for v in violations
          ]}})

    # 1단계: 단계별 문법 위반(R1~R6) 국소 교정 — 위반 있는 단계만 LLM에 넘긴다.
    # 단계마다 누적 흐름도(고친 단계 + 남은 원본)를 프레임으로 흘려 점진 스트리밍한다 —
    # 전체 재생성이 아니라 '단계별로 고쳐지는' 과정을 프론트가 실시간 렌더한다.
    repaired = False
    new_steps = []
    all_steps = flow.get("steps", [])
    total = len(all_steps)
    for i, step in enumerate(all_steps):
        # 위반 있는 단계만 LLM 국소 교정을 부른다(느림) — 그 직전에 '이 단계 수정 중' 프레임을
        # active_step_id와 함께 방출해, 프론트가 해당 단계 박스를 붉게 강조·깜빡이고 그 위치로
        # 스크롤하게 한다(어떤 액션을 고치는 중인지 사용자에게 보이게). 위반 없는 단계는 교정을
        # 건너뛰므로(빠름) 강조 프레임도 내지 않아 깜빡임이 실제 수정 단계에만 머문다.
        if run_checks(step.get("actions", []), catalog):
            pending = {**flow, "steps": new_steps + all_steps[i:]}
            emit_flow_frame(pending, collect_violations(pending, catalog),
                            f"{i + 1}/{total} 단계 수정 중", active_step_id=step.get("step_id"))
        new_actions, did = _repair_one_step(step, catalog)
        if did:
            step = {**step, "actions": new_actions}  # step_id·order 등은 원본 보존
            repaired = True
        new_steps.append(step)
        # 교정 결과 프레임(active_step_id 없음 → 강조 해제): 방금 단계까지 반영된 누적 흐름도.
        partial = {**flow, "steps": new_steps + all_steps[i + 1:]}
        emit_flow_frame(partial, collect_violations(partial, catalog), f"{i + 1}/{total} 단계 국소 수정")
    if repaired:
        flow = {**flow, "steps": new_steps}
        violations = collect_violations(flow, catalog)

    # 2단계: 잔여 세션 위반(R7~R8)은 단계 경계를 넘으므로 흐름도 전체로 교정.
    if any(v["rule"] in _SESSION_RULES for v in violations):
        flow, violations, sess_repaired = _repair_sessions(flow, violations, catalog)
        repaired = repaired or sess_repaired
        if sess_repaired:
            emit_flow_frame(flow, violations, "세션 정합성 교정")

    # 전역 회귀 가드: 국소 교정이 R7~R8을 새로 만들어 전체 위반이 원본보다 늘었으면
    # (단계별·세션별 채택 기준으로는 못 잡는 경로) 모든 교정을 폐기하고 원본을 유지한다.
    if len(violations) > len(original_violations):
        logger.info("교정 후 전체 위반 증가(%d→%d) — 모든 교정 폐기, 원본 유지",
                    len(original_violations), len(violations))
        return {"flow": original_flow, "violations": original_violations, "repaired": False}

    return {"flow": flow, "violations": violations, "repaired": repaired}
