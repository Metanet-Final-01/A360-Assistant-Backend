"""v4 2상 운영 배선 — 초안 선노출 / 수정 잠금 / 탈출구 / 실패·타임아웃 (설계 §6.3, RPA-298).

`test_agent_v4.py`가 두 상의 **계약**(DraftResult 직렬화·반환 형태·시그니처)을 이미
지키고 있다. 여기서 막는 것은 그 다음 층, 즉 **운영 배선**이다:

1. 초안이 정밀화보다 **먼저** 나가는가 — 안 나가면 2상으로 쪼갠 이유 자체가 사라진다.
2. 정밀화 중 수정이 **잠기는가**, 그리고 그 잠금이 **세션 범위**인가 — 전역 잠금이면
   한 사용자의 정밀화가 다른 사용자의 편집을 막는다.
3. **탈출구**가 실제로 초안을 확정하고 잠금을 푸는가 — 잠금만 있으면 갇힌 느낌이 된다.
4. 실패·타임아웃에 **초안이 살아남고 잠금이 풀리고 사유가 사용자에게 닿는가** —
   조용히 초안을 주면 사용자는 정밀화된 결과를 받았다고 믿는다.

LLM은 한 번도 부르지 않는다: compose·verify·judge·refine을 전부 결정론 대역으로 갈고,
시간이 걸리는 구간은 asyncio.sleep 대역으로 대신한다.
"""

import asyncio
import inspect
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.api.sessions as sessions_api
from app.agent.v4.orchestrator import state as v4_state
from app.agent.v4.recommend import graph as g
from app.db import get_db
from app.main import app
from app.schemas import ProgressEvent

SID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _clean_locks():
    """잠금 레지스트리는 프로세스 전역이다 — 테스트 간에 새면 뒤 테스트가 이유 없이 잠긴다."""
    v4_state.reset_refine_locks()
    yield
    v4_state.reset_refine_locks()
    app.dependency_overrides.clear()


# ─────────────────────────────────────────────────────────────────────────────
# 공용 대역
# ─────────────────────────────────────────────────────────────────────────────

def _sample_flow() -> dict:
    return {
        "steps": [{
            "step_id": "step-1",
            "label": "준비",
            "actions": [{
                "package": "Excel advanced", "action": "Open", "label": "열기",
                "order": 1, "parameters": [], "children": [],
            }],
        }],
        "variables": [],
    }


def _sample_hit() -> dict:
    return {
        "package_name": "Excel advanced", "action_name": "Open",
        "score": 0.91, "source_type": "action_schema", "title": "Open", "url": None,
    }


def _fake_ctx():
    return SimpleNamespace(catalog=None, retriever=None, searchable=False, is_a360=False,
                           has_overlay=False, overlay=[])


def _draft(draft_id: str = "d1") -> g.DraftResult:
    return g.DraftResult(
        flow=_sample_flow(),
        spec={"goal": "엑셀 정리", "requirements": [{"req_id": "REQ-1", "text": "열기", "priority": "must"}]},
        dossier={},
        reports=({"candidate_id": "A", "flow": _sample_flow(), "violations": [],
                  "must_coverage": 0.8, "sim_pass_rate": 0.9},),
        verdict={"winner": "A"},
        sink=(_sample_hit(),),
        findings=(),
        draft_id=draft_id,
    )


def _capture_frames(monkeypatch) -> list[dict]:
    """emit을 가로채 partial 프레임의 kind 순서를 관측한다 (스트림 컨텍스트 없이)."""
    from app.agent.v4.recommend import stream as s

    frames: list[dict] = []
    monkeypatch.setattr(s, "emit", frames.append)
    return frames


def _kinds(frames: list[dict]) -> list[str]:
    return [(f.get("data") or {}).get("kind") for f in frames if f.get("event") == "partial"]


def _stub_two_phase(monkeypatch, refine):
    """1상은 고정 초안으로, 2상은 주어진 코루틴 함수로 갈아끼운다."""
    async def fake_draft_flow(analysis, document, spec, ctx):
        from app.agent.v4.recommend.stream import emit_draft_frame

        d = _draft()
        emit_draft_frame(d.flow, [], d.draft_id, "선택된 초안 · 다듬기 시작")
        return d

    monkeypatch.setattr(g, "draft_flow", fake_draft_flow)
    monkeypatch.setattr(g, "refine_draft", refine)


# ─────────────────────────────────────────────────────────────────────────────
# 잠금 레지스트리 — 세션 범위·자가 치유 (state.py)
# ─────────────────────────────────────────────────────────────────────────────

def test_refine_lock_is_scoped_to_one_session():
    """전역 뮤텍스였다면 한 사용자의 정밀화가 다른 사용자의 수정을 통째로 막는다."""
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    v4_state.acquire_refine_lock(a, "d1", 60.0)

    assert v4_state.is_refine_locked(a)
    assert not v4_state.is_refine_locked(b)


def test_refine_lock_without_session_scope_is_a_noop():
    """세션 밖 실행(평가 스크립트·단독 recommend)은 잠글 대상이 없다 — None이어야 한다."""
    assert v4_state.acquire_refine_lock(None, "d1", 60.0) is None
    assert not v4_state.is_refine_locked(None)
    assert v4_state.request_refine_cancel(None, "x") is None


