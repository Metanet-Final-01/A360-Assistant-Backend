"""SSE 진행 이벤트 스키마 — 분석·추천·챗봇 스트림의 공통 규약.

프론트는 event 필드로 분기한다. 규약 상세와 예시는 docs/INTERFACES.md 참고.
"""

from typing import Any, Literal

from pydantic import BaseModel


class ProgressEvent(BaseModel):
    """스트림 이벤트 한 건.

    - stage:   처리 단계 진입 (message에 사람용 문구)
    - partial: 중간 산출물 도착 (data에 단계별 결과 등)
    - token:   LLM 답변 텍스트 조각 (message에 토큰)
    - done:    완료 (data에 최종 산출물)
    - error:   실패 (message에 사용자용 오류 문구)
    """

    event: Literal["stage", "partial", "token", "done", "error"]
    stage: str | None = None  # routing|reading|analyzing|searching|composing|recommending|refining|verifying|compacting
    message: str | None = None
    data: dict[str, Any] | None = None

    def to_sse(self) -> str:
        """text/event-stream 한 프레임으로 직렬화."""
        return f"data: {self.model_dump_json(exclude_none=True)}\n\n"
