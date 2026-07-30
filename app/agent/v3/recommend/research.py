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

from app.agent.knowledge.derive import (
    derive_competing_packages,
    derive_packages,
    derive_structural_actions,
)

from ..orchestrator.jsonio import chat_json
from ..verify.checker import derive_session_registry
from .stream import emit

logger = logging.getLogger(__name__)

_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "research_queries.md").read_text(encoding="utf-8")

# recommend 검색은 액션 후보 메뉴용 — 문서 페이지 오염 방지 (v2 계약 유지).
ACTION_SOURCE_TYPES = ["action_schema", "bot_example"]
_SEARCH_LIMIT = 5
# 기능 단위·메뉴 상한. 스펙에 운영 골격 요구(priority=should, source=inferred)가 들어오면서
# 조사 대상이 업무 요구 + 골격 요구로 늘었다 — 상한을 그대로 두면 골격 질의가 업무 질의를
# 밀어내(실제로 자동화할 기능이 메뉴에서 빠져) 조용히 품질이 내려간다. 그래서 폭을 넓히되,
# 우선순위는 코드로 강제한다(_expand_queries: must 먼저, should는 남는 자리).
# 검색은 병렬 I/O라 단위 증가 비용이 작고, 메뉴 증가만 프롬프트 토큰에 비례한다.
_MAX_UNITS = 10          # 기능 단위 상한 — 질의 폭주 방지
# 검색 유래 액션에는 상한을 두지 않는다 (RPA-355).
#
# 앞서 18이었다. 그 값에 **근거가 없었다** — v3 최초 커밋의 14를 `_MAX_UNITS` 8→10에
# 맞춰 비례로 올린 것이고, 토큰 예산에서 역산한 값이 아니다. 산수도 안 맞았다: 단위가 10개면
# 깊이 1까지만 완주해도 20칸이 필요해 **최대 단위 수에서는 깊이 1조차 못 채웠다.**
#
# 그 상한이 실제로 업무를 망쳤다. 실측(2026-07-30 턴 861f64deebd9) 「증권 버튼 클릭」 단위에서
# 요소 클릭(`Recorder/Click`, 0.4062)이 좌표 클릭(`Mouse/Click`, 0.4902)에 0.084점 차로
# 밀려 6위가 됐고, 단위당 2칸이라 잘렸다. 모델은 요소 클릭이 필요함을 알고 있었지만
# (notes에 그렇게 적었다) 메뉴에 없어 자리표시자를 남겼다. 같은 문서를 두 번 돌렸을 때
# 한 턴은 받고 한 턴은 못 받았다 — 잡음이 결과를 갈랐다.
#
# 무제한이 아니다. 구조적 상한이 `_MAX_UNITS × 2 × _SEARCH_LIMIT = 100`이고, 중복을 걷어낸
# 실측은 48개였다. 토큰은 구조 보완 과잉 공급을 걷어낸 것으로 상계된다
# (`structural_complement` 주석 참고).
_MAX_MENU_ACTIONS = None  # None = 상한 없음
# 사용자 제공 카탈로그의 메뉴 상한 — 검색으로 좁힐 수 없어 전량을 싣지만, 프롬프트가
# 무한정 커지는 것은 막는다. 검색 경로보다 훨씬 넉넉하다(실제 카탈로그는 보통 수십 개라
# 이 값에 닿지 않고, 닿으면 잘린 사실을 프롬프트·로그·진행 메시지 셋 다에 남긴다).
_MAX_USER_MENU_ACTIONS = 200
_DOC_BG_LIMIT = 3        # 배경 지식(doc_page) 검색 건수
# 기능 단위의 최고 점수가 '가장 잘 찾힌 단위'의 이 비율 미만이면 검색이 약한 것으로 본다.
# 절대값이 아니라 상대 비율인 이유: 재정렬 점수의 절대 크기는 질의·모델마다 달라 임계를
# 박아두면 모델을 바꾸는 순간 거짓말이 된다. 실측(2026-07-28) 웹 조작 0.315 / 메일 0.471
# = 0.67로 이 선에 걸린다.
_WEAK_UNIT_RATIO = 0.7


