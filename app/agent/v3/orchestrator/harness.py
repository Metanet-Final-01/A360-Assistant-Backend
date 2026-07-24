"""verify harness v3 — 검수(R1~R12) + surgeon(EditOps) 기반 refine 루프 + confidence 합성.

v2와의 차이:
- 검사: run_flow_checks(L0 정적 + L1 데이터플로우·세션·골격) — R9~R12 포함, 세션
  레지스트리는 카탈로그에서 유도.
- 교정: '단계 서브트리 재출력'을 폐기하고 **surgeon LLM이 EditOps 패치만 출력**한다.
  서브트리 재출력도 축소판 전체 재출력이라 게으른 에코·라벨 유실을 앓는다(설계 관찰 2).
  패치는 라운드당 토큰이 1/10이라 예산을 3라운드로 늘려도 v2 재출력 1회보다 싸다 —
  패치화가 예산 확대의 전제조건. 생성 refine·수정 교정·기타 솔루션 경로가 모두 이
  엔진 하나를 지난다.
- 회귀 가드: 라운드 단위 — 교정 후 심각도 가중합(findings.weight)이 줄지 않으면 그
  라운드를 폐기한다. 2라운드 연속 무개선이면 종료(진동 방지).
- confidence: RAG 단일 산식 → 증거 합성(grounding × evidence × agreement × semantic).
  R3는 감점하지 않는다 — 질문 카드가 붙은 R3는 결함이 아니라 입력 대기다(관찰 3).

catalog는 CatalogLookup 프로토콜이면 무엇이든 된다: 호출부가 CatalogContext로 주입하며,
타 솔루션은 채팅에서 추출한 UserCatalog — 같은 checker가 양쪽을 검수한다.
"""

import copy
import logging
from pathlib import Path

from ..recommend.research import structural_complement
from ..recommend.stream import emit, emit_flow_frame
from ..verify.catalog import CatalogLookup
from ..verify.checker import derive_session_registry, run_flow_checks
from ..verify.findings import Finding, from_violations, weight
from .edit_ops import EditOps, annotate_ids, apply_edit_ops, render_outline, renumber, strip_ids
from .jsonio import chat_json

logger = logging.getLogger(__name__)

_SURGEON_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "surgeon.md").read_text(encoding="utf-8")

MAX_REFINE_ROUNDS = 3   # 패치 기반이라 v2(2)보다 예산을 늘려도 총비용이 싸다
_MAX_FINDINGS_IN_PROMPT = 15
_STOP_AFTER_NO_IMPROVE = 2  # 연속 무개선 종료 — 진동 방지

# 질문 카드로 승격되는 규칙 — 교정 대상도, confidence 감점 대상도 아니다.
CARD_RULES = frozenset({"R3"})
# confidence 감점 대상 오류 규칙 (warning 등급 R10/R12는 감점하지 않는다).
_PENALTY_RULES = frozenset({"R2", "R4", "R5", "R6", "R7", "R8", "R9", "R11", "R13", "R14"})
# 같은 위치에 위반이 겹칠 때의 우선순위 — R1(환각) > 오류 규칙 > 경고. 경고(R12 등)가
# 오류(R7 등)를 가려 감점이 누락되는 것을 막는다.
_RULE_RANK = {"R1": 3}


def _rule_rank(rule: str | None) -> int:
    if rule in _RULE_RANK:
        return _RULE_RANK[rule]
    return 2 if rule in _PENALTY_RULES else 1


def collect_violations(flow: dict, catalog: CatalogLookup) -> list[dict]:
    """흐름도 전체를 R1~R12로 검사한다 (세션 레지스트리는 카탈로그에서 유도)."""
    registry = derive_session_registry(catalog)
    return [v.as_dict() for v in run_flow_checks(flow, catalog, registry)]


# ─────────────────────────────────────────────────────────────────────────────
# confidence — 증거 합성 (v3 설계 §6)
# ─────────────────────────────────────────────────────────────────────────────

