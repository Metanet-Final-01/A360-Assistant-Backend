"""app/rag/build/naive_leaf_actions.py 단위 테스트.

필터링 없이 모든 리프를 그대로 액션 후보로 나열하는지만 확인한다 — 파라미터
스키마는 만들지 않는다는 게 이 모듈의 핵심 제약이라, 반환 필드에 그런 키가
없다는 것도 같이 검증한다.
"""

from app.rag.build.doc_action_tree import build_children_index, resolve_tree
from app.rag.build.naive_leaf_actions import leaves_as_naive_actions

_BREADCRUMBS = ["빌드 자동화", "Task Bot", "자동화 구축을 위한 작업"]


def _doc(title, menu_id, url, parent_menu_id=None):
    d = {"title": title, "menu_id": menu_id, "url": url, "breadcrumbs": _BREADCRUMBS}
    if parent_menu_id:
        d["parent_menu_id"] = parent_menu_id
    return d


def test_every_leaf_becomes_one_action_record_without_schema_fields():
    root = _doc("Snowflake 패키지", "root", "https://docs/root")
    leaf1 = _doc("연결 문서", "leaf1", "https://docs/leaf1", parent_menu_id="root")
    leaf2 = _doc("선택 문서", "leaf2", "https://docs/leaf2", parent_menu_id="root")
    docs = [root, leaf1, leaf2]
    tree = resolve_tree(root, build_children_index(docs))

    records = leaves_as_naive_actions("Snowflake", tree)

    assert len(records) == 2
    assert {r["title"] for r in records} == {"연결 문서", "선택 문서"}
    for r in records:
        assert r["package_name"] == "Snowflake"
        assert set(r.keys()) == {"package_name", "title", "url", "path_titles"}
