"""에이전트 단일 진입점(/turn) 테스트 (RPA-64).

intent 없이 full context를 조립해 넘기고, 반환 type으로 저장을 분기하는지 검증한다.
에이전트는 lazy import(app.agent.stream_agent_turn)로 붙으므로, 가짜 async 제너레이터를
그 자리에 심어 백엔드 로직만 격리 검증한다.
"""

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
DID = uuid.uuid4()


def _recommendation() -> dict:
    return {
        "schema_version": "1.0",
        "steps": [{"step_id": "step-1", "actions": [
            {"order": 1, "package": "Browser", "action": "openbrowser",
             "label": "열기", "parameters": [], "children": []}]}],
        "variables": [], "notes": "",
    }


def _analysis_result() -> dict:
    return {
        "schema_version": "1.0", "document_title": "t", "summary": "s",
        "steps": [{"step_id": "step-1", "order": 1, "name": "n", "description": "d"}],
        "ambiguities": [],
    }


class FakeDB:
    """세션 조회 + 컨텍스트 조립용 4개 쿼리(chat/analyses/recommendations/documents)를 흉내낸다."""

    def __init__(self, session=None, history=None, analysis=None, rec=None, document=None,
                 compact=None):
        self.session = session
        self.history = history or []
        self.analysis = analysis
        self.rec = rec
        self.document = document
        self.compact = compact

    def get(self, model, key):
        return self.session

    def execute(self, stmt):
        text = str(stmt).lower()
        if "session_compacts" in text:
            return SimpleNamespace(scalar_one_or_none=lambda: self.compact)
        if "chat_messages" in text:
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.history))
        if "from analyses" in text or " analyses" in text:
            return SimpleNamespace(scalar_one_or_none=lambda: self.analysis)
        if "recommendations" in text:
            return SimpleNamespace(scalar_one_or_none=lambda: self.rec)
        if "documents" in text:
            # 실제 쿼리는 status="parsed"만 고른다 — fake도 미파싱 문서는 제외
            doc = self.document
            if doc is not None and getattr(doc, "status", "parsed") != "parsed":
                doc = None
            return SimpleNamespace(scalar_one_or_none=lambda: doc)
        return SimpleNamespace(scalar_one_or_none=lambda: None,
                               scalars=lambda: SimpleNamespace(all=lambda: []))


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


def _override(db, user=None):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[sessions_api.get_optional_user] = lambda: user


def _make_persist(captured, max_version=None):
    class _P:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, stmt): return SimpleNamespace(scalar=lambda: max_version)
        def add(self, row):
            # 실제 DB는 flush 시 id를 채운다 — fake는 저장 후 참조 체이닝 검증을 위해 부여
            if type(row).__name__ == "Analysis" and getattr(row, "id", None) is None:
                row.id = uuid.uuid4()
            captured.setdefault(type(row).__name__, []).append(row)
        def commit(self): pass
    return _P


def _install_agent(monkeypatch, events):
    async def _fake_turn(message, context):
        _fake_turn.seen_context = context  # 컨텍스트 검증용
        for ev in events:
            yield ev
    monkeypatch.setattr("app.agent.stream_agent_turn", _fake_turn, raising=False)
    return _fake_turn


def _run(sid=SID, operation=None):
    body = {"message": "안녕"}
    if operation is not None:
        body["operation"] = operation
    with TestClient(app) as c:
        with c.stream("POST", f"/api/sessions/{sid}/turn", json=body) as r:
            status = r.status_code
            events = [json.loads(l[5:]) for l in r.iter_lines() if l.startswith("data:")]
    return status, events


def _compact_payload() -> dict:
    return {
        "schema_version": "1.0", "task_overview": "업무 개요",
        "decisions": ["엑셀 저장 경로는 D드라이브"], "flow_journal": ["3단계 반복문화"],
        "open_questions": [], "verbatim": [{"kind": "catalog", "content": "요약 금지 원문"}],
    }


# --- type=answer: 대화만 저장 ---

