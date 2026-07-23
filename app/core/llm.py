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

# langchain-core는 하드 의존성(requirements.txt — Agent가 사용). UsageCallbackHandler가
# 상속한다 (덕타이핑 __getattr__은 run_inline 등 접근에서 터져 스트림을 끊었다, RPA-48).
from langchain_core.callbacks import BaseCallbackHandler

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


# LLM 호출의 시간 상한 (RPA-202) — SDK 기본값을 그대로 쓰면 **read timeout이 600초(10분)**다.
# 응답이 안 오는 상황에서 요청 하나가 10분간 워커를 붙잡는다. `/turn`·vision 파싱은 사용자가
# 화면에서 기다리는 경로라 사실상 무한대다.
#
# ⚠️ 값은 실측에서 뽑았다 (관측 DB llm_usage, 챗 모델 5,441콜):
#     turn_generate  p50 3.5초 / p95 34초 / p99 73초 / 최대 152초
#     전체 챗 호출    p99 46초 / 최대 152초
# 처음에 60초로 잡으려 했으나 **정상 요청을 잘랐을 값**이다(turn_generate p99가 73초).
# 관측 최대(152초)에 여유를 둬 180초로 정한다 — 600초 대비 3.3배 조인 값이면서
# 지금까지 성공한 어떤 호출도 자르지 않는다. 임계는 감이 아니라 실측에서 나와야 한다(RPA-171 교훈).
#
# connect는 짧게 유지한다(5초) — 연결 자체가 안 되는 상황은 기다릴 이유가 없다.
_DEFAULT_TIMEOUT_SECONDS = 180.0
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
# SDK 기본과 동일(2회). 명시하는 이유는 값이 코드에 보이고 env로 조정 가능해지기 때문이다.
# 재시도 대상은 SDK가 정한다: 408·409·429·5xx·연결 오류 (지수 백오프 + Retry-After 존중).
_DEFAULT_MAX_RETRIES = 2


