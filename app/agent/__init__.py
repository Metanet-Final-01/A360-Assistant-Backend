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

from .registry import (
    available_versions,
    default_version,
    import_version,
    resolve_version,
    resolve_version_name,
)

# done.data에 싣는 '실제 실행된 버전' 필드 이름 (RPA-184 공개 계약).
# 백엔드·테스트가 문자열을 복제하지 않도록 여기서 한 번만 정한다.
RESOLVED_VERSION_FIELD = "resolved_agent_version"


def _stamp_resolved_version(event: ProgressEvent, resolved: str) -> ProgressEvent:
    """done 이벤트에 실제 실행된 버전을 새긴다 (RPA-184).

    **버전 구현이 아니라 디스패처가 새긴다.** 버전을 고른 주체가 여기이기 때문이다. 각 `vN`이
    자기 이름을 자기신고하게 하면 (1) 벤더링된 사본마다 같은 코드가 복제되고, (2) 사본이 틀린
    이름을 적어도 아무도 못 잡으며, (3) 새 버전 폴더를 드롭할 때 계약이 따라오지 않는다.
    여기서 새기면 `v4/`를 추가하는 것만으로 계약이 자동으로 지켜진다(registry 자동탐색과 한 쌍).

    기존 키는 보존하고 이 필드만 덮어쓴다 — 구현이 먼저 적어 뒀더라도 **디스패처가 권위**다.
    `data`가 없는 done(있어선 안 되지만 계약상 nullable)이면 이 필드만 담아 만든다.

    ⚠️ 이벤트를 제자리에서 고치지 않고 복사본을 낸다 — 버전 그래프가 done data로 넘긴 dict는
    그래프 상태와 같은 객체일 수 있어, 그걸 변형하면 에이전트 내부 상태를 오염시킨다.
    """
    return event.model_copy(
        update={"data": {**(event.data or {}), RESOLVED_VERSION_FIELD: resolved}}
    )


async def stream_agent_turn(message: str, context: dict) -> AsyncIterator[ProgressEvent]:
    """단일 진입점(백엔드 POST /turn) — `context["agent_version"]`로 버전을 골라 위임한다.

    시그니처·done.data 계약은 버전과 무관하게 동일(INTERFACES §3). 버전 키가 없으면
    env 기본(`AGENT_VERSION`, 없으면 폴백)으로 동작한다 — operation/compact처럼 '없으면 기본값'.

    done 이벤트에는 실제 실행된 버전을 `resolved_agent_version`으로 실어 보낸다 (RPA-184).
    백엔드는 요청값(`agent_version`)이나 서버 기본값만으로는 무엇이 돌았는지 확정할 수 없어
    보증 기록을 미완성으로 남길 수밖에 없었다 — 이 필드가 그 공백을 메운다. `operation`과
    무관하게(chat·compact·fill_cards 모두) 같은 자리에서 새기므로 자동 compact 턴에도 남는다.
    """
    # 해석과 import를 나눠 부른다 — 둘 다 필요하기 때문이다(이름은 done에 새기고, 모듈은
    # 실행한다). 합쳐진 resolve_version()을 쓰면 같은 검증을 두 번 탄다 (Qodo #475).
    resolved = resolve_version_name((context or {}).get("agent_version"))
    impl = import_version(resolved)
    async for event in impl.stream_agent_turn(message, context):
        yield _stamp_resolved_version(event, resolved) if event.event == "done" else event


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
    "RESOLVED_VERSION_FIELD",
    "analyze",
    "available_versions",
    "default_version",
    "recommend",
    "stream_agent_turn",
]
