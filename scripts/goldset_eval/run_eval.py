"""골드셋 평가 러너 — 업무정의서 PDF → analyze → v3 recommend → 골드 비교 채점.

사용:
    python -m scripts.goldset_eval.run_eval --goldset "...\\골드셋" --out "...\\평가결과" \
        [--cases 1,2,3] [--tag baseline] [--timeout 720]

케이스마다 저장: analysis.json / recommendation.json / events.jsonl / score.json
전체 저장: summary.json / report.md

실패 격리: 케이스 하나의 예외는 기록 후 다음 케이스로 진행한다.

엄밀 축(RPA-298 Phase 0-1): 골드셋에 믿을 수 있는 고정 정답지(`정답흐름도/`)가 있으면
`exact_metrics.attach_exact_axes`로 `action_exact*`·중첩·순서를 **기존 축과 나란히** 낸다.
재채점기(`rescore.py`)와 같은 함수를 쓴다 — 갈리면 재채점 수치를 원 런과 비교할 수 없다.
정답지가 없거나 못 믿으면 기존 축만 낸다(정답지 생성 전 환경에서도 러너는 돌아야 한다).
"""

import argparse
import asyncio
import importlib
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

# 채점 축 정의만 가져온다(앱·DB를 끌고 오지 않는 순수 모듈이라 모듈 최상위에 둬도 안전).
from .build_gold_flows import DEFAULT_OUT_NAME, check_gold_flows, load_gold_flow
from .exact_metrics import EXACT_ROW_KEYS
from .quality_axes import L1_ROW_KEYS, L3_ROW_KEYS, l1_row, l3_row

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("goldset_eval")
logger.setLevel(logging.INFO)


def _within(root: Path, seg: str) -> bool:
    """seg를 root 아래 하위경로로 붙였을 때 결과가 root 안에 머무는지 — 절대경로·`..`로
    root 밖을 가리키면 False. 매니페스트(외부 입력)의 case_dir가 goldset 밖을 읽거나 out_dir
    밖에 파일을 덮어쓰는 경로 탈출을 차단한다.
    """
    if not seg or not isinstance(seg, str):
        return False
    root_r = root.resolve()
    target = (root_r / seg).resolve()
    return target == root_r or root_r in target.parents


def _safe_entries(entries: list[dict], goldset: Path, out_dir: Path) -> list[dict]:
    """case_dir가 읽기 루트(goldset/정답셋·업무정의서_정규화)와 쓰기 루트(out_dir) 안에
    모두 머무는 항목만 남긴다. 탈출 항목은 경고 후 제외(실패 격리 — 나머지는 계속 진행).
    """
    read_roots = [goldset / "정답셋", goldset / "업무정의서_정규화"]
    safe: list[dict] = []
    for e in entries:
        cd = e.get("case_dir", "")
        if all(_within(r, cd) for r in read_roots) and _within(out_dir, cd):
            safe.append(e)
        else:
            logger.warning("[%s] case_dir 경로 탈출 차단 — 제외: %r",
                           e.get("index"), cd)
    return safe


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


def agent_module(version: str):
    """평가 대상 에이전트 버전 모듈. 기본 v3 — 기존 실행 명령이 그대로 동작해야 한다.

    v3 하드코딩이던 것을 인자화했다. v4를 만들어도 골드셋으로 잴 수 없으면 '나아졌는지'를
    판정할 수단이 없어 개선 자체가 무의미해진다.
    """
    import importlib

    return importlib.import_module(f"app.agent.{version}")


