"""중앙 설정 레지스트리 래칫 (RPA-224).

목표는 "새 환경변수의 산재를 코드로 막는 것"이다 — 규약("config.py에 선언하세요")만으로는
지켜지지 않는다(가드는 코드여야 한다는 이 레포의 반복된 교훈). 두 래칫:
  ① app/ 어디서든 참조되는 env 키는 REGISTRY에 선언돼 있어야 한다
  ② os.getenv를 직접 부르는 파일은 현재 화이트리스트에서 늘어나면 안 된다
     (기존 26개 파일은 후속 단계에서 config 경유로 줄여간다 — 래칫은 한 방향)
"""

import re
from pathlib import Path

import pytest

from app.core import config

ROOT = Path(__file__).resolve().parent.parent / "app"
_GETENV = re.compile(r"os\.(?:getenv|environ\.get)\(\s*['\"]([A-Z0-9_]+)['\"]")

# ② 의 화이트리스트 — 2026-07-21 기준 os.getenv 직접 호출이 남아 있는 파일.
#    여기서 파일을 **빼는** 변경(마이그레이션)은 환영, **넣는** 변경은 래칫 위반이다.
_DIRECT_GETENV_ALLOWED = {
    "agent/registry.py", "agent/v1/config.py", "agent/v2/config.py", "agent/v3/config.py",
    "api/admin.py", "api/assurance_writer.py", "api/auth.py", "api/debug.py",
    "api/documents.py", "api/sessions.py",
    "core/llm.py", "core/observability_db.py", "core/scheduler.py",
    "db.py", "main.py",
    "rag/config.py", "rag/event_queue.py", "rag/pipeline.py", "rag/sources/control_room.py",
    "services/alerts.py", "services/parser/pdf.py", "services/parser/ppt.py",
    "services/parser/vision.py", "services/rag_cache.py",
    "core/config.py",  # 레지스트리 자신
}
# core/security.py·services/storage.py는 1단계에서 config 경유로 마이그레이션돼 목록에서 빠졌다.


def _scan() -> dict[str, set[str]]:
    """app/ 전체의 os.getenv 참조를 {키: {파일}}로 수집한다."""
    found: dict[str, set[str]] = {}
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        for m in _GETENV.finditer(p.read_text(encoding="utf-8", errors="replace")):
            found.setdefault(m.group(1), set()).add(rel)
    return found


def test_every_referenced_key_is_declared():
    """①: 참조되는 모든 키가 REGISTRY에 선언돼 있다 — 새 키는 선언부터."""
    undeclared = {k: sorted(files) for k, files in _scan().items() if k not in config.REGISTRY}
    assert not undeclared, (
        f"REGISTRY에 미선언된 환경변수: {undeclared}\n"
        "app/core/config.py에 EnvSpec으로 선언하세요 (기본값·그룹·설명 포함)."
    )


def test_direct_getenv_files_do_not_grow():
    """②: os.getenv 직접 호출 파일이 늘지 않는다 — 새 코드는 config 경유."""
    current = {f for files in _scan().values() for f in files}
    new_files = current - _DIRECT_GETENV_ALLOWED
    assert not new_files, (
        f"새 파일이 os.getenv를 직접 호출합니다: {sorted(new_files)}\n"
        "`from app.core import config` 로 레지스트리를 경유하세요 (RPA-224)."
    )


def test_get_reads_at_access_time(monkeypatch):
    """호출 시점 읽기 계약 — monkeypatch.setenv가 즉시 보여야 한다.

    이 성질이 깨지면(임포트 시점 캐시 도입 등) 토글 스크립트·conftest 격리·통합 테스트의
    DATABASE_NAME 전환이 전부 무너진다. 레지스트리의 존재 이유 절반이 이 테스트다.
    """
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_MINUTES", "5")
    assert config.ACCESS_TOKEN_EXPIRE_MINUTES == 5
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_MINUTES", "7")
    assert config.ACCESS_TOKEN_EXPIRE_MINUTES == 7


def test_cast_and_default(monkeypatch):
    monkeypatch.delenv("REFRESH_TOKEN_EXPIRE_DAYS", raising=False)
    assert config.REFRESH_TOKEN_EXPIRE_DAYS == 14  # 선언된 기본값 + int cast
    monkeypatch.setenv("SECURE_COOKIES", "false")
    assert config.SECURE_COOKIES is False


def test_empty_string_means_unset(monkeypatch):
    """빈 문자열 = 미설정 — 기존 산재 호출부의 `or` 폴백 의미론을 유지한다."""
    monkeypatch.setenv("OPENSEARCH_HOST", "")
    assert config.OPENSEARCH_HOST is None  # default=None → 부재
    monkeypatch.setenv("DATABASE_PORT", "")
    assert config.DATABASE_PORT == "5432"  # 빈 값이면 선언된 기본값


def test_unknown_key_raises():
    """미선언 키는 조용한 None이 아니라 즉시 예외 — 오타가 무음 폴백이 되지 않게."""
    with pytest.raises(AttributeError):
        _ = config.NO_SUCH_KEY_EVER
    with pytest.raises(KeyError):
        config.get("NO_SUCH_KEY_EVER")


def test_startup_report_lists_unset_warn_keys(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_HOST", "")
    missing = config.startup_report()
    assert "OPENSEARCH_HOST" in missing
    monkeypatch.setenv("OPENSEARCH_HOST", "https://example.com")
    assert "OPENSEARCH_HOST" not in config.startup_report()
