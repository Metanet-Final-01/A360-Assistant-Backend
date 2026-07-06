"""도메인 JSON 스키마 (Pydantic) — 백엔드·Agent·프론트가 공유하는 데이터 계약.

이 패키지가 프로젝트 산출물의 형태를 정의한다:
- analysis: 업무정의서 분석 결과 (FR-05, 08)
- recommendation: A360 작업 추천안 (FR-09~12, 17)
- events: SSE 진행 이벤트 (비기능: 진행 상태 표시)

변경 규칙: 필드 추가/변경은 백엔드·Agent 담당 합의 후 백엔드가 커밋한다.
"""

from app.schemas.analysis import AnalysisResult, SourceEvidence, WorkStep
from app.schemas.events import ProgressEvent
from app.schemas.recommendation import (
    ActionParameter,
    BotVariable,
    RagSource,
    Recommendation,
    RecommendedAction,
    StepRecommendation,
)

__all__ = [
    "ActionParameter",
    "AnalysisResult",
    "BotVariable",
    "ProgressEvent",
    "RagSource",
    "Recommendation",
    "RecommendedAction",
    "SourceEvidence",
    "StepRecommendation",
    "WorkStep",
]