# 제어 흐름 구조 액션 후보 — 카탈로그에 실재하는 것만 메뉴에 실린다. 요구사항 문장에는
# 이런 액션이 명시되지 않아 검색 질의가 생성되지 않으므로(0374 JIRA 봇 실측: Loop 이터레이터
# 부재 → Continue 오용, 세션 opener 부재 → 세션 생명주기 통누락) 결정론으로 보완한다.
# 카탈로그 표기 세대가 바뀔 때마다 이 목록이 깨지는 회귀가 반복됐다(과거 "ifPackageIfAction"
# MISS로 Else If 오용 — 정준환 실측 / 2026-07-18 재적재로 9개 중 8개 MISS 재발).
# ⚠ **이 목록은 폴백이다.** 위 주석이 예고한 "정본 어휘층"이 `app.agent.knowledge`로
# 만들어졌고, `derive_structural_actions(catalog)`가 카탈로그의 제어 흐름 패키지 액션을
# **전량** 열거한다. 수기 병기는 아무리 늘려도 다음 세대를 못 따라가고, 실측에서
# 실재 39개 중 29개를 놓치고 있었다(Loop 이터레이터 변형이 20여 개다).
# 유도가 빈 결과를 낼 때(카탈로그 순회 불가·테스트 스텁)만 이 목록이 쓰인다.
_STRUCTURAL_CANDIDATES: list[tuple[str, str]] = [
    # v2 문서 정본 표기 (khub identity 카탈로그, 2026-07-19)
    ("Loop", "Loop"),
    ("Loop", "Break"),
    ("Loop", "Continue"),
    ("If", "If"),
    ("If", "Else if (optional)"),
    ("If", "Else"),
    ("Error handler", "Try"),
    ("Error handler", "Catch"),
    ("Error handler", "Finally"),
    ("Error handler", "Throw"),
    ("Step", "Step"),
    # llm_agent 재파싱 표기 (RPA-141 시절 카탈로그 — 문서 슬러그 camelCase. 테스트 스텁
    # FakeCatalog가 이 세대를 사용하며, 재적재로 이 세대가 돌아올 수 있어 유지)
    ("Loop", "cloudUsingLoopAction"),
    ("Loop", "loopPackageBreakAction"),
    ("Loop", "loopPackageContinueAction"),
    ("If", "ifPackageElseIfOptionalAction"),
    ("If", "ifPackageElseAction"),
    ("Error handler", "errorHandlerTry"),
    ("Error handler", "errorHandlerCatch"),
    ("Error handler", "errorHandlerFinally"),
    ("Error handler", "errorHandlerThrow"),
    ("Step", "stepAction"),
    # 구 JAR 표기 (2026-07-18 재적재 후 네온 카탈로그 실측: ErrorHandler/try·catch,
    # Loop/loop.commands.*, If/if·elseIf·else, Step/step)
    ("Loop", "loop.commands.start"),
    ("Loop", "loop.commands.break"),
    ("Loop", "loop.commands.continue"),
    ("If", "if"),
    ("If", "elseIf"),
    ("If", "else"),
    ("ErrorHandler", "try"),
    ("ErrorHandler", "catch"),
    ("ErrorHandler", "finally"),
    ("ErrorHandler", "throw"),
    ("Step", "step"),
]


# 구조 보완에서 제외하는 컨테이너 패키지 (RPA-355).
#
# 트리거는 **흐름도 steps에 들어가지 않는다** — 별도 노드(`recommend_trigger`)가 추천하고
# 스키마의 `trigger` 칸에 담긴다. 그런데 이 패키지가 구조 보완으로 무조건 실려 액션 15개 ·
# 3,150자(메뉴의 26%)를 먹고 있었고, **저장된 흐름도 80건 중 0건에 쓰였다.** 게다가 그 액션들은
# 호스트·인증정보·폴링 주기를 받는 설정이라 제어 흐름 어휘도 아니다.
#
# 트리거 구동 업무라면 요구사항 질의가 검색으로 찾는다 — `action_schema` 행이므로 검색 대상이다.
# `CONTAINER_PACKAGES`는 그대로 둔다: 검수의 컨테이너 판정(빈 껍데기·children 규칙)이 쓰는
# 목록이라 여기 사정으로 건드리면 안 된다.
_COMPLEMENT_EXCLUDED_PACKAGES: frozenset[str] = frozenset({"Trigger loop"})

