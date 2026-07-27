"""judge — 다중 후보 심판·승자 선정·이식 지시 (v3 설계 §2-[5], v4 Phase 4 상관 오류 대응).

병합이 아니라 "승자 + 개선 지시"다: 두 트리의 자동 병합은 세션·변수 정합을 깨는
고위험 연산이라, 패자의 장점은 findings(이식 지시)로 내려 refine이 EditOps로 안전하게
반영한다. 루브릭의 절반 이상은 **결정론 신호**(위반 가중합·must 커버리지·시뮬레이션
통과율)로 앵커링해 LLM 심판의 분산·장황 편향을 줄인다. must 요구 미충족(missing)은
가중합에 묻히지 않는 하드 게이트다.

## v4에서 바뀐 것 — 왜 채점형을 버리고 반증형을 얹었나

2026-07-26 골드셋 3반복 실측: v4는 v3 대비 **재현율 +0.048, 정밀도 −0.103**. 재현율은
올랐는데 정밀도만 빠졌다는 건 "더 많이 만들었는데 더 많이 틀렸다"는 뜻이고, 그걸 걸러야
할 심판이 못 걸렀다는 뜻이다. 생성기(compose)와 심판이 **같은 모델**이라 틀린 방향으로
함께 확신하는 **상관 오류**가 유력 후보다. 모델을 바꿀 수 없으므로 대응은 셋뿐이다:

  ① **과업 뒤집기** — 심판에게 "점수를 매겨라"가 아니라 "이 후보가 **실패할 이유를 찾아라**".
     채점은 생성기와 같은 선호(장황·방어적 구조를 좋게 봄)를 재생산하지만, 반증은 산출물의
     성격 자체가 다르다(결함 목록 + 실패 시나리오). 근거를 못 대면 적을 수 없어진다.
  ② **입력 비대칭** — 후보를 보여주기 **전에** spec(+문서)만으로 "이 업무라면 어떤 단계가
     필요한가"를 독립 생성시키고, 그 목록을 후보와 대조한다. 후보를 먼저 보면 앵커링돼
     **후보에 있는 것만** 검증한다 — 아무도 안 만든 단계가 영영 안 보이는 이유다.
  ③ **중요 슬롯 3회 독립 다수결** — 판정을 뒤집을 만큼 무거운 지적만 3회 재심해 다수결.
     전량 3회는 안 한다(비용 3배). "중요 슬롯"의 정의는 `_is_critical`에 코드로 박아 뒀다.

### 반증 결과를 어떻게 판정에 넣나 (설계 판단)

**점수 환산과 게이트를 둘 다 쓴다.**
- 점수만 쓰면: 확증된 치명 결함이 0.4 가중치 안에서 희석돼, 결정론 신호가 좋은 후보가
  "치명 결함을 안고" 이긴다. 정밀도 대책으로서는 무력하다.
- 게이트만 쓰면: LLM이 fatal을 남발하는 라운드에 전 후보가 자격을 잃고, 폴백이 사실상
  결정론 점수 단독 판정으로 퇴화한다(= 심판을 안 한 것).
그래서 **모든 지적은 점수(감점)로, 3표 다수결을 통과한 치명 지적만 게이트로** 승격한다.
게이트는 후보를 통째로 배제하는 불연속 결정이라 1표로는 못 내린다 — 이게 ③을 붙인 자리다.

### 비용 (호출 수 — 반드시 같이 읽어야 하는 숫자)

기존: **LLM 1콜**(후보 전원을 한 프롬프트에 넣고 채점).
새 구조: ② 맹목 기대 **1콜** + ① 후보별 반증 **N콜** + ③ 중요 슬롯 다수결 **3콜**
        = `1 + N + 3`. v4 후보 수는 문서 없으면 2, 있으면 3 → **6~7콜 (최대 7배)**.
        중요 슬롯이 하나도 없으면 ③을 건너뛰어 3~4콜이다.
입력 토큰은 7배가 아니다 — 반증 콜은 후보 **하나**의 아웃라인만 싣는다(레거시는 전원).
지연도 7배가 아니다 — 3라운드(맹목 → 반증 팬아웃 → 투표 팬아웃)로 접힌다.
그래도 초안(1상) 구간의 비용/지연이라 **끄는 스위치**를 둔다: `V4_JUDGE_REFUTATION=false`면
레거시 채점형 1콜 경로(`_judge_by_rubric`)로 그대로 돌아간다.
"""

import concurrent.futures
import contextvars
import copy
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.core import config as _core_config

