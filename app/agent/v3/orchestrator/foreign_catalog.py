"""대화에 붙여넣은 '타 솔루션 카탈로그' 신호 (RPA-285).

왜 필요한가: a360 세션에서 사용자가 UiPath 같은 다른 솔루션의 액션 카탈로그를 붙여넣고
"이걸로 만들어줘"라고 하면, 그 카탈로그를 통째로 무시한 A360 흐름도가 **경고 한 줄 없이**
나온다. 사용자는 자기 카탈로그가 반영된 줄 안다 — 조용한 오답이 이 기능의 가장 나쁜 실패다.

## 판정을 둘로 나눈 이유

예전에는 이 모듈이 텍스트에서 "패키지/액션" 쌍을 정규식으로 긁어 판정했다. 그 방식은
**형식 하나에 묶여 있었다**: `- Excel advanced/cloudExcelOpen` 같은 슬래시 목록만 잡히고,
마크다운 표나 `- **Launch Excel** — 설명` 류의 산문형 카탈로그는 쌍이 0개로 나와 신호가
아예 안 떴다(실측: 448개 액션 카탈로그 3종 전부 0쌍). 조용한 오답을 막으려고 만든 장치가
정작 실사용 형식에서 침묵한 것이다.

그래서 **긁기는 LLM에게 넘기고, 검증만 결정론으로 남긴다**:

    intake LLM  → CatalogSignal(present/solution/sample_actions/confidence)
    이 모듈      → verify()가 sample_actions를 A360 카탈로그에 조회해 오탐을 기각

긁기가 형식에 묶여 있었을 뿐, 검증("A360 실재 비율이 높으면 그건 A360 액션을 옮겨 적은
것이다")은 형식과 무관하게 잘 동작하는 장치다. 이 분리로 미탐은 LLM이 줄이고, 오탐은
결정론이 막는다 — intake가 이미 쓰는 'LLM 판정 + 결정론 가드' 패턴과 같다.

LLM에게 카탈로그 **전량**을 재출력시키지 않는다는 점이 중요하다. 샘플 몇 개만 받으므로
출력 토큰이 몇십 개고, 448개짜리 카탈로그에서도 규모 문제가 없다(전량 추출은 별도 문제 —
extract_user_catalog 참고).
"""

import re

from pydantic import BaseModel, Field

from ..verify.catalog import CatalogLookup

# A360 카탈로그 실재 비율이 이 값을 넘으면 '타 솔루션'이 아니라고 본다. 사용자가 A360 액션을
# 옮겨 적으며 오타를 내거나 구 표기를 쓸 수 있어 0을 요구하지 않는다.
MAX_KNOWN_RATIO = 0.3

# 사용자가 솔루션 이름을 밝힌 경우 그대로 쓴다(세션 solution 확정·안내 문구용).
# A360/Automation Anywhere는 이 제품 자신이라 목록에 없다.
_KNOWN_SOLUTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("uipath", ("uipath", "유아이패스")),
    ("power automate", ("power automate", "파워 오토메이트", "파워오토메이트")),
    ("blue prism", ("blue prism", "블루프리즘", "블루 프리즘")),
    ("brity rpa", ("brity", "브리티")),
    ("winactor", ("winactor", "윈액터")),
)

# 세션 solution 컬럼이 받는 형식 (sessions.py `_SOLUTION_RE`와 같은 문자 집합).
# LLM이 "Power Automate for desktop"처럼 대문자로 주면 백엔드가 형식 불일치로 버리므로
# 여기서 맞춰 보낸다 — 판정이 맞았는데 표기 때문에 유실되면 진단이 어렵다.
_DISALLOWED = re.compile(r"[^a-z0-9 ._-]")
_LEADING = re.compile(r"^[^a-z0-9]+")


class CatalogSignal(BaseModel):
    """intake LLM이 내는 **검증 전** 원시 판정.

    sample_actions는 "패키지/액션" 표기 몇 개다 — 전량이 아니라 검증용 표본이다.
    """

    present: bool = False
    solution: str | None = None
    sample_actions: list[str] = Field(default_factory=list)
    confidence: str = "low"  # "high" | "low"


