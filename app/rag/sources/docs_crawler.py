"""docs.automationanywhere.com 공식 문서 크롤러.

find_map()으로 문서 맵(예: "Automation 360" 한국어판)을 찾고, get_menu()/flatten_menu()로
사이트 메뉴(좌측 사이드바 목차)를 breadcrumb 붙은 평평한 토픽 목록으로 바꾼 뒤,
crawl_topics()가 토픽 본문을 받아 JSONL로 저장한다(재시작 시 이미 받은 content_id는 건너뜀).

구현 참고: 이 문서 사이트는 Fluid Topics라는 CMS 플랫폼으로 서비스되고 있어, HTML
스크레이핑 없이 그 /api/khub/* JSON API로 맵/메뉴/본문을 바로 받아올 수 있다.
"""

import json
import time

import httpx
from bs4 import BeautifulSoup

from ..config import DOCS_BASE_URL
from .doc_structure import extract_doc_structure
from .html_structurizer import html_to_structured_json


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


def get_menu(map_id: str) -> list[dict]:
    """문서 사이트 좌측 사이드바 메뉴(목차) 트리를 그대로 받아온다."""
    with _client() as client:
        resp = _get_with_retry(client, f"/api/khub/maps/{map_id}/toc")
        data = resp.json()
        return data if isinstance(data, list) else data.get("toc", [])


def flatten_menu(menu: list[dict]) -> list[dict]:
    """메뉴 트리를 breadcrumbs가 붙은 평평한 토픽 리스트로 변환.

    `parent_menu_id`를 같이 남긴다 — 메뉴의 `children`이 사이트 사이드바와 정확히
    일치하는 진짜 부모-자식 계층이라는 게 실측 확인됐다(2026-07-10,
    app/rag/_investigation_notes/HTML_STRUCTURE_INSIGHTS.md 참고). 본문 안 하이퍼링크로
    계층을 재구성하는 것보다 이게 근본적으로 더 정확하고
    (순환 자체가 구조적으로 불가능), 트리거처럼 다른 브랜치에 있는 것만 별도로 다룬다.
    """
    topics: list[dict] = []

    def walk(nodes: list[dict], ancestors: list[str], parent_menu_id: str | None) -> None:
        for node in nodes:
            title = node.get("title", "")
            menu_id = node.get("tocId")
            entry = {
                "content_id": node.get("contentId"),
                "menu_id": menu_id,
                "parent_menu_id": parent_menu_id,
                "title": title,
                "breadcrumbs": ancestors,
                "pretty_url": node.get("prettyUrl", ""),
            }
            if entry["content_id"]:
                topics.append(entry)
            walk(node.get("children", []), ancestors + [title], menu_id)

    walk(menu, [], None)
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
    locale: str | None = None,
) -> int:
    """토픽 본문을 받아 JSONL로 저장. 이미 저장된 content_id는 건너뛴다(재시작 안전).

    locale을 주면 레코드에 "locale" 필드로 같이 남긴다 — 다국어 크롤(예: ko-KR/en-US를
    같은 파일에 합칠 때)에서 레코드가 어느 언어인지 파일 구분 없이도 알 수 있게 한다.
    """
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
                "structure": extract_doc_structure(html),
                # 향후 LLM 기반 파싱 Agent가 raw HTML 대신 저렴하게 소비할 압축 표현
                # (CSS/JS/이미지 데이터 제거, tag/class/data-tocid만 남김).
                "structured_html": html_to_structured_json(html),
                "locale": locale,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1
            if on_progress:
                on_progress(i + 1, len(topics), topic["title"])
            time.sleep(delay_seconds)
    return written