from .. import config
from ..verify.findings import Finding, weight
from .edit_ops import annotate_ids, render_outline
from .jsonio import chat_json

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _load(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


_PROMPT = _load("judge.md")                    # 레거시 채점형 — 플래그 off 경로
_BLIND_PROMPT = _load("judge_blind.md")        # ② 입력 비대칭: 후보를 보기 전 독립 생성
_REFUTE_PROMPT = _load("judge_refute.md")      # ① 반증 프레이밍
_VOTE_PROMPT = _load("judge_slot_vote.md")     # ③ 중요 슬롯 재심(다수결)

# ─────────────────────────────────────────────────────────────────────────────
# 상한·가중치 — 전부 비용 또는 편향 방어가 이유다
# ─────────────────────────────────────────────────────────────────────────────

# 맹목 기대 상한. 기대가 요구 수를 압도하면 대조가 아니라 **새 스펙 작성**이 되고, 후보는
# 아무도 못 만족시키는 목록에 대해 일제히 감점당한다(= 판별력 0). spec의 접착제 도출 상한
# (MAX_GLUE_REQUIREMENTS=6)과 같은 자릿수로 둔다.
MAX_EXPECTATIONS = 12

# 재심 슬롯 상한. 다수결 프롬프트 하나에 전 슬롯을 배치로 싣기 때문에(콜 수를 슬롯 수와
# 무관하게 3으로 고정하는 방법), 슬롯이 많아지면 프롬프트가 부풀고 판정이 흐려진다.
# 무게순으로 잘라 정말 판정을 뒤집을 것만 남긴다.
MAX_CRITICAL_SLOTS = 8

# 다수결 라운드 수. **홀수여야** 과반이 명확하다. 3은 계획서가 지정한 값이고, 1로 두면
# 다수결 자체가 꺼진다(비용 절감 노브 — env가 아니라 코드 상수인 이유는 키를 하나만
# 늘리기 위해서다. 운영에서 끄고 싶으면 V4_JUDGE_REFUTATION으로 통째로 끈다).
VOTE_ROUNDS = 3

# 지적 심각도 → 감점. fatal 1건이 major 3건과 맞먹게 잡았다 — 반증 프롬프트가 fatal의
# 문턱을 "산출물이 안 나오거나 잘못 나온다"로 못 박고 있어, 그 급이면 major 몇 건보다
# 무거운 게 맞다. minor는 순위를 뒤집지 않는 잡음 수준(1)으로 남긴다.
_DEFECT_PENALTY = {"fatal": 12, "major": 4, "minor": 1}

# 기대 미충족(누락) 감점 — must 미충족은 fatal과 동급이다. 정밀도가 아니라 **재현율**을
# 지키는 항이라 낮추면 ②를 붙인 의미가 없어진다(누락을 찾자고 만든 신호다).
_UNMET_PENALTY = {"must": 12, "should": 3}

# 감점 → 0~1 환산의 반감점. penalty=12(치명 1건)에서 정확히 0.5가 되도록 잡았다.
_PENALTY_HALF = 12.0

# 반증이 **실행되지 않은** 후보의 반증 축 점수 — 중립.
# 왜 만점(1.0)이면 안 되나: 지적 0건은 "반증해 보니 결함이 없었다"와 "반증을 못 했다"에서
# 똑같이 나온다. 만점을 주면 rate limit 하나가 0.4 가중치 보너스 + 게이트 면제를 주는
# **부분 실패 보상**이 된다(실패는 강등이어야 한다).
# 왜 0(최저)도 아닌가: 모름은 유죄가 아니다. LLM 인프라 장애로 후보를 탈락시키면 이번엔
# 반대 방향으로 심판이 왜곡된다.
# 하필 0.5인 근거 둘: (a) 레거시 채점형이 LLM 점수를 못 받았을 때 주는 값과 같다
# (`_judge_by_rubric`의 `qual = ... if ls else 0.5`) — 두 경로가 같은 상황에 같은 값을
# 내야 A/B가 성립한다. (b) `_PENALTY_HALF` 정의상 0.5는 "치명 1건 상당"의 자리다. 즉
# 미실행은 무결 판정보다 항상 불리하고, 확증된 치명 결함을 안은 후보보다는 유리하지 않다.
_NO_REFUTATION_QUAL = 0.5

# 결정론 앵커 : 반증 축 = 6 : 4. 레거시 채점형의 비율(0.6 det + 0.4 LLM 정성)을 **그대로**
# 유지한다 — 이번 변경의 가설은 "LLM 축의 비중이 잘못됐다"가 아니라 "LLM 축이 재는 것이
# 잘못됐다"이다. 비율까지 같이 흔들면 골드셋에서 어느 쪽이 효과였는지 못 가른다.
_DET_WEIGHT = 0.6


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


# ─────────────────────────────────────────────────────────────────────────────
# LLM 출력 스키마
# ─────────────────────────────────────────────────────────────────────────────

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


class _Expectation(BaseModel):
    """맹목 기대 한 건 — 후보를 보지 않고 spec만으로 세운 "있어야 할 단계"."""

    exp_id: str = ""
    text: str = ""
    criticality: Literal["must", "should"] = "must"
    req_ids: list[str] = Field(default_factory=list, description="근거가 된 spec 요구 id")


class _BlindPlan(BaseModel):
    expectations: list[_Expectation] = Field(default_factory=list)


class _Defect(BaseModel):
    """반증자가 찾아낸 실패 이유 한 건."""

    exp_id: str | None = Field(None, description="관련 기대 id (있으면)")
    location: str | None = Field(None, description="아웃라인 노드 id (n3 등)")
    severity: Literal["fatal", "major", "minor"] = "major"
    claim: str = Field("", description="무엇이 실패하는가 — 한 문장")
    trigger: str = Field("", description="어떤 상황에서 실패하는가 — 없으면 결함이 아니다")


class _Refutation(BaseModel):
    defects: list[_Defect] = Field(default_factory=list)
    unmet: list[str] = Field(default_factory=list, description="이 후보가 다루지 않는 기대 id들")
    strengths: list[str] = Field(default_factory=list, description="다른 후보에 이식할 만한 구조 0~2")


class _SlotVote(BaseModel):
    slot_id: str = ""
    upheld: bool = True


class _SlotVotes(BaseModel):
    votes: list[_SlotVote] = Field(default_factory=list)


@dataclass
class _Charge:
    """후보 하나에 제기된 지적 한 건 — 결함이든 기대 미충족이든 여기로 정규화된다.

    점수(감점)와 게이트(자격 박탈)를 같은 목록에서 뽑기 위한 단일 어휘다. `upheld`는
    재심 결과: True=확증, False=기각(감점에서 제외), None=재심 미실시 또는 불능.
    """

    charge_id: str
    candidate_id: str
    kind: Literal["defect", "unmet"]    # 후보가 틀렸다 / 후보에 없다
    claim: str
    penalty: int
    gateable: bool                      # 확증되면 승자 자격을 박탈할 급인가
    critical: bool                      # 3회 다수결로 재심할 "중요 슬롯"인가
    location: str | None = None
    exp_id: str | None = None
    upheld: bool | None = None

    @property
    def counts(self) -> bool:
        """감점에 반영되는가 — 기각(False)된 지적만 빠진다."""
        return self.upheld is not False


# ─────────────────────────────────────────────────────────────────────────────
# 공통 렌더·실행 유틸
# ─────────────────────────────────────────────────────────────────────────────

def _outline_of(flow: dict) -> str:
    return render_outline(annotate_ids(copy.deepcopy(flow)))


def _render_requirements(spec: dict) -> str:
    return "\n".join(
        f"- [{r.get('req_id')}] ({r.get('priority', 'must')}) {r.get('text', '')}"
        for r in spec.get("requirements") or []
    ) or "(요구사항 없음)"


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


def _safe_call(fn, item):
    """항목 하나의 LLM 호출을 예외째 가둔다 — 부분 실패가 심판 전체를 죽이지 않게.

    ValueError(교정 후에도 형식 불일치)·RuntimeError(키·인증·rate limit)를 잡는 범위는
    레거시 심판과 같다. 여기서 죽으면 그 항목만 None이 되고 나머지는 계속 간다.

    **None을 '지적 없음'으로 읽지 마라.** 반증 결과의 None은 "결함이 없다"가 아니라
    "확인하지 못했다"이며, 호출부는 그 둘을 구별해 중립(`_NO_REFUTATION_QUAL`)으로
    처리해야 한다 — 안 그러면 부분 실패가 만점이라는 보상으로 돌아온다.
    """
    try:
        return fn(item)
    except (ValueError, RuntimeError) as e:
        logger.warning("심판 부분 실패 — 해당 호출만 폐기: %s", e)
        return None


def _fanout(fn, items: list) -> list:
    """항목별로 fn을 병렬 실행하고 (결과 | None)을 **입력 순서대로** 돌려준다.

    스레드 상한은 graph와 같은 MAX_LLM_CONCURRENCY다. 심판은 compose·verify가 전부 끝난
    뒤에만 도는 구간이라(graph의 asyncio 세마포어가 비어 있다) 같은 수를 그대로 써도 총
    동시성이 상한을 넘지 않는다. 순차로 돌리면 6~7콜이 그대로 초안 지연이 되므로 접는다.

    ⚠️ **ContextVar를 항목마다 따로 복사해 넘긴다.** 사용량 귀속(user/session/component)은
    `core.llm`의 ContextVar로 전파되는데, `asyncio.to_thread`와 달리 ThreadPoolExecutor는
    호출자 컨텍스트를 옮겨 주지 않는다 — 그냥 submit하면 심판이 태운 5~6콜의 비용이 전부
    귀속 없는 system/other로 기록돼 링 게이지·예산 집계에서 조용히 샌다. 복사본을 **항목당
    하나씩** 뜨는 이유는 같은 Context 객체를 두 스레드가 동시에 `run()` 할 수 없기 때문이다
    (RuntimeError: cannot enter context).
    """
    if not items:
        return []
    if len(items) == 1:
        return [_safe_call(fn, items[0])]
    workers = max(1, min(len(items), config.MAX_LLM_CONCURRENCY or 1))
    ctxs = [contextvars.copy_context() for _ in items]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda pair: pair[0].run(_safe_call, fn, pair[1]), zip(ctxs, items)))


