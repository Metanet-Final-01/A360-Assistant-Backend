"""v4 검수 게이트 — 동작 옵션 승격(§5-E)과 타 솔루션 구조 검사 게이트(§6.6) (RPA-298).

두 결정을 못 박는다.

1. **동작 옵션 값 누락 = 결함.** 필수 파라미터 미충족(R3)을 전부 질문 카드로 보내면
   비전문가 사용자에게 "Read-write 모드인가요?"·"세션 이름을 정해 주세요" 같은 답할 수
   없는 질문이 간다. 카탈로그 파라미터 `type`으로 동작 옵션·내부 식별자(우리가 확정)와
   업무 데이터(사용자만 안다)를 결정론으로 가른다.

2. **구조·세션 검사는 A360 전용.** 어휘는 이식되지만 구조는 이식되지 않는다 — UiPath의
   Sequence·TryCatch 본문이 R6 major로 전량 오탐이 되고, 스코프 자동 종료 때문에 R8이
   올바른 자동화를 결함 판정한다. `is_a360=False`면 R6/R7/R8/R12/R13/R14를 끈다.

여기서 쓰는 카탈로그는 이 파일 안의 최소 픽스처다 — 공용 스텁(agent_stubs)에 의존하면
그쪽 픽스처가 바뀔 때 게이트 테스트가 무관한 이유로 깨진다.
"""

import pytest

from app.agent.knowledge import derive as knowledge_derive
from app.agent.v4.verify.checker import Violation, run_flow_checks
from app.agent.v4.verify.findings import from_violations, weight

# ─────────────────────────────────────────────────────────────────────────────
# 픽스처 — 세션 어휘 유도가 성립하는 최소 카탈로그
# ─────────────────────────────────────────────────────────────────────────────
# 'Excel advanced'가 SESSION 타입 파라미터를 갖고 있어야 derive_session_registry의
# 패키지 게이팅을 통과한다(게이팅이 없으면 File/Open까지 opener로 잡힌다 — derive 참조).
_SCHEMAS: list[dict] = [
    {
        "package": "Excel advanced", "action": "Open",
        "parameters": [
            {"name": "session", "label": "세션 이름", "type": "SESSION", "required": True},
            {"name": "filePath", "label": "파일 경로", "type": "FILE", "required": True},
        ],
    },
    {
        "package": "Excel advanced", "action": "Close",
        "parameters": [{"name": "session", "type": "SESSION", "required": True}],
    },
    {
        "package": "Excel advanced", "action": "Get cell",
        "parameters": [
            {"name": "session", "type": "SESSION", "required": True},
            {"name": "cellAddress", "label": "셀 주소", "type": "TEXT", "required": True},
            {"name": "readMode", "label": "읽기 모드", "type": "SELECT", "required": True},
        ],
    },
    {"package": "Log", "action": "Write", "parameters": []},
    # 타 솔루션(UiPath 계열) 어휘 — A360 CONTAINER_PACKAGES에 없는 컨테이너.
    {"package": "Sequence", "action": "Run", "parameters": []},
    {"package": "TryCatch", "action": "Execute", "parameters": []},
    # A360 제어 흐름 — R13/R14 게이트 확인용.
    {"package": "Error handler", "action": "errorHandlerTry", "parameters": []},
]
_INDEX = {(s["package"], s["action"]): s for s in _SCHEMAS}


class _Catalog:
    def get_action_schema(self, package, action):
        return _INDEX.get((package, action))

    def iter_action_schemas(self):
        yield from _SCHEMAS


@pytest.fixture()
def catalog():
    # 유도 캐시는 id(catalog) 기준이라 인스턴스가 재사용되면 이전 테스트 결과가 샌다.
    knowledge_derive.clear_cache()
    return _Catalog()


def _act(package, action, params=None, children=None):
    return {
        "package": package, "action": action, "label": action,
        "parameters": params or [], "children": children or [],
    }


def _flow(*actions, step_id="step-1"):
    return {"steps": [{"step_id": step_id, "actions": list(actions)}]}


