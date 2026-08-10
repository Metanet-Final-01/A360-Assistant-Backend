"""카탈로그 주입 — 타 솔루션도 같은 v3 품질 루프를 탄다 (RPA-285 1단계).

이 경로는 그동안 **테스트가 0건**이었다. v3의 타 솔루션 가지는 프로덕션에서 도달 불가라
(세션 solution을 쓰는 코드가 없었다) 아무도 깨뜨릴 수 없었고, 유일한 타 솔루션 테스트인
test_orchestrator_turn.py는 v2를 대상으로 한다. 주입으로 되살리면서 그 공백을 메운다.
"""

import asyncio

import pytest

from app.agent.v3.catalog_context import CatalogContext, a360_context, user_catalog_context
from app.agent.v3.orchestrator import generate as gen_mod
from app.agent.v3.orchestrator.generate import (
    CatalogExtraction,
    UserCatalog,
    UserCatalogAction,
    UserCatalogParam,
    resolve_catalog_context,
)
from app.agent.v3.orchestrator.tools import build_kb_tools
from app.agent.v3.recommend.research import build_dossier

UIPATH_ACTIONS = [
    UserCatalogAction(
        package="UiPath.Excel.Activities", action="ReadRange",
        parameters=[UserCatalogParam(name="SheetName", type="TEXT", required=True)],
    ),
    UserCatalogAction(package="UiPath.Mail.Activities", action="SendOutlookMail"),
]


def _user_ctx():
    specs = [a.as_spec() for a in UIPATH_ACTIONS]
    return user_catalog_context(UserCatalog(specs), "uipath")


# ─────────────────────────────────────────────────────────────────────────────
# CatalogContext — 어휘 출처를 나르는 값
# ─────────────────────────────────────────────────────────────────────────────

def test_a360_context_is_searchable():
    ctx = a360_context()
    assert ctx.searchable and ctx.is_a360


def test_user_catalog_context_is_not_searchable():
    """사용자 제공 카탈로그는 검색할 KB가 없다 — 전량이 곧 메뉴다."""
    ctx = _user_ctx()
    assert not ctx.searchable
    assert not ctx.is_a360
    assert ctx.solution == "uipath"


def test_user_catalog_iterates_and_looks_up():
    cat = _user_ctx().catalog
    assert cat.get_action_schema("UiPath.Excel.Activities", "ReadRange") is not None
    assert cat.get_action_schema("Excel advanced", "cloudExcelOpen") is None
    assert len(list(cat.iter_action_schemas())) == 2


# ─────────────────────────────────────────────────────────────────────────────
# resolve_catalog_context — 세션 solution → 어휘 출처
# ─────────────────────────────────────────────────────────────────────────────

def test_resolve_defaults_to_a360():
    ctx = asyncio.run(resolve_catalog_context({"solution": "a360"}))
    assert ctx.is_a360 and ctx.searchable
    # solution 미지정도 a360으로 본다
    assert asyncio.run(resolve_catalog_context({})).is_a360


def test_resolve_builds_user_catalog_for_other_solution(monkeypatch):
    monkeypatch.setattr(
        gen_mod, "extract_user_catalog",
        lambda state: CatalogExtraction(solution="uipath", actions=UIPATH_ACTIONS),
    )
    ctx = asyncio.run(resolve_catalog_context({"solution": "uipath", "message": "만들어줘"}))
    assert not ctx.searchable
    assert ctx.catalog.get_action_schema("UiPath.Excel.Activities", "ReadRange") is not None


def test_resolve_returns_none_when_no_catalog_found(monkeypatch):
    """어휘가 없으면 None — 호출부가 A360 어휘로 만들지 않고 되묻는다(조용한 오답 방지)."""
    monkeypatch.setattr(gen_mod, "extract_user_catalog", lambda state: CatalogExtraction(actions=[]))
    assert asyncio.run(resolve_catalog_context({"solution": "uipath", "message": "만들어줘"})) is None


def test_generate_node_asks_for_catalog_when_missing(monkeypatch):
    monkeypatch.setattr(gen_mod, "extract_user_catalog", lambda state: CatalogExtraction(actions=[]))

    async def boom(*a, **k):
        raise AssertionError("어휘가 없으면 흐름도를 만들지 않아야 함")

    monkeypatch.setattr(gen_mod, "generate_flow", boom)
    out = asyncio.run(gen_mod.generate_node({"solution": "uipath", "message": "만들어줘"}))
    assert out["turn_type"] == "answer"
    assert "카탈로그" in out["answer"]


# ─────────────────────────────────────────────────────────────────────────────
# 주입된 컨텍스트가 파이프라인 동작을 바꾼다
# ─────────────────────────────────────────────────────────────────────────────

