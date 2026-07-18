"""LLM 사용량 귀속(usage attribution) 테스트 (RPA-33).

정준환 피드백 검증 포함:
- UsageCallbackHandler가 스트리밍 usage(usage_metadata)를 0이 아닌 값으로 읽는지
- 콜백 __init__ 스냅샷이 워커 스레드에서도 귀속을 유지하는지
"""

import concurrent.futures
import contextvars
import uuid
from types import SimpleNamespace

import pytest

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

    # record_usage는 관측 세션(observability_sessionmaker, RPA-90)으로 쓴다 — 그 경로를 패치.
    # (앱 DB 폴백 경로 app.db.SessionLocal도 함께 패치해 로컬 .env에 관측 URL이 있어도 Neon을 안 때린다)
    import app.core.observability_db as obs
    monkeypatch.setattr(obs, "observability_sessionmaker", lambda: (lambda: _FakeDB()))
    monkeypatch.setattr("app.db.SessionLocal", lambda: _FakeDB())

    class _Row:
        def __init__(self, **kw): self.__dict__.update(kw)
    monkeypatch.setattr("app.models.LlmUsage", _Row)

    with usage_context(component="agent", actor_type="user", user_id=None):
        record_usage(purpose="chat", model="m", input_tokens=10, output_tokens=5)
    assert captured["component"] == "agent"
    assert captured["actor_type"] == "user"
    assert captured["input_tokens"] == 10 and captured["output_tokens"] == 5


def test_record_usage_captures_request_id(monkeypatch):
    """record_usage가 현재 request_id를 붙여 audit/turn/rag와 턴 단위 비용 조인을 가능케 한다 (RPA-158)."""
    captured = {}

    class _FakeDB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def add(self, row): captured.update(vars(row))
        def commit(self): pass

    import app.core.observability_db as obs
    monkeypatch.setattr(obs, "observability_sessionmaker", lambda: (lambda: _FakeDB()))
    monkeypatch.setattr("app.db.SessionLocal", lambda: _FakeDB())

    class _Row:
        def __init__(self, **kw): self.__dict__.update(kw)
    monkeypatch.setattr("app.models.LlmUsage", _Row)

    from app.rag.observability import new_request_id
    rid = new_request_id()  # 현재 요청 컨텍스트에 request_id 설정
    record_usage(purpose="chat", model="m", input_tokens=1, output_tokens=1)
    assert captured["request_id"] == rid


def test_callback_snapshots_request_id_for_worker_thread(monkeypatch):
    """콜백은 생성 시점(요청 스레드)에 request_id를 스냅샷해, on_llm_end가 워커 스레드에서
    돌아 ContextVar가 전파 안 돼도 request_id가 유실되지 않아야 한다 (RPA-158, CodeRabbit #224).
    """
    import threading

    import app.core.llm as llm
    from app.rag.observability import new_request_id

    captured = {}
    monkeypatch.setattr(llm, "record_usage", lambda **kw: captured.update(kw))

    rid = new_request_id()  # 요청 스레드에서 설정
    handler = llm.UsageCallbackHandler(purpose="chat")  # 여기서 스냅샷

    result = _llm_result(usage_metadata={"input_tokens": 1, "output_tokens": 1})
    # 워커 스레드에서 on_llm_end 실행 — 새 스레드엔 ContextVar가 전파되지 않는다
    t = threading.Thread(target=lambda: handler.on_llm_end(result))
    t.start()
    t.join()

    assert captured["request_id"] == rid  # 스냅샷 덕에 유실 없음


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


# --- response_format 패스스루 (RPA-36, analyze/recommend 선행) ---

def _mock_client(captured):
    from types import SimpleNamespace

    def _create(**kwargs):
        captured.update(kwargs)
        msg = SimpleNamespace(content="{}")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg)],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))


def test_chat_passes_response_format_when_given(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm, "_get_client", lambda: _mock_client(captured))
    monkeypatch.setattr(llm, "record_usage", lambda **k: None)

    rf = {"type": "json_schema", "json_schema": {"name": "AnalysisResult", "schema": {}}}
    llm.chat([{"role": "user", "content": "x"}], purpose="analyze", response_format=rf)
    assert captured["response_format"] == rf  # create()에 그대로 실림


def test_chat_omits_response_format_when_none(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm, "_get_client", lambda: _mock_client(captured))
    monkeypatch.setattr(llm, "record_usage", lambda **k: None)

    llm.chat([{"role": "user", "content": "x"}], purpose="vision_parse")
    assert "response_format" not in captured  # 기존 호출부 동작 무변경


