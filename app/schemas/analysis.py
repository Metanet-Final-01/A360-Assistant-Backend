"""업무정의서 분석 결과 스키마 (FR-05: 단계·입출력·시스템·분기 식별)."""

from pydantic import BaseModel, Field

MAX_CONSTRAINTS = 20
MAX_CONSTRAINT_CHARS = 500


def normalize_constraints(values: object) -> list[str]:
    """비신뢰 제약 목록을 프롬프트에 싣기 전 결정론적 상한으로 정규화한다."""
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        text = " ".join(value.split()).strip()
        if text:
            normalized.append(text[:MAX_CONSTRAINT_CHARS])
        if len(normalized) >= MAX_CONSTRAINTS:
            break
    return normalized


class SourceEvidence(BaseModel):
    """이 단계가 문서 어디에서 나왔는지 (근거 제시용)."""

    page: int | None = None
    snippet: str | None = Field(None, description="문서 원문 발췌")


class WorkStep(BaseModel):
    """업무 단계 하나. step_id는 이 분석 내부의 안정 식별자다(피드백 등이 특정 단계를 지목할 때 참조).

    추천 흐름도는 이제 자체 지역 step_id를 쓰며 이 값을 참조하지 않는다(에이전트가 단계를 재구성)."""

    step_id: str = Field(description="안정 식별자, 예: 'step-1'")
    order: int
    name: str = Field(description="단계 이름, 예: '금 시세 내역 엑셀 가공'")
    description: str = ""
    inputs: list[str] = Field(default_factory=list, description="예: ['금 시세 표 (웹)']")
    outputs: list[str] = Field(default_factory=list, description="예: ['시세 엑셀 파일']")
    systems: list[str] = Field(default_factory=list, description="예: ['Edge', 'Excel']")
    branching: str | None = Field(None, description="분기/반복 조건 서술, 예: '최근 3일치만 반복'")
    evidence: SourceEvidence | None = None


class AnalysisResult(BaseModel):
    """문서 분석 최종 산출물. Analysis.result(JSONB)에 이 형태로 저장된다."""

    schema_version: str = "1.0"
    document_title: str | None = None
    summary: str = Field("", description="비개발자용 한 문단 업무 요약")
    steps: list[WorkStep]
    constraints: list[str] = Field(
        default_factory=list,
        description="문서나 사용자 발화에 명시된 자동화 제약·필수 조건",
    )
    ambiguities: list[str] = Field(
        default_factory=list,
        description="문서만으로 확정 못 한 항목 — 챗봇 재질의 후보 (FR-16)",
    )
