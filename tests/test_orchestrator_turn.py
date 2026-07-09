"""stream_agent_turn 엔드투엔드 테스트 (RPA-65) — LLM·RAG 없이 그래프 배선을 구동한다.

핵심 검증: 반환 type이 실행된 브랜치와 항상 일치한다(백엔드 RPA-64 저장 분기 요건).
qa/edit의 ChatOpenAI는 _make_llm 몽키패치로, 구조화 호출(intake/compact/기타)은
각 모듈의 chat_json 몽키패치로 대체한다.
"""

import asyncio
import json

from app.agent.orchestrator import compact as compact_mod
from app.agent.orchestrator import edit as edit_mod
from app.agent.orchestrator import generate as generate_mod
from app.agent.orchestrator import intake as intake_mod
from app.agent.orchestrator import qa as qa_mod
from app.agent.orchestrator.compact import CompactContext
from app.agent.orchestrator.graph import stream_agent_turn
from app.agent.orchestrator.intake import IntakeOutput
from app.schemas import AnalysisResult

_CTX = {"solution": "a360", "history": [], "analysis": None,
        "recommendation": None, "parsed_doc": None}

_CLEAN_FLOW = {
    "schema_version": "1.0",
    "steps": [{
        "step_id": "step-1",
        "actions": [{
            "order": 1, "package": "Excel_MS", "action": "SaveSpreadSheet", "label": "저장",
            "parameters": [{"name": "session", "value": "Default", "value_source": "schema_default"}],
            "children": [],
        }],
    }],
    "variables": [], "notes": None,
}

_ANALYSIS = AnalysisResult(
    summary="테스트 업무",
    steps=[{"step_id": "step-1", "order": 1, "name": "저장", "description": "엑셀 저장"}],
)


def _collect(message, context):
    async def run():
        return [e async for e in stream_agent_turn(message, context)]

    return asyncio.run(run())


def _done(events):
    dones = [e for e in events if e.event == "done"]
    assert len(dones) == 1, f"done 이벤트는 정확히 1개여야 함: {[e.event for e in events]}"
    return dones[0].data


# ── 스트리밍/도구 스텁 ───────────────────────────────────────────────────────

class _Chunk:
    """AIMessageChunk 대역: text 누적과 tool_calls 인터페이스만 흉내낸다."""

    def __init__(self, text="", tool_calls=None):
        self.text = text
        self.tool_calls = tool_calls or []

    def __add__(self, other):
        return _Chunk(self.text + other.text, self.tool_calls + other.tool_calls)


class _FakeStreamLLM:
    """qa용: 지정된 라운드 시나리오대로 astream 청크를 흘린다."""

    def __init__(self, rounds):
        self._rounds = list(rounds)  # 각 라운드 = _Chunk 리스트

    def bind_tools(self, tools):
        return self

    async def astream(self, messages):
        for chunk in self._rounds.pop(0):
            yield chunk


class _FakeInvokeLLM:
    """edit용: ainvoke가 순서대로 응답을 돌려준다."""

    def __init__(self, responses):
        self._responses = list(responses)

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        return self._responses.pop(0)


def _set_api_key(monkeypatch):
    from app.agent import config
    from app.agent.orchestrator import graph as graph_mod

    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(graph_mod.config, "OPENAI_API_KEY", "test-key", raising=False)


def _route(monkeypatch, route):
    monkeypatch.setattr(intake_mod, "chat_json",
                        lambda *a, **k: IntakeOutput(route=route, reason="test"))


# ── 브랜치별 type 일치 ───────────────────────────────────────────────────────

def test_qa_turn_streams_tokens_and_returns_answer_type(monkeypatch):
    _set_api_key(monkeypatch)
    _route(monkeypatch, "qa")
    monkeypatch.setattr(
        qa_mod, "_make_llm",
        lambda: _FakeStreamLLM([[_Chunk("안녕"), _Chunk("하세요")]]),
    )
    events = _collect("안녕", dict(_CTX))
    data = _done(events)
    assert data["type"] == "answer"
    assert data["answer"] == "안녕하세요"
    assert data["updated_recommendation"] is None
    tokens = [e.message for e in events if e.event == "token"]
    assert tokens == ["안녕", "하세요"]


