"""용례 자산 로더 테스트 (RPA-298).

가장 중요한 건 **홀드아웃 계약**이다. `load_examples()` 기본 호출에 홀드아웃 예제가
섞이면 골드셋 평가가 자기 시험지를 미리 본 것이 되어 모든 측정이 무의미해진다.
이 파일의 첫 두 테스트가 그 래칫이다.
"""

import json
from pathlib import Path

from app.agent.knowledge import examples as ex

_DATA = (
    Path(__file__).resolve().parent.parent
    / "app" / "agent" / "knowledge" / "examples" / "data" / "automation_examples.json"
)


# ─────────────────────────────────────────────────────────────────────────────
# 홀드아웃 계약 — 이 파일의 존재 이유
# ─────────────────────────────────────────────────────────────────────────────

def test_default_load_excludes_holdout():
    """기본 호출에 홀드아웃이 섞이면 평가가 무의미해진다."""
    raw = json.loads(_DATA.read_text(encoding="utf-8"))
    holdout_ids = {e["doc_id"] for e in raw["examples"] if e.get("holdout")}
    assert holdout_ids, "홀드아웃이 하나도 없다 — 자산 생성이 잘못됐거나 유출 검토를 안 했다"

    loaded = {e.doc_id for e in ex.load_examples()}
    assert not (loaded & holdout_ids), f"홀드아웃이 기본 로드에 섞였다: {loaded & holdout_ids}"


def test_include_holdout_difference_is_exactly_the_holdout_set():
    raw = json.loads(_DATA.read_text(encoding="utf-8"))
    n_holdout_flow = sum(
        1 for e in raw["examples"] if e.get("holdout") and e.get("kind") == "flow"
    )
    base = len(ex.load_examples())
    withheld = len(ex.load_examples(include_holdout=True))
    assert withheld - base == n_holdout_flow


def test_select_examples_never_leaks_holdout():
    """선택 경로도 기본이 제외여야 한다 — 로드만 막고 선택에서 새면 소용없다."""
    raw = json.loads(_DATA.read_text(encoding="utf-8"))
    holdout = next(e for e in raw["examples"] if e.get("holdout"))
    picked = ex.select_examples(holdout["title"], hint_packages=holdout["packages"], limit=5)
    assert holdout["doc_id"] not in {e.doc_id for e in picked}


# ─────────────────────────────────────────────────────────────────────────────
# 자산 무결성
# ─────────────────────────────────────────────────────────────────────────────

def test_only_flow_kind_by_default():
    """액션 0건 인덱스 문서·변수 표기 튜토리얼은 조합 용례가 아니다."""
    assert all(e.kind == "flow" for e in ex.load_examples())
    assert all(e.steps for e in ex.load_examples()), "스텝 없는 예제는 few-shot에 쓸모가 없다"


def test_no_bot_creation_boilerplate_in_render():
    """봇 생성 UI 절차는 배치 단계에서 제거됐다 — 모델이 베낄 것은 조합 패턴이다.

    검사어는 **UI 클릭 절차 문구**로 좁힌다. "Task Bot" 같은 단어만 보면 업무 서술
    ("create a Task Bot using JSON actions …")까지 잡는 오탐이 난다.
    """
    block = ex.render_examples_block(ex.load_examples()[:8])
    for junk in (
        "Click + Create",
        "On the left panel",
        "Accept the default folder location",
        "click Automation",
    ):
        assert junk not in block, f"보일러플레이트가 렌더에 남았다: {junk}"


# ─────────────────────────────────────────────────────────────────────────────
# 선택 규칙 (결정론)
# ─────────────────────────────────────────────────────────────────────────────

def test_select_prefers_matching_packages():
    picked = ex.select_examples(
        "엑셀 파일을 열어 데이터를 읽는다", hint_packages=["Excel advanced"], limit=2
    )
    assert picked, "패키지 힌트가 맞는데 아무것도 못 골랐다"
    assert any("Excel advanced" in e.packages for e in picked)


def test_select_returns_nothing_when_unrelated():
    """점수 0이면 싣지 않는다 — 무관한 예제는 컨텍스트만 잡아먹는다."""
    assert ex.select_examples("zzzz", hint_packages=[], limit=2) == ()


def test_select_is_deterministic():
    goal = "스프레드시트 데이터를 읽어 메일로 보낸다"
    a = ex.selection_trace(ex.select_examples(goal, hint_packages=["Excel advanced"]))
    b = ex.selection_trace(ex.select_examples(goal, hint_packages=["Excel advanced"]))
    assert a == b, "같은 입력에 다른 용례가 선택된다 — 실행마다 결과가 흔들린다"


def test_render_block_is_readable():
    picked = ex.select_examples("엑셀 데이터를 읽는다", hint_packages=["Excel advanced"], limit=1)
    block = ex.render_examples_block(picked)
    assert "[용례]" in block and "/" in block