# 파라미터 타입이 이 값이면 그 액션은 **세션을 받아야** 동작한다 (derive.py와 같은 판정).
_SESSION_PARAM_TYPE = "SESSION"


def _needs_session(spec: dict | None) -> bool:
    """이 액션이 SESSION 파라미터를 요구하는가 — 특정 시스템에 묶였다는 신호.

    `type`이 문자열이 아닐 수 있다(Qodo). 카탈로그 스펙은 DB의 JSON 메타데이터에서 오고
    사용자 제공 카탈로그도 같은 통로를 쓰므로, 여기 들어오는 값은 사실상 외부 입력이다.
    `(p.get("type") or "").upper()`는 truthy 비문자열(예: 숫자)에서 AttributeError로 터지고,
    그러면 `structural_complement` 전체가 죽어 **Dossier 조립이 실패한다** — 판정 하나를
    못 해서 조사를 통째로 잃는 것은 균형이 안 맞는다. 문자열이 아니면 SESSION이 아니다.
    """
    for p in (spec or {}).get("parameters") or []:
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if isinstance(t, str) and t.strip().upper() == _SESSION_PARAM_TYPE:
            return True
    return False


def structural_complement(catalog, menu_packages: set[str]) -> list[tuple[str, str]]:
    """메뉴를 결정론으로 보완할 (package, action) 목록 — 검색 없이 카탈로그 직조회 (비용 0).

    ① 메뉴에 등장한 패키지의 세션 opener/closer (derive_session_registry 재사용) —
       업무 액션이 뽑혔는데 여닫기가 빠지는 연쇄(세션 생명주기 통누락)를 차단한다.
    ② 제어 흐름 구조 액션(Loop 이터레이터·If·Error handler·Step) — 요구사항 질의로는
       절대 검색되지 않지만 모든 흐름도에 필요한 어휘다. **카탈로그에서 전량 유도한다**
       (`derive_structural_actions`) — 수기 병기 목록은 표기 세대가 바뀔 때마다 깨졌고
       실측에서 실재 39개 중 29개를 놓치고 있었다. 유도가 비면 그 목록으로 폴백한다.
    카탈로그에 실재하는 것만 반환한다(폐쇄어휘 유지).

    ## 왜 ②에 예산 규율을 붙였나 (RPA-355)

    ①에는 관련성 필터가 있는데(`key[0] in menu_packages`) ②에는 없었다. 그래서 '전량 유도'가
    그대로 메뉴가 되어, 실측 메뉴 12,304자 중 **루프 변형 31개가 6,434자(52%)** 를 먹고 정작
    업무 액션은 18개 3,456자(28%)였다. 그 31개는 저장된 흐름도 80건에서 **한 번도 쓰이지
    않았다.**

    두 가지를 뺀다. 판별 기준은 **카탈로그 데이터에서** 나온다:

    - `Trigger loop` 패키지 전체 (`_COMPLEMENT_EXCLUDED_PACKAGES` 주석 참고)
    - **`SESSION`을 요구하는 이터레이터** — `For each mail in mail box` ·
      `For each channel in a team` 등 10개가 `Session name:SESSION`을 받는다. 그 세션을 열
      패키지가 메뉴에 없으면 **애초에 쓸 수 없다.** 이건 범용 제어 흐름이 아니라 특정 시스템에
      묶인 업무 액션이므로 검색 경로가 맡는다(그쪽은 이제 상한이 없다).

    남는 범용 제어 흐름: `Loop action for data iteration` · `For each row in table` ·
    `For each work item in queue` · `Break` · `Continue` · If 3종 · Error handler 4종 · Step.

    ⚠ 이터레이터 이름에서 담당 패키지를 추론해 "그 패키지가 메뉴에 있을 때만 싣는" 방식도
    검토했는데 매핑이 성립하지 않는다(`For each mail in mail box` ↔ `Microsoft 365 Outlook`은
    이름에 공통 토큰이 없다). 추측으로 필터를 만들면 조용히 틀리므로 안 한다.
    """
    openers, closers = derive_session_registry(catalog)
    candidates: list[tuple[str, str]] = [
        key for key in sorted(openers | closers) if key[0] in menu_packages
    ]
    derived = derive_structural_actions(catalog)
    candidates += list(derived) if derived else _STRUCTURAL_CANDIDATES
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    dropped: list[str] = []
    for pkg, act in candidates:
        if (pkg, act) in seen:
            continue
        seen.add((pkg, act))
        spec = catalog.get_action_schema(pkg, act)
        if spec is None:
            continue
        # 세션 여닫기(①)는 그대로 싣는다 — 세션 인자를 받는 것이 당연하고, 이미 메뉴 패키지로
        # 좁혀져 있다. 예산 규율은 '전량 유도'로 들어온 것(②)에만 적용한다.
        if (pkg, act) not in openers and (pkg, act) not in closers:
            if pkg in _COMPLEMENT_EXCLUDED_PACKAGES or _needs_session(spec):
                dropped.append(f"{pkg}/{act}")
                continue
        out.append((pkg, act))
    if dropped:
        # 조용히 자르지 않는다 — 무엇이 메뉴에서 빠졌는지 남긴다(RPA-355 실측 절차가 이걸 본다).
        logger.info("구조 보완에서 제외 %d개 (트리거·세션 전용 이터레이터): %s",
                    len(dropped), ", ".join(dropped[:12]))
    return out


