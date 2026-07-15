"""오케스트레이터 그래프 상태와 라우트 상수 (RPA-65).

턴 하나가 그래프를 흐르는 동안의 모든 입출력이 이 상태에 담긴다.
turn_type은 어느 브랜치 노드가 실행됐는지로 결정론적으로 찍힌다(LLM 출력 아님) —
백엔드가 이 값으로 저장을 분기하므로(RPA-64 계약) "수행한 작업 ≠ type" 불일치가
구조적으로 발생하지 않는 것이 핵심 불변식이다.
"""

from typing import TypedDict

# intake 분류 결과이자 그래프 분기 키.
ROUTE_ANALYZE = "analyze"
ROUTE_GENERATE = "generate"
ROUTE_EDIT = "edit"
ROUTE_QA = "qa"
ROUTES = (ROUTE_ANALYZE, ROUTE_GENERATE, ROUTE_EDIT, ROUTE_QA)

# 백엔드 저장 분기용 반환 type (RPA-64 계약 + compact 확장 제안).
TYPE_ANSWER = "answer"
TYPE_ANALYSIS = "analysis"
TYPE_RECOMMENDATION = "recommendation"
TYPE_COMPACT = "compact"


class TurnState(TypedDict, total=False):
    """그래프 최상위 상태.

    입력 블록은 stream_agent_turn이 백엔드 context에서 채운다. operation/compact는
    백엔드 계약 확장 제안분 — 안 오면 각각 "chat"/None으로 동작해 하위호환된다.
    """

    # --- 입력 (백엔드 context) ---
    message: str
    solution: str  # "a360" | ... — generate/edit의 카탈로그 소스를 가르는 세션 확정 키
    operation: str  # "chat" | "compact" — compact 버튼은 라우터를 우회한다
    history: list[dict]  # [{"role", "content"}] — compact 시점 이후의 대화만
    compact: dict | None  # 이전 압축본 (CompactContext.model_dump())
    analysis: dict | None  # 최신 분석본 (AnalysisResult.model_dump())
    recommendation: dict | None  # 최신 흐름도 트리 (Recommendation.model_dump())
    parsed_doc: dict | None  # 최신 문서 파싱본

    # --- intake 산출 (v3: TaskPlan) ---
    route: str  # 첫 task (관측·하위호환용)
    route_reason: str  # 분류 근거 (로깅·디버그용)
    plan: list[str]  # 순서 있는 task 목록 (ROUTES 원소들) — supervisor가 결정론 순회
    current_task: str  # supervisor가 방금 디스패치한 task (artifact 스냅샷 키)
    next_node: str  # supervisor 조건부 엣지 키 (내부용)
    artifacts: list[dict]  # task별 산출 스냅샷 [{task, type, answer, ...}] — done에 동봉
    card_values: dict  # operation="fill_cards"의 카드 응답 {card_id: value}

    # --- 최종 산출 (stream_agent_turn이 done data로 조립) ---
    turn_type: str  # TYPE_* — 실행된 브랜치 노드가 찍는다
    answer: str
    sources: list[dict]  # RagSource 형태 dict
    analysis_out: dict | None  # 이번 턴에 새로 만든 분석본
    recommendation_out: dict | None  # 이번 턴에 만든/수정한 흐름도
    change_summary: str | None
    compact_out: dict | None
    violations: list[dict]  # 검수 후에도 남은 위반 (프론트 경고 표시용)
