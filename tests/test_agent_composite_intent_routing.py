"""분석+흐름도 합성 요청이 recommendation까지 가는가 (RPA-218).

## 왜 이 파일이 생겼나

실사용 문구 "이 업무정의서를 분석해서 자동화 흐름도까지 만들어줘"가 v1/v2/v3 모두
`type=answer`로 끝난다는 보고가 있었다(RPA-218). 그러면 백엔드는 계약대로 일반 대화만
저장하고, 추천이 없으니 Output 보증 기록도 만들 수 없다.

원인을 좁혀 보니 **라우팅 로직은 정상**이었다. 실측(현행 코드, 버전당 3회):

    v1 → generate          v2 → generate          v3 → [analyze, generate]

그리고 HTTP SSE E2E에서도 세 버전 모두 `type=recommendation`으로 저장까지 갔다.
문제는 그게 **어디에도 고정돼 있지 않았다**는 것이다 — intake의 결정론 가드(`_guard_plan`)
말고는 "합성 요청이 산출로 이어진다"를 지키는 테스트가 하나도 없었다. 그래서 라우팅이
조용히 퇴화해도 CI가 초록이다.

## 무엇을 고정하나

1. **결정론 체인**: intake가 산출 의도를 냈을 때, 그래프 배선이 그걸 recommendation까지
   실어 나르는가. LLM은 안 태운다 — 분류는 LLM 몫이고 여기서 볼 것은 배선이다.
2. **회귀 방지**: 일반 질문은 여전히 answer로 끝나는가.
3. **분류 자체**(선택): 실제 LLM 호출은 `-m llm`으로만 돈다. 기본 실행에 넣으면 CI가
   요금과 비결정성을 떠안고, 빼면 분류가 영영 검증되지 않는다 — 그래서 옵트인으로 남긴다.
"""

import os

import pytest

from app.agent.v1.orchestrator import graph as v1_graph
from app.agent.v2.orchestrator import graph as v2_graph
from app.agent.v3.orchestrator.graph import respond_node, supervisor_node
from app.agent.v3.orchestrator.intake import _guard_plan
from app.agent.v3.orchestrator.state import (
    ROUTE_ANALYZE,
    ROUTE_EDIT,
    ROUTE_GENERATE,
    ROUTE_QA,
    ROUTES,
    TYPE_ANALYSIS,
    TYPE_ANSWER,
    TYPE_RECOMMENDATION,
)

# 재현 문구 그대로 — 프론트가 실제로 보내는 합성 의도다.
COMPOSITE = "이 업무정의서를 분석해서 자동화 흐름도까지 만들어줘"
SPLIT_ANALYZE = "이 업무정의서를 분석해줘"
SPLIT_GENERATE = "흐름도까지 생성해줘"
PLAIN_QUESTION = "A360에서 엑셀 다루는 패키지가 뭐야?"

_ANALYSIS = {"steps": [{"step_id": "s1"}, {"step_id": "s2"}]}
_EMPTY_ANALYSIS = {"steps": []}


# ─────────────────────────────────────────────────────────────────────────────
# v1 / v2 — 단일 route 배선: generate는 분석본이 없으면 analyze를 경유한다
# ─────────────────────────────────────────────────────────────────────────────

V12 = pytest.mark.parametrize("graph", [v1_graph, v2_graph], ids=["v1", "v2"])


@V12
def test_분석본_없는_generate는_analyze를_경유해_이어_달린다(graph):
    """산출 요청이 분석이 없다고 중간에 멈추면 그게 곧 `type=answer/analysis` 오종착이다."""
    state = {"route": ROUTE_GENERATE, "analysis": None}
    assert graph._route_from_intake(state) == "analyze"
    # analyze가 끝나도 종착은 generate다 — 여기서 END로 새면 recommendation이 안 나온다.
    assert graph._route_after_analyze(state) == "generate"


@V12
def test_빈_분석본도_analyze를_경유한다(graph):
    """steps가 0건인 분석본은 '있음'으로 쳐주지 않는다 — 어휘 없는 흐름도가 나온다."""
    assert graph._route_from_intake({"route": ROUTE_GENERATE, "analysis": _EMPTY_ANALYSIS}) == "analyze"


