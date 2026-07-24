"""BackendCatalog._load — schema 없는 행의 params_unknown 최소 스펙 적재 (RPA-206 후속).

v2 문서 카탈로그는 dl 추출 실패 + LLM 보강 미도달 행이 metadata.schema=None으로 온다.
그런 행을 인덱스에서 제외하면 검수 R1이 실존 액션을 환각으로 오판하므로, 존재(행)와
스펙(schema)을 분리해 적재하는 계약을 검증한다. DB는 가짜 커넥션으로 대체한다.
"""

import pytest

from app.services import catalog as cat_mod
from app.services.catalog import BackendCatalog


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        pass

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)

    def close(self):
        pass


def _catalog_with(rows, monkeypatch):
    from app.rag.store import db

    # connect는 이제 connect_timeout= 키워드를 받는다(RPA-225) — 인자를 흡수한다.
    monkeypatch.setattr(db, "connect", lambda **kw: _FakeConn(rows))
    return BackendCatalog()


def test_schema_row_loads_full_spec(monkeypatch):
    rows = [("Excel advanced", "Open", {"schema": {"name": "Open", "parameters": [{"name": "session"}]}})]
    cat = _catalog_with(rows, monkeypatch)
    spec = cat.get_action_schema("Excel advanced", "Open")
    assert spec["parameters"] == [{"name": "session"}]
    assert "params_unknown" not in spec


def test_schemaless_row_becomes_params_unknown_not_dropped(monkeypatch):
    # 이전 동작: schema 없으면 행 제외 → get_action_schema=None → R1 '카탈로그에 없음' 오판.
    rows = [("Google Drive", "Move file", {"doc_uid": "x", "schema": None})]
    cat = _catalog_with(rows, monkeypatch)
    spec = cat.get_action_schema("Google Drive", "Move file")
    assert spec is not None
    assert spec["params_unknown"] is True
    assert spec.get("parameters") is None  # '미상'은 빈 목록([])과 구분된다


def test_schema_with_null_parameters_normalized_to_params_unknown(monkeypatch):
    # v2 보강은 '파라미터 미상'을 schema.parameters=None으로 기록한다 — None인 채로 흘리면
    # v1/v2 검수의 spec.get("parameters", [])가 None을 받아 깨지므로, 키를 제거한
    # params_unknown 형태로 정규화되어야 한다.
    rows = [("REST Web Services", "Delete method",
             {"schema": {"name": "Delete method", "parameters": None}})]
    cat = _catalog_with(rows, monkeypatch)
    spec = cat.get_action_schema("REST Web Services", "Delete method")
    assert spec["params_unknown"] is True
    assert "parameters" not in spec


def test_schema_row_upgrades_earlier_placeholder(monkeypatch):
    # 같은 (pkg, act)에 schema 없는 행이 먼저 와도, schema 보유 행이 최소 스펙을 대체한다.
    rows = [
        ("Email", "Send", {"schema": None}),
        ("Email", "Send", {"schema": {"name": "Send", "parameters": []}}),
    ]
    cat = _catalog_with(rows, monkeypatch)
    spec = cat.get_action_schema("Email", "Send")
    assert "params_unknown" not in spec
    assert spec["parameters"] == []


def test_malformed_metadata_rows_do_not_break_load(monkeypatch):
    # 비정상 metadata(배열 JSON 문자열·깨진 JSON·비딕셔너리)가 한 행이라도 있으면 전체
    # 적재가 AttributeError로 무너지던 것 방지 (PR #283 CodeRabbit 리뷰 반영) — 해당
    # 행은 params_unknown 최소 스펙으로 살리고, 같은 배치의 정상 행은 그대로 적재된다.
    rows = [
        ("Excel advanced", "Open", {"schema": {"name": "Open", "parameters": []}}),
        ("Broken", "ArrayMeta", "[1, 2]"),
        ("Broken", "BadJson", "{not json"),
        ("Broken", "ListMeta", [1, 2]),
    ]
    cat = _catalog_with(rows, monkeypatch)
    assert cat.get_action_schema("Excel advanced", "Open")["parameters"] == []
    for act in ("ArrayMeta", "BadJson", "ListMeta"):
        spec = cat.get_action_schema("Broken", act)
        assert spec is not None and spec["params_unknown"] is True


