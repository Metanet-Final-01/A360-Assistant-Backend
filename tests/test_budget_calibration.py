"""예산 캘리브레이션 리포트의 순수 계산 검증 (RPA-171).

리포트 자체는 사람이 읽는 산출물이라 DB 조회부는 테스트하지 않는다(gauge_calibration_report와
같은 구조 — 순수 계산만 테스트 대상으로 분리). 여기서 막는 건 **권장값이 조용히 이상해지는 것**이다.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "budget_calibration_report.py"
_spec = importlib.util.spec_from_file_location("budget_calibration_report", _PATH)
rpt = importlib.util.module_from_spec(_spec)
sys.modules["budget_calibration_report"] = rpt
_spec.loader.exec_module(rpt)


# --- percentile ---

def test_percentile_basics():
    xs = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert rpt.percentile(xs, 0.9) == 10
    assert rpt.percentile(xs, 0.5) == 6
    assert rpt.percentile([], 0.9) == 0.0


def test_percentile_sorts_input():
    """입력이 정렬돼 있지 않아도 분위가 맞아야 한다."""
    assert rpt.percentile([10, 1, 5], 0.0) == 1


# --- recommend_limit ---

def test_no_observation_means_no_recommendation():
    """관측이 없으면 권장하지 않는다 — 근거 없는 숫자를 뱉느니 0."""
    assert rpt.recommend_limit(0, 2.5) == 0.0
    assert rpt.recommend_limit(-1, 2.5) == 0.0


def test_recommend_covers_observed_max():
    """권장값은 반드시 관측 최대치보다 커야 한다 — 아니면 정상 사용자를 막는다.

    이게 RPA-171 최초 구현의 실제 사고였다($1 상한 vs 실측 최대 $2.02).
    """
    for observed in (0.05, 2.02, 3.22, 17.5, 240.0):
        assert rpt.recommend_limit(observed, 2.5) > observed


@pytest.mark.parametrize("raw,expected", [
    (5.05, 6),      # <10 → 정수 올림
    (2.02 * 2.5, 6),
    (42.0, 45),     # <100 → 5 단위
    (202.0, 250),   # >=100 → 50 단위
    (0.5, 1),
])
def test_round_up_ladder(raw, expected):
    assert rpt._round_up(raw) == expected


def test_round_up_does_not_overshoot_badly():
    """반올림이 과하게 튀지 않아야 한다 — 예전 사다리는 200을 500으로 만들었다(2.5배)."""
    for raw in (11, 42, 99, 101, 200, 240, 499):
        assert rpt._round_up(raw) < raw * 1.5


# --- monthly_from_daily ---

def test_monthly_is_daily_times_business_days():
    assert rpt.monthly_from_daily(6, active_days=20) == 120
    assert rpt.monthly_from_daily(9, active_days=20) == 180


def test_monthly_exceeds_daily():
    """월 상한이 일 상한보다 작으면 일 상한이 무의미해진다."""
    for d in (1, 6, 9, 50):
        assert rpt.monthly_from_daily(d) > d


# --- project_month ---

def test_project_month_scales_to_30_days():
    assert rpt.project_month(observed_total=8.13, observed_days=6) == pytest.approx(40.65, abs=0.01)


def test_project_month_guards_zero_days():
    """표본 기간이 0이면 ZeroDivision 대신 0 — 리포트가 죽으면 안 된다."""
    assert rpt.project_month(10.0, 0) == 0.0
