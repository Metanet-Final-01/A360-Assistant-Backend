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


def _call_digest(meta: dict) -> str:
    """왜 깨졌나를 가르는 최소 신호 — 값·원문은 여전히 안 싣는다.

    `finish_reason == "length"`면 **잘림**이라 처방이 다르다(부피를 줄이게 하거나 상한을
    올린다). 이게 없으면 실패가 전부 "JSON 구문 오류" 한 줄로 뭉쳐, 프롬프트를 키우는 변경
    뒤에 surgeon이 잘리기 시작해도 원인을 못 짚는다.
    """
    reason = meta.get("finish_reason")
    return f"finish_reason={reason}, {meta.get('content_chars')}자"


def chat_json(messages: list[dict], *, purpose: str, model_cls: type[T]) -> T:
    """JSON mode로 LLM을 호출해 model_cls로 검증한다. 위반 시 1회 교정, 재실패면 ValueError.

    사용량은 core.llm.chat이 purpose로 귀속 기록한다 — 오케스트레이터의 모든 구조화
    호출이 usage 기록 경로를 타야 하는 계약(링 게이지)의 이행 지점이다.
    """
    meta: dict = {}
    raw = llm.chat(messages, purpose=purpose, response_format=_RESPONSE_FORMAT, meta=meta)
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
        meta2: dict = {}
        repaired = llm.chat(repair_messages, purpose=purpose,
                            response_format=_RESPONSE_FORMAT, meta=meta2)
        try:
            return _parse(repaired, model_cls)
        except (json.JSONDecodeError, ValidationError) as second_error:
            # 예외 체인(로그)에는 원본이 남지만 메시지 문자열에는 원문 값을 싣지 않는다.
            # finish_reason·길이는 값이 아니라 **호출 결과의 형태**라 실어도 된다 — 없으면
            # 잘림과 문법 오류가 같은 문구로 뭉쳐 다음 사고에서 또 추측만 하게 된다.
            raise ValueError(
                f"{purpose} 출력 파싱 실패(교정 후에도): {_error_digest(second_error)} "
                f"[{_call_digest(meta2)}]"
            ) from second_error
