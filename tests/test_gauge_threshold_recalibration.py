"""게이지 임계 재보정이 실제로 의도한 동작을 내는지 (RPA-172).

이 이슈의 본질은 "값을 6000으로 바꿨다"가 아니라 **"그 값에서 자동 compact가 실제로 발동하고,
엉뚱하게는 발동하지 않는다"**이다. 이전 기본값(100000)에서 하드 도달에 ~527턴이 필요해 자동
compact 경로가 한 번도 실행된 적이 없었다 — 값만 바꾸고 발동을 확인하지 않으면 아무것도 증명하지
못한다.

실측 근거 (관측 DB 6,237행 / 2026-07-15):
  - 대화 누적 Δ ≈ 188 토큰/턴, 첫 턴 base ≈ 582 토큰
  - 최대 단일 사용자 메시지 ≈ 3,400 토큰
"""

import uuid
from types import SimpleNamespace

import pytest

import app.api.sessions as sessions_api

SID = uuid.uuid4()

BASE_TOKENS = 582      # 실측 첫 턴 intake
DELTA_PER_TURN = 188   # 실측 턴당 누적
MAX_MESSAGE_TOKENS = 3400  # 실측 최대 단일 메시지


def _gauge_with(monkeypatch, intake_tokens: int) -> dict:
    class _S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: intake_tokens)

    monkeypatch.setattr("app.db.SessionLocal", _S)
    monkeypatch.delenv("TURN_GAUGE_LIMIT_TOKENS", raising=False)
    monkeypatch.delenv("TURN_GAUGE_WARN_RATIO", raising=False)
    monkeypatch.delenv("TURN_GAUGE_HARD_RATIO", raising=False)
    return sessions_api._read_intake_gauge(SID)


def _tokens_at_turn(n: int) -> int:
    """실측 성장 모델 — n턴째의 intake 토큰."""
    return BASE_TOKENS + DELTA_PER_TURN * n


def _message_of_tokens(target: int) -> str:
    """실제 추정기가 정확히 ~target 토큰으로 세는 메시지를 만든다.

    글자 수로 어림하면 안 된다 — 추정기는 tiktoken(cl100k_base)이라 한글은 1자가 1토큰이
    아니다(실측으로 잡음: '가'×6800을 3400토큰으로 착각했다). 추정기에 직접 물어 맞춘다.
    """
    unit = "가"
    per_unit = sessions_api._estimate_message_tokens(unit * 100) / 100
    return unit * max(1, int(target / max(per_unit, 1e-9)))


# --- 재보정의 목적: 실제로 발동한다 ---

def test_hard_threshold_is_reachable_in_a_plausible_conversation(monkeypatch):
    """하드 임계가 '현실적인 대화 길이'에서 도달 가능해야 한다.

    이전 100000은 ~527턴이 필요해 영원히 안 터졌다. 재보정의 존재 이유가 이것이다.
    """
    limit = sessions_api._GAUGE_LIMIT_DEFAULT
    turns_to_hard = (limit - BASE_TOKENS) / DELTA_PER_TURN
    assert turns_to_hard < 50, (
        f"하드 도달에 {turns_to_hard:.0f}턴 — 현실 대화에서 자동 compact가 발동하지 않는다")


def test_auto_compact_fires_at_hard_threshold(monkeypatch):
    """임계 도달 시 compact_required가 서고, _needs_auto_compact가 True."""
    g = _gauge_with(monkeypatch, sessions_api._GAUGE_LIMIT_DEFAULT)
    assert g["compact_required"] is True
    assert sessions_api._needs_auto_compact(g, "짧은 메시지") is True


def test_auto_compact_does_not_fire_in_normal_short_conversation(monkeypatch):
    """실측 p90(3턴)짜리 평범한 대화에선 발동하지 않아야 한다 — 과빈발은 compact 비용 낭비."""
    g = _gauge_with(monkeypatch, _tokens_at_turn(3))
    assert g["compact_required"] is False
    assert g["compact_recommended"] is False
    assert sessions_api._needs_auto_compact(g, "짧은 메시지") is False


# --- 오발동 방지: 아래 경계가 왜 6000인가 ---

def test_largest_observed_single_message_does_not_trigger_compact(monkeypatch):
    """관측 최대 단일 메시지(3,400tok)를 1턴째에 붙여넣어도 자동 compact가 돌면 안 된다.

    이게 LIMIT의 **아래 경계**다. LIMIT=3000이면 base(582)+3400=3982로 임계를 넘어 1턴째부터
    선행 가드(RPA-86)가 compact를 돌린다 — 압축할 history가 없는데 비용($0.00843/콜)만 나간다.
    """
    g = _gauge_with(monkeypatch, BASE_TOKENS)
    huge = _message_of_tokens(MAX_MESSAGE_TOKENS)
    assert sessions_api._needs_auto_compact(g, huge) is False, (
        "관측 최대 크기의 단일 메시지가 1턴째 자동 compact를 유발한다 — LIMIT이 너무 낮다")


def test_lookahead_still_fires_for_genuinely_oversized_input(monkeypatch):
    """단, 진짜 초대형 입력은 여전히 선행 가드에 잡혀야 한다 (RPA-86 목적 유지).

    아래 경계를 올리다가 선행 가드를 통째로 죽이면 안 된다.
    """
    g = _gauge_with(monkeypatch, BASE_TOKENS)
    massive = _message_of_tokens(sessions_api._GAUGE_LIMIT_DEFAULT * 2)
    assert sessions_api._needs_auto_compact(g, massive) is True


# --- 경고(소프트)가 하드보다 먼저, 그러나 과하게 이르지 않게 ---

def test_warn_precedes_hard_with_actionable_headroom(monkeypatch):
    """경고는 하드보다 먼저 뜨되, 여유가 지나치게 크면 상시 알림이 된다.

    WARN_RATIO=0.87은 'LIMIT=6000에서 하드 4턴 전 경고'를 목표로 산출됐다(1 − 4Δ/LIMIT).
    """
    limit = sessions_api._GAUGE_LIMIT_DEFAULT
    warn = sessions_api._gauge_warn_ratio()
    hard = sessions_api._gauge_hard_ratio()
    assert 0 < warn < hard, "경고 임계가 하드보다 낮아야 한다"

    headroom_turns = (hard - warn) * limit / DELTA_PER_TURN
    assert 2 <= headroom_turns <= 8, (
        f"경고 후 하드까지 {headroom_turns:.1f}턴 — 너무 짧으면 못 누르고, 너무 길면 상시 알림")


@pytest.mark.parametrize("turn", [1, 2, 3, 5, 7])
def test_gauge_ratio_is_visible_not_pinned_at_zero(monkeypatch, turn):
    """게이지가 눈에 보이게 움직여야 한다 — 100000에선 ratio가 늘 0.0x라 UI가 죽어 있었다."""
    g = _gauge_with(monkeypatch, _tokens_at_turn(turn))
    assert g["ratio"] > 0.05, f"{turn}턴째 ratio={g['ratio']} — 게이지가 사실상 0으로 붙어 있다"
