"""추천안(흐름도) 생성·저장·버전 편집 엔드포인트 테스트 (RPA-61)."""

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.api.sessions as sessions_api
from app.db import get_db
from app.main import app

SID = uuid.uuid4()
AID = uuid.uuid4()


def _valid_recommendation() -> dict:
    return {
        "schema_version": "1.0",
        "steps": [
            {
                "step_id": "step-1",
                "actions": [
                    {"order": 1, "package": "Browser", "action": "openbrowser",
                     "label": "열기", "parameters": [], "children": []}
                ],
            }
        ],
        "variables": [],
        "notes": "",
    }


class FakeDB:
    """세션 조회 + Analysis/RecommendationVersion 쿼리를 시나리오별로 흉내낸다."""

    def __init__(self, session=None, analysis=None, versions=None):
        self.session = session
        self.analysis = analysis
        self.versions = versions or []

    def get(self, model, key):
        return self.session

    def execute(self, stmt):
        text = str(stmt).lower()
        if "analyses" in text:
            a = self.analysis
            return SimpleNamespace(scalar_one_or_none=lambda: a)
        if "max(" in text:  # 다음 버전 계산
            mx = max((v.version for v in self.versions), default=None)
            return SimpleNamespace(scalar=lambda: mx)
        # recommendations 목록/최신
        rows = sorted(self.versions, key=lambda v: v.version, reverse=True)
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: rows),
            scalar_one_or_none=lambda: (rows[0] if rows else None),
        )


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


def _override(db, user=None):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[sessions_api.get_optional_user] = lambda: user


def _row(version, source="agent", payload=None):
    return SimpleNamespace(
        id=uuid.uuid4(), version=version, parent_version=None, source=source,
        change_summary=None, created_at=None, analysis_id=AID,
        payload=payload or _valid_recommendation(),
    )


# --- recommend: 생성 + v1 저장 (SSE) ---

def test_recommend_streams_and_saves_v1(monkeypatch):
    from app.schemas import ProgressEvent

    async def _fake_recommend(analysis, constraints=None):
        yield ProgressEvent(event="stage", stage="recommending", message="검색 중")
        yield ProgressEvent(event="done", data={"recommendation": _valid_recommendation()})

    monkeypatch.setattr("app.agent.recommend", _fake_recommend, raising=False)

    saved = {}

    class _Persist:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, stmt): return SimpleNamespace(scalar=lambda: None)  # max version None → v1
        def add(self, row):
            if type(row).__name__ == "RecommendationVersion":
                saved["row"] = row
        def commit(self): pass

    monkeypatch.setattr("app.db.SessionLocal", _Persist)  # 실제 RecommendationVersion 인스턴스 생성

    session = SimpleNamespace(id=SID, user_id=None)
    analysis = SimpleNamespace(id=AID, status="completed", result=_analysis_result())
    _override(FakeDB(session=session, analysis=analysis))

    with TestClient(app) as c:
        with c.stream("POST", f"/api/sessions/{SID}/recommend") as r:
            events = [json.loads(l[5:]) for l in r.iter_lines() if l.startswith("data:")]

    assert events[0]["event"] == "stage"
    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["version"] == 1
    assert done["data"]["recommendation"]["steps"][0]["step_id"] == "step-1"
    assert saved["row"].source == "agent" and saved["row"].version == 1


def test_recommend_forwards_error_without_duplicating(monkeypatch):
    """recommend가 자체 error를 흘리면, 백엔드가 done 없음을 또 error로 내지 않는다 (CodeRabbit)."""
    from app.schemas import ProgressEvent

    async def _err_recommend(analysis, constraints=None):
        yield ProgressEvent(event="stage", stage="recommending", message="시작")
        yield ProgressEvent(event="error", stage="recommending", message="내부 실패")

    monkeypatch.setattr("app.agent.recommend", _err_recommend, raising=False)
    session = SimpleNamespace(id=SID, user_id=None)
    analysis = SimpleNamespace(id=AID, status="completed", result=_analysis_result())
    _override(FakeDB(session=session, analysis=analysis))

    with TestClient(app) as c:
        with c.stream("POST", f"/api/sessions/{SID}/recommend") as r:
            events = [json.loads(l[5:]) for l in r.iter_lines() if l.startswith("data:")]

    # error가 정확히 1번만 (recommend 것) — 백엔드가 중복 error를 붙이지 않음
    assert [e["event"] for e in events].count("error") == 1
    assert events[-1]["event"] == "error"


def test_recommend_409_without_analysis(monkeypatch):
    session = SimpleNamespace(id=SID, user_id=None)
    _override(FakeDB(session=session, analysis=None))
    with TestClient(app) as c:
        with c.stream("POST", f"/api/sessions/{SID}/recommend") as r:
            # 409는 스트림 시작 전 판정
            assert r.status_code == 409


