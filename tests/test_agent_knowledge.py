"""공용 지식층 테스트 (RPA-298).

핵심은 **표기 세대 내성**이다. 이 지식층이 존재하는 이유가 "카탈로그 재적재로 어휘가
바뀔 때마다 검사가 조용히 죽는 것"을 막기 위해서이므로, 세대가 달라도 같은 판정이
나오는지를 못 박는다.
"""

from app.agent.knowledge import lexicon


# ─────────────────────────────────────────────────────────────────────────────
# 표기 세대 내성 — 이 지식층의 존재 이유
# ─────────────────────────────────────────────────────────────────────────────

def test_eh_role_survives_all_notation_generations():
    """구 봇 JSON(`try`)·JAR(`errorHandlerTry`)·문서 표시명(`Try`)이 같은 역할로 판정된다."""
    for name in ("try", "errorHandlerTry", "Try", "Error handler Try"):
        assert lexicon.eh_role(name) == "try", name
    for name in ("catch", "errorHandlerCatch", "Catch"):
        assert lexicon.eh_role(name) == "catch", name
    for name in ("finally", "errorHandlerFinally", "Finally"):
        assert lexicon.eh_role(name) == "finally", name


def test_eh_role_detects_throw_without_colliding_with_try():
    """Throw는 본문 없는 제어 신호다 — 'try'와 부분 문자열이 겹치지 않는지 확인."""
    assert lexicon.eh_role("errorHandlerThrow") == "throw"
    assert lexicon.eh_role("Throw") == "throw"
    assert lexicon.eh_role("무관한액션") == "other"


def test_if_role_prefers_elseif_over_else():
    """순서를 뒤집으면 elseIf가 else로 잡혀 분기가 뒤집힌다 — 회귀 방지."""
    assert lexicon.if_role("elseIf") == "elseif"
    assert lexicon.if_role("else_if") == "elseif"
    assert lexicon.if_role("ifPackageElseIfAction") == "elseif"
    assert lexicon.if_role("else") == "else"
    assert lexicon.if_role("if") == "if"
    assert lexicon.if_role(None) == "if"


def test_loop_signal_role():
    assert lexicon.loop_signal_role("loopPackageBreakAction") == "break"
    assert lexicon.loop_signal_role("Continue") == "continue"
    assert lexicon.loop_signal_role("cloudUsingLoopAction") is None


def test_normalize_package_collapses_display_variants():
    keys = {
        lexicon.normalize_package(n)
        for n in ("Excel advanced", "Excel advanced 패키지", "excel  advanced", "Excel_advanced")
    }
    assert len(keys) == 1, f"같은 패키지가 다른 키로 갈렸다: {keys}"
    assert lexicon.normalize_package(None) == ""


# ─────────────────────────────────────────────────────────────────────────────
# is_container — non_container 주입이 v3의 stale 상수 문제를 푸는지
# ─────────────────────────────────────────────────────────────────────────────

def test_is_container_by_package():
    assert lexicon.is_container("Loop", "cloudUsingLoopAction")
    assert lexicon.is_container("Error handler", "errorHandlerTry")
    assert not lexicon.is_container("Excel advanced", "cloudExcelOpen")


def test_is_container_default_has_no_exceptions():
    """기본값이 빈 집합인 게 설계 의도다 — v3는 수기 3쌍을 들고 있었는데 전부 카탈로그에
    부재라 Break/Continue가 컨테이너로 오판됐다. 예외는 호출부가 유도해서 넣는다."""
    assert lexicon.is_container("Loop", "loopPackageBreakAction") is True


def test_is_container_honours_injected_exceptions():
    exceptions = frozenset({("Loop", "Break"), ("Error handler", "Throw")})
    assert not lexicon.is_container("Loop", "Break", non_container=exceptions)
    assert not lexicon.is_container("Error handler", "Throw", non_container=exceptions)
    # 예외 목록에 없는 컨테이너 액션은 그대로 컨테이너
    assert lexicon.is_container("Loop", "cloudUsingLoopAction", non_container=exceptions)


def test_is_attended():
    assert lexicon.is_attended("MessageBox")
    assert lexicon.is_attended("Message Box")
    assert lexicon.is_attended("Prompt")
    assert not lexicon.is_attended("Excel advanced")


def test_var_ref_regex():
    assert lexicon.VAR_REF_RE.findall("경로는 $sPath$ 이고 개수는 $nCount$") == ["sPath", "nCount"]
    assert lexicon.VAR_REF_RE.findall("달러 $100 표기") == []


# ─────────────────────────────────────────────────────────────────────────────
# 격리 계약
# ─────────────────────────────────────────────────────────────────────────────

def test_knowledge_layer_does_not_touch_infrastructure():
    """이 층은 catalog/retriever를 인자로 받을 뿐 자체 팩토리를 두지 않는다.

    자체 팩토리를 두면 conftest 스텁 대상이 되고(안 덮으면 테스트가 운영 인프라를 때린다)
    DB 소유권을 서비스 계층에 두는 INTERFACES 계약도 깨진다.
    """
    import ast
    from pathlib import Path

    # 문자열 검색이 아니라 AST로 본다 — 이 파일들의 docstring이 왜 그러면 안 되는지를
    # 산문으로 설명하고 있어서, 순진한 grep은 설명문 자체를 위반으로 잡는다.
    banned_attrs = {("os", "getenv"), ("os", "environ")}
    banned_names = {"get_backend_catalog", "get_retriever", "get_hybrid_retriever"}

    root = Path(__file__).resolve().parent.parent / "app" / "agent" / "knowledge"
    offenders: list[str] = []
    for p in root.rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if (node.value.id, node.attr) in banned_attrs:
                    offenders.append(f"{p.relative_to(root)}:{node.lineno} {node.value.id}.{node.attr}")
            elif isinstance(node, ast.Name) and node.id in banned_names:
                offenders.append(f"{p.relative_to(root)}:{node.lineno} {node.id}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    if alias.name in banned_names:
                        offenders.append(f"{p.relative_to(root)}:{node.lineno} import {alias.name}")
    assert not offenders, f"지식층이 인프라에 직접 붙었다: {offenders}"


def test_knowledge_layer_not_imported_by_legacy_versions():
    """v1~v3는 지식층을 쓰지 않는다 — 비교 셀렉터가 오염되면 버전 비교가 무의미해진다."""
    from pathlib import Path

    agent_root = Path(__file__).resolve().parent.parent / "app" / "agent"
    offenders = [
        f"{p.relative_to(agent_root)}:{i}"
        for ver in ("v1", "v2", "v3")
        for p in (agent_root / ver).rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "agent.knowledge" in line or "from ..knowledge" in line
    ]
    assert not offenders, f"레거시 버전이 지식층을 import한다: {offenders}"
