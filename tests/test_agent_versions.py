"""에이전트 버전 선택 API 배선 테스트 (RPA-169).

⚠️ 이 테스트가 스텁하는 `app.agent.available_versions`/`default_version`은 **아직 존재하지
않는다** — RPA-167(정준환)이 랜딩하면 실물이 된다. 그래서 여기 스텁은 "가짜"가 아니라 요청서에
합의된 **계약의 실행 가능한 명세**다: 실물이 이 형태를 안 지키면 이 테스트가 깨진다.

RPA-167 랜딩 후에야 확인 가능한 것(v1/v2 경로가 실제로 갈리는지)은 여기서 못 한다 —
요청서 6항 3·5번은 랜딩 후 live로 확인해야 한다.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.agent as agent_mod
import app.api.sessions as sessions_api
from app.main import app

SID = uuid.uuid4()

# RPA-167 요청서가 명시한 반환 형태 그대로
_VERSIONS = [
    {"id": "v1", "label": "v1 · 단계분해 매핑", "description": "…", "default": False},
    {"id": "v2", "label": "v2 · Agentic (ReAct)", "description": "…", "default": True},
]


@pytest.fixture
def registry(monkeypatch):
    """RPA-167이 export할 registry를 계약대로 심는다 (아직 실물이 없으므로 raising=False)."""
    monkeypatch.setattr(agent_mod, "available_versions", lambda: _VERSIONS, raising=False)
    monkeypatch.setattr(agent_mod, "default_version", lambda: "v2", raising=False)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


# --- GET /api/agent/versions ---

def test_versions_endpoint_lists_registry(registry):
    """프론트 셀렉터 소스 — 목록과 기본값을 registry에서 그대로 노출한다."""
    with TestClient(app) as c:
        r = c.get("/api/agent/versions")
    assert r.status_code == 200
    body = r.json()
    assert [v["id"] for v in body["versions"]] == ["v1", "v2"]
    assert body["default"] == "v2"


def test_versions_endpoint_503_before_registry_lands():
    """RPA-167 랜딩 전엔 503 — /turn의 에이전트 미구현 응답과 같은 코드로 통일한다.

    registry를 심지 않았으므로 실제 dev 상태(레지스트리 부재)를 그대로 탄다.
    """
    with TestClient(app) as c:
        r = c.get("/api/agent/versions")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "AGENT_UNAVAILABLE"


# --- agent_version 검증 (동적 — 하드코딩 금지가 요청서 핵심 원칙) ---

def test_unknown_version_rejected_422(registry):
    """미지 버전은 422로 떨어져 프론트에 명확히 피드백된다 (요청서 6항 4번)."""
    with TestClient(app) as c:
        r = c.post(f"/api/sessions/{SID}/turn", json={"message": "안녕", "agent_version": "v9"})
    assert r.status_code == 422


def test_registry_drives_validation_not_hardcoded_literal(monkeypatch):
    """검증은 registry가 주도한다 — v3를 추가하면 백엔드 코드 변경 없이 통과해야 한다.

    이게 요청서의 ⭐핵심 원칙이다. Literal["v1","v2"]로 하드코딩했다면 v3가 422로 막혀 이
    테스트가 깨진다.
    """
    v3 = _VERSIONS + [{"id": "v3", "label": "v3 · 미래", "description": "…", "default": False}]
    monkeypatch.setattr(agent_mod, "available_versions", lambda: v3, raising=False)
    monkeypatch.setattr(agent_mod, "default_version", lambda: "v2", raising=False)

    assert sessions_api.AgentTurnRequest(message="안녕", agent_version="v3").agent_version == "v3"
    with pytest.raises(ValueError):
        sessions_api.AgentTurnRequest(message="안녕", agent_version="v4")


def test_version_omitted_is_allowed_without_registry():
    """agent_version 미지정은 레지스트리가 없어도 통과한다 — 기존 동작을 깨지 않는다."""
    assert sessions_api.AgentTurnRequest(message="안녕").agent_version is None


# --- context 전달 (누락 시 버전 혼선) ---

def _fake_db(session):
    class _DB:
        def get(self, model, key):
            return session

        def execute(self, stmt):
            return SimpleNamespace(
                scalar_one_or_none=lambda: None,
                scalars=lambda: SimpleNamespace(all=lambda: []),
            )

    return _DB()


def test_context_carries_agent_version():
    """조립된 agent_context에 agent_version이 실린다 — 에이전트가 이 키로 그래프를 고른다."""
    session = SimpleNamespace(id=SID, user_id=None, solution="A360")
    ctx = sessions_api._assemble_turn_context(
        session, _fake_db(session), operation="chat", agent_version="v1")
    assert ctx["agent_context"]["agent_version"] == "v1"


def test_context_defaults_to_none_so_agent_picks_server_default():
    """미지정이면 None으로 실린다 — 에이전트가 env AGENT_VERSION 기본을 쓴다."""
    session = SimpleNamespace(id=SID, user_id=None, solution="A360")
    ctx = sessions_api._assemble_turn_context(session, _fake_db(session))
    assert ctx["agent_context"]["agent_version"] is None
