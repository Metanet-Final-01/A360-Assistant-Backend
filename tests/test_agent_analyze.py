"""app/agent/analysis.py (analyze, FR-05) 단위 테스트 (RPA-26).

LLM 없이 검증한다: 입력 포맷팅(출처·vision·표·경고 태깅), JSON 파싱·검증, step_id
정규화, 그리고 llm.chat을 몽키패치한 analyze의 happy/repair/빈문서 경로.
"""
import json

import pytest
from pydantic import ValidationError

from app.agent.v2 import analysis
from app.schemas import AnalysisResult


def test_format_document_tags_vision_pages_tables_and_warnings():
    parsed = {
        "pages": [
            {"page": 1, "blocks": [
                {"type": "text", "text": "제목: 금 시세 자동화"},
                {"type": "table", "rows": [["날짜", "가격"], ["1일", "100"]]},
            ]},
            {"page": 2, "blocks": [
                {"type": "vision_text", "text": "화면 캡처: Excel 저장 버튼"},
            ]},
        ],
        "warnings": ["2페이지에서 텍스트를 찾지 못함"],
    }
    out = analysis._format_document(parsed)
    assert "[1페이지]" in out and "[2페이지]" in out
    assert "제목: 금 시세 자동화" in out
    assert "날짜 | 가격" in out and "1일 | 100" in out
    # vision 블록은 불확실 마커로 구분된다
    assert "⟨2페이지·비전추출·불확실⟩ 화면 캡처: Excel 저장 버튼" in out
    assert "[파서 경고]" in out and "2페이지에서 텍스트를 찾지 못함" in out


def test_parse_analysis_valid():
    good = json.dumps({"summary": "s", "steps": [{"step_id": "step-1", "order": 1, "name": "n"}]})
    res = analysis._parse_analysis(good)
    assert res.steps[0].name == "n"


def test_parse_analysis_invalid_raises():
    # 필수 필드(name) 누락 → ValidationError
    with pytest.raises(ValidationError):
        analysis._parse_analysis(json.dumps({"steps": [{"step_id": "step-1", "order": 1}]}))
    # JSON 자체가 깨짐 → JSONDecodeError
    with pytest.raises(json.JSONDecodeError):
        analysis._parse_analysis("이건 JSON이 아님")


def test_normalize_reassigns_stable_step_ids():
    result = AnalysisResult(steps=[
        {"step_id": "x", "order": 9, "name": "A"},
        {"step_id": "y", "order": 3, "name": "B"},
    ])
    analysis._normalize(result)
    assert [s.step_id for s in result.steps] == ["step-1", "step-2"]
    assert [s.order for s in result.steps] == [1, 2]


def test_analyze_happy_path_calls_chat_and_normalizes(monkeypatch):
    calls = []

    def _fake_chat(messages, *, purpose, response_format=None, **kw):
        calls.append({"purpose": purpose, "response_format": response_format})
        return json.dumps({
            "document_title": "금 시세 조회",
            "summary": "매일 금 시세를 조회해 엑셀로 정리한다.",
            "steps": [
                {"step_id": "s-A", "order": 5, "name": "시세 조회",
                 "description": "네이버 증권에서 조회", "inputs": [], "outputs": ["시세 표"],
                 "systems": ["Edge"], "branching": None,
                 "evidence": {"page": 1, "snippet": "금 시세"}},
            ],
            "ambiguities": ["메일 주소 미명시"],
        })

    monkeypatch.setattr(analysis.llm, "chat", _fake_chat)
    parsed = {"full_text": "금 시세 조회",
              "pages": [{"page": 1, "blocks": [{"type": "text", "text": "금 시세 조회"}]}],
              "warnings": []}

    result = analysis.analyze(parsed)

    assert result.document_title == "금 시세 조회"
    assert result.ambiguities == ["메일 주소 미명시"]
    assert len(result.steps) == 1
    # 모델이 준 s-A/order=5 → 결정론적으로 재부여
    assert result.steps[0].step_id == "step-1"
    assert result.steps[0].order == 1
    assert result.steps[0].systems == ["Edge"]
    assert result.steps[0].evidence.page == 1
    # chat 호출 계약: purpose=analyze + JSON mode
    assert calls[0] == {"purpose": "analyze", "response_format": {"type": "json_object"}}


