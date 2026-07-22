from __future__ import annotations

import json

import pytest

from assurance.change.foundation import AssuranceError
from assurance.change.review_evidence import (
    build_review_evidence,
    main,
    validate_review_evidence,
)


REPOSITORY = "Metanet-Final-01/A360-Assistant-Backend"
HEAD = "a" * 40


def _event(*, reviewer: str = "reviewer", commit_id: str = HEAD, action: str = "submitted"):
    return {
        "action": action,
        "repository": {"full_name": REPOSITORY},
        "pull_request": {
            "number": 329,
            "head": {"sha": HEAD},
            "user": {"login": "author"},
        },
        "review": {
            "state": "approved" if action == "submitted" else "dismissed",
            "commit_id": commit_id,
            "submitted_at": "2026-07-21T08:00:00Z",
            "user": {"login": reviewer, "type": "User"},
        },
    }


def test_exact_head_independent_human_approval_is_verified():
    evidence = build_review_evidence(
        _event(),
        event_name="pull_request_review",
        repository=REPOSITORY,
        expected_head_sha=HEAD,
    )

    assert evidence["status"] == "approved"
    assert evidence["reason_code"] == "HUMAN_REVIEW_VERIFIED"
    assert evidence["review"] == {
        "reviewer_login": "reviewer",
        "submitted_at": "2026-07-21T08:00:00Z",
        "commit_id": HEAD,
    }
    assert validate_review_evidence(
        evidence, repository=REPOSITORY, expected_head_sha=HEAD
    ) == evidence


@pytest.mark.parametrize(
    ("event", "status", "reason"),
    [
        (_event(reviewer="author"), "rejected", "HUMAN_REVIEW_NOT_INDEPENDENT"),
        (_event(commit_id="b" * 40), "stale", "HUMAN_REVIEW_HEAD_MISMATCH"),
        (_event(action="dismissed"), "dismissed", "HUMAN_REVIEW_DISMISSED"),
    ],
)
def test_unusable_reviews_fail_closed(event, status, reason):
    evidence = build_review_evidence(
        event,
        event_name="pull_request_review",
        repository=REPOSITORY,
        expected_head_sha=HEAD,
    )

    assert evidence["status"] == status
    assert evidence["reason_code"] == reason
    assert evidence["review"] is None


def test_pull_request_event_does_not_claim_a_review():
    event = _event()
    event.pop("review")
    evidence = build_review_evidence(
        event,
        event_name="pull_request",
        repository=REPOSITORY,
        expected_head_sha=HEAD,
    )
    assert evidence["status"] == "missing"


def test_approved_evidence_cannot_be_rebound_to_another_head():
    evidence = build_review_evidence(
        _event(),
        event_name="pull_request_review",
        repository=REPOSITORY,
        expected_head_sha=HEAD,
    )
    forged = json.loads(json.dumps(evidence))
    forged["review"]["commit_id"] = "b" * 40

    with pytest.raises(AssuranceError, match="different commit"):
        validate_review_evidence(forged, repository=REPOSITORY, expected_head_sha=HEAD)


def test_cli_reports_safe_contract_error_detail(tmp_path, capsys):
    event_path = tmp_path / "event.json"
    output_path = tmp_path / "evidence.json"
    event_path.write_text("{}", encoding="utf-8")

    result = main(
        [
            "--event",
            str(event_path),
            "--event-name",
            "pull_request_review",
            "--repository",
            REPOSITORY,
            "--head-sha",
            HEAD,
            "--output",
            str(output_path),
        ]
    )

    assert result == 2
    assert (
        capsys.readouterr().err
        == "Human review evidence failed: AssuranceError: review event is missing pull_request\n"
    )


def test_cli_generalizes_file_errors(tmp_path, capsys):
    missing_path = tmp_path / "sensitive-event-name.json"

    result = main(
        [
            "--event",
            str(missing_path),
            "--event-name",
            "pull_request_review",
            "--repository",
            REPOSITORY,
            "--head-sha",
            HEAD,
            "--output",
            str(tmp_path / "evidence.json"),
        ]
    )

    error = capsys.readouterr().err
    assert result == 2
    assert error == "Human review evidence failed: FileNotFoundError\n"
    assert str(missing_path) not in error
