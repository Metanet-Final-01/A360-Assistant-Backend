"""app/rag/sources/docs_crawler.py 단위 테스트 — 네트워크 없이 순수 함수만."""

from app.rag.sources.docs_crawler import flatten_menu


def test_flatten_menu_tracks_parent_menu_id():
    # 실측 확인(2026-07-10): 사이트 메뉴의 children이 실제 사이드바와 정확히 일치하는 진짜
    # 부모-자식 계층이다 — 이 parent_menu_id가 doc_action_tree.py의 계층 판별 근거가 된다.
    menu = [
        {
            "tocId": "root1", "contentId": "c-root1", "title": "Aisera 패키지", "prettyUrl": "/r/aisera",
            "children": [
                {"tocId": "child1", "contentId": "c-child1", "title": "콘텐츠 수집 작업", "prettyUrl": "/r/aisera/collect"},
                {"tocId": "child2", "contentId": "c-child2", "title": "콘텐츠 질문하기 작업", "prettyUrl": "/r/aisera/ask"},
            ],
        },
    ]
    topics = flatten_menu(menu)
    by_menu_id = {t["menu_id"]: t for t in topics}
    assert by_menu_id["root1"]["parent_menu_id"] is None
    assert by_menu_id["child1"]["parent_menu_id"] == "root1"
    assert by_menu_id["child2"]["parent_menu_id"] == "root1"


def test_flatten_menu_still_tracks_breadcrumbs():
    menu = [
        {"tocId": "a", "contentId": "ca", "title": "빌드 자동화", "children": [
            {"tocId": "b", "contentId": "cb", "title": "Task Bot", "children": [
                {"tocId": "c", "contentId": "cc", "title": "자동화 구축을 위한 작업", "children": []},
            ]},
        ]},
    ]
    topics = flatten_menu(menu)
    by_menu_id = {t["menu_id"]: t for t in topics}
    assert by_menu_id["c"]["breadcrumbs"] == ["빌드 자동화", "Task Bot"]


def test_flatten_menu_skips_nodes_without_content_id_but_still_walks_children():
    # 순수 네비게이션용 그룹 노드는 contentId가 없을 수 있다 — 자기 자신은 결과에서
    # 빠지지만, 그 자식들의 parent_menu_id는 여전히 정확해야 한다.
    menu = [
        {"tocId": "group", "contentId": None, "title": "그룹", "children": [
            {"tocId": "real", "contentId": "c-real", "title": "실제 문서", "children": []},
        ]},
    ]
    topics = flatten_menu(menu)
    assert len(topics) == 1
    assert topics[0]["menu_id"] == "real"
    assert topics[0]["parent_menu_id"] == "group"
