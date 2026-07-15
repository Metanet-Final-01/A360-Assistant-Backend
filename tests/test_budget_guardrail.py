"""LLM 예산 가드레일 테스트 (RPA-171).

핵심은 세 가지다:
1. **미설정=비활성** — 예산을 안 켠 배포의 기존 동작을 바꾸지 않는다(DB도 안 읽는다).
2. **익명 구멍** — /turn이 익명을 허용하므로 user_id 상한만 있으면 그냥 뚫린다. session_id로 덮는다.
3. **전역 vs 주체 구분** — 전역 초과를 "당신이 많이 썼다"로 오해시키거나 남의 합계를 노출하면 안 된다.
"""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services import budget

UID = uuid.uuid4()
SID = uuid.uuid4()
NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clear_budget_env(monkeypatch):
    """상한 env·캐시를 매 테스트마다 초기화 — 개발자 .env와 앞 테스트가 새어들지 않게.

    캐시(RPA-173 런타임 오버라이드)는 모듈 전역이라 안 비우면 앞 테스트가 넣은 상한이 남아
    다음 테스트를 오염시킨다. DB 오버라이드도 없는 것으로 고정해 이 파일은 env 경로만 본다.
    """
    for k in ("BUDGET_SUBJECT_DAILY_USD", "BUDGET_SUBJECT_MONTHLY_USD",
              "BUDGET_GLOBAL_DAILY_USD", "BUDGET_GLOBAL_MONTHLY_USD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(budget, "_read_override", lambda: None)
    budget.bust_cache()
    yield
    budget.bust_cache()


def _spend(monkeypatch, amount: float):
    """관측 DB 조회를 대체해 누적 비용을 고정한다."""
    monkeypatch.setattr(budget, "_spent", lambda s, since, subject: amount)
    monkeypatch.setattr(budget, "observability_sessionmaker", lambda: _FakeSM(), raising=False)
    import app.core.observability_db as obs
    monkeypatch.setattr(obs, "observability_sessionmaker", lambda: _FakeSM())


class _FakeSM:
    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# --- 주체 해석 (익명 구멍) ---

def test_subject_is_user_when_logged_in():
    s = budget.subject_of(SimpleNamespace(id=UID), SID)
    assert (s.kind, s.id) == ("user", UID)


def test_subject_falls_back_to_session_when_anonymous():
    """익명이면 session_id로 건다 — 이게 없으면 익명 요청이 상한을 그냥 통과한다."""
    s = budget.subject_of(None, SID)
    assert (s.kind, s.id) == ("session", SID)


# --- 미설정 = 비활성 ---

def test_no_limits_configured_is_noop(monkeypatch):
    """상한이 없으면 통과하고 **DB를 아예 안 읽는다** (끈 배포에 쿼리 비용을 물리지 않는다)."""
    called = {"n": 0}

    def _boom():
        called["n"] += 1
        raise AssertionError("상한 미설정인데 관측 DB를 읽었다")

    import app.core.observability_db as obs
    monkeypatch.setattr(obs, "observability_sessionmaker", _boom)

    v = budget.check_budget(budget.subject_of(None, SID), now=NOW)
    assert v.exceeded is False and called["n"] == 0


@pytest.mark.parametrize("bad", ["", "  ", "abc", "0", "-5"])
def test_invalid_or_zero_limit_disables_that_check(monkeypatch, bad):
    """비정상·0·음수 상한은 그 검사를 끈다(fail-open) — 오설정으로 서비스를 막지 않는다."""
    monkeypatch.setenv("BUDGET_SUBJECT_DAILY_USD", bad)
    _spend(monkeypatch, 999.0)
    assert budget.check_budget(budget.subject_of(None, SID), now=NOW).exceeded is False


# --- 초과 판정 ---

def test_subject_daily_exceeded(monkeypatch):
    monkeypatch.setenv("BUDGET_SUBJECT_DAILY_USD", "1.0")
    _spend(monkeypatch, 1.5)
    v = budget.check_budget(budget.subject_of(SimpleNamespace(id=UID), SID), now=NOW)
    assert v.exceeded and v.scope == "subject" and v.period == "daily"
    assert v.spent_usd == 1.5 and v.limit_usd == 1.0


def test_under_limit_passes(monkeypatch):
    monkeypatch.setenv("BUDGET_SUBJECT_DAILY_USD", "1.0")
    _spend(monkeypatch, 0.99)
    assert budget.check_budget(budget.subject_of(None, SID), now=NOW).exceeded is False


def test_exactly_at_limit_is_exceeded(monkeypatch):
    """경계값: 상한에 '도달'하면 막는다(>=) — 넘긴 뒤 막으면 상한이 상한이 아니다."""
    monkeypatch.setenv("BUDGET_SUBJECT_DAILY_USD", "1.0")
    _spend(monkeypatch, 1.0)
    assert budget.check_budget(budget.subject_of(None, SID), now=NOW).exceeded is True


def test_global_limit_blocks_even_when_subject_is_fine(monkeypatch):
    """전역 상한은 주체가 멀쩡해도 막는다 — OpenAI 청구서 자체를 지키는 백스톱."""
    monkeypatch.setenv("BUDGET_GLOBAL_DAILY_USD", "10.0")
    _spend(monkeypatch, 50.0)
    v = budget.check_budget(budget.subject_of(None, SID), now=NOW)
    assert v.exceeded and v.scope == "global"


# --- 429 detail (사유 구분·정보 노출) ---

def test_global_detail_hides_spend_amount():
    """전역 초과 시 사용량을 노출하지 않는다 — 다른 사용자들의 합계가 새어나간다."""
    v = budget.BudgetVerdict(exceeded=True, scope="global", period="daily",
                             spent_usd=123.45, limit_usd=10.0, resets_at="2026-07-16T00:00:00+00:00")
    d = budget.exceeded_detail(v)
    assert d["code"] == "BUDGET_EXCEEDED" and d["scope"] == "global"
    assert "123.45" not in str(d) and "spent_usd" not in d
    assert "관리자" in d["message"]  # "당신이 많이 썼다"가 아니라 서비스 사유임을 알린다


def test_subject_detail_shows_own_usage():
    v = budget.BudgetVerdict(exceeded=True, scope="subject", period="monthly",
                             spent_usd=21.5, limit_usd=20.0, resets_at="2026-08-01T00:00:00+00:00")
    d = budget.exceeded_detail(v)
    assert d["spent_usd"] == 21.5 and d["limit_usd"] == 20.0
    assert d["resets_at"] == "2026-08-01T00:00:00+00:00"


# --- 기간 경계 ---

def test_period_boundaries():
    """일별=UTC 자정, 월별=월초. 리셋 시각은 다음 경계."""
    assert budget._period_start("daily", NOW) == datetime(2026, 7, 15, tzinfo=timezone.utc)
    assert budget._period_start("monthly", NOW) == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert budget._period_end("daily", datetime(2026, 7, 15, tzinfo=timezone.utc)) == \
        datetime(2026, 7, 16, tzinfo=timezone.utc)
    assert budget._period_end("monthly", datetime(2026, 7, 1, tzinfo=timezone.utc)) == \
        datetime(2026, 8, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("month_start,expected", [
    (datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 2, 1, tzinfo=timezone.utc)),
    (datetime(2026, 2, 1, tzinfo=timezone.utc), datetime(2026, 3, 1, tzinfo=timezone.utc)),  # 28일 달
    (datetime(2024, 2, 1, tzinfo=timezone.utc), datetime(2024, 3, 1, tzinfo=timezone.utc)),  # 윤년
    (datetime(2026, 12, 1, tzinfo=timezone.utc), datetime(2027, 1, 1, tzinfo=timezone.utc)),  # 연말
])
def test_monthly_reset_handles_short_months_and_year_end(month_start, expected):
    assert budget._period_end("monthly", month_start) == expected


# --- /turn 배선 (서비스만 테스트하면 배선 누락을 못 잡는다) ---

class _FakeDB:
    def __init__(self, session):
        self._session = session

    def get(self, model, key):
        return self._session

    def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: None,
                               scalars=lambda: SimpleNamespace(all=lambda: []))