def test_analyze_turn_returns_analysis_type(monkeypatch):
    _set_api_key(monkeypatch)
    _route(monkeypatch, "analyze")
    monkeypatch.setattr(generate_mod, "analyze_text", lambda text: _ANALYSIS)
    data = _done(_collect("이 업무 분석해줘: 엑셀 저장", dict(_CTX)))
    assert data["type"] == "analysis"
    assert data["analysis_result"]["steps"][0]["step_id"] == "step-1"
    assert data["updated_recommendation"] is None


def test_generate_without_analysis_runs_analyze_first_and_returns_both(monkeypatch):
    """분석본 없는 generate: analyze 경유 → type은 recommendation, 분석본도 함께 반환."""
    _set_api_key(monkeypatch)
    _route(monkeypatch, "generate")
    monkeypatch.setattr(generate_mod, "analyze_text", lambda text: _ANALYSIS)

    class _FakeRecommendGraph:
        async def astream(self, inputs, **kwargs):
            assert inputs["analysis"]["steps"], "analyze 산출물이 주입돼야 함"
            yield ("values", {"recommendation": _CLEAN_FLOW})

    monkeypatch.setattr(generate_mod, "get_recommend_graph", lambda: _FakeRecommendGraph())
    data = _done(_collect("흐름도 만들어줘", dict(_CTX)))
    assert data["type"] == "recommendation"
    assert data["updated_recommendation"]["steps"][0]["step_id"] == "step-1"
    assert data["analysis_result"] is not None  # 조정 요청 1: non-null 산출물 동시 반환
    assert data["violations"] == []


def test_generate_with_existing_analysis_skips_analyze(monkeypatch):
    _set_api_key(monkeypatch)
    _route(monkeypatch, "generate")

    def boom(text):
        raise AssertionError("분석본이 있으면 analyze를 타면 안 됨")

    monkeypatch.setattr(generate_mod, "analyze_text", boom)

    class _FakeRecommendGraph:
        async def astream(self, inputs, **kwargs):
            yield ("values", {"recommendation": _CLEAN_FLOW})

    monkeypatch.setattr(generate_mod, "get_recommend_graph", lambda: _FakeRecommendGraph())
    ctx = dict(_CTX, analysis=_ANALYSIS.model_dump())
    data = _done(_collect("흐름도 만들어줘", ctx))
    assert data["type"] == "recommendation"
    assert data["analysis_result"] is None  # 이번 턴에 새로 만든 분석 없음


def test_edit_turn_returns_recommendation_with_change_summary(monkeypatch):
    _set_api_key(monkeypatch)
    _route(monkeypatch, "edit")
    edited = json.dumps({
        "recommendation": _CLEAN_FLOW,
        "change_summary": "저장 단계 세션명을 Default로 정리",
        "answer": "수정했어요.",
    }, ensure_ascii=False)
    monkeypatch.setattr(edit_mod, "_make_llm", lambda: _FakeInvokeLLM([_Chunk(edited)]))
    ctx = dict(_CTX, recommendation=_CLEAN_FLOW, analysis=_ANALYSIS.model_dump())
    data = _done(_collect("세션명 정리해줘", ctx))
    assert data["type"] == "recommendation"
    assert data["change_summary"] == "저장 단계 세션명을 Default로 정리"
    assert data["answer"] == "수정했어요."


def test_edit_no_change_returns_answer_type(monkeypatch):
    """LLM이 수정 없음(recommendation=null)을 고르면 type은 answer — type 정확성 원칙."""
    _set_api_key(monkeypatch)
    _route(monkeypatch, "edit")
    no_change = json.dumps({"recommendation": None, "change_summary": "", "answer": "그 단계는 이미 있어요."},
                           ensure_ascii=False)
    monkeypatch.setattr(edit_mod, "_make_llm", lambda: _FakeInvokeLLM([_Chunk(no_change)]))
    ctx = dict(_CTX, recommendation=_CLEAN_FLOW)
    data = _done(_collect("루프 있어?", ctx))
    assert data["type"] == "answer"
    assert data["updated_recommendation"] is None


