"""v4 뭉갬(conflation) 탐지 — 한 자리가 요구 여럿을 떠맡은 경우 (RPA-298 Phase 3).

여기서 못 박는 것은 하나다: **뭉갬이 더 이상 누락으로 둔갑하지 않는다.**

사용자 불만은 "빠뜨리거나 뭉갠다" 둘이었는데, 누락만 blocker로 잡혔고 뭉갬은 아무도
안 봤다. 게다가 그냥 안 잡힌 게 아니라 **누락으로 잘못 잡혔다**: 한 액션이 "req-1,req-2"를
담당한다고 적으면 기존 판정은 그 문자열을 어느 요구와도 안 맞는 통짜 키로 세어 요구
둘을 미배정(blocker 200)으로 올렸고, surgeon은 "삽입하라"는 지시를 받아 이미 있는 일을
하는 액션을 하나 더 넣었다. 재분해가 아니라 중복 생성이다.

`test_conflated_slot_is_not_counted_as_missing`(둔갑 차단)과
`test_refine_accepts_honest_split_even_when_it_adds_a_violation`(재분해가 실제로 통과)는
한 쌍으로 읽어야 한다 — 앞은 진단, 뒤는 그 진단이 교정까지 이어지는지다.

LLM은 한 번도 부르지 않는다. surgeon(chat_json)은 결정론 대역으로 갈아끼운다.
"""

from app.agent.v4.orchestrator import harness as harness_mod
from app.agent.v4.orchestrator.edit_ops import EditOp, EditOps, annotate_ids
from app.agent.v4.verify.coverage_det import (
    completeness_findings,
    conflated_slots,
    conflation_findings,
    coverage_findings,
    missing_requirements,
    slot_req_ids,
)
from app.agent.v4.verify.findings import SEVERITY_WEIGHT, weight

from tests.agent_stubs import FakeCatalog

_CATALOG = FakeCatalog()

# String/assign은 카탈로그에 있고 필수 파라미터가 value(TEXT) 하나뿐이라 세션(R7/R8)·
# 데이터플로우(R9~R11) 노이즈 없이 R2만 골라 심을 수 있다 (test_agent_v4_coverage_det과 동일).
_PKG, _ACT = "String", "assign"


def _action(label: str, *, req_id=None, bogus: bool = False, children=None) -> dict:
    params = [{"name": "value", "value": label, "value_source": "llm"}]
    if bogus:  # R2(파라미터명 실재) major를 하나 심는다
        params.append({"name": "존재하지않는파라미터", "value": "x", "value_source": "llm"})
    a = {"order": 1, "package": _PKG, "action": _ACT, "label": label,
         "parameters": params, "children": children or []}
    if req_id is not None:
        a["req_id"] = req_id
    return a


def _flow(*actions: dict) -> dict:
    for i, a in enumerate(actions, 1):
        a["order"] = i
    return {
        "schema_version": "1.0",
        "steps": [{"step_id": "step-1", "label": "본문", "actions": list(actions)}],
        "variables": [], "notes": None,
    }


def _spec(*reqs: tuple[str, str, str]) -> dict:
    return {
        "goal": "테스트 업무",
        "requirements": [
            {"req_id": rid, "text": text, "priority": prio, "source": "doc"}
            for rid, text, prio in reqs
        ],
    }


_TWO_MUST = _spec(("req-1", "매출 파일을 연다", "must"), ("req-2", "결과를 메일로 보낸다", "must"))


def _labels(flow: dict) -> list[str]:
    return [a["label"] for s in flow["steps"] for a in s["actions"]]


def _req_ids(flow: dict) -> list:
    return [a.get("req_id") for s in flow["steps"] for a in s["actions"]]


def _scripted_surgeon(*rounds: EditOps):
    """라운드별로 정해진 EditOps를 내는 surgeon 대역 — 목록이 끝나면 무연산(=종료 신호)."""
    calls: list[int] = []

    def _fake(messages, **kwargs):
        calls.append(1)
        idx = len(calls) - 1
        return rounds[idx] if idx < len(rounds) else EditOps(operations=[])

    _fake.calls = calls
    return _fake


