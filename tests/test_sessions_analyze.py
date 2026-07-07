"""분석 SSE 엔드포인트 테스트 (RPA-38).

agent analyze()는 아직 미구현이므로: 실제 상태에선 503 스위치가 동작하고,
모킹 시 SSE(stage→done)와 Analysis 영속화가 배선대로 동작하는지 검증한다.
"""

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.api.sessions as sessions_api
from app.db import get_db
from app.main import app
from app.schemas import AnalysisResult, WorkStep

SESSION_ID = uuid.uuid4()
DOC_ID = uuid.uuid4()


def _parsed_doc():
    return {"parser": "pypdf", "page_count": 1, "pages": [], "full_text": "업무 내용"}


class FakeDB:
    """세션/문서 조회만 흉내내는 페이크. 시나리오별로 session/document를 갈아끼운다."""

    def __init__(self, session=None, document=None):
        self._session = session
        self._document = document

    def get(self, model, key):
        return self._session

    def execute(self, stmt):
        doc = self._document
        return SimpleNamespace(scalar_one_or_none=lambda: doc)


def _client(session=None, document=None):
    app.dependency_overrides[get_db] = lambda: FakeDB(session, document)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides.clear()


def _session_row():
    return SimpleNamespace(id=SESSION_ID)


def _document_row(status="parsed", parsed_content=_parsed_doc()):
    return SimpleNamespace(id=DOC_ID, status=status, parsed_content=parsed_content)


def test_invalid_session_id_returns_400():
    with _client() as c:
        r = c.post("/api/sessions/not-a-uuid/analyze")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_ID"


def test_unknown_session_returns_404():
    with _client(session=None) as c:
        r = c.post(f"/api/sessions/{SESSION_ID}/analyze")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "SESSION_NOT_FOUND"


def test_session_without_document_returns_404():
    with _client(session=_session_row(), document=None) as c:
        r = c.post(f"/api/sessions/{SESSION_ID}/analyze")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "NO_DOCUMENT"


def test_unparsed_document_returns_409():
    with _client(session=_session_row(), document=_document_row(status="failed", parsed_content=None)) as c:
        r = c.post(f"/api/sessions/{SESSION_ID}/analyze")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "NOT_PARSED"


def test_agent_not_landed_returns_503():
    """지금 실제 상태: app.agent가 analyze를 아직 내보내지 않음 → 503 스위치."""
    with _client(session=_session_row(), document=_document_row()) as c:
        r = c.post(f"/api/sessions/{SESSION_ID}/analyze")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "AGENT_UNAVAILABLE"


def _fake_analysis_result():
    return AnalysisResult(
        schema_version="1.0",
        document_title="금 시세 조회",
        summary="요약",
        steps=[
            WorkStep(step_id="step-1", order=1, name="시세 조회", description="웹에서 조회"),
        ],
    )


class _FakePersist:
    """스트림 안 SessionLocal 영속화를 가로채 저장된 행을 검사한다."""

    saved = []

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def add(self, row):
        row.id = uuid.uuid4()
        _FakePersist.saved.append(row)

    def commit(self):
        pass


def test_analyze_streams_stage_then_done_and_persists(monkeypatch):
    from app.core.llm import current_usage_context

    seen_ctx = {}

    def _fake_analyze(parsed):
        # analyze 실행 시점에 usage_context가 살아있는지 (agent 귀속) 함께 검증
        seen_ctx["component"] = current_usage_context().component
        return _fake_analysis_result()

    _FakePersist.saved = []
    monkeypatch.setattr(sessions_api, "_get_agent_analyze", lambda: _fake_analyze)
    monkeypatch.setattr("app.db.SessionLocal", _FakePersist())

    with _client(session=_session_row(), document=_document_row()) as c:
        with c.stream("POST", f"/api/sessions/{SESSION_ID}/analyze") as r:
            assert r.status_code == 200
            events = [
                json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data:")
            ]

    assert events[0]["event"] == "stage" and events[0]["stage"] == "analyzing"
    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["document_title"] == "금 시세 조회"
    assert done["data"]["steps"][0]["step_id"] == "step-1"
    assert done["data"]["analysis_id"]  # 영속화된 행 id가 실림

    assert len(_FakePersist.saved) == 1
    row = _FakePersist.saved[0]
    assert row.status == "completed" and row.result["summary"] == "요약"
    assert seen_ctx["component"] == "agent"  # LLM 사용량이 agent로 귀속


def test_analyze_failure_emits_error_event(monkeypatch):
    def _boom(parsed):
        raise ValueError("LLM 응답 파싱 실패")

    _FakePersist.saved = []
    monkeypatch.setattr(sessions_api, "_get_agent_analyze", lambda: _boom)
    monkeypatch.setattr("app.db.SessionLocal", _FakePersist())

    with _client(session=_session_row(), document=_document_row()) as c:
        with c.stream("POST", f"/api/sessions/{SESSION_ID}/analyze") as r:
            events = [
                json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data:")
            ]

    assert events[-1]["event"] == "error"
    # 실패도 Analysis 행으로 남는다
    assert any(getattr(row, "status", None) == "failed" for row in _FakePersist.saved)


def test_analyze_config_error_maps_to_error_event(monkeypatch):
    def _no_key(parsed):
        raise RuntimeError("OPENAI_API_KEY 환경변수가 필요합니다")

    monkeypatch.setattr(sessions_api, "_get_agent_analyze", lambda: _no_key)

    with _client(session=_session_row(), document=_document_row()) as c:
        with c.stream("POST", f"/api/sessions/{SESSION_ID}/analyze") as r:
            events = [
                json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data:")
            ]

    assert events[-1]["event"] == "error"
    assert "구성 오류" in events[-1]["message"]
