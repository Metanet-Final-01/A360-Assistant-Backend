# -*- coding: utf-8 -*-
"""RAG 캐시 Redis 백엔드 (RPA-274) — fakeredis로 실 Redis 없이 검증한다.

루트 conftest가 REDIS_URL을 빈 문자열로 격리하므로(공유 캐시 오염 방지), 여기서는
자기 setenv + 팩토리 패치로 Redis 모드를 명시적으로 켠다.

검증 축:
- 라운드트립 **동일 값**(투명성 불변식) — strict JSON, NaN 거부, tuple→list 무언 변형 거부
- 세대(generation) 무효화 — 늦은 SET이 새 세대에 노출되지 않는가(SCAN+DELETE 레이스 제거)
- pending 마커 — 부분 적재 동안 신규 캐싱 차단
- control path fail-closed — Redis 다운이면 무효화가 조용히 성공하지 않는가
- hot path fail-open — 장애·오염값·서킷·half-open 프로브
- 네임스페이스 — 환경·스키마 버전 격리
- 클라이언트/서킷 상태의 경합 방어 — 옛 클라이언트 실패가 새 URL 서킷을 못 연다
"""

import time

import fakeredis
import pytest

from app.services import rag_cache
from app.services.rag_cache import CacheInvalidationError


class _Params:
    """search_key가 getattr로 읽는 파라미터 5종의 최소 스텁."""

    candidate_pool_size = 50
    rerank_candidates = 20
    rrf_k = 60
    vector_weight = 0.7
    bm25_weight = 0.3


def _results(n: int = 1) -> list[dict]:
    """읽기 계약(id·content·score)을 충족하는 검색 결과 픽스처."""
    return [{"id": i, "content": f"본문 {i}", "score": 0.9 - i * 0.1,
             "title": f"t{i}", "bm25_available": True} for i in range(n)]


def _sk(query: str = "q") -> str:
    return rag_cache.search_key(query, 5, None, _Params(), "voyage-3", "rerank-2.5-lite")


def _reset_module_state(monkeypatch, factory):
    monkeypatch.setattr(rag_cache, "_make_redis_client", factory)
    monkeypatch.setattr(rag_cache, "_redis_client", None)
    monkeypatch.setattr(rag_cache, "_redis_url_cached", None)
    monkeypatch.setattr(rag_cache, "_redis_down_until", 0.0)


