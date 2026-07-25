"""v4 에이전트 스모크 — 벤더링 무결성과 인프라 격리를 못 박는다 (RPA-298).

`test_agent_v3.py`(817줄)를 통째 복제하지 않는다. v4는 v3의 벤더링 복사본에서 출발하므로
동작 검증은 그쪽이 이미 하고 있고, 여기서 지켜야 할 것은 다른 두 가지다:

1. **벤더링 무결성** — v4가 자기 모듈만 쓰고(v3로 새지 않고), 진입점·상대 임포트·프롬프트
   경로가 온전한가.
2. **인프라 격리** — v4 폴더를 만들면 conftest의 autouse 스텁이 v4 팩토리도 덮어야 한다.
   안 덮으면 **테스트가 운영 Neon/Bonsai를 때린다**(v2·v3는 이미 덮고 있다).

지식층·채널·2상 구조 테스트는 후속 단계에서 이 파일에 얹는다.
"""

import asyncio
import dataclasses
import importlib
import inspect
import json
import types

import pytest

from app.agent.v4.orchestrator.edit_ops import EditOp, annotate_ids, apply_edit_ops, strip_ids
from app.agent.v4.orchestrator.intake import _guard_plan
from app.agent.v4.orchestrator.state import ROUTE_EDIT, ROUTE_GENERATE, ROUTE_QA
from app.agent.v4.verify.checker import is_container


# ─────────────────────────────────────────────────────────────────────────────
# 벤더링 무결성
# ─────────────────────────────────────────────────────────────────────────────

def test_v4_entrypoints_exposed():
    """registry가 요구하는 진입점 3종이 v4에도 있다."""
    mod = importlib.import_module("app.agent.v4")
    for attr in ("stream_agent_turn", "recommend", "analyze"):
        assert callable(getattr(mod, attr, None)), f"v4.{attr} 누락"


def test_v4_meta_is_not_v3():
    """셀렉터 라벨이 v3에서 안 바뀐 채 남아 있으면 사용자가 두 버전을 구분 못 한다."""
    from app.agent.v3.meta import VERSION_META as v3_meta
    from app.agent.v4.meta import VERSION_META as v4_meta

    assert v4_meta["label"] != v3_meta["label"]
    assert v4_meta["label"].startswith("v4")


