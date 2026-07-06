"""app.agent 공개 입출력 계약. 백엔드는 이 모델을 import해서 응답을 구성한다.

출처 모델은 공유 도메인 스키마(app.schemas.RagSource)를 그대로 쓴다 —
추천안(Recommendation)의 액션 출처와 챗 답변 출처가 같은 형태여야
프론트·검수 하네스가 한 가지 모델만 다루면 된다 (RPA-15).
"""

from pydantic import BaseModel, Field

from app.schemas import RagSource


class AgentResult(BaseModel):
    """run_agent 반환값. 구조화 추천 등 추가 필드는 후속 이슈에서 확장한다."""

    answer: str
    sources: list[RagSource] = Field(default_factory=list)
