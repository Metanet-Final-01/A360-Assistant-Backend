"""v4 반증 심판 가드 — 상관 오류 대응 3종(반증 프레이밍·입력 비대칭·중요 슬롯 다수결).

LLM은 한 번도 실제로 부르지 않는다. judge 모듈의 `chat_json`을 프롬프트별 결정론 대역으로
갈아끼우고 **무엇을 보여줬는지·몇 번 불렀는지·어떻게 갈랐는지**만 본다. 품질 측정은 이
파일의 관심사가 아니다(그건 골드셋의 일이다).

이 파일이 막는 설계 위반:
  - 심판이 후보를 **미리 보는 것** — 앵커링이 돌아오면 ②를 붙인 의미가 사라진다.
  - 다수결이 **소수 의견을 채택**하는 것 — 2:1이 뒤집히면 ③은 비용만 3배인 장식이 된다.
  - 검증 안 된 치명 주장으로 후보를 **탈락**시키는 것 — 게이트는 확증된 것에만 걸려야 한다.
  - 부분 실패가 **심판 전체를 죽이는** 것 — 초안 구간이라 실패는 강등이어야 한다.
  - 호출 수가 **조용히 늘어나는** 것 — 1콜에서 1+N+3콜로 올린 변경이라 상한이 계약이다.
  - `judge_candidates` 시그니처·반환 형태가 바뀌는 것 — recommend/graph.py가 부른다.
"""

import inspect
import re
import threading

import pytest

from app.agent.v4.orchestrator import judge as judge_mod
from app.agent.v4.orchestrator.judge import (
    CandidateReport,
    _BlindPlan,
    _Defect,
    _Expectation,
    _Refutation,
    _SlotVote,
    _SlotVotes,
    judge_candidates,
)

_SPEC = {
    "goal": "매일 아침 엑셀 실적 파일을 정리해 결과 폴더에 저장한다",
    "requirements": [
        {"req_id": "req-1", "priority": "must", "text": "대상 엑셀 파일을 읽는다"},
        {"req_id": "req-2", "priority": "should", "text": "처리 로그를 남긴다"},
    ],
}

# 후보 흐름도 라벨에 심는 표식 — 맹목 단계 프롬프트에 이게 새어 들어가면 ②가 깨진 것이다.
MARKER = "ZZ_CANDIDATE_ONLY_MARKER"


def _rep(cid, *, cov=1.0, sim=1.0, gate=(), persona="모범 사례") -> CandidateReport:
    return CandidateReport(
        candidate_id=cid,
        persona=persona,
        flow={"steps": [{
            "step_id": "step-1", "label": f"{MARKER}-{cid}",
            "actions": [{
                "package": "Excel advanced", "action": "Open",
                "label": f"{MARKER}-{cid}-열기", "parameters": [], "children": [],
            }],
        }]},
        must_coverage=cov, sim_pass_rate=sim, gate_failures=list(gate),
    )


class _LLMStub:
    """프롬프트별 결정론 대역. 호출 내용을 순서·스레드 안전하게 기록한다.

    `_fanout`이 스레드로 팬아웃하므로 기록은 락으로 감싼다. 값에 Exception을 넣어 두면
    그 단계만 실패시킨다(부분 실패 격리 검증용).
    """

    def __init__(self, *, blind=None, refute=None, votes=None):
        self.blind = blind
        self.refute = refute or {}
        self.votes = list(votes or [])
        self.calls: list[tuple[str, str]] = []  # (stage, user_content)
        self._lock = threading.Lock()
        self._vote_idx = 0

    def stages(self) -> list[str]:
        return [s for s, _ in self.calls]

    def count(self, stage: str) -> int:
        return sum(1 for s in self.stages() if s == stage)

    def user_of(self, stage: str) -> list[str]:
        return [u for s, u in self.calls if s == stage]

    def __call__(self, messages, *, purpose, model_cls):
        system, user = messages[0]["content"], messages[1]["content"]
        stage = {
            judge_mod._BLIND_PROMPT: "blind",
            judge_mod._REFUTE_PROMPT: "refute",
            judge_mod._VOTE_PROMPT: "vote",
            judge_mod._PROMPT: "rubric",
        }[system]
        with self._lock:
            self.calls.append((stage, user))
            if stage == "vote":
                payload = self.votes[self._vote_idx % len(self.votes)] if self.votes else _SlotVotes()
                self._vote_idx += 1
            elif stage == "refute":
                cid = re.search(r"### 후보 (\S+)", user).group(1)
                payload = self.refute.get(cid, _Refutation())
            elif stage == "blind":
                payload = self.blind if self.blind is not None else _BlindPlan()
            else:
                payload = None
        if isinstance(payload, Exception):
            raise payload
        return payload


@pytest.fixture(autouse=True)
def _refutation_on(monkeypatch):
    """토글은 접근 시점에 읽히므로 테스트마다 명시로 켠다 — 로컬 .env에 좌우되지 않게."""
    monkeypatch.setenv("V4_JUDGE_REFUTATION", "true")


def _install(monkeypatch, stub: _LLMStub) -> _LLMStub:
    monkeypatch.setattr(judge_mod, "chat_json", stub)
    return stub