class _ResearchUnit(BaseModel):
    topic: str = ""
    packages: list[str] = Field(default_factory=list)  # 이 기능을 풀 A360 패키지 (질의 앵커)
    ko_query: str = ""
    en_query: str = ""


class _ResearchPlan(BaseModel):
    units: list[_ResearchUnit] = Field(default_factory=list)


def _split_by_priority(spec: dict) -> tuple[list[dict], list[dict]]:
    """요구를 (must, should)로 가른다. priority 미기재는 must로 본다(spec_builder 기본값)."""
    musts: list[dict] = []
    shoulds: list[dict] = []
    for r in spec.get("requirements") or []:
        (shoulds if r.get("priority") == "should" else musts).append(r)
    return musts, shoulds


_MAX_QUERY_PACKAGES = 2  # 질의 앞에 붙일 패키지 수 상한 — 더 붙이면 동작 어휘가 묻힌다


def _package_vocabulary(catalog) -> tuple[str, set[str]]:
    """(프롬프트에 실을 패키지 목록 문자열, 실재 패키지 이름 집합).

    카탈로그에서 유도한다 — 실측 120개, 약 500토큰이라 LLM 호출 증가 없이 실을 수 있다.
    """
    try:
        pkgs = derive_packages(catalog) if catalog is not None else ()
    except Exception as e:  # noqa: BLE001 — 어휘 사전이 없어도 조사는 굴러가야 한다
        logger.warning("패키지 어휘 유도 실패 — 사전 없이 질의 설계: %s", e)
        return "", set()
    if not pkgs:
        return "", set()
    return ", ".join(f"{p}({n})" for p, n in pkgs), {p for p, _ in pkgs}


def _with_packages(query: str, packages: list[str], known: set[str]) -> str:
    """질의 앞에 카탈로그 패키지명을 붙인다 — 어휘 검색(BM25)이 액션을 맞히게 하는 앵커다.

    LLM이 고른 것 중 **카탈로그에 실재하는 이름만** 쓴다(환각 앵커가 검색을 오염시키지
    않게). 코드가 붙이는 이유: 프롬프트로 부탁만 하면 빠뜨리는 질의가 생기는데, 그 질의는
    조용히 점수가 반토막 난다(실측 0.31~0.49 대 0.66~0.91).
    """
    picked = [p for p in packages if p in known][:_MAX_QUERY_PACKAGES]
    return f"{' '.join(picked)} {query}".strip() if picked else query