def test_dead_owner_cannot_lock_the_session_forever():
    """소유자가 반납 못 하고 죽으면(프로세스 강제 종료) 세션이 영구 잠긴다 — 자동 만료로 막는다."""
    sid = str(uuid.uuid4())
    lock = v4_state.acquire_refine_lock(sid, "d1", 60.0)
    lock.deadline_mono = time.monotonic() - v4_state._LOCK_GRACE_SEC - 1  # 소유자 사망 재현

    assert not v4_state.is_refine_locked(sid)
    # 왜 풀렸는지 남는다 — 사용자가 "왜 갑자기 풀렸나"를 알 수 있어야 한다.
    assert v4_state.refine_lock_status(sid)["last"]["status"] == v4_state.REFINE_TIMEOUT


def test_late_owner_release_does_not_unlock_the_new_draft():
    """늦게 끝난 이전 정밀화가 새 초안의 잠금을 지우면, 새 정밀화 중에 수정이 열린다."""
    sid = str(uuid.uuid4())
    old = v4_state.acquire_refine_lock(sid, "d1", 60.0)
    new = v4_state.acquire_refine_lock(sid, "d2", 60.0)

    # 새 초안이 이전 소유자에게 중단을 건다(같은 세션에 초안이 둘일 이유가 없다).
    assert old.cancel_requested and not new.cancel_requested
    v4_state.release_refine_lock(old, v4_state.REFINE_CANCELLED)
    assert v4_state.is_refine_locked(sid)  # 새 잠금은 살아 있다
    # 밀려난 소유자는 마지막 결과도 안 남긴다 — 남기면 프론트가 "정밀화가 끝났다"로 읽는다.
    assert v4_state.refine_lock_status(sid)["last"] is None

    v4_state.release_refine_lock(new, v4_state.REFINE_DONE)
    assert not v4_state.is_refine_locked(sid)
    assert v4_state.refine_lock_status(sid)["last"]["draft_id"] == "d2"


def test_cancel_is_a_request_not_an_unlock():
    """남이 잠금을 풀어버리면 소유자는 계속 돌다가 나중에 초안을 덮어쓴다 — 요청만 건다."""
    sid = str(uuid.uuid4())
    lock = v4_state.acquire_refine_lock(sid, "d1", 60.0)

    snap = v4_state.request_refine_cancel(sid, "사용자 중단")
    assert snap["cancel_requested"] and snap["draft_id"] == "d1"
    assert lock.cancel_requested and lock.cancel_reason == "사용자 중단"
    assert v4_state.is_refine_locked(sid)  # 아직 잠겨 있다 — 반납은 소유자 몫


def test_status_keeps_last_outcome_after_release():
    """완료 직후 '잠금 없음'만 주면 정상 완료인지 중단인지 프론트가 구분할 수 없다."""
    sid = str(uuid.uuid4())
    lock = v4_state.acquire_refine_lock(sid, "d1", 60.0)
    v4_state.release_refine_lock(lock, v4_state.REFINE_CANCELLED, "사용자가 중단")

    st = v4_state.refine_lock_status(sid)
    assert st["locked"] is False and st["refine"] is None
    assert st["last"]["status"] == v4_state.REFINE_CANCELLED and st["last"]["draft_id"] == "d1"


def test_last_outcome_cache_is_bounded():
    """세션마다 결과를 무한히 들고 있으면 장수 프로세스에서 누수가 된다."""
    for i in range(v4_state._LAST_MAX + 20):
        lock = v4_state.acquire_refine_lock(f"s{i}", "d", 60.0)
        v4_state.release_refine_lock(lock, v4_state.REFINE_DONE)

    assert len(v4_state._LAST) == v4_state._LAST_MAX


# ─────────────────────────────────────────────────────────────────────────────
# 2상 배선 — generate_flow_two_phase (graph.py)
# ─────────────────────────────────────────────────────────────────────────────

def test_draft_frame_goes_out_before_refinement_starts(monkeypatch):
    """초안이 정밀화 뒤에 나가면 사용자는 여전히 끝날 때까지 기다린다 — 2상의 존재 이유."""
    frames = _capture_frames(monkeypatch)
    seen_at_refine: list[list[str]] = []

    async def fake_refine(draft, ctx, **kw):
        seen_at_refine.append(_kinds(frames))  # 정밀화 진입 시점의 프레임 목록
        return {"recommendation": {"steps": []}, "violations": []}

    _stub_two_phase(monkeypatch, fake_refine)
    asyncio.run(g.generate_flow_two_phase({"summary": "x"}, None, {"goal": "g"}, _fake_ctx()))

    # 정밀화가 시작되기 전에 초안 프레임과 '정밀화 중' 프레임이 이미 나가 있어야 한다.
    assert "draft" in seen_at_refine[0]
    assert seen_at_refine[0].index("draft") < seen_at_refine[0].index("refine")
    assert _kinds(frames)[-1] == "refine"  # 종료 프레임도 refine


def test_session_is_locked_while_refining_and_unlocked_after(monkeypatch):
    """정밀화 중 잠금이 실제로 걸려 있어야 수정 경로가 그걸 보고 거절할 수 있다."""
    sid = str(uuid.uuid4())
    _capture_frames(monkeypatch)
    during: list[bool] = []

    async def fake_refine(draft, ctx, **kw):
        during.append(v4_state.is_refine_locked(sid))
        return {"recommendation": {"steps": []}, "violations": []}

    _stub_two_phase(monkeypatch, fake_refine)
    out = asyncio.run(
        g.generate_flow_two_phase({"summary": "x"}, None, {"goal": "g"}, _fake_ctx(), session_id=sid)
    )

    assert during == [True]
    assert out["refine"]["status"] == v4_state.REFINE_DONE and out["refine"]["locked"] is False
    # 반납은 백엔드 몫이다(아래 test_lock_is_held_until_the_backend_persists_the_turn).
    v4_state.release_pending_refine_lock(sid)
    assert not v4_state.is_refine_locked(sid)