@V12
def test_분석본이_있으면_generate로_직행한다(graph):
    """이미 분석이 있으면 재분석하지 않는다(같은 턴을 두 번 태우지 않는다)."""
    assert graph._route_from_intake({"route": ROUTE_GENERATE, "analysis": _ANALYSIS}) == "generate"


@V12
def test_analyze_단독은_analyze에서_끝난다(graph):
    """분석만 요청했는데 흐름도까지 만들면 그것대로 계약 위반이다(반대 방향 회귀)."""
    state = {"route": ROUTE_ANALYZE, "analysis": None}
    assert graph._route_from_intake(state) == "analyze"
    assert graph._route_after_analyze(state) != "generate"


@V12
def test_일반_질문은_qa로_남는다(graph):
    """RPA-218 수정이 'qa를 산출로 끌어올리는' 방향으로 새지 않게 못 박는다."""
    assert graph._route_from_intake({"route": ROUTE_QA, "analysis": None}) == "qa"


@V12
def test_미지_route도_산출로_새지_않는다(graph):
    """route가 비었을 때의 하한은 qa다 — 잘못 산출하는 것보다 되묻는 쪽이 안전하다."""
    assert graph._route_from_intake({"analysis": None}) == "qa"


# ─────────────────────────────────────────────────────────────────────────────
# v3 — TaskPlan 순회: [analyze, generate]가 recommendation으로 수렴한다
# ─────────────────────────────────────────────────────────────────────────────


def _fake_analyze(state: dict) -> dict:
    """analyze_node의 반환 계약만 흉내낸다 (LLM 없이)."""
    return {"analysis": _ANALYSIS, "analysis_out": _ANALYSIS,
            "turn_type": TYPE_ANALYSIS, "answer": "업무를 2개 단계로 분해했어요.", "sources": []}


def _fake_analyze_empty(state: dict) -> dict:
    """자동화할 단계를 못 찾은 분석 — generate 전제가 깨지는 경우."""
    return {"analysis": _EMPTY_ANALYSIS, "analysis_out": _EMPTY_ANALYSIS,
            "turn_type": TYPE_ANALYSIS, "answer": "자동화할 단계를 찾지 못했어요.", "sources": []}


def _fake_generate(state: dict) -> dict:
    return {"turn_type": TYPE_RECOMMENDATION, "recommendation_out": {"steps": [{"step_id": "s1"}]},
            "answer": "2개 업무 단계의 자동화 흐름도를 만들었어요.", "sources": []}


def _fake_qa(state: dict) -> dict:
    return {"turn_type": TYPE_ANSWER, "answer": "Excel advanced 패키지를 씁니다.", "sources": []}


def _drive(plan: list[str], nodes: dict, *, analysis: dict | None = None) -> dict:
    """supervisor 순회를 끝까지 돌린다 — 결정론이므로 노드만 페이크로 갈아끼우면 된다.

    LangGraph를 띄우지 않는 이유: 여기서 보려는 건 배선(누가 다음에 뛰는가)이지 런타임이
    아니고, 실제 그래프를 태우면 analyze/generate가 LLM·RAG를 끌고 온다.
    """
    state: dict = {"plan": list(plan), "analysis": analysis, "artifacts": [], "current_task": ""}
    for _ in range(len(ROUTES) * 4):  # 무한 루프 방어 — plan 상한(3)의 넉넉한 배수
        state.update(supervisor_node(state))
        nxt = state["next_node"]
        if nxt == "respond":
            state.update(respond_node(state))
            return state
        state.update(nodes[nxt](state))
    raise AssertionError("supervisor가 수렴하지 않았다 — plan 소비가 멈췄다")


