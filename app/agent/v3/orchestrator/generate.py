"""analyze·generate 브랜치 노드 (RPA-65).

analyze → generate는 직렬 파이프라인이고 intake가 종착점을 정한다:
- route=analyze: analyze_node에서 멈춤 (type="analysis")
- route=generate: 분석본이 없으면 analyze_node를 경유해 generate_node까지 —
  이때 analysis_out도 함께 반환해 백엔드가 분석본을 유실하지 않게 한다(조정 요청 1).

generate_node는 solution(세션 확정 키)으로 **카탈로그만** 가르고 파이프라인은 하나다
(RPA-285). 어휘 출처를 CatalogContext로 주입한다:
- "a360": DB 적재 카탈로그 + 하이브리드 검색기 — 어휘가 수천 개라 검색으로 좁힌다.
- 그 외: 대화에서 추출한 사용자 카탈로그(UserCatalog), 검색기 없음 — 전량이 곧 메뉴다.
어느 쪽이든 같은 v3 품질 루프(spec→research→후보 N→judge→verify→refine→cards)를 탄다.
예전엔 타 솔루션이 LLM 단발 호출로 갈라져 품질 루프가 a360에만 쌓였다.
"""

import asyncio
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from app.schemas import Recommendation

from .. import config
from ..analysis import _format_document, _has_text, analyze, analyze_text
from ..catalog_context import A360, a360_context, user_catalog_context
from ..recommend.graph import generate_flow
from ..recommend.stream import emit, emit_analysis_frame
from ..verify.checker import run_environment_checks
from .foreign_catalog import detect as detect_foreign_catalog
from .jsonio import chat_json
from .render import chat_task_brief, render_compact, render_history
from .spec import build_flow_spec
from .state import TYPE_ANALYSIS, TYPE_ANSWER, TYPE_RECOMMENDATION, TurnState
from .triggers import recommend_trigger

logger = logging.getLogger(__name__)

# 분석 결과를 단계별로 '드러내는' 프레임 사이 지연(초) — 흐름도 초안 노출과 같은 이유
# (지연이 없으면 네트워크가 한꺼번에 밀어내 점진 노출이 안 보인다).
_ANALYSIS_REVEAL_DELAY = 0.15

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
_CATALOG_PROMPT = (_PROMPT_DIR / "other_catalog.md").read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# analyze 노드
# ─────────────────────────────────────────────────────────────────────────────

async def analyze_node(state: TurnState) -> dict:
    """업무 서술(문서 또는 채팅) → AnalysisResult. 소스를 여기서 정규화한다.

    이후 파이프라인(generate)은 입력이 문서였는지 채팅이었는지 모른다.
    analysis(상태)와 analysis_out(반환 산출물)을 함께 갱신한다.
    """
    emit({"event": "stage", "stage": "analyzing", "message": "업무 내용 분석 중"})
    parsed = state.get("parsed_doc")
    if parsed and _has_text(parsed):
        result = analyze(parsed)
    else:
        result = analyze_text(chat_task_brief(state))
    d = result.model_dump()

    # 분석도 스트리밍 — analyze는 결과 전체를 한 번에 내지만(단일 LLM 호출), 요약 → 단계 하나씩
    # → 확인필요 순으로 점진 노출해 분석 결과가 채워지는 과정을 라이브로 보여준다(업로드 패널이
    # kind="analysis" 프레임마다 다시 그린다). 마지막 프레임에만 ambiguities(확인필요)를 싣는다.
    steps = d.get("steps") or []
    emit_analysis_frame({**d, "steps": [], "ambiguities": []}, "업무 요약 정리")
    await asyncio.sleep(_ANALYSIS_REVEAL_DELAY)
    for i in range(len(steps)):
        emit_analysis_frame({**d, "steps": steps[: i + 1], "ambiguities": []}, f"업무 단계 분석 {i + 1}/{len(steps)}")
        await asyncio.sleep(_ANALYSIS_REVEAL_DELAY)
    emit_analysis_frame(d, "분석 완료")

    n = len(d.get("steps", []))
    answer = f"업무를 {n}개 단계로 분해했어요."
    if d.get("ambiguities"):
        answer += " 확정하지 못한 항목: " + " / ".join(d["ambiguities"])
    return {
        "analysis": d,
        "analysis_out": d,
        "turn_type": TYPE_ANALYSIS,
        "answer": answer,
        "sources": [],
    }


# ─────────────────────────────────────────────────────────────────────────────
# generate 노드 — a360 경로
# ─────────────────────────────────────────────────────────────────────────────