# --- 스트리밍 끊김 회귀 (RPA-48): 콜백이 LangChain 콜백 프로토콜을 안전히 만족하는가 ---

def test_callback_inherits_base_and_attrs_dont_raise():
    """구 버그: __getattr__ 덕타이핑이 run_inline 접근에서 AttributeError → 스트림 끊김.
    이제 BaseCallbackHandler 상속으로 run_inline/raise_error/ignore_* 기본값이 잡힌다."""
    from langchain_core.callbacks import BaseCallbackHandler

    cb = UsageCallbackHandler()
    assert isinstance(cb, BaseCallbackHandler)
    # LangChain async 매니저가 읽는 속성들 — 하나라도 AttributeError면 스트림이 끊긴다
    for attr in ("run_inline", "raise_error", "ignore_llm", "ignore_chat_model", "ignore_retry"):
        assert getattr(cb, attr) is False, f"{attr}가 False여야 안전"

    # 실제 CallbackManager에 등록돼도 예외 없이 구성돼야 한다
    from langchain_core.callbacks import CallbackManager

    assert len(CallbackManager(handlers=[cb]).handlers) == 1


# --- 모델별 비용 계산 (RPA-97) ---

def test_cost_usd_aux_models_use_own_price(monkeypatch):
    """임베딩·리랭커는 env와 무관하게 자기 공식 단가로 계산된다 (챗 단가 오염 방지)."""
    # 챗 단가를 일부러 크게 설정 — 보조 모델이 이걸 쓰면 안 된다
    monkeypatch.setenv("LLM_INPUT_COST_PER_1M", "0.75")
    monkeypatch.setenv("LLM_OUTPUT_COST_PER_1M", "4.50")
    # text-embedding-3-small: in $0.02, out 0 → 1,000,000 토큰 = $0.02
    assert llm.cost_usd(1_000_000, 0, "text-embedding-3-small") == 0.02
    # rerank-2.5-lite: in $0.02 → 43만 토큰 = $0.0086 (챗 단가면 $0.3225로 뻥튀기됐을 것)
    assert abs(llm.cost_usd(430_000, 0, "rerank-2.5-lite") - 0.0086) < 1e-9
    assert llm.cost_usd(430_000, 0, "rerank-2.5-lite") != 430_000 * 0.75 / 1e6  # 챗 단가 아님


def test_cost_usd_chat_model_uses_env(monkeypatch):
    """주 챗 모델(gpt-5.4-mini)은 env 단가로 — 데모 조정 가능·하위호환."""
    monkeypatch.setenv("LLM_INPUT_COST_PER_1M", "0.75")
    monkeypatch.setenv("LLM_OUTPUT_COST_PER_1M", "4.50")
    # 1M in + 1M out = 0.75 + 4.50 = 5.25
    assert llm.cost_usd(1_000_000, 1_000_000, "gpt-5.4-mini") == 5.25
    # 날짜 스냅샷 이름도 prefix로 챗 취급(보조 테이블엔 없으니 env)
    assert llm.cost_usd(1_000_000, 0, "gpt-5.4-mini-2026-03-17") == 0.75


def test_cost_usd_longest_prefix_wins():
    """rerank-2.5-lite는 rerank-2.5(0.05)가 아니라 자기 단가(0.02)로 — 최장 prefix 우선."""
    # 1M 토큰 → lite=0.02, 일반=0.05. lite가 일반으로 새면 안 된다.
    assert llm.cost_usd(1_000_000, 0, "rerank-2.5-lite") == 0.02
    assert llm.cost_usd(1_000_000, 0, "rerank-2.5") == 0.05


def test_cost_usd_none_when_no_price(monkeypatch):
    """미지 모델 + env 미설정이면 None (억지 계산 금지)."""
    monkeypatch.delenv("LLM_INPUT_COST_PER_1M", raising=False)
    monkeypatch.delenv("LLM_OUTPUT_COST_PER_1M", raising=False)
    assert llm.cost_usd(1000, 500, "some-unknown-model") is None
    assert llm.cost_usd(1000, 500, None) is None
    # 단, 보조 모델은 env 없어도 내장 단가로 계산된다
    assert llm.cost_usd(1_000_000, 0, "text-embedding-3-small") == 0.02


# --- 프롬프트 캐시 (RPA-199) — 미반영이 실청구 대비 4.7배 과대계상의 주원인 ---

