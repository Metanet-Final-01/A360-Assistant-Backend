"""사이트 메뉴(목차)의 부모-자식 관계(`parent_menu_id`)를 따라가, 패키지 개요
페이지(루트) 아래 실제로 몇 단계에 있든 모든 리프 문서를 찾아 그 루트에 귀속시킨다.

이전 버전은 본문 안 하이퍼링크(`structure.action_index`)로 계층을 재구성했다. 실측
확인(2026-07-10, `app/rag/_investigation_notes/HTML_STRUCTURE_INSIGHTS.md` 참고) 결과
사이트 메뉴 API의 `children`이 실제 사이드바 트리와 정확히 일치하는 진짜 계층이라,
본문 링크 파싱보다 근본적으로 더 정확하다 — 진짜 트리 구조라 순환 자체가 있을 수
없고, 노드마다 부모가 정확히 하나뿐이라 같은 리프를 두 루트가 공유하는 경우도 있을
수 없다(둘 다 이전 버전에서는 실제로 겪었던 문제). `docs_crawler.py::flatten_menu()`가
각 문서에 `parent_menu_id`를 이미 남겨두므로 재크롤링 없이 바로 쓸 수 있다.

"이 리프가 진짜 액션인지, 참고자료/사용예시일 뿐인지"는 이 모듈이 답하지 않는다 —
그건 규칙 기반으로 풀리지 않는다고 확인됐고(같은 문서 참고), 별도의 LLM 기반 파싱
Agent가 맡을 몫이다. 이 모듈은 오직 "이 문서가 어느 패키지 밑 몇 단계에 있는가"라는,
확정적으로 풀리는 부분만 담당한다.
"""

from dataclasses import dataclass, field

_PACKAGE_TITLE_SUFFIXES = ("패키지", "package")

# 진짜 패키지는 전부 이 메뉴 경로 밑에 있다(실측 확인, 2026-07-10): "빌드 자동화 > Task Bot >
# 자동화 구축을 위한 작업 > {이름} 패키지". 제목이 "~패키지"로 끝나는 노드가 143개나
# 있었는데, 그중 6개는 완전히 다른 브랜치(릴리스 정보, Cloud Service, 관리 등)에 있는
# 버전별 패키지 업데이트 목록/호환성 표였다 — 진짜 패키지가 아닌데 제목만 우연히
# "패키지"로 끝난 것. 이 브랜치 스코프를 같이 확인해야 그런 오탐을 피할 수 있다.
_PACKAGE_ROOT_BREADCRUMB = "자동화 구축을 위한 작업"


def build_children_index(docs: list[dict]) -> dict[str, list[dict]]:
    """parent_menu_id -> 그 자식 문서 리스트."""
    children: dict[str, list[dict]] = {}
    for d in docs:
        parent_menu_id = d.get("parent_menu_id")
        if parent_menu_id:
            children.setdefault(parent_menu_id, []).append(d)
    return children


def find_root_docs(docs: list[dict], children_index: dict[str, list[dict]]) -> list[dict]:
    """"자동화 구축을 위한 작업" 밑에서, 제목이 "~패키지"/"~package"로 끝나고 실제 메뉴
    자식이 있는 문서만 루트로 본다.

    두 조건 다 실측으로 확인된 것이다 — 브랜치 스코프 없이 제목만 봤다면 "v.40에서
    사용 가능한 패키지" 같은, 완전히 다른 브랜치에 있는 버전별 목록 페이지까지 패키지로
    오인했을 것이다. 반대로 브랜치만 보고 제목을 안 보면, 같은 브랜치의 비-패키지
    형제 문서("Bot 편집기의 고급 검색 옵션" 등)까지 걸린다.
    """
    roots = []
    for d in docs:
        title = (d.get("title") or "").rstrip().lower()
        if not any(title.endswith(suffix) for suffix in _PACKAGE_TITLE_SUFFIXES):
            continue
        if _PACKAGE_ROOT_BREADCRUMB not in (d.get("breadcrumbs") or []):
            continue
        if children_index.get(d.get("menu_id")):
            roots.append(d)
    return roots


@dataclass
class LeafEntry:
    doc: dict
    depth: int  # 1 = 루트의 직계 자식
    path_titles: list[str]  # 감사(audit)용 — 루트 -> ... -> 이 리프까지의 제목 경로


@dataclass
class PackageActionTree:
    root_doc: dict
    leaves: list[LeafEntry] = field(default_factory=list)
    category_docs: list[dict] = field(default_factory=list)


def resolve_tree(root_doc: dict, children_index: dict[str, list[dict]]) -> PackageActionTree:
    """root_doc의 메뉴 자식을 리프(더 이상 자식이 없는 문서)에 도달할 때까지 재귀적으로
    따라간다. 메뉴는 진짜 트리라 순환/공유 자체가 구조적으로 불가능하므로(이전 버전이
    본문 링크 기반이었을 때는 둘 다 실제로 겪었던 문제), 그런 경우를 막는 로직이
    필요 없다.
    """
    tree = PackageActionTree(root_doc=root_doc)

    def dfs(doc: dict, depth: int, path_titles: list[str]) -> None:
        children = children_index.get(doc["menu_id"], [])
        for child in children:
            child_path_titles = path_titles + [child["title"]]
            if children_index.get(child["menu_id"]):
                tree.category_docs.append(child)
                dfs(child, depth + 1, child_path_titles)
            else:
                tree.leaves.append(LeafEntry(child, depth + 1, child_path_titles))

    dfs(root_doc, 0, [root_doc["title"]])
    return tree


def build_all_trees(docs: list[dict]) -> list[PackageActionTree]:
    """전체 문서에서 발견되는 모든 패키지 트리를 만든다."""
    children_index = build_children_index(docs)
    roots = find_root_docs(docs, children_index)
    return [resolve_tree(root, children_index) for root in roots]


def tree_to_dict(tree: PackageActionTree) -> dict:
    """패키지 하나의 확정된 구조(루트/카테고리/리프)를 JSON으로 남기기 위한 형태로 바꾼다.

    구조화 여부(패키지 판별, 계층)는 팀 결정으로 확정된 부분이라 JAR 유무와 무관하게
    모든 패키지에 대해 남긴다 — 리프가 진짜 액션인지 여부는 여기 없다(그건 Agent 몫).
    """
    return {
        "root_title": tree.root_doc.get("title"),
        "root_url": tree.root_doc.get("url"),
        "categories": [
            {"title": c.get("title"), "url": c.get("url"), "menu_id": c.get("menu_id")}
            for c in tree.category_docs
        ],
        "leaves": [
            {
                "title": leaf.doc.get("title"),
                "url": leaf.doc.get("url"),
                "menu_id": leaf.doc.get("menu_id"),
                "depth": leaf.depth,
                "path_titles": leaf.path_titles,
            }
            for leaf in tree.leaves
        ],
    }