def _client_timeout():
    """httpx Timeout — read만 늘리고 connect는 짧게 유지한다."""
    import httpx

    read = float(os.getenv("LLM_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS))
    connect = float(os.getenv("LLM_CONNECT_TIMEOUT_SECONDS", _DEFAULT_CONNECT_TIMEOUT_SECONDS))
    return httpx.Timeout(read, connect=connect)


def _get_client():
    global _client
    if _client is None:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY 환경변수가 필요합니다")
        from openai import OpenAI

        _client = OpenAI(
            api_key=api_key,
            timeout=_client_timeout(),
            max_retries=int(os.getenv("LLM_MAX_RETRIES", _DEFAULT_MAX_RETRIES)),
        )
    return _client


# 보조 모델(임베딩·리랭커) 공식 단가 (USD per 1M tokens, 2026-07 확인) — RPA-97.
# 이들은 주 챗 모델과 단가가 크게 달라(임베딩·리랭커는 훨씬 쌈), env 단일 단가로 계산하면
# 심하게 과대추정된다(rerank 43만 토큰이 챗 단가면 실제의 19배). 그래서 자기 단가로 계산한다.
# (output은 임베딩·리랭커 모두 과금 없음 → 0). 챗 모델은 아래 env로 조정 가능하게 남긴다.
_AUX_MODEL_PRICES: dict[str, tuple[float, float]] = {
    "text-embedding-3-small": (0.02, 0.0),  # OpenAI
    "text-embedding-3-large": (0.13, 0.0),  # OpenAI (혹시 전환 시)
    "rerank-2.5-lite": (0.02, 0.0),         # Voyage
    "rerank-2.5": (0.05, 0.0),              # Voyage (혹시 전환 시, docs.voyageai.com)
}


def normalize_cached_tokens(cached_tokens: int | None, input_tokens: int) -> int | None:
    """캐시 토큰을 [0, input_tokens]로 정규화한다 (RPA-199). None(측정 안 됨)은 유지.

    ⚠️ **저장값과 계산값이 같아야 한다** — 비용 계산에서만 클램프하고 원본을 저장하면,
    `cached > input`인 이상 응답에서 비용은 맞는데 DB엔 적중률 100% 초과가 남는다(#271 리뷰).
    그래서 record_usage가 기록 **전에** 한 번 정규화해 저장·계산에 같은 값을 쓴다.
    """
    if cached_tokens is None:
        return None
    return min(max(int(cached_tokens), 0), max(int(input_tokens), 0))


def cost_usd(
    input_tokens: int,
    output_tokens: int,
    model: str | None = None,
    cached_tokens: int | None = None,
) -> float | None:
    """비용(USD)을 모델별 단가로 계산한다 (RPA-97, 캐시 반영 RPA-199).

    - 보조 모델(임베딩·리랭커)은 내장 공식 단가 테이블로 — 프롬프트 캐시 개념이 없어
      cached_tokens는 무시한다.
    - 주 챗 모델 등 그 외는 env 단가(LLM_INPUT/OUTPUT_COST_PER_1M) — 데모 조정·하위호환.
    - cached_tokens(입력 중 프롬프트 캐시 적중분)는 LLM_CACHED_INPUT_COST_PER_1M이 설정된
      경우에만 그 단가로 갈라 계산한다: (input−cached)×정가 + cached×캐시단가 + output×출력단가.
      캐시 단가 미설정이면 기존식(전액)으로 폴백 — 회귀 없음. 캐시 미반영이 곧 4.7배
      과대계상의 주원인이었다(캐시 입력은 정가의 10%).
    - cached > input인 이상 응답은 input으로 클램프 — 음수 비용을 만들지 않는다(호출부가
      normalize_cached_tokens로 이미 정규화해 넘기지만, 이 함수는 공개 API라 자체 방어한다).
    - 단가를 못 구하면(미지 모델 + env 미설정) None.
    """
    if model:
        # 최장 prefix 우선 — "rerank-2.5-lite"가 "rerank-2.5"보다 먼저 매칭돼야 한다
        # (dict 순서에 기대지 않고 항상 가장 구체적인 항목을 고른다).
        for prefix in sorted(_AUX_MODEL_PRICES, key=len, reverse=True):
            if model.startswith(prefix):
                in_price, out_price = _AUX_MODEL_PRICES[prefix]
                return (input_tokens * in_price + output_tokens * out_price) / 1_000_000
    try:
        in_price = float(os.environ["LLM_INPUT_COST_PER_1M"])
        out_price = float(os.environ["LLM_OUTPUT_COST_PER_1M"])
    except (KeyError, ValueError):
        return None
    base = input_tokens * in_price + output_tokens * out_price
    if cached_tokens:
        try:
            cached_price = float(os.environ["LLM_CACHED_INPUT_COST_PER_1M"])
        except (KeyError, ValueError):
            cached_price = None  # 캐시 단가 미설정 — 전액 계산 유지(기존 동작과 100% 동일)
        if cached_price is not None:
            cached = normalize_cached_tokens(cached_tokens, input_tokens) or 0
            base -= cached * (in_price - cached_price)
    return base / 1_000_000


def _log_llm_failure(purpose: str, model: str, kind: str, started: float, exc: Exception) -> None:
    """LLM 호출 실패를 관측에 남긴다 (RPA-202).

    임베딩 경로(`app/rag/retrieval/embed.py`)는 시도마다 `external_api_attempt`를 남기는데
    LLM은 SDK가 **조용히** 재시도하고 실패해서 우리 관측에 흔적이 없었다 — "이 턴이 왜 오래
    걸렸나"에 답할 수 없었다. 같은 이벤트 이름을 써서 두 경로를 한 축에서 볼 수 있게 한다.

    ⚠️ 예외 문자열을 남기지 않는다 — OpenAI 오류 본문에 요청 페이로드 일부가 실릴 수 있다.
    타입명과 소요만 남긴다(SLACK 웹훅 토큰 유출 선례, RPA-189).
    기록 실패가 호출 실패를 덮으면 안 되므로 예외는 삼킨다.
    """
    try:
        from app.rag.observability import log_event

        log_event(
            "external_api_attempt",
            url="openai:chat.completions",
            purpose=purpose,
            model=model,
            status="error",
            error_type=type(exc).__name__,
            failure_kind=kind,
            duration_ms=round((time.monotonic() - started) * 1000, 2),
        )
    except Exception:  # noqa: BLE001 — 관측 실패가 호출 경로를 죽이면 안 된다
        logger.warning("LLM 실패 관측 기록 실패 (호출 오류는 그대로 전파): %s", kind)


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
    from openai import APIConnectionError, APITimeoutError

    started = time.monotonic()
    try:
        response = _get_client().chat.completions.create(**create_kwargs)
    except AuthenticationError as e:
        # 인증 실패도 관측에 남긴다 — 키 만료·교체 사고는 "어느 시점부터 전부 실패했나"를
        # 봐야 원인을 좁힐 수 있는데, 여기가 비어 있으면 그 흔적이 없다(#279 리뷰).
        _log_llm_failure(purpose, model, "auth", started, e)
        raise RuntimeError("OpenAI 인증 실패 — API 키를 확인하세요") from e
    except RateLimitError as e:
        # ⚠️ 여기는 **SDK 재시도(기본 2회)를 이미 소진한 뒤**다. 그래도 원인을 단정하지 않는다 —
        # 429는 크레딧 소진일 수도, 순간 요청 폭주일 수도 있다. "요금제를 확인하라"고 단정하면
        # 일시적 폭주일 때 운영자가 엉뚱한 곳을 본다(RPA-202).
        _log_llm_failure(purpose, model, "rate_limit", started, e)
        raise RuntimeError(
            "OpenAI 요청이 한도에 걸렸습니다 — 잠시 후 다시 시도하세요. "
            "반복되면 크레딧/요금제를 확인하세요."
        ) from e
    except APITimeoutError as e:
        # 타임아웃은 그냥 터뜨리면 raw 예외가 사용자에게 올라간다. 관측에 남기고 친화적 메시지로.
        _log_llm_failure(purpose, model, "timeout", started, e)
        raise RuntimeError("LLM 응답이 제한 시간을 넘겼습니다 — 잠시 후 다시 시도하세요.") from e
    except APIConnectionError as e:
        _log_llm_failure(purpose, model, "connection", started, e)
        raise RuntimeError("LLM 서버에 연결하지 못했습니다 — 잠시 후 다시 시도하세요.") from e
    latency_ms = int((time.monotonic() - started) * 1000)

    usage = response.usage
    # 캐시 적중분 (RPA-199) — details가 없으면 None(측정 안 됨)으로 남긴다. 0으로 채우면
    # "캐시 없음"과 "모름"이 섞여 대사가 거짓말을 하게 된다.
    details = getattr(usage, "prompt_tokens_details", None) if usage else None
    cached = getattr(details, "cached_tokens", None) if details is not None else None
    record_usage(
        purpose=purpose,
        model=model,
        input_tokens=usage.prompt_tokens if usage else 0,
        output_tokens=usage.completion_tokens if usage else 0,
        cached_tokens=cached,
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
    cached_tokens: int | None = None,
    latency_ms: int | None = None,
    session_id: uuid.UUID | None = None,
    ctx: UsageContext | None = None,
    request_id: str | None = None,
) -> None:
    """llm_usage에 한 건 기록한다. 귀속 정보는 ctx(명시) 또는 현재 ContextVar에서 온다.

    session_id를 명시로 주면 ctx의 session_id보다 우선한다.
    request_id를 명시로 주면 그것을, 아니면 현재 ContextVar에서 읽는다 — 워커 스레드에서
    도는 콜백(UsageCallbackHandler)은 ContextVar가 전파 안 되므로 생성 시점 스냅샷을 명시로
    넘겨야 request_id가 유실되지 않는다(ctx와 동일한 스레드 경계 문제, RPA-158).
    기록 실패가 호출을 실패시키면 안 되므로 예외는 삼킨다.
    """
    ctx = ctx or current_usage_context()
    # 저장 전에 한 번 정규화 — 저장값과 비용 계산값이 갈리면 리포트의 적중률이 100%를 넘는다.
    cached_tokens = normalize_cached_tokens(cached_tokens, input_tokens)
    try:
        from app.core.observability_db import observability_sessionmaker
        from app.models import LlmUsage
        from app.rag.observability import get_request_id

        # 관측 전용 DB(RPA-90) — 미설정/장애 시 아래 best-effort 경계에서 기록만 건너뛴다.
        with observability_sessionmaker()() as db:
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
                    cached_tokens=cached_tokens,
                    cost_usd=cost_usd(input_tokens, output_tokens, model, cached_tokens),
                    latency_ms=latency_ms,
                    # request_id로 audit/turn/rag와 턴 단위 비용 조인 (RPA-158). 명시값(콜백의
                    # 생성-시점 스냅샷) 우선, 없으면 현재 ContextVar — 워커 스레드 유실 방지.
                    request_id=request_id if request_id is not None else get_request_id(),
                )
            )
            db.commit()
    except Exception as e:  # noqa: BLE001 — 기록 실패가 호출을 실패시키면 안 된다
        logger.warning("LLM 사용량 기록 실패 (호출은 정상): %s", e)