def _expand_queries(spec: dict, package_list: str = "") -> list[_ResearchUnit]:
    """FlowSpec 요구를 기능 단위로 묶어 (한국어, 영어) 질의 쌍을 만든다 (LLM 1회, 경량).

    must(업무)와 should(운영 골격)를 프롬프트에서 갈라 보여 준다 — 섞어 주면 계획자가
    골격 요구로 단위를 채워 업무 기능이 조사에서 빠진다(그러면 그 기능은 흐름도에 아예
    못 들어간다). 강등 경로도 같은 순서를 지킨다.
    """
    musts, shoulds = _split_by_priority(spec)

    def _lines(reqs: list[dict]) -> str:
        return "\n".join(f"- [{r.get('req_id')}] {r.get('text', '')}" for r in reqs) or "(없음)"

    try:
        plan = chat_json(
            [
                {"role": "system", "content": _PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"[목표]\n{spec.get('goal', '')}\n\n"
                        f"[필수 요구(must) — 먼저 빠짐없이 덮을 것]\n{_lines(musts)}\n\n"
                        f"[운영 골격 요구(should) — 남는 자리에 크게 묶을 것]\n{_lines(shoulds)}"
                        + (
                            f"\n\n[카탈로그 패키지 — 이름(액션 수). 여기 있는 이름만 packages에 쓸 것]\n"
                            f"{package_list}"
                            if package_list
                            else ""
                        )
                    ),
                },
            ],
            purpose="recommend",
            model_cls=_ResearchPlan,
        )
        units = [u for u in plan.units if u.ko_query or u.en_query][:_MAX_UNITS]
        if units:
            return units
    except (ValueError, RuntimeError) as e:
        logger.warning("research 질의 확장 실패 — 요구 원문 질의로 강등: %s", e)
    # 강등: 요구 텍스트를 그대로 한국어 질의로 (영어 질의 없음 — 다국어 임베딩에 맡긴다).
    # must를 앞에 둬 상한에 걸려도 업무 요구가 먼저 살아남게 한다.
    return [
        _ResearchUnit(topic=r.get("req_id") or "", ko_query=r.get("text") or "")
        for r in (musts + shoulds)[:_MAX_UNITS]
    ]


def _interleave(
    unit_ranked: list[list[tuple[tuple[str, str], float]]], limit: int | None = None
) -> list[tuple[tuple[str, str], float]]:
    """단위별 상위부터 라운드로빈으로 뽑는다 — 단위 사이에서는 점수를 비교하지 않는다.

    모든 단위가 1위를 먼저 내고, 그다음 2위를 낸다. 임계값도 배분 비율도 없어서 단위 수가
    몇이든 자연히 공평해지고, 후보가 적은 단위는 알아서 빠진다. `limit=None`이면 상한 없음.

    ## 중복은 그 단위의 차례를 소모하지 않는다 (RPA-355)

    앞서는 단위의 그 깊이 후보가 이미 뽑힌 것이면 `continue`로 넘어갔다 — **다음 순위로
    내려가지 않고 그 차례를 통째로 잃었다.** 그래서 상위 후보가 다른 단위와 겹치는 단위가
    굶었다: 실측(2026-07-30) 「국내 금 클릭」 단위는 후보 9개를 갖고도 1개만 올렸다(1위
    `Browser/Open`은 「웹 열기」가, 2위 `Mouse/Click`은 「증권 버튼 클릭」이 먼저 가져가
    깊이 0·1을 둘 다 잃었다). 클릭 단위 둘이 합쳐 3개만 올린 원인이 이것이다.

    이제 단위마다 커서를 들고, 이미 뽑힌 것은 **건너뛰며 자기 차례에 하나를 채운다.**
    """
    out: list[tuple[tuple[str, str], float]] = []
    seen: set[tuple[str, str]] = set()
    cursor = [0] * len(unit_ranked)          # 단위별로 어디까지 봤나
    while limit is None or len(out) < limit:
        progressed = False
        for ui, ranked in enumerate(unit_ranked):
            i = cursor[ui]
            while i < len(ranked) and ranked[i][0] in seen:
                i += 1                        # 중복은 넘기고 계속 본다 — 차례를 잃지 않는다
            cursor[ui] = i
            if i >= len(ranked):
                continue                      # 이 단위는 후보를 다 썼다
            key, score = ranked[i]
            cursor[ui] = i + 1
            seen.add(key)
            out.append((key, score))
            progressed = True
            if limit is not None and len(out) >= limit:
                return out
        if not progressed:                    # 모든 단위가 소진 — 더 뽑을 것이 없다
            break
    return out


