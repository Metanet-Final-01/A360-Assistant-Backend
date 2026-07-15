"""recommend v3 — 품질 루프 흐름도 생성 (설계 §2).

v2의 "compose ReAct 1후보 → 정적 검수 → 국소 교정"을 다음으로 대체한다:

    spec(FlowSpec)                          … 채점 기준 (orchestrator spec_builder가 선행)
      → research (이중 질의 Dossier)         … 조사 선행·공유 — 후보들이 같은 근거에서 경쟁
      → compose ×N (A360 전문가 페르소나)     … 모범 사례 / 운영 안정성 / 문서 충실, 병렬
      → verify 스택 (후보별)                 … L0/L1 정적 → L2 시맨틱 → L3 시뮬레이션
      → judge (루브릭·하드 게이트)            … 승자 + 패자 장점 이식 지시
      → refine (surgeon EditOps 패치 루프)    … 회귀 가드, ≤3라운드
      → finalize                             … sources·confidence 합성·질문 카드·flow_confidence

구현은 내부 StateGraph 없이 순수 async 파이프라인이다 — emit()은 부모(오케스트레이터)
그래프의 스트림 컨텍스트를 그대로 탄다. 후보 하나의 실패는 N 강등일 뿐 턴 실패가 아니다
(부분 실패 격리). 전 후보 실패만 RuntimeError로 끊는다.
"""

import asyncio
import copy
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from app.core.llm import UsageCallbackHandler
from app.schemas import ProgressEvent, Recommendation

from .. import config
from .stream import (
    emit,
    emit_candidates_frame,
    emit_flow_frame,
    emit_scorecard_frame,
    emit_verdict_frame,
)

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
_BASE_PROMPT = (_PROMPT_DIR / "compose_agent.md").read_text(encoding="utf-8")
_ADDENDUM = (_PROMPT_DIR / "compose_v3_addendum.md").read_text(encoding="utf-8")
_PERSONAS: list[tuple[str, str, str, bool]] = [
    # (candidate_id, 표시명, 프롬프트 파일, 문서 필요 여부)
    ("A", "모범 사례 전문가", "persona_best_practice.md", False),
    ("B", "운영 안정성 전문가", "persona_ops.md", False),
    ("C", "문서 충실 재현", "persona_doc_faithful.md", True),
]

# 초안 흐름도를 단계별로 '드러내는' 프레임 사이 지연(초) — v2와 동일한 인지적 페이싱.
_REVEAL_DELAY = 0.18
# 후보별 compose 예산: escape hatch 툴콜 ≤2회 + 파싱 재출력 1회를 담는 총 왕복 상한.
_COMPOSE_MAX_TURNS = 5
_ESCAPE_HATCH_ROUNDS = 2
# recommend 검색은 액션 후보 메뉴용 — 문서 페이지·패키지 개요 오염을 막는다.
SEARCH_SOURCE_TYPES = ["action_schema", "bot_example"]

# 에이전트가 흔히 슬립하는 enum 필드의 허용값 — 벗어나면 안전값으로 강등한다.
_VALID_VALUE_SOURCE = {"schema_default", "llm", "user"}
_VALID_DIRECTION = {"input", "output", "local"}


# ─────────────────────────────────────────────────────────────────────────────
# 헬퍼 (v2 계승 — _coerce/_parse/_attach_sources는 실측 검증 자산)
# ─────────────────────────────────────────────────────────────────────────────

def _make_llm():
    """compose용 ChatOpenAI 클라이언트를 만든다(사용량 스트리밍 on)."""
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=config.OPENAI_MODEL, api_key=config.OPENAI_API_KEY, stream_usage=True)


def _to_dict(analysis: Any) -> dict:
    """AnalysisResult(Pydantic) 또는 dict를 상태용 dict로 정규화한다."""
    if hasattr(analysis, "model_dump"):
        return analysis.model_dump()
    return dict(analysis)


