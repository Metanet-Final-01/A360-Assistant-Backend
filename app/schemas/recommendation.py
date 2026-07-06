"""A360 작업 추천안 스키마 (FR-09~12) — 최종 내보내기(FR-17)와 골드셋 채점의 대상.

RecommendationVersion.payload(JSONB)에 이 형태로 저장된다.
package/action 이름은 RAG 카탈로그(docs/RAG_CATALOG.md)의 표기를 따른다
(예: package="Excel_MS", action="GoToCell").
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ActionParameter(BaseModel):
    """액션 입력 파라미터. 카탈로그의 파라미터 스키마(name/type/required/options)를 따른다."""

    name: str = Field(description="카탈로그 파라미터 name, 예: 'cellOption'")
    label: str | None = Field(None, description="사람용 라벨, 예: '셀 옵션'")
    value: Any = None
    value_source: Literal["schema_default", "llm", "user"] = Field(
        "llm", description="값의 출처 — 기본값 그대로/LLM 추론/사용자 지정"
    )


class RagSource(BaseModel):
    """추천 근거가 된 RAG 문서 참조 (FR-11)."""

    source_type: str = Field(description="doc_page|action_schema|package_overview|bot_example")
    title: str
    url: str | None = None
    score: float | None = Field(None, description="검색 유사도")


class RecommendedAction(BaseModel):
    """추천된 A360 액션 하나."""

    order: int
    package: str = Field(description="예: 'Excel_MS'")
    action: str = Field(description="예: 'GoToCell'")
    label: str | None = Field(None, description="사람용 라벨, 예: '셀로 이동'")
    parameters: list[ActionParameter] = Field(default_factory=list)
    rationale: str | None = Field(None, description="왜 이 액션인지 (FR-11)")
    sources: list[RagSource] = Field(default_factory=list)
    confidence: float | None = Field(None, ge=0.0, le=1.0, description="FR-12 신뢰도")


class StepRecommendation(BaseModel):
    """업무 단계 하나에 대한 액션 시퀀스. step_id는 AnalysisResult.steps[].step_id 참조."""

    step_id: str
    actions: list[RecommendedAction]


class BotVariable(BaseModel):
    """봇 입출력/내부 변수 (FR-10의 '입력/출력 변수')."""

    name: str
    type: str = Field("STRING", description="A360 변수 타입: STRING|NUMBER|BOOLEAN|TABLE|SESSION 등")
    direction: Literal["input", "output", "local"] = "local"
    description: str | None = None


class Recommendation(BaseModel):
    """추천안 전체 — 이 JSON이 최종 내보내기 형식이자 골드셋 채점 대상이다."""

    schema_version: str = "1.0"
    steps: list[StepRecommendation]
    variables: list[BotVariable] = Field(default_factory=list)
    notes: str | None = Field(None, description="전제·주의사항, 예: 'Knox 메일은 Email 패키지 기준'")
