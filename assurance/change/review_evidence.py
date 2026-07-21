"""Normalize GitHub review events into subject-bound human review evidence."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from .foundation import GIT_SHA, SCHEMA_VERSION, AssuranceError, canonical_bytes


REVIEW_STATUSES = frozenset({"approved", "missing", "stale", "dismissed", "rejected"})


def _text(value: Any, *, limit: int = 100) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def build_review_evidence(
    event: dict[str, Any], *, event_name: str, repository: str, expected_head_sha: str
) -> dict[str, Any]:
    """Accept only a distinct human APPROVED review bound to the current PR head."""
    if not GIT_SHA.fullmatch(expected_head_sha):
        raise AssuranceError("review evidence expected head SHA is invalid")
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict):
        raise AssuranceError("review event is missing pull_request")
    observed_repository = _text(
        (event.get("repository") or {}).get("full_name")
        if isinstance(event.get("repository"), dict)
        else None
    )
    observed_head = _text(
        (pull_request.get("head") or {}).get("sha")
        if isinstance(pull_request.get("head"), dict)
        else None
    )
    number = pull_request.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise AssuranceError("review event pull request number is invalid")
    author = _text(
        (pull_request.get("user") or {}).get("login")
        if isinstance(pull_request.get("user"), dict)
        else None
    )
    action = _text(event.get("action"), limit=30) or "unknown"
    status = "missing"
    reason_code = "HUMAN_REVIEW_NOT_SUBMITTED"
    review_out: dict[str, str] | None = None

    if observed_repository != repository or observed_head != expected_head_sha:
        status = "stale"
        reason_code = "HUMAN_REVIEW_SUBJECT_MISMATCH"
    elif event_name == "pull_request_review":
        review = event.get("review")
        if not isinstance(review, dict):
            raise AssuranceError("pull_request_review event is missing review")
        state = (_text(review.get("state"), limit=30) or "").lower()
        reviewer = review.get("user") if isinstance(review.get("user"), dict) else {}
        reviewer_login = _text(reviewer.get("login"))
        reviewer_type = _text(reviewer.get("type"), limit=30)
        commit_id = _text(review.get("commit_id"), limit=40)
        submitted_at = _text(review.get("submitted_at"), limit=40)
        if action == "dismissed" or state == "dismissed":
            status = "dismissed"
            reason_code = "HUMAN_REVIEW_DISMISSED"
        elif action != "submitted" or state != "approved":
            status = "missing"
            reason_code = "HUMAN_REVIEW_NOT_APPROVED"
        elif commit_id != expected_head_sha:
            status = "stale"
            reason_code = "HUMAN_REVIEW_HEAD_MISMATCH"
        elif not reviewer_login or reviewer_login == author:
            status = "rejected"
            reason_code = "HUMAN_REVIEW_NOT_INDEPENDENT"
        elif reviewer_type != "User" or reviewer_login.lower().endswith("[bot]"):
            status = "rejected"
            reason_code = "HUMAN_REVIEW_NOT_HUMAN"
        elif not submitted_at:
            status = "rejected"
            reason_code = "HUMAN_REVIEW_TIME_MISSING"
        else:
            status = "approved"
            reason_code = "HUMAN_REVIEW_VERIFIED"
            review_out = {
                "reviewer_login": reviewer_login,
                "submitted_at": submitted_at,
                "commit_id": commit_id,
            }

    return {
        "schema_version": SCHEMA_VERSION,
        "repository": repository,
        "pull_request_number": number,
        "event_name": event_name,
        "event_action": action,
        "expected_head_sha": expected_head_sha,
        "observed_head_sha": observed_head,
        "status": status,
        "reason_code": reason_code,
        "review": review_out,
    }


def validate_review_evidence(
    value: Any, *, repository: str, expected_head_sha: str
) -> dict[str, Any]:
    required = {
        "schema_version",
        "repository",
        "pull_request_number",
        "event_name",
        "event_action",
        "expected_head_sha",
        "observed_head_sha",
        "status",
        "reason_code",
        "review",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise AssuranceError("human review evidence fields do not match the contract")
    if value["schema_version"] != SCHEMA_VERSION:
        raise AssuranceError("human review evidence schema version mismatch")
    if value["repository"] != repository or value["expected_head_sha"] != expected_head_sha:
        raise AssuranceError("human review evidence is bound to a different subject")
    if value["observed_head_sha"] != expected_head_sha:
        if value["status"] != "stale":
            raise AssuranceError("mismatched review evidence must be stale")
    elif not GIT_SHA.fullmatch(str(value["observed_head_sha"])):
        raise AssuranceError("human review observed head SHA is invalid")
    if value["status"] not in REVIEW_STATUSES:
        raise AssuranceError("human review evidence status is invalid")
    number = value["pull_request_number"]
    if number is not None and (
        not isinstance(number, int) or isinstance(number, bool) or number < 1
    ):
        raise AssuranceError("human review pull request number is invalid")
    if not isinstance(value["reason_code"], str) or not re.fullmatch(
        r"[A-Z0-9_]+", value["reason_code"]
    ):
        raise AssuranceError("human review reason code is invalid")
    review = value["review"]
    if value["status"] == "approved":
        if not isinstance(review, dict) or set(review) != {
            "reviewer_login",
            "submitted_at",
            "commit_id",
        }:
            raise AssuranceError("approved human review evidence is incomplete")
        if review["commit_id"] != expected_head_sha:
            raise AssuranceError("approved human review targets a different commit")
        if not all(isinstance(review[key], str) and review[key] for key in review):
            raise AssuranceError("approved human review fields are invalid")
    elif review is not None:
        raise AssuranceError("non-approved human review evidence must not identify a reviewer")
    return value


def missing_review_evidence(*, repository: str, expected_head_sha: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": repository,
        "pull_request_number": None,
        "event_name": "local",
        "event_action": "none",
        "expected_head_sha": expected_head_sha,
        "observed_head_sha": expected_head_sha,
        "status": "missing",
        "reason_code": "HUMAN_REVIEW_NOT_SUBMITTED",
        "review": None,
    }


def load_review_evidence(
    path: Path | None, *, repository: str, expected_head_sha: str
) -> dict[str, Any]:
    if path is None:
        return missing_review_evidence(repository=repository, expected_head_sha=expected_head_sha)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssuranceError("human review evidence is unreadable") from exc
    return validate_review_evidence(value, repository=repository, expected_head_sha=expected_head_sha)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Build trusted GitHub human review evidence")
    value.add_argument("--event", type=Path, required=True)
    value.add_argument("--event-name", required=True)
    value.add_argument("--repository", required=True)
    value.add_argument("--head-sha", required=True)
    value.add_argument("--output", type=Path, required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        event = json.loads(args.event.read_text(encoding="utf-8"))
        evidence = build_review_evidence(
            event,
            event_name=args.event_name,
            repository=args.repository,
            expected_head_sha=args.head_sha,
        )
        validate_review_evidence(
            evidence, repository=args.repository, expected_head_sha=args.head_sha
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(canonical_bytes(evidence))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AssuranceError) as exc:
        print(f"Human review evidence failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
