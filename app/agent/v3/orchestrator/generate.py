"""analyze·generate 브랜치 노드 (RPA-65).

analyze → generate는 직렬 파이프라인이고 intake가 종착점을 정한다:
- route=analyze: analyze_node에서 멈춤 (type="analysis")
- route=generate: 분석본이 없으면 analyze_node를 경유해 generate_node까지 —
  이때 analysis_out도 함께 반환해 백엔드가 분석본을 유실하지 않게 한다(조정 요청 1).

generate_node는 solution(세션 확정 키)으로 **카탈로그만** 가르고 파이프라인은 하나다
(RPA-285). 어휘 출처를 CatalogContext로 주입한다:
- "a360": DB 적재 카탈로그 + 하이브리드 검색기 — 어휘가 수천 개라 검색으로 좁힌다.
- 그 외: 대화에서 추출한 사용자 카탈로그(UserCatalog), 검색기 없음 — 전량이 곧 메뉴다.
어느 쪽이든 같은 v3 품질 루프(spec→research→compose→verify→refine→cards)를 탄다.
예전엔 타 솔루션이 LLM 단발 호출로 갈라져 품질 루프가 a360에만 쌓였다.
"""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from app.schemas import Recommendation

from .. import config
from ..analysis import _format_document, _has_text, analyze, analyze_text
from ..catalog_context import A360, a360_context, user_catalog_context
from ..recommend.graph import generate_flow
from ..recommend.stream import emit, emit_analysis_frame
from ..verify.catalog import get_catalog
from ..verify.checker import run_environment_checks
from .catalog_parse import parse_catalog
from .foreign_catalog import CatalogSignal, detect_solution_name, verify as verify_catalog_signal
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


@dataclass(frozen=True)
class SignalOutcome:
    """타 솔루션 카탈로그 신호를 소비한 결과.

    셋을 분리하는 이유: "판정됐다"와 "세션을 바꾼다"와 "사용자에게 알린다"가 각각 다른
    조건에서 참이다. 확신이 낮으면 바꾸지 않고 알리기만 하고, 바꿨으면 알릴 필요가 없다.
    """

    notice: str | None = None    # 답변에 덧붙일 고지 (어휘를 못 바꿨을 때만)
    detected: str | None = None  # 백엔드가 세션 solution 확정에 쓸 이름
    switched: bool = False       # 이번 턴 어휘를 사용자 카탈로그로 바꿨는가


def apply_catalog_signal(state: TurnState) -> SignalOutcome:
    """intake 판정을 검증해 이번 턴 어휘와 세션 확정 신호를 정한다 (RPA-285).

    **어휘를 정하기 전에** 부른다. 예전에는 흐름도를 다 만든 뒤 감지해서, 판정이 맞아도
    이번 턴은 이미 A360으로 나가고 다음 턴부터 반영되는 2턴 지연이 있었다. 판정이 intake로
    올라온 지금은 같은 턴에 올바른 카탈로그를 집을 수 있다.

    이미 a360이 아닌 세션은 건드리지 않는다 — 사용자가 PATCH로 정했거나 이전 턴에 확정된
    값이 판정보다 우선한다(오탐이 사용자 선택을 덮어쓰면 되돌려도 다시 뒤집힌다).
    """
    if (state.get("solution") or A360) != A360:
        return SignalOutcome()

    raw = state.get("catalog_signal")
    if not raw:
        return SignalOutcome()

    sig = verify_catalog_signal(CatalogSignal(**raw), get_catalog())
    if not sig.found:
        return SignalOutcome()

    # 이름을 LLM이 못 밝혔으면 사용자 발화에서 한 번 더 찾고, 그래도 없으면 "other" —
    # 어느 솔루션인지 몰라도 "A360은 아니다"는 확정할 수 있다.
    name = sig.solution or detect_solution_name(state.get("message") or "") or "other"
    logger.info(
        "타 솔루션 카탈로그 신호 — solution=%s 표본 %d개 중 A360 실재 %d개 confirm=%s",
        name, sig.samples, sig.known, sig.confirm,
    )
    if not sig.confirm:
        # 확신이 낮거나 검증할 표본이 없다 — 어휘는 A360으로 두되 사실은 알린다.
        return SignalOutcome(notice=sig.notice())

    state["solution"] = name  # 이 턴의 resolve_catalog_context가 이 값을 본다
    return SignalOutcome(detected=name, switched=True)