def _rules(violations):
    return [v.rule for v in violations]


# ─────────────────────────────────────────────────────────────────────────────
# (A) 동작 옵션 값 누락 = 결함 (설계 §5-E)
# ─────────────────────────────────────────────────────────────────────────────

def _r3(param, ptype):
    """R3 위반 한 건 — checker가 만드는 것과 같은 모양(spec_excerpt에 type)."""
    return Violation(
        "R3", "actions[0]", f"필수 파라미터 '{param}'에 값이 없습니다.",
        package="Excel advanced", action="Get cell", param=param,
        spec_excerpt={"type": ptype, "required": True},
    )


def test_select_option_missing_is_defect_not_card():
    """선택지가 고정된 동작 옵션(SELECT)은 우리가 확정해야 한다 — 카드로 내보내지 않는다."""
    findings, cards = from_violations([_r3("readMode", "SELECT")])
    assert not cards
    assert [(f.rule, f.severity, f.layer) for f in findings] == [("R3", "major", "L0")]
    assert "묻지 말고" in (findings[0].fix_hint or "")


def test_business_data_missing_stays_card():
    """업무 데이터(셀 주소·파일 경로·계정)는 사용자만 안다 — 결함이 아니라 질문 카드다."""
    for ptype in ("TEXT", "FILE", "NUMBER", "CREDENTIAL", "LIST", "DICTIONARY"):
        findings, cards = from_violations([_r3("cellAddress", ptype)])
        assert not findings, f"{ptype}이(가) 결함으로 승격됐다"
        assert len(cards) == 1, f"{ptype} 카드가 사라졌다"


def test_internal_identifier_missing_is_defect():
    """세션 핸들·산출 변수 이름은 흐름도 내부 명명 — 비전문가에게 물을 수 없다."""
    for ptype in ("SESSION", "VARIABLE"):
        findings, cards = from_violations([_r3("session", ptype)])
        assert not cards, f"{ptype}이(가) 카드로 나갔다"
        assert findings[0].severity == "major"
        assert "내부 식별자" in (findings[0].fix_hint or "")


def test_unknown_param_type_stays_card():
    """타입을 모르면 카드로 떨어진다 — 근거 없이 결함으로 올리면 아무 값이나 채우게 된다.

    타 솔루션 카탈로그(대화 추출)는 파라미터 타입이 없는 경우가 많다(§6.6: UiPath·Blue
    Prism은 enum 자체가 희소) — 그 경로가 여기로 떨어진다.
    """
    for excerpt in ({"required": True}, {"type": None}, {"type": "UNKNOWN"}, {}):
        v = Violation("R3", "actions[0]", "값 없음", param="p", spec_excerpt=excerpt)
        findings, cards = from_violations([v])
        assert not findings and len(cards) == 1, f"spec_excerpt={excerpt}"


def test_param_type_survives_the_dict_shuttle(catalog):
    """실사용 경로 회귀 가드 — harness는 Violation을 as_dict로 눕혀 findings에 넘긴다.

    as_dict가 파라미터 타입을 떨어뜨리면 판별이 전부 '타입 미상'이 되어 동작 옵션도
    카드로 흘러간다. 이 테스트가 그 회귀를 잡는다.
    """
    flow = _flow(_act("Excel advanced", "Get cell", [
        {"name": "session", "value": "wb"},
        {"name": "cellAddress", "value": "A1"},
        # readMode(SELECT) 미충족 — 동작 옵션
    ]))
    r3 = [v for v in run_flow_checks(flow, catalog) if v.rule == "R3"]
    assert {v.param for v in r3} == {"readMode"}

    class _Shuttle:  # harness.from_violations_dicts와 같은 모양
        def __init__(self, d):
            self._d = d

        def as_dict(self):
            return self._d

    findings, cards = from_violations([_Shuttle(v.as_dict()) for v in r3])
    assert not cards
    assert [f.rule for f in findings] == ["R3"]
    assert weight(findings) == 10  # major — 액션 삭제(위반 제거)보다 무겁게 남는다


