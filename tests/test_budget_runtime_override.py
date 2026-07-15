"""예산 상한 런타임 오버라이드 (RPA-173).

RPA-149(retrieval_params)와 같은 패턴이되 **한 곳에서 갈린다**: 조회 실패 시 저하 방향.
retrieval_params는 config로 저하해도 검색이 조금 덜 최적일 뿐이지만, 예산에서 같은 저하는
**관리자가 건 상한이 사라져 비용이 새는** 것이다. 그래서 캐시가 있으면 지킨다.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.services import budget

UID = uuid.uuid4()
SID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    budget.bust_cache()
    for env in budget._LIMIT_ENV.values():
        monkeypatch.delenv(env, raising=False)
    yield
    budget.bust_cache()


def _override(monkeypatch, **limits):
    """budget_limits 최신 행이 있는 상태."""
    full = {"subject_daily": None, "subject_monthly": None,
            "global_daily": None, "global_monthly": None, **limits}
    monkeypatch.setattr(budget, "_read_override", lambda: full)


def _read_fails(monkeypatch):
    def _boom():
        raise RuntimeError("DB 다운")
    monkeypatch.setattr(budget, "_read_override", _boom)


# --- 오버라이드 없음 = env (기존 동작 불변) ---

def test_no_override_falls_back_to_env(monkeypatch):
    monkeypatch.setattr(budget, "_read_override", lambda: None)
    monkeypatch.setenv("BUDGET_SUBJECT_DAILY_USD", "6")
    assert budget.active_limits()["subject_daily"] == 6.0


def test_no_override_no_env_means_all_disabled(monkeypatch):
    monkeypatch.setattr(budget, "_read_override", lambda: None)
    assert all(v is None for v in budget.active_limits().values())


# --- 오버라이드가 env를 이긴다 ---

def test_db_override_wins_over_env(monkeypatch):
    monkeypatch.setenv("BUDGET_SUBJECT_DAILY_USD", "6")
    _override(monkeypatch, subject_daily=99.0)
    assert budget.active_limits()["subject_daily"] == 99.0


def test_override_replaces_env_wholesale(monkeypatch):
    """행이 있으면 4개 값이 통째로 env를 대체한다 — 일부만 오버라이드하는 혼합 규칙은
    '지금 실제 상한이 얼마냐'를 추적 불가능하게 만든다."""
    monkeypatch.setenv("BUDGET_GLOBAL_DAILY_USD", "15")
    _override(monkeypatch, subject_daily=99.0)  # global은 행에서 NULL
    limits = budget.active_limits()
    assert limits["subject_daily"] == 99.0
    assert limits["global_daily"] is None, "행이 있으면 env가 섞여 들어오면 안 된다"


# --- 캐시 ---

def test_cache_avoids_db_on_every_turn(monkeypatch):
    """/turn hot path — TTL 내엔 DB를 다시 안 읽는다."""
    calls = {"n": 0}

    def _counted():
        calls["n"] += 1
        return {"subject_daily": 5.0, "subject_monthly": None,
                "global_daily": None, "global_monthly": None}

    monkeypatch.setattr(budget, "_read_override", _counted)
    for _ in range(5):
        budget.active_limits()
    assert calls["n"] == 1


def test_bust_cache_applies_change_without_restart(monkeypatch):
    """admin PUT 직후 bust_cache()로 즉시 반영 — 재시작 없이."""
    _override(monkeypatch, subject_daily=5.0)
    assert budget.active_limits()["subject_daily"] == 5.0
    _override(monkeypatch, subject_daily=50.0)
    assert budget.active_limits()["subject_daily"] == 5.0, "TTL 내엔 캐시"
    budget.bust_cache()
    assert budget.active_limits()["subject_daily"] == 50.0


# --- 조회 실패 시 저하 방향 (RPA-149와 갈리는 지점) ---

def test_read_failure_keeps_last_known_limits(monkeypatch):
    """조회가 실패해도 **직전 상한을 지킨다** — 관리자가 건 상한이 조용히 사라지면 비용이 샌다."""
    _override(monkeypatch, subject_daily=5.0)
    assert budget.active_limits()["subject_daily"] == 5.0

    _read_fails(monkeypatch)
    budget._cache = (budget._cache[0] - 999, budget._cache[1])  # TTL 만료 강제
    assert budget.active_limits()["subject_daily"] == 5.0, "실패 시 상한이 사라지면 안 된다"


def test_read_failure_without_cache_falls_back_to_env(monkeypatch):
    """한 번도 못 읽었으면 env로 — 배포에 명시된 값이 '아무 상한 없음'보다 안전한 기본."""
    _read_fails(monkeypatch)
    monkeypatch.setenv("BUDGET_SUBJECT_DAILY_USD", "6")
    assert budget.active_limits()["subject_daily"] == 6.0


# --- 값 저하 규칙 ---

@pytest.mark.parametrize("bad", [0, -1, -0.5])
def test_nonpositive_db_value_disables_that_limit(monkeypatch, bad):
    """직접 SQL 등으로 0·음수가 들어가 있어도 None으로 저하 — env와 같은 규칙."""
    assert budget._positive_or_none(bad) is None


def test_check_budget_uses_override(monkeypatch):
    """오버라이드가 실제 판정에 먹는다 — 로더만 고치고 check_budget이 안 쓰면 무의미."""
    _override(monkeypatch, subject_daily=1.0)
    monkeypatch.setattr(budget, "_spent", lambda s, since, subject: 5.0)
    import app.core.observability_db as obs

    class _SM:
        def __call__(self): return self
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(obs, "observability_sessionmaker", lambda: _SM())
    v = budget.check_budget(budget.subject_of(SimpleNamespace(id=UID), SID))
    assert v.exceeded and v.limit_usd == 1.0


# --- admin API (RPA-149의 retrieval-params 테스트와 같은 구조) ---

class _FakeSession:
    def __init__(self, row=None):
        self.row = row
        self.added = []

    def query(self, model):
        return self

    def order_by(self, *a):
        return self

    def first(self):
        return self.row

    def add(self, row):
        self.added.append(row)

    def commit(self):
        pass

    def refresh(self, row):
        row.created_at = None


def _auth_admin():
    import app.api.admin as admin_api
    from app.main import app

    app.dependency_overrides[admin_api.require_admin] = lambda: SimpleNamespace(
        id=uuid.uuid4(), email="admin@test.com")


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    from app.main import app
    app.dependency_overrides.clear()


def test_get_returns_config_when_no_override(monkeypatch):
    """GET: 오버라이드 없으면 source=config + .env 값 (ops 화면 프리필용)."""
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

    monkeypatch.setenv("BUDGET_SUBJECT_DAILY_USD", "6")
    _auth_admin()
    app.dependency_overrides[get_db] = lambda: _FakeSession(row=None)
    with TestClient(app) as c:
        r = c.get("/api/admin/budget-limits")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "config" and body["subject_daily_usd"] == 6.0
    assert body["updated_by"] is None


def test_put_persists_and_busts_cache(monkeypatch):
    """PUT: 200·source=db·행 추가·캐시 무효화(무중단 반영)."""
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

    _auth_admin()
    fake = _FakeSession(row=None)
    app.dependency_overrides[get_db] = lambda: fake
    budget._cache = (0.0, budget._env_limits())  # 캐시가 차 있다고 가정
    payload = {"subject_daily_usd": 6, "subject_monthly_usd": 120,
               "global_daily_usd": 15, "global_monthly_usd": 300}
    with TestClient(app) as c:
        r = c.put("/api/admin/budget-limits", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "db" and body["subject_daily_usd"] == 6
    assert body["updated_by"] == "admin@test.com"
    assert len(fake.added) == 1 and fake.added[0].global_monthly_usd == 300
    assert budget._cache is None, "bust_cache가 안 불렸다 — 변경이 최대 30초 안 먹는다"


def test_put_service_identity_sets_updated_by_service():
    """X-API-Key(머신) 경로는 user가 None이라 updated_by=service — 감사 주체 구분."""
    from fastapi.testclient import TestClient

    import app.api.admin as admin_api
    from app.db import get_db
    from app.main import app

    app.dependency_overrides[admin_api.require_admin] = lambda: None  # 서비스 신원
    fake = _FakeSession(row=None)
    app.dependency_overrides[get_db] = lambda: fake
    with TestClient(app) as c:
        r = c.put("/api/admin/budget-limits", json={"subject_daily_usd": 6})
    assert r.status_code == 200 and r.json()["updated_by"] == "service"


@pytest.mark.parametrize("payload,why", [
    ({"subject_daily_usd": 0}, "0은 '비활성'이 아니라 오설정 — null로 끄게 강제"),
    ({"subject_daily_usd": -5}, "음수"),
    ({"subject_daily_usd": 10, "subject_monthly_usd": 5}, "월<일이면 일 상한이 무의미해짐"),
    ({"global_daily_usd": 20, "global_monthly_usd": 1}, "전역도 동일"),
])
def test_put_rejects_invalid_limits(payload, why):
    """잘못된 상한은 422로 거부 — 서비스를 막는 값이라 조용히 받으면 안 된다."""
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

    _auth_admin()
    app.dependency_overrides[get_db] = lambda: _FakeSession(row=None)
    with TestClient(app) as c:
        r = c.put("/api/admin/budget-limits", json=payload)
    assert r.status_code == 422, why


def test_put_allows_null_to_disable():
    """null은 '그 상한 비활성' — .env 미설정과 같은 의미라 허용해야 한다."""
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

    _auth_admin()
    app.dependency_overrides[get_db] = lambda: _FakeSession(row=None)
    with TestClient(app) as c:
        r = c.put("/api/admin/budget-limits", json={"subject_daily_usd": None})
    assert r.status_code == 200 and r.json()["subject_daily_usd"] is None