class UsageCallbackHandler(BaseCallbackHandler):
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
       usage_context(ctx)뿐 아니라 request_id도 같은 ContextVar라 함께 스냅샷한다(RPA-158) —
       안 그러면 워커 스레드의 agent/streaming 사용량이 request_id NULL로 남아 턴 조인이 깨진다.
    """

    def __init__(self, purpose: str = "chat") -> None:
        super().__init__()
        from app.rag.observability import get_request_id

        self._ctx = current_usage_context()  # 생성 시점 스냅샷 (요청 스레드)
        self._request_id = get_request_id()  # request_id도 요청 스레드에서 스냅샷 (RPA-158)
        self._purpose = purpose
        self._started = time.monotonic()

    def on_llm_start(self, *args, **kwargs) -> None:
        self._started = time.monotonic()

    def on_llm_end(self, response, **kwargs) -> None:
        input_tokens, output_tokens, model, cached_tokens = _extract_tokens(response)
        latency_ms = int((time.monotonic() - self._started) * 1000)
        record_usage(
            purpose=self._purpose,
            model=model or os.getenv("OPENAI_MODEL", "unknown"),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            latency_ms=latency_ms,
            ctx=self._ctx,
            request_id=self._request_id,  # 생성-시점 스냅샷 (워커 스레드 유실 방지, RPA-158)
        )


def _extract_tokens(response) -> tuple[int, int, str | None, int | None]:
    """LangChain LLMResult에서 (input, output, model, cached)를 뽑는다.

    usage_metadata(스트림·비스트림 공통) 우선, llm_output.token_usage(비스트림) 폴백.
    cached(프롬프트 캐시 적중분, RPA-199)는 경로별 위치가 다르다:
    usage_metadata는 input_token_details.cache_read(LangChain 표준),
    token_usage는 prompt_tokens_details.cached_tokens(OpenAI 원형).
    못 찾으면 None — "측정 안 됨"과 "캐시 0"을 구분해 기록한다.
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
        cached = (um.get("input_token_details") or {}).get("cache_read")
        return (
            int(um.get("input_tokens", 0)),
            int(um.get("output_tokens", 0)),
            model,
            int(cached) if cached is not None else None,
        )

    # 2) 폴백: llm_output.token_usage (OpenAI 원형: prompt_tokens/completion_tokens)
    try:
        tu = (response.llm_output or {}).get("token_usage") or {}
    except AttributeError:
        tu = {}
    cached = (tu.get("prompt_tokens_details") or {}).get("cached_tokens")
    return (
        int(tu.get("prompt_tokens", 0)),
        int(tu.get("completion_tokens", 0)),
        model,
        int(cached) if cached is not None else None,
    )
