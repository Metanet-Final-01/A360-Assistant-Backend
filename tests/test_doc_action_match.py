"""app/rag/build/doc_action_match.py 단위 테스트.

패키지 개요 페이지 <-> 실제 액션 문서 매칭(계층 재귀 포함)은 doc_action_tree.py로
이전됐다 — 여기 남은 건 순수 문자열/URL 유틸(normalize_pretty_url/pair_by_pretty_url/
canonical_package_name)과 legacy 매칭 함수(normalize_key) 테스트뿐이다.
"""

from app.rag.build.doc_action_match import (
    canonical_package_name,
    normalize_key,
    normalize_pretty_url,
    pair_by_pretty_url,
)


def test_normalize_key_still_works_for_legacy_matching():
    assert normalize_key("Database") == "database"
    assert normalize_key("데이터베이스 패키지") == "데이터베이스패키지"


def test_normalize_pretty_url_strips_locale_prefix():
    assert normalize_pretty_url("/r/ko-kr/cloud-build/cloud-database-command") == "/r/cloud-build/cloud-database-command"


def test_normalize_pretty_url_leaves_locale_free_url_unchanged():
    assert normalize_pretty_url("/r/cloud-build/cloud-database-command") == "/r/cloud-build/cloud-database-command"


def test_pair_by_pretty_url_matches_ko_and_en_docs():
    ko = [{"content_id": "ko1", "pretty_url": "/r/ko-kr/cloud-build/cloud-database-command"}]
    en = [{"content_id": "en1", "pretty_url": "/r/cloud-build/cloud-database-command", "title": "Database package"}]
    pairs = pair_by_pretty_url(ko, en)
    assert pairs["ko1"]["title"] == "Database package"


def test_pair_by_pretty_url_skips_unmatched():
    ko = [{"content_id": "ko1", "pretty_url": "/r/ko-kr/no-english-equivalent"}]
    pairs = pair_by_pretty_url(ko, [])
    assert pairs == {}


def test_canonical_package_name_strips_package_suffix():
    assert canonical_package_name("Database package") == "Database"
    assert canonical_package_name("Snowflake Package") == "Snowflake"


def test_canonical_package_name_handles_korean_suffix_too():
    assert canonical_package_name("Snowflake 패키지") == "Snowflake"
