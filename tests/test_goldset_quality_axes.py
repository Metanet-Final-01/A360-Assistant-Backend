# -*- coding: utf-8 -*-
"""납득 기준 L1(문서 근거성)·L3(독립 LLM 심판) — 설계 제약 #13, §4 측정 계층.

제약 #13은 납득 기준을 **4개 모두**로 확정했는데 하네스에는 재현율(L2)만 있었다.
이 두 축이 재현율이 답하지 못하는 질문에 답한다.

이 파일이 지키는 계약은 셋이다.

1. **L3는 정답 봇을 보지 않는다.** 보는 순간 재현율의 복사본이 되고, 재현율이 못 잡는
   대안 경로(케이스 01의 `Jira/Create project` vs `Rest/restPost`)를 똑같이 놓친다.
2. **못 쟀으면 0점이 아니라 무측정이다.** 둘을 같은 숫자로 만들면 축을 못 읽는다 —
   반증 심판 구현에서 실제로 났던 결함과 같은 부류다(LLM 실패가 만점이 됐다).
3. **L1은 결정론이다.** LLM·DB를 안 탄다. 여기서 LLM이 불리면 "비용 0"이라는 전제가 깨진다.

LLM은 전량 monkeypatch — 이 파일은 API를 호출하지 않는다.
"""

import pytest

from scripts.goldset_eval.quality_axes import (
    groundedness,
    judge_axis,
    l1_row,
    l3_row,
)


def _action(pkg="Excel advanced", act="Open", sources=None, params=None, children=None):
    return {
        "package": pkg, "action": act, "order": 1,
        "sources": sources if sources is not None else [],
        "parameters": params or [],
        "children": children or [],
    }


def _flow(*actions, **kw):
    base = {"schema_version": "1.0",
            "steps": [{"step_id": "s1", "label": "l", "actions": list(actions)}]}
    base.update(kw)
    return base


_SRC = [{"source_type": "action_schema", "title": "Excel advanced/Open", "score": 0.9}]


# ── L1 근거성 — 결정론 ───────────────────────────────────────────────────────

def test_인용된_액션_비율을_잰다():
    flow = _flow(_action(sources=_SRC), _action("Email", "Send", sources=[]))
    g = groundedness(flow)
    assert g["action_cited"] == 0.5
    assert (g["n_action_cited"], g["n_action_citable"]) == (1, 2)


def test_구조_액션은_분모에서_뺀다():
    """🔴 안 빼면 '구조를 잘 갖출수록 근거성이 떨어진다'는 거꾸로 된 지표가 된다.

    Loop·If·Error handler는 `structural_complement`가 카탈로그 직조회로 넣는다 —
    검색 히트가 없는 게 정상이고 그게 결함이 아니다.
    """
    flow = _flow(
        _action(sources=_SRC),
        _action("Loop", "For each row in table", sources=[]),
        _action("If", "If", sources=[]),
        _action("Error handler", "Try", sources=[]),
    )
    g = groundedness(flow)
    assert g["n_action_citable"] == 1
    assert g["action_cited"] == 1.0


def test_컨테이너_안의_액션도_센다():
    """중첩 액션을 놓치면 Loop 본문이 통째로 근거성 집계에서 빠진다."""
    inner = _action("Email", "Send", sources=[])
    flow = _flow(_action("Loop", "For each row in table", children=[inner]))
    g = groundedness(flow)
    assert g["n_action_citable"] == 1      # Loop는 제외, 본문 Email만
    assert g["action_cited"] == 0.0


@pytest.mark.parametrize("source,grounded", [
    ("schema_default", True),   # 카탈로그 기본값
    ("user", True),             # 사용자 지정
    ("llm", False),             # 모델이 정함 = 근거 없음
])
def test_파라미터_값의_출처로_근거성을_가른다(source, grounded):
    """`llm`이 높으면 비전문가가 못 고치는 값을 모델이 임의로 정하고 있다는 뜻이다(제약 #10)."""
    flow = _flow(_action(params=[{"name": "p", "value": "v", "value_source": source}]))
    assert groundedness(flow)["param_grounded"] == (1.0 if grounded else 0.0)


