"""게이지 임계 재보정이 실제로 의도한 동작을 내는지 (RPA-172).

이 이슈의 본질은 "값을 6000으로 바꿨다"가 아니라 **"그 값에서 자동 compact가 실제로 발동하고,
엉뚱하게는 발동하지 않는다"**이다. 이전 기본값(100000)에서 하드 도달에 ~527턴이 필요해 자동
compact 경로가 한 번도 실행된 적이 없었다 — 값만 바꾸고 발동을 확인하지 않으면 아무것도 증명하지
못한다.

실측 근거 (관측 DB 6,237행 / 2026-07-15):
  - 대화 누적 Δ ≈ 188 토큰/턴, 첫 턴 base ≈ 582 토큰
  - 최대 단일 사용자 메시지 ≈ 3,400 토큰

⚠️ 문자 ≠ 토큰. `AgentTurnRequest.message`의 max_length=4000은 **문자** 상한이라 토큰 상한을
   주지 않는다 (cl100k_base 실측 tok/char: 영문 0.12 / 한글 1.08 / 희귀 CJK 2.0 / 이모지 3.0
   → 같은 4,000자가 500~12,000토큰). 이 파일에서 입력 크기는 반드시 `_estimate_message_tokens`로
   재고, 문자 수를 토큰처럼 쓰지 말 것 — 한때 그렇게 적어 결론이 뒤집혔다.
   인코더 폴백은 UTF-8 **바이트** 수를 쓴다(byte-level BPE라 tokens ≤ bytes가 증명됨).
"""

import uuid
from types import SimpleNamespace

import pytest

import app.api.sessions as sessions_api

SID = uuid.uuid4()

BASE_TOKENS = 582      # 실측 첫 턴 intake
DELTA_PER_TURN = 188   # 실측 턴당 누적
MAX_MESSAGE_TOKENS = 3400  # 실측 최대 단일 메시지


@pytest.fixture
def real_encoder(monkeypatch):
    """`_estimate_message_tokens`가 실제 tiktoken으로 세게 한다.

    ⚠️ 이 픽스처 없이는 폴백(len)으로 동작한다 — `_TOKEN_ENCODER`는 앱 lifespan 워밍업에서만
       채워지고 테스트는 워밍업을 안 하기 때문이다. 프로덕션은 항상 워밍업하므로 폴백으로 재면
       **프로덕션과 다른 경로를 검증**하게 된다. 실제로 이 파일의 이모지 테스트가 12,000이 아닌
       4,000(=len)으로 나와 이 갭이 드러났다(RPA-172). 토큰 비율에 근거하는 테스트는 이걸 쓸 것.
    """
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
    except Exception:  # noqa: BLE001 — 미설치·오프라인(BPE 다운로드 실패)
        pytest.skip("tiktoken 인코더 로드 불가 — 토큰 비율 검증 생략")
    monkeypatch.setattr(sessions_api, "_TOKEN_ENCODER", enc)  # 테스트 끝나면 None으로 원복
    return enc


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


def _schema_max_message_len() -> int:
    """AgentTurnRequest.message의 max_length를 스키마에서 읽는다 — 하드코딩하면 계약이 바뀌어도
    테스트가 조용히 옛 값을 검증한다. metadata 순서(MinLen/MaxLen)에 의존하지 않게 탐색한다."""
    for m in sessions_api.AgentTurnRequest.model_fields["message"].metadata:
        if hasattr(m, "max_length"):
            return m.max_length
    raise AssertionError("message 필드에 max_length가 없다 — 단일 입력 상한이 사라졌다")


def _message_of_tokens(target: int, unit: str = "가") -> str:
    """실제 추정기가 정확히 ~target 토큰으로 세는 메시지를 만든다.

    글자 수로 어림하면 안 된다 — 추정기는 tiktoken(cl100k_base)이라 한글은 1자가 1토큰이
    아니다(실측으로 잡음: '가'×6800을 3400토큰으로 착각했다). 추정기에 직접 물어 맞춘다.
    """
    per_unit = sessions_api._estimate_message_tokens(unit * 100) / 100
    return unit * max(1, int(target / max(per_unit, 1e-9)))


def _schema_valid_message(unit: str) -> str:
    """`unit`을 반복해 만든 **스키마 유효**(max_length 이하) 최대 길이 메시지."""
    max_chars = _schema_max_message_len()
    msg = (unit * max_chars)[:max_chars]
    sessions_api.AgentTurnRequest(message=msg)  # 스키마가 실제로 받아주는지 확인
    return msg


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

def test_largest_observed_single_message_does_not_trigger_compact(monkeypatch, real_encoder):
    """관측 최대 단일 메시지(3,400tok)를 붙여넣어도 자동 compact가 돌면 안 된다.

    LIMIT **아래 경계**의 관측 기반 근거. LIMIT=3000이면 base(582)+3400=3982로 임계를 넘어
    선행 가드(RPA-86)가 compact를 돌린다 — 압축할 history가 없는데 비용만 나간다.
    """
    g = _gauge_with(monkeypatch, BASE_TOKENS)
    huge = _message_of_tokens(MAX_MESSAGE_TOKENS)
    assert sessions_api._needs_auto_compact(g, huge) is False, (
        "관측 최대 크기의 단일 메시지가 자동 compact를 유발한다 — LIMIT이 너무 낮다")


