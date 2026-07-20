"""LLM JSON 출력 공통 하네스: JSON mode 호출 → Pydantic 검증 → 1회 교정(repair).

analysis.py·recommend/compose.py에서 반복되던 패턴의 공통화 — 오케스트레이터의
모든 구조화 출력 노드(intake/other/edit/harness/compact)가 이걸 쓴다.
인프라 오류(RuntimeError: 키 미설정·인증·rate limit)는 core.llm.chat이 던지는 것을
그대로 올린다 — 진입점이 error 이벤트로 처리한다.
"""

import json
import logging
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.core import llm

logger = logging.getLogger(__name__)

_RESPONSE_FORMAT = {"type": "json_object"}

T = TypeVar("T", bound=BaseModel)


def _parse(raw: str, model_cls: type[T]) -> T:
    return model_cls.model_validate(json.loads(raw))


def _error_digest(err: Exception) -> str:
    """검증 오류를 필드 경로·유형만 담은 요약으로 축약한다.

    Pydantic ValidationError의 기본 문자열은 위반 값(input_value=…)을 그대로 되비추어
    문서 원문 등 입력 데이터가 로그·재교정 프롬프트로 새어나갈 수 있다. 어느 필드가 어떤
    유형으로 틀렸는지만 남겨 재교정에 필요한 정보는 주되 원문 값은 노출하지 않는다.
    """
    if isinstance(err, ValidationError):
        parts = [
            f"{'.'.join(str(x) for x in e.get('loc', ()))}: {e.get('type')}"
            for e in err.errors()
        ]
        return "; ".join(parts) or "형식 불일치"
    return "JSON 구문 오류"  # JSONDecodeError 위치정보도 굳이 노출하지 않는다


def chat_json(messages: list[dict], *, purpose: str, model_cls: type[T]) -> T:
    """JSON mode로 LLM을 호출해 model_cls로 검증한다. 위반 시 1회 교정, 재실패면 ValueError.

    사용량은 core.llm.chat이 purpose로 귀속 기록한다 — 오케스트레이터의 모든 구조화
    호출이 usage 기록 경로를 타야 하는 계약(링 게이지)의 이행 지점이다.
    """
    raw = llm.chat(messages, purpose=purpose, response_format=_RESPONSE_FORMAT)
    try:
        return _parse(raw, model_cls)
    except (json.JSONDecodeError, ValidationError) as first_error:
        digest = _error_digest(first_error)
        logger.warning("%s 첫 출력 파싱 실패(%s), 1회 교정", purpose, digest)
        repair_messages = [
            *messages,
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"위 출력이 지정한 JSON 형식을 만족하지 못했습니다. 문제 필드:\n{digest}\n"
                    "설명 없이, 형식에 맞는 JSON 객체만 다시 출력하세요."
                ),
            },
        ]
        repaired = llm.chat(repair_messages, purpose=purpose, response_format=_RESPONSE_FORMAT)
        try:
            return _parse(repaired, model_cls)
        except (json.JSONDecodeError, ValidationError) as second_error:
            # 예외 체인(로그)에는 원본이 남지만 메시지 문자열에는 원문 값을 싣지 않는다.
            raise ValueError(f"{purpose} 출력 파싱 실패(교정 후에도): {_error_digest(second_error)}") from second_error
