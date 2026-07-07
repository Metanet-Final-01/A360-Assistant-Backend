"""PDF 텍스트·표 추출 — pypdf(텍스트, 기본) → PDFBox(레이아웃, 폴백), pdfplumber(구조화 표).

텍스트는 pypdf layout 모드로 빠르게 뽑고, 그 위에 pdfplumber로 표를 셀 단위 구조
({"type":"table","rows":[...]})로 얹는다 (PPTX와 동일 계약). 표 셀 내용은 pypdf 텍스트에도
이미 공백 배치로 들어있으므로 full_text는 pypdf 텍스트를 그대로 쓴다(중복 토큰 방지).
pdfplumber가 없거나 실패하면 표 없이 텍스트만 반환한다(안전 폴백).
"""

import io
import logging
import os
import re
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

    tables_by_page = _extract_tables(content)  # {page_no: [rows, ...]}

    pages = []
    warnings = []
    for i, text in enumerate(page_texts, start=1):
        blocks: list[dict] = [
            {"type": "text", "text": chunk.strip()}
            for chunk in text.split("\n\n")
            if chunk.strip()
        ]
        for rows in tables_by_page.get(i, []):
            blocks.append({"type": "table", "rows": rows})
        if not blocks:
            warnings.append(f"{i}페이지에서 텍스트를 찾지 못함 (이미지 페이지 가능성 — OCR은 후속 지원)")
        pages.append({"page": i, "blocks": blocks})

    if tables_by_page:
        parser = f"{parser}+tables"

    return {
        "parser": parser,
        "page_count": len(pages),
        "pages": pages,
        "full_text": "\n\n".join(t.strip() for t in page_texts if t.strip()),
        "warnings": warnings,
    }


def _extract_tables(content: bytes) -> dict[int, list[list[list[str]]]]:
    """pdfplumber로 페이지별 표를 구조화 추출한다. {page_no: [table_rows, ...]}.

    pdfplumber 미설치·손상 등은 표 없이 진행(텍스트는 pypdf가 이미 확보). CPU 비용이
    있어 표가 있는 문서에서만 의미가 있으므로, 실패는 경고만 남기고 조용히 넘어간다.
    """
    try:
        import pdfplumber
    except ImportError:
        return {}

    result: dict[int, list[list[list[str]]]] = {}
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                try:
                    raw_tables = page.extract_tables()
                except Exception:  # noqa: BLE001 — 특정 페이지 표 추출 실패는 건너뛴다
                    continue
                cleaned = [t for t in (_clean_table(rt) for rt in raw_tables) if t]
                if cleaned:
                    result[i] = cleaned
    except Exception as e:  # noqa: BLE001 — 손상/암호화 등은 표 없이 텍스트만 사용
        logger.warning("pdfplumber 표 추출 실패 (텍스트만 사용): %s", e)
    return result


def _clean_table(rows: list[list]) -> list[list[str]]:
    """None 셀을 빈 문자열로, 완전히 빈 행은 제거한다."""
    out: list[list[str]] = []
    for row in rows:
        cells = [(cell or "").strip() for cell in row]
        if any(cells):
            out.append(cells)
    return out


def _with_pypdf(content: bytes) -> tuple[str, list[str]]:
    reader = PdfReader(io.BytesIO(content))
    return "pypdf", [_extract_page_text(page) for page in reader.pages]


def _extract_page_text(page) -> str:
    """layout 모드 우선 추출 — 표·다단 구성의 공간 배치를 보존한다.

    layout 모드는 정렬을 위해 공백을 대량 패딩하므로(실측: 179자 문서가 1,021자로 부풀음)
    3칸 이상 연속 공백은 2칸으로 압축한다 — 열 구분 신호는 남기고 토큰 낭비는 제거.
    """
    try:
        text = page.extract_text(extraction_mode="layout") or ""
    except Exception:  # noqa: BLE001 — 문서에 따라 layout 모드가 실패하면 기본 모드로
        text = page.extract_text() or ""
    lines = (re.sub(r" {3,}", "  ", line.rstrip()) for line in text.split("\n"))
    return "\n".join(lines)


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
