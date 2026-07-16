"""두 평가 런 비교 — 케이스별 지표 델타 표.

사용: python -m scripts.goldset_eval.compare_runs <run_dir_A> <run_dir_B>
(A=기준(베이스라인), B=개선 후)
"""

import json
import sys
from pathlib import Path

_KEYS = ["precision", "recall", "recall_achv", "f1", "pkg_f1", "order"]


def _rows(run_dir: Path) -> dict[int, dict]:
    data = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    return {r["index"]: r for r in data["rows"]}


def main() -> int:
    a_dir, b_dir = Path(sys.argv[1]), Path(sys.argv[2])
    a, b = _rows(a_dir), _rows(b_dir)
    idxs = sorted(set(a) | set(b))

    def fmt(v):
        return f"{v:.3f}" if isinstance(v, float) else ("—" if v is None else str(v))

    print(f"A = {a_dir.name}\nB = {b_dir.name}\n")
    header = ["idx", "bot"] + [f"{k}(A→B)" for k in _KEYS]
    print(" | ".join(header))
    print("-" * 110)
    sums: dict[str, list[float]] = {k: [] for k in _KEYS}
    for i in idxs:
        ra, rb = a.get(i, {}), b.get(i, {})
        cells = [f"{i:02d}", (ra.get("bot_name") or rb.get("bot_name", ""))[:28]]
        for k in _KEYS:
            va, vb = ra.get(k), rb.get(k)
            cells.append(f"{fmt(va)}→{fmt(vb)}")
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
                sums[k].append(vb - va)
        print(" | ".join(cells))
    print("-" * 110)
    deltas = [
        f"{k}: {sum(v)/len(v):+.3f}" for k, v in sums.items() if v
    ]
    print("평균 델타 — " + " · ".join(deltas))
    return 0


if __name__ == "__main__":
    sys.exit(main())
