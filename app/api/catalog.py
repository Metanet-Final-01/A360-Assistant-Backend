"""패키지/액션 카탈로그 조회 API — 흐름도 편집기 피커용 (RPA-313, FR-18).

프론트 편집기가 목업(actionCatalog.js) 대신 실제 카탈로그를 쓰게 한다. 데이터는 RAG 카탈로그
(rag_documents의 action_schema/package_overview)에서 오며, package/action 표기는 추천 payload와
동일하다(카탈로그가 그 표기의 단일 출처 — docs/INTERFACES.md). 카탈로그는 A360 제품 참조 데이터
(민감정보 아님)라 /api/rag/search처럼 인증 없이 연다.
"""

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool  # fastapi 재-export (starlette 직접 의존 회피, Qodo #424)

from app.services.catalog import get_backend_catalog

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("/packages")
async def list_packages() -> dict:
    """패키지 → 액션 → 파라미터 스키마 전체를 한 응답으로 (57 패키지·368 액션 규모, 페이지네이션 불필요).

    응답: `{"packages": [{package, label, actions: [{action, label, isContainer, parameters?}]}]}`
    - package/action = 카탈로그 machine명(예: `Excel_MS`/`GoToCell`) — 프론트는 이 값을 노드의
      package/action에 그대로 넣어야 추천 스키마와 어긋나지 않는다(표시명 아님).
    - parameters 없음 = 스키마 미상(params_unknown)이지 '파라미터 0개'가 아니다.
    """
    catalog = get_backend_catalog()
    # package_overview DB 조회 + 인덱스 순회는 동기 → 이벤트 루프 밖 threadpool에서.
    packages = await run_in_threadpool(catalog.list_package_catalog)
    return {"packages": packages}
