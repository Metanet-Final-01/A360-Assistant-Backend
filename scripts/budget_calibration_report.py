# -*- coding: utf-8 -*-
"""RPA-171 — LLM 예산 상한 캘리브레이션 리포트.

관측 DB의 llm_usage에서 **대화의 실제 모양**(세션당 턴 수 · 턴당 비용 · 주체별 일 사용량)을
분석해 예산 상한(BUDGET_*_USD)의 **권장값**을 산출한다.

설계 원칙(gauge_calibration_report.py와 동일 — 통제형 거버넌스):
- 이 스크립트는 **자동으로 상한을 바꾸지 않는다.** 리포트만 출력하고, 사람이 검토해 env를 갱신한다.
- **근거를 남긴다** — 어떤 표본(행수·기간·사용자수)에서 나온 권장인지, 표본이 약하면 왜 약한지
  함께 출력한다. 근거 없는 숫자가 서비스를 막는 것이 이 리포트가 막으려는 사고다.

왜 필요한가: 상한을 감으로 정하면 (a) 너무 낮으면 정상 사용자가 429를 맞고, (b) 너무 높으면
방어가 안 된다. 실제로 RPA-171 최초 구현의 .env.example 예시값($1/일)은 실측 최다 사용자의
하루($2.02)보다 낮아, 켰다면 정상 사용자를 막았을 값이었다.

유도 방식: 일 상한 ≈ (사용자별 일 최대 비용) × headroom. 턴 단위 재료(턴 수·턴당 비용)도 함께
출력해 "몇 턴 쓰는 사용자를 허용하려는가"로 검산할 수 있게 한다.

사용법:
  python scripts/budget_calibration_report.py                # 관측 DB 전체
  python scripts/budget_calibration_report.py --headroom 2.5 # 여유 배수(기본 2.5)
  python scripts/budget_calibration_report.py --days 7       # 최근 N일만
"""

import argparse
import statistics
import sys
from pathlib import Path

# scripts/에서 실행해도 app 패키지를 찾도록 리포 루트를 path에 추가
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows 콘솔(cp949)에서도 한글·기호가 깨지지 않게
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# ── 순수 계산 (테스트 대상) ───────────────────────────────────────────────

def percentile(xs: list[float], p: float) -> float:
    """정렬 후 p분위(0~1). 빈 리스트는 0."""
    if not xs:
        return 0.0
    s = sorted(xs)
    idx = min(int(len(s) * p), len(s) - 1)
    return s[idx]


def _round_up(raw: float) -> float:
    """env에 쓰기 좋은 자리로 올림. 자릿수에 비례해 성글게 — 정밀도 과신을 주지 않되,
    과하게 튀지도 않게 한다(예전 사다리는 200 → 500으로 2.5배 튀었다)."""
    import math

    if raw <= 0:
        return 0.0
    if raw < 10:
        return float(math.ceil(raw))            # 5.05 -> 6
    if raw < 100:
        return float(math.ceil(raw / 5) * 5)    # 42 -> 45
    return float(math.ceil(raw / 50) * 50)      # 202 -> 250


def recommend_limit(observed_max: float, headroom: float) -> float:
    """관측 최대치 × 여유배수 → 보기 좋은 자리로 올림.

    최대치 기준인 이유: 상한의 목적은 '정상 사용자를 막지 않으면서 폭주를 막는 것'이라,
    중앙값 기준이면 헤비 유저가 상시 차단된다. 관측이 없으면(0) 권장하지 않는다(0 반환).
    """
    if observed_max <= 0:
        return 0.0
    return _round_up(observed_max * headroom)


def monthly_from_daily(daily_limit: float, active_days: int = 20) -> float:
    """월 상한 = 일 상한 × 영업일 가정. 일 상한을 매일 꽉 채우는 사용자는 없다고 본다.

    여기서 다시 올림하지 않는다 — daily_limit이 이미 _round_up을 거친 값이라, 또 올리면
    이중 반올림으로 상한이 의도보다 헐거워진다(6/일 → 120/월이 맞는데 150이 됐다).
    """
    return float(daily_limit * active_days)


def project_month(observed_total: float, observed_days: int) -> float:
    """관측 기간 총액 → 30일 환산. 표본 기간이 짧으면 과대/과소가 크다(리포트에 경고)."""
    if observed_days <= 0:
        return 0.0
    return observed_total / observed_days * 30


# ── 리포트 (DB 조회) ──────────────────────────────────────────────────────