def test_analyze_repairs_once_on_bad_first_output(monkeypatch):
    outputs = iter([
        "이건 JSON이 아니고 설명입니다",  # 1차: 파싱 실패
        json.dumps({"summary": "요약", "steps": [], "ambiguities": []}),  # 교정 성공
    ])
    n = {"calls": 0}

    def _fake_chat(messages, *, purpose, response_format=None, **kw):
        n["calls"] += 1
        return next(outputs)

    monkeypatch.setattr(analysis.llm, "chat", _fake_chat)
    parsed = {"full_text": "x", "pages": [{"page": 1, "blocks": [{"type": "text", "text": "x"}]}]}

    result = analysis.analyze(parsed)

    assert n["calls"] == 2  # 원 호출 + 1회 교정
    assert result.steps == []
    assert result.summary == "요약"


def test_analyze_empty_document_skips_llm(monkeypatch):
    called = {"n": 0}

    def _fake_chat(*a, **k):
        called["n"] += 1
        return "{}"

    monkeypatch.setattr(analysis.llm, "chat", _fake_chat)
    parsed = {"full_text": "", "pages": [{"page": 1, "blocks": []}],
              "warnings": ["1페이지에서 텍스트를 찾지 못함"]}

    result = analysis.analyze(parsed)

    assert called["n"] == 0  # LLM 미호출 (비용 절감)
    assert result.steps == []
    assert result.ambiguities and "텍스트" in result.ambiguities[0]


def test_analyze_raises_valueerror_if_repair_also_fails(monkeypatch):
    def _fake_chat(*a, **k):
        return "여전히 JSON 아님"

    monkeypatch.setattr(analysis.llm, "chat", _fake_chat)
    parsed = {"full_text": "x", "pages": [{"page": 1, "blocks": [{"type": "text", "text": "x"}]}]}

    with pytest.raises(ValueError, match="파싱 실패"):
        analysis.analyze(parsed)


def test_build_messages_has_roles_and_json_keyword():
    parsed = {"pages": [{"page": 1, "blocks": [{"type": "text", "text": "금 시세 조회"}]}]}
    msgs = analysis._build_messages(parsed)
    assert [m["role"] for m in msgs] == ["system", "user"]
    # OpenAI json_object 모드는 메시지 어딘가에 'json' 단어가 있어야 400이 안 난다
    assert "json" in json.dumps(msgs, ensure_ascii=False).lower()
    assert "금 시세 조회" in msgs[1]["content"]


def test_analyze_repairs_on_schema_invalid_first_output(monkeypatch):
    # 1차: 유효 JSON이지만 필수 'name' 누락 → ValidationError 경유 repair (JSONDecodeError와 다른 분기)
    outputs = iter([
        json.dumps({"summary": "s", "steps": [{"step_id": "s", "order": 1}]}),
        json.dumps({"summary": "s", "steps": [{"step_id": "step-1", "order": 1, "name": "n"}]}),
    ])
    n = {"calls": 0}

    def _fake_chat(messages, *, purpose, response_format=None, **kw):
        n["calls"] += 1
        return next(outputs)

    monkeypatch.setattr(analysis.llm, "chat", _fake_chat)
    parsed = {"pages": [{"page": 1, "blocks": [{"type": "text", "text": "x"}]}]}

    result = analysis.analyze(parsed)

    assert n["calls"] == 2
    assert result.steps[0].name == "n"


def test_analyze_blank_table_only_document_skips_llm(monkeypatch):
    # 빈 셀만 있는 표(레이아웃용)뿐이고 full_text가 " | " 노이즈여도 텍스트 없음으로 판정 → LLM 미호출
    called = {"n": 0}

    def _fake_chat(*a, **k):
        called["n"] += 1
        return "{}"

    monkeypatch.setattr(analysis.llm, "chat", _fake_chat)
    parsed = {"full_text": " | \n | ",
              "pages": [{"page": 1, "blocks": [{"type": "table", "rows": [["", ""], ["", ""]]}]}]}

    result = analysis.analyze(parsed)

    assert called["n"] == 0
    assert result.steps == []
