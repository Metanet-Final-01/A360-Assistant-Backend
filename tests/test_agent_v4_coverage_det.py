"""v4 누락 blocker + 회귀 가드 완성도 항 (RPA-298, 설계 §5 결정 A·B·D·F·G·H).

여기서 못 박는 것은 하나다: **액션 삭제가 더 이상 공짜가 아니다.**

기존 교정 루프의 목적 함수는 정적 위반 가중합뿐이었고 `remove`가 surgeon의 허용
연산이라, 위반 있는 액션을 지우면 가중합이 반드시 줄어 삭제가 항상 유효한 '개선'
경로였다. 실측 서명: 정밀도 0.295→0.430(상승) vs 재현율 0.109→0.151(정체).
`test_refine_flow_without_spec_accepts_deletion`이 그 편향을 재현하고,
`test_refine_flow_with_spec_rejects_deletion_of_assigned_action`이 완성도 항으로
막히는 것을 본다 — 두 테스트는 한 쌍으로 읽어야 한다.

LLM은 한 번도 부르지 않는다. surgeon(chat_json)은 결정론 대역으로 갈아끼운다.
"""

import pytest

from app.agent.v4.orchestrator import harness as harness_mod
from app.agent.v4.orchestrator.edit_ops import EditOp, EditOps
from app.agent.v4.verify.coverage_det import coverage_findings, missing_requirements
from app.schemas.recommendation import Recommendation, RecommendedAction

from tests.agent_stubs import FakeCatalog

_CATALOG = FakeCatalog()

# String/assign은 카탈로그에 있고 필수 파라미터가 value(TEXT) 하나뿐이라 세션(R7/R8)·
# 데이터플로우(R9~R11) 노이즈 없이 R2만 골라 낼 수 있다.
_PKG, _ACT = "String", "assign"


def _action(label: str, *, req_id: str | None = None, bogus: bool = False) -> dict:
    params = [{"name": "value", "value": label, "value_source": "llm"}]
    if bogus:  # R2(파라미터명 실재) major를 하나 심는다
        params.append({"name": "존재하지않는파라미터", "value": "x", "value_source": "llm"})
    a = {"order": 1, "package": _PKG, "action": _ACT, "label": label,
         "parameters": params, "children": []}
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


def _surgeon_removes(node_id: str):
    """target 노드를 지우는 EditOps만 내는 surgeon 대역 — '가장 싼 수리'의 재현."""
    calls: list[int] = []

    def _fake(messages, **kwargs):
        calls.append(1)
        return EditOps(operations=[EditOp(op="remove", target=node_id)])

    _fake.calls = calls
    return _fake


def _labels(flow: dict) -> list[str]:
    return [a["label"] for s in flow["steps"] for a in s["actions"]]


# ─────────────────────────────────────────────────────────────────────────────
# (1) 스키마 — RecommendedAction.req_id
# ─────────────────────────────────────────────────────────────────────────────

def test_req_id_is_optional_with_none_default():
    """v1~v3가 같은 스키마로 자기 산출물을 검증한다 — 필수화하면 구버전 출력이 통째로 거부된다."""
    field = RecommendedAction.model_fields["req_id"]
    assert field.is_required() is False
    assert field.default is None

    legacy = RecommendedAction(order=1, package=_PKG, action=_ACT)
    assert legacy.req_id is None


def test_req_id_survives_recommendation_roundtrip():
    """앵커는 저장·재주입(payload JSONB)을 건너야 edit 턴에서도 누락 추적이 이어진다."""
    rec = Recommendation.model_validate(_flow(_action("A", req_id="req-1")))
    assert rec.model_dump()["steps"][0]["actions"][0]["req_id"] == "req-1"


# ─────────────────────────────────────────────────────────────────────────────
# (2) 결정론 누락 추적 — coverage_det
# ─────────────────────────────────────────────────────────────────────────────

def test_missing_requirements_is_silent_without_any_anchor():
    """흐름도에 req_id가 하나도 없으면 침묵 — 정보 없이 검사하면 전량 오탐이다."""
    flow = _flow(_action("A"), _action("B"))
    spec = _spec(("req-1", "메일 발송", "must"), ("req-2", "로그 기록", "must"))

    assert missing_requirements(flow, spec) == []
    assert coverage_findings(flow, spec) == []


def test_missing_requirements_lists_unassigned_in_spec_order():
    flow = _flow(_action("A", req_id="req-2"))
    spec = _spec(("req-1", "메일 발송", "must"), ("req-2", "로그", "must"), ("req-3", "정리", "should"))

    assert missing_requirements(flow, spec) == ["req-1", "req-3"]