# ─────────────────────────────────────────────────────────────────────────────
# (1) 규명 — 이 파이프라인에서 뭉갬은 어떤 '모양'으로 도착하는가
#
# 스키마의 req_id는 스칼라 하나지만, 검수·교정 루프가 보는 흐름도는 Recommendation
# 검증을 아직 안 거친 원시 dict다(_parse_flow → refine_flow → 맨 끝에서야 model_validate).
# 그래서 스칼라 칸에 우겨넣은 "req-1, req-2"와 리스트 슬립 ["req-1","req-2"] 둘 다 도착한다.
# ─────────────────────────────────────────────────────────────────────────────

def test_slot_req_ids_reads_the_normal_scalar_unchanged():
    """정상 경로가 그대로여야 한다 — 여기가 흔들리면 누락 판정 전체가 흔들린다."""
    assert slot_req_ids(_action("A", req_id="req-1"), {"req-1", "req-2"}) == ["req-1"]
    assert slot_req_ids(_action("A"), {"req-1"}) == []
    assert slot_req_ids(_action("A", req_id="   "), {"req-1"}) == []


def test_slot_req_ids_splits_ids_crammed_into_the_scalar_field():
    """뭉갬의 실제 모양 ① — 한 칸에 콤마로 우겨넣기. 안 쪼개면 통짜 키가 되어 둘 다 미배정이 된다."""
    known = {"req-1", "req-2"}
    assert slot_req_ids(_action("A", req_id="req-1, req-2"), known) == ["req-1", "req-2"]
    assert slot_req_ids(_action("A", req_id="req-1/req-2"), known) == ["req-1", "req-2"]


def test_slot_req_ids_accepts_the_list_slip():
    """뭉갬의 실제 모양 ② — 리스트 슬립. 앞 단계(operation_units)가 req_ids 리스트를 가르쳐서 나온다.

    기존 코드는 str이 아닌 값을 통째로 버렸다 = 담당이 있는데도 없는 것으로 셌다.
    """
    assert slot_req_ids(_action("A", req_id=["req-1", "req-2"]), {"req-1", "req-2"}) == ["req-1", "req-2"]
    assert slot_req_ids(_action("A", req_id=["req-1", "req-1"]), {"req-1"}) == ["req-1"]


def test_slot_req_ids_keeps_unknown_text_whole():
    """쪼갠 조각 중 실재 요구가 하나도 없으면 원문을 통짜로 남긴다 — 모름 → 침묵.

    앵커를 쪼개는 건 되돌릴 수 없는 해석이라 스펙이 확인해 줄 때만 한다.
    """
    assert slot_req_ids(_action("A", req_id="자유 서술, 아무거나"), {"req-1"}) == ["자유 서술, 아무거나"]
    assert slot_req_ids(_action("A", req_id="req-1,req-2"), None) == ["req-1,req-2"]


# ─────────────────────────────────────────────────────────────────────────────
# (2) 탐지 — 발화와 침묵
# ─────────────────────────────────────────────────────────────────────────────

def test_conflated_slot_fires_one_finding_per_requirement():
    """뭉친 자리는 요구마다 한 건씩 잡는다 — 이유는 가중치 규약 절(아래)에 있다."""
    flow = _flow(_action("파일 열고 메일까지", req_id="req-1, req-2"))

    fnd = conflation_findings(flow, _TWO_MUST)

    assert [f.req_id for f in fnd] == ["req-1", "req-2"]
    assert {f.severity for f in fnd} == {"major"}
    assert {f.layer for f in fnd} == {"L2"}
    # 좌표는 checker/Violation과 같은 트리 경로 표기여야 surgeon 프롬프트에서 일관된다.
    assert {f.location for f in fnd} == {"actions[0]"}
    assert {f.step_id for f in fnd} == {"step-1"}
    assert "req-1·req-2" in fnd[0].message and "파일 열고 메일까지" in fnd[0].message
    # 힌트가 '담당을 줄여서 해결'을 명시적으로 닫아야 surgeon이 req_id 떼기로 도망가지 않는다.
    assert "req_id만 떼어내" in (fnd[0].fix_hint or "")


def test_conflated_slot_is_detected_through_the_list_slip_too():
    """모양이 달라도 같은 문제다 — 리스트로 들어와도 재분해 지시가 나가야 한다."""
    flow = _flow(_action("두 가지 한꺼번에", req_id=["req-1", "req-2"]))

    assert [f.req_id for f in conflation_findings(flow, _TWO_MUST)] == ["req-1", "req-2"]


