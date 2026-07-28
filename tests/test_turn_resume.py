"""챗 턴 SSE 재개 (RPA-339) — 새로고침해도 답변이 이어지는지 검증한다.

무엇을 보는가:
- **끊겨도 생성이 계속되고** 모든 프레임이 공유 버퍼에 남는가
- 재구독이 `after` 다음부터 이어 붙여 **끊긴 적 없는 것과 같은 프레임 열**을 주는가
- 세션당 활성 턴 1개(새로고침 연타로 LLM이 중복 실행되지 않는가)
- **남의 세션 turn_id는 못 읽는가**(버퍼 키가 세션 네임스페이스 안)
- `REDIS_URL` 미설정이면 **기존 동작 그대로**인가

실 Redis 없이 fakeredis(async)로 돈다 — rag_cache의 Redis 테스트 선례와 같은 방식이다.
"""

import json
import uuid
from types import SimpleNamespace

import fakeredis
import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

import app.api.sessions as sessions_api
import app.services.turn_stream as turn_stream
from app.db import get_db
from app.main import app

SID = uuid.uuid4()
OTHER_SID = uuid.uuid4()


class FakeDB:
    """세션 조회 + 컨텍스트 조립 쿼리를 흉내낸다(test_agent_turn의 축약본)."""

    def __init__(self, session=None):
        self.session = session

    def get(self, model, key):
        return self.session

    def execute(self, stmt):
        return SimpleNamespace(
            scalar_one_or_none=lambda: None,
            scalars=lambda: SimpleNamespace(all=lambda: []),
            scalar=lambda: None,
        )


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()
    turn_stream.reset_client()


@pytest.fixture
def redis_on(monkeypatch):
    """turn_stream을 fakeredis(async)로 켠다. 반환값은 **동기** 검사용 클라이언트다.

    같은 `FakeServer`를 async(앱이 쓰는 것)와 sync(테스트가 들여다보는 것)가 공유한다 —
    async 클라이언트를 테스트에서 동기로 부르면 코루틴만 돌려받아 검증이 조용히 헛돈다.
    """
    server = fakeredis.FakeServer()
    app_client = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setattr(turn_stream, "_make_client", lambda url: app_client)
    turn_stream.reset_client()
    return fakeredis.FakeRedis(server=server, decode_responses=True)


def _override(db, user=None):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[sessions_api.get_optional_user] = lambda: user


def _install_agent(monkeypatch, events):
    async def _fake_turn(message, context):
        _fake_turn.calls += 1
        for ev in events:
            yield ev

    _fake_turn.calls = 0
    monkeypatch.setattr("app.agent.stream_agent_turn", _fake_turn, raising=False)
    return _fake_turn


def _persist_noop(monkeypatch):
    """DB 저장을 무해하게 — 이 파일의 관심사는 스트림 재개지 저장 스키마가 아니다."""

    class _P:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, stmt): return SimpleNamespace(scalar=lambda: None)
        def add(self, row): pass
        def commit(self): pass

    monkeypatch.setattr("app.db.SessionLocal", _P)


def _answer_events():
    from app.schemas import ProgressEvent

    return [
        ProgressEvent(event="stage", stage="routing", message="라우팅"),
        ProgressEvent(event="token", message="안"),
        ProgressEvent(event="token", message="녕"),
        ProgressEvent(event="done", stage="agent", data={"type": "answer", "answer": "안녕"}),
    ]


def _post_turn(sid=SID, message="안녕"):
    with TestClient(app) as c:
        with c.stream("POST", f"/api/sessions/{sid}/turn", json={"message": message}) as r:
            status = r.status_code
            body = r.read().decode() if status != 200 else None
            frames = [] if status != 200 else [
                json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data:")
            ]
    return status, frames, body


# --- Redis 미설정: 기존 동작 보존 ---

