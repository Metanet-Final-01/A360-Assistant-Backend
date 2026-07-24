# -*- coding: utf-8 -*-
"""ingest의 캐시 publish 경계 (RPA-274) — 순서와 실패 처리를 고정한다.

pipeline.py는 커버리지 스코프 밖(오프라인 CLI)이지만, **캐시 정합성 프로토콜의 절반**이
여기 있다: ① pending 마커 → ② PG → ③ OpenSearch(+refresh) → ④ 세대 공개. 이 순서가
무너지면 rag_cache의 세대 무효화가 장식이 되므로 테스트로 고정한다.

스토어(db·opensearch)와 rag_cache를 전부 기록형 스텁으로 갈아끼운다 — 검증 대상은
"무엇을 어떤 순서로 부르고, 실패를 어떻게 끝내는가"이지 스토어 동작이 아니다.
"""

import argparse
import json

import pytest

import app.rag.config as rag_config
import app.rag.pipeline as pipeline
import app.rag.store.db as rag_db
import app.rag.store.opensearch_client as os_client_mod
from app.services import rag_cache
from app.services.rag_cache import CacheInvalidationError


class _FakeConn:
    def close(self):
        pass


def _args(**over):
    base = {"skip_embedding": True, "clean": False, "skip_opensearch": False}
    base.update(over)
    return argparse.Namespace(**base)


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """cmd_ingest의 모든 외부 의존을 호출 순서 기록 스텁으로 배선한다."""
    calls: list[str] = []

    docs = tmp_path / "rag_documents.jsonl"
    docs.write_text(json.dumps({"id": "d1", "source_type": "doc", "title": "t", "content": "c"}) + "\n",
                    encoding="utf-8")
    monkeypatch.setattr(rag_config, "RAG_DOCUMENTS_JSONL", docs)

    monkeypatch.setattr(rag_db, "connect", lambda: (calls.append("pg_connect"), _FakeConn())[1])
    monkeypatch.setattr(rag_db, "ensure_schema", lambda c: calls.append("pg_schema"))
    monkeypatch.setattr(rag_db, "clear_all", lambda c: calls.append("pg_clean"))
    monkeypatch.setattr(rag_db, "upsert_documents", lambda c, d, e: (calls.append("pg_upsert"), len(d))[1])

    monkeypatch.setattr(os_client_mod, "connect", lambda: (calls.append("os_connect"), object())[1])
    monkeypatch.setattr(os_client_mod, "delete_index", lambda c: calls.append("os_clean"))
    monkeypatch.setattr(os_client_mod, "ensure_index", lambda c: calls.append("os_ensure"))
    monkeypatch.setattr(os_client_mod, "bulk_index", lambda c, d: (calls.append("os_bulk"), len(d))[1])
    monkeypatch.setattr(os_client_mod, "refresh_index", lambda c: calls.append("os_refresh"))

    monkeypatch.setattr(rag_cache, "mark_ingest_pending", lambda: calls.append("cache_pending"))
    monkeypatch.setattr(rag_cache, "publish_generation", lambda: (calls.append("cache_publish"), 1)[1])
    monkeypatch.setattr(rag_cache, "cleanup_old_generations", lambda: (calls.append("cache_cleanup"), 0)[1])
    monkeypatch.setattr(rag_cache, "stats", lambda: {"backend": "redis"})
    return calls


def test_success_publishes_after_refresh(wired):
    pipeline.cmd_ingest(_args())
    assert wired.index("cache_pending") < wired.index("pg_upsert")   # 마커가 스토어 변경보다 먼저
    assert wired.index("os_refresh") > wired.index("os_bulk")        # refresh는 bulk 뒤
    assert wired.index("cache_publish") > wired.index("os_refresh")  # 공개는 refresh 뒤에만
    assert "cache_cleanup" in wired


def test_opensearch_failure_never_publishes(wired, monkeypatch):
    """PG 성공 후 OpenSearch 실패(부분 적재) — 세대 공개 없이 non-zero 종료, 마커는 남는다."""
    monkeypatch.setattr(os_client_mod, "bulk_index",
                        lambda c, d: (_ for _ in ()).throw(RuntimeError("bulk 실패")))
    with pytest.raises(SystemExit) as exc:
        pipeline.cmd_ingest(_args())
    assert exc.value.code                      # non-zero(메시지) 종료 — 성공으로 안 숨는다
    assert "pg_upsert" in wired                # PG는 이미 커밋됐다(부분 상태)
    assert "cache_publish" not in wired        # 부분 상태를 새 세대로 공개하지 않는다
    assert wired.count("cache_pending") == 1   # 마커는 세워졌고, 지우는 호출은 없다


def test_clean_partial_failure_never_publishes(wired, monkeypatch):
    """--clean 후 OpenSearch 재색인 실패 — 색인이 빈 최악의 부분 상태도 공개되지 않는다."""
    monkeypatch.setattr(os_client_mod, "bulk_index",
                        lambda c, d: (_ for _ in ()).throw(RuntimeError("재색인 실패")))
    with pytest.raises(SystemExit) as exc:
        pipeline.cmd_ingest(_args(clean=True))
    assert exc.value.code
    assert "pg_clean" in wired and "os_clean" in wired
    assert "cache_publish" not in wired


def test_pending_marker_failure_aborts_before_stores(wired, monkeypatch):
    """마커를 못 세우면 스토어를 만지기 전에 중단 — 무효화 실패가 조용히 지나가지 않는다."""
    monkeypatch.setattr(rag_cache, "mark_ingest_pending",
                        lambda: (_ for _ in ()).throw(CacheInvalidationError("redis down")))
    with pytest.raises(SystemExit) as exc:
        pipeline.cmd_ingest(_args())
    assert exc.value.code
    assert "pg_connect" not in wired           # 아무것도 안 만졌다


def test_publish_failure_exits_nonzero(wired, monkeypatch):
    """적재는 됐는데 세대 공개 실패 — '0건 무효화 성공'으로 숨기지 않고 non-zero 종료."""
    monkeypatch.setattr(rag_cache, "publish_generation",
                        lambda: (_ for _ in ()).throw(CacheInvalidationError("redis down")))
    with pytest.raises(SystemExit) as exc:
        pipeline.cmd_ingest(_args())
    assert exc.value.code
    assert "os_refresh" in wired               # 적재 자체는 끝났다
    assert "cache_cleanup" not in wired        # 공개 실패면 정리도 없다


def test_skip_opensearch_publishes_after_pg_only(wired):
    """--skip-opensearch는 운영자의 명시적 선택 — PG 반영 뒤 공개하고 refresh는 없다."""
    pipeline.cmd_ingest(_args(skip_opensearch=True))
    assert "os_bulk" not in wired and "os_refresh" not in wired
    assert wired.index("cache_publish") > wired.index("pg_upsert")
