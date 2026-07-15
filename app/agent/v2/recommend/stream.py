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