def test_lock_is_held_until_the_backend_persists_the_turn(monkeypatch):
    """에이전트가 잠금을 먼저 풀면, 추천 버전 INSERT까지 남은 구간(trigger LLM·R15 재검사·
    그래프 마무리·DB 쓰기) 동안 편집이 409를 안 받고 vN을 만들고 턴 저장이 그 위를 덮는다."""
    sid = str(uuid.uuid4())
    _capture_frames(monkeypatch)

    async def fake_refine(draft, ctx, **kw):
        return {"recommendation": {"steps": []}, "violations": []}

    _stub_two_phase(monkeypatch, fake_refine)
    out = asyncio.run(
        g.generate_flow_two_phase({"summary": "x"}, None, {"goal": "g"}, _fake_ctx(), session_id=sid)
    )

    assert out["refine"]["persist_pending"] is True
    assert v4_state.is_refine_locked(sid)  # 파이프라인이 끝나도 아직 잠겨 있다
    status = v4_state.refine_lock_status(sid)
    assert status["refine"]["pending_release"] is True
    assert status["last"] is None  # 아직 '끝났다'고 기록하지도 않았다

    released = v4_state.release_pending_refine_lock(sid)
    assert released["status"] == v4_state.REFINE_DONE
    assert not v4_state.is_refine_locked(sid)


def test_pending_release_expires_so_a_dead_backend_cannot_lock_the_session(monkeypatch):
    """저장 단계가 반납을 못 한 채 죽어도 세션이 잠긴 채 남으면 안 된다 — 짧은 자동 만료."""
    sid = str(uuid.uuid4())
    lock = v4_state.acquire_refine_lock(sid, "d1", 600.0)
    v4_state.defer_refine_lock_release(lock, v4_state.REFINE_DONE)
    lock.pending_since_mono = time.monotonic() - v4_state._PERSIST_GRACE_SEC - 1

    assert not v4_state.is_refine_locked(sid)
    # 정밀화 자체는 정상 완료였다 — 만료가 그 결과를 timeout으로 덮으면 거짓말이 된다.
    assert v4_state.refine_lock_status(sid)["last"]["status"] == v4_state.REFINE_DONE


def test_cancel_is_ignored_once_the_refinement_is_only_waiting_to_be_saved():
    """정밀화가 이미 끝난 뒤의 중단 클릭이 마지막 결과 사유를 '사용자가 중단'으로 바꾸면 거짓 기록이다."""
    sid = str(uuid.uuid4())
    lock = v4_state.acquire_refine_lock(sid, "d1", 60.0)
    v4_state.defer_refine_lock_release(lock, v4_state.REFINE_DONE)

    assert v4_state.request_refine_cancel(sid, "사용자가 중단") is None
    assert v4_state.release_pending_refine_lock(sid)["status"] == v4_state.REFINE_DONE


def test_escape_hatch_cancels_refinement_and_confirms_the_draft(monkeypatch):
    """탈출구 — 중단 요청이 오면 정밀화를 접고 **초안을** 확정하고 잠금을 푼다 (§6.3)."""
    sid = str(uuid.uuid4())
    _capture_frames(monkeypatch)
    monkeypatch.setattr(g, "_REFINE_POLL_SEC", 0.01)

    async def never_ending_refine(draft, ctx, **kw):
        await asyncio.sleep(30)
        raise AssertionError("취소됐어야 한다")

    _stub_two_phase(monkeypatch, never_ending_refine)

    async def scenario():
        task = asyncio.create_task(
            g.generate_flow_two_phase({"summary": "x"}, None, {"goal": "g"}, _fake_ctx(), session_id=sid)
        )
        while not v4_state.is_refine_locked(sid):  # 잠금이 걸릴 때까지 (탈출구 버튼이 눌리는 시점)
            await asyncio.sleep(0.01)
        v4_state.request_refine_cancel(sid, "사용자가 중단")
        return await asyncio.wait_for(task, timeout=5)

    out = asyncio.run(scenario())

    assert out["refine"]["status"] == v4_state.REFINE_CANCELLED
    assert out["refine"]["reason"] == "사용자가 중단"
    v4_state.release_pending_refine_lock(sid)  # 저장 뒤 반납(백엔드 몫)을 대신 수행
    assert not v4_state.is_refine_locked(sid)
    # 초안이 확정본이다 — 빈 추천안으로 붕괴하지 않는다.
    assert out["recommendation"]["steps"][0]["actions"][0]["action"] == "Open"


