"""오케스트레이터 그래프 상태와 라우트 상수 (RPA-65).

턴 하나가 그래프를 흐르는 동안의 모든 입출력이 이 상태에 담긴다.
turn_type은 어느 브랜치 노드가 실행됐는지로 결정론적으로 찍힌다(LLM 출력 아님) —
백엔드가 이 값으로 저장을 분기하므로(RPA-64 계약) "수행한 작업 ≠ type" 불일치가
구조적으로 발생하지 않는 것이 핵심 불변식이다.

여기에 2상 구조(설계 §6.3)의 **세션 범위 정밀화 잠금 레지스트리**도 함께 둔다.
그래프 상태(턴 1회)보다 수명이 긴 유일한 상태라 자리가 어색해 보이지만, 이 모듈을
고른 이유가 있다: 잠금은 에이전트(정밀화를 도는 쪽)와 백엔드 API(수정을 막는 쪽)가
**둘 다** 봐야 하는데, state.py는 v4에서 유일하게 무거운 의존(langchain·프롬프트 파일
읽기)이 없는 리프 모듈이라 `app/api/sessions.py`가 싸게 들여다볼 수 있다.
"""

import contextlib
import contextvars
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TypedDict

logger = logging.getLogger(__name__)

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
    # 대화에서 타 솔루션 카탈로그가 확인되면 그 이름 — 백엔드가 세션 solution 확정에 쓴다
    # (RPA-285 2단계). 감지 없으면 부재.
    detected_solution: str | None
    violations: list[dict]  # 검수 후에도 남은 위반 (프론트 경고 표시용)
    # 2상 정밀화 결과 요약 (설계 §6.3) — {status, reason, draft_id, elapsed_ms, locked}.
    # TypedDict에 선언하지 않으면 노드가 이 키를 반환할 때 langgraph가 채널을 못 찾는다.
    # ⚠️ orchestrator/graph.py의 `_done_data`가 아직 이 값을 done으로 올리지 않는다 —
    #    프론트에 필요하면 그쪽에 한 줄 추가해야 한다(이번 배정 밖 파일).
    refine_status: dict | None


# ─────────────────────────────────────────────────────────────────────────────
# 2상 정밀화 잠금 — 세션 범위 (설계 §6.3 "동시 수정: 정밀화 끝날 때까지 수정 잠금")
#
# ⚠️ **프로세스 메모리 잠금이다.** 서버가 워커/레플리카 여러 개로 뜨면 A 워커에서 도는
#    정밀화를 B 워커로 들어온 수정 요청이 못 본다 — 잠금이 그냥 없는 것과 같다.
#    지금 Dockerfile은 `uvicorn app.main:app`(워커 1) 단일 프로세스라 실효가 있지만,
#    스케일아웃하는 순간 이 보증은 사라진다. 그때는 잠금 상태를 DB(세션 행)나 Redis로
#    옮겨야 한다 — 고치라는 뜻이 아니라 **숨기지 말라는 뜻으로** 여기 적어 둔다.
#
# 왜 전역 뮤텍스가 아니라 세션별 레지스트리인가: 한 사용자의 정밀화가 다른 사용자의
# 수정을 막으면 안 된다. 잠금 단위는 세션 하나다.
# ─────────────────────────────────────────────────────────────────────────────

# 정밀화 상태값 — 에이전트(partial kind="refine")와 백엔드(GET /refine)가 같은 어휘를 쓴다.
REFINE_RUNNING = "running"        # 정밀화 진행 중 = 수정 잠금 중
REFINE_DONE = "done"              # 정상 완료 (정밀화 결과가 확정본)
REFINE_CANCELLED = "cancelled"    # 탈출구 — 사용자가 중단, 초안이 확정본
REFINE_TIMEOUT = "timeout"        # 예산 초과 — 초안이 확정본
REFINE_FAILED = "failed"          # 정밀화 예외 — 초안이 확정본
REFINE_SUPERSEDED = "superseded"  # 같은 세션에 새 초안이 생겨 이전 정밀화가 밀려남
# 턴 자체가 밖에서 취소됨(클라이언트 끊김·턴 상한). 초안조차 저장되지 않았다는 뜻이라
# **정상 완료와 반드시 구분돼야 한다** — 재접속한 클라이언트가 GET /refine의 last를 보고
# "정밀화가 끝났다"로 읽으면 그게 곧 조용한 오답이다(추천 버전은 0개다).
REFINE_ABORTED = "aborted"

