"""문서 파싱 (FR-02, 04) — PDF/PPTX에서 텍스트·표를 구조 보존 추출해 분석용 JSON으로.

산출 형식 (documents.parsed_content):
{
  "parser": "pypdf" | "pdfbox" | "python-pptx" (+"+vision" 보강 시),
  "page_count": int,
  "pages": [{"page": 1, "blocks": [{"type": "text"|"table"|"notes"|"vision_text", ...}]}],
  "full_text": str,          # LLM 분석 입력용 전체 텍스트
  "warnings": [str],         # 예: 텍스트 없는 페이지(이미지 페이지 가능성)
  "vision": {"enriched_pages": [int]},   # 비전 보강 수행 시
}

이미지 중심 페이지는 vision.enrich_document()가 비전 LLM으로 보강한다 (FR-03).
"""

from app.services.parser.pdf import parse_pdf
from app.services.parser.pptx import parse_pptx


def parse_document(filename: str, content: bytes) -> dict:
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        return parse_pdf(content)
    if ext == "pptx":
        return parse_pptx(content)
    raise ValueError(f"파서가 지원하지 않는 형식: .{ext}")
