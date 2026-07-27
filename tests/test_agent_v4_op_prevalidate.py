# -*- coding: utf-8 -*-
"""연산 사전 검증 — 환각 표기를 적용 전에 걸러낸다 (RPA-298).

## 무엇을 막는가 (실측, 2026-07-27 — 교정 라운드 추적으로 드러남)

    라운드 1 — discarded  가중합 526 → 556  연산 7개 전부 적용
    ops: update n8 → Excel advanced/Paste cell   ← 이 액션은 **없다**
         update n10 → Microsoft 365 Excel/Format cell
         remove n16 · update n17 → Excel advanced/Close…  …

surgeon은 R17(세션 핸들 패키지 불일치)을 실제로 고쳤다(2건 → 1건, −100). 그런데 같은
패치에 든 `Excel advanced/Paste cell`이 R1(blocker, +100)을 만들어 순증 +30이 됐고,
회귀 가드가 **패치를 통째로 폐기**했다 — 정상 연산 6개가 나쁜 1개에 끌려 죽었다.

R1이 사후에 하는 것과 **같은 카탈로그 조회**를 사전에 한다. 환각 자체는 못 막는다
(surgeon은 여전히 없는 표기를 제안한다) — 막는 것은 그 하나가 나머지를 죽이는 구조다.
"""

import pytest

from app.agent.v4.orchestrator import harness
from app.agent.v4.orchestrator.edit_ops import (
    EditOp,
    EditOps,
    annotate_ids,
    drop_unknown_action_ops,
    op_notations,
)

# 실재하는 표기만 담은 최소 카탈로그
_REAL = {
    ("Microsoft 365 Excel", "Paste cell"),
    ("Microsoft 365 Excel", "Format cell"),
    ("Excel advanced", "Open"),
    ("Excel advanced", "Close action in Excel advanced package"),
    ("Error handler", "Try"),
    ("Error handler", "Catch"),
}


def _exists(pkg, act):
    return (pkg, act) in _REAL


def _flow():
    return annotate_ids({"steps": [{"step_id": "step-1", "label": "s", "actions": [
        {"package": "Microsoft 365 Excel", "action": "Paste cell", "label": "붙여넣기", "children": []},
        {"package": "Excel advanced", "action": "Open", "label": "열기", "children": []},
    ]}]})


# ── 걸러내기 ─────────────────────────────────────────────────────────────────

def test_실측_결함이_잡힌다():
    """🔴 'Excel advanced/Paste cell'은 없다 — 붙여넣기는 M365·Google Sheets에만 있다."""
    ops = [EditOp(op="update", target="n1", package="Excel advanced", action_name="Paste cell")]
    kept, dropped = drop_unknown_action_ops(_flow(), ops, _exists)

    assert kept == []
    assert "Excel advanced/Paste cell" in dropped[0]


def test_정상_연산은_살아남는다():
    """🔴 이 수정의 요점 — 나쁜 1개 때문에 좋은 6개가 죽지 않게 한다."""
    ops = [
        EditOp(op="update", target="n1", package="Excel advanced", action_name="Paste cell"),  # 환각
        EditOp(op="update", target="n2", package="Excel advanced",
               action_name="Close action in Excel advanced package"),                          # 정상
        EditOp(op="remove", target="n2"),
    ]
    kept, dropped = drop_unknown_action_ops(_flow(), ops, _exists)

    assert [o.op for o in kept] == ["update", "remove"]
    assert len(dropped) == 1