def test_healthy_flow_is_silent():
    """요구마다 자리가 하나씩 = 정상. 여기서 발화하면 잘 만든 흐름도가 매번 재분해된다."""
    flow = _flow(_action("열기", req_id="req-1"), _action("메일", req_id="req-2"))

    assert conflation_findings(flow, _TWO_MUST) == []
    assert conflated_slots(flow, _TWO_MUST) == []


def test_fan_out_is_not_conflation():
    """한 요구를 여러 액션이 나눠 담당하는 건 compose 프롬프트가 시킨 정상 형태다(역방향)."""
    flow = _flow(_action("열기", req_id="req-1"), _action("읽기", req_id="req-1"),
                 _action("메일", req_id="req-2"))

    assert conflation_findings(flow, _TWO_MUST) == []


def test_container_carrying_several_requirements_is_not_conflation():
    """오탐 방지 — 컨테이너(Loop/If/Error handler)는 본문이 여러 요구를 수행하는 게 정상이다.

    compose_v4_addendum이 "컨테이너에도 붙인다 — 본문이 그 요구를 수행한다면"이라고 직접
    지시한다. 컨테이너를 세면 정상 흐름도가 통째로 발화한다.
    """
    container = _action("반복", req_id="req-1, req-2", children=[
        _action("열기", req_id="req-1"), _action("메일", req_id="req-2"),
    ])

    assert conflation_findings(_flow(container), _TWO_MUST) == []
    assert missing_requirements(_flow(container), _TWO_MUST) == []


def test_conflation_is_detected_inside_container_children():
    """반대로 컨테이너 **본문의 리프**가 뭉치면 잡아야 한다 — 재귀를 빠뜨리면 뭉갬이 숨는다."""
    container = _action("반복", children=[_action("한꺼번에", req_id="req-1,req-2")])

    slots = conflated_slots(_flow(container), _TWO_MUST)

    assert [s["location"] for s in slots] == ["actions[0].children[0]"]


def test_pure_plumbing_without_anchor_is_silent():
    """순수 배관(경로 조립·폴더 확인)은 req_id가 비는 게 정상 — 주장하는 요구가 0개라 침묵."""
    flow = _flow(_action("경로 조립"), _action("열기", req_id="req-1"), _action("메일", req_id="req-2"))

    assert conflation_findings(flow, _TWO_MUST) == []


def test_whole_module_is_silent_without_any_anchor():
    """앵커가 하나도 없으면 통째로 침묵 — coverage_det의 기존 계약을 뭉갬도 그대로 따른다."""
    flow = _flow(_action("A"), _action("B"))

    assert conflation_findings(flow, _TWO_MUST) == []
    assert coverage_findings(flow, _TWO_MUST) == []
    assert completeness_findings(flow, _TWO_MUST) == []


def test_conflation_is_silent_without_requirements():
    """스펙에 요구가 없으면 채점 기준 자체가 없다 — 근거 없는 발화는 소음이다."""
    flow = _flow(_action("A", req_id="req-1,req-2"))

    assert conflation_findings(flow, {"goal": "g"}) == []
    assert conflation_findings(flow, {}) == []


def test_should_priority_conflation_is_minor():
    """등급 축은 누락과 같다 — must는 major, should는 minor."""
    spec = _spec(("req-1", "매출 파일을 연다", "must"), ("req-2", "로그를 남긴다", "should"))

    graded = {f.req_id: f.severity for f in conflation_findings(_flow(_action("A", req_id="req-1,req-2")), spec)}

    assert graded == {"req-1": "major", "req-2": "minor"}


def test_conflated_slot_is_not_counted_as_missing():
    """**둔갑 차단** — 뭉친 자리가 주장하는 요구는 '배정'이다.

    미배정으로 세면 누락 blocker가 발화해 surgeon이 이미 (부실하게) 하고 있는 일을 하는
    액션을 하나 더 삽입한다. 뭉갬은 뭉갬으로(재분해 지시로) 잡아야 한다.
    """
    flow = _flow(_action("한꺼번에", req_id="req-1, req-2"))

    assert missing_requirements(flow, _TWO_MUST) == []
    assert coverage_findings(flow, _TWO_MUST) == []
    assert [f.req_id for f in completeness_findings(flow, _TWO_MUST)] == ["req-1", "req-2"]


