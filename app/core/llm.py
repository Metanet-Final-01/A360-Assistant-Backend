"""백엔드 LLM 호출의 공용 진입점 — 토큰·비용·지연을 llm_usage 테이블에 기록한다.

비기능 "관측성"(토큰/비용·응답시간 모니터링)의 데이터 수집 지점.

사용량 귀속(누가/어디서)은 ContextVar로 전파한다. 라우트에서 usage_context()로
(user_id, session_id, component)를 심어두면, 그 안에서 일어나는 모든 기록이 자동
태깅된다 — 호출부 시그니처를 바꾸지 않아도 된다.

- 백엔드 직접 호출: chat() 래퍼 경유 → context를 그대로 읽어 기록
- Agent(LangChain): UsageCallbackHandler를 콜백으로 부착 → 같은 테이블에 기록

사용량 기록 실패(DB 다운 등)는 경고만 남기고 호출 자체는 성공시킨다.
"""

import contextvars
import logging
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_client = None


@dataclass(frozen=True)
class UsageContext:
    """현재 LLM 사용의 귀속 정보. 기본은 시스템(사용자 무관)."""

    actor_type: str = "system"          # "user" | "system"
    user_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    component: str = "other"            # vision | agent | rag_embed | rag_rerank | other


_usage_ctx: contextvars.ContextVar[UsageContext] = contextvars.ContextVar(
    "usage_ctx", default=UsageContext()
)


@contextmanager
def usage_context(
    *,
    component: str,
    user_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    actor_type: str | None = None,
):
    """이 블록 안의 모든 LLM 사용을 (component/user/session)로 귀속시킨다.

    actor_type을 안 주면 user_id 유무로 자동 판정한다 (user_id 있으면 user, 없으면 system).
    """
    resolved_actor = actor_type or ("user" if user_id is not None else "system")
    token = _usage_ctx.set(
        UsageContext(
            actor_type=resolved_actor,
            user_id=user_id,
            session_id=session_id,
            component=component,
        )
    )
    try:
        yield
    finally:
        _usage_ctx.reset(token)


def current_usage_context() -> UsageContext:
    return _usage_ctx.get()


def _get_client():
    global _client
    if _client is None:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY 환경변수가 필요합니다")
        from openai import OpenAI

        _client = OpenAI(api_key=api_key)
    return _client


def cost_usd(input_tokens: int, output_tokens: int) -> float | None:
    """환경변수 단가(USD per 1M tokens)가 설정된 경우에만 비용을 계산한다."""
    try:
        in_price = float(os.environ["LLM_INPUT_COST_PER_1M"])
        out_price = float(os.environ["LLM_OUTPUT_COST_PER_1M"])
    except (KeyError, ValueError):
        return None
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000


def chat(
    messages: list[dict],
    *,
    purpose: str,
    model: str | None = None,
    session_id: uuid.UUID | None = None,
    response_format: dict | None = None,
) -> str:
    """Chat Completions 호출 후 응답 텍스트를 반환하고 사용량을 기록한다.

    귀속(user/component)은 usage_context()에서 읽는다. session_id를 명시로 주면
    context보다 우선한다 (기존 호출부 하위호환).

    response_format: OpenAI JSON mode / Structured Outputs를 위한 dict를 그대로
    패스스루한다. 미지정 시 create()에 전달하지 않아 기존 호출부(vision_parse 등)는
    동작 무변경. 스키마 강제가 필요한 analyze/recommend는 다음을 넘긴다:
      - Structured Outputs: {"type": "json_schema", "json_schema": {...}}
      - 최소 JSON mode:     {"type": "json_object"}
    반환은 str 그대로이며, JSON 파싱·검증은 호출부(agent)가 한다.
    """
    from openai import AuthenticationError, RateLimitError

    model = model or os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
    create_kwargs: dict = {"model": model, "messages": messages}
    if response_format is not None:
        create_kwargs["response_format"] = response_format
    started = time.monotonic()
    try:
        response = _get_client().chat.completions.create(**create_kwargs)
    except AuthenticationError as e:
        raise RuntimeError("OpenAI 인증 실패 — API 키를 확인하세요") from e
    except RateLimitError as e:
        raise RuntimeError("OpenAI 사용량 한도 초과 — 크레딧/요금제를 확인하세요") from e
    latency_ms = int((time.monotonic() - started) * 1000)

    usage = response.usage
    record_usage(
        purpose=purpose,
        model=model,
        input_tokens=usage.prompt_tokens if usage else 0,
        output_tokens=usage.completion_tokens if usage else 0,
        latency_ms=latency_ms,
        session_id=session_id,
    )
    return response.choices[0].message.content or ""