def test_합성_계획이_recommendation으로_수렴한다():
    """[analyze, generate] → 분석·흐름도가 **둘 다** 나오고 최상위 type은 recommendation.

    RPA-218이 보고한 실패는 정확히 이 자리가 answer로 끝나는 것이었다. 백엔드는 이 type으로
    저장을 분기하므로, 여기가 틀리면 추천 저장도 Output 보증 기록도 생기지 않는다.
    """
    final = _drive([ROUTE_ANALYZE, ROUTE_GENERATE],
                   {"analyze": _fake_analyze, "generate": _fake_generate})

    assert final["turn_type"] == TYPE_RECOMMENDATION
    assert final["analysis_out"] == _ANALYSIS          # 분석본도 유실되지 않는다
    assert final["recommendation_out"]["steps"]        # 흐름도가 실제로 실렸다
    assert [a["task"] for a in final["artifacts"]] == [ROUTE_ANALYZE, ROUTE_GENERATE]
    # 두 산출의 답변이 합쳐져 사용자에게 한 번에 간다
    assert "분해했어요" in final["answer"] and "흐름도를 만들었어요" in final["answer"]


def test_generate_단독_계획도_분석을_선행해_수렴한다():
    """intake가 generate 하나만 냈어도(v1/v2 계약과 동형) 분석을 경유해 흐름도까지 간다.

    프롬프트가 "generate면 analyze를 함께 나열할 필요 없다"고 말하므로, 이 경로가 실사용의
    기본값이다 — 여기가 막히면 합성 요청 대부분이 흐름도 없이 끝난다.
    """
    final = _drive([ROUTE_GENERATE], {"analyze": _fake_analyze, "generate": _fake_generate})

    assert final["turn_type"] == TYPE_RECOMMENDATION
    assert [a["task"] for a in final["artifacts"]] == [ROUTE_ANALYZE, ROUTE_GENERATE]


def test_분석_단계가_0건이면_generate를_거둬들인다():
    """자동화 대상이 없는데 흐름도를 억지로 만들지 않는다 — 이때만 recommendation이 아니다.

    '합성 요청은 항상 recommendation'으로 못 박으면 이 결정론 재계획이 죽는다. 정상 종료를
    함께 고정해 두 규칙이 서로를 덮지 않게 한다.
    """
    final = _drive([ROUTE_ANALYZE, ROUTE_GENERATE],
                   {"analyze": _fake_analyze_empty, "generate": _fake_generate})

    assert final["turn_type"] == TYPE_ANALYSIS
    assert [a["task"] for a in final["artifacts"]] == [ROUTE_ANALYZE]  # generate는 안 뛰었다


def test_qa가_섞여도_대표_type은_recommendation이다():
    """복합 턴에서 답변이 섞여도 저장 가치가 큰 산출이 대표 type이 된다."""
    final = _drive([ROUTE_GENERATE, ROUTE_QA],
                   {"analyze": _fake_analyze, "generate": _fake_generate, "qa": _fake_qa})

    assert final["turn_type"] == TYPE_RECOMMENDATION
    assert [a["task"] for a in final["artifacts"]] == [ROUTE_ANALYZE, ROUTE_GENERATE, ROUTE_QA]


def test_질문만_있으면_answer로_끝난다():
    """회귀 방지 — 일반 대화가 산출 경로로 끌려가지 않는다."""
    final = _drive([ROUTE_QA], {"qa": _fake_qa})
    assert final["turn_type"] == TYPE_ANSWER
    assert final["recommendation_out"] is None if "recommendation_out" in final else True


# ─────────────────────────────────────────────────────────────────────────────
# 결정론 가드가 산출 의도를 깎지 않는다
# ─────────────────────────────────────────────────────────────────────────────


def test_가드가_합성_계획을_보존한다():
    """`_guard_plan`은 오분류의 하한만 보정한다 — 멀쩡한 합성 계획을 줄이면 안 된다."""
    no_flow = {"recommendation": None, "message": COMPOSITE}
    assert _guard_plan([ROUTE_ANALYZE, ROUTE_GENERATE], no_flow)[0] == [ROUTE_ANALYZE, ROUTE_GENERATE]
    assert _guard_plan([ROUTE_GENERATE], no_flow)[0] == [ROUTE_GENERATE]


