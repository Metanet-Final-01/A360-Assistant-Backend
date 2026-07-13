"""추천 스키마 계약 테스트 (RPA-146) — StepRecommendation label/description + step_id 로컬화.

정준환(Agent)의 계약 변경: 흐름도 step은 자기완결적 단위로 label/description을 갖되,
label은 반드시 선택이어야 한다 — agent 검수 하네스의 국소 교정 중간산출물(label 누락)이
Recommendation 검증을 통과해야 교정 루프가 안 깨진다. step_id는 흐름도 내부 지역 id.
"""

import pytest
from pydantic import ValidationError

from app.schemas.recommendation import Recommendation, StepRecommendation


def _step(**overrides) -> dict:
    base = {"step_id": "step-1", "actions": [
        {"order": 1, "package": "String", "action": "assign",
         "parameters": [{"name": "value", "value": "x", "value_source": "llm"}]},
    ]}
    base.update(overrides)
    return base


def test_step_label_and_description_are_optional():
    """label/description 없는 step도 검증 통과 — 하네스 국소 교정 중간산출물 보호(핵심 계약)."""
    step = StepRecommendation.model_validate(_step())  # label/description 미포함
    assert step.label is None and step.description is None
    assert step.step_id == "step-1"


def test_step_accepts_label_and_description():
    step = StepRecommendation.model_validate(_step(label="엑셀 가공", description="3일치 반복 처리"))
    assert step.label == "엑셀 가공" and step.description == "3일치 반복 처리"


def test_step_id_still_required():
    """step_id는 여전히 필수 — 로컬 id일 뿐 존재는 강제."""
    with pytest.raises(ValidationError):
        StepRecommendation.model_validate({"actions": []})


def test_roundtrip_preserves_label_description_and_params():
    """model_validate→model_dump 왕복에 label·description·parameters·중첩 액션이 보존된다."""
    payload = {
        "schema_version": "1.0",
        "steps": [
            _step(label="열기", description="스프레드시트 연다"),
            {"step_id": "step-2", "label": "반복", "description": None, "actions": [
                {"order": 1, "package": "Loop", "action": "loop.commands.start",
                 "parameters": [], "children": [
                     {"order": 1, "package": "Excel_MS", "action": "SetCell",
                      "parameters": [{"name": "cell", "value": "A1", "value_source": "user"}]},
                 ]},
            ]},
        ],
        "variables": [{"name": "n", "type": "NUMBER", "direction": "input"}],
        "notes": "테스트",
    }
    rec = Recommendation.model_validate(payload)
    dumped = rec.model_dump()
    assert dumped["steps"][0]["label"] == "열기" and dumped["steps"][0]["description"] == "스프레드시트 연다"
    assert dumped["steps"][1]["label"] == "반복"
    # 중첩 액션·파라미터 보존 (value_source="user"는 챗봇 수정 보존의 기준 — 반드시 왕복돼야)
    child = dumped["steps"][1]["actions"][0]["children"][0]
    assert child["action"] == "SetCell"
    assert child["parameters"][0]["value"] == "A1"
    assert child["parameters"][0]["value_source"] == "user"


def test_step_id_is_not_bound_to_analysis():
    """추천 step_id는 분석과 무관한 지역 id — 임의 값이어도 검증 통과(참조 강제 없음)."""
    step = StepRecommendation.model_validate(_step(step_id="local-xyz"))
    assert step.step_id == "local-xyz"