async def _run_case(
    entry: dict, goldset: Path, case_out: Path, timeout: float, kb_canons,
    agent_version: str = "v3", gold_flow: dict | None = None, judge: bool = False,
) -> dict:
    """케이스 1회 실행: PDF 파싱 → analyze → recommend → 채점. 반환은 요약 행.

    `gold_flow`(고정 정답지 한 케이스)를 주면 엄밀 축을 기존 축 **옆에** 얹는다.
    없으면 기존 축만 — 정답지가 없는 환경에서도 러너는 그대로 돌아야 한다.
    """
    _agent = agent_module(agent_version)
    analyze, recommend = _agent.analyze, _agent.recommend
    from app.services.parser import parse_document

    from .gold import load_case, merged_boilerplate, merged_sequence
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
    score = score_case(pred_seq, gold_seq, kb_canons, merged_boilerplate(flows))
    pred_canons = [CanonAction(p, a) for p, a in pred_seq]
    score["structure_gold"] = {**flows[0].structure, "flows": len(flows)}
    score["structure_pred"] = _pred_structure(recommendation, pred_canons)
    score["flow_confidence"] = recommendation.get("flow_confidence")
    score["needs_input_cards"] = len(recommendation.get("needs_input") or [])

    # 커버리지: 업무정의서 원문(분석·정답 봇과 독립)을 기준으로 흐름도가 문서 요구를
    # 달성했는지 LLM 심판. 성긴 문서엔 성긴 요구만 나오므로 미명시 접착제를 감점하지 않는다.
    _analysis = importlib.import_module(f"app.agent.{agent_version}.analysis")
    _format_document, _has_text = _analysis._format_document, _analysis._has_text

    from .coverage import score_coverage

    doc_text = _format_document(parsed) if _has_text(parsed) else ""
    cov = await asyncio.to_thread(score_coverage, doc_text, recommendation)
    score["coverage"] = cov

    # 엄밀 축 — 고정 정답지 대비 문자열 일치·중첩·순서. 기존 축 값은 건드리지 않는다.
    if gold_flow:
        from .exact_metrics import attach_exact_axes

        attach_exact_axes(score, recommendation, gold_flow)

    # 납득 기준 L1·L3 (제약 #13). L1은 결정론이라 항상, L3는 LLM 2콜이라 opt-in.
    from .quality_axes import groundedness, judge_axis, l1_row, l3_row

    score["groundedness"] = groundedness(recommendation)
    judged = None
    if judge:
        spec_for_judge = recommendation.get("spec") or {}
        if spec_for_judge.get("requirements"):
            judged = await asyncio.to_thread(
                judge_axis, recommendation, spec_for_judge, doc_text or None,
                score.get("violations"),
            )
        else:
            # spec이 없으면 기대를 세울 근거가 없다 — 조용히 0점을 내면 '나쁜 흐름도'와
            # 구별이 안 된다. 축을 아예 안 낸다.
            logger.warning("[%02d] spec 요구가 없어 심판 축 생략", idx)
    if judged:
        score["judge"] = judged
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
        "recall_core": score["action_core"]["recall"],
        "f1_core": score["action_core"]["f1"],
        "n_gold_core": score["action_core"]["n_gold_core"],
        "n_gold_boiler": score["action_core"]["n_gold_boilerplate"],
        # 기능 등가 축 (RPA-298) — 다른 패키지로 같은 자원을 다룬 것도 성공.
        # n_cross가 0이면 그 케이스엔 대안 경로가 없었다는 뜻이라 엄격 축과 같은 값이 된다.
        "f1_equiv": score["action_equiv"]["f1"],
        "precision_equiv": score["action_equiv"]["precision"],
        "recall_equiv": score["action_equiv"]["recall"],
        "n_cross_pkg": score["action_equiv"]["n_cross_package"],
        "pkg_f1": score["package"]["f1"],
        "order": score["order_score"],
        "kb_gaps": len(score["kb_gaps"]),
        "flow_confidence": score["flow_confidence"],
        "cards": score["needs_input_cards"],
        "coverage": (cov or {}).get("coverage"),
        "cov_covered": (cov or {}).get("n_covered"),
        "cov_total": (cov or {}).get("n_total"),
    })
    # 엄밀 축 스칼라 — 정답지가 없으면 빈 dict라 행 모양이 기존과 완전히 같다.
    from .exact_metrics import exact_row

    row.update(exact_row(score))
    row.update(l1_row(recommendation))     # L1은 결정론 — 항상 낸다
    row.update(l3_row(judged))             # L3는 못 냈으면 빈 dict (0으로 안 채운다)
    return row


_AGG_KEYS = ("precision", "recall", "recall_achv", "recall_core", "f1", "f1_core",
             "f1_equiv", "precision_equiv", "recall_equiv", "n_cross_pkg",
             "n_gold_core", "n_gold_boiler", "pkg_f1", "order",
             "coverage", "cov_covered", "cov_total", "n_pred", "n_matched", "kb_gaps",
             "flow_confidence", "cards", "analyze_sec", "recommend_sec",
             # 엄밀 축 (RPA-298 Phase 0-1) — 정답지가 있는 런에서만 행에 존재한다.
             *EXACT_ROW_KEYS,
             # 납득 기준 L1·L3 (제약 #13). L3는 --judge 런에서만 행에 존재한다.
             *L1_ROW_KEYS, *L3_ROW_KEYS)


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


# 리포트 표에 붙이는 엄밀 축 열. 정답지가 있는 런에만 붙는다 — 없는 런의 표 모양은
# 기존과 한 칸도 달라지지 않아야 지난 리포트와 나란히 읽힌다.
_EXACT_COLS = ["f1_exact", "recall_exact", "recall_exact_core", "nesting_path", "order_exact"]