def _collect_sources(flow: dict) -> list[dict]:
    """흐름도 액션 트리에 부착된 sources를 평탄화한다 (제목 기준 중복 제거)."""
    seen: set[str] = set()
    out: list[dict] = []

    def walk(actions: list[dict]) -> None:
        for a in actions:
            for s in a.get("sources") or []:
                title = s.get("title") or ""
                if title and title not in seen:
                    seen.add(title)
                    out.append(s)
            walk(a.get("children") or [])

    for step in flow.get("steps", []):
        walk(step.get("actions") or [])
    # 실행 시점 제안(trigger)의 근거 문서도 답변 근거에 포함한다 (A-2).
    for s in (flow.get("trigger") or {}).get("sources") or []:
        title = s.get("title") or ""
        if title and title not in seen:
            seen.add(title)
            out.append(s)
    return out


def _flow_answer(flow: dict, violations: list[dict]) -> str:
    n_steps = len(flow.get("steps", []))
    answer = f"{n_steps}개 업무 단계의 자동화 흐름도를 만들었어요."
    if flow.get("notes"):
        answer += f" 참고: {flow['notes']}"
    if violations:
        answer += f" (검수에서 해소하지 못한 위반 {len(violations)}건이 있어요 — 흐름도에서 확인해 주세요.)"
    return answer


async def _generate_with(state: TurnState, ctx) -> dict:
    """v3 품질 루프 실행: spec 정형화 → recommend 파이프라인(generate_flow).

    진행 이벤트(spec/candidates/verdict/flow/scorecard)는 파이프라인이 직접 부모 그래프
    스트림으로 emit한다. 업무정의서 원문(RPA-142)은 spec과 compose 양쪽에 실린다 —
    분석은 힌트, 원문이 근거.

    ctx(CatalogContext)가 어휘 출처를 나른다 — a360이든 사용자 제공 카탈로그든 **같은
    루프**를 탄다(RPA-285). 솔루션마다 파이프라인을 따로 두면 한쪽만 발전한다.
    """
    parsed = state.get("parsed_doc")
    document = _format_document(parsed) if parsed and _has_text(parsed) else None
    # build_flow_spec은 동기 LLM 호출 — 이벤트 루프를 막지 않게 스레드로 내린다.
    spec = await asyncio.to_thread(build_flow_spec, dict(state), document)
    result = await generate_flow(state["analysis"], document, spec, ctx)

    flow = result.get("recommendation") or Recommendation(steps=[]).model_dump()
    violations = result.get("violations") or []

    # 실행 시점 제안 (A-2): "매일 아침"·"메일이 오면" 같은 시점 의도를 트리거/스케줄로 잇는다.
    # 의도 없음·트리거 카탈로그 부재·LLM 실패면 None — 추천은 그대로 진행.
    # 트리거 패키지·Control Room 스케줄은 A360 고유 개념이라 타 솔루션에선 건너뛴다
    # (제안해도 사용자 환경에 대응물이 없어 오해만 만든다).
    if ctx.is_a360:
        trigger = await asyncio.to_thread(recommend_trigger, spec, document)
        if trigger and flow.get("steps"):
            flow["trigger"] = trigger
            # 트리거가 붙으면 무인 실행 전제가 생긴다 — attended 함정(R15)만 추가 점검한다
            # (트리거는 흐름 구조를 바꾸지 않으므로 전체 재검수는 과잉).
            violations = violations + [
                v.as_dict() for v in run_environment_checks(flow, ctx.catalog) if v.rule == "R15"
            ]
            note = f"실행 제안: {trigger['title']}" + (f" — {trigger['reason']}" if trigger.get("reason") else "")
            flow["notes"] = f"{flow['notes']} / {note}" if flow.get("notes") else note

    answer = _flow_answer(flow, violations)
    cards = flow.get("needs_input") or []
    if cards:
        answer += f" 확인이 필요한 질문 카드 {len(cards)}장을 함께 담았어요."
    out: dict = {
        "turn_type": TYPE_RECOMMENDATION,
        "recommendation_out": flow,
        "violations": violations,
        # 사용자 제공 카탈로그 경로는 KB 검색을 안 하므로 sources가 자연히 빈다.
        "sources": _collect_sources(flow),
    }
    if ctx.is_a360:
        # 사용자가 타 솔루션 카탈로그를 줬는데 A360으로 만든 경우, 그 사실을 알린다 (RPA-285).
        # 조용히 넘어가면 사용자는 자기 카탈로그가 반영된 줄 안다 — 가장 나쁜 실패다.
        signal = detect_foreign_catalog(dict(state), ctx.catalog)
        if signal.found:
            logger.info("타 솔루션 카탈로그 정황 — 쌍 %d개 중 A360 실재 %d개", signal.pairs, signal.known)
            answer += "\n\n" + signal.notice()
            # 백엔드가 세션 solution을 확정하게 신호를 올린다 (2단계). 이름을 못 밝혔으면
            # "other" — 어느 솔루션인지 몰라도 "A360은 아니다"는 확정할 수 있다.
            out["detected_solution"] = signal.solution or "other"
    out["answer"] = answer
    return out


# ─────────────────────────────────────────────────────────────────────────────
# generate 노드 — 타 솔루션 경로 (채팅 제공 카탈로그)
# ─────────────────────────────────────────────────────────────────────────────