def _chat_prices(monkeypatch, cached_price="0.075"):
    """챗 모델 단가 3종을 세팅한다. cached_price=None이면 캐시 단가 미설정(구 동작)."""
    monkeypatch.setenv("LLM_INPUT_COST_PER_1M", "0.75")
    monkeypatch.setenv("LLM_OUTPUT_COST_PER_1M", "4.50")
    if cached_price is None:
        monkeypatch.delenv("LLM_CACHED_INPUT_COST_PER_1M", raising=False)
    else:
        monkeypatch.setenv("LLM_CACHED_INPUT_COST_PER_1M", cached_price)


def test_cost_usd_cached_tokens_discounted(monkeypatch):
    """캐시 적중분은 캐시 단가(정가의 10%)로 갈라 계산된다."""
    _chat_prices(monkeypatch)
    # 전부 캐시: 1M in → $0.075 (전액이면 $0.75 — 이 10배가 과대계상의 실체)
    assert llm.cost_usd(1_000_000, 0, "gpt-5.4-mini", cached_tokens=1_000_000) == pytest.approx(0.075)
    # 절반 캐시: 0.5×0.75 + 0.5×0.075
    assert llm.cost_usd(1_000_000, 0, "gpt-5.4-mini", cached_tokens=500_000) == pytest.approx(0.4125)
    # output은 캐시와 무관 — 출력 단가 그대로
    assert llm.cost_usd(0, 1_000_000, "gpt-5.4-mini", cached_tokens=999) == pytest.approx(4.50)


def test_cost_usd_cached_env_missing_keeps_old_formula(monkeypatch):
    """LLM_CACHED_INPUT_COST_PER_1M 미설정이면 cached가 있어도 전액 — 배포 회귀 없음."""
    _chat_prices(monkeypatch, cached_price=None)
    assert llm.cost_usd(1_000_000, 0, "gpt-5.4-mini", cached_tokens=1_000_000) == pytest.approx(0.75)


def test_cost_usd_cached_none_or_zero_is_full_price(monkeypatch):
    """cached None(측정 안 됨)·0(캐시 없음) 둘 다 전액 — 기존 호출부와 결과 동일."""
    _chat_prices(monkeypatch)
    full = llm.cost_usd(1_000_000, 0, "gpt-5.4-mini")
    assert llm.cost_usd(1_000_000, 0, "gpt-5.4-mini", cached_tokens=None) == full == pytest.approx(0.75)
    assert llm.cost_usd(1_000_000, 0, "gpt-5.4-mini", cached_tokens=0) == full


def test_cost_usd_cached_clamped_to_input(monkeypatch):
    """cached > input 이상 응답은 input으로 클램프 — 음수 비용을 만들지 않는다."""
    _chat_prices(monkeypatch)
    assert llm.cost_usd(1_000_000, 0, "gpt-5.4-mini", cached_tokens=2_000_000) == pytest.approx(0.075)
    assert llm.cost_usd(1_000_000, 0, "gpt-5.4-mini", cached_tokens=-5) == pytest.approx(0.75)


def test_normalize_cached_tokens_bounds():
    """정규화는 [0, input] 범위로. None(측정 안 됨)은 0으로 바꾸지 않고 그대로 유지한다."""
    assert llm.normalize_cached_tokens(None, 100) is None   # '모름'을 '캐시 0'으로 만들면 안 된다
    assert llm.normalize_cached_tokens(150, 100) == 100
    assert llm.normalize_cached_tokens(-5, 100) == 0
    assert llm.normalize_cached_tokens(30, 100) == 30
    assert llm.normalize_cached_tokens(10, 0) == 0


def test_cost_usd_aux_model_ignores_cached(monkeypatch):
    """보조 모델(임베딩·리랭커)은 프롬프트 캐시 개념이 없다 — cached를 무시하고 자기 단가."""
    _chat_prices(monkeypatch)
    assert llm.cost_usd(1_000_000, 0, "text-embedding-3-small", cached_tokens=1_000_000) == 0.02


def _mock_client_usage(captured, usage):
    """주어진 usage 객체를 그대로 돌려주는 OpenAI 클라이언트 목업."""

    def _create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))], usage=usage)

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))


def test_chat_records_cached_tokens(monkeypatch):
    """chat() 직접 경로: usage.prompt_tokens_details.cached_tokens → record_usage로 전달."""
    recorded = {}
    usage = SimpleNamespace(
        prompt_tokens=10, completion_tokens=2,
        prompt_tokens_details=SimpleNamespace(cached_tokens=7),
    )
    monkeypatch.setattr(llm, "_get_client", lambda: _mock_client_usage({}, usage))
    monkeypatch.setattr(llm, "record_usage", lambda **k: recorded.update(k))
    llm.chat([{"role": "user", "content": "x"}], purpose="chat")
    assert recorded["cached_tokens"] == 7