def _write_report(out_dir: Path, rows: list[dict], meta: dict) -> None:
    ok = [r for r in rows if "error" not in r]
    cols = ["index", "bot_name", "n_gold", "n_gold_core", "n_gold_boiler", "n_pred", "n_matched",
            "precision", "recall", "recall_core", "f1", "f1_equiv", "n_cross_pkg",
            "coverage", "cov_covered", "cov_total",
            "pkg_f1", "order", "flow_confidence", "recommend_sec"]
    exact_on = any(any(c in r for c in _EXACT_COLS) for r in ok)
    if exact_on:
        cols += _EXACT_COLS
    # L1은 항상 있고, L3는 --judge 런에만 있다 — 없는 열을 표에 넣으면 전부 '—'로 채워져
    # "쟀는데 0"처럼 읽힌다.
    cols += ["action_cited", "param_grounded"]
    if any("judge_met_rate" in r for r in ok):
        cols += ["judge_met_rate", "judge_soundness"]
    lines = [
        f"# 골드셋 평가 리포트 — {meta['tag']}",
        "",
        f"- 실행: {meta['started']} · 에이전트 {meta.get('agent_version', 'v3')} · "
        f"모델: {meta['model']} · 케이스 {len(rows)}건 (성공 {len(ok)})",
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
            f"- **실업무 재현율(보일러플레이트 제외): {_fmt(mean('recall_core'))}** · 실업무 F1: {_fmt(mean('f1_core'))}",
            f"- 달성가능 재현율(KB gap 제외): {_fmt(mean('recall_achv'))}",
            f"- **기능 등가 P/R/F1: {_fmt(mean('precision_equiv'))} / {_fmt(mean('recall_equiv'))} "
            f"/ {_fmt(mean('f1_equiv'))}** · 케이스당 교차패키지 매칭 {_fmt(mean('n_cross_pkg'))}건",
            f"- package F1: {_fmt(mean('pkg_f1'))} · 순서 보존: {_fmt(mean('order'))}",
            "",
            "### 납득 기준 (제약 #13)",
            f"- **L1 문서 근거성**: 액션 인용률 {_fmt(mean('action_cited'))} "
            f"({_fmt(mean('n_action_cited'))}/{_fmt(mean('n_action_citable'))}) · "
            f"파라미터 근거값 비율 {_fmt(mean('param_grounded'))}",
            (
                f"- **L3 독립 심판**: must 기대 충족률 {_fmt(mean('judge_met_rate'))} · "
                f"견고성 {_fmt(mean('judge_soundness'))} · "
                f"미충족 must {_fmt(mean('judge_unmet_must'))}건 · 치명 결함 {_fmt(mean('judge_fatal'))}건"
                if any("judge_met_rate" in r for r in ok)
                else "- L3 독립 심판: 미측정 (`--judge`로 켠다 — 케이스·반복당 LLM 2콜)"
            ),
            "",
            "> **L1**은 결정론이다(LLM 0콜). `sources`가 빈 액션은 검색에 한 번도 안 잡힌 것 —",
            "> 모델이 기억에서 꺼냈거나 결정론 보완으로 들어왔다. 구조 액션(Loop·If·Error",
            "> handler)은 카탈로그 직조회로 들어오므로 분모에서 뺀다. ⚠️ 근거의 **존재**를",
            "> 재지 적절성을 재지 않는다 — 엉뚱한 문서가 붙어도 1.0이다(적절성은 L3의 몫).",
            ">",
            "> **L3**는 정답 봇을 **보지 않는다.** spec+문서만으로 세운 기대에 흐름도를 대므로,",
            "> 재현율이 '정답 봇을 얼마나 베꼈나'를 잰다면 이쪽은 '실제로 돌아가는가'를 잰다.",
            "> 두 축이 갈리는 지점이 곧 정답 봇이 문서 없이 채운 구현량이다. 실측 예: 케이스",
            "> 01에서 에이전트가 `Jira/Create project`를 냈는데 정답이 `Rest/restPost`라 재현율은",
            "> 0점이었다 — 전용 패키지 쪽이 더 관용적인데도 감점이다. L3엔 그 편향이 없다.",
            "",
            "> 커버리지=문서가 명시한 작업의 달성률(covered+0.5·partial)/total. F1=정답 봇 액션",
            "> 시퀀스와의 문자열 매칭. 문서가 성길수록 둘의 격차가 크며, 그 격차가 곧 '정답 봇이",
            "> 문서 없이 채운 구현량'이다.",
            ">",
            "> **실업무 재현율**은 Bot Store 제출 규약 보일러플레이트(봇 이름·벤더명 확인, 로그",
            "> 폴더 생성, 30일 로그 정리, 오류 로깅, 스냅샷)를 정답에서 뺀 재현율이다. 이건",
            "> 마켓 심사 요건이라 업무정의서에도 공식 문서에도 없어 에이전트가 만들어낼 근거가",
            "> 없다 — 실측 528개 중 180개(34%). `recall`은 기존 기준선과의 비교용,",
            "> `recall_core`는 실제 실력 측정용으로 나란히 본다.",
            ">",
            "> **기능 등가**는 패키지가 달라도 **같은 자원**을 다루면 성공으로 친다",
            "> (`Email/emailConnect` ↔ `Microsoft 365 Outlook/Connect`). 엄격 채점은 패키지가",
            "> 다르면 유사도를 0으로 막는데, 우리 정답 기준은 '사람이 손으로 옮겨 돌아가면 성공'",
            "> 이라 대안 경로도 성공이다. `n_cross_pkg`가 0인 케이스는 대안 경로가 없어 엄격",
            "> 채점과 같은 값이다. 도메인 표(`notation._PKG_DOMAIN`)는 사람 판단이므로 어떤",
            "> 등가가 발동했는지 각 `score.json`의 `equiv_pairs`에 남는다 — 사후 감사용이다.",
        ]
        if exact_on:
            gf = meta.get("gold_flows") or {}
            lines += [
                "",
                "## 엄밀 축 (고정 정답지 대비 — 퍼지 유사도 없음)",
                f"- 정답지: `{gf.get('dir')}` (생성 {gf.get('generated_at')}, "
                f"KB 사상률 {_fmt(gf.get('resolve_rate'))})",
                f"- **엄밀 P/R/F1: {_fmt(mean('precision_exact'))} / {_fmt(mean('recall_exact'))} "
                f"/ {_fmt(mean('f1_exact'))}**",
                f"- 엄밀 실업무 재현율(보일러플레이트 제외): {_fmt(mean('recall_exact_core'))} · "
                f"엄밀 실업무 F1: {_fmt(mean('f1_exact_core'))}",
                f"- 사상된 정답만 대비한 재현율: {_fmt(mean('recall_exact_resolved'))} "
                f"(정답지 미사상 액션 케이스당 {_fmt(mean('n_gold_unresolved'))}건은 절대 안 맞는다)",
                f"- 중첩 경로 일치율 {_fmt(mean('nesting_path'))} · 깊이 일치율 "
                f"{_fmt(mean('nesting_depth'))} · 순서 보존(엄밀 매칭 기준) "
                f"{_fmt(mean('order_exact'))} · 파라미터 이름 Jaccard {_fmt(mean('param_jaccard'))}",
                "",
                "> **엄밀 축이 기존 축보다 낮게 나오는 것이 정상이다.** 기존 축은 토큰 유사도",
                "> ≥0.55면 맞는 것으로 치지만, 엄밀 축은 정답지에 박아 둔 **KB 표기 문자열이",
                "> 정확히 같아야** 맞고(`Create` ≠ `Create folder`) 정답지에서 KB로 사상되지",
                "> 못한 액션이 분모에 그대로 남는다. 대신 기존 축이 아예 못 보던 것 —",
                "> 액션이 Loop/If **안에 있는가**(`nesting_path`) — 을 잰다.",
                "> 기존 축과 값·의미가 다르므로 서로 빼거나 합치지 말고 나란히 읽어라.",
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
    ap.add_argument("--agent-version", default="v3",
                    help="평가할 에이전트 버전 (기본 v3 — 기존 명령 호환)")
    ap.add_argument("--timeout", type=float, default=720.0, help="케이스당 recommend 타임아웃(초)")
    ap.add_argument("--judge", action="store_true",
                    help="독립 LLM 심판 축(L3)을 함께 낸다 — **케이스·반복당 LLM 2콜 추가**. spec+문서만 보고 세운 기대에 흐름도를 대므로 정답 봇과 무관한 축이다(제약 #13)")
    ap.add_argument("--repeat", type=int, default=1,
                    help="케이스당 반복 실행 수 — LLM 분산 억제용. >1이면 케이스 폴더에 rep1/rep2/… 저장, 요약 행은 평균±표준편차")
    ap.add_argument("--parallel", type=int, default=1,
                    help="동시에 실행할 (케이스,반복) 작업 수 상한. 기본 1(순차). v3는 케이스당 "
                         "내부 LLM 호출이 많아(20~40회) 무제한 병렬은 레이트리밋을 터뜨림 — 2~4 권장")
    args = ap.parse_args()
    if args.repeat < 1:
        ap.error("--repeat 는 1 이상이어야 합니다 (0·음수면 실행할 반복이 없습니다)")

    goldset = Path(args.goldset)
    manifest = json.loads((goldset / "정답셋" / "manifest.json").read_text(encoding="utf-8"))
    entries = manifest["entries"]
    if args.cases:
        wanted = {int(x) for x in args.cases.split(",") if x.strip()}
        entries = [e for e in entries if e["index"] in wanted]

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) / f"{stamp}-{args.tag}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 매니페스트(외부 입력)의 case_dir 경로 탈출 차단 — goldset/out_dir 밖 읽기·덮어쓰기 방지.
    entries = _safe_entries(entries, goldset, out_dir)
    if not entries:
        print("실행할 유효 케이스 없음 (case_dir 검증 실패 또는 --cases 불일치)", file=sys.stderr)
        return 2

    # 환경 사전 점검 — 카탈로그·키 없이 13케이스를 돌다 말면 낭비다
    agent_config = agent_module(args.agent_version).config
    from app.services.catalog import get_backend_catalog

    from .notation import CanonAction

    if not agent_config.OPENAI_API_KEY:
        print("OPENAI_API_KEY 없음 — .env 확인", file=sys.stderr)
        return 2
    kb_canons = [
        CanonAction(s["package"], s["action"])
        for s in get_backend_catalog().iter_action_schemas()
    ]
    # 고정 정답지(엄밀 축). 없거나 못 믿으면 기존 축만 낸다 — 정답지 생성 전 환경에서도
    # 러너가 돌아야 하고, 망가진 정답지 위의 그럴듯한 0점은 없느니만 못하다.
    flows_dir = goldset / DEFAULT_OUT_NAME
    gold_status = check_gold_flows(flows_dir)
    for msg in gold_status.messages:
        # ⚠는 사람이 조치해야 하는 것(낡음·못 믿음), ⓘ는 정상 상태의 안내다
        (logger.warning if msg.startswith("⚠") else logger.info)("%s", msg)

    meta = {
        "tag": args.tag,
        "agent_version": args.agent_version,
        "started": stamp,
        "model": agent_config.OPENAI_MODEL,
        "kb_actions": len(kb_canons),
    }
    if gold_status.usable:
        gm = gold_status.manifest or {}
        meta["gold_flows"] = {
            "dir": str(flows_dir),
            "generated_at": gm.get("generated_at"),
            "resolve_rate": (gm.get("totals") or {}).get("resolve_rate"),
            "builder": gm.get("builder"),
        }
    logger.info("평가 시작: %d케이스, 모델=%s, KB=%d액션, out=%s",
                len(entries), meta["model"], len(kb_canons), out_dir)

    # (케이스,반복) 작업 단위를 상한 병렬로 실행한다. --parallel 1이면 순차와 동일.
    # 각 반복은 독립 recommend() 파이프라인이라 asyncio 태스크로 안전히 병렬화된다
    # (usage_context ContextVar·langgraph 스트림 컨텍스트는 태스크별 격리). 세마포어로
    # 동시 (케이스,반복) 수를 묶어 레이트리밋을 지킨다 — 케이스 내부 동시성은 그대로.
    sem = asyncio.Semaphore(max(1, args.parallel))
    started_at = time.monotonic()

    # 케이스별 정답지는 반복마다 다시 읽지 않고 한 번만 읽는다(같은 파일 × repeat회).
    gold_flows: dict[str, dict] = {}
    if gold_status.usable:
        for e in entries:
            gf = load_gold_flow(flows_dir, e["case_dir"])
            if gf is None:
                logger.warning("[%02d] 정답지에 이 케이스가 없다 — 엄밀 축 생략: %s",
                               e["index"], e["case_dir"])
            else:
                gold_flows[e["case_dir"]] = gf

    async def _one(e: dict, rep_i: int) -> dict:
        case_out = out_dir / e["case_dir"] / (f"rep{rep_i + 1}" if args.repeat > 1 else "")
        gold_flow = gold_flows.get(e["case_dir"])  # 못 믿는 정답지면 비어 있다
        async with sem:  # 세마포어 안에서만 시간 측정 — 큐 대기 제외, 실제 compute만
            t0 = time.monotonic()
            try:
                row = await _run_case(
                    e, goldset, case_out, args.timeout, kb_canons, args.agent_version,
                    gold_flow=gold_flow, judge=args.judge,
                )
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