def test_trigger_menu_failure_not_cached(monkeypatch):
    # 일시 DB 실패가 빈 목록으로 캐싱되면 복구 후에도 재시작 전까지 트리거 제안이 죽는다
    # (PR #288 CodeRabbit 리뷰 반영) — 실패는 캐싱하지 않고 다음 호출에서 재시도한다.
    from app.rag.store import db

    calls = {"n": 0}
    rows = [("Email trigger", "Creating an email trigger", "/x", "메일 수신 시 실행", 0)]

    def _connect(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db down")
        return _FakeConn(rows)

    monkeypatch.setattr(db, "connect", _connect)
    cat = BackendCatalog()
    assert cat.list_trigger_schemas() == []  # 실패 — 빈 목록 반환, 캐싱 안 함
    out = cat.list_trigger_schemas()  # DB 복구 후 재시도 성공
    assert [r["package"] for r in out] == ["Email trigger"]


def test_placeholder_never_overwrites_schema_row(monkeypatch):
    rows = [
        ("Email", "Send", {"schema": {"name": "Send", "parameters": []}}),
        ("Email", "Send", {"schema": None}),
    ]
    cat = _catalog_with(rows, monkeypatch)
    assert "params_unknown" not in cat.get_action_schema("Email", "Send")


# --- TTL 재적재 (RPA-225) — 적재 후 옛 카탈로그 영구화 방지 ---

def _clock(monkeypatch, start=1000.0):
    """catalog의 monotonic 시계를 제어 가능하게 대체한다."""
    box = {"t": start}
    monkeypatch.setattr(cat_mod.time, "monotonic", lambda: box["t"])
    return box


def _counting_connect(monkeypatch, rows_by_call):
    """호출 순서별로 다른 rows를 주는 가짜 connect. 호출 횟수를 함께 돌려준다."""
    from app.rag.store import db

    calls = {"n": 0}

    def _connect(**kw):
        calls["n"] += 1
        rows = rows_by_call(calls["n"])
        if isinstance(rows, Exception):
            raise rows
        return _FakeConn(rows)

    monkeypatch.setattr(db, "connect", _connect)
    return calls


def _wait_reload(cat, flag_attr, timeout=5.0):
    """백그라운드 재적재 스레드가 끝날 때까지 기다린다(플래그가 내려갈 때까지 폴링).

    catalog의 monotonic만 monkeypatch되고 time.time/sleep은 실제라 폴링이 가능하다.
    """
    import time as real_time

    deadline = real_time.time() + timeout
    while getattr(cat, flag_attr) and real_time.time() < deadline:
        real_time.sleep(0.005)
    assert not getattr(cat, flag_attr), "백그라운드 재적재가 시간 내 끝나지 않음"


def test_no_reload_within_ttl(monkeypatch):
    """TTL 이내 반복 조회는 재적재하지 않는다 — 병렬 step 노드가 매번 DB를 때리면 안 된다."""
    _clock(monkeypatch)
    monkeypatch.setattr(cat_mod, "_CATALOG_TTL_SEC", 600.0)
    calls = _counting_connect(monkeypatch, lambda n: [("Excel", "Open", {"schema": {"name": "Open"}})])
    cat = BackendCatalog()
    for _ in range(5):
        cat.get_action_schema("Excel", "Open")
    assert calls["n"] == 1


def test_reload_after_ttl_reflects_new_ingest(monkeypatch):
    """TTL 경과 후 재적재(백그라운드)로 새로 적재된 액션을 본다 — 다중 인스턴스 수렴의 핵심."""
    clock = _clock(monkeypatch)
    monkeypatch.setattr(cat_mod, "_CATALOG_TTL_SEC", 600.0)
    calls = _counting_connect(monkeypatch, lambda n: (
        [("Excel", "Open", {"schema": {"name": "Open"}})] if n == 1
        else [("Excel", "Open", {"schema": {"name": "Open"}}),
              ("New", "Action", {"schema": {"name": "Action"}})]
    ))
    cat = BackendCatalog()
    assert cat.get_action_schema("New", "Action") is None  # 첫 동기 적재 — New 없음
    assert calls["n"] == 1
    clock["t"] += 601  # TTL 경과
    cat.get_action_schema("New", "Action")  # stale 조회가 백그라운드 재적재를 트리거
    #  (반환값은 옛 값/새 값 레이스라 검증하지 않는다 — non-blocking 보장은
    #   test_reload_is_nonblocking_during_slow_load가 느린 DB로 안정적으로 검증한다)
    _wait_reload(cat, "_reloading_index")  # 백그라운드 재적재 완료 대기
    assert calls["n"] == 2
    assert cat.get_action_schema("New", "Action") is not None  # 재적재 반영


def test_reload_is_nonblocking_during_slow_load(monkeypatch):
    """재적재(느린 DB) 중에도 다른 조회는 막히지 않는다 — Qodo 지적(락 블로킹)의 직접 검증."""
    import threading as _th

    clock = _clock(monkeypatch)
    monkeypatch.setattr(cat_mod, "_CATALOG_TTL_SEC", 600.0)
    release = _th.Event()
    from app.rag.store import db

    calls = {"n": 0}

    def _connect(**kw):
        calls["n"] += 1
        if calls["n"] >= 2:  # 재적재는 release 전까지 블록(느린 DB 시뮬레이션)
            release.wait(timeout=5)
        return _FakeConn([("Excel", "Open", {"schema": {"name": "Open"}})])

    monkeypatch.setattr(db, "connect", _connect)
    cat = BackendCatalog()
    cat.get_action_schema("Excel", "Open")  # 첫 동기 적재
    clock["t"] += 601
    cat.get_action_schema("Excel", "Open")  # 백그라운드 재적재 트리거(release 대기 중)
    assert cat._reloading_index is True  # 재적재 진행 중
    # 재적재가 DB에 매여 있어도 이 조회는 옛 값을 즉시 받아야 한다(락 대기로 막히면 실패)
    assert cat.get_action_schema("Excel", "Open") is not None
    release.set()
    _wait_reload(cat, "_reloading_index")


def test_reload_failure_keeps_old_index_and_backs_off(monkeypatch):
    """재적재 실패 시 옛 인덱스를 유지하고, 다음 TTL까지 재시도하지 않는다(매 조회 DB 폭격 방지)."""
    clock = _clock(monkeypatch)
    monkeypatch.setattr(cat_mod, "_CATALOG_TTL_SEC", 600.0)
    calls = _counting_connect(monkeypatch, lambda n: (
        [("Excel", "Open", {"schema": {"name": "Open"}})] if n == 1 else RuntimeError("db down")
    ))
    cat = BackendCatalog()
    assert cat.get_action_schema("Excel", "Open") is not None  # 첫 적재 성공
    clock["t"] += 601
    assert cat.get_action_schema("Excel", "Open") is not None  # 옛 것 즉시 반환(예외 X)
    _wait_reload(cat, "_reloading_index")  # 백그라운드 재적재(실패) 완료
    assert calls["n"] == 2  # 재적재 시도함
    assert cat.get_action_schema("Excel", "Open") is not None  # 실패해도 옛 것 유지
    cat.get_action_schema("Excel", "Open")  # 백오프 — 같은 창에선 재트리거 안 함
    assert calls["n"] == 2


def test_first_load_failure_raises(monkeypatch):
    """첫 적재 실패는 올린다 — 쓸 인덱스가 아예 없다(기존 동작 보존)."""
    _clock(monkeypatch)
    _counting_connect(monkeypatch, lambda n: RuntimeError("db down"))
    cat = BackendCatalog()
    with pytest.raises(RuntimeError):
        cat.get_action_schema("Excel", "Open")


def test_ttl_zero_means_infinite_cache(monkeypatch):
    """TTL<=0이면 재적재하지 않는다(무한 캐시, RPA-225 이전 동작)."""
    clock = _clock(monkeypatch)
    monkeypatch.setattr(cat_mod, "_CATALOG_TTL_SEC", 0)
    calls = _counting_connect(monkeypatch, lambda n: [("Excel", "Open", {"schema": {"name": "Open"}})])
    cat = BackendCatalog()
    cat.get_action_schema("Excel", "Open")
    clock["t"] += 100_000
    cat.get_action_schema("Excel", "Open")
    assert calls["n"] == 1


def test_trigger_menu_reloads_after_ttl(monkeypatch):
    """트리거 메뉴도 TTL 경과 후 백그라운드 재적재한다(index와 대칭)."""
    clock = _clock(monkeypatch)
    monkeypatch.setattr(cat_mod, "_CATALOG_TTL_SEC", 600.0)
    calls = _counting_connect(monkeypatch, lambda n: (
        [("T1", "trig one", "/x", "c", 0)] if n == 1
        else [("T1", "trig one", "/x", "c", 0), ("T2", "trig two", "/y", "c", 0)]
    ))
    cat = BackendCatalog()
    assert len(cat.list_trigger_schemas()) == 1  # 첫 동기 적재
    clock["t"] += 601
    cat.list_trigger_schemas()  # stale 조회가 백그라운드 재적재를 트리거(반환값은 레이스)
    _wait_reload(cat, "_reloading_triggers")
    assert len(cat.list_trigger_schemas()) == 2  # 재적재 반영
    assert calls["n"] == 2


def test_reload_thread_start_failure_rolls_back_flag(monkeypatch):
    """스레드 시작 실패 시 재적재 플래그가 롤백된다 — 영구 stale 고착 방지 (Qodo 리뷰).

    _start_reload가 플래그를 True로 세운 뒤 Thread.start()가 실패하면 worker가 안 돌아
    finally가 플래그를 못 내린다. 롤백이 없으면 이후 재적재가 영원히 억제된다.
    """
    import threading as _th

    clock = _clock(monkeypatch)
    monkeypatch.setattr(cat_mod, "_CATALOG_TTL_SEC", 600.0)
    calls = _counting_connect(monkeypatch, lambda n: [("Excel", "Open", {"schema": {"name": "Open"}})])
    cat = BackendCatalog()
    cat.get_action_schema("Excel", "Open")  # 첫 적재
    clock["t"] += 601

    orig_start = _th.Thread.start
    monkeypatch.setattr(_th.Thread, "start", lambda self: (_ for _ in ()).throw(RuntimeError("no thread")))
    cat.get_action_schema("Excel", "Open")  # stale → _start_reload → start 실패
    assert cat._reloading_index is False, "start 실패 후 플래그가 True로 고착됨"

    # start 복구 후 다음 stale 조회는 재적재를 다시 시도할 수 있어야 한다
    monkeypatch.setattr(_th.Thread, "start", orig_start)
    cat.get_action_schema("Excel", "Open")
    _wait_reload(cat, "_reloading_index")
    assert calls["n"] == 2  # 재적재가 다시 돌았다(영구 정지 아님)


def test_load_applies_db_timeouts(monkeypatch):
    """_load가 connect_timeout과 statement_timeout을 건다 — 무한 블로킹 방지 (Qodo 리뷰)."""
    from app.rag.store import db

    captured = {"connect_timeout": "unset", "statements": []}

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql):
            captured["statements"].append(sql)

        def fetchall(self):
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    def _connect(**kw):
        captured["connect_timeout"] = kw.get("connect_timeout")
        return _Conn()

    monkeypatch.setattr(db, "connect", _connect)
    BackendCatalog().get_action_schema("x", "y")
    assert captured["connect_timeout"] == cat_mod._CATALOG_DB_TIMEOUT_SEC
    assert any("statement_timeout" in s for s in captured["statements"])