def test_answer_streams_and_persists_chat(monkeypatch):
    from app.schemas import ProgressEvent
    _install_agent(monkeypatch, [
        ProgressEvent(event="token", stage="agent", message="안"),
        ProgressEvent(event="done", data={"type": "answer", "answer": "안녕하세요", "sources": []}),
    ])
    captured = {}
    monkeypatch.setattr("app.db.SessionLocal", _make_persist(captured))

    session = SimpleNamespace(id=SID, user_id=None, solution="a360")
    _override(FakeDB(session=session))
    _, events = _run()

    assert events[0]["event"] == "token"
    done = events[-1]
    assert done["event"] == "done" and done["data"]["type"] == "answer"
    assert done["data"]["answer"] == "안녕하세요"
    # 대화 턴(user+assistant) 저장
    assert len(captured.get("ChatMessage", [])) == 2


# --- type=recommendation: source="chat" 새 버전 저장 ---

def test_recommendation_saves_chat_version(monkeypatch):
    from app.schemas import ProgressEvent
    _install_agent(monkeypatch, [
        ProgressEvent(event="stage", stage="agent", message="수정 중"),
        ProgressEvent(event="done", data={
            "type": "recommendation", "answer": "3단계를 반복문으로 바꿨어요", "sources": [],
            "updated_recommendation": _recommendation(), "change_summary": "3단계 반복문화"}),
    ])
    captured = {}
    monkeypatch.setattr("app.db.SessionLocal", _make_persist(captured, max_version=1))  # 현재 v1 → v2

    session = SimpleNamespace(id=SID, user_id=None, solution="a360")
    rec = SimpleNamespace(payload=_recommendation(), analysis_id=AID, version=1)
    _override(FakeDB(session=session, rec=rec))
    _, events = _run()

    done = events[-1]
    assert done["data"]["type"] == "recommendation"
    assert done["data"]["version"] == 2 and done["data"]["source"] == "chat"
    assert done["data"]["change_summary"] == "3단계 반복문화"
    assert done["data"]["recommendation"]["steps"][0]["step_id"] == "step-1"
    saved = captured["RecommendationVersion"][0]
    assert saved.source == "chat" and saved.analysis_id == AID
    # 추천을 만든 assistant 메시지에 버전 번호 기록 (CodeRabbit)
    assistant_msg = [m for m in captured["ChatMessage"] if m.role == "assistant"][0]
    assert assistant_msg.recommendation_version == 2


# --- type=analysis: Analysis 저장 ---

def test_analysis_saves_analysis_row(monkeypatch):
    from app.schemas import ProgressEvent
    _install_agent(monkeypatch, [
        ProgressEvent(event="done", data={
            "type": "analysis", "answer": "분석했어요", "sources": [],
            "analysis_result": _analysis_result()}),
    ])
    captured = {}
    monkeypatch.setattr("app.db.SessionLocal", _make_persist(captured))

    session = SimpleNamespace(id=SID, user_id=None, solution="a360")
    document = SimpleNamespace(id=DID, parsed_content={"pages": []})
    _override(FakeDB(session=session, document=document))
    _, events = _run()

    done = events[-1]
    assert done["data"]["type"] == "analysis"
    assert done["data"]["analysis_result"]["document_title"] == "t"
    saved = captured["Analysis"][0]
    assert saved.status == "completed" and saved.document_id == DID


# --- type=recommendation + analysis_result 동봉: 둘 다 저장, 흐름도는 새 분석에 귀속 ---