def _plan(*items) -> _BlindPlan:
    return _BlindPlan(expectations=[
        _Expectation(exp_id=f"E{i + 1}", text=t, criticality=c)
        for i, (t, c) in enumerate(items)
    ])


def _fatal(claim="폴더를 만들지 않고 저장해 첫 실행에서 중단", trigger="결과 폴더가 없는 최초 실행", **kw):
    return _Defect(severity="fatal", claim=claim, trigger=trigger, **kw)


# ─────────────────────────────────────────────────────────────────────────────
# ① 반증 프레이밍
# ─────────────────────────────────────────────────────────────────────────────

def test_refutation_framing_reaches_the_prompt():
    """심판 프롬프트가 '채점'이 아니라 '반증'을 시킨다 — 프레이밍이 파일에 실제로 있는가.

    ①의 전부는 과업 문장이다. 코드가 반증 스키마를 받아도 프롬프트가 "점수를 매겨라"면
    상관 오류는 그대로다. 문구가 조용히 채점형으로 되돌아가는 것을 여기서 막는다.
    """
    p = judge_mod._REFUTE_PROMPT
    assert "반증자" in p and "채점관이 아닙니다" in p
    assert "실패할 이유를 찾아내는 것" in p
    assert "불확실하면 결함 쪽으로 기울이세요" in p  # 불확실 → 결함 쪽 기울기
    assert "trigger" in p  # 근거 없는 의심은 못 적게 하는 반대편 저울


def test_refutation_prompt_gets_expectations_and_only_its_own_candidate(monkeypatch):
    """반증 콜은 '독립 기대 목록 + 후보 하나'만 본다 — 후보를 나란히 놓으면 상대평가가 된다."""
    stub = _install(monkeypatch, _LLMStub(
        blind=_plan(("결과 폴더가 없으면 만든다", "must")),
        refute={"A": _Refutation(), "B": _Refutation()},
    ))
    judge_candidates(_SPEC, [_rep("A"), _rep("B")])

    users = stub.user_of("refute")
    assert len(users) == 2
    for u in users:
        assert "결과 폴더가 없으면 만든다" in u          # 맹목 기대가 대조 재료로 실린다
        assert len(re.findall(r"### 후보 ", u)) == 1     # 다른 후보는 안 보여준다


# ─────────────────────────────────────────────────────────────────────────────
# ② 입력 비대칭 — 맹목 생성
# ─────────────────────────────────────────────────────────────────────────────

def test_blind_stage_never_sees_any_candidate(monkeypatch):
    """맹목 기대 생성 프롬프트에 후보가 **한 글자도** 안 실린다 (②의 핵심 계약).

    후보를 먼저 보여주면 심판은 후보에 있는 것만 검증한다(앵커링). 그러면 아무도 안 만든
    단계는 영영 안 잡히고, 지금 정밀도/재현율이 갈린 자리가 그대로 남는다.
    """
    stub = _install(monkeypatch, _LLMStub(blind=_plan(("파일을 연다", "must"))))
    judge_candidates(_SPEC, [_rep("A"), _rep("B", persona="운영 안전")])

    (blind_user,) = stub.user_of("blind")
    assert MARKER not in blind_user            # 후보 아웃라인 라벨
    assert "### 후보" not in blind_user         # 후보 블록 자체
    assert "Excel advanced" not in blind_user   # 후보가 고른 액션
    assert "운영 안전" not in blind_user         # 페르소나
    assert _SPEC["goal"] in blind_user          # spec은 실려야 한다
    assert "req-1" in blind_user


def test_blind_stage_runs_before_any_refutation(monkeypatch):
    """맹목 생성이 반증보다 **먼저** 일어난다 — 순서가 뒤집히면 비대칭이 무의미하다."""
    stub = _install(monkeypatch, _LLMStub(blind=_plan(("파일을 연다", "must"))))
    judge_candidates(_SPEC, [_rep("A"), _rep("B")])
    assert stub.stages()[0] == "blind"


def test_blind_expectations_are_reanchored_to_spec(monkeypatch):
    """맹목 기대의 id·criticality를 코드가 다시 도장 찍는다 — 스펙이 진실 원천.

    LLM이 매긴 id를 그대로 믿으면 중복/공백 id가 다수결 집계를 깨고, must 요구에 연결된
    기대를 should로 내면 게이트가 새어 나간다.
    """
    stub = _install(monkeypatch, _LLMStub(
        blind=_BlindPlan(expectations=[
            _Expectation(exp_id="", text="대상 엑셀 파일을 읽는다", criticality="should", req_ids=["req-1", "없는id"]),
            _Expectation(exp_id="E1", text="대상 엑셀 파일을 읽는다"),  # 중복 텍스트
            _Expectation(exp_id="X", text="로그를 남긴다", criticality="should", req_ids=["req-2"]),
        ]),
        refute={"A": _Refutation(), "B": _Refutation()},
    ))
    judge_candidates(_SPEC, [_rep("A"), _rep("B")])

    listing = stub.user_of("refute")[0]
    assert "[E1] (must) 대상 엑셀 파일을 읽는다" in listing   # req-1이 must라 must로 승격
    assert "[E2] (should) 로그를 남긴다" in listing           # should 요구는 should 유지
    assert "E3" not in listing                               # 중복 텍스트는 접힌다