# ─────────────────────────────────────────────────────────────────────────────
# 적재 순서 결정성 + 논리 중복 관측 (RPA-287)
# ─────────────────────────────────────────────────────────────────────────────

def _schema_row(pkg, act, parent_id, params):
    """(pkg, act, metadata, parent_id) 4튜플 — 현행 쿼리가 돌려주는 행 모양."""
    return (pkg, act, {"schema": {"name": act, "parameters": params}}, parent_id)


def test_load_adopts_first_row_in_sorted_order(monkeypatch):
    """정렬은 SQL이 하지만, 채택 규칙이 '첫 행'임을 고정한다 — 뒤 행이 덮어쓰면 안 된다."""
    rows = [
        _schema_row("Excel advanced", "Open", "doc-a", [{"name": "session"}]),
        _schema_row("Excel advanced", "Open", "doc-b", [{"name": "완전히_다른_파라미터"}]),
    ]
    cat = _catalog_with(rows, monkeypatch)
    assert cat.get_action_schema("Excel advanced", "Open")["parameters"] == [{"name": "session"}]


def test_load_query_orders_deterministically(monkeypatch):
    """ORDER BY가 없으면 어느 행이 채택될지가 DB 반환 순서에 달린다 — 쿼리에 고정돼야 한다."""
    seen = {}

    class _CapturingCursor(_FakeCursor):
        def execute(self, sql):
            if "rag_documents" in sql:
                seen["sql"] = " ".join(sql.split())

    class _CapturingConn(_FakeConn):
        def cursor(self):
            return _CapturingCursor(self._rows)

    from app.rag.store import db
    monkeypatch.setattr(db, "connect", lambda **kw: _CapturingConn([]))
    BackendCatalog()._load()

    sql = seen["sql"]
    assert "ORDER BY package_name, action_name, parent_id, chunk_index, id" in sql
    # parent_id가 chunk_index보다 앞 — 같은 문서의 청크가 흩어지지 않게
    assert sql.index("parent_id, chunk_index") > 0


