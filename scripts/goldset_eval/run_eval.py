"""골드셋 평가 러너 — 업무정의서 PDF → analyze → v3 recommend → 골드 비교 채점.

사용:
    python -m scripts.goldset_eval.run_eval --goldset "...\\골드셋" --out "...\\평가결과" \
        [--cases 1,2,3] [--tag baseline] [--timeout 720]

케이스마다 저장: analysis.json / recommendation.json / events.jsonl / score.json
전체 저장: summary.json / report.md

실패 격리: 케이스 하나의 예외는 기록 후 다음 케이스로 진행한다.
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("goldset_eval")
logger.setLevel(logging.INFO)


def _pred_sequence(rec: dict) -> list[tuple[str, str]]:
    """Recommendation dict → pre-order (package, action) 시퀀스.

    Step/Comment 구획은 골드 쪽(gold.SCAFFOLD)과 동일하게 제외한다 — 에이전트가
    Step을 트리 안 액션으로도 내놓는 것이 스모크에서 실측됐다(비대칭 감점 방지).
    """
    from .notation import is_scaffold

    seq: list[tuple[str, str]] = []

    def walk(actions: list) -> None:
        for a in actions or []:
            if isinstance(a, dict):
                pkg, act = a.get("package"), a.get("action")
                if pkg and act and not is_scaffold(pkg, act):
                    seq.append((pkg, act))
                walk(a.get("children"))

    for step in rec.get("steps") or []:
        if isinstance(step, dict):
            walk(step.get("actions"))
    return seq


def _pred_structure(rec: dict, pred_canons) -> dict:
    """예측 흐름도의 구조 요약 — 골드 structure와 비교용."""
    counts: dict[str, int] = {"steps": len(rec.get("steps") or [])}
    for c in pred_canons:
        if c.pkg_key == "loop":
            counts["loop"] = counts.get("loop", 0) + 1
        elif c.pkg_key == "if":
            counts["if"] = counts.get("if", 0) + 1
        elif c.pkg_key == "errorhandler":
            name = next(iter(c.tokens), "")
            counts[name] = counts.get(name, 0) + 1
    counts["variables"] = len(rec.get("variables") or [])
    return counts


async def _run_case(entry: dict, goldset: Path, case_out: Path, timeout: float, kb_canons) -> dict:
    """케이스 1회 실행: PDF 파싱 → analyze → recommend → 채점. 반환은 요약 행."""
    from app.agent.v3 import analyze, recommend
    from app.services.parser import parse_document

    from .gold import load_case, merged_sequence
    from .metrics import score_case
    from .notation import CanonAction

    idx, bot = entry["index"], entry["bot_name"]
    case_dir = goldset / "정답셋" / entry["case_dir"]
    case_out.mkdir(parents=True, exist_ok=True)
    row: dict = {"index": idx, "bot_name": bot, "title": entry.get("title", "")}

    # 1) 정답 시퀀스
    flows = load_case(case_dir)
    gold_seq = merged_sequence(flows)
    row["n_gold"] = len(gold_seq)
    row["gold_files"] = [f.source_file for f in flows]

    # 2) 업무정의서 — 정규화본(사람이 따라할 수준으로 손질한 .md)이 있으면 우선.
    #    무명시 "전용 프로그램" 참조를 구체 방법으로 바꾼 판(업무정의서_정규화/)을 쓰면
    #    커버리지·F1이 "정답 봇 복제"가 아니라 "문서 요구 달성"을 재게 된다. 없으면 PDF 폴백.
    from app.services.parser import parse_text

    norm = goldset / "업무정의서_정규화" / f"{entry['case_dir']}.md"
    if norm.is_file():
        parsed = await asyncio.to_thread(parse_text, norm.read_text(encoding="utf-8"))
        row["doc_source"] = "normalized"
    else:
        pdfs = sorted((goldset / "업무정의서").glob(f"{bot}__*.pdf"))
        if not pdfs:
            row["error"] = f"업무정의서 없음: 정규화본·PDF 모두 부재 ({bot})"
            return row
        pdf = pdfs[0]
        parsed = await asyncio.to_thread(parse_document, pdf.name, pdf.read_bytes())
        row["doc_source"] = "pdf"

    # 3) analyze
    t0 = time.monotonic()
    analysis = await asyncio.to_thread(analyze, parsed)
    analysis_d = analysis.model_dump()
    (case_out / "analysis.json").write_text(
        json.dumps(analysis_d, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    row["analyze_sec"] = round(time.monotonic() - t0, 1)
    row["n_analysis_steps"] = len(analysis_d.get("steps") or [])

    # 4) v3 recommend (스트림 소비 — done 데이터만 취함)
    t1 = time.monotonic()
    recommendation: dict | None = None
    events_path = case_out / "events.jsonl"
    err_msgs: list[str] = []

    async def consume() -> None:
        nonlocal recommendation
        with events_path.open("w", encoding="utf-8") as ef:
            async for ev in recommend(analysis, parsed_doc=parsed):
                d = ev.model_dump() if hasattr(ev, "model_dump") else dict(ev)
                lite = {k: d.get(k) for k in ("event", "stage", "message") if d.get(k) is not None}
                if d.get("event") == "done":
                    recommendation = (d.get("data") or {}).get("recommendation")
                elif d.get("event") == "error":
                    err_msgs.append(d.get("message") or "")
                ef.write(json.dumps(lite, ensure_ascii=False) + "\n")

    await asyncio.wait_for(consume(), timeout=timeout)
    row["recommend_sec"] = round(time.monotonic() - t1, 1)

    if not recommendation:
        row["error"] = "recommendation 없음: " + ("; ".join(err_msgs) or "done 데이터 누락")
        return row
    (case_out / "recommendation.json").write_text(
        json.dumps(recommendation, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 5) 채점 — (A) 액션 시퀀스 F1 + (B) 문서 요구사항 커버리지(정답 봇 독립)
    pred_seq = _pred_sequence(recommendation)
    score = score_case(pred_seq, gold_seq, kb_canons)
    pred_canons = [CanonAction(p, a) for p, a in pred_seq]
    score["structure_gold"] = {**flows[0].structure, "flows": len(flows)}
    score["structure_pred"] = _pred_structure(recommendation, pred_canons)
    score["flow_confidence"] = recommendation.get("flow_confidence")
    score["needs_input_cards"] = len(recommendation.get("needs_input") or [])

    # 커버리지: 업무정의서 원문(분석·정답 봇과 독립)을 기준으로 흐름도가 문서 요구를
    # 달성했는지 LLM 심판. 성긴 문서엔 성긴 요구만 나오므로 미명시 접착제를 감점하지 않는다.
    from app.agent.v3.analysis import _format_document, _has_text

    from .coverage import score_coverage

    doc_text = _format_document(parsed) if _has_text(parsed) else ""
    cov = await asyncio.to_thread(score_coverage, doc_text, recommendation)
    score["coverage"] = cov
    (case_out / "score.json").write_text(
        json.dumps(score, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    row.update({
        "n_pred": score["n_pred"],
        "n_matched": score["n_matched"],
        "f1": score["action"]["f1"],
        "precision": score["action"]["precision"],
        "recall": score["action"]["recall"],
        "recall_achv": score["action_achievable"]["recall"],
        "pkg_f1": score["package"]["f1"],
        "order": score["order_score"],
        "kb_gaps": len(score["kb_gaps"]),
        "flow_confidence": score["flow_confidence"],
        "cards": score["needs_input_cards"],
        "coverage": (cov or {}).get("coverage"),
        "cov_covered": (cov or {}).get("n_covered"),
        "cov_total": (cov or {}).get("n_total"),
    })
    return row


_AGG_KEYS = ("precision", "recall", "recall_achv", "f1", "pkg_f1", "order",
             "coverage", "n_pred", "n_matched", "kb_gaps", "flow_confidence", "cards",
             "analyze_sec", "recommend_sec")


def _aggregate_reps(entry: dict, reps: list[dict]) -> dict:
    """반복 실행 행들을 평균±표준편차 요약 행 하나로 접는다.

    성공한 반복만 집계하고, 전부 실패면 첫 오류를 대표로 남긴다. repeat=1이면
    그 행을 그대로 반환해 기존 산출 형태(compare_runs 포함)와 완전 호환된다.
    """
    ok = [r for r in reps if "error" not in r]
    if not ok:
        return dict(reps[0])
    if len(reps) == 1:
        return dict(reps[0])

    import statistics as st

    row: dict = {"index": entry["index"], "bot_name": entry["bot_name"],
                 "title": entry.get("title", ""), "reps": len(reps), "reps_ok": len(ok)}
    first = ok[0]
    for k in ("n_gold", "gold_files", "n_analysis_steps"):
        if k in first:
            row[k] = first[k]
    for k in _AGG_KEYS:
        vals = [r[k] for r in ok if isinstance(r.get(k), (int, float))]
        if vals:
            row[k] = round(st.mean(vals), 3)
            if len(vals) >= 2:
                row[f"{k}_std"] = round(st.stdev(vals), 3)
    if len(ok) < len(reps):
        row["rep_errors"] = [r["error"] for r in reps if "error" in r]
    return row


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.3f}" if v <= 1 else f"{v:.1f}"
    return str(v)


def _write_report(out_dir: Path, rows: list[dict], meta: dict) -> None:
    ok = [r for r in rows if "error" not in r]
    cols = ["index", "bot_name", "n_gold", "n_pred", "n_matched",
            "precision", "recall", "f1", "coverage", "cov_covered", "cov_total",
            "pkg_f1", "order", "flow_confidence", "recommend_sec"]
    lines = [
        f"# 골드셋 평가 리포트 — {meta['tag']}",
        "",
        f"- 실행: {meta['started']} · 모델: {meta['model']} · 케이스 {len(rows)}건 (성공 {len(ok)})",
        f"- KB 액션 스펙: {meta['kb_actions']}개",
        "",
        "| " + " | ".join(cols) + " |",
        "|" + "---|" * len(cols),
    ]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['index']} | {r['bot_name']} | " + f"⚠ {r['error']} |" * 1)
            continue
        lines.append("| " + " | ".join(_fmt(r.get(c)) for c in cols) + " |")
    if ok:
        def mean(key):
            vals = [r[key] for r in ok if isinstance(r.get(key), (int, float))]
            return sum(vals) / len(vals) if vals else None
        lines += [
            "",
            "## 매크로 평균",
            f"- **문서 요구사항 커버리지(A, 정답봇 독립): {_fmt(mean('coverage'))}** — 성긴 문서엔 성긴 요구, 미명시 접착제 무감점",
            f"- action P/R/F1: {_fmt(mean('precision'))} / {_fmt(mean('recall'))} / {_fmt(mean('f1'))}",
            f"- 달성가능 재현율(KB gap 제외): {_fmt(mean('recall_achv'))}",
            f"- package F1: {_fmt(mean('pkg_f1'))} · 순서 보존: {_fmt(mean('order'))}",
            "",
            "> 커버리지=문서가 명시한 작업의 달성률(covered+0.5·partial)/total. F1=정답 봇 액션",
            "> 시퀀스와의 문자열 매칭. 문서가 성길수록 둘의 격차가 크며, 그 격차가 곧 '정답 봇이",
            "> 문서 없이 채운 구현량'이다.",
        ]
    # KB gap 롤업 (repeat>1이면 rep 하위 폴더까지 — rglob)
    gap_counter: dict[str, int] = {}
    for sc in out_dir.rglob("score.json"):
        s = json.loads(sc.read_text(encoding="utf-8"))
        for g in s.get("kb_gaps", []):
            key = "/".join(g["gold"])
            gap_counter[key] = gap_counter.get(key, 0) + 1
    if gap_counter:
        lines += ["", "## KB 결손(골드 액션인데 KB에 동치 없음) — 등장 횟수", ""]
        for k, v in sorted(gap_counter.items(), key=lambda x: -x[1]):
            lines.append(f"- {k}: {v}회")
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--goldset", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cases", default="", help="쉼표 구분 인덱스 (기본: 전체)")
    ap.add_argument("--tag", default="run")
    ap.add_argument("--timeout", type=float, default=720.0, help="케이스당 recommend 타임아웃(초)")
    ap.add_argument("--repeat", type=int, default=1,
                    help="케이스당 반복 실행 수 — LLM 분산 억제용. >1이면 케이스 폴더에 rep1/rep2/… 저장, 요약 행은 평균±표준편차")
    ap.add_argument("--parallel", type=int, default=1,
                    help="동시에 실행할 (케이스,반복) 작업 수 상한. 기본 1(순차). v3는 케이스당 "
                         "내부 LLM 호출이 많아(20~40회) 무제한 병렬은 레이트리밋을 터뜨림 — 2~4 권장")
    args = ap.parse_args()

    goldset = Path(args.goldset)
    manifest = json.loads((goldset / "정답셋" / "manifest.json").read_text(encoding="utf-8"))
    entries = manifest["entries"]
    if args.cases:
        wanted = {int(x) for x in args.cases.split(",") if x.strip()}
        entries = [e for e in entries if e["index"] in wanted]

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) / f"{stamp}-{args.tag}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 환경 사전 점검 — 카탈로그·키 없이 13케이스를 돌다 말면 낭비다
    from app.agent.v3 import config as agent_config
    from app.services.catalog import get_backend_catalog

    from .notation import CanonAction

    if not agent_config.OPENAI_API_KEY:
        print("OPENAI_API_KEY 없음 — .env 확인", file=sys.stderr)
        return 2
    kb_canons = [
        CanonAction(s["package"], s["action"])
        for s in get_backend_catalog().iter_action_schemas()
    ]
    meta = {
        "tag": args.tag,
        "started": stamp,
        "model": agent_config.OPENAI_MODEL,
        "kb_actions": len(kb_canons),
    }
    logger.info("평가 시작: %d케이스, 모델=%s, KB=%d액션, out=%s",
                len(entries), meta["model"], len(kb_canons), out_dir)

    # (케이스,반복) 작업 단위를 상한 병렬로 실행한다. --parallel 1이면 순차와 동일.
    # 각 반복은 독립 recommend() 파이프라인이라 asyncio 태스크로 안전히 병렬화된다
    # (usage_context ContextVar·langgraph 스트림 컨텍스트는 태스크별 격리). 세마포어로
    # 동시 (케이스,반복) 수를 묶어 레이트리밋을 지킨다 — 케이스 내부 동시성은 그대로.
    sem = asyncio.Semaphore(max(1, args.parallel))
    started_at = time.monotonic()

    async def _one(e: dict, rep_i: int) -> dict:
        case_out = out_dir / e["case_dir"] / (f"rep{rep_i + 1}" if args.repeat > 1 else "")
        async with sem:  # 세마포어 안에서만 시간 측정 — 큐 대기 제외, 실제 compute만
            t0 = time.monotonic()
            try:
                row = await _run_case(e, goldset, case_out, args.timeout, kb_canons)
            except Exception as ex:  # noqa: BLE001 — 반복 1회 실패 격리
                logger.exception("[%02d] rep%d 실패", e["index"], rep_i + 1)
                row = {"index": e["index"], "bot_name": e["bot_name"],
                       "error": f"{type(ex).__name__}: {ex}"}
            row["_rep_sec"] = round(time.monotonic() - t0, 1)
            return row

    logger.info("작업 %d건 (%d케이스×%d반복) 실행 — 동시 상한 %d",
                len(entries) * args.repeat, len(entries), args.repeat, args.parallel)

    done_rows: list[dict] = []  # 완료 순 누적 — 케이스마다 summary.json 증분 기록(크래시 복원력)

    async def _case(e: dict) -> dict:
        reps = await asyncio.gather(*(_one(e, i) for i in range(args.repeat)))
        row = _aggregate_reps(e, list(reps))
        row["case_dir"] = e["case_dir"]
        row["total_sec"] = round(sum(r.get("_rep_sec", 0) for r in reps), 1)  # 반복 compute 합
        done_rows.append(row)  # asyncio 단일 스레드 — 락 불필요
        (out_dir / "summary.json").write_text(
            json.dumps({"meta": meta, "rows": sorted(done_rows, key=lambda r: r["index"])},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("[%02d] 완료 (%.0fs) f1=%s", e["index"], row["total_sec"], row.get("f1", "—"))
        return row

    results = await asyncio.gather(*(_case(e) for e in entries))
    rows = sorted(results, key=lambda r: r["index"])
    _write_report(out_dir, rows, meta)
    logger.info("전체 완료 (%.0fs). 리포트: %s", time.monotonic() - started_at, out_dir / "report.md")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