def test_딸린_연산도_함께_버린다():
    """🔴 표기 교체와 파라미터 설정은 짝으로 나온다.

    앞을 버리고 뒤만 남기면 **바뀌지 않은 액션에 다른 액션의 파라미터를 꽂아** R2를
    새로 만든다 — 고치려다 다른 결함을 만드는 꼴이다.
    """
    ops = [
        EditOp(op="update", target="n1", package="Excel advanced", action_name="Paste cell"),
        EditOp(op="set_params", target="n1", parameters=[{"name": "Session name", "value": "$s$"}]),
        EditOp(op="set_params", target="n2", parameters=[{"name": "Session name", "value": "$s$"}]),
    ]
    kept, dropped = drop_unknown_action_ops(_flow(), ops, _exists)

    assert [o.target for o in kept] == ["n2"], "같은 대상(n1)만 함께 빠진다"
    assert len(dropped) == 2


# ── 표기 추출 경계 ───────────────────────────────────────────────────────────

def test_update가_한쪽만_바꾸면_나머지는_현재값과_합친다():
    """package만 바꾸면 action은 그대로 남는다 — 결과 표기로 판정해야 맞다."""
    flow = _flow()
    assert op_notations(flow, EditOp(op="update", target="n1", package="Excel advanced")) == [
        ("Excel advanced", "Paste cell")
    ]
    assert op_notations(flow, EditOp(op="update", target="n2", action_name="Nope")) == [
        ("Excel advanced", "Nope")
    ]


def test_라벨만_바꾸는_update는_표기를_안_건드린다():
    assert op_notations(_flow(), EditOp(op="update", target="n1", label="새 이름")) == []


def test_insert와_wrap의_표기도_본다():
    flow = _flow()
    assert op_notations(flow, EditOp(op="insert", anchor="n1", position="after",
                                     action={"package": "Excel advanced", "action": "Open"})) == [
        ("Excel advanced", "Open")
    ]
    assert op_notations(flow, EditOp(op="wrap", targets=["n1"],
                                     container={"package": "Error handler", "action": "Try"},
                                     siblings_after=[{"package": "Error handler", "action": "Catch"}])) == [
        ("Error handler", "Try"), ("Error handler", "Catch")
    ]


@pytest.mark.parametrize("op", [
    EditOp(op="remove", target="n1"),
    EditOp(op="move", target="n1", anchor="n2", position="after"),
    EditOp(op="set_flow", notes="메모"),
])
def test_표기를_안_쓰는_연산은_통과한다(op):
    kept, dropped = drop_unknown_action_ops(_flow(), [op], _exists)
    assert kept == [op] and dropped == []


def test_없는_노드를_겨냥한_update는_판정하지_않는다():
    """어차피 적용도 실패한다 — 여기서 버리면 '적용 실패' 신호가 사라져 원인이 흐려진다."""
    assert op_notations(_flow(), EditOp(op="update", target="없음", package="X")) == []


def test_환각_update는_동봉한_파라미터째_버려진다():
    """표기 교체와 파라미터 설정은 한 연산 안에 함께 온다(update가 parameters를 받는다).

    표기만 걸러내고 파라미터를 남기면 **바뀌지 않은 액션에 다른 액션의 파라미터를 꽂아**
    R2를 새로 만든다 — 고치려다 다른 결함을 만드는 꼴이다. 연산 단위로 통째 버린다.
    """
    kept, dropped = drop_unknown_action_ops(_flow(), [EditOp(
        op="update", target="n1", package="Excel advanced", action_name="Paste cell",
        parameters=[{"name": "Source cell selection", "value": "A1"}],
    )], _exists)

    assert kept == [] and len(dropped) == 1


