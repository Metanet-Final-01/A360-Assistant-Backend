# -*- coding: utf-8 -*-
"""챗 턴 SSE 재개 버퍼 — 새로고침해도 답변이 이어지게 (RPA-339).

## 무엇을 푸는가

답변 스트리밍 도중 새로고침하면 답변을 **영영 못 받았다**. SSE 연결이 끊기면 백엔드가
생성을 중단하고(`is_disconnected` → break), `done` 전이었으면 저장조차 하지 않아
사용자 질문까지 사라졌다. 원래는 의도된 절약(RPA-106 — 끊긴 클라이언트에 LLM 비용을 계속
쓰지 않는다)이었으나, "답변 유실"이 더 큰 손해라 트레이드오프를 뒤집었다.

## 왜 Redis인가 (프로세스 메모리로는 안 된다)

백엔드 ASG는 `MaxCapacity: 2`다. 새로고침한 요청은 **다른 인스턴스로 갈 수 있다** —
버퍼가 프로세스 메모리에 있으면 재구독이 빈손으로 돌아온다. 이벤트 버퍼는 인스턴스 간
공유가 **본질**이므로 Redis가 선택이 아니라 요건이다.

Redis **Stream**이 이 용도에 정확히 맞는다: `XADD`로 append, `XRANGE`로 밀린 구간 replay,
`XREAD BLOCK`으로 이어받기, 그리고 **엔트리 id가 곧 재개 커서**다(따로 시퀀스를 만들 필요 없음).

## 실패 정책 — 여기서 나는 예외가 턴을 죽이면 안 된다

이 모듈은 **부가 기능**이고 통지(=실제 답변 스트림)가 본체다. 그래서 모든 공개 함수는
예외를 삼키고 저하한다(로그만). Redis가 죽어도 사용자는 원래대로 답변을 받아야 한다 —
다만 그 턴은 재개가 안 될 뿐이다. `REDIS_URL` 미설정이면 전 기능 no-op이라 로컬·CI는
기존 동작 그대로다(이 모듈을 도입하기 전과 바이트 단위로 같은 경로).

## TTL

`TURN_MAX_DURATION_SEC` 기본이 900초라, 버퍼 TTL을 재개 창(10분)과 같게 두면 **긴 턴은
진행 중에 버퍼가 만료**된다. 그래서 `900 + 600`을 덮는 1800초로 잡는다 — 어떤 턴이든
완료 후 최소 10분의 재개 창이 남는다. 만료 뒤엔 `GET /chat-messages`로 복원한다(완료된
턴은 DB에 있다).
"""

import logging
import uuid
from typing import Any

from app.core import config

logger = logging.getLogger(__name__)

# 턴 상한(기본 900s) + 재개 창(600s)을 덮는다 — 긴 턴이 진행 중 만료되지 않게.
_TTL_SEC = 1800
_END_FIELD = "end"  # 종료 마커 엔트리 표식 — 팔로워가 이걸 보면 스트림을 닫는다
_SSE_FIELD = "sse"  # 원문 SSE 프레임을 그대로 보관한다(재생 시 바이트 동일)
_REPLAY_PAGE = 200  # replay 한 페이지 — 토큰 단위 버퍼라 전량 적재를 피한다

_client: Any = None
_client_url: str | None = None


def _redis_url() -> str:
    """호출 시점 읽기 — conftest가 빈 문자열로 격리한다(rag_cache와 같은 계약).

    ⚠️ `os.getenv` 직접 호출이 아니라 **설정 레지스트리를 경유**한다 (RPA-224). 새 파일이
    env를 직접 읽으면 `test_config_registry`의 래칫이 막는다 — 선언되지 않은 키가 조용히
    늘어나는 것을 방지하는 계약이다. `config.X`는 여전히 호출 시점 읽기라 테스트의
    monkeypatch가 그대로 듣는다.
    """
    return (config.REDIS_URL or "").strip()


def enabled() -> bool:
    """재개 기능이 켜져 있나 — `REDIS_URL` 미설정이면 전부 no-op(기존 동작 보존)."""
    return bool(_redis_url())


