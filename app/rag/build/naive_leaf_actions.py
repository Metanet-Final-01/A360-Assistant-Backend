"""모든 리프 문서를 그냥 액션으로 간주하는 가장 단순한 베이스라인.

팀 결정(2026-07-10)으로 "이 리프가 진짜 액션인지 참고/예제 문서일 뿐인지"는 규칙
기반으로도 풀리지 않아 별도 LLM 기반 파싱 Agent가 맡기로 했다. 그 Agent가 준비되기
전에도 패키지에 어떤 액션 후보가 있는지 한눈에 훑어볼 수 있도록, 필터링 없이
모든 리프를 그대로 나열하는 이 베이스라인을 따로 둔다.

실제 파라미터 스키마는 모른다 — action_schema로 쓰지 않는다(추천 메뉴 노출 금지,
merge.py가 조회하지 않음). label/url/경로만 담는다.
"""

from .doc_action_tree import PackageActionTree


def leaves_as_naive_actions(package_name: str, tree: PackageActionTree) -> list[dict]:
    return [
        {
            "package_name": package_name,
            "title": leaf.doc.get("title"),
            "url": leaf.doc.get("url"),
            "path_titles": leaf.path_titles,
        }
        for leaf in tree.leaves
    ]