def test_without_redis_contract_is_unchanged(monkeypatch):
    """`REDIS_URL` 미설정이면 재개 프레임이 **아예 없다** — 도입 전과 같은 프레임 열.

    이게 깨지면 Redis를 안 쓰는 배포·로컬·CI의 계약이 조용히 바뀐 것이다(실제로 초안에서
    무조건 발행했다가 기존 턴 테스트 2건이 깨졌다).
    """
    monkeypatch.delenv("REDIS_URL", raising=False)
    turn_stream.reset_client()
    _install_agent(monkeypatch, _answer_events())
    _persist_noop(monkeypatch)
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))

    status, frames, _ = _post_turn()
    assert status == 200
    assert not [f for f in frames if f.get("stage") == "turn_started"]


def test_resume_endpoint_503_without_redis(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    turn_stream.reset_client()
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/turns/deadbeef/stream")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "RESUME_UNAVAILABLE"


# --- Redis 켠 상태: 버퍼·재구독 ---

def test_turn_id_is_announced_and_buffered(monkeypatch, redis_on):
    """재개가 켜지면 첫 프레임으로 turn_id를 주고, 모든 프레임이 버퍼에 남는다."""
    _install_agent(monkeypatch, _answer_events())
    _persist_noop(monkeypatch)
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))

    status, frames, _ = _post_turn()
    assert status == 200
    head = [f for f in frames if f.get("stage") == "turn_started"]
    assert len(head) == 1, "turn_started 프레임이 없다 — 프론트가 재구독 키를 못 받는다"
    turn_id = head[0]["data"]["turn_id"]
    assert head[0]["data"]["resumable"] is True

    rows = list(redis_on.xrange(turn_stream._events_key(str(SID), turn_id)))
    assert rows, "버퍼가 비었다"
    assert rows[-1][1]["end"] == "1", "종료 마커가 없다 — 재구독자가 끝을 모른다"


def test_resume_replays_from_cursor(monkeypatch, redis_on):
    """재구독이 `after` **다음**부터만 준다 — 이미 본 프레임을 두 번 그리지 않게."""
    _install_agent(monkeypatch, _answer_events())
    _persist_noop(monkeypatch)
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))

    _, frames, _ = _post_turn()
    turn_id = next(f for f in frames if f.get("stage") == "turn_started")["data"]["turn_id"]
    entries = list(redis_on.xrange(turn_stream._events_key(str(SID), turn_id)))
    first_id = entries[0][0]

    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/turns/{turn_id}/stream", params={"after": first_id})
    assert r.status_code == 200
    text = r.text
    # 첫 엔트리(turn_started)는 제외되고 이후 프레임만
    assert "turn_started" not in text
    assert "안" in text and "done" in text
    assert "id: " in text, "재구독 커서(id: 줄)가 없다 — 다음 재구독이 이어붙일 지점을 모른다"


def test_resume_from_scratch_replays_everything(monkeypatch, redis_on):
    """`after` 없이 붙으면 처음부터 — 프론트가 마지막 id를 모를 때 화면을 다시 그린다."""
    _install_agent(monkeypatch, _answer_events())
    _persist_noop(monkeypatch)
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))

    _, frames, _ = _post_turn()
    turn_id = next(f for f in frames if f.get("stage") == "turn_started")["data"]["turn_id"]

    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/turns/{turn_id}/stream")
    assert r.status_code == 200
    assert "turn_started" in r.text and "done" in r.text


# --- 중복 실행 차단 ---

def test_second_turn_while_active_returns_409(monkeypatch, redis_on):
    """활성 턴이 있으면 새 턴을 시작하지 않고 그 turn_id로 재구독하라고 알린다.

    새로고침 연타로 같은 세션에 LLM 턴이 여러 개 도는 것을 막는 계약이다.
    """
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))
    # 활성 표식만 먼저 심어 "이미 도는 턴"을 만든다(에이전트는 부르지 않는다).
    redis_on.set(turn_stream._active_key(str(SID)), "running-turn", ex=60)
    agent = _install_agent(monkeypatch, _answer_events())
    _persist_noop(monkeypatch)

    status, _, body = _post_turn()
    assert status == 409
    detail = json.loads(body)["detail"]
    assert detail["code"] == "TURN_IN_PROGRESS"
    assert detail["turn_id"] == "running-turn"
    assert agent.calls == 0, "409인데 에이전트를 불렀다 — LLM 비용이 중복으로 나간다"