def _refutation_enabled() -> bool:
    """반증 심판 토글 (기본 켜짐). 접근 시점 읽기 — 레지스트리 계약.

    v4 config는 노출 키를 `_KEYS`로 고정해 두는데 그 파일은 다른 항목도 건드리는 중이라,
    여기서는 core 레지스트리를 직접 조회한다(선언 자체는 app/core/config.py REGISTRY).
    """
    return bool(_core_config.get("V4_JUDGE_REFUTATION"))


# ─────────────────────────────────────────────────────────────────────────────
# ② 입력 비대칭 — 후보를 보기 전에 spec+문서만으로 기대 단계를 세운다
# ─────────────────────────────────────────────────────────────────────────────

def _blind_expectations(spec: dict, document: str | None, purpose: str) -> list[_Expectation]:
    """spec(+문서)만으로 "이 업무라면 있어야 할 단계"를 독립 생성한다 (LLM 1콜).

    **후보는 단 한 글자도 싣지 않는다.** 이 함수의 존재 이유가 그것이다 — 후보를 먼저
    보여주면 심판은 후보에 있는 항목만 검증하고(앵커링), 아무도 만들지 않은 단계는
    영영 발견되지 않는다. 실패하면 빈 목록으로 강등하고 반증 단계는 계속 간다
    (기대 없는 반증 = 레거시 대비 손해 없음).
    """
    # 지연 임포트 — spec 모듈은 recommend.stream을 물고 오므로 모듈 최상단에서 당기면
    # 임포트 그래프가 recommend 패키지를 경유하게 된다(순환 위험).
    from .spec import fenced_doc_block

    content = (
        f"[목표]\n{spec.get('goal', '')}\n\n"
        f"[요구사항]\n{_render_requirements(spec)}"
        f"{fenced_doc_block(document)}\n\n"
        f"이 업무를 A360으로 자동화한다면 흐름도에 있어야 할 단계를 최대 {MAX_EXPECTATIONS}개 세우세요."
    )
    try:
        plan = chat_json(
            [{"role": "system", "content": _BLIND_PROMPT}, {"role": "user", "content": content}],
            purpose=purpose, model_cls=_BlindPlan,
        )
    except (ValueError, RuntimeError) as e:
        logger.warning("맹목 기대 생성 실패 — 대조 없이 반증만 수행: %s", e)
        return []

    # 앵커 무결성 (semantic.py와 같은 관례):
    #  - req_ids는 스펙에 실재하는 id만 남긴다(환각 연결이 must 승격을 유발하지 않게).
    #  - criticality는 연결된 요구가 must면 must로 덮는다 — 스펙이 진실 원천이다.
    #  - exp_id는 LLM 값을 믿지 않고 E1..En으로 재부여한다(중복·공백 id가 투표 집계를 깬다).
    prio = {r.get("req_id"): r.get("priority", "must") for r in spec.get("requirements") or []}
    out: list[_Expectation] = []
    seen: set[str] = set()
    for e in plan.expectations:
        text = " ".join((e.text or "").split())
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        req_ids = [rid for rid in e.req_ids if rid in prio]
        crit = "must" if any(prio.get(rid) == "must" for rid in req_ids) else e.criticality
        out.append(_Expectation(
            exp_id=f"E{len(out) + 1}", text=text, criticality=crit, req_ids=req_ids,
        ))
        if len(out) >= MAX_EXPECTATIONS:
            break
    return out


