"""업로드 검증 (FR-01 + 악성 파일 검사) 단위 테스트."""

import io
import zipfile

import pytest
from fastapi import HTTPException

from app.services.upload_security import validate_upload


def _make_zip(names: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, "x")
    return buf.getvalue()


def _code(exc_info) -> str:
    return exc_info.value.detail["code"]


def test_pdf_ok():
    assert validate_upload("doc.pdf", b"%PDF-1.7 hello", 20) == "application/pdf"


def test_pptx_ok():
    content = _make_zip(["[Content_Types].xml", "ppt/slides/slide1.xml"])
    assert "presentation" in validate_upload("deck.pptx", content, 20)


def test_rejects_unknown_extension():
    with pytest.raises(HTTPException) as e:
        validate_upload("run.exe", b"MZ....", 20)
    assert _code(e) == "INVALID_FILE_TYPE"


def test_rejects_magic_mismatch():
    # 확장자는 pdf인데 내용물은 zip — 확장자 위조
    with pytest.raises(HTTPException) as e:
        validate_upload("fake.pdf", b"PK\x03\x04....", 20)
    assert _code(e) == "FILE_TYPE_MISMATCH"


def test_rejects_oversize():
    with pytest.raises(HTTPException) as e:
        validate_upload("big.pdf", b"%PDF-" + b"0" * (1024 * 1024 + 1), 1)
    assert e.value.status_code == 413


def test_rejects_pdf_with_javascript():
    with pytest.raises(HTTPException) as e:
        validate_upload("evil.pdf", b"%PDF-1.7 /JavaScript (alert)", 20)
    assert _code(e) == "SUSPICIOUS_FILE"


def test_rejects_pptx_with_macro():
    content = _make_zip(["[Content_Types].xml", "ppt/vbaProject.bin"])
    with pytest.raises(HTTPException) as e:
        validate_upload("macro.pptx", content, 20)
    assert _code(e) == "SUSPICIOUS_FILE"


def test_rejects_corrupted_pptx():
    with pytest.raises(HTTPException) as e:
        validate_upload("broken.pptx", b"PK\x03\x04 not a real zip", 20)
    assert _code(e) == "CORRUPTED_FILE"
