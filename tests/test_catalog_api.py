"""패키지/액션 카탈로그 API 테스트 (RPA-313)."""

from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.api.catalog as catalog_api
from app.main import app
from app.services.catalog import BackendCatalog, _public_param

# ("If","if")는 checker.CONTAINER_ACTIONS에 있고, Excel_MS 액션들은 컨테이너가 아니다.
_SPECS = [
    {"package": "Excel_MS", "action": "GoToCell", "label": "셀로 이동",
     "parameters": [{"name": "cellOption", "label": "셀 옵션", "type": "RADIO", "required": True,
                     "options": [{"label": "특정 셀", "value": "specific"}],
                     "default": {"string": "specific", "type": "STRING"}}]},
    {"package": "If", "action": "if", "label": "조건", "params_unknown": True},
    {"package": "Excel_MS", "action": "OpenWorkbook", "label": "열기", "parameters": []},
]


def test_public_param_normalizes_default_options_required():
    p = _public_param({"name": "n", "label": "라벨", "type": "NUMBER", "required": True,
                       "default": {"number": 5, "type": "NUMBER"},
                       "options": [{"label": "A", "value": "a"}], "description": "설명"})
    assert p["default"] == 5            # nested {number,type} → 스칼라
    assert p["required"] is True
    assert p["label"] == "라벨"
    assert p["options"] == [{"label": "A", "value": "a"}]
    assert p["description"] == "설명"

    bare = _public_param({"name": "x", "type": "TEXT"})
    assert bare["required"] is False and bare["label"] == "x"
    assert "default" not in bare and "options" not in bare

    # 재귀 typed-value 봉투(SESSION이 STRING 봉투를 감쌈) → 안쪽 스칼라까지 언랩
    sess = _public_param({"name": "session", "type": "SESSION",
                          "default": {"type": "SESSION", "sessionName": {"type": "STRING", "string": "Default"}}})
    assert sess["default"] == "Default"

    # 다중 키 봉투(EXCEPTION)는 스칼라화 불가 → 구조 유지
    exc = _public_param({"name": "e", "type": "EXCEPTION",
                         "default": {"type": "EXCEPTION", "packageName": "ErrorHandler", "exceptionName": "BotException"}})
    assert isinstance(exc["default"], dict)


def test_list_package_catalog_groups_sorts_and_flags_containers(monkeypatch):
    cat = BackendCatalog()
    monkeypatch.setattr(cat, "iter_action_schemas", lambda: iter(_SPECS))
    monkeypatch.setattr(cat, "_load_package_labels", lambda: {"Excel_MS": "Excel 고급"})

    out = cat.list_package_catalog()

    assert [p["package"] for p in out] == ["Excel_MS", "If"]  # 패키지 정렬
    excel = out[0]
    assert excel["label"] == "Excel 고급"                      # package_overview 라벨
    assert [a["action"] for a in excel["actions"]] == ["GoToCell", "OpenWorkbook"]  # 액션 정렬
    goto = excel["actions"][0]
    assert goto["isContainer"] is False
    assert goto["parameters"][0]["default"] == "specific"     # nested → 스칼라
    assert goto["parameters"][0]["options"] == [{"label": "특정 셀", "value": "specific"}]
    assert excel["actions"][1]["parameters"] == []            # 스키마 있고 파라미터 0개

    if_pkg = out[1]
    assert if_pkg["label"] == "If"                            # 라벨 없음 → machine명 폴백
    if_action = if_pkg["actions"][0]
    assert if_action["isContainer"] is True                   # ("If","if") ∈ CONTAINER_ACTIONS
    assert "parameters" not in if_action                      # params_unknown → parameters 생략


def test_package_labels_cached_across_calls(monkeypatch):
    """패키지 라벨 조회는 캐시된다 — 매 요청 DB 히트 금지 (Qodo #424)."""
    cat = BackendCatalog()
    calls = {"n": 0}

    def _load():
        calls["n"] += 1
        return {"Excel_MS": "Excel 고급"}

    monkeypatch.setattr(cat, "iter_action_schemas", lambda: iter(_SPECS))
    monkeypatch.setattr(cat, "_load_package_labels", _load)
    cat.list_package_catalog()
    cat.list_package_catalog()
    assert calls["n"] == 1  # 두 번째 호출은 캐시 — DB 재조회 없음


def test_catalog_packages_endpoint(monkeypatch):
    fake = SimpleNamespace(list_package_catalog=lambda: [
        {"package": "Excel_MS", "label": "Excel 고급",
         "actions": [{"action": "GoToCell", "label": "셀로 이동", "isContainer": False, "parameters": []}]},
    ])
    monkeypatch.setattr(catalog_api, "get_backend_catalog", lambda: fake)
    with TestClient(app) as c:
        r = c.get("/api/catalog/packages")
    assert r.status_code == 200
    body = r.json()
    assert body["packages"][0]["package"] == "Excel_MS"        # machine명
    assert body["packages"][0]["label"] == "Excel 고급"
    assert body["packages"][0]["actions"][0]["action"] == "GoToCell"