class UserCatalogParam(BaseModel):
    """사용자 제공 카탈로그의 파라미터 스펙 (checker가 읽는 형태로 정규화)."""

    name: str
    type: str = "TEXT"
    required: bool = False
    options: list[dict] | None = None
    default: object | None = None

    def as_spec(self) -> dict:
        spec: dict = {"name": self.name, "label": self.name, "type": self.type, "required": self.required}
        if self.options is not None:
            spec["options"] = self.options
        if self.default is not None:
            spec["default"] = self.default
        return spec


class UserCatalogAction(BaseModel):
    package: str
    action: str
    label: str | None = None
    parameters: list[UserCatalogParam] = Field(default_factory=list)

    def as_spec(self) -> dict:
        return {
            "package": self.package,
            "action": self.action,
            "label": self.label or self.action,
            "parameters": [p.as_spec() for p in self.parameters],
        }


class CatalogExtraction(BaseModel):
    solution: str | None = None
    actions: list[UserCatalogAction] = Field(default_factory=list)


class UserCatalog:
    """대화에서 추출한 카탈로그의 CatalogLookup 구현 — 기존 checker(R1~R6)가 그대로 검수한다."""

    def __init__(self, actions: list[dict]):
        self._index: dict[tuple[str, str], dict] = {
            (a["package"], a["action"]): a for a in actions
        }

    def get_action_schema(self, package: str, action: str) -> dict | None:
        return self._index.get((package, action))

    def iter_action_schemas(self):
        """전체 액션 스펙 순회 — BackendCatalog와 같은 계약 (RPA-285).

        검색기가 없는 타 솔루션 경로에서 research가 이걸로 액션 메뉴를 만들고,
        세션 레지스트리 유도(derive_session_registry)도 같은 입구를 쓴다.
        """
        yield from self._index.values()


def extract_user_catalog(state: TurnState) -> CatalogExtraction:
    """message + 이력 + 압축본(보존 원문 포함)에서 사용자 제공 카탈로그를 추출한다.

    카탈로그가 이전 턴이나 compact의 verbatim에 있었을 수 있어 셋을 모두 본다.
    """
    user_content = (
        f"[이전 대화 압축 요약]\n{render_compact(state.get('compact'))}\n\n"
        f"[대화 이력]\n{render_history(state.get('history'))}\n\n"
        f"[현재 요청]\n{state.get('message', '')}"
    )
    return chat_json(
        [{"role": "system", "content": _CATALOG_PROMPT},
         {"role": "user", "content": user_content}],
        purpose="recommend", model_cls=CatalogExtraction,
    )


async def resolve_catalog_context(state: TurnState):
    """이번 턴이 쓸 어휘 출처를 정한다 — a360 카탈로그 또는 대화에서 추출한 사용자 카탈로그.

    세션 solution이 a360이 아니면 대화(메시지+이력+compact.verbatim)에서 카탈로그를
    추출한다. 못 찾으면 None — 호출부가 "카탈로그를 달라"고 안내한다(흐름도를 만들 어휘가
    없는데 A360 어휘로 만들면 그게 곧 조용한 오답이다).
    """
    solution = state.get("solution") or A360
    if solution == A360:
        return a360_context()

    emit({"event": "stage", "stage": "recommending", "message": "제공하신 카탈로그 확인 중"})
    # extract_user_catalog는 동기 LLM 호출 — 이벤트 루프를 막지 않게 스레드로 내린다.
    extraction = await asyncio.to_thread(extract_user_catalog, state)
    if not extraction.actions:
        return None

    specs = [a.as_spec() for a in extraction.actions]
    emit({"event": "stage", "stage": "recommending",
          "message": f"{extraction.solution or '제공된'} 카탈로그 {len(specs)}개 액션 확인"})
    return user_catalog_context(UserCatalog(specs), solution)


_NEED_CATALOG_ANSWER = (
    "흐름도를 만들려면 사용 중인 솔루션의 액션 카탈로그가 필요해요. "
    "패키지/액션 이름 목록(가능하면 파라미터 포함)을 채팅으로 알려주시면 "
    "그 표기 그대로 흐름도를 구성할게요."
)


async def generate_node(state: TurnState) -> dict:
    """어휘 출처를 정하고 v3 품질 루프를 돌린다. analysis는 선행 노드가 보장한다.

    (RPA-285) 예전에는 solution으로 파이프라인 자체를 갈랐다 — a360은 품질 루프,
    나머지는 LLM 단발 호출. 이제 갈리는 건 카탈로그뿐이고 루프는 하나다.
    """
    ctx = await resolve_catalog_context(state)
    if ctx is None:  # 타 솔루션인데 쓸 어휘가 없다 — 만들지 않고 되묻는다(type 정확성)
        return {"turn_type": TYPE_ANSWER, "answer": _NEED_CATALOG_ANSWER, "sources": []}
    return await _generate_with(state, ctx)
