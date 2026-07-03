"""docs.automationanywhere.com (Fluid Topics) 공개 API 클라이언트.

문서 사이트는 Fluid Topics 플랫폼이라 HTML 스크레이핑 없이
/api/khub/* JSON API로 맵 목록 → 목차(TOC) → 토픽 본문을 정형으로 받을 수 있다.
"""

import json
import time

import httpx
from bs4 import BeautifulSoup

from .config import DOCS_BASE_URL


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=DOCS_BASE_URL,
        headers={"Accept": "application/json", "User-Agent": "a360-assistant-ingest/0.1"},
        timeout=30.0,
    )


def _get_with_retry(client: httpx.Client, url: str, retries: int = 3) -> httpx.Response:
    for attempt in range(retries):
        try:
            resp = client.get(url)
            if resp.status_code in (429, 502, 503):
                raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
            resp.raise_for_status()
            return resp
        except (httpx.TransportError, httpx.HTTPStatusError):
            if attempt == retries - 1:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("unreachable")


def list_maps() -> list[dict]:
    with _client() as client:
        resp = _get_with_retry(client, "/api/khub/maps?page=1&perPage=200")
        return resp.json()


def find_map(locale: str = "ko-KR", title: str = "Automation 360") -> dict:
    for m in list_maps():
        metadata = {x["key"]: x["values"] for x in m.get("metadata", [])}
        if m.get("title") == title and metadata.get("ft:locale") == [locale]:
            return m
    raise ValueError(f"map not found: title={title!r} locale={locale!r}")


def get_toc(map_id: str) -> list[dict]:
    with _client() as client:
        resp = _get_with_retry(client, f"/api/khub/maps/{map_id}/toc")
        data = resp.json()
        return data if isinstance(data, list) else data.get("toc", [])


def flatten_toc(toc: list[dict]) -> list[dict]:
    """TOC 트리를 breadcrumbs가 붙은 평평한 토픽 리스트로 변환."""
    topics: list[dict] = []

    def walk(nodes: list[dict], ancestors: list[str]) -> None:
        for node in nodes:
            title = node.get("title", "")
            entry = {
                "content_id": node.get("contentId"),
                "toc_id": node.get("tocId"),
                "title": title,
                "breadcrumbs": ancestors,
                "pretty_url": node.get("prettyUrl", ""),
            }
            if entry["content_id"]:
                topics.append(entry)
            walk(node.get("children", []), ancestors + [title])

    walk(toc, [])
    return topics


def fetch_topic_html(client: httpx.Client, map_id: str, content_id: str) -> str:
    resp = _get_with_retry(client, f"/api/khub/maps/{map_id}/topics/{content_id}/content")
    return resp.text


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def crawl_topics(
    map_id: str,
    topics: list[dict],
    out_path,
    delay_seconds: float = 0.2,
    on_progress=None,
) -> int:
    """토픽 본문을 받아 JSONL로 저장. 이미 저장된 content_id는 건너뛴다(재시작 안전)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done: set[str] = set()
    if out_path.exists():
        with open(out_path, encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line)["content_id"])
                except (json.JSONDecodeError, KeyError):
                    continue

    written = 0
    with _client() as client, open(out_path, "a", encoding="utf-8") as f:
        for i, topic in enumerate(topics):
            if topic["content_id"] in done:
                continue
            html = fetch_topic_html(client, map_id, topic["content_id"])
            record = {
                **topic,
                "url": DOCS_BASE_URL + topic["pretty_url"] if topic["pretty_url"] else "",
                "text": html_to_text(html),
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1
            if on_progress:
                on_progress(i + 1, len(topics), topic["title"])
            time.sleep(delay_seconds)
    return written
