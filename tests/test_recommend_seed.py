"""recommend _seed_messages 단위 테스트 (RPA-142) — 업무정의서 원문 주입.

에이전트 시스템 메시지에 [업무 분석](힌트)과 [업무정의서 원문](근거)이 함께 실리는지,
원문이 없으면 기존 형태를 유지하는지, 과대 문서는 캡에서 잘리는지 검증한다.
"""

from app.agent.recommend.graph import MAX_DOC_CHARS, _seed_messages

_ANALYSIS = {
    "summary": "테스트 업무",
    "steps": [{"step_id": "step-1", "order": 1, "name": "저장", "description": "엑셀 저장"}],
}

# 블록 헤더(줄 단위)로 검사한다 — 프롬프트 본문에도 "[업무정의서 원문]이 함께 오면…"
# 문구가 있어 부분 문자열 검사는 오탐한다.
_DOC_HEADER = "\n[업무정의서 원문]\n"


def _system(state):
    messages = _seed_messages(state)
    assert len(messages) == 2  # system + user
    return messages[0].content


def test_document_block_included():
    sys = _system({"analysis": _ANALYSIS, "document": "1페이지: 최근 3일치 시세를 표로 정리"})
    assert _DOC_HEADER in sys
    assert "최근 3일치" in sys
    assert "[업무 분석]" in sys  # 분석 힌트도 유지


def test_no_document_keeps_previous_shape():
    sys = _system({"analysis": _ANALYSIS})
    assert _DOC_HEADER not in sys
    assert "[업무 분석]" in sys


def test_blank_document_is_ignored():
    sys = _system({"analysis": _ANALYSIS, "document": "   \n  "})
    assert _DOC_HEADER not in sys


def test_oversized_document_is_capped():
    oversized = "가" * (MAX_DOC_CHARS + 500)
    sys = _system({"analysis": _ANALYSIS, "document": oversized})
    assert "…(생략)" in sys
    # 원문이 통째로 들어가지 않는다("가"는 프롬프트 본문에도 있어 소량 오차 허용)
    assert sys.count("가") < len(oversized)


def test_constraints_still_rendered_after_document():
    sys = _system({"analysis": _ANALYSIS, "document": "원문", "constraints": ["Knox 금지"]})
    assert "[제약]" in sys and "Knox 금지" in sys
