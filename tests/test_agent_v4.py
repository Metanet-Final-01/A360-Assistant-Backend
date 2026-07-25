"""v4 에이전트 스모크 — 벤더링 무결성과 인프라 격리를 못 박는다 (RPA-298).

`test_agent_v3.py`(817줄)를 통째 복제하지 않는다. v4는 v3의 벤더링 복사본에서 출발하므로
동작 검증은 그쪽이 이미 하고 있고, 여기서 지켜야 할 것은 다른 두 가지다:

1. **벤더링 무결성** — v4가 자기 모듈만 쓰고(v3로 새지 않고), 진입점·상대 임포트·프롬프트
   경로가 온전한가.
2. **인프라 격리** — v4 폴더를 만들면 conftest의 autouse 스텁이 v4 팩토리도 덮어야 한다.
   안 덮으면 **테스트가 운영 Neon/Bonsai를 때린다**(v2·v3는 이미 덮고 있다).

지식층·채널·2상 구조 테스트는 후속 단계에서 이 파일에 얹는다.
"""

import importlib

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
