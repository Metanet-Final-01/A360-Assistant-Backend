# -*- coding: utf-8 -*-
"""폐기 라운드 되먹임과 '순효과 0' 판정 (RPA-298 항목 C).

## 무엇을 막는가 (실측, 2026-07-27 — 교정 5라운드 추적)

    라운드 1  discarded  526 → 556
    라운드 2  accepted   516 → 260
    라운드 3  discarded  260 → 290
    라운드 4  accepted   290 → 210   ← 실제로는 `set_params ["Title"]` ×12, **이미 있는 이름**
    라운드 5  accepted   210 → 210   ← `action_name` 없는 update ×12

라운드 4·5는 흐름도를 **한 글자도 바꾸지 않았다.** 그런데 `applied=12`가 돌아오고
`new_weight == current_weight`라, extras_pending이 살아 있으면 `<=` 완화로 **채택**돼
`repaired=True`가 섰다 — 바뀌지도 않은 흐름도로 L2 재채점 + L3 재실행이 돌았다.

그리고 왜 같은 답이 나왔나: **프롬프트가 직전과 바이트 단위로 같았다.** 아웃라인·findings·
스펙 발췌·수리 어휘가 그대로였고, "이미 이걸 시도했고 이래서 안 됐다"가 어디에도 없었다.

## 이 파일이 고정하는 계약

1. 순효과 0은 `work == current` **결과 비교**로 잡는다(연산별 술어가 아니다 — 술어는 적용부
   필드 집합의 거울이라 A가 필드를 늘리는 순간 조용히 어긋나 **고칠 수 있는 연산을 버린다**).
2. 반영되지 않은 라운드는 다음 프롬프트 **맨 뒤**에 되먹인다(앞부분 프리픽스 캐시 보호).
3. 채택되면 되먹임을 비운다 — renumber로 id가 다시 매겨져 옛 좌표가 **틀린 자리**를 가리킨다.
"""

import pytest

from app.agent.v4.orchestrator import harness
from app.agent.v4.orchestrator.edit_ops import EditOp, EditOps
from app.agent.v4.verify.findings import Finding

_REAL = {("String", "assign"), ("Step", "stepAction")}


class _Catalog:
    def get_action_schema(self, pkg, act):
        if (pkg, act) not in _REAL:
            return None
        params = ([{"name": "value", "type": "TEXT", "required": True}] if act == "assign"
                  else [{"name": "title", "type": "TEXT", "required": False}])
        return {"package": pkg, "action": act, "parameters": params}

    def iter_action_schemas(self):
        return iter([self.get_action_schema(p, a) for p, a in sorted(_REAL)])


def _flow():
    return {"steps": [{"step_id": "step-1", "label": "s", "actions": [
        {"order": 1, "package": "Step", "action": "stepAction", "label": "구획",
         "parameters": [{"name": "title", "value": "구획", "value_source": "llm"}], "children": []},
    ]}]}


def _viol(rule, loc="actions[0]"):
    return {"rule": rule, "location": loc, "message": f"{rule} 위반", "step_id": "step-1"}


def _swap():
    """실측이 필요로 했던 그 연산 — 구획을 실제 액션으로 갈아끼운다."""
    return EditOp(op="update", target="n1", package="String", action_name="assign")


@pytest.fixture
def bench(monkeypatch):
    """라운드별 surgeon 출력과 재검증 결과를 고정하는 실행기.

    `bench.events`(emit된 것 전부)와 `bench.prompts`(라운드별 user 메시지)로 결과를 본다.
    """
    events: list[dict] = []
    prompts: list[str] = []
    monkeypatch.setattr(harness, "emit", lambda ev: events.append(ev))
    monkeypatch.setattr(harness, "emit_flow_frame", lambda *a, **k: None)

    def run(ops_per_round, violations_seq, *, max_rounds=3, **kw):
        plan = list(ops_per_round)

        def fake_chat(messages, **kwargs):
            prompts.append(messages[1]["content"])
            return EditOps(operations=plan.pop(0) if plan else [])

        seq = list(violations_seq)
        monkeypatch.setattr(harness, "chat_json", fake_chat)
        monkeypatch.setattr(harness, "collect_violations",
                            lambda *a, **k: seq.pop(0) if seq else [])
        return harness.refine_flow(_flow(), _Catalog(), max_rounds=max_rounds, **kw)

    run.events = events
    run.prompts = prompts
    return run


def _rounds(events):
    return [e for e in events if e.get("stage") == "refining" and "round" in (e.get("data") or {})]


# ── 순효과 0 판정 ────────────────────────────────────────────────────────────

