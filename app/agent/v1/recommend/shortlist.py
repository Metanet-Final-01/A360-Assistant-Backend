"""shortlist — 단계별 후보 액션 '메뉴판'을 만든다 (검색 → 하이드레이션).

두 단계 조회(A360 카탈로그 전략):
1. 의미 검색: substep 문장으로 retriever(hybrid search_actions 위임)를 돌려 후보
   (package, action)을 좁힌다.
2. 하이드레이션: 각 후보를 카탈로그 구조 스펙으로 부풀린다 — compose가 정확한
   파라미터 name·enum·기본값을 보고 고르게. 렌더 문자열(content)이 아니라 구조
   스펙을 메뉴로 쓰는 게 골드셋 표기 일치의 핵심.

여기에 결정론 보강을 더한다: plan이 감지한 컨테이너(Loop/If)를 검색 결과와 무관하게
강제 후보로 주입하고, 검색 최고점이 임계 미만인 substep은 '미커버'로 플래그한다.

LLM 없음 — 순수 검색+조회라 단위 테스트 가능(retriever·catalog 주입).
"""

import re

from ..retrieval import Retriever, get_retriever
from ..verify.catalog import CatalogLookup, get_catalog
from .plan import detect_container_candidates, prefmap_packages

# 이 점수 미만이면 substep을 '카탈로그 미커버'로 본다 (휴리스틱, 재정렬 점수 기준).
UNCOVERED_SCORE = 0.35
# substep당 검색 후보 수.
SHORTLIST_K = 8

_SUBSTEP_LINE = re.compile(r"^\s*\d+[\.\)]\s*(.+)$")


def _substeps(step: dict) -> list[str]:
    """단계 description의 번호 목록을 substep으로 쪼갠다 (매핑 단위).

    번호 목록이 없으면 name+description 한 덩어리를 단일 substep으로 본다.
    """
    lines = (step.get("description") or "").splitlines()
    subs = [m.group(1).strip() for line in lines if (m := _SUBSTEP_LINE.match(line))]
    if subs:
        return subs
    base = (step.get("name") or "") + " " + (step.get("description") or "")
    return [base.strip()] if base.strip() else []


def _hydrate(hit: dict, catalog: CatalogLookup) -> dict | None:
    """검색 히트를 카탈로그 구조 스펙으로 부풀린다. 스펙 없으면 None(메뉴 제외)."""
    pkg, act = hit.get("package_name"), hit.get("action_name")
    if not pkg or not act:
        return None
    spec = catalog.get_action_schema(pkg, act)
    if spec is None:
        return None
    return {
        "package": pkg,
        "action": act,
        "label": spec.get("label") or hit.get("title") or act,
        "schema": spec,
        "source": {"title": hit.get("title"), "url": hit.get("url"), "score": hit.get("score")},
        "forced": False,
    }


def _menu_key(entry: dict) -> tuple[str, str]:
    return (entry["package"], entry["action"])


def shortlist(
    step: dict,
    constraints: list[str] | None = None,
    retriever: Retriever | None = None,
    catalog: CatalogLookup | None = None,
) -> dict:
    """단계 하나의 메뉴판을 만든다.

    반환: {menu, examples, uncovered, source_map}.
    - menu: 하이드레이션된 후보 액션 스펙 리스트 (compose 입력)
    - examples: bot_example 히트 (few-shot 관용구)
    - uncovered: 검색이 약한 substep 문장들 (notes 후보)
    - source_map: "pkg/act" → source (check가 액션에 sources 부착할 때 사용)
    """
    retriever = retriever or get_retriever()
    catalog = catalog or get_catalog()
    systems = " ".join(step.get("systems", []))

    menu: dict[tuple[str, str], dict] = {}
    examples: list[dict] = []
    uncovered: list[str] = []

    for sub in _substeps(step):
        hits = retriever.search(f"{sub} {systems}".strip(), limit=SHORTLIST_K)
        best_score = max((h.get("score") or 0 for h in hits), default=0)
        if best_score < UNCOVERED_SCORE:
            uncovered.append(sub)
        for hit in hits:
            if hit.get("source_type") == "bot_example":
                examples.append({"title": hit.get("title"), "content": hit.get("content")})
                continue
            entry = _hydrate(hit, catalog)
            if entry and _menu_key(entry) not in menu:
                menu[_menu_key(entry)] = entry

    # 결정론 보강: 컨테이너(Loop/If) 강제 후보를 스펙과 함께 주입.
    for pkg, act in detect_container_candidates(step):
        spec = catalog.get_action_schema(pkg, act)
        if spec and (pkg, act) not in menu:
            menu[(pkg, act)] = {
                "package": pkg, "action": act, "label": spec.get("label") or act,
                "schema": spec, "source": None, "forced": True,
            }

    entries = list(menu.values())
    source_map = {
        f"{e['package']}/{e['action']}": e["source"] for e in entries if e.get("source")
    }
    return {
        "menu": entries,
        "examples": examples[:2],  # few-shot은 소수만
        "uncovered": uncovered,
        "source_map": source_map,
        "prefmap": prefmap_packages(step),  # 진단·정렬 힌트
    }