def test_coverage_findings_grade_must_blocker_should_minor():
    """must 미배정 = R1 환각과 동급 blocker(100). 넣기/빼기의 무게를 대칭화하는 지점."""
    flow = _flow(_action("A", req_id="req-2"))
    spec = _spec(("req-1", "메일 발송", "must"), ("req-2", "로그", "must"), ("req-3", "정리", "should"))

    graded = {f.req_id: f for f in coverage_findings(flow, spec)}
    assert graded["req-1"].severity == "blocker"
    assert graded["req-3"].severity == "minor"
    assert graded["req-1"].layer == "L2"
    assert "메일 발송" in graded["req-1"].message
    # 힌트가 '지워서 해결'을 명시적으로 닫아야 surgeon이 삭제로 도망가지 않는다.
    assert "지워서" in (graded["req-1"].fix_hint or "")


def test_coverage_findings_empty_when_spec_has_no_requirements():
    assert coverage_findings(_flow(_action("A", req_id="req-1")), {"goal": "g"}) == []
    assert coverage_findings(_flow(_action("A", req_id="req-1")), {}) == []


def test_missing_requirements_sees_nested_children_anchors():
    """컨테이너(Loop/If) 본문에 배정된 앵커도 배정이다 — 재귀를 빠뜨리면 전량 오탐."""
    parent = _action("컨테이너")
    parent["children"] = [_action("자식", req_id="req-1")]
    spec = _spec(("req-1", "본문 처리", "must"))

    assert missing_requirements(_flow(parent), spec) == []


# ─────────────────────────────────────────────────────────────────────────────
# (3) 회귀 가드 완성도 항 — 삭제 편향 (설계 §5-D)
# ─────────────────────────────────────────────────────────────────────────────

def test_refine_flow_without_spec_accepts_deletion(monkeypatch):
    """편향의 재현 — spec이 없으면 목적 함수가 정적 위반뿐이라 '지우면 개선'이 성립한다."""
    monkeypatch.setattr(harness_mod, "chat_json", _surgeon_removes("n2"))

    out = harness_mod.refine_flow(
        _flow(_action("정상", req_id="req-1"), _action("위반", req_id="req-2", bogus=True)),
        _CATALOG,
    )

    assert out["repaired"] is True
    assert _labels(out["flow"]) == ["정상"]  # 위반 액션이 사라졌다 = 재현율이 깎인다


def test_refine_flow_with_spec_rejects_deletion_of_assigned_action(monkeypatch):
    """spec을 주면 삭제가 누락 blocker(100)를 만들어 major(10) 제거를 압도한다 → 패치 폐기."""
    monkeypatch.setattr(harness_mod, "chat_json", _surgeon_removes("n2"))
    spec = _spec(("req-1", "값 지정", "must"), ("req-2", "값 기록", "must"))

    out = harness_mod.refine_flow(
        _flow(_action("정상", req_id="req-1"), _action("위반", req_id="req-2", bogus=True)),
        _CATALOG, spec=spec,
    )

    assert _labels(out["flow"]) == ["정상", "위반"], "요구를 담당하던 액션이 삭제됐다 — 완성도 항이 안 걸렸다"
    assert out["repaired"] is False
    # 무개선 2라운드로 종료 — 예산(8)을 다 태우지 않는다.
    assert len(harness_mod.chat_json.calls) == harness_mod._STOP_AFTER_NO_IMPROVE


def test_refine_flow_with_spec_still_accepts_deletion_of_unassigned_action(monkeypatch):
    """완성도 항은 '요구를 담당하는' 액션만 지킨다 — 담당 없는 위반 액션 정리는 그대로 개선이다."""
    monkeypatch.setattr(harness_mod, "chat_json", _surgeon_removes("n2"))
    spec = _spec(("req-1", "값 지정", "must"))

    out = harness_mod.refine_flow(
        _flow(_action("정상", req_id="req-1"), _action("군더더기", bogus=True)),
        _CATALOG, spec=spec,
    )

    assert out["repaired"] is True
    assert _labels(out["flow"]) == ["정상"]


def test_refine_budget_expanded_but_bounded():
    """§5-F — 예산은 늘리되 무한이 아니다. 진동은 _STOP_AFTER_NO_IMPROVE가 막는다."""
    assert harness_mod.MAX_REFINE_ROUNDS > 3
    assert harness_mod._STOP_AFTER_NO_IMPROVE >= 2


def test_refine_flow_without_spec_keeps_legacy_signature(monkeypatch):
    """하위호환 — spec 미지정 호출(edit 경로 등)은 기존 동작 그대로."""
    monkeypatch.setattr(harness_mod, "chat_json", _surgeon_removes("n1"))
    clean = _flow(_action("정상", req_id="req-1"))

    out = harness_mod.refine_flow(clean, _CATALOG)
    assert out == {"flow": clean, "violations": [], "repaired": False}


