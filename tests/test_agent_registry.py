"""app/agent 버전 레지스트리·디스패처 단위 테스트 (RPA-167).

버전 자동탐색(v\\d+)·기본버전(env AGENT_VERSION)·지연 위임·미지버전 거부·버전 격리를
LLM/DB 없이 검증한다. "v1/v2가 각자 위치에서 온전히 import되고 디스패처가 올바른 구현으로
위임한다"는 계약과 "버전 추가 시 목록이 코드 수정 없이 반영된다"는 원칙을 CI에서 지킨다.
"""

import importlib

import pytest

from app.agent import available_versions, default_version
from app.agent.registry import resolve_version


def test_discovers_v1_and_v2_with_metadata():
    """레지스트리가 vN 폴더를 자동 발견하고 meta.py의 label/description을 싣는다."""
    versions = available_versions()
    ids = {v["id"] for v in versions}
    assert {"v1", "v2"} <= ids
    for v in versions:
        assert set(v) >= {"id", "label", "description", "default"}
        assert v["label"]  # meta.py가 비어도 id로 폴백하므로 최소 id는 보장
    # 기본은 정확히 하나.
    assert sum(v["default"] for v in versions) == 1


def test_default_version_is_v2_without_env(monkeypatch):
    monkeypatch.delenv("AGENT_VERSION", raising=False)
    assert default_version() == "v2"


def test_default_version_follows_env(monkeypatch):
    """env AGENT_VERSION이 발견된 버전이면 그것이 기본이 되고 available_versions에도 반영된다."""
    monkeypatch.setenv("AGENT_VERSION", "v1")
    assert default_version() == "v1"
    v1 = next(v for v in available_versions() if v["id"] == "v1")
    assert v1["default"] is True


def test_default_version_falls_back_for_unknown_env(monkeypatch):
    """존재하지 않는 env 값은 조용히 폴백(v2) — 부팅을 죽이지 않는다."""
    monkeypatch.setenv("AGENT_VERSION", "v99")
    assert default_version() == "v2"


@pytest.mark.parametrize("name", ["v1", "v2"])
def test_resolve_version_exposes_entrypoints(name):
    """양 버전이 각자 위치에서 온전히 import되고 진입점 3종을 노출한다(이동 무결성)."""
    mod = resolve_version(name)
    for attr in ("stream_agent_turn", "recommend", "analyze"):
        assert hasattr(mod, attr), f"{name}.{attr} 누락"


def test_resolve_none_uses_default(monkeypatch):
    monkeypatch.delenv("AGENT_VERSION", raising=False)
    assert resolve_version(None) is resolve_version("v2")


def test_unknown_explicit_version_raises():
    """명시 요청이 미지 버전이면 계약을 코드에서도 강제(ValueError)."""
    with pytest.raises(ValueError):
        resolve_version("v99")


def test_version_isolation_v1_plan_v2_agentic():
    """벤더링 격리 — v1은 단계분해(build_graph), v2는 agentic(build_agent_graph)."""
    g1 = importlib.import_module("app.agent.v1.recommend.graph")
    g2 = importlib.import_module("app.agent.v2.recommend.graph")
    assert hasattr(g1, "build_graph") and not hasattr(g1, "build_agent_graph")
    assert hasattr(g2, "build_agent_graph")


def test_dispatcher_keeps_public_symbol():
    """백엔드 import·테스트 monkeypatch 대상인 app.agent.stream_agent_turn이 최상위에 유지된다."""
    import app.agent as agent_pkg

    assert callable(agent_pkg.stream_agent_turn)
    assert callable(agent_pkg.available_versions)
