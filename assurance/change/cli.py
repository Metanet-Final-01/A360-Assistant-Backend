"""CLI for the Change Assurance Warn harness."""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from .checker import markdown_summary, run_assurance, write_error_report


HERE = Path(__file__).resolve().parent
DEFAULT_POLICY = HERE / "policy" / "dependency-policy.json"
EVIDENCE_LOCATION = re.compile(
    r"(?P<path>(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\."
    r"(?:py|txt|toml|json|ya?ml)):(?P<line>[1-9][0-9]*):"
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Run A360 Change Assurance in Warn mode")
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


def _workflow_escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _warning_locations(finding: str) -> list[tuple[str, int, str]]:
    locations: dict[tuple[str, int], str] = {}
    for detail in finding.split(" / "):
        if detail.strip().startswith("[base]"):
            continue
        match = EVIDENCE_LOCATION.search(detail)
        if match is None:
            continue
        key = (match.group("path"), int(match.group("line")))
        locations.setdefault(key, detail.strip())
    return [
        (path, line, detail)
        for (path, line), detail in list(locations.items())[:10]
    ]


def emit_warning_annotations(report: dict) -> None:
    for control in report["controls"]:
        if control["status"] in {"pass", "not_applicable"}:
            continue
        explanation = control.get("explanation") or {}
        title = _workflow_escape(
            f"Change Assurance 경고: {control['control_id']} {control['reason_code']}"
        )
        finding = explanation.get("finding", control["reason"])
        action = explanation.get("action", "원본 증거를 확인하세요.")
        locations = _warning_locations(finding)
        if not locations:
            message = _workflow_escape(f"{finding} 확인/조치: {action}")
            print(f"::warning title={title}::{message}")
            continue
        for path, line, detail in locations:
            message = _workflow_escape(f"{detail} 확인/조치: {action}")
            print(
                f"::warning file={_workflow_escape(path)},line={line},"
                f"title={title}::{message}"
            )


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
                f"Change Assurance could not write a Warn receipt: {type(receipt_error).__name__}",
                file=sys.stderr,
            )
            return 2
    summary = markdown_summary(report)
    print(summary, end="")
    append_job_summary(summary)
    emit_warning_annotations(report)
    # Warn deliberately reports non-passing controls without failing the required check.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