def test_가드가_합성_문구를_edit로_상향하지_않는다():
    """"만들어줘"에 수정 동사가 없다 — 흐름도가 있어도 신규 산출 의도가 edit로 바뀌면 안 된다.

    RPA-98 가드는 qa 단일 계획에만 관여한다. 그 범위가 넓어지면 재생성 요청이 기존 흐름도
    수정으로 둔갑한다.
    """
    with_flow = {"recommendation": {"steps": [{}]}, "message": COMPOSITE}
    assert _guard_plan([ROUTE_GENERATE], with_flow)[0] == [ROUTE_GENERATE]
    assert ROUTE_EDIT not in _guard_plan([ROUTE_ANALYZE, ROUTE_GENERATE], with_flow)[0]


# ─────────────────────────────────────────────────────────────────────────────
# 프롬프트 계약 — 라우트가 조용히 사라지지 않는다
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
def test_intake_프롬프트가_네_라우트를_모두_설명한다(version):
    """라우트 하나가 프롬프트에서 빠지면 LLM은 그걸 영영 안 고른다 — 코드는 멀쩡한 채로.

    RPA-218의 증상(모든 요청이 answer)이 정확히 그런 모양이라, 어휘 자체를 고정해 둔다.
    """
    import importlib

    mod = importlib.import_module(f"app.agent.{version}.orchestrator.intake")
    prompt = mod._PROMPT
    for route in ROUTES:
        assert f'"{route}"' in prompt, f"{version} intake 프롬프트에 {route} 설명이 없다"


def test_v3_프롬프트가_복합_요청을_명시한다():
    """v3만 복수 task를 낼 수 있다 — 출력 계약(`tasks`)이 사라지면 합성 의도가 하나로 눌린다."""
    from app.agent.v3.orchestrator.intake import _PROMPT

    assert '"tasks"' in _PROMPT


# ─────────────────────────────────────────────────────────────────────────────
# 분류 자체 (옵트인) — 실제 LLM 호출
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.llm
@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (COMPOSITE, ROUTE_GENERATE),
        (SPLIT_ANALYZE, ROUTE_ANALYZE),
        (SPLIT_GENERATE, ROUTE_GENERATE),
        (PLAIN_QUESTION, ROUTE_QA),
    ],
    ids=["합성", "분석만", "흐름도만", "일반질문"],
)
def test_실제_분류(monkeypatch, version, message, expected):
    """실 LLM 분류 — `RUN_LLM_TESTS=1 pytest -m llm`일 때만 돈다.

    끄는 방식을 addopts가 아니라 **테스트 자신의 skip**으로 둔 이유: CI가 `-m "not integration"`
    같은 필터를 주면 addopts의 `-m`이 통째로 덮어써져 요금 나가는 테스트가 조용히 켜진다.

    합성 문구는 v3에서 `[analyze, generate]`, v1/v2에서 `generate`로 나온다. 어느 쪽이든
    **흐름도 산출이 계획에 들어 있는가**를 본다 — 앞에 analyze가 붙는지는 버전 계약 차이지
    분류의 성패가 아니다.
    """
    import importlib

    if os.getenv("RUN_LLM_TESTS") != "1":
        pytest.skip("실 LLM 테스트는 RUN_LLM_TESTS=1 일 때만 실행")
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY 없음")
    # 사용량 기록은 DB를 탄다 — 분류만 보는 테스트가 인프라를 요구하지 않게 끊는다.
    monkeypatch.setattr("app.core.llm.record_usage", lambda **kwargs: None)

    mod = importlib.import_module(f"app.agent.{version}.orchestrator.intake")
    state = {
        "message": message, "solution": "a360", "operation": "chat", "history": [],
        "compact": None, "analysis": None, "recommendation": None,
        "parsed_doc": {"page_count": 1, "full_text": "매일 오전 9시에 포털에 로그인해 "
                                                     "매출 엑셀을 내려받고 보고서를 메일로 보낸다."},
    }
    out = mod.intake_node(state)
    plan = out.get("plan") or [out["route"]]
    assert expected in plan, f"{version}: {message!r} → {plan}"
