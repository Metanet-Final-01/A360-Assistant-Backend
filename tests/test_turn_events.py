"""턴 노드 타임라인 관측 테스트 (RPA-105) — /turn SSE 래퍼가 stage/done/error를 버퍼링·적재."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.api.sessions as sessions_api
from app.db import get_db
from app.main import app

SID = uuid.uuid4()


class FakeDB:
    def __init__(self, session=None):
        self.session = session

    def get(self, model, key):
        return self.session

    def execute(self, stmt):
        return SimpleNamespace(
            scalar_one_or_none=lambda: None,
            scalars=lambda: SimpleNamespace(all=lambda: []),
        )


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


def _run_turn(monkeypatch, agent_events, captured_saves):
    """가짜 에이전트로 턴 실행, _save_turn_events 호출을 가로챈다."""
    async def _fake_turn(message, context):
        for ev in agent_events:
            yield ev
    monkeypatch.setattr("app.agent.stream_agent_turn", _fake_turn, raising=False)
    monkeypatch.setattr(sessions_api, "_save_turn_events",
                        lambda sid, rid, rows: captured_saves.append((sid, rid, rows)))
    monkeypatch.setattr("app.db.SessionLocal", _persist_stub())
    app.dependency_overrides[get_db] = lambda: FakeDB(
        session=SimpleNamespace(id=SID, user_id=None, solution="a360"))
    app.dependency_overrides[sessions_api.get_optional_user] = lambda: None
    with TestClient(app) as c:
        with c.stream("POST", f"/api/sessions/{SID}/turn", json={"message": "안녕"}) as r:
            list(r.iter_lines())


def _persist_stub():
    class _P:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, stmt): return SimpleNamespace(scalar=lambda: None, scalar_one_or_none=lambda: None)
        def add(self, row): pass
        def commit(self): pass
    return _P


def test_turn_events_buffered_and_saved(monkeypatch):
    """stage(+data)·done이 seq 순서로 버퍼링돼 턴 종료 시 한 번 적재된다."""
    from app.schemas import ProgressEvent

    saves = []
    _run_turn(monkeypatch, [
        ProgressEvent(event="stage", stage="routing", message="요청 분석 중",
                      data={"route": "qa", "reason": "테스트"}),
        ProgressEvent(event="token", stage="chat", message="안"),   # token은 제외돼야 함
        ProgressEvent(event="stage", stage="reading", message="질문 이해 중"),
        ProgressEvent(event="done", data={"type": "answer", "answer": "응답", "sources": []}),
    ], saves)

    assert len(saves) == 1, "턴 종료 시 정확히 1회 일괄 적재"
    sid, rid, rows = saves[0]
    assert sid == SID
    kinds = [r["kind"] for r in rows]
    assert kinds == ["stage", "stage", "done"]           # token 제외, 순서 유지
    assert [r["seq"] for r in rows] == [0, 1, 2]
    assert rows[0]["stage"] == "routing"
    assert '"route": "qa"' in rows[0]["detail"]           # 에이전트 보강 data가 JSON으로
    assert rows[1]["detail"] is None                      # data 없으면 None
    assert '"type": "answer"' in rows[2]["detail"]
    assert all(isinstance(r["elapsed_ms"], int) for r in rows)


def test_turn_events_error_recorded(monkeypatch):
    """에이전트 error도 타임라인에 남는다."""
    from app.schemas import ProgressEvent

    saves = []
    _run_turn(monkeypatch, [
        ProgressEvent(event="stage", stage="routing", message="요청 분석 중"),
        ProgressEvent(event="error", stage="agent", message="실패했습니다"),
    ], saves)

    (_, _, rows), = saves
    assert [r["kind"] for r in rows] == ["stage", "error"]
    assert rows[1]["message"] == "실패했습니다"


def test_save_turn_events_best_effort(monkeypatch):
    """관측 DB 장애가 예외로 새지 않는다 (best-effort)."""
    class _Boom:
        def __call__(self):
            raise RuntimeError("db down")
    import app.core.observability_db as obs
    monkeypatch.setattr(obs, "observability_sessionmaker", _Boom())
    # 예외 없이 조용히 넘어가야 한다
    sessions_api._save_turn_events(SID, "req1", [{"seq": 0, "kind": "done", "stage": None,
                                                  "message": None, "detail": None, "elapsed_ms": 1}])
