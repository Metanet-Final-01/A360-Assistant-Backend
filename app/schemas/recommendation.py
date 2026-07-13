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
    """추천된 A360 액션 하나 — A360 봇 JSON의 노드와 동일한 재귀 트리 구조.

    Loop·If·Else If·Else·Step·Error handler 같은 컨테이너 액션은 본문을
    children에 담는다. A360에는 임의의 병합점이 없다: 분기(If/Else) 블록이
    끝나면 실행은 "다음 형제 액션"으로 이어진다 — 그것이 병합이다.

    예) If(조건) [children: 참일 때 액션들] → Else [children: ...] → 다음 형제 = 병합 지점
    예) Loop(3일치 반복) [children: 반복 본문]
    """

    order: int
    package: str = Field(description="예: 'Excel_MS'")
    action: str = Field(description="예: 'GoToCell'")
    label: str | None = Field(None, description="사람용 라벨, 예: '셀로 이동'")
    parameters: list[ActionParameter] = Field(default_factory=list)
    children: list["RecommendedAction"] = Field(
        default_factory=list, description="컨테이너 액션(Loop/If/Step 등)의 본문"
    )
    rationale: str | None = Field(None, description="왜 이 액션인지 (FR-11)")
    sources: list[RagSource] = Field(default_factory=list)
    confidence: float | None = Field(None, ge=0.0, le=1.0, description="FR-12 신뢰도")


class StepRecommendation(BaseModel):
    """추천 흐름도의 한 단계(액션 묶음). 에이전트가 업무를 재구성해 만든 자기완결적 단위다.

    step_id는 흐름도 내부의 지역 식별자다 — 더는 AnalysisResult.steps[].step_id를 참조하지 않는다
    (에이전트가 분석 단계를 합치거나 쪼갠다). label/description으로 이 단계가 스스로를 설명하므로
    흐름도만으로 렌더할 수 있다.

    ⚠️ label/description은 반드시 선택(str|None). 필수로 하면 agent 검수 하네스의 국소 교정 중간
    산출물(label 누락)이 Recommendation 검증에 걸려 교정 루프가 무력화된다(정준환 실측 회귀).
    최종 저장·렌더 직전 agent `_coerce_flow`가 label을 step_id로 폴백해 채운다 — 스키마는 관대하게,
    실제 데이터엔 항상 존재.
    """

    step_id: str = Field(description="흐름도 내부 지역 id, 예: 'step-1'")
    label: str | None = Field(None, description="단계 제목(사람이 읽는)")
    description: str | None = Field(None, description="이 단계가 무엇을 하는지 한 줄 설명")
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

    def iter_actions(self):
        """트리를 평탄화해 모든 액션을 순회 (골드셋 채점·검증용)."""

        def walk(actions: list[RecommendedAction]):
            for a in actions:
                yield a
                yield from walk(a.children)

        for step in self.steps:
            yield from walk(step.actions)


RecommendedAction.model_rebuild()
