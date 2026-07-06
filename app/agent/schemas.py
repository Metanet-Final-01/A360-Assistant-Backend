"""app.agent 공개 입출력 계약. 백엔드는 이 모델을 import해서 응답을 구성한다."""

from pydantic import BaseModel, Field


class Source(BaseModel):
    """답변 근거 문서 메타데이터. 검색 결과에서 본문(content)을 뺀 요약이다."""

    id: str
    title: str
    package_name: str | None = None
    action_name: str | None = None
    url: str | None = None
    score: float


class AgentResult(BaseModel):
    """run_agent 반환값. 구조화 추천 등 추가 필드는 후속 이슈에서 확장한다."""

    answer: str
    sources: list[Source] = Field(default_factory=list)