def test_dossier_uses_whole_catalog_when_not_searchable():
    """검색기가 없으면 카탈로그 전량이 메뉴다 — 사용자가 준 액션이 누락되면 안 된다."""
    dossier = asyncio.run(build_dossier({"goal": "메일 보내기"}, [], _user_ctx()))
    assert set(dossier["actions"]) == {
        ("UiPath.Excel.Activities", "ReadRange"),
        ("UiPath.Mail.Activities", "SendOutlookMail"),
    }
    # 메뉴 한 줄은 칸 이름을 직접 말한다 — 모델이 슬래시를 쪼개 추측하지 않게 (RPA-354)
    assert 'package="UiPath.Excel.Activities" action="ReadRange"' in dossier["menu"]
    assert "SheetName" in dossier["menu"]  # 파라미터 스펙까지 실린다


def test_dossier_searches_when_searchable():
    """a360은 검색으로 어휘를 좁힌다 — 전량 메뉴가 아니다(어휘가 수천 개)."""
    dossier = asyncio.run(build_dossier({"goal": "엑셀 읽기", "requirements": []}, [], a360_context()))
    assert isinstance(dossier["menu"], str)
    # 스텁 카탈로그 전량이 아니라 검색 히트 기반이라 UiPath 액션은 당연히 없다
    assert "UiPath" not in dossier["menu"]


def test_kb_search_tool_is_withheld_without_retriever():
    """검색할 KB가 없는데 툴을 쥐여주면 LLM이 빈 결과를 '카탈로그에 없음'으로 오판한다."""
    names = {t.name for t in build_kb_tools([], _user_ctx())}
    assert names == {"get_action_schema"}

    a360_names = {t.name for t in build_kb_tools([], a360_context())}
    assert a360_names == {"search_kb", "get_action_schema"}


def test_generate_passes_user_catalog_into_the_same_pipeline(monkeypatch):
    """핵심 계약: 타 솔루션도 generate_flow(=v3 품질 루프)를 탄다. 별도 경로가 아니다."""
    monkeypatch.setattr(
        gen_mod, "extract_user_catalog",
        lambda state: CatalogExtraction(solution="uipath", actions=UIPATH_ACTIONS),
    )
    monkeypatch.setattr(gen_mod, "build_flow_spec", lambda state, doc: {"goal": "g", "requirements": []})
    seen = {}

    async def fake_generate_flow(analysis, document, spec, ctx):
        seen["ctx"] = ctx
        return {"recommendation": {"steps": []}, "violations": []}

    monkeypatch.setattr(gen_mod, "generate_flow", fake_generate_flow)

    out = asyncio.run(gen_mod.generate_node(
        {"solution": "uipath", "message": "만들어줘", "analysis": {"steps": []}}
    ))
    assert out["turn_type"] == "recommendation"
    ctx = seen["ctx"]
    assert isinstance(ctx, CatalogContext) and not ctx.searchable
    assert ctx.catalog.get_action_schema("UiPath.Mail.Activities", "SendOutlookMail") is not None


def test_trigger_and_foreign_notice_are_a360_only(monkeypatch):
    """트리거 제안·타 솔루션 안내는 A360 세션에서만 — 타 솔루션엔 대응물이 없다."""
    monkeypatch.setattr(
        gen_mod, "extract_user_catalog",
        lambda state: CatalogExtraction(solution="uipath", actions=UIPATH_ACTIONS),
    )
    monkeypatch.setattr(gen_mod, "build_flow_spec", lambda state, doc: {"goal": "매일 아침 9시", "requirements": []})

    async def fake_generate_flow(analysis, document, spec, ctx):
        return {"recommendation": {"steps": [{"step_id": "s1", "actions": []}]}, "violations": []}

    monkeypatch.setattr(gen_mod, "generate_flow", fake_generate_flow)

    def trigger_boom(*a, **k):
        raise AssertionError("타 솔루션에는 A360 트리거를 제안하지 않아야 함")

    monkeypatch.setattr(gen_mod, "recommend_trigger", trigger_boom)
    out = asyncio.run(gen_mod.generate_node(
        {"solution": "uipath", "message": "매일 아침 만들어줘", "analysis": {"steps": []}}
    ))
    assert "trigger" not in out["recommendation_out"]
    # A360 카탈로그 기준 안내도 붙지 않는다(이 세션의 어휘가 곧 사용자 카탈로그이므로)
    assert "A360 카탈로그로" not in out["answer"]


@pytest.mark.parametrize("solution", ["a360", "uipath"])
def test_both_paths_reach_the_same_entry_point(monkeypatch, solution):
    """분기는 '어느 파이프라인'이 아니라 '어느 카탈로그'다 — 진입점은 하나다."""
    monkeypatch.setattr(
        gen_mod, "extract_user_catalog",
        lambda state: CatalogExtraction(solution="uipath", actions=UIPATH_ACTIONS),
    )
    monkeypatch.setattr(gen_mod, "build_flow_spec", lambda state, doc: {"goal": "g", "requirements": []})
    monkeypatch.setattr(gen_mod, "recommend_trigger", lambda *a, **k: None)
    calls = []

    async def fake_generate_flow(analysis, document, spec, ctx):
        calls.append(ctx.solution)
        return {"recommendation": {"steps": []}, "violations": []}

    monkeypatch.setattr(gen_mod, "generate_flow", fake_generate_flow)
    asyncio.run(gen_mod.generate_node(
        {"solution": solution, "message": "만들어줘", "analysis": {"steps": []}}
    ))
    assert calls == [solution]