# 소유자가 잠금을 반납하지 못한 채 죽었을 때(프로세스 강제 종료, finally 미실행) 세션이
# 영구 잠기는 것을 막는 자동 만료 여유. deadline 이후 이만큼 더 지나면 죽은 잠금으로 본다.
_LOCK_GRACE_SEC = 30.0

# 정밀화가 끝난 뒤 **턴 저장이 끝날 때까지** 잠금을 더 붙잡아 두는 유예 (설계 §6.3).
# 에이전트가 잠금을 풀어도 추천 버전 INSERT는 한참 뒤 백엔드(`_persist_turn_result`)에서
# 일어난다 — 그 창이 열려 있으면 탈출구 사용자의 편집이 vN을 만들고 턴 저장이 그 위를
# 덮어쓴다. 그래서 반납 책임을 백엔드로 올렸고(release_pending_refine_lock), 백엔드가
# 그걸 못 부른 채 죽어도 세션이 오래 잠기지 않게 저장 대기에는 **짧은** 만료를 준다.
_PERSIST_GRACE_SEC = 20.0

# 종료된 정밀화의 마지막 결과를 잠시 들고 있는다 — 프론트가 완료 직후 상태를 물었을 때
# "잠금 없음"만 돌려주면 정상 완료인지 취소·타임아웃인지 구분할 수 없다. 세션 수만큼
# 무한히 쌓이면 누수라 상한을 둔다(FIFO 폐기).
_LAST_MAX = 256


@dataclass
class RefineLock:
    """세션 하나의 정밀화 점유권. 정밀화를 시작한 턴이 들고, 끝나면 반납한다.

    취소 신호를 `threading.Event`로 둔 이유: 취소를 거는 쪽은 **다른 요청**이고,
    FastAPI의 동기 라우트는 스레드풀에서 돈다 — asyncio.Event였다면 다른 스레드에서
    set할 때 루프 경계를 넘어야 한다. Event는 스레드 안전하고, 정밀화 쪽은 어차피
    폴링으로 확인하므로(그 사이 await 지점이 없는 to_thread 구간이 길다) 이게 맞다.

    수명은 정밀화보다 길다: 정밀화가 끝나면 소유자가 `pending_*`에 결과를 적어 두고
    **잠금은 그대로** 둔 채 빠지고(defer_refine_lock_release), 턴 저장을 마친 백엔드가
    반납한다(release_pending_refine_lock). 그 구간이 곧 "저장 전 편집 금지" 창이다.
    """

    session_id: str
    draft_id: str
    started_at: float          # time.time() — 프론트 표시용 epoch
    started_mono: float        # time.monotonic() — 경과 계산용(시계 변경 내성)
    deadline_mono: float
    cancel_reason: str | None = None
    # 저장 대기 — 정밀화는 끝났고 턴 저장만 남은 상태. 셋 다 함께 채워진다.
    pending_status: str | None = None
    pending_reason: str | None = None
    pending_since_mono: float | None = None
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    @property
    def pending_release(self) -> bool:
        """정밀화는 끝났고 턴 저장을 기다리는 중인가 — 잠금은 아직 살아 있다."""
        return self.pending_since_mono is not None

    def request_cancel(self, reason: str) -> None:
        """취소를 요청한다(멱등). 실제 중단은 정밀화 루프가 다음 폴링에서 수행한다."""
        if not self._cancel.is_set():
            self.cancel_reason = reason
            self._cancel.set()

    def expired(self, now_mono: float | None = None) -> bool:
        now_mono = time.monotonic() if now_mono is None else now_mono
        if self.pending_since_mono is not None:
            # 저장 대기는 DB 쓰기 한 번이라 초 단위다 — 정밀화 예산을 그대로 적용하면
            # 백엔드가 반납을 못 한 채 죽었을 때 세션이 분 단위로 잠긴다.
            return now_mono > self.pending_since_mono + _PERSIST_GRACE_SEC
        return now_mono > self.deadline_mono + _LOCK_GRACE_SEC

    def snapshot(self) -> dict:
        now_mono = time.monotonic()
        return {
            "session_id": self.session_id,
            "draft_id": self.draft_id,
            # status는 RUNNING으로 유지한다 — 저장 대기도 프론트 입장에선 "아직 잠김"이라
            # 표시가 달라질 이유가 없다. 구분이 필요하면 pending_release(추가 필드)를 본다.
            "status": REFINE_RUNNING,
            "started_at": self.started_at,
            "elapsed_sec": round(now_mono - self.started_mono, 1),
            "remaining_sec": round(max(0.0, self.deadline_mono - now_mono), 1),
            "cancel_requested": self.cancel_requested,
            "pending_release": self.pending_release,
            "reason": self.cancel_reason,
        }