def test_chat_cached_none_when_details_missing(monkeypatch):
    """details가 없으면 None(측정 안 됨) — 0으로 채우면 '캐시 없음'과 '모름'이 섞인다."""
    recorded = {}
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=2)  # details 없음 (구 SDK/모델)
    monkeypatch.setattr(llm, "_get_client", lambda: _mock_client_usage({}, usage))
    monkeypatch.setattr(llm, "record_usage", lambda **k: recorded.update(k))
    llm.chat([{"role": "user", "content": "x"}], purpose="chat")
    assert recorded["cached_tokens"] is None


def test_callback_reads_cache_read_from_usage_metadata(monkeypatch):
    """콜백(스트리밍 포함): usage_metadata.input_token_details.cache_read → cached_tokens."""
    recorded = {}
    monkeypatch.setattr(llm, "record_usage", lambda **kw: recorded.update(kw))
    cb = UsageCallbackHandler()
    cb.on_llm_end(_llm_result(usage_metadata={
        "input_tokens": 123, "output_tokens": 45,
        "input_token_details": {"cache_read": 100},
    }))
    assert recorded["cached_tokens"] == 100
    assert recorded["input_tokens"] == 123  # input은 캐시 포함 총량 그대로


def test_callback_reads_cached_from_token_usage_fallback(monkeypatch):
    """비스트리밍 폴백: llm_output.token_usage.prompt_tokens_details.cached_tokens."""
    recorded = {}
    monkeypatch.setattr(llm, "record_usage", lambda **kw: recorded.update(kw))
    cb = UsageCallbackHandler()
    cb.on_llm_end(_llm_result(usage_metadata=None, llm_output={
        "token_usage": {"prompt_tokens": 200, "completion_tokens": 80,
                        "prompt_tokens_details": {"cached_tokens": 55}},
        "model_name": "gpt-x",
    }))
    assert recorded["cached_tokens"] == 55


def test_callback_cached_none_when_absent(monkeypatch):
    """양쪽 경로 다 캐시 정보가 없으면 None — 기존 형태 응답에 회귀 없음."""
    recorded = {}
    monkeypatch.setattr(llm, "record_usage", lambda **kw: recorded.update(kw))
    cb = UsageCallbackHandler()
    cb.on_llm_end(_llm_result(usage_metadata={"input_tokens": 1, "output_tokens": 1}))
    assert recorded["cached_tokens"] is None


def test_record_usage_persists_cached_and_costs_with_it(monkeypatch):
    """record_usage가 cached_tokens를 행에 싣고, cost_usd 계산에도 같은 값을 쓴다 —
    기록과 비용이 다른 값을 읽으면 대사가 애초에 불가능하다."""
    _chat_prices(monkeypatch)
    captured = {}

    class _FakeDB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def add(self, row): captured.update(vars(row))
        def commit(self): pass

    import app.core.observability_db as obs
    monkeypatch.setattr(obs, "observability_sessionmaker", lambda: (lambda: _FakeDB()))
    monkeypatch.setattr("app.db.SessionLocal", lambda: _FakeDB())

    class _Row:
        def __init__(self, **kw): self.__dict__.update(kw)
    monkeypatch.setattr("app.models.LlmUsage", _Row)

    record_usage(purpose="chat", model="gpt-5.4-mini",
                 input_tokens=1_000_000, output_tokens=0, cached_tokens=1_000_000)
    assert captured["cached_tokens"] == 1_000_000
    assert captured["cost_usd"] == pytest.approx(0.075)  # 전액이면 0.75 — 캐시 단가가 실제로 쓰였다

    # cached > input 이상 응답: **저장값도** 클램프돼야 한다 (#271 리뷰).
    # 비용만 보정하고 원본을 저장하면 리포트 적중률이 100%를 넘는다.
    captured.clear()
    record_usage(purpose="chat", model="gpt-5.4-mini",
                 input_tokens=1_000, output_tokens=0, cached_tokens=9_999)
    assert captured["cached_tokens"] == 1_000
    assert captured["cached_tokens"] <= captured["input_tokens"]


# --- 호출 시간 상한·실패 관측 (RPA-202) ---

