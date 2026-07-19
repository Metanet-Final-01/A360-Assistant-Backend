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

    schema가 없는 행은 {package, action, params_unknown: True} 최소 스펙으로 적재한다 —
    존재 판정(R1)은 행의 존재만으로 성립하고, 파라미터 판정(R2~R5)은 스펙이 있을 때만
    가능하다는 분리. 제외해 버리면 실존 액션이 R1에서 환각으로 오판된다.
    """

    def __init__(self) -> None:
        self._index: dict[tuple[str, str], dict] | None = None
        self._triggers: list[dict] | None = None
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
            if key in index and not index[key].get("params_unknown"):
                # 청킹으로 (pkg, act)당 여러 행이 있을 수 있으나 metadata.schema는 동일 — 첫 행이면 충분.
                continue
            if isinstance(metadata, str):  # jsonb는 dict로 오지만 드라이버 차이에 방어적으로
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    metadata = None
            # 최상위가 dict가 아니면(배열·문자열·깨진 JSON) schema 없음으로 처리 — 한 행의
            # 비정상 metadata가 전체 적재를 AttributeError로 무너뜨리지 않게 한다.
            schema = metadata.get("schema") if isinstance(metadata, dict) else None
            if not isinstance(schema, dict):
                # schema 없는 행도 어휘로는 실존한다(v2 문서 카탈로그의 보강 미도달 행 —
                # --enrich 생략 빌드면 액션의 ~92%). parameters 키를 아예 두지 않아
                # 소비처(.get("parameters"))가 '스펙 미상'(None)을 '파라미터 없음'([])과
                # 구분하게 한다. 같은 키의 schema 보유 행이 나중에 오면 위에서 덮어쓴다.
                index.setdefault(key, {"package": package_name, "action": action_name, "params_unknown": True})
                continue
            # package/action을 상위 키로 부여해 문서화된 스펙 형태와 맞춘다
            # (schema에는 두 키가 없으므로 덮어쓰지 않는다).
            spec = {"package": package_name, "action": action_name, **schema}
            if spec.get("parameters") is None:
                # v2 보강은 '파라미터 미상'을 schema.parameters=None으로 기록한다 — 키를
                # 제거해 schema 없는 행과 같은 params_unknown 형태로 정규화한다. None인 채로
                # 흘리면 v1/v2 검수의 spec.get("parameters", [])가 None을 받아 순회에서 깨진다.
                spec.pop("parameters", None)
                spec["params_unknown"] = True
            index[key] = spec

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

    def list_trigger_schemas(self) -> list[dict]:
        """trigger_schema 행 전량 — [{package, title, url, content}] (RPA-206 A-2 소비부).

        트리거 카탈로그는 7패키지·9문서 규모라 검색이 아니라 전량 메뉴로 소비한다 —
        희귀 소스타입은 하이브리드 검색 상위 k에서 굶는다(후단 필터 실측 0-hit).
        행이 없으면(구 카탈로그) 빈 목록을 반환해 소비처가 조용히 기능을 쉰다.
        """
        if self._triggers is None:
            with self._lock:
                if self._triggers is None:
                    loaded = self._load_triggers()
                    if loaded is None:
                        # 일시 실패는 캐싱하지 않는다 — 빈 목록으로 굳히면 DB가 복구돼도
                        # 재시작 전까지 트리거 제안이 계속 죽는다(_ensure_index와 대칭).
                        return []
                    self._triggers = loaded
        return self._triggers

    def _load_triggers(self) -> list[dict] | None:
        from app.rag.store import db

        try:
            conn = db.connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT package_name, title, url, content, chunk_index
                        FROM rag_documents
                        WHERE source_type = 'trigger_schema'
                        ORDER BY package_name, title, chunk_index
                        """
                    )
                    rows = cur.fetchall()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — 트리거 메뉴 실패가 추천 전체를 막으면 안 된다
            logger.warning("trigger_schema 조회 실패 — 트리거 제안 없이 진행", exc_info=True)
            return None  # 실패 신호 — 호출부가 캐싱하지 않고 다음 호출에서 재시도한다

        out: list[dict] = []
        seen: set[tuple] = set()
        for package_name, title, url, content, _chunk in rows:
            key = (package_name, title)
            if key in seen:  # 청킹 복수 행 — 메뉴에는 첫 청크(개요부)면 충분
                continue
            seen.add(key)
            out.append({"package": package_name, "title": title, "url": url, "content": content or ""})
        logger.info("트리거 메뉴 적재: %d건", len(out))
        return out

    def iter_action_schemas(self):
        """전체 액션 스펙을 순회한다 — agent v3의 세션 레지스트리·라벨 인덱스 유도용.

        인덱스는 이미 메모리 캐시라 추가 비용 없음. (v2 이하는 이 메서드를 쓰지 않으며,
        테스트 스텁에 없어도 duck-typing 폴백으로 동작하도록 사용처가 getattr로 조회한다.)
        """
        yield from self._ensure_index().values()


_backend_catalog: BackendCatalog | None = None


def get_backend_catalog() -> BackendCatalog:
    """Agent 검수 하네스(verify.catalog.get_catalog)가 받는 실제 카탈로그 조회기 (프로세스 1개 재사용)."""
    global _backend_catalog
    if _backend_catalog is None:
        _backend_catalog = BackendCatalog()
    return _backend_catalog