def test_escape_hatch_is_not_delayed_by_the_orphan_refine_thread(monkeypatch):
    """잠금 반납이 턴 저장 뒤로 밀린 만큼, 그 앞에 있는 고아 태스크 정리가 탈출구를 늦추면
    안 된다 — refine이 to_thread 안이면 그 스레드는 취소돼도 끝까지 돈다(파이썬은 스레드를
    못 죽인다). asyncio 래퍼는 취소 즉시 확정되므로 실제 대기는 0이어야 한다."""
    sid = str(uuid.uuid4())
    _capture_frames(monkeypatch)
    monkeypatch.setattr(g, "_REFINE_POLL_SEC", 0.01)

    async def refine_stuck_in_a_thread(draft, ctx, **kw):
        await asyncio.to_thread(time.sleep, 2.0)  # 취소해도 이 스레드는 끝까지 돈다

    _stub_two_phase(monkeypatch, refine_stuck_in_a_thread)

    async def scenario():
        task = asyncio.create_task(
            g.generate_flow_two_phase({"summary": "x"}, None, {"goal": "g"}, _fake_ctx(), session_id=sid)
        )
        while not v4_state.is_refine_locked(sid):
            await asyncio.sleep(0.01)
        v4_state.request_refine_cancel(sid, "사용자가 중단")
        # 고아 스레드(2초)를 기다리는 구현이면 여기서 TimeoutError로 터진다.
        return await asyncio.wait_for(task, timeout=1.0)

    out = asyncio.run(scenario())

    assert out["refine"]["status"] == v4_state.REFINE_CANCELLED
    assert v4_state.release_pending_refine_lock(sid)["status"] == v4_state.REFINE_CANCELLED


def test_outer_cancellation_is_not_reported_as_a_finished_refinement(monkeypatch):
    """클라이언트 끊김·턴 상한으로 턴이 죽으면 추천 버전은 0개다 — 그걸 '정밀화 정상 완료'로
    남기면 재접속 클라이언트가 GET /refine의 last를 보고 끝난 줄 안다(조용한 오답)."""
    sid = str(uuid.uuid4())
    _capture_frames(monkeypatch)
    monkeypatch.setattr(g, "_REFINE_POLL_SEC", 0.01)

    async def never_ending_refine(draft, ctx, **kw):
        await asyncio.sleep(30)

    _stub_two_phase(monkeypatch, never_ending_refine)

    async def scenario():
        task = asyncio.create_task(
            g.generate_flow_two_phase({"summary": "x"}, None, {"goal": "g"}, _fake_ctx(), session_id=sid)
        )
        while not v4_state.is_refine_locked(sid):
            await asyncio.sleep(0.01)
        task.cancel()
        # 취소를 삼키면 asyncio 협조적 취소가 깨진다 — 반드시 다시 던져야 한다.
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    last = v4_state.release_pending_refine_lock(sid)
    assert last["status"] == v4_state.REFINE_ABORTED
    assert last["status"] != v4_state.REFINE_DONE


def test_refine_timeout_keeps_the_draft_and_unlocks(monkeypatch):
    """정밀화가 예산을 넘겨도 턴은 성공이다 — 잠금 해제 + 초안 유지 + 사유 (§6.3)."""
    sid = str(uuid.uuid4())
    _capture_frames(monkeypatch)
    monkeypatch.setattr(g, "_REFINE_POLL_SEC", 0.01)
    monkeypatch.setattr(g, "_REFINE_MIN_BUDGET_SEC", 0.0)  # 아래 0.05초 예산이 '생략' 분기로 새지 않게

    async def slow_refine(draft, ctx, **kw):
        await asyncio.sleep(30)

    _stub_two_phase(monkeypatch, slow_refine)
    out = asyncio.run(g.generate_flow_two_phase(
        {"summary": "x"}, None, {"goal": "g"}, _fake_ctx(), session_id=sid, refine_timeout_sec=0.05,
    ))

    assert out["refine"]["status"] == v4_state.REFINE_TIMEOUT
    assert "중단" in out["refine"]["reason"]
    v4_state.release_pending_refine_lock(sid)
    assert not v4_state.is_refine_locked(sid)
    assert out["recommendation"]["steps"]  # 초안은 살아 있다


def test_refine_failure_keeps_the_draft_and_unlocks(monkeypatch):
    """2상 예외로 1상 산출물까지 잃으면 안 된다 — 실패해도 초안은 사용자에게 남는다."""
    sid = str(uuid.uuid4())
    _capture_frames(monkeypatch)

    async def boom(draft, ctx, **kw):
        raise RuntimeError("surgeon 폭발")

    _stub_two_phase(monkeypatch, boom)
    out = asyncio.run(
        g.generate_flow_two_phase({"summary": "x"}, None, {"goal": "g"}, _fake_ctx(), session_id=sid)
    )

    assert out["refine"]["status"] == v4_state.REFINE_FAILED and out["refine"]["reason"]
    v4_state.release_pending_refine_lock(sid)
    assert not v4_state.is_refine_locked(sid)
    assert out["recommendation"]["steps"]


def test_refine_is_skipped_when_the_turn_budget_is_already_gone(monkeypatch):
    """1상이 턴 상한을 거의 다 먹었으면 정밀화를 켜지 않는다 — 켜면 초안까지 잃는다."""
    sid = str(uuid.uuid4())
    _capture_frames(monkeypatch)
    monkeypatch.setattr(g, "_TURN_SOFT_BUDGET_SEC", 0.0)  # 1상이 예산을 다 쓴 상황 재현
    called: list[int] = []

    async def fake_refine(draft, ctx, **kw):
        called.append(1)
        return {"recommendation": {"steps": []}, "violations": []}

    _stub_two_phase(monkeypatch, fake_refine)
    out = asyncio.run(
        g.generate_flow_two_phase({"summary": "x"}, None, {"goal": "g"}, _fake_ctx(), session_id=sid)
    )

    assert not called  # 시작조차 안 했다
    assert out["refine"]["status"] == v4_state.REFINE_TIMEOUT
    assert not v4_state.is_refine_locked(sid)  # 잠그지도 않았다
    assert out["recommendation"]["steps"]