# ─────────────────────────────────────────────────────────────────────────────
# ③ 중요 슬롯 3회 독립 다수결
# ─────────────────────────────────────────────────────────────────────────────

def test_majority_upholds_two_to_one_and_dismisses_one_to_two(monkeypatch):
    """2:1은 채택, 1:2는 기각 — 다수결이 실제로 갈리는가.

    ③이 없으면 치명 결함 1표가 곧바로 후보를 탈락시킨다. 여기가 뒤집히면 비용만 3배인
    장식이 된다.
    """
    stub = _install(monkeypatch, _LLMStub(
        blind=_plan(("결과 폴더가 없으면 만든다", "must")),
        refute={
            "A": _Refutation(defects=[
                _fatal(claim="결과 폴더 미생성"),                       # C1
                _fatal(claim="세션을 닫지 않음", trigger="오류 발생 시"),  # C2
            ]),
            "B": _Refutation(),
        },
        votes=[
            _SlotVotes(votes=[_SlotVote(slot_id="C1", upheld=True), _SlotVote(slot_id="C2", upheld=False)]),
            _SlotVotes(votes=[_SlotVote(slot_id="C1", upheld=True), _SlotVote(slot_id="C2", upheld=False)]),
            _SlotVotes(votes=[_SlotVote(slot_id="C1", upheld=False), _SlotVote(slot_id="C2", upheld=True)]),
        ],
    ))
    out = judge_candidates(_SPEC, [_rep("A"), _rep("B")])

    assert stub.count("vote") == judge_mod.VOTE_ROUNDS
    row_a = next(r for r in out["verdict"]["scores"] if r["candidate_id"] == "A")
    assert row_a["charges"] == 1        # C1만 살아남는다 (C2는 1:2로 기각)
    assert row_a["refuted"] is True     # 확증된 치명 결함 → 게이트
    assert out["winner"].candidate_id == "B"


def test_duplicate_votes_in_one_round_count_once(monkeypatch):
    """한 라운드가 같은 슬롯을 두 번 찍어도 1표 — 한 판정관이 표를 늘릴 수 없다."""
    stub = _install(monkeypatch, _LLMStub(
        blind=_plan(("폴더를 만든다", "must")),
        refute={"A": _Refutation(defects=[_fatal()]), "B": _Refutation()},
        votes=[
            # 한 라운드가 C1을 세 번 upheld=True로 찍는다 — 그래도 1표여야 한다.
            _SlotVotes(votes=[_SlotVote(slot_id="C1", upheld=True)] * 3),
            _SlotVotes(votes=[_SlotVote(slot_id="C1", upheld=False)]),
            _SlotVotes(votes=[_SlotVote(slot_id="C1", upheld=False)]),
        ],
    ))
    out = judge_candidates(_SPEC, [_rep("A"), _rep("B")])
    row_a = next(r for r in out["verdict"]["scores"] if r["candidate_id"] == "A")
    assert row_a["charges"] == 0 and row_a["refuted"] is False  # 1:2로 기각


def test_vote_rounds_see_rotated_slot_order(monkeypatch):
    """라운드마다 슬롯 순서를 회전시킨다 — 같은 순서 3번은 위치 편향이 세 표에 똑같이 실린다."""
    stub = _install(monkeypatch, _LLMStub(
        blind=_plan(("폴더를 만든다", "must")),
        refute={
            "A": _Refutation(defects=[_fatal(claim="첫째"), _fatal(claim="둘째", trigger="t")]),
            "B": _Refutation(),
        },
    ))
    judge_candidates(_SPEC, [_rep("A"), _rep("B")])
    firsts = {
        re.search(r"\[재심할 지적\]\n- (C\d+)", u).group(1) for u in stub.user_of("vote")
    }
    assert len(firsts) > 1  # 세 라운드가 같은 지적으로 시작하지 않는다


def test_minor_defects_do_not_trigger_voting(monkeypatch):
    """중요 슬롯이 없으면 다수결 3콜을 통째로 건너뛴다 — '전량 3회 금지'의 이행 지점."""
    stub = _install(monkeypatch, _LLMStub(
        blind=_plan(("로그를 남긴다", "should")),
        refute={
            "A": _Refutation(defects=[_Defect(severity="minor", claim="하드코딩 경로", trigger="이관 시")]),
            "B": _Refutation(),
        },
    ))
    judge_candidates(_SPEC, [_rep("A"), _rep("B")])
    assert stub.count("vote") == 0


def test_unmet_must_expectation_is_always_reviewed(monkeypatch):
    """must 기대 미충족은 반드시 재심을 거친다 — 맹목 생성의 **환각 기대**를 거르는 유일한 관문.

    ②는 앵커링을 없애는 대신 "문서에 없는 단계"를 지어낼 위험을 새로 만든다. 그 기대로
    후보를 탈락시키기 전에 3표를 받게 하는 것이 ③을 여기에 건 이유다.
    """
    stub = _install(monkeypatch, _LLMStub(
        blind=_plan(("결과 폴더가 없으면 만든다", "must")),
        refute={"A": _Refutation(unmet=["E1"]), "B": _Refutation()},
        votes=[_SlotVotes(votes=[_SlotVote(slot_id="C1", upheld=False)])],
    ))
    out = judge_candidates(_SPEC, [_rep("A"), _rep("B")])
    assert stub.count("vote") == judge_mod.VOTE_ROUNDS
    row_a = next(r for r in out["verdict"]["scores"] if r["candidate_id"] == "A")
    assert row_a["charges"] == 0  # 전원 기각 → 환각 기대가 감점으로 남지 않는다


