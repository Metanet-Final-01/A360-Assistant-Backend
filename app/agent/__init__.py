"""Agent 공개 진입점 — 버전 디스패처.

서비스 에이전트는 버전별로 완전 분리돼 있다(`app/agent/v1`, `v2`, …). 각 버전은
orchestrator·recommend·verify·prompts까지 자기 사본을 가진 독립 구현이다. 이 모듈은
백엔드가 넘긴 버전(`context["agent_version"]`, 없으면 env 기본)을 골라 그 버전으로 위임한다 —
백엔드가 import하는 공개 심볼(`stream_agent_turn`/`analyze`/`recommend`)의 이름·시그니처는
불변이라, 백엔드는 context에 키 하나(`agent_version`)만 실어주면 된다(`operation`/`compact` 선례).

    from app.agent import stream_agent_turn
    async for event in stream_agent_turn(message, context):  # context["agent_version"]로 버전 선택
        ...

버전 추가(v3 등)는 `app/agent/v3/` 폴더를 두는 것만으로 끝난다 — registry가 자동 발견하고
`available_versions()`/`GET /api/agent/versions`가 노출하므로 프론트·백엔드 코드는 안 바뀐다.
버전 내부 그래프·검수·프롬프트는 각 `vN/` 안에서 관리한다(INTERFACES §1 — Agent 담당 소유).
"""

from collections.abc import AsyncIterator
from typing import Any

from app.schemas import ProgressEvent

from .registry import available_versions, default_version, resolve_version


async def stream_agent_turn(message: str, context: dict) -> AsyncIterator[ProgressEvent]:
    """단일 진입점(백엔드 POST /turn) — `context["agent_version"]`로 버전을 골라 위임한다.

    시그니처·done.data 계약은 버전과 무관하게 동일(INTERFACES §3). 버전 키가 없으면
    env 기본(`AGENT_VERSION`, 없으면 폴백)으로 동작한다 — operation/compact처럼 '없으면 기본값'.
    """
    impl = resolve_version((context or {}).get("agent_version"))
    async for event in impl.stream_agent_turn(message, context):
        yield event


async def recommend(
    *args: Any, agent_version: str | None = None, **kwargs: Any
) -> AsyncIterator[ProgressEvent]:
    """AnalysisResult → Recommendation 스트림 (버전 위임). 각 버전 recommend로 그대로 전달한다."""
    async for event in resolve_version(agent_version).recommend(*args, **kwargs):
        yield event


def analyze(*args: Any, agent_version: str | None = None, **kwargs: Any):
    """문서 분석 (버전 위임). 반환은 각 버전 analyze와 동일(AnalysisResult)."""
    return resolve_version(agent_version).analyze(*args, **kwargs)


__all__ = [
    "analyze",
    "available_versions",
    "default_version",
    "recommend",
    "stream_agent_turn",
]