def test_completeness_findings_carry_both_axes():
    """누락과 뭉갬이 같은 축에 있어야 양쪽 도피로(삭제·요구 떼기)가 동시에 막힌다."""
    spec = _spec(("req-1", "열기", "must"), ("req-2", "메일", "must"), ("req-3", "정리", "must"))
    flow = _flow(_action("한꺼번에", req_id="req-1,req-2"))  # req-3은 진짜 누락

    graded = {(f.req_id, f.severity) for f in completeness_findings(flow, spec)}

    assert graded == {("req-3", "blocker"), ("req-1", "major"), ("req-2", "major")}


# ─────────────────────────────────────────────────────────────────────────────
# (3) 가중치 규약 — 왜 blocker가 아니고, 왜 슬롯당이 아니라 요구당인가
# ─────────────────────────────────────────────────────────────────────────────

def test_conflation_weight_absorbs_one_incidental_violation_but_not_a_missing():
    """뭉갬 무게는 두 부등식 사이에 있어야 한다.

    ① 정적 위반 1건(major·10)보다 무겁다 → 쪼개다 부수 위반이 하나 생겨도 회귀 가드가
       재분해를 폐기하지 않는다. 슬롯당 1건(10)이면 ±0이라 **영원히 못 고친다.**
    ② 누락 1건(blocker·100)보다 가볍다 → "쪼개느니 req_id 하나 떼자"가 절대 이득이 아니다.
    """
    fnd = conflation_findings(_flow(_action("A", req_id="req-1,req-2")), _TWO_MUST)

    assert weight(fnd) > SEVERITY_WEIGHT["major"]
    assert weight(fnd) < SEVERITY_WEIGHT["blocker"]


# ─────────────────────────────────────────────────────────────────────────────
# (4) 회귀 가드 상호작용 — 재분해는 액션 수를 늘린다
# ─────────────────────────────────────────────────────────────────────────────

def _split_ops(*, drop_one: bool = False, bogus_on_second: bool = False) -> EditOps:
    """뭉친 자리(n1)를 요구별로 쪼개는 surgeon 대역의 연산.

    surgeon.md가 지시하는 순서 그대로다 — 먼저 insert(anchor=n1), 그 다음 remove(n1).
    먼저 지우면 anchor가 사라져 삽입이 전부 실패한다.
    drop_one=True면 요구 하나를 조용히 버리는 '가짜 재분해'가 된다.
    """
    ops = [EditOp(
        op="insert", anchor="n1", position="after",
        action={"package": _PKG, "action": _ACT, "label": "열기", "req_id": "req-1",
                "parameters": [{"name": "value", "value": "x", "value_source": "llm"}]},
    )]
    if not drop_one:
        params = [{"name": "value", "value": "y", "value_source": "llm"}]
        if bogus_on_second:  # 삽입이 새 정적 위반(R2 major)을 자작하는 상황의 재현
            params.append({"name": "존재하지않는파라미터", "value": "z", "value_source": "llm"})
        ops.append(EditOp(
            op="insert", anchor="n1", position="after",
            action={"package": _PKG, "action": _ACT, "label": "메일", "req_id": "req-2",
                    "parameters": params},
        ))
    ops.append(EditOp(op="remove", target="n1"))
    return EditOps(operations=ops)


def test_refine_accepts_honest_split_even_when_it_adds_a_violation(monkeypatch):
    """**핵심 상호작용** — 재분해는 액션을 늘리고, 늘어난 액션은 새 위반을 만든다.

    그게 가중합을 밀어 올려 교정이 거부되면 뭉갬은 영원히 안 고쳐진다. 뭉갬을 요구당
    1건(2중 뭉갬=20)으로 세기 때문에 부수 위반 1건(10)을 흡수하고도 가중합이 준다.
    """
    monkeypatch.setattr(harness_mod, "chat_json",
                        _scripted_surgeon(_split_ops(bogus_on_second=True)))

    out = harness_mod.refine_flow(
        _flow(_action("한꺼번에", req_id="req-1, req-2")), _CATALOG, spec=_TWO_MUST,
    )

    assert out["repaired"] is True, "재분해가 폐기됐다 — 뭉갬을 영원히 못 고치는 상태"
    assert sorted(_req_ids(out["flow"])) == ["req-1", "req-2"]
    assert conflation_findings(out["flow"], _TWO_MUST) == []
    assert missing_requirements(out["flow"], _TWO_MUST) == []
    # 부수 위반을 안고도 채택했다는 사실 자체가 이 테스트의 요지다.
    assert [v["rule"] for v in out["violations"]] == ["R2"]


