"""PDF 텍스트·표 추출 — pdfplumber(표 영역 제외 텍스트 + 구조화 표, 기본) → pypdf → PDFBox 폴백.

표는 셀 단위로 {"type":"table","rows":[...]}로 뽑고, 텍스트 블록에는 표 영역을 제외한
본문만 담는다 — 같은 표가 텍스트와 표 블록에 중복 노출돼 LLM 입력을 오염시키는 것을 막는다
(다운스트림 analyze는 pages[].blocks를 프롬프트로 조립하므로 중복이 그대로 비용이 된다).
pdfplumber가 없거나 텍스트를 못 뽑으면 pypdf(레이아웃)→PDFBox로 폴백한다(표는 생략).
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
    page_data = _with_pdfplumber(content)  # [(text, [table_rows,...]), ...] | None
    has_tables = False

    if page_data is not None and any(text.strip() or tables for text, tables in page_data):
        parser = "pdfplumber"
        has_tables = any(tables for _, tables in page_data)
    else:
        parser, page_texts = _with_pypdf(content)
        if not any(t.strip() for t in page_texts):
            pdfbox_text = _try_pdfbox(content)
            if pdfbox_text and pdfbox_text.strip():
                parser = "pdfbox"
                page_texts = pdfbox_text.split("\f") if "\f" in pdfbox_text else [pdfbox_text]
        page_data = [(t, []) for t in page_texts]

    pages: list[dict] = []
    warnings: list[str] = []
    full_parts: list[str] = []
    for i, (text, tables) in enumerate(page_data, start=1):
        blocks: list[dict] = [
            {"type": "text", "text": chunk.strip()}
            for chunk in text.split("\n\n")
            if chunk.strip()
        ]
        for rows in tables:
            blocks.append({"type": "table", "rows": rows})
        if not blocks:
            warnings.append(f"{i}페이지에서 텍스트를 찾지 못함 (이미지 페이지 가능성 — OCR은 후속 지원)")
        pages.append({"page": i, "blocks": blocks})

        parts = [text.strip()] if text.strip() else []
        parts += ["\n".join(" | ".join(r) for r in rows) for rows in tables]
        if parts:
            full_parts.append("\n".join(parts))

    if has_tables:
        parser = f"{parser}+tables"

    return {
        "parser": parser,
        "page_count": len(pages),
        "pages": pages,
        "full_text": "\n\n".join(full_parts),
        "warnings": warnings,
    }


def _with_pdfplumber(content: bytes) -> list[tuple[str, list[list[list[str]]]]] | None:
    """pdfplumber로 페이지별 (표 영역 제외 텍스트, 구조화 표들)을 추출한다.

    pdfplumber 미설치·손상 등은 None을 돌려 상위에서 pypdf로 폴백하게 한다.
    표 영역의 텍스트는 제외해 텍스트 블록과 표 블록이 겹치지 않게 한다.
    """
    try:
        import pdfplumber
    except ImportError:
        return None

    out: list[tuple[str, list[list[list[str]]]]] = []
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                out.append(_plumber_page(page))
    except Exception as e:  # noqa: BLE001 — 손상/암호화 등은 pypdf로 폴백
        logger.warning("pdfplumber 파싱 실패, pypdf로 폴백: %s", e)
        return None
    return out


def _plumber_page(page) -> tuple[str, list[list[list[str]]]]:
    try:
        found = page.find_tables()
    except Exception:  # noqa: BLE001
        found = []
    bboxes = [t.bbox for t in found]

    if bboxes:
        try:
            src = page.filter(lambda obj: _outside_bboxes(obj, bboxes))
        except Exception:  # noqa: BLE001 — 필터 실패 시 전체 텍스트 사용
            src = page
    else:
        src = page
    try:
        text = src.extract_text(layout=True) or ""
    except Exception:  # noqa: BLE001
        text = src.extract_text() or ""
    text = _compress_spaces(text)

    tables = [t for t in (_clean_table(f.extract()) for f in found) if t]
    return text, tables


def _outside_bboxes(obj: dict, bboxes: list[tuple]) -> bool:
    """객체 중심이 어떤 표 bbox에도 들어가지 않으면 True (표 영역 제외용)."""
    cx = (obj["x0"] + obj["x1"]) / 2
    cy = (obj["top"] + obj["bottom"]) / 2
    return not any(x0 <= cx <= x1 and top <= cy <= bottom for x0, top, x1, bottom in bboxes)


def _compress_spaces(text: str) -> str:
    """layout 모드의 과도한 정렬 공백(3칸+)을 2칸으로 압축 — 열 신호는 남기고 토큰 절약."""
    lines = (re.sub(r" {3,}", "  ", line.rstrip()) for line in text.split("\n"))
    return "\n".join(lines)


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
    return _compress_spaces(text)


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
