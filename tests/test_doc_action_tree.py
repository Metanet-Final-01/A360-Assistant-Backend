"""app/rag/build/doc_action_tree.py 단위 테스트.

메뉴의 parent_menu_id 기반 계층(실측 확인된 패턴: 3~4단계 계층 포함)을 합성 픽스처로
검증한다. 메뉴는 진짜 트리라 순환/공유 리프가 구조적으로 불가능하므로(본문 링크 기반이던
이전 버전과 달리) 그런 케이스의 테스트는 없다.
"""

from app.rag.build.doc_action_tree import (
    build_all_trees,
    build_children_index,
    find_root_docs,
    resolve_tree,
    tree_to_dict,
)

_PACKAGE_BREADCRUMBS = ["빌드 자동화", "Task Bot", "자동화 구축을 위한 작업"]


def _doc(title, menu_id, breadcrumbs=None):
    return {"title": title, "menu_id": menu_id, "breadcrumbs": breadcrumbs if breadcrumbs is not None else _PACKAGE_BREADCRUMBS}


def _with_parent(doc, parent_menu_id):
    return {**doc, "parent_menu_id": parent_menu_id}


def test_find_root_docs_requires_title_suffix_and_children():
    root = _doc("Snowflake 패키지", "root1")
    leaf = _with_parent(_doc("연결 문서", "leaf1"), "root1")
    no_children = _doc("Google 패키지", "root2")
    not_package = _doc("일반 문서", "other1")
    leaf_of_not_package = _with_parent(_doc("리프", "leaf2"), "other1")
    english = _doc("Aisera Package", "root3")
    leaf_english = _with_parent(_doc("리프2", "leaf3"), "root3")

    docs = [root, leaf, no_children, not_package, leaf_of_not_package, english, leaf_english]
    children_index = build_children_index(docs)
    roots = find_root_docs(docs, children_index)
    assert {d["menu_id"] for d in roots} == {"root1", "root3"}


def test_find_root_docs_excludes_package_titled_node_outside_the_real_branch():
    # 실측 확인(2026-07-10): 제목이 "~패키지"로 끝나는 노드가 143개 있었는데, 그중
    # "v.40에서 사용 가능한 패키지" 등 6개는 "릴리스 정보"/"Cloud Service"/"관리" 같은
    # 완전히 다른 브랜치에 있는 버전별 패키지 목록/호환성 표였다 — 진짜 패키지가 아니다.
    real_package = _doc("Snowflake 패키지", "root1")
    real_leaf = _with_parent(_doc("리프", "leaf1"), "root1")
    fake_package = _doc(
        "v.40에서 사용 가능한 패키지", "fake1",
        breadcrumbs=["릴리스 정보", "Automation 360 릴리스 정보", "Automation 360 v.40 릴리스 정보"],
    )
    fake_leaf = _with_parent(_doc("가짜 리프", "fakeleaf1"), "fake1")

    docs = [real_package, real_leaf, fake_package, fake_leaf]
    children_index = build_children_index(docs)
    roots = find_root_docs(docs, children_index)
    assert {d["menu_id"] for d in roots} == {"root1"}


def test_direct_leaf_resolution_depth_1():
    root = _doc("Snowflake 패키지", "root")
    leaf1 = _with_parent(_doc("연결 문서", "leaf1"), "root")
    leaf2 = _with_parent(_doc("선택 문서", "leaf2"), "root")
    docs = [root, leaf1, leaf2]
    tree = resolve_tree(root, build_children_index(docs))
    assert len(tree.leaves) == 2
    assert {leaf.doc["menu_id"] for leaf in tree.leaves} == {"leaf1", "leaf2"}
    assert all(leaf.depth == 1 for leaf in tree.leaves)
    assert tree.category_docs == []


def test_multi_hop_category_resolves_to_real_leaves():
    # 실측 확인된 패턴: 루트 -> 카테고리(메뉴 자식을 가짐) -> 리프
    root = _doc("Apple Numbers 패키지", "root")
    category = _with_parent(_doc("Apple Numbers의 셀 작업", "category"), "root")
    leaf1 = _with_parent(_doc("셀 지우기 작업", "leaf1"), "category")
    leaf2 = _with_parent(_doc("찾기 작업", "leaf2"), "category")
    docs = [root, category, leaf1, leaf2]
    tree = resolve_tree(root, build_children_index(docs))
    assert len(tree.leaves) == 2
    assert {leaf.doc["menu_id"] for leaf in tree.leaves} == {"leaf1", "leaf2"}
    assert all(leaf.depth == 2 for leaf in tree.leaves)
    assert len(tree.category_docs) == 1
    assert tree.category_docs[0]["menu_id"] == "category"


def test_deep_4_level_hierarchy():
    root = _doc("데이터베이스 패키지", "root")
    cat1 = _with_parent(_doc("데이터베이스에 연결 작업 사용", "cat1"), "root")
    cat2 = _with_parent(_doc("Windows 인증을 사용하여 Microsoft SQL Server에 연결", "cat2"), "cat1")
    leaf1 = _with_parent(_doc("읽어오기 작업", "leaf1"), "cat2")
    docs = [root, cat1, cat2, leaf1]
    tree = resolve_tree(root, build_children_index(docs))
    assert len(tree.leaves) == 1
    assert tree.leaves[0].doc["menu_id"] == "leaf1"
    assert tree.leaves[0].depth == 3
    assert len(tree.category_docs) == 2
    assert tree.leaves[0].path_titles == [
        "데이터베이스 패키지", "데이터베이스에 연결 작업 사용",
        "Windows 인증을 사용하여 Microsoft SQL Server에 연결", "읽어오기 작업",
    ]


def test_build_all_trees_finds_all_roots():
    root1 = _doc("Snowflake 패키지", "root1")
    leaf1 = _with_parent(_doc("리프1", "leaf1"), "root1")
    root2 = _doc("Aisera 패키지", "root2")
    leaf2 = _with_parent(_doc("리프2", "leaf2"), "root2")
    trees = build_all_trees([root1, leaf1, root2, leaf2])
    assert {t.root_doc["menu_id"] for t in trees} == {"root1", "root2"}


def test_tree_to_dict_serializes_categories_and_leaves_with_paths():
    root = {**_doc("Apple Numbers 패키지", "root"), "url": "https://docs/root"}
    category = {**_with_parent(_doc("셀 작업", "category"), "root"), "url": "https://docs/category"}
    leaf = {**_with_parent(_doc("지우기 작업", "leaf1"), "category"), "url": "https://docs/leaf1"}
    docs = [root, category, leaf]
    tree = resolve_tree(root, build_children_index(docs))

    result = tree_to_dict(tree)
    assert result["root_title"] == "Apple Numbers 패키지"
    assert result["root_url"] == "https://docs/root"
    assert result["categories"] == [{"title": "셀 작업", "url": "https://docs/category", "menu_id": "category"}]
    assert result["leaves"] == [{
        "title": "지우기 작업", "url": "https://docs/leaf1", "menu_id": "leaf1",
        "depth": 2, "path_titles": ["Apple Numbers 패키지", "셀 작업", "지우기 작업"],
    }]