def test_순효과가_없으면_no_effect로_기록된다(bench):
    """🔴 실측 라운드 4·5의 재현 — `applied=1`인데 흐름도는 그대로였다.

    예전에는 이게 `accepted`(가중합 동일 + extras_pending) 또는 `discarded`로 라벨링돼,
    '고쳤다'와 '되돌렸다' 중 어느 쪽도 사실이 아닌 기록이 남았다.
    """
    bench([[EditOp(op="set_params", target="n1",
                   parameters=[{"name": "title", "value": "구획", "value_source": "llm"}])]],
          [[_viol("R7")]], max_rounds=1)

    (r,) = _rounds(bench.events)
    assert r["data"]["outcome"] == "no_effect"
    assert r["data"]["applied"] == 1, "적용 수는 정직하게 남긴다 — '1건 적용, 순변경 0'이다"


def test_무효과_라운드는_extras_pending으로_채택되지_않는다(bench):
    """바뀌지도 않은 흐름도로 L2 재채점 + L3 재실행이 도는 것을 막는다(refine_draft의
    `if refined["repaired"]` 분기).

    extras_pending은 **소진하지 않는다** — 이식 지시가 실제로 반영된 적이 없으므로 소진하면
    지시가 조용히 사라진다. 대신 완화가 무장된 채 남는 반대 위험을 이벤트로 관측 가능하게 한다.
    """
    out = bench(
        [[EditOp(op="set_params", target="n1",
                 parameters=[{"name": "title", "value": "구획", "value_source": "llm"}])]],
        [[_viol("R7")]],
        extra_findings=[Finding(layer="judge", severity="major", message="이식")],
        max_rounds=1,
    )

    assert out["repaired"] is False
    (r,) = _rounds(bench.events)
    assert r["data"]["outcome"] == "no_effect"
    assert r["data"]["extras_pending"] is True


def test_파라미터를_실은_update는_무효과로_판정하지_않는다(bench):
    """🔴 항목 A가 적용할 바로 그 연산 — package·action이 같고 label/produces/consumes가 없다.

    연산별 술어(`_update_no_effect`)로 짰다면 여기서 "무효과"로 판정돼 **R2를 고칠 유일한
    연산이 적용부에 닿기도 전에 버려졌을** 자리다. 결과 비교는 실제로 바뀐 것을 보므로 통과한다.
    """
    out = bench(
        [[EditOp(op="update", target="n1", package="Step", action_name="stepAction",
                 parameters=[{"name": "title", "value": "다른 제목"}])]],
        [[_viol("R7"), _viol("R8")], [_viol("R7")]],
        max_rounds=1,
    )

    (r,) = _rounds(bench.events)
    assert r["data"]["outcome"] == "accepted"
    assert out["repaired"] is True


# ── 되먹임 ───────────────────────────────────────────────────────────────────

def test_폐기된_라운드가_다음_프롬프트에_실린다(bench):
    """🔴 폐기 뒤 프롬프트가 직전과 바이트 동일하면 같은 답이 나온다 — 실측 라운드 4·5."""
    bench([[_swap()], [_swap()]],
          [[_viol("R7")], [_viol("R7"), _viol("R2")], [_viol("R7"), _viol("R2")]])

    assert len(bench.prompts) == 2
    assert "[직전 시도" not in bench.prompts[0]
    assert "[직전 시도" in bench.prompts[1]
    assert "가중합 10 → 20" in bench.prompts[1], "악화 폭이 보여야 처방이 갈린다"


def test_새로_생긴_위반만_실린다(bench):
    """'내가 만든 것'과 '원래 있던 것'이 뒤섞이면 무엇을 되돌려야 하는지 알 수 없다.
    남은 findings 전체는 이미 [고칠 문제들]에 있다 — 중복하면 프롬프트만 커진다."""
    bench([[_swap()], [_swap()]],
          [[_viol("R7")], [_viol("R7"), _viol("R2")], [_viol("R7"), _viol("R2")]])

    tail = bench.prompts[1][bench.prompts[1].index("[직전 시도"):]
    assert "새로 만든 위반: R2×1" in tail
    assert "R7" not in tail, "원래 있던 R7은 되먹임에 다시 싣지 않는다"


def test_피드백은_프롬프트_맨_뒤에_붙는다(bench):
    """중간에 끼우면 뒤따르는 수리 어휘(수천 토큰)가 통째로 프리픽스 캐시에서 빠진다 —
    llm.py가 cached_tokens를 단가에 반영하므로 추정이 아니라 요금이다."""
    bench([[_swap()], [_swap()]],
          [[_viol("R7")], [_viol("R7"), _viol("R2")], [_viol("R7"), _viol("R2")]])

    tail = bench.prompts[1][bench.prompts[1].index("[직전 시도"):]
    assert "[고칠 문제들" not in tail and "[스펙 발췌]" not in tail


