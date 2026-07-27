"""공용 지식층 테스트 (RPA-298).

핵심은 **표기 세대 내성**이다. 이 지식층이 존재하는 이유가 "카탈로그 재적재로 어휘가
바뀔 때마다 검사가 조용히 죽는 것"을 막기 위해서이므로, 세대가 달라도 같은 판정이
나오는지를 못 박는다.
"""

import pytest

from app.agent.knowledge import derive, lexicon


class _StubCatalog:
    """iter_action_schemas만 있는 최소 카탈로그 — 유도층은 이 인터페이스만 쓴다."""

    def __init__(self, specs):
        self._specs = list(specs)

    def iter_action_schemas(self):
        return list(self._specs)


def _spec(package, action, *, session_param=False):
    params = [{"name": "Session name", "type": "SESSION"}] if session_param else []
    return {"package": package, "action": action, "parameters": params}


@pytest.fixture(autouse=True)
def _clear_derive_cache():
    """유도 캐시를 앞뒤로 비운다 — 이 파일은 같은 카탈로그 객체에 세대별 스펙을 갈아 끼운다.

    (예전에는 캐시가 catalog 객체 **id** 기준이라 테스트 간 주소 재사용이 오염을 만들어서
    이 fixture가 그 방어까지 겸했다. 지금은 약한 참조 키라 그 위험은 없다 —
    `test_유도_캐시는_주소_재사용에_오염되지_않는다` 참조.)
    """
    derive.clear_cache()
    yield
    derive.clear_cache()


def test_유도_캐시는_주소_재사용에_오염되지_않는다():
    """🔴 조용한 오답 — 새 카탈로그가 **죽은 카탈로그의 어휘**를 그대로 받던 버그.

    캐시 키가 `id(catalog)`였고 CPython은 수거된 객체의 주소를 재사용한다. 이 캐시가 담는
    것은 세션 opener/closer 레지스트리와 제어 흐름 액션 전량이라, 오염되면 R7/R8/R17 판정과
    수리 어휘가 통째로 남의 것이 된다. 세션마다 만들어졌다 LRU에서 밀려나는 `OverlayCatalog`가
    실제로 그 조건을 만든다.

    실측 서명(2026-07-27): 전체 스위트에서만 `Error handler/Try`가 수리 메뉴에서 사라졌다 —
    파일 단독 실행은 통과해서 원인을 짚기 전까지 '테스트 순서 문제'로 보였다.

    주소 재사용 자체는 재현이 확률적이라, 그것을 **불가능하게 만드는 불변식**을 직접 못
    박는다: 카탈로그가 죽으면 그 캐시 항목도 함께 죽는다. 남은 항목이 없으면 재사용된
    주소가 무엇을 가리키든 오염될 것이 없다.
    """
    import gc

    dead = _StubCatalog([_spec("Error handler", "errorHandlerTry")])
    assert derive.derive_structural_actions(dead) == (("Error handler", "errorHandlerTry"),)
    assert len(derive._CACHE) == 1

    del dead
    gc.collect()
    assert len(derive._CACHE) == 0, "카탈로그와 함께 사라져야 주소 재사용이 무해해진다"

    fresh = _StubCatalog([_spec("Loop", "cloudUsingLoopAction")])
    assert derive.derive_structural_actions(fresh) == (("Loop", "cloudUsingLoopAction"),)


def test_약한_참조가_안_되는_카탈로그도_유도된다():
    """캐시를 못 붙이는 객체(__slots__에 __weakref__ 없음)는 매번 만든다 — 느려도 맞다.

    캐시를 못 다는 것을 예외로 흘리면 그 카탈로그로는 검사가 통째로 죽는다.
    """
    class Slotted:
        __slots__ = ()

        def iter_action_schemas(self):
            return [_spec("Loop", "cloudUsingLoopAction")]

    assert derive.derive_structural_actions(Slotted()) == (("Loop", "cloudUsingLoopAction"),)


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


# ─────────────────────────────────────────────────────────────────────────────
# derive — 세션 어휘 유도
# ─────────────────────────────────────────────────────────────────────────────

def test_session_gating_excludes_non_session_packages():
    """게이팅이 없으면 File/Open·Folder/Open이 opener로 오검출된다 (실측 4건)."""
    catalog = _StubCatalog([
        _spec("Excel advanced", "Open", session_param=True),
        _spec("Excel advanced", "Close", session_param=True),
        _spec("File", "Open"),            # 세션 파라미터 없음 — opener 아님
        _spec("Folder", "Open"),
    ])
    r = derive.derive_session_registry(catalog)
    assert ("Excel advanced", "Open") in r.openers
    assert ("File", "Open") not in r.openers
    assert ("Folder", "Open") not in r.openers
    assert r.session_packages == frozenset({"Excel advanced"})


