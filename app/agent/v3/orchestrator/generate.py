"""analyze·generate 브랜치 노드 (RPA-65).

analyze → generate는 직렬 파이프라인이고 intake가 종착점을 정한다:
- route=analyze: analyze_node에서 멈춤 (type="analysis")
- route=generate: 분석본이 없으면 analyze_node를 경유해 generate_node까지 —
  이때 analysis_out도 함께 반환해 백엔드가 분석본을 유실하지 않게 한다(조정 요청 1).

generate_node는 solution(세션 확정 키)으로 카탈로그 소스를 가른다:
- "a360": 기존 recommend 서브그래프 재사용 — 결정론 shortlist(RAG)→compose→
  단계별 check가 내장돼 있고, 진행 이벤트는 부모 스트림으로 중계한다.
- 그 외: 대화에서 사용자 제공 카탈로그를 추출(UserCatalog)해 폐쇄어휘로 생성한다.
양쪽 모두 마지막에 공유 verify harness(R1~R6 + repair)를 통과한다.
"""

import asyncio
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from app.schemas import Recommendation

from .. import config
from ..analysis import _format_document, _has_text, analyze, analyze_text
from ..recommend.graph import generate_flow
from ..recommend.stream import emit, emit_analysis_frame
from .harness import verify_and_repair
from .jsonio import chat_json
from .render import analysis_brief, chat_task_brief, render_compact, render_history
from .spec import build_flow_spec
from .state import TYPE_ANALYSIS, TYPE_ANSWER, TYPE_RECOMMENDATION, TurnState

logger = logging.getLogger(__name__)

# 분석 결과를 단계별로 '드러내는' 프레임 사이 지연(초) — 흐름도 초안 노출과 같은 이유
# (지연이 없으면 네트워크가 한꺼번에 밀어내 점진 노출이 안 보인다).
_ANALYSIS_REVEAL_DELAY = 0.15

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
_CATALOG_PROMPT = (_PROMPT_DIR / "other_catalog.md").read_text(encoding="utf-8")
_COMPOSE_PROMPT = (_PROMPT_DIR / "other_compose.md").read_text(encoding="utf-8")


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
    return out


def _flow_answer(flow: dict, violations: list[dict]) -> str:
    n_steps = len(flow.get("steps", []))
    answer = f"{n_steps}개 업무 단계의 자동화 흐름도를 만들었어요."
    if flow.get("notes"):
        answer += f" 참고: {flow['notes']}"
    if violations:
        answer += f" (검수에서 해소하지 못한 위반 {len(violations)}건이 있어요 — 흐름도에서 확인해 주세요.)"
    return answer


async def _generate_a360(state: TurnState) -> dict:
    """v3 품질 루프 실행: spec 정형화 → recommend 파이프라인(generate_flow).

    진행 이벤트(spec/candidates/verdict/flow/scorecard)는 파이프라인이 직접 부모 그래프
    스트림으로 emit한다. 업무정의서 원문(RPA-142)은 spec과 compose 양쪽에 실린다 —
    분석은 힌트, 원문이 근거.
    """
    parsed = state.get("parsed_doc")
    document = _format_document(parsed) if parsed and _has_text(parsed) else None
    # build_flow_spec은 동기 LLM 호출 — 이벤트 루프를 막지 않게 스레드로 내린다.
    spec = await asyncio.to_thread(build_flow_spec, dict(state), document)
    result = await generate_flow(state["analysis"], document, spec)

    flow = result.get("recommendation") or Recommendation(steps=[]).model_dump()
    violations = result.get("violations") or []
    answer = _flow_answer(flow, violations)
    cards = flow.get("needs_input") or []
    if cards:
        answer += f" 확인이 필요한 질문 카드 {len(cards)}장을 함께 담았어요."
    return {
        "turn_type": TYPE_RECOMMENDATION,
        "recommendation_out": flow,
        "violations": violations,
        "answer": answer,
        "sources": _collect_sources(flow),
    }


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


class OtherFlowOutput(BaseModel):
    recommendation: Recommendation
    answer: str = ""


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


def _format_user_menu(actions: list[dict]) -> str:
    blocks = []
    for a in actions:
        params = ", ".join(
            f"{p['name']}({p.get('type')}{', 필수' if p.get('required') else ''})"
            for p in a.get("parameters", [])
        )
        blocks.append(f"- {a['package']}/{a['action']} ({a.get('label')})\n    파라미터: {params or '없음'}")
    return "\n".join(blocks)


def _generate_other(state: TurnState) -> dict:
    """사용자 카탈로그 폐쇄어휘로 흐름도 생성 → UserCatalog 기준 harness 검수.

    카탈로그를 못 찾으면 흐름도를 만들지 않고 안내 답변으로 종료한다 —
    실제로 recommendation을 안 만들었으므로 type도 "answer"다(type 정확성 원칙).
    """
    emit({"event": "stage", "stage": "recommending", "message": "제공하신 카탈로그 확인 중"})
    extraction = extract_user_catalog(state)
    if not extraction.actions:
        return {
            "turn_type": TYPE_ANSWER,
            "answer": (
                "흐름도를 만들려면 사용 중인 솔루션의 액션 카탈로그가 필요해요. "
                "패키지/액션 이름 목록(가능하면 파라미터 포함)을 채팅으로 알려주시면 "
                "그 표기 그대로 흐름도를 구성할게요."
            ),
            "sources": [],
        }

    specs = [a.as_spec() for a in extraction.actions]
    emit({"event": "stage", "stage": "recommending",
          "message": f"{extraction.solution or '제공된'} 카탈로그 {len(specs)}개 액션으로 흐름도 생성 중"})

    user_content = (
        f"[업무 단계 분석 결과]\n{analysis_brief(state.get('analysis'))}\n\n"
        f"[카탈로그]\n{_format_user_menu(specs)}\n\n"
        f"[사용자 요청]\n{state.get('message', '')}"
    )
    out = chat_json(
        [{"role": "system", "content": _COMPOSE_PROMPT},
         {"role": "user", "content": user_content}],
        purpose="recommend", model_cls=OtherFlowOutput,
    )
    result = verify_and_repair(out.recommendation.model_dump(), UserCatalog(specs))
    return {
        "turn_type": TYPE_RECOMMENDATION,
        "recommendation_out": result["flow"],
        "violations": result["violations"],
        "answer": out.answer or _flow_answer(result["flow"], result["violations"]),
        "sources": [],  # 사용자 제공 카탈로그 기반 — KB 근거 없음
    }


async def generate_node(state: TurnState) -> dict:
    """solution 키로 a360/타 솔루션 경로를 가른다. analysis는 선행 노드가 보장한다."""
    if (state.get("solution") or "a360") == "a360":
        return await _generate_a360(state)
    return _generate_other(state)