def attach_confidence(
    flow: dict,
    sink: list[dict],
    violations: list[dict],
    *,
    agreement: set[tuple[str, str]] | None = None,
    coverage_by_step: dict[str, str] | None = None,
) -> None:
    """액션별 신뢰도(FR-12)를 증거 합성으로 산정해 제자리에 채운다.

    grounding: R1 위반(카탈로그 부재=환각 의심)이면 즉시 0.2 (v2 유지).
    evidence : RAG best score, 소스 없으면 0.4 (v2 유지).
    agreement: 이 (package, action)이 다른 후보에도 등장했으면 ×1.1, 아니면 ×0.9 —
               다중 후보의 공짜 앙상블 신호. None이면(수정 경로 등) 미적용.
    semantic : 소속 단계의 L2 status — covered ×1.0 / partial ×0.85 / violated ×0.6.
               None이면 미적용.
    감점     : 오류 규칙(R2·R4~R9·R11·R13·R14)만 ×0.75. R3(카드)·warning(R10/R12)은 감점 없음.
    """
    best: dict[tuple, float] = {}
    for h in sink:
        pkg, act = h.get("package_name"), h.get("action_name")
        if pkg and act:
            best[(pkg, act)] = max(best.get((pkg, act), 0.0), h.get("score") or 0.0)

    viol: dict[tuple, str | None] = {}
    for v in violations:
        rule = v.get("rule")
        if rule in CARD_RULES:
            continue  # 입력 대기 ≠ 결함
        loc = (v.get("step_id"), v.get("location"))
        if loc[1] and (loc not in viol or _rule_rank(rule) > _rule_rank(viol[loc])):
            viol[loc] = rule  # 심각도 높은 규칙 우선 — 경고가 오류를 가리지 않게

    sem_factor = {"covered": 1.0, "partial": 0.85, "violated": 0.6}

    def _conf(pkg: str | None, act: str | None, rule: str | None, step_status: str | None) -> float:
        if rule == "R1":
            return 0.2
        base = best.get((pkg, act)) or 0.4
        if rule in _PENALTY_RULES:
            base *= 0.75
        if agreement is not None:
            base *= 1.1 if (pkg, act) in agreement else 0.9
        if step_status:
            base *= sem_factor.get(step_status, 1.0)
        return round(min(1.0, max(0.05, base)), 2)

    def _walk(actions: list[dict], sid, status: str | None, base: str) -> None:
        for i, a in enumerate(actions):
            path = f"{base}[{i}]" if base else f"actions[{i}]"
            a["confidence"] = _conf(a.get("package"), a.get("action"), viol.get((sid, path)), status)
            _walk(a.get("children") or [], sid, status, f"{path}.children")

    for step in flow.get("steps", []):
        sid = step.get("step_id")
        status = (coverage_by_step or {}).get(sid)
        _walk(step.get("actions") or [], sid, status, "")


def compute_flow_confidence(
    *,
    must_coverage: float | None,
    findings: list[Finding],
    sim_pass_rate: float | None,
    blocking_cards: int = 0,
) -> float:
    """흐름도 수준 신뢰도 — "이 봇이 업무를 하는가" (액션 수준과 다른 질문, 설계 §6).

    must 커버리지 × blocker 감쇠 × 시뮬레이션 통과율 × 카드 완만 감쇠. 카드는 해소 시
    재산정으로 자동 회복된다. 신호가 없는 축은 중립(1.0)으로 둔다.
    """
    base = must_coverage if must_coverage is not None else 1.0
    blockers = sum(1 for f in findings if f.severity == "blocker")
    base *= 0.8 ** blockers
    if sim_pass_rate is not None:
        base *= max(0.3, sim_pass_rate)  # 경로 일부 실패가 0으로 폭락시키지 않게 하한
    base *= max(0.7, 1.0 - 0.05 * blocking_cards)
    return round(min(1.0, max(0.05, base)), 2)


# ─────────────────────────────────────────────────────────────────────────────
# surgeon refine 루프 — findings → EditOps 패치 → 재검증 (v3 설계 §2-[6])
# ─────────────────────────────────────────────────────────────────────────────

def _spec_block(pkg: str, act: str, spec: dict) -> str:
    params = ", ".join(
        f"{p['name']}({p.get('type')}{', 필수' if p.get('required') else ''})"
        for p in spec.get("parameters", [])
    )
    return f"- {pkg}/{act}: 파라미터 [{params or '없음'}]"


def _spec_excerpts(violations: list[dict], catalog: CatalogLookup) -> tuple[str, set[tuple[str, str]]]:
    """위반 액션의 스펙 발췌 — surgeon이 올바른 표기·파라미터를 보고 고치게 한다."""
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
        blocks.append(_spec_block(pkg, act, spec))
    text = "\n".join(blocks) or "(해당 액션의 스펙 없음 — R1 위반 액션은 교체/제거 대상)"
    return text, seen


