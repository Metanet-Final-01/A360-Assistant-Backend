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


def test_enrich_parallel_pages_all_processed(monkeypatch):
    """병렬 처리에서도 모든 페이지가 보강되고 이벤트 순서(stage→partial*→done)가 유지된다."""
    monkeypatch.setenv("VISION_MIN_TEXT_CHARS", "200")
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "추출된 내용")
    monkeypatch.setattr(
        vision, "render_pdf_pages", lambda content, nums: {n: [b"\x89PNGfake"] for n in nums}
    )
    parsed = {"parser": "pypdf", "pages": [_poor_page(1), _poor_page(2), _poor_page(3)],
              "full_text": "", "warnings": []}

    events = list(vision.enrich_document_stream("doc.pdf", b"%PDF-", parsed))

    assert events[0].event == "stage"
    assert events[-1].event == "done"
    partials = [e for e in events[1:-1]]
    assert all(e.event == "partial" for e in partials)
    assert {e.data["page"] for e in partials} == {1, 2, 3}  # 완료 순서는 무관, 전부 처리
    assert events[-1].data["enriched_pages"] == [1, 2, 3]  # done에서는 정렬 보장


def test_enrich_continues_when_one_page_fails(monkeypatch):
    """한 페이지의 LLM 오류가 나머지 페이지 보강을 막지 않는다."""
    def _chat(messages, **kwargs):
        if _chat.calls == 0:
            _chat.calls += 1
            raise ValueError("일시 오류")
        return "추출된 내용"
    _chat.calls = 0

    monkeypatch.setattr(llm, "chat", _chat)
    monkeypatch.setattr(
        vision, "render_pdf_pages", lambda content, nums: {n: [b"\x89PNGfake"] for n in nums}
    )
    monkeypatch.setenv("VISION_CONCURRENCY", "1")  # 실패 순서 결정적으로
    parsed = {"parser": "pypdf", "pages": [_poor_page(1), _poor_page(2)],
              "full_text": "", "warnings": []}

    events = list(vision.enrich_document_stream("doc.pdf", b"%PDF-", parsed))

    assert events[-1].data["enriched_pages"] == [2]  # 1페이지 실패, 2페이지 성공
    assert any(e.data.get("error") for e in events if e.event == "partial")


def test_pdf_text_page_without_images_is_skipped(monkeypatch):
    """이미지 없는 PDF 페이지는 텍스트가 좀 부족해도(50자 이상) 비전을 낭비하지 않는다."""
    def _fail(*a, **k):
        raise AssertionError("LLM이 호출되면 안 됨")

    monkeypatch.setattr(llm, "chat", _fail)
    # 실제 빈 PDF(이미지 객체 없음) + 실질 텍스트 100자(임계값 200 미만, 강제기준 50 이상)
    parsed = {"parser": "pypdf", "full_text": "", "warnings": [],
              "pages": [{"page": 1, "blocks": [{"type": "text", "text": "가" * 100}]}]}
    result, stats = vision.enrich_document("doc.pdf", _blank_pdf(), parsed)
    assert stats["enriched_pages"] == []


def test_whitespace_padding_does_not_inflate_char_count():
    """layout 모드의 공백 패딩이 임계값 판정을 왜곡하지 않는다 (비공백 기준)."""
    padded = {"page": 1, "blocks": [{"type": "text", "text": "표  제목      값        2026" + " " * 500}]}
    assert vision._page_text_chars(padded) < 20


def test_cost_usd_from_env(monkeypatch):
    monkeypatch.setenv("LLM_INPUT_COST_PER_1M", "0.15")
    monkeypatch.setenv("LLM_OUTPUT_COST_PER_1M", "0.60")
    assert llm.cost_usd(1_000_000, 1_000_000) == 0.75
    monkeypatch.delenv("LLM_INPUT_COST_PER_1M")
    assert llm.cost_usd(1000, 1000) is None


# --- 라우트 레벨 회귀: SSE 스트림에서 usage_context 유지 (RPA-38에서 발견된 버그) ---
# 동기 제너레이터는 StreamingResponse가 next()마다 다른 스레드 컨텍스트에서 재개하므로,
# usage_context를 yield 너머로 걸치면 귀속이 끊기고 종료 시 reset이 ValueError로 터져
# done 뒤에 가짜 error 이벤트가 붙는다. 라우트는 copy_context로 매 재개를 감싸야 한다.

def test_enrich_vision_route_keeps_context_across_yields(monkeypatch):
    import json
    import uuid as _uuid
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    import app.api.documents as documents_api
    from app.core.llm import current_usage_context
    from app.db import get_db
    from app.main import app
    from app.schemas import ProgressEvent

    doc_id = _uuid.uuid4()
    doc = SimpleNamespace(
        id=doc_id, session_id=_uuid.uuid4(), filename="doc.pdf",
        status="parsed", parsed_content={"pages": []}, storage_path="p",
    )

    class FakeDB:
        def get(self, model, key):
            return doc

    seen_components = []

    def _fake_stream(filename, content, parsed, session_id=None):
        # 두 번 이상 yield — 재개 구간마다 컨텍스트가 유지되는지 검증
        seen_components.append(current_usage_context().component)
        yield ProgressEvent(event="stage", stage="vision", message="1")
        seen_components.append(current_usage_context().component)
        yield ProgressEvent(event="stage", stage="vision", message="2")

    monkeypatch.setattr(documents_api.storage, "load", lambda path: b"%PDF-")
    monkeypatch.setattr(vision, "enrich_document_stream", _fake_stream)
    app.dependency_overrides[get_db] = lambda: FakeDB()
    try:
        with TestClient(app) as c:
            with c.stream("POST", f"/api/documents/{doc_id}/enrich-vision") as r:
                events = [json.loads(l[5:]) for l in r.iter_lines() if l.startswith("data:")]
    finally:
        app.dependency_overrides.clear()

    # 가짜 error 이벤트가 뒤에 붙지 않아야 한다 (ContextVar reset ValueError 회귀)
    assert [e["event"] for e in events] == ["stage", "stage"]
    # 모든 재개 구간에서 vision 귀속 유지 (끊기면 기본값 'other'로 샌다)
    assert seen_components == ["vision", "vision"]