def test_refine_budget_is_measured_from_the_real_turn_deadline(monkeypatch):
    """예산을 '2상 진입점부터의 잔여'로 재면 그 앞(intake 라우팅·analyze·spec 생성)이 통째로
    빠진다. 그러면 가드가 막겠다고 선언한 실패 — 턴 상한에 걸려 **초안조차 저장 안 됨** —
    이 기본 설정에서 그대로 남는다."""
    sid = str(uuid.uuid4())
    _capture_frames(monkeypatch)
    called: list[int] = []

    async def fake_refine(draft, ctx, **kw):
        called.append(1)
        return {"recommendation": {"steps": []}, "violations": []}

    _stub_two_phase(monkeypatch, fake_refine)
    # 초안 자체는 즉시 끝났다(draft_elapsed≈0) — 초안 기준 계산이면 840초가 그대로 남는다.
    # 하지만 턴은 이미 늙어서 저장 몫(_TURN_RESERVE_SEC)을 빼면 5초밖에 안 남았다.
    out = asyncio.run(g.generate_flow_two_phase(
        {"summary": "x"}, None, {"goal": "g"}, _fake_ctx(), session_id=sid,
        turn_deadline_mono=time.monotonic() + g._TURN_RESERVE_SEC + 5.0,
    ))

    assert not called  # 켜지 않는다 — 켜면 턴 상한에 걸려 초안까지 잃는다
    assert out["refine"]["status"] == v4_state.REFINE_TIMEOUT
    assert not v4_state.is_refine_locked(sid)


def test_turn_deadline_is_optional_and_defaults_to_the_old_budget(monkeypatch):
    """새 인자는 기본값이 있어야 한다 — 평가 스크립트·기존 호출부가 안 넘겨도 그대로 돌아야 한다."""
    sig = inspect.signature(g.generate_flow_two_phase).parameters["turn_deadline_mono"]
    assert sig.default is None

    _capture_frames(monkeypatch)
    called: list[int] = []

    async def fake_refine(draft, ctx, **kw):
        called.append(1)
        return {"recommendation": {"steps": []}, "violations": []}

    _stub_two_phase(monkeypatch, fake_refine)
    asyncio.run(g.generate_flow_two_phase({"summary": "x"}, None, {"goal": "g"}, _fake_ctx()))

    assert called  # 데드라인을 모르면 종전대로 초안 소요만 빼고 정밀화를 돈다


def test_two_phase_return_is_a_superset_of_generate_flow(monkeypatch):
    """refine 키는 **추가**다 — {recommendation, violations}만 읽던 호출부가 안 깨진다."""
    _capture_frames(monkeypatch)

    async def fake_refine(draft, ctx, **kw):
        return {"recommendation": {"steps": []}, "violations": [{"rule": "R2"}]}

    _stub_two_phase(monkeypatch, fake_refine)
    out = asyncio.run(g.generate_flow_two_phase({"summary": "x"}, None, {"goal": "g"}, _fake_ctx()))

    assert {"recommendation", "violations"} <= set(out)
    assert set(out) - {"recommendation", "violations"} == {"refine"}


def test_generate_flow_keeps_its_signature_and_stays_budget_free():
    """골드셋 비교 기준선 — 2상 운영 장치(시간 예산·잠금)를 여기에 얹으면 점수가 흔들린다."""
    assert [p.name for p in inspect.signature(g.generate_flow).parameters.values()] == [
        "analysis", "document", "spec", "ctx",
    ]
    src = inspect.getsource(g.generate_flow)
    assert "acquire_refine_lock" not in src and "refine_timeout" not in src


def test_judge_sees_the_source_document_like_the_candidates_do(monkeypatch):
    """심판의 맹목 기대는 'spec+문서만 보고 독립 생성'이 전제다. 문서를 안 넘기면 후보만 원문을
    본 비대칭이 되고, 원문에만 있는 누락은 아무도 못 잡는다."""
    seen: dict = {}

    async def fake_compose(cid, persona_file, spec, dossier, analysis, document, sink, sem, ctx):
        return _sample_flow()

    async def fake_verify(cid, persona_name, flow, spec, sem, ctx):
        return SimpleNamespace(candidate_id=cid, persona=persona_name, flow=flow,
                               violations=[], findings=[], model_dump=lambda: {"candidate_id": cid})

    def fake_judge(spec, reports, **kwargs):
        seen.update(kwargs)
        return {"winner": reports[0], "verdict": {"winner": reports[0].candidate_id},
                "transplant_findings": []}

    async def fake_dossier(spec, sink, ctx):
        return {"menu": "", "background": "", "examples": ""}

    # draft_flow가 함수 안에서 import하므로 모듈 속성을 갈아야 한다(문자열 경로는 패키지
    # __init__의 동명 함수에 막힌다).
    from app.agent.v4.orchestrator import judge as judge_mod
    from app.agent.v4.recommend import research as research_mod

    _capture_frames(monkeypatch)
    monkeypatch.setattr(g, "_compose_candidate", fake_compose)
    monkeypatch.setattr(g, "_verify_candidate", fake_verify)
    monkeypatch.setattr(g, "_REVEAL_DELAY", 0.0)
    monkeypatch.setattr(research_mod, "build_dossier", fake_dossier)
    monkeypatch.setattr(judge_mod, "judge_candidates", fake_judge)

    asyncio.run(g.draft_flow({"summary": "x"}, "업무정의서 원문", {"goal": "g"}, _fake_ctx()))

    assert seen.get("document") == "업무정의서 원문"


