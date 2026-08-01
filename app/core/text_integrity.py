"""Request-text integrity checks at public API boundaries."""

from __future__ import annotations

import re


_CJK_RE = re.compile(r"[\u1100-\u11FF\u2E80-\u9FFF\uAC00-\uD7AF\uF900-\uFAFF]")
_ENCODING_LOSS_RATIO = 0.25


def is_likely_encoding_loss(text: str) -> bool:
    """Return whether a likely non-UTF-8 client replaced CJK text with ``?``."""
    normalized = text.strip()
    if not normalized or _CJK_RE.search(normalized):
        return False
    return normalized.count("?") / len(normalized) >= _ENCODING_LOSS_RATIO


def encoding_loss_detail() -> dict[str, str]:
    return {
        "code": "TEXT_ENCODING_SUSPECTED",
        "message": "요청 텍스트가 문자 인코딩 손상으로 보입니다. 요청 본문을 UTF-8로 인코딩해 다시 보내 주세요.",
    }