# --- 소유권: 남의 세션 턴은 못 본다 ---

def test_other_session_turn_id_is_not_readable(monkeypatch, redis_on):
    """다른 세션에서 만든 turn_id를 내 session_id로 구독하면 404.

    버퍼 키가 세션 네임스페이스 안이라, 소유권 검사(session)와 실제 읽는 대상(버퍼)이
    일치한다 — 검사와 동작이 갈리면 그 자체가 취약점이다.
    """
    _install_agent(monkeypatch, _answer_events())
    _persist_noop(monkeypatch)
    _override(FakeDB(session=SimpleNamespace(id=OTHER_SID, user_id=None, solution="a360")))
    _, frames, _ = _post_turn(sid=OTHER_SID)
    other_turn_id = next(f for f in frames if f.get("stage") == "turn_started")["data"]["turn_id"]

    # 이제 내 세션으로 그 turn_id를 구독 시도
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/turns/{other_turn_id}/stream")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "TURN_NOT_FOUND"


def test_unknown_turn_id_is_404(monkeypatch, redis_on):
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/turns/{uuid.uuid4().hex}/stream")
    assert r.status_code == 404


# --- Qodo #455 반영분 회귀 ---

def test_redis_down_falls_back_to_non_resumable(monkeypatch):
    """`REDIS_URL`은 있는데 Redis가 죽었으면 **재개 모드로 가지 않는다**.

    설정(enabled)만 보고 재개 모드로 가면, 끊긴 뒤에도 계속 생성해 **비용만 쓰고 재개도 안 되는**
    양쪽 손해가 된다. 선점(claim)이 실제로 성공했는지로 판단해야 한다.
    """
    class _Dead:
        async def set(self, *a, **k):
            raise ConnectionError("redis down")

        async def get(self, *a, **k):
            raise ConnectionError("redis down")

    monkeypatch.setenv("REDIS_URL", "redis://dead")
    monkeypatch.setattr(turn_stream, "_make_client", lambda url: _Dead())
    turn_stream.reset_client()
    _install_agent(monkeypatch, _answer_events())
    _persist_noop(monkeypatch)
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))

    status, frames, _ = _post_turn()
    assert status == 200, "Redis 장애가 턴 자체를 막으면 안 된다"
    assert not [f for f in frames if f.get("stage") == "turn_started"], (
        "버퍼가 죽었는데 재개 모드로 진입했다 — 끊겨도 계속 생성해 비용만 나간다"
    )


def test_resume_follows_when_turn_timeout_disabled(monkeypatch, redis_on):
    """`TURN_MAX_DURATION_SEC=0`(=상한 끔)에서도 재구독이 **이어받기까지** 한다.

    0을 그대로 더하면 deadline이 현재시각이라 follow 루프가 한 번도 안 돌고, 재개가 replay까지만
    되고 끝난다 — 문서화된 설정값에서 기능이 죽는다.
    """
    monkeypatch.setenv("TURN_MAX_DURATION_SEC", "0")
    key = turn_stream._events_key(str(SID), "t-follow")
    redis_on.xadd(key, {"sse": "data: {\"event\":\"stage\"}\n\n", "end": "0"})  # 끝 마커 없음

    async def _fake_follow(session_id, turn_id, after, block_ms):
        return [("9-9", 'data: {"event":"token","message":"FOLLOWED"}\n\n', False),
                ("9-10", "", True)]

    monkeypatch.setattr(turn_stream, "follow", _fake_follow)
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))

    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/turns/t-follow/stream")
    assert r.status_code == 200
    assert "FOLLOWED" in r.text, "상한 0에서 follow 루프가 돌지 않았다 — 재개가 replay에서 멈춘다"
