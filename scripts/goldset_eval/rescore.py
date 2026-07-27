# -*- coding: utf-8 -*-
"""저장된 런을 **에이전트 재실행 없이** 다시 채점한다 (RPA-298).

왜 필요한가: 채점 축을 하나 늘릴 때마다 13케이스×3반복을 다시 돌리면 회당 $5~15가
든다. 예측 결과(`recommendation.json`)는 rep마다 이미 저장돼 있으므로, 지표만 바뀐
경우엔 그걸 다시 읽어 채점하면 된다 — **API 호출 0회**.

`run_eval`과 같은 `score_case`를 쓴다. 두 경로가 갈리면 재채점 수치를 원 런과 비교할 수
없으므로, 채점 로직을 여기에 복제하지 않는 것이 이 파일의 유일한 계약이다.

**엄밀 축**(RPA-298 Phase 0-1): 골드셋에 **믿을 수 있는** `정답흐름도/`(고정 정답지)가
있으면 `exact_metrics.attach_exact_axes`를 함께 돌려 `action_exact`·`action_exact_core`·중첩
경로 일치율을 나란히 낸다. 퍼지 유사도를 쓰지 않는 축이라 수치가 낮게 나오는 게 정상이다.
정답지가 없으면 기존 축만 나온다 — 정답지 생성 전에 찍힌 런도 그대로 재채점된다.
"있으면 켠다"가 아니라 `build_gold_flows.check_gold_flows`가 카탈로그 사용 여부·사상률까지
보고 판정한다 — 망가진 정답지 위의 그럴듯한 0점이 제일 위험하다.
정답지 생성: `python -m scripts.goldset_eval.build_gold_flows --goldset <골드셋>`

사용:
    python -m scripts.goldset_eval.rescore <골드셋> <런디렉터리> [<런디렉터리>...]
"""

import json
import statistics
import sys
from pathlib import Path

from .build_gold_flows import DEFAULT_OUT_NAME, check_gold_flows, load_gold_flow
from .exact_metrics import attach_exact_axes
from .glue_analysis import _flatten_pred
from .gold import load_case, merged_boilerplate, merged_sequence
from .metrics import score_case
from .notation import CanonAction

_AXES = [
    ("action", "엄격"),
    ("action_core", "실업무"),
    ("action_equiv", "기능등가"),
]

# 고정 정답지(`정답흐름도/`)가 있을 때만 추가되는 축 (RPA-298 Phase 0-1).
# 퍼지 유사도가 아니라 **KB 표기 문자열 일치**로 잰다. 기존 축과 나란히 나오며 대체하지
# 않는다 — 지난 기준선 런이 전부 기존 축으로 재졌기 때문이다.
_EXACT_AXES = [
    ("action_exact", "엄밀일치"),
    ("action_exact_core", "엄밀실업무"),
]


def _kb_canons() -> list[CanonAction]:
    """KB 달성가능 판정용 카탈로그. 없으면 빈 목록 — 그 지표만 비고 나머지는 정상."""
    try:
        from app.services.catalog import get_backend_catalog

        return [CanonAction(s["package"], s["action"])
                for s in get_backend_catalog().iter_action_schemas()]
    except Exception as e:  # noqa: BLE001 — 카탈로그 없이도 등가 축은 재계산된다
        print(f"⚠ 카탈로그 접속 실패 — achievable 지표는 비워둔다: {type(e).__name__}",
              file=sys.stderr)
        return []