def test_값이_없는_파라미터는_세지_않는다():
    """자리표시자(value=null)는 '근거 없이 채웠다'가 아니라 '정직하게 비웠다'다."""
    flow = _flow(_action(params=[
        {"name": "a", "value": None, "value_source": "llm"},
        {"name": "b", "value": "", "value_source": "llm"},
        {"name": "c", "value": "x", "value_source": "schema_default"},
    ]))
    g = groundedness(flow)
    assert g["n_param_valued"] == 1
    assert g["param_grounded"] == 1.0


def test_잴_것이_없으면_None이지_0이_아니다():
    """0.0은 '전부 근거 없음'이고 None은 '잴 게 없음'이다 — 매크로 평균에서 갈린다."""
    g = groundedness(_flow())
    assert g["action_cited"] is None and g["param_grounded"] is None


def test_L1은_LLM을_부르지_않는다(monkeypatch):
    """'비용 0'이 이 축의 전제다 — 깨지면 매 런에 조용히 요금이 붙는다."""
    import app.agent.v4.orchestrator.jsonio as jsonio

    def _boom(*a, **kw):
        raise AssertionError("L1이 LLM을 불렀다")

    monkeypatch.setattr(jsonio, "chat_json", _boom)
    groundedness(_flow(_action(sources=_SRC), _action("Email", "Send")))


# ── L3 독립 심판 ─────────────────────────────────────────────────────────────

_SPEC = {"goal": "환율을 조회해 엑셀에 기록한다",
         "requirements": [{"req_id": "req-1", "text": "환율 조회", "priority": "must"}]}


def _patch_judge(monkeypatch, *, expectations=None, refutation=None,
                 blind_raises=None, refute_raises=None, seen=None):
    """judge의 두 LLM 진입점을 결정론 스텁으로."""
    import scripts.goldset_eval.quality_axes as qa

    def _blind(spec, document=None, purpose="eval_judge"):
        if seen is not None:
            seen.append({"spec": spec, "document": document})
        if blind_raises:
            raise blind_raises
        return expectations if expectations is not None else [
            {"exp_id": "E1", "text": "환율을 조회한다", "criticality": "must", "req_ids": ["req-1"]},
            {"exp_id": "E2", "text": "엑셀에 기록한다", "criticality": "must", "req_ids": []},
        ]

    def _refute(spec, exps, flow, violations=None, purpose="eval_judge"):
        if refute_raises:
            raise refute_raises
        return refutation if refutation is not None else {"defects": [], "unmet": [], "strengths": []}

    import app.agent.v4.orchestrator.judge as judge_mod

    monkeypatch.setattr(judge_mod, "blind_expectations", _blind)
    monkeypatch.setattr(judge_mod, "refute_flow", _refute)
    return qa


def test_기대를_전부_충족하면_만점이다(monkeypatch):
    _patch_judge(monkeypatch)
    out = judge_axis(_flow(_action()), _SPEC)
    assert out["met_rate"] == 1.0 and out["soundness"] == 1.0


def test_미충족과_결함을_따로_잰다(monkeypatch):
    """🔴 합치면 '전부 다뤘지만 다 깨진다'와 '절반만 다뤘지만 다 돌아간다'가 같은 점수가 된다.

    그 둘은 고칠 방법이 정반대라 한 숫자로 뭉치면 지표가 방향을 못 준다.
    """
    _patch_judge(monkeypatch, refutation={
        "unmet": ["E2"], "defects": [{"severity": "fatal", "claim": "세션 미종료"}],
    })
    out = judge_axis(_flow(_action()), _SPEC)
    assert out["met_rate"] == 0.5      # must 2건 중 1건 미충족
    assert out["soundness"] < 1.0      # 치명 결함 1건
    assert out["n_fatal"] == 1


def test_심판은_정답_봇을_보지_않는다(monkeypatch):
    """🔴 이 축의 존재 이유. 정답이 새어 들어가면 재현율의 복사본이 된다.

    실측 근거: 케이스 01에서 에이전트가 `Jira/Create project`를 냈는데 정답이
    `Rest/restPost`라 재현율 0점이었다 — 전용 패키지 쪽이 더 관용적인데도 감점이다.
    L3에 정답이 들어가면 그 편향을 그대로 물려받는다.
    """
    seen: list[dict] = []
    _patch_judge(monkeypatch, seen=seen)
    judge_axis(_flow(_action()), _SPEC, document="업무정의서 원문")

    payload = repr(seen)
    assert "Rest" not in payload and "restPost" not in payload
    assert set(seen[0]["spec"]) <= {"goal", "requirements", "inputs", "outputs",
                                    "error_policy", "unknowns", "assumptions"}
    assert seen[0]["document"] == "업무정의서 원문"


