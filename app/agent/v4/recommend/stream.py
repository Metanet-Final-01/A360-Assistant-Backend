"""진행 이벤트 방출 헬퍼.

노드는 emit()로 ProgressEvent 형태의 dict를 흘려보내고, recommend() 진입점이
astream(stream_mode="custom")으로 받아 ProgressEvent로 감싼다. 스트리밍 컨텍스트
밖(단위 테스트에서 노드를 직접 호출하는 등)에서는 get_stream_writer()가 없으므로
조용히 무시한다 — 노드 로직이 스트리밍 유무에 의존하지 않게 한다.
"""


import logging

logger = logging.getLogger(__name__)


def emit(payload: dict) -> None:
    """ProgressEvent-형 dict를 스트림에 방출한다 (컨텍스트 없으면 no-op)."""
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()(payload)
    except Exception:  # noqa: BLE001 — 스트림 컨텍스트 밖에서는 무시
        logger.debug("emit 무시됨 (스트림 컨텍스트 없음 또는 실패): %s", payload)


def emit_flow_frame(
    flow: dict, violations: list[dict] | None, caption: str, active_step_id: str | None = None
) -> None:
    """진행 중 흐름도 스냅샷을 partial 이벤트로 흘려보낸다 — 프론트 라이브 렌더용(스트리밍 흐름도).

    stage(상태 텍스트)와 달리 partial은 '중간 산출물'이라 data에 흐름도 트리를 통째로 싣는다.
    프론트는 프레임마다 트리를 다시 그려, 초안 → 검수(위반 노드 강조) → 최종으로 흐름도가
    자라나는 과정을 보여준다. 생성(recommend 그래프)과 수정(edit_node)이 공유한다.
    위반은 관측 이벤트와 같은 7필드로 축약한다(step_id + 스텝 내 location으로 노드 매칭).
    active_step_id를 주면 '지금 이 단계를 수정 중'이라는 뜻 — 프론트가 그 단계 박스를 붉게
    강조·깜빡이고 그 위치로 스크롤한다(어떤 액션이 수정 중인지 사용자에게 보이게)."""
    emit({
        "event": "partial",
        "stage": "recommending",
        "message": caption,
        "data": {
            "kind": "flow",
            "caption": caption,
            "flow": flow,
            "active_step_id": active_step_id,
            "violations": [
                {k: v.get(k) for k in ("rule", "location", "message", "step_id", "package", "action", "param")}
                for v in (violations or [])
            ],
        },
    })


def emit_draft_frame(
    flow: dict, violations: list[dict] | None, draft_id: str, caption: str
) -> None:
    """확정된 초안(정밀화 전)을 partial(kind="draft")로 흘린다 — 2상 구조의 1상 종료 신호 (설계 §6.3).

    **새 event 값을 만들지 않는 이유**: 프론트는 모르는 `data.kind`를 무시하므로 kind 추가는
    FE 무변경으로 안전하지만, 새 event는 `ProgressEvent`의 Literal과 FE 분기를 둘 다 고쳐야
    한다. 그래서 채널은 partial 그대로 두고 kind로만 갈랐다.

    싣는 위반은 **결정론 검사 결과뿐이고 교정하지 않은 상태**다(설계 §5-I) — 초안 단계에서
    교정을 돌리면 초안 지연이 정밀화 지연만큼 늘어 2상으로 쪼갠 의미가 사라진다.

    draft_id는 이 초안의 식별자다. 후속 이슈에서 정밀화가 백그라운드로 빠지면 프론트가
    "이 초안의 정밀화 결과"를 이 id로 잇는다 — 지금은 같은 스트림 안이라 참조용이다.
    """
    emit({
        "event": "partial",
        "stage": "recommending",
        "message": caption,
        "data": {
            "kind": "draft",
            "caption": caption,
            "draft_id": draft_id,
            "flow": flow,
            "violations": [
                {k: v.get(k) for k in ("rule", "location", "message", "step_id", "package", "action", "param")}
                for v in (violations or [])
            ],
        },
    })