def test_business_data_and_option_split_in_one_action(catalog):
    """한 액션에 둘이 섞여 있으면 갈라진다 — 옵션은 결함, 업무 데이터는 카드."""
    flow = _flow(_act("Excel advanced", "Get cell", []))  # 필수 3개 전부 미충족
    r3 = [v for v in run_flow_checks(flow, catalog) if v.rule == "R3"]
    findings, cards = from_violations(r3)
    assert {f.rule for f in findings} == {"R3"}
    assert {v.param for v in cards} == {"cellAddress"}          # TEXT — 사용자만 안다
    assert {v.param for v in r3} - {"cellAddress"} == {"session", "readMode"}


# ─────────────────────────────────────────────────────────────────────────────
# (B) 구조·세션 검사 A360 게이트 (설계 §6.6)
# ─────────────────────────────────────────────────────────────────────────────

def test_foreign_container_body_is_not_r6_violation(catalog):
    """UiPath의 Sequence 본문은 정상이다 — A360 컨테이너 어휘로 재면 전량 major 오탐."""
    flow = _flow(_act("Sequence", "Run", children=[_act("Log", "Write")]))
    assert "R6" in _rules(run_flow_checks(flow, catalog))
    assert "R6" not in _rules(run_flow_checks(flow, catalog, is_a360=False))


def test_foreign_session_is_not_r7_r8_violation(catalog):
    """UiPath는 스코프가 자동 종료된다 — 닫는 액션이 없는 게 정상인데 R8이 결함 판정한다."""
    flow = _flow(
        _act("Excel advanced", "Open", [{"name": "session", "value": "wb"}]),
        _act("Log", "Write"),
    )
    assert "R8" in _rules(run_flow_checks(flow, catalog))
    foreign = _rules(run_flow_checks(flow, catalog, is_a360=False))
    assert "R8" not in foreign and "R7" not in foreign


def test_foreign_control_flow_is_not_r13_r14_violation(catalog):
    """R13/R14는 'Error handler'·'Loop' 패키지명 리터럴에 걸린다 — 명시적으로 끈다."""
    flow = _flow(
        _act("Error handler", "errorHandlerTry", children=[_act("Log", "Write")]),
        _act("Log", "Write"),  # Try 다음 형제가 Catch가 아님 → R13
    )
    assert "R13" in _rules(run_flow_checks(flow, catalog))
    assert "R13" not in _rules(run_flow_checks(flow, catalog, is_a360=False))


def test_foreign_flow_is_not_told_to_add_a360_error_handler(catalog):
    """R12a는 A360의 'Error handler' 패키지를 처방한다 — 타 솔루션에 넣으면 R1 blocker가 된다."""
    flow = _flow(*[_act("Log", "Write") for _ in range(6)])
    assert "R12" in _rules(run_flow_checks(flow, catalog))
    assert "R12" not in _rules(run_flow_checks(flow, catalog, is_a360=False))


def test_vocabulary_checks_still_run_for_foreign_solution(catalog):
    """게이트는 구조·세션만 끈다 — 어휘(R1~R5)는 타 솔루션에서도 그대로 검사한다."""
    flow = _flow(
        _act("Sequence", "Run", children=[_act("없는패키지", "없는액션")]),
        _act("Excel advanced", "Get cell", [{"name": "오타파라미터", "value": "x"}]),
    )
    rules = _rules(run_flow_checks(flow, catalog, is_a360=False))
    assert "R1" in rules  # 환각 액션 — 타 솔루션이라도 카탈로그에 없으면 위반
    assert "R2" in rules  # 스펙에 없는 파라미터 이름
    assert "R3" in rules  # 필수 파라미터 미충족


def test_is_a360_defaults_to_true_for_backward_compatibility(catalog):
    """호출부가 인자를 안 넘겨도 기존 A360 동작이 유지된다 (harness 하위호환)."""
    flow = _flow(_act("Sequence", "Run", children=[_act("Log", "Write")]))
    assert _rules(run_flow_checks(flow, catalog)) == _rules(
        run_flow_checks(flow, catalog, None, is_a360=True)
    )
