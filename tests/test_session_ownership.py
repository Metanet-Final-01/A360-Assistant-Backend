"""세션 소유권 검사 테스트 (RPA-40).

핵심 규칙: 소유자가 있는 세션은 본인만 접근(403 otherwise), 익명 세션(user_id NULL)은
누구나 접근(하위호환). 세션/문서 조회 라우트에 적용된다.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

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


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


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
