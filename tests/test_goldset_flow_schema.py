# -*- coding: utf-8 -*-
"""정답 봇 JSON → 흐름도 스키마 변환 · KB 어휘 고정 사상 · 엄밀 채점 (RPA-298 Phase 0-1).

여기서 막으려는 것:
1. **중첩이 조용히 평평해지는 것.** 이 작업의 존재 이유가 중첩·파라미터 보존이라, 변환기가
   children을 흘리면 새 채점 축 전체가 의미를 잃는다.
2. **기존 채점 축과 정답 모집단이 어긋나는 것.** 변환 트리를 평탄화하면 `gold.merged_sequence`와
   같아야 한다 — 다르면 새 축과 옛 축을 나란히 놓고 비교할 수 없다.
3. **사상이 빌드마다 흔들리는 것.** 동률 후보가 있을 때 DB 반환 순서에 따라 정답지가 달라지면
   "고정된 정답지"가 아니다.
4. **사상 실패가 조용히 사라지는 것.** unresolved 건수가 KB 결손 규모의 유일한 신호다.
5. **엄밀 채점이 퍼지로 되돌아가는 것.** 이름이 다르면 안 맞아야 하고, 자리가 다르면
   중첩 지표가 떨어져야 한다.
6. **망가진 정답지가 정본을 덮어쓰는 것.** 산출물 트리는 git이 아니라 복구 수단이 없고,
   `manifest.json`이 정상적으로 생겨 소비처가 망가진 줄 모른 채 그럴듯한 0점을 낸다.
7. **엄밀 축이 러너에서 풀리는 것.** 재채점기에만 배선돼 있으면 앞으로 도는 모든 런이
   여전히 퍼지 축만 낸다 — 이 축을 만든 목적 자체가 무너진다.

LLM·DB를 전혀 부르지 않는다(카탈로그는 리스트 스텁, 에이전트·파서·커버리지는 monkeypatch).
"""

import asyncio
import json
import types
from pathlib import Path

import pytest

from scripts.goldset_eval import run_eval
from scripts.goldset_eval.build_gold_flows import (
    BUILDER_VERSION,
    DEFAULT_OUT_NAME,
    GoldFlowBuildRefused,
    build,
    check_gold_flows,
    convention_hash,
)
from scripts.goldset_eval.build_gold_flows import load_gold_flow as load_gold_flow_case
from scripts.goldset_eval.exact_metrics import (
    EXACT_SCORE_KEYS,
    attach_exact_axes,
    exact_key,
    pair_slots,
    pred_slots,
    score_case_exact,
)
from scripts.goldset_eval.flow_schema import (
    KbResolver,
    annotate_kb,
    container_kind,
    convert_bot_json,
    iter_flow_actions,
    load_overrides,
)
from scripts.goldset_eval.gold import load_gold_flow

# 실제 골드셋 위치 — 없으면 관련 테스트만 skip한다(CI에는 이 자산이 없다).
GOLDSET = Path(r"C:/Users/qoqkd/Desktop/final-etc-files/골드셋")


# ── 합성 픽스처 ──────────────────────────────────────────────────────────────


def _attr(name, **value):
    return {"name": name, "value": value}


def _node(pkg, cmd, *, children=None, branches=None, attrs=None, disabled=False, ret=None):
    n = {"packageName": pkg, "commandName": cmd, "disabled": disabled, "uid": f"{pkg}-{cmd}"}
    if attrs:
        n["attributes"] = attrs
    if children:
        n["children"] = children
    if branches:
        n["branches"] = branches
    if ret:
        n["returnTo"] = {"type": "VARIABLE", "variableName": ret}
    return n


def _bot() -> dict:
    """try{ loop{ if{ create } } } catch{ log } + disabled·Comment·최상위 비-Step 노드."""
    return {
        "variables": [{"name": "sPath", "type": "STRING", "input": True}],
        "nodes": [
            _node("Comment", "Comment", attrs=[_attr("comment", string="설명", type="STRING")]),
            _node("Step", "step",
                  attrs=[_attr("title", string="Setup log folders/locations", type="STRING")],
                  children=[
                      _node("String", "assign",
                            attrs=[_attr("value", string="$sPath$/logs", type="STRING")],
                            ret="sLog"),
                  ]),
            _node("Step", "step",
                  attrs=[_attr("title", string="본 업무", type="STRING")],
                  children=[
                      _node("ErrorHandler", "try",
                            children=[
                                _node("Loop", "loop.commands.start",
                                      attrs=[_attr("iterator", type="ITERATOR",
                                                   packageName="DataTable",
                                                   iteratorName="iteratorTableRow")],
                                      children=[
                                          _node("If", "if",
                                                attrs=[_attr("condition", type="CONDITIONAL",
                                                             packageName="Folder",
                                                             conditionalName="folderDoesNotExists")],
                                                children=[
                                                    _node("Folder", "createFolder",
                                                          attrs=[_attr("path", string="$sLog$",
                                                                       type="FILE")]),
                                                ]),
                                      ]),
                                _node("File", "deleteFiles", disabled=True,
                                      children=[_node("Folder", "createFolder")]),
                            ],
                            branches=[
                                _node("ErrorHandler", "catch",
                                      children=[
                                          _node("LogToFile", "logToFile",
                                                attrs=[_attr("text", string="실패", type="STRING")]),
                                      ]),
                            ]),
                  ]),
            _node("MessageBox", "messageBox",
                  attrs=[_attr("message", string="끝", type="STRING")]),
        ],
    }


def _kb_specs() -> list[dict]:
    """카탈로그 스텁 — 실 카탈로그 표기를 흉내 낸 최소 집합."""
    return [
        {"package": "String", "action": "Assign"},
        {"package": "Error handler", "action": "Try"},
        {"package": "Error handler", "action": "Catch"},
        {"package": "Loop", "action": "For each row in table"},
        {"package": "Loop", "action": "Loop action for data iteration"},
        {"package": "If", "action": "If"},
        {"package": "Folder", "action": "Create"},
        {"package": "Message Box", "action": "Message box"},
        # 동률 유발 쌍 — logToFile은 둘 다 0.85로 붙는다
        {"package": "Logging", "action": "Log text to file"},
        {"package": "Logging", "action": "Log variables to file"},
    ]


