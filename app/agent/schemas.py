"""app.agent 공개 입출력 계약. 백엔드는 이 모델을 import해서 응답을 구성한다."""

from pydantic import BaseModel


class AgentResult(BaseModel):
    """run_agent 반환값. 근거 문서 등 추가 필드는 후속 이슈에서 확장한다."""

    answer: str
