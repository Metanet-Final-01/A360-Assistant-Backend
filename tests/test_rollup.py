"""일별 롤업 테스트 (RPA-104) — 순수 집계 + 멱등 반영 + 스케줄러 게이트."""

from datetime import date

from app.services import rollup


# --- 순수 계산 ---

def test_percentile():
    assert rollup.percentile([], 0.5) is None
    assert rollup.percentile([10], 0.95) == 10.0
    assert rollup.percentile([10, 20, 30, 40], 0.5) == 25.0   # 선형 보간
    assert rollup.percentile([10, 20, 30, 40], 1.0) == 40.0


def test_aggregate_metrics_groups_and_errors():
    rows = [
        ("GET", "/api/sessions/:id", 200, 10),
        ("GET", "/api/sessions/:id", 200, 30),
        ("GET", "/api/sessions/:id", 404, 20),
        ("GET", "/api/sessions/:id", 500, None),  # latency 없음 → 분위수에서 제외
        ("POST", "/api/sessions/:id/turn", 200, 300),
    ]
    aggs = {(a["method"], a["path"]): a for a in rollup.aggregate_metrics(rows)}
    g = aggs[("GET", "/api/sessions/:id")]
    assert g["calls"] == 4 and g["err_4xx"] == 1 and g["err_5xx"] == 1
    assert g["p50_ms"] == 20 and g["max_ms"] == 30  # [10,20,30] 기준
    t = aggs[("POST", "/api/sessions/:id/turn")]
    assert t["calls"] == 1 and t["p95_ms"] == 300


def test_aggregate_usage_sums_and_null_cost():
    rows = [
        ("agent", "intake", "gpt-5.4-mini", 100, 10, 0.001),
        ("agent", "intake", "gpt-5.4-mini", 200, 20, 0.002),
        ("rag_embed", "embed", "text-embedding-3-small", 50, 0, None),  # cost 미계산 행
    ]
    aggs = {(a["component"], a["purpose"]): a for a in rollup.aggregate_usage(rows)}
    intake = aggs[("agent", "intake")]
    assert intake["calls"] == 2 and intake["input_tokens"] == 300
    assert abs(intake["cost_usd"] - 0.003) < 1e-9
    assert aggs[("rag_embed", "embed")]["cost_usd"] is None  # 전부 null이면 null 유지


# --- 멱등 반영 (fake 세션으로 delete→insert 검증) ---

class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.deleted = 0
        self.added = []

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, stmt):
        from sqlalchemy import Delete
        if isinstance(stmt, Delete):
            self.deleted += 1
            return None
        return self  # select → .all()이 preset 행 반환

    def all(self): return self._rows
    def add(self, row): self.added.append(row)
    def commit(self): pass


def test_rollup_metrics_day_idempotent_delete_then_insert(monkeypatch):
    rows = [("GET", "/api/health", 200, 5), ("GET", "/api/health", 200, 7)]
    fake = _FakeSession(rows)
    n = rollup.rollup_metrics_day(lambda: fake, date(2026, 7, 10))
    assert fake.deleted == 1          # 해당 일자 기존 집계 DELETE (멱등)
    assert n == 1 and len(fake.added) == 1
    added = fake.added[0]
    assert added.path == "/api/health" and added.calls == 2 and added.p50_ms == 6


def test_rollup_usage_day(monkeypatch):
    rows = [("agent", "verify", "gpt-5.4-mini", 1000, 500, 0.003)]
    fake = _FakeSession(rows)
    n = rollup.rollup_usage_day(lambda: fake, date(2026, 7, 10))
    assert fake.deleted == 1 and n == 1
    assert fake.added[0].purpose == "verify" and fake.added[0].input_tokens == 1000


# --- 스케줄러 게이트 ---

def test_scheduler_disabled_by_env(monkeypatch):
    from app.core import scheduler

    monkeypatch.setenv("METRICS_ROLLUP_ENABLED", "false")
    assert scheduler.start_scheduler() is False  # 비활성이면 안 켬 (테스트 기본)
