"""BackendCatalog._load — schema 없는 행의 params_unknown 최소 스펙 적재 (RPA-206 후속).

v2 문서 카탈로그는 dl 추출 실패 + LLM 보강 미도달 행이 metadata.schema=None으로 온다.
그런 행을 인덱스에서 제외하면 검수 R1이 실존 액션을 환각으로 오판하므로, 존재(행)와
스펙(schema)을 분리해 적재하는 계약을 검증한다. DB는 가짜 커넥션으로 대체한다.
"""

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


def test_placeholder_never_overwrites_schema_row(monkeypatch):
    rows = [
        ("Email", "Send", {"schema": {"name": "Send", "parameters": []}}),
        ("Email", "Send", {"schema": None}),
    ]
    cat = _catalog_with(rows, monkeypatch)
    assert "params_unknown" not in cat.get_action_schema("Email", "Send")