def test_refine_rejects_a_fake_split_that_drops_a_requirement(monkeypatch):
    """뭉갬을 'req_id 하나 떼기'로 없애면 그 요구가 즉시 누락 blocker(100)가 되어 거부된다.

    §5-D의 대칭짝이다: 삭제 편향은 누락 항이, 요구 떼기는 누락 항이 뭉갬 항과 함께 막는다.
    """
    monkeypatch.setattr(harness_mod, "chat_json", _scripted_surgeon(_split_ops(drop_one=True),
                                                                   _split_ops(drop_one=True)))

    out = harness_mod.refine_flow(
        _flow(_action("한꺼번에", req_id="req-1, req-2")), _CATALOG, spec=_TWO_MUST,
    )

    assert out["repaired"] is False
    assert _req_ids(out["flow"]) == ["req-1, req-2"], "요구 하나가 조용히 증발했다"
    assert _labels(out["flow"]) == ["한꺼번에"]
    # 무개선 2라운드로 종료 — 예산(8)을 다 태우지 않는다.
    assert len(harness_mod.chat_json.calls) == harness_mod._STOP_AFTER_NO_IMPROVE


def test_refine_without_spec_ignores_conflation(monkeypatch):
    """하위호환 — spec 미지정 호출(edit 경로 등)은 완성도 항이 0이라 기존 동작 그대로."""
    monkeypatch.setattr(harness_mod, "chat_json", _scripted_surgeon())
    flow = _flow(_action("한꺼번에", req_id="req-1, req-2"))

    out = harness_mod.refine_flow(flow, _CATALOG)

    assert out == {"flow": flow, "violations": [], "repaired": False}


# ─────────────────────────────────────────────────────────────────────────────
# (5) surgeon에게 전달 — 슬롯 목적 블록과 프롬프트 계약
# ─────────────────────────────────────────────────────────────────────────────

def test_slot_purpose_block_shows_a_conflated_slot_under_one_node_id():
    """뭉친 자리는 같은 노드 id로 여러 줄에 나와야 한다.

    ① surgeon이 finding이 가리키는 자리를 아웃라인에서 찾을 수 있고,
    ② 그 자리가 '담당 요구 있는 자리'로 인식돼 삭제 보호를 받는다.
    스칼라로만 읽으면 뭉갬 슬롯이 **목적 없는 자리**로 보여 가장 싼 remove 후보가 된다.
    """
    flow = annotate_ids(_flow(_action("한꺼번에", req_id="req-1, req-2")))
    node_id = flow["steps"][0]["actions"][0]["_id"]

    block = harness_mod.slot_purpose_block(flow, _TWO_MUST)

    assert block.count(f"[{node_id}]") == 2
    assert "req-1" in block and "req-2" in block
    assert "⚠뭉갬" in block
    assert "요구마다 액션을 나누고" in block


def test_slot_purpose_block_stays_silent_without_anchors():
    """기존 침묵 계약이 유지되는지 — 뭉갬 지원을 넣다가 프롬프트가 바뀌면 안 된다."""
    flow = annotate_ids(_flow(_action("A"), _action("B")))

    assert harness_mod.slot_purpose_block(flow, _TWO_MUST) == ""
    assert harness_mod.slot_purpose_block(annotate_ids(_flow(_action("A", req_id="req-1"))), None) == ""


def test_surgeon_prompt_teaches_the_split_recipe():
    """프롬프트 계약 — 이 지시가 빠지면 surgeon은 뭉갬 finding을 받고도 고칠 방법을 모른다.

    쓸 수 있는 연산에 req_id 재지정이 없어서(EditOp.update는 package/action/label만 바꾼다)
    'insert 먼저, remove 나중'이라는 순서까지 프롬프트가 알려줘야 한다.
    """
    prompt = harness_mod._SURGEON_PROMPT

    assert "뭉갬" in prompt
    assert "req_id 하나만 떼어 내는 식으로 해결하지 마세요" in prompt
    assert "먼저 지우면 anchor가 사라집니다" in prompt
