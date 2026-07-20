"""추천안(흐름도) 저장·버전 관리를 실 Postgres로 검증한다 (RPA-168).

여기 있는 건 전부 **mock이 원리적으로 못 잡는 것**들이다 — DB가 실행하는 것(UNIQUE 제약,
JSONB 직렬화, FK CASCADE)과 커밋 의미론. `tests/test_recommend_endpoints.py`의 대응 테스트는
`SessionLocal`을 통째로 가짜로 바꾸므로 이 중 어느 것도 검증하지 못한다.

예: 거기 `test_save_recommendation_retries_on_version_conflict`는 **가짜 IntegrityError를 손으로
던진다** — 재시도 분기는 밟지만 진짜 경쟁도, 진짜 제약도 없다.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.sessions import _save_recommendation
from app.main import app

pytestmark = pytest.mark.integration


def _rec(n_actions: int = 1) -> dict:
    """Recommendation 스키마를 만족하는 최소 흐름도. n_actions로 편집 결과를 구분한다."""
    return {
        "schema_version": "1.0",
        "steps": [{
            "step_id": "step-1",
            "label": "엑셀 열기",
            "actions": [
                {"order": i + 1, "package": "Excel_MS", "action": "GoToCell",
                 "label": f"액션{i + 1}", "parameters": [], "children": []}
                for i in range(n_actions)
            ],
        }],
        "variables": [],
        "notes": "",
    }


@pytest.fixture
def as_owner(seeded):
    """소유권 검사를 타되 세션 소유자로 인증된 상태 — 관심사는 저장이 실제로 되느냐다."""
    from types import SimpleNamespace

    import app.api.sessions as sessions_api

    app.dependency_overrides[sessions_api.get_optional_user] = lambda: SimpleNamespace(
        id=seeded["user_id"])
    yield
    app.dependency_overrides.pop(sessions_api.get_optional_user, None)


@pytest.fixture
def seeded(db_session):
    """소유자·세션·문서·분석 + base 추천안 v1(에이전트 경로 모사)을 실제로 심는다."""
    uid, sid, did, aid = (uuid.uuid4() for _ in range(4))
    db_session.execute(text(
        "insert into users (id, email, password_hash, created_at) "
        "values (:i, :e, 'x', now())"), {"i": uid, "e": f"it-{uid.hex[:8]}@test.com"})
    db_session.execute(text(
        "insert into analysis_sessions (id, user_id, title, created_at, updated_at) "
        "values (:i, :u, 'it', now(), now())"), {"i": sid, "u": uid})
    db_session.execute(text(
        "insert into documents (id, session_id, filename, status, masked, created_at) "
        "values (:i, :s, 'it.docx', 'parsed', false, now())"), {"i": did, "s": sid})
    db_session.execute(text(
        "insert into analyses (id, session_id, document_id, status, result, created_at) "
        "values (:i, :s, :d, 'completed', '{}', now())"), {"i": aid, "s": sid, "d": did})
    db_session.commit()
    _save_recommendation(sid, aid, _rec(), source="chat", parent_version=None)
    return {"user_id": uid, "session_id": sid, "analysis_id": aid}


def test_save_writes_real_row_and_increments_version(seeded, db_session):
    """저장이 실제 행을 남기고 버전이 오른다 — mock은 add() 호출만 봤을 뿐 커밋을 안 봤다."""
    out = _save_recommendation(
        seeded["session_id"], seeded["analysis_id"], _rec(2),
        source="drag", parent_version=None, change_summary="사용자 드래그 편집")
    assert out["version"] == 2 and out["parent_version"] == 1

    rows = db_session.execute(text(
        "select version, parent_version, source, change_summary "
        "from recommendations where session_id = :s order by version"),
        {"s": seeded["session_id"]}).all()
    assert [(r.version, r.parent_version, r.source) for r in rows] == [
        (1, None, "chat"), (2, 1, "drag")]
    assert rows[1].change_summary == "사용자 드래그 편집"


def test_save_recovers_from_real_unique_violation(seeded, db_session, integration_engine,
                                                  monkeypatch):
    """다른 커넥션이 같은 version을 먼저 채가면, **진짜 UNIQUE 제약**에 걸리고 재시도로 회복한다.

    mock 테스트(`test_save_recommendation_retries_on_version_conflict`)는 IntegrityError를
    **손으로 던진다** — 재시도 분기만 밟을 뿐 제약이 실재하는지, 재계산이 맞는지는 못 본다.
    여기선 uq_recommendations_session_version이 실제로 던진다.

    ⚠️ 스레드 두 개를 붙여 경쟁을 노리는 방식은 **결함을 못 잡는다**(2026-07-15 실측: 재시도 로직을
    제거해도 통과했다 — 스케줄링상 경쟁이 아예 안 일어나 재시도 경로를 안 밟는다). 그래서
    타이밍에 기대지 않고, max() 읽은 뒤 INSERT 커밋 전에 다른 커넥션이 끼어들도록 훅으로
    결정론적으로 재현한다.
    """
    from sqlalchemy.orm import sessionmaker

    from app import models

    orig_init = models.RecommendationVersion.__init__
    stolen = {}

    def steal_version_once(self, **kw):
        """max() 읽은 뒤 INSERT 커밋 전에 끼어들어, 다른 커넥션이 그 version을 선점하게 한다."""
        orig_init(self, **kw)
        if stolen:  # 아래 끼어들기가 만드는 행에는 재귀하지 않는다
            return
        stolen["version"] = kw["version"]  # _save_recommendation이 방금 계산한 version
        with sessionmaker(bind=integration_engine)() as other:
            other.add(models.RecommendationVersion(
                session_id=kw["session_id"], analysis_id=kw["analysis_id"],
                version=kw["version"], parent_version=kw.get("parent_version"),
                source="chat", payload=_rec(), change_summary="다른 커넥션이 선점"))
            other.commit()  # 이 시점부터 원래 INSERT는 실제 제약에 걸린다

    monkeypatch.setattr(models.RecommendationVersion, "__init__", steal_version_once)

    out = _save_recommendation(seeded["session_id"], seeded["analysis_id"], _rec(),
                               source="drag", parent_version=None)

    assert stolen["version"] == 2, "선점이 일어나지 않았다 — 이 테스트는 무의미해진다"
    assert out["version"] == 3, "충돌 후 version을 재계산해 다음 번호를 받아야 한다"

    rows = db_session.execute(text(
        "select version, source from recommendations where session_id = :s order by version"),
        {"s": seeded["session_id"]}).all()
    assert [(r.version, r.source) for r in rows] == [
        (1, "chat"), (2, "chat"), (3, "drag")]  # 선점행 보존 + 내 편집이 v3로


def test_edit_endpoint_preserves_frontend_ui_metadata(seeded, db_session, as_owner):
    """프론트가 붙이는 UI 메타(x/y/collapsed)가 편집 저장 왕복에서 보존된다.

    `save_edited_recommendation`은 검증한 모델이 아니라 **원본 dict를 그대로 저장**한다:
    `Recommendation.model_validate(...)`는 게이트로만 쓰고 결과를 버린 뒤 `payload.recommendation`을
    넘긴다. Pydantic이 extra="ignore"라 model_validate 결과를 저장하면 UI 필드가 **소리 없이**
    날아간다. 이 설계는 의도적이지만 **아무도 검증하지 않고 있었다**.

    ⚠️ `_save_recommendation`을 직접 부르면 안 된다 — 그럼 JSONB가 준 걸 그대로 담는다는
    당연한 사실만 확인할 뿐, 진짜 회귀 지점(엔드포인트가 검증본을 저장하도록 바뀌는 것)을
    놓친다. 그래서 HTTP로 태운다. 프론트 드래그 편집에서 좌표가 유실되면 여기가 원인이다.
    """
    tree = _rec()
    tree["steps"][0]["x"] = 120
    tree["steps"][0]["y"] = 40
    tree["steps"][0]["collapsed"] = False
    tree["steps"][0]["actions"][0]["_uiKey"] = "node-abc"

    with TestClient(app) as c:
        r = c.post(f"/api/sessions/{seeded['session_id']}/recommendations",
                   json={"recommendation": tree, "source": "drag"})
    assert r.status_code == 201, r.text

    stored = db_session.execute(text(
        "select payload from recommendations where session_id = :s and version = 2"),
        {"s": seeded["session_id"]}).scalar()
    step = stored["steps"][0]
    assert (step["x"], step["y"], step["collapsed"]) == (120, 40, False)
    assert step["actions"][0]["_uiKey"] == "node-abc"


def test_deleting_session_cascades_recommendations(seeded, db_session):
    """세션 삭제 시 추천안이 FK CASCADE로 함께 지워진다 — DB가 하는 일이라 mock은 못 본다."""
    sid = seeded["session_id"]
    assert db_session.execute(text(
        "select count(*) from recommendations where session_id = :s"), {"s": sid}).scalar() == 1

    db_session.execute(text("delete from analysis_sessions where id = :s"), {"s": sid})
    db_session.commit()

    assert db_session.execute(text(
        "select count(*) from recommendations where session_id = :s"), {"s": sid}).scalar() == 0


def test_http_edit_endpoint_persists_end_to_end(seeded, db_session, as_owner):
    """HTTP 경로 전체(라우트→소유권→검증→저장)가 실 DB에 남는다."""
    with TestClient(app) as c:
        r = c.post(f"/api/sessions/{seeded['session_id']}/recommendations",
                   json={"recommendation": _rec(3), "source": "drag",
                         "change_summary": "블록 3개로"})
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 2

    n_actions = db_session.execute(text(
        "select jsonb_array_length(payload->'steps'->0->'actions') "
        "from recommendations where session_id = :s and version = 2"),
        {"s": seeded["session_id"]}).scalar()
    assert n_actions == 3
