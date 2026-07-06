"""백엔드 LLM 호출의 공용 진입점 — 토큰·비용·지연을 llm_usage 테이블에 기록한다.

비기능 "관측성"(토큰/비용·응답시간 모니터링)의 데이터 수집 지점.
백엔드 서비스의 직접 LLM 호출은 반드시 이 래퍼를 경유한다.
(app/agent는 LangChain 경유라 별도 — 사용량 통합은 후속 이슈)

사용량 기록 실패(DB 다운 등)는 경고만 남기고 호출 자체는 성공시킨다.
"""

import logging
import os
import time
import uuid

logger = logging.getLogger(__name__)

_client = None


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
) -> str:
    """Chat Completions 호출 후 응답 텍스트를 반환하고 사용량을 기록한다.

    purpose: analyze|recommend|chat|summarize|vision_parse|other — llm_usage 집계 키.
    """
    from openai import AuthenticationError, RateLimitError

    model = model or os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
    started = time.monotonic()
    try:
        response = _get_client().chat.completions.create(model=model, messages=messages)
    except AuthenticationError as e:
        raise RuntimeError("OpenAI 인증 실패 — API 키를 확인하세요") from e
    except RateLimitError as e:
        # 사용자가 조치 가능한 오류는 구성 오류(RuntimeError)로 변환해 명확히 전달
        raise RuntimeError("OpenAI 사용량 한도 초과 — 크레딧/요금제를 확인하세요") from e
    latency_ms = int((time.monotonic() - started) * 1000)

    usage = response.usage
    _record(
        purpose=purpose,
        model=model,
        input_tokens=usage.prompt_tokens if usage else 0,
        output_tokens=usage.completion_tokens if usage else 0,
        latency_ms=latency_ms,
        session_id=session_id,
    )
    return response.choices[0].message.content or ""


def _record(
    *,
    purpose: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    session_id: uuid.UUID | None,
) -> None:
    try:
        from app.db import SessionLocal
        from app.models import LlmUsage

        with SessionLocal() as db:
            db.add(
                LlmUsage(
                    session_id=session_id,
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
