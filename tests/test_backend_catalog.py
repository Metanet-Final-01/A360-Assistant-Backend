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

    monkeypatch.setattr(db, "connect", lambda: _FakeConn(rows))
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

    def _connect():
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

    def _connect():
        calls["n"] += 1
        rows = rows_by_call(calls["n"])
        if isinstance(rows, Exception):
            raise rows
        return _FakeConn(rows)

    monkeypatch.setattr(db, "connect", _connect)
    return calls


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
    """TTL 경과 후 조회는 재적재해 새로 적재된 액션을 본다 — 다중 인스턴스 수렴의 핵심."""
    clock = _clock(monkeypatch)
    monkeypatch.setattr(cat_mod, "_CATALOG_TTL_SEC", 600.0)
    calls = _counting_connect(monkeypatch, lambda n: (
        [("Excel", "Open", {"schema": {"name": "Open"}})] if n == 1
        else [("Excel", "Open", {"schema": {"name": "Open"}}),
              ("New", "Action", {"schema": {"name": "Action"}})]
    ))
    cat = BackendCatalog()
    assert cat.get_action_schema("New", "Action") is None  # 적재 전 — 없음
    assert calls["n"] == 1
    clock["t"] += 601  # TTL 경과
    assert cat.get_action_schema("New", "Action") is not None  # 재적재로 반영
    assert calls["n"] == 2


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
    assert cat.get_action_schema("Excel", "Open") is not None  # 재적재 실패해도 옛 것 유지(예외 X)
    assert calls["n"] == 2
    cat.get_action_schema("Excel", "Open")  # 백오프 — 같은 창에선 재시도 안 함
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
    """트리거 메뉴도 TTL 경과 후 재적재한다(index와 대칭)."""
    clock = _clock(monkeypatch)
    monkeypatch.setattr(cat_mod, "_CATALOG_TTL_SEC", 600.0)
    calls = _counting_connect(monkeypatch, lambda n: (
        [("T1", "trig one", "/x", "c", 0)] if n == 1
        else [("T1", "trig one", "/x", "c", 0), ("T2", "trig two", "/y", "c", 0)]
    ))
    cat = BackendCatalog()
    assert len(cat.list_trigger_schemas()) == 1
    clock["t"] += 601
    assert len(cat.list_trigger_schemas()) == 2
    assert calls["n"] == 2
