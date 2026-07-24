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
import time

logger = logging.getLogger(__name__)


def _param_names(spec: dict) -> tuple | None:
    """스펙의 파라미터 이름 집합 (정렬된 튜플). 스펙 미상이면 None — 비교 대상에서 뺀다.

    논리 중복 판정의 기준이다(RPA-287). 파라미터 '집합'만 보는 이유는 검수 R2/R3가 이름
    존재 여부로 판정하기 때문이다 — 라벨·설명이 달라도 이름이 같으면 검수 결과는 같다.

    이름을 str로 강제한다: schema는 수집 파이프라인(LLM 보강 포함)이 만든 외부 데이터라
    name이 문자열이 아닐 수 있는데, 그러면 sorted()가 TypeError로 죽고 **카탈로그 적재
    전체가 무너진다**(모든 액션이 '카탈로그에 없음'이 돼 R1이 전부 환각으로 오판). 이 모듈이
    이미 지키는 '한 행의 비정상 데이터가 전체 적재를 깨뜨리지 않는다'는 원칙과 같은 취지다.
    지문은 비교용이라 문자열화해도 판정력이 유지된다(다른 값은 다른 문자열이 된다).
    """
    if spec.get("params_unknown"):
        return None
    return tuple(sorted(
        str(p["name"]) for p in (spec.get("parameters") or [])
        if isinstance(p, dict) and p.get("name")
    ))

# 인메모리 카탈로그 캐시의 재적재 주기 (RPA-225).
# 왜 필요한가: 이 캐시는 최초 1회 적재 후 갱신 경로가 없어, 적재(ingest)로 액션이
# 추가돼도 이미 떠 있는 프로세스는 **영원히** 옛 카탈로그를 봤다. 다중 인스턴스(ASG)면
# 재시작된 인스턴스와 아닌 인스턴스가 서로 다른 카탈로그로 검수해 같은 질의가 한쪽은
# 통과, 다른 쪽은 R1 환각으로 갈린다. TTL 경과 뒤 재적재해 모든 인스턴스가 이 창 안에서
# 수렴하게 한다 — MAX=2 규모라 Redis 무효화 전파 대신 TTL로 버틴다.
# 적재는 팀원이 수동으로 하는 드문 이벤트라 길게 잡아 요청 경로 재적재 빈도를 낮춘다.
# ⚠️ env가 아니라 모듈 상수다 — 런타임 조정이 필요하면 RPA-224 설정 레지스트리에 등록해
#    그쪽을 경유한다(여기서 os.getenv를 부르면 그 PR의 '직접 getenv 파일 증가 금지' 래칫에
#    걸린다). 테스트는 이 상수를 monkeypatch한다.
# 0 이하 = 재적재 없음(무한 캐시, RPA-225 이전 동작).
_CATALOG_TTL_SEC = 600.0

# 백그라운드 재적재의 DB 연결·쿼리 타임아웃(초) — 무한 블로킹이 재적재 스레드를 붙잡아
# _reloading_* 플래그가 영구 True로 고착되면(=이후 재적재 영구 정지) 카탈로그가 오래된
# 상태에 고정된다(Qodo 리뷰). 첫 동기 적재에도 적용해 기동 시 DB 지연에 무한 대기하지 않게.
_CATALOG_DB_TIMEOUT_SEC = 10


