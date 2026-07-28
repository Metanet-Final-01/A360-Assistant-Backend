# -*- coding: utf-8 -*-
"""source_type 필터 push-down 검증 (RPA-298 §8).

**왜 필요했나.** `search_actions`는 source_types를 검색에 내려보내지 않고 결과 단계에서
걸렀다. 파이프라인은 후보 풀 → RRF 융합 → `rerank_candidates`(20)로 좁힌 뒤 그 20건에서
필터를 적용하므로, 필터가 실제로 보는 창은 `min(k*3, 20)`이다. 코퍼스의 90%가
doc_page라 그 창을 doc_page가 독식하고, `package_overview`(136행)·`trigger_schema`(31행)
같은 희소 타입은 k를 올려도 0건이 됐다(실측: trigger_schema 26/26 질의 0건).

**여기서 지키는 두 축.**
  1. 기본 경로(push-down 미사용)가 **바이트 단위로** 이전과 같다 — SQL 문자열, BM25
     질의 body, 캐시 다이제스트 입력. v1~v3가 이 경로를 타고, 캐시 키가 흔들리면
     배포 순간 전량 미스가 된다.
  2. 필터가 **모든 하위 경로에** 실제로 도달한다 — vector/BM25 두 branch, mode="vector"
     조기 반환, BM25 장애 저하. 한 경로라도 새면 필터가 조용히 유실된다.

인프라를 안 탄다: db·opensearch·embed·rerank를 전부 몽키패치한다.
"""

import asyncio

import pytest

from app.rag.retrieval import hybrid_search
from app.rag.retrieval.params import RetrievalParams
from app.rag.store import db, opensearch_client
from app.services import rag as rag_service
from app.services import rag_cache


# ── 1. db 계층: 기본 경로 SQL 불변 ────────────────────────────────────────────

def test_default_plan_reuses_the_unfiltered_sql_object():
    """source_types 없으면 **같은 문자열 객체**를 쓴다 — 조건부 조립이면 이게 깨진다.

    `is` 비교인 이유: 동등성만 보면 "우연히 같은 문자열을 새로 만드는" 구현도 통과한다.
    기본 경로는 분기 없이 상수를 그대로 돌려줘야 쿼리 플랜 캐시도 그대로다.
    """
    for source_types in (None, [], ()):
        sql, args = db._search_plan("[0.1]", 5, source_types)
        assert sql is db._SEARCH_SQL
        assert args == ("[0.1]", "[0.1]", 5)


def test_filtered_plan_binds_types_between_the_two_vectors():
    """필터 SQL의 파라미터 순서 — %s 자리가 (벡터, 타입, 벡터, limit)다.

    SELECT의 벡터와 ORDER BY의 벡터 사이에 WHERE의 타입이 끼므로 순서를 틀리기 쉽다.
    틀리면 psycopg가 타입 오류를 내거나(운 좋을 때) 엉뚱한 정렬이 된다.
    """
    sql, args = db._search_plan("[0.1]", 7, ["action_schema"])
    assert sql is db._SEARCH_SQL_FILTERED
    assert args == ("[0.1]", ["action_schema"], "[0.1]", 7)
    assert "source_type = ANY(%s)" in sql


def test_filtered_sql_differs_from_default_only_by_the_where_clause():
    """두 SQL의 차이가 WHERE 한 줄뿐임을 고정한다 — SELECT 컬럼이 갈리면 반환
    스키마가 경로마다 달라져 상위 계층이 조용히 KeyError를 낸다."""
    default_lines = [ln.strip() for ln in db._SEARCH_SQL.strip().splitlines()]
    filtered_lines = [ln.strip() for ln in db._SEARCH_SQL_FILTERED.strip().splitlines()]
    extra = [ln for ln in filtered_lines if ln not in default_lines]
    assert extra == ["AND source_type = ANY(%s)"]


def test_db_search_passes_source_types_through(monkeypatch):
    """db.search가 _search_plan의 결과를 그대로 실행한다 (동기)."""
    executed = {}

    class _Cur:
        description = [type("D", (), {"name": "id"})()]

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, args): executed.update(sql=sql, args=args)
        def fetchall(self): return [("doc-1",)]

    conn = type("C", (), {"cursor": lambda self: _Cur()})()
    db.search.__wrapped__(conn, [0.1], limit=3, source_types=["trigger_schema"])
    assert executed["sql"] is db._SEARCH_SQL_FILTERED
    assert executed["args"][1] == ["trigger_schema"]