def _rows(sql: str, **kw):
    from sqlalchemy import text

    from app.core.observability_db import observability_sessionmaker
    with observability_sessionmaker()() as s:
        return s.execute(text(sql), kw).all()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headroom", type=float, default=2.5, help="관측 최대치 대비 여유 배수")
    ap.add_argument("--days", type=int, default=0, help="최근 N일만 (0=전체)")
    args = ap.parse_args()

    # ⚠️ .env를 먼저 읽어야 관측 DB(Neon)를 본다 — 안 하면 OBSERVABILITY_DATABASE_URL이 비어
    #    로컬 앱 DB로 폴백해 **엉뚱한 표본으로 상한을 권고**한다(실측으로 잡음: 6163행 대신
    #    36행을 읽었다). gauge_calibration_report.py도 같은 이유로 여기서 load_dotenv를 부른다.
    from dotenv import load_dotenv
    load_dotenv()

    where = "created_at >= now() - make_interval(days => :d)" if args.days else "true"
    p = {"d": args.days} if args.days else {}

    # ── 표본
    r = _rows(f"select count(*), min(created_at)::date, max(created_at)::date, "
              f"count(distinct user_id), count(distinct session_id), count(request_id) "
              f"from llm_usage where {where}", **p)[0]
    total, d0, d1, n_users, n_sessions, n_rid = r
    if not total:
        print("관측 DB에 llm_usage 데이터가 없습니다 — 트래픽을 쌓은 뒤 다시 실행하세요.")
        return 1
    span_days = max((d1 - d0).days + 1, 1) if d0 and d1 else 1

    print("=" * 72)
    print("LLM 예산 상한 캘리브레이션 리포트 (RPA-171)")
    print("=" * 72)
    print(f"표본: llm_usage {total}행 | {d0} ~ {d1} ({span_days}일) | "
          f"사용자 {n_users}명 | 세션 {n_sessions}개")

    # 표본 신뢰도 경고 — 근거의 약점을 숨기지 않는다
    warnings = []
    if span_days < 7:
        warnings.append(f"관측 기간이 {span_days}일뿐 — 월 환산은 오차가 크다")
    if n_users < 5:
        warnings.append(f"사용자 {n_users}명뿐 — 사용자별 분포가 대표성이 없다")
    rid_pct = n_rid * 100 // total
    if rid_pct < 80:
        warnings.append(f"request_id가 {rid_pct}%뿐 — 턴 단위 통계 표본이 작다. "
                        f"RPA-158(2026-07-14) 이전 행에는 없고 **소급 복원이 불가능**하다"
                        f"(런타임 값이라 backfill_llm_cost.py로도 못 채운다). 새 트래픽이 쌓이면 올라간다")
    for w in warnings:
        print(f"  ⚠️  {w}")

    # ── 턴 단위 재료 ("몇 턴 쓰는 사용자를 허용할 것인가"로 검산하게)
    turn_costs = [float(x[0]) for x in _rows(
        f"select sum(cost_usd) from llm_usage where request_id is not null and {where} "
        f"group by request_id having sum(cost_usd) > 0", **p)]
    print("\n── 턴당 비용 (같은 request_id = 한 턴, RPA-158)")
    if turn_costs:
        print(f"  턴 {len(turn_costs)}개 | 중앙 ${statistics.median(turn_costs):.4f} | "
              f"p90 ${percentile(turn_costs, 0.9):.4f} | 최대 ${max(turn_costs):.4f}")
    else:
        print("  (request_id 있는 턴이 없어 산출 불가)")

    turns_per_session = [int(x[0]) for x in _rows(
        f"select count(*) from llm_usage where purpose='intake' and session_id is not null "
        f"and {where} group by session_id", **p)]
    print("\n── 세션당 턴 수 (purpose='intake' = 턴 1회)")
    if turns_per_session:
        print(f"  세션 {len(turns_per_session)}개 | 중앙 {statistics.median(turns_per_session):.0f}턴 | "
              f"p90 {percentile(turns_per_session, 0.9):.0f}턴 | 최대 {max(turns_per_session)}턴")

    # ── 주체별 일 사용량 → 일 상한 유도
    user_days = [float(x[2]) for x in _rows(
        f"select user_id, created_at::date, sum(cost_usd) from llm_usage "
        f"where user_id is not null and {where} group by user_id, created_at::date "
        f"having sum(cost_usd) > 0", **p)]
    print("\n── 사용자별 하루 비용 (일 상한의 직접 근거)")
    subject_daily = 0.0
    if user_days:
        subject_daily = max(user_days)
        print(f"  사용자-일 {len(user_days)}건 | 중앙 ${statistics.median(user_days):.4f} | "
              f"p90 ${percentile(user_days, 0.9):.4f} | **최대 ${subject_daily:.4f}**")
    else:
        print("  (사용자 귀속 비용 없음 — 익명 트래픽뿐이면 session 기준으로 봐야 한다)")

    global_days = [float(x[1]) for x in _rows(
        f"select created_at::date, sum(cost_usd) from llm_usage where {where} "
        f"group by created_at::date having sum(cost_usd) > 0", **p)]
    print("\n── 전역 하루 비용 (전역 상한의 직접 근거)")
    global_daily = max(global_days) if global_days else 0.0
    if global_days:
        print(f"  {len(global_days)}일 | 중앙 ${statistics.median(global_days):.4f} | "
              f"**최대 ${global_daily:.4f}** | 기간 총 ${sum(global_days):.4f}")

    # ── 권장값
    print("\n" + "=" * 72)
    print(f"권장 상한 (관측 최대치 × 여유 {args.headroom}배)")
    print("=" * 72)
    sd = recommend_limit(subject_daily, args.headroom)
    gd = recommend_limit(global_daily, args.headroom)
    print(f"  BUDGET_SUBJECT_DAILY_USD={sd:g}      # 실측 최대 ${subject_daily:.4f}/사용자-일")
    print(f"  BUDGET_SUBJECT_MONTHLY_USD={monthly_from_daily(sd):g}    # 일 상한 × 영업일 20")
    print(f"  BUDGET_GLOBAL_DAILY_USD={gd:g}       # 실측 최대 ${global_daily:.4f}/일")
    print(f"  BUDGET_GLOBAL_MONTHLY_USD={monthly_from_daily(gd):g}     # 일 상한 × 영업일 20")
    print(f"\n  참고 — 관측 기간 30일 환산: ${project_month(sum(global_days), span_days):.2f}")
    if turn_costs and sd:
        print(f"  검산 — 일 상한 ${sd:g}이면 p90 턴(${percentile(turn_costs, 0.9):.4f}) 기준 "
              f"약 {int(sd / max(percentile(turn_costs, 0.9), 1e-9))}턴/일 허용")
    print("\n  ⚠️  이 값을 그대로 붙여넣지 말 것 — 위 표본 경고를 먼저 확인하고, 데모/운영 트래픽이")
    print("      쌓인 뒤 재실행해 검토하세요. 상한을 켜는 판단은 사람이 합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
