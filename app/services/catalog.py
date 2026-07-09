"""액션 카탈로그 조회 서비스 — (package, action) → 구조 스펙.

Agent의 검수 하네스(app/agent/verify/catalog.py의 get_catalog)가 쓰는 실제 조회기다.
agent_retriever.HybridRetriever와 같은 경계·패턴이다(INTERFACES: Agent는 DB에 직접
접근하지 않고 백엔드 서비스만 호출). 테스트는 conftest가 인메모리 스텁을 주입한다.

스펙 출처: 수집 파이프라인이 JAR `package.json`을 정규화해 `rag_documents.metadata.schema`
(source_type='action_schema')에 저장해 둔 것과 같은 형태다(jar_parser 산출:
name/label/type/required/options/default). shortlist는 이 스펙으로 후보 파라미터를
부풀리고, checker(R1~R6)는 렌더 문자열이 아니라 이 구조 스펙으로 검수한다.

왜 이 서비스가 필요한가: hybrid 검색기는 실제 KB의 (package_name, action_name)을
그대로 돌려준다(예: Excel_MS/ReadExcelColumn). 검수 하네스가 그 액션의 구조 스펙을
조회해야 shortlist 메뉴가 채워지고 compose가 정확한 파라미터로 액션을 고른다 —
스펙 조회가 없으면 하이드레이션이 None으로 떨어져 추천에 제대로 된 package/action이
안 들어간다. 이 서비스가 실제 스펙을 돌려줘 그 경로를 채운다.
"""

import json
import logging
import threading

logger = logging.getLogger(__name__)


class BackendCatalog:
    """rag_documents(action_schema)의 metadata.schema를 (package_name, action_name)으로 조회.

    카탈로그는 정적 참조 데이터라 최초 조회 시 전체 액션 스펙을 1회 적재해 메모리에
    캐싱한다 — 병렬 step 노드가 매번 DB를 때리지 않게 한다(RPA-27 리뷰의 캐싱 취지와 동일).
    """

    def __init__(self) -> None:
        self._index: dict[tuple[str, str], dict] | None = None
        self._lock = threading.Lock()

    def _load(self) -> dict[tuple[str, str], dict]:
        # 지연 임포트 — 카탈로그를 실제로 쓸 때만 DB(psycopg)에 의존하게 한다.
        from app.rag.store import db

        index: dict[tuple[str, str], dict] = {}
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT package_name, action_name, metadata
                    FROM rag_documents
                    WHERE source_type = 'action_schema'
                      AND package_name IS NOT NULL
                      AND action_name IS NOT NULL
                    """
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        for package_name, action_name, metadata in rows:
            key = (package_name, action_name)
            if key in index:
                # 청킹으로 (pkg, act)당 여러 행이 있을 수 있으나 metadata.schema는 동일 — 첫 행이면 충분.
                continue
            if isinstance(metadata, str):  # jsonb는 dict로 오지만 드라이버 차이에 방어적으로
                metadata = json.loads(metadata)
            schema = (metadata or {}).get("schema")
            if not isinstance(schema, dict):
                continue
            # package/action을 상위 키로 부여해 문서화된 스펙 형태와 맞춘다
            # (schema에는 두 키가 없으므로 덮어쓰지 않는다).
            index[key] = {"package": package_name, "action": action_name, **schema}

        logger.info("BackendCatalog 적재 완료: 액션 스펙 %d개", len(index))
        return index

    def _ensure_index(self) -> dict[tuple[str, str], dict]:
        if self._index is None:
            with self._lock:  # 병렬 step 노드의 최초 조회 경합 방지 (더블 체크)
                if self._index is None:
                    self._index = self._load()
        return self._index

    def get_action_schema(self, package: str, action: str) -> dict | None:
        """(package, action)의 구조 스펙을 반환한다. 없으면 None."""
        return self._ensure_index().get((package, action))


_backend_catalog: BackendCatalog | None = None


def get_backend_catalog() -> BackendCatalog:
    """Agent 검수 하네스(verify.catalog.get_catalog)가 받는 실제 카탈로그 조회기 (프로세스 1개 재사용)."""
    global _backend_catalog
    if _backend_catalog is None:
        _backend_catalog = BackendCatalog()
    return _backend_catalog