def test_recommendation_with_analysis_saves_both_and_chains(monkeypatch):
    """분석 없이 흐름도 턴 — 분석 선행 산출본도 함께 저장하고, 흐름도를 그 분석에 귀속한다 (조정1)."""
    from app.schemas import ProgressEvent
    _install_agent(monkeypatch, [
        ProgressEvent(event="done", data={
            "type": "recommendation", "answer": "분석하고 흐름도까지 만들었어요", "sources": [],
            "analysis_result": _analysis_result(),        # 선행 분석도 동봉
            "updated_recommendation": _recommendation(),
            "change_summary": "신규 생성"}),
    ])
    captured = {}
    monkeypatch.setattr("app.db.SessionLocal", _make_persist(captured, max_version=None))  # 첫 버전 → v1

    session = SimpleNamespace(id=SID, user_id=None, solution="a360")
    document = SimpleNamespace(id=DID, parsed_content={"pages": []})
    _override(FakeDB(session=session, document=document))  # 기존 분석·추천 없음
    _, events = _run()

    done = events[-1]
    assert done["data"]["type"] == "recommendation"
    # 분석·추천 둘 다 저장
    analysis_row = captured["Analysis"][0]
    rec_row = captured["RecommendationVersion"][0]
    assert analysis_row.status == "completed" and analysis_row.document_id == DID
    # 흐름도가 이번 턴에 새로 저장한 분석 id에 귀속됐는지 (참조 무결성)
    assert rec_row.analysis_id == analysis_row.id
    assert rec_row.source == "chat" and rec_row.version == 1


# --- 컨텍스트 조립: full context를 solution과 함께 넘긴다 ---

def test_full_context_passed_to_agent(monkeypatch):
    from app.schemas import ProgressEvent
    fake = _install_agent(monkeypatch, [
        ProgressEvent(event="done", data={"type": "answer", "answer": "ok", "sources": []}),
    ])
    monkeypatch.setattr("app.db.SessionLocal", _make_persist({}))

    session = SimpleNamespace(id=SID, user_id=None, solution="uipath")
    # 쿼리는 created_at ASC(시간순)로 온다 (상한 없이 전체) → fake도 시간순으로
    history = [SimpleNamespace(role="user", content="이전"), SimpleNamespace(role="assistant", content="답")]
    rec = SimpleNamespace(payload=_recommendation(), analysis_id=AID, version=1)
    analysis = SimpleNamespace(result=_analysis_result(), id=AID)
    document = SimpleNamespace(id=DID, parsed_content={"pages": [1]})
    _override(FakeDB(session=session, history=history, analysis=analysis, rec=rec, document=document))
    _run()

    ctx = fake.seen_context
    assert ctx["solution"] == "uipath"  # 세션 solution 전달
    assert ctx["history"] == [{"role": "user", "content": "이전"}, {"role": "assistant", "content": "답"}]
    assert ctx["analysis"]["document_title"] == "t"
    assert ctx["recommendation"]["steps"][0]["step_id"] == "step-1"
    assert ctx["parsed_doc"] == {"pages": [1]}
    assert "intent" not in ctx  # intent는 넘기지 않는다


# --- compact: 압축본 저장, 이력엔 안 남김, operation 신호·주입 (RPA-66) ---

def test_compact_saves_and_skips_chat(monkeypatch):
    from app.schemas import ProgressEvent
    _install_agent(monkeypatch, [
        ProgressEvent(event="done", data={
            "type": "compact", "answer": "이전 대화를 요약했어요", "sources": [],
            "compact": _compact_payload()}),
    ])
    captured = {}
    monkeypatch.setattr("app.db.SessionLocal", _make_persist(captured))
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))
    _, events = _run(operation="compact")

    done = events[-1]
    assert done["data"]["type"] == "compact"
    assert done["data"]["compact"]["decisions"] == ["엑셀 저장 경로는 D드라이브"]
    assert len(captured.get("SessionCompact", [])) == 1        # 압축본 저장
    assert "ChatMessage" not in captured                       # 압축 턴은 이력에 안 남긴다


def test_operation_compact_passed_to_agent(monkeypatch):
    from app.schemas import ProgressEvent
    fake = _install_agent(monkeypatch, [
        ProgressEvent(event="done", data={
            "type": "compact", "answer": "ok", "sources": [], "compact": _compact_payload()}),
    ])
    monkeypatch.setattr("app.db.SessionLocal", _make_persist({}))
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))
    _run(operation="compact")
    assert fake.seen_context["operation"] == "compact"