_LOCKS: dict[str, RefineLock] = {}
_LAST: OrderedDict[str, dict] = OrderedDict()
_REGISTRY_LOCK = threading.RLock()


def acquire_refine_lock(session_id: str | None, draft_id: str, timeout_sec: float) -> RefineLock | None:
    """세션의 정밀화 점유권을 잡는다. session_id가 없으면(세션 밖 실행) None.

    이미 살아 있는 잠금이 있으면 **새 초안이 이긴다** — 같은 세션에서 흐름도를 다시
    생성했다는 뜻이고, 그 순간 이전 초안은 사용자 화면에서 이미 대체됐기 때문이다.
    이전 소유자에게는 취소를 걸어(superseded) 스스로 접고 나가게 한다. 반납은 신원
    비교(`release_refine_lock`)라 늦게 끝난 이전 소유자가 새 잠금을 지우지 않는다.
    """
    if not session_id:
        return None
    now_mono, now = time.monotonic(), time.time()
    lock = RefineLock(
        session_id=session_id, draft_id=draft_id,
        started_at=now, started_mono=now_mono, deadline_mono=now_mono + max(0.0, timeout_sec),
    )
    with _REGISTRY_LOCK:
        prev = _LOCKS.get(session_id)
        if prev is not None and not prev.expired(now_mono):
            prev.request_cancel("같은 세션에 새 초안이 생성돼 이전 정밀화를 중단합니다")
            logger.info("정밀화 잠금 교체 — session=%s prev_draft=%s", session_id, prev.draft_id)
        _LOCKS[session_id] = lock
    return lock


def release_refine_lock(lock: RefineLock | None, status: str, reason: str | None = None) -> None:
    """점유권을 반납하고 마지막 결과를 남긴다. 남의 잠금은 건드리지 않는다(신원 비교).

    이미 새 초안에 밀려난 잠금이면 **아무것도 하지 않는다** — 잠금을 지우면 진행 중인
    새 정밀화가 풀리고, 마지막 결과를 남기면 프론트가 "정밀화가 끝났다"로 읽는다.
    """
    if lock is None:
        return
    with _REGISTRY_LOCK:
        if _LOCKS.get(lock.session_id) is not lock:
            logger.info(
                "정밀화 잠금 반납 생략(%s) — session=%s draft=%s",
                REFINE_SUPERSEDED, lock.session_id, lock.draft_id,
            )
            return
        del _LOCKS[lock.session_id]
        _remember_last(lock, status, reason)


