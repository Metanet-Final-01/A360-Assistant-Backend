"""자동화 용례 자산 로더 — 공식 문서에서 뽑은 (업무 → 액션 순서 → 파라미터) 삼중항.

## 왜 필요한가

흐름 생성 검색이 `["action_schema", "bot_example"]`로 좁혀져 있는데 `bot_example`은 DB에
**0건**이다. 원 설계는 어휘(action_schema) + 용례(bot_example)였는데, 실봇을 배제하기로
하면서 용례 슬롯이 영구히 빈 채로 남았다. 지금 에이전트는 단어장만 있고 예문이 없다.

이 모듈이 그 슬롯을 공식 문서로 채운다. 대상이 34개 토픽뿐이라 **검색을 쓰지 않는다** —
오프라인 배치(`scripts/agent_v4/build_examples.py`)가 만든 JSON을 읽어 프롬프트 블록으로
렌더할 뿐이다. DB 조회 0회·LLM 0회.

검색을 안 쓰는 게 오히려 유리하다: `app/services/rag.py`의 소스타입 필터는 리랭크가 끝난
뒤 적용되는 후단 필터라, doc_page가 코퍼스의 90%인 상황에서 희귀 소스는 상위 k에서 굶는다
(`trigger_schema`가 같은 이유로 검색을 포기하고 전량 메뉴로 갔다).

## 홀드아웃

`load_examples()`의 `include_holdout` 기본값이 **False**다. 이게 런타임 홀드아웃 보증이다 —
골드셋 평가에서도 이 기본을 쓰고, ablation만 명시적으로 True를 넘긴다. 자산의 `holdout`
필드가 근거(`holdout_reason`)와 함께 어느 예제를 왜 뺐는지 남긴다.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_DATA = Path(__file__).resolve().parent / "data" / "automation_examples.json"

# few-shot으로 쓸 종류. "excluded"(액션 0건 인덱스·관리 절차)와 "variable_syntax"(변수 표기
# 튜토리얼)는 흐름 구성 용례가 아니라 제외가 기본이다.
_DEFAULT_KINDS = ("flow",)

# 프롬프트에 실을 스텝 상한 — 한 예제가 20스텝을 넘으면 컨텍스트를 잡아먹고 모델이
# 예제를 그대로 베끼는 쪽으로 기운다. 앞부분이 업무 흐름의 골격을 담는다.
_MAX_STEPS_IN_BLOCK = 12


@dataclass(frozen=True)
class ExampleStep:
    package: str
    action: str
    intent: str
    params: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class AutomationExample:
    doc_id: str
    title: str
    title_ko: str | None
    url: str
    kind: str
    domain: str
    task_summary: str
    packages: frozenset[str]
    steps: tuple[ExampleStep, ...]
    holdout: bool
    holdout_reason: str
    related_goldset: tuple[str, ...]


def _to_example(raw: dict) -> AutomationExample:
    steps = tuple(
        ExampleStep(
            package=s.get("package") or "",
            action=s.get("action") or "",
            intent=(s.get("intent") or "").strip(),
            params=tuple((str(k), str(v)) for k, v in (s.get("params") or [])),
        )
        for s in (raw.get("steps") or [])
    )
    return AutomationExample(
        doc_id=raw.get("doc_id") or "",
        title=raw.get("title") or "",
        title_ko=raw.get("title_ko") or None,
        url=raw.get("url") or "",
        kind=raw.get("kind") or "flow",
        domain=raw.get("domain") or "",
        task_summary=(raw.get("task_summary") or "").strip(),
        packages=frozenset(raw.get("packages") or ()),
        steps=steps,
        holdout=bool(raw.get("holdout")),
        holdout_reason=raw.get("holdout_reason") or "",
        related_goldset=tuple(raw.get("related_goldset") or ()),
    )


@lru_cache(maxsize=1)
def _all_examples() -> tuple[AutomationExample, ...]:
    """자산 전량(홀드아웃 포함). 파일이 없으면 빈 튜플 — 용례가 없어도 생성은 계속된다."""
    if not _DATA.is_file():
        return ()
    raw = json.loads(_DATA.read_text(encoding="utf-8"))
    return tuple(_to_example(e) for e in raw.get("examples") or ())


def load_examples(
    *,
    include_holdout: bool = False,
    kinds: Iterable[str] = _DEFAULT_KINDS,
) -> tuple[AutomationExample, ...]:
    """few-shot에 쓸 용례. **기본은 홀드아웃 제외** — 이게 유출 방지 계약이다."""
    wanted = set(kinds)
    return tuple(
        e for e in _all_examples()
        if e.kind in wanted and (include_holdout or not e.holdout)
    )


def _score(example: AutomationExample, goal_tokens: set[str], hint_packages: set[str]) -> int:
    """목표 문장·힌트 패키지와의 결정론 매칭 점수. LLM·임베딩 없음."""
    score = 0
    # 패키지 일치가 가장 강한 신호 — 같은 패키지를 쓰는 예제가 조합 패턴도 비슷하다.
    score += 3 * len(example.packages & hint_packages)
    haystack = f"{example.title} {example.task_summary} {example.domain}".lower()
    score += sum(1 for t in goal_tokens if t and t in haystack)
    return score


def select_examples(
    goal: str,
    *,
    hint_packages: Iterable[str] = (),
    limit: int = 2,
    include_holdout: bool = False,
) -> tuple[AutomationExample, ...]:
    """목표에 가까운 용례를 고른다 (결정론).

    점수가 0이면 싣지 않는다 — 무관한 예제는 도움이 안 되고 컨텍스트만 잡아먹는다.
    동점은 doc_id 순으로 갈라 실행마다 결과가 흔들리지 않게 한다.
    """
    pool = load_examples(include_holdout=include_holdout)
    if not pool:
        return ()
    tokens = {t for t in (goal or "").lower().replace(",", " ").split() if len(t) > 2}
    hints = {p for p in hint_packages if p}
    ranked = sorted(
        ((_score(e, tokens, hints), e) for e in pool),
        key=lambda pair: (-pair[0], pair[1].doc_id),
    )
    return tuple(e for score, e in ranked[:limit] if score > 0)


def render_examples_block(examples: Iterable[AutomationExample]) -> str:
    """프롬프트 삽입용 결정론 렌더 — (업무 → 액션 순서 → 파라미터)만 싣는다.

    봇 생성 UI 절차("Click + Create > Task Bot")는 배치 단계에서 이미 제거됐다.
    모델이 베껴야 할 것은 조합 패턴이지 클릭 순서가 아니다.
    """
    blocks: list[str] = []
    for e in examples:
        lines = [f"[용례] {e.title}"]
        if e.task_summary:
            lines.append(f"  업무: {e.task_summary}")
        for i, s in enumerate(e.steps[:_MAX_STEPS_IN_BLOCK], 1):
            params = ", ".join(f"{k}={v}" for k, v in s.params[:4])
            tail = f"  ({params})" if params else ""
            intent = f" — {s.intent}" if s.intent else ""
            lines.append(f"  {i}. {s.package} / {s.action}{intent}{tail}")
        if len(e.steps) > _MAX_STEPS_IN_BLOCK:
            lines.append(f"  … 이하 {len(e.steps) - _MAX_STEPS_IN_BLOCK}단계 생략")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def selection_trace(examples: Iterable[AutomationExample]) -> list[str]:
    """어떤 용례가 주입됐는지 — 평가에서 '점수가 오른 게 패턴 주입 덕인지' 가르는 원료."""
    return [e.doc_id for e in examples]
