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
        _fake_turn.calls += 1
        for ev in events:
            yield ev
    _fake_turn.calls = 0
    monkeypatch.setattr("app.agent.stream_agent_turn", _fake_turn, raising=False)
    return _fake_turn


def _install_agent_seq(monkeypatch, call_events):
    """호출마다 다른 이벤트 시퀀스를 yield — 자동 compact는 stream_turn을 2번 부른다."""
    seq = iter(call_events)

    async def _fake(message, context):
        _fake.calls += 1
        for ev in next(seq):
            yield ev
    _fake.calls = 0
    monkeypatch.setattr("app.agent.stream_agent_turn", _fake, raising=False)
    return _fake


def _run(sid=SID, operation=None, message="안녕"):
    body = {"message": message}
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


def test_compact_malformed_payload_errors(monkeypatch):
    """빈/섹션 누락 compact는 저장 경계 검증에 걸려 error (CodeRabbit) — session_compacts에 안 샌다."""
    from app.schemas import ProgressEvent
    captured = {}
    _install_agent(monkeypatch, [
        ProgressEvent(event="done", data={
            "type": "compact", "answer": "x", "sources": [],
            "compact": {"task_overview": "개요"}}),  # decisions/flow_journal/... 누락
    ])
    monkeypatch.setattr("app.db.SessionLocal", _make_persist(captured))
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))
    _, events = _run(operation="compact")
    assert events[-1]["event"] == "error"
    assert "SessionCompact" not in captured  # 저장 안 됨


def test_compact_bad_verbatim_shape_errors(monkeypatch):
    """verbatim 항목이 {kind, content} 형태가 아니면 error (유실-critical 섹션 보강, CodeRabbit)."""
    from app.schemas import ProgressEvent
    captured = {}
    bad = _compact_payload()
    bad["verbatim"] = ["원문 문자열"]  # dict 아님 — 형태 위반
    _install_agent(monkeypatch, [
        ProgressEvent(event="done", data={
            "type": "compact", "answer": "x", "sources": [], "compact": bad}),
    ])
    monkeypatch.setattr("app.db.SessionLocal", _make_persist(captured))
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))
    _, events = _run(operation="compact")
    assert events[-1]["event"] == "error"
    assert "SessionCompact" not in captured


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


# --- 사용량 게이지 (RPA-83) ---

def test_answer_includes_usage_gauge(monkeypatch):
    from app.schemas import ProgressEvent
    _install_agent(monkeypatch, [
        ProgressEvent(event="done", data={"type": "answer", "answer": "hi", "sources": []}),
    ])
    monkeypatch.setattr("app.db.SessionLocal", _make_persist({}))
    monkeypatch.setattr(sessions_api, "_read_intake_gauge",
                        lambda sid: {"intake_tokens": 8000, "limit_tokens": 100000,
                                     "ratio": 0.08, "compact_recommended": False})
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))
    _, events = _run()
    assert events[-1]["data"]["usage_gauge"]["intake_tokens"] == 8000


def test_compact_turn_skips_gauge(monkeypatch):
    """compact 턴은 intake가 없어 게이지 조회 자체를 안 한다."""
    from app.schemas import ProgressEvent
    _install_agent(monkeypatch, [
        ProgressEvent(event="done", data={
            "type": "compact", "answer": "요약", "sources": [], "compact": _compact_payload()}),
    ])
    monkeypatch.setattr("app.db.SessionLocal", _make_persist({}))
    called = {"n": 0}
    monkeypatch.setattr(sessions_api, "_read_intake_gauge",
                        lambda sid: called.__setitem__("n", called["n"] + 1))
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))
    _, events = _run(operation="compact")
    assert "usage_gauge" not in events[-1]["data"]
    assert called["n"] == 0


def test_read_intake_gauge_ratio_and_recommend(monkeypatch):
    class _S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, stmt): return SimpleNamespace(scalar_one_or_none=lambda: 75000)

    monkeypatch.setattr("app.db.SessionLocal", _S)
    monkeypatch.setenv("TURN_GAUGE_LIMIT_TOKENS", "100000")
    g = sessions_api._read_intake_gauge(SID)
    assert g["intake_tokens"] == 75000 and g["ratio"] == 0.75
    assert g["compact_recommended"] is True  # 0.75 >= 0.7


