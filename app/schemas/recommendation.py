"""A360 작업 추천안 스키마 (FR-09~12) — 최종 내보내기(FR-17)와 골드셋 채점의 대상.

RecommendationVersion.payload(JSONB)에 이 형태로 저장된다.
package/action 이름은 RAG 카탈로그(docs/RAG_CATALOG.md)의 표기를 따른다
(예: package="Excel_MS", action="GoToCell").
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ActionParameter(BaseModel):
    """액션 입력 파라미터. 카탈로그의 파라미터 스키마(name/type/required/options)를 따른다."""

    name: str = Field(description="카탈로그 파라미터 name, 예: 'cellOption'")
    label: str | None = Field(None, description="사람용 라벨, 예: '셀 옵션'")
    value: Any = None
    value_source: Literal["schema_default", "llm", "user"] = Field(
        "llm", description="값의 출처 — 기본값 그대로/LLM 추론/사용자 지정"
    )


class VarRef(BaseModel):
    """액션↔변수 연결 한 건 (v3 데이터플로우 검증 R9~R11의 원료).

    실봇 JSON의 returnTo(생산)·VARIABLE attribute/`$var$` 보간(소비)에 대응한다.
    composer가 명시 출력하는 것이 1차이고, 검증기가 `$var$` 파싱으로 교차 보정한다.
    미기재 시 데이터플로우 검사가 침묵할 뿐 오탐은 없다(하위호환).
    """

    name: str = Field(description="BotVariable.name 참조")
    role: str | None = Field(None, description="'session'|'data'|'counter' 등 용도 힌트")


class RagSource(BaseModel):
    """추천 근거가 된 RAG 문서 참조 (FR-11)."""

    source_type: str = Field(description="doc_page|action_schema|package_overview|bot_example")
    title: str
    url: str | None = None
    score: float | None = Field(None, description="검색 유사도")


class RecommendedAction(BaseModel):
    """추천된 A360 액션 하나 — A360 봇 JSON의 노드와 동일한 재귀 트리 구조.

    Loop·If·Else If·Else·Step·Error handler 같은 컨테이너 액션은 본문을
    children에 담는다. A360에는 임의의 병합점이 없다: 분기(If/Else) 블록이
    끝나면 실행은 "다음 형제 액션"으로 이어진다 — 그것이 병합이다.

    예) If(조건) [children: 참일 때 액션들] → Else [children: ...] → 다음 형제 = 병합 지점
    예) Loop(3일치 반복) [children: 반복 본문]
    """

    order: int
    package: str = Field(description="예: 'Excel_MS'")
    action: str = Field(description="예: 'GoToCell'")
    # ⚠️ Optional + default 고정. v1~v3가 같은 스키마로 자기 산출물을 검증하므로 필수화하면
    # req_id를 안 내는 구버전 출력이 통째로 검증 거부된다(버전 비교 셀렉터가 깨진다).
    req_id: str | None = Field(
        None, description="이 액션이 담당하는 FlowSpec 요구 id — 누락 추적의 앵커"
    )
    label: str | None = Field(None, description="사람용 라벨, 예: '셀로 이동'")
    parameters: list[ActionParameter] = Field(default_factory=list)
    children: list["RecommendedAction"] = Field(
        default_factory=list, description="컨테이너 액션(Loop/If/Step 등)의 본문"
    )
    rationale: str | None = Field(None, description="왜 이 액션인지 (FR-11)")
    sources: list[RagSource] = Field(default_factory=list)
    confidence: float | None = Field(None, ge=0.0, le=1.0, description="FR-12 신뢰도")
    produces: list[VarRef] = Field(
        default_factory=list, description="이 액션이 쓰기(할당)하는 변수 — 실봇 returnTo에 대응 (v3)"
    )
    consumes: list[VarRef] = Field(
        default_factory=list, description="이 액션이 읽는 변수 — 파라미터의 $var$ 참조 포함 (v3)"
    )


class StepRecommendation(BaseModel):
    """추천 흐름도의 한 단계(액션 묶음). 에이전트가 업무를 재구성해 만든 자기완결적 단위다.

    step_id는 흐름도 내부의 지역 식별자다 — 더는 AnalysisResult.steps[].step_id를 참조하지 않는다
    (에이전트가 분석 단계를 합치거나 쪼갠다). label/description으로 이 단계가 스스로를 설명하므로
    흐름도만으로 렌더할 수 있다.

    ⚠️ label/description은 반드시 선택(str|None). 필수로 하면 agent 검수 하네스의 국소 교정 중간
    산출물(label 누락)이 Recommendation 검증에 걸려 교정 루프가 무력화된다(정준환 실측 회귀).
    최종 저장·렌더 직전 agent `_coerce_flow`가 label을 step_id로 폴백해 채운다 — 스키마는 관대하게,
    실제 데이터엔 항상 존재.
    """

    step_id: str = Field(description="흐름도 내부 지역 id, 예: 'step-1'")
    label: str | None = Field(None, description="단계 제목(사람이 읽는)")
    description: str | None = Field(None, description="이 단계가 무엇을 하는지 한 줄 설명")
    actions: list[RecommendedAction]


class BotVariable(BaseModel):
    """봇 입출력/내부 변수 (FR-10의 '입력/출력 변수')."""

    name: str
    type: str = Field("STRING", description="A360 변수 타입: STRING|NUMBER|BOOLEAN|TABLE|SESSION 등")
    direction: Literal["input", "output", "local"] = "local"
    description: str | None = None


class CardTarget(BaseModel):
    """질문 카드가 채울 위치 — fill_cards가 이 좌표로 set_params EditOps를 결정론 생성한다."""

    step_id: str
    node_path: str = Field(description="단계 내 트리 경로, 예: 'actions[0].children[1]'")
    param_name: str


class QuestionCard(BaseModel):
    """미확정 항목을 사용자에게 묻는 1급 산출물 (v3 — R3·모호성·전제 확인의 승격).

    흐름도는 항상 완성 상태로 출고된다: 카드는 '빈칸'이 아니라 default(시안값)가
    채워진 확인 요청이 기본이고, 진짜 빈칸은 kind="missing_param"뿐이다.
    """

    card_id: str
    kind: Literal["missing_param", "ambiguity", "assumption_confirm"]
    question: str = Field(description="사용자에게 보일 질문")
    why: str | None = Field(None, description="왜 이 값이 필요한지")
    targets: list[CardTarget] = Field(default_factory=list)
    input_type: Literal["text", "number", "select", "file_path", "credential_ref", "confirm"] = "text"
    options: list[Any] | None = Field(None, description="select일 때 — 카탈로그 enum에서 결정론 추출")
    default: Any = Field(None, description="시안값 — 사용자가 승인만 해도 되게")
    blocking: bool = Field(False, description="미해결 시 봇이 아예 실행 불가한가")
    resolved: bool = Field(False, description="fill_cards로 해소되었는가")


class SpecRequirement(BaseModel):
    """FlowSpec의 요구사항 한 줄 — req_id가 L2 커버리지·심판·질문 카드의 공유 앵커다."""

    req_id: str = Field(description="예: 'req-1'")
    text: str
    priority: Literal["must", "should"] = "must"
    source: Literal["doc", "chat", "inferred"] = "chat"
    # 이 요구가 업무 분석의 어느 단계에서 왔나. must 요구의 **입도를 고정**하는 앵커다
    # (RPA-298): 같은 문서로 3회 실행했더니 분석은 매번 7단계로 같은데 must 요구가
    # 5·7·5로 갈렸고, 그러면 must_coverage의 **분모**가 달라져 실행 간 점수 비교가
    # 성립하지 않는다(심판 결정론 점수의 50%, 하드 게이트, flow_confidence가 전부 이 값을 탄다).
    # should·접착제 요구는 단계에 매이지 않으므로 None이다.
    step_id: str | None = Field(None, description="대응하는 업무 분석 단계 id (must 요구만)")


class SpecUnknown(BaseModel):
    """spec 단계에서 수집한 미확정 사항 — finalize에서 질문 카드로 전환된다."""

    what: str
    why_needed: str | None = None
    blocking: bool = False


class FlowSpec(BaseModel):
    """요구사항 정형화 산출물 (v3) — 시맨틱 검증(L2)·심판·시뮬레이션의 채점 기준 문서.

    recommendation과 함께 저장·재주입되어 이후 edit의 재채점도 원래 요구 기준으로 한다.
    """

    goal: str = ""
    requirements: list[SpecRequirement] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list, description="필요한 입력(파일·시스템·데이터)")
    outputs: list[str] = Field(default_factory=list, description="기대 산출물")
    error_policy: list[str] = Field(default_factory=list, description="예외 상황별 기대 처리")
    unknowns: list[SpecUnknown] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list, description="생성이 임의로 정한 전제(명시 강제)")
    constraints: list[str] = Field(
        default_factory=list,
        description="문서나 사용자 발화에 명시된 자동화 제약·필수 조건",
    )


class TriggerRecommendation(BaseModel):
    """이 자동화를 '언제 실행할지' 제안 (A-2) — 실봇 JSON triggers[]·Control Room 스케줄에 대응.

    업무정의서의 시점 표현("매일 아침", "메일이 오면", "폴더에 파일이 생기면")을 실행 방식
    제안으로 잇는다. kind="trigger"는 트리거 패키지(무인 실행·Public 체크인 전제),
    kind="schedule"은 Control Room > Activity 예약 실행이다.
    """

    kind: Literal["trigger", "schedule"] = "trigger"
    package: str | None = Field(None, description="트리거 패키지명 (예: 'Email trigger') — schedule이면 None")
    title: str = Field(description="트리거/스케줄 이름 (사람이 읽는)")
    reason: str | None = Field(None, description="업무정의서의 어떤 표현이 근거인지")
    setup_hint: str | None = Field(None, description="설정 방법 한 줄 요약")
    sources: list[RagSource] = Field(default_factory=list)


class BotMeta(BaseModel):
    """봇 저장 메타 — 사람이 Control Room에 옮길 때 **첫 화면**에서 요구받는 항목 (설계 제약 #15).

    ⚠️ **골드셋으로 채점되지 않는다.** 정답 봇 JSON에 이 정보가 없기 때문이다 — 최상위 키가
    `breakpoints/nodes/packages/triggers/variables/workItemTemplateName`뿐이고, 이름은
    파일명에서 오고 폴더는 Control Room이 저장할 때 붙인다(실측). 공식 문서 코퍼스에도
    "봇을 어떻게 이름 짓고 어디 두는가"를 다루는 페이지가 없다(검색 결과 릴리스 노트뿐).
    그래서 이 필드들의 목적은 점수가 아니라 **비전문가가 옮길 때 빈칸 앞에서 멈추지 않는 것**이다.

    각 필드의 출처를 의도적으로 갈랐다 — 근거 없는 값을 지어내지 않기 위해서다(설계 §5.2-G):
      - `name`   — 에이전트가 업무 목표에서 **제안**한다. 사람이 바꿔도 그만인 값이라
                   지어내도 손해가 없는 유일한 항목이다.
      - `folder` — **자리표시자만.** 사용자 작업공간 경로는 업무 데이터지 동작 옵션이
                   아니다(제약 #10). 추측하면 존재하지 않는 경로를 확신 있게 적게 된다.
      - `target_os` / `run_mode` — **결정론.** LLM에 묻지 않는다. 각각 검수기의
                   `target_os(flow)`(R16)와 트리거 유무(R15)가 이미 내리는 판단이라,
                   여기서 따로 판단하면 경고와 출력이 어긋난다.
    """

    name: str | None = Field(None, description="제안 봇 이름 — 업무 목표에서 유도 (사람이 바꿔도 됨)")
    folder: str | None = Field(
        None, description="저장 폴더. 사용자 작업공간 경로라 에이전트는 자리표시자만 남긴다"
    )
    target_os: Literal["windows", "macos"] | None = Field(
        None, description="대상 러너 OS — spec.assumptions에서 결정론으로 읽는다 (R16과 같은 출처)"
    )
    run_mode: Literal["attended", "unattended"] | None = Field(
        None, description="트리거가 붙으면 unattended, 없으면 attended (R15와 같은 판단)"
    )


class Recommendation(BaseModel):
    """추천안 전체 — 이 JSON이 최종 내보내기 형식이자 골드셋 채점 대상이다."""

    schema_version: str = "1.0"
    steps: list[StepRecommendation]
    bot_meta: BotMeta | None = Field(
        None, description="봇 저장 메타(이름·폴더·OS·실행 방식) — 제약 #15. 채점 대상 아님"
    )
    variables: list[BotVariable] = Field(default_factory=list)
    notes: str | None = Field(None, description="전제·주의사항, 예: 'Knox 메일은 Email 패키지 기준'")
    trigger: TriggerRecommendation | None = Field(
        None, description="실행 시점 제안 (A-2) — 시점 의도가 없으면 None"
    )
    needs_input: list[QuestionCard] = Field(
        default_factory=list, description="사용자 입력 대기 질문 카드 (v3)"
    )
    flow_confidence: float | None = Field(
        None, ge=0.0, le=1.0, description="흐름도 수준 신뢰도 — must 커버리지×blocker×시뮬레이션 (v3)"
    )
    spec: FlowSpec | None = Field(None, description="이 흐름도의 채점 기준이 된 FlowSpec (v3)")

    def iter_actions(self):
        """트리를 평탄화해 모든 액션을 순회 (골드셋 채점·검증용)."""

        def walk(actions: list[RecommendedAction]):
            for a in actions:
                yield a
                yield from walk(a.children)

        for step in self.steps:
            yield from walk(step.actions)


RecommendedAction.model_rebuild()
