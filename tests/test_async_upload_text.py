"""비동기 업로드(저장만) + 파싱 SSE + 자연어 입력 테스트 (RPA-43)."""

import io
import json
import uuid
from types import SimpleNamespace

import pytest
from docx import Document as DocxDocument
from fastapi.testclient import TestClient

import app.api.documents as documents_api
from app.db import get_db
from app.main import app


def _make_docx() -> bytes:
    doc = DocxDocument()
    doc.add_paragraph("웹에서 금 시세를 조회한다")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


class FakeSession:
    """세션 조회/추가를 흉내내는 인메모리 DB 페이크."""

    def __init__(self):
        self.docs: dict = {}
        self.sessions: dict = {}
        self.added = []

    def get(self, model, key):
        name = getattr(model, "__name__", "")
        if name == "Document":
            return self.docs.get(key)
        if name == "AnalysisSession":
            return self.sessions.get(key)
        return None

    def add(self, obj):
        # id 부여 + 저장 (flush/commit에서 쓰이도록)
        if not getattr(obj, "id", None):
            obj.id = uuid.uuid4()
        self.added.append(obj)
        cls = type(obj).__name__
        if cls == "AnalysisSession":
            self.sessions[obj.id] = obj
        elif cls == "Document":
            self.docs[obj.id] = obj

    def flush(self):
        pass

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def fake_db():
    db = FakeSession()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[documents_api.get_optional_user] = lambda: None
    yield db
    app.dependency_overrides.clear()


# --- 비동기 업로드: 저장만, status=uploaded ---

def test_upload_returns_uploaded_without_parsing(fake_db, monkeypatch):
    monkeypatch.setattr(documents_api.storage, "save", lambda *a: "stored/path")
    # 파싱이 호출되면 안 된다 (업로드 단계에선 저장만)
    monkeypatch.setattr(
        documents_api, "parse_document", lambda *a: pytest.fail("업로드가 파싱을 호출하면 안 됨")
    )

    with TestClient(app) as c:
        r = c.post("/api/documents", files={"file": ("업무.docx", _make_docx(), "x")})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "uploaded"  # 파싱 전
    assert body["page_count"] is None


def test_upload_rejects_bad_type(fake_db):
    with TestClient(app) as c:
        r = c.post("/api/documents", files={"file": ("x.exe", b"MZ\x00", "x")})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_FILE_TYPE"


# --- 파싱 SSE ---

def test_parse_stream_emits_stage_then_done(fake_db, monkeypatch):
    doc_id = uuid.uuid4()
    sess_id = uuid.uuid4()
    fake_db.sessions[sess_id] = SimpleNamespace(id=sess_id, user_id=None)
    fake_db.docs[doc_id] = SimpleNamespace(
        id=doc_id, session_id=sess_id, filename="업무.docx", size_bytes=1,
        status="uploaded", error=None, parsed_content=None, storage_path="p", created_at=None,
    )
    monkeypatch.setattr(documents_api.storage, "load", lambda p: _make_docx())
    monkeypatch.setattr("app.db.SessionLocal", lambda: fake_db)

    with TestClient(app) as c:
        with c.stream("POST", f"/api/documents/{doc_id}/parse") as r:
            events = [json.loads(l[5:]) for l in r.iter_lines() if l.startswith("data:")]

    assert events[0]["event"] == "stage" and events[0]["stage"] == "parsing"
    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["status"] == "parsed"
    assert fake_db.docs[doc_id].status == "parsed"


def test_parse_stream_failure_marks_failed(fake_db, monkeypatch):
    doc_id = uuid.uuid4()
    sess_id = uuid.uuid4()
    fake_db.sessions[sess_id] = SimpleNamespace(id=sess_id, user_id=None)
    fake_db.docs[doc_id] = SimpleNamespace(
        id=doc_id, session_id=sess_id, filename="깨진.docx", size_bytes=1,
        status="uploaded", error=None, parsed_content=None, storage_path="p", created_at=None,
    )
    monkeypatch.setattr(documents_api.storage, "load", lambda p: b"not a real docx")
    monkeypatch.setattr("app.db.SessionLocal", lambda: fake_db)

    with TestClient(app) as c:
        with c.stream("POST", f"/api/documents/{doc_id}/parse") as r:
            events = [json.loads(l[5:]) for l in r.iter_lines() if l.startswith("data:")]

    assert events[-1]["event"] == "error"
    assert fake_db.docs[doc_id].status == "failed"


# --- 자연어 입력 ---

def test_text_input_creates_parsed_document(fake_db):
    with TestClient(app) as c:
        r = c.post("/api/documents/text", json={"text": "웹에서 금 시세를 긁어 엑셀로 정리해줘"})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "parsed"  # 파싱 불필요 → 바로 분석 가능
    # 저장된 문서의 parsed_content가 자연어 텍스트를 담았는지
    doc = next(o for o in fake_db.added if type(o).__name__ == "Document")
    assert doc.parsed_content["parser"] == "text"
    assert "금 시세" in doc.parsed_content["full_text"]


def test_text_input_rejects_empty(fake_db):
    with TestClient(app) as c:
        r = c.post("/api/documents/text", json={"text": ""})
    assert r.status_code == 422  # min_length=1
