"""접착 액션(connective tissue) 격차 정량화 — 정답 봇 vs 에이전트.

사용자 관찰(2026-07-16): 실봇은 큰 액션 사이를 잇는 부수 액션(가드·파생값·로깅)을
포함하는데 우리 에이전트는 골격만 만든다. 그 격차를 유형별로 집계해 어느 접착이 실제로
큰지 본다(무턱대고 elaboration 넣으면 과생성 → precision 붕괴).

분류(패키지·액션 의미 기준, 구/신 표기 모두 인식):
- core      : 큰 액션 단위 (Excel·Email·Browser·REST·DLL·Loop·ErrorHandler·PDF·Word·XML…)
- guard     : If/Else/ElseIf, Boolean 비교 — 조건 가드
- derived   : Datetime·Number 변환, String 변형(toNumber/replace/substring/length…) — 파생값 계산
- assembly  : String/assign, Dictionary/put, List/Record 조립 — 값을 변수로 조립(절반은 스타일)
- logging   : LogToFile·MessageBox·Screen 캡처·File 삭제(청소)·TaskBot 중지 — 무인 운영·기록

에이전트 카운트는 rep 평균(반복 폴더가 있으면). 결과는 케이스별·매크로 표.
"""

import json
import re
from pathlib import Path

# 패키지명 정규화 — 구/신 표기 흡수
_PKG = {
    "error handler": "errorhandler", "errorhandler": "errorhandler",
    "message box": "messagebox", "messagebox": "messagebox",
    "log to file": "logtofile", "logtofile": "logging", "logging": "logging",
    "data table": "datatable", "datatable": "datatable",
    "csv/txt": "csv", "csv txt": "csv",
}


def _norm_pkg(p: str) -> str:
    n = re.sub(r"[_/\.\-]+", " ", (p or "").strip()).lower()
    n = re.sub(r"\s+", " ", n)
    return _PKG.get(n, n.replace(" ", ""))


def classify(package: str, action: str) -> str:
    """(package, action) → 접착 유형. 구/신 표기 모두 대응."""
    p = _norm_pkg(package)
    a = (action or "").lower()

    # guard — If 분기 / Boolean 비교
    if p == "if":
        return "guard"
    if p == "boolean" and ("compare" in a or "equal" in a):
        return "guard"

    # derived — 날짜·수 변환, 문자열 변형
    if p == "datetime":
        return "derived"
    if p == "number" and a not in ("assign",):  # numberAssign은 조립에 가깝다
        return "derived"
    if p == "string" and any(k in a for k in ("tonumber", "replace", "substring", "length",
                                              "uppercase", "lowercase", "trim", "reverse", "compare")):
        return "derived"

    # assembly — 값을 변수로 조립 (절반은 스타일)
    if p == "string" and "assign" in a:
        return "assembly"
    if p == "number" and a == "assign":
        return "assembly"
    if p in ("dictionary", "list", "record") and any(k in a for k in ("assign", "put", "add", "insert", "set")):
        return "assembly"

    # logging — 무인 운영·기록·청소
    if p in ("logging", "logtofile"):
        return "logging"
    if p == "messagebox":
        return "logging"
    if p == "screen":
        return "logging"
    if p == "taskbot" and "stop" in a:
        return "logging"
    if p == "file" and ("delete" in a):
        return "logging"

    return "core"


CATS = ["core", "guard", "derived", "assembly", "logging"]


def _flatten_pred(rec: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def walk(actions):
        for x in actions or []:
            p, a = x.get("package"), x.get("action")
            if p and a and _norm_pkg(p) != "step":
                out.append((p, a))
            walk(x.get("children"))

    for s in rec.get("steps") or []:
        walk(s.get("actions"))
    return out


def _counts(seq: list[tuple[str, str]]) -> dict:
    c = dict.fromkeys(CATS, 0)
    for p, a in seq:
        c[classify(p, a)] += 1
    return c


def analyze_case(gold_seq: list[tuple[str, str]], case_dir: Path) -> dict:
    """정답 vs 에이전트(rep 평균) 유형별 카운트."""
    gold = _counts(gold_seq)
    # rep 폴더 또는 단일
    rep_dirs = sorted([d for d in case_dir.iterdir() if d.is_dir() and d.name.startswith("rep")]) \
        if case_dir.is_dir() else []
    targets = rep_dirs or ([case_dir] if (case_dir / "recommendation.json").is_file() else [])
    pred_counts = []
    for t in targets:
        rf = t / "recommendation.json"
        if rf.is_file():
            pred_counts.append(_counts(_flatten_pred(json.loads(rf.read_text(encoding="utf-8")))))
    if pred_counts:
        pred = {k: round(sum(pc[k] for pc in pred_counts) / len(pred_counts), 1) for k in CATS}
    else:
        pred = dict.fromkeys(CATS, 0)
    return {"gold": gold, "pred": pred, "n_reps": len(pred_counts)}


if __name__ == "__main__":
    import sys

    from .gold import load_case, merged_sequence

    goldset = Path(sys.argv[1])
    run = Path(sys.argv[2])
    manifest = json.loads((goldset / "정답셋" / "manifest.json").read_text(encoding="utf-8"))

    agg_gold = dict.fromkeys(CATS, 0.0)
    agg_pred = dict.fromkeys(CATS, 0.0)
    n = 0
    print(f"{'#':>2s} {'케이스':22s} | " + " ".join(f"{c[:5]:>11s}" for c in CATS))
    print(f"{'':22s}   |  (정답 / 에이전트평균)")
    for e in manifest["entries"]:
        case_dir = run / e["case_dir"]
        gold_flows = load_case(goldset / "정답셋" / e["case_dir"])
        if not gold_flows:
            continue
        gold_seq = merged_sequence(gold_flows)
        r = analyze_case(gold_seq, case_dir)
        if r["n_reps"] == 0:
            continue
        n += 1
        cells = []
        for c in CATS:
            g, p = r["gold"][c], r["pred"][c]
            agg_gold[c] += g
            agg_pred[c] += p
            cells.append(f"{g:>4}/{p:<5}")
        print(f"{e['index']:2d} {e['bot_name'][5:27]:22s} | " + " ".join(f"{x:>11s}" for x in cells))
    print(f"\n매크로 합계 ({n}케이스):")
    for c in CATS:
        g, p = agg_gold[c], agg_pred[c]
        gap = g - p
        print(f"  {c:9s}: 정답 {g:5.0f} / 에이전트 {p:6.1f}  → 놓친 접착 {gap:6.1f}"
              + ("  ★" if c != "core" and gap > 20 else ""))