# ─────────────────────────────────────────────────────────────────────────────
# ① 반증 프레이밍 — 후보별로 "실패할 이유"를 찾는다
# ─────────────────────────────────────────────────────────────────────────────

def _render_expectations(expectations: list[_Expectation]) -> str:
    if not expectations:
        return "(기대 목록 생성 실패 — 대조 없이 후보 자체만 반증하세요)"
    return "\n".join(
        f"- [{e.exp_id}] ({e.criticality}) {e.text}" for e in expectations
    )


def _refute(spec: dict, expectations: list[_Expectation], r: CandidateReport, purpose: str) -> _Refutation:
    """후보 하나를 반증한다 (LLM 1콜). 이 후보의 아웃라인만 싣는다 — 다른 후보는 안 보여준다.

    후보를 나란히 놓고 비교시키면 심판이 "둘 중 나은 쪽"을 고르는 상대 평가로 돌아가고,
    둘 다 틀렸을 때 둘 다 통과한다. 반증은 후보 하나를 절대 기준(기대 목록)에 대는 일이다.
    """
    content = (
        f"[목표]\n{spec.get('goal', '')}\n\n"
        f"[요구사항]\n{_render_requirements(spec)}\n\n"
        f"[독립 감사관이 세운 기대 단계 — 이 후보를 보지 않고 만든 목록]\n"
        f"{_render_expectations(expectations)}\n\n"
        f"{_render_candidate(r)}\n\n"
        "이 후보가 실제 운영에서 실패할 이유를 찾으세요."
    )
    ref = chat_json(
        [{"role": "system", "content": _REFUTE_PROMPT}, {"role": "user", "content": content}],
        purpose=purpose, model_cls=_Refutation,
    )
    # 환각 id 차단 — 없는 기대를 미충족으로 찍으면 감점만 발생하고 재심할 근거도 없다.
    valid = {e.exp_id for e in expectations}
    ref.unmet = list(dict.fromkeys(x for x in ref.unmet if x in valid))
    for d in ref.defects:
        if d.exp_id not in valid:
            d.exp_id = None
    return ref


# ─────────────────────────────────────────────────────────────────────────────
# 지적 정규화 + ③ 중요 슬롯 정의
# ─────────────────────────────────────────────────────────────────────────────

def _is_critical(penalty: int, gateable: bool) -> bool:
    """**중요 슬롯**의 정의 — 3회 독립 다수결로 재심할 지적인가.

    계획서가 "중요 슬롯만"이라고 한정한 것을 이 코드에서 이렇게 못 박는다:
      (a) 확증되면 후보를 탈락시키는 지적 = 치명 결함, must 기대 미충족  → 무조건 재심
      (b) 그 외에도 감점이 major 급(4) 이상인 지적                        → 재심
    minor(1)는 순위를 뒤집지 못하므로 재심하지 않는다 — 전량 재심은 비용 3배라 금지다.
    """
    return gateable or penalty >= _DEFECT_PENALTY["major"]


def _build_charges(
    reports: list[CandidateReport],
    refutations: list[_Refutation | None],
    exp_by_id: dict[str, _Expectation],
) -> list[_Charge]:
    """반증 결과를 후보별 지적(_Charge) 목록으로 정규화한다."""
    charges: list[_Charge] = []
    for r, ref in zip(reports, refutations):
        if ref is None:
            continue
        for d in ref.defects:
            claim = " ".join((d.claim or "").split())
            if not claim:
                continue
            # trigger(실패 시나리오)가 없는 지적은 한 급 낮춘다 — 프롬프트가 "trigger를 못
            # 쓰면 결함이 아니라 취향"이라고 못 박고 있으니, 코드도 같은 저울을 든다.
            sev = d.severity
            if not (d.trigger or "").strip() and sev == "fatal":
                sev = "major"
            penalty = _DEFECT_PENALTY.get(sev, _DEFECT_PENALTY["major"])
            gateable = sev == "fatal"
            charges.append(_Charge(
                charge_id=f"C{len(charges) + 1}", candidate_id=r.candidate_id, kind="defect",
                claim=claim + (f" (상황: {d.trigger.strip()})" if (d.trigger or "").strip() else ""),
                penalty=penalty, gateable=gateable,
                critical=_is_critical(penalty, gateable),
                location=d.location, exp_id=d.exp_id,
            ))
        for exp_id in ref.unmet:
            exp = exp_by_id.get(exp_id)
            if exp is None:
                continue
            penalty = _UNMET_PENALTY[exp.criticality]
            gateable = exp.criticality == "must"
            charges.append(_Charge(
                charge_id=f"C{len(charges) + 1}", candidate_id=r.candidate_id, kind="unmet",
                claim=f"기대 단계 미충족 [{exp.exp_id}] {exp.text}",
                penalty=penalty, gateable=gateable,
                critical=_is_critical(penalty, gateable),
                exp_id=exp.exp_id,
            ))
    return charges