def test_compact_operation_bypasses_intake(monkeypatch):
    _set_api_key(monkeypatch)

    def intake_boom(*a, **k):
        raise AssertionError("compact는 intake를 우회해야 함")

    monkeypatch.setattr(intake_mod, "chat_json", intake_boom)
    monkeypatch.setattr(compact_mod, "chat_json",
                        lambda *a, **k: CompactContext(task_overview="요약"))
    ctx = dict(_CTX, operation="compact",
               history=[{"role": "user", "content": "긴 대화"}])
    data = _done(_collect("대화를 압축해줘", ctx))
    assert data["type"] == "compact"
    assert data["compact"]["task_overview"] == "요약"


def test_missing_api_key_yields_error(monkeypatch):
    from app.agent.orchestrator import graph as graph_mod

    monkeypatch.setattr(graph_mod.config, "OPENAI_API_KEY", "", raising=False)
    events = _collect("안녕", dict(_CTX))
    assert [e.event for e in events] == ["error"]


def test_infra_error_becomes_error_event(monkeypatch):
    _set_api_key(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("rate limit")

    monkeypatch.setattr(intake_mod, "chat_json", boom)
    events = _collect("안녕", dict(_CTX))
    assert events[-1].event == "error"
    assert "rate limit" in events[-1].message


# ── usage 콜백 전파 (CodeRabbit #100 반박용 회귀 방지) ─────────────────────────
# 진입점이 최상위 config에 얹은 UsageCallbackHandler가, 노드 안에서 직접 만드는
# ChatOpenAI 호출 시점까지 전파돼야 "모든 LLM 호출은 usage 경로 경유" 불변식이
# 성립한다(링 게이지 전제). async 노드/서브그래프로의 contextvar 전파에 의존하므로,
# 누가 이를 깨뜨리면(sync 노드화 등) 이 테스트가 잡는다.

class _CallbackSpyStreamLLM:
    """astream 시점의 config.callbacks를 포착하는 qa용 스파이."""

    def __init__(self, captured):
        self._captured = captured

    def bind_tools(self, tools):
        return self

    async def astream(self, messages):
        from langchain_core.runnables.config import ensure_config

        self._captured["callbacks"] = ensure_config().get("callbacks") or []
        yield _Chunk("ok")


class _CallbackSpyInvokeLLM:
    """ainvoke 시점의 config.callbacks를 포착하는 edit용 스파이."""

    def __init__(self, captured, response):
        self._captured = captured
        self._response = response

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        from langchain_core.runnables.config import ensure_config

        self._captured["callbacks"] = ensure_config().get("callbacks") or []
        return self._response


def _has_usage_handler(callbacks) -> bool:
    from app.core.llm import UsageCallbackHandler

    # 노드 컨텍스트 안에서는 callbacks가 CallbackManager로 변환돼 온다(핸들러는 .handlers).
    # 리스트로 오는 경우(직접 config)도 대비해 둘 다 처리한다.
    handlers = getattr(callbacks, "handlers", callbacks) or []
    return any(isinstance(h, UsageCallbackHandler) for h in handlers)


def test_usage_callback_propagates_into_qa_llm(monkeypatch):
    _set_api_key(monkeypatch)
    _route(monkeypatch, "qa")
    captured = {}
    monkeypatch.setattr(qa_mod, "_make_llm", lambda: _CallbackSpyStreamLLM(captured))
    _collect("안녕", dict(_CTX))
    assert _has_usage_handler(captured["callbacks"])  # 노드 안 astream까지 전파됨


def test_usage_callback_propagates_into_edit_llm(monkeypatch):
    _set_api_key(monkeypatch)
    _route(monkeypatch, "edit")
    edited = json.dumps({"recommendation": _CLEAN_FLOW, "change_summary": "정리", "answer": "수정"},
                        ensure_ascii=False)
    captured = {}
    monkeypatch.setattr(edit_mod, "_make_llm",
                        lambda: _CallbackSpyInvokeLLM(captured, _Chunk(edited)))
    ctx = dict(_CTX, recommendation=_CLEAN_FLOW, analysis=_ANALYSIS.model_dump())
    _collect("세션명 정리해줘", ctx)
    assert _has_usage_handler(captured["callbacks"])  # 노드 안 ainvoke까지 전파됨