# ── 2. OpenSearch 계층: 기본 body 불변 + filter 절 ────────────────────────────

_LEGACY_BODY = {
    "size": 20,
    "query": {
        "multi_match": {
            "query": "엑셀 열기",
            "fields": ["title^2", "content"],
            "type": "best_fields",
        }
    },
}


def test_default_bm25_body_is_byte_identical_to_the_legacy_body():
    """필터가 없으면 예전 body와 **완전히 같은 dict**다 — bool로 감싸기만 해도
    BM25 점수 분포가 달라져 RRF 융합 순위가 바뀐다."""
    assert opensearch_client._keyword_search_body("엑셀 열기", 20) == _LEGACY_BODY
    assert opensearch_client._keyword_search_body("엑셀 열기", 20, None) == _LEGACY_BODY
    assert opensearch_client._keyword_search_body("엑셀 열기", 20, []) == _LEGACY_BODY


def test_filtered_bm25_puts_types_in_filter_not_must():
    """타입은 filter 절에 — must에 넣으면 term이 BM25 점수에 섞여 같은 타입 안의
    상대 순위가 필터 없을 때와 달라진다. filter는 점수에 기여하지 않는다."""
    body = opensearch_client._keyword_search_body("엑셀 열기", 20, ["action_schema"])
    bool_q = body["query"]["bool"]
    assert bool_q["must"] == [_LEGACY_BODY["query"]]
    assert bool_q["filter"] == [{"terms": {"source_type": ["action_schema"]}}]
    assert body["size"] == 20


def test_keyword_search_forwards_source_types(monkeypatch):
    captured = {}

    class _Client:
        def search(self, index, body):
            captured["body"] = body
            return {"hits": {"hits": []}}

    opensearch_client.keyword_search.__wrapped__(
        _Client(), "q", size=5, source_types=["package_overview"]
    )
    assert captured["body"]["query"]["bool"]["filter"] == [
        {"terms": {"source_type": ["package_overview"]}}
    ]


# ── 3. hybrid 계층: 두 branch 모두 + 저하 경로 ────────────────────────────────

@pytest.fixture
def _stub_hybrid(monkeypatch):
    """hybrid_search의 외부 의존을 전부 끊고 호출 인자를 기록한다."""
    seen = {"db": [], "bm25": []}

    def _db_search(conn, emb, limit=5, source_types=None):
        seen["db"].append(source_types)
        return [{"id": "v1", "title": "t", "content": "c", "source_type": "action_schema"}]

    def _bm25(client, query, size, source_types=None):
        seen["bm25"].append(source_types)
        return [{"id": "b1", "title": "t", "content": "c", "source_type": "action_schema"}]

    monkeypatch.setattr(hybrid_search.db, "search", _db_search)
    monkeypatch.setattr(hybrid_search.opensearch_client, "keyword_search", _bm25)
    monkeypatch.setattr(hybrid_search, "embed_query", lambda q: [0.1])
    monkeypatch.setattr(
        hybrid_search, "voyage_rerank",
        lambda q, docs, top_k: [{"index": i, "relevance_score": 1.0 - i * 0.1} for i in range(top_k)],
    )
    return seen


def test_hybrid_pushes_filter_into_both_branches(_stub_hybrid):
    """벡터에만 걸면 BM25가 다른 타입을 후보 풀에 밀어 넣어 융합 순위를 오염시킨다."""
    hybrid_search.search.__wrapped__(
        None, None, "q", limit=5, source_types=["action_schema"]
    )
    assert _stub_hybrid["db"] == [["action_schema"]]
    assert _stub_hybrid["bm25"] == [["action_schema"]]


def test_hybrid_default_sends_none_to_both_branches(_stub_hybrid):
    hybrid_search.search.__wrapped__(None, None, "q", limit=5)
    assert _stub_hybrid["db"] == [None]
    assert _stub_hybrid["bm25"] == [None]


