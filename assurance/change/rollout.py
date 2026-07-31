"""Fail-closed governance checks for Warn-to-Enforce promotion."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
JIRA_KEY = re.compile(r"^RPA-[1-9][0-9]*$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
HERE = Path(__file__).resolve().parent
DEFAULT_POLICY = HERE / "rollout-policy.json"


class GovernanceError(ValueError):
    """A governance artifact is invalid or cannot prove the requested action."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def report_digest(report: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(report)).hexdigest()


def _time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise GovernanceError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GovernanceError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise GovernanceError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _approvers(
    artifact: dict[str, Any], *, minimum: int, requester_field: str
) -> list[str]:
    values = artifact.get("approvers")
    if not isinstance(values, list) or not all(
        isinstance(value, str) and value.strip() for value in values
    ):
        raise GovernanceError("approvers must be a non-empty string array")
    normalized = [value.strip() for value in values]
    if len(set(normalized)) != len(normalized):
        raise GovernanceError("approvers must be distinct")
    if len(normalized) < minimum:
        raise GovernanceError(f"at least {minimum} independent approvers are required")
    if artifact.get(requester_field) in normalized:
        raise GovernanceError("the requester cannot approve their own exception")
    return normalized


def validate_waiver(
    artifact: dict[str, Any],
    *,
    now: datetime,
    max_duration_hours: int = 336,
    min_approvers: int = 2,
) -> None:
    required = {
        "schema_version",
        "waiver_id",
        "repository",
        "head_sha",
        "control_id",
        "jira",
        "reason",
        "compensating_controls",
        "requested_by",
        "approved_at",
        "expires_at",
        "approvers",
    }
    if set(artifact) != required or artifact.get("schema_version") != "1.0":
        raise GovernanceError("waiver fields do not match the v1.0 contract")
    if not GIT_SHA.fullmatch(str(artifact.get("head_sha", ""))):
        raise GovernanceError("waiver must bind a full PR head SHA")
    if not JIRA_KEY.fullmatch(str(artifact.get("jira", ""))):
        raise GovernanceError("waiver must reference an RPA Jira issue")
    if not re.fullmatch(r"CH-[0-9]{2}", str(artifact.get("control_id", ""))):
        raise GovernanceError("waiver must bind one Change Assurance control")
    controls = artifact.get("compensating_controls")
    if not isinstance(controls, list) or not controls or not all(
        isinstance(value, str) and value.strip() for value in controls
    ):
        raise GovernanceError("waiver requires compensating controls")
    _approvers(artifact, minimum=min_approvers, requester_field="requested_by")
    approved = _time(artifact["approved_at"], "approved_at")
    expires = _time(artifact["expires_at"], "expires_at")
    current = now.astimezone(timezone.utc)
    if approved > current:
        raise GovernanceError("waiver approval is in the future")
    if expires <= current:
        raise GovernanceError("waiver is expired")
    if expires <= approved:
        raise GovernanceError("waiver expiry must follow approval")
    if expires - approved > timedelta(hours=max_duration_hours):
        raise GovernanceError("waiver lifetime exceeds the policy maximum")


def validate_break_glass(
    artifact: dict[str, Any],
    *,
    now: datetime,
    max_duration_hours: int = 2,
    min_approvers: int = 2,
    rollback_test_max_age_days: int = 30,
) -> None:
    required = {
        "schema_version",
        "break_glass_id",
        "repository",
        "branch",
        "jira",
        "reason",
        "requested_by",
        "requested_at",
        "expires_at",
        "approvers",
        "rollback",
    }
    if set(artifact) != required or artifact.get("schema_version") != "1.0":
        raise GovernanceError("break-glass fields do not match the v1.0 contract")
    if artifact.get("branch") not in {"dev", "main"}:
        raise GovernanceError("break-glass scope must bind dev or main")
    if not JIRA_KEY.fullmatch(str(artifact.get("jira", ""))):
        raise GovernanceError("break-glass must reference an RPA Jira issue")
    _approvers(artifact, minimum=min_approvers, requester_field="requested_by")
    requested = _time(artifact["requested_at"], "requested_at")
    expires = _time(artifact["expires_at"], "expires_at")
    current = now.astimezone(timezone.utc)
    if requested > current or expires <= current:
        raise GovernanceError("break-glass window is not currently active")
    if expires - requested > timedelta(hours=max_duration_hours):
        raise GovernanceError("break-glass lifetime exceeds the policy maximum")
    rollback = artifact.get("rollback")
    if not isinstance(rollback, dict) or set(rollback) != {
        "procedure_ref",
        "evidence_digest",
        "tested_at",
        "successful",
    }:
        raise GovernanceError("break-glass requires a complete rollback proof")
    if rollback.get("successful") is not True:
        raise GovernanceError("the latest rollback drill did not succeed")
    if not DIGEST.fullmatch(str(rollback.get("evidence_digest", ""))):
        raise GovernanceError("rollback evidence digest is malformed")
    tested = _time(rollback["tested_at"], "rollback.tested_at")
    if tested > current or current - tested > timedelta(days=rollback_test_max_age_days):
        raise GovernanceError("rollback drill evidence is stale")