def _override_turn_deps(monkeypatch, user=None):
    import app.api.sessions as sessions_api
    from app.db import get_db
    from app.main import app

    session = SimpleNamespace(id=SID, user_id=None, solution="A360")
    app.dependency_overrides[get_db] = lambda: _FakeDB(session)
    app.dependency_overrides[sessions_api.get_optional_user] = lambda: user
    # 에이전트가 준비된 것으로 — 예산 검사가 503보다 먼저/뒤인지와 무관하게 429를 확인하려면
    # 에이전트 부재로 인한 503이 가려선 안 된다.
    monkeypatch.setattr(sessions_api, "_get_agent_turn", lambda: (lambda *a, **k: None))
    return app


def test_turn_returns_429_when_budget_exceeded(monkeypatch):
    """배선 확인 — 예산 초과면 /turn이 스트림을 열기 전에 429로 끊는다."""
    from fastapi.testclient import TestClient

    app = _override_turn_deps(monkeypatch)
    monkeypatch.setenv("BUDGET_SUBJECT_DAILY_USD", "1.0")
    _spend(monkeypatch, 5.0)
    try:
        with TestClient(app) as c:
            r = c.post(f"/api/sessions/{SID}/turn", json={"message": "안녕하세요"})
        assert r.status_code == 429
        assert r.json()["detail"]["code"] == "BUDGET_EXCEEDED"
    finally:
        app.dependency_overrides.clear()


def test_turn_not_blocked_when_budget_unset(monkeypatch):
    """상한 미설정이면 예산 때문에 막히지 않는다 — 429가 아니어야 한다(기존 동작 불변)."""
    from fastapi.testclient import TestClient

    app = _override_turn_deps(monkeypatch)
    try:
        with TestClient(app) as c:
            r = c.post(f"/api/sessions/{SID}/turn", json={"message": "안녕하세요"})
        assert r.status_code != 429
    finally:
        app.dependency_overrides.clear()