@pytest.fixture
def flow() -> dict:
    return convert_bot_json(_bot(), "synthetic.json")


@pytest.fixture
def resolved_flow(flow) -> dict:
    return annotate_kb(flow, KbResolver(_kb_specs()))


# ── 1. 변환 ──────────────────────────────────────────────────────────────────


def test_컨테이너_중첩이_보존된다(flow):
    """Loop/If의 본문이 children으로 남는가 — 평평해지면 이 작업 전체가 무의미해진다."""
    step2 = flow["steps"][1]
    try_node = step2["actions"][0]
    assert (try_node["package"], try_node["action"]) == ("ErrorHandler", "try")
    loop = try_node["children"][0]
    assert loop["container"] == "loop"
    if_node = loop["children"][0]
    assert if_node["container"] == "if"
    assert [(a["package"], a["action"]) for a in if_node["children"]] == [("Folder", "createFolder")]


def test_branches는_형제로_승격되고_출처가_남는다(flow):
    """`RecommendedAction`엔 branches가 없다 — catch는 try의 **다음 형제**여야 한다."""
    actions = flow["steps"][1]["actions"]
    assert [(a["package"], a["action"]) for a in actions] == [
        ("ErrorHandler", "try"), ("ErrorHandler", "catch")
    ]
    catch = actions[1]
    assert catch["branch_of"] == "try"          # 어느 컨테이너의 분기였는지 보존
    assert catch["order"] == 2                  # 형제 순번 재부여
    assert [(a["package"], a["action"]) for a in catch["children"]] == [("LogToFile", "logToFile")]


def test_disabled_노드는_하위까지_통째로_빠진다(flow):
    """실행되지 않는 노드는 정답이 아니다. 하위만 남으면 재현율 분모가 부풀어 오른다."""
    names = {(a["package"], a["action"]) for a, _p in iter_flow_actions(flow)}
    assert ("File", "deleteFiles") not in names
    # disabled try 아래의 createFolder는 활성 경로의 것 1건만 남아야 한다
    assert sum(1 for a, _p in iter_flow_actions(flow)
               if (a["package"], a["action"]) == ("Folder", "createFolder")) == 1
    assert flow["stats"]["disabled"] == 1


def test_Comment는_빠지되_개수는_남는다(flow):
    """실행 의미가 없어 빼지만, 몇 개를 뺐는지 모르면 변환 손실을 감사할 수 없다."""
    assert flow["stats"]["comments"] == 1
    assert all(a["package"] != "Comment" for a, _p in iter_flow_actions(flow))


def test_최상위_비Step_노드는_암묵_단계로_묶인다(flow):
    """실 골드셋에 최상위 비-Step 노드가 34개 있다 — 버리면 정답이 통째로 준다."""
    last = flow["steps"][-1]
    assert last["implicit"] is True
    assert [(a["package"], a["action"]) for a in last["actions"]] == [("MessageBox", "messageBox")]


def test_보일러플레이트_Step은_하위까지_표시된다(flow):
    """Bot Store 제출 규약 Step은 실업무 재현율에서 빠져야 한다 — 표시가 하위로 내려가야 한다."""
    boiler_step = flow["steps"][0]
    assert boiler_step["boilerplate"] is True
    assert all(a["boilerplate"] for a in boiler_step["actions"])
    assert flow["steps"][1]["boilerplate"] is False


def test_파라미터와_변수흐름이_옮겨진다(flow):
    """파라미터를 안 옮기면 '무엇을 하는 액션인가'가 사라져 컨테이너 사상이 불가능해진다."""
    assign = flow["steps"][0]["actions"][0]
    assert assign["parameters"] == [
        {"name": "value", "type": "STRING", "value": "$sPath$/logs"}
    ]
    assert assign["produces"] == [{"name": "sLog"}]      # returnTo
    assert assign["consumes"] == [{"name": "sPath"}]     # $var$ 보간

    loop = flow["steps"][1]["actions"][0]["children"][0]
    assert loop["parameters"][0]["value"] == "DataTable/iteratorTableRow"


def test_returns는_리스트가_아니라_dict다():
    """`ErrorHandler/catch`의 `returns`는 dict다 — 리스트로 순회하면 출력 변수가 통째로 사라진다."""
    bot = {"nodes": [{
        "packageName": "ErrorHandler", "commandName": "catch", "disabled": False,
        "returns": {"errorMessage": {"type": "VARIABLE", "variableName": "sErr"},
                    "errorLineNumber": {"type": "VARIABLE", "variableName": "nLine"}},
    }]}
    catch = convert_bot_json(bot, "x.json")["steps"][0]["actions"][0]
    assert catch["produces"] == [
        {"name": "sErr", "role": "errorMessage"},
        {"name": "nLine", "role": "errorLineNumber"},
    ]


def test_자격증명은_가려진다():
    """정답 봇에 평문 Basic 인증이 들어 있다 — 새 산출물로 한 벌 더 퍼뜨리면 안 된다."""
    bot = {"nodes": [_node("Rest", "restPost", attrs=[
        _attr("customHeaders", type="LIST", list=[{
            "type": "DICTIONARY",
            "dictionary": [
                {"key": "name", "value": {"type": "STRING", "string": "Authorization"}},
                {"key": "value", "value": {"type": "STRING",
                                           "string": "Basic dXNlcjpwYXNzd29yZDEyMzQ1Ng=="}},
            ],
        }]),
        _attr("password", type="STRING", string="hunter2"),
    ])]}
    f = convert_bot_json(bot, "x.json")
    dumped = json.dumps(f, ensure_ascii=False)
    assert "hunter2" not in dumped
    assert "dXNlcjpwYXNzd29yZDEyMzQ1Ng" not in dumped
    assert "«redacted»" in dumped