def _select_slots(charges: list[_Charge]) -> list[_Charge]:
    """재심 슬롯을 후보 간 **순위 라운드로빈**으로 배정한다 (상한 MAX_CRITICAL_SLOTS).

    무게 단일 정렬(`sorted(key=-penalty)[:N]`)로 자르면 안 되는 이유: fatal도 must 미충족도
    감점이 똑같이 12라 상위권이 전부 동점이고, 파이썬 정렬은 안정 정렬이라 `_build_charges`가
    쌓은 순서 — 즉 `reports` 순서, 그리고 graph의 `_PERSONAS` 고정 순서 — 가 그대로 우선순위가
    된다. 상한을 넘는 라운드에서는 앞쪽 후보의 지적만 재심을 받아 확증→게이트까지 가고, 뒤쪽
    후보의 치명 지적은 `upheld=None`으로 남아 **절대 게이트되지 않는다**. 감점이 포화라 순위
    영향이 작다는 건 위안이 안 된다 — 여기서 갈리는 건 감점이 아니라 후보를 통째로 배제하는
    불연속 결정이고, 페르소나 순서가 고정이라 편향이 라운드마다 같은 방향으로 쌓인다.

    해법은 이 레포에 이미 있다: `knowledge/channels.py`의 `merge_by_rank`가 "각 질의의 1등이
    다른 질의의 2등보다 항상 먼저"를 병합 방식 자체로 보장한다. 같은 꼴로 각 후보의 1순위
    지적을 먼저 다 배정하고 그다음 2순위로 돈다 — 잘리는 자리는 언제나 마지막 순위의 꼬리라,
    지적을 제기당한 후보라면 최소 한 건은 재심을 받는다.

    같은 순위 안의 순서는 감점 큰 것부터(동점이면 후보 목록 순서)로 결정론이다. 여기 남는
    후보 순서 의존은 "같은 순위·같은 무게" 사이의 꼬리 하나뿐이라 체계적 배제로 자라지 않는다.
    """
    by_cand: dict[str, list[_Charge]] = {}
    for c in charges:
        if c.critical:
            by_cand.setdefault(c.candidate_id, []).append(c)
    for cs in by_cand.values():
        cs.sort(key=lambda c: -c.penalty)

    out: list[_Charge] = []
    depth = max((len(cs) for cs in by_cand.values()), default=0)
    for rank in range(depth):
        row = sorted(
            (cs[rank] for cs in by_cand.values() if rank < len(cs)), key=lambda c: -c.penalty
        )
        for c in row:
            out.append(c)
            if len(out) >= MAX_CRITICAL_SLOTS:
                return out
    return out


def _vote_on_slots(
    spec: dict, reports: list[CandidateReport], slots: list[_Charge], purpose: str
) -> None:
    """중요 슬롯을 3회 독립 재심해 다수결로 확증/기각한다 (LLM VOTE_ROUNDS콜, 제자리 갱신).

    콜 수를 **슬롯 수와 무관하게 3으로 고정**하려고 전 슬롯을 한 프롬프트에 배치로 싣는다.
    슬롯마다 3콜이면 상한 8슬롯에서 24콜이 되어 비용 계산이 무너진다.

    "독립"의 실현: 모델이 고정이므로 (a) core.llm.chat이 temperature를 지정하지 않아
    공급자 기본 샘플링(비결정론)이 걸리고, (b) 라운드마다 슬롯 **순서를 회전**시킨다.
    같은 순서로 세 번 물으면 위치 편향(앞쪽 지적에 후한/박한 경향)이 세 표에 똑같이 실려
    다수결이 1표와 다를 게 없어진다.

    집계 규칙:
      - 한 라운드가 같은 슬롯을 두 번 찍어도 1표만 센다.
      - 확증 = 던져진 표의 과반이 upheld. 기각되면 감점에서 통째로 빠진다(환각 결함 제거).
      - 세 라운드가 전부 실패해 표가 0장이면 `upheld=None` — 감점에는 원래 무게로 남기되
        **게이트 승격은 하지 않는다**. 검증되지 않은 치명 주장으로 후보를 탈락시키지 않는
        쪽이 부분 실패 격리의 방향이다.
    """
    by_id = {r.candidate_id: r for r in reports}
    involved = sorted({s.candidate_id for s in slots})
    outlines = "\n\n".join(
        f"### 후보 {cid} 아웃라인\n{_outline_of(by_id[cid].flow)}" for cid in involved if cid in by_id
    )

    def _one_round(round_no: int) -> _SlotVotes:
        rotated = slots[round_no:] + slots[:round_no]  # 위치 편향 상쇄
        listing = "\n".join(
            f"- {s.charge_id} · 후보 {s.candidate_id}"
            + (f" · 위치 {s.location}" if s.location else "")
            + f": {s.claim}"
            for s in rotated
        )
        return chat_json(
            [
                {"role": "system", "content": _VOTE_PROMPT},
                {"role": "user", "content": (
                    f"[목표]\n{spec.get('goal', '')}\n\n"
                    f"[요구사항]\n{_render_requirements(spec)}\n\n{outlines}\n\n"
                    f"[재심할 지적]\n{listing}\n\n"
                    "각 지적이 해당 후보 아웃라인에서 실제로 성립하는지 판정하세요."
                )},
            ],
            purpose=purpose, model_cls=_SlotVotes,
        )

    rounds = _fanout(_one_round, list(range(VOTE_ROUNDS)))
    tally: dict[str, list[int]] = {}  # slot_id -> [던진 표, upheld 표]
    for rd in rounds:
        if rd is None:
            continue
        seen: set[str] = set()
        for v in rd.votes:
            if not v.slot_id or v.slot_id in seen:
                continue
            seen.add(v.slot_id)
            cell = tally.setdefault(v.slot_id, [0, 0])
            cell[0] += 1
            cell[1] += 1 if v.upheld else 0
    for s in slots:
        cast, up = tally.get(s.charge_id, [0, 0])
        s.upheld = (up * 2 > cast) if cast else None


# ─────────────────────────────────────────────────────────────────────────────
# 이식 지시
# ─────────────────────────────────────────────────────────────────────────────

def _donor_for(exp_id: str, winner_id: str, unmet_by_cand: dict[str, set[str]]) -> str | None:
    """이 기대를 실제로 다루고 있는 다른 후보 — 이식 지시에 "어디서 베낄지"를 적기 위해."""
    for cid, unmet in unmet_by_cand.items():
        if cid != winner_id and exp_id not in unmet:
            return cid
    return None