def _make_client(url: str) -> Any:
    """redis.asyncio 클라이언트 — 테스트가 fakeredis로 갈아끼우는 팩토리(rag_cache 선례).

    ⚠️ **async 클라이언트**를 쓴다. 이 버퍼는 토큰 단위로 append되므로, 동기 클라이언트를
    async 제너레이터 안에서 부르면 토큰마다 이벤트 루프가 멈춘다(또는 스레드 홉 비용).
    """
    import redis.asyncio as aioredis  # noqa: PLC0415 — 미설정 모드는 redis를 안 탄다

    return aioredis.from_url(url, decode_responses=True)


def _get_client() -> Any | None:
    """프로세스 공용 클라이언트. URL이 바뀌면 새로 만든다(테스트가 켜고 끈다)."""
    global _client, _client_url
    url = _redis_url()
    if not url:
        return None
    if _client is None or _client_url != url:
        try:
            _client = _make_client(url)
        except Exception:  # noqa: BLE001 — 클라이언트 생성 실패는 기능 비활성으로 저하
            logger.warning("turn_stream Redis 클라이언트 생성 실패 — 재개 비활성", exc_info=True)
            _client, _client_url = None, None
            return None
        _client_url = url
    return _client


def reset_client() -> None:
    """테스트 격리용 — 다음 호출이 클라이언트를 새로 만들게 한다."""
    global _client, _client_url
    _client, _client_url = None, None


def _ns() -> str:
    env = (config.APP_ENV or "development").strip() or "development"
    return f"a360:{env}:turn:v1"


def _events_key(session_id: str, turn_id: str) -> str:
    """이벤트 버퍼 키 — **세션 네임스페이스 안**에 둔다.

    🔴 turn_id만으로 키를 만들면, 재구독 엔드포인트가 세션 소유권을 검사해도 **남의 세션
    turn_id를 자기 session_id로 구독**할 수 있다(검사한 것과 읽는 것이 갈린다). 세션을 키에
    넣으면 그 불일치가 구조적으로 불가능해진다 — 다른 세션의 turn_id는 여기서 존재하지 않는다.
    """
    return f"{_ns()}:session:{session_id}:turn:{turn_id}:events"


def _active_key(session_id: str) -> str:
    return f"{_ns()}:session:{session_id}:active"


def new_turn_id() -> str:
    return uuid.uuid4().hex


async def claim(session_id: str, turn_id: str) -> tuple[bool, str | None]:
    """활성 턴으로 선점한다. `(버퍼 사용가능, 이미 도는 turn_id)`를 돌려준다.

    - `(True, None)`  선점 성공 — 버퍼가 살아 있다 → 재개 모드로 가도 된다
    - `(True, "xyz")` 이미 다른 턴이 돈다 → 호출부가 409로 재구독을 유도
    - `(False, None)` Redis 미설정·장애 → **재개 불가**. 호출부는 기존 절약 정책으로 돌아가야 한다

    🔴 `enabled()`(설정 여부)만으로 재개 모드를 켜면 안 된다 (Qodo #455): `REDIS_URL`은 있는데
    Redis가 죽은 경우, 끊긴 뒤에도 계속 생성해 **비용만 쓰고 재개도 안 된다**(양쪽 다 손해).
    이 함수는 턴의 첫 Redis 작업이라 실제 가용성 프로브를 겸한다 — 설정이 아니라 **동작**으로 판단한다.

    중복 차단 자체는 부가 기능이라, 장애 시엔 턴을 막지 않고 그냥 재개만 포기한다.
    """
    r = _get_client()
    if r is None:
        return (False, None)
    try:
        ok = await r.set(_active_key(session_id), turn_id, nx=True, ex=_TTL_SEC)
        if ok:
            return (True, None)
        existing = await r.get(_active_key(session_id))
        return (True, existing or None)
    except Exception:  # noqa: BLE001 — 선점 실패가 턴을 막으면 안 된다(재개만 포기)
        logger.warning("turn_stream 선점 실패 — 재개 비활성: session=%s", session_id, exc_info=True)
        return (False, None)


async def publish(session_id: str, turn_id: str, sse_text: str) -> None:
    """SSE 프레임 원문을 버퍼에 append한다(+TTL 갱신, 1 RTT).

    원문을 그대로 넣는 이유: 재생 시 가공 없이 그대로 흘리면 **끊긴 적 없는 것과 동일한
    바이트**가 나간다. 이벤트를 파싱·재조립하면 그 지점이 새 버그 표면이 된다.
    """
    r = _get_client()
    if r is None:
        return
    try:
        key = _events_key(session_id, turn_id)
        async with r.pipeline(transaction=False) as p:
            p.xadd(key, {_SSE_FIELD: sse_text, _END_FIELD: "0"})
            p.expire(key, _TTL_SEC)
            await p.execute()
    except Exception:  # noqa: BLE001 — 버퍼 실패가 실제 스트림을 죽이면 안 된다
        logger.warning("turn_stream publish 실패 (무시): turn=%s", turn_id, exc_info=True)


