"""verify harness v4 — 검수(R1~R12) + surgeon(EditOps) 기반 refine 루프 + confidence 합성.

v2와의 차이:
- 검사: run_flow_checks(L0 정적 + L1 데이터플로우·세션·골격) — R9~R12 포함, 세션
  레지스트리는 카탈로그에서 유도.
- 교정: '단계 서브트리 재출력'을 폐기하고 **surgeon LLM이 EditOps 패치만 출력**한다.
  서브트리 재출력도 축소판 전체 재출력이라 게으른 에코·라벨 유실을 앓는다(설계 관찰 2).
  패치는 라운드당 토큰이 1/10이라 예산을 3라운드로 늘려도 v2 재출력 1회보다 싸다 —
  패치화가 예산 확대의 전제조건. 생성 refine·수정 교정·기타 솔루션 경로가 모두 이
  엔진 하나를 지난다.
- 회귀 가드: 라운드 단위 — 교정 후 심각도 가중합(findings.weight)이 줄지 않으면 그
  라운드를 폐기한다. 2라운드 연속 무개선이면 종료(진동 방지). spec을 주면 가중합에
  **완성도 항**(coverage_det의 결정론 누락 + 뭉갬)이 함께 들어가 삭제 편향과 뭉갬이
  동시에 막힌다(설계 §5-D, Phase 3).
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
from ..verify.coverage_det import (
    completeness_findings,
    conflated_slots,
    hollow_requirements,
    missing_requirements,
    slot_req_ids,
)
from ..verify.findings import Finding, from_violations, weight
from .edit_ops import (
    NODE_ID,
    EditOps,
    annotate_ids,
    apply_edit_ops,
    drop_unknown_action_ops,
    render_outline,
    renumber,
    strip_ids,
)
from .jsonio import chat_json

logger = logging.getLogger(__name__)

_SURGEON_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "surgeon.md").read_text(encoding="utf-8")

# 교정 예산 (설계 §5-F "대폭 확대"). 3 → 8.
# 왜 늘리나: 누락 blocker가 목적 함수에 들어오면서 라운드가 할 일이 '위반 제거'에서
# '위반 제거 + 누락 채움'으로 늘었다. 누락 하나를 메우는 삽입(insert/wrap)은 그 자체가
# 새 정적 위반(파라미터 미충족 등)을 만들어 다음 라운드가 또 필요하다 — 3라운드면
# blocker 하나를 메우다 예산이 끝난다.
# 왜 무한이 아닌가: _STOP_AFTER_NO_IMPROVE(2)가 실질 상한을 쥔다. 개선이 멈추면 2라운드
# 만에 빠져나오므로 8은 '개선이 계속 나오는 동안만' 소모되는 예산이다. 그럼에도 상한을
# 두는 이유는 surgeon이 매 라운드 미세 개선을 내며 무한 지연시키는 병리를 막기 위함.
# 비용: 패치는 라운드당 토큰이 전체 재출력의 1/10이라 8라운드 ≈ v2 재출력 1회 미만.
MAX_REFINE_ROUNDS = 8
_MAX_FINDINGS_IN_PROMPT = 15
_STOP_AFTER_NO_IMPROVE = 2  # 연속 무개선 종료 — 진동 방지

# 자리표시자 단계 id 접두사 (설계 §5-G·H). 재검수(edit 경로 등)에서 stale 자리표시자를
# 식별해 걷어내기 위한 앵커다 — 접두사로 판별하므로 스키마에 새 필드가 필요 없다.
PLACEHOLDER_STEP_PREFIX = "step-unresolved-"

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


# 흐름도가 이미 쓰는 패키지에서 surgeon에게 열어 줄 업무 액션 상한. 실측 케이스는
# 6패키지 91개(Excel advanced 59 · Email 16 · Browser 8 …)로 ≈5k 토큰이라 그대로 담긴다.
# 상한은 패키지를 널리 건드린 흐름도(10패키지 × 50액션)에서 프롬프트가 터지는 것만 막는다.
_MAX_REPAIR_BUSINESS_ACTIONS = 120


def _business_repair_actions(
    flow: dict, catalog: CatalogLookup, violations: list[dict] | None
) -> list[tuple[str, str]]:
    """흐름도가 **이미 쓰는 패키지**의 업무 액션 — 잘못 고른 액션을 갈아끼울 재료.

    범위를 '이미 쓰는 패키지'로 묶는 이유: 흐름도는 이미 그 제품 계열에 커밋했고, 같은
    패키지 안에서의 재선택은 세션 핸들·구조 가정을 깨지 않는다. 카탈로그 전량(1,200개)을
    열면 프롬프트가 터지는 것은 물론이고 surgeon이 제품을 갈아타 R17(세션 핸들 패키지
    불일치)을 스스로 만든다.

    상한에 걸리면 **위반이 걸린 패키지를 먼저** 남긴다 — 어휘가 가장 아쉬운 곳이 거기다.
    잘리는 것은 조용히 넘기지 않고 로그로 남긴다(`_whole_catalog_dossier`와 같은 규약).
    """
    pkgs = _flow_packages(flow)
    if not pkgs:
        return []
    hot = {v.get("package") for v in violations or [] if v.get("package")}
    rows = [
        (s.get("package"), s.get("action"))
        for s in catalog.iter_action_schemas()
        if s.get("package") in pkgs and s.get("action")
    ]
    # 위반 패키지 우선, 그 안에서는 카탈로그 순서(결정론).
    rows.sort(key=lambda pa: 0 if pa[0] in hot else 1)
    if len(rows) > _MAX_REPAIR_BUSINESS_ACTIONS:
        logger.info("수리 어휘가 상한을 넘어 %d개 중 %d개만 싣는다(위반 패키지 우선)",
                    len(rows), _MAX_REPAIR_BUSINESS_ACTIONS)
    return rows[:_MAX_REPAIR_BUSINESS_ACTIONS]


def repair_spec_excerpts(
    flow: dict,
    catalog: CatalogLookup,
    exclude: set[tuple[str, str]],
    violations: list[dict] | None = None,
) -> str:
    """수리에 필요한 액션 스펙 — 위반 목록에는 없는 어휘를 동봉한다 (처방 3).

    surgeon은 스펙에 없는 표기를 못 쓴다(환각 방지 규칙). 그래서 **여기 없는 어휘로만
    풀리는 위반은 구조적으로 수리 불가**다. 두 종류를 싣는다.

    **1) 구조 보완** — 세션 누수(R8)·가짜 반복(R14)의 수리는 흐름도에 아직 없는
    opener/closer·Loop 이터레이터·Try/Catch를 삽입해야 한다. 위반 액션 발췌만으로는
    재료가 없어 정직한 무연산으로 끝난다(0374 JIRA 봇 실측).

    **2) 흐름도가 이미 쓰는 패키지의 업무 액션** (RPA-298 추가). 1)만 있던 동안 수리
    어휘는 45개 **전부 세션·제어 흐름**이었고 업무 액션이 0개였다 — 실측 산출물에서
    `Excel advanced/Format cell`(환각) blocker가 매 라운드 목록 맨 위에 뜨는데도 갈아끼울
    표기가 없어 무연산으로 끝났고, 무개선 2라운드에 루프가 종료됐다. 100점짜리 blocker가
    목적 함수를 점유한 채 **고칠 수단이 구조적으로 없는** 상태였다.

    ⚠️ 이걸 연다고 모든 R1이 풀리지는 않는다. 그 실측 건은 `Excel advanced`에 서식 액션
    자체가 없어(59개 전수 확인) 어떤 어휘를 줘도 못 고친다 — 그건 결함이 아니라 제약이라
    질문 카드로 올려야 할 종류다(별건).
    """
    blocks: list[str] = []
    seen: set[tuple[str, str]] = set(exclude)
    for pkg, act in list(structural_complement(catalog, _flow_packages(flow))) + \
            _business_repair_actions(flow, catalog, violations):
        if (pkg, act) in seen:
            continue
        spec = catalog.get_action_schema(pkg, act)
        if spec is None:
            continue
        seen.add((pkg, act))
        blocks.append(_spec_block(pkg, act, spec))
    return "\n".join(blocks)


# 라운드 기록에 실을 연산 수 상한 — turn_events.detail은 4,000자를 넘으면 통째로 preview
# 마커로 대체돼(sessions._tev) 구조가 사라진다. 연산은 라운드당 보통 1~5개다.
_MAX_LOGGED_OPS = 12


def _op_digest(ops) -> list[dict]:
    """surgeon 연산을 관측용으로 축약한다 — '무엇을 어디에 하려 했나'만 남긴다.

    파라미터 값·라벨 같은 자유 텍스트는 싣지 않는다: 사용자 업무 내용이 섞일 수 있고
    (turn_events는 관측 DB로 나간다) 부피도 크다. 진단에 필요한 것은 **연산 종류와 대상**이다.
    """
    out: list[dict] = []
    for op in (ops or [])[:_MAX_LOGGED_OPS]:
        row = {"op": op.op}
        if op.target:
            row["target"] = op.target
        if op.targets:
            row["targets"] = op.targets[:4]
        if op.anchor:
            row["anchor"] = op.anchor
        if op.package or op.action_name:
            row["to"] = f"{op.package or '?'}/{op.action_name or '?'}"
        if op.action:
            row["insert"] = f"{op.action.get('package') or '?'}/{op.action.get('action') or '?'}"
        if op.container:
            row["wrap_in"] = f"{op.container.get('package') or '?'}/{op.container.get('action') or '?'}"
        if op.parameters:
            row["params"] = [p.get("name") for p in op.parameters if isinstance(p, dict)][:6]
        out.append(row)
    return out


def _rule_counts(findings: list[Finding]) -> dict[str, int]:
    """남은 결함을 규칙별 개수로 — '무엇이 안 고쳐졌나'를 한 줄로 본다."""
    counts: dict[str, int] = {}
    for f in _error_findings(findings):
        key = f.rule or f.layer or "?"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _emit_round(round_no: int, outcome: str, **data) -> None:
    """교정 라운드 하나의 결과를 관측 이벤트로 남긴다 (LLM 0회, 결정론).

    ## 왜 필요한가 (실측, 2026-07-27)

    산출물에 R17(세션 핸들 패키지 불일치) blocker가 남았는데, 그 수리에 필요한 `Email`
    어휘는 수리 메뉴에 이미 있었다. 그런데 **surgeon이 시도조차 안 했는지, 시도했는데
    적용이 실패했는지, 적용됐는데 가중합이 안 줄어 폐기됐는지 알 방법이 없었다** —
    로그는 컨테이너 재시작으로 날아가고, turn_events에는 라운드 정보가 아예 없었다.

    원인을 못 짚으면 고칠 수도 없다. 라운드마다 '무엇을 시도했고 왜 그렇게 끝났나'를
    남긴다. `emit`의 data는 sessions._tev가 turn_events.detail(JSON)로 적재한다.
    """
    emit({
        "event": "stage",
        "stage": "refining",
        "message": f"교정 라운드 {round_no} — {outcome}",
        "data": {"round": round_no, "outcome": outcome, **data},
    })


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


# 슬롯 목적 블록의 상한 — 프롬프트 예산 보호. findings 상한(15)과 같은 자릿수로 둔다.
_MAX_SLOTS_IN_PROMPT = 25


def slot_purpose_block(flow: dict, spec: dict | None) -> str:
    """id가 붙은 흐름도의 각 액션이 **어떤 요구를 담당하는 자리인지**를 렌더한다 (설계 §5.2-C).

    왜 필요한가(§5.1-②): surgeon은 흐름도 노드만 보고 그 자리가 무엇을 하려던 자리인지
    모른다. 그래서 "이 액션이 카탈로그에 없다"(R1)를 받으면 재선택할 근거가 없어 가장 싸고
    확실한 remove를 고른다 — 위반은 사라지지만 업무도 함께 사라진다. 슬롯의 목적을 함께
    주면 "지운다"가 아니라 "그 요구를 실제로 수행하는 액션으로 바꾼다"가 자연스러운
    선택지가 된다.

    액션에 req_id가 없거나 spec이 없으면 빈 문자열 — 프롬프트 불변(기존 동작).
    이는 coverage_det의 침묵 원칙과 같다: 앵커 미기재는 '목적이 없다'가 아니라
    '연결 정보가 없다'이므로, 없는 정보를 지어내 보여주지 않는다.

    한 자리가 요구를 여럿 주장하면(뭉갬) **같은 노드 id로 줄이 여러 개** 나온다. 이건
    렌더 사고가 아니라 신호다: 뭉갬 finding이 지목하는 자리를 surgeon이 아웃라인에서
    찾을 수 있어야 하고, 동시에 그 자리가 '담당 요구 있는 자리'로 인식돼 삭제 보호를
    받아야 한다. 앵커 해석은 coverage_det.slot_req_ids와 **같은 함수**를 쓴다 —
    여기서만 스칼라로 읽으면 뭉갬 슬롯이 목적 없는 자리로 보여 remove 후보가 된다.
    """
    reqs = {
        r.get("req_id"): r
        for r in (spec or {}).get("requirements") or []
        if isinstance(r, dict) and r.get("req_id")
    }
    if not reqs:
        return ""

    known = set(reqs)
    lines: list[str] = []

    def walk(actions) -> None:
        for a in actions or []:
            if not isinstance(a, dict):
                continue
            claimed = [reqs[r] for r in slot_req_ids(a, known) if r in reqs]
            for req in claimed:
                if len(lines) >= _MAX_SLOTS_IN_PROMPT:
                    break
                mark = " ⚠뭉갬" if len(claimed) > 1 else ""
                lines.append(
                    f"- [{a.get(NODE_ID)}] «{a.get('label') or a.get('action')}» "
                    f"→ {req.get('req_id')}({req.get('priority', 'must')}): {req.get('text', '')}{mark}"
                )
            walk(a.get("children"))

    for step in flow.get("steps") or []:
        if isinstance(step, dict):
            walk(step.get("actions"))
    if not lines:
        return ""
    return (
        "\n\n[슬롯 목적 — 각 자리가 담당하는 요구]\n"
        + "\n".join(lines)
        + "\n담당 요구가 있는 자리를 remove로 비우면 그 업무가 흐름도에서 사라진다. "
        "위반이 있으면 먼저 **그 요구를 수행하는 다른 액션으로 교체(update)** 하거나 "
        "파라미터를 고쳐라 — 삭제는 그 요구가 더는 필요 없다는 근거가 있을 때만이다."
        "\n같은 노드 id가 ⚠뭉갬으로 여러 줄에 걸쳐 있으면 그 한 자리가 요구 여럿을 떠맡고 "
        "있다는 뜻이다 — 요구마다 액션을 나누고 각각 req_id를 부여하라."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 자리표시자 — 예산을 다 써도 남는 누락의 최종 폴백 (설계 §5-G·H)
# ─────────────────────────────────────────────────────────────────────────────

def _strip_placeholder_steps(flow: dict) -> dict:
    """이전 실행이 남긴 자리표시자 단계를 걷어낸다 (제자리 변형 금지 — 얕은 사본).

    edit 경로가 같은 흐름도로 verify_and_repair를 반복 호출하므로, 걷어내지 않으면
    해소된 요구의 자리표시자가 눌어붙고 매 턴 중복 누적된다. spec이 있을 때만 부르는
    이유는 재유도 가능성 때문이다 — spec 없이 지우면 정보만 잃는다.
    """
    steps = flow.get("steps") or []
    kept = [s for s in steps
            if not (isinstance(s, dict) and str(s.get("step_id") or "").startswith(PLACEHOLDER_STEP_PREFIX))]
    if len(kept) == len(steps):
        return flow
    return {**flow, "steps": kept}


def _placeholder_steps(flow: dict, spec: dict) -> dict:
    """미해결 must 요구마다 자리표시자 단계를 덧붙인다 — "아무것도 안 내보내기"의 대안.

    설계 §5-H: 예산을 늘려도 누락은 0이 되지 않는다. 그때 흐름도에서 요구를 통째로
    증발시키면 사용자는 무엇이 빠졌는지 알 길이 없다(=조용한 삭제, 결정 A가 막으려던 바로 그
    실패). 대신 "여기에 ○○가 필요한데 맞는 액션을 못 찾았어요"를 흐름도 안에 남긴다.

    구현 선택 — **액션 없는 단계(Step 스캐폴드)**를 쓴다. 근거:
    - `Recommendation`에 새 최상위 필드를 추가할 수 없다. output_assurance._unknown_field_findings가
      스키마 model_fields 밖의 키를 미지 필드로 보고 fail_decision="deny"로 기록한다.
    - 자리표시자를 '액션'으로 만들면 package/action에 실재하지 않는 표기를 넣게 되어
      R1(환각) blocker가 발화한다 — 누락을 알리려다 환각 위반을 자작하는 꼴.
      actions=[]인 단계는 어떤 정적 규칙도 건드리지 않으면서 렌더에는 남는다.
    - step_id 접두사만으로 식별되므로 스키마·전송 포맷이 그대로다.

    should 요구는 자리표시자를 만들지 않는다 — minor 등급이라 '흐름도에 자리를 파둘 만큼'의
    미해결이 아니고, 낮은 우선순위까지 넣으면 스캐폴드가 실제 단계를 압도한다.
    """
    by_id = {r.get("req_id"): r for r in (spec or {}).get("requirements") or [] if isinstance(r, dict)}
    pending = [
        rid for rid in missing_requirements(flow, spec)
        if (by_id.get(rid) or {}).get("priority", "must") != "should"
    ]
    if not pending:
        return flow
    extra = []
    for rid in pending:
        text = str((by_id.get(rid) or {}).get("text") or "").strip() or rid
        extra.append({
            "step_id": f"{PLACEHOLDER_STEP_PREFIX}{rid}",
            "label": f"[미해결] {text[:60]}",
            "description": (
                f"요구 {rid}('{text}')를 담당할 액션을 찾지 못했습니다. "
                "카탈로그에서 맞는 액션을 직접 지정하거나, 이 요구가 필요 없다면 알려 주세요."
            ),
            "actions": [],
        })
    logger.info("자리표시자 %d건 부착 — 미해결 must 요구: %s", len(extra), pending)
    return {**flow, "steps": list(flow.get("steps") or []) + extra}


def refine_flow(
    flow: dict,
    catalog: CatalogLookup,
    *,
    extra_findings: list[Finding] | None = None,
    max_rounds: int = MAX_REFINE_ROUNDS,
    purpose: str = "verify",
    spec: dict | None = None,
) -> dict:
    """findings(정적 위반 + 누락 + 심판/L2/L3 지시)를 surgeon EditOps 패치로 반복 교정한다.

    라운드마다: findings → surgeon(EditOps만 출력) → 결정론 적용 → L0/L1 + 결정론 누락
    재검증 → 심각도 가중합이 줄었을 때만 채택(회귀 가드). 수렴: 오류 findings 소진 /
    라운드 소진 / 연속 무개선 2회. extra_findings(심판 이식 지시 등)는 첫 라운드에만
    싣는다 — 적용 여부를 정적 재검증으로 판정할 수 없으므로 반복 강제하면 진동한다.

    **spec을 주면 회귀 가드 비교축에 완성도 항(결정론 누락 + 뭉갬)이 들어간다** (설계 §5-D).
    기존 비교축은 정적 위반 가중합뿐이라, remove가 허용 연산인 상태에서 "위반 있는 액션을
    지우면 가중합이 반드시 준다" → 삭제가 항상 유효한 개선 경로였다. 누락을 같은 축에
    넣으면 req_id를 든 액션의 삭제가 blocker(100)를 만들어 major(10) 제거를 압도하고,
    회귀 가드가 그 패치를 스스로 거부한다. 목적 함수를 재설계하지 않고 규칙 하나로
    삭제 편향이 해소되는 것이 §5-D의 요지다.
    같은 축의 뭉갬 항(요구당 major)은 반대 방향 도피로를 막는다: 뭉갬을 "req_id 하나를
    떼서" 없애면 그 요구가 즉시 누락 blocker(100)가 되어 뭉갬 제거(−10·−20)를 압도한다.
    반대로 **정직한 재분해는 통과한다** — 2중 뭉갬(20)을 쪼개다 부수 위반 1건(10)이
    생겨도 가중합은 준다(뭉갬을 요구당 1건으로 세는 이유, coverage_det 참조).
    spec을 안 주면 완성도 항이 0이라 기존 동작 그대로다(하위호환).

    반환: {"flow", "violations", "repaired"}.
    """
    if spec is not None:
        flow = _strip_placeholder_steps(flow)
    violations = collect_violations(flow, catalog)
    findings, _cards = from_violations_dicts(violations)
    gaps = completeness_findings(flow, spec) if spec is not None else []
    round_findings = findings + gaps + list(extra_findings or [])
    if not _error_findings(round_findings):
        return {"flow": flow, "violations": violations, "repaired": False}

    # 진행 메시지는 '무엇을 고치는 중인가'를 사람 말로 나눠 보여준다 — 누락과 뭉갬은
    # 사용자가 체감하는 불만이 서로 달라서(빠뜨림 vs 뭉갬) 한 숫자로 합치면 안 읽힌다.
    # 빈껍데기도 따로 센다: 누락에 합치면 "누락 0건"이 다시 거짓말이 되는 게 아니라
    # 이번엔 **좌표 없는 숫자**가 되어, 사용자가 어느 자리가 비었는지 알 수 없다.
    n_missing = len(missing_requirements(flow, spec)) if spec is not None else 0
    n_conflated = len(conflated_slots(flow, spec)) if spec is not None else 0
    n_hollow = len(hollow_requirements(flow, spec)) if spec is not None else 0
    emit({"event": "stage", "stage": "verifying",
          "message": (f"검수 위반 {len(violations)}건 · 요구 누락 {n_missing}건 · "
                      f"요구 뭉갬 {n_conflated}건 · 빈껍데기 {n_hollow}건 · "
                      f"개선 지시 {len(extra_findings or [])}건 교정 중"),
          "data": {"violations": [
              {k: v.get(k) for k in ("rule", "location", "message", "step_id", "package", "action", "param")}
              for v in violations
          ]}})

    current = flow
    current_violations = violations
    # 회귀 가드 비교축 = '정적 위반 + 결정론 누락 + 뭉갬' 가중합. extra(이식 지시 등)는 여기서
    # 제외한다 — 정적 재검증으로 소거를 판정할 수 없어, 합산하면 첫 라운드가 정적 결함을
    # 새로 만들어도 통과해 버린다. 반면 완성도 항은 매 라운드 결정론으로 다시 재는 축이라
    # 같은 문제가 없고, 이 항이 있어야 '삭제로 위반을 줄이는' 경로가 막힌다(§5-D).
    current_weight = weight(_error_findings(findings + gaps))
    # extra만 있고 정적 위반이 0인 흐름도 개선(이식)은 '정적 악화 없음(<=)'이면 채택한다.
    extras_pending = bool(_error_findings(list(extra_findings or [])))
    repaired = False
    no_improve = 0
    round_no = 0  # max_rounds=0(교정 끄기)이면 루프가 안 돌아 아래 요약이 참조할 값이 없다

    for round_no in range(1, max_rounds + 1):
        work = annotate_ids(copy.deepcopy(current))
        outline = render_outline(work)
        excerpts, excerpt_keys = _spec_excerpts(current_violations, catalog)
        repair_menu = repair_spec_excerpts(current, catalog, excerpt_keys, current_violations)
        # 슬롯 목적은 아웃라인 바로 뒤에 붙인다 — surgeon이 노드 id를 읽는 그 자리에서
        # "이 자리는 무엇을 하려던 자리인가"가 같이 보여야 재선택이 선택지가 된다(§5.2-C).
        # spec 인자가 없으면 흐름도에 동봉된 spec을 쓴다(edit 경로는 spec을 흐름도에 싣고 온다).
        purposes = slot_purpose_block(work, spec if spec is not None else current.get("spec"))
        user_content = (
            f"[흐름도 아웃라인]\n{outline}{purposes}\n\n"
            f"[고칠 문제들 (심각도순)]\n{_findings_lines(round_findings)}\n\n"
            f"[스펙 발췌]\n{excerpts}"
            + (f"\n\n[수리용 액션 스펙 — 삽입(insert/wrap)·교체(update)에 쓸 수 있는 표기. "
               f"세션 여닫기·반복·분기·예외 처리 + 이 흐름도가 이미 쓰는 패키지의 업무 액션. "
               f"여기 없는 표기는 쓰지 말 것]\n{repair_menu}"
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
            _emit_round(round_no, "llm_error", weight=current_weight,
                        remaining=_rule_counts(round_findings), error=str(e)[:200])
            break
        if not ops.operations:  # 고칠 방법이 없다는 정직한 신호 — 가짜 성공 방지
            logger.info("surgeon 라운드 %d: 연산 없음 — 종료", round_no)
            # 🔴 진단상 가장 중요한 종료 사유다 — "고칠 방법이 없다"는 뜻이라, 남은 규칙이
            # 무엇인지가 곧 '수리 어휘가 부족한 지점'이다.
            _emit_round(round_no, "no_ops", weight=current_weight,
                        remaining=_rule_counts(round_findings))
            break

        # 환각 표기를 **적용 전에** 걸러낸다 — 안 그러면 그 하나가 만든 R1(100점)이
        # 정상 연산까지 끌고 폐기된다(drop_unknown_action_ops 독스트링의 실측).
        # 판정은 `work`(id가 붙은 사본) 기준이어야 update의 target 조회가 맞는다.
        proposed = len(ops.operations)
        operations, dropped = drop_unknown_action_ops(
            work, ops.operations, lambda p, a: catalog.get_action_schema(p, a) is not None
        )
        if dropped:
            logger.info("surgeon 라운드 %d: 환각 표기 연산 %d개 제외 — %s",
                        round_no, len(dropped), dropped)
        if not operations:
            # 낼 것이 전부 환각이었다 — 연산 없음과 같은 상태다(가짜 성공 방지).
            _emit_round(round_no, "all_dropped", weight=current_weight, proposed=proposed,
                        dropped=[d[:120] for d in dropped[:5]],
                        remaining=_rule_counts(round_findings))
            no_improve += 1
            if no_improve >= _STOP_AFTER_NO_IMPROVE:
                break
            continue

        applied, errors = apply_edit_ops(work, operations)
        if errors:
            logger.info("surgeon 라운드 %d: 연산 %d개 적용, 실패 %s", round_no, applied, errors)
        strip_ids(work)
        renumber(work)
        if applied == 0:
            no_improve += 1
            _emit_round(round_no, "apply_failed", weight=current_weight,
                        ops=_op_digest(operations), proposed=proposed,
                        dropped=[d[:120] for d in dropped[:5]],
                        errors=[e[:120] for e in errors[:5]],
                        remaining=_rule_counts(round_findings))
            if no_improve >= _STOP_AFTER_NO_IMPROVE:
                break
            continue

        new_violations = collect_violations(work, catalog)
        new_findings, _ = from_violations_dicts(new_violations)
        new_gaps = completeness_findings(work, spec) if spec is not None else []
        new_weight = weight(_error_findings(new_findings + new_gaps))
        # 회귀 가드 — 정적 가중합이 줄었을 때만 채택. 이식 지시가 걸려 있는 라운드는
        # '정적 악화 없음(<=)'까지 허용한다 (이식은 정적 신호에 안 잡히는 개선이므로).
        digest = _op_digest(operations)
        drop_note = [d[:120] for d in dropped[:5]]
        if new_weight < current_weight or (extras_pending and new_weight <= current_weight):
            _emit_round(round_no, "accepted", weight_before=current_weight, weight_after=new_weight,
                        ops=digest, applied=applied, proposed=proposed, dropped=drop_note,
                        errors=[e[:120] for e in errors[:5]],
                        remaining=_rule_counts(new_findings + new_gaps))
            current, current_violations = work, new_violations
            current_weight = new_weight
            repaired = True
            no_improve = 0
            extras_pending = False  # 이식 지시는 1회 반영으로 소진 — 반복 강제하면 진동한다
            emit_flow_frame(current, current_violations, f"교정 라운드 {round_no} 적용")
            # 이후 라운드는 잔여 정적 위반 + 잔여 완성도 항(누락·뭉갬)만 (extra는 1회성)
            round_findings = new_findings + new_gaps
            if not _error_findings(round_findings):
                break
        else:
            logger.info("surgeon 라운드 %d: 가중합 %d→%d 개선 없음 — 폐기",
                        round_no, current_weight, new_weight)
            # 🔴 폐기는 "고쳤는데 되돌렸다"는 뜻이다. 무엇을 시도했는지(ops)와 가중합이
            # 어떻게 움직였는지가 같이 있어야 "삽입이 부수 위반을 만들어 상쇄됐다" 같은
            # 진짜 원인을 판별할 수 있다.
            _emit_round(round_no, "discarded", weight_before=current_weight, weight_after=new_weight,
                        ops=digest, applied=applied, proposed=proposed, dropped=drop_note,
                        remaining=_rule_counts(new_findings + new_gaps))
            no_improve += 1
            if no_improve >= _STOP_AFTER_NO_IMPROVE:
                break

    # 루프가 어떻게 끝났는지를 한 줄로 남긴다 — 라운드 기록만 있으면 "8라운드를 다 썼나,
    # 무개선으로 일찍 빠졌나"가 안 보인다(둘은 처방이 정반대다: 예산 부족 vs 수리 불능).
    final_findings, _ = from_violations_dicts(current_violations)
    final_gaps = completeness_findings(current, spec) if spec is not None else []
    emit({
        "event": "stage", "stage": "refining",
        "message": (f"교정 종료 — 위반 {len(current_violations)}건 · 가중합 {current_weight}"
                    + ("" if repaired else " (한 라운드도 채택되지 않음)")),
        "data": {
            "rounds_used": round_no,
            "max_rounds": max_rounds,
            "repaired": repaired,
            "final_weight": current_weight,
            "remaining": _rule_counts(final_findings + final_gaps),
        },
    })

    # 최종 폴백 — 예산을 다 쓰고도 남은 미해결 must 요구는 자리표시자로 남긴다 (§5-H).
    # 조용히 빠뜨리는 것은 선택지가 아니다.
    if spec is not None:
        current = _placeholder_steps(current, spec)
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


def verify_and_repair(flow: dict, catalog: CatalogLookup, *, spec: dict | None = None) -> dict:
    """흐름도를 검수하고 위반이 있으면 surgeon refine 루프로 교정한다 (v2 시그니처 유지).

    edit 경로·타 솔루션(generate_other) 경로가 이 관문을 그대로 쓴다.
    spec은 선택 — 주면 누락(완성도) 항이 회귀 가드에 함께 들어간다(refine_flow 참조).
    ⚠️ edit 경로에서 spec을 넘길 때는 "이 단계 빼주세요"가 요구 삭제까지 동반해야 한다
    (설계 §6.1의 set_spec 연산). 요구가 남은 채 액션만 지우면 누락 blocker가 그 액션을
    도로 넣는다 — 그래서 여기 기본값은 None이다.
    반환: {"flow": dict, "violations": list[dict], "repaired": bool}.
    """
    emit({"event": "stage", "stage": "verifying", "message": "흐름도 최종 검수 중"})
    return refine_flow(flow, catalog, spec=spec)
