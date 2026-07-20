"""골드 vs 예측 액션 시퀀스 채점.

1) 탐욕 1:1 매칭(유사도 내림차순, 동률이면 위치차 오름차순)
2) 액션 P/R/F1 + '달성가능 재현율'(KB에 동치 액션이 아예 없는 골드 액션 제외)
3) 순서 보존율: 매칭쌍을 골드 순서로 놓았을 때 예측 위치의 LIS 비율
4) 패키지 멀티셋 P/R/F1

KB gap(달성 불가) 판정이 이 평가의 1급 산출물이다 — 에이전트 결함과 KB 결손을
분리하지 않으면 개선 방향을 잘못 잡는다.
"""

from bisect import bisect_left
from dataclasses import dataclass, field

from .notation import CONTAINER_PKG_KEYS, MATCH_THRESHOLD, CanonAction


@dataclass
class MatchResult:
    pairs: list[dict] = field(default_factory=list)      # 매칭된 (pred_i, gold_j, sim)
    pred_unmatched: list[int] = field(default_factory=list)
    gold_unmatched: list[int] = field(default_factory=list)


def greedy_match(pred: list[CanonAction], gold: list[CanonAction]) -> MatchResult:
    cands = []
    for i, p in enumerate(pred):
        for j, g in enumerate(gold):
            s = p.sim(g)
            if s >= MATCH_THRESHOLD:
                cands.append((s, -abs(i - j), i, j))
    cands.sort(reverse=True)  # 유사도 desc, 위치차 asc

    used_p: set[int] = set()
    used_g: set[int] = set()
    res = MatchResult()
    for s, _negd, i, j in cands:
        if i in used_p or j in used_g:
            continue
        used_p.add(i)
        used_g.add(j)
        res.pairs.append({"pred_i": i, "gold_j": j, "sim": round(s, 3)})
    res.pred_unmatched = [i for i in range(len(pred)) if i not in used_p]
    res.gold_unmatched = [j for j in range(len(gold)) if j not in used_g]
    return res


def _lis_len(seq: list[int]) -> int:
    tails: list[int] = []
    for x in seq:
        k = bisect_left(tails, x)
        if k == len(tails):
            tails.append(x)
        else:
            tails[k] = x
    return len(tails)


def order_score(match: MatchResult) -> float | None:
    """매칭쌍 ≥2일 때, 골드 순으로 정렬한 예측 인덱스의 LIS 비율."""
    if len(match.pairs) < 2:
        return None
    by_gold = sorted(match.pairs, key=lambda p: p["gold_j"])
    return round(_lis_len([p["pred_i"] for p in by_gold]) / len(by_gold), 3)


def _prf(n_match: int, n_pred: int, n_gold: int) -> dict:
    p = n_match / n_pred if n_pred else 0.0
    r = n_match / n_gold if n_gold else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(f, 3)}


def package_prf(pred: list[CanonAction], gold: list[CanonAction]) -> dict:
    """패키지 정준 키 멀티셋 P/R/F1."""
    from collections import Counter

    cp, cg = Counter(a.pkg_key for a in pred), Counter(a.pkg_key for a in gold)
    overlap = sum((cp & cg).values())
    return _prf(overlap, sum(cp.values()), sum(cg.values()))


def score_case(
    pred_raw: list[tuple[str, str]],
    gold_raw: list[tuple[str, str]],
    kb_canons: list[CanonAction],
) -> dict:
    pred = [CanonAction(p, a) for p, a in pred_raw]
    gold = [CanonAction(p, a) for p, a in gold_raw]
    match = greedy_match(pred, gold)

    # KB 달성 가능성: 골드 액션마다 KB 전체에서 최고 유사도
    kb_by_pkg: dict[str, list[CanonAction]] = {}
    for c in kb_canons:
        kb_by_pkg.setdefault(c.pkg_key, []).append(c)
    achievable_flags: list[bool] = []
    gap_detail: list[dict] = []
    for g in gold:
        best, best_raw = 0.0, None
        for c in kb_by_pkg.get(g.pkg_key, []):
            s = g.sim(c)
            if s > best:
                best, best_raw = s, c.raw
        # 컨테이너 패키지(Loop/If/Step/Error handler)는 검수기가 KB 스키마 없이 허용 —
        # KB에 액션 스키마가 없어도 에이전트가 출력할 수 있으므로 달성 가능으로 본다.
        ok = best >= MATCH_THRESHOLD or g.pkg_key in CONTAINER_PKG_KEYS
        achievable_flags.append(ok)
        if not ok:
            gap_detail.append({
                "gold": list(g.raw),
                "kb_best": list(best_raw) if best_raw else None,
                "kb_best_sim": round(best, 3),
            })

    n_match = len(match.pairs)
    matched_gold = {p["gold_j"] for p in match.pairs}
    n_gold_achv = sum(achievable_flags)
    n_match_achv = sum(1 for j in matched_gold if achievable_flags[j])

    return {
        "n_pred": len(pred),
        "n_gold": len(gold),
        "n_matched": n_match,
        "action": _prf(n_match, len(pred), len(gold)),
        "action_achievable": {
            **_prf(n_match_achv, len(pred), n_gold_achv),
            "n_gold_achievable": n_gold_achv,
        },
        "package": package_prf(pred, gold),
        "order_score": order_score(match),
        "kb_gaps": gap_detail,
        "pairs": [
            {
                "pred": list(pred[p["pred_i"]].raw),
                "gold": list(gold[p["gold_j"]].raw),
                "sim": p["sim"],
            }
            for p in sorted(match.pairs, key=lambda x: x["gold_j"])
        ],
        "pred_only": [list(pred[i].raw) for i in match.pred_unmatched],
        "gold_only": [
            {"action": list(gold[j].raw), "achievable": achievable_flags[j]}
            for j in match.gold_unmatched
        ],
    }