def assess_promotion(
    reports: list[dict[str, Any]],
    dispositions: list[dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, Any]:
    thresholds = policy["promotion"]
    unique: dict[str, dict[str, Any]] = {}
    invalid_reports = 0
    for report in reports:
        run_id = report.get("run_id")
        enforcement = report.get("enforcement")
        if (
            not isinstance(run_id, str)
            or not isinstance(enforcement, dict)
            or enforcement.get("mode") != "warn"
        ):
            invalid_reports += 1
            continue
        unique.setdefault(run_id, report)

    detector_errors = {
        run_id
        for run_id, report in unique.items()
        if any(
            isinstance(control, dict) and control.get("status") == "error"
            for control in report.get("controls", [])
        )
    }
    allow_count = sum(
        report.get("assurance_decision") == "allow_candidate"
        for report in unique.values()
    )
    nonpassing = {
        run_id
        for run_id, report in unique.items()
        if report.get("assurance_decision") in {"deny", "unassured"}
    }

    reviewed: dict[str, str] = {}
    invalid_dispositions = 0
    for item in dispositions:
        run_id = item.get("run_id")
        classification = item.get("classification")
        report = unique.get(run_id)
        if (
            report is None
            or run_id not in nonpassing
            or classification not in {"true_positive", "false_positive", "detector_error"}
            or item.get("report_digest") != report_digest(report)
            or not JIRA_KEY.fullmatch(str(item.get("jira", "")))
            or not isinstance(item.get("reviewer"), str)
            or item.get("reviewer_type") != "human"
        ):
            invalid_dispositions += 1
            continue
        reviewed[run_id] = classification

    adjudicated = [
        value for value in reviewed.values() if value in {"true_positive", "false_positive"}
    ]
    false_positives = sum(value == "false_positive" for value in adjudicated)
    sample_count = len(unique)
    detector_error_rate = len(detector_errors) / sample_count if sample_count else 1.0
    false_positive_rate = (
        false_positives / len(adjudicated) if adjudicated else None
    )
    blockers = []
    if sample_count < thresholds["min_warn_samples"]:
        blockers.append("WARN_SAMPLE_COUNT")
    if allow_count < thresholds["min_allow_candidates"]:
        blockers.append("ALLOW_CANDIDATE_COUNT")
    if len(adjudicated) < thresholds["min_reviewed_nonpassing"]:
        blockers.append("NONPASSING_REVIEW_COUNT")
    if false_positive_rate is None:
        blockers.append("FALSE_POSITIVE_RATE_UNKNOWN")
    elif false_positive_rate > thresholds["max_false_positive_rate"]:
        blockers.append("FALSE_POSITIVE_RATE")
    if detector_error_rate > thresholds["max_detector_error_rate"]:
        blockers.append("DETECTOR_ERROR_RATE")
    if invalid_reports:
        blockers.append("INVALID_REPORTS")
    if invalid_dispositions:
        blockers.append("INVALID_DISPOSITIONS")
    return {
        "ready": not blockers,
        "sample_count": sample_count,
        "allow_candidate_count": allow_count,
        "nonpassing_count": len(nonpassing),
        "reviewed_nonpassing_count": len(adjudicated),
        "false_positive_count": false_positives,
        "false_positive_rate": false_positive_rate,
        "detector_error_count": len(detector_errors),
        "detector_error_rate": detector_error_rate,
        "blockers": blockers,
    }


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assess Change Assurance promotion readiness")
    parser.add_argument("--reports", type=Path, nargs="+", required=True)
    parser.add_argument("--dispositions", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    args = parser.parse_args(argv)
    reports = [
        _load_json(path)
        for root in args.reports
        for path in root.rglob("assurance-report.json")
    ]
    result = assess_promotion(reports, _load_json(args.dispositions), _load_json(args.policy))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
