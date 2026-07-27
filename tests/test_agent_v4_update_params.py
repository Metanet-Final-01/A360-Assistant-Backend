# -*- coding: utf-8 -*-
"""update가 파라미터를 적용하고, 표기가 바뀌면 옛 파라미터를 걷어낸다 (RPA-298 항목 A).

## 무엇을 막는가 (실측, 2026-07-27 — 교정 5라운드 추적)

    최종 가중합 210 = R13 1건(10) + R7 2건(20) + R8 2건(20) + R9 1건(10) + **R2 15건(150)**

R2 15건이 **전부 같은 원인**이었다: surgeon이 `Step/stepAction`을 실제 액션으로 갈아끼웠는데
옛 자리의 `title` 파라미터가 그대로 남았다. 그리고 그 15건은 **현행 연산 어휘로 수리 불가**였다.

  - `set_params`는 name 기준 **병합**이라 이름을 지울 수 없다.
  - `_apply_update`는 `op.parameters`를 **읽지도 않았다** — 스키마에는 필드가 있고 모델은
    실제로 채워 보내는데 적용부만 무시했다.

그래서 라운드 4는 이미 있는 이름 12개를 다시 넣었고(210→210), 라운드 5는 `action_name` 없이
package만 바꾸는 update 12개를 냈다(210→210). **어휘에 없는 수리를 프롬프트로 요구한 결과다.**

## 이 파일이 고정하는 계약

1. 표기가 바뀌면 새 스펙에 없는 파라미터를 걷는다 — 그리고 R2가 **실제로** 사라진다.
2. 걷어내는 집합 = R2가 보는 집합. 이름 없는 항목·스펙 미상은 양쪽 다 침묵한다.
3. 스펙을 모르면 아무것도 안 한다. **보고(R2)는 무시할 수 있지만 삭제는 되돌릴 수 없다.**
"""

import pytest

from app.agent.v4.orchestrator.edit_ops import EditOp, annotate_ids, apply_edit_ops
from app.agent.v4.verify.checker import (
    _check_parameters,
    derive_session_registry,
    run_flow_checks,
    spec_param_names,
)

from tests.agent_stubs import FakeCatalog

_CATALOG = FakeCatalog()


def _lookup(package: str, action: str):
    return spec_param_names(_CATALOG.get_action_schema(package, action))


def _flow(*actions: dict) -> dict:
    for i, a in enumerate(actions, 1):
        a.setdefault("order", i)
        a.setdefault("children", [])
    return {"schema_version": "1.0", "variables": [], "notes": None,
            "steps": [{"step_id": "step-1", "label": "본문", "actions": list(actions)}]}


def _step_scaffold(**extra) -> dict:
    """실측이 남긴 그 자리 — 구획(Step)에 요구가 배정돼 있고 파라미터는 title 하나."""
    return {"package": "Step", "action": "stepAction", "label": "범위 식별",
            "parameters": [{"name": "title", "value": "범위 식별", "value_source": "llm"}],
            **extra}


def _apply(flow: dict, *ops: EditOp, lookup=_lookup):
    prune: list[dict] = []
    annotate_ids(flow)
    applied, errors = apply_edit_ops(flow, list(ops), spec_params=lookup, prune_log=prune)
    return applied, errors, prune


def _params(flow: dict, idx: int = 0) -> list[str]:
    node = flow["steps"][0]["actions"][idx]
    return [p.get("name") for p in node.get("parameters") or []]


def _rules(flow: dict) -> list[str]:
    registry = derive_session_registry(_CATALOG)
    return [v.rule for v in run_flow_checks(flow, _CATALOG, registry)]


# ── A. 실측 재현 ─────────────────────────────────────────────────────────────

def test_표기가_바뀌면_스펙에_없는_옛_파라미터가_걷힌다():
    """🔴 실측 R2 15건(150점)의 원인 제거 — `Step/stepAction [title]` → `String/assign`."""
    flow = _flow(_step_scaffold(req_id="req-1"))
    applied, errors, prune = _apply(flow, EditOp(
        op="update", target="n1", package="String", action_name="assign",
        parameters=[{"name": "value", "value": "$범위$"}],
    ))

    assert (applied, errors) == (1, [])
    assert _params(flow) == ["value"], "새 스펙에 없는 title은 걷히고 value만 남는다"
    assert prune == [{"node": "n1", "to": "String/assign", "dropped": ["title"]}]


