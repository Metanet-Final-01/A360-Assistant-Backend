"""OpenSearch BM25 키워드 검색. pgvector와 별도로 rag_documents를 동일 id로 색인한다."""

import threading

import httpx
from opensearchpy import OpenSearch
from opensearchpy.helpers import bulk

from .. import config
from ..observability import log_call

_INDEX_BODY = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "analysis": {
            "filter": {
                "korean_cjk_bigram": {"type": "cjk_bigram"},
                "english_stop": {"type": "stop", "stopwords": "_english_"},
            },
            "analyzer": {
                "korean_cjk": {
                    "type": "custom",
                    "tokenizer": "standard",
                    "filter": ["cjk_width", "lowercase", "korean_cjk_bigram", "english_stop"],
                }
            },
        },
    },
    "mappings": {
        "properties": {
            "id": {"type": "keyword"},
            "source_type": {"type": "keyword"},
            "package_name": {"type": "keyword"},
            "action_name": {"type": "keyword"},
            "locale": {"type": "keyword"},
            "title": {"type": "text", "analyzer": "korean_cjk", "fields": {"raw": {"type": "keyword"}}},
            "url": {"type": "keyword", "index": False},
            "content": {"type": "text", "analyzer": "korean_cjk"},
            "parent_id": {"type": "keyword"},
            "chunk_index": {"type": "integer"},
        }
    },
}


def connect() -> OpenSearch:
    kwargs = {"hosts": [config.OPENSEARCH_HOST], "http_compress": True, "timeout": 30}
    if config.OPENSEARCH_HOST.startswith("https"):
        kwargs.update(use_ssl=True, verify_certs=True)
    if config.OPENSEARCH_USERNAME:
        kwargs["http_auth"] = (config.OPENSEARCH_USERNAME, config.OPENSEARCH_PASSWORD)
    return OpenSearch(**kwargs)


_client_lock = threading.Lock()
_shared_client: OpenSearch | None = None


def get_shared_client() -> OpenSearch:
    """검색 경로용 프로세스 공용 OpenSearch 클라이언트 (RPA-219).

    검색마다 connect()를 부르면 urllib3 커넥션 풀을 매번 새로 만들어 연결 재사용이
    안 된다. OpenSearch 클라이언트는 내부적으로 스레드 세이프한 풀을 쓰므로
    to_thread 워커들이 공유해도 된다. 적재(ingest)·debug는 계속 connect()를 쓴다.
    """
    global _shared_client
    if _shared_client is None:
        with _client_lock:
            if _shared_client is None:
                _shared_client = connect()
    return _shared_client


def close_shared_client() -> None:
    """앱 종료 시 공용 클라이언트를 닫는다 (main.py lifespan)."""
    global _shared_client
    with _client_lock:
        if _shared_client is not None:
            try:
                _shared_client.close()
            except Exception:  # noqa: BLE001 — 종료 경로에서 실패는 무시
                pass
            _shared_client = None


def connect_async() -> httpx.AsyncClient:
    """/api/rag/search 전용 비동기 경로. opensearchpy는 이 환경에 async 클라이언트
    extra(aiohttp)가 안 깔려 있어(opensearchpy[async] 미설치) 쓸 수 없다 — OpenSearch가
    순수 REST API라, opensearchpy 없이 httpx.AsyncClient로 _search를 직접 호출한다.
    적재(ingest)·debug 엔드포인트는 그대로 동기 connect()+opensearchpy를 쓴다."""
    auth = (config.OPENSEARCH_USERNAME, config.OPENSEARCH_PASSWORD) if config.OPENSEARCH_USERNAME else None
    verify = config.OPENSEARCH_HOST.startswith("https")
    return httpx.AsyncClient(base_url=config.OPENSEARCH_HOST, auth=auth, verify=verify, timeout=30.0)


def ensure_index(client: OpenSearch) -> None:
    if not client.indices.exists(index=config.OPENSEARCH_INDEX):
        client.indices.create(index=config.OPENSEARCH_INDEX, body=_INDEX_BODY)


def delete_index(client: OpenSearch) -> None:
    """색인 전체 삭제. bulk_index는 옛 문서를 지우지 않는 순수 색인(op_type=index)이라,
    RAG 구조를 크게 바꿔 재적재할 때는 명시적으로 지우고 ensure_index로 새로 만들어야
    한다 — 자동 호출되지 않고 `ingest --clean`에서만 실행된다."""
    if client.indices.exists(index=config.OPENSEARCH_INDEX):
        client.indices.delete(index=config.OPENSEARCH_INDEX)


def bulk_index(client: OpenSearch, documents: list[dict]) -> int:
    def _actions():
        for doc in documents:
            yield {
                "_op_type": "index",
                "_index": config.OPENSEARCH_INDEX,
                "_id": doc["id"],
                "_source": {
                    "id": doc["id"],
                    "source_type": doc["source_type"],
                    "package_name": doc.get("package_name"),
                    "action_name": doc.get("action_name"),
                    "locale": doc.get("locale"),
                    "title": doc["title"],
                    "url": doc.get("url"),
                    "content": doc["content"],
                    "parent_id": doc.get("parent_id", doc["id"]),
                    "chunk_index": doc.get("chunk_index", 0),
                },
            }

    success, _ = bulk(client, _actions())
    return success


def refresh_index(client: OpenSearch) -> None:
    """색인 refresh — bulk 직후의 문서를 검색 가시 상태로 만든다 (RPA-274).

    ingest는 이게 끝난 뒤에야 캐시 세대를 공개한다: refresh 전에 공개하면 새 세대의
    첫 검색들이 '색인엔 있는데 아직 안 보이는' 문서를 놓친 결과를 캐싱한다.
    """
    client.indices.refresh(index=config.OPENSEARCH_INDEX)


def _keyword_search_body(query: str, size: int) -> dict:
    return {
        "size": size,
        "query": {
            "multi_match": {
                "query": query,
                "fields": ["title^2", "content"],
                "type": "best_fields",
            }
        },
    }


def _keyword_search_results(resp_json: dict) -> list[dict]:
    return [{**hit["_source"], "score": hit["_score"]} for hit in resp_json["hits"]["hits"]]


@log_call("bm25_search", capture_args=("query", "size"), capture_result=lambda r: {"count": len(r)})
def keyword_search(client: OpenSearch, query: str, size: int) -> list[dict]:
    resp = client.search(index=config.OPENSEARCH_INDEX, body=_keyword_search_body(query, size))
    return _keyword_search_results(resp)


@log_call("bm25_search", capture_args=("query", "size"), capture_result=lambda r: {"count": len(r)})
async def keyword_search_async(client: httpx.AsyncClient, query: str, size: int) -> list[dict]:
    resp = await client.post(f"/{config.OPENSEARCH_INDEX}/_search", json=_keyword_search_body(query, size))
    resp.raise_for_status()
    return _keyword_search_results(resp.json())
