"""v4 접착제 승격 · 조작 단위 확정 (RPA-298, 설계 Phase 3).

두 가지를 못 박는다.

1. **구현 접착제를 요구로 승격** — 문서가 한 번도 말하지 않는 배관(경로 조립·폴더 생성·
   로깅·날짜 포맷)이 놓친 정답의 53%인데, 요구에 없으면 누락 판정이 영영 못 본다.
   spec 단계에서 도출해 requirements에 올리되 **원 요구와 구분되게**(source=inferred,
   priority=should) 찍힌다.
2. **조작 단위를 액션 선택 전에 확정** — 액션 어휘를 몰라도 답할 수 있는 '분해' 질문을
   compose에서 떼어낸다.

LLM은 전부 몽키패치한다 — 여기서 검증하는 것은 프롬프트 렌더·파싱·정규화의 **결정론**이지
모델 출력 품질이 아니다(API 한도 상황과도 무관하게 돌아야 한다).
"""

import asyncio

import pytest

from app.agent.v4.catalog_context import a360_context, user_catalog_context
from app.agent.v4.orchestrator import spec as spec_mod
from app.agent.v4.orchestrator.generate import UserCatalog, UserCatalogAction
from app.agent.v4.orchestrator.spec import (
    MAX_GLUE_REQUIREMENTS,
    _SpecDraft,
    build_flow_spec,
    merge_glue_requirements,
)
from app.agent.v4.recommend import research as research_mod
from app.agent.v4.recommend.research import (
    _MAX_OPERATIONS,
    _OperationPlan,
    _OperationUnit,
    _ResearchPlan,
    build_dossier,
    normalize_operations,
    plan_operations,
    render_operations_block,
)