def test_R2가_실제로_사라진다():
    """단위로만 통과하고 검수와 어긋나는 회귀 — 판정기를 직접 돌려 확인한다."""
    flow = _flow(_step_scaffold())
    flow["steps"][0]["actions"][0].update(package="String", action="assign")
    assert _rules(flow).count("R2") == 1, "전제: 갈아끼우기만 하면 title이 R2를 낸다"

    fixed = _flow(_step_scaffold())
    _apply(fixed, EditOp(op="update", target="n1", package="String", action_name="assign",
                         parameters=[{"name": "value", "value": "x"}]))
    assert _rules(fixed).count("R2") == 0


def test_걷어내도_R3가_새로_생기지_않는다():
    """🔴 이 설계의 핵심 불변식 — 결함 하나를 다른 결함으로 바꾸면 이득이 0이다.

    '표기가 바뀌면 파라미터를 전부 비운다'는 더 단순하지만, 이름이 이어지는 파라미터까지
    날리고 그 자리에 required R3가 선다. required의 R3는 질문 카드가 아니라 major finding이라
    가중치가 그대로 돌아온다 — 무심한 리팩터링이 이 테스트에 걸린다.
    """
    flow = _flow(_step_scaffold())
    _apply(flow, EditOp(op="update", target="n1", package="String", action_name="assign",
                        parameters=[{"name": "value", "value": "x"}]))
    assert "R3" not in _rules(flow)


# ── B. 파괴 금지 ─────────────────────────────────────────────────────────────

def test_새_스펙에_있는_이름은_남는다():
    """R17(세션 핸들 패키지 불일치) 수리는 package만 바꾼다 — sessionName이 날아가면 R3가 선다."""
    flow = _flow({
        "package": "Excel advanced", "action": "excelAdvancedPackageCloseAction", "label": "닫기",
        "parameters": [{"name": "sessionName", "value": "Default", "value_source": "llm"}],
    })
    _, _, prune = _apply(flow, EditOp(
        op="update", target="n1", package="Excel advanced",
        action_name="excelAdvancedPackageSaveWorkbookAction",
    ))

    assert _params(flow) == ["sessionName"]
    assert prune == []


def test_user_값은_dict_그대로_보존된다():
    """사용자가 넣은 값은 표기를 갈아끼워도 살아남는다 — 이름이 새 스펙에 이어지는 한.

    되돌릴 수 없기 때문에 중요하다: edit 경로의 `_restore_user_values`가 읽는 collect()는
    **이미 정리된 흐름도**를 훑으므로, 여기서 지운 값은 애초에 저장 대상에 들어가지 않는다.
    """
    flow = _flow({
        "package": "Excel advanced", "action": "excelAdvancedPackageCloseAction", "label": "닫기",
        "parameters": [{"name": "sessionName", "value": "내세션", "value_source": "user"}],
    })
    _apply(flow, EditOp(op="update", target="n1",
                        action_name="excelAdvancedPackageSaveWorkbookAction"))

    assert flow["steps"][0]["actions"][0]["parameters"] == [
        {"name": "sessionName", "value": "내세션", "value_source": "user"}
    ]


def test_이름_없는_파라미터는_걷지_않는다():
    """R2는 이름 없는 항목을 판정 대상에서 뺀다(checker: `if p.get("name")`).

    여기서 지우면 '걷어내는 집합 = R2가 보는 집합' 불변식이 깨진다 — 검수가 침묵하는 것을
    교정이 조용히 파괴하는 상태다.
    """
    flow = _flow({
        "package": "Step", "action": "stepAction", "label": "구획",
        "parameters": [{"name": None, "value": "?"}, {"name": "title", "value": "t"}],
    })
    _apply(flow, EditOp(op="update", target="n1", package="String", action_name="assign"))

    assert _params(flow) == [None], "title만 걷히고 이름 없는 항목은 그대로"


@pytest.mark.parametrize("params", [{"a": 1}, "없음", 3])
def test_스펙_parameters가_리스트가_아니면_손대지_않는다(params):
    """🔴 frozenset()은 '파라미터 없는 액션 확정'이라 전량 삭제가 맞다 — '모름'과 같은 표현을
    쓰면 dict·문자열 슬립 하나에 노드 파라미터가 통째로 지워진다."""
    assert spec_param_names({"package": "P", "action": "A", "parameters": params}) is None