def _competing_note(catalog, packages: set[str]) -> str:
    """메뉴에 같은 일을 하는 패키지가 여럿 실렸을 때 붙이는 안내.

    **후보에서 빼지 않는다 — 다 주되 하나만 고르라고 알린다.** 어느 쪽이 맞는지는 업무와
    실행 환경이 정하지 검색 점수가 정하지 않는다: 실측(2026-07-28)에서 엑셀 서식 액션은
    `Microsoft 365 Excel`에만 있고 `Excel advanced`에는 아예 없어서, 점수로 한쪽을 접었으면
    테두리 설정이 영영 불가능해졌을 것이다.

    지금까지 메뉴가 평평한 목록이라 모델이 경쟁 관계를 볼 방법이 없었고, 흐름도 18개 중
    6개가 엑셀 패키지를 섞었다(한 개는 3종을 함께 썼다).
    """
    try:
        groups = derive_competing_packages(catalog) if catalog is not None else ()
    except Exception as e:  # noqa: BLE001 — 안내가 없어도 메뉴는 나가야 한다
        logger.warning("경쟁 패키지 유도 실패 — 안내 생략: %s", e)
        return ""
    lines = [
        "- " + " · ".join(sorted(g & packages))
        for g in groups
        if len(g & packages) > 1
    ]
    if not lines:
        return ""
    return (
        "\n[⚠ 같은 일을 하는 경쟁 패키지 — 각 줄에서 **하나만** 골라 흐름도 전체에서 일관되게 "
        "쓸 것. 패키지마다 세션이 따로라 섞으면 세션이 이어지지 않아 실행이 깨진다. "
        "필요한 액션이 한쪽에만 있으면 그 패키지로 통일한다]\n" + "\n".join(lines)
    )


def _discouraged_note(catalog, packages: set[str]) -> str:
    """메뉴에 **신규 개발 비권장** 패키지가 실렸을 때 붙이는 안내.

    경쟁 패키지 안내와 같은 원칙이다 — 후보에서 빼지 않고, 왜 나중 순위인지를 알린다.
    빼 버리면 그 패키지에만 있는 액션이 필요한 경우(마이그레이션 봇 유지보수 등)에
    자동화가 통째로 막힌다.

    비권장 여부는 카탈로그가 패키지 개요 문서에서 유도한다(`discouraged_packages`) —
    패키지 이름을 코드나 프롬프트에 박지 않는다.
    """
    lookup = getattr(catalog, "discouraged_packages", None)
    if not callable(lookup):
        return ""
    try:
        flagged = {p: r for p, r in lookup().items() if p in packages}
    except Exception as e:  # noqa: BLE001 — 안내가 없어도 메뉴는 나가야 한다
        logger.warning("비권장 패키지 유도 실패 — 안내 생략: %s", e)
        return ""
    if not flagged:
        return ""
    lines = [f"- {pkg} — {reason}" for pkg, reason in sorted(flagged.items())]
    return (
        "\n[⚠ 신규 개발 비권장 패키지 — 공식 문서가 신규 봇 개발에 권장하지 않는다고 명시한 "
        "패키지다. 같은 일을 하는 다른 패키지의 액션이 메뉴에 있으면 **그쪽을 쓴다.** "
        "여기에만 있는 액션이라 대안이 없을 때만 쓰고, 그 이유를 rationale에 남긴다]\n"
        + "\n".join(lines)
    )