def test_같은_대상에_update가_둘이면_뒤의_판정이_앞의_효과를_본다():
    """🔴 사전 검증은 **아무 연산도 적용되기 전** 흐름도로 판정한다 — 같은 노드에 update가
    둘 오면 뒤의 판정이 앞의 효과를 못 본다.

    예전에는 그 결과가 '연산 하나를 헛되이 버림'이었다. 지금은 update가 **판정된 표기 기준으로
    파라미터를 지우므로**, 사전 검증이 본 적 없는 표기가 삭제 기준이 되면 파괴적이다.

    여기서 n1은 `Microsoft 365 Excel/Paste cell`이다. 첫 연산이 `Excel advanced/Open`으로
    바꾼 뒤 둘째가 action만 `Paste cell`로 되돌리면 결과는 **없는 표기**가 된다 — 투영이
    없으면 옛 package로 판정해 통과시킨다.
    """
    ops = [
        EditOp(op="update", target="n1", package="Excel advanced", action_name="Open"),
        EditOp(op="update", target="n1", action_name="Paste cell"),
    ]
    kept, dropped = drop_unknown_action_ops(_flow(), ops, _exists)

    assert [o.action_name for o in kept] == ["Open"]
    assert "Excel advanced/Paste cell" in dropped[0]


# ── 루프 배선 ────────────────────────────────────────────────────────────────

@pytest.fixture
def events(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(harness, "emit", lambda ev: seen.append(ev))
    monkeypatch.setattr(harness, "emit_flow_frame", lambda *a, **k: None)
    return seen


class _Catalog:
    # ⚠ 실재 표기에 `parameters: []`를 준다 = "파라미터 없는 액션 확정". 표기를 갈아끼우는
    # update가 오면 _retarget_params가 그 노드의 파라미터를 **전부** 걷는다 — 아래 픽스처는
    # 파라미터가 없어 무해하지만, 파라미터 있는 흐름도를 새로 쓸 거면 스텁을 함께 고쳐야 한다.
    def get_action_schema(self, pkg, act):
        return {"package": pkg, "action": act, "parameters": []} if (pkg, act) in _REAL else None

    def iter_action_schemas(self):
        return iter([{"package": p, "action": a, "parameters": []} for p, a in sorted(_REAL)])


def _run(monkeypatch, ops, violations_seq, **kw):
    monkeypatch.setattr(harness, "chat_json", lambda *a, **k: EditOps(operations=ops))
    seq = list(violations_seq)
    monkeypatch.setattr(harness, "collect_violations", lambda *a, **k: seq.pop(0) if seq else [])
    flow = {"steps": [{"step_id": "step-1", "label": "s", "actions": [
        {"package": "Microsoft 365 Excel", "action": "Paste cell", "label": "붙여넣기", "children": []},
        {"package": "Excel advanced", "action": "Open", "label": "열기", "children": []},
    ]}]}
    return harness.refine_flow(flow, _Catalog(), max_rounds=1, **kw)


def _viol(rule, loc="actions[0]"):
    return {"rule": rule, "location": loc, "message": f"{rule} 위반", "step_id": "step-1"}


def test_환각_연산만_빠지고_나머지는_적용된다(events, monkeypatch):
    """🔴 실측 라운드 1의 재현 — 나쁜 1개를 빼면 좋은 것들이 살아 채택될 수 있다."""
    _run(monkeypatch, [
        EditOp(op="update", target="n1", package="Excel advanced", action_name="Paste cell"),  # 환각
        EditOp(op="update", target="n2", label="이름 변경"),                                    # 정상
    ], [[_viol("R7"), _viol("R8")], [_viol("R7")]])

    (r,) = [e for e in events if "round" in (e.get("data") or {})]
    assert r["data"]["outcome"] == "accepted"
    assert r["data"]["proposed"] == 2
    assert r["data"]["applied"] == 1
    assert r["data"]["dropped"] and "Excel advanced/Paste cell" in r["data"]["dropped"][0]


def test_전부_환각이면_적용하지_않는다(events, monkeypatch):
    """가짜 성공 방지 — 낼 것이 전부 환각이면 '연산 없음'과 같은 상태다."""
    _run(monkeypatch, [
        EditOp(op="update", target="n1", package="Excel advanced", action_name="Paste cell"),
    ], [[_viol("R7")]])

    (r,) = [e for e in events if "round" in (e.get("data") or {})]
    assert r["data"]["outcome"] == "all_dropped"
    assert r["data"]["proposed"] == 1
