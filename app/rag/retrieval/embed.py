"""임베딩 생성. Anthropic은 임베딩 API가 없어 Voyage AI(공식 권장) 또는 OpenAI를 사용한다."""

import time

import httpx

from .. import config

# 한국어는 문자당 토큰 수가 많아(최대 ~2토큰/자) 보수적으로 자른다: 4000자 ≈ 최대 8k 토큰
_BATCH_SIZE = 16
_MAX_CHARS = 4000


def _post_with_retry(url: str, headers: dict, payload: dict, retries: int = 5) -> dict:
    with httpx.Client(timeout=60.0) as client:
        for attempt in range(retries):
            resp = client.post(url, headers=headers, json=payload)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = float(resp.headers.get("retry-after", 2**attempt))
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
    raise RuntimeError(f"embedding API failed after {retries} retries: {url}")


def _embed_voyage(texts: list[str]) -> list[list[float]]:
    if not config.VOYAGE_API_KEY:
        raise RuntimeError("VOYAGE_API_KEY 환경변수가 필요합니다")
    data = _post_with_retry(
        "https://api.voyageai.com/v1/embeddings",
        {"Authorization": f"Bearer {config.VOYAGE_API_KEY}"},
        {"model": config.EMBEDDING_MODEL, "input": texts, "input_type": "document"},
    )
    return [item["embedding"] for item in data["data"]]


def _embed_openai(texts: list[str]) -> list[list[float]]:
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY 환경변수가 필요합니다")
    data = _post_with_retry(
        "https://api.openai.com/v1/embeddings",
        {"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        {"model": config.EMBEDDING_MODEL, "input": texts},
    )
    return [item["embedding"] for item in data["data"]]


def embed_texts(texts: list[str], on_progress=None) -> list[list[float]]:
    embed_fn = _embed_voyage if config.EMBEDDING_PROVIDER == "voyage" else _embed_openai
    vectors: list[list[float]] = []
    for start in range(0, len(texts), _BATCH_SIZE):
        batch = [t[:_MAX_CHARS] for t in texts[start : start + _BATCH_SIZE]]
        vectors.extend(embed_fn(batch))
        if on_progress:
            on_progress(min(start + _BATCH_SIZE, len(texts)), len(texts))
    return vectors


def embed_query(text: str) -> list[float]:
    """검색 시 질의 임베딩 (Voyage는 query/document input_type을 구분)."""
    if config.EMBEDDING_PROVIDER == "voyage":
        data = _post_with_retry(
            "https://api.voyageai.com/v1/embeddings",
            {"Authorization": f"Bearer {config.VOYAGE_API_KEY}"},
            {"model": config.EMBEDDING_MODEL, "input": [text], "input_type": "query"},
        )
        return data["data"][0]["embedding"]
    return _embed_openai([text])[0]