def test_critical_slots_are_capped(monkeypatch):
    """재심 슬롯에 상한이 있다 — 배치 프롬프트가 부풀면 판정이 흐려지고 상한 계산도 깨진다."""
    many = [_fatal(claim=f"결함{i}", trigger="t") for i in range(20)]
    stub = _install(monkeypatch, _LLMStub(
        blind=_plan(("폴더를 만든다", "must")),
        refute={"A": _Refutation(defects=many), "B": _Refutation()},
    ))
    judge_candidates(_SPEC, [_rep("A"), _rep("B")])
    listing = stub.user_of("vote")[0].split("[재심할 지적]\n")[1]
    assert sum(1 for ln in listing.splitlines() if ln.startswith("- C")) == judge_mod.MAX_CRITICAL_SLOTS


def _crit(cid: str, n: int, penalty: int = 12):
    """`_select_slots` 단위 검증용 지적 — 전부 critical, 기본은 fatal 동점(12)."""
    return judge_mod._Charge(
        charge_id=f"{cid}{n}", candidate_id=cid, kind="defect", claim=f"{cid}{n}",
        penalty=penalty, gateable=penalty >= 12, critical=True,
    )


def test_slot_selection_is_rank_round_robin_not_candidate_list_order():
    """각 후보의 1순위 지적이 다른 후보의 2순위보다 먼저 배정된다 (merge_by_rank와 같은 보장).

    fatal·must 미충족은 감점이 전부 12로 동점이라, 무게 단일 정렬은 안정 정렬 때문에
    `reports` 순서(= graph의 고정 페르소나 순서)를 그대로 우선순위로 삼는다. 그러면 상한을
    넘는 라운드에서 뒤쪽 후보는 재심을 **한 건도** 못 받는다.
    """
    charges = [_crit("A", i) for i in range(12)] + [_crit("B", i) for i in range(3)]
    picked = [c.charge_id for c in judge_mod._select_slots(charges)]
    assert len(picked) == judge_mod.MAX_CRITICAL_SLOTS
    assert picked[:4] == ["A0", "B0", "A1", "B1"]  # 1순위끼리 → 2순위끼리
    assert "B2" in picked                          # 뒤쪽 후보가 상한에 굶지 않는다


def test_same_rank_orders_by_penalty_before_candidate_order():
    """같은 순위 안에서는 무게가 먼저다 — 라운드로빈이 감점 우선순위를 뒤집지는 않는다.

    ⚠️ 이건 **과교정 방지 가드**이지 슬롯 편향(③)의 회귀 테스트가 아니다. 입력이
    penalty 4 vs 12라 옛 단일 정렬과 라운드로빈이 같은 순서를 내므로, 구현을 되돌려도
    초록이다(실측). 편향을 실제로 붙드는 건
    `test_late_candidate_fatal_still_gets_reviewed_and_can_gate`다 — 상한을 넘는
    동점 구간을 만들어 뒤쪽 후보가 재심에서 배제되는지를 본다.
    """
    charges = [_crit("A", 0, penalty=4), _crit("B", 0, penalty=12)]
    assert [c.charge_id for c in judge_mod._select_slots(charges)] == ["B0", "A0"]


def test_late_candidate_fatal_still_gets_reviewed_and_can_gate(monkeypatch):
    """지적이 몰린 후보 뒤에 있어도 치명 지적은 재심을 받아 게이트까지 갈 수 있다.

    상한을 앞쪽 후보가 독식하면 뒤쪽 후보의 fatal은 `upheld=None`으로 남아 **절대 게이트되지
    않는다.** 감점 차이가 아니라 후보를 통째로 배제하는 결정이 목록 순서로 갈리는 자리다.
    """
    stub = _install(monkeypatch, _LLMStub(
        blind=_plan(("폴더를 만든다", "must")),
        refute={
            "A": _Refutation(defects=[_fatal(claim=f"A결함{i}", trigger="t") for i in range(12)]),
            "B": _Refutation(defects=[_fatal(claim=f"B결함{i}", trigger="t") for i in range(2)]),
        },
        # B의 첫 지적(C13)만 확증한다 — 슬롯에 오르지 못하면 이 표는 어디에도 닿지 않는다.
        votes=[_SlotVotes(votes=[_SlotVote(slot_id="C13", upheld=True)])],
    ))
    out = judge_candidates(_SPEC, [_rep("A"), _rep("B")])

    lines = [
        ln for ln in stub.user_of("vote")[0].split("[재심할 지적]\n")[1].splitlines()
        if ln.startswith("- C")
    ]
    assert len(lines) == judge_mod.MAX_CRITICAL_SLOTS
    assert sum(1 for ln in lines if "후보 B" in ln) == 2  # B의 두 건 모두 재심 대상
    rows = {r["candidate_id"]: r for r in out["verdict"]["scores"]}
    assert rows["B"]["refuted"] is True   # 뒤쪽 후보도 확증되면 게이트된다
    assert out["winner"].candidate_id == "A"