def _transplant_findings(
    winner: CandidateReport,
    charges_by_cand: dict[str, list[_Charge]],
    refutations_by_cand: dict[str, _Refutation | None],
    exp_by_id: dict[str, _Expectation],
) -> list[Finding]:
    """승자에게 내릴 개선 지시 — 살아남은 지적 + 패자 장점. LLM 추가 호출 없이 만든다.

    레거시는 이식 지시를 심판 LLM이 문장으로 냈지만, 반증 경로는 이미 "무엇이 비었고 무엇이
    틀렸는지"를 구조화해 갖고 있다. 한 번 더 물을 이유가 없다(콜 수를 6~7에 묶는 자리이기도).

    **기각(upheld=False)되지 않은 지적이면 다 내려보낸다** — 게이트와 저울이 다르다. 게이트는
    후보를 탈락시키므로 3표 확증을 요구하지만, 여기는 refine에게 "여기를 보라"고 말할 뿐이라
    틀린 지시의 비용이 낮다(회귀 가드가 나쁜 패치를 어차피 되돌린다). 승자가 게이트를 통과했다는
    것과 승자가 무결하다는 것은 다른 말이고, 그 차이를 메우는 게 이 목록이다.
    """
    unmet_by_cand = {
        cid: {c.exp_id for c in cs if c.kind == "unmet" and c.exp_id and c.counts}
        for cid, cs in charges_by_cand.items()
    }
    out: list[Finding] = []
    # (a) 승자에게 남은 지적 — 무거운 것부터. minor 결함은 잡음이라 싣지 않는다.
    #     refine의 회귀 가드 비교축(정적 위반 + 결정론 누락)에는 안 들어가므로 여기 심각도가
    #     가드를 왜곡하지 않는다 — surgeon 프롬프트의 처리 순서만 앞당긴다.
    mine = sorted(
        (c for c in charges_by_cand.get(winner.candidate_id, []) if c.counts),
        key=lambda c: -c.penalty,
    )
    for c in mine:
        if c.penalty < _DEFECT_PENALTY["major"] and c.kind == "defect":
            continue
        if c.kind == "unmet":
            exp = exp_by_id.get(c.exp_id or "")
            hint = exp.text if exp else c.claim
            donor = _donor_for(c.exp_id or "", winner.candidate_id, unmet_by_cand)
            if donor:
                hint += f" — 후보 {donor}가 이 단계를 갖고 있으니 그 구조를 참고해 삽입하라"
            severity = "major" if c.gateable else "minor"
            message = f"반증 대조 누락: {c.claim}"
        else:
            hint, severity, message = c.claim, "major", f"반증 지적: {c.claim}"
        out.append(Finding(
            layer="judge", severity=severity, location=c.location,
            message=message, fix_hint=hint,
        ))
    # (b) 패자 장점 이식 — 레거시와 같은 severity(minor)·같은 문구 규약을 유지한다.
    for cid, ref in refutations_by_cand.items():
        if cid == winner.candidate_id or ref is None:
            continue
        for s in ref.strengths[:2]:
            s = " ".join((s or "").split())
            if s:
                out.append(Finding(
                    layer="judge", severity="minor",
                    message=f"이식 지시: 후보 {cid}의 {s}", fix_hint=s,
                ))
    return out[:5]


# ─────────────────────────────────────────────────────────────────────────────
# 판정 본체
# ─────────────────────────────────────────────────────────────────────────────

def _pick_eligible(
    reports: list[CandidateReport], refuted: dict[str, bool]
) -> list[CandidateReport]:
    """승자 자격이 있는 후보 — 게이트를 두 겹으로 완화하며 내려간다.

    ① L2 하드 게이트(must missing)도 없고 확증된 치명 결함도 없는 후보
    ② 없으면 L2 게이트만 통과한 후보 (반증 게이트는 포기 — 레거시와 같은 자격 기준)
    ③ 그것도 없으면 전원 (최악 중 최선)
    반증 게이트를 L2 게이트보다 **먼저 포기**하는 이유: L2는 결정론 신호고 반증은 LLM
    판정이다. 둘 다 못 만족시킬 때 믿을 것은 결정론 쪽이다.
    """
    clean = [r for r in reports if not r.gate_failures and not refuted.get(r.candidate_id)]
    if clean:
        return clean
    gate_ok = [r for r in reports if not r.gate_failures]
    return gate_ok or list(reports)


def _refutation_reason(win_row: dict, rows: list[dict], eligible: set[str]) -> str:
    """사용자에게 그대로 보이는 선정 이유 — 두 문장 이내 (프롬프트 규약과 같은 계약).

    **실제로 자격에서 빠진 후보만** 그렇게 말한다. 전원이 게이트에 걸려 자격이 완화된
    라운드에서 "다른 후보는 제외됐다"고 쓰면 사용자에게 거짓말이 된다 — 승자도 같은
    결함을 안고 있는 상황이기 때문이다.

    같은 이유로 **반증이 실제로 돌았을 때만** 결함 주장을 한다. 지적 0건은 "반증해 보니
    무결"과 "반증을 못 함"에서 똑같이 나오므로, 호출이 죽은 라운드에 "결함이 나오지
    않았습니다"라고 쓰면 하지도 않은 검사를 통과했다고 말하는 셈이다(모름 → 침묵 원칙 위반).
    건수 비교("가장 가볍다")는 별도 가드가 필요 없다 — 미실행 후보의 건수는 0이라 승자가
    그보다 적거나 같으려면 승자도 0이어야 하고, 그러면 위 분기가 먼저 잡는다.
    """
    wid = win_row["candidate_id"]
    others = [r for r in rows if r["candidate_id"] != wid]
    excluded = [r["candidate_id"] for r in others if r["candidate_id"] not in eligible]
    if not win_row.get("refute_ran", True):
        head = (
            f"후보 {wid}는 반증 검사가 실패해 결함 여부를 확인하지 못했고, "
            f"요구 커버리지·검수·시뮬레이션 등 결정론 신호 종합 {win_row['total']}점으로 앞섭니다."
        )
    elif win_row["charges"] == 0:
        head = f"후보 {wid}는 반증에서 성립하는 결함이 나오지 않았습니다."
    elif all(win_row["charges"] <= r["charges"] for r in others):
        head = (
            f"후보 {wid}는 반증 지적 {win_row['charges']}건으로 가장 가볍고, "
            f"결정론 신호까지 합친 종합 {win_row['total']}점으로 앞섭니다."
        )
    else:
        head = (
            f"후보 {wid}는 반증 지적 {win_row['charges']}건을 안고도 요구 커버리지·검수·"
            f"시뮬레이션 우위로 종합 {win_row['total']}점을 냈습니다."
        )
    if excluded:
        head += f" 후보 {', '.join(excluded)}는 확증된 치명 결함 또는 요구 누락으로 자격에서 제외됐습니다."
    return head