def test_평탄화가_기존_gold_시퀀스와_일치한다(tmp_path):
    """새 축과 옛 축이 **같은 정답**을 봐야 나란히 비교된다. 어긋나면 두 수치가 남남이 된다."""
    p = tmp_path / "b.json"
    p.write_text(json.dumps(_bot()), encoding="utf-8")
    legacy = load_gold_flow(p).sequence
    converted = [(a["package"], a["action"])
                 for a, _path in iter_flow_actions(convert_bot_json(_bot(), "b.json"))]
    assert converted == legacy


def test_중첩경로에서_Step은_빠진다(flow):
    """정답 봇은 Step 노드로, 에이전트는 steps[]와 Step 액션을 섞어 구획한다 —
    Step을 경로에 넣으면 같은 구조도 다른 경로로 읽혀 중첩 지표가 표기 차이만 잰다."""
    paths = {(a["package"], a["action"]): p for a, p in iter_flow_actions(flow)}
    assert paths[("Folder", "createFolder")] == ("try", "loop", "if")
    assert paths[("LogToFile", "logToFile")] == ("catch",)


# ── 2. KB 어휘 사상 ──────────────────────────────────────────────────────────


def test_사상_결과가_액션마다_박힌다(resolved_flow):
    """'고정된 정답지' = 채점 때 다시 푸는 게 아니라 파일에 결과가 들어 있는 것."""
    by_name = {(a["package"], a["action"]): a["kb"] for a, _p in iter_flow_actions(resolved_flow)}
    assert by_name[("Folder", "createFolder")]["package"] == "Folder"
    assert by_name[("Folder", "createFolder")]["action"] == "Create"
    assert by_name[("ErrorHandler", "try")]["action"] == "Try"
    assert by_name[("MessageBox", "messageBox")]["package"] == "Message Box"


def test_동률은_사전순으로_결정론적이고_후보가_남는다():
    """DB 반환 순서에 사상이 흔들리면 빌드마다 정답지가 달라진다."""
    forward = KbResolver(_kb_specs())
    backward = KbResolver(list(reversed(_kb_specs())))
    a = forward.resolve("LogToFile", "logToFile")
    b = backward.resolve("LogToFile", "logToFile")
    assert a["action"] == b["action"] == "Log text to file"
    assert a["alternatives"] == [["Logging", "Log variables to file"]]  # 버려진 동률 후보 감사용


def test_Loop는_액션이름이_아니라_반복자로_사상된다(resolved_thing=None):
    """봇 Loop는 늘 `loop.commands.start`다 — 이름으로 풀면 토큰이 하나도 안 겹쳐 전건 실패한다."""
    r = KbResolver(_kb_specs())
    table = r.resolve("Loop", "loop.commands.start",
                      [{"name": "iterator", "type": "ITERATOR",
                        "value": "DataTable/iteratorTableRow"}])
    assert (table["package"], table["action"]) == ("Loop", "For each row in table")
    assert table["via"] == "iterator"
    # 반복자가 없으면 제네릭 Loop로 (버리지 않는다)
    generic = r.resolve("Loop", "loop.commands.start", [])
    assert generic["action"] == "Loop action for data iteration"
    assert generic["status"] == "resolved"


def test_사상_실패는_버려지지_않고_unresolved로_남는다():
    """미사상 건수가 KB 결손 규모의 유일한 신호다 — 조용히 빼면 재현율이 거짓으로 좋아진다."""
    r = KbResolver(_kb_specs())
    out = r.resolve("Twilio", "Send SMS from Twilio")
    assert out["status"] == "unresolved"
    assert out["package"] is None
    assert "note" in out


def test_수동교정표가_자동사상을_이긴다(tmp_path):
    """자동 사상이 동률로 잘못 고른 걸 코드 수정 없이 뒤집을 수 있어야 한다."""
    p = tmp_path / "ov.json"
    p.write_text(json.dumps({
        "_comment": "무시돼야 함",
        "LogToFile/logToFile": {"kb": ["Logging", "Log variables to file"], "why": "테스트"},
        "Folder/createFolder": None,
    }, ensure_ascii=False), encoding="utf-8")
    r = KbResolver(_kb_specs(), load_overrides(p))
    forced = r.resolve("LogToFile", "logToFile")
    assert (forced["package"], forced["action"]) == ("Logging", "Log variables to file")
    assert forced["via"] == "override" and forced["note"] == "테스트"
    # null은 '사상 없음' 강제 — 자동으로는 풀리는 것도 막는다
    assert r.resolve("Folder", "createFolder")["status"] == "unresolved"


def test_컨테이너_종류는_봇표기와_KB표기를_같게_읽는다():
    """`ErrorHandler/try`와 `Error handler/Try`, 표기 사고 `Error handler/Error handler/Try`까지."""
    assert container_kind("ErrorHandler", "try") == "try"
    assert container_kind("Error handler", "Try") == "try"
    assert container_kind("Error handler", "Error handler/Try") == "try"
    assert container_kind("If", "Else if (optional)") == "elseif"
    assert container_kind("Loop", "For each row in table") == "loop"
    assert container_kind("Loop", "Break") is None       # 흐름 제어지 컨테이너가 아니다
    assert container_kind("Folder", "Create") is None


# ── 3. 엄밀 채점 ─────────────────────────────────────────────────────────────


def _pred_from_gold(gold_flow: dict) -> dict:
    """정답지를 KB 표기로 그대로 베낀 '완벽한 예측' — 상한이 1.0인지 확인하는 기준선."""
    def conv(actions):
        out = []
        for a in actions:
            kb = a["kb"]
            if kb["status"] != "resolved":
                continue
            out.append({"package": kb["package"], "action": kb["action"],
                        "parameters": a["parameters"], "children": conv(a["children"])})
        return out

    return {"steps": [{"step_id": s["step_id"], "actions": conv(s["actions"])}
                      for s in gold_flow["steps"]]}


def test_완전히_같으면_엄밀_f1이_1이다(resolved_flow):
    """정답지를 그대로 베낀 예측이 1.0이 아니면 채점기가 정답을 못 읽는 것이다."""
    pred = _pred_from_gold(resolved_flow)
    s = score_case_exact(pred, resolved_flow)
    assert s["action_exact"]["f1"] == 1.0
    assert s["nesting"]["path_match"] == 1.0
    assert s["order_exact"] == 1.0


