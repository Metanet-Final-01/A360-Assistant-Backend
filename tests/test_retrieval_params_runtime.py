"""검색 파라미터 무중단 런타임 튜닝 테스트 (RPA-149).

두 층을 검증한다:
- 로더(app/services/retrieval_params): DB 오버라이드 우선, 없으면 .env 폴백, TTL 캐시·bust,
  DB에 깨진 값이 있어도 검색을 죽이지 않고 config로 저하.
- admin API(GET/PUT /api/admin/retrieval-params): 현재값 조회, 검증 재사용(400), append 저장·캐시 무효화.
"""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.api.admin as admin_api
import app.services.retrieval_params as rp
from app.db import get_db
from app.main import app
from app.rag.retrieval.params import RetrievalParams


class _FakeSession:
    """ORM 경로(query/order_by/first, add/commit/refresh/close)를 흉내내는 세션 스텁."""

    def __init__(self, row=None):
        self._row = row
        self.added: list = []

    def query(self, _model):
        return self

    def order_by(self, *_a):
        return self

    def first(self):
        return self._row

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass

    def refresh(self, obj):
        # 서버 기본값(id·created_at) 채움을 흉내 — 응답 직렬화가 created_at.isoformat()을 쓴다.
        if getattr(obj, "id", None) is None:
            obj.id = 1
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime(2026, 7, 14, tzinfo=timezone.utc)

    def close(self):
        pass


def _row(pool=33, rerank=11, k=42, vw=2.0, bw=0.5):
    return SimpleNamespace(
        candidate_pool_size=pool, rerank_candidates=rerank, rrf_k=k,
        vector_weight=vw, bm25_weight=bw,
        updated_by="admin@test.com", created_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
    )


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """각 테스트 전 로더 캐시를 비우고, 끝나면 dependency override를 정리한다."""
    rp.bust_cache()
    yield
    rp.bust_cache()
    app.dependency_overrides.clear()


# --- 로더: DB 오버라이드 우선 / .env 폴백 ---

def test_loader_falls_back_to_config_when_no_row(monkeypatch):
    """오버라이드 행이 없으면 from_config(.env)와 동일 — 로컬/데모 무변경."""
    monkeypatch.setattr(rp, "SessionLocal", lambda: _FakeSession(row=None))
    assert rp.load_active_params() == RetrievalParams.from_config()


def test_loader_uses_db_override_when_row_present(monkeypatch):
    """행이 있으면 그 값이 활성 파라미터 — 재시작 없이 반영되는 값의 출처."""
    monkeypatch.setattr(rp, "SessionLocal", lambda: _FakeSession(row=_row()))
    p = rp.load_active_params()
    assert (p.candidate_pool_size, p.rerank_candidates, p.rrf_k) == (33, 11, 42)
    assert p.vector_weight == 2.0 and p.bm25_weight == 0.5


def test_loader_caches_within_ttl(monkeypatch):
    """TTL 안에서는 DB를 다시 안 읽는다 — 검색 hot path 부하 방어."""
    calls = {"n": 0}

    def _session():
        calls["n"] += 1
        return _FakeSession(row=_row())

    monkeypatch.setattr(rp, "SessionLocal", _session)
    rp.load_active_params()
    rp.load_active_params()
    assert calls["n"] == 1  # 두 번째는 캐시


def test_bust_cache_forces_reread(monkeypatch):
    """bust_cache 후에는 DB를 다시 읽어 새 값을 반영한다 — PUT 직후 무중단 반영 경로."""
    state = {"row": _row(k=42)}
    monkeypatch.setattr(rp, "SessionLocal", lambda: _FakeSession(row=state["row"]))
    assert rp.load_active_params().rrf_k == 42
    state["row"] = _row(k=77)
    assert rp.load_active_params().rrf_k == 42  # 아직 캐시
    rp.bust_cache()
    assert rp.load_active_params().rrf_k == 77  # 무효화 후 재조회


def test_loader_degrades_to_config_on_bad_db_value(monkeypatch):
    """DB에 잘못된 값(rrf_k=0)이 있어도 검색을 죽이지 않고 config로 저하 — 가용성 보호."""
    monkeypatch.setattr(rp, "SessionLocal", lambda: _FakeSession(row=_row(k=0)))
    assert rp.load_active_params() == RetrievalParams.from_config()


# --- admin API ---

def _auth_admin():
    app.dependency_overrides[admin_api.require_admin] = lambda: SimpleNamespace(
        id=uuid.uuid4(), email="admin@test.com"
    )


def test_get_returns_config_when_no_override(monkeypatch):
    """GET: 오버라이드 없으면 source=config + .env 기본값(슬라이더 프리필용)."""
    _auth_admin()
    app.dependency_overrides[get_db] = lambda: _FakeSession(row=None)
    with TestClient(app) as c:
        r = c.get("/api/admin/retrieval-params")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "config"
    assert body["candidate_pool_size"] == RetrievalParams.from_config().candidate_pool_size
    assert body["updated_by"] is None


def test_put_valid_persists_and_busts_cache(monkeypatch):
    """PUT 유효값: 200·source=db·행 추가·캐시 무효화(무중단 반영)."""
    _auth_admin()
    fake = _FakeSession(row=None)
    app.dependency_overrides[get_db] = lambda: fake
    rp._cache = (0.0, RetrievalParams.from_config())  # 캐시가 차 있다고 가정
    payload = {"candidate_pool_size": 80, "rerank_candidates": 30,
               "rrf_k": 40, "vector_weight": 2.0, "bm25_weight": 0.5}
    with TestClient(app) as c:
        r = c.put("/api/admin/retrieval-params", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "db" and body["rrf_k"] == 40 and body["vector_weight"] == 2.0
    assert body["updated_by"] == "admin@test.com"
    assert len(fake.added) == 1 and fake.added[0].candidate_pool_size == 80
    assert rp._cache is None  # bust_cache 호출됨


def test_put_service_identity_sets_updated_by_service(monkeypatch):
    """X-API-Key(머신) 경로는 user가 None이라 updated_by=service."""
    app.dependency_overrides[admin_api.require_admin] = lambda: None  # 서비스 신원
    app.dependency_overrides[get_db] = lambda: _FakeSession(row=None)
    payload = {"candidate_pool_size": 50, "rerank_candidates": 20,
               "rrf_k": 60, "vector_weight": 1.0, "bm25_weight": 1.0}
    with TestClient(app) as c:
        r = c.put("/api/admin/retrieval-params", json=payload)
    assert r.status_code == 200
    assert r.json()["updated_by"] == "service"


@pytest.mark.parametrize("bad", [
    {"candidate_pool_size": 50, "rerank_candidates": 20, "rrf_k": 0, "vector_weight": 1.0, "bm25_weight": 1.0},
    {"candidate_pool_size": 0, "rerank_candidates": 20, "rrf_k": 60, "vector_weight": 1.0, "bm25_weight": 1.0},
    {"candidate_pool_size": 50, "rerank_candidates": 20, "rrf_k": 60, "vector_weight": -1.0, "bm25_weight": 1.0},
])
def test_put_invalid_rejected_400(monkeypatch, bad):
    """PUT 잘못된 값: RetrievalParams 검증 재사용으로 400(저장 안 함) — 규칙 이중정의 없음."""
    _auth_admin()
    fake = _FakeSession(row=None)
    app.dependency_overrides[get_db] = lambda: fake
    with TestClient(app) as c:
        r = c.put("/api/admin/retrieval-params", json=bad)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_PARAMS"
    assert fake.added == []  # 검증 실패 → 저장 없음
