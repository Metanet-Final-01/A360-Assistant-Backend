"""업로드 파일 검증 (FR-01 + 추가기능: 악성 파일 가능성 검사).

검사 순서: 확장자 화이트리스트 → 크기 → 매직바이트(확장자 위조 방지) → 형식별 위험 요소.
실패 시 HTTPException(400/413)을 던진다. detail은 {code, message} 표준 포맷.
"""

import io
import zipfile

from fastapi import HTTPException

# 확장자 → (매직바이트, 표준 content_type)
_ALLOWED: dict[str, tuple[bytes, str]] = {
    ".pdf": (b"%PDF-", "application/pdf"),
    ".pptx": (
        b"PK\x03\x04",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
}

# PDF 원문에 이 토큰이 보이면 실행형 콘텐츠 가능성 → 차단
_PDF_BLOCKED_TOKENS = (b"/JavaScript", b"/Launch", b"/EmbeddedFile")


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def validate_upload(filename: str, content: bytes, max_mb: int) -> str:
    """검증 통과 시 표준 content_type을 반환한다."""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _ALLOWED:
        raise _error(400, "INVALID_FILE_TYPE", f"지원하지 않는 형식입니다: {ext or '확장자 없음'} (PDF/PPTX만 가능)")

    if len(content) == 0:
        raise _error(400, "EMPTY_FILE", "빈 파일입니다.")
    if len(content) > max_mb * 1024 * 1024:
        raise _error(413, "FILE_TOO_LARGE", f"파일이 너무 큽니다 (최대 {max_mb}MB).")

    magic, content_type = _ALLOWED[ext]
    if not content.startswith(magic):
        raise _error(400, "FILE_TYPE_MISMATCH", "확장자와 실제 파일 형식이 다릅니다.")

    if ext == ".pdf":
        _check_pdf(content)
    elif ext == ".pptx":
        _check_pptx(content)
    return content_type


def _check_pdf(content: bytes) -> None:
    for token in _PDF_BLOCKED_TOKENS:
        if token in content:
            raise _error(
                400,
                "SUSPICIOUS_FILE",
                f"보안상 허용되지 않는 요소({token.decode()})가 포함된 PDF입니다.",
            )


def _check_pptx(content: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile:
        raise _error(400, "CORRUPTED_FILE", "손상된 PPTX 파일입니다.")
    if "[Content_Types].xml" not in names:
        raise _error(400, "FILE_TYPE_MISMATCH", "올바른 PPTX 구조가 아닙니다.")
    if any(n.endswith("vbaProject.bin") for n in names):  # 매크로 포함(.pptm 위장) 차단
        raise _error(400, "SUSPICIOUS_FILE", "매크로가 포함된 프레젠테이션은 업로드할 수 없습니다.")