def test_logical_duplicate_is_warned(monkeypatch, caplog):
    """같은 (pkg, act)에 출처 문서가 둘 — 조용히 버리지 않고 경고로 남긴다."""
    rows = [
        _schema_row("Excel advanced", "Open", "doc-a", [{"name": "session"}]),
        _schema_row("Excel advanced", "Open", "doc-b", [{"name": "other"}]),
    ]
    with caplog.at_level("WARNING"):
        _catalog_with(rows, monkeypatch)._load()
    msgs = [r.getMessage() for r in caplog.records]
    assert any("논리 중복" in m and "Excel advanced" in m for m in msgs)
    # 어느 문서들이 충돌했는지 실어야 수집 쪽에서 추적할 수 있다
    assert any("doc-a" in m and "doc-b" in m for m in msgs)


def test_same_document_chunks_are_silent(monkeypatch, caplog):
    """같은 문서의 청킹 복수 행은 정상 — 경고를 내면 로그가 소음으로 덮인다."""
    rows = [
        _schema_row("Excel advanced", "Open", "doc-a", [{"name": "session"}]),
        _schema_row("Excel advanced", "Open", "doc-a", [{"name": "session"}]),
    ]
    with caplog.at_level("WARNING"):
        _catalog_with(rows, monkeypatch)._load()
    assert not any("논리 중복" in r.getMessage() for r in caplog.records)


def test_three_column_rows_still_load(monkeypatch):
    """parent_id 없는 3튜플(구 스텁·구 호출부)도 깨지지 않는다 — 하위호환."""
    rows = [("Excel advanced", "Open", {"schema": {"name": "Open", "parameters": [{"name": "session"}]}})]
    cat = _catalog_with(rows, monkeypatch)
    assert cat.get_action_schema("Excel advanced", "Open")["parameters"] == [{"name": "session"}]
