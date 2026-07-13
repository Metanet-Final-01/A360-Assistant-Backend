"""관측 로그용 경량 PII 마스킹 (RPA-123).

관측 DB에 남는 자유 텍스트(turn_events.detail의 reason·query 등)에 사용자 업무 내용이
섞일 수 있어, 저장 직전에 명백한 PII 패턴만 레닥션한다. 목적은 '완전한 익명화'가 아니라
'관측 로그로 개인정보가 새어 들어가는 것 방지'다 — 관측성(디버그 가치)은 최대한 보존한다.

대상: 이메일, 장기 숫자열(전화·계좌·주민번호 등 후보). 나머지 관측 컬럼(user_id는 UUID,
path는 정규화)은 PII가 아니라 마스킹 대상이 아니다(정책 문서 참고).
"""

import re

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# 하이픈/공백이 섞인 7자리 이상 숫자열 — 전화·계좌·카드·주민번호 등 식별번호 후보.
# 짧은 숫자(코드·수량·좌표)는 오탐이 크므로 건드리지 않는다.
_LONGNUM = re.compile(r"\b(?:\d[\d\s\-]{5,}\d)\b")


def mask_pii(text: str | None) -> str | None:
    """이메일·장기 숫자열을 고정 토큰으로 치환. None/빈값은 그대로."""
    if not text:
        return text
    masked = _EMAIL.sub("[EMAIL]", text)
    masked = _LONGNUM.sub("[NUM]", masked)
    return masked


def mask_fields(data: dict, keys: tuple[str, ...]) -> dict:
    """dict에서 지정한 자유 텍스트 키들만 마스킹한 새 dict를 반환(원본 불변).

    step_id·route 같은 구조적 값은 건드리지 않고, reason·query 등 사용자 텍스트가
    섞일 수 있는 키만 마스킹한다."""
    if not data:
        return data
    out = dict(data)
    for k in keys:
        if isinstance(out.get(k), str):
            out[k] = mask_pii(out[k])
    return out
