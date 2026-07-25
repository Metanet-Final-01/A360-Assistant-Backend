"""FR-14 제약 공급 경로의 버전 공통 계약."""

import asyncio

import pytest

from app.agent.v1 import analysis as analysis_v1
from app.agent.v1.orchestrator import generate as generate_v1
from app.agent.v2 import analysis as analysis_v2
from app.agent.v3 import analysis as analysis_v3


@pytest.mark.parametrize("analysis_module", [analysis_v1, analysis_v2, analysis_v3])
def test_analysis_prompts_extract_only_explicit_constraints(analysis_module):
    prompt = analysis_module._SYSTEM_PROMPT

    assert '"constraints"' in prompt
    assert "문서에 없는 제약을 추론하거나 생성하지 않는다" in prompt


def test_v1_generate_passes_analysis_constraints(monkeypatch):
    captured = {}

    class _FakeRecommendGraph:
        async def astream(self, inputs, **kwargs):
            captured.update(inputs)
            yield ("values", {"recommendation": {"steps": []}})

    monkeypatch.setattr(generate_v1, "get_recommend_graph", lambda: _FakeRecommendGraph())
    monkeypatch.setattr(generate_v1, "get_catalog", lambda: object())
    monkeypatch.setattr(
        generate_v1,
        "verify_and_repair",
        lambda flow, catalog: {"flow": flow, "violations": []},
    )

    result = asyncio.run(generate_v1._generate_a360({
        "analysis": {
            "steps": [],
            "constraints": ["승인 전 외부 발송 금지"],
        },
    }))

    assert result["turn_type"] == "recommendation"
    assert captured["constraints"] == ["승인 전 외부 발송 금지"]