async def _generate_with(state: TurnState, ctx, outcome: "SignalOutcome") -> dict:
    """v3 품질 루프 실행: spec 정형화 → recommend 파이프라인(generate_flow).

    진행 이벤트(spec/candidates/flow/scorecard)는 파이프라인이 직접 부모 그래프
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
    # 사용자가 타 솔루션 카탈로그를 줬는데 A360으로 만든 경우, 그 사실을 알린다 (RPA-285).
    # 조용히 넘어가면 사용자는 자기 카탈로그가 반영된 줄 안다 — 가장 나쁜 실패다.
    # (어휘를 실제로 바꾼 경우엔 outcome.notice가 None이라 고지가 붙지 않는다.)
    if outcome.notice:
        answer += "\n\n" + outcome.notice
    if outcome.detected:
        out["detected_solution"] = outcome.detected
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
    # 기본값이 `[]`가 아니라 `None`인 이유: 체커는 둘을 다르게 읽는다 — `[]`는 "파라미터
    # 없음 **확정**"이라 R2가 "스펙에 없는 파라미터"를 잡고, `None`은 "모름"이라 R2~R5를
    # 침묵한다(_check_parameters 주석). 카탈로그가 필수 인자만 적어 주는 경우가 흔한데
    # 그걸 '없음'으로 단정하면 멀쩡한 흐름도가 위반투성이가 된다.
    parameters: list[UserCatalogParam] | None = None

    def as_spec(self) -> dict:
        spec: dict = {
            "package": self.package,
            "action": self.action,
            "label": self.label or self.action,
        }
        # 모름이면 **키 자체를 빼야** 한다 — `_menu_block`의 `spec.get("parameters", [])`는
        # 키가 있고 값이 None이면 None을 그대로 돌려줘 순회에서 터진다.
        if self.parameters is not None:
            spec["parameters"] = [p.as_spec() for p in self.parameters]
        return spec


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


def _catalog_source_text(state: TurnState) -> str:
    """카탈로그가 있을 수 있는 **사용자 발화**만 모은다 (규칙 파서 입력).

    어시스턴트 발화를 빼는 이유: 우리가 답변에 나열한 액션 목록이 사용자 카탈로그와 섞이면
    엉뚱한 액션이 어휘로 들어온다. 압축본의 verbatim은 카탈로그 원문 보존용이라 포함한다.
    """
    parts: list[str] = []
    for block in ((state.get("compact") or {}).get("verbatim") or []):
        if isinstance(block, dict) and block.get("content"):
            parts.append(str(block["content"]))
    for turn in (state.get("history") or []):
        if turn.get("role") == "user" and turn.get("content"):
            parts.append(str(turn["content"]))
    parts.append(state.get("message") or "")
    return "\n".join(parts)


# 규칙 파싱을 채택하려면 intake 표본 중 이 비율 이상이 결과에 있어야 한다. 전부를 요구하면
# LLM이 표본 하나를 살짝 다르게 적기만 해도 빠른 길이 막히고, 하나만 요구하면 우연한 일치를
# 걸러내지 못한다.
_SIGNAL_AGREE_RATIO = 0.5


def _agrees_with_signal(parsed: list[dict], state: TurnState) -> bool:
    """규칙 파싱 결과가 intake의 판정 표본과 일치하는가 (RPA-285).

    ## 왜 필요한가

    규칙 파서의 위험은 "못 읽는 것"이 아니라 **"엉뚱한 걸 읽는 것"**이다. 못 읽으면 None을 내고
    LLM이 받지만, 카탈로그가 아닌 불릿 목록(할 일 메모·단계 나열)에서 그럴듯한 항목을 3개
    이상 긁어내면 **LLM을 부르지도 않고** 엉터리 어휘로 확정된다. 그 어휘로 만든 흐름도는
    R1이 전부 잡아내지만, 사용자는 왜 자기 카탈로그가 통째로 무시됐는지 알 수 없다.

    intake는 이미 "이게 카탈로그다"라고 판정하면서 `sample_actions` 표본을 함께 준다. 그
    표본이 규칙 결과에 없다면 **둘이 다른 것을 보고 있다**는 뜻이므로 규칙을 버린다.
    추가 비용은 0이다 — 이미 받아 둔 신호를 대조만 한다.

    표본이 없으면(구형 신호·LLM 생략) 대조할 근거가 없어 규칙을 그대로 채택한다 — 없는
    근거로 막으면 빠른 길이 영원히 닫힌다.
    """
    raw = state.get("catalog_signal") or {}
    samples = [s for s in (raw.get("sample_actions") or []) if isinstance(s, str) and s.strip()]
    if not samples:
        return True

    def _key(pkg: str, act: str) -> str:
        return f"{pkg}/{act}".strip().casefold()

    have = {_key(a.get("package", ""), a.get("action", "")) for a in parsed}
    have |= {(a.get("action") or "").strip().casefold() for a in parsed}  # 패키지 표기가 갈릴 수 있다
    hit = sum(1 for s in samples if s.strip().casefold() in have
              or s.rpartition("/")[2].strip().casefold() in have)
    ok = hit >= max(1, int(len(samples) * _SIGNAL_AGREE_RATIO))
    if not ok:
        logger.info("규칙 파싱을 버린다 — intake 표본 %d개 중 %d개만 일치(액션 %d개 파싱)",
                    len(samples), hit, len(parsed))
    return ok


def extract_user_catalog(state: TurnState) -> CatalogExtraction:
    """message + 이력 + 압축본(보존 원문 포함)에서 사용자 제공 카탈로그를 추출한다.

    카탈로그가 이전 턴이나 compact의 verbatim에 있었을 수 있어 셋을 모두 본다.

    **규칙 파서를 먼저 태운다.** LLM 재출력은 액션당 약 90토큰이 들어 규모가 곧 벽이다
    (실측: 448개 = 출력 4만 토큰 필요 → 796토큰에서 포기, 0개 반환). 형식이 규칙적인
    카탈로그는 `parse_catalog`가 LLM 없이 전량을 읽으므로 그 벽이 아예 없다. 규칙이
    형식을 못 알아보면 None을 내고, 그때만 LLM이 돈다.
    """
    parsed = parse_catalog(_catalog_source_text(state))
    if parsed and _agrees_with_signal(parsed, state):
        return CatalogExtraction(
            # 이름은 이미 intake 판정으로 세션에 확정돼 있다 — 규칙 파서는 표기만 읽는다.
            solution=(state.get("solution") if state.get("solution") != A360 else None),
            actions=[UserCatalogAction.model_validate(a) for a in parsed],
        )

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
    outcome = apply_catalog_signal(state)
    ctx = await resolve_catalog_context(state)
    if ctx is None:  # 타 솔루션인데 쓸 어휘가 없다 — 만들지 않고 되묻는다(type 정확성)
        out: dict = {"turn_type": TYPE_ANSWER, "answer": _NEED_CATALOG_ANSWER, "sources": []}
        # 어휘 추출에 실패해도 판정 자체는 유효하다 — 세션을 확정해 다음 턴이 되묻지 않게 한다.
        if outcome.detected:
            out["detected_solution"] = outcome.detected
        return out
    return await _generate_with(state, ctx, outcome)
