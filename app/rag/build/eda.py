"""청킹 전 원본 문서의 source_type별 문자 길이 분포를 분석한다 (청크 크기/overlap 결정용).

주의: 이미 청킹된 rag_documents.jsonl을 분석하면 청크 크기 상한이 그대로 max로 나와
순환 오류가 생긴다 — 반드시 chunk_size=None으로 만든 청킹 전 문서를 넘겨야 한다.
"""

import statistics


def _percentile(sorted_values: list[int], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    k = (len(sorted_values) - 1) * (pct / 100)
    lower = int(k)
    upper = min(lower + 1, len(sorted_values) - 1)
    if lower == upper:
        return float(sorted_values[lower])
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (k - lower)


def _stats_for(lengths: list[int]) -> dict:
    lengths = sorted(lengths)
    return {
        "count": len(lengths),
        "mean": round(statistics.mean(lengths), 1),
        "median": statistics.median(lengths),
        "stdev": round(statistics.stdev(lengths), 1) if len(lengths) > 1 else 0.0,
        "min": lengths[0],
        "max": lengths[-1],
        "p50": round(_percentile(lengths, 50), 1),
        "p75": round(_percentile(lengths, 75), 1),
        "p90": round(_percentile(lengths, 90), 1),
        "p95": round(_percentile(lengths, 95), 1),
        "p99": round(_percentile(lengths, 99), 1),
    }


def compute_length_stats(documents: list[dict]) -> dict[str, dict]:
    """source_type별 content 문자 길이 통계. "_all"에는 전체 통계도 함께 담는다."""
    by_type: dict[str, list[int]] = {}
    all_lengths: list[int] = []
    for doc in documents:
        length = len(doc["content"])
        by_type.setdefault(doc["source_type"], []).append(length)
        all_lengths.append(length)

    result = {source_type: _stats_for(lengths) for source_type, lengths in by_type.items()}
    if all_lengths:
        result["_all"] = _stats_for(all_lengths)
    return result


def print_report(stats: dict[str, dict]) -> None:
    header = f"{'source_type':<18}{'count':>7}{'mean':>8}{'median':>8}{'stdev':>8}{'min':>7}{'max':>7}{'p75':>8}{'p90':>8}{'p95':>8}{'p99':>8}"
    print(header)
    print("-" * len(header))
    for source_type, s in sorted(stats.items(), key=lambda kv: kv[0] != "_all"):
        print(
            f"{source_type:<18}{s['count']:>7}{s['mean']:>8}{s['median']:>8}{s['stdev']:>8}"
            f"{s['min']:>7}{s['max']:>7}{s['p75']:>8}{s['p90']:>8}{s['p95']:>8}{s['p99']:>8}"
        )
