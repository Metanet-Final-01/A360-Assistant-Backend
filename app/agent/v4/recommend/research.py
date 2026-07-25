"""research — 선행 KB 조사로 Capability Dossier를 만든다 (v3 설계 §2-[2]).

v2는 compose ReAct가 후보 1개를 만들며 순차 도구 왕복(≤6)을 했다. v3는 후보가 2~3개라
그 비용이 배가되므로 조사를 선행·공유한다: 후보들이 같은 근거 위에서 경쟁해 심판이
공정해지고, 후보별 compose는 escape hatch 툴콜 2회의 준-단일 호출로 가벼워진다.

한/영 이중 질의(dual-query): KB는 한 행에 영어 식별자+한국어 본문이 혼재하고 검색에
언어 처리가 없다 — 기능 단위마다 (한국어 자연어, 영어 액션 어휘) 질의 쌍을 만들어 둘 다
검색한다. 한국어는 본문(의미), 영어는 식별자(어휘)를 맞혀 상호 보완한다.

검색 히트는 run 단위 sink에 누적된다 — finalize의 sources/confidence 부착 계약 유지.

v4는 조사 앞에 **조작 단위 확정**을 둔다(설계 Phase 3). "이 요구를 하려면 몇 번의 조작이
필요한가"는 액션 어휘를 몰라도 답할 수 있는 질문이라 compose에서 떼어낼 수 있고, 떼어내면
compose가 동시에 지는 다섯 과업(분해·선택·파라미터·구조·스키마) 중 하나가 빠진다.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..orchestrator.jsonio import chat_json
from app.agent.knowledge import channels
from app.agent.knowledge.derive import derive_structural_actions
from app.agent.knowledge.examples import (
    render_examples_block,
    select_examples,
    selection_trace,
)

from ..verify.checker import derive_session_registry
from .stream import emit

logger = logging.getLogger(__name__)

_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "research_queries.md").read_text(encoding="utf-8")
_OPS_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "operation_units.md").read_text(encoding="utf-8")

# 검색 채널은 `app.agent.knowledge.channels` 하나에서만 정의된다 (RPA-298).
# v3까지는 같은 목록이 이 파일(`ACTION_SOURCE_TYPES`)과 `recommend/graph.py`
# (`SEARCH_SOURCE_TYPES`)에 **따로** 있어 한쪽만 고치면 dossier와 compose 툴이 서로 다른
# 채널을 보게 됐다(에러 없이 품질만 갈리는 어긋남). 그 상수는 v4에서 폐기한다.
_MAX_UNITS = 8           # 기능 단위 상한 — 질의 폭주 방지
_MAX_MENU_ACTIONS = channels.ACTION.quota  # Dossier 액션 메뉴 상한 (스펙 포함이라 토큰 비용이 큼)
# 사용자 제공 카탈로그의 메뉴 상한 — 검색으로 좁힐 수 없어 전량을 싣지만, 프롬프트가
# 무한정 커지는 것은 막는다. 검색 경로보다 훨씬 넉넉하다(실제 카탈로그는 보통 수십 개라
# 이 값에 닿지 않고, 닿으면 잘린 사실을 프롬프트·로그·진행 메시지 셋 다에 남긴다).
_MAX_USER_MENU_ACTIONS = 200
# 파라미터 채널에 문서를 찾아 줄 액션 수 — 메뉴 상위 N개만. 전량(14개)에 던지면 검색
# 팬아웃이 두 배가 되는데, 문서가 실제로 필요한 것은 composer가 파라미터를 채워야 하는
# 상위 액션들이다. 나머지는 escape hatch 툴콜로 필요할 때 각자 찾는다.
_PARAM_DOC_ACTIONS = 6
# 업무 분해 채널에 던질 요구 문장 수 — 목표 1건 + 요구 상위 N건.
_DECOMPOSE_QUERIES = 3
# 조작 단위 상한 — 봇 수준 완성도(정답 봇 70액션)를 노리므로 요구 수의 3~5배를 담을 만큼
# 넉넉해야 한다. 다만 여기가 터지면 compose 프롬프트가 통째로 조작 목록에 잠식된다.
_MAX_OPERATIONS = 40
# 조작 단위 블록을 액션 메뉴 앞에 함께 실을지.
# compose 프롬프트를 조립하는 곳은 graph.py인데 그 파일은 2상 구조 작업이 잡고 있다.
# 그래서 dossier["operations"](정식 키)로 내보내면서, 확실히 주입되는 menu 앞에도 같은
# 블록을 붙여 오늘 당장 효력을 갖게 한다. graph가 operations를 자기 섹션으로 주입하게 되면
# **이 값을 False로 내려** 같은 블록이 두 번 실리는 것을 막을 것.
_INLINE_OPERATIONS_IN_MENU = True
# 프롬프트에 실을 용례 수 — 2건이면 조합 패턴을 보여주기 충분하다. 늘리면 컨텍스트를
# 잡아먹으면서 모델이 예제를 그대로 베끼는 쪽으로 기운다.
_MAX_EXAMPLES = 2
# 배경 지식 발췌 길이(문자) — 액션당 문서 조각이 여러 개 실리므로 v3의 200자를 유지한다.
_BG_SNIPPET = 200


# 제어 흐름 구조 액션 폴백 — 요구사항 문장에는 이런 액션이 명시되지 않아 검색 질의가
# 생성되지 않으므로(0374 JIRA 봇 실측: Loop 이터레이터 부재 → Continue 오용, 세션 opener
# 부재 → 세션 생명주기 통누락) 결정론으로 보완한다.
#
# **이 병기 목록은 이제 폴백이다** (RPA-298). 카탈로그 표기 세대가 바뀔 때마다 목록이
# 깨지는 회귀가 반복돼 알려진 세대를 전부 병기했는데, 실측 결과 32쌍 중 **10쌍만 실재**했고
# 더 나쁘게는 **실재하는 제어 흐름 액션 39개 중 29개를 놓치고** 있었다 — Loop 이터레이터
# 전부(For each row in table, For each mail in mail box, Loop action for data iteration …)가
# 메뉴에서 빠져 에이전트가 반복문을 만들 어휘 자체를 못 봤다.
#
# 이제 knowledge.derive_structural_actions가 카탈로그의 제어 흐름 패키지 액션을 전량
# 열거한다. 유도가 빈 결과를 내는 환경(순회 미지원 스텁 등)에서만 아래 목록이 쓰인다.
_STRUCTURAL_FALLBACK: list[tuple[str, str]] = [
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


def structural_complement(catalog, menu_packages: set[str]) -> list[tuple[str, str]]:
    """메뉴를 결정론으로 보완할 (package, action) 목록 — 검색 없이 카탈로그 직조회 (비용 0).

    ① 메뉴에 등장한 패키지의 세션 opener/closer (derive_session_registry 재사용) —
       업무 액션이 뽑혔는데 여닫기가 빠지는 연쇄(세션 생명주기 통누락)를 차단한다.
    ② 제어 흐름 구조 액션(Loop 이터레이터·If·Error handler·Step) — 요구사항 질의로는
       절대 검색되지 않지만 모든 흐름도에 필요한 어휘다. 카탈로그에서 전량 유도한다
       (RPA-298: 수기 병기 목록은 실재 39개 중 29개를 놓치고 있었다).
    카탈로그에 실재하는 것만 반환한다(폐쇄어휘 유지).
    """
    _reg = derive_session_registry(catalog)
    openers, closers = _reg.openers, _reg.closers
    candidates: list[tuple[str, str]] = [
        key for key in sorted(openers | closers) if key[0] in menu_packages
    ]
    derived = derive_structural_actions(catalog)
    candidates += list(derived) if derived else _STRUCTURAL_FALLBACK
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


class _OperationUnit(BaseModel):
    """조작 단위 한 건 — 액션 어휘가 아니라 '몇 번의 조작인가'만 담는다.

    req_ids가 느슨한 타입인 이유: 모델이 리스트 대신 문자열 하나를 내는 슬립이 흔한데,
    그 한 건 때문에 분해 전체를 잃으면 손해가 크다(정규화가 흡수한다).
    """

    op_id: str = ""
    intent: str = ""
    req_ids: Any = None
    repeat: bool = False


class _OperationPlan(BaseModel):
    operations: list[_OperationUnit] = Field(default_factory=list)


def normalize_operations(units: list, spec: dict) -> list[dict]:
    """조작 단위 초안을 결정론으로 정리한다 — 번호 재부여·중복 제거·요구 id 대조·상한.

    - **번호 재부여**: 뒤 단계(compose·역방향 감사)가 op-N으로 조작을 지목하는데, LLM이 낸
      번호는 건너뛰거나 중복된다. 위치 순서대로 다시 매겨 앵커를 신뢰 가능하게 만든다.
    - **미실재 req_id 제거**: 환각 앵커가 섞이면 "어느 요구가 어느 조작으로 실현됐나"의
      역매핑이 조용히 틀린다. spec에 있는 id만 남긴다.
    - **중복 제거**: 같은 조작이 두 번 세어지면 조작 수 자체가 신호가 되지 못한다.
    """
    def field(u, name):
        # 모델(초안)로도 dict(상태 왕복분)로도 들어올 수 있다 — 둘 다 받는다.
        return u.get(name) if isinstance(u, dict) else getattr(u, name, None)

    known = {r.get("req_id") for r in spec.get("requirements") or [] if r.get("req_id")}
    out: list[dict] = []
    seen: set[str] = set()
    for u in units or []:
        intent = " ".join(str(field(u, "intent") or "").split())
        key = intent.lower()
        if not intent or key in seen:
            continue
        seen.add(key)
        raw_ids = field(u, "req_ids")
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        req_ids = [r for r in (raw_ids or []) if isinstance(r, str) and r in known]
        out.append({
            "op_id": f"op-{len(out) + 1}",
            "intent": intent,
            "req_ids": req_ids,
            "repeat": bool(field(u, "repeat")),
        })
        if len(out) >= _MAX_OPERATIONS:
            break
    return out


def render_operations_block(operations: list[dict]) -> str:
    """조작 단위를 프롬프트용 한 덩어리 텍스트로 (결정론 — LLM 없음)."""
    lines = []
    for op in operations:
        tail = f" ← {', '.join(op['req_ids'])}" if op.get("req_ids") else ""
        tail += " (반복 안)" if op.get("repeat") else ""
        lines.append(f"- [{op['op_id']}] {op['intent']}{tail}")
    return "\n".join(lines)


def plan_operations(spec: dict, packages: str = "") -> list[dict]:
    """액션을 고르기 **전에** 조작 단위만 확정한다 (LLM 1회 — 설계 Phase 3).

    왜 별도 호출인가: 지금 compose 호출 하나가 mini 모델에게 업무 분해 + 액션 선택 +
    파라미터 + 구조 + 스키마 준수를 동시에 요구한다. 다섯 과업을 한 컨텍스트에 얹으면 전부
    중간 품질이 된다(설계 §3.1 원인 B). 분해는 어휘를 몰라도 답할 수 있는 질문이라 떼어낼
    수 있고, 떼어내면 compose는 '확정된 단위'를 어휘로 옮기는 일만 한다.

    `packages`는 **업무 분해 채널**(package_overview)이 실어 주는 패키지 지형이다 —
    액션 어휘가 아니라 "이 업무가 어떤 제품·시스템 영역에 걸치나"만 알려준다. 분해 단계에
    액션 스펙을 주면 모델이 어휘를 먼저 고르고 분해를 거기 맞추는데(단계 격리가 무너진다),
    개요만 주면 "메일함·엑셀·웹 세 영역이구나" 수준의 힌트로만 쓴다.

    실패는 강등이다 — 조작 단위 없이도 파이프라인은 예전 그대로 돈다.
    """
    reqs = spec.get("requirements") or []
    goal = (spec.get("goal") or "").strip()
    if not reqs and not goal:
        return []
    req_lines = "\n".join(
        f"- [{r.get('req_id')}] ({r.get('priority', 'must')}) {r.get('text', '')}" for r in reqs
    )
    pkg_block = (
        f"\n\n[관련 있어 보이는 패키지 — 참고용. 액션 어휘가 아니라 영역 힌트다]\n{packages}"
        if packages else ""
    )
    try:
        plan = chat_json(
            [
                {"role": "system", "content": _OPS_PROMPT},
                {"role": "user", "content": f"[목표]\n{goal}\n\n[요구사항]\n{req_lines}{pkg_block}"},
            ],
            purpose="recommend",
            model_cls=_OperationPlan,
        )
    except (ValueError, RuntimeError) as e:
        logger.warning("조작 단위 확정 실패 — 분해 없이 진행: %s", e)
        return []
    return normalize_operations(plan.operations, spec)


def _expand_queries(spec: dict, operations: list[dict] | None = None) -> list[_ResearchUnit]:
    """FlowSpec 요구를 기능 단위로 묶어 (한국어, 영어) 질의 쌍을 만든다 (LLM 1회, 경량).

    확정된 조작 단위가 있으면 함께 준다 — 요구 문장("일별 시세를 정리한다")보다 조작
    ("날짜 문자열을 만든다", "행 목록을 순회한다")이 검색어에 가깝다. 분해를 앞세운 값이
    질의 품질로도 돌아오는 지점.
    """
    req_lines = "\n".join(
        f"- [{r.get('req_id')}] {r.get('text', '')}" for r in spec.get("requirements") or []
    )
    ops_block = (
        f"\n\n[확정된 조작 단위 — 이 조작들을 수행할 어휘를 찾아야 한다]\n"
        f"{render_operations_block(operations)}"
        if operations else ""
    )
    try:
        plan = chat_json(
            [
                {"role": "system", "content": _PROMPT},
                {"role": "user",
                 "content": f"[목표]\n{spec.get('goal', '')}\n\n[요구사항]\n{req_lines}{ops_block}"},
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
    메뉴에서 누락돼 composer가 "카탈로그에 없다"고 오판한다. 그래서 검색 경로의 상한
    (_MAX_MENU_ACTIONS=14)은 여기 적용하지 않는다.

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
        # 용례는 A360 공식 문서 유래라 타 솔루션 카탈로그 경로에는 싣지 않는다 —
        # 다른 제품의 흐름에 A360 액션 조합을 보여주면 폐쇄 어휘를 깨는 유도가 된다.
        "examples": "",
        "example_ids": [],
        # 조작 단위도 이 경로에서는 만들지 않는다(제약 #24·#25: 타 솔루션은 측정 대상이 아니고
        # 파이프라인만 공유한다). 키는 항상 있어야 소비자가 get 없이 읽어도 안 깨진다.
        "operations": "",
        "operation_units": [],
        # 업무 분해 채널도 이 경로에는 없다(A360 KB 전용). 키 계약만 지킨다.
        "packages": "",
    }


def _decompose_queries(spec: dict) -> list[str]:
    """업무 분해 채널에 던질 질의 — 목표 + 요구 상위 몇 건 (LLM 없음, 질의 확장 **이전**).

    분해 단계는 아직 조작 단위도 액션 어휘도 없다. 그래서 확장된 질의를 못 쓰고 원문을
    그대로 쓴다 — 어차피 이 채널이 답할 질문은 "어떤 제품 영역인가"라 원문으로 충분하다.
    """
    out = [(spec.get("goal") or "").strip()]
    out += [
        (r.get("text") or "").strip()
        for r in (spec.get("requirements") or [])[:_DECOMPOSE_QUERIES]
    ]
    return [q for q in out if q]


def _one_line(text: str, limit: int) -> str:
    """검색 본문을 '- 항목' 한 줄에 넣을 수 있게 공백을 접는다.

    KB 본문에는 줄바꿈이 흔한데(실측: doc_page 발췌 200자에 개행 3~5개) 그대로 넣으면
    한 항목이 여러 줄로 흩어져 프롬프트의 목록 구조가 무너진다 — 모델이 어디까지가 한
    문서인지 못 읽는다.
    """
    return " ".join((text or "").split())[:limit]


def _packages_block(hits: list[dict]) -> str:
    """패키지 개요 히트를 분해 프롬프트용 한 줄씩으로 (액션 어휘는 싣지 않는다)."""
    lines: list[str] = []
    seen: set[str] = set()
    for h in hits:
        name = h.get("package_name") or h.get("title") or ""
        if not name or name in seen:
            continue
        seen.add(name)
        lines.append(f"- {name}: {_one_line(h.get('content') or '', 120)}")
    return "\n".join(lines)


def _param_doc_queries(catalog, menu_actions: list[tuple[str, str]], goal: str) -> list[str]:
    """파라미터 채널 질의 — 고른 액션의 **표기**로 그 액션 문서를 집는다.

    `doc_page` 행은 package_name·action_name이 비어 있어(실측: 16,164행 전부) 필터로
    액션을 지목할 수 없다. 대신 표기+라벨을 질의어에 넣으면 문서 제목("Get multiple cells
    action / 여러 셀 가져오기 작업")과 정면으로 맞는다 — 실측 3개 액션 전부 1위가 정확히
    그 액션 문서였다.

    목표 문장 질의를 **맨 앞에 유지**하는 이유: v3까지 `background`를 채우던 유일한 통로가
    그것이고, 액션이 하나도 안 뽑힌 턴에도 배경이 비지 않아야 한다(조용한 회귀 방지).
    """
    queries = [goal.strip()] if (goal or "").strip() else []
    for pkg, act in menu_actions[:_PARAM_DOC_ACTIONS]:
        spec_dict = catalog.get_action_schema(pkg, act) or {}
        label = spec_dict.get("label") or ""
        queries.append(f"{pkg} {act} {label} 파라미터".replace("  ", " ").strip())
    return queries


async def build_dossier(spec: dict, sink: list[dict], ctx) -> dict:
    """Capability Dossier를 만든다: {menu, actions, background, examples, operations, …}.

    - **업무 분해 채널**(package_overview) → 패키지 지형만 뽑아 조작 단위 확정에 실어 준다
    - **조작 단위 확정**(plan_operations) — 액션 선택 전에 "몇 번의 조작인가"만 먼저 정한다
    - **액션 채널**(action_schema) — 기능 단위별 이중 질의 병렬 검색 → 순위 병합으로 후보 집계
    - 상위 후보의 카탈로그 스펙 프리페치 → 파라미터까지 담긴 액션 메뉴 텍스트
    - **파라미터 채널**(doc_page) — 고른 액션의 표기로 그 액션 문서를 집어 `background`에
    - 구조는 **검색 없음** — `structural_complement`가 카탈로그에서 결정론으로 유도한다

    단계마다 채널이 다른 이유는 `knowledge/channels.py` 참조 — 요약하면 doc_page가 코퍼스의
    91%라 한 검색에 섞으면 랭킹을 문서가 덮는다. 채널 정의는 그 모듈 하나에만 있다.

    ctx(CatalogContext)가 어휘 출처를 나른다 — 검색기가 없으면 카탈로그 전량을 메뉴로
    쓴다(사용자 제공 카탈로그 경로).
    """
    if not ctx.searchable:
        return _whole_catalog_dossier(ctx)

    retriever = ctx.retriever
    catalog = ctx.catalog

    # [0] 업무 분해 채널 — 액션 어휘 이전에 "어떤 제품 영역인가"만 본다.
    pkg_hits = channels.merge_by_rank(
        await channels.gather_channel(retriever, channels.DECOMPOSE, _decompose_queries(spec)),
        channels.DECOMPOSE,
    )
    sink.extend(pkg_hits)
    packages = _packages_block(pkg_hits)

    # [1] 조작 단위 확정 — 액션 선택 **전에**. 질의 확장보다 앞서므로 검색어도 이 분해를 탄다.
    # 두 호출 모두 동기 LLM이라 to_thread로 뺀다 — 여기서 이벤트 루프를 잡으면 같은 워커의
    # 다른 턴까지 멈춘다(호출을 하나 더 얹는 김에 기존 것도 함께 내보낸다).
    operations = await asyncio.to_thread(plan_operations, spec, packages)
    if operations:
        emit({"event": "stage", "stage": "searching",
              "message": f"조작 단위 {len(operations)}개 확정 — 이 단위로 어휘를 찾는다"})

    units = await asyncio.to_thread(_expand_queries, spec, operations)

    queries: list[str] = []
    for u in units:
        if u.ko_query.strip():
            queries.append(u.ko_query.strip())
        if u.en_query.strip():
            queries.append(u.en_query.strip())
    emit({"event": "stage", "stage": "searching",
          "message": f"액션 카탈로그 조사 중 ({len(units)}개 기능, 질의 {len(queries)}건)",
          "data": {"queries": [q[:80] for q in queries]}})

    # [2] 액션 채널 — 질의별 결과를 **따로** 받아 순위로 병합한다. 점수로 합치지 않는 이유는
    # merge_by_rank 참조(리랭커가 한 질의만 폴백해도 그 질의 후보가 통째로 밀려난다).
    results = await channels.gather_channel(retriever, channels.ACTION, queries)
    for hits in results:
        sink.extend(hits)
    ranked = channels.merge_by_rank(results, channels.ACTION)

    blocks: list[str] = []
    menu_actions: list[tuple[str, str]] = []
    for hit in ranked:
        pkg, act = hit.get("package_name"), hit.get("action_name")
        if not (pkg and act) or (pkg, act) in menu_actions:
            continue
        spec_dict = catalog.get_action_schema(pkg, act)
        if spec_dict is None:
            continue
        menu_actions.append((pkg, act))
        blocks.append(_menu_block(pkg, act, spec_dict))
        if len(menu_actions) >= _MAX_MENU_ACTIONS:
            break

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

    # [3] 파라미터 채널 — 이제 액션이 정해졌으니 **그 액션의 문서**를 집는다. v3는 목표
    # 문장 1질의로 doc_page 3건을 뽑는 게 전부였는데, 그렇게 나온 문서는 "이 액션의
    # 파라미터를 어떻게 채우나"에 답하지 못했다(목표어와 액션 문서는 어휘가 다르다).
    # 목표 질의는 맨 앞에 남아 있어 액션이 0개인 턴에도 background가 비지 않는다.
    bg_hits = channels.merge_by_rank(
        await channels.gather_channel(
            retriever, channels.PARAM_DOC,
            _param_doc_queries(catalog, menu_actions, spec.get("goal") or ""),
        ),
        channels.PARAM_DOC,
        # ⚠️ 제목으로 접으면 안 된다. doc_page는 제목이 문서 단위가 아니라 **청크 공유 키**라
        # (16,164행 / 제목 6,111개 — 60%가 2행 이상), 제목 dedup은 같은 문서의 서로 다른
        # 조각을 지운다. 파라미터 표가 든 조각이 버려질 수 있어 이 채널의 존재 이유를 깎는다.
        # id로 접으면 진짜 중복(같은 질의 재등장)만 걸러진다 — merge_by_rank의 기본 키다.
    )
    background = "\n".join(
        f"- {_one_line(h.get('title') or '', 80)}: {_one_line(h.get('content') or '', _BG_SNIPPET)}"
        for h in bg_hits
    )
    if bg_hits:
        sink.extend(bg_hits)

    # 용례 — 어휘가 아니라 **조합 패턴**을 나른다. 검색 0회(커밋된 자산 직독)이고
    # 홀드아웃은 기본 제외다(select_examples의 include_holdout 기본 False가 유출 방지 계약).
    picked = select_examples(
        spec.get("goal") or "",
        hint_packages={pkg for pkg, _ in menu_actions},
        limit=_MAX_EXAMPLES,
    )
    examples = render_examples_block(picked)
    if picked:
        logger.info("용례 주입 %d건: %s", len(picked), selection_trace(picked))

    emit({"event": "stage", "stage": "searching",
          "message": f"조사 완료 — 액션 후보 {len(menu_actions)}개 확보 "
                     f"(구조·세션 보완 {len(extra_blocks)}개 · 용례 {len(picked)}건 · "
                     f"문서 {len(bg_hits)}건 포함)"})

    ops_block = render_operations_block(operations)
    menu = "\n".join(blocks) or "(조사된 액션 없음 — 도구로 직접 검색 필요)"
    if ops_block and _INLINE_OPERATIONS_IN_MENU:
        # 어휘 목록보다 **앞에** 둔다 — 무엇을 할지 정한 뒤 무엇으로 할지 고르는 순서다.
        menu = (
            "[조작 단위 — 액션을 고르기 전에 확정됨. 각 조작은 흐름도에서 최소 한 액션으로 "
            "실현돼야 하고, 여러 액션으로 나뉘어도 된다]\n"
            f"{ops_block}\n\n[사용 가능한 액션]\n{menu}"
        )
    return {
        "menu": menu,
        "actions": menu_actions,
        "background": background,
        "examples": examples,
        "example_ids": selection_trace(picked),
        "operations": ops_block,
        "operation_units": operations,
        # 업무 분해 채널이 본 패키지 지형. graph는 아직 안 읽지만(그 파일은 다른 작업이
        # 잡고 있다) dossier 계약에 실어 둔다 — 소비 배선은 graph 쪽 후속.
        "packages": packages,
    }
