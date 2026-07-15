"""에이전트 메타 라우터 — 버전 목록 노출 (RPA-169).

프론트 버전 셀렉터·검증의 단일 소스. 목록은 app/agent의 registry가 `app/agent/vN` 폴더를
자동탐색해 만든다 (RPA-167) — **여기서 하드코딩하지 않는다**. v3가 추가되면 폴더만 생기고
이 라우트·프론트는 코드 변경 없이 새 버전을 노출한다.

버전 선택 자체는 POST /api/sessions/{id}/turn 의 `agent_version` 필드로 한다.
"""

from fastapi import APIRouter, HTTPException

from app.api.sessions import _get_agent_versions

router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.get("/versions")
def list_agent_versions() -> dict:
    """사용 가능한 에이전트 버전 목록 — 프론트 셀렉터·검증 소스.

    인증 없이 공개한다 — 세션 데이터가 아니라 배포된 기능의 메타이고, 프론트가 로그인 전
    화면에서도 셀렉터를 그릴 수 있어야 한다.

    레지스트리가 아직 배포 전이면(RPA-167 미랜딩) 503 — /turn이 에이전트 미구현 시 내는 것과
    같은 코드를 써서, 프론트가 "아직 준비 안 됨"을 한 가지 방식으로 다루게 한다.
    """
    registry = _get_agent_versions()
    if registry is None:
        raise HTTPException(
            503,
            detail={"code": "AGENT_UNAVAILABLE", "message": "에이전트가 아직 준비되지 않았습니다."},
        )
    available_versions, default_version = registry
    return {"versions": available_versions(), "default": default_version()}