def test_vector_mode_still_filters(_stub_hybrid):
    """mode="vector"는 융합 전에 조기 반환한다 — 필터가 _fuse_candidates에 있었다면
    이 경로가 통째로 우회했을 것이다. db.search 안에 있어야 덮인다."""
    hybrid_search.search.__wrapped__(
        None, None, "q", limit=5, mode="vector", source_types=["doc_page"]
    )
    assert _stub_hybrid["db"] == [["doc_page"]]
    assert _stub_hybrid["bm25"] == [], "vector 모드는 BM25를 부르지 않는다"


def test_filter_survives_bm25_outage(monkeypatch, _stub_hybrid):
    """BM25 장애로 저하돼도 필터가 살아 있다 — OpenSearch 질의에만 필터를 넣었다면
    장애 순간 필터가 통째로 사라져 잘못된 타입이 그대로 통과했을 것이다."""
    def _boom(client, query, size, source_types=None):
        raise RuntimeError("OpenSearch down")

    monkeypatch.setattr(hybrid_search.opensearch_client, "keyword_search", _boom)
    results = hybrid_search.search.__wrapped__(
        None, None, "q", limit=5, source_types=["action_schema"]
    )
    assert _stub_hybrid["db"] == [["action_schema"]]
    assert all(r["source_type"] == "action_schema" for r in results)
    assert results[0]["bm25_available"] is False


def test_async_path_mirrors_sync(monkeypatch):
    """search_async는 공유 코드가 아니라 별개 구현이라 한쪽만 고치기 쉽다."""
    seen = {"db": [], "bm25": []}

    async def _db_search_async(conn, emb, limit=5, source_types=None):
        seen["db"].append(source_types)
        return [{"id": "v1", "title": "t", "content": "c", "source_type": "action_schema"}]

    async def _bm25_async(client, query, size, source_types=None):
        seen["bm25"].append(source_types)
        return []

    async def _embed(q, client=None):
        return [0.1]

    monkeypatch.setattr(hybrid_search.db, "search_async", _db_search_async)
    monkeypatch.setattr(hybrid_search.opensearch_client, "keyword_search_async", _bm25_async)
    monkeypatch.setattr(hybrid_search, "embed_query_async", _embed)
    monkeypatch.setattr(hybrid_search, "voyage_rerank_async", None)

    asyncio.run(hybrid_search.search_async.__wrapped__(
        None, None, "q", limit=5, mode="hybrid", source_types=["action_schema"]
    ))
    assert seen["db"] == [["action_schema"]]
    assert seen["bm25"] == [["action_schema"]]


# ── 4. 서비스 계층: 과다인출 보정과 후단 필터 ─────────────────────────────────

@pytest.fixture
def _stub_service(monkeypatch):
    calls = []

    def _fake_hybrid(conn, os_client, query, limit=5, params=None, source_types=None):
        calls.append({"limit": limit, "source_types": source_types})
        return [
            {"id": "a", "source_type": "action_schema", "rerank_score": 0.9},
            {"id": "d", "source_type": "doc_page", "rerank_score": 0.8},
        ]

    monkeypatch.setattr(rag_service, "_hybrid_search", _fake_hybrid)
    monkeypatch.setattr(rag_service.opensearch_client, "get_shared_client", lambda: None)

    class _Conn:
        def __enter__(self): return None
        def __exit__(self, *a): return False

    monkeypatch.setattr(rag_service.db, "connection", lambda: _Conn())
    monkeypatch.setattr(rag_cache, "get_search", lambda key: None)
    monkeypatch.setattr(rag_cache, "put_search", lambda key, value: None)
    return calls


def test_legacy_path_keeps_the_3x_overfetch(_stub_service):
    """pushdown=False는 예전 그대로 — k*3을 뽑아 후단에서 거른다."""
    rag_service.search_actions("q", k=5, source_types=["action_schema"])
    assert _stub_service[0] == {"limit": 15, "source_types": None}


def test_pushdown_drops_the_overfetch_compensation(_stub_service):
    """k*3 과다인출은 후단 필터가 버릴 것을 감안한 보정이다. push면 버릴 게 없다."""
    rag_service.search_actions("q", k=5, source_types=["action_schema"], pushdown=True)
    assert _stub_service[0] == {"limit": 5, "source_types": ["action_schema"]}


