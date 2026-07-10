"""크롤링한 문서(docs.jsonl)를 패키지/액션에 연결하는 퍼지 매칭 + 로케일 페어링/이름 정규화.

match_package_docs/match_action_doc(문서 제목·breadcrumb과 패키지/액션 라벨을 정규화한
문자열로 단순 포함 비교)는 merge.py가 JAR 스키마에 공식문서 설명을 붙이는 용도로 쓴다.

패키지 개요 페이지에서 실제 하위 액션을 찾는 일(예전 build_package_action_index)은
doc_action_tree.py로 이전됐다 — 그 함수는 색인표가 있는 문서를 1홉만 보고 flat하게
취급해서, 실제 최대 4단계 계층/순환참조를 못 다뤘다(자세한 경위는
app/rag/_investigation_notes/DOC_SCHEMA_PIPELINE_NOTES.md 18번 항목 참고). 여기 남은 건 그 계층 탐색과
무관한, 순수 문자열/URL 유틸(normalize_pretty_url/pair_by_pretty_url/
canonical_package_name)과 JAR-문서 매칭용 함수뿐이다.
"""

import re


def normalize_key(s: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]", "", (s or "").lower())


def match_package_docs(package: dict, docs: list[dict]) -> list[dict]:
    """패키지명이 breadcrumbs/제목에 등장하는 문서 페이지를 찾는다."""
    keys = {normalize_key(package["package_name"]), normalize_key(package["package_label"])}
    keys.discard("")
    matched = []
    for doc in docs:
        haystack = normalize_key(doc["title"]) + "".join(normalize_key(b) for b in doc["breadcrumbs"])
        if any(k in haystack for k in keys):
            matched.append(doc)
    return matched


def match_action_doc(action: dict, package_docs: list[dict]) -> dict | None:
    """액션 라벨(영문)이 문서 제목에 들어있으면 그 페이지를 액션 문서로 본다.

    한국어 문서는 제목이 번역돼 있어 대부분 패키지 수준 매칭에 그친다 — 그래도 동작에는 문제 없음.
    """
    label = normalize_key(action.get("label") or action.get("name") or "")
    if not label:
        return None
    for doc in package_docs:
        if label in normalize_key(doc["title"]):
            return doc
    return None


_LOCALE_PREFIX = re.compile(r"^(/r)/[a-z]{2}-[a-z]{2}(/.*)$")
_PACKAGE_SUFFIX = re.compile(r"\s*패키지\s*$|\s*package\s*$", re.IGNORECASE)


def normalize_pretty_url(pretty_url: str) -> str:
    """로케일 접두어(/ko-kr, /ja-jp 등)를 제거해 로케일 무관 페이지 키로 만든다.

    실측 확인: 같은 페이지의 ko-KR pretty_url은 "/r/ko-kr/..."이고 en-US는 "/r/..."
    (접두어 없음) — 접두어만 떼면 완전히 동일한 문자열이라 100% 확정적으로 페어링된다.
    """
    m = _LOCALE_PREFIX.match(pretty_url or "")
    return m.group(1) + m.group(2) if m else (pretty_url or "")


def pair_by_pretty_url(docs_a: list[dict], docs_b: list[dict]) -> dict[str, dict]:
    """docs_a의 content_id -> pretty_url이 일치하는 docs_b의 문서. (실측: 116/116 성공)"""
    by_url = {normalize_pretty_url(d["pretty_url"]): d for d in docs_b if d.get("pretty_url")}
    result = {}
    for d in docs_a:
        match = by_url.get(normalize_pretty_url(d.get("pretty_url", "")))
        if match:
            result[d["content_id"]] = match
    return result


def canonical_package_name(english_overview_title: str) -> str:
    """영어 개요 페이지 제목("Database package")에서 " package" 접미어를 떼 순수 이름을 얻는다.

    이게 bots.jsonl의 실제 packageName 표기와 가장 가깝다 — 한국어 제목("데이터베이스
    패키지")에서 뽑으면 완전히 번역된 패키지는 영어 실명과 안 맞는 문제가 있었다(실측: Database
    -> "데이터베이스" 버그).
    """
    return _PACKAGE_SUFFIX.sub("", english_overview_title or "").strip()
