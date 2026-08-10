"""타 솔루션 카탈로그 신호 검증 (RPA-285).

판정은 intake LLM이 하고(형식 무관), 이 모듈은 그 판정을 **결정론으로 기각**하는 일만 한다.
그래서 검증의 설계 목표는 여전히 **오탐이 어려울 것**이다 — A360 액션을 붙여넣은 사용자에게
"다른 솔루션 카탈로그를 주셨네요"가 나가면 안 된다. 침묵 케이스를 발화 케이스보다 두껍게
검증하는 이유다.

예전 정규식 긁기(`_PAIR_LINE`)는 제거됐다: 마크다운 표·산문형 카탈로그에서 쌍이 0개로
나와 실사용 형식에서 통째로 침묵했다(실측: 448개 액션 카탈로그 3종 전부 0쌍).
"""

import pytest

from app.agent.v3.orchestrator.foreign_catalog import (
    CatalogSignal,
    detect_solution_name,
    normalize_solution,
    verify,
)


class _Cat:
    """(pkg, act) 쌍이 A360 카탈로그에 실재하는지만 답하는 최소 스텁."""

    def __init__(self, known=()):
        self._known = set(known)

    def get_action_schema(self, package, action):
        return {"package": package, "action": action} if (package, action) in self._known else None


A360_KNOWN = [
    ("Excel advanced", "cloudExcelOpen"),
    ("Excel advanced", "excelAdvancedPackageCloseAction"),
    ("Browser", "browserPackageOpenAction"),
    ("Loop", "cloudUsingLoopAction"),
    ("Error handler", "errorHandlerTry"),
]


def _signal(**kw):
    base = {
        "present": True,
        "solution": "uipath",
        "sample_actions": [
            "UiPath.Excel.Activities/ReadRange",
            "UiPath.Excel.Activities/WriteRange",
            "UiPath.Mail.Activities/SendOutlookMail",
        ],
        "confidence": "high",
    }
    return CatalogSignal(**{**base, **kw})


# ─────────────────────────────────────────────────────────────────────────────
# 발화 — 타 솔루션 카탈로그
# ─────────────────────────────────────────────────────────────────────────────

def test_confirms_foreign_catalog():
    sig = verify(_signal(), _Cat(A360_KNOWN))
    assert sig.found and sig.confirm
    assert sig.samples == 3 and sig.known == 0
    assert sig.solution == "uipath"
    assert "A360" in sig.notice()


def test_solution_name_is_optional():
    """이름을 못 밝혀도 감지된다 — 이름은 안내 문구를 다듬는 용도일 뿐."""
    sig = verify(_signal(solution=None), _Cat(A360_KNOWN))
    assert sig.found and sig.solution is None
    assert "다른 솔루션" in sig.notice()


def test_format_independence():
    """표본만 보므로 원문이 표든 산문이든 무관하다 — 정규식 시절의 미탐이 사라진 지점."""
    sig = verify(
        _signal(solution="power automate",
                sample_actions=["Excel/Launch Excel", "Browser automation/Go to web page"]),
        _Cat(A360_KNOWN),
    )
    assert sig.found and sig.confirm and sig.solution == "power automate"


# ─────────────────────────────────────────────────────────────────────────────
# 침묵·강등 — 오탐 방지 (이쪽이 더 중요하다)
# ─────────────────────────────────────────────────────────────────────────────

def test_silent_when_llm_says_absent():
    assert not verify(_signal(present=False), _Cat(A360_KNOWN)).found
    assert not verify(None, _Cat(A360_KNOWN)).found


def test_rejects_when_samples_are_actually_a360():
    """A360 액션을 옮겨 적은 사용자에게 경고가 나가면 안 된다 — LLM이 틀려도 여기서 막는다."""
    sig = _signal(sample_actions=[
        "Excel advanced/cloudExcelOpen",
        "Browser/browserPackageOpenAction",
        "Loop/cloudUsingLoopAction",
    ])
    assert not verify(sig, _Cat(A360_KNOWN)).found


def test_tolerates_a_few_unknown_samples():
    """일부가 오타·구표기라 실재하지 않아도, 대부분 맞으면 A360으로 본다."""
    sig = _signal(sample_actions=[
        "Excel advanced/cloudExcelOpen",
        "Browser/browserPackageOpenAction",
        "Excel advanced/typoAction",
    ])
    assert not verify(sig, _Cat(A360_KNOWN)).found


def test_low_confidence_notifies_but_does_not_confirm():
    """확신이 낮으면 세션을 바꾸지 않는다 — 고지만 나가 조용한 오답은 여전히 막힌다."""
    sig = verify(_signal(confidence="low"), _Cat(A360_KNOWN))
    assert sig.found and not sig.confirm


def test_no_samples_notifies_but_does_not_confirm():
    """검증할 표본이 없으면 판정을 믿되 확정은 미룬다."""
    sig = verify(_signal(sample_actions=[]), _Cat(A360_KNOWN))
    assert sig.found and not sig.confirm


def test_unsplittable_samples_are_not_counted():
    """슬래시 없는 표기는 쌍으로 셀 수 없다 — 검증 표본에서 빠진다."""
    sig = verify(_signal(sample_actions=["ReadRange", "WriteRange"]), _Cat(A360_KNOWN))
    assert sig.found and sig.samples == 0 and not sig.confirm


def test_catalog_failure_degrades_to_no_signal():
    """카탈로그 조회가 깨져도 감지 실패일 뿐 턴을 죽이지 않는다."""

    class _Broken:
        def get_action_schema(self, package, action):
            raise RuntimeError("DB 연결 실패")

    assert not verify(_signal(), _Broken()).found


# ─────────────────────────────────────────────────────────────────────────────
# 이름 처리
# ─────────────────────────────────────────────────────────────────────────────

def test_solution_name_extraction():
    assert detect_solution_name("우리는 UiPath를 씁니다") == "uipath"
    assert detect_solution_name("파워 오토메이트로 짜줘") == "power automate"
    assert detect_solution_name("A360으로 만들어줘") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Power Automate for desktop", "power automate for desktop"),
        ("  UiPath Studio  ", "uipath studio"),
        ("Blue/Prism!", "blue prism"),
        ("...", None),
        ("", None),
        (None, None),
        ("X" * 80, "x" * 49),
    ],
)
def test_normalize_solution(raw, expected):
    """LLM이 준 이름을 세션 solution 형식(소문자·허용문자·49자)으로 맞춘다.

    맞추지 않으면 백엔드 `_SOLUTION_RE`가 형식 불일치로 버려서, 판정이 맞았는데도
    세션이 안 바뀌는 조용한 유실이 생긴다.
    """
    assert normalize_solution(raw) == expected