def test_latest_compact_injected_into_context(monkeypatch):
    from app.schemas import ProgressEvent
    fake = _install_agent(monkeypatch, [
        ProgressEvent(event="done", data={"type": "answer", "answer": "ok", "sources": []}),
    ])
    monkeypatch.setattr("app.db.SessionLocal", _make_persist({}))
    from datetime import datetime, timezone
    cp = SimpleNamespace(payload=_compact_payload(), created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360"), compact=cp))
    _run()  # operation 기본 "chat"
    assert fake.seen_context["operation"] == "chat"
    assert fake.seen_context["compact"]["task_overview"] == "업무 개요"  # 최신 압축본 주입


def test_compact_type_without_payload_errors(monkeypatch):
    from app.schemas import ProgressEvent
    _install_agent(monkeypatch, [
        ProgressEvent(event="done", data={"type": "compact", "answer": "x", "sources": []}),
    ])
    monkeypatch.setattr("app.db.SessionLocal", _make_persist({}))
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))
    _, events = _run(operation="compact")
    assert events[-1]["event"] == "error"


# --- 계약 위반은 성공 done이 아니라 error로 (CodeRabbit) ---

def test_unknown_type_errors(monkeypatch):
    from app.schemas import ProgressEvent
    _install_agent(monkeypatch, [
        ProgressEvent(event="done", data={"type": "bogus", "answer": "x", "sources": []}),
    ])
    monkeypatch.setattr("app.db.SessionLocal", _make_persist({}))
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))
    _, events = _run()
    assert events[-1]["event"] == "error"


def test_analysis_type_without_result_errors(monkeypatch):
    from app.schemas import ProgressEvent
    _install_agent(monkeypatch, [
        ProgressEvent(event="done", data={"type": "analysis", "answer": "x", "sources": []}),
    ])
    monkeypatch.setattr("app.db.SessionLocal", _make_persist({}))
    document = SimpleNamespace(id=DID, status="parsed", parsed_content={})
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360"), document=document))
    _, events = _run()
    assert events[-1]["event"] == "error"


def test_unparsed_document_not_used_for_analysis(monkeypatch):
    """최신 문서가 uploaded면 파싱본이 없어 분석 저장 불가 → error (미파싱 문서 배제, CodeRabbit)."""
    from app.schemas import ProgressEvent
    _install_agent(monkeypatch, [
        ProgressEvent(event="done", data={
            "type": "analysis", "answer": "분석", "sources": [], "analysis_result": _analysis_result()}),
    ])
    monkeypatch.setattr("app.db.SessionLocal", _make_persist({}))
    document = SimpleNamespace(id=DID, status="uploaded", parsed_content=None)  # 미파싱
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360"), document=document))
    _, events = _run()
    assert events[-1]["event"] == "error"


# --- 에이전트 미구현 시 503 ---

def test_503_when_agent_absent(monkeypatch):
    import app.agent as agent_pkg
    monkeypatch.delattr(agent_pkg, "stream_agent_turn", raising=False)
    session = SimpleNamespace(id=SID, user_id=None, solution="a360")
    _override(FakeDB(session=session))
    with TestClient(app) as c:
        with c.stream("POST", f"/api/sessions/{SID}/turn", json={"message": "안녕"}) as r:
            assert r.status_code == 503


# --- 에이전트 error를 백엔드가 중복해서 내지 않는다 ---

def test_error_forwarded_without_duplication(monkeypatch):
    from app.schemas import ProgressEvent
    _install_agent(monkeypatch, [
        ProgressEvent(event="stage", stage="agent", message="시작"),
        ProgressEvent(event="error", stage="agent", message="내부 실패"),
    ])
    monkeypatch.setattr("app.db.SessionLocal", _make_persist({}))
    session = SimpleNamespace(id=SID, user_id=None, solution="a360")
    _override(FakeDB(session=session))
    _, events = _run()

    assert [e["event"] for e in events].count("error") == 1
    assert events[-1]["event"] == "error"


# --- 소유권: 남의 세션 차단 ---

def test_blocks_non_owner(monkeypatch):
    session = SimpleNamespace(id=SID, user_id=uuid.uuid4(), solution="a360")
    _override(FakeDB(session=session), user=SimpleNamespace(id=uuid.uuid4()))
    with TestClient(app) as c:
        with c.stream("POST", f"/api/sessions/{SID}/turn", json={"message": "안녕"}) as r:
            assert r.status_code == 403
