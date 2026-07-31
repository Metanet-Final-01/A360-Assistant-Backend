from __future__ import annotations

from datetime import datetime, timezone

import pytest

from assurance.change.rollout import (
    GovernanceError,
    assess_promotion,
    report_digest,
    validate_break_glass,
    validate_waiver,
)


NOW = datetime(2026, 7, 31, 3, tzinfo=timezone.utc)
POLICY = {
    "promotion": {
        "min_warn_samples": 2,
        "min_allow_candidates": 1,
        "min_reviewed_nonpassing": 1,
        "max_false_positive_rate": 0.05,
        "max_detector_error_rate": 0.01,
    }
}


def _report(run_id: str, decision: str, status: str = "pass") -> dict:
    return {
        "run_id": run_id,
        "assurance_decision": decision,
        "enforcement": {"mode": "warn", "blocks_merge": False},
        "controls": [{"control_id": "CH-04", "status": status}],
    }


def test_promotion_requires_reviewed_false_positive_and_detector_metrics() -> None:
    reports = [_report("CA-allow", "allow_candidate"), _report("CA-deny", "deny", "fail")]
    disposition = {
        "run_id": "CA-deny",
        "classification": "true_positive",
        "report_digest": report_digest(reports[1]),
        "jira": "RPA-183",
        "reviewer": "reviewer-a",
        "reviewer_type": "human",
    }
    result = assess_promotion(reports, [disposition], POLICY)
    assert result["ready"] is True
    assert result["false_positive_rate"] == 0
    assert result["detector_error_rate"] == 0


def test_promotion_fails_closed_when_detector_errors_or_labels_are_missing() -> None:
    reports = [
        _report("CA-allow", "allow_candidate"),
        _report("CA-error", "unassured", "error"),
    ]
    result = assess_promotion(reports, [], POLICY)
    assert result["ready"] is False
    assert "FALSE_POSITIVE_RATE_UNKNOWN" in result["blockers"]
    assert "DETECTOR_ERROR_RATE" in result["blockers"]


def _waiver() -> dict:
    return {
        "schema_version": "1.0",
        "waiver_id": "WV-RPA-183-1",
        "repository": "Metanet-Final-01/A360-Assistant-Backend",
        "head_sha": "a" * 40,
        "control_id": "CH-04",
        "jira": "RPA-183",
        "reason": "Time-boxed exception",
        "compensating_controls": ["Manual dependency review"],
        "requested_by": "requester",
        "approved_at": "2026-07-31T02:00:00Z",
        "expires_at": "2026-08-01T02:00:00Z",
        "approvers": ["reviewer-a", "reviewer-b"],
    }


def test_waiver_is_sha_bound_two_person_and_expiring() -> None:
    validate_waiver(_waiver(), now=NOW)
    expired = _waiver()
    expired["expires_at"] = "2026-07-31T02:30:00Z"
    with pytest.raises(GovernanceError, match="expired"):
        validate_waiver(expired, now=NOW)


@pytest.mark.parametrize("repository", ["", "not-a-repository", "owner/repo/extra"])
def test_waiver_rejects_an_invalid_repository(repository: str) -> None:
    waiver = _waiver()
    waiver["repository"] = repository
    with pytest.raises(GovernanceError, match="repository is invalid"):
        validate_waiver(waiver, now=NOW)


def _break_glass() -> dict:
    return {
        "schema_version": "1.0",
        "break_glass_id": "BG-RPA-183-1",
        "repository": "Metanet-Final-01/A360-Assistant-Backend",
        "branch": "dev",
        "jira": "RPA-183",
        "reason": "Restore service during a verified checker outage",
        "requested_by": "requester",
        "requested_at": "2026-07-31T02:30:00Z",
        "expires_at": "2026-07-31T04:00:00Z",
        "approvers": ["reviewer-a", "reviewer-b"],
        "rollback": {
            "procedure_ref": "docs/runbooks/change-assurance-rollback.md",
            "evidence_digest": "sha256:" + "b" * 64,
            "tested_at": "2026-07-30T03:00:00Z",
            "successful": True,
        },
    }


def test_break_glass_requires_recent_successful_rollback_proof() -> None:
    validate_break_glass(_break_glass(), now=NOW)
    failed = _break_glass()
    failed["rollback"]["successful"] = False
    with pytest.raises(GovernanceError, match="did not succeed"):
        validate_break_glass(failed, now=NOW)


def test_break_glass_rejects_an_invalid_repository() -> None:
    break_glass = _break_glass()
    break_glass["repository"] = "owner/repo/extra"
    with pytest.raises(GovernanceError, match="repository is invalid"):
        validate_break_glass(break_glass, now=NOW)