def _coerce_action(a: dict, order: int = 1) -> None:
    """액션 dict의 흔한 타입/enum 슬립을 제자리 보정한다 (재귀) — v2 계승 + v3 필드.

    value_source 강등(0단계 버그 방지)·order 폴백에 더해, v3의 produces/consumes가
    dict 리스트가 아니면 버린다(검증 깨짐 방지 — 미기재와 동일한 자연 강등).
    """
    if not isinstance(a.get("order"), int):
        a["order"] = order
    params = a.get("parameters")
    if not isinstance(params, list):
        a["parameters"] = params = []
    for p in params:
        if isinstance(p, dict) and p.get("value_source") not in _VALID_VALUE_SOURCE:
            p["value_source"] = "llm"
    for key in ("produces", "consumes"):
        refs = a.get(key)
        if not isinstance(refs, list):
            a[key] = []
        else:
            a[key] = [
                r if isinstance(r, dict) else {"name": str(r)}
                for r in refs
                if (isinstance(r, dict) and r.get("name")) or (isinstance(r, str) and r.strip())
            ]
    children = a.get("children")
    if not isinstance(children, list):
        a["children"] = children = []
    for i, c in enumerate(children):
        if isinstance(c, dict):
            _coerce_action(c, i + 1)


def _coerce_flow(obj: dict) -> dict:
    """Recommendation 검증 직전, 에이전트 출력의 슬립을 제자리 보정한다 (v2 계승)."""
    steps = obj.get("steps")
    if not isinstance(steps, list):
        obj["steps"] = steps = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        if not isinstance(step.get("step_id"), str) or not step.get("step_id"):
            step["step_id"] = f"step-{i + 1}"
        if not isinstance(step.get("label"), str) or not step.get("label"):
            step["label"] = step.get("step_id") or f"step-{i + 1}"
        actions = step.get("actions")
        if not isinstance(actions, list):
            step["actions"] = actions = []
        for j, a in enumerate(actions):
            if isinstance(a, dict):
                _coerce_action(a, j + 1)
    variables = obj.get("variables")
    if not isinstance(variables, list):
        obj["variables"] = variables = []
    for v in variables:
        if isinstance(v, dict) and v.get("direction") not in _VALID_DIRECTION:
            v["direction"] = "local"
    notes = obj.get("notes")
    if isinstance(notes, list):
        obj["notes"] = " · ".join(str(n) for n in notes) or None
    elif notes is not None and not isinstance(notes, str):
        obj["notes"] = str(notes)
    return obj