def defer_refine_lock_release(lock: RefineLock | None, status: str, reason: str | None = None) -> bool:
    """정밀화는 끝났지만 **턴 저장이 남아** 잠금을 놓지 않는다 (설계 §6.3).

    에이전트가 여기서 잠금을 풀어버리면, 추천 버전 INSERT(`app/api/sessions.py::
    _persist_turn_result`)까지 남은 구간 — trigger LLM 호출·R15 재검사·그래프 마무리·
    DB 쓰기 — 동안 `POST /{id}/recommendations`가 409를 안 받고 통과한다. 탈출구를 눌러
    초안을 고친 사용자의 vN이 만들어지고, 그 뒤 턴 저장이 그 위를 덮어쓴다.

    그래서 최종 상태만 적어 두고 잠금은 살려 둔다. 실제 반납은 저장을 마친 백엔드가
    `release_pending_refine_lock(session_id)`으로 한다. 반납이 영영 안 와도 저장 대기에는
    짧은 만료(`_PERSIST_GRACE_SEC`)가 걸려 있어 세션이 잠긴 채 남지 않는다.

    반환: 저장 대기로 넘겼으면 True. 잠금이 없거나(세션 밖 실행) 이미 새 초안에 밀려났으면
    False — 호출부는 그때 종전대로 `release_refine_lock`을 부르면 된다.
    """
    if lock is None:
        return False
    with _REGISTRY_LOCK:
        if _LOCKS.get(lock.session_id) is not lock:
            return False
        lock.pending_status = status
        lock.pending_reason = reason
        lock.pending_since_mono = time.monotonic()
    return True


def release_pending_refine_lock(session_id: str | None) -> dict | None:
    """턴 저장을 마친 백엔드가 부르는 실제 반납. 남긴 마지막 결과를 돌려준다(없으면 None).

    **저장 대기 중인 잠금만** 푼다 — 아직 정밀화가 도는 잠금은 소유자 것이라 남이 풀면
    소유자가 계속 돌다가 나중에 결과를 확정해 초안을 덮어쓴다(`request_refine_cancel`
    주석과 같은 이유). 그래서 이 함수는 "이 세션의 잠금을 무조건 푼다"가 아니다.
    """
    if not session_id:
        return None
    key = str(session_id)
    with _REGISTRY_LOCK:
        lock = _LOCKS.get(key)
        if lock is None or not lock.pending_release:
            return None
        del _LOCKS[key]
        _remember_last(lock, lock.pending_status or REFINE_DONE, lock.pending_reason)
        return dict(_LAST[key])


def _remember_last(lock: RefineLock, status: str, reason: str | None) -> None:
    """호출자가 _REGISTRY_LOCK을 들고 있어야 한다."""
    _LAST[lock.session_id] = {
        "session_id": lock.session_id,
        "draft_id": lock.draft_id,
        "status": status,
        "reason": reason or lock.cancel_reason,
        "finished_at": time.time(),
        "elapsed_sec": round(time.monotonic() - lock.started_mono, 1),
    }
    _LAST.move_to_end(lock.session_id)
    while len(_LAST) > _LAST_MAX:
        _LAST.popitem(last=False)


def _live_lock(session_id: str) -> RefineLock | None:
    """살아 있는 잠금만 돌려준다 — 만료된 잠금은 여기서 청소한다(자가 치유)."""
    with _REGISTRY_LOCK:
        lock = _LOCKS.get(session_id)
        if lock is None:
            return None
        if lock.expired():
            del _LOCKS[session_id]
            if lock.pending_release:
                # 정밀화 자체는 끝났다 — 그 결과를 timeout으로 덮으면 사용자에게 거짓말이 된다.
                _remember_last(lock, lock.pending_status or REFINE_DONE, lock.pending_reason)
                logger.warning(
                    "정밀화 잠금 저장 대기 만료 — session=%s draft=%s status=%s",
                    session_id, lock.draft_id, lock.pending_status,
                )
            else:
                _remember_last(lock, REFINE_TIMEOUT, "정밀화 소유자가 잠금을 반납하지 못했습니다")
                logger.warning("정밀화 잠금 자동 만료 — session=%s draft=%s", session_id, lock.draft_id)
            return None
        return lock


def is_refine_locked(session_id: str | None) -> bool:
    """이 세션이 지금 정밀화로 잠겨 있는가 — 수정 경로의 게이트."""
    return bool(session_id) and _live_lock(str(session_id)) is not None