# ─────────────────────────────────────────────────────────────────────────────
# 게이트 — 확증된 치명 결함만 자격을 박탈한다
# ─────────────────────────────────────────────────────────────────────────────

def _gate_pair():
    """결정론 점수는 A가 압도적으로 높은 한 쌍 — 게이트가 없으면 무조건 A가 이긴다."""
    return [_rep("A", cov=1.0, sim=1.0), _rep("B", cov=0.3, sim=0.3)]


def test_confirmed_fatal_loses_eligibility_even_with_better_signals(monkeypatch):
    """확증된 치명 결함은 결정론 우위를 이긴다 — 정밀도 대책이 실제로 승자를 바꾸는가."""
    _install(monkeypatch, _LLMStub(
        blind=_plan(("폴더를 만든다", "must")),
        refute={"A": _Refutation(defects=[_fatal()]), "B": _Refutation()},
        votes=[_SlotVotes(votes=[_SlotVote(slot_id="C1", upheld=True)])],
    ))
    out = judge_candidates(_SPEC, _gate_pair())
    assert out["winner"].candidate_id == "B"
    assert "후보 A는 확증된 치명 결함 또는 요구 누락으로 자격에서 제외" in out["verdict"]["reason"]


def test_unconfirmed_fatal_scores_but_does_not_gate(monkeypatch):
    """재심이 전부 실패해 표가 0장이면 감점만 하고 게이트로는 못 올린다 (부분 실패 격리).

    검증되지 않은 치명 주장으로 후보를 통째로 탈락시키는 쪽이 더 위험하다 — 재심 인프라가
    죽었을 때 심판이 '더 공격적'이 되면 안 된다.
    """
    _install(monkeypatch, _LLMStub(
        blind=_plan(("폴더를 만든다", "must")),
        refute={"A": _Refutation(defects=[_fatal()]), "B": _Refutation()},
        votes=[RuntimeError("vote llm down")],
    ))
    out = judge_candidates(_SPEC, _gate_pair())
    row_a = next(r for r in out["verdict"]["scores"] if r["candidate_id"] == "A")
    assert row_a["charges"] == 1 and row_a["refuted"] is False
    assert row_a["qualitative"] == 0.5      # 치명 1건 = 반감점
    assert out["winner"].candidate_id == "A"  # 감점을 안고도 결정론 우위로 이긴다


def test_fatal_without_trigger_is_demoted_and_cannot_gate(monkeypatch):
    """실패 상황을 못 쓴 치명 주장은 major로 강등된다 — 프롬프트의 저울을 코드도 든다."""
    _install(monkeypatch, _LLMStub(
        blind=_plan(("폴더를 만든다", "must")),
        refute={"A": _Refutation(defects=[_fatal(trigger="   ")]), "B": _Refutation()},
        votes=[_SlotVotes(votes=[_SlotVote(slot_id="C1", upheld=True)])],
    ))
    out = judge_candidates(_SPEC, _gate_pair())
    row_a = next(r for r in out["verdict"]["scores"] if r["candidate_id"] == "A")
    assert row_a["refuted"] is False      # 확증돼도 게이트로는 못 오른다
    assert row_a["qualitative"] == 0.75   # major(4) 감점만
    assert out["winner"].candidate_id == "A"


def test_l2_hard_gate_still_wins_over_refutation_gate(monkeypatch):
    """반증 게이트를 L2 하드 게이트보다 **먼저** 포기한다 — 둘 다 못 넘으면 결정론을 믿는다."""
    _install(monkeypatch, _LLMStub(
        blind=_plan(("폴더를 만든다", "must")),
        refute={"A": _Refutation(defects=[_fatal()]), "B": _Refutation()},
        votes=[_SlotVotes(votes=[_SlotVote(slot_id="C1", upheld=True)])],
    ))
    # B는 L2 하드 게이트 실패 → 남는 자격자는 '반증 게이트에 걸린 A'뿐이다.
    out = judge_candidates(_SPEC, [_rep("A", cov=1.0, sim=1.0), _rep("B", cov=0.3, sim=0.3, gate=["req-1"])])
    assert out["winner"].candidate_id == "A"


# ─────────────────────────────────────────────────────────────────────────────
# 앵커 무결성
# ─────────────────────────────────────────────────────────────────────────────

def test_hallucinated_expectation_ids_are_dropped(monkeypatch):
    """존재하지 않는 기대 id는 감점도 재심도 만들지 않는다 — 근거 없는 감점 차단."""
    stub = _install(monkeypatch, _LLMStub(
        blind=_plan(("폴더를 만든다", "must")),
        refute={
            "A": _Refutation(unmet=["E9", "E1", "E1"], defects=[_fatal(exp_id="E9")]),
            "B": _Refutation(),
        },
        votes=[_SlotVotes(votes=[_SlotVote(slot_id=f"C{i}", upheld=True) for i in range(1, 6)])],
    ))
    out = judge_candidates(_SPEC, [_rep("A"), _rep("B")])
    row_a = next(r for r in out["verdict"]["scores"] if r["candidate_id"] == "A")
    # 살아남는 지적: 치명 결함 1 + E1 미충족 1 (E9 미충족과 중복 E1은 소거)
    assert row_a["charges"] == 2
    assert stub.count("vote") == judge_mod.VOTE_ROUNDS


