"""verify harness v3 — 검수(R1~R18) + surgeon(EditOps) 기반 refine 루프 + confidence 합성.

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

from ..recommend.research import menu_quote, structural_complement
from ..recommend.stream import emit, emit_flow_frame
from ..verify.catalog import CatalogLookup
from ..verify.checker import derive_session_registry, run_flow_checks
from ..verify.findings import Finding, from_violations, weight
from .edit_ops import (
    EditOps,
    annotate_ids,
    apply_edit_ops,
    render_outline,
    renumber,
    shrink_reason,
    strip_ids,
)
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
    """흐름도 전체를 R1~R18로 검사한다 (세션 레지스트리는 카탈로그에서 유도)."""
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
        # **키 부재일 때만** 0.4다. `or`로 쓰면 점수 0.0(= 검색이 전혀 못 맞힌 액션)이
        # falsy라 "근거 없음"으로 승격돼 0.4를 받는다 — 가장 나쁜 액션이 중간 점수를
        # 받는 셈이다. flow_confidence가 이 값들의 평균이 된 뒤로는 그 오류가 흐름도
        # 신뢰도를 통째로 부풀린다(Qodo #490).
        base = best.get((pkg, act), 0.4)
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


def _action_confidences(flow: dict) -> list[float]:
    """흐름도의 모든 액션 confidence를 중첩(children)까지 훑어 모은다."""
    out: list[float] = []

    def walk(actions: list[dict] | None) -> None:
        for a in actions or []:
            c = a.get("confidence")
            if isinstance(c, (int, float)) and not isinstance(c, bool):
                out.append(float(c))
            walk(a.get("children"))

    for step in flow.get("steps") or []:
        walk(step.get("actions"))
    return out


def retrieval_scale(sink: list[dict] | None) -> str | None:
    """sink의 `score`가 **유사도인지 순위 융합값인지** 판정한다. 판단 불가면 None.

    이 검사가 필요한 이유: `hybrid_search.py`가 점수를 고를 때 리랭커를 우선하고 없으면
    RRF로 떨어진다(`score = item.get("rerank_score") or item.get("rrf_score")`). 그런데 둘은
    **척도가 완전히 다르다** — Voyage relevance는 0~1이고, RRF는 `Σ w/(k+rank)`라 k=60에서
    최댓값이 0.033이다. 그리고 그 폴백은 설계상 조용하다(README: "절대 API가 죽지 않는다").

    신뢰도의 본체를 검색 점수로 삼으면 이 폴백이 **VOYAGE_API_KEY 하나로 신뢰도를 20배
    붕괴**시킨다 — 흐름도는 그대로인데 숫자만 바닥에 눕는다. 그래서 척도를 먼저 확인하고,
    유사도가 아니면 숫자를 내지 않는다(측정 불가). 억지로 0.05를 내면 '나쁜 흐름도'와
    '리랭커가 죽은 것'이 구별되지 않는다.

    ## 점수 크기가 아니라 메타데이터로 가른다 (Qodo #490)

    처음엔 `max(score) > 0.05`로 갈랐는데 양쪽으로 틀린다. (1) 약하게 맞은 정상 relevance를
    폴백으로 오탐해 멀쩡한 흐름도를 측정 불가로 떨군다. (2) `RRF_K`는 env로 조절되므로
    (`int(os.getenv("RRF_K", "60"))`) 값이 작아지면 RRF 상한이 임계를 넘어 **폴백을 못
    잡는다** — 가드가 막으려던 바로 그 방향으로 조용히 실패한다.

    `services/rag.py`가 `score`를 채울 때 원본 필드를 지우지 않고 제자리 갱신하므로
    (`r["score"] = r.get("rerank_score", r.get("rrf_score", ...))`), 판정 근거가 sink에 그대로
    남아 있다. 단 **`rrf_score` 유무로는 못 가른다** — 리랭커를 거친 항목은 융합 결과를
    물려받아 둘 다 갖는다. 기준은 `rerank_score`의 유무다.

    섞인 sink(도구를 여러 번 부르는 사이 리랭커가 죽은 경우)는 **오염으로 본다** — 일부만
    RRF 척도여도 그 액션들의 근거가 바닥으로 눌려 평균이 왜곡된다.
    """
    scored = [
        s for s in (sink or [])
        if isinstance(s.get("score"), (int, float)) and not isinstance(s.get("score"), bool)
    ]
    if not scored:
        return None
    # rerank를 못 거쳤는데 융합 점수를 들고 있는 항목 = 폴백 산물
    if any("rerank_score" not in s and "rrf_score" in s for s in scored):
        return "rrf"
    # 남는 경우: 전부 rerank됨 / 벡터 단독 검색(`db.search`의 `1 - cosine`) — 둘 다 유사도다
    return "relevance"


def compute_flow_confidence(
    flow: dict,
    *,
    sink: list[dict] | None = None,
    findings: list[Finding] | tuple = (),
    must_coverage: float | None = None,
    sim_pass_rate: float | None = None,
    blocking_cards: int = 0,
) -> float | None:
    """흐름도 수준 신뢰도 — **액션별 검색 근거의 평균**. 근거가 없으면 None을 낸다.

    ## 왜 곱셈 산식을 버렸나

    이전 산식은 `must_coverage × 0.8^blockers × 0.95^majors × max(0.3, sim)`이었다. 계수에
    출처가 없다는 것보다 심각한 문제가 실측으로 드러났다 — **판별을 못 했다.**

    실측(v3 런 13케이스 × 3반복 = 39런, `action_core.f1`을 품질 라벨로):

    | 지표 | 케이스 내 rho | 케이스 간 rho |
    |---|---|---|
    | 이전 곱셈 산식 | **+0.011** | +0.593 |
    | 액션 confidence 평균 | **+0.303** | +0.264 |
    | 액션 confidence 최솟값 | −0.038 | +0.567 |

    **케이스 내**가 제품이 화면에 쓰는 주장이다 — "방금 만든 이 흐름도가 몇 점". 같은
    업무정의서로 만든 세 판 중 어느 것이 잘 나왔는지를 이전 산식은 13케이스 중 4개만
    맞혔다(찍으면 절반). 케이스 간 +0.59는 "어려운 문서엔 낮은 점수"인데, 그건 문서를 보면
    알고 `must_coverage`가 입력이라 부분적으로 동어반복이다.

    검색 근거 평균은 케이스 내에서 이전 산식의 27배다. 다만 n=13케이스라 통계적으로
    0과 확실히 구별되지는 않는다 — **더 낫다는 방향은 실측이고, 크기는 아직 미확정**이다.

    ## 무엇을 재는가

    액션별 confidence는 이미 검색 근거가 본체다(`attach_confidence`: `base = RAG best score`,
    R1 환각이면 0.2). 그것을 흐름도 단위로 평균 낸 값이다 — "이 흐름도의 액션들이 카탈로그에
    얼마나 단단히 붙어 있나".

    **한계(명시)**: 이건 자기 일관성 측정이다. 검색 질의가 모델 자신의 계획에서 나오므로,
    카탈로그에 그럴듯한 액션이 있는 **확신에 찬 오답은 높게 나온다**. 카탈로그에 아예 없는
    환각은 R1이 잡지만(0.2), '있지만 이 업무엔 틀린 액션'은 이 지표로 못 잡는다.

    blocker·major·커버리지·시뮬레이션은 **곱하지 않고 세기만 한다** — 곱해도 판별이 안 되는
    것이 위 실측이고, 개수 자체는 "무엇을 고치나"에 답하므로 관측에는 그대로 남긴다.
    """
    return confidence_breakdown(
        flow,
        sink=sink,
        findings=findings,
        must_coverage=must_coverage,
        sim_pass_rate=sim_pass_rate,
        blocking_cards=blocking_cards,
    )["confidence"]


def confidence_breakdown(
    flow: dict,
    *,
    sink: list[dict] | None = None,
    findings: list[Finding] | tuple = (),
    must_coverage: float | None = None,
    sim_pass_rate: float | None = None,
    blocking_cards: int = 0,
) -> dict:
    """`compute_flow_confidence`의 근거를 펼친 것. 산식은 여기 한 벌만 있다.

    왜 함수로 두는가: 관측 이벤트가 근거를 실어야 한다("0.15를 올리려면 무엇을 고치나"에
    답하려면 결과값만으로는 안 된다). 그런데 호출부가 계수를 **베껴 쓰면** 산식을 고칠 때
    한쪽만 고쳐져 관측이 조용히 거짓말을 한다(Qodo). 그래서 결과값과 분해를 같은 함수가 낸다.

    `confidence`가 None이면 `unmeasurable`에 사유가 있다 — 숫자를 못 낸 것과 낮게 나온 것은
    다른 사실이라 섞지 않는다.
    """
    confs = _action_confidences(flow)
    scale = retrieval_scale(sink)
    obs = {
        # 아래는 전부 **관측용** — 신뢰도에 곱하지 않는다
        "basis": "retrieval",
        "n_actions": len(confs),
        "retrieval_scale": scale,
        "blockers": sum(1 for f in findings if f.severity == "blocker"),
        "majors": sum(1 for f in findings if f.severity == "major"),
        "blocking_cards": blocking_cards,
        "must_coverage": round(must_coverage, 3) if must_coverage is not None else None,
        "sim_pass_rate": round(sim_pass_rate, 3) if sim_pass_rate is not None else None,
    }
    if scale == "rrf":
        # 리랭커 폴백 — score가 순위 융합값이라 유사도로 읽으면 안 된다
        return {**obs, "confidence": None, "unmeasurable": "리랭커 폴백(검색 점수가 유사도가 아님)"}
    if not confs:
        return {**obs, "confidence": None, "unmeasurable": "액션 신뢰도 없음"}
    return {**obs, "confidence": round(sum(confs) / len(confs), 2), "unmeasurable": None}


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


def repair_spec_excerpts(
    flow: dict, catalog: CatalogLookup, exclude: set[tuple[str, str]], vocabulary=None,
) -> str:
    """'삽입' 수리에 필요한 액션 스펙 — 위반 목록에는 없는 어휘를 동봉한다 (처방 3).

    surgeon은 스펙에 없는 표기를 못 쓴다(환각 방지 규칙). 그런데 세션 누수(R8)·가짜
    반복(R14)의 수리는 흐름도에 **아직 없는** opener/closer·Loop 이터레이터·Try/Catch를
    삽입해야 한다 — 위반 액션 발췌만으로는 재료가 없어 정직한 무연산으로 끝난다(0374
    JIRA 봇 실측). research.structural_complement를 재사용해 카탈로그 직조회로 공급한다.

    ## 턴 어휘를 덧붙인다 (RPA-359)

    구조 보완만으로는 **제어 흐름과 세션 여닫기뿐이고 업무 액션이 0종**이었다(실측 15종).
    그래서 조사가 찾아 둔 업무 액션을 이 흐름도가 쓰는 패키지에 한해 덧붙인다 — 같은 턴에
    이미 확보한 어휘이므로 추가 조회가 없고, 패키지로 좁히므로 프롬프트가 부풀지 않는다.

    ⚠ **어휘 전량을 싣지 않는다.** 84종에 스펙까지 실으면 약 12,000자이고 수리는 한 턴에
    최대 5라운드다. 저장소는 단일 진실이고 여기 실리는 것은 그 view다.
    """
    seen = set(exclude)
    blocks: list[str] = []

    def _put(pkg: str, act: str) -> bool:
        if (pkg, act) in seen:
            return False
        spec = catalog.get_action_schema(pkg, act)
        if spec is None:
            return False
        seen.add((pkg, act))
        blocks.append(_spec_block(pkg, act, spec))
        return True

    flow_pkgs = _flow_packages(flow)
    for pkg, act in structural_complement(catalog, flow_pkgs):
        _put(pkg, act)
    if vocabulary is not None:
        # 삽입 순서 그대로 — 정렬하면 라운드마다 순서가 흔들려 캐시를 잃는다. 그리고 그
        # 순서가 곧 조사 관련도순이라(단위별 상위부터 라운드로빈) 앞에서 자르는 것이 맞다.
        added = 0
        for pkg, act in vocabulary:
            if added >= _REPAIR_VOCAB_CAP:
                break
            if pkg in flow_pkgs and _put(pkg, act):
                added += 1
    return "\n".join(blocks)


# R1 되묻기에 실을 패키지당 액션 이름 수 상한. 이름만이라 한 줄 ~25자 — 60개면 약 1.5KB로,
# 스펙까지 싣는 것(액션당 ~190자)보다 한 자릿수 싸다.
_R1_PACKAGE_NAME_CAP = 60

# 수리 메뉴에 덧붙일 턴 어휘 개수 상한 (RPA-359).
#
# 상한 없이 붙이면 부푼다 — 실측(2026-07-30) 흐름도가 쓰는 7개 패키지의 어휘를 전부 실었더니
# 삽입 재료가 15종 1,490자에서 **139종 16,314자**가 됐다. 수리는 한 턴 최대 5라운드이고
# 이 블록은 user 메시지라 라운드 간 캐시도 안 탄다.
#
# 24종이면 약 4.5KB — 구조 보완(1.5KB) 위에 3배쯤 얹는 수준이다. 어휘의 삽입 순서가 곧
# 조사 관련도순(단위별 상위부터 라운드로빈)이라 앞에서 자르는 것이 관련도순으로 자르는 것과
# 같다. ⚠ 이 값은 시작점이고 실측으로 조정한다 — 수리 채택률과 턴 비용을 함께 본다.
_REPAIR_VOCAB_CAP = 24


def r1_package_hints(violations: list[dict], catalog: CatalogLookup, vocabulary=None) -> str:
    """R1 위반이 난 **패키지의 실제 액션 이름 목록** (RPA-359).

    실측(2026-07-30 턴 fcd58640): `Microsoft 365 Excel/Read cell`이 두 번 나왔는데
    카탈로그에 그 이름이 없다. 비슷한 것은 셋이다 — `Get cell`(의미 일치),
    `Read cell format`·`Read cell formula`(접두 일치). **문자열이 가까운 쪽을 코드가 고르면
    서식을 읽는 엉뚱한 액션이 조용히 들어간다.** 그래서 코드는 후보를 좁혀 주기만 하고
    고르는 것은 모델이다 — `_fix_vocab` 주석의 원칙과 같다.

    앞서는 되묻기가 "메뉴에 없다"까지만 말하고 무엇이 있는지는 말하지 않았다. 그 회차는
    44턴 중 4회 발동해 **1회 성공**했다.

    ⚠ **패키지가 카탈로그에 실재할 때만** 낸다. `package="needs"`처럼 패키지 자체가 없는
    오염(실측 3건)은 나열할 것이 없고, 그 경우는 다른 처방이 필요하다.
    """
    packages = {v.get("package") for v in violations if v.get("rule") == "R1" and v.get("package")}
    if not packages:
        return ""
    by_pkg, all_packages = _package_action_names(catalog, packages, _R1_PACKAGE_NAME_CAP)
    blocks: list[str] = []
    missing: list[str] = []
    for pkg in sorted(packages):
        shown, total = by_pkg[pkg]
        if not shown:
            # 패키지 자체가 카탈로그에 없다 — 나열할 액션이 없다. 그렇다고 **빈손으로 두면
            # 안 된다**: 그러면 재요청이 "표기를 바로잡아라"라고만 하고 바로잡을 대상조차
            # 없는 상태가 된다(실측: 사용자가 "microsoft 패키지로 바꿔줘"라고 했는데
            # 카탈로그에는 `Microsoft 365 Excel` 등 6종이 있고 `microsoft`는 없다).
            # 이름이 겹치는 후보를 주고, 없으면 **없다는 사실 자체**를 알린다.
            missing.append(_missing_package_line(pkg, all_packages))
            continue
        if vocabulary is not None:
            # **실제로 프롬프트에 실은 것만** 어휘에 넣는다 (Qodo #473). 전량을 넣으면 대형
            # 카탈로그에서 어휘가 표시 상한과 무관하게 부풀고, 모델이 본 적 없는 이름이
            # 나중에 수리 메뉴(repair_spec_excerpts)에 올라간다.
            vocabulary.extend(((pkg, a) for a in shown), "r1_hint")
        more = f" … 외 {total - len(shown)}개" if total > len(shown) else ""
        # 값은 menu_quote로 경계를 고정한다 (Qodo #473). 메뉴 렌더링과 같은 규칙이다 —
        # 따옴표·개행이 든 이름이 블록 형식을 깨거나 프롬프트 주입이 되지 않게 한다.
        blocks.append(
            f"- package={menu_quote(pkg)} 의 실제 액션: "
            + ", ".join(menu_quote(a) for a in shown)
            + more
        )
    out = ""
    if blocks:
        out += (
            "\n\n[표기가 틀린 패키지의 실제 액션 이름]\n"
            "아래 이름 중에서 **의도에 맞는 것을 골라** update 하라. 이름이 비슷하다고 고르지 말고 "
            "무엇을 하는 액션인지로 고른다(예: 셀 '값'을 읽는 것과 '서식'을 읽는 것은 다른 액션이다).\n"
            "정말 대응이 없으면 그 액션을 지우지 말고 notes에 남긴다.\n" + "\n".join(blocks)
        )
    if missing:
        out += (
            "\n\n[카탈로그에 **없는** 패키지]\n"
            "아래 패키지는 카탈로그에 존재하지 않는다. 표기를 고쳐도 통과하지 않는다 — "
            "후보가 있으면 그중에서 고르고, **없으면 지어내지 말고** operations를 비운 뒤 "
            "answer에 '카탈로그에 없다'고 답하라.\n" + "\n".join(missing)
        )
    return out


# 없는 패키지에 제시할 후보 수 상한. 이름이 겹치는 것만 고르므로 보통 몇 개다
# (실측: "microsoft" → Microsoft 365 Excel/Outlook/OneDrive/Calendar/Teams 등 6종).
_MISSING_PACKAGE_CANDIDATES = 8


def _missing_package_line(pkg: str, names: list[str]) -> str:
    """카탈로그에 없는 패키지 한 줄 — 이름이 겹치는 실재 패키지를 후보로 붙인다.

    부분 문자열(대소문자 무시) 양방향으로 본다: 사용자가 짧게 말한 경우("microsoft")와
    길게 말한 경우("Microsoft 365 Excel 고급") 둘 다 걸리게 하기 위해서다. 의미 검색이
    아니라 **표기 매칭**이라 결정론이고, 아무것도 안 걸리면 후보 없이 사실만 남긴다.

    ⚠ `names`를 **받는다** — 여기서 카탈로그를 훑으면 없는 패키지 수만큼 전량 스캔이
    반복된다(Qodo #485). `_package_action_names`가 같은 이유로 이미 1회 순회로 고쳐졌는데
    새로 만든 이 함수가 그 실수를 되풀이했다.
    """
    key = (pkg or "").strip().lower()
    hits = [p for p in names if key and (key in p.lower() or p.lower() in key)]
    shown = hits[:_MISSING_PACKAGE_CANDIDATES]
    if not shown:
        return f"- package={menu_quote(pkg)} — 카탈로그에 없음 (이름이 겹치는 패키지도 없음)"
    more = f" … 외 {len(hits) - len(shown)}종" if len(hits) > len(shown) else ""
    return (
        f"- package={menu_quote(pkg)} — 카탈로그에 없음. 이름이 겹치는 패키지: "
        + ", ".join(menu_quote(p) for p in shown) + more
    )


def _package_action_names(
    catalog: CatalogLookup, packages: set[str], cap: int
) -> tuple[dict[str, tuple[list[str], int]], list[str]]:
    """카탈로그 **1회 순회**로 두 가지를 모은다 → ({패키지: (이름 상한개, 총 개수)}, 전체 패키지명).

    패키지마다 `_iter_catalog`를 다시 부르면 전량 스캔이 패키지 수만큼 반복된다. 이 함수는
    수리 라운드마다 불리므로(한 턴 최대 5라운드) 그 곱이 그대로 쌓인다 (Qodo #473).

    전체 패키지명도 여기서 함께 낸다 — 없는 패키지의 후보를 고를 때 필요한데, 따로 훑으면
    순회가 두 번이 된다(Qodo #485). 필요한 것이 둘이어도 순회는 하나면 된다.

    이름은 **상한까지만** 들고 나머지는 세기만 한다 — 뒤쪽은 "… 외 N개" 한 조각으로만 쓰이니
    전량을 리스트로 만들 이유가 없다. 카탈로그가 1,375종인 지금도, 더 커져도 상한이 재료비를
    묶는다.
    """
    kept: dict[str, list[str]] = {p: [] for p in packages}
    total: dict[str, int] = dict.fromkeys(packages, 0)
    all_names: set[str] = set()
    for pkg, act in _iter_catalog(catalog):
        all_names.add(pkg)
        if pkg not in kept:
            continue
        total[pkg] += 1
        if len(kept[pkg]) < cap:
            kept[pkg].append(act)
    return {p: (kept[p], total[p]) for p in packages}, sorted(all_names)


def _iter_catalog(catalog: CatalogLookup):
    """카탈로그 전량을 (package, action)으로 훑는다. 순회를 지원하지 않으면 빈 결과."""
    it = getattr(catalog, "iter_action_schemas", None)
    if it is None:
        return
    try:
        for spec in it():
            pkg, act = spec.get("package"), spec.get("action")
            if pkg and act:
                yield pkg, act
    except Exception as e:  # noqa: BLE001 — 힌트 실패가 수리를 막지 않게
        logger.warning("카탈로그 순회 실패 — R1 힌트 생략: %s", e)


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


def _op_line(op) -> str:
    """EditOp 한 건을 한 줄로 — 관측 이벤트에 실을 요약."""
    tail = op.target or (",".join(op.targets) if op.targets else "") or op.step_id or ""
    where = f" {op.position or ''}{(' ' + op.anchor) if op.anchor else ''}".rstrip()
    what = ""
    if op.action:
        what = f" {op.action.get('package')}/{op.action.get('action')}"
    elif op.container:
        what = f" {op.container.get('package')}/{op.container.get('action')}"
    elif op.parameters:
        what = f" ({len(op.parameters)}개 파라미터)"
    return f"{op.op} {tail}{where}{what}".strip()


def _emit_round(
    round_no: int, ops: list, applied: int, errors: list[str],
    verdict: str, before: int, after: int | None,
) -> None:
    """수리 라운드 한 번의 **제안·적용·채택**을 관측 이벤트로 남긴다.

    이게 없으면 수리가 헛돌 때 원인을 못 가른다 — 연산을 못 냈는지, 냈는데 적용에 실패했는지
    (id가 앞선 remove로 사라지는 등), 적용은 됐는데 가중합이 안 줄었는지는 처방이 전혀 다르다.
    실측(2026-07-29): 게이트 2라운드 + refine 2라운드가 전부 폐기됐는데, 로그가 logger.info라
    (루트 로거 WARNING) 어느 경우인지 토큰 수로도 역산할 수 없었다.
    """
    delta = f"{before}→{after}" if after is not None else str(before)
    emit({
        "event": "stage", "stage": "verifying",
        "message": f"수리 라운드 {round_no} · 연산 {len(ops)}건 중 {applied}건 적용 — {verdict}",
        "data": {
            "round": round_no, "verdict": verdict, "applied": applied,
            "weight": delta,
            "ops": [_op_line(o) for o in ops][:12],
            "errors": list(errors)[:6],
        },
    })


def _round_feedback(verdict: str, errors: list[str], *, kept: bool, hint: str = "") -> str:
    """직전 라운드가 왜 버려졌는지를 다음 라운드 입력에 싣는다 (RPA-371).

    앞서는 라운드마다 프롬프트를 `current`에서 새로 만들 뿐이었다. 그런데 **반려된 라운드는
    흐름도를 되돌리므로** 아웃라인·위반·스펙 발췌가 전부 그대로고, 결국 입력이 직전 라운드와
    글자까지 같아진다 — 모델은 자기가 방금 무엇에 실패했는지 못 본 채 같은 답을 낸다.

    실측(358초 턴 `44326af94fdb`, 2026-08-03): 게이트 수리 1·2라운드의 연산 5건이 완전히
    동일했다(`Browser/browserClose` insert ×3 + move + remove). 세션 닫기 insert가 전부
    카탈로그에 없는 이름이라 탈락하고 move·remove만 남아 순액션 −1이 되면서 반려됐는데,
    2라운드가 똑같은 답을 내 31.3초를 더 태웠다. refine 루프의 2라운드도 같은 모양이었다.

    ⚠ **반려된 라운드에는 "지금 보는 것이 적용 전 상태"라고 못 박아야 한다.** 안 그러면
    모델이 자기 수리가 이미 들어간 줄 알고 다음 문제로 넘어가, 정작 반려된 결함을 방치한다.
    `r1_package_hints`는 이 구멍을 못 메운다 — 그쪽은 **흐름도에 이미 있는** R1 위반만
    대상이라, *삽입하려다* 실패한 이름은 힌트를 못 받는다.

    ## 판정만으로는 부족하다 — `hint`가 필요한 이유

    이름 오류는 `errors`가 처방까지 대신해 준다("카탈로그 표기 그대로 나눠 적으세요").
    그런데 **축소 반려는 `errors`가 비어 있을 수 있다** — 연산이 전부 적용됐는데 결과가
    줄어든 경우다. 그때 판정 한 줄만 주면 모델은 답을 바꾸긴 해도 방향을 모른다.
    실측(355초 턴 `6d4c3da18c93`, 2026-08-03): 1라운드가 액션 5개를 잃고 반려되자
    2라운드는 remove를 하나 더 늘려 7개를 잃었다. 그래서 그 판정에는 **무엇을 지켜야
    하는지**를 같이 준다.
    """
    # 에러 문구에는 **모델이 쓴 값이 그대로 박혀 있다** — `카탈로그에 없는 액션
    # "Browser"/"browserClose"`의 인용부호 안이 모델 출력이다. 손대지 않고 프롬프트에
    # 붙이면 (1) 그 안의 개행이 bullet 구조를 깨고 (2) 모델이 쓴 문장이 우리 지시문처럼
    # 읽힌다. 한 줄로 접고 길이를 묶는다 (Qodo 리뷰).
    errs = [" ".join(str(e).split())[:300] for e in (errors or []) if str(e or "").strip()][:6]
    if kept and not errs:
        return ""
    if kept:
        lines = ["\n\n[직전 라운드는 채택됐지만 아래 연산은 적용되지 않았다]"]
    else:
        lines = [
            "\n\n[직전 라운드는 통째로 버려졌다 — 같은 답을 다시 내지 마라]",
            f"판정: {verdict}",
            "위 [흐름도 아웃라인]은 그 수리가 **적용되지 않은** 상태다. 같은 연산을 그대로 "
            "다시 내면 같은 이유로 또 버려진다.",
        ]
    if hint:
        lines.append(hint)
    if errs:
        lines.append("적용되지 않은 연산:")
        lines.extend(f"- {e}" for e in errs)
    return "\n".join(lines)


def refine_flow(
    flow: dict,
    catalog: CatalogLookup,
    *,
    vocabulary=None,
    extra_findings: list[Finding] | None = None,
    max_rounds: int = MAX_REFINE_ROUNDS,
    purpose: str = "verify",
    rules: frozenset[str] | None = None,
    note: str = "",
    caption: str | None = None,
) -> dict:
    """findings(정적 위반 + 심판/L2/L3 지시)를 surgeon EditOps 패치로 반복 교정한다.

    라운드마다: findings → surgeon(EditOps만 출력) → 결정론 적용 → L0/L1 재검증 →
    심각도 가중합이 줄었을 때만 채택(회귀 가드). 수렴: 오류 findings 소진 / 라운드
    소진 / 연속 무개선 2회. extra_findings(심판 이식 지시 등)는 첫 라운드에만 싣는다 —
    적용 여부를 정적 재검증으로 판정할 수 없으므로 반복 강제하면 진동한다.

    `rules`를 주면 그 규칙의 위반만 findings와 회귀 가드 축에 넣는다 — **값이 아직 없는
    구조 단계**를 교정할 때 쓴다. 그때 R2~R5(파라미터)·R9~R11(변수 흐름)까지 세면 "필수
    파라미터가 비었다"가 목록을 덮어 정작 구조 결함이 묻히고, 가중합도 값 단계가 채울
    항목에 좌우된다. `note`는 그 호출에만 붙는 추가 지시(예: 파라미터를 채우지 마라).

    반환: {"flow", "violations", "repaired"}.
    """
    def _scoped(fs: list[Finding]) -> list[Finding]:
        return fs if rules is None else [f for f in fs if f.rule in rules]

    violations = collect_violations(flow, catalog)
    findings, _cards = from_violations_dicts(violations)
    findings = _scoped(findings)
    round_findings = findings + list(extra_findings or [])
    if not _error_findings(round_findings):
        return {"flow": flow, "violations": violations, "repaired": False}

    emit({"event": "stage", "stage": "verifying",
          "message": caption or
          f"검수 위반 {len(violations)}건 · 개선 지시 {len(extra_findings or [])}건 교정 중",
          "data": {
              # 수리가 **무엇을 재료로 쥐고 있었는지** — 이게 없으면 "고칠 방법이 없었다"와
              # "방법이 있었는데 못 골랐다"를 사후에 못 가른다 (RPA-359).
              "vocabulary": len(vocabulary) if vocabulary is not None else 0,
              "violations": [
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
    # 직전 라운드의 판정·미적용 연산 — 다음 라운드 입력에 실어 같은 답이 또 나오지 않게 한다.
    feedback = ""

    for round_no in range(1, max_rounds + 1):
        work = annotate_ids(copy.deepcopy(current))
        outline = render_outline(work)
        excerpts, excerpt_keys = _spec_excerpts(current_violations, catalog)
        repair_menu = repair_spec_excerpts(current, catalog, excerpt_keys, vocabulary)
        # R1 위반 패키지의 실제 액션 이름 — 라운드마다 새로 잰다(위반이 바뀌면 대상도 바뀐다).
        r1_hints = r1_package_hints(current_violations, catalog, vocabulary)
        user_content = (
            f"[흐름도 아웃라인]\n{outline}\n\n"
            f"[고칠 문제들 (심각도순)]\n{_findings_lines(round_findings)}\n\n"
            f"[스펙 발췌]\n{excerpts}"
            + (f"\n\n[수리용 액션 스펙 — 세션 여닫기·반복·분기·예외 처리를 삽입(insert/wrap)할 때 이 표기 사용]\n{repair_menu}"
               if repair_menu else "")
            + r1_hints
            + feedback
            + (f"\n\n{note}" if note else "")
        )
        try:
            ops = chat_json(
                [{"role": "system", "content": _SURGEON_PROMPT},
                 {"role": "user", "content": user_content}],
                purpose=purpose, model_cls=EditOps,
            )
        except (ValueError, RuntimeError) as e:
            logger.warning("surgeon 라운드 %d 출력 실패 — 현재본 유지: %s", round_no, e)
            _emit_round(round_no, [], 0, [], "출력 실패", current_weight, None)
            break
        if not ops.operations:  # 고칠 방법이 없다는 정직한 신호 — 가짜 성공 방지
            _emit_round(round_no, [], 0, [], "연산 없음", current_weight, None)
            break

        applied, errors = apply_edit_ops(work, ops.operations, catalog=catalog)
        strip_ids(work)
        renumber(work)
        if applied == 0:
            _emit_round(round_no, ops.operations, 0, errors, "적용 실패", current_weight, None)
            feedback = _round_feedback("연산이 하나도 적용되지 않음", errors, kept=False)
            no_improve += 1
            if no_improve >= _STOP_AFTER_NO_IMPROVE:
                break
            continue

        new_violations = collect_violations(work, catalog)
        new_findings, _ = from_violations_dicts(new_violations)
        new_findings = _scoped(new_findings)
        new_weight = weight(_error_findings(new_findings))
        # 회귀 가드 — 정적 가중합이 줄었을 때만 채택.
        #
        # 이식 지시(L2/L3)가 걸린 라운드는 '정적 악화 없음(<=)'까지 허용해 왔다. 정적 신호에
        # 안 잡히는 개선이 있을 수 있어서다. 그런데 그 관용이 **순서를 흔드는 라운드**에도
        # 적용돼 사고가 났다 — 실측(2026-07-29): `move n2 before n7` 한 줄이 `Browser/Open`을
        # 클릭 두 개 뒤로 옮겼는데 가중합이 50→50이라 채택됐다. 브라우저를 열기 전에 클릭하는
        # 흐름도가 그렇게 나왔다. 정적 검수는 이걸 못 본다: `Recorder/Click`은 브라우저 세션을
        # 파라미터로 받지 않아 R7의 의존 그래프에 안 걸린다.
        #
        # 그래서 관용은 **덧붙이기만 하는 라운드**에만 준다. 기존 액션을 옮기거나 지우는 연산이
        # 섞였으면 정적 가중합이 **실제로 줄어야** 받는다 — 순서·구성을 건드리는 변경은
        # 증거를 요구한다. 정당한 재배치(변수 정의 전 사용 해소 등)는 어차피 가중합을 줄인다.
        #
        # 그리고 **줄어든 라운드는 가중합과 무관하게 반려한다.** 액션이 사라지면 그 요구를
        # 담당하던 자리가 통째로 없어지는데, 정적 검수는 '없는 것'을 지적하지 못하므로
        # 가중합이 오히려 **떨어진다** — 지우면 점수가 오르는 구조다. 실측(2026-07-29):
        # 게이트 수리가 20액션을 4액션으로 줄이고 notes에 "자동화 불가"로 적어 낸 적이 있어
        # 그쪽에는 `_repair_regression` 가드를 뒀는데, 여기에는 없어 비대칭이었다. 커버리지
        # 지적을 받은 라운드가 remove로 답하면 같은 일이 이 루프에서도 성립한다.
        disruptive = any(o.op in ("move", "remove") for o in ops.operations)
        lenient = extras_pending and not disruptive
        shrank = shrink_reason(current, work)
        if shrank:
            _emit_round(round_no, ops.operations, applied, errors,
                        f"흐름이 줄어 반려 ({shrank})", current_weight, new_weight)
            # 이 판정은 대개 **절반짜리 수리**의 그림자다 — 짝이 되는 insert가 탈락하고
            # move·remove만 남으면 순액션이 준다. 그래서 errors를 같이 보여줘야 모델이
            # "지우지 마라"가 아니라 "닫기 액션 이름을 바로 써라"로 읽는다.
            # errors가 비었으면(연산은 다 적용됐는데 결과가 줄었으면) 처방이 없으므로 hint를 준다.
            feedback = _round_feedback(
                f"흐름이 줄어 반려 ({shrank})", errors, kept=False,
                hint="액션 총수가 줄면 그 라운드는 **무조건** 버려진다 — 같이 낸 멀쩡한 연산까지 "
                     "함께 버려진다. 지운 액션이 하던 일을 대신할 액션을 같은 출력에 insert 하거나, "
                     "지우는 대신 update로 바꿔라. ⚠ 컨테이너(Try·Catch·Finally·Loop·If·Step)를 "
                     "remove 하면 그 안의 액션이 전부 같이 사라진다 — 껍데기만 걷어내려면 안의 "
                     "액션을 먼저 move로 빼낸 뒤 지워라.")
            no_improve += 1
            if no_improve >= _STOP_AFTER_NO_IMPROVE:
                break
            continue
        if new_weight < current_weight or (lenient and new_weight <= current_weight):
            _emit_round(round_no, ops.operations, applied, errors, "채택",
                        current_weight, new_weight)
            # 채택돼도 탈락한 연산은 남는다 — 다음 라운드가 같은 이름으로 또 시도하지 않게.
            feedback = _round_feedback("채택", errors, kept=True)
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
            _emit_round(round_no, ops.operations, applied, errors, "개선 없어 폐기",
                        current_weight, new_weight)
            feedback = _round_feedback(
                f"결함 가중합이 줄지 않아 폐기 ({current_weight}→{new_weight})",
                errors, kept=False)
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


def verify_and_repair(flow: dict, catalog: CatalogLookup, vocabulary=None) -> dict:
    """흐름도를 검수하고 위반이 있으면 surgeon refine 루프로 교정한다 (v2 시그니처 유지).

    edit 경로·타 솔루션(generate_other) 경로가 이 관문을 그대로 쓴다.
    `vocabulary`는 선택이다 — v1/v2가 같은 함수를 부르고 그쪽엔 턴 어휘가 없다.
    반환: {"flow": dict, "violations": list[dict], "repaired": bool}.
    """
    emit({"event": "stage", "stage": "verifying", "message": "흐름도 최종 검수 중"})
    return refine_flow(flow, catalog, vocabulary=vocabulary)
