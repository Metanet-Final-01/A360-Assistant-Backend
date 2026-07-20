"""게이지 임계 캘리브레이션 계산 로직 + WARN_RATIO env화 테스트 (RPA-89)."""

import importlib.util
from pathlib import Path

import app.api.sessions as sessions_api

# scripts/gauge_calibration_report.py를 모듈로 로드(패키지 아님)
_spec = importlib.util.spec_from_file_location(
    "gauge_calibration_report",
    Path(__file__).resolve().parent.parent / "scripts" / "gauge_calibration_report.py",
)
calib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(calib)


# --- 순수 계산 ---

def test_session_deltas_skips_short_sessions():
    """3턴 미만 세션은 Δ 추정에서 제외된다."""
    series = [[500, 700], [500, 700, 900, 1200]]  # 2턴(제외), 4턴(포함)
    out = calib.session_deltas(series)
    assert len(out) == 1
    s = out[0]
    assert s["turns"] == 4 and s["first"] == 500 and s["last"] == 1200
    assert s["median_delta"] == 200  # 차이 [200,200,300]의 중앙값


def test_robust_delta_is_median_of_session_medians():
    sessions = [{"median_delta": 100}, {"median_delta": 300}, {"median_delta": 200}]
    assert calib.robust_delta(sessions) == 200


def test_turns_to_ratio():
    # base=577, Δ=199, LIMIT=6000, ratio 1.0 → (6000-577)/199 ≈ 27.3턴
    assert round(calib.turns_to_ratio(577, 199, 6000, 1.0), 1) == 27.3
    # Δ가 0이면 영원히 안 참
    assert calib.turns_to_ratio(500, 0, 6000, 1.0) == float("inf")


def test_warn_ratio_formula():
    # 1 − 4×199/6000 = 0.867
    assert calib.warn_ratio(199, 6000, 4) == 0.867
    # LIMIT 0이면 안전 기본값
    assert calib.warn_ratio(199, 0, 4) == 0.7


# --- WARN_RATIO env화 (적용 경로) ---

def test_gauge_warn_ratio_env(monkeypatch):
    """env 파싱·폴백 규칙을 검증한다. 기본값 자체는 상수를 참조 — 재보정(RPA-172 등)으로
    값이 바뀌어도 이 테스트가 깨지지 않게(값이 아니라 규칙을 지킨다)."""
    default = sessions_api._GAUGE_WARN_RATIO_DEFAULT
    monkeypatch.delenv("TURN_GAUGE_WARN_RATIO", raising=False)
    assert sessions_api._gauge_warn_ratio() == default  # 기본
    monkeypatch.setenv("TURN_GAUGE_WARN_RATIO", "0.55")
    assert sessions_api._gauge_warn_ratio() == 0.55  # 캘리브레이션 권장값 적용
    monkeypatch.setenv("TURN_GAUGE_WARN_RATIO", "banana")
    assert sessions_api._gauge_warn_ratio() == default  # 비정상값 폴백
    monkeypatch.setenv("TURN_GAUGE_WARN_RATIO", "1.5")
    assert sessions_api._gauge_warn_ratio() == default  # 범위 밖(>1) 폴백
