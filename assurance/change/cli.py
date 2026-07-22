"""CLI for the Change Assurance Observe harness."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .checker import markdown_summary, run_assurance, write_error_report


HERE = Path(__file__).resolve().parent
DEFAULT_POLICY = HERE / "policy" / "dependency-policy.json"


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Run A360 Change Assurance in Observe mode")
    value.add_argument("--repo", type=Path, default=Path.cwd())
    value.add_argument("--base-sha", required=True)
    value.add_argument("--head-sha", required=True)
    value.add_argument("--repository", required=True)
    value.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    value.add_argument("--output", type=Path, default=Path(".artifacts/change-assurance"))
    value.add_argument("--review-evidence", type=Path)
    return value


def append_job_summary(summary: str) -> None:
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if not destination:
        return
    with Path(destination).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(summary)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        report = run_assurance(
            repo_root=args.repo,
            base_sha=args.base_sha,
            head_sha=args.head_sha,
            repository=args.repository,
            output=args.output,
            policy_path=args.policy,
            review_evidence_path=args.review_evidence,
        )
    except Exception as exc:
        try:
            report = write_error_report(
                output=args.output,
                repository=args.repository,
                base_sha=args.base_sha,
                head_sha=args.head_sha,
                error=exc,
            )
        except Exception as receipt_error:
            print(
                f"Change Assurance could not write an Observe receipt: {type(receipt_error).__name__}",
                file=sys.stderr,
            )
            return 2
    summary = markdown_summary(report)
    print(summary, end="")
    append_job_summary(summary)
    # Observe deliberately does not turn an assurance deny into a merge failure.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