def test_이름이_비슷하기만_하면_안_맞는다(resolved_flow):
    """이 축의 계약: 퍼지 유사도 금지. `Create folder`는 `Create`와 다른 이름이다."""
    pred = _pred_from_gold(resolved_flow)
    pred["steps"][1]["actions"][0]["children"][0]["children"][0]["children"][0]["action"] = "Create folder"
    s = score_case_exact(pred, resolved_flow)
    assert s["action_exact"]["f1"] < 1.0
    assert ["Folder", "createFolder"] in [g["action"] for g in s["gold_only"]]


def test_같은_액션이라도_컨테이너_밖이면_중첩지표가_떨어진다(resolved_flow):
    """액션 이름을 다 맞혀도 Loop 밖에 놓으면 봇이 다르게 돈다 — 옛 축은 그걸 못 봤다."""
    pred = _pred_from_gold(resolved_flow)
    loop = pred["steps"][1]["actions"][0]["children"][0]
    if_node = loop["children"].pop(0)
    pred["steps"][1]["actions"][0]["children"].append(if_node)  # Loop 밖(try 직속)으로 이동
    s = score_case_exact(pred, resolved_flow)
    assert s["action_exact"]["f1"] == 1.0          # 액션 집합은 그대로
    assert s["nesting"]["path_match"] < 1.0        # 자리는 틀렸다
    assert s["nesting"]["mismatched"]


def test_순서가_뒤집히면_order가_떨어진다(resolved_flow):
    pred = _pred_from_gold(resolved_flow)
    pred["steps"].reverse()
    for st in pred["steps"]:
        st["actions"].reverse()
    s = score_case_exact(pred, resolved_flow)
    assert s["order_exact"] is not None and s["order_exact"] < 1.0


def test_미사상_정답은_절대_매칭되지_않고_분모에_남는다():
    """KB에 없는 액션을 우연히 맞힌 것처럼 세면 재현율이 거짓으로 좋아진다."""
    bot = {"nodes": [
        _node("Folder", "createFolder"),
        _node("Twilio", "Send SMS from Twilio"),
    ]}
    gf = annotate_kb(convert_bot_json(bot, "x.json"), KbResolver(_kb_specs()))
    pred = {"steps": [{"step_id": "s1", "actions": [
        {"package": "Folder", "action": "Create", "children": []},
        {"package": "Twilio", "action": "Send SMS from Twilio", "children": []},
    ]}]}
    s = score_case_exact(pred, gf)
    assert s["n_gold"] == 2
    assert s["n_matched"] == 1                                    # Twilio는 안 맞는다
    assert s["action_exact"]["recall"] == 0.5                     # 전체 분모엔 남는다
    assert s["action_exact_resolved"]["recall"] == 1.0            # 사상된 것만 보면 만점
    assert s["action_exact_resolved"]["n_gold_unresolved"] == 1
    assert s["unresolved_gold"] == [{"action": "Twilio/Send SMS from Twilio", "count": 1}]


def test_Step과_Comment는_양쪽에서_액션_모집단에서_빠진다(resolved_flow):
    """기존 축(`gold.SCAFFOLD`)과 같은 모집단이어야 두 축을 나란히 읽을 수 있다."""
    pred = {"steps": [{"step_id": "s1", "actions": [
        {"package": "Step", "action": "Step", "children": [
            {"package": "Folder", "action": "Create", "children": []},
        ]},
    ]}]}
    slots = pred_slots(pred)
    assert [s.raw for s in slots] == [("Folder", "Create")]
    assert slots[0].path == ()  # Step은 경로에서도 빠진다


def test_같은_키가_여럿이면_등장순서대로_짝지어진다():
    """`String/Assign`처럼 흔한 액션에서 매칭이 흔들리면 순서 지표가 무의미해진다."""
    gold = {"steps": [{"step_id": "s", "actions": [
        {"package": "String", "action": "assign", "children": [],
         "kb": {"status": "resolved", "package": "String", "action": "Assign"}},
        {"package": "String", "action": "assign", "children": [],
         "kb": {"status": "resolved", "package": "String", "action": "Assign"}},
    ]}]}
    pred = {"steps": [{"step_id": "s", "actions": [
        {"package": "String", "action": "Assign", "children": []},
        {"package": "String", "action": "Assign", "children": []},
    ]}]}
    from scripts.goldset_eval.exact_metrics import gold_slots
    pairs = pair_slots(gold_slots(gold), pred_slots(pred))
    assert [(g.index, p.index) for g, p in pairs] == [(0, 0), (1, 1)]


def test_예측_JSON이_이상해도_채점이_죽지_않는다(resolved_flow):
    """에이전트 산출물은 외부 JSON이다 — 노드 하나가 이상하다고 케이스 전체가 날아가면
    러너의 '케이스 실패 격리'가 무의미해진다."""
    pred = {"steps": [
        None,
        {"step_id": "s", "actions": [
            "쓰레기",
            {"package": "Folder"},                                   # action 없음
            {"package": "Folder", "action": "Create", "children": None},
        ]},
    ]}
    s = score_case_exact(pred, resolved_flow)
    assert s["n_pred"] == 1
    assert s["n_matched"] == 1


def test_정준키는_표기_흔들림만_흡수한다():
    """대소문자·구분자는 접지만 의미 추정은 하지 않는다(그건 정답지 빌드 때 끝났다)."""
    assert exact_key("Excel advanced", "Get multiple cells") == exact_key("Excel_MS", "GetMultipleCells")
    assert exact_key("Loop", "For each row in CSV/TXT") == ("loop", "foreachrowincsvtxt")
    assert exact_key("Folder", "Create") != exact_key("Folder", "Create folder")


# ── 4. 실 골드셋 회귀 (자산 없으면 skip) ─────────────────────────────────────


@pytest.mark.skipif(not (GOLDSET / "정답셋" / "manifest.json").is_file(),
                    reason="골드셋 자산 없음 (CI)")