# ─────────────────────────────────────────────────────────────────────────────
# (4) 자리표시자 — 최종 폴백 (설계 §5-G·H)
# ─────────────────────────────────────────────────────────────────────────────

def _no_op_surgeon(messages, **kwargs):
    """'고칠 방법이 없다'는 정직한 신호 — 예산 소진과 같은 종착점으로 간다."""
    return EditOps(operations=[])


def test_placeholder_step_left_for_unresolved_must(monkeypatch):
    """아무것도 안 내보내는 건 선택지가 아니다 — 못 채운 요구는 흐름도에 자리로 남는다."""
    monkeypatch.setattr(harness_mod, "chat_json", _no_op_surgeon)
    spec = _spec(("req-1", "값 지정", "must"), ("req-2", "메일 발송", "must"),
                 ("req-3", "정리", "should"))

    out = harness_mod.refine_flow(_flow(_action("정상", req_id="req-1")), _CATALOG, spec=spec)

    ids = [s["step_id"] for s in out["flow"]["steps"]]
    assert ids == ["step-1", f"{harness_mod.PLACEHOLDER_STEP_PREFIX}req-2"], (
        "must 미해결에만 자리표시자를 남긴다(should는 minor라 제외)"
    )
    ph = out["flow"]["steps"][-1]
    assert ph["actions"] == []  # 액션으로 만들면 R1(환각)을 자작한다
    assert "메일 발송" in ph["description"]


def test_placeholder_keeps_recommendation_schema_intact(monkeypatch):
    """새 최상위 필드를 만들지 않는다 — output_assurance가 미지 필드를 deny로 기록한다."""
    from app.services.output_assurance import _unknown_field_findings

    monkeypatch.setattr(harness_mod, "chat_json", _no_op_surgeon)
    spec = _spec(("req-1", "값 지정", "must"), ("req-2", "메일 발송", "must"))

    out = harness_mod.refine_flow(_flow(_action("정상", req_id="req-1")), _CATALOG, spec=spec)
    rec = Recommendation.model_validate(out["flow"])

    assert _unknown_field_findings(rec.model_dump()) == []
    assert len(rec.steps) == 2 and rec.steps[-1].actions == []


def test_placeholder_is_not_duplicated_on_recheck(monkeypatch):
    """edit 경로가 같은 흐름도로 재검수한다 — stale 자리표시자가 누적되면 매 턴 불어난다."""
    monkeypatch.setattr(harness_mod, "chat_json", _no_op_surgeon)
    spec = _spec(("req-1", "값 지정", "must"), ("req-2", "메일 발송", "must"))

    once = harness_mod.refine_flow(_flow(_action("정상", req_id="req-1")), _CATALOG, spec=spec)
    twice = harness_mod.refine_flow(once["flow"], _CATALOG, spec=spec)

    assert [s["step_id"] for s in twice["flow"]["steps"]] == [s["step_id"] for s in once["flow"]["steps"]]


def test_placeholder_removed_once_requirement_is_covered(monkeypatch):
    """요구가 해소되면 자리표시자도 사라진다 — 남으면 '해결됐는데 미해결 표시'가 된다."""
    monkeypatch.setattr(harness_mod, "chat_json", _no_op_surgeon)
    spec = _spec(("req-1", "값 지정", "must"), ("req-2", "메일 발송", "must"))

    stale = harness_mod.refine_flow(_flow(_action("정상", req_id="req-1")), _CATALOG, spec=spec)["flow"]
    filled = dict(stale)
    filled["steps"] = list(stale["steps"])
    filled["steps"][0] = {
        **stale["steps"][0],
        "actions": stale["steps"][0]["actions"] + [_action("메일", req_id="req-2")],
    }

    out = harness_mod.refine_flow(filled, _CATALOG, spec=spec)

    assert [s["step_id"] for s in out["flow"]["steps"]] == ["step-1"]


@pytest.mark.parametrize("spec", [None, {}])
def test_no_placeholder_without_usable_spec(monkeypatch, spec):
    """spec이 없거나 요구가 비면 자리표시자도 없다 — 근거 없는 스캐폴드는 소음이다."""
    monkeypatch.setattr(harness_mod, "chat_json", _no_op_surgeon)
    flow = _flow(_action("위반", req_id="req-1", bogus=True))

    out = harness_mod.refine_flow(flow, _CATALOG, spec=spec)

    assert [s["step_id"] for s in out["flow"]["steps"]] == ["step-1"]
