# -*- coding: utf-8 -*-
"""RPA-89 — 게이지 임계 캘리브레이션 리포트.

관측 DB의 llm_usage(purpose='intake')에서 세션별 대화 누적을 분석해 게이지 임계
(LIMIT·WARN_RATIO)의 **권장값**을 산출한다.

설계 원칙(통제형 거버넌스):
- 이 스크립트는 **자동으로 임계를 바꾸지 않는다.** 리포트만 출력하고, 사람이 값을
  검토해 env(TURN_GAUGE_LIMIT_TOKENS / TURN_GAUGE_WARN_RATIO)를 갱신한다.
- 근거를 남긴다 — 어떤 데이터(표본 수·기간)에서 나온 권장인지 함께 출력한다.

사용법:
  python scripts/gauge_calibration_report.py                 # 관측 DB 전체
  python scripts/gauge_calibration_report.py --limit 6000    # 특정 LIMIT 가정 시 WARN_RATIO
  python scripts/gauge_calibration_report.py --headroom 4    # 경고 후 여유 턴 수(기본 4)
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

def median(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def session_deltas(series: list[list[int]]) -> list[dict]:
    """세션별 intake 토큰 시계열 → 턴당 증가량 통계. 3턴 미만 세션은 제외(Δ 추정 불가)."""
    out = []
    for toks in series:
        if len(toks) < 3:
            continue
        deltas = [toks[i + 1] - toks[i] for i in range(len(toks) - 1)]
        out.append({
            "turns": len(toks),
            "median_delta": median(deltas),
            "first": toks[0],
            "last": toks[-1],
        })
    return out


def robust_delta(sessions: list[dict]) -> float:
    """세션별 Δ중앙값들의 중앙값 — 세션 길이 편차에 강건한 '턴당 누적' 대표값."""
    return median([s["median_delta"] for s in sessions])


def turns_to_ratio(base: float, delta: float, limit: int, ratio: float) -> float:
    """base 토큰에서 시작해 턴당 delta씩 늘 때, ratio*limit에 닿기까지의 턴 수."""
    if delta <= 0:
        return float("inf")
    return max(0.0, (ratio * limit - base) / delta)


def warn_ratio(delta: float, limit: int, headroom_turns: int) -> float:
    """'하드 임계 도달 headroom_turns턴 전에 경고'가 되는 WARN_RATIO = 1 − N·Δ/LIMIT."""
    if limit <= 0:
        return 0.7
    return round(max(0.0, 1 - headroom_turns * delta / limit), 3)


# ── 리포트 (DB 조회) ──────────────────────────────────────────────────────

def _fetch_intake_series() -> list[list[int]]:
    from sqlalchemy import text

    from app.core.observability_db import observability_sessionmaker
    with observability_sessionmaker()() as s:
        rows = s.execute(text(
            "select session_id, input_tokens from llm_usage "
            "where purpose='intake' and session_id is not null order by session_id, id"
        )).fetchall()
    series: dict = {}
    for sid, tok in rows:
        series.setdefault(sid, []).append(int(tok))
    return list(series.values())


def _fetch_message_token_p95() -> tuple[int, int] | None:
    """앱 DB의 chat_messages(user 역할)에서 메시지 토큰 분포 p95 — 선행 가드 캘리브레이션.

    관측 DB엔 메시지 원문이 없다(llm_usage는 토큰 수만). 원문은 앱 DB에 있으므로
    거기서 읽어 실제 토큰으로 추정한다. best-effort(앱 DB 접속 실패 시 None)."""
    try:
        from sqlalchemy import text

        from app.api.sessions import _estimate_message_tokens
        from app.db import SessionLocal
        with SessionLocal() as s:
            rows = s.execute(text(
                "select content from chat_messages where role='user'"
            )).fetchall()
        toks = sorted(_estimate_message_tokens(r[0]) for r in rows if r[0])
        if not toks:
            return None
        return toks[int(len(toks) * 0.95)], max(toks)
    except Exception:  # noqa: BLE001 — 앱 DB 미가용이면 이 섹션만 생략
        return None


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv()

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="가정할 LIMIT(미지정 시 후보 표)")
    ap.add_argument("--headroom", type=int, default=4, help="경고 후 여유 턴 수(WARN_RATIO 도출)")
    args = ap.parse_args()

    series = _fetch_intake_series()
    sessions = session_deltas(series)
    all_tokens = [t for s in series for t in s]

    print("=" * 68)
    print("게이지 임계 캘리브레이션 리포트 (RPA-89)")
    print("=" * 68)
    print(f"표본: intake 세션 {len(series)}개 / 그중 3턴+ {len(sessions)}개 / intake 행 {len(all_tokens)}개")
    if len(sessions) < 5:
        print("⚠️  3턴+ 세션이 5개 미만 — 통계적으로 이르다. 데이터를 더 모으고 재실행 권장.")
    if not sessions:
        print("중단: 분석할 멀티턴 세션이 없음."); return

    delta = robust_delta(sessions)
    base = median([s["first"] for s in sessions])
    overall_deltas = [
        toks[i + 1] - toks[i] for toks in series if len(toks) >= 2
        for i in range(len(toks) - 1)
    ]
    print(f"\n── 관측된 대화 누적 ──")
    print(f"  턴당 증가 Δ (세션중앙값들의 중앙값): {delta:.0f} 토큰/턴")
    print(f"  전체 턴간 Δ 중앙값: {median(overall_deltas):.0f} 토큰/턴")
    print(f"  첫 턴 intake(base) 중앙값: {base:.0f} 토큰")
    print(f"  intake 토큰 분포: min={min(all_tokens)} / 중앙={median(all_tokens):.0f} "
          f"/ p95={sorted(all_tokens)[int(len(all_tokens) * 0.95)]} / max={max(all_tokens)}")

    # 현재 LIMIT 진단
    import os
    cur_limit = int(os.getenv("TURN_GAUGE_LIMIT_TOKENS", "100000"))
    fill = max(all_tokens) / cur_limit * 100
    print(f"\n── 현재 설정 진단 (LIMIT={cur_limit}) ──")
    print(f"  최대 관측 intake가 LIMIT의 {fill:.1f}%밖에 안 참.")
    if fill < 25:
        t_hard = turns_to_ratio(base, delta, cur_limit, 1.0)
        print(f"  ❌ LIMIT이 과도하게 큼 — 이 누적 속도면 하드 도달에 ~{t_hard:.0f}턴 필요(비현실).")
        print(f"     → compact/경고가 사실상 안 터진다. LIMIT을 실제 대화 규모에 맞게 낮춰야 함.")
    elif fill > 90:
        print(f"  ⚠️  LIMIT이 작을 수 있음 — 세션이 자주 임계를 넘음. 상향 검토.")
    else:
        print(f"  ✓ LIMIT이 대체로 적정 범위.")

    # LIMIT 후보 표
    print(f"\n── LIMIT 후보별 동작 (base={base:.0f}, Δ={delta:.0f}/턴 기준) ──")
    print(f"  {'LIMIT':>7} | {'경고(0.7)까지':>12} | {'하드(1.0)까지':>12} | 비고")
    for L in sorted({3000, 6000, 10000, 20000, cur_limit}):
        tw = turns_to_ratio(base, delta, L, 0.7)
        th = turns_to_ratio(base, delta, L, 1.0)
        note = "데모 시연에 적합" if 6 <= th <= 25 else ("너무 빨리 터짐" if th < 6 else "거의 안 터짐")
        print(f"  {L:>7} | {tw:>10.0f}턴 | {th:>10.0f}턴 | {note}")

    # WARN_RATIO 권장
    target_limit = args.limit or 6000
    wr = warn_ratio(delta, target_limit, args.headroom)
    print(f"\n── WARN_RATIO 권장 (LIMIT={target_limit} 가정, 경고 후 {args.headroom}턴 여유) ──")
    print(f"  WARN_RATIO = 1 − {args.headroom}×{delta:.0f}/{target_limit} = {wr}")
    print(f"  (현재 하드코딩값 0.7과 비교해 결정. 근거: '하드 전 {args.headroom}턴 여유')")

    # 선행 가드용 message p95
    mp = _fetch_message_token_p95()
    if mp:
        print(f"\n── 선행 가드(RPA-86) 캘리브레이션: message 토큰 분포 ──")
        print(f"  사용자 메시지 토큰 p95={mp[0]} / max={mp[1]}")
        print(f"  → '대형 입력'의 실측 정의. 선행 가드가 이 크기의 단일 입력을 잡는지 검증 가능.")

    print(f"\n{'=' * 68}")
    print("적용: 이 리포트는 권장일 뿐이다. 값을 검토해 .env(TURN_GAUGE_LIMIT_TOKENS,")
    print("TURN_GAUGE_WARN_RATIO)를 사람이 갱신하고, 변경 근거를 커밋/ADR로 남긴다.")


if __name__ == "__main__":
    main()