def test_openai_prefix_is_not_an_opener():
    """'OpenAI: Chat AI'는 Open으로 시작하지만 세션 opener가 아니다 — \\b 가드 회귀 방지.

    실측: 이 가드가 없으면 Generative AI 패키지의 OpenAI 액션 6건이 opener로 잡힌다.
    """
    catalog = _StubCatalog([
        _spec("Generative AI", "OpenAI: Chat AI", session_param=True),
        _spec("Generative AI", "Connect", session_param=True),
        _spec("Generative AI", "Disconnect", session_param=True),
    ])
    r = derive.derive_session_registry(catalog)
    assert ("Generative AI", "OpenAI: Chat AI") not in r.openers
    assert ("Generative AI", "Connect") in r.openers


def test_empty_catalog_falls_back_to_constants():
    """순회를 지원하지 않는 스텁에서는 수기 상수를 그대로 쓴다 — 지금 수준 유지."""
    fallback_open = frozenset({("Excel advanced", "cloudExcelOpen")})
    fallback_close = frozenset({("Excel advanced", "cloudExcelClose")})
    r = derive.derive_session_registry(
        object(), fallback_openers=fallback_open, fallback_closers=fallback_close
    )
    assert r.source == "constants"
    assert r.openers == fallback_open and r.closers == fallback_close
    assert r.usable


def test_no_signal_and_no_fallback_is_empty_and_unusable():
    r = derive.derive_session_registry(object())
    assert r.source == "empty" and not r.usable


def test_one_sided_derivation_is_unusable():
    """opener만 있고 closer가 비면 모든 열기가 미종료로 잡힌다 — 전량 오탐이므로 침묵한다.

    지금 v3가 정확히 반대 상황(opener 공집합·closer 3건)이라 R7이 대량 오탐 중이다.
    """
    catalog = _StubCatalog([_spec("Excel advanced", "Open", session_param=True)])
    r = derive.derive_session_registry(catalog)
    assert r.openers and not r.closers
    assert not r.usable, "한쪽만 유도됐는데 usable — R7/R8이 전량 오탐을 낸다"


def test_partial_derivation_mixes_fallback_for_missing_side():
    catalog = _StubCatalog([_spec("Excel advanced", "Open", session_param=True)])
    r = derive.derive_session_registry(
        catalog, fallback_closers=frozenset({("Excel advanced", "cloudExcelClose")})
    )
    assert r.source == "mixed" and r.usable


def test_derive_container_exceptions_from_live_names():
    """v3의 수기 3쌍(loopPackageBreakAction 등)은 현행 카탈로그에 부재다 — 실재 이름을 뽑는다."""
    catalog = _StubCatalog([
        _spec("Loop", "Break"),
        _spec("Loop", "Continue"),
        _spec("Loop", "Loop action for data iteration"),
        _spec("Error handler", "Throw"),
        _spec("Error handler", "Try"),
        _spec("Excel advanced", "Break time"),   # 컨테이너 패키지가 아니므로 제외
    ])
    exc = derive.derive_container_exceptions(catalog)
    assert exc == frozenset({("Loop", "Break"), ("Loop", "Continue"), ("Error handler", "Throw")})
    # 주입하면 컨테이너 판정이 뒤집힌다
    assert not lexicon.is_container("Loop", "Break", non_container=exc)
    assert lexicon.is_container("Loop", "Loop action for data iteration", non_container=exc)


def test_derive_structural_actions_enumerates_control_flow():
    catalog = _StubCatalog([
        _spec("Loop", "Loop action for data iteration"),
        _spec("If", "If"),
        _spec("Error handler", "Try"),
        _spec("Excel advanced", "Open"),
    ])
    actions = derive.derive_structural_actions(catalog)
    assert ("Excel advanced", "Open") not in actions
    assert ("Loop", "Loop action for data iteration") in actions
    assert list(actions) == sorted(actions), "결정론 정렬이 아니다"


def test_derive_survives_broken_catalog():
    """유도 실패가 턴을 죽이면 안 된다 — 폴백이 받는다."""

    class Broken:
        def iter_action_schemas(self):
            raise RuntimeError("DB 연결 끊김")

    r = derive.derive_session_registry(
        Broken(), fallback_openers=frozenset({("A", "b")}), fallback_closers=frozenset({("A", "c")})
    )
    assert r.source == "constants" and r.usable


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
