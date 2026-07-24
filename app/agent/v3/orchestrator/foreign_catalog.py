"""대화에 붙여넣은 '타 솔루션 카탈로그' 감지 (RPA-285).

왜 필요한가: a360 세션에서 사용자가 UiPath 같은 다른 솔루션의 액션 카탈로그를 붙여넣고
"이걸로 만들어줘"라고 하면, 지금은 그 카탈로그를 통째로 무시한 A360 흐름도가 **경고 한 줄
없이** 나온다. 사용자는 자기 카탈로그가 반영된 줄 안다 — 조용한 오답이 이 기능의 가장 나쁜
실패다. 여기서 정황을 잡아 답변에서 알린다(0단계). 같은 신호를 세션의 solution 확정에도
쓴다(2단계).

판정은 결정론이고 LLM을 부르지 않는다 — 매 generate 턴에 LLM을 한 번 더 태우는 비용은
'안내 한 줄'에 비해 과하다. 대신 카탈로그 조회로 판정한다:

    붙여넣은 목록에서 "패키지/액션" 쌍을 긁고, 그중 **A360 카탈로그에 실재하는 비율**을 본다.
    쌍이 충분히 많은데 대부분 실재하지 않으면 타 솔루션 카탈로그로 본다.

이 설계의 요점은 **오탐이 어렵다**는 것이다. A360 액션을 붙여넣은 사용자는 실재 비율이 높아
신호가 안 뜬다. 카탈로그가 아닌 잡담·경로·URL은 애초에 쌍이 안 잡힌다. 반대로 미탐(놓침)은
허용한다 — 표 형식이나 산문형 카탈로그는 못 잡지만, 못 잡으면 그냥 현재 동작이라 손해가 없다.
"""

import re

from ..verify.catalog import CatalogLookup

# 카탈로그로 인정할 최소 쌍 개수 — 한두 개는 지나가는 언급("Excel/Open 쓰면 되나요?")일 수
# 있어 목록으로 보지 않는다.
MIN_PAIRS = 3

# A360 카탈로그 실재 비율이 이 값 이하면 '타 솔루션'으로 본다. 사용자가 A360 액션을 옮겨
# 적으며 오타를 내거나 구 표기를 쓸 수 있어 0을 요구하지 않는다.
MAX_KNOWN_RATIO = 0.3

# "패키지/액션" 한 쌍. 줄머리의 목록 기호·번호를 걷어내고 첫 토큰 쌍만 본다.
# 이름에 공백·점·&·+·_·-를 허용한다(예: "Excel advanced/cloudExcelOpen",
# "UiPath.Excel.Activities/ReadRange"). 뒤에 라벨·설명·파라미터가 붙어도 무시한다.
_PAIR_LINE = re.compile(
    r"^[\s\-*•·\d.)\]]*"                    # 목록 기호·번호
    r"([A-Za-z][\w .&+-]{0,48}?)"           # 패키지
    r"\s*/\s*"
    r"([A-Za-z][\w .&+-]{0,48}?)"           # 액션
    r"\s*(?:[(\[:—–\-]|$)",                 # 구분자 또는 줄 끝
    re.MULTILINE,
)

# 쌍처럼 보이지만 카탈로그가 아닌 줄 — URL·경로는 슬래시가 흔하다.
_NOT_CATALOG = re.compile(r"://|^[A-Za-z]:[\\/]|^[\\/]{1,2}\w")

# 사용자가 솔루션 이름을 밝힌 경우 그대로 쓴다(2단계의 solution 확정·안내 문구용).
# A360/Automation Anywhere는 이 제품 자신이라 목록에 없다.
_KNOWN_SOLUTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("uipath", ("uipath", "유아이패스")),
    ("power automate", ("power automate", "파워 오토메이트", "파워오토메이트")),
    ("blue prism", ("blue prism", "블루프리즘", "블루 프리즘")),
    ("brity rpa", ("brity", "브리티")),
    ("winactor", ("winactor", "윈액터")),
)


def _candidate_pairs(text: str) -> list[tuple[str, str]]:
    """텍스트에서 "패키지/액션" 쌍을 긁는다 (URL·파일 경로 줄은 제외)."""
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or _NOT_CATALOG.search(stripped):
            continue
        m = _PAIR_LINE.match(line)
        if not m:
            continue
        pkg, act = m.group(1).strip(), m.group(2).strip()
        if pkg and act:
            pairs.append((pkg, act))
    return pairs


def detect_solution_name(text: str) -> str | None:
    """텍스트에 밝혀진 타 솔루션 이름 (없으면 None)."""
    low = text.lower()
    for key, words in _KNOWN_SOLUTIONS:
        if any(w in low for w in words):
            return key
    return None


def user_text(state: dict) -> str:
    """이번 메시지 + 이력의 사용자 발화 — 카탈로그는 이전 턴에 붙여넣었을 수 있다.

    어시스턴트 발화는 뺀다: 우리가 A360 액션을 나열한 답변이 '실재 비율'을 끌어올려
    사용자가 붙여넣은 타 솔루션 카탈로그를 가려버린다.
    """
    parts = [
        turn.get("content") or ""
        for turn in (state.get("history") or [])
        if turn.get("role") == "user"
    ]
    parts.append(state.get("message") or "")
    return "\n".join(parts)


class ForeignCatalogSignal:
    """감지 결과. `found`가 False면 나머지 필드는 의미 없다."""

    def __init__(self, found: bool, pairs: int = 0, known: int = 0, solution: str | None = None):
        self.found = found
        self.pairs = pairs          # 긁힌 쌍 개수
        self.known = known          # 그중 A360 카탈로그에 실재하는 개수
        self.solution = solution    # 밝혀진 솔루션 이름(없으면 None)

    def notice(self) -> str:
        """사용자에게 보여줄 안내 — 무엇을 안 했는지 분명히 말한다."""
        who = f"{self.solution} " if self.solution else "다른 솔루션의 "
        return (
            f"참고로 대화에 {who}액션 목록을 주신 것 같은데, 이번 흐름도는 **그 목록이 아니라 "
            "A360 카탈로그로** 만들었어요. 지금 이 세션은 A360 기준이라 주신 표기를 그대로 "
            "쓰지 못했습니다."
        )


def detect(state: dict, catalog: CatalogLookup) -> ForeignCatalogSignal:
    """대화에 타 솔루션 카탈로그가 제공됐는지 판정한다 (결정론, LLM 없음).

    쌍이 MIN_PAIRS 미만이면 목록으로 보지 않고, 그 이상이면 A360 실재 비율로 가른다.
    카탈로그 조회가 실패하면(인프라 이상) 신호 없음으로 강등한다 — 감지 실패가 생성을
    막지 않는다.
    """
    pairs = _candidate_pairs(user_text(state))
    if len(pairs) < MIN_PAIRS:
        return ForeignCatalogSignal(False)

    try:
        known = sum(1 for pkg, act in pairs if catalog.get_action_schema(pkg, act) is not None)
    except Exception:  # noqa: BLE001 — 감지는 부가 기능, 실패해도 턴을 죽이지 않는다
        return ForeignCatalogSignal(False)

    if known / len(pairs) > MAX_KNOWN_RATIO:
        return ForeignCatalogSignal(False)  # 대부분 실재 → A360 액션을 적은 것
    return ForeignCatalogSignal(
        True, pairs=len(pairs), known=known, solution=detect_solution_name(user_text(state))
    )