def test_같은_모양의_연산은_한_줄로_묶인다():
    """실측 라운드 5는 같은 모양의 update 12건이었다 — 12줄이면 되먹임이 프롬프트를 먹는다."""
    ops = [EditOp(op="update", target=f"n{i}", package="String", action_name="assign")
           for i in range(12)]
    assert harness._attempt_lines(ops) == ["update 표기→String/assign ×12"]


def test_연산_종류가_많으면_절단을_표기한다():
    """조용한 절단 금지 — 잘린 걸 안 밝히면 '이것만 시도했다'로 읽힌다."""
    ops = [EditOp(op="update", target="n1", package=f"P{i}", action_name="A") for i in range(9)]
    assert harness._attempt_lines(ops)[-1] == "… 외 3종(총 3건)"


def test_채택된_라운드_뒤에는_피드백이_비워진다(bench):
    """🔴 채택되면 renumber가 노드 id를 다시 매긴다 — 옛 좌표를 근거로 말하면 **틀린 자리**를
    가리킨다. 게다가 '네 수정은 반영 안 됐다'는 사실 자체가 거짓이 된다."""
    bench([[_swap()], [_swap()], [_swap()]],
          [[_viol("R7"), _viol("R8")],              # 진입
           [_viol("R7"), _viol("R8"), _viol("R2")],  # 라운드1 → 악화, 폐기
           [_viol("R7")],                            # 라운드2 → 개선, 채택
           [_viol("R7")]])

    assert "[직전 시도" in bench.prompts[1]
    assert "[직전 시도" not in bench.prompts[2]


def test_금지_표기만_남으면_되돌려졌다는_헤더가_안_나온다(bench):
    """🔴 banned는 한 번 채워지면 채택 뒤에도 남는다. 그것만으로 헤더를 렌더하면 **채택된
    라운드 직후마다** 모델이 "네 수정은 반영 안 됐다"를 읽는다 — 막으려던 병리의 재생산."""
    bad = EditOp(op="insert", anchor="n1", position="after",
                 action={"package": "없는패키지", "action": "없는액션"})
    bench([[_swap(), bad], [_swap()]],
          [[_viol("R7"), _viol("R8")], [_viol("R7")], [_viol("R7")]])

    assert "없는패키지/없는액션" in bench.prompts[1], "금지 표기는 채택 뒤에도 계속 알려준다"
    assert "[직전 시도" not in bench.prompts[1], "그런데 '되돌려졌다'는 아니다"


def test_반쪽_교체로_버려지면_왜_그런지까지_알려준다(bench):
    """🔴 실측 2026-07-27 — 금지 표기 목록에 `Microsoft 365 Excel/Step`이 실려 있는데도
    surgeon이 4라운드 연속 같은 실수를 했다.

    표기만 나열하면 모델은 "그 표기를 쓰지 말라"로만 읽는다. 자기가 **package만 주는 바람에
    그 표기를 만들고 있다**는 것을 모르기 때문이다. 안 준 필드를 짚어 줘야 달라진다.
    """
    half = EditOp(op="update", target="n1", package="Email")  # action은 stepAction으로 남는다
    bench([[half], [_swap()]], [[_viol("R7")], [_viol("R7")], [_viol("R7")]])

    assert "Email/stepAction" in bench.prompts[1]
    assert "action_name" in bench.prompts[1], "안 준 필드를 지목한다"


def test_살아남은_반쪽_교체에는_주의가_안_붙는다(bench):
    """반쪽 교체 자체는 정상이다(R17 수리). 멀쩡한 연산에 "네가 만든 것"이라고 하면 안 된다."""
    ok = EditOp(op="update", target="n1", action_name="assign", package="String")
    bad = EditOp(op="insert", anchor="n1", position="after",
                 action={"package": "없는패키지", "action": "없는액션"})
    bench([[ok, bad], [_swap()]],
          [[_viol("R7"), _viol("R8")], [_viol("R7")], [_viol("R7")]])

    assert "없는패키지/없는액션" in bench.prompts[1]
    assert "네가 만든 것" not in bench.prompts[1]