def test_draft_only_finalize_carries_spec_but_no_cards():
    """초안 확정본에 spec이 빠지면 이후 수정 턴의 회귀 가드가 삭제 편향으로 되돌아간다(§5-D)."""
    out = g._finalize_draft_only(_draft())

    rec = out["recommendation"]
    assert rec["spec"]["requirements"][0]["req_id"] == "REQ-1"
    assert rec["needs_input"] == []          # 카드 문구 다듬기는 LLM 경로 — 탈출구에서 안 부른다
    assert rec["flow_confidence"] is not None
    assert rec["steps"][0]["actions"][0]["sources"]  # 근거 부착(FR-11)은 결정론이라 살린다


def test_draft_only_finalize_does_not_mutate_the_draft():
    """1상 산출물을 제자리 변형하면 같은 초안으로 재시도할 때 입력이 이미 오염돼 있다."""
    draft = _draft()
    g._finalize_draft_only(draft)

    assert "spec" not in draft.flow and "needs_input" not in draft.flow


# ─────────────────────────────────────────────────────────────────────────────
# 노드 배선 — generate_node가 2상 진입점을 타는가 (generate.py)
# ─────────────────────────────────────────────────────────────────────────────

def test_generate_node_passes_the_session_scope_to_the_lock(monkeypatch):
    """에이전트는 stateless라 세션 id가 없다 — usage_context에서 집어와야 잠금이 세션 범위가 된다."""
    from app.core.llm import usage_context

    from app.agent.v4.orchestrator import generate as gen

    seen: dict = {}

    async def fake_two_phase(analysis, document, spec, ctx, *, session_id=None, turn_deadline_mono=None):
        seen["session_id"] = session_id
        return {"recommendation": _sample_flow(), "violations": [],
                "refine": {"status": v4_state.REFINE_DONE, "reason": None}}

    monkeypatch.setattr(gen, "generate_flow_two_phase", fake_two_phase)
    monkeypatch.setattr(gen, "build_flow_spec", lambda state, doc: {"goal": "g", "requirements": []})

    async def run():
        with usage_context(component="agent", session_id=SID):
            return await gen._generate_with({"analysis": {"steps": []}}, _fake_ctx())

    out = asyncio.run(run())

    assert seen["session_id"] == str(SID)
    assert out["refine_status"]["status"] == v4_state.REFINE_DONE
    assert "초안 그대로" not in out["answer"]  # 정상 완료엔 사유 안내를 붙이지 않는다


def test_generate_node_hands_the_real_turn_deadline_to_the_budget(monkeypatch):
    """에이전트는 자기가 언제부터 돌았는지 모른다 — 백엔드가 심은 데드라인을 집어와야
    2상 예산이 intake·분석까지 포함한 '턴 잔여'가 된다."""
    from app.agent.v4.orchestrator import generate as gen

    seen: dict = {}

    async def fake_two_phase(analysis, document, spec, ctx, *, session_id=None, turn_deadline_mono=None):
        seen["deadline"] = turn_deadline_mono
        return {"recommendation": _sample_flow(), "violations": []}

    monkeypatch.setattr(gen, "generate_flow_two_phase", fake_two_phase)
    monkeypatch.setattr(gen, "build_flow_spec", lambda state, doc: {"goal": "g", "requirements": []})

    with v4_state.turn_deadline_scope(120.0):
        asyncio.run(gen._generate_with({"analysis": {"steps": []}}, _fake_ctx()))
    outside = seen["deadline"]

    assert outside is not None and outside > time.monotonic()
    assert outside <= time.monotonic() + 120.0

    # 스코프 밖(평가 스크립트·단독 실행)에선 None — graph가 종전 근사로 폴백한다.
    asyncio.run(gen._generate_with({"analysis": {"steps": []}}, _fake_ctx()))
    assert seen["deadline"] is None


def test_generate_node_discloses_an_unfinished_refinement(monkeypatch):
    """조용히 초안을 주면 사용자는 정밀화된 결과를 받았다고 믿는다 — '조용한 오답'."""
    from app.agent.v4.orchestrator import generate as gen

    async def fake_two_phase(analysis, document, spec, ctx, *, session_id=None, turn_deadline_mono=None):
        return {"recommendation": _sample_flow(), "violations": [],
                "refine": {"status": v4_state.REFINE_TIMEOUT, "reason": "정밀화가 420초를 넘겨 중단했어요."}}

    monkeypatch.setattr(gen, "generate_flow_two_phase", fake_two_phase)
    monkeypatch.setattr(gen, "build_flow_spec", lambda state, doc: {"goal": "g", "requirements": []})

    out = asyncio.run(gen._generate_with({"analysis": {"steps": []}}, _fake_ctx()))

    assert "초안 그대로 확정" in out["answer"]
    assert "420초를 넘겨" in out["answer"]      # 사유가 그대로 닿는다
    assert "수정 잠금은 풀렸" in out["answer"]  # 지금 뭘 할 수 있는지도 함께


def test_generate_node_runs_without_a_session_scope(monkeypatch):
    """세션 밖(평가·단독 실행)에서도 생성은 돌아야 한다 — 잠금만 없다."""
    from app.agent.v4.orchestrator import generate as gen

    seen: dict = {}

    async def fake_two_phase(analysis, document, spec, ctx, *, session_id=None, turn_deadline_mono=None):
        seen["session_id"] = session_id
        return {"recommendation": _sample_flow(), "violations": []}

    monkeypatch.setattr(gen, "generate_flow_two_phase", fake_two_phase)
    monkeypatch.setattr(gen, "build_flow_spec", lambda state, doc: {"goal": "g", "requirements": []})
    out = asyncio.run(gen._generate_with({"analysis": {"steps": []}}, _fake_ctx()))

    assert seen["session_id"] is None
    assert out["refine_status"] is None