# ─────────────────────────────────────────────────────────────────────────────
# Qodo 리뷰 반영 (RPA-285)
# ─────────────────────────────────────────────────────────────────────────────

def test_user_catalog_is_never_narrowed_by_search():
    """타 솔루션은 **무조건 전량**이 메뉴다 — 크기와 무관하게 검색으로 좁히지 않는다.

    한때 큰 카탈로그를 인메모리 검색기로 좁혔다가 걷어냈다. 실측(PAD 448개, 같은 문서)이
    좁히는 쪽에 불리했다 — 전량은 액션 23개에 환각(R1) 0건, 검색은 액션 8개였다. 위반 수는
    검색이 적었지만 대부분 R3(필수값 미입력)이라 질문 카드로 흡수되는 항목이고, 굴러가는
    흐름도를 내는 것은 전량 쪽이었다. 사용자가 카탈로그를 준다는 건 "이 어휘로 만들어라"는
    뜻이고, 그중 무엇을 쓸지는 우리가 미리 좁힐 게 아니라 모델이 업무를 보고 고를 일이다.
    """
    from app.agent.v3.recommend import research as research_mod

    many = [
        UserCatalogAction(package="Big", action=f"Act{i}")
        for i in range(research_mod._MAX_USER_MENU_ACTIONS + 25)
    ]
    ctx = user_catalog_context(UserCatalog([a.as_spec() for a in many]), "uipath")
    assert not ctx.searchable, "사용자 카탈로그에는 검색기를 붙이지 않는다"


def test_user_menu_is_capped_and_says_so():
    """유일한 규모 방어는 메뉴 상한이다 — 두되 **조용히** 자르지는 않는다.

    잘린 액션은 composer가 영영 못 쓴다. 그래서 잘렸다는 사실을 프롬프트·로그·진행 메시지
    셋 다에 남긴다.
    """
    from app.agent.v3.recommend import research as research_mod

    many = [
        UserCatalogAction(package="Big", action=f"Act{i}")
        for i in range(research_mod._MAX_USER_MENU_ACTIONS + 25)
    ]
    ctx = user_catalog_context(UserCatalog([a.as_spec() for a in many]), "uipath")
    dossier = asyncio.run(build_dossier({"goal": "g"}, [], ctx))

    assert len(dossier["actions"]) == research_mod._MAX_USER_MENU_ACTIONS
    assert dossier["dropped"] == 25
    assert "주의" in dossier["menu"] and "25" in dossier["menu"]


def test_small_user_catalog_is_not_capped():
    """일반적인 규모(수십 개)는 그대로 전량 실린다 — 상한은 병적 입력 방어용이다."""
    dossier = asyncio.run(build_dossier({"goal": "g"}, [], _user_ctx()))
    assert dossier["dropped"] == 0
    assert "주의" not in dossier["menu"]


# ─────────────────────────────────────────────────────────────────────────────
# R6 어휘 의존 — 사전이 모르는 카탈로그에서는 컨테이너를 판정하지 않는다 (RPA-285)
# ─────────────────────────────────────────────────────────────────────────────

def _with_children(package, action):
    return [{
        "package": package, "action": action, "parameters": [],
        "children": [{"package": "X", "action": "Y", "parameters": [], "children": []}],
    }]


def test_r6_is_silent_when_container_vocabulary_is_unknown():
    """타 솔루션 카탈로그의 제어 흐름 액션이 R6으로 오탐되면 안 된다.

    `CONTAINER_PACKAGES`는 A360 패키지 이름(Loop·If·Step·Error handler)이라, PAD의
    `Loops`·`Conditionals`에는 하나도 안 맞는다. 실측: PAD 448개로 만든 흐름도의 R6 4건이
    전부 이 오탐이었다(`Loops/Loop` 1건, `Conditionals/If` 3건).
    """
    from app.agent.v3.verify.checker import container_vocabulary_known, run_checks

    catalog = UserCatalog([
        UserCatalogAction(package="Loops", action="Loop").as_spec(),
        UserCatalogAction(package="Conditionals", action="If").as_spec(),
        UserCatalogAction(package="X", action="Y").as_spec(),
    ])
    assert not container_vocabulary_known(catalog)
    assert [v for v in run_checks(_with_children("Loops", "Loop"), catalog) if v.rule == "R6"] == []


def test_r6_still_fires_when_vocabulary_is_known():
    """A360 표기를 쓰는 카탈로그면 그대로 판정한다 — 솔루션 이름이 아니라 어휘로 가른다."""
    from app.agent.v3.verify.checker import container_vocabulary_known, run_checks

    catalog = UserCatalog([
        UserCatalogAction(package="Loop", action="cloudUsingLoopAction").as_spec(),
        UserCatalogAction(package="Email", action="sendMail").as_spec(),
    ])
    assert container_vocabulary_known(catalog)
    got = run_checks(_with_children("Email", "sendMail"), catalog)
    assert any(v.rule == "R6" for v in got), "컨테이너가 아닌 액션의 children은 여전히 잡혀야 한다"