def rescore_case(
    goldset: Path, case_dir: Path, entry: dict, kb: list[CanonAction],
    gold_flow: dict | None = None,
) -> list[dict]:
    """케이스의 rep마다 재채점 — rep별 score dict 목록.

    `gold_flow`(고정 정답지)를 주면 엄밀 축(`action_exact*`·`nesting`·`order_exact`)을
    같은 dict에 얹는다. 없으면 기존 축만 나온다 — 정답지 없는 환경에서도 재채점이 돈다.
    """
    gold_flows = load_case(goldset / "정답셋" / entry["case_dir"])
    if not gold_flows:
        return []
    gold_seq = merged_sequence(gold_flows)
    gold_boiler = merged_boilerplate(gold_flows)

    reps = sorted([d for d in case_dir.iterdir() if d.is_dir() and d.name.startswith("rep")]) \
        if case_dir.is_dir() else []
    targets = reps or ([case_dir] if (case_dir / "recommendation.json").is_file() else [])

    out = []
    for t in targets:
        rf = t / "recommendation.json"
        if not rf.is_file():
            continue
        rec = json.loads(rf.read_text(encoding="utf-8"))
        score = score_case(_flatten_pred(rec), gold_seq, kb, gold_boilerplate=gold_boiler)
        if gold_flow:
            # 얹는 키 목록은 `run_eval`과 **같은 함수**가 정한다 — 갈리면 재채점 수치를
            # 원 런과 비교할 수 없다.
            attach_exact_axes(score, rec, gold_flow)
        out.append(score)
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    goldset = Path(argv[1])
    manifest = json.loads((goldset / "정답셋" / "manifest.json").read_text(encoding="utf-8"))
    kb = _kb_canons()

    # 믿을 수 있는 고정 정답지가 있을 때만 엄밀 축을 낸다. 없거나 못 믿으면 기존 축만 —
    # 정답지 생성 전에 찍힌 런도 그대로 재채점되어야 한다.
    flows_dir = goldset / DEFAULT_OUT_NAME
    gold_status = check_gold_flows(flows_dir)
    for msg in gold_status.messages:
        print(msg, file=sys.stderr)
    axes = _AXES + (_EXACT_AXES if gold_status.usable else [])

    for run_arg in argv[2:]:
        run = Path(run_arg)
        print("=" * 84)
        print(f"■ {run.name}")
        print("=" * 84)
        agg: dict[str, list[float]] = {}
        cross_total = 0
        cross_examples: list[dict] = []
        n_cases = 0

        nest_scores: list[float] = []
        unresolved_total = 0

        print(f"{'#':>2} {'케이스':<24}" + "".join(f"{lbl+' f1':>12}" for _, lbl in axes)
              + f"{'교차':>6}{'중첩':>7}")
        for e in manifest["entries"]:
            gold_flow = load_gold_flow(flows_dir, e["case_dir"]) if gold_status.usable else None
            if gold_status.usable and gold_flow is None:
                # 정답지는 믿을 만한데 **이 케이스만** 없다 — 정답셋에 케이스를 추가하고
                # 정답흐름도 재생성을 잊은 상태다. run_eval은 같은 상황을 경고 후 그 케이스만
                # 엄밀 축 생략으로 넘긴다(run_eval.py의 gold_flows 수집). 여기도 같아야 한다 —
                # 예전엔 axes에 엄밀 축이 이미 들어간 채로 없는 키를 읽어 KeyError로 죽었다.
                print(f"⚠ [{e['index']:02d}] 정답지에 이 케이스가 없다 — 엄밀 축 생략: "
                      f"{e['case_dir']}", file=sys.stderr)
            scores = rescore_case(goldset, run / e["case_dir"], e, kb, gold_flow)
            if not scores:
                continue
            n_cases += 1
            cells = []
            for key, _lbl in axes:
                # 케이스별로 축이 빠질 수 있다(위 경고 경로). 없는 축은 '-'로 두고 평균에서도
                # 뺀다 — 0으로 채우면 매크로가 조용히 낮아져 "엄밀 채점이 나쁘다"로 오독된다.
                vals = [s[key]["f1"] for s in scores if key in s]
                if not vals:
                    cells.append(f"{'-':>12}")
                    continue
                m = statistics.mean(vals)
                agg.setdefault(key + ".f1", []).append(m)
                agg.setdefault(key + ".recall", []).append(
                    statistics.mean(s[key]["recall"] for s in scores if key in s))
                agg.setdefault(key + ".precision", []).append(
                    statistics.mean(s[key]["precision"] for s in scores if key in s))
                cells.append(f"{m:>12.3f}")
            xs = statistics.mean(s["action_equiv"]["n_cross_package"] for s in scores)
            cross_total += xs
            for s in scores:
                cross_examples.extend(s["equiv_pairs"])
            # 중첩 경로 일치율 — 매칭쌍이 없으면(path_match=None) 평균에서 뺀다.
            nests = [s["nesting"]["path_match"] for s in scores
                     if s.get("nesting") and s["nesting"]["path_match"] is not None]
            nest = statistics.mean(nests) if nests else None
            if nest is not None:
                nest_scores.append(nest)
            if scores[0].get("action_exact_resolved"):
                unresolved_total += scores[0]["action_exact_resolved"]["n_gold_unresolved"]
            print(f"{e['index']:>2} {e['bot_name'][5:28]:<24}" + "".join(cells)
                  + f"{xs:>6.1f}" + (f"{nest:>7.3f}" if nest is not None else f"{'-':>7}"))

        print("-" * 84)
        if not n_cases:
            print("  채점할 rep이 없다 — 런 디렉터리에 recommendation.json이 있는지 확인할 것\n")
            continue
        print(f"매크로 ({n_cases}케이스)")
        for key, lbl in axes:
            # 그 축을 낸 케이스가 하나도 없으면(정답지에 케이스가 통째로 빠진 골드셋)
            # 평균을 낼 표본이 없다 — 0.000으로 찍으면 "채점 결과가 나쁘다"로 읽힌다.
            vals = agg.get(key + ".f1") or []
            if not vals:
                print(f"  {lbl:<10} — (이 축을 낸 케이스 없음)")
                continue
            n_axis = len(vals)
            suffix = f"  [{n_axis}/{n_cases}케이스]" if n_axis != n_cases else ""
            print(f"  {lbl:<10} f1={statistics.mean(vals):.3f}"
                  f"  precision={statistics.mean(agg[key+'.precision']):.3f}"
                  f"  recall={statistics.mean(agg[key+'.recall']):.3f}{suffix}")
        print(f"  케이스당 교차패키지 매칭 평균 {cross_total/max(n_cases,1):.2f}건")
        if nest_scores:
            print(f"  중첩 경로 일치율 {statistics.mean(nest_scores):.3f}"
                  f"  (정답지 미사상 액션 {unresolved_total}건은 엄밀 축에서 절대 안 맞는다)")

        # 발동한 등가를 빈도순으로 — 도메인 표가 사람 판단이라 감사 가능해야 한다.
        tally: dict[tuple, int] = {}
        for x in cross_examples:
            tally[(x["domain"], x["gold"][0], x["pred"][0])] = \
                tally.get((x["domain"], x["gold"][0], x["pred"][0]), 0) + 1
        if tally:
            print("\n  발동한 등가 (도메인 | 정답 패키지 → 에이전트 패키지 | 건수):")
            for (dom, gp, pp), n in sorted(tally.items(), key=lambda x: -x[1]):
                print(f"    {dom:<12} {gp:<22} → {pp:<26} {n:>4}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