# ─────────────────────────────────────────────────────────────────────────────
# API 경계 — 수정 잠금과 탈출구 (app/api/sessions.py)
# ─────────────────────────────────────────────────────────────────────────────

class _FakeDB:
    def __init__(self, session):
        self._session = session

    def get(self, model, key):
        return self._session

    def execute(self, stmt):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []),
                               scalar_one_or_none=lambda: None)

    def commit(self):
        pass


def _override_session(user_id=None):
    session = SimpleNamespace(id=SID, user_id=user_id, title="채팅", solution="a360",
                              created_at=None, updated_at=None)
    app.dependency_overrides[get_db] = lambda: _FakeDB(session)
    app.dependency_overrides[sessions_api.get_optional_user] = lambda: None
    return session


def test_drag_edit_save_is_rejected_while_refining():
    """초안이 화면에 뜬 순간부터 드래그 편집이 가능하다 — 여기가 실제로 부딪히는 자리다."""
    _override_session()
    v4_state.acquire_refine_lock(str(SID), "d1", 60.0)

    with TestClient(app) as c:
        r = c.post(f"/api/sessions/{SID}/recommendations",
                   json={"recommendation": {"steps": []}, "source": "drag"})

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "REFINE_IN_PROGRESS"
    # 거절만 하면 갇힌 느낌이다 — 탈출구 경로와 진행 상태를 함께 준다.
    assert detail["cancel_path"] == f"/api/sessions/{SID}/refine/cancel"
    assert detail["refine"]["draft_id"] == "d1"


def test_drag_edit_save_is_still_rejected_while_the_turn_is_being_saved():
    """정밀화가 끝난 순간부터 턴 저장(추천 버전 INSERT)까지가 실제 위험 구간이다 — 여기서
    열리면 사용자의 편집이 vN이 되고 뒤이은 턴 저장이 그 위를 조용히 덮는다."""
    _override_session()
    lock = v4_state.acquire_refine_lock(str(SID), "d1", 60.0)
    v4_state.defer_refine_lock_release(lock, v4_state.REFINE_DONE)  # 에이전트가 저장 대기로 넘김

    with TestClient(app) as c:
        r = c.post(f"/api/sessions/{SID}/recommendations",
                   json={"recommendation": {"steps": []}, "source": "drag"})

    assert r.status_code == 409 and r.json()["detail"]["code"] == "REFINE_IN_PROGRESS"


def _stub_turn_route(monkeypatch, fake_stream, fake_persist=None):
    """POST /turn을 DB·LLM 없이 끝까지 태운다 — 저장 경계 밖의 배선만 관측하기 위한 대역."""
    def default_persist(session_id, rec_analysis_id, document_id, user_message, result):
        return {"type": "answer", "answer": "ok", "sources": [], "session_id": str(SID)}

    monkeypatch.setattr(sessions_api, "_get_agent_turn", lambda: fake_stream)
    monkeypatch.setattr(sessions_api, "_persist_turn_result", fake_persist or default_persist)
    monkeypatch.setattr(sessions_api, "_assemble_turn_context", lambda session, db, **kw: {
        "agent_context": {}, "rec_analysis_id": None, "document_id": None})
    monkeypatch.setattr(sessions_api, "_read_intake_gauge", lambda sid: None)
    monkeypatch.setattr(sessions_api, "_save_turn_events", lambda *a, **k: None)
    monkeypatch.setattr(sessions_api.budget, "check_budget",
                        lambda subject: SimpleNamespace(exceeded=False))


def _done_only_stream(before_done=None):
    async def fake_stream(message, context):
        if before_done is not None:
            before_done()
        yield ProgressEvent(event="done", stage="agent",
                            data={"type": "answer", "answer": "ok", "sources": []})

    return fake_stream


def test_backend_releases_the_lock_only_after_it_persists_the_turn(monkeypatch):
    """반납이 저장보다 먼저면 그 창으로 편집이 새어 들어온다 — 저장 시점엔 아직 잠겨 있어야 한다."""
    _override_session()
    lock = v4_state.acquire_refine_lock(str(SID), "d1", 60.0)
    locked_at_persist: list[bool] = []

    def fake_persist(session_id, rec_analysis_id, document_id, user_message, result):
        locked_at_persist.append(v4_state.is_refine_locked(str(SID)))
        return {"type": "answer", "answer": "ok", "sources": [], "session_id": str(SID)}

    # 2상 배선 그대로: 에이전트가 정밀화를 마치고 잠금을 '저장 대기'로 넘긴 뒤 done을 낸다.
    _stub_turn_route(
        monkeypatch,
        _done_only_stream(lambda: v4_state.defer_refine_lock_release(lock, v4_state.REFINE_DONE)),
        fake_persist,
    )

    with TestClient(app) as c:
        r = c.post(f"/api/sessions/{SID}/turn", json={"message": "안녕"})

    assert r.status_code == 200
    assert locked_at_persist == [True]                # 저장하는 동안 잠금이 살아 있었다
    assert not v4_state.is_refine_locked(str(SID))    # 저장이 끝나고서야 풀린다
    assert v4_state.refine_lock_status(str(SID))["last"]["status"] == v4_state.REFINE_DONE