def _fresh_client(monkeypatch):
    """캐시된 전역 클라이언트를 비워 _get_client()가 다시 만들게 한다."""
    monkeypatch.setattr(llm, "_client", None, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


def test_client_bounds_read_timeout_far_below_sdk_default(monkeypatch):
    """🔴 SDK 기본 read timeout은 600초(10분)다 — 요청 하나가 그만큼 워커를 붙잡는다.

    실측(llm_usage, 챗 5,441콜) 최대가 152초라 180초면 정상 호출을 자르지 않으면서
    600초를 3.3배 조인다. 이 단언이 깨지면 '10분 매달림'이 되살아난 것이다.
    """
    _fresh_client(monkeypatch)
    monkeypatch.delenv("LLM_TIMEOUT_SECONDS", raising=False)
    c = llm._get_client()
    assert c.timeout.read <= 300, "read timeout이 5분을 넘으면 요청 경로가 사실상 무한대가 된다"
    assert c.timeout.read >= 160, "실측 최대(152초)보다 커야 정상 호출을 자르지 않는다"
    assert c.timeout.connect <= 10, "연결 자체가 안 되는 상황은 오래 기다릴 이유가 없다"


def test_timeout_and_retries_are_env_tunable(monkeypatch):
    """데모 직전에 코드 수정 없이 조일 수 있어야 한다."""
    _fresh_client(monkeypatch)
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "42")
    monkeypatch.setenv("LLM_CONNECT_TIMEOUT_SECONDS", "3")
    monkeypatch.setenv("LLM_MAX_RETRIES", "5")
    c = llm._get_client()
    assert c.timeout.read == 42 and c.timeout.connect == 3
    assert c.max_retries == 5


def test_retries_stay_enabled(monkeypatch):
    """SDK 재시도(408·409·429·5xx·연결오류)를 죽이지 않았음을 못 박는다.

    timeout을 명시하면서 max_retries=0을 같이 넣으면 일시적 429가 즉시 실패로 바뀐다 —
    조용히 안정성을 깎는 회귀라 여기서 막는다.
    """
    _fresh_client(monkeypatch)
    monkeypatch.delenv("LLM_MAX_RETRIES", raising=False)
    assert llm._get_client().max_retries >= 1


def _raising_client(exc):
    from types import SimpleNamespace

    def _create(**kwargs):
        raise exc

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))


def _openai_error(cls):
    """OpenAI 예외는 생성자 시그니처가 제각각이라 __new__로 우회 생성한다."""
    return cls.__new__(cls)


def test_rate_limit_message_does_not_blame_credits(monkeypatch):
    """429는 크레딧 소진일 수도, 순간 폭주일 수도 있다 — 원인을 단정하면 운영자가 엉뚱한 곳을 본다."""
    from openai import RateLimitError

    monkeypatch.setattr(llm, "_get_client", lambda: _raising_client(_openai_error(RateLimitError)))
    monkeypatch.setattr(llm, "record_usage", lambda **k: None)
    with pytest.raises(RuntimeError) as ei:
        llm.chat([{"role": "user", "content": "x"}], purpose="chat")
    msg = str(ei.value)
    assert "잠시 후 다시 시도" in msg  # 일시적 가능성을 먼저 안내
    assert not msg.startswith("OpenAI 사용량 한도 초과")  # 단정하지 않는다


def test_timeout_becomes_friendly_error_and_is_observed(monkeypatch):
    """타임아웃이 raw 예외로 새지 않고, 관측에 흔적을 남긴다."""
    from openai import APITimeoutError

    events = []
    monkeypatch.setattr(llm, "_get_client", lambda: _raising_client(_openai_error(APITimeoutError)))
    monkeypatch.setattr(llm, "record_usage", lambda **k: None)
    monkeypatch.setattr("app.rag.observability.log_event",
                        lambda event, **f: events.append((event, f)))

    with pytest.raises(RuntimeError, match="제한 시간"):
        llm.chat([{"role": "user", "content": "x"}], purpose="analyze")

    assert events, "실패가 관측에 안 남으면 '왜 느렸나'에 답할 수 없다"
    name, fields = events[0]
    assert name == "external_api_attempt"  # 임베딩 경로와 같은 축
    assert fields["failure_kind"] == "timeout" and fields["purpose"] == "analyze"
    # 예외 문자열은 남기지 않는다 — 요청 페이로드가 실릴 수 있다
    assert "error_message" not in fields


def test_observation_failure_does_not_mask_llm_error(monkeypatch):
    """관측이 깨져도 원래 오류가 그대로 올라와야 한다 (fail-open)."""
    from openai import APIConnectionError

    monkeypatch.setattr(llm, "_get_client", lambda: _raising_client(_openai_error(APIConnectionError)))
    monkeypatch.setattr(llm, "record_usage", lambda **k: None)

    def _boom(*a, **k):
        raise RuntimeError("관측 DB 다운")

    monkeypatch.setattr("app.rag.observability.log_event", _boom)
    with pytest.raises(RuntimeError, match="연결하지 못했습니다"):
        llm.chat([{"role": "user", "content": "x"}], purpose="chat")