def test_read_intake_gauge_invalid_limit_falls_back(monkeypatch):
    """비정상 env(non-numeric)는 기본값으로 폴백 — 게이지가 꺼지거나 크래시하지 않는다 (CodeRabbit)."""
    class _S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, stmt): return SimpleNamespace(scalar_one_or_none=lambda: 50000)

    monkeypatch.setattr("app.db.SessionLocal", _S)
    for bad in ("abc", "0", "-5", ""):
        monkeypatch.setenv("TURN_GAUGE_LIMIT_TOKENS", bad)
        g = sessions_api._read_intake_gauge(SID)
        assert g["limit_tokens"] == 100000 and g["ratio"] == 0.5  # 폴백값 사용


def test_read_intake_gauge_none_when_no_intake(monkeypatch):
    class _S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, stmt): return SimpleNamespace(scalar_one_or_none=lambda: None)

    monkeypatch.setattr("app.db.SessionLocal", _S)
    assert sessions_api._read_intake_gauge(SID) is None


# --- 하드 자동 compact (RPA-84) ---

def test_auto_compact_triggers_over_hard(monkeypatch):
    """대화 누적이 하드 임계를 넘으면 실제 턴 전에 자동 compact가 먼저 돈다."""
    from app.schemas import ProgressEvent
    fake = _install_agent_seq(monkeypatch, [
        [ProgressEvent(event="done", data={"type": "compact", "answer": "요약",
                                           "sources": [], "compact": _compact_payload()})],
        [ProgressEvent(event="done", data={"type": "answer", "answer": "실제 답", "sources": []})],
    ])
    captured = {}
    monkeypatch.setattr("app.db.SessionLocal", _make_persist(captured))
    monkeypatch.setattr(sessions_api, "_read_intake_gauge",
                        lambda sid: {"intake_tokens": 120000, "limit_tokens": 100000, "ratio": 1.2,
                                     "compact_recommended": True, "compact_required": True})
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))
    _, events = _run()  # operation 기본 "chat"

    assert fake.calls == 2  # 자동 compact + 실제 턴
    assert len(captured.get("SessionCompact", [])) == 1  # 압축본 저장됨
    assert any(e["event"] == "stage" and "자동 압축" in e.get("message", "") for e in events)
    assert events[-1]["data"]["answer"] == "실제 답"  # 원래 턴이 이어짐


def test_no_auto_compact_under_hard(monkeypatch):
    """임계 미만이면 자동 compact 없이 실제 턴만 돈다 (1회 호출)."""
    from app.schemas import ProgressEvent
    fake = _install_agent_seq(monkeypatch, [
        [ProgressEvent(event="done", data={"type": "answer", "answer": "답", "sources": []})],
    ])
    monkeypatch.setattr("app.db.SessionLocal", _make_persist({}))
    monkeypatch.setattr(sessions_api, "_read_intake_gauge",
                        lambda sid: {"intake_tokens": 10000, "limit_tokens": 100000, "ratio": 0.1,
                                     "compact_recommended": False, "compact_required": False})
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))
    _, events = _run()

    assert fake.calls == 1  # 자동 compact 없음
    assert not any(e["event"] == "stage" and "자동 압축" in e.get("message", "") for e in events)
    assert events[-1]["data"]["answer"] == "답"


# --- 선행(look-ahead) 가드 (RPA-86) ---

def test_lookahead_compact_triggers_on_huge_input(monkeypatch):
    """게이지 0.99여도 이번 입력이 거대하면 예상 비율이 임계를 넘어 선행 compact가 돈다."""
    from app.schemas import ProgressEvent
    fake = _install_agent_seq(monkeypatch, [
        [ProgressEvent(event="done", data={"type": "compact", "answer": "요약",
                                           "sources": [], "compact": _compact_payload()})],
        [ProgressEvent(event="done", data={"type": "answer", "answer": "실제 답", "sources": []})],
    ])
    captured = {}
    monkeypatch.setattr("app.db.SessionLocal", _make_persist(captured))
    # 직전 intake 기준 게이지는 0.995(하드 미만) — compact_required=False. 이번 입력 토큰만 넘으면 됨
    monkeypatch.setattr(sessions_api, "_read_intake_gauge",
                        lambda sid: {"intake_tokens": 99500, "limit_tokens": 100000, "ratio": 0.995,
                                     "compact_recommended": True, "compact_required": False})
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))
    # message max_length=4000. 3000자 → 폴백 추정 len//2=1500 ≥ 필요치 500 (tiktoken이면 더 큼)
    huge = "업무 자동화 요청 " * 300  # ≈3000자
    _, events = _run(message=huge)

    assert fake.calls == 2  # 선행 compact + 실제 턴
    assert len(captured.get("SessionCompact", [])) == 1
    assert any(e["event"] == "stage" and "자동 압축" in e.get("message", "") for e in events)
    assert events[-1]["data"]["answer"] == "실제 답"


