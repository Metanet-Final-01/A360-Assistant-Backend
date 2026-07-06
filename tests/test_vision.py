"""비전 보강 파싱 (FR-03) 단위 테스트 — LLM 호출은 모킹, 렌더링은 실제 수행."""

import io

from pypdf import PdfWriter

from app.core import llm
from app.services.parser import parse_document, vision


def _blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _rich_page(page: int) -> dict:
    return {"page": page, "blocks": [{"type": "text", "text": "가" * 500}]}


def _poor_page(page: int) -> dict:
    return {"page": page, "blocks": [{"type": "text", "text": "제목뿐"}]}


def test_pages_needing_vision_selects_only_poor_pages(monkeypatch):
    monkeypatch.setenv("VISION_MIN_TEXT_CHARS", "200")
    parsed = {"pages": [_rich_page(1), _poor_page(2), _poor_page(3)]}
    assert vision.pages_needing_vision(parsed) == [2, 3]


def test_table_text_counts_toward_threshold(monkeypatch):
    monkeypatch.setenv("VISION_MIN_TEXT_CHARS", "10")
    parsed = {"pages": [{"page": 1, "blocks": [{"type": "table", "rows": [["가나다라마", "바사아자차"]]}]}]}
    assert vision.pages_needing_vision(parsed) == []


def test_render_pdf_pages_produces_png():
    images = vision.render_pdf_pages(_blank_pdf(), [1])
    assert images[1][0].startswith(b"\x89PNG")


def test_enrich_stream_event_order_and_merge(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "화면 캡처: 네이버 증권에서 국내 금 클릭")
    parsed = parse_document("blank.pdf", _blank_pdf())  # 빈 페이지 → 보강 대상
    assert parsed["warnings"]  # 보강 전에는 경고 존재

    events = list(vision.enrich_document_stream("blank.pdf", _blank_pdf(), parsed))

    assert [e.event for e in events] == ["stage", "partial", "done"]
    result = events[-1].data["parsed"]
    assert events[-1].data["enriched_pages"] == [1]
    blocks = result["pages"][0]["blocks"]
    assert blocks[-1]["type"] == "vision_text"
    assert "네이버 증권" in result["full_text"]
    assert result["parser"].endswith("+vision")
    assert result["warnings"] == []  # 보강된 페이지의 경고 제거


def test_enrich_skips_when_all_pages_rich(monkeypatch):
    def _fail(*a, **k):
        raise AssertionError("LLM이 호출되면 안 됨")

    monkeypatch.setattr(llm, "chat", _fail)
    parsed = {"parser": "pypdf", "pages": [_rich_page(1)], "full_text": "가" * 500, "warnings": []}
    result, stats = vision.enrich_document("doc.pdf", b"%PDF-", parsed)
    assert stats["enriched_pages"] == []
    assert result["parser"] == "pypdf"  # 변경 없음


def test_cost_usd_from_env(monkeypatch):
    monkeypatch.setenv("LLM_INPUT_COST_PER_1M", "0.15")
    monkeypatch.setenv("LLM_OUTPUT_COST_PER_1M", "0.60")
    assert llm.cost_usd(1_000_000, 1_000_000) == 0.75
    monkeypatch.delenv("LLM_INPUT_COST_PER_1M")
    assert llm.cost_usd(1000, 1000) is None
