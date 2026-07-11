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


# --- SSE 클라이언트 끊김 처리 (RPA-106) ---

def _setup_disconnect(monkeypatch, agent_events, disconnect_flags):
    """가짜 에이전트 + is_disconnected 시퀀스. (소비된 이벤트 수, persist 호출, tev 저장) 반환."""
    consumed = {"n": 0}

    async def _fake_turn(message, context):
        for ev in agent_events:
            consumed["n"] += 1
            yield ev

    flags = iter(disconnect_flags)

    async def _fake_dc(self):
        return next(flags, True)

    monkeypatch.setattr("app.agent.stream_agent_turn", _fake_turn, raising=False)
    monkeypatch.setattr("starlette.requests.Request.is_disconnected", _fake_dc)
    persists = []
    monkeypatch.setattr(sessions_api, "_persist_turn_result",
                        lambda *a: persists.append(a) or {"type": "answer", "answer": "x", "sources": []})
    monkeypatch.setattr(sessions_api, "_read_intake_gauge", lambda sid: None)
    saves = []
    monkeypatch.setattr(sessions_api, "_save_turn_events",
                        lambda sid, rid, rows: saves.append(rows))
    monkeypatch.setattr("app.db.SessionLocal", _persist_stub())
    app.dependency_overrides[get_db] = lambda: FakeDB(
        session=SimpleNamespace(id=SID, user_id=None, solution="a360"))
    app.dependency_overrides[sessions_api.get_optional_user] = lambda: None
    lines = []
    with TestClient(app) as c:
        with c.stream("POST", f"/api/sessions/{SID}/turn", json={"message": "안녕"}) as r:
            lines = [ln for ln in r.iter_lines() if ln.startswith("data:")]
    return consumed["n"], persists, saves, lines


def test_disconnect_stops_stream_and_skips_persist(monkeypatch):
    """done 전 끊김 → 에이전트 스트림 소비 중단(비용 방어) + 저장 없음 + 관측 기록."""
    from app.schemas import ProgressEvent

    events = [ProgressEvent(event="stage", stage=f"s{i}", message=f"단계{i}") for i in range(5)]
    events.append(ProgressEvent(event="done", data={"type": "answer", "answer": "a", "sources": []}))
    # 첫 이벤트 체크만 연결됨, 이후 끊김
    n, persists, saves, _ = _setup_disconnect(monkeypatch, events, [False, True])

    assert n == 2, f"2번째 이벤트에서 끊김 감지 후 소비 중단이어야 함 (실제 {n}/6)"
    assert persists == [], "done 미도달 — 저장하면 안 됨"
    rows = saves[0]
    assert rows[-1]["kind"] == "error" and "끊김" in rows[-1]["message"]


def test_disconnect_after_done_persists_but_skips_send(monkeypatch):
    """done 후 끊김 → 완료된 작업은 저장(비용 유실 방지), 전송만 생략."""
    from app.schemas import ProgressEvent

    events = [
        ProgressEvent(event="done", data={"type": "answer", "answer": "a", "sources": []}),
        ProgressEvent(event="stage", stage="tail", message="후속"),
    ]
    n, persists, saves, lines = _setup_disconnect(monkeypatch, events, [False, True])

    assert len(persists) == 1, "done을 받았으므로 저장돼야 함"
    assert not any('"event": "done"' in ln or '"event":"done"' in ln for ln in lines), \
        "끊긴 클라이언트에 done 전송은 생략"
    assert saves[0][-2]["kind"] == "error" and saves[0][-1]["kind"] == "done"  # 끊김 기록 + done 기록


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