# ─────────────────────────────────────────────────────────────────────────────
# 부분 실패 격리
# ─────────────────────────────────────────────────────────────────────────────

def test_blind_failure_degrades_to_refutation_only(monkeypatch):
    """맹목 생성이 죽어도 반증은 계속 간다 — 대조만 잃고 심판은 산다."""
    stub = _install(monkeypatch, _LLMStub(
        blind=ValueError("blind parse fail"),
        refute={"A": _Refutation(defects=[_fatal()]), "B": _Refutation()},
        votes=[_SlotVotes(votes=[_SlotVote(slot_id="C1", upheld=True)])],
    ))
    out = judge_candidates(_SPEC, _gate_pair())
    assert stub.count("refute") == 2
    assert "기대 목록 생성 실패" in stub.user_of("refute")[0]
    assert out["winner"].candidate_id == "B"  # 반증 게이트는 그대로 작동


def test_one_candidate_refutation_failure_does_not_kill_the_judge(monkeypatch):
    """후보 하나의 반증이 죽어도 나머지는 판정된다 — 그 후보만 **중립 점수**로 강등."""
    stub = _install(monkeypatch, _LLMStub(
        blind=_plan(("폴더를 만든다", "must")),
        refute={"A": RuntimeError("rate limit"), "B": _Refutation(defects=[_fatal()])},
        votes=[_SlotVotes(votes=[_SlotVote(slot_id="C1", upheld=True)])],
    ))
    out = judge_candidates(_SPEC, [_rep("A", cov=0.6, sim=0.6), _rep("B", cov=1.0, sim=1.0)])
    assert stub.count("refute") == 2
    rows = {r["candidate_id"]: r for r in out["verdict"]["scores"]}
    assert rows["A"]["charges"] == 0 and rows["A"]["refuted"] is False
    assert rows["A"]["qualitative"] == judge_mod._NO_REFUTATION_QUAL  # 만점이 아니라 중립
    assert rows["B"]["refuted"] is True
    assert out["winner"].candidate_id == "A"


def test_failed_refutation_is_neutral_not_a_perfect_score(monkeypatch):
    """반증 콜이 죽은 후보는 반증 축에서 **중립**을 받는다 — 실패가 보상이 되면 안 된다.

    지적 0건은 "반증해 보니 결함이 없었다"와 "반증을 못 했다"에서 똑같이 나온다. 감점 환산에
    그대로 태우면 뒤쪽이 만점(1.0)을 받아, rate limit 하나가 0.4 가중치 보너스와 게이트
    면제를 동시에 주는 셈이 된다.
    """
    _install(monkeypatch, _LLMStub(
        blind=_plan(("폴더를 만든다", "must")),
        refute={"A": RuntimeError("rate limit"), "B": _Refutation()},
    ))
    out = judge_candidates(_SPEC, [_rep("A"), _rep("B")])
    rows = {r["candidate_id"]: r for r in out["verdict"]["scores"]}
    assert rows["A"]["refute_ran"] is False and rows["B"]["refute_ran"] is True
    assert rows["A"]["qualitative"] == judge_mod._NO_REFUTATION_QUAL == 0.5
    assert rows["B"]["qualitative"] == 1.0  # 실제로 반증을 거쳐 나온 무지적
    # 결정론 신호가 동률이면 승자가 갈린다 — 미실행이 만점이면 A가 이겨 버린다.
    assert out["winner"].candidate_id == "B"


def test_unrun_refutation_never_claims_absence_of_defects(monkeypatch):
    """검사하지 않았으면 "지적 없음"이라고 쓰지 않는다 — 모름은 침묵하거나 명시한다.

    사용자에게 "반증에서 결함이 나오지 않았습니다"가 나가는데 실제로는 검사 자체가 실행되지
    않은 상태면, 심판 신뢰도를 과대 표시하는 것이다.
    """
    _install(monkeypatch, _LLMStub(
        blind=_plan(("폴더를 만든다", "must")),
        refute={"A": RuntimeError("rate limit"), "B": ValueError("schema")},
    ))
    out = judge_candidates(_SPEC, [_rep("A", cov=1.0, sim=1.0), _rep("B", cov=0.3, sim=0.3)])
    notes = [r["note"] for r in out["verdict"]["scores"]]
    assert all("지적 없음" not in n for n in notes)
    assert all("미실행" in n and "미확인" in n for n in notes)
    reason = out["verdict"]["reason"]
    assert "결함이 나오지 않았습니다" not in reason
    assert "확인하지 못했" in reason  # 사용자 문구도 미실행 사실을 말한다


