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
    ".docx": (
        b"PK\x03\x04",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
}

# PDF 원문에 이 토큰이 보이면 실행형 콘텐츠 가능성 → 차단
_PDF_BLOCKED_TOKENS = (b"/JavaScript", b"/Launch", b"/EmbeddedFile")

# OOXML 종류별 필수 파트 — 다른 OOXML(xlsx 등)을 확장자만 바꿔 위장하는 것을 차단
_OOXML_REQUIRED = {"PPTX": "ppt/presentation.xml", "DOCX": "word/document.xml"}

# 압축 해제 총량 상한 (ZIP bomb DoS 방지) — 원본 크기 제한(max_mb)과 별개로 검사
_MAX_UNCOMPRESSED = 400 * 1024 * 1024


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def validate_upload(filename: str, content: bytes, max_mb: int) -> str:
    """검증 통과 시 표준 content_type을 반환한다."""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _ALLOWED:
        raise _error(
            400,
            "INVALID_FILE_TYPE",
            f"지원하지 않는 형식입니다: {ext or '확장자 없음'} (PDF/PPTX/DOCX만 가능)",
        )

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
        _check_ooxml(content, "PPTX")
    elif ext == ".docx":
        _check_ooxml(content, "DOCX")
    return content_type


def _check_pdf(content: bytes) -> None:
    for token in _PDF_BLOCKED_TOKENS:
        if token in content:
            raise _error(
                400,
                "SUSPICIOUS_FILE",
                f"보안상 허용되지 않는 요소({token.decode()})가 포함된 PDF입니다.",
            )


def _check_ooxml(content: bytes, kind: str) -> None:
    """OOXML(zip 기반 PPTX/DOCX) 공통 검증 — zip 구조·종류별 필수 파트·매크로·ZIP bomb."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            infos = zf.infolist()
            names = [zi.filename for zi in infos]
            uncompressed = sum(zi.file_size for zi in infos)
    except zipfile.BadZipFile:
        raise _error(400, "CORRUPTED_FILE", f"손상된 {kind} 파일입니다.")
    if "[Content_Types].xml" not in names:
        raise _error(400, "FILE_TYPE_MISMATCH", f"올바른 {kind} 구조가 아닙니다.")
    required = _OOXML_REQUIRED[kind]
    if required not in names:  # xlsx 등 다른 OOXML을 확장자만 바꿔 위장 차단
        raise _error(400, "FILE_TYPE_MISMATCH", f"올바른 {kind} 구조가 아닙니다.")
    if any(n.endswith("vbaProject.bin") for n in names):  # 매크로 포함(.pptm/.docm 위장) 차단
        raise _error(400, "SUSPICIOUS_FILE", "매크로가 포함된 문서는 업로드할 수 없습니다.")
    if uncompressed > _MAX_UNCOMPRESSED:  # 작은 파일이 거대하게 풀리는 ZIP bomb 차단
        raise _error(400, "SUSPICIOUS_FILE", "압축 해제 크기가 비정상적으로 큰 문서입니다.")