def _judge_by_refutation(
    spec: dict, reports: list[CandidateReport], *, purpose: str, document: str | None
) -> dict:
    """반증 심판 본체 — ② 맹목 기대 → ① 후보별 반증 → ③ 중요 슬롯 다수결 → 점수·게이트."""
    expectations = _blind_expectations(spec, document, purpose)          # 1콜
    exp_by_id = {e.exp_id: e for e in expectations}

    refutations = _fanout(                                               # N콜
        lambda r: _refute(spec, expectations, r, purpose), list(reports)
    )
    refutations_by_cand = {r.candidate_id: ref for r, ref in zip(reports, refutations)}
    if all(ref is None for ref in refutations):
        # 전원이 중립(0.5)을 받아 반증 축이 상수가 된다 = 사실상 결정론 단독 판정.
        logger.warning("전 후보 반증 실패 — 반증 축을 중립으로 두고 결정론 점수만으로 선정")

    charges = _build_charges(reports, refutations, exp_by_id)
    # 후보 간 순위 라운드로빈으로 배정한다 — 상한을 넘어 탈락한 지적은 재심 없이 감점만
    # 유지된다(게이트로는 못 오른다). 왜 무게 단일 정렬이 아닌지는 `_select_slots` 참조.
    slots = _select_slots(charges)
    if slots and VOTE_ROUNDS > 1:
        _vote_on_slots(spec, reports, slots, purpose)                    # 3콜

    charges_by_cand: dict[str, list[_Charge]] = {r.candidate_id: [] for r in reports}
    for c in charges:
        charges_by_cand[c.candidate_id].append(c)

    rows: list[dict] = []
    refuted: dict[str, bool] = {}
    for r in reports:
        det = r.deterministic_score()
        live = [c for c in charges_by_cand[r.candidate_id] if c.counts]
        penalty = sum(c.penalty for c in live)
        # 반증 콜이 죽은 후보는 지적이 0건이지만 그건 "결함이 없다"가 아니라 "안 재 봤다"다.
        # 감점 환산에 그대로 태우면 만점이 나와 실패가 보상이 되므로 중립으로 끊는다.
        ran = refutations_by_cand.get(r.candidate_id) is not None
        refute_factor = (
            round(1.0 / (1.0 + penalty / _PENALTY_HALF), 3) if ran else _NO_REFUTATION_QUAL
        )
        refuted[r.candidate_id] = any(c.gateable and c.upheld is True for c in live)
        top = max(live, key=lambda c: c.penalty, default=None)
        if not ran:
            # 검사를 못 했으면서 "지적 없음"이라고 쓰면 심판 신뢰도를 과대 표시한다(모름→침묵).
            note = "반증 미실행(호출 실패) — 결함 여부 미확인"
        else:
            note = top.claim if top else "반증에서 지적 없음"
        rows.append({
            "candidate_id": r.candidate_id, "persona": r.persona,
            "deterministic": det, "qualitative": refute_factor,
            "total": round(_DET_WEIGHT * det + (1 - _DET_WEIGHT) * refute_factor, 3),
            "gate_failed": bool(r.gate_failures),
            "note": note,
            # 반증 경로에서만 붙는 추가 키 — 기존 키는 하나도 빼지 않는다(프레임 계약 보존).
            "charges": len(live),
            "refuted": refuted[r.candidate_id],
            # 지적 0건의 두 의미(무결/미검사)를 구별하는 유일한 신호 — 문구 생성이 이걸 읽는다.
            "refute_ran": ran,
        })

    by_id = {r.candidate_id: r for r in reports}
    eligible = {r.candidate_id for r in _pick_eligible(reports, refuted)}
    win_row = max((row for row in rows if row["candidate_id"] in eligible), key=lambda row: row["total"])
    winner = by_id[win_row["candidate_id"]]

    return {
        "winner": winner,
        "verdict": {
            "winner": winner.candidate_id,
            "reason": _refutation_reason(win_row, rows, eligible),
            "scores": rows,
        },
        "transplant_findings": _transplant_findings(
            winner, charges_by_cand, refutations_by_cand, exp_by_id
        ),
    }


