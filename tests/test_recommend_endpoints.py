"""추천안(흐름도) 생성·저장·버전 편집 엔드포인트 테스트 (RPA-61)."""

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


def test_save_edited_400_names_offending_fields():
    """400은 개수만이 아니라 어느 필드가 왜 틀렸는지 알려준다 — 프론트 연동 디버깅용."""
    session = SimpleNamespace(id=SID, user_id=None)
    _override(FakeDB(session=session, versions=[_row(1)]))
    bad = _valid_recommendation()
    del bad["steps"][0]["actions"][0]["order"]  # 드래그 재정렬 때 흔한 누락
    with TestClient(app) as c:
        r = c.post(f"/api/sessions/{SID}/recommendations", json={"recommendation": bad})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "INVALID_RECOMMENDATION"
    fields = [e["field"] for e in detail["errors"]]
    assert "steps.0.actions.0.order" in fields  # 정확한 경로를 짚어준다


def test_save_edited_400_does_not_reflect_input():
    """400은 사용자가 보낸 값을 응답에 반향하지 않는다 — 원문 유출·응답 비대 방지.

    sentinel을 Pydantic이 err["input"]에 싣는 자리(int 자리의 문자열)에 놓는다. 유효한
    필드(notes 등)에 두면 어떤 에러에도 그 값이 안 실려, input을 그대로 내보내도 통과하는
    무력한 테스트가 된다.
    """
    session = SimpleNamespace(id=SID, user_id=None)
    _override(FakeDB(session=session, versions=[_row(1)]))
    sentinel = "SENTINEL-7f3ac91e-원문유출탐지"
    bad = _valid_recommendation()
    bad["steps"][0]["actions"][0]["order"] = sentinel  # int 자리 → input에 sentinel이 실린다
    with TestClient(app) as c:
        r = c.post(f"/api/sessions/{SID}/recommendations", json={"recommendation": bad})
    assert r.status_code == 400
    assert r.json()["detail"]["errors"], "위반 필드가 비면 반향 검사가 무의미하다"
    assert sentinel not in r.text  # 응답 어디에도(errors·message 포함) 원문이 없다


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