def emit_refine_frame(
    status: str,
    draft_id: str,
    caption: str,
    *,
    reason: str | None = None,
    locked: bool = False,
    elapsed_ms: int | None = None,
) -> None:
    """2상 정밀화의 진행/종료를 partial(kind="refine")로 흘린다 (설계 §6.3).

    emit_draft_frame과 짝이다: draft가 "초안 나왔다", refine이 "그 초안을 지금 다듬는
    중이고 그동안 수정이 잠긴다 / 다 됐다 / 이런 사유로 접었다"를 말한다. 채널을 새
    event가 아닌 partial+kind로 가른 이유는 emit_draft_frame의 docstring과 같다.

    - status  : state.py의 REFINE_* (running|done|cancelled|timeout|failed|superseded)
    - locked  : 지금 이 세션의 수정이 잠겨 있는가. **status=running이어도 False일 수 있다**
                — 세션 밖 실행(평가 스크립트·단독 recommend)은 잠글 대상이 없다.
    - reason  : 정상 완료가 아닐 때 사용자에게 보일 사유. 잠금은 이미 풀린 상태다.

    프론트가 status=running·locked=true를 보면 편집 UI를 잠그고 "정밀화 중단하고 지금
    초안으로 수정하기"(POST /api/sessions/{id}/refine/cancel)를 노출하면 된다.
    """
    emit({
        "event": "partial",
        "stage": "verifying",
        "message": caption,
        "data": {
            "kind": "refine",
            "caption": caption,
            "draft_id": draft_id,
            "status": status,
            "locked": locked,
            "reason": reason,
            "elapsed_ms": elapsed_ms,
        },
    })


def emit_candidates_frame(candidates: list[dict], caption: str) -> None:
    """후보 진행 요약 카드를 partial(kind="candidates")로 흘린다 (v3).

    후보별 **전체 트리는 싣지 않는다** — 두 트리가 동시에 자라는 화면은 소음이고, 탈락
    후보에 시각적 애착이 생기면 심판 결과가 배신처럼 보인다. 전략 이름·상태·단계/액션
    수 카운터만 싣고, 트리 라이브 렌더(kind="flow")는 승자 확정 이후부터 시작한다.
    candidates 원소: {id, persona, status: composing|verifying|done|failed, steps, actions}.
    """
    emit({
        "event": "partial",
        "stage": "recommending",
        "message": caption,
        "data": {"kind": "candidates", "caption": caption, "candidates": candidates},
    })


def emit_verdict_frame(verdict: dict, caption: str) -> None:
    """심판 결과(루브릭 점수판+선정 이유)를 partial(kind="verdict")로 흘린다 (v3)."""
    emit({
        "event": "partial",
        "stage": "verifying",
        "message": caption,
        "data": {"kind": "verdict", "caption": caption, "verdict": verdict},
    })


def emit_scorecard_frame(scorecard: dict, caption: str) -> None:
    """검증 현황(must 커버리지·위반·시뮬레이션)을 partial(kind="scorecard")로 흘린다 (v3).

    scorecard: {must_coverage: float, blockers: int, warnings: int, sim_pass_rate: float|None,
                cards: int} — 프론트가 게이지/카운터로 렌더한다.
    """
    emit({
        "event": "partial",
        "stage": "verifying",
        "message": caption,
        "data": {"kind": "scorecard", "caption": caption, "scorecard": scorecard},
    })


def emit_analysis_frame(analysis: dict, caption: str) -> None:
    """진행 중 업무 분석 스냅샷을 partial 이벤트로 흘려보낸다 — 분석 결과 라이브 렌더용.

    흐름도 프레임(kind="flow")과 같은 partial 채널을 쓰되 kind="analysis"로 구분한다.
    프론트(업로드 패널)가 프레임마다 요약·단계를 다시 그려, 분석 단계가 하나씩 채워지는
    과정을 실시간으로 보여준다(분석도 스트리밍). analysis는 AnalysisResult.model_dump() 형태."""
    emit({
        "event": "partial",
        "stage": "analyzing",
        "message": caption,
        "data": {"kind": "analysis", "caption": caption, "analysis": analysis},
    })