class BackendCatalog:
    """rag_documents(action_schema)의 metadata.schema를 (package_name, action_name)으로 조회.

    카탈로그는 정적 참조 데이터라 최초 조회 시 전체 액션 스펙을 1회 적재해 메모리에
    캐싱하되, `_CATALOG_TTL_SEC` 경과 뒤 다음 조회에서 재적재한다(RPA-225) — 병렬 step
    노드가 매번 DB를 때리지 않으면서도 적재 후 옛 카탈로그가 영구화되지 않게.

    schema가 없는 행은 {package, action, params_unknown: True} 최소 스펙으로 적재한다 —
    존재 판정(R1)은 행의 존재만으로 성립하고, 파라미터 판정(R2~R5)은 스펙이 있을 때만
    가능하다는 분리. 제외해 버리면 실존 액션이 R1에서 환각으로 오판된다.
    """

    def __init__(self) -> None:
        self._index: dict[tuple[str, str], dict] | None = None
        self._triggers: list[dict] | None = None
        # 벽시계(time.time)가 아니라 monotonic — NTP 보정으로 시계가 뒤로 가도 TTL 판정이
        # 음수가 되어 영구 stale/영구 fresh로 깨지지 않게. 미적재는 0.0(항상 stale로 판정되나
        # _index is None이 먼저 걸린다).
        self._index_loaded_at = 0.0
        self._triggers_loaded_at = 0.0
        # 백그라운드 재적재 진행 플래그 — 같은 캐시를 여러 스레드가 동시에 재적재하지 않게
        # (stale-while-revalidate, RPA-225).
        self._reloading_index = False
        self._reloading_triggers = False
        self._lock = threading.Lock()

    @staticmethod
    def _is_stale(loaded_at: float) -> bool:
        """마지막 적재로부터 TTL이 지났나. TTL<=0이면 항상 False(무한 캐시)."""
        if _CATALOG_TTL_SEC <= 0:
            return False
        return (time.monotonic() - loaded_at) >= _CATALOG_TTL_SEC

    def _start_reload(self, flag_attr: str, worker) -> None:
        """stale-while-revalidate 재적재를 **백그라운드 스레드로 1개만** 시작한다 (RPA-225).

        핵심: 재적재(DB 조회)를 요청 경로에서 락을 잡고 동기로 하면, TTL 경계에 들어온
        동시 요청이 전부 락 대기로 막혀 지연 스파이크가 난다(검수 경로가 이 캐시를 부른다).
        그래서 stale일 때 호출자는 **옛 값을 즉시 받고**, 갱신은 여기 백그라운드에서 돈다.
        flag로 동시 재적재를 1개로 제한한다 — 락은 flag를 세우는 짧은 구간만 잡는다.
        """
        with self._lock:
            if getattr(self, flag_attr):
                return
            setattr(self, flag_attr, True)

        def _run():
            try:
                worker()
            finally:
                setattr(self, flag_attr, False)

        try:
            threading.Thread(target=_run, daemon=True).start()
        except Exception:  # noqa: BLE001 — 스레드 생성 실패(자원 고갈 등)
            # start()가 실패하면 _run이 아예 안 돌아 finally가 플래그를 못 내린다 →
            # 플래그가 영구 True로 남아 이후 재적재가 영원히 억제된다(Qodo 리뷰). 롤백한다.
            setattr(self, flag_attr, False)
            logger.warning("카탈로그 재적재 스레드 시작 실패 — 재적재 건너뜀", exc_info=True)

    def _reload_index(self) -> None:
        """백그라운드 인덱스 재적재. 실패 시 옛 인덱스 유지 + loaded_at 백오프."""
        try:
            self._index = self._load()
            self._index_loaded_at = time.monotonic()
        except Exception:  # noqa: BLE001
            # 옛 인덱스로 버틴다. loaded_at을 현재로 당겨 다음 TTL까지 재적재를 다시
            # 트리거하지 않는다(DB가 잠깐 죽었을 때 매 조회가 스레드를 띄우는 걸 막는다).
            self._index_loaded_at = time.monotonic()
            logger.warning("카탈로그 인덱스 재적재 실패 — 옛 인덱스 유지", exc_info=True)

    def _reload_triggers(self) -> None:
        """백그라운드 트리거 재적재. 실패 시 옛 목록 유지 + loaded_at 백오프."""
        loaded = self._load_triggers()
        if loaded is None:  # 일시 실패 — 옛 목록 유지, 다음 TTL까지 백오프
            self._triggers_loaded_at = time.monotonic()
            return
        self._triggers = loaded
        self._triggers_loaded_at = time.monotonic()

    def _load(self) -> dict[tuple[str, str], dict]:
        # 지연 임포트 — 카탈로그를 실제로 쓸 때만 DB(psycopg)에 의존하게 한다.
        from app.rag.store import db

        index: dict[tuple[str, str], dict] = {}
        # connect/statement 둘 다 타임아웃 — 연결이든 쿼리든 무한 블로킹이면 재적재 스레드가
        # 안 끝나 플래그가 고착된다(RPA-225, Qodo 리뷰). ms 상수라 f-string 인젝션 없음.
        conn = db.connect(connect_timeout=_CATALOG_DB_TIMEOUT_SEC)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = {int(_CATALOG_DB_TIMEOUT_SEC * 1000)}")
                # ORDER BY 없이는 (pkg, act) 중복 시 어느 행을 채택할지가 DB 반환 순서에
                # 달려 빌드·프로세스마다 스펙이 흔들린다 — 재현 가능한 순서로 고정한다.
                # parent_id를 chunk_index보다 앞에 두어 같은 문서의 청크가 붙어 있게 한다.
                cur.execute(
                    """
                    SELECT package_name, action_name, metadata, parent_id
                    FROM rag_documents
                    WHERE source_type = 'action_schema'
                      AND package_name IS NOT NULL
                      AND action_name IS NOT NULL
                    ORDER BY package_name, action_name, parent_id, chunk_index, id
                    """
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        # 논리 중복 관측 — 같은 (pkg, act)에 **파라미터 집합이 서로 다른** 행이 있는가.
        #
        # 판정 기준을 parent_id(출처 문서)가 아니라 파라미터 집합으로 둔다. parent_id는
        # 나중에 추가된 nullable 컬럼이라 레거시 행이 전부 NULL이고, 그러면 서로 다른
        # 문서도 None == None으로 같아 보여 관측이 조용히 무력화된다(Qodo 리뷰).
        # 게다가 우리가 실제로 걱정하는 피해는 '출처가 둘'이 아니라 '채택 행에 따라
        # 파라미터가 달라져 검수 R2/R3가 오판하는 것'이라, 파라미터 집합이 더 정확한 신호다
        # (출처가 둘이어도 파라미터가 같으면 무해하다). parent_id는 추적 정보로만 싣는다.
        adopted_parent: dict[tuple[str, str], str | None] = {}
        adopted_params: dict[tuple[str, str], tuple | None] = {}
        conflicts: dict[tuple[str, str], dict] = {}

        for row in rows:
            # 컬럼 수에 관대하게 언팩 — parent_id 없이 3튜플을 주는 기존 스텁/구 호출부와 호환.
            package_name, action_name, metadata = row[0], row[1], row[2]
            parent_id = row[3] if len(row) > 3 else None
            key = (package_name, action_name)

            if isinstance(metadata, str):  # jsonb는 dict로 오지만 드라이버 차이에 방어적으로
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    metadata = None
            # 최상위가 dict가 아니면(배열·문자열·깨진 JSON) schema 없음으로 처리 — 한 행의
            # 비정상 metadata가 전체 적재를 AttributeError로 무너뜨리지 않게 한다.
            schema = metadata.get("schema") if isinstance(metadata, dict) else None
            if isinstance(schema, dict):
                # package/action을 상위 키로 부여해 문서화된 스펙 형태와 맞춘다
                # (schema에는 두 키가 없으므로 덮어쓰지 않는다).
                spec = {"package": package_name, "action": action_name, **schema}
                if spec.get("parameters") is None:
                    # v2 보강은 '파라미터 미상'을 schema.parameters=None으로 기록한다 — 키를
                    # 제거해 schema 없는 행과 같은 params_unknown 형태로 정규화한다. None인 채로
                    # 흘리면 v1/v2 검수의 spec.get("parameters", [])가 None을 받아 순회에서 깨진다.
                    spec.pop("parameters", None)
                    spec["params_unknown"] = True
            else:
                # schema 없는 행도 어휘로는 실존한다(v2 문서 카탈로그의 보강 미도달 행 —
                # --enrich 생략 빌드면 액션의 ~92%). parameters 키를 아예 두지 않아
                # 소비처(.get("parameters"))가 '스펙 미상'(None)을 '파라미터 없음'([])과
                # 구분하게 한다.
                spec = {"package": package_name, "action": action_name, "params_unknown": True}

            params = _param_names(spec)
            # 채택 규칙: 스펙 미상 자리에는 나중에 온 실스펙이 들어온다. 이미 실스펙이 있으면
            # 정렬 순서상 앞선 그 행을 유지한다.
            existing = index.get(key)
            if existing is None or existing.get("params_unknown"):
                index[key] = spec
                adopted_parent[key], adopted_params[key] = parent_id, params
                continue

            # 버려지는 행이다. 파라미터 집합이 채택본과 다르면 그 차이가 곧 검수 오판의 씨앗이다.
            if params is not None and adopted_params.get(key) is not None and params != adopted_params[key]:
                c = conflicts.setdefault(key, {"sources": set(), "param_sets": set()})
                c["sources"].update({str(adopted_parent.get(key)), str(parent_id)})
                c["param_sets"].update({adopted_params[key], params})

        for (package_name, action_name), c in sorted(conflicts.items()):
            # 채택 행이 바뀌면 검수(R2/R3)가 실존 파라미터를 '스펙에 없음'으로 오판할 수
            # 있다 — 수집 쪽에서 이름 정규화를 고칠 때까지 발현 여부를 로그로 관측한다.
            # 실제로 채택된 출처를 함께 남긴다(어느 행이 이겼는지 모르면 추적이 안 된다).
            logger.warning(
                "카탈로그 논리 중복 — (%s, %s)에 파라미터 집합이 다른 행이 %d종. "
                "채택: parent_id=%s (파라미터 %d개). 관련 출처: %s. "
                "나머지 행의 파라미터는 인덱스에 없어 검수가 오판할 수 있다",
                package_name,
                action_name,
                len(c["param_sets"]),
                adopted_parent.get((package_name, action_name)),
                len(adopted_params.get((package_name, action_name)) or ()),
                sorted(c["sources"]),
            )
        logger.info("BackendCatalog 적재 완료: 액션 스펙 %d개", len(index))
        return index

    def _ensure_index(self) -> dict[tuple[str, str], dict]:
        # 첫 적재는 **동기**로 막고 기다린다 — 쓸 인덱스가 아예 없으니 옛 값을 줄 수 없다.
        # 첫 적재 실패는 그대로 올린다(기존 동작).
        if self._index is None:
            with self._lock:  # 병렬 step 노드의 최초 조회 경합 방지 (더블 체크)
                if self._index is None:
                    self._index = self._load()
                    self._index_loaded_at = time.monotonic()
            return self._index
        # 이미 인덱스가 있으면 stale이어도 **호출자를 막지 않는다**: 옛 인덱스를 즉시
        # 돌려주고 갱신은 백그라운드로 (stale-while-revalidate, RPA-225 — Qodo 리뷰 반영).
        if self._is_stale(self._index_loaded_at):
            self._start_reload("_reloading_index", self._reload_index)
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
        # 첫 적재는 동기 (_ensure_index와 대칭, RPA-225). 첫 적재 실패는 캐싱하지 않고
        # 빈 목록을 반환해 다음 조회에서 재시도한다(옛 계약 유지).
        if self._triggers is None:
            with self._lock:
                if self._triggers is None:
                    loaded = self._load_triggers()
                    if loaded is None:
                        return []
                    self._triggers = loaded
                    self._triggers_loaded_at = time.monotonic()
            return self._triggers
        # 이미 목록이 있으면 stale이어도 옛 목록을 즉시 반환하고 갱신은 백그라운드로.
        if self._is_stale(self._triggers_loaded_at):
            self._start_reload("_reloading_triggers", self._reload_triggers)
        return self._triggers

    def _load_triggers(self) -> list[dict] | None:
        from app.rag.store import db

        try:
            # 재적재가 무한 블로킹으로 스레드/플래그를 붙잡지 않게 타임아웃 (RPA-225, _load와 동일).
            conn = db.connect(connect_timeout=_CATALOG_DB_TIMEOUT_SEC)
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SET statement_timeout = {int(_CATALOG_DB_TIMEOUT_SEC * 1000)}")
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