def _spec(*texts):
    return {
        "goal": "매출 집계",
        "requirements": [
            {"req_id": f"req-{i}", "text": t, "priority": "must", "source": "doc"}
            for i, t in enumerate(texts, 1)
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# (A) 접착제 승격 — merge_glue_requirements
# ─────────────────────────────────────────────────────────────────────────────

def test_glue_is_promoted_and_marked_apart_from_document_requirements():
    """도출 요구는 요구 목록에 들어가되 원 요구와 구분돼야 한다 — 채점에서 달리 다뤄지므로."""
    d = _spec("매출.xlsx의 B열 합계를 C1에 기록한다")
    added = merge_glue_requirements(d, [{"text": "출력 폴더가 없으면 생성한다"}])

    assert added == 1
    glue = d["requirements"][-1]
    assert glue["text"] == "출력 폴더가 없으면 생성한다"
    assert glue["source"] == "inferred"  # doc/chat(원 요구)과 구분되는 in-band 표식
    assert glue["priority"] == "should"  # must 커버리지 하드 게이트를 추론이 막지 않게
    assert d["requirements"][0]["source"] == "doc"  # 원 요구는 그대로


def test_glue_accepts_bare_strings_and_skips_malformed_items():
    """형식 슬립 한 건이 나머지 접착제까지 날리면 안 된다 — 관대하게 흡수한다."""
    d = _spec("문서 요구")
    added = merge_glue_requirements(d, ["경로를 조립한다", 42, None, {"no_text": "x"}, {"text": "  "}])

    assert added == 1
    assert [r["text"] for r in d["requirements"][1:]] == ["경로를 조립한다"]


def test_glue_dedupes_against_existing_and_within_itself():
    """모델이 문서 요구를 접착제로 되풀이하면 같은 일이 요구 2건이 되어 분모만 부푼다."""
    d = _spec("출력 폴더가 없으면 생성한다")
    added = merge_glue_requirements(d, [
        {"text": "출력   폴더가 없으면 생성한다"},  # 공백만 다른 원 요구 재탕
        {"text": "날짜를 yyyyMMdd로 만든다"},
        {"text": "날짜를 yyyyMMdd로 만든다"},      # 자기 안 중복
    ])

    assert added == 1
    assert len(d["requirements"]) == 2


def test_glue_is_capped():
    """도출이 원 요구를 압도하면 채점 기준이 통째로 추론이 된다."""
    d = _spec("문서 요구")
    added = merge_glue_requirements(d, [{"text": f"접착제 {i}"} for i in range(MAX_GLUE_REQUIREMENTS + 5)])

    assert added == MAX_GLUE_REQUIREMENTS


# ─────────────────────────────────────────────────────────────────────────────
# (A) 접착제 승격 — build_flow_spec 결합
# ─────────────────────────────────────────────────────────────────────────────

def _stub_spec_llm(monkeypatch, glue):
    draft = _SpecDraft(
        goal="매출 집계",
        requirements=[{"req_id": "req-1", "text": "B열 합계를 낸다", "priority": "must", "source": "doc"}],
        glue_requirements=glue,
    )
    monkeypatch.setattr(spec_mod, "chat_json", lambda *a, **k: draft)


def test_build_flow_spec_merges_glue_and_renumbers_anchors(monkeypatch):
    """승격된 접착제도 req_id를 받아야 한다 — req_id가 L2·심판·카드의 공유 앵커다."""
    _stub_spec_llm(monkeypatch, [{"text": "결과 파일 경로 문자열을 조립한다"}])
    out = build_flow_spec({"message": "만들어줘"}, None)

    ids = [r["req_id"] for r in out["requirements"]]
    assert ids == ["req-1", "req-2"]
    assert out["requirements"][1]["source"] == "inferred"
    # 접착제 배열은 spec에 남기지 않는다 — output_assurance가 FlowSpec 밖 키를 미지 필드로 집는다.
    assert "glue_requirements" not in out


def test_build_flow_spec_drops_glue_for_other_solutions(monkeypatch):
    """접착제 승격은 A360 전용(제약 #24) — 제품마다 배관이 달라 도출이 오답이 된다."""
    _stub_spec_llm(monkeypatch, [{"text": "결과 파일 경로 문자열을 조립한다"}])
    out = build_flow_spec({"message": "만들어줘", "solution": "uipath"}, None)

    assert [r["req_id"] for r in out["requirements"]] == ["req-1"]


def test_build_flow_spec_prompt_carries_glue_contract():
    """프롬프트에서 이 절이 사라지면 도출이 조용히 멈춘다 — 계약을 테스트로 붙든다."""
    assert "glue_requirements" in spec_mod._PROMPT
    assert "벤더명" in spec_mod._PROMPT  # 제출 규약 보일러플레이트 금지 조항(설계 §3.4)


# ─────────────────────────────────────────────────────────────────────────────
# (B) 조작 단위 — 정규화·렌더
# ─────────────────────────────────────────────────────────────────────────────

def test_normalize_operations_renumbers_and_drops_duplicates():
    """LLM이 낸 op 번호는 건너뛰거나 겹친다 — 위치 순서로 다시 매겨야 앵커가 된다."""
    spec = _spec("요구 1")
    ops = normalize_operations([
        _OperationUnit(op_id="op-7", intent="날짜 문자열을 만든다"),
        _OperationUnit(op_id="op-7", intent="날짜   문자열을 만든다"),  # 공백만 다른 중복
        _OperationUnit(op_id="", intent=""),                            # 빈 조작
        _OperationUnit(op_id="op-2", intent="파일을 연다", repeat=True),
    ], spec)

    assert [o["op_id"] for o in ops] == ["op-1", "op-2"]
    assert ops[0]["intent"] == "날짜 문자열을 만든다"
    assert ops[1]["repeat"] is True


def test_normalize_operations_keeps_only_existing_req_ids():
    """환각 앵커가 섞이면 '어느 요구가 어느 조작으로 실현됐나' 역매핑이 조용히 틀린다."""
    spec = _spec("요구 1")
    ops = normalize_operations([
        _OperationUnit(intent="표를 읽는다", req_ids=["req-1", "req-99"]),
        _OperationUnit(intent="로그를 남긴다", req_ids="req-1"),  # 리스트 아닌 슬립
        _OperationUnit(intent="정리한다", req_ids=None),
    ], spec)

    assert ops[0]["req_ids"] == ["req-1"]
    assert ops[1]["req_ids"] == ["req-1"]
    assert ops[2]["req_ids"] == []


def test_normalize_operations_accepts_dict_units():
    """상태를 왕복해 dict로 돌아온 조작도 같은 정규화를 탄다."""
    ops = normalize_operations(
        [{"op_id": "op-9", "intent": "행을 읽는다", "req_ids": ["req-1"], "repeat": True}],
        _spec("요구 1"),
    )
    assert ops == [{"op_id": "op-1", "intent": "행을 읽는다", "req_ids": ["req-1"], "repeat": True}]


def test_normalize_operations_caps_runaway_plans():
    spec = _spec("요구 1")
    ops = normalize_operations(
        [_OperationUnit(intent=f"조작 {i}") for i in range(_MAX_OPERATIONS + 10)], spec
    )
    assert len(ops) == _MAX_OPERATIONS


def test_render_operations_block_is_deterministic():
    block = render_operations_block([
        {"op_id": "op-1", "intent": "날짜 문자열을 만든다", "req_ids": [], "repeat": False},
        {"op_id": "op-2", "intent": "행을 읽는다", "req_ids": ["req-1", "req-2"], "repeat": True},
    ])
    assert block == (
        "- [op-1] 날짜 문자열을 만든다\n"
        "- [op-2] 행을 읽는다 ← req-1, req-2 (반복 안)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (B) 조작 단위 — LLM 경계
# ─────────────────────────────────────────────────────────────────────────────

def test_plan_operations_degrades_to_empty_on_llm_failure(monkeypatch):
    """분해 실패는 강등이지 턴 실패가 아니다 — 조작 단위 없이도 파이프라인은 예전대로 돈다."""
    def boom(*a, **k):
        raise RuntimeError("rate limit")

    monkeypatch.setattr(research_mod, "chat_json", boom)
    assert plan_operations(_spec("요구 1")) == []


def test_plan_operations_skips_llm_when_nothing_to_decompose(monkeypatch):
    """분해할 요구도 목표도 없으면 호출 자체를 하지 않는다(흐름당 20~40회 예산)."""
    def boom(*a, **k):
        raise AssertionError("빈 스펙에는 LLM을 부르지 않아야 함")

    monkeypatch.setattr(research_mod, "chat_json", boom)
    assert plan_operations({"goal": "", "requirements": []}) == []


def _stub_research_llm(monkeypatch, operations):
    """조작 단위 호출과 질의 확장 호출을 model_cls로 갈라 응답한다."""
    seen: list[str] = []

    def fake(messages, *, purpose, model_cls):
        seen.append(messages[1]["content"])
        if model_cls is _OperationPlan:
            return _OperationPlan(operations=operations)
        return _ResearchPlan(units=[])

    monkeypatch.setattr(research_mod, "chat_json", fake)
    return seen


def test_dossier_confirms_operations_before_action_menu(monkeypatch):
    """조작 단위는 dossier로 나오고, 액션 메뉴보다 **앞에** 실린다(무엇을 → 무엇으로 순서)."""
    prompts = _stub_research_llm(monkeypatch, [
        _OperationUnit(op_id="op-1", intent="출력 폴더가 없으면 만든다", req_ids=["req-1"]),
    ])
    dossier = asyncio.run(build_dossier(_spec("요구 1"), [], a360_context()))

    assert dossier["operation_units"][0]["op_id"] == "op-1"
    assert "출력 폴더가 없으면 만든다" in dossier["operations"]
    assert dossier["menu"].index("[조작 단위") < dossier["menu"].index("[사용 가능한 액션]")
    # 확정된 조작이 질의 확장 프롬프트에도 실린다 — 검색어가 분해를 타야 한다.
    assert any("확정된 조작 단위" in p for p in prompts)


def test_dossier_without_operations_keeps_plain_menu(monkeypatch):
    """분해가 비면 메뉴는 예전 모양 그대로 — 빈 헤더를 남기지 않는다."""
    _stub_research_llm(monkeypatch, [])
    dossier = asyncio.run(build_dossier(_spec("요구 1"), [], a360_context()))

    assert dossier["operations"] == ""
    assert dossier["operation_units"] == []
    assert "[조작 단위" not in dossier["menu"]


def test_user_catalog_dossier_exposes_empty_operation_keys(monkeypatch):
    """타 솔루션 경로는 조작 단위를 만들지 않지만(제약 #24) 키는 항상 있어야 한다."""
    def boom(*a, **k):
        raise AssertionError("타 솔루션 경로에서는 LLM을 부르지 않아야 함")

    monkeypatch.setattr(research_mod, "chat_json", boom)
    ctx = user_catalog_context(
        UserCatalog([UserCatalogAction(package="UiPath.Excel.Activities", action="ReadRange").as_spec()]),
        "uipath",
    )
    dossier = asyncio.run(build_dossier(_spec("요구 1"), [], ctx))

    assert dossier["operations"] == ""
    assert dossier["operation_units"] == []


@pytest.mark.parametrize("key", ["menu", "actions", "background", "examples", "operations"])
def test_dossier_contract_keys_present_on_both_paths(monkeypatch, key):
    """소비자(graph)가 dict로 읽는다 — 경로마다 키가 다르면 조용히 KeyError가 난다."""
    _stub_research_llm(monkeypatch, [])
    searchable = asyncio.run(build_dossier(_spec("요구 1"), [], a360_context()))
    assert key in searchable
