"""타 솔루션 카탈로그 감지 (RPA-285 0단계).

이 감지의 설계 목표는 **오탐이 어려울 것**이다 — A360 액션을 붙여넣은 사용자에게
"다른 솔루션 카탈로그를 주셨네요"가 나가면 안 된다. 미탐(못 잡음)은 현재 동작이라
손해가 없으므로 허용한다. 그래서 침묵 케이스를 발화 케이스보다 두껍게 검증한다.
"""

from app.agent.v3.orchestrator.foreign_catalog import (
    MIN_PAIRS,
    detect,
    detect_solution_name,
    user_text,
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

UIPATH_PASTE = """우리는 UiPath를 씁니다. 액션 목록이에요:
- UiPath.Excel.Activities/ReadRange
- UiPath.Excel.Activities/WriteRange
- UiPath.Mail.Activities/SendOutlookMail
- UiPath.Core.Activities/MessageBox
이걸로 흐름도 만들어줘
"""

A360_PASTE = """이 액션들 써서 만들어줘
- Excel advanced/cloudExcelOpen
- Excel advanced/excelAdvancedPackageCloseAction
- Browser/browserPackageOpenAction
- Loop/cloudUsingLoopAction
"""


def _state(message="", history=None):
    return {"message": message, "history": history or []}


# ─────────────────────────────────────────────────────────────────────────────
# 발화 — 타 솔루션 카탈로그
# ─────────────────────────────────────────────────────────────────────────────

def test_detects_foreign_catalog_paste():
    sig = detect(_state(UIPATH_PASTE), _Cat(A360_KNOWN))
    assert sig.found
    assert sig.pairs >= MIN_PAIRS
    assert sig.known == 0
    assert sig.solution == "uipath"
    assert "A360" in sig.notice()


def test_detects_catalog_pasted_in_earlier_turn():
    """카탈로그는 이전 턴에 붙여넣고 이번 턴엔 '만들어줘'만 올 수 있다."""
    history = [{"role": "user", "content": UIPATH_PASTE}, {"role": "assistant", "content": "확인했어요"}]
    assert detect(_state("이제 흐름도 만들어줘", history), _Cat(A360_KNOWN)).found


def test_solution_name_is_optional():
    """솔루션 이름을 안 밝혀도 감지된다 — 이름은 안내 문구를 다듬는 용도일 뿐."""
    paste = "\n".join(f"- Foo.Bar.Activities/DoThing{i}" for i in range(4))
    sig = detect(_state(paste), _Cat(A360_KNOWN))
    assert sig.found and sig.solution is None
    assert "다른 솔루션" in sig.notice()


# ─────────────────────────────────────────────────────────────────────────────
# 침묵 — 오탐 방지 (이쪽이 더 중요하다)
# ─────────────────────────────────────────────────────────────────────────────

def test_silent_when_user_pastes_a360_actions():
    """A360 액션을 옮겨 적은 사용자에게 경고가 나가면 안 된다."""
    assert not detect(_state(A360_PASTE), _Cat(A360_KNOWN)).found


def test_silent_on_a360_paste_with_a_few_typos():
    """일부가 오타·구표기라 실재하지 않아도, 대부분 맞으면 A360으로 본다."""
    paste = A360_PASTE + "- Excel advanced/typoAction\n"
    assert not detect(_state(paste), _Cat(A360_KNOWN)).found


def test_silent_below_minimum_pairs():
    """지나가는 언급 한두 개는 목록이 아니다."""
    assert not detect(_state("Excel/Open 쓰면 되나요? Mail/Send는요?"), _Cat()).found


def test_silent_on_urls_and_file_paths():
    """슬래시가 흔한 URL·경로를 쌍으로 오인하지 않는다."""
    text = """대상은 아래와 같아요
https://intranet.corp/reports/daily
C:/Users/hong/Desktop/매출.xlsx
\\\\fileserver\\share\\input
/var/log/app/output.csv
"""
    assert not detect(_state(text), _Cat()).found


def test_silent_on_ordinary_conversation():
    assert not detect(_state("매일 아침 9시에 메일 보내는 봇 만들어줘"), _Cat()).found
    assert not detect(_state(""), _Cat()).found


def test_assistant_turns_do_not_mask_the_signal():
    """우리가 A360 액션을 나열한 답변이 실재 비율을 끌어올려 신호를 가리면 안 된다."""
    history = [
        {"role": "user", "content": UIPATH_PASTE},
        {"role": "assistant", "content": A360_PASTE},  # 어시스턴트 발화 — 집계에서 빠져야 한다
    ]
    assert "ReadRange" in user_text(_state("만들어줘", history))
    assert "cloudExcelOpen" not in user_text(_state("만들어줘", history))
    assert detect(_state("만들어줘", history), _Cat(A360_KNOWN)).found


def test_catalog_failure_degrades_to_no_signal():
    """카탈로그 조회가 깨져도 감지 실패일 뿐 턴을 죽이지 않는다."""

    class _Broken:
        def get_action_schema(self, package, action):
            raise RuntimeError("DB 연결 실패")

    assert not detect(_state(UIPATH_PASTE), _Broken()).found


def test_solution_name_extraction():
    assert detect_solution_name("우리는 UiPath를 씁니다") == "uipath"
    assert detect_solution_name("파워 오토메이트로 짜줘") == "power automate"
    assert detect_solution_name("A360으로 만들어줘") is None