def _flow_packages(flow: dict) -> set[str]:
    pkgs: set[str] = set()

    def walk(actions: list[dict]) -> None:
        for a in actions or []:
            if a.get("package"):
                pkgs.add(a["package"])
            walk(a.get("children") or [])

    for step in flow.get("steps", []):
        walk(step.get("actions") or [])
    return pkgs


def repair_spec_excerpts(flow: dict, catalog: CatalogLookup, exclude: set[tuple[str, str]]) -> str:
    """'삽입' 수리에 필요한 액션 스펙 — 위반 목록에는 없는 어휘를 동봉한다 (처방 3).

    surgeon은 스펙에 없는 표기를 못 쓴다(환각 방지 규칙). 그런데 세션 누수(R8)·가짜
    반복(R14)의 수리는 흐름도에 **아직 없는** opener/closer·Loop 이터레이터·Try/Catch를
    삽입해야 한다 — 위반 액션 발췌만으로는 재료가 없어 정직한 무연산으로 끝난다(0374
    JIRA 봇 실측). research.structural_complement를 재사용해 카탈로그 직조회로 공급한다.
    """
    blocks: list[str] = []
    for pkg, act in structural_complement(catalog, _flow_packages(flow)):
        if (pkg, act) in exclude:
            continue
        spec = catalog.get_action_schema(pkg, act)
        if spec is None:
            continue
        blocks.append(_spec_block(pkg, act, spec))
    return "\n".join(blocks)


def _findings_lines(findings: list[Finding]) -> str:
    order = {"blocker": 0, "major": 1, "minor": 2, "warning": 3}
    ranked = sorted(findings, key=lambda f: order.get(f.severity, 1))[:_MAX_FINDINGS_IN_PROMPT]
    lines = []
    for f in ranked:
        tag = f.rule or f.req_id or f.layer
        loc = f"{f.step_id or ''}/{f.location or '-'}"
        hint = f" (힌트: {f.fix_hint})" if f.fix_hint else ""
        lines.append(f"- [{f.severity}·{tag}] {loc}: {f.message}{hint}")
    return "\n".join(lines)


def _error_findings(findings: list[Finding]) -> list[Finding]:
    """교정을 강제하는 findings — warning은 감점·앵커용일 뿐 수리 대상이 아니다."""
    return [f for f in findings if f.severity != "warning"]


