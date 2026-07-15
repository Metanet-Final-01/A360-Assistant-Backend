"""research — 선행 KB 조사로 Capability Dossier를 만든다 (v3 설계 §2-[2]).

v2는 compose ReAct가 후보 1개를 만들며 순차 도구 왕복(≤6)을 했다. v3는 후보가 2~3개라
그 비용이 배가되므로 조사를 선행·공유한다: 후보들이 같은 근거 위에서 경쟁해 심판이
공정해지고, 후보별 compose는 escape hatch 툴콜 2회의 준-단일 호출로 가벼워진다.

한/영 이중 질의(dual-query): KB는 한 행에 영어 식별자+한국어 본문이 혼재하고 검색에
언어 처리가 없다 — 기능 단위마다 (한국어 자연어, 영어 액션 어휘) 질의 쌍을 만들어 둘 다
검색한다. 한국어는 본문(의미), 영어는 식별자(어휘)를 맞혀 상호 보완한다.

검색 히트는 run 단위 sink에 누적된다 — finalize의 sources/confidence 부착 계약 유지.
"""

import asyncio
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from ..orchestrator.jsonio import chat_json
from ..retrieval import get_retriever
from ..verify.catalog import get_catalog
from ..verify.checker import derive_session_registry
from .stream import emit

logger = logging.getLogger(__name__)

_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "research_queries.md").read_text(encoding="utf-8")

# recommend 검색은 액션 후보 메뉴용 — 문서 페이지 오염 방지 (v2 계약 유지).
ACTION_SOURCE_TYPES = ["action_schema", "bot_example"]
_SEARCH_LIMIT = 5
_MAX_UNITS = 8           # 기능 단위 상한 — 질의 폭주 방지
_MAX_MENU_ACTIONS = 14   # Dossier 액션 메뉴 상한 (스펙 포함이라 토큰 비용이 큼)
_DOC_BG_LIMIT = 3        # 배경 지식(doc_page) 검색 건수


# 제어 흐름 구조 액션 후보 — 카탈로그에 실재하는 것만 메뉴에 실린다. 요구사항 문장에는
# 이런 액션이 명시되지 않아 검색 질의가 생성되지 않으므로(0374 JIRA 봇 실측: Loop 이터레이터
# 부재 → Continue 오용, 세션 opener 부재 → 세션 생명주기 통누락) 결정론으로 보완한다.
_STRUCTURAL_CANDIDATES: list[tuple[str, str]] = [
    ("Loop", "cloudUsingLoopAction"),
    ("If", "ifPackageIfAction"),
    ("If", "ifPackageElseIfOptionalAction"),
    ("If", "ifPackageElseAction"),
    ("Error handler", "errorHandlerTry"),
    ("Error handler", "errorHandlerCatch"),
    ("Error handler", "errorHandlerFinally"),
    ("Error handler", "errorHandlerThrow"),
    ("Step", "stepAction"),
]


def structural_complement(catalog, menu_packages: set[str]) -> list[tuple[str, str]]:
    """메뉴를 결정론으로 보완할 (package, action) 목록 — 검색 없이 카탈로그 직조회 (비용 0).

    ① 메뉴에 등장한 패키지의 세션 opener/closer (derive_session_registry 재사용) —
       업무 액션이 뽑혔는데 여닫기가 빠지는 연쇄(세션 생명주기 통누락)를 차단한다.
    ② 제어 흐름 구조 액션(Loop 이터레이터·If·Error handler·Step) — 요구사항 질의로는
       절대 검색되지 않지만 모든 흐름도에 필요한 어휘다.
    카탈로그에 실재하는 것만 반환한다(폐쇄어휘 유지).
    """
    openers, closers = derive_session_registry(catalog)
    candidates: list[tuple[str, str]] = [
        key for key in sorted(openers | closers) if key[0] in menu_packages
    ]
    candidates += _STRUCTURAL_CANDIDATES
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pkg, act in candidates:
        if (pkg, act) in seen:
            continue
        seen.add((pkg, act))
        if catalog.get_action_schema(pkg, act) is not None:
            out.append((pkg, act))
    return out


class _ResearchUnit(BaseModel):
    topic: str = ""
    ko_query: str = ""
    en_query: str = ""


class _ResearchPlan(BaseModel):
    units: list[_ResearchUnit] = Field(default_factory=list)


def _expand_queries(spec: dict) -> list[_ResearchUnit]:
    """FlowSpec 요구를 기능 단위로 묶어 (한국어, 영어) 질의 쌍을 만든다 (LLM 1회, 경량)."""
    req_lines = "\n".join(
        f"- [{r.get('req_id')}] {r.get('text', '')}" for r in spec.get("requirements") or []
    )
    try:
        plan = chat_json(
            [
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": f"[목표]\n{spec.get('goal', '')}\n\n[요구사항]\n{req_lines}"},
            ],
            purpose="recommend",
            model_cls=_ResearchPlan,
        )
        units = [u for u in plan.units if u.ko_query or u.en_query][:_MAX_UNITS]
        if units:
            return units
    except (ValueError, RuntimeError) as e:
        logger.warning("research 질의 확장 실패 — 요구 원문 질의로 강등: %s", e)
    # 강등: 요구 텍스트를 그대로 한국어 질의로 (영어 질의 없음 — 다국어 임베딩에 맡긴다)
    return [
        _ResearchUnit(topic=r.get("req_id") or "", ko_query=r.get("text") or "")
        for r in (spec.get("requirements") or [])[:_MAX_UNITS]
    ]