def test_금지_표기_수집이_결정론이다():
    """set 순회로 모으면 PYTHONHASHSEED에 따라 순서가 달라지고, 그 순서가 절단 대상을 정하며,
    그게 **프롬프트 본문에 실린다** — 같은 입력에 프로세스마다 다른 프롬프트가 나간다.

    수집을 사전 검증(drop_unknown_action_ops)이 직접 하는 이유: 호출부가 따로 계산하면
    거기의 순차 투영을 다시 구현해야 하고, 어긋나면 프롬프트가 "쓰지 마라"고 말하지 않은
    표기를 실제로는 버린다.
    """
    from app.agent.v4.orchestrator.edit_ops import drop_unknown_action_ops

    flow = {"steps": [{"step_id": "s", "label": "s", "actions": []}]}
    ops = [
        EditOp(op="insert", anchor="n1", position="after", action={"package": "B", "action": "b"}),
        EditOp(op="insert", anchor="n1", position="after", action={"package": "A", "action": "a"}),
        EditOp(op="insert", anchor="n1", position="after", action={"package": "B", "action": "b"}),
    ]
    banned: list[str] = []
    drop_unknown_action_ops(flow, ops, lambda p, a: False, banned_out=banned)

    assert banned == ["B/b", "A/a"]


@pytest.mark.parametrize("outcome,ops,viols", [
    ("all_dropped",
     [EditOp(op="update", target="n1", package="없음", action_name="없음")],
     [[_viol("R7")]]),
    ("no_effect",
     [EditOp(op="set_params", target="n1",
             parameters=[{"name": "title", "value": "구획", "value_source": "llm"}])],
     [[_viol("R7")]]),
    ("apply_failed", [EditOp(op="remove", target="없는노드")], [[_viol("R7")]]),
    ("discarded",
     [EditOp(op="update", target="n1", package="String", action_name="assign")],
     [[_viol("R7")], [_viol("R7"), _viol("R2")]]),
])
def test_fed_back이_네_실패_분기_모두에_실린다(bench, outcome, ops, viols):
    """효과 판정축은 "되먹임을 받은 라운드의 채택률"이다 — 실측 라운드 4·5가 정확히 이
    분기들로 가므로, 한 분기라도 빠지면 실측으로 못 뽑는다."""
    bench([ops], viols, max_rounds=1)

    (r,) = _rounds(bench.events)
    assert r["data"]["outcome"] == outcome
    assert "fed_back" in r["data"]


def test_무효과_분기에도_연산과_폐기목록이_남는다(bench):
    """환각 3건 + 무효과 9건인 라운드에서 환각 기록이 통째로 사라지지 않게."""
    bad = EditOp(op="insert", anchor="n1", position="after",
                 action={"package": "없는패키지", "action": "없는액션"})
    noop = EditOp(op="set_params", target="n1",
                  parameters=[{"name": "title", "value": "구획", "value_source": "llm"}])
    bench([[noop, bad]], [[_viol("R7")]], max_rounds=1)

    (r,) = _rounds(bench.events)
    assert r["data"]["outcome"] == "no_effect"
    assert r["data"]["ops"] and r["data"]["dropped"]


def test_폐기_라운드의_파라미터_정리는_되돌려짐으로_표시된다(bench):
    """폐기되면 work를 버리므로 그 정리는 **일어나지 않은 사실**이다. 그대로 실으면 다음
    프롬프트가 아웃라인(정리 전)과 모순되는 상태를 말한다."""
    bench([[_swap()]], [[_viol("R7")], [_viol("R7"), _viol("R2")]], max_rounds=1)

    (r,) = _rounds(bench.events)
    assert r["data"]["outcome"] == "discarded"
    assert "param_prune" not in r["data"]
    assert r["data"]["param_prune_reverted"][0]["dropped"] == ["title"]


def test_채택_라운드의_파라미터_정리는_그대로_기록된다(bench):
    """관측이 없으면 A 이후 라운드 델타를 'surgeon 연산'과 '하네스 정리'로 못 가른다."""
    bench([[_swap()]], [[_viol("R7"), _viol("R8")], [_viol("R7")]], max_rounds=1)

    (r,) = _rounds(bench.events)
    assert r["data"]["outcome"] == "accepted"
    assert r["data"]["param_prune"] == [{"node": "n1", "to": "String/assign", "dropped": ["title"]}]


def test_피드백이_없으면_프롬프트가_기존과_같다(bench):
    """첫 라운드 하위호환 — 되먹임 블록은 있을 때만 붙는다."""
    bench([[_swap()]], [[_viol("R7"), _viol("R8")], [_viol("R7")]], max_rounds=1)

    assert "[직전 시도" not in bench.prompts[0]
    assert "[카탈로그에 없어" not in bench.prompts[0]