def test_note_still_reports_no_defects_when_refutation_actually_ran(monkeypatch):
    """반증이 실제로 돌아 무지적이면 그렇게 말한다 — ②의 수정이 긍정 판정까지 지우면 과교정이다."""
    _install(monkeypatch, _LLMStub(
        blind=_plan(("폴더를 만든다", "must")),
        refute={"A": _Refutation(), "B": _Refutation()},
    ))
    out = judge_candidates(_SPEC, [_rep("A"), _rep("B")])
    assert all(r["note"] == "반증에서 지적 없음" for r in out["verdict"]["scores"])
    assert "결함이 나오지 않았습니다" in out["verdict"]["reason"]


def test_total_llm_outage_falls_back_to_deterministic_score(monkeypatch):
    """전 단계 LLM이 죽어도 승자는 나온다 — 결정론 앵커만으로 판정 (레거시와 같은 폴백)."""
    def _boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(judge_mod, "chat_json", _boom)
    out = judge_candidates(_SPEC, [_rep("A", cov=0.4, sim=0.4), _rep("B", cov=0.95, sim=0.95)])
    assert out["winner"].candidate_id == "B"
    assert out["transplant_findings"] == []
    # 전원 중립 — 반증 축이 상수가 되어 결정론만으로 갈린다. 만점을 주면 "전 후보가 무결"이
    # 되어 반증 게이트가 통째로 면제되고, 장애 라운드일수록 심판이 관대해진다.
    assert all(r["qualitative"] == judge_mod._NO_REFUTATION_QUAL for r in out["verdict"]["scores"])


# ─────────────────────────────────────────────────────────────────────────────
# 비용 — 호출 수가 계약이다
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("n_candidates", [2, 3])
def test_call_count_is_one_plus_n_plus_three(monkeypatch, n_candidates):
    """호출 수 = 맹목 1 + 반증 N + 재심 3. 이 숫자가 조용히 늘면 초안 비용이 배로 샌다."""
    reports = [_rep(chr(ord("A") + i)) for i in range(n_candidates)]
    stub = _install(monkeypatch, _LLMStub(
        blind=_plan(("폴더를 만든다", "must")),
        refute={r.candidate_id: _Refutation(defects=[_fatal()]) for r in reports},
    ))
    judge_candidates(_SPEC, reports)
    assert stub.count("blind") == 1
    assert stub.count("refute") == n_candidates
    assert stub.count("vote") == judge_mod.VOTE_ROUNDS
    assert len(stub.calls) == 1 + n_candidates + judge_mod.VOTE_ROUNDS


def test_fanout_threads_keep_usage_attribution(monkeypatch):
    """팬아웃 스레드에서도 사용량 귀속(ContextVar)이 유지된다.

    ThreadPoolExecutor는 `asyncio.to_thread`와 달리 호출자 컨텍스트를 옮기지 않는다.
    이게 깨지면 심판이 태운 5~6콜의 비용이 전부 귀속 없는 기본값으로 기록돼 링 게이지와
    예산 집계에서 조용히 샌다 — 실패가 안 보이는 종류라 테스트로 붙잡는다.
    """
    from app.core import llm

    seen: list[str] = []

    class _Recording(_LLMStub):
        def __call__(self, messages, *, purpose, model_cls):
            with self._lock:
                seen.append(llm.current_usage_context().component)
            return super().__call__(messages, purpose=purpose, model_cls=model_cls)

    _install(monkeypatch, _Recording(
        blind=_plan(("폴더를 만든다", "must")),
        refute={"A": _Refutation(defects=[_fatal()]), "B": _Refutation()},
    ))
    with llm.usage_context(component="agent"):
        judge_candidates(_SPEC, [_rep("A"), _rep("B")])

    assert len(seen) == 1 + 2 + judge_mod.VOTE_ROUNDS
    assert set(seen) == {"agent"}  # 팬아웃 스레드 포함 전부 귀속된다


def test_single_candidate_short_circuits_without_any_llm_call(monkeypatch):
    """후보가 하나면 LLM을 아예 안 부른다 — 비교할 게 없는데 6콜을 쓰지 않는다."""
    stub = _install(monkeypatch, _LLMStub())
    out = judge_candidates(_SPEC, [_rep("A")])
    assert stub.calls == []
    assert out["winner"].candidate_id == "A" and out["verdict"]["reason"] == "단일 후보"


def test_flag_off_restores_legacy_single_call_rubric(monkeypatch):
    """V4_JUDGE_REFUTATION=false면 레거시 채점형 1콜로 되돌아간다 — 끄는 스위치가 실제로 끈다."""
    monkeypatch.setenv("V4_JUDGE_REFUTATION", "false")
    stub = _install(monkeypatch, _LLMStub())
    out = judge_candidates(_SPEC, [_rep("A"), _rep("B")])
    assert stub.stages() == ["rubric"]
    assert set(out) == {"winner", "verdict", "transplant_findings"}


# ─────────────────────────────────────────────────────────────────────────────
# 이식 지시
# ─────────────────────────────────────────────────────────────────────────────

