"""중앙 설정 레지스트리 래칫 (RPA-224).

목표는 "새 환경변수의 산재를 코드로 막는 것"이다 — 규약("config.py에 선언하세요")만으로는
지켜지지 않는다(가드는 코드여야 한다는 이 레포의 반복된 교훈). 두 래칫:
  ① app/ 어디서든 참조되는 env 키는 REGISTRY에 선언돼 있어야 한다
  ② os.getenv를 직접 부르는 파일은 현재 화이트리스트에서 늘어나면 안 된다
     (기존 26개 파일은 후속 단계에서 config 경유로 줄여간다 — 래칫은 한 방향)
"""

import ast
from pathlib import Path

import pytest

from app.core import config

ROOT = Path(__file__).resolve().parent.parent / "app"

# ② 의 화이트리스트 — 2026-07-21 기준 env를 직접 읽는 파일(리터럴·변수 키 포함).
#    여기서 파일을 **빼는** 변경(마이그레이션)은 환영, **넣는** 변경은 래칫 위반이다.
_DIRECT_GETENV_ALLOWED = {
    "agent/registry.py", "agent/v1/config.py", "agent/v2/config.py", "agent/v3/config.py",
    "api/admin.py", "api/assurance_writer.py", "api/auth.py", "api/debug.py",
    "api/documents.py", "api/sessions.py",
    "core/llm.py", "core/observability_db.py", "core/scheduler.py",
    "db.py", "main.py",
    "rag/config.py", "rag/event_queue.py", "rag/pipeline.py", "rag/sources/control_room.py",
    "services/alerts.py", "services/budget.py", "services/rollup.py",
    "services/parser/pdf.py", "services/parser/ppt.py",
    "services/parser/vision.py", "services/rag_cache.py",
    "core/config.py",  # 레지스트리 자신
}
# core/security.py·services/storage.py는 1단계에서 config 경유로 마이그레이션돼 목록에서 빠졌다.

# 변수/비리터럴 키로 env를 읽는 파일 — 스캐너가 키를 **정적으로 못 본다**. 그 파일이 읽는
# 키가 REGISTRY에 선언됐는지는 코드 리뷰로 보장하고, 여기 명시 등록한다(RPA-224 Qodo 반영:
# 정규식이 os.environ[]·변수 키를 놓쳐 래칫이 뚫려 있었다 — AST로 바꾸고 이 목록으로 막는다).
_DYNAMIC_KEY_FILES = {
    "core/config.py",      # 레지스트리 구현 자체 — os.getenv(key)
    "services/alerts.py",  # _threshold(name) — ALERT_GLOBAL_DAILY_USD·ALERT_5XX_DAILY
    "services/budget.py",  # _limit(name) — BUDGET_*_USD 4종
    "services/rollup.py",  # _RETENTION env_key — *_RETENTION_DAYS 4종
}


def _is_os_environ(node: ast.AST) -> bool:
    """node가 `os.environ`인가."""
    return (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id == "os")


def _env_access(node: ast.AST):
    """env 접근이면 (리터럴키_or_None, is_dynamic), 아니면 None.

    잡는 형태: os.getenv("K")/os.getenv(var), os.environ.get("K"), os.environ["K"]/os.environ[var].
    리터럴 문자열 키는 그 값을, 변수/비리터럴 키는 (None, True)를 돌려준다.
    """
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        f = node.func
        is_getenv = f.attr == "getenv" and isinstance(f.value, ast.Name) and f.value.id == "os"
        is_environ_get = f.attr == "get" and _is_os_environ(f.value)
        if is_getenv or is_environ_get:
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                return node.args[0].value, False
            return None, True  # 인자 없음/변수 키
    if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
        sl = node.slice
        if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
            return sl.value, False
        return None, True
    return None


def _scan() -> tuple[dict[str, set[str]], set[str]]:
    """app/ 전체를 AST로 훑어 env 접근을 수집한다.

    반환: (리터럴키→{파일}, 변수키로 읽는 파일 집합).
    정규식 대신 AST를 쓰는 이유(Qodo 반영): 정규식은 os.environ[]와 변수 키를 못 잡아
    래칫이 뚫려 있었다(LLM_*_COST_PER_1M·*_RETENTION_DAYS 등이 미선언인데 통과).
    """
    literal: dict[str, set[str]] = {}
    dynamic: set[str] = set()
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            hit = _env_access(node)
            if hit is None:
                continue
            key, is_dynamic = hit
            if key is not None:
                literal.setdefault(key, set()).add(rel)
            elif is_dynamic:
                dynamic.add(rel)
    return literal, dynamic


def test_every_referenced_key_is_declared():
    """①: 리터럴로 참조되는 모든 키가 REGISTRY에 선언돼 있다 — 새 키는 선언부터."""
    literal, _ = _scan()
    undeclared = {k: sorted(files) for k, files in literal.items() if k not in config.REGISTRY}
    assert not undeclared, (
        f"REGISTRY에 미선언된 환경변수: {undeclared}\n"
        "app/core/config.py에 EnvSpec으로 선언하세요 (기본값·그룹·설명 포함)."
    )


def test_direct_getenv_files_do_not_grow():
    """②: env를 직접 읽는 파일이 늘지 않는다 — 새 코드는 config 경유."""
    literal, dynamic = _scan()
    current = {f for files in literal.values() for f in files} | dynamic
    new_files = current - _DIRECT_GETENV_ALLOWED
    assert not new_files, (
        f"새 파일이 env를 직접 읽습니다: {sorted(new_files)}\n"
        "`from app.core import config` 로 레지스트리를 경유하세요 (RPA-224)."
    )


def test_dynamic_key_access_files_are_known():
    """③(Qodo 반영): 변수 키로 env를 읽는 파일은 미리 등록된 것뿐이어야 한다.

    스캐너가 변수 키의 실제 값을 못 보므로, 그런 파일이 새로 생기면 그 파일이 읽는 키가
    REGISTRY에 있는지 자동 검증이 불가능하다 — 목록으로 막아 코드 리뷰를 강제한다.
    """
    _, dynamic = _scan()
    new_files = dynamic - _DYNAMIC_KEY_FILES
    assert not new_files, (
        f"변수 키로 env를 읽는 새 파일: {sorted(new_files)}\n"
        "읽는 키를 REGISTRY에 선언하고 이 목록(_DYNAMIC_KEY_FILES)에 추가하세요."
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