def _judge_by_rubric(spec: dict, reports: list[CandidateReport], *, purpose: str) -> dict:
    """레거시 채점형 심판 (LLM 1콜) — `V4_JUDGE_REFUTATION=false`일 때의 경로.

    v3와 동일한 구현을 그대로 남긴다. 반증형과의 A/B가 성립하려면 off 쪽이 **기존 그대로**
    여야 하기 때문이다(비교 기준선을 같이 손대면 무엇이 효과였는지 못 가른다).
    """
    llm_out: _JudgeOutput | None = None
    try:
        llm_out = chat_json(
            [
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": (
                    f"[목표]\n{spec.get('goal', '')}\n\n[요구사항]\n{_render_requirements(spec)}\n\n"
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

    # 이식 지시는 LLM이 지목한 승자 기준으로 쓰였다 — 하드 게이트로 실제 승자가 달라졌으면
    # 좌표·전제가 안 맞는 지시이므로 폐기한다 (엉뚱한 트리에 수술하는 것보다 무이식이 낫다).
    llm_winner_matches = bool(llm_out) and llm_out.winner == winner.candidate_id
    transplant_findings = [
        Finding(layer="judge", severity="minor", location=t.to_location or None,
                message=f"이식 지시: {t.instruction}", fix_hint=t.instruction)
        for t in (llm_out.transplants if llm_winner_matches else [])
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


def judge_candidates(
    spec: dict,
    reports: list[CandidateReport],
    *,
    purpose: str = "turn_generate",
    document: str | None = None,
) -> dict:
    """후보들을 심판해 승자와 이식 지시를 정한다.

    기본 경로는 **반증 심판**(모듈 docstring 참조): 후보를 보기 전 spec+문서만으로 기대
    단계를 세우고(②), 후보별로 실패할 이유를 찾고(①), 판정을 뒤집을 무거운 지적만 3회
    다수결로 재심한다(③). 최종 순위 = 결정론 앵커 60% + 반증 축 40%, 확증된 치명 결함은
    별도 게이트. `V4_JUDGE_REFUTATION=false`면 레거시 채점형(LLM 1콜)으로 돌아간다.

    LLM이 어느 단계에서 죽어도 심판 전체는 살아남는다 — 맹목 기대 실패는 대조 없는 반증으로,
    후보 하나의 반증 실패는 그 후보만 **중립 점수**(`_NO_REFUTATION_QUAL`)로, 재심 전패는
    게이트 미승격으로 강등된다. 어느 강등도 후보에게 이득이 되지 않는다 — 실패가 보상이면
    불안정한 라운드일수록 심판이 무력해진다.

    `document`는 ②의 기대 생성(맹목 단계)에만 실린다 — 반증·재심 프롬프트는 원문을 보지
    않는다(후보와 원문을 같이 보면 앵커링이 돌아온다). 기본값 None이라 안 넘겨도 동작하지만,
    그러면 기대가 spec 요약만 보고 세워져 ②의 절반만 켜진 상태다. 호출부(recommend/graph.py)가
    원문을 넘겨야 문서 기반 누락 탐지가 완성된다.

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
    if not _refutation_enabled():
        return _judge_by_rubric(spec, reports, purpose=purpose)
    return _judge_by_refutation(spec, reports, purpose=purpose, document=document)


# ─────────────────────────────────────────────────────────────────────────────
# 평가 하네스용 공개 진입점 (측정 계층 L3 — 설계 §4 측정 계층 표)
# ─────────────────────────────────────────────────────────────────────────────
#
# 왜 여기 두나: 골드셋 하네스가 `_blind_expectations`/`_refute`를 직접 부르면 심판의
# **정의가 두 곳으로 갈린다** — 파이프라인이 쓰는 기대와 채점이 쓰는 기대가 달라지면
# "심판 점수가 올랐다"가 무엇을 뜻하는지 알 수 없게 된다. 얇은 공개 래퍼로 묶어
# 두 소비처가 같은 함수를 지나게 한다.
#
# ⚠️ 이 축은 **정답 봇과 무관하다.** spec+문서만 보고 세운 기대에 흐름도를 대는 것이라,
#    골드셋 재현율(L2)이 "정답 봇을 얼마나 베꼈나"를 잰다면 이쪽은 "이 업무를 실제로
#    돌릴 수 있나"를 잰다. 둘이 갈리는 지점이 곧 정답 봇이 문서 없이 채운 구현량이다.

def blind_expectations(spec: dict, document: str | None = None,
                       purpose: str = "eval_judge") -> list[dict]:
    """흐름도를 **보지 않고** spec(+문서)만으로 "있어야 할 단계"를 세운다 (LLM 1콜).

    입력 비대칭(설계 Phase 4 ②)의 채점판. 실패하면 빈 목록 — 호출부가 축을 건너뛴다.
    """
    return [e.model_dump() for e in _blind_expectations(spec, document, purpose)]


def refute_flow(spec: dict, expectations: list[dict], flow: dict,
                violations: list[dict] | None = None,
                purpose: str = "eval_judge") -> dict:
    """흐름도 하나를 기대 목록에 대고 반증한다 (LLM 1콜).

    반환: {"defects": [...], "unmet": [exp_id], "strengths": [...]}.
    LLM 실패는 예외로 올린다 — 채점 축은 조용히 0점을 내면 안 된다(그건 '나쁜 흐름도'와
    구별이 안 된다). 호출부가 잡아서 '측정 실패'로 남긴다.
    """
    exps = [_Expectation.model_validate(e) for e in expectations]
    report = CandidateReport(candidate_id="eval", flow=flow, violations=violations or [])
    return _refute(spec, exps, report, purpose).model_dump()


def score_expectations(expectations: list[dict], refutation: dict) -> dict:
    """반증 결과를 0~1 점수로 환산한다 — 결정론(LLM 없음).

    두 축을 따로 낸다. 합치면 "기대를 다 다뤘지만 전부 깨진다"와 "절반만 다뤘지만
    다 돌아간다"가 같은 점수가 되는데, 그 둘은 고칠 방법이 정반대다.

      met_rate     — must 기대 중 unmet이 아닌 비율 = **완성도**
      soundness    — 치명/중대 결함의 무게를 감쇠한 값 = **견고성**
                     (`1/(1+penalty/6)` — 결함 0이면 1.0, 6이면 0.5. `_PENALTY_HALF`와
                      같은 꼴이지만 후보 비교가 아니라 절대 점수라 상수를 따로 둔다.)
    """
    musts = [e for e in expectations if e.get("criticality") == "must"]
    unmet = set(refutation.get("unmet") or [])
    met = [e for e in musts if e.get("exp_id") not in unmet]
    met_rate = round(len(met) / len(musts), 3) if musts else None

    penalty = sum(
        {"fatal": 4, "major": 2}.get(d.get("severity"), 0)
        for d in refutation.get("defects") or []
    )
    return {
        "met_rate": met_rate,
        "soundness": round(1.0 / (1.0 + penalty / 6.0), 3),
        "n_expected_must": len(musts),
        "n_unmet_must": len(musts) - len(met),
        "n_fatal": sum(1 for d in refutation.get("defects") or [] if d.get("severity") == "fatal"),
        "n_major": sum(1 for d in refutation.get("defects") or [] if d.get("severity") == "major"),
    }