def test_v4_does_not_import_v3():
    """벤더링 원칙 — v4 코드가 v3 모듈을 import하면 두 버전이 얽혀 독립 진화가 깨진다."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "app" / "agent" / "v4"
    offenders = [
        f"{p.relative_to(root)}:{i}"
        for p in root.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "agent.v3" in line or "agent import v3" in line
    ]
    assert not offenders, f"v4가 v3를 import한다: {offenders}"


def test_v4_prompt_files_resolve():
    """프롬프트 경로는 상대 계산이라 복사만으로 동작해야 한다 — 누락 시 import 시점에 터진다."""
    from app.agent.v4.recommend import graph as recommend_graph

    assert recommend_graph._BASE_PROMPT.strip()
    assert recommend_graph._ADDENDUM.strip()
    # v3 파일명을 그대로 참조하면 v4 폴더에 없어 read_text가 터진다(이미 import 시점에 검증됨).
    assert "compose_v4_addendum" in str(recommend_graph._PROMPT_DIR / "compose_v4_addendum.md")


def test_v4_config_reads_registry_not_getenv():
    """v4 config는 app.core.config 경유다 — getenv 래칫(_DIRECT_GETENV_ALLOWED)을 안 늘린다."""
    from app.agent.v4 import config as v4_config

    assert v4_config.OPENAI_MODEL  # 기본값이라도 값이 나온다
    assert isinstance(v4_config.MAX_LLM_CONCURRENCY, int)
    with pytest.raises(AttributeError):
        v4_config.NOT_A_DECLARED_KEY


# ─────────────────────────────────────────────────────────────────────────────
# 인프라 격리 (이 파일의 핵심)
# ─────────────────────────────────────────────────────────────────────────────

def test_v4_retriever_and_catalog_are_stubbed():
    """conftest autouse 스텁이 v4 팩토리를 덮었는지 — 안 덮이면 실제 DB/OpenSearch로 나간다."""
    from app.agent.v4.retrieval import _make_retriever
    from app.agent.v4.verify.catalog import _make_catalog

    retriever, catalog = _make_retriever(), _make_catalog()
    assert type(retriever).__name__ == "FakeRetriever", (
        "v4 retriever가 스텁이 아니다 — tests/conftest.py의 _stub_agent_rag에 v4를 추가하라"
    )
    assert type(catalog).__name__ == "FakeCatalog", (
        "v4 catalog가 스텁이 아니다 — tests/conftest.py의 _stub_agent_rag에 v4를 추가하라"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 결정론 로직 스모크 (복사가 온전한지 — 동작 세부는 v3 테스트가 본다)
# ─────────────────────────────────────────────────────────────────────────────

def test_guard_plan_drops_unknown_and_caps():
    plan, notes = _guard_plan(["analyze", "generate", "없는task", "qa", "analyze"], {})
    assert "없는task" not in plan
    assert len(plan) <= 3
    assert any("미지 task" in n for n in notes)


def test_guard_plan_demotes_edit_without_flow():
    """수정 대상이 없으면 edit는 generate로 강등된다."""
    plan, _ = _guard_plan([ROUTE_EDIT], {})
    assert ROUTE_EDIT not in plan
    assert ROUTE_GENERATE in plan


def test_guard_plan_never_returns_empty():
    """하한 보장 — 어떤 입력이 와도 최소 qa."""
    plan, _ = _guard_plan([], {})
    assert plan == [ROUTE_QA]


def test_is_container_matches_control_flow_packages():
    assert is_container("Loop", "cloudUsingLoopAction")
    assert is_container("Error handler", "errorHandlerTry")
    assert not is_container("Excel advanced", "cloudExcelOpen")


def test_edit_ops_roundtrip_preserves_flow():
    """annotate → apply → strip 왕복이 흐름도를 깨지 않는다."""
    flow = {
        "steps": [
            {
                "step_id": "step-1",
                "label": "준비",
                "actions": [
                    {"package": "Excel advanced", "action": "Open", "label": "열기",
                     "order": 1, "parameters": [], "children": []},
                ],
            }
        ],
        "variables": [],
    }
    work = annotate_ids(flow)
    target = work["steps"][0]["actions"][0]["_id"]
    applied, errors = apply_edit_ops(work, [EditOp(op="remove", target=target)])
    strip_ids(work)
    assert applied == 1 and not errors
    assert work["steps"][0]["actions"] == []


# ─────────────────────────────────────────────────────────────────────────────
# 2상 구조 — draft_flow / refine_draft / DraftResult (설계 §6.3, §5-I)
#
# LLM은 한 번도 부르지 않는다: compose·verify·judge·refine·카드 문구 다듬기를 전부
# 결정론 대역으로 갈아끼우고 **계약**(반환 형태·직렬화·시그니처)만 본다. 품질은 이
# 이슈의 관심사가 아니다 — 이번 작업은 파이프라인을 두 상으로 쪼갠 구조 작업이다.
# ─────────────────────────────────────────────────────────────────────────────

def _sample_flow() -> dict:
    return {
        "steps": [{
            "step_id": "step-1",
            "label": "준비",
            "actions": [{
                "package": "Excel advanced", "action": "Open", "label": "열기",
                "order": 1, "parameters": [], "children": [],
            }],
        }],
        "variables": [],
    }


def _sample_hit() -> dict:
    return {
        "package_name": "Excel advanced", "action_name": "Open",
        "score": 0.91, "source_type": "action_schema", "title": "Open", "url": None,
    }


def _fake_ctx():
    """CatalogContext 대역 — 이 테스트들은 카탈로그를 실제로 조회하는 경로를 전부 대역으로 막는다."""
    return types.SimpleNamespace(catalog=None, retriever=None, searchable=False, is_a360=True)


def _stub_draft_phase(monkeypatch):
    """draft_flow의 LLM 3구간(compose·verify·judge)과 검색(research)을 결정론 대역으로 교체."""
    from app.agent.v4.orchestrator import judge as judge_mod
    from app.agent.v4.orchestrator.judge import CandidateReport
    from app.agent.v4.recommend import graph as g
    from app.agent.v4.recommend import research as research_mod
    from app.agent.v4.verify.findings import Finding

    async def fake_dossier(spec, sink, ctx):
        sink.append(_sample_hit())  # sink는 1상이 채워 2상에 넘기는 근거 통로다
        return {"menu": "메뉴", "actions": [("Excel advanced", "Open")], "background": "", "examples": ""}

    async def fake_compose(cid, persona_file, spec, dossier, analysis, document, sink, sem, ctx):
        return _sample_flow()

    async def fake_verify(cid, persona_name, flow, spec, sem, ctx):
        return CandidateReport(
            candidate_id=cid, persona=persona_name, flow=flow, violations=[],
            findings=[
                Finding(layer="L0", severity="major", rule="R2", message="정적"),
                Finding(layer="L2", severity="blocker", req_id="REQ-1", message="누락"),
            ],
            must_coverage=0.8, sim_pass_rate=0.9,
        )

    def fake_judge(spec, reports, **kw):
        return {
            "winner": reports[0],
            "verdict": {"winner": reports[0].candidate_id, "reason": "대역", "scores": []},
            "transplant_findings": [Finding(layer="judge", severity="major", message="이식")],
        }

    monkeypatch.setattr(research_mod, "build_dossier", fake_dossier)
    monkeypatch.setattr(g, "_compose_candidate", fake_compose)
    monkeypatch.setattr(g, "_verify_candidate", fake_verify)
    monkeypatch.setattr(judge_mod, "judge_candidates", fake_judge)
    monkeypatch.setattr(g, "_REVEAL_DELAY", 0.0)  # 점진 노출 페이싱은 계약이 아니다


def test_draft_flow_standalone_yields_valid_draft_result(monkeypatch):
    """1상만 단독 호출해도 완결된 DraftResult가 나온다 — 2상을 떼어낼 수 있다는 뜻."""
    from app.agent.v4.recommend import graph as g

    _stub_draft_phase(monkeypatch)
    draft = asyncio.run(g.draft_flow({"summary": "엑셀 정리"}, None, {"goal": "g", "requirements": []}, _fake_ctx()))

    assert isinstance(draft, g.DraftResult)
    assert draft.flow["steps"][0]["step_id"] == "step-1"
    # 문서 없이 2후보(A·B) — 후보 flow는 2상의 합의(agreement) 산출 재료라 반드시 실려야 한다.
    assert len(draft.reports) == 2
    assert all(isinstance(r, dict) and r.get("flow") for r in draft.reports)
    assert draft.verdict["winner"] == "A"
    assert draft.winner_report()["must_coverage"] == 0.8
    # 2상에 넘기는 개선 지시 = 승자의 L2/L3 + 심판 이식 지시. L0 정적은 2상이 다시 뽑으므로 제외.
    assert [f["layer"] for f in draft.findings] == ["L2", "judge"]
    assert draft.sink and draft.sink[0]["action_name"] == "Open"
    assert draft.draft_id


def test_draft_result_is_json_serializable(monkeypatch):
    """후속 이슈에서 이 dataclass가 그대로 백그라운드 잡 페이로드가 된다 — 큐에 실려야 한다."""
    from app.agent.v4.recommend import graph as g

    _stub_draft_phase(monkeypatch)
    draft = asyncio.run(g.draft_flow({"summary": "x"}, None, {"goal": "g", "requirements": []}, _fake_ctx()))

    blob = json.dumps(dataclasses.asdict(draft), ensure_ascii=False)
    assert json.loads(blob)["draft_id"] == draft.draft_id


def test_draft_result_is_frozen():
    """2상이 1상 산출물을 제자리 변형하면 재시도 시 입력이 달라진다 — 그걸 타입으로 막는다."""
    from app.agent.v4.recommend.graph import DraftResult

    draft = DraftResult(flow={}, spec={}, dossier={}, reports=(), verdict={}, sink=(), findings=(), draft_id="d1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        draft.draft_id = "d2"


def test_refine_draft_returns_generate_flow_shape_and_leaves_draft_intact(monkeypatch):
    """2상 단독 호출 — 반환 형태가 generate_flow와 같고, 넘겨받은 초안을 오염시키지 않는다."""
    from app.agent.v4.orchestrator import cards as cards_mod
    from app.agent.v4.orchestrator import harness as harness_mod
    from app.agent.v4.recommend import graph as g

    # 교정(surgeon)·카드 문구 다듬기는 LLM 경로라 대역으로 막는다. repaired=False면 L2/L3
    # 재채점도 건너뛰므로 이 테스트는 LLM을 한 번도 안 부른다.
    monkeypatch.setattr(
        harness_mod, "refine_flow",
        lambda flow, catalog, **kw: {"flow": flow, "violations": [], "repaired": False},
    )
    monkeypatch.setattr(cards_mod, "build_cards", lambda flow, spec, r3, catalog: [])

    draft = g.DraftResult(
        flow=_sample_flow(), spec={"goal": "g", "requirements": []}, dossier={},
        reports=({"candidate_id": "A", "flow": _sample_flow(), "must_coverage": 0.8, "sim_pass_rate": 0.9},),
        verdict={"winner": "A"}, sink=(_sample_hit(),), findings=(), draft_id="d1",
    )
    out = asyncio.run(g.refine_draft(draft, _fake_ctx()))

    assert set(out) == {"recommendation", "violations"}
    rec = out["recommendation"]
    assert rec["steps"][0]["actions"][0]["package"] == "Excel advanced"
    assert rec["flow_confidence"] is not None
    # finalize의 제자리 변형이 1상 산출물로 새면 재시도 입력이 오염된다.
    assert "needs_input" not in draft.flow and "flow_confidence" not in draft.flow


def test_generate_flow_is_two_phase_composition_with_same_signature(monkeypatch):
    """generate_flow는 두 상의 순차 합성일 뿐 — 시그니처·반환 계약은 그대로다(done 1회 가정)."""
    from app.agent.v4.recommend import graph as g

    assert [p.name for p in inspect.signature(g.generate_flow).parameters.values()] == [
        "analysis", "document", "spec", "ctx",
    ]

    seen: dict = {}

    async def fake_draft(analysis, document, spec, ctx):
        seen["draft_ctx"] = ctx
        return "DRAFT-SENTINEL"

    async def fake_refine(draft, ctx):
        seen["passed"] = draft
        seen["refine_ctx"] = ctx
        return {"recommendation": {"steps": []}, "violations": []}

    monkeypatch.setattr(g, "draft_flow", fake_draft)
    monkeypatch.setattr(g, "refine_draft", fake_refine)

    ctx = _fake_ctx()
    out = asyncio.run(g.generate_flow({"summary": "x"}, None, {"goal": "g"}, ctx))

    assert seen["passed"] == "DRAFT-SENTINEL"
    # 두 상이 같은 어휘 출처를 봐야 한다 — ctx를 상마다 새로 만들면 카탈로그가 갈린다(RPA-285).
    assert seen["draft_ctx"] is ctx and seen["refine_ctx"] is ctx
    assert set(out) == {"recommendation", "violations"}


def test_emit_draft_frame_reuses_partial_event(monkeypatch):
    """새 event 값을 만들지 않는다 — 모르는 kind는 FE가 무시하지만 새 event는 FE 분기를 깬다."""
    from app.agent.v4.recommend import stream as s
    from app.schemas import ProgressEvent

    captured: list[dict] = []
    monkeypatch.setattr(s, "emit", captured.append)
    s.emit_draft_frame(
        _sample_flow(),
        [{"rule": "R2", "location": "actions[0]", "message": "미지 파라미터", "step_id": "step-1",
          "package": "Excel advanced", "action": "Open", "param": "path"}],
        "d1", "선택된 초안 · 다듬기 시작",
    )

    (payload,) = captured
    assert payload["event"] == "partial"
    assert payload["data"]["kind"] == "draft"
    assert payload["data"]["draft_id"] == "d1"
    assert payload["data"]["violations"][0]["rule"] == "R2"
    ProgressEvent(**payload)  # ProgressEvent Literal 확장 없이 통과해야 한다


# ─────────────────────────────────────────────────────────────────────────────
# set_spec 연산 + 수정 지시 전환 (설계 §5.2-C, §6.1)
#
# 지키려는 것 하나: **사용자의 삭제가 검수에 의해 되돌려지지 않는다.**
# 누락이 blocker가 되면서, 액션만 지우고 요구를 남기면 교정 루프가 그 액션을 도로 넣는다.
# 아래 테스트는 요구까지 함께 지워지는 경로와, surgeon이 애초에 삭제를 덜 고르게 만드는
# 슬롯 목적 주입을 못 박는다. LLM은 부르지 않는다 — 전부 결정론 경로다.
# ─────────────────────────────────────────────────────────────────────────────

def _flow_with_spec() -> dict:
    """req_id 앵커가 달린 액션 2개 + 요구 2건. §6.1의 '메일 발송 단계 빼주세요' 재현용."""
    return {
        "steps": [{
            "step_id": "step-1",
            "label": "본문",
            "actions": [
                {"package": "Excel advanced", "action": "Open", "label": "열기",
                 "order": 1, "parameters": [], "children": [], "req_id": "req-1"},
                {"package": "Email", "action": "sendMail", "label": "메일 발송",
                 "order": 2, "parameters": [], "children": [], "req_id": "req-2"},
            ],
        }],
        "variables": [],
        "spec": {
            "goal": "엑셀을 정리해 메일로 보낸다",
            "requirements": [
                {"req_id": "req-1", "text": "엑셀 파일을 연다", "priority": "must", "source": "doc"},
                {"req_id": "req-2", "text": "결과를 메일로 보낸다", "priority": "must", "source": "doc"},
            ],
        },
    }


def test_set_spec_removes_requirement_so_missing_blocker_cannot_revive_action():
    """"메일 발송 빼주세요" = 액션 remove + 요구 remove. 요구가 없으면 누락도 없다 (§6.1)."""
    from app.agent.v4.orchestrator.edit_ops import spec_requirements
    from app.agent.v4.verify.coverage_det import missing_requirements

    flow = annotate_ids(_flow_with_spec())
    mail_id = flow["steps"][0]["actions"][1]["_id"]
    applied, errors = apply_edit_ops(flow, [
        EditOp(op="remove", target=mail_id),
        EditOp(op="set_spec", remove_req_ids=["req-2"]),
    ])
    strip_ids(flow)

    assert applied == 2 and not errors
    assert [r["req_id"] for r in spec_requirements(flow)] == ["req-1"]
    # 핵심 단언: 요구가 사라졌으니 결정론 누락 판정이 침묵한다 — 교정 루프가 되살릴 근거가 없다.
    assert missing_requirements(flow, flow["spec"]) == []


def test_set_spec_without_it_the_deletion_is_reverted():
    """대조군 — 요구를 남긴 채 액션만 지우면 그 요구가 blocker로 되돌아온다(이 연산의 존재 이유)."""
    from app.agent.v4.verify.coverage_det import coverage_findings

    flow = annotate_ids(_flow_with_spec())
    mail_id = flow["steps"][0]["actions"][1]["_id"]
    apply_edit_ops(flow, [EditOp(op="remove", target=mail_id)])
    strip_ids(flow)

    fnd = coverage_findings(flow, flow["spec"])
    assert [f.severity for f in fnd] == ["blocker"]
    assert fnd[0].req_id == "req-2"


def test_set_spec_detaches_dangling_req_id_from_surviving_actions():
    """요구를 지우면 그 요구를 가리키던 남은 액션의 앵커도 끊는다 — 매달린 참조 방지."""
    flow = _flow_with_spec()
    applied, errors = apply_edit_ops(flow, [EditOp(op="set_spec", remove_req_ids=["req-2"])])

    assert applied == 1 and not errors
    assert flow["steps"][0]["actions"][1]["req_id"] is None  # 액션은 남았지만 앵커는 끊겼다
    assert flow["steps"][0]["actions"][0]["req_id"] == "req-1"  # 무관한 앵커는 보존


def test_set_spec_materializes_spec_when_none():
    """Recommendation.spec 기본값이 None이라 setdefault로는 못 고친다 (RPA-282와 같은 함정)."""
    flow = {"steps": [], "spec": None}
    applied, errors = apply_edit_ops(
        flow, [EditOp(op="set_spec", requirements=[{"text": "실패 시 담당자에게 알린다"}])]
    )

    assert applied == 1 and not errors
    assert flow["spec"]["requirements"] == [
        {"req_id": "req-1", "text": "실패 시 담당자에게 알린다", "priority": "must", "source": "chat"}
    ]


def test_set_spec_new_requirement_gets_non_colliding_req_id():
    """req_id는 L2 채점·심판·카드의 공유 앵커 — 중복되면 서로 다른 요구가 한 칸으로 뭉개진다."""
    flow = _flow_with_spec()
    apply_edit_ops(flow, [EditOp(op="set_spec", requirements=[{"text": "로그를 남긴다"}])])

    assert [r["req_id"] for r in flow["spec"]["requirements"]] == ["req-1", "req-2", "req-3"]


def test_set_spec_partial_update_keeps_untouched_fields():
    """기존 요구 수정은 준 필드만 덮는다 — priority/source를 안 주면 원래 값이 남아야 한다."""
    flow = _flow_with_spec()
    apply_edit_ops(flow, [
        EditOp(op="set_spec", requirements=[{"req_id": "req-1", "text": "엑셀 파일을 읽기전용으로 연다"}])
    ])

    req = flow["spec"]["requirements"][0]
    assert req["text"] == "엑셀 파일을 읽기전용으로 연다"
    assert req["priority"] == "must" and req["source"] == "doc"


def test_set_spec_reports_failure_instead_of_silently_succeeding():
    """없는 req_id를 지우라는 연산을 '성공'으로 삼키면 사용자는 지워진 줄 안다."""
    flow = {"steps": [], "spec": None}
    applied, errors = apply_edit_ops(flow, [EditOp(op="set_spec", remove_req_ids=["req-9"])])

    assert applied == 0 and len(errors) == 1
    assert "req_id" in errors[0]  # 노드 못 찾음이 아니라 스펙 사유를 알려야 재요청이 제자리를 본다
    assert flow["spec"] is None  # 무효 연산이 spec을 빈 dict로 실체화하면 무변경 판정이 흔들린다


def test_set_spec_coerces_llm_string_slips():
    """요구를 문자열로, 지울 id를 단일 문자열로 낸 슬립을 관대 수용 (배치 전체를 살린다)."""
    flow = _flow_with_spec()
    applied, errors = apply_edit_ops(
        flow, [EditOp(op="set_spec", remove_req_ids="req-1", requirements=["로그를 남긴다"])]
    )

    assert applied == 1 and not errors
    assert [r["req_id"] for r in flow["spec"]["requirements"]] == ["req-2", "req-1"]
    assert flow["spec"]["requirements"][1]["text"] == "로그를 남긴다"


def test_requirement_only_edit_is_not_treated_as_noop():
    """"그 업무는 이제 필요 없어요"는 set_spec 하나로 끝나 액션 트리가 그대로다 — 무변경으로
    저하시키면 사용자가 지운 요구가 살아남아 다음 검수에서 blocker로 되살아난다."""
    from app.agent.v4.orchestrator.edit import _is_noop_edit

    base = _flow_with_spec()
    after = _flow_with_spec()
    apply_edit_ops(after, [EditOp(op="set_spec", remove_req_ids=["req-2"])])

    assert not _is_noop_edit(after, base)
    assert _is_noop_edit(_flow_with_spec(), base)  # 대조군 — 진짜 무변경은 여전히 무변경


def test_insert_preserves_req_id_so_the_repair_loop_converges():
    """누락을 메우려 삽입한 액션이 앵커를 잃으면 다음 라운드가 같은 요구를 또 누락으로 센다."""
    flow = annotate_ids(_flow_with_spec())
    anchor = flow["steps"][0]["actions"][0]["_id"]
    apply_edit_ops(flow, [EditOp(
        op="insert", anchor=anchor, position="after",
        action={"package": "Email", "action": "sendMail", "label": "알림", "req_id": "req-9"},
    )])
    strip_ids(flow)

    assert flow["steps"][0]["actions"][1]["req_id"] == "req-9"


# ── (B) 위반 → 수정 지시 전환: surgeon에 슬롯 목적 주입 (§5.1-②, §5.2-C) ──────────

def test_slot_purpose_block_tells_surgeon_what_each_slot_is_for():
    """surgeon은 노드만 보면 그 자리의 목적을 몰라 가장 싼 remove를 고른다 — 목적을 실어 준다."""
    from app.agent.v4.orchestrator.harness import slot_purpose_block

    flow = annotate_ids(_flow_with_spec())
    block = slot_purpose_block(flow, flow["spec"])

    assert "req-2" in block and "결과를 메일로 보낸다" in block
    assert flow["steps"][0]["actions"][1]["_id"] in block  # 아웃라인과 같은 노드 id로 지목
    assert "삭제" in block  # 삭제가 최후 수단이라는 지시가 같은 블록에 붙어야 한다


def test_slot_purpose_block_is_silent_without_anchors():
    """req_id 미기재는 '목적이 없다'가 아니라 '연결 정보가 없다' — 지어내지 않고 침묵한다
    (coverage_det의 침묵 원칙과 동일). 프롬프트가 그대로면 기존 동작이 보존된다."""
    from app.agent.v4.orchestrator.harness import slot_purpose_block

    flow = _flow_with_spec()
    for a in flow["steps"][0]["actions"]:
        a.pop("req_id")

    assert slot_purpose_block(annotate_ids(flow), flow["spec"]) == ""
    assert slot_purpose_block(annotate_ids(_flow_with_spec()), None) == ""


def test_surgeon_prompt_makes_deletion_a_last_resort():
    """프롬프트 계약 — 이 문구가 빠지면 교정 루프가 다시 '지워서 위반 줄이기'로 최적화된다."""
    from app.agent.v4.orchestrator.harness import _SURGEON_PROMPT

    assert "삭제는 최후 수단" in _SURGEON_PROMPT
    assert "슬롯 목적" in _SURGEON_PROMPT
    # 요구를 없앨지는 사용자만 정한다 — surgeon에게 set_spec을 주면 스스로 근거를 만들어 낸다.
    assert "set_spec" in _SURGEON_PROMPT and "쓰지 마세요" in _SURGEON_PROMPT


def test_edit_prompt_pairs_action_removal_with_requirement_removal():
    """edit LLM이 set_spec을 모르면 액션만 지워, 검수가 그 액션을 도로 넣는다 (§6.1)."""
    from app.agent.v4.orchestrator.edit import _PROMPT, _requirements_block

    assert "set_spec" in _PROMPT and "remove_req_ids" in _PROMPT
    block = _requirements_block(_flow_with_spec())
    assert "[req-2]" in block and "remove_req_ids" in block
    assert _requirements_block({"steps": [], "spec": None}) == ""  # 요구 없으면 프롬프트 불변


# ─────────────────────────────────────────────────────────────────────────────
# 커스텀 액션 오버레이 + 모드 판정 시점 (설계 §6.2·§6.6)
#
# 오버레이가 지켜야 할 불변식은 둘이다:
#   (1) 커스텀 액션이 **1급 어휘**다 — R1을 통과하고 composer가 실제로 볼 수 있다.
#   (2) 그 대가로 **A360 어휘·검색을 잃지 않는다** — 이전 두 모드는 양자택일이었다.
# 그리고 판정은 **생성 전**에 끝나야 한다 — 헛된 생성 한 턴을 없애는 게 §6.6의 목적이다.
# ─────────────────────────────────────────────────────────────────────────────

_CUSTOM_DESCRIBED = {
    "package": "AcmeCommon",
    "action": "SendKakaoAlert",
    "label": "카카오 알림 발송",
    "parameters": [
        {"name": "message", "label": "메시지", "type": "TEXT", "required": True},
    ],
}
_CUSTOM_NAME_ONLY = {
    "package": "AcmeErp",
    "action": "PostVoucher",
    "label": "전표 전기",
    # 사용자가 이름만 줬다 — parameters 키 자체가 없다(params_unknown 경로).
    "params_unknown": True,
}

# 제품명·"사내" 같은 단서가 하나도 없는 붙여넣기 — 텍스트만으로는 사내 커스텀인지
# 타 솔루션인지 구분할 수 없는, 되물어야 하는 바로 그 입력이다.
_UNLABELED_PASTE = """액션 목록입니다
- AcmeCommon/SendKakaoAlert
- AcmeErp/PostVoucher
- AcmeErp/CloseLedger
이걸로 흐름도 만들어줘
"""


class _StubBase:
    """A360 쪽 스텁 — 오버레이가 base를 정말 살려 두는지 보려면 base가 답을 해야 한다."""

    A360_SPEC = {"package": "Excel advanced", "action": "cloudExcelOpen", "parameters": []}

    def get_action_schema(self, package, action):
        if (package, action) == ("Excel advanced", "cloudExcelOpen"):
            return dict(self.A360_SPEC)
        if (package, action) == ("AcmeCommon", "SendKakaoAlert"):
            return {"package": package, "action": action, "parameters": [{"name": "표준스펙"}]}
        return None

    def iter_action_schemas(self):
        yield dict(self.A360_SPEC)

    def search(self, query, limit=4, source_types=None):
        return [{"source_type": "action_schema", "package_name": "Excel advanced",
                 "action_name": "cloudExcelOpen", "title": "엑셀 열기", "score": 0.8}]


def _overlay_catalog(custom=None):
    from app.agent.v4.catalog_context import OverlayCatalog

    return OverlayCatalog(
        _StubBase(), custom if custom is not None else [_CUSTOM_DESCRIBED, _CUSTOM_NAME_ONLY]
    )


def test_overlay_catalog_keeps_a360_vocabulary():
    """오버레이의 존재 이유 — 커스텀을 얹어도 A360 어휘가 그대로 조회된다."""
    cat = _overlay_catalog()

    assert cat.get_action_schema("Excel advanced", "cloudExcelOpen") is not None
    assert cat.get_action_schema("AcmeCommon", "SendKakaoAlert") is not None
    assert cat.get_action_schema("없는패키지", "없는액션") is None


def test_overlay_catalog_prefers_custom_on_name_collision():
    """같은 표기가 양쪽에 있으면 사용자가 설명한 쪽이 진실이다 — 사내에 실제로 배포된 것."""
    spec = _overlay_catalog().get_action_schema("AcmeCommon", "SendKakaoAlert")

    assert [p["name"] for p in spec["parameters"]] == ["message"]


def test_overlay_catalog_iterates_union_without_duplicates():
    keys = {(s["package"], s["action"]) for s in _overlay_catalog().iter_action_schemas()}

    assert ("Excel advanced", "cloudExcelOpen") in keys
    assert ("AcmeCommon", "SendKakaoAlert") in keys
    assert len(list(_overlay_catalog().iter_action_schemas())) == 3  # 중복 없이 1 + 2


def test_overlay_context_stays_a360_and_searchable():
    """모드 신설의 핵심 — 커스텀을 받았다고 검색·트리거·구조 검사를 잃으면 안 된다."""
    from app.agent.v4.catalog_context import A360_CUSTOM, overlay_context

    ctx = overlay_context([_CUSTOM_DESCRIBED])

    assert ctx.solution == A360_CUSTOM
    assert ctx.is_a360 is True       # 트리거 제안·구조/세션 검사 게이트가 열려 있어야 한다
    assert ctx.searchable is True    # 검색을 잃지 않는다(user_catalog 모드와의 결정적 차이)
    assert ctx.has_overlay is True
    assert ctx.catalog.get_action_schema("AcmeCommon", "SendKakaoAlert") is not None


def test_overlay_context_without_custom_degrades_to_plain_a360():
    """추출이 빈손이어도 흐름 생성을 막지 않는다 — A360 어휘는 그대로 쓸 수 있다."""
    from app.agent.v4.catalog_context import A360, overlay_context

    ctx = overlay_context([])

    assert ctx.solution == A360 and ctx.has_overlay is False


def test_overlay_catalog_instance_is_stable_for_the_same_custom_set():
    """knowledge.derive의 유도 캐시가 `id(catalog)` 키다 — 턴마다 새 래퍼면 매번 재유도하고,
    GC 후 id가 재사용되면 남의 유도 결과를 읽는다."""
    from app.agent.v4.catalog_context import overlay_context

    a = overlay_context([dict(_CUSTOM_DESCRIBED)])
    b = overlay_context([dict(_CUSTOM_DESCRIBED)])
    c = overlay_context([dict(_CUSTOM_DESCRIBED), dict(_CUSTOM_NAME_ONLY)])

    assert a.catalog is b.catalog       # 같은 커스텀 집합 → 같은 인스턴스
    assert a.catalog is not c.catalog   # 집합이 다르면 다른 인스턴스


def test_overlay_catalog_cache_is_bounded():
    """세션이 쌓여도 캐시가 무한히 자라지 않는다."""
    from app.agent.v4.catalog_context import _OVERLAY_CACHE, _OVERLAY_CACHE_MAX, overlay_context

    for i in range(_OVERLAY_CACHE_MAX * 2):
        overlay_context([{"package": "Acme", "action": f"Bounded{i}"}])

    assert len(_OVERLAY_CACHE) <= _OVERLAY_CACHE_MAX


def test_overlay_solution_value_passes_backend_solution_regex():
    """세션 지속(§6.2)은 기존 detected_solution 경로 재사용이라 백엔드 문법을 지켜야 한다.

    `a360+custom`이었다면 `_SOLUTION_RE`에 막혀 세션에 저장되지 않고, 재오픈 때 커스텀
    모드가 조용히 사라진다.
    """
    import re

    from app.agent.v4.catalog_context import A360_CUSTOM

    assert re.match(r"^[a-z0-9][a-z0-9 ._-]{0,48}$", A360_CUSTOM)


# --- 커스텀 액션이 composer에게 실제로 보이는가 (검색 경계) ---

def _overlay_retriever(custom=None):
    from app.agent.v4.catalog_context import OverlayRetriever

    return OverlayRetriever(
        _StubBase(), custom if custom is not None else [_CUSTOM_DESCRIBED, _CUSTOM_NAME_ONLY]
    )


def test_overlay_retriever_adds_custom_without_dropping_a360_hits():
    """카탈로그에만 넣으면 R1은 통과해도 composer가 액션의 존재를 모른다 — 메뉴는 검색이 만든다."""
    hits = _overlay_retriever().search("카카오 알림 보내기", source_types=["action_schema", "bot_example"])
    pairs = [(h["package_name"], h["action_name"]) for h in hits]

    assert ("AcmeCommon", "SendKakaoAlert") in pairs
    assert ("Excel advanced", "cloudExcelOpen") in pairs  # A360 검색 결과가 살아 있다


def test_overlay_retriever_leaves_background_search_alone():
    """배경 지식(doc_page) 검색에 액션을 끼워 넣으면 배경 자리를 액션이 잡아먹는다."""
    hits = _overlay_retriever().search("입금 대사 업무", source_types=["doc_page"])

    assert all(h.get("source_type") != "user_catalog" for h in hits)


def test_overlay_retriever_scores_described_action_above_name_only():
    """근거 강도(§6.6) — 파라미터까지 설명한 액션이 이름만 준 액션보다 강한 근거다.

    이 점수가 그대로 attach_confidence의 evidence 기반값이 되므로 신뢰도로 이어진다.
    """
    hits = {h["action_name"]: h["score"] for h in _overlay_retriever().search("전표와 알림")}

    assert hits["SendKakaoAlert"] > hits["PostVoucher"]


def test_overlay_retriever_ranks_query_match_first_and_caps():
    """A360 액션이 메뉴에서 통째로 밀려나지 않게 상한을 둔다(메뉴 상한 14의 절반 이하)."""
    from app.agent.v4.catalog_context import _OVERLAY_MAX_HITS

    many = [
        {"package": "Acme", "action": f"Action{i}", "label": f"라벨{i}"}
        for i in range(_OVERLAY_MAX_HITS + 5)
    ]
    hits = _overlay_retriever([*many, _CUSTOM_DESCRIBED]).search("카카오 알림")
    custom_hits = [h for h in hits if h["source_type"] == "user_catalog"]

    assert len(custom_hits) == _OVERLAY_MAX_HITS
    assert custom_hits[0]["action_name"] == "SendKakaoAlert"  # 질의에 맞은 액션이 먼저


# --- 파라미터 검수: 설명한 만큼만 검사한다 (params_unknown 재사용) ---

def test_name_only_custom_action_passes_r1_and_skips_param_rules():
    """사용자가 이름만 준 액션에 R2가 발화하면 교정 루프가 파라미터를 지운다 — 모름은 침묵."""
    from app.agent.v4.orchestrator.generate import UserCatalogAction
    from app.agent.v4.verify.checker import run_checks

    spec = UserCatalogAction(package="AcmeErp", action="PostVoucher").as_spec()
    assert "parameters" not in spec and spec["params_unknown"] is True

    class _Cat:
        def get_action_schema(self, package, action):
            return spec if (package, action) == ("AcmeErp", "PostVoucher") else None

    violations = run_checks(
        [{"package": "AcmeErp", "action": "PostVoucher", "order": 1,
          "parameters": [{"name": "voucherId", "value": "V-1"}], "children": []}],
        _Cat(),
    )
    assert violations == []


def test_described_custom_action_is_actually_checked():
    """설명해 준 범위는 검수한다 — 침묵이 '검사 포기'가 되면 오버레이가 검수 구멍이 된다."""
    from app.agent.v4.orchestrator.generate import UserCatalogAction, UserCatalogParam
    from app.agent.v4.verify.checker import run_checks

    spec = UserCatalogAction(
        package="AcmeCommon", action="SendKakaoAlert",
        parameters=[UserCatalogParam(name="message", required=True)],
    ).as_spec()
    assert [p["name"] for p in spec["parameters"]] == ["message"]

    class _Cat:
        def get_action_schema(self, package, action):
            return spec if (package, action) == ("AcmeCommon", "SendKakaoAlert") else None

    violations = run_checks(
        [{"package": "AcmeCommon", "action": "SendKakaoAlert", "order": 1,
          "parameters": [{"name": "지어낸파라미터", "value": "x"}], "children": []}],
        _Cat(),
    )
    rules = {v.rule for v in violations}
    assert "R2" in rules  # 스펙에 없는 파라미터
    assert "R3" in rules  # 필수 message 누락


# --- 모드 판정: 텍스트로 구분 불가하면 묻는다 (§6.2) ---

def _mode(message="", history=None, solution="a360"):
    from app.agent.v4.orchestrator.foreign_catalog import decide_catalog_mode
    from app.agent.v4.verify.catalog import get_catalog

    state = {"message": message, "history": history or [], "solution": solution}
    return decide_catalog_mode(state, get_catalog())


def test_mode_is_plain_a360_without_any_signal():
    from app.agent.v4.orchestrator.foreign_catalog import MODE_A360

    d = _mode("매일 아침 엑셀 읽어서 메일 보내는 봇 만들어줘")

    assert d.mode == MODE_A360 and d.confirm is False


def test_mode_asks_when_paste_could_be_either():
    """`detect()`가 잡는 건 'A360에 없는 액션'이지 '타 솔루션'이 아니다 — 사내 패키지도 없다."""
    from app.agent.v4.orchestrator.foreign_catalog import MODE_ASK

    d = _mode(_UNLABELED_PASTE)

    assert d.mode == MODE_ASK
    assert "사내 커스텀 패키지" in d.question and "다른 RPA 솔루션" in d.question
    assert d.confirm is False  # 확정 전에 세션을 굳히지 않는다


def test_mode_skips_question_when_product_name_is_given():
    """제품명이 밝혀졌으면 사내 패키지일 리 없다 — 되묻기는 순수 지연이다."""
    from app.agent.v4.orchestrator.foreign_catalog import MODE_FOREIGN

    d = _mode("우리는 UiPath를 씁니다\n" + _UNLABELED_PASTE)

    assert d.mode == MODE_FOREIGN and d.solution == "uipath" and d.confirm is True


def test_mode_skips_question_when_user_already_said_in_house():
    """처음부터 '사내 패키지'라고 밝혔으면 물을 게 없다."""
    from app.agent.v4.catalog_context import A360_CUSTOM
    from app.agent.v4.orchestrator.foreign_catalog import MODE_OVERLAY

    d = _mode("사내에서 만든 패키지예요\n" + _UNLABELED_PASTE)

    assert d.mode == MODE_OVERLAY and d.solution == A360_CUSTOM and d.confirm is True


def _asked_history(paste=_UNLABELED_PASTE):
    from app.agent.v4.orchestrator.foreign_catalog import ASK_MODE_ANSWER

    return [{"role": "user", "content": paste}, {"role": "assistant", "content": ASK_MODE_ANSWER}]


def test_reply_in_house_confirms_overlay_session():
    from app.agent.v4.catalog_context import A360_CUSTOM
    from app.agent.v4.orchestrator.foreign_catalog import MODE_OVERLAY

    d = _mode("커스텀이에요", _asked_history())

    assert d.mode == MODE_OVERLAY and d.solution == A360_CUSTOM and d.confirm is True


def test_reply_naming_product_confirms_foreign_session():
    from app.agent.v4.orchestrator.foreign_catalog import MODE_FOREIGN

    d = _mode("Power Automate입니다", _asked_history())

    assert d.mode == MODE_FOREIGN and d.solution == "power automate" and d.confirm is True


def test_reply_other_solution_without_name_is_still_not_a360():
    from app.agent.v4.orchestrator.foreign_catalog import MODE_FOREIGN

    d = _mode("다른 솔루션 거예요", _asked_history())

    assert d.mode == MODE_FOREIGN and d.solution == "other"


def test_unanswered_question_is_not_repeated_forever():
    """같은 질문을 매 턴 반복하면 대화가 막힌다. 오버레이는 순수 a360의 상위집합이라
    이 추측이 틀려도 잃는 게 없다 — 다만 추측이므로 세션에 굳히지는 않는다(confirm=False)."""
    from app.agent.v4.orchestrator.foreign_catalog import MODE_OVERLAY

    d = _mode("그냥 만들어줘", _asked_history())

    assert d.mode == MODE_OVERLAY and d.confirm is False


def test_pasted_custom_package_name_does_not_preempt_the_question():
    """'Custom.Excel/Read' 같은 표기가 답변 단서로 오독되면 되묻기가 통째로 죽는다."""
    from app.agent.v4.orchestrator.foreign_catalog import MODE_ASK

    paste = "액션 목록\n- Custom.Excel/Read\n- Custom.Mail/Send\n- Custom.Erp/Post\n만들어줘"

    assert _mode(paste).mode == MODE_ASK


def test_confirmed_session_wins_over_detection():
    """사용자 PATCH·이전 턴 확정이 감지보다 우선 — 오탐이 선택을 덮으면 되돌려도 다시 뒤집힌다."""
    from app.agent.v4.catalog_context import A360_CUSTOM
    from app.agent.v4.orchestrator.foreign_catalog import MODE_FOREIGN, MODE_OVERLAY

    assert _mode(_UNLABELED_PASTE, solution=A360_CUSTOM).mode == MODE_OVERLAY
    assert _mode(_UNLABELED_PASTE, solution="uipath").mode == MODE_FOREIGN
    assert _mode(_UNLABELED_PASTE, solution=A360_CUSTOM).confirm is False  # 이미 확정된 값


def test_edit_path_never_asks_and_never_redetects():
    """edit은 '이미 만들어진 흐름이 어느 어휘냐'만 알면 된다 — 수정 요청에 되묻기는 부적절하다."""
    from app.agent.v4.orchestrator.foreign_catalog import MODE_A360, decide_catalog_mode
    from app.agent.v4.verify.catalog import get_catalog

    d = decide_catalog_mode(
        {"message": _UNLABELED_PASTE, "history": [], "solution": "a360"},
        get_catalog(), detect_new=False,
    )

    assert d.mode == MODE_A360


# --- 감지 시점: 생성 전 (§6.6) ---

def test_generate_node_asks_before_spending_a_generation():
    """예전엔 생성 뒤에 감지해 첫 턴 흐름도(LLM 수십 회)가 통째로 버려졌다."""
    import asyncio

    from app.agent.v4.orchestrator import generate as gen
    from app.agent.v4.orchestrator.state import TYPE_ANSWER

    async def _must_not_run(*a, **k):
        raise AssertionError("모드를 확정하기 전에 흐름을 만들면 안 된다")

    original = gen._generate_with
    gen._generate_with = _must_not_run
    try:
        out = asyncio.run(gen.generate_node(
            {"message": _UNLABELED_PASTE, "history": [], "solution": "a360"}
        ))
    finally:
        gen._generate_with = original

    assert out["turn_type"] == TYPE_ANSWER
    assert "사내 커스텀 패키지" in out["answer"]
    assert "detected_solution" not in out  # 확정 전이라 세션을 바꾸지 않는다


def test_generate_node_builds_with_overlay_on_the_same_turn():
    """확정된 턴에 바로 커스텀 어휘로 만든다 — '한 턴 더 요청'이 없어야 §6.6이 달성된다."""
    import asyncio

    from app.agent.v4.catalog_context import A360_CUSTOM
    from app.agent.v4.orchestrator import generate as gen

    seen = {}

    async def _capture(state, ctx):
        seen["ctx"] = ctx
        return {"turn_type": "recommendation", "answer": "만들었어요", "sources": []}

    def _fake_extract(state):
        return gen.CatalogExtraction(
            solution=None,
            actions=[gen.UserCatalogAction(package="AcmeErp", action="PostVoucher")],
        )

    originals = (gen._generate_with, gen.extract_user_catalog)
    gen._generate_with, gen.extract_user_catalog = _capture, _fake_extract
    try:
        out = asyncio.run(gen.generate_node(
            {"message": "사내 커스텀 패키지예요\n" + _UNLABELED_PASTE, "history": [], "solution": "a360"}
        ))
    finally:
        gen._generate_with, gen.extract_user_catalog = originals

    assert out["detected_solution"] == A360_CUSTOM  # 세션에 오버레이 모드가 굳는다(재오픈 복원)
    assert seen["ctx"].has_overlay and seen["ctx"].is_a360 and seen["ctx"].searchable


def test_generate_node_says_so_when_custom_extraction_came_back_empty():
    """조용히 A360으로 만들면 사용자는 자기 액션이 반영된 줄 안다 — RPA-285의 그 실패."""
    import asyncio

    from app.agent.v4.orchestrator import generate as gen

    async def _plain(state, ctx):
        return {"turn_type": "recommendation", "answer": "만들었어요", "sources": []}

    originals = (gen._generate_with, gen.extract_user_catalog)
    gen._generate_with = _plain
    gen.extract_user_catalog = lambda state: gen.CatalogExtraction(solution=None, actions=[])
    try:
        out = asyncio.run(gen.generate_node(
            {"message": "사내 커스텀 패키지예요\n" + _UNLABELED_PASTE, "history": [], "solution": "a360"}
        ))
    finally:
        gen._generate_with, gen.extract_user_catalog = originals

    assert "A360 표준 카탈로그로만" in out["answer"]
