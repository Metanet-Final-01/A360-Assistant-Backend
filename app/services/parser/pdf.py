"""PDF 텍스트 추출 — pypdf(페이지 단위, 기본) → PDFBox(레이아웃 정렬, 폴백).

pypdf가 텍스트를 전혀 못 뽑는 문서(스캔본·특수 인코딩)에서만 PDFBox CLI를 시도한다.
PDFBox는 Docker 이미지에 내장되어 있고(PDFBOX_JAR_PATH), 로컬에 Java가 없으면 건너뛴다.
"""

import io
import logging
import os
import shutil
import subprocess
import tempfile

from pypdf import PdfReader

logger = logging.getLogger(__name__)


def parse_pdf(content: bytes) -> dict:
    parser, page_texts = _with_pypdf(content)

    if not any(t.strip() for t in page_texts):
        pdfbox_text = _try_pdfbox(content)
        if pdfbox_text and pdfbox_text.strip():
            parser = "pdfbox"
            page_texts = pdfbox_text.split("\f") if "\f" in pdfbox_text else [pdfbox_text]

    pages = []
    warnings = []
    for i, text in enumerate(page_texts, start=1):
        blocks = [
            {"type": "text", "text": chunk.strip()}
            for chunk in text.split("\n\n")
            if chunk.strip()
        ]
        if not blocks:
            warnings.append(f"{i}페이지에서 텍스트를 찾지 못함 (이미지 페이지 가능성 — OCR은 후속 지원)")
        pages.append({"page": i, "blocks": blocks})

    return {
        "parser": parser,
        "page_count": len(pages),
        "pages": pages,
        "full_text": "\n\n".join(t.strip() for t in page_texts if t.strip()),
        "warnings": warnings,
    }


def _with_pypdf(content: bytes) -> tuple[str, list[str]]:
    reader = PdfReader(io.BytesIO(content))
    return "pypdf", [(page.extract_text() or "") for page in reader.pages]


def _try_pdfbox(content: bytes) -> str | None:
    jar = os.getenv("PDFBOX_JAR_PATH", "").strip()
    if not jar or not os.path.exists(jar) or not shutil.which("java"):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.pdf")
        dst = os.path.join(tmp, "out.txt")
        with open(src, "wb") as f:
            f.write(content)
        try:
            subprocess.run(
                ["java", "-jar", jar, "export:text", "-sort", "-i", src, "-o", dst],
                check=True,
                capture_output=True,
                timeout=60,
            )
            with open(dst, encoding="utf-8", errors="replace") as f:
                return f.read()
        except (subprocess.SubprocessError, OSError) as e:
            logger.warning("PDFBox 추출 실패 (pypdf 결과 사용): %s", e)
            return None