def refine_lock_status(session_id: str | None) -> dict:
    """세션의 정밀화 상태 — {locked, refine(진행 중 스냅샷|None), last(마지막 결과|None)}."""
    if not session_id:
        return {"locked": False, "refine": None, "last": None}
    key = str(session_id)
    lock = _live_lock(key)
    with _REGISTRY_LOCK:
        last = dict(_LAST[key]) if key in _LAST else None
    return {"locked": lock is not None, "refine": lock.snapshot() if lock else None, "last": last}


def request_refine_cancel(session_id: str | None, reason: str) -> dict | None:
    """탈출구 — 진행 중인 정밀화에 중단을 요청한다. 잠금이 없으면 None.

    여기서 즉시 푸는 게 아니라 **소유자에게 부탁**한다: 잠금을 남이 풀면 소유자가 여전히
    돌면서 나중에 결과를 확정해 초안을 덮어쓴다. 소유자가 접고 초안을 확정한 뒤 반납한다.
    """
    if not session_id:
        return None
    lock = _live_lock(str(session_id))
    if lock is None or lock.pending_release:
        # 저장 대기 중이면 중단할 대상이 없다 — 정밀화는 이미 끝났고 잠금은 곧 풀린다.
        # 여기서 취소를 걸면 취소 사유가 마지막 결과의 reason으로 새어 "사용자가 중단했다"는
        # 거짓 기록이 남는다.
        return None
    lock.request_cancel(reason)
    return lock.snapshot()


def reset_refine_locks() -> None:
    """레지스트리를 비운다 — **테스트 전용**(프로세스 전역 상태가 테스트 간에 새지 않게)."""
    with _REGISTRY_LOCK:
        _LOCKS.clear()
        _LAST.clear()


# ─────────────────────────────────────────────────────────────────────────────
# 턴 데드라인 — 백엔드가 이 턴을 끊는 시각 (설계 §6.3의 시간 예산)
#
# 2상 정밀화는 "턴 상한에 걸리기 전에 먼저 접는다"가 존재 이유인데, 에이전트는 자기가
# 언제부터 돌았는지 모른다: 파이프라인이 아는 건 흐름도 생성에 걸린 시간뿐이고, 그 앞의
# intake 라우팅·analyze·build_flow_spec은 이미 지나간 뒤다. 그 구간을 못 세면 "남은 예산"이
# 실제보다 크게 잡혀 정밀화가 턴 상한을 넘기고, 그러면 **초안조차 저장되지 않는다** —
# 가드가 막겠다고 선언한 바로 그 실패다.
#
# 백엔드 context(RPA-64 계약)를 늘리지 않고 값을 내려보내는 통로로 ContextVar를 쓴다 —
# `_session_scope()`가 usage_context에서 session_id를 집어오는 것과 같은 방식이다.
# 이 모듈에 두는 이유도 같다: v4에서 무거운 의존이 없는 리프라 `app/api/sessions.py`가
# 싸게 import할 수 있다.
# ─────────────────────────────────────────────────────────────────────────────

_TURN_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "v4_turn_deadline_mono", default=None
)


@contextlib.contextmanager
def turn_deadline_scope(max_total_sec: float | None):
    """이 블록 안의 턴이 `max_total_sec` 뒤에 끊긴다고 알린다 (백엔드가 감싼다).

    받는 값이 절대 시각이 아니라 잔여 초인 이유: 호출부(`_iter_with_heartbeat`)는
    `loop.time()` 기준이고 여기 소비자는 `time.monotonic()` 기준이라, 시각을 그대로
    주고받으면 두 시계의 원점이 다를 때 예산이 통째로 어긋난다.
    """
    if not max_total_sec or max_total_sec <= 0:
        yield  # 상한 미설정 = 데드라인 없음 (기존 동작)
        return
    token = _TURN_DEADLINE.set(time.monotonic() + float(max_total_sec))
    try:
        yield
    finally:
        _TURN_DEADLINE.reset(token)


def current_turn_deadline() -> float | None:
    """이 턴이 끊기는 time.monotonic() 시각. 모르면 None(= 기존 예산 계산으로 폴백)."""
    return _TURN_DEADLINE.get()