def _menu_block(pkg: str, act: str, spec_dict: dict) -> str:
    params = ", ".join(
        f"{p['name']}({p.get('type')}{', 필수' if p.get('required') else ''})"
        for p in spec_dict.get("parameters", [])
    )
    rt = spec_dict.get("return_type")
    # 스펙 미상(params_unknown 행)을 '없음'으로 표기하면 파라미터가 정말 없는 액션과
    # 구분이 안 돼 composer가 스펙 확인을 건너뛴다 — '미상'으로 구분 표기한다.
    unknown = spec_dict.get("parameters") is None
    return (
        f"- {pkg}/{act} «{spec_dict.get('label') or act}»"
        + (f" → 리턴 {rt}" if rt else "")
        + f"\n    파라미터: {params or ('미상 — get_action_schema로 확인' if unknown else '없음')}"
    )


def _whole_catalog_dossier(ctx) -> dict:
    """검색기가 없는 경로(사용자 제공 카탈로그)의 Dossier — 전량이 곧 메뉴다 (RPA-285).

    어휘가 수십 개 규모라 검색으로 좁힐 이유가 없고, 좁히면 오히려 사용자가 준 액션이
    메뉴에서 누락돼 composer가 "카탈로그에 없다"고 오판한다.

    다만 무제한은 아니다 — 사용자가 수천 개짜리 카탈로그를 붙여넣으면 시스템 프롬프트가
    통째로 부풀어 지연·비용이 폭증하고 컨텍스트 한도에 걸린다(Qodo 리뷰). 안전 상한을 두되
    **잘렸다는 사실을 조용히 넘기지 않는다**: 잘린 액션은 composer가 영영 못 쓰므로,
    사용자가 그 사실을 알아야 카탈로그를 추려 다시 줄 수 있다.

    카탈로그 전체를 한 번 훑고 슬라이스한다. 상한 뒤로 순회를 끊으면 몇 개가 잘렸는지 셀 수
    없어 "조용히 자르지 않는다"는 목적이 깨진다 — 그리고 이 카탈로그는 LLM 구조화 출력에서
    나와 출력 토큰 한도가 곧 크기 상한이라(현실적으로 수백 개) 순회 비용은 같은 턴의 LLM
    호출보다 몇 자릿수 아래다. 비싼 쪽(_menu_block 문자열 조립)만 상한 안에서 돈다.
    """
    rows = [
        (s.get("package"), s.get("action"), s)
        for s in ctx.catalog.iter_action_schemas()
        if s.get("package") and s.get("action")
    ]
    selected = rows[:_MAX_USER_MENU_ACTIONS]
    actions = [(pkg, act) for pkg, act, _ in selected]
    blocks = [_menu_block(pkg, act, spec_dict) for pkg, act, spec_dict in selected]

    total = len(rows)
    dropped = total - len(actions)
    if dropped:
        logger.warning(
            "사용자 카탈로그가 상한을 초과 — %d개 중 %d개만 메뉴에 실었다(나머지 %d개는 사용 불가)",
            total, len(actions), dropped,
        )
        blocks.append(
            f"\n[주의] 제공된 카탈로그가 커서 앞의 {len(actions)}개만 실었다. "
            f"{dropped}개는 이번 설계에 쓸 수 없으니, 필요한 액션이 빠졌다면 answer에서 알려라."
        )
    message = f"제공된 카탈로그 {len(actions)}개 액션을 후보로 사용"
    if dropped:
        message += f" (상한 초과로 {dropped}개 제외)"
    emit({"event": "stage", "stage": "searching", "message": message})
    return {
        "menu": "\n".join(blocks) or "(제공된 액션 없음)",
        "actions": actions,
        "background": "",
        "dropped": dropped,
    }


