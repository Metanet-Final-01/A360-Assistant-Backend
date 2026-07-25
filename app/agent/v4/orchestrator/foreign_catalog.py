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

## 감지에서 '모드 판정'으로 (설계 §6.2·§6.6)

detect()가 잡는 건 "A360 카탈로그에 없는 액션 목록"이지 "타 솔루션"이 아니다.
**A360 사내 커스텀 패키지도 A360 카탈로그에 없다** — 텍스트만으로는 둘을 구분할 수 없다.
그래서 판정을 두 단계로 나눈다:

1. 제품명이 밝혀졌으면(`detect_solution_name` — "UiPath") **확정**. 물을 게 없다.
2. 안 밝혀졌으면 **사용자에게 묻는다** — "A360 사내 커스텀 패키지인가요, 다른 RPA 솔루션인가요?"

그리고 이 판정을 **흐름 생성 전에** 한다(§6.6 감지 시점). 예전엔 생성 뒤에 감지해 첫 턴이
A360 어휘로 만들어지고 안내만 붙었다 — 사용자가 같은 요청을 한 번 더 해야 했다.
"""

import re

from ..catalog_context import A360, A360_CUSTOM
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
    """감지 결과. `found`가 False면 나머지 필드는 의미 없다.

    v3에는 여기 `notice()`("주신 목록이 아니라 A360 카탈로그로 만들었어요")가 있었다.
    감지가 생성 **뒤**였기에 필요했던 사과 문구다 — 판정이 생성 앞으로 옮겨진 지금은
    애초에 틀린 어휘로 만들지 않으므로 지웠다. 되살리면 '생성 후 사과' 흐름이 함께 돌아온다.
    """

    def __init__(self, found: bool, pairs: int = 0, known: int = 0, solution: str | None = None):
        self.found = found
        self.pairs = pairs          # 긁힌 쌍 개수
        self.known = known          # 그중 A360 카탈로그에 실재하는 개수
        self.solution = solution    # 밝혀진 솔루션 이름(없으면 None)


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


# ─────────────────────────────────────────────────────────────────────────────
# 모드 판정 — 어떤 어휘로 만들 것인가 (설계 §6.2)
# ─────────────────────────────────────────────────────────────────────────────

MODE_A360 = "a360"          # 순수 A360 카탈로그
MODE_OVERLAY = "overlay"    # A360 + 사내 커스텀 액션 합집합
MODE_FOREIGN = "foreign"    # 타 솔루션 카탈로그로 교체
MODE_ASK = "ask"            # 판정 불가 — 사용자에게 되묻고 이번 턴은 만들지 않는다

# 되묻는 문구. 이 문장이 곧 '이미 물었다'의 표식이 되므로(아래 _already_asked) 마커
# 문자열(_ASK_MARKER)을 반드시 포함해야 한다.
_ASK_MARKER = "사내 커스텀 패키지"

ASK_MODE_ANSWER = (
    "대화에 주신 액션 목록을 확인했어요. 흐름도를 만들기 전에 하나만 확정할게요 — "
    f"**A360 {_ASK_MARKER}**인가요, 아니면 **다른 RPA 솔루션**의 액션인가요?\n\n"
    "- 사내 패키지라면: A360 표준 액션과 KB 검색은 그대로 두고, 주신 액션을 그 위에 얹어 "
    "표준 액션과 똑같이 씁니다.\n"
    "- 다른 솔루션이라면: 솔루션 이름을 함께 알려주세요. 그 카탈로그 어휘만으로 흐름도를 "
    "구성하고 A360 고유 개념(트리거·세션 구조 검사)은 끕니다.\n\n"
    "\"사내 패키지예요\" / \"UiPath예요\" 처럼 한 줄만 주시면 이어서 만들어 드릴게요."
)

# 답변 분류용 어휘 (결정론 — 되물음 한 번에 LLM을 또 태우지 않는다).
#
# 두 벌인 이유: 되묻기 **전**에는 사용자가 붙여넣은 카탈로그 본문까지 같이 훑는다.
# 거기엔 "Custom.Excel/Read" 같은 패키지 표기가 흔해서, 맨 "custom"·"커스텀"을 단서로
# 삼으면 **표기 하나 때문에 되묻기를 건너뛴다**. 그래서 평문에서만 나오는 표현으로 좁힌다.
# 되묻기 **후**의 답변은 짧고 지시적이라 맨 "커스텀"도 답으로 봐도 안전하다.
_CUSTOM_WORDS = (
    "사내", "자체 개발", "자체개발", "자체 제작", "자체제작",
    "인하우스", "in-house", "inhouse", "우리 회사", "우리회사", "직접 만든", "직접만든",
    "커스텀 패키지", "커스텀패키지", "커스텀 액션", "커스텀액션",
)
_CUSTOM_WORDS_REPLY = _CUSTOM_WORDS + ("커스텀", "custom")

# 제품명을 못 밝힌 채 "다른 솔루션"이라고만 답한 경우.
_FOREIGN_WORDS = ("다른 솔루션", "타 솔루션", "다른 rpa", "타사", "다른 제품", "외부 솔루션", "타 제품")


class CatalogModeDecision:
    """이번 턴의 어휘 모드 판정 결과.

    solution: 세션에 확정할 값 (백엔드 detected_solution 경로로 올라간다).
    confirm : 이번 턴에 **새로** 확정됐는가 — 확정 못 한 추정치를 세션에 굳히지 않기 위한 구분.
    """

    def __init__(
        self,
        mode: str,
        solution: str = A360,
        *,
        confirm: bool = False,
        signal: ForeignCatalogSignal | None = None,
    ):
        self.mode = mode
        self.solution = solution
        self.confirm = confirm
        self.signal = signal

    @property
    def question(self) -> str:
        return ASK_MODE_ANSWER


def _already_asked(state: dict) -> bool:
    """이미 되물었는가 — 답을 못 알아들었다고 매 턴 같은 질문을 반복하면 대화가 막힌다."""
    return any(
        turn.get("role") == "assistant" and _ASK_MARKER in (turn.get("content") or "")
        for turn in (state.get("history") or [])
    )


def _reply_text(state: dict) -> str:
    """되물음 **이후**의 사용자 발화 + 이번 메시지.

    되물음 이전 발화까지 보면 붙여넣은 카탈로그 본문("Custom.Excel/Read" 같은 표기)이
    _CUSTOM_WORDS에 걸려 답변으로 오독된다. 아직 물은 적이 없으면 전체를 본다 —
    사용자가 처음부터 "사내 패키지 목록이야"라고 밝혔으면 물을 필요가 없다.
    """
    history = state.get("history") or []
    start = 0
    for i in range(len(history) - 1, -1, -1):
        turn = history[i]
        if turn.get("role") == "assistant" and _ASK_MARKER in (turn.get("content") or ""):
            start = i + 1
            break
    parts = [t.get("content") or "" for t in history[start:] if t.get("role") == "user"]
    parts.append(state.get("message") or "")
    return "\n".join(parts)


def classify_mode_reply(state: dict) -> str | None:
    """되물음에 대한 답을 결정론 분류한다. 판정 불가면 None.

    제품명이 우선이다 — "사내에서 UiPath 씁니다"는 사내 패키지가 아니라 UiPath다.
    """
    text = _reply_text(state)
    name = detect_solution_name(text)
    if name:
        return MODE_FOREIGN
    low = text.lower()
    words = _CUSTOM_WORDS_REPLY if _already_asked(state) else _CUSTOM_WORDS
    if any(w in low for w in words):
        return MODE_OVERLAY
    if any(w in low for w in _FOREIGN_WORDS):
        return MODE_FOREIGN
    return None


def decide_catalog_mode(
    state: dict, catalog: CatalogLookup, *, detect_new: bool = True
) -> CatalogModeDecision:
    """이번 턴이 어떤 어휘로 흐름을 만들지 정한다 — **생성 전에** 호출된다 (설계 §6.6).

    세션 solution이 이미 확정돼 있으면 그대로 따른다(사용자 PATCH·이전 턴 확정이 감지보다
    우선 — 오탐이 사용자 선택을 덮으면 되돌려도 다시 뒤집힌다). 아직 a360이면 결정론 감지로
    판정하고, 구분 불가면 MODE_ASK로 되묻는다.

    detect_new=False면 세션 값만 본다 — edit 경로처럼 "이미 만들어진 흐름이 어느 어휘로
    만들어졌나"만 알면 되는 곳은 새 감지를 돌릴 이유가 없다(되묻기도 부적절하다).
    """
    session = (state.get("solution") or A360).strip().lower()
    if session == A360_CUSTOM:
        return CatalogModeDecision(MODE_OVERLAY, A360_CUSTOM)
    if session != A360:
        return CatalogModeDecision(MODE_FOREIGN, session)
    if not detect_new:
        return CatalogModeDecision(MODE_A360, A360)

    signal = detect(state, catalog)
    if not signal.found:
        return CatalogModeDecision(MODE_A360, A360)
    if signal.solution:
        # 제품명이 밝혀졌다 — 사내 패키지일 리 없으니 되묻지 않고 이번 턴부터 그 어휘로 만든다.
        return CatalogModeDecision(MODE_FOREIGN, signal.solution, confirm=True, signal=signal)

    reply = classify_mode_reply(state)
    if reply == MODE_OVERLAY:
        return CatalogModeDecision(MODE_OVERLAY, A360_CUSTOM, confirm=True, signal=signal)
    if reply == MODE_FOREIGN:
        # 어느 제품인지는 몰라도 "A360이 아니다"는 확정할 수 있다 (기존 2단계 계약).
        return CatalogModeDecision(MODE_FOREIGN, "other", confirm=True, signal=signal)
    if _already_asked(state):
        # 이미 물었는데 답을 못 알아들었다 → 오버레이로 진행한다. 오버레이는 순수 a360의
        # **상위집합**이라(카탈로그·검색·트리거 전부 유지 + 커스텀 어휘 추가) 이 추측이
        # 틀려도 잃는 게 없다. 단 confirm=False — 추측을 세션에 굳히지는 않는다.
        return CatalogModeDecision(MODE_OVERLAY, A360_CUSTOM, signal=signal)
    return CatalogModeDecision(MODE_ASK, A360, signal=signal)