def refine_flow(
    flow: dict,
    catalog: CatalogLookup,
    *,
    extra_findings: list[Finding] | None = None,
    max_rounds: int = MAX_REFINE_ROUNDS,
    purpose: str = "verify",
) -> dict:
    """findings(정적 위반 + 심판/L2/L3 지시)를 surgeon EditOps 패치로 반복 교정한다.

    라운드마다: findings → surgeon(EditOps만 출력) → 결정론 적용 → L0/L1 재검증 →
    심각도 가중합이 줄었을 때만 채택(회귀 가드). 수렴: 오류 findings 소진 / 라운드
    소진 / 연속 무개선 2회. extra_findings(심판 이식 지시 등)는 첫 라운드에만 싣는다 —
    적용 여부를 정적 재검증으로 판정할 수 없으므로 반복 강제하면 진동한다.

    반환: {"flow", "violations", "repaired"}.
    """
    violations = collect_violations(flow, catalog)
    findings, _cards = from_violations_dicts(violations)
    round_findings = findings + list(extra_findings or [])
    if not _error_findings(round_findings):
        return {"flow": flow, "violations": violations, "repaired": False}

    emit({"event": "stage", "stage": "verifying",
          "message": f"검수 위반 {len(violations)}건 · 개선 지시 {len(extra_findings or [])}건 교정 중",
          "data": {"violations": [
              {k: v.get(k) for k in ("rule", "location", "message", "step_id", "package", "action", "param")}
              for v in violations
          ]}})

    current = flow
    current_violations = violations
    # 회귀 가드 비교축은 '정적 위반 가중합'만 쓴다 — extra(이식 지시 등)는 정적 재검증으로
    # 소거를 판정할 수 없어, 합산하면 첫 라운드가 정적 결함을 새로 만들어도 통과해 버린다.
    current_weight = weight(_error_findings(findings))
    # extra만 있고 정적 위반이 0인 흐름도 개선(이식)은 '정적 악화 없음(<=)'이면 채택한다.
    extras_pending = bool(_error_findings(list(extra_findings or [])))
    repaired = False
    no_improve = 0

    for round_no in range(1, max_rounds + 1):
        work = annotate_ids(copy.deepcopy(current))
        outline = render_outline(work)
        excerpts, excerpt_keys = _spec_excerpts(current_violations, catalog)
        repair_menu = repair_spec_excerpts(current, catalog, excerpt_keys)
        user_content = (
            f"[흐름도 아웃라인]\n{outline}\n\n"
            f"[고칠 문제들 (심각도순)]\n{_findings_lines(round_findings)}\n\n"
            f"[스펙 발췌]\n{excerpts}"
            + (f"\n\n[수리용 액션 스펙 — 세션 여닫기·반복·분기·예외 처리를 삽입(insert/wrap)할 때 이 표기 사용]\n{repair_menu}"
               if repair_menu else "")
        )
        try:
            ops = chat_json(
                [{"role": "system", "content": _SURGEON_PROMPT},
                 {"role": "user", "content": user_content}],
                purpose=purpose, model_cls=EditOps,
            )
        except (ValueError, RuntimeError) as e:
            logger.warning("surgeon 라운드 %d 출력 실패 — 현재본 유지: %s", round_no, e)
            break
        if not ops.operations:  # 고칠 방법이 없다는 정직한 신호 — 가짜 성공 방지
            logger.info("surgeon 라운드 %d: 연산 없음 — 종료", round_no)
            break

        applied, errors = apply_edit_ops(work, ops.operations)
        if errors:
            logger.info("surgeon 라운드 %d: 연산 %d개 적용, 실패 %s", round_no, applied, errors)
        strip_ids(work)
        renumber(work)
        if applied == 0:
            no_improve += 1
            if no_improve >= _STOP_AFTER_NO_IMPROVE:
                break
            continue

        new_violations = collect_violations(work, catalog)
        new_findings, _ = from_violations_dicts(new_violations)
        new_weight = weight(_error_findings(new_findings))
        # 회귀 가드 — 정적 가중합이 줄었을 때만 채택. 이식 지시가 걸려 있는 라운드는
        # '정적 악화 없음(<=)'까지 허용한다 (이식은 정적 신호에 안 잡히는 개선이므로).
        if new_weight < current_weight or (extras_pending and new_weight <= current_weight):
            current, current_violations = work, new_violations
            current_weight = new_weight
            repaired = True
            no_improve = 0
            extras_pending = False  # 이식 지시는 1회 반영으로 소진 — 반복 강제하면 진동한다
            emit_flow_frame(current, current_violations, f"교정 라운드 {round_no} 적용")
            round_findings = new_findings  # 이후 라운드는 잔여 정적 위반만 (extra는 1회성)
            if not _error_findings(round_findings):
                break
        else:
            logger.info("surgeon 라운드 %d: 가중합 %d→%d 개선 없음 — 폐기",
                        round_no, current_weight, new_weight)
            no_improve += 1
            if no_improve >= _STOP_AFTER_NO_IMPROVE:
                break

    return {"flow": current, "violations": current_violations, "repaired": repaired}


def from_violations_dicts(violations: list[dict]) -> tuple[list[Finding], list[dict]]:
    """위반 dict 목록 → (Finding 목록, R3 카드 후보 dict 목록). findings.from_violations의 dict 어댑터."""

    class _V:  # Violation.as_dict 호환 셔틀
        def __init__(self, d: dict):
            self._d = d

        def as_dict(self) -> dict:
            return self._d

    fs, cards = from_violations([_V(v) for v in violations])
    return fs, [c.as_dict() for c in cards]


def verify_and_repair(flow: dict, catalog: CatalogLookup) -> dict:
    """흐름도를 검수하고 위반이 있으면 surgeon refine 루프로 교정한다 (v2 시그니처 유지).

    edit 경로·타 솔루션(generate_other) 경로가 이 관문을 그대로 쓴다.
    반환: {"flow": dict, "violations": list[dict], "repaired": bool}.
    """
    emit({"event": "stage", "stage": "verifying", "message": "흐름도 최종 검수 중"})
    return refine_flow(flow, catalog)