async def build_dossier(spec: dict, sink: list[dict], ctx) -> dict:
    """Capability Dossier를 만든다: {menu: str, actions: [(pkg, act)], background: str}.

    - 기능 단위별 이중 질의 병렬 검색(action_schema/bot_example) → (pkg, act) 후보 집계
    - 상위 후보의 카탈로그 스펙 프리페치 → 파라미터까지 담긴 액션 메뉴 텍스트
    - 배경 지식: 목표 문장으로 doc_page 1회 검색 (전체 문서 적재 가정 — 없으면 빈 결과)

    ctx(CatalogContext)가 어휘 출처를 나른다 — 검색기가 없으면 카탈로그 전량을 메뉴로
    쓴다(사용자 제공 카탈로그 경로).
    """
    if not ctx.searchable:
        return _whole_catalog_dossier(ctx)

    retriever = ctx.retriever
    catalog = ctx.catalog
    package_list, known_packages = _package_vocabulary(catalog)
    units = _expand_queries(spec, package_list)

    queries: list[str] = []
    query_unit: list[int] = []  # queries[i]가 어느 기능 단위에서 나왔는지 (집계에 필요)
    for idx, u in enumerate(units):
        for q in (u.ko_query.strip(), u.en_query.strip()):
            if q:
                queries.append(_with_packages(q, u.packages, known_packages))
                query_unit.append(idx)
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

    # (pkg, act)별 최고 점수를 **기능 단위 안에서만** 집계한다.
    #
    # 재정렬 점수는 (질의, 문서) 쌍의 관련도라 **질의가 다르면 비교 대상이 아니다** — 어려운
    # 질의는 모든 후보가 낮게 나온다. 전역 정렬로 자르면 그 단위가 통째로 밀린다:
    # 실측(2026-07-28) 웹 조작 단위 최고점 0.315 < 엑셀 단위 최저점 0.35라, 웹 조작 후보
    # 10건 중 3건만 메뉴에 남고 잘린 7건에 `Browser/Open`이 들어 있었다(네이버에 접속하는
    # 액션이 사라진 것). 단위 안에서만 점수를 비교하고 단위 사이는 라운드로빈으로 나눈다.
    per_unit: list[dict[tuple[str, str], float]] = [{} for _ in units]
    for qi, hits in enumerate(results):
        sink.extend(hits)
        bucket = per_unit[query_unit[qi]]
        for h in hits:
            pkg, act = h.get("package_name"), h.get("action_name")
            if pkg and act:
                key = (pkg, act)
                bucket[key] = max(bucket.get(key, 0.0), h.get("score") or 0.0)

    unit_ranked = [sorted(b.items(), key=lambda kv: kv[1], reverse=True) for b in per_unit]
    ranked = _interleave(unit_ranked, _MAX_MENU_ACTIONS)

    # 검색이 약한 단위 감지 — 지금은 **기록만** 한다(후보를 버리지 않는다).
    # 절대 임계는 오늘 실측에 과적합될 뿐이라 상대 기준을 쓰고, 임계가 실제로 갈리는지
    # 몇 턴 관측한 뒤에 '버리기·재질의' 같은 행동을 붙인다.
    unit_top = [(units[i].topic or f"단위{i + 1}", (r[0][1] if r else 0.0), len(r))
                for i, r in enumerate(unit_ranked)]
    for topic, top_score, n in unit_top:
        logger.info("조사 단위 '%s' — 후보 %d건, 최고 점수 %.3f", topic, n, top_score)
    peak = max((s for _, s, _ in unit_top), default=0.0)
    weak = [t for t, s, _ in unit_top if peak > 0 and s < peak * _WEAK_UNIT_RATIO]
    if weak:
        logger.warning(
            "검색이 약한 기능 단위: %s — 최고 점수가 상위 단위의 %.0f%% 미만이다. "
            "그 기능의 액션이 메뉴에 부실하게 실렸을 수 있다(질의가 업무 문장에 가까울수록 그렇다).",
            ", ".join(weak), _WEAK_UNIT_RATIO * 100,
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

    menu_packages = {p for p, _ in menu_actions}
    rival = _competing_note(catalog, menu_packages)
    if rival:
        blocks.append(rival)
    stale = _discouraged_note(catalog, menu_packages)
    if stale:
        blocks.append(stale)

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