def record_usage(
    *,
    purpose: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int | None = None,
    session_id: uuid.UUID | None = None,
    ctx: UsageContext | None = None,
) -> None:
    """llm_usage에 한 건 기록한다. 귀속 정보는 ctx(명시) 또는 현재 ContextVar에서 온다.

    session_id를 명시로 주면 ctx의 session_id보다 우선한다.
    기록 실패가 호출을 실패시키면 안 되므로 예외는 삼킨다.
    """
    ctx = ctx or current_usage_context()
    try:
        from app.db import SessionLocal
        from app.models import LlmUsage

        with SessionLocal() as db:
            db.add(
                LlmUsage(
                    actor_type=ctx.actor_type,
                    user_id=ctx.user_id,
                    component=ctx.component,
                    session_id=session_id if session_id is not None else ctx.session_id,
                    purpose=purpose,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd(input_tokens, output_tokens),
                    latency_ms=latency_ms,
                )
            )
            db.commit()
    except Exception as e:  # noqa: BLE001 — 기록 실패가 호출을 실패시키면 안 된다
        logger.warning("LLM 사용량 기록 실패 (호출은 정상): %s", e)


class UsageCallbackHandler:
    """Agent(LangChain)의 LLM 호출 사용량을 llm_usage에 기록하는 콜백.

    부착: llm.invoke(msgs, config={"callbacks": [UsageCallbackHandler()]})
    run_agent의 graph.invoke, stream_agent의 graph.astream 최상위 config에 얹으면
    두 진입점을 한 번에 커버한다.

    ⚠️ 설계 근거 (정준환 피드백):
    1) 토큰 읽기 — 스트리밍은 usage를 message.usage_metadata에만 싣고 llm_output은
       안 만든다. usage_metadata(input/output_tokens) 우선, llm_output(prompt/
       completion_tokens) 폴백으로 읽어야 스트리밍 챗이 0으로 새지 않는다.
    2) 귀속 전파 — LLM 노드가 워커 스레드(LangGraph run_in_executor)에서 돌면
       ContextVar가 그 스레드까지 안 갈 수 있다. 그래서 __init__(콜백 생성 시점,
       usage_context 안=요청 스레드)에서 ContextVar를 스냅샷으로 잡아두고,
       on_llm_end는 그 스냅샷을 쓴다 → 스레드 무관하게 정확히 귀속된다.
    """

    def __init__(self, purpose: str = "chat") -> None:
        self._ctx = current_usage_context()  # 생성 시점 스냅샷 (요청 스레드)
        self._purpose = purpose
        self._started = time.monotonic()

    # LangChain BaseCallbackHandler 인터페이스 (덕타이핑 — import 의존 없음)
    ignore_llm = False
    raise_error = False

    def on_llm_start(self, *args, **kwargs) -> None:
        self._started = time.monotonic()

    def on_llm_end(self, response, **kwargs) -> None:
        input_tokens, output_tokens, model = _extract_tokens(response)
        latency_ms = int((time.monotonic() - self._started) * 1000)
        record_usage(
            purpose=self._purpose,
            model=model or os.getenv("OPENAI_MODEL", "unknown"),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            ctx=self._ctx,
        )

    # LangChain이 호출할 수 있는 나머지 훅은 무시 (덕타이핑 안정성)
    def __getattr__(self, name):
        if name.startswith("on_") or name.startswith("ignore_"):
            return lambda *a, **k: None
        raise AttributeError(name)


def _extract_tokens(response) -> tuple[int, int, str | None]:
    """LangChain LLMResult에서 (input, output, model)을 뽑는다.

    usage_metadata(스트림·비스트림 공통) 우선, llm_output.token_usage(비스트림) 폴백.
    """
    model = None
    try:
        model = (response.llm_output or {}).get("model_name")
    except AttributeError:
        pass

    # 1) usage_metadata 우선 (LangChain 표준: input_tokens/output_tokens)
    try:
        um = response.generations[0][0].message.usage_metadata
    except (IndexError, AttributeError, TypeError):
        um = None
    if um:
        return int(um.get("input_tokens", 0)), int(um.get("output_tokens", 0)), model

    # 2) 폴백: llm_output.token_usage (OpenAI 원형: prompt_tokens/completion_tokens)
    try:
        tu = (response.llm_output or {}).get("token_usage") or {}
    except AttributeError:
        tu = {}
    return int(tu.get("prompt_tokens", 0)), int(tu.get("completion_tokens", 0)), model