def test_transplants_carry_winner_gaps_and_loser_strengths(monkeypatch):
    """승자에게 내리는 지시 = 승자에게 남은 지적 + 패자 장점. LLM 추가 호출 없이 만든다.

    게이트를 통과했다는 것과 무결하다는 것은 다른 말이다 — 여기서 두 후보 모두 must 기대를
    하나씩 놓쳐 자격 완화로 A가 이겼고, A가 놓친 단계는 B가 갖고 있으니 어디서 베낄지까지
    적어 내려보낸다.
    """
    stub = _install(monkeypatch, _LLMStub(
        blind=_plan(("결과 폴더가 없으면 만든다", "must"), ("처리 로그를 남긴다", "must")),
        refute={
            "A": _Refutation(unmet=["E1"]),   # C1
            "B": _Refutation(unmet=["E2"], strengths=["엑셀 작업을 Try로 감싸고 Finally에서 세션을 닫는 구조", ""]),  # C2
        },
        votes=[_SlotVotes(votes=[_SlotVote(slot_id="C1", upheld=True), _SlotVote(slot_id="C2", upheld=True)])],
    ))
    out = judge_candidates(_SPEC, [_rep("A", cov=1.0, sim=1.0), _rep("B", cov=0.2, sim=0.2)])

    assert out["winner"].candidate_id == "A"  # 전원 반증 게이트 → 자격 완화 후 결정론 우위
    # 전원이 걸린 라운드에서 "다른 후보는 제외됐다"고 쓰면 거짓말이다 — 승자도 같은 결함을 안고 있다.
    assert "자격에서 제외" not in out["verdict"]["reason"]
    assert stub.count("vote") == judge_mod.VOTE_ROUNDS  # 이식 지시에 추가 콜은 없다
    gap = next(f for f in out["transplant_findings"] if f.severity == "major")
    assert gap.layer == "judge" and "결과 폴더" in gap.fix_hint
    assert "후보 B가 이 단계를 갖고 있으니" in gap.fix_hint
    graft = next(f for f in out["transplant_findings"] if f.severity == "minor")
    assert graft.message.startswith("이식 지시: 후보 B의")
    assert len(out["transplant_findings"]) == 2  # 빈 strength는 버려진다


def test_transplant_findings_are_capped(monkeypatch):
    """이식 지시 상한 5건 — refine 첫 라운드 프롬프트가 지시로 덮이지 않게 (레거시와 동일)."""
    _install(monkeypatch, _LLMStub(
        blind=_plan(*[(f"기대{i}", "must") for i in range(6)]),
        refute={
            "A": _Refutation(unmet=[f"E{i}" for i in range(1, 7)]),
            "B": _Refutation(unmet=[f"E{i}" for i in range(1, 7)], strengths=["s1", "s2"]),
        },
        votes=[_SlotVotes(votes=[_SlotVote(slot_id=f"C{i}", upheld=True) for i in range(1, 13)])],
    ))
    out = judge_candidates(_SPEC, [_rep("A", cov=1.0, sim=1.0), _rep("B", cov=0.2, sim=0.2)])
    assert out["winner"].candidate_id == "A"
    assert len(out["transplant_findings"]) == 5


# ─────────────────────────────────────────────────────────────────────────────
# 계약 — recommend/graph.py가 부르는 형태
# ─────────────────────────────────────────────────────────────────────────────

def test_public_signature_and_return_shape_unchanged(monkeypatch):
    """graph.py의 `judge_candidates(spec, list(reports))` 호출이 그대로 통해야 한다.

    graph.py는 다른 항목의 에이전트가 동시에 고치는 파일이라 시그니처가 계약이다.
    새 인자는 전부 기본값 있는 키워드여야 한다.
    """
    sig = inspect.signature(judge_candidates)
    params = list(sig.parameters.values())
    assert [p.name for p in params[:2]] == ["spec", "reports"]
    assert all(p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD for p in params[:2])
    for p in params[2:]:
        assert p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is not inspect.Parameter.empty

    _install(monkeypatch, _LLMStub(blind=_plan(("폴더를 만든다", "must"))))
    out = judge_candidates(_SPEC, [_rep("A"), _rep("B")])
    assert set(out) == {"winner", "verdict", "transplant_findings"}
    assert isinstance(out["winner"], CandidateReport)
    assert set(out["verdict"]) == {"winner", "reason", "scores"}
    # 프레임이 읽는 기존 점수 키는 하나도 빠지지 않는다 (추가만 허용).
    for row in out["verdict"]["scores"]:
        assert {"candidate_id", "persona", "deterministic", "qualitative",
                "total", "gate_failed", "note"} <= set(row)
        assert isinstance(out["verdict"]["reason"], str) and out["verdict"]["reason"]


def test_document_is_fenced_into_the_blind_prompt_only(monkeypatch):
    """문서를 넘기면 맹목 단계만 원문을 본다 — 인젝션 펜스는 spec 모듈 것을 그대로 쓴다."""
    stub = _install(monkeypatch, _LLMStub(
        blind=_plan(("폴더를 만든다", "must")),
        refute={"A": _Refutation(), "B": _Refutation()},
    ))
    judge_candidates(_SPEC, [_rep("A"), _rep("B")], document="사내 규정: 결과는 월별 폴더에 저장한다.")
    (blind_user,) = stub.user_of("blind")
    assert "<<<DOC>>>" in blind_user and "월별 폴더" in blind_user
    assert all("월별 폴더" not in u for u in stub.user_of("refute"))