async def close(session_id: str, turn_id: str) -> None:
    """종료 마커를 남기고 활성 표식을 푼다 — 팔로워가 이 마커를 보면 스트림을 닫는다.

    활성 키는 **내 turn_id일 때만** 지운다(다른 턴이 이미 선점했으면 건드리지 않는다).
    """
    r = _get_client()
    if r is None:
        return
    try:
        key = _events_key(session_id, turn_id)
        async with r.pipeline(transaction=False) as p:
            p.xadd(key, {_SSE_FIELD: "", _END_FIELD: "1"})
            p.expire(key, _TTL_SEC)
            await p.execute()
        if await r.get(_active_key(session_id)) == turn_id:
            await r.delete(_active_key(session_id))
    except Exception:  # noqa: BLE001
        logger.warning("turn_stream close 실패 (무시): turn=%s", turn_id, exc_info=True)


def _rows(entries: Any) -> list[tuple[str, str, bool]]:
    """XRANGE/XREAD 응답 → [(entry_id, sse_text, is_end)]."""
    out: list[tuple[str, str, bool]] = []
    for entry_id, fields in entries or []:
        out.append((entry_id, fields.get(_SSE_FIELD, ""), fields.get(_END_FIELD) == "1"))
    return out


async def replay(session_id: str, turn_id: str, after: str | None):
    """`after` **다음**부터 쌓인 것을 **페이지 단위로** 흘린다(재구독 시 따라잡기용).

    `after`가 없으면 처음부터 — 새로고침 후 프론트가 마지막으로 본 id를 모를 때 화면을
    통째로 다시 그릴 수 있다.

    ⚠️ 한 번에 다 읽지 않는다 (Qodo #455). 버퍼는 **토큰 단위**로 쌓이므로 긴 턴이면 엔트리가
    수천 개다 — 전량을 리스트로 만들면 첫 바이트까지 지연되고 메모리도 튄다. 페이지로 끊어
    호출부가 받는 즉시 흘려보내게 한다.
    """
    r = _get_client()
    if r is None:
        return
    key = _events_key(session_id, turn_id)
    cursor = after
    while True:
        try:
            min_ = f"({cursor}" if cursor else "-"
            rows = _rows(await r.xrange(key, min=min_, count=_REPLAY_PAGE))
        except Exception:  # noqa: BLE001
            logger.warning("turn_stream replay 실패: turn=%s", turn_id, exc_info=True)
            return
        if not rows:
            return
        for row in rows:
            yield row
        if len(rows) < _REPLAY_PAGE:
            return
        cursor = rows[-1][0]


async def follow(session_id: str, turn_id: str, after: str, block_ms: int) -> list[tuple[str, str, bool]]:
    """`after` 이후 새 엔트리를 기다린다(최대 block_ms). 없으면 빈 리스트.

    호출부가 이 함수를 반복 호출하는 구조라, 짧은 block을 여러 번 도는 편이 취소(클라이언트가
    또 끊김)에 빠르게 반응한다 — 한 번에 길게 막으면 그동안 응답이 없다.
    """
    r = _get_client()
    if r is None:
        return []
    try:
        res = await r.xread({_events_key(session_id, turn_id): after}, count=200, block=block_ms)
        if not res:
            return []
        return _rows(res[0][1])
    except Exception:  # noqa: BLE001
        logger.warning("turn_stream follow 실패: turn=%s", turn_id, exc_info=True)
        return []


async def exists(session_id: str, turn_id: str) -> bool:
    """버퍼가 아직 살아 있나 — 만료·오타 turn_id를 404로 가르기 위해."""
    r = _get_client()
    if r is None:
        return False
    try:
        return bool(await r.exists(_events_key(session_id, turn_id)))
    except Exception:  # noqa: BLE001
        logger.warning("turn_stream exists 실패: turn=%s", turn_id, exc_info=True)
        return False