def _parse_flow(content: str) -> dict:
    """LLM 최종 출력에서 Recommendation 흐름도 dict를 뽑는다 (코드펜스 내성)."""
    text = (content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("JSON 객체를 찾지 못함")
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 파싱 실패: {e}") from e
    if not isinstance(obj, dict) or "steps" not in obj:
        raise ValueError("최상위에 steps 키가 있는 JSON 객체가 아님")
    return _coerce_flow(obj)


def _attach_sources(flow: dict, sink: list[dict]) -> dict:
    """검색 히트(sink)에서 (package, action)별 최고 점수 근거를 액션에 부착한다 (FR-11)."""
    best: dict[tuple, dict] = {}
    for h in sink:
        key = (h.get("package_name"), h.get("action_name"))
        if not key[0] or not key[1]:
            continue
        cur = best.get(key)
        if cur is None or (h.get("score") or 0) > (cur.get("score") or 0):
            best[key] = h

    def walk(actions: list[dict]) -> None:
        for a in actions:
            hit = best.get((a.get("package"), a.get("action")))
            if hit and not a.get("sources"):
                a["sources"] = [{
                    "source_type": hit.get("source_type") or "action_schema",
                    "title": hit.get("title") or f"{a.get('package')}/{a.get('action')}",
                    "url": hit.get("url"),
                    "score": hit.get("score"),
                }]
            walk(a.get("children") or [])

    for step in flow.get("steps", []):
        walk(step.get("actions") or [])
    return flow


def _iter_pkg_actions(flow: dict):
    def walk(actions: list[dict]):
        for a in actions:
            if a.get("package") and a.get("action"):
                yield (a["package"], a["action"])
            yield from walk(a.get("children") or [])

    for step in flow.get("steps", []):
        yield from walk(step.get("actions") or [])


def _flow_counts(flow: dict) -> tuple[int, int]:
    steps = flow.get("steps") or []
    actions = sum(1 for _ in _iter_pkg_actions(flow))
    return len(steps), actions


def _render_spec_block(spec: dict) -> str:
    lines = [f"목표: {spec.get('goal', '')}"]
    for r in spec.get("requirements") or []:
        lines.append(f"- [{r.get('req_id')}] ({r.get('priority', 'must')}) {r.get('text', '')}")
    if spec.get("error_policy"):
        lines.append("예외 처리 기대: " + " / ".join(spec["error_policy"]))
    if spec.get("assumptions"):
        lines.append("전제(이미 확정): " + " / ".join(spec["assumptions"]))
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# compose — 페르소나 후보 생성 (Dossier 주입 + escape hatch ≤2회)
# ─────────────────────────────────────────────────────────────────────────────

async def _compose_candidate(
    cid: str,
    persona_file: str,
    spec: dict,
    dossier: dict,
    analysis: dict,
    document: str | None,
    sink: list[dict],
    sem: asyncio.Semaphore,
) -> dict | None:
    """페르소나 하나로 후보 흐름도를 생성한다. 실패 시 None (부분 실패 격리)."""
    from ..orchestrator.render import analysis_brief
    from ..orchestrator.spec import fenced_doc_block
    from ..orchestrator.tools import build_kb_tools, execute_tool_calls

    persona = (_PROMPT_DIR / persona_file).read_text(encoding="utf-8")
    llm = _make_llm()
    tools = build_kb_tools(sink, source_types=SEARCH_SOURCE_TYPES)
    runnable = llm.bind_tools(tools)
    usage_config = {"callbacks": [UsageCallbackHandler(purpose="turn_generate")]}

    background = f"\n\n[배경 지식 (공식 문서 발췌)]\n{dossier['background']}" if dossier.get("background") else ""
    system = (
        f"{_BASE_PROMPT}\n\n{_ADDENDUM}\n\n{persona}\n\n"
        f"[업무 분석]\n{analysis_brief(analysis)}\n\n"
        f"[요구사항 스펙]\n{_render_spec_block(spec)}\n\n"
        f"[액션 후보 메뉴]\n{dossier['menu']}{background}"
    )
    user = (
        "위 요구사항 스펙을 달성하는 A360 흐름도를 당신의 설계 관점대로 설계하고, "
        "최종 Recommendation JSON 하나만 출력하라."
        f"{fenced_doc_block(document)}"
    )
    msgs: list = [SystemMessage(content=system), HumanMessage(content=user)]
    tool_rounds = 0
    parse_retried = False

    for _ in range(_COMPOSE_MAX_TURNS):
        target = runnable if tool_rounds < _ESCAPE_HATCH_ROUNDS else llm
        try:
            async with sem:
                ai = await target.ainvoke(msgs, config=usage_config)
        except Exception as e:  # noqa: BLE001 — 후보 하나의 인프라 실패는 N 강등
            logger.warning("후보 %s compose 호출 실패: %s", cid, e)
            return None
        msgs.append(ai)
        if getattr(ai, "tool_calls", None):
            tool_rounds += 1
            msgs.extend(execute_tool_calls(tools, ai))
            continue
        try:
            return _parse_flow(ai.content)
        except ValueError as e:
            if parse_retried:
                logger.warning("후보 %s 파싱 재실패 — 탈락: %s", cid, e)
                return None
            parse_retried = True
            msgs.append(HumanMessage(content=(
                f"출력이 올바른 Recommendation JSON이 아니다({e}). "
                "코드펜스·설명 없이 JSON 객체 하나만 다시 출력하라."
            )))
    logger.warning("후보 %s compose 예산 소진 — 탈락", cid)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# verify 스택 — 후보별 L0/L1 → L2 → L3
# ─────────────────────────────────────────────────────────────────────────────

async def _verify_candidate(cid: str, persona_name: str, flow: dict, spec: dict, sem: asyncio.Semaphore):
    """후보 하나에 검증 스택을 돌려 CandidateReport를 만든다. L2/L3 실패는 신호 결측일 뿐."""
    from ..orchestrator.harness import collect_violations, from_violations_dicts
    from ..orchestrator.judge import CandidateReport
    from ..verify import findings as F
    from ..verify.catalog import get_catalog
    from ..verify.semantic import run_semantic_check
    from ..verify.simulate import run_simulation

    violations = collect_violations(flow, get_catalog())
    fnd, _cards = from_violations_dicts(violations)

    coverage = None
    try:
        async with sem:
            coverage = await asyncio.to_thread(run_semantic_check, spec, flow)
    except Exception as e:  # noqa: BLE001
        logger.warning("후보 %s L2 채점 실패 — 커버리지 결측: %s", cid, e)
    sim = None
    try:
        async with sem:
            sim = await asyncio.to_thread(run_simulation, spec, flow)
    except Exception as e:  # noqa: BLE001
        logger.warning("후보 %s L3 시뮬레이션 실패 — 통과율 결측: %s", cid, e)

    if coverage is not None:
        fnd += F.from_coverage(coverage)
    if sim is not None:
        fnd += F.from_simulation(sim)

    cov_by_step: dict[str, str] = {}
    cov_by_req: dict[str, str] = {}
    if coverage is not None:
        # evidence 노드 id는 국소 좌표라, 단계 상태는 req 상태를 step에 근사 배정하지 않고
        # 전 단계 공통(최저 status)로 쓰기보다 req 단위로만 유지한다. 단계 매핑은 evidence
        # 기반 정밀화가 후속 — confidence의 semantic 축은 req 상태의 흐름도 전역 요약을 쓴다.
        cov_by_req = {e.req_id: e.status for e in coverage.entries}

    return CandidateReport(
        candidate_id=cid,
        persona=persona_name,
        flow=flow,
        violations=violations,
        findings=fnd,
        must_coverage=coverage.must_coverage if coverage is not None else None,
        gate_failures=[e.req_id for e in coverage.hard_gate_failures()] if coverage is not None else [],
        sim_pass_rate=sim.pass_rate if sim is not None else None,
        coverage_by_step=cov_by_step,
        coverage_by_req=cov_by_req,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 파이프라인 본체
# ─────────────────────────────────────────────────────────────────────────────

async def generate_flow(analysis: Any, document: str | None, spec: dict) -> dict:
    """spec → research → compose×N → verify → judge → refine → finalize.

    반환: {"recommendation": Recommendation dict, "violations": list[dict]}.
    전 후보 실패 시 RuntimeError (호출부가 error 이벤트로 처리).
    """
    from ..orchestrator import cards as cards_mod
    from ..orchestrator.harness import (
        attach_confidence,
        collect_violations,
        compute_flow_confidence,
        from_violations_dicts,
        refine_flow,
    )
    from ..orchestrator.judge import judge_candidates
    from ..verify import findings as F
    from ..verify.catalog import get_catalog
    from ..verify.semantic import run_semantic_check
    from .research import build_dossier

    analysis = _to_dict(analysis)
    sink: list[dict] = []
    sem = asyncio.Semaphore(config.MAX_LLM_CONCURRENCY)

    # [2] research — 조사 선행·공유
    dossier = await build_dossier(spec, sink)

    # [3] compose ×N — 문서 기반이면 문서 충실 페르소나까지 3후보
    personas = [(c, n, f) for c, n, f, needs_doc in _PERSONAS if document or not needs_doc]
    cand_status = [
        {"id": c, "persona": n, "status": "composing", "steps": 0, "actions": 0}
        for c, n, _ in personas
    ]
    emit_candidates_frame(cand_status, f"{len(personas)}가지 설계 관점으로 후보 생성 중")

    composed = await asyncio.gather(*(
        _compose_candidate(c, f, spec, dossier, analysis, document, sink, sem)
        for c, n, f in personas
    ))
    flows: list[tuple[str, str, dict]] = []
    for (c, n, _f), flow in zip(personas, composed):
        st = next(s for s in cand_status if s["id"] == c)
        if flow is None:
            st["status"] = "failed"
        else:
            st["status"] = "verifying"
            st["steps"], st["actions"] = _flow_counts(flow)
            flows.append((c, n, flow))
    emit_candidates_frame(cand_status, f"후보 {len(flows)}개 생성 — 검증 스택 통과 중")
    if not flows:
        raise RuntimeError("흐름도 후보를 하나도 생성하지 못했습니다")

    # [4] verify 스택 — 후보별 병렬
    reports = await asyncio.gather(*(
        _verify_candidate(c, n, flow, spec, sem) for c, n, flow in flows
    ))
    for r in reports:
        st = next(s for s in cand_status if s["id"] == r.candidate_id)
        st["status"] = "done"
    emit_candidates_frame(cand_status, "후보 검증 완료 — 심판 채점 중")

    # [5] judge — 승자 + 이식 지시
    verdict = await asyncio.to_thread(judge_candidates, spec, list(reports))
    winner = verdict["winner"]
    emit_verdict_frame(verdict["verdict"], f"후보 {winner.candidate_id} 선택")

    # 승자 트리 점진 노출 — '자라나는 흐름도' 경험은 승자 확정 이후부터 (v2 계승).
    steps = winner.flow.get("steps") or []
    for i in range(len(steps)):
        emit_flow_frame({**winner.flow, "steps": steps[: i + 1]}, None,
                        f"선택된 설계 구성 {i + 1}/{len(steps)}")
        await asyncio.sleep(_REVEAL_DELAY)
    emit_flow_frame(winner.flow, winner.violations, "선택된 초안 · 다듬기 시작")

    # [6] refine — 정적 위반 + L2/L3 발견 + 이식 지시를 surgeon 패치로
    extra = [f for f in winner.findings if f.layer in ("L2", "L3")] + verdict["transplant_findings"]
    refined = await asyncio.to_thread(
        refine_flow, winner.flow, get_catalog(), extra_findings=extra, purpose="turn_generate"
    )
    flow, violations = refined["flow"], refined["violations"]

    # [7] finalize — 근거·confidence·질문 카드·flow_confidence
    flow = _attach_sources(flow, sink)

    coverage = None
    sim_rate = winner.sim_pass_rate
    if refined["repaired"]:  # 흐름이 바뀌었을 때만 L2/L3 재채점 (설계: 심판 시점+최종 시점 2회)
        try:
            async with sem:
                coverage = await asyncio.to_thread(run_semantic_check, spec, flow)
        except Exception as e:  # noqa: BLE001
            logger.warning("최종 L2 재채점 실패 — 승자 채점 재사용: %s", e)
        # L3도 재실행 — 교정으로 구조가 바뀌었는데 교정 전 통과율을 쓰면 신뢰도가 낡는다.
        from ..verify.simulate import run_simulation
        try:
            async with sem:
                sim_rate = (await asyncio.to_thread(run_simulation, spec, flow)).pass_rate
        except Exception as e:  # noqa: BLE001
            logger.warning("최종 L3 재실행 실패 — 승자 통과율 재사용: %s", e)
    must_cov = coverage.must_coverage if coverage is not None else winner.must_coverage

    findings_final, r3_cards = from_violations_dicts(violations)
    if coverage is not None:
        findings_final += F.from_coverage(coverage)

    # 질문 카드 — R3 + unknowns + assumptions (결정론) → 문구 다듬기 (no-fail LLM 1회)
    cards = cards_mod.build_cards(flow, spec, r3_cards, get_catalog())
    if cards:
        async with sem:
            cards = await asyncio.to_thread(cards_mod.polish_card_wording, cards)

    # confidence — agreement(후보 간 합의)는 후보 2개 이상일 때만
    agreement = None
    if len(flows) >= 2:
        counts: dict[tuple[str, str], int] = {}
        for _c, _n, f in flows:
            for key in set(_iter_pkg_actions(f)):
                counts[key] = counts.get(key, 0) + 1
        agreement = {k for k, v in counts.items() if v >= 2}
    attach_confidence(flow, sink, violations, agreement=agreement)

    blocking_cards = sum(1 for c in cards if c.get("blocking") and not c.get("resolved"))
    flow["needs_input"] = cards
    flow["spec"] = spec
    flow["flow_confidence"] = compute_flow_confidence(
        must_coverage=must_cov,
        findings=findings_final,
        sim_pass_rate=sim_rate,
        blocking_cards=blocking_cards,
    )

    emit_scorecard_frame({
        "must_coverage": must_cov,
        "blockers": sum(1 for f in findings_final if f.severity == "blocker"),
        "warnings": sum(1 for f in findings_final if f.severity == "warning"),
        "sim_pass_rate": sim_rate,
        "cards": len(cards),
        "flow_confidence": flow["flow_confidence"],
    }, "최종 검증 요약")

    flow = _coerce_flow(flow)
    try:
        rec = Recommendation.model_validate(flow)
    except ValidationError as e:
        # 마지막 관문에서 전부 버리지 않는다 — 교정 전 승자 초안(직전 유효 후보)으로 강등 시도.
        logger.warning("최종 흐름도 정규화 실패 — 승자 초안으로 강등 시도: %s", e)
        try:
            rec = Recommendation.model_validate(_coerce_flow(copy.deepcopy(winner.flow)))
        except ValidationError as e2:
            logger.warning("승자 초안도 정규화 실패, 빈 추천안: %s", e2)
            rec = Recommendation(steps=[])
    rec_dict = rec.model_dump()
    emit_flow_frame(rec_dict, violations, "완료")
    return {"recommendation": rec_dict, "violations": violations}


# ─────────────────────────────────────────────────────────────────────────────
# 공개 진입점 (독립 실행용 — 오케스트레이터 밖에서도 스트리밍)
# ─────────────────────────────────────────────────────────────────────────────

async def recommend(
    analysis: Any, constraints: list[str] | None = None, parsed_doc: dict | None = None
) -> AsyncIterator[ProgressEvent]:
    """AnalysisResult → A360 추천안 스트림 (INTERFACES §4 ② — v3 품질 루프).

    단일 노드 StateGraph로 감싸 emit()의 스트림 컨텍스트를 만든다 — 파이프라인 자체는
    generate_flow와 동일하다. constraints는 spec의 assumptions로 편입된다.
    """
    if not config.OPENAI_API_KEY:
        yield ProgressEvent(event="error", message="OPENAI_API_KEY 환경변수가 필요합니다")
        return

    from langgraph.graph import END, START, StateGraph
    from typing_extensions import TypedDict

    from ..orchestrator.spec import build_flow_spec

    document: str | None = None
    if parsed_doc:
        from ..analysis import _format_document, _has_text

        if _has_text(parsed_doc):
            document = _format_document(parsed_doc)

    class _S(TypedDict, total=False):
        result: dict

    async def _run(_state: _S) -> dict:
        # 동기 LLM 호출 — 이벤트 루프 블로킹 방지 (파이프라인의 다른 LLM 호출과 동일 정책).
        spec = await asyncio.to_thread(
            build_flow_spec, {"analysis": _to_dict(analysis), "message": ""}, document
        )
        if constraints:
            spec.setdefault("assumptions", []).extend(constraints)
        return {"result": await generate_flow(analysis, document, spec)}

    g = StateGraph(_S)
    g.add_node("run", _run)
    g.add_edge(START, "run")
    g.add_edge("run", END)
    graph = g.compile()

    final_state: dict = {}
    try:
        async for mode, chunk in graph.astream(
            {}, stream_mode=["custom", "values"], config={"recursion_limit": 10},
        ):
            if mode == "custom":
                yield ProgressEvent(**chunk)
            elif mode == "values":
                final_state = chunk
    except RuntimeError as e:
        yield ProgressEvent(event="error", message=str(e))
        return
    except Exception:  # noqa: BLE001 — 예기치 못한 실패도 스트림을 죽이지 않는다
        logger.exception("recommend 실패")
        yield ProgressEvent(event="error", message="추천 생성 중 오류가 발생했습니다")
        return

    result = final_state.get("result") or {}
    yield ProgressEvent(event="done", data={"recommendation": result.get("recommendation")})
