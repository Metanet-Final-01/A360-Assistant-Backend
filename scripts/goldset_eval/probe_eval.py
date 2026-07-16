"""역량 probe 평가 — 정답 봇 없이 커버리지 + 역량 신호로 채점.

probe 정의서(final-etc-files/골드셋/역량probe/*.md)는 특정 역량(derived/guard/loop/
session/exception…)을 겨냥해 사람이 만든 공식형식 문서다. 정답 봇이 없으므로 F1은 못
재고, (1) 커버리지(문서 요구 달성률, 독립 LLM 심판)와 (2) 역량 신호(겨냥한 접착 액션이
실제로 생성됐는지 카테고리 카운트)로 진단한다.

사용:
    python -m scripts.goldset_eval.probe_eval --probes "...\\역량probe" --out "...\\probe결과" \
        [--parallel 3]
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
logger = logging.getLogger("probe_eval")
logger.setLevel(logging.INFO)


def _cap_counts(rec: dict) -> dict:
    """흐름도의 역량 신호 카운트 — 겨냥한 접착 액션이 나왔는지."""
    from .glue_analysis import _norm_pkg, classify

    cnt = {"core": 0, "guard": 0, "derived": 0, "assembly": 0, "logging": 0,
           "loop": 0, "exception": 0}

    def walk(actions):
        for x in actions or []:
            p, a = x.get("package"), x.get("action")
            if p and a and _norm_pkg(p) != "step":
                cnt[classify(p, a)] += 1
                if _norm_pkg(p) == "loop":
                    cnt["loop"] += 1
                if _norm_pkg(p) == "errorhandler":
                    cnt["exception"] += 1
            walk(x.get("children"))

    for s in rec.get("steps") or []:
        walk(s.get("actions"))
    return cnt


async def _run_probe(md: Path, out_dir: Path, timeout: float) -> dict:
    from app.agent.v3 import analyze, recommend
    from app.services.parser import parse_text

    from .coverage import score_coverage

    name = md.stem
    row: dict = {"probe": name}
    text = md.read_text(encoding="utf-8")
    case_out = out_dir / name
    case_out.mkdir(parents=True, exist_ok=True)

    parsed = await asyncio.to_thread(parse_text, text)
    analysis = await asyncio.to_thread(analyze, parsed)
    row["n_analysis_steps"] = len(analysis.model_dump().get("steps") or [])

    recommendation: dict | None = None
    errs: list[str] = []

    async def consume():
        nonlocal recommendation
        with (case_out / "events.jsonl").open("w", encoding="utf-8") as ef:
            async for ev in recommend(analysis, parsed_doc=parsed):
                d = ev.model_dump() if hasattr(ev, "model_dump") else dict(ev)
                if d.get("event") == "done":
                    recommendation = (d.get("data") or {}).get("recommendation")
                elif d.get("event") == "error":
                    errs.append(d.get("message") or "")
                ef.write(json.dumps({k: d.get(k) for k in ("event", "stage", "message")
                                     if d.get(k)}, ensure_ascii=False) + "\n")

    t0 = time.monotonic()
    await asyncio.wait_for(consume(), timeout=timeout)
    row["recommend_sec"] = round(time.monotonic() - t0, 1)
    if not recommendation:
        row["error"] = "recommendation 없음: " + ("; ".join(errs) or "done 누락")
        return row

    (case_out / "recommendation.json").write_text(
        json.dumps(recommendation, ensure_ascii=False, indent=2), encoding="utf-8")

    cov = await asyncio.to_thread(score_coverage, text, recommendation)
    caps = _cap_counts(recommendation)
    (case_out / "coverage.json").write_text(
        json.dumps(cov, ensure_ascii=False, indent=2), encoding="utf-8")

    row.update({
        "coverage": (cov or {}).get("coverage"),
        "cov_covered": (cov or {}).get("n_covered"),
        "cov_partial": (cov or {}).get("n_partial"),
        "cov_total": (cov or {}).get("n_total"),
        "n_actions": sum(caps[k] for k in ("core", "guard", "derived", "assembly", "logging")),
        **{f"cap_{k}": v for k, v in caps.items()},
    })
    return row


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probes", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=900.0)
    args = ap.parse_args()

    from app.agent.v3 import config as agent_config
    if not agent_config.OPENAI_API_KEY:
        print("OPENAI_API_KEY 없음", file=sys.stderr)
        return 2

    probes = sorted(Path(args.probes).glob("*.md"))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) / f"{stamp}-probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info("probe %d건, 모델=%s, 동시 %d, out=%s",
                len(probes), agent_config.OPENAI_MODEL, args.parallel, out_dir)

    sem = asyncio.Semaphore(max(1, args.parallel))
    rows: list[dict] = []

    async def one(md: Path) -> dict:
        async with sem:
            try:
                r = await _run_probe(md, out_dir, args.timeout)
            except Exception as ex:  # noqa: BLE001
                logger.exception("%s 실패", md.stem)
                r = {"probe": md.stem, "error": f"{type(ex).__name__}: {ex}"}
            rows.append(r)
            (out_dir / "summary.json").write_text(
                json.dumps(sorted(rows, key=lambda x: x["probe"]), ensure_ascii=False, indent=2),
                encoding="utf-8")
            logger.info("[%s] cov=%s actions=%s", md.stem[:24],
                        r.get("coverage", "—"), r.get("n_actions", "—"))
            return r

    await asyncio.gather(*(one(md) for md in probes))
    logger.info("완료: %s", out_dir / "summary.json")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