def test_실_골드셋_13케이스가_전부_변환되고_옛_시퀀스와_일치한다(tmp_path):
    """13케이스 중 하나라도 변환이 깨지거나 옛 축과 어긋나면 새 축을 쓸 수 없다.

    카탈로그는 쓰지 않는다(`use_catalog=False`) — DB 없이 **변환 자체**만 검증한다.
    사상률은 빌드 산출물(`정답흐름도/manifest.json`)로 따로 본다.
    """
    m = build(GOLDSET, tmp_path, use_catalog=False)
    assert m["totals"]["cases"] == 13
    assert m["totals"]["actions"] == 528  # gold.merged_sequence 합계와 같아야 한다
    assert all(e["sequence_matches_legacy_gold"] for e in m["entries"])
    assert all(e["n_actions"] > 0 for e in m["entries"])
    # 카탈로그 없이 돌리면 컨테이너·Loop만 풀리고 나머지는 전부 unresolved여야 한다
    assert m["totals"]["resolved"] < m["totals"]["actions"]


@pytest.mark.skipif(not (GOLDSET / "정답흐름도" / "manifest.json").is_file(),
                    reason="정답지 산출물 없음")
def test_커밋된_정답지가_현재_변환기와_일치한다():
    """산출물이 코드보다 오래되면(재생성 잊음) 채점이 옛 정답으로 돈다."""
    m = json.loads((GOLDSET / "정답흐름도" / "manifest.json").read_text(encoding="utf-8"))
    assert m["schema_version"] == "gold-flow/1.0"
    assert m["totals"]["cases"] == 13
    for e in m["entries"]:
        case = json.loads(
            (GOLDSET / "정답흐름도" / e["file"]).read_text(encoding="utf-8")
        )
        actions = [a for a, _p in iter_flow_actions(case)]
        assert len(actions) == e["n_actions"]
        assert all("kb" in a for a in actions)


# ── 5. 정답지 무결성 — 덮어쓰기 가드·신뢰도 판정·낡음 감지 ───────────────────
#
# 정답지는 리포 밖(`final-etc-files`)에 산다. 그 트리는 **git 저장소가 아니라**
# (실측: `git rev-parse --is-inside-work-tree` → fatal) 한 번 덮어쓰면 되돌릴 수 없다.
# 그런데 `--no-catalog` 빌드도 정상적인 `manifest.json`을 남기므로, 소비처가 "있으면 켠다"
# 로 판단하면 **전건 unresolved인 정답지 위에서 0에 가까운 그럴듯한 수치**가 나온다.
# 아래 테스트가 그 두 구멍(쓰기·읽기)을 각각 막는다.


def _synthetic_goldset(root: Path, bot: dict | None = None) -> Path:
    """실 골드셋 자산 없이 build()를 돌릴 수 있는 최소 골드셋(1케이스)."""
    gs = root / "골드셋"
    wf = gs / "정답셋" / "case01" / "workflows"
    wf.mkdir(parents=True)
    (wf / "main.json").write_text(json.dumps(bot or _bot()), encoding="utf-8")
    (gs / "정답셋" / "manifest.json").write_text(
        json.dumps({"entries": [{"index": 1, "case_dir": "case01",
                                 "bot_name": "01_bot", "title": "t"}]}, ensure_ascii=False),
        encoding="utf-8")
    return gs


@pytest.fixture
def stub_catalog(monkeypatch):
    """빌드가 DB 대신 리스트 스텁 카탈로그를 보게 한다."""
    monkeypatch.setattr("scripts.goldset_eval.build_gold_flows._kb_specs", _kb_specs)


def test_카탈로그_없이는_기본_산출자리에_쓰지_못한다(tmp_path):
    """`--no-catalog` 산출물이 정본 자리에 쓰이면 커밋된 정답지가 복구 불가능하게 날아간다."""
    gs = _synthetic_goldset(tmp_path)
    out = gs / DEFAULT_OUT_NAME
    with pytest.raises(GoldFlowBuildRefused) as ex:
        build(gs, out, use_catalog=False)
    assert "--force" in str(ex.value)      # 해제 방법이 메시지에 있어야 한다
    assert not out.exists(), "거부했는데 산출물이 일부라도 남으면 정본이 반쯤 덮인다"


def test_카탈로그로_만든_정답지를_망가진_빌드가_덮어쓰지_못한다(tmp_path, stub_catalog):
    """경로 이름이 기본과 달라도 **정본이면** 보호한다 — 판단 기준은 이름이 아니라 내용이다."""
    gs = _synthetic_goldset(tmp_path)
    out = tmp_path / "정답지사본"
    good = build(gs, out)
    before = (out / "manifest.json").read_text(encoding="utf-8")
    assert good["totals"]["resolve_rate"] == 1.0

    with pytest.raises(GoldFlowBuildRefused):
        build(gs, out, use_catalog=False)
    assert (out / "manifest.json").read_text(encoding="utf-8") == before


def test_사상률이_무너진_빌드도_정본_자리를_건드리지_못한다(tmp_path, monkeypatch):
    """카탈로그를 **쓰긴 썼는데** 비었거나 엉뚱한 DB를 본 경우 — `catalog.used`만으론 못 잡는다."""
    monkeypatch.setattr("scripts.goldset_eval.build_gold_flows._kb_specs", lambda: [])
    sparse = {"nodes": [_node("Twilio", f"sendSms{i}") for i in range(4)] + [_node("If", "if")]}
    gs = _synthetic_goldset(tmp_path, sparse)
    with pytest.raises(GoldFlowBuildRefused) as ex:
        build(gs, gs / DEFAULT_OUT_NAME, use_catalog=True)
    assert "사상률" in str(ex.value)


def test_force면_가드를_해제할_수_있다(tmp_path):
    """가드는 사고를 막는 것이지 재생성을 금지하는 게 아니다."""
    gs = _synthetic_goldset(tmp_path)
    out = gs / DEFAULT_OUT_NAME
    m = build(gs, out, use_catalog=False, force=True)
    assert m["catalog"]["used"] is False
    assert (out / "manifest.json").is_file()


