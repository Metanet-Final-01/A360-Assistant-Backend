"""자동화 용례 자산 — 공식 문서의 'Examples of building automations' 34건 (RPA-298).

`bot_example` 소스타입이 DB에 0건이라 비어 있던 용례 슬롯을 채운다. 런타임은 커밋된
JSON만 읽는다(DB 조회 0회·LLM 0회) — 자세한 배경은 `loader` 모듈 docstring 참조.
"""

from .loader import (
    AutomationExample,
    ExampleStep,
    load_examples,
    render_examples_block,
    select_examples,
    selection_trace,
)

__all__ = [
    "AutomationExample",
    "ExampleStep",
    "load_examples",
    "render_examples_block",
    "select_examples",
    "selection_trace",
]