@pytest.mark.parametrize("kw", [
    {"blind_raises": RuntimeError("rate limit")},
    {"refute_raises": RuntimeError("rate limit")},
    {"expectations": []},
])
def test_못_쟀으면_None이지_0점이_아니다(monkeypatch, kw):
    """🔴 '측정 실패'와 '나쁜 흐름도'를 같은 숫자로 만들면 그 축은 못 읽는다.

    반증 심판 구현에서 실제로 났던 결함과 같은 부류다(LLM 실패가 **만점**이 됐다).
    여기선 반대 방향으로 틀리기 쉽다 — 실패를 0점으로 적으면 매크로가 조용히 내려간다.
    """
    _patch_judge(monkeypatch, **kw)
    assert judge_axis(_flow(_action()), _SPEC) is None


def test_must가_없으면_충족률은_None이다(monkeypatch):
    """should만 있는 기대에 충족률 1.0을 주면 '아무것도 안 해도 만점'이 된다."""
    _patch_judge(monkeypatch, expectations=[
        {"exp_id": "E1", "text": "로그를 남긴다", "criticality": "should", "req_ids": []},
    ])
    assert judge_axis(_flow(_action()), _SPEC)["met_rate"] is None


# ── 행 조립 ─────────────────────────────────────────────────────────────────

def test_L1은_항상_행에_붙는다():
    """L1(근거성)과 흐름 내부 일관성(R17/R18 대응)은 둘 다 결정론이라 조건 없이 붙는다.

    `L1_ROW_KEYS`와 정확히 일치해야 한다 — 행에만 넣고 `_AGG_KEYS`에 안 넣으면
    반복 실행 평균에서 조용히 빠진다(그 반대도 마찬가지).
    """
    from scripts.goldset_eval.quality_axes import L1_ROW_KEYS

    row = l1_row(_flow(_action(sources=_SRC)))
    assert set(row) == set(L1_ROW_KEYS)
    assert {"action_cited", "param_grounded"} <= set(row), "근거성 축"
    assert {"session_pkg_breaks", "scaffold_claims"} <= set(row), "흐름 내부 일관성 축"


def test_L3를_못_냈으면_행에_키가_아예_없다():
    """0으로 채우면 --judge 안 켠 런과 켰는데 실패한 런이 구별되지 않는다."""
    assert l3_row(None) == {}


def test_L3를_냈으면_스칼라만_행에_올린다():
    row = l3_row({"met_rate": 0.5, "soundness": 0.6, "n_unmet_must": 1, "n_fatal": 1,
                  "expectations": [{"exp_id": "E1"}], "defects": [{"severity": "fatal"}]})
    assert row == {"judge_met_rate": 0.5, "judge_soundness": 0.6,
                   "judge_unmet_must": 1, "judge_fatal": 1}


# ── 배선 ─────────────────────────────────────────────────────────────────────

def test_러너가_L1을_항상_L3를_플래그로_낸다():
    """L3가 기본으로 켜지면 매 런에 케이스·반복당 LLM 2콜이 조용히 붙는다."""
    import inspect

    from scripts.goldset_eval import run_eval

    src = inspect.getsource(run_eval._run_case)
    assert "l1_row(recommendation)" in src, "L1은 조건 없이 붙어야 한다"
    assert "if judge:" in src, "L3는 opt-in이어야 한다"

    main_src = inspect.getsource(run_eval.main)
    assert "--judge" in main_src and "judge=args.judge" in main_src


def test_심판_축이_파이프라인과_같은_함수를_쓴다():
    """따로 만들면 파이프라인이 쓰는 기대와 채점이 쓰는 기대가 갈려, '심판 점수가 올랐다'가
    무엇을 뜻하는지 알 수 없게 된다."""
    import inspect

    from app.agent.v4.orchestrator import judge as judge_mod
    from scripts.goldset_eval import quality_axes

    assert "from app.agent.v4.orchestrator.judge import" in inspect.getsource(quality_axes.judge_axis)
    for fn in ("blind_expectations", "refute_flow", "score_expectations"):
        assert callable(getattr(judge_mod, fn)), f"{fn}이 공개 진입점이어야 한다"