@pytest.mark.parametrize("lookup", [
    None,                                    # 콜백 자체가 없음(prune_params=False 경로)
    lambda p, a: None,                       # 스펙 부재 / params_unknown
])
def test_스펙을_모르면_손대지_않는다(lookup):
    """'모름 → 침묵'. R3의 required tri-state·checker의 params_unknown과 같은 원칙이다."""
    flow = _flow(_step_scaffold())
    _, _, prune = _apply(flow, EditOp(op="update", target="n1", package="String",
                                      action_name="assign"), lookup=lookup)

    assert _params(flow) == ["title"]
    assert prune == []


def test_부분_사용자_카탈로그에서는_정리하지_않는다():
    """🔴 사용자가 5개 중 2개만 설명한 액션 — 나머지 3개를 결정론으로 지우면 안 된다.

    편집 경로에는 회귀 가드도 복원 경로도 없다. checker가 같은 데이터로 R2를 내는 것과는
    다른 문제다 — **보고는 사용자가 무시할 수 있지만 삭제는 되돌릴 수 없다.**
    게이팅은 호출부(edit.py의 prune_params=ctx.is_a360)가 하고, 여기서는 게이팅이 꺼졌을 때
    실제로 아무 일도 안 일어나는지를 본다.
    """
    from app.agent.v4.orchestrator.generate import UserCatalogAction

    partial = UserCatalogAction.model_validate({
        "package": "사내ERP", "action": "전표등록",
        "parameters": [{"name": "전표번호"}, {"name": "금액"}],
    }).as_spec()
    assert spec_param_names(partial) == {"전표번호", "금액"}, "전제: 부분 목록이 그대로 잡힌다"

    flow = _flow({"package": "사내ERP", "action": "조회", "label": "조회",
                  "parameters": [{"name": "전표번호", "value": "1"},
                                 {"name": "담당자", "value": "홍", "value_source": "user"}]})
    _apply(flow, EditOp(op="update", target="n1", action_name="전표등록"), lookup=None)

    assert _params(flow) == ["전표번호", "담당자"]


def test_표기가_그대로면_병합만_한다():
    """요청하지 않은 삭제 금지 — 라벨·값만 바꾸는 update가 기존 데이터를 지우면 놀랍다."""
    flow = _flow(_step_scaffold())
    _, _, prune = _apply(flow, EditOp(op="update", target="n1", label="새 라벨",
                                      parameters=[{"name": "title", "value": "새 제목"}]))

    assert _params(flow) == ["title"]
    assert prune == []


def test_표기_안_바꾸는_update가_스펙_밖_이름을_꽂을_수_있다():
    """핀 테스트 — **고치지 않기로 한 구멍**을 코드에 드러낸다.

    표기를 안 바꾸는 update는 정리 필터를 지나지 않으므로 스펙에 없는 이름이 그대로 남아
    영구 R2가 된다. 여기서 막지 않는 이유: 라벨만 바꾸는 update가 기존 파라미터를 지우면
    놀랍고, 그 R2는 compose가 만든 별건이라 여기서 숨기면 원인이 사라진다.
    실패가 아니라 **현재 동작의 문서화**다 — 동작을 바꾸려면 이 테스트를 함께 바꿔라.
    """
    flow = _flow({"package": "String", "action": "assign", "label": "지정",
                  "parameters": [{"name": "value", "value": "x"}]})
    _apply(flow, EditOp(op="update", target="n1", label="새 라벨",
                        parameters=[{"name": "없는파라미터", "value": "y"}]))

    assert _params(flow) == ["value", "없는파라미터"]
    assert _rules(flow).count("R2") == 1


# ── C. update ↔ set_params ───────────────────────────────────────────────────

def test_update와_set_params의_순서가_결과를_바꾸지_않는다():
    """두 연산이 같은 병합 함수를 쓴다 — 갈라지면 어느 연산으로 넣었는지에 따라 결과가 달라진다."""
    a = _flow(_step_scaffold())
    _apply(a, EditOp(op="update", target="n1", package="String", action_name="assign"),
           EditOp(op="set_params", target="n1", parameters=[{"name": "value", "value": "x"}]))

    b = _flow(_step_scaffold())
    _apply(b, EditOp(op="update", target="n1", package="String", action_name="assign",
                     parameters=[{"name": "value", "value": "x"}]))

    assert a["steps"] == b["steps"]


def test_같은_이름은_나중_연산이_이긴다():
    flow = _flow({"package": "String", "action": "assign", "label": "지정",
                  "parameters": [{"name": "value", "value": "처음"}]})
    _apply(flow, EditOp(op="set_params", target="n1", parameters=[{"name": "value", "value": "나중"}]))

    assert flow["steps"][0]["actions"][0]["parameters"][0]["value"] == "나중"


