"""진행 이벤트 방출 헬퍼.

노드는 emit()로 ProgressEvent 형태의 dict를 흘려보내고, recommend() 진입점이
astream(stream_mode="custom")으로 받아 ProgressEvent로 감싼다. 스트리밍 컨텍스트
밖(단위 테스트에서 노드를 직접 호출하는 등)에서는 get_stream_writer()가 없으므로
조용히 무시한다 — 노드 로직이 스트리밍 유무에 의존하지 않게 한다.
"""


def emit(payload: dict) -> None:
    """ProgressEvent-형 dict를 스트림에 방출한다 (컨텍스트 없으면 no-op)."""
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()(payload)
    except Exception:  # noqa: BLE001 — 스트림 컨텍스트 밖에서는 무시
        pass