# --- 조회 ---

def test_get_latest_returns_tree():
    session = SimpleNamespace(id=SID, user_id=None)
    _override(FakeDB(session=session, versions=[_row(1), _row(2)]))
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/recommendations/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 2  # 최신
    assert body["recommendation"]["steps"][0]["step_id"] == "step-1"


def test_list_versions():
    session = SimpleNamespace(id=SID, user_id=None)
    _override(FakeDB(session=session, versions=[_row(1), _row(2)]))
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/recommendations")
    assert [v["version"] for v in r.json()["versions"]] == [2, 1]


def test_latest_404_when_none():
    session = SimpleNamespace(id=SID, user_id=None)
    _override(FakeDB(session=session, versions=[]))
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/recommendations/latest")
    assert r.status_code == 404


# --- 편집 저장 (새 버전) ---

def test_save_edited_creates_new_version(monkeypatch):
    saved = {}

    class _Persist:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, stmt): return SimpleNamespace(scalar=lambda: 2)  # 현재 max=2 → 새 v3
        def add(self, row):
            if type(row).__name__ == "RecommendationVersion":
                saved["row"] = row
        def commit(self): pass

    monkeypatch.setattr("app.db.SessionLocal", _Persist)  # 실제 RecommendationVersion 인스턴스 생성

    session = SimpleNamespace(id=SID, user_id=None)
    _override(FakeDB(session=session, versions=[_row(2)]))  # base = v2

    with TestClient(app) as c:
        r = c.post(f"/api/sessions/{SID}/recommendations",
                   json={"recommendation": _valid_recommendation(), "source": "drag",
                         "change_summary": "Task1 액션 교체"})
    assert r.status_code == 201
    assert r.json()["version"] == 3
    assert saved["row"].source == "drag" and saved["row"].change_summary == "Task1 액션 교체"


def test_save_recommendation_retries_on_version_conflict(monkeypatch):
    """동시 저장 version 충돌(IntegrityError) 시 재계산 재시도로 성공한다 (CodeRabbit)."""
    from sqlalchemy.exc import IntegrityError

    # RecommendationVersion 저장 시도만 센다 (감사 미들웨어의 AuditLog commit은 제외)
    rv = {"n": 0}

    class _Persist:
        def __init__(self): self.has_rv = False
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, stmt): return SimpleNamespace(scalar=lambda: 1)
        def add(self, row):
            if type(row).__name__ == "RecommendationVersion":
                self.has_rv = True
        def commit(self):
            if self.has_rv:
                rv["n"] += 1
                if rv["n"] == 1:  # 첫 시도만 version 충돌
                    raise IntegrityError("stmt", {}, Exception("uq conflict"))

    monkeypatch.setattr("app.db.SessionLocal", _Persist)
    session = SimpleNamespace(id=SID, user_id=None)
    _override(FakeDB(session=session, versions=[_row(1)]))

    with TestClient(app) as c:
        r = c.post(f"/api/sessions/{SID}/recommendations", json={"recommendation": _valid_recommendation()})
    assert r.status_code == 201  # 재시도로 성공
    assert rv["n"] == 2          # 1회 충돌 + 1회 성공


def test_save_edited_rejects_malformed_400(monkeypatch):
    session = SimpleNamespace(id=SID, user_id=None)
    _override(FakeDB(session=session, versions=[_row(1)]))
    with TestClient(app) as c:
        r = c.post(f"/api/sessions/{SID}/recommendations",
                   json={"recommendation": {"steps": "not-a-list"}})  # 스키마 위반
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_RECOMMENDATION"


def test_save_edited_409_without_base(monkeypatch):
    session = SimpleNamespace(id=SID, user_id=None)
    _override(FakeDB(session=session, versions=[]))  # 기준 버전 없음
    with TestClient(app) as c:
        r = c.post(f"/api/sessions/{SID}/recommendations",
                   json={"recommendation": _valid_recommendation()})
    assert r.status_code == 409


# --- 소유권 ---

def test_blocks_non_owner():
    owner = uuid.uuid4()
    session = SimpleNamespace(id=SID, user_id=owner)
    _override(FakeDB(session=session, versions=[_row(1)]), user=SimpleNamespace(id=uuid.uuid4()))
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/recommendations/latest")
    assert r.status_code == 403


def _analysis_result() -> dict:
    return {
        "schema_version": "1.0", "document_title": "t", "summary": "s",
        "steps": [{"step_id": "step-1", "order": 1, "name": "n", "description": "d"}],
        "ambiguities": [],
    }
