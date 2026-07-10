"""OpenSearch BM25 키워드 검색. pgvector와 별도로 rag_documents를 동일 id로 색인한다."""

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


@log_call("bm25_search", capture_args=("query", "size"), capture_result=lambda r: {"count": len(r)})
def keyword_search(client: OpenSearch, query: str, size: int) -> list[dict]:
    body = {
        "size": size,
        "query": {
            "multi_match": {
                "query": query,
                "fields": ["title^2", "content"],
                "type": "best_fields",
            }
        },
    }
    resp = client.search(index=config.OPENSEARCH_INDEX, body=body)
    results = []
    for hit in resp["hits"]["hits"]:
        src = hit["_source"]
        results.append({**src, "score": hit["_score"]})
    return results
