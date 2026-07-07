"""문서 파싱 (FR-02, 04) — PDF/PPTX/DOCX에서 텍스트·표를 구조 보존 추출해 분석용 JSON으로.

산출 형식 (documents.parsed_content):
{
  "parser": "pypdf"|"pypdf+tables"|"pdfbox"|"python-pptx"|"python-docx"|"text" (+"+vision" 보강 시),
  "page_count": int,
  "pages": [{"page": 1, "blocks": [{"type": "text"|"table"|"notes"|"vision_text", ...}]}],
  "full_text": str,          # LLM 분석 입력용 전체 텍스트
  "warnings": [str],         # 예: 텍스트 없는 페이지(이미지 페이지 가능성)
  "vision": {"enriched_pages": [int]},   # 비전 보강 수행 시
}

표는 {"type":"table","rows":[[셀,...],...]}로 구조화된다 (PPTX·DOCX는 셀 단위, PDF는
pdfplumber). 이미지 중심 페이지는 vision.enrich_document()가 비전 LLM으로 보강한다 (FR-03).
"""

from app.services.parser.docx import parse_docx
from app.services.parser.pdf import parse_pdf
from app.services.parser.pptx import parse_pptx


def parse_document(filename: str, content: bytes) -> dict:
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        return parse_pdf(content)
    if ext == "pptx":
        return parse_pptx(content)
    if ext == "docx":
        return parse_docx(content)
    raise ValueError(f"파서가 지원하지 않는 형식: .{ext}")


def parse_text(text: str) -> dict:
    """자연어 업무 요청 텍스트를 파싱 결과 형태로 감싼다 (RPA-43 자연어 입력용).

    파일이 아닌 순수 텍스트를 기존 analyze 흐름(parsed_content 소비)에 그대로 태우기 위한
    합성 문서. 단일 페이지·단일 텍스트 블록.
    """
    clean = text.strip()
    return {
        "parser": "text",
        "page_count": 1,
        "pages": [{"page": 1, "blocks": [{"type": "text", "text": clean}] if clean else []}],
        "full_text": clean,
        "warnings": [] if clean else ["입력 텍스트가 비어 있습니다."],
    }