def test_병합은_기존_label을_보존한다():
    """기존의 조용한 데이터 손실 — set_params가 dict를 3키로 재구성하며 사람용 라벨을 버렸다.

    edit 경로는 model_dump()한 흐름도를 넣으므로(ActionParameter에 label이 있다) 사용자가
    화면에서 보던 파라미터 이름이 수정 한 번에 사라졌다.
    """
    flow = _flow({"package": "String", "action": "assign", "label": "지정",
                  "parameters": [{"name": "value", "label": "값", "value": "처음"}]})
    _apply(flow, EditOp(op="set_params", target="n1", parameters=[{"name": "value", "value": "나중"}]))

    assert flow["steps"][0]["actions"][0]["parameters"][0]["label"] == "값"


def test_update가_실은_값은_value_source가_llm으로_고정된다():
    """사용자 입력은 update로 오지 않는다.

    LLM이 지어낸 값에 "user"가 붙으면 edit 경로의 `_restore_user_values`가 교정 결과에 그
    값을 **다시 고정**해, 검수·교정이 영원히 못 건드리는 값이 된다.
    """
    flow = _flow({"package": "String", "action": "assign", "label": "지정", "parameters": []})
    _apply(flow, EditOp(op="update", target="n1", label="x",
                        parameters=[{"name": "value", "value": "지어냄", "value_source": "user"}]))

    assert flow["steps"][0]["actions"][0]["parameters"][0]["value_source"] == "llm"


# ── D. 반환값·관측 ───────────────────────────────────────────────────────────

def test_parameters만_든_update도_적용으로_센다():
    """예전엔 False를 돌려줘 apply_edit_ops가 '대상 노드를 못 찾았다'는 **거짓 사유**를 남겼고,
    edit 경로는 errors가 비지 않으면 "반영하지 못했어요"로 저하했다."""
    flow = _flow({"package": "String", "action": "assign", "label": "지정", "parameters": []})
    applied, errors, _ = _apply(flow, EditOp(
        op="update", target="n1", parameters=[{"name": "value", "value": "x"}]))

    assert (applied, errors) == (1, [])


def test_notes는_errors에_섞이지_않는다():
    """정상 정리를 실패로 보고하면 edit 경로가 사용자에게 "반영하지 못했어요"를 낸다."""
    flow = _flow(_step_scaffold())
    applied, errors, prune = _apply(flow, EditOp(
        op="update", target="n1", package="String", action_name="assign"))

    assert (applied, errors) == (1, [])
    assert prune and prune[0]["dropped"] == ["title"]


# ── E. 정합성 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("pkg,act,given", [
    ("String", "assign", ["value", "title", "없는것"]),
    ("Step", "stepAction", ["title", "value"]),
    ("Email", "sendMail", ["to", "subject", "없는것", "attachment"]),
    ("Error handler", "errorHandlerTry", ["아무거나"]),
])
def test_spec_param_names는_R2의_판정_집합과_같다(pkg, act, given):
    """🔴 두 곳이 각자 스펙을 읽으면 "걷어냈는데 R2가 남는다"가 언젠가 생긴다.

    행동 등가로 못 박는다: R2가 지목하는 이름 = 준 이름 − spec_param_names.
    """
    spec = _CATALOG.get_action_schema(pkg, act)
    action = {"package": pkg, "action": act,
              "parameters": [{"name": n, "value": "x"} for n in given]}

    flagged = {v.param for v in _check_parameters(action, spec, "actions[0]") if v.rule == "R2"}
    assert flagged == set(given) - (spec_param_names(spec) or set())


def test_이름_없는_스펙_행이_있어도_검수가_죽지_않는다():
    """잠복 크래시 — name 키 없는 스펙 행은 KeyError, 비-dict 행은 TypeError로 검수를 통째로
    죽였다. 카탈로그 한 행의 흠이 검수 전체를 무력화하는 쪽이 더 나쁘다."""
    spec = {"package": "P", "action": "A",
            "parameters": [{"label": "이름 없음"}, "문자열", {"name": "ok", "required": False}]}
    action = {"package": "P", "action": "A", "parameters": [{"name": "ok", "value": "x"}]}

    assert _check_parameters(action, spec, "actions[0]") == []
    assert spec_param_names(spec) == {"ok"}
