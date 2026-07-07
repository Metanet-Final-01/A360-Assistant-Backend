"""LLM 사용량 귀속(usage attribution) 테스트 (RPA-33).

정준환 피드백 검증 포함:
- UsageCallbackHandler가 스트리밍 usage(usage_metadata)를 0이 아닌 값으로 읽는지
- 콜백 __init__ 스냅샷이 워커 스레드에서도 귀속을 유지하는지
"""

import concurrent.futures
import contextvars
import uuid
from types import SimpleNamespace

import app.core.llm as llm
from app.core.llm import (
    UsageCallbackHandler,
    current_usage_context,
    record_usage,
    usage_context,
)


# --- ContextVar 귀속 ---

def test_usage_context_sets_and_resets():
    assert current_usage_context().actor_type == "system"  # 기본
    uid = uuid.uuid4()
    with usage_context(component="agent", actor_type="user", user_id=uid):
        ctx = current_usage_context()
        assert ctx.component == "agent" and ctx.actor_type == "user" and ctx.user_id == uid
    assert current_usage_context().actor_type == "system"  # 블록 벗어나면 복원


def test_usage_context_auto_actor_type():
    with usage_context(component="rag_embed"):
        assert current_usage_context().actor_type == "system"  # user_id 없으면 system
    with usage_context(component="agent", user_id=uuid.uuid4()):
        assert current_usage_context().actor_type == "user"    # user_id 있으면 user


def test_record_usage_uses_context(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm, "SessionLocal", None, raising=False)

    def _fake_record_row(**kw):
        captured.update(kw)

    # record_usage 내부 DB를 모킹: LlmUsage 생성 인자를 가로챈다
    class _FakeDB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def add(self, row): captured.update(vars(row) if hasattr(row, "__dict__") else {})
        def commit(self): pass

    monkeypatch.setattr("app.db.SessionLocal", lambda: _FakeDB())

    class _Row:
        def __init__(self, **kw): self.__dict__.update(kw)
    monkeypatch.setattr("app.models.LlmUsage", _Row)

    with usage_context(component="agent", actor_type="user", user_id=None):
        record_usage(purpose="chat", model="m", input_tokens=10, output_tokens=5)
    assert captured["component"] == "agent"
    assert captured["actor_type"] == "user"
    assert captured["input_tokens"] == 10 and captured["output_tokens"] == 5


# --- UsageCallbackHandler: 스트리밍 토큰 0 버그 방지 (핵심) ---

def _llm_result(usage_metadata=None, llm_output=None):
    """LangChain LLMResult 형태 목업."""
    msg = SimpleNamespace(usage_metadata=usage_metadata)
    gen = SimpleNamespace(message=msg)
    return SimpleNamespace(generations=[[gen]], llm_output=llm_output)


def test_callback_reads_streaming_usage_metadata(monkeypatch):
    """스트리밍: usage_metadata에만 토큰이 있고 llm_output은 None — 0으로 새면 안 된다."""
    recorded = {}
    monkeypatch.setattr(llm, "record_usage", lambda **kw: recorded.update(kw))

    with usage_context(component="agent", actor_type="user"):
        cb = UsageCallbackHandler()
    cb.on_llm_start()
    cb.on_llm_end(_llm_result(usage_metadata={"input_tokens": 123, "output_tokens": 45}, llm_output=None))

    assert recorded["input_tokens"] == 123  # ← 0이 아님
    assert recorded["output_tokens"] == 45


def test_callback_falls_back_to_llm_output(monkeypatch):
    """비스트리밍: usage_metadata 없고 llm_output.token_usage(prompt/completion)만 있을 때."""
    recorded = {}
    monkeypatch.setattr(llm, "record_usage", lambda **kw: recorded.update(kw))

    cb = UsageCallbackHandler()
    cb.on_llm_end(_llm_result(
        usage_metadata=None,
        llm_output={"token_usage": {"prompt_tokens": 200, "completion_tokens": 80}, "model_name": "gpt-x"},
    ))
    assert recorded["input_tokens"] == 200 and recorded["output_tokens"] == 80
    assert recorded["model"] == "gpt-x"


# --- 콜백 __init__ 스냅샷: 워커 스레드 전파 ---

def test_callback_snapshot_survives_worker_thread(monkeypatch):
    """usage_context 안에서 만든 콜백은, on_llm_end가 다른 스레드에서 돌아도 귀속을 유지한다."""
    recorded = {}
    monkeypatch.setattr(llm, "record_usage", lambda **kw: recorded.update({"ctx": kw["ctx"]}))

    uid = uuid.uuid4()
    with usage_context(component="agent", actor_type="user", user_id=uid):
        cb = UsageCallbackHandler()  # 스냅샷 캡처

    # 완전히 다른 스레드(컨텍스트 미설정)에서 콜백 종료 호출
    def run_in_bare_thread():
        assert current_usage_context().actor_type == "system"  # 이 스레드엔 context 없음
        cb.on_llm_end(_llm_result(usage_metadata={"input_tokens": 1, "output_tokens": 1}))

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(run_in_bare_thread).result()

    assert recorded["ctx"].component == "agent"   # 스냅샷 덕에 유지됨
    assert recorded["ctx"].user_id == uid


def test_copy_context_propagates_to_worker():
    """vision이 쓰는 방식: copy_context().run으로 워커에 usage_context가 전파된다."""
    seen = {}

    def worker():
        seen["ctx"] = current_usage_context()

    with usage_context(component="vision", actor_type="user"):
        ctx = contextvars.copy_context()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(ctx.run, worker).result()

    assert seen["ctx"].component == "vision"  # copy_context 없으면 여기가 'other'로 샘