def detect_solution_name(text: str) -> str | None:
    """텍스트에 밝혀진 타 솔루션 이름 (없으면 None). LLM이 이름을 못 준 경우의 폴백."""
    low = text.lower()
    for key, words in _KNOWN_SOLUTIONS:
        if any(w in low for w in words):
            return key
    return None


def normalize_solution(name: str | None) -> str | None:
    """LLM이 준 이름을 세션 solution 형식으로 맞춘다 (소문자·허용문자·49자)."""
    if not name:
        return None
    s = _DISALLOWED.sub(" ", name.strip().lower())
    s = re.sub(r"\s+", " ", s).strip()
    s = _LEADING.sub("", s)
    return s[:49].strip() or None


def _split_pair(sample: str) -> tuple[str, str] | None:
    """샘플 표기 "패키지/액션"을 쌍으로 가른다. 슬래시가 없으면 검증할 수 없다."""
    pkg, sep, act = sample.partition("/")
    if not sep:
        return None
    pkg, act = pkg.strip(), act.strip()
    return (pkg, act) if pkg and act else None


class ForeignCatalogSignal:
    """검증을 마친 신호. `found`가 False면 나머지 필드는 의미 없다."""

    def __init__(
        self,
        found: bool,
        samples: int = 0,
        known: int = 0,
        solution: str | None = None,
        confirm: bool = False,
    ) -> None:
        self.found = found
        self.samples = samples      # 검증 가능한 표본 개수
        self.known = known          # 그중 A360 카탈로그에 실재하는 개수
        self.solution = solution    # 정규화된 솔루션 이름(없으면 None)
        # 세션 solution을 확정하고 이번 턴 어휘까지 바꿔도 되는가. 확신이 낮거나 검증할
        # 표본이 없으면 False — 그때는 흐름도를 A360으로 만들되 사실을 고지만 한다.
        self.confirm = confirm

    def notice(self) -> str:
        """사용자에게 보여줄 안내 — 무엇을 안 했는지 분명히 말한다."""
        who = f"{self.solution} " if self.solution else "다른 솔루션의 "
        return (
            f"참고로 대화에 {who}액션 목록을 주신 것 같은데, 이번 흐름도는 **그 목록이 아니라 "
            "A360 카탈로그로** 만들었어요. 지금 이 세션은 A360 기준이라 주신 표기를 그대로 "
            "쓰지 못했습니다."
        )


def verify(signal: CatalogSignal | None, catalog: CatalogLookup) -> ForeignCatalogSignal:
    """intake의 원시 판정을 결정론으로 검증한다 (LLM 호출 없음).

    표본이 대부분 A360에 실재하면 기각한다 — 사용자가 A360 액션을 옮겨 적은 것이지 타
    솔루션 카탈로그가 아니다. 이 한 줄이 LLM 오탐(예: "UiPath에서는 이렇게 하던데 A360은?"
    같은 비교 질문)을 막는 주 방어선이다.

    카탈로그 조회가 실패하면(인프라 이상) 신호 없음으로 강등한다 — 감지 실패가 생성을 막지
    않는다.
    """
    if signal is None or not signal.present:
        return ForeignCatalogSignal(False)

    pairs = [p for p in (_split_pair(s) for s in signal.sample_actions) if p]
    try:
        known = sum(1 for pkg, act in pairs if catalog.get_action_schema(pkg, act) is not None)
    except Exception:  # noqa: BLE001 — 감지는 부가 기능, 실패해도 턴을 죽이지 않는다
        return ForeignCatalogSignal(False)

    if pairs and known / len(pairs) > MAX_KNOWN_RATIO:
        return ForeignCatalogSignal(False)  # 대부분 실재 → A360 액션을 적은 것

    # 표본으로 검증까지 된 경우에만 세션을 바꾼다. 표본이 없으면 판정을 믿되 확정은 미룬다
    # (고지는 나가므로 조용한 오답은 여전히 막힌다).
    return ForeignCatalogSignal(
        True,
        samples=len(pairs),
        known=known,
        solution=normalize_solution(signal.solution),
        confirm=signal.confidence == "high" and bool(pairs),
    )