def test_realistic_longest_korean_input_does_not_trigger_compact(monkeypatch, real_encoder):
    """**현실 입력의 최악**(스키마 최대 길이의 한국어 산문)으로는 초반 오발동이 없어야 한다.

    LIMIT 아래 경계의 근거. 한글 산문은 cl100k_base에서 ≈1.08 tok/char라 4,000자 ≈ 4,309토큰,
    base(582)를 더해 **≈4,891 < LIMIT(6000)**. 이 테스트가 깨지면 LIMIT을 낮춘 것이고, 평범한 긴
    한국어 입력만으로 압축할 history도 없이 compact 비용이 나간다.

    경계를 **관계로** 단언한다(숫자를 하드코딩하면 상수가 바뀌어도 조용히 통과한다).
    """
    prose = _schema_valid_message("업무 정의서 분석 요청 ")
    worst_realistic = BASE_TOKENS + sessions_api._estimate_message_tokens(prose)

    assert sessions_api._GAUGE_LIMIT_DEFAULT > worst_realistic, (
        f"LIMIT({sessions_api._GAUGE_LIMIT_DEFAULT}) ≤ 현실 최악({worst_realistic}) — "
        f"평범한 긴 한국어 입력이 초반 턴에 compact를 유발한다")

    g = _gauge_with(monkeypatch, BASE_TOKENS)
    assert sessions_api._needs_auto_compact(g, prose) is False


def test_schema_length_cap_does_not_cap_tokens(monkeypatch, real_encoder):
    """⚠️ `max_length=4000`은 **문자** 상한이지 **토큰** 상한이 아니다 (RPA-172 오진 정정).

    한때 "단일 입력만으론 임계를 넘길 수 없다 — 스키마상 불가능"이라고 문서에 적었다. 거짓이다:
    이모지는 3 tok/char라 **스키마를 통과하는** 4,000자가 12,000토큰(LIMIT의 2배)이 된다. 즉
    RPA-86의 원래 문구("초대형 단일 입력이 당턴을 넘치게 하는 갭")가 옳았고 내 '정정'이 틀렸다.

    이 테스트는 그 착각이 되살아나는 걸 막는다 — 깨지면 누군가 문자 상한을 토큰 상한으로
    되돌려 놓은 것이다.
    """
    emoji = _schema_valid_message("🙃")
    tokens = sessions_api._estimate_message_tokens(emoji)
    assert len(emoji) <= _schema_max_message_len(), "전제: 스키마가 받아주는 입력이어야 한다"
    assert tokens > sessions_api._GAUGE_LIMIT_DEFAULT, (
        f"스키마 유효 입력({len(emoji)}자)이 {tokens}토큰 — LIMIT을 못 넘으면 이 정정의 전제가 바뀐 것")

    g = _gauge_with(monkeypatch, BASE_TOKENS)
    assert sessions_api._needs_auto_compact(g, emoji) is True, (
        "초대형 단일 입력이 선행 가드에 안 잡힌다 — RPA-86이 닫으려던 바로 그 갭이다")


def test_history_plus_large_input_triggers(monkeypatch, real_encoder):
    """가장 흔한 발동 경로: 누적 history + 큰 입력 (live 실측: 4턴째).

    단일 입력 경로(위)와 달리 이건 병리적 입력이 필요 없다 — 평범한 대화가 쌓이면 도달한다.
    """
    prose = _schema_valid_message("업무 정의서 분석 요청 ")
    input_tokens = sessions_api._estimate_message_tokens(prose)  # 문자 수가 아니라 토큰으로
    grown = sessions_api._GAUGE_LIMIT_DEFAULT - input_tokens + 500  # history가 쌓인 상태
    g = _gauge_with(monkeypatch, grown)
    assert g["compact_required"] is False, "게이지 단독으론 아직 임계 미만이어야 검증이 의미 있다"
    assert sessions_api._needs_auto_compact(g, prose) is True


@pytest.mark.parametrize("unit", ["a", "업무 정의서 분석 요청 ", "🙃", "龘", "ก"])
def test_fallback_never_underestimates(monkeypatch, real_encoder, unit):
    """인코더 폴백은 **절대 과소추정하면 안 된다** — 과소추정은 곧 가드가 뚫리는 것이다.

    cl100k_base는 byte-level BPE라 모든 토큰이 ≥1바이트를 소비 → `tokens ≤ bytes`가 항상 참이다.
    폴백이 UTF-8 바이트 수를 쓰는 근거가 이것이다. 이 테스트는 그 상한 관계를 실제 인코더와
    대조해 지킨다 — 누가 폴백을 `len(text)`(문자 수)로 되돌리면 이모지·CJK에서 깨진다.
    """
    msg = _schema_valid_message(unit)
    actual = len(real_encoder.encode(msg))

    monkeypatch.setattr(sessions_api, "_TOKEN_ENCODER", None)  # 워밍업 실패 상황
    fallback = sessions_api._estimate_message_tokens(msg)

    assert fallback >= actual, (
        f"폴백({fallback})이 실제 토큰({actual})보다 작다 — 스파이크를 놓치는 상한이다")


def test_fallback_still_catches_the_emoji_spike(monkeypatch):
    """폴백 상태(tiktoken 로드 실패)에서도 이모지 스파이크를 **잡아야** 한다.

    한때 폴백이 `len(text)`라 이모지 4,000자(실제 12,000토큰)를 4,000으로 세서 선행 가드를
    그냥 통과시켰다. 저하 모드라고 가드가 뚫리면 안 된다 — CodeRabbit #256 지적.
    """
    monkeypatch.setattr(sessions_api, "_TOKEN_ENCODER", None)
    emoji = _schema_valid_message("🙃")
    g = _gauge_with(monkeypatch, BASE_TOKENS)

    assert sessions_api._needs_auto_compact(g, emoji) is True, (
        "폴백이 이모지 스파이크를 놓친다 — 폴백이 토큰 상한이 아닌 값으로 되돌아갔다")


def test_lookahead_still_fires_for_genuinely_oversized_input(monkeypatch, real_encoder):
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