@pytest.fixture
def fake_redis(monkeypatch):
    """rag_cache를 fakeredis 백엔드로 켠다. 반환값은 검사용 fake 클라이언트."""
    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setenv("RAG_CACHE_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "redis://fake-for-test:6379/0")
    _reset_module_state(monkeypatch, lambda url, **kw: fake)
    rag_cache.bust_all()
    return fake


# ── 라운드트립·투명성 ────────────────────────────────────────────────────────

def test_embedding_roundtrip_is_identical(fake_redis):
    key = rag_cache.embedding_key("구글시트에서 값 읽기", "voyage-3", 4)
    assert key.startswith(rag_cache._ns() + ":emb:")
    vector = [0.125, -0.5, 3.0e-7, 1.0]
    rag_cache.put_embedding(key, vector)

    assert fake_redis.exists(key) == 1                      # 실제로 Redis에 갔는가
    assert rag_cache.get_embedding(key, expected_dim=4) == vector
    s = rag_cache.stats()
    assert s["backend"] == "redis" and s["embed_hit"] == 1


def test_search_roundtrip_is_identical_and_isolated(fake_redis):
    key = _sk("이메일 보내기")
    results = _results(2)
    results[0]["meta"] = {"nested": [1, 2.5, None, True], "유니코드": "✓"}
    assert rag_cache.put_search(key, results) is None
    got = rag_cache.get_search(key)
    assert got == results
    got[0]["content"] = "오염"                               # 반환값 변형이 원본을 못 건드린다
    assert rag_cache.get_search(key) == results


def test_nan_infinity_rejected(fake_redis):
    key = _sk()
    bad = [{"id": 1, "content": "x", "score": float("nan")}]
    assert rag_cache.put_search(key, bad) == "unserializable"
    bad2 = [{"id": 1, "content": "x", "score": float("inf")}]
    assert rag_cache.put_search(key, bad2) == "unserializable"
    assert rag_cache.stats()["skip_unserializable"] == 2
    assert fake_redis.keys(rag_cache._ns() + ":search:*") == []


def test_silent_json_mutation_rejected(fake_redis):
    """tuple→list처럼 json.dumps가 예외 없이 값을 바꾸는 경우도 투명성 위반 — 저장 포기."""
    key = _sk()
    with_tuple = [{"id": 1, "content": "x", "score": 0.5, "pair": (1, 2)}]
    assert rag_cache.put_search(key, with_tuple) == "unserializable"
    with_int_key = [{"id": 1, "content": "x", "score": 0.5, "m": {1: "a"}}]
    assert rag_cache.put_search(key, with_int_key) == "unserializable"
    assert rag_cache.get_search(key) is None


# ── 읽기 검증: 문법·계약 ─────────────────────────────────────────────────────

def test_corrupt_json_is_miss_and_evicted(fake_redis):
    key = _sk()
    fake_redis.set(key, "{이건 JSON이 아니다")
    assert rag_cache.get_search(key) is None
    s = rag_cache.stats()
    assert s["redis_decode_error"] == 1 and s["search_miss"] == 1 and s["search_hit"] == 0
    assert fake_redis.exists(key) == 0


def test_valid_json_wrong_contract_is_miss_and_evicted(fake_redis):
    """유효한 JSON이지만 우리 값이 아닌 것 — 타입·필수 필드까지 검증한다."""
    key = _sk()
    for payload in ("42", '{"id": 1}', '[{"id": 1}]', "[]"):  # 스칼라/비리스트/필드 누락/빈 리스트
        fake_redis.set(key, payload)
        assert rag_cache.get_search(key) is None
        assert fake_redis.exists(key) == 0
    assert rag_cache.stats()["redis_contract_error"] == 4


def test_embedding_contract_validation(fake_redis):
    key = rag_cache.embedding_key("q", "m", 3)
    # 차원 불일치
    fake_redis.set(key, "[1.0, 2.0]")
    assert rag_cache.get_embedding(key, expected_dim=3) is None
    # 수치 아님 / bool 섞임
    fake_redis.set(key, '[1.0, "x", 2.0]')
    assert rag_cache.get_embedding(key, expected_dim=3) is None
    fake_redis.set(key, "[true, 1.0, 2.0]")
    assert rag_cache.get_embedding(key, expected_dim=3) is None
    # 비유한 수 — json.loads는 Infinity를 기본 허용하므로 읽기 검증이 마지막 방어다
    fake_redis.set(key, "[Infinity, 1.0, 2.0]")
    assert rag_cache.get_embedding(key, expected_dim=3) is None
    assert rag_cache.stats()["redis_contract_error"] == 4
    assert fake_redis.exists(key) == 0


# ── 기존 스킵 규칙 유지 ──────────────────────────────────────────────────────

def test_skip_rules_still_apply_in_redis_mode(fake_redis):
    key = _sk()
    assert rag_cache.put_search(key, []) == "empty"
    degraded = [{"id": 1, "content": "x", "score": 0.5, "bm25_available": False}]
    assert rag_cache.put_search(key, degraded) == "degraded"
    assert rag_cache.get_search(key) is None
    assert fake_redis.keys(rag_cache._ns() + ":search:*") == []


# ── 세대(generation) 무효화 ──────────────────────────────────────────────────

def test_late_set_from_old_request_is_invisible_after_publish(fake_redis):
    """SCAN+DELETE의 레이스 제거 검증: 구 요청이 세대 전환 **후** 저장해도 새 세대에 안 보인다."""
    old_key = _sk("구 코퍼스 질의")            # 세대 g0 캡처 (요청 시작)
    assert ":search:g0:" in old_key

    new_gen = rag_cache.publish_generation()   # ingest 완료 (요청 진행 중에 발생)
    assert new_gen == 1

    rag_cache.put_search(old_key, _results())  # 구 요청의 늦은 SET — 구 세대 키로 저장된다
    new_key = _sk("구 코퍼스 질의")            # 새 요청은 g1 캡처
    assert ":search:g1:" in new_key
    assert rag_cache.get_search(new_key) is None        # 새 세대 독자에게 보이지 않는다
    assert rag_cache.get_search(old_key) == _results()  # 구 요청 자신의 일관성은 유지


def test_generation_unavailable_disables_caching_for_request(fake_redis, monkeypatch):
    """세대를 못 읽으면(장애) 그 요청은 캐싱을 건너뛴다 — 모르는 세대에 저장하지 않는다."""
    monkeypatch.setattr(rag_cache, "_redis_down_until", time.monotonic() + 60)  # 서킷 열림
    key = _sk()
    assert ":search:nogen:" in key
    monkeypatch.setattr(rag_cache, "_redis_down_until", 0.0)  # 서킷이 닫혀도 nogen 키는 불저장
    assert rag_cache.put_search(key, _results()) == "generation_unavailable"
    assert rag_cache.get_search(key) is None
    assert rag_cache.stats()["skip_no_generation"] >= 2


def test_cleanup_old_generations_is_hygiene_only(fake_redis):
    k0 = _sk("q0")
    rag_cache.put_search(k0, _results())
    rag_cache.publish_generation()
    k1 = _sk("q1")
    rag_cache.put_search(k1, _results())

    removed = rag_cache.cleanup_old_generations()
    assert removed == 1
    assert rag_cache.get_search(k1) == _results()   # 현 세대는 보존
    assert fake_redis.exists(k0) == 0               # 구 세대만 제거


# ── pending 마커: 부분 적재 캐싱 차단 ────────────────────────────────────────

def test_pending_marker_blocks_new_caching_until_publish(fake_redis):
    rag_cache.mark_ingest_pending()
    key = _sk()
    assert rag_cache.put_search(key, _results()) == "ingest_pending"
    assert rag_cache.get_search(key) is None
    assert rag_cache.stats()["skip_ingest_pending"] == 1

    rag_cache.publish_generation()                  # 성공 publish가 마커를 지운다
    key2 = _sk()
    assert rag_cache.put_search(key2, _results()) is None
    assert rag_cache.get_search(key2) == _results()


# ── control path: fail-closed ────────────────────────────────────────────────

class _DownRedis:
    """모든 연산이 접속 오류를 던지는 스텁."""

    def __init__(self):
        self.calls = 0

    def _boom(self, *a, **k):
        self.calls += 1
        raise ConnectionError("redis down")

    get = set = scan_iter = delete = unlink = exists = incr = _boom

    def close(self):
        pass


def test_control_path_raises_when_redis_down(monkeypatch):
    """무효화 실패는 0건 성공으로 숨지 않는다 — 유한 재시도 후 예외."""
    down = _DownRedis()
    monkeypatch.setenv("RAG_CACHE_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "redis://down:6379/0")
    _reset_module_state(monkeypatch, lambda url, **kw: down)
    monkeypatch.setattr(rag_cache.time, "sleep", lambda s: None)  # 재시도 대기 생략
    # hot path 서킷이 열려 있어도 control은 **우회해서 시도**해야 한다
    monkeypatch.setattr(rag_cache, "_redis_down_until", time.monotonic() + 999)

    with pytest.raises(CacheInvalidationError):
        rag_cache.mark_ingest_pending()
    assert down.calls == rag_cache._CONTROL_ATTEMPTS      # 유한 재시도(무한 아님·0회 아님)

    down.calls = 0
    with pytest.raises(CacheInvalidationError):
        rag_cache.publish_generation()
    assert down.calls == rag_cache._CONTROL_ATTEMPTS


# ── hot path: 서킷·half-open·상태 경합 ──────────────────────────────────────

def test_hot_failure_opens_circuit_with_bounded_calls(monkeypatch):
    down = _DownRedis()
    monkeypatch.setenv("RAG_CACHE_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "redis://down:6379/0")
    _reset_module_state(monkeypatch, lambda url, **kw: down)

    key = rag_cache.embedding_key("q", "m", 2)   # emb 층은 세대 무관 — nogen 영향 없음
    assert rag_cache.get_embedding(key) is None  # 첫 실패 → 서킷 열림
    assert down.calls == 1
    assert rag_cache.get_embedding(key) is None  # 서킷 창 안 — 클라이언트를 안 때린다
    assert down.calls == 1
    now = time.monotonic()
    assert 20 <= rag_cache._redis_down_until - now <= 40   # 30s ± 지터(0.8~1.2)


def test_half_open_probe_closes_circuit_on_success(fake_redis, monkeypatch):
    key = rag_cache.embedding_key("q", "m", 2)
    rag_cache.put_embedding(key, [1.0, 2.0])
    # 서킷이 열렸다가 만료된 상황을 만든다
    monkeypatch.setattr(rag_cache, "_redis_down_until", time.monotonic() - 0.1)
    assert rag_cache.get_embedding(key, expected_dim=2) == [1.0, 2.0]  # 프로브 성공
    assert rag_cache._redis_down_until == 0.0                          # 서킷 닫힘


def test_stale_client_failure_cannot_open_new_circuit(monkeypatch):
    """URL 교체 뒤 도착한 **옛 클라이언트의** 실패 보고가 새 대상의 서킷을 열면 안 된다."""
    down = _DownRedis()
    fake = fakeredis.FakeRedis(decode_responses=True)
    clients = {"redis://old:6379/0": down, "redis://new:6379/0": fake}
    monkeypatch.setenv("RAG_CACHE_ENABLED", "true")
    _reset_module_state(monkeypatch, lambda url, **kw: clients[url])

    monkeypatch.setenv("REDIS_URL", "redis://old:6379/0")
    old_client = rag_cache._hot_client()
    assert old_client is down
    monkeypatch.setenv("REDIS_URL", "redis://new:6379/0")
    assert rag_cache._hot_client() is fake       # 교체 완료
    rag_cache._mark_redis_down(old_client)       # 늦게 도착한 옛 실패 보고
    assert rag_cache._redis_down_until == 0.0    # 새 대상 서킷은 닫힌 채


def test_url_change_clears_backoff_immediately(monkeypatch):
    down = _DownRedis()
    fake = fakeredis.FakeRedis(decode_responses=True)
    clients = {"redis://old-dead:6379/0": down, "redis://new-alive:6379/0": fake}
    monkeypatch.setenv("RAG_CACHE_ENABLED", "true")
    _reset_module_state(monkeypatch, lambda url, **kw: clients[url])

    monkeypatch.setenv("REDIS_URL", "redis://old-dead:6379/0")
    key = rag_cache.embedding_key("q", "m", 1)
    assert rag_cache.get_embedding(key) is None
    assert rag_cache._redis_down_until > 0       # 백오프 진입

    monkeypatch.setenv("REDIS_URL", "redis://new-alive:6379/0")
    rag_cache.put_embedding(key, [1.5])          # 새 대상은 즉시 시도
    assert rag_cache.get_embedding(key, expected_dim=1) == [1.5]


# ── 네임스페이스 격리 ────────────────────────────────────────────────────────

def test_namespace_isolates_environments_and_schema(fake_redis, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    dev_key = _sk("같은 질의")
    rag_cache.put_search(dev_key, _results())
    assert dev_key.startswith(f"a360:development:rag:v{rag_cache._SCHEMA_VERSION}:")

    monkeypatch.setenv("APP_ENV", "production")
    prod_key = _sk("같은 질의")
    assert prod_key.startswith(f"a360:production:rag:v{rag_cache._SCHEMA_VERSION}:")
    assert rag_cache.get_search(prod_key) is None          # 환경 간 혼입 없음
    monkeypatch.setenv("APP_ENV", "development")
    assert rag_cache.get_search(dev_key) == _results()


# ── stats: configured / circuit 구분 ────────────────────────────────────────

def test_stats_distinguishes_configured_and_circuit(fake_redis, monkeypatch):
    s = rag_cache.stats()
    assert s["backend"] == "redis" and s["redis_configured"] and not s["redis_circuit_open"]
    monkeypatch.setattr(rag_cache, "_redis_down_until", time.monotonic() + 60)
    assert rag_cache.stats()["redis_circuit_open"] is True


# ── 인프로세스 모드 회귀 ─────────────────────────────────────────────────────

def test_cache_off_never_touches_redis_even_with_url(monkeypatch):
    """캐시 OFF + REDIS_URL 설정 조합에서 검색 경로가 Redis에 **한 번도** 안 닿아야 한다
    (Qodo #380). 호출부(rag.py)는 키를 무조건 만들므로 search_key의 세대 조회가 I/O면
    '끄면 기존 동작 그대로' 계약이 깨진다 — 팩토리 호출 0회가 그 증명이다."""
    calls = {"n": 0}

    def _counting_factory(url, **kw):
        calls["n"] += 1
        return fakeredis.FakeRedis(decode_responses=True)

    monkeypatch.setenv("RAG_CACHE_ENABLED", "")           # OFF (미설정과 동일)
    monkeypatch.setenv("REDIS_URL", "redis://set-but-off:6379/0")
    monkeypatch.setattr(rag_cache, "_make_redis_client", _counting_factory)
    monkeypatch.setattr(rag_cache, "_redis_client", None)
    monkeypatch.setattr(rag_cache, "_redis_url_cached", None)
    monkeypatch.setattr(rag_cache, "_redis_down_until", 0.0)

    sk = rag_cache.search_key("q", 5, None, _Params(), "e", "r")
    assert rag_cache.get_search(sk) is None
    assert rag_cache.put_search(sk, [{"id": 1, "content": "c", "score": 0.5}]) is None
    ek = rag_cache.embedding_key("q", "m", 8)
    assert rag_cache.get_embedding(ek) is None
    rag_cache.put_embedding(ek, [1.0])

    assert calls["n"] == 0                                 # 클라이언트 생성조차 없어야 한다
    assert ":search:off:" in sk                            # 세대 대신 고정 세그먼트


def test_control_error_message_masks_url_password(monkeypatch):
    """control 실패 메시지는 원인 구분이 되도록 예외 메시지를 포함하되(Qodo #380),
    REDIS_URL의 비밀번호는 마스킹돼야 한다 — 오류 문자열로 크레덴셜이 새는 선례 방지."""
    secret = "supersecretpw"

    def _raising_factory(url, **kw):
        raise ConnectionError(f"AUTH failed connecting with password {secret} to host")

    monkeypatch.setenv("RAG_CACHE_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", f"redis://user:{secret}@dead:6379/0")
    monkeypatch.setattr(rag_cache, "_make_redis_client", _raising_factory)
    monkeypatch.setattr(rag_cache, "_CONTROL_RETRY_BASE_SEC", 0.01)

    with pytest.raises(rag_cache.CacheInvalidationError) as e:
        rag_cache.publish_generation()
    msg = str(e.value)
    assert "ConnectionError" in msg and "AUTH failed" in msg  # 원인 구분 가능
    assert secret not in msg and "***" in msg                 # 크레덴셜은 마스킹


def test_password_at_truncation_boundary_never_leaks(monkeypatch):
    """마스킹은 절단(300자)보다 먼저여야 한다 (Qodo #380 2차) — 순서가 반대면 경계에 걸친
    비밀번호의 앞부분 조각이 마스킹을 피해 그대로 나간다."""
    secret = "boundarysecretpw"
    # 예외 메시지의 290자 지점에서 비밀번호가 시작되게 구성 — 절단(300자) 경계에 걸친다
    filler = "x" * (290 - len("ConnectionError: "))

    def _raising_factory(url, **kw):
        raise ConnectionError(f"{filler}{secret} rest of message")

    monkeypatch.setenv("RAG_CACHE_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", f"redis://user:{secret}@dead:6379/0")
    monkeypatch.setattr(rag_cache, "_make_redis_client", _raising_factory)
    monkeypatch.setattr(rag_cache, "_CONTROL_RETRY_BASE_SEC", 0.01)

    with pytest.raises(rag_cache.CacheInvalidationError) as e:
        rag_cache.publish_generation()
    msg = str(e.value)
    assert secret not in msg
    # 부분 조각도 안 된다 — 절단이 먼저면 secret[:10] 같은 앞조각이 남는다
    assert secret[:8] not in msg


def test_memory_backend_when_url_unset(monkeypatch):
    monkeypatch.setenv("RAG_CACHE_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "")
    rag_cache.bust_all()
    key = rag_cache.embedding_key("q", "m", 1)
    rag_cache.put_embedding(key, [0.5])
    assert rag_cache.get_embedding(key) == [0.5]
    s = rag_cache.stats()
    assert s["backend"] == "memory" and s["redis_configured"] is False


def test_memory_mode_generation_isolates_late_set_too(monkeypatch):
    """세대 무효화는 인프로세스에도 같은 의미론 — 로컬 bust의 재-SET 레이스도 없앤다."""
    monkeypatch.setenv("RAG_CACHE_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "")
    rag_cache.bust_all()
    old_key = _sk("q")
    rag_cache.publish_generation()
    rag_cache.put_search(old_key, _results())   # 늦은 SET
    assert rag_cache.get_search(_sk("q")) is None
