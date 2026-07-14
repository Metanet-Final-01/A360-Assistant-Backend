"""업로드 저장소 경로 순회(Path Traversal) 방어 테스트.

save()는 사용자 제어 filename을 저장 경로/S3 key에 넣기 전에 마지막 경로 요소로 축소해야
한다 — validate_upload가 경로 구분자를 안 거르므로 여기가 마지막 방어선이다.
"""

from pathlib import Path

import pytest

from app.services import storage
from app.services.storage import _safe_name


@pytest.mark.parametrize("raw, expected", [
    ("report.pdf", "report.pdf"),           # 정상 파일명 무변경
    ("../../evil.txt", "evil.txt"),          # POSIX 순회 제거
    ("..\\..\\evil.txt", "evil.txt"),        # Windows 구분자 순회 제거
    ("a/b/c/deep.txt", "deep.txt"),          # 중첩 경로 → 마지막 요소
    ("/etc/passwd", "passwd"),               # 절대경로 탈출 차단
    ("..", "unnamed"),                        # 정제 후 빈 → 폴백
    ("/", "unnamed"),                         # 구분자만 → 폴백
])
def test_safe_name_strips_path_components(raw, expected):
    assert _safe_name(raw) == expected


def _save_local(tmp_path, monkeypatch, filename):
    """로컬 디스크 분기로 save()를 태우고 반환 경로를 준다(S3 미설정)."""
    monkeypatch.delenv("DOCUMENT_BUCKET", raising=False)
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    return storage.save("sess1", "doc1", filename, b"data")


def test_save_traversal_stays_within_upload_dir(tmp_path, monkeypatch):
    """악성 filename('../../..')이라도 저장 경로가 UPLOAD_DIR 밖으로 나가지 않는다."""
    result = _save_local(tmp_path, monkeypatch, "../../../../evil.txt")
    resolved = Path(result).resolve()
    assert resolved.is_relative_to(tmp_path.resolve())  # 탈출 없음
    assert resolved.name == "evil.txt"
    assert resolved.read_bytes() == b"data"


def test_save_windows_separator_traversal_blocked(tmp_path, monkeypatch):
    """Windows 구분자('..\\')로도 UPLOAD_DIR를 탈출하지 못한다."""
    result = _save_local(tmp_path, monkeypatch, "..\\..\\evil.txt")
    resolved = Path(result).resolve()
    assert resolved.is_relative_to(tmp_path.resolve())
    assert resolved.name == "evil.txt"


def test_save_normal_filename_unchanged(tmp_path, monkeypatch):
    """정상 파일명은 그대로 documents/<sess>/<doc>/<name> 아래 저장된다(기존 동작 무변경)."""
    result = _save_local(tmp_path, monkeypatch, "report.pdf")
    resolved = Path(result).resolve()
    assert resolved == (tmp_path / "documents" / "sess1" / "doc1" / "report.pdf").resolve()
    assert resolved.read_bytes() == b"data"