def test_카탈로그_없이_만든_정답지로는_엄밀축을_켜지_않는다(tmp_path):
    """읽는 쪽 방어 — 어떤 경로로든 망가진 정답지가 자리에 있으면 축을 켜지 말아야 한다.

    `manifest.json` 존재만 보던 옛 판정이면 여기서 usable=True가 되어, 채점기가
    전건 unresolved 정답지 위에서 0에 가까운 수치를 '측정값'으로 내놓는다.
    """
    gs = _synthetic_goldset(tmp_path)
    out = gs / DEFAULT_OUT_NAME
    build(gs, out, use_catalog=False, force=True)
    st = check_gold_flows(out)
    assert st.usable is False
    assert any("카탈로그" in m for m in st.messages)


def test_사상률이_낮은_정답지로는_엄밀축을_켜지_않는다(tmp_path, stub_catalog):
    """카탈로그를 썼다고 기록돼 있어도 사상률이 무너졌으면 못 믿는다."""
    gs = _synthetic_goldset(tmp_path)
    out = tmp_path / "out"
    build(gs, out)
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    m["totals"]["resolve_rate"] = 0.31
    (out / "manifest.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

    st = check_gold_flows(out)
    assert st.usable is False
    assert any("사상률" in msg for msg in st.messages)


def test_정상_정답지는_엄밀축을_켜고_잔소리하지_않는다(tmp_path, stub_catalog):
    """가드가 과민하면(정상 산출물에도 경고) 아무도 경고를 안 읽게 된다."""
    gs = _synthetic_goldset(tmp_path)
    out = tmp_path / "out"
    build(gs, out)
    st = check_gold_flows(out)
    assert st.usable is True
    assert st.messages == []
    assert st.manifest["builder"] == {"version": BUILDER_VERSION,
                                      "convention_hash": convention_hash()}


def test_변환규약이_바뀐_뒤의_낡은_정답지는_경고로_잡힌다(tmp_path, stub_catalog):
    """빌더는 리포 안, 산출물은 리포 밖이라 규약이 바뀌면 산출물이 조용히 낡는다.

    끄지 않고 **경고만** 하는 이유: 낡은 것과 망가진 것은 다르다. 재생성 판단은 사람 몫이고,
    여기서 축을 꺼 버리면 규약을 손댄 날 러너가 조용히 반쪽 리포트를 낸다.
    """
    gs = _synthetic_goldset(tmp_path)
    out = tmp_path / "out"
    build(gs, out)
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    m["builder"]["convention_hash"] = "deadbeefdeadbeef"
    (out / "manifest.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

    st = check_gold_flows(out)
    assert st.usable is True
    assert any("지문 불일치" in msg for msg in st.messages)

    # 지문 자체가 없는 구 산출물(커밋된 정본이 이 상태다)도 경고만 하고 축은 켠다
    m.pop("builder")
    (out / "manifest.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    st2 = check_gold_flows(out)
    assert st2.usable is True
    assert any("빌더 지문이 없다" in msg for msg in st2.messages)


def test_규약해시는_주석_변경에는_흔들리지_않는다(tmp_path):
    """오탐하는 경고는 읽히지 않는다 — 주석·docstring 밀도 규약상 설명은 자주 손질된다."""
    from scripts.goldset_eval.build_gold_flows import _py_digest

    src = Path(run_eval.__file__).with_name("flow_schema.py").read_text(encoding="utf-8")
    a = tmp_path / "a.py"
    a.write_text(src, encoding="utf-8")
    b = tmp_path / "b.py"
    b.write_text('"""다른 설명."""\n' + src.split('"""', 2)[2] + "\n# 새 주석\n", encoding="utf-8")
    assert _py_digest(a) == _py_digest(b)

    c = tmp_path / "c.py"
    c.write_text(src.replace("MATCH_THRESHOLD", "MATCH_THRESHOLD_CHANGED"), encoding="utf-8")
    assert _py_digest(a) != _py_digest(c), "실제 로직 변경은 잡아야 한다"


# ── 6. 러너 배선 — 엄밀 축이 `run_eval`에서 실제로 나오는가 ──────────────────
#
# 엄밀 축이 `rescore.py`(재채점 전용)에만 배선돼 있으면, 앞으로 도는 **모든** 골드셋 런은
# 여전히 퍼지 축만 낸다. 계획서 0-1의 산출이 "이후 이 숫자만 신뢰"할 정답지였으므로,
# 러너에 배선되지 않으면 이 작업의 목적 자체가 미달이다.


class _StubAnalysis:
    def model_dump(self):
        return {"steps": [{"title": "t"}]}


def _stub_agent(recommendation: dict):
    """analyze/recommend를 결정론 스텁으로 — LLM·네트워크 없음."""
    async def recommend(analysis, parsed_doc=None):
        yield types.SimpleNamespace(
            model_dump=lambda: {"event": "done",
                                "data": {"recommendation": recommendation}})

    return types.SimpleNamespace(
        analyze=lambda parsed: _StubAnalysis(), recommend=recommend,
        config=types.SimpleNamespace(OPENAI_API_KEY="stub", OPENAI_MODEL="stub-model"))


@pytest.fixture
def runner_env(tmp_path, monkeypatch, stub_catalog):
    """`_run_case`를 LLM 없이 돌릴 수 있는 환경 — 골드셋·정답지·에이전트 스텁 한 벌."""
    gs = _synthetic_goldset(tmp_path)
    (gs / "업무정의서_정규화").mkdir()
    (gs / "업무정의서_정규화" / "case01.md").write_text("업무 설명", encoding="utf-8")
    flows_dir = gs / DEFAULT_OUT_NAME
    build(gs, flows_dir)
    gold_flow = load_gold_flow_case(flows_dir, "case01")
    rec = _pred_from_gold(gold_flow)

    monkeypatch.setattr(run_eval, "agent_module", lambda v: _stub_agent(rec))
    monkeypatch.setattr("app.services.parser.parse_text", lambda text: {"pages": []})
    monkeypatch.setattr("scripts.goldset_eval.coverage.score_coverage",
                        lambda doc_text, flow: {"coverage": 1.0, "n_covered": 1, "n_total": 1})
    return types.SimpleNamespace(goldset=gs, flows_dir=flows_dir, gold_flow=gold_flow,
                                 rec=rec, out=tmp_path / "run",
                                 entry={"index": 1, "case_dir": "case01",
                                        "bot_name": "01_bot", "title": "t"})


def _run(env, gold_flow):
    return asyncio.run(run_eval._run_case(
        env.entry, env.goldset, env.out / "case01", 60.0, [], "v3", gold_flow=gold_flow))


def test_러너가_정답지가_있으면_엄밀축을_함께_낸다(runner_env):
    """정답지를 그대로 베낀 예측이니 엄밀 f1·중첩이 1.0이어야 한다.

    배선이 풀리면(=`gold_flow`를 안 넘기거나 attach를 빼면) 이 키들이 아예 사라진다.
    """
    row = _run(runner_env, runner_env.gold_flow)
    assert row["f1_exact"] == 1.0
    assert row["nesting_path"] == 1.0
    assert row["recall_exact_resolved"] == 1.0

    score = json.loads((runner_env.out / "case01" / "score.json").read_text(encoding="utf-8"))
    assert all(k in score for k in EXACT_SCORE_KEYS)
    # 기존 축은 한 글자도 바뀌지 않는다 — 지난 기준선 런과의 비교가 유지돼야 한다
    assert set(("action", "action_core", "action_equiv")) <= set(score)
    assert row["f1"] == score["action"]["f1"]


def test_정답지가_없으면_기존_축만_낸다(runner_env):
    """정답지 생성 전 환경에서도 러너는 그대로 돌아야 하고, 행 모양이 기존과 같아야 한다."""
    row = _run(runner_env, None)
    assert "f1_exact" not in row and "nesting_path" not in row
    score = json.loads((runner_env.out / "case01" / "score.json").read_text(encoding="utf-8"))
    assert "action_exact" not in score
    assert score["action"]["f1"] == row["f1"]


def test_러너와_재채점기가_같은_엄밀_수치를_낸다(runner_env):
    """두 경로가 갈리면 재채점 수치를 원 런과 비교할 수 없다 — 같은 함수를 쓴다는 계약."""
    from scripts.goldset_eval.rescore import rescore_case

    _run(runner_env, runner_env.gold_flow)
    from_run = json.loads(
        (runner_env.out / "case01" / "score.json").read_text(encoding="utf-8"))
    from_rescore = rescore_case(runner_env.goldset, runner_env.out / "case01",
                                runner_env.entry, [], runner_env.gold_flow)[0]
    for k in EXACT_SCORE_KEYS:
        assert from_run[k] == from_rescore[k], k
    assert from_run["exact_detail"] == from_rescore["exact_detail"]


def test_러너_main이_정답지를_스스로_찾아_엄밀축까지_리포트한다(runner_env, monkeypatch):
    """`_run_case`만 배선하고 `main`이 정답지를 안 넘기면 실제 런은 여전히 퍼지 축만 낸다.

    그래서 CLI 진입점을 통째로 돌려 확인한다(에이전트·파서·카탈로그·커버리지 전량 스텁 —
    LLM 호출 0회). summary/report까지 봐야 '숫자가 실제로 나온다'가 증명된다.
    """
    import sys

    monkeypatch.setattr("app.services.catalog.get_backend_catalog",
                        lambda: types.SimpleNamespace(iter_action_schemas=_kb_specs))
    out_root = runner_env.out / "결과"
    monkeypatch.setattr(sys, "argv", ["run_eval", "--goldset", str(runner_env.goldset),
                                      "--out", str(out_root), "--tag", "wiring"])
    assert asyncio.run(run_eval.main()) == 0

    run_dir = next(p for p in out_root.iterdir() if p.is_dir())
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["rows"][0]["f1_exact"] == 1.0
    assert summary["meta"]["gold_flows"]["resolve_rate"] == 1.0   # 어느 정답지로 쟀는지 남는다
    md = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "엄밀 축" in md and "f1_exact" in md


def test_정답지가_없는_골드셋에서도_러너가_그대로_돈다(runner_env, monkeypatch):
    """정답지 생성 전 환경 — 조용히 기존 축만 내야 한다(에러도, 빈 열도 없이)."""
    import shutil
    import sys

    shutil.rmtree(runner_env.flows_dir)
    monkeypatch.setattr("app.services.catalog.get_backend_catalog",
                        lambda: types.SimpleNamespace(iter_action_schemas=_kb_specs))
    out_root = runner_env.out / "결과2"
    monkeypatch.setattr(sys, "argv", ["run_eval", "--goldset", str(runner_env.goldset),
                                      "--out", str(out_root), "--tag", "nogold"])
    assert asyncio.run(run_eval.main()) == 0

    run_dir = next(p for p in out_root.iterdir() if p.is_dir())
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert "f1_exact" not in summary["rows"][0]
    assert "gold_flows" not in summary["meta"]
    assert summary["rows"][0]["f1"] > 0        # 기존(퍼지) 축은 정상적으로 나온다


def test_엄밀축을_얹어도_기존_축_값은_그대로다(resolved_flow):
    """지난 기준선 런이 전부 기존 축으로 재졌다 — 한 글자라도 바뀌면 비교가 끊긴다."""
    score = {"action": {"f1": 0.42}, "action_core": {"f1": 0.5}, "n_matched": 7}
    before = json.dumps(score, sort_keys=True)
    attach_exact_axes(score, _pred_from_gold(resolved_flow), resolved_flow)
    assert json.dumps({k: score[k] for k in ("action", "action_core", "n_matched")},
                      sort_keys=True) == before
    assert score["action_exact"]["f1"] == 1.0   # 새 축은 별도 키로만 들어온다


def test_엄밀축이_반복평균에_들어간다():
    """`_AGG_KEYS`에 없으면 --repeat>1 런에서 엄밀 축이 요약 행에서 조용히 사라진다."""
    reps = [{"index": 1, "bot_name": "b", "f1": 0.5, "f1_exact": 0.4, "nesting_path": 0.6},
            {"index": 1, "bot_name": "b", "f1": 0.5, "f1_exact": 0.6, "nesting_path": 0.8}]
    row = run_eval._aggregate_reps({"index": 1, "bot_name": "b"}, reps)
    assert row["f1_exact"] == 0.5 and row["f1_exact_std"] == pytest.approx(0.141, abs=1e-3)
    assert row["nesting_path"] == 0.7


def test_리포트에_엄밀축_열과_왜_낮은지_설명이_붙는다(tmp_path):
    """수치만 붙이면 다음 사람이 '엄밀 f1이 낮으니 에이전트가 나빠졌다'로 읽는다."""
    meta = {"tag": "t", "started": "s", "model": "m", "kb_actions": 1,
            "gold_flows": {"dir": "d", "generated_at": "g", "resolve_rate": 0.96}}
    rows = [{"index": 1, "bot_name": "b", "f1": 0.5, "precision": 0.5, "recall": 0.5,
             "f1_exact": 0.2, "recall_exact": 0.2, "recall_exact_core": 0.3,
             "nesting_path": 0.4, "order_exact": 0.9, "precision_exact": 0.2,
             "f1_exact_core": 0.3, "recall_exact_resolved": 0.25,
             "nesting_depth": 0.5, "n_gold_unresolved": 2, "param_jaccard": 0.1}]
    run_eval._write_report(tmp_path, rows, meta)
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "f1_exact" in md and "nesting_path" in md
    assert "엄밀 축" in md and "퍼지" in md

    # 정답지 없는 런의 리포트는 기존과 한 칸도 달라지지 않는다
    run_eval._write_report(tmp_path, [{"index": 1, "bot_name": "b", "f1": 0.5,
                                       "precision": 0.5, "recall": 0.5}], meta)
    md2 = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "f1_exact" not in md2 and "엄밀 축" not in md2


# ─────────────────────────────────────────────────────────────────────────────
# (H) 재채점기 배선 — 판정 함수를 **실제로 쓰는가**
# ─────────────────────────────────────────────────────────────────────────────
#
# 🔴 여기가 비어 있었다. `check_gold_flows`(판정)만 따로 검증돼 있었고, `rescore.main`이
# 그 판정을 쓰는지는 아무도 안 봤다 — 배선을 `manifest.json` 존재 확인으로 되돌려도
# 스위트가 전부 초록이었다(적대 검증 실측). run_eval 쪽에는 대칭 테스트가 있다.

def _rescore_run(env, rec: dict) -> Path:
    """`rescore.main`이 읽을 수 있는 최소 런 디렉터리를 만든다."""
    case = env.out / "case01"
    case.mkdir(parents=True, exist_ok=True)
    (case / "recommendation.json").write_text(
        json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return env.out


def _rescore_stdout(env, monkeypatch, capsys) -> tuple[str, str]:
    from scripts.goldset_eval import rescore

    monkeypatch.setattr(rescore, "_kb_canons", lambda: [])
    rc = rescore.main(["rescore", str(env.goldset), str(env.out)])
    assert rc == 0
    cap = capsys.readouterr()
    return cap.out, cap.err


def test_재채점기가_망가진_정답지로는_엄밀축을_켜지_않는다(runner_env, monkeypatch, capsys):
    """판정 함수가 아니라 **`rescore.main`의 배선**을 고정한다.

    정본 판정을 못 믿게 만든 뒤(카탈로그 없이 만든 정답지로 교체) 재채점기를 돌려,
    엄밀 축 열이 표에서 **사라지는지**를 본다. `main`이 `manifest.json` 존재만 보고
    축을 켜던 옛 배선으로 되돌리면 이 테스트가 빨개진다.
    """
    _rescore_run(runner_env, runner_env.rec)
    # 정본을 카탈로그 없이 만든 산출물로 갈아끼운다(가드 우회를 위해 force).
    build(runner_env.goldset, runner_env.flows_dir, use_catalog=False, force=True)

    out, err = _rescore_stdout(runner_env, monkeypatch, capsys)

    assert "엄밀일치" not in out, "못 믿을 정답지인데 엄밀 축이 켜졌다"
    assert "엄격" in out, "기존 축은 그대로 나와야 한다"
    assert "카탈로그" in err, "축을 끈 이유가 화면에 남아야 사람이 재생성한다"


def test_재채점기가_정상_정답지에서는_엄밀축을_낸다(runner_env, monkeypatch, capsys):
    """위 테스트의 짝 — 조건을 안 건드리면 축이 실제로 켜져야 한다.

    이게 없으면 위 테스트는 '엄밀 축이 아예 배선 안 됨'으로도 통과한다.
    """
    _rescore_run(runner_env, runner_env.rec)
    out, _ = _rescore_stdout(runner_env, monkeypatch, capsys)

    assert "엄밀일치" in out
    assert "중첩 경로 일치율" in out


def test_재채점기가_케이스_누락에_죽지_않는다(runner_env, monkeypatch, capsys):
    """정답지는 믿을 만한데 **이 케이스만** 없는 경우 — 정답셋에 케이스를 추가하고
    정답흐름도 재생성을 잊은 상태다.

    예전엔 axes에 엄밀 축을 넣어둔 채 없는 키를 읽어 `KeyError: action_exact`로 죽었다.
    run_eval은 같은 상황을 경고 후 그 케이스만 생략으로 넘긴다 — 두 경로가 같아야 한다.
    """
    _rescore_run(runner_env, runner_env.rec)
    (runner_env.flows_dir / "cases" / "case01.json").unlink()

    out, err = _rescore_stdout(runner_env, monkeypatch, capsys)

    assert "정답지에 이 케이스가 없다" in err
    assert "엄격" in out, "엄밀 축이 빠져도 기존 축 채점은 계속돼야 한다"
    # 표본이 0인 축을 0.000으로 찍으면 "채점이 나쁘다"로 오독된다
    assert "f1=0.000" not in out.split("매크로")[-1].split("엄밀일치")[-1][:60]