async def build_dossier(spec: dict, sink: list[dict]) -> dict:
    """Capability Dossier를 만든다: {menu: str, actions: [(pkg, act)], background: str}.

    - 기능 단위별 이중 질의 병렬 검색(action_schema/bot_example) → (pkg, act) 후보 집계
    - 상위 후보의 카탈로그 스펙 프리페치 → 파라미터까지 담긴 액션 메뉴 텍스트
    - 배경 지식: 목표 문장으로 doc_page 1회 검색 (전체 문서 적재 가정 — 없으면 빈 결과)
    """
    retriever = get_retriever()
    catalog = get_catalog()
    units = _expand_queries(spec)

    queries: list[str] = []
    for u in units:
        if u.ko_query.strip():
            queries.append(u.ko_query.strip())
        if u.en_query.strip():
            queries.append(u.en_query.strip())
    emit({"event": "stage", "stage": "searching",
          "message": f"액션 카탈로그 조사 중 ({len(units)}개 기능, 질의 {len(queries)}건)",
          "data": {"queries": [q[:80] for q in queries]}})

    async def _search(q: str, source_types: list[str] | None, limit: int) -> list[dict]:
        try:
            return await asyncio.to_thread(retriever.search, q, limit=limit, source_types=source_types)
        except Exception as e:  # noqa: BLE001 — 검색 한 건 실패가 조사 전체를 막지 않게
            logger.warning("research 검색 실패(%r): %s", q[:50], e)
            return []

    results = await asyncio.gather(*(_search(q, ACTION_SOURCE_TYPES, _SEARCH_LIMIT) for q in queries))
    bg_hits = await _search(spec.get("goal") or "", ["doc_page"], _DOC_BG_LIMIT) if spec.get("goal") else []

    # (pkg, act)별 최고 점수 집계 — RRF/rerank 점수 내림차순 상위만 메뉴에 올린다.
    best: dict[tuple[str, str], float] = {}
    for hits in results:
        sink.extend(hits)
        for h in hits:
            pkg, act = h.get("package_name"), h.get("action_name")
            if pkg and act:
                key = (pkg, act)
                best[key] = max(best.get(key, 0.0), h.get("score") or 0.0)
    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:_MAX_MENU_ACTIONS]

    def _menu_block(pkg: str, act: str, spec_dict: dict) -> str:
        params = ", ".join(
            f"{p['name']}({p.get('type')}{', 필수' if p.get('required') else ''})"
            for p in spec_dict.get("parameters", [])
        )
        rt = spec_dict.get("return_type")
        return (
            f"- {pkg}/{act} «{spec_dict.get('label') or act}»"
            + (f" → 리턴 {rt}" if rt else "")
            + f"\n    파라미터: {params or '없음'}"
        )

    blocks: list[str] = []
    menu_actions: list[tuple[str, str]] = []
    for (pkg, act), score in ranked:
        spec_dict = catalog.get_action_schema(pkg, act)
        if spec_dict is None:
            continue
        menu_actions.append((pkg, act))
        blocks.append(_menu_block(pkg, act, spec_dict))

    # 결정론 보완: 세션 여닫기 + 제어 흐름 구조 액션 — 검색이 못 뽑는 필수 어휘 (실측 보강).
    extra_blocks: list[str] = []
    for pkg, act in structural_complement(catalog, {p for p, _ in menu_actions}):
        if (pkg, act) in menu_actions:
            continue
        spec_dict = catalog.get_action_schema(pkg, act)
        menu_actions.append((pkg, act))
        extra_blocks.append(_menu_block(pkg, act, spec_dict))
    if extra_blocks:
        blocks.append("\n[구조·세션 액션 — 자동 보완: 반복·분기·예외 처리와 세션 여닫기는 반드시 이 표기를 사용]")
        blocks.extend(extra_blocks)

    background = "\n".join(
        f"- {h.get('title')}: {(h.get('content') or '')[:200]}" for h in bg_hits
    )
    if bg_hits:
        sink.extend(bg_hits)

    emit({"event": "stage", "stage": "searching",
          "message": f"조사 완료 — 액션 후보 {len(menu_actions)}개 확보 (구조·세션 보완 {len(extra_blocks)}개 포함)"})
    return {
        "menu": "\n".join(blocks) or "(조사된 액션 없음 — 도구로 직접 검색 필요)",
        "actions": menu_actions,
        "background": background,
    }
