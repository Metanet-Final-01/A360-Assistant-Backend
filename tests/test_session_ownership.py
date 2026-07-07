"""세션 소유권 검사 테스트 (RPA-40).

핵심 규칙: 소유자가 있는 세션은 본인만 접근(403 otherwise), 익명 세션(user_id NULL)은
누구나 접근(하위호환). analyze·문서 조회 라우트에 적용된다.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.sessions as sessions_api
from app.api.auth import assert_session_owner
from app.db import get_db
from app.main import app

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()
SESSION_ID = uuid.uuid4()
DOC_ID = uuid.uuid4()


# --- 단위: assert_session_owner ---

def test_anonymous_session_allows_anyone():
    anon = SimpleNamespace(user_id=None)
    assert_session_owner(anon, None)  # 예외 없음
    assert_session_owner(anon, SimpleNamespace(id=USER_A))  # 예외 없음


def test_owned_session_allows_owner():
    owned = SimpleNamespace(user_id=USER_A)
    assert_session_owner(owned, SimpleNamespace(id=USER_A))  # 예외 없음


def test_owned_session_blocks_other_user():
    owned = SimpleNamespace(user_id=USER_A)
    with pytest.raises(HTTPException) as exc:
        assert_session_owner(owned, SimpleNamespace(id=USER_B))
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "FORBIDDEN"


def test_owned_session_blocks_anonymous():
    owned = SimpleNamespace(user_id=USER_A)
    with pytest.raises(HTTPException) as exc:
        assert_session_owner(owned, None)
    assert exc.value.status_code == 403


# --- 라우트: analyze가 소유권을 강제하는가 ---

class FakeDB:
    def __init__(self, session):
        self._session = session

    def get(self, model, key):
        return self._session

    def execute(self, stmt):
        doc = SimpleNamespace(id=DOC_ID, status="parsed", parsed_content={"full_text": "x"})
        return SimpleNamespace(scalar_one_or_none=lambda: doc)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


def _override(session, user):
    app.dependency_overrides[get_db] = lambda: FakeDB(session)
    app.dependency_overrides[sessions_api.get_optional_user] = lambda: user


def test_analyze_blocks_non_owner():
    from fastapi.testclient import TestClient

    owned_session = SimpleNamespace(id=SESSION_ID, user_id=USER_A)
    _override(owned_session, SimpleNamespace(id=USER_B))  # 다른 사용자
    with TestClient(app) as c:
        r = c.post(f"/api/sessions/{SESSION_ID}/analyze")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "FORBIDDEN"


def test_analyze_allows_anonymous_session():
    """익명 세션(user_id NULL)은 통과 → 소유권이 아닌 다음 단계(agent 미존재 시 503)로."""
    from fastapi.testclient import TestClient

    anon_session = SimpleNamespace(id=SESSION_ID, user_id=None)
    _override(anon_session, None)
    with TestClient(app) as c:
        r = c.post(f"/api/sessions/{SESSION_ID}/analyze")
    # 소유권은 통과 → 403이 아니어야 한다 (agent 상태에 따라 200/503)
    assert r.status_code != 403


def test_analyze_allows_owner():
    from fastapi.testclient import TestClient

    owned_session = SimpleNamespace(id=SESSION_ID, user_id=USER_A)
    _override(owned_session, SimpleNamespace(id=USER_A))
    with TestClient(app) as c:
        r = c.post(f"/api/sessions/{SESSION_ID}/analyze")
    assert r.status_code != 403  # 소유자 본인은 통과


# --- 라우트: 문서 조회도 세션 소유권을 강제하는가 ---

def test_get_document_blocks_non_owner():
    from fastapi.testclient import TestClient

    import app.api.documents as documents_api
    from app import models

    doc = SimpleNamespace(
        id=DOC_ID, session_id=SESSION_ID, filename="f.pdf", size_bytes=1,
        status="parsed", error=None, parsed_content={"page_count": 1}, created_at=None,
    )
    owned_session = SimpleNamespace(id=SESSION_ID, user_id=USER_A)

    class _DB:
        def get(self, model, key):
            return doc if model is models.Document else owned_session

    app.dependency_overrides[get_db] = lambda: _DB()
    app.dependency_overrides[documents_api.get_optional_user] = lambda: SimpleNamespace(id=USER_B)
    with TestClient(app) as c:
        r = c.get(f"/api/documents/{DOC_ID}")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "FORBIDDEN"