def test_turn_deadline_reaches_the_agent_through_the_turn_stream(monkeypatch):
    """에이전트가 데드라인을 못 보면 2상 예산이 '2상 진입점부터의 잔여'로 되돌아간다 —
    intake·analyze·spec 생성이 통째로 빠진 채 턴 상한을 넘겨 초안까지 잃는다."""
    _override_session()
    seen: list = []

    _stub_turn_route(monkeypatch, _done_only_stream(
        lambda: seen.append(v4_state.current_turn_deadline())))
    monkeypatch.setattr(sessions_api, "_turn_max_sec", lambda: 300.0)

    with TestClient(app) as c:
        r = c.post(f"/api/sessions/{SID}/turn", json={"message": "안녕"})

    assert r.status_code == 200
    assert seen and seen[0] is not None
    # _iter_with_heartbeat에 넘기는 상한과 같은 값이어야 절단선이 어긋나지 않는다.
    assert seen[0] <= time.monotonic() + 300.0
    assert v4_state.current_turn_deadline() is None  # 턴 밖으로는 새지 않는다


def test_backend_does_not_release_a_lock_that_is_still_refining():
    """저장 뒤 반납이 '이 세션 잠금을 무조건 푼다'가 되면, 아직 도는 소유자의 잠금까지 풀려
    그 소유자가 나중에 결과를 확정해 초안을 덮어쓴다."""
    v4_state.acquire_refine_lock(str(SID), "d1", 60.0)

    assert v4_state.release_pending_refine_lock(str(SID)) is None
    assert v4_state.is_refine_locked(str(SID))


def test_drag_edit_save_is_open_when_another_session_is_refining():
    """잠금은 세션 범위다 — 남의 정밀화가 내 편집을 막으면 안 된다."""
    _override_session()
    v4_state.acquire_refine_lock(str(uuid.uuid4()), "d1", 60.0)

    with TestClient(app) as c:
        r = c.post(f"/api/sessions/{SID}/recommendations",
                   json={"recommendation": {"steps": []}, "source": "drag"})

    # 잠금 게이트는 통과했고, 그 뒤 정상 검사(기준 추천안 없음)에 걸린 것이어야 한다.
    assert r.json()["detail"]["code"] == "NO_RECOMMENDATION"


def test_fill_cards_turn_is_rejected_while_refining():
    """카드 응답 반영은 흐름도를 결정론으로 고쳐 새 버전을 만든다 — 정밀화와 충돌한다."""
    _override_session()
    v4_state.acquire_refine_lock(str(SID), "d1", 60.0)

    with TestClient(app) as c:
        r = c.post(f"/api/sessions/{SID}/turn",
                   json={"message": "카드 반영", "operation": "fill_cards", "card_values": {}})

    assert r.status_code == 409 and r.json()["detail"]["code"] == "REFINE_IN_PROGRESS"


def test_refine_status_endpoint_reports_the_lock():
    """SSE partial을 놓친 클라이언트(새로고침·재접속)가 편집 UI 상태를 복원할 곳."""
    _override_session()
    v4_state.acquire_refine_lock(str(SID), "d7", 60.0)

    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/refine")

    assert r.status_code == 200
    body = r.json()
    assert body["locked"] is True and body["refine"]["draft_id"] == "d7"
    assert body["refine"]["remaining_sec"] > 0


def test_cancel_endpoint_requests_cancel_without_unlocking():
    """탈출구는 요청만 건다 — 여기서 풀어버리면 소유자가 나중에 초안을 덮어쓴다."""
    _override_session()
    lock = v4_state.acquire_refine_lock(str(SID), "d1", 60.0)

    with TestClient(app) as c:
        r = c.post(f"/api/sessions/{SID}/refine/cancel")

    assert r.status_code == 200 and r.json()["accepted"] is True
    assert lock.cancel_requested
    assert r.json()["locked"] is True  # 반납은 소유자 몫 — 곧 풀린다


def test_cancel_endpoint_is_not_an_error_when_nothing_is_running():
    """정밀화 완료와 사용자 클릭이 겹치는 건 정상 시나리오다 — 404로 만들면 실패로 보인다."""
    _override_session()

    with TestClient(app) as c:
        r = c.post(f"/api/sessions/{SID}/refine/cancel")

    assert r.status_code == 200 and r.json()["accepted"] is False


def test_lock_gate_never_forces_the_agent_to_import(monkeypatch):
    """모듈이 로드조차 안 됐으면 잠금도 없다 — 드래그 저장 한 번에 에이전트를 끌어올리지 않는다."""
    _override_session()
    v4_state.acquire_refine_lock(str(SID), "d1", 60.0)
    monkeypatch.setattr(sessions_api, "_REFINE_LOCK_MODULES", ("app.agent.v9.nope",))

    assert sessions_api._refine_lock_module() is None
    assert sessions_api._refine_status(SID) == {"locked": False, "refine": None, "last": None}


def test_refine_status_survives_a_broken_registry(monkeypatch):
    """관측 장치의 고장이 본 기능(저장·조회)을 막으면 안 된다 — 잠금 없음으로 저하한다."""
    _override_session()

    class _Broken:
        @staticmethod
        def refine_lock_status(_sid):
            raise RuntimeError("레지스트리 고장")

    monkeypatch.setattr(sessions_api, "_refine_lock_module", lambda: _Broken)
    with TestClient(app) as c:
        r = c.get(f"/api/sessions/{SID}/refine")

    assert r.status_code == 200 and r.json()["locked"] is False