def test_lookahead_no_compact_on_small_input(monkeypatch):
    """게이지 0.99여도 이번 입력이 작으면 예상 비율이 임계 미만 → compact 없이 실제 턴만."""
    from app.schemas import ProgressEvent
    fake = _install_agent_seq(monkeypatch, [
        [ProgressEvent(event="done", data={"type": "answer", "answer": "답", "sources": []})],
    ])
    monkeypatch.setattr("app.db.SessionLocal", _make_persist({}))
    monkeypatch.setattr(sessions_api, "_read_intake_gauge",
                        lambda sid: {"intake_tokens": 99000, "limit_tokens": 100000, "ratio": 0.99,
                                     "compact_recommended": True, "compact_required": False})
    _override(FakeDB(session=SimpleNamespace(id=SID, user_id=None, solution="a360")))
    _, events = _run(message="짧은 질문")  # 몇 토큰 → 99000+n < 100000

    assert fake.calls == 1  # 자동 compact 없음
    assert events[-1]["data"]["answer"] == "답"


def test_estimate_message_tokens_fallback():
    """빈 입력은 0, 비어있지 않으면 양수. (tiktoken 유무와 무관하게 성립)"""
    assert sessions_api._estimate_message_tokens("") == 0
    assert sessions_api._estimate_message_tokens("업무 자동화 요청입니다") > 0


def test_estimate_uses_char_fallback_before_warmup(monkeypatch):
    """인코더 미준비(워밍업 전/실패)면 요청 경로에서 로드하지 않고 문자 폴백(len//2)."""
    monkeypatch.setattr(sessions_api, "_TOKEN_ENCODER", None)
    assert sessions_api._estimate_message_tokens("가" * 100) == 50


def test_warmup_failure_is_not_sticky(monkeypatch):
    """워밍업 실패가 영구 고착되지 않는다 — 재워밍업이 성공하면 인코더를 쓴다 (CodeRabbit #134)."""
    import builtins

    real_import = builtins.__import__

    def _no_tiktoken(name, *a, **kw):
        if name == "tiktoken":
            raise ImportError("offline")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(sessions_api, "_TOKEN_ENCODER", None)
    monkeypatch.setattr(builtins, "__import__", _no_tiktoken)
    sessions_api.warmup_token_encoder()  # 실패 → None 유지, 예외 없음
    assert sessions_api._token_encoder() is None

    monkeypatch.setattr(builtins, "__import__", real_import)
    sessions_api.warmup_token_encoder()  # 재시도 성공 → 인코더 준비
    enc = sessions_api._token_encoder()
    if enc is not None:  # 오프라인 CI면 BPE 다운로드가 실패할 수 있어 조건부 검증
        assert sessions_api._estimate_message_tokens("hello world") > 0


def test_gauge_includes_compact_required(monkeypatch):
    class _S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, stmt): return SimpleNamespace(scalar_one_or_none=lambda: 130000)

    monkeypatch.setattr("app.db.SessionLocal", _S)
    monkeypatch.setenv("TURN_GAUGE_LIMIT_TOKENS", "100000")
    monkeypatch.setenv("TURN_GAUGE_HARD_RATIO", "1.0")
    g = sessions_api._read_intake_gauge(SID)
    assert g["compact_required"] is True and g["compact_recommended"] is True  # 1.3 >= 1.0


# --- 소유권: 남의 세션 차단 ---

def test_blocks_non_owner(monkeypatch):
    session = SimpleNamespace(id=SID, user_id=uuid.uuid4(), solution="a360")
    _override(FakeDB(session=session), user=SimpleNamespace(id=uuid.uuid4()))
    with TestClient(app) as c:
        with c.stream("POST", f"/api/sessions/{SID}/turn", json={"message": "안녕"}) as r:
            assert r.status_code == 403