def test_pushdown_without_source_types_is_the_default_path(_stub_service):
    """필터가 없으면 내려보낼 것도 없다 — 전체 검색은 v1~v3와 같은 경로여야 캐시를 공유한다."""
    rag_service.search_actions("q", k=5, pushdown=True)
    assert _stub_service[0] == {"limit": 5, "source_types": None}


def test_post_filter_net_still_catches_leaks_under_pushdown(_stub_service):
    """하위 계층이 필터를 흘려도(스텁이 일부러 doc_page를 섞어 반환) 서비스가 잡는다."""
    out = rag_service.search_actions("q", k=5, source_types=["action_schema"], pushdown=True)
    assert [r["id"] for r in out] == ["a"]


# ── 5. 캐시 키: 기본 경로 불변 + push 분리 ───────────────────────────────────

_KEY_ARGS = ("엑셀 열기", 5, ("action_schema",), RetrievalParams.from_config(), "emb-1", "rr-1")


def test_default_cache_key_matches_the_pre_pushdown_digest():
    """다이제스트 입력이 예전과 한 글자도 안 달라야 한다 — 인자를 하나 더 붙였으면
    배포 순간 전체 캐시가 미스로 돌아섰을 것이다. 예전 표현식을 그대로 재현해 비교한다."""
    query, k, types, params, embed_model, rerank_model = _KEY_ARGS
    legacy_digest = rag_cache._digest(
        "search", rag_cache._norm(query), k, tuple(types or ()),
        params.candidate_pool_size, params.rerank_candidates, params.rrf_k,
        params.vector_weight, params.bm25_weight, embed_model, rerank_model,
    )
    assert legacy_digest in rag_cache.search_key(*_KEY_ARGS)
    assert legacy_digest in rag_cache.search_key(*_KEY_ARGS, pushdown=False)


def test_pushdown_gets_its_own_cache_namespace():
    """같은 (query, k, source_types)로 **다른 결과**를 내므로 키를 갈라야 한다 —
    안 가르면 두 방식이 서로의 결과를 읽는다."""
    assert rag_cache.search_key(*_KEY_ARGS) != rag_cache.search_key(*_KEY_ARGS, pushdown=True)


# ── 6. 배선: 누가 push-down을 켜는가 ─────────────────────────────────────────

def test_hybrid_retriever_defaults_to_legacy_behaviour():
    from app.services.agent_retriever import HybridRetriever, get_hybrid_retriever

    assert HybridRetriever().pushdown is False
    assert get_hybrid_retriever().pushdown is False
    assert get_hybrid_retriever(pushdown=True).pushdown is True


def test_retriever_forwards_its_pushdown_flag(monkeypatch):
    from app.services import agent_retriever

    seen = {}
    monkeypatch.setattr(
        agent_retriever, "search_actions",
        lambda q, k, source_types, pushdown: seen.update(pushdown=pushdown) or [],
    )
    agent_retriever.HybridRetriever(pushdown=True).search("q", 4, ["action_schema"])
    assert seen["pushdown"] is True


def test_no_version_opts_into_pushdown():
    """모든 버전이 인자 없이 부른다 — 후단 필터가 현재 기준선이다.

    원래 이 테스트는 "v4만 pushdown=True"를 못 박았다. v4가 폐기되면서 pushdown을 켜는
    버전이 하나도 없어졌다 — `get_hybrid_retriever(pushdown=...)` 자체는 살아 있으므로
    v3에 켤 수 있고, 켤 때 이 테스트가 그 사실을 드러낸다(조용히 바뀌지 않게).

    **파일 원문**을 읽는다. `_make_retriever`를 부르면 실제 인프라에 붙고, 함수 객체를
    inspect하면 conftest의 autouse 스텁이 갈아끼운 람다가 잡힌다(둘 다 이 사실을 못 본다).
    """
    from pathlib import Path

    import app.agent as agent_pkg

    agent_root = Path(agent_pkg.__file__).parent
    for version in ("v1", "v2", "v3"):
        source = (agent_root / version / "retrieval.py").read_text(encoding="utf-8")
        assert "get_hybrid_retriever()" in source
        assert "pushdown" not in source, f"{version}는 후단 필터 기준선을 유지해야 한다"
