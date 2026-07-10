"""app/rag/sources/html_structurizer.py 단위 테스트."""

from app.rag.sources.html_structurizer import html_to_structured_json

_SAMPLE_HTML = """
<html><head><style>.x{color:red}</style><script>alert(1)</script></head>
<body>
<div class="body conbody">
  <p class="shortdesc">한 줄 설명</p>
  <section class="section prereq">
    <ul class="ul">
      <li class="li"><span class="xref ft-internal-link" data-tocid="ABC123">다른 문서</span></li>
    </ul>
  </section>
  <p class="p"><a class="xref ft-external-link" href="https://example.com">외부 링크</a></p>
</div>
</body></html>
"""


def test_drops_script_and_style_tags():
    result = html_to_structured_json(_SAMPLE_HTML)
    dumped = str(result)
    assert "alert" not in dumped
    assert "color:red" not in dumped


def test_keeps_class_and_data_tocid():
    result = html_to_structured_json(_SAMPLE_HTML)
    dumped = str(result)
    assert "data-tocid" in dumped
    assert "ABC123" in dumped
    assert "prereq" in dumped


def test_keeps_href_for_external_links():
    result = html_to_structured_json(_SAMPLE_HTML)
    dumped = str(result)
    assert "https://example.com" in dumped


def test_scopes_to_body_div_when_present():
    # <html>/<head>/<body> 래퍼는 결과에 안 남아야 한다 — <div class="body ...">부터 시작.
    result = html_to_structured_json(_SAMPLE_HTML)
    assert result["tag"] == "div"
    assert result["attrs"]["class"] == "body conbody"


def test_falls_back_to_whole_document_when_no_body_div():
    result = html_to_structured_json("<div><p>내용</p></div>")
    assert result is not None
    dumped = str(result)
    assert "내용" in dumped


def test_malformed_html_never_raises():
    result = html_to_structured_json("<div><p>안 닫힌 태그")
    assert result is not None


def test_text_content_preserved():
    result = html_to_structured_json(_SAMPLE_HTML)
    dumped = str(result)
    assert "한 줄 설명" in dumped
    assert "다른 문서" in dumped
