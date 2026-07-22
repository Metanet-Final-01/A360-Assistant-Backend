"""Publish one validated Change Assurance artifact bundle to the protected writer API."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from assurance.change.foundation import AssuranceError, canonical_bytes
from assurance.change.transport import load_change_envelope


WORKFLOW_NAME = "Change Assurance (Observe)"
WRITER_PATH = "/api/internal/assurance/change-receipts"
MAX_RESPONSE_BYTES = 64 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep the writer bearer token on the configured origin."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _positive_int(value: Any, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise AssuranceError(f"workflow_run.{field} must be a positive integer")
    return value


def source_from_event(
    event: Any,
    *,
    expected_repository: str,
    resolved_pull_request_number: Any = None,
) -> dict[str, Any]:
    """Derive source identity only from GitHub's trusted workflow_run event."""
    if not isinstance(event, dict):
        raise AssuranceError("workflow_run event root must be an object")
    repository = event.get("repository")
    run = event.get("workflow_run")
    if not isinstance(repository, dict) or not isinstance(run, dict):
        raise AssuranceError("workflow_run event is incomplete")
    full_name = repository.get("full_name")
    run_repository = run.get("repository")
    if not isinstance(run_repository, dict):
        raise AssuranceError("workflow_run repository identity is missing")
    if full_name != expected_repository or run_repository.get("full_name") != full_name:
        raise AssuranceError("workflow_run repository does not match the trusted repository")
    if run.get("name") != WORKFLOW_NAME:
        raise AssuranceError("workflow_run name is not authoritative")
    run_event = run.get("event")
    if (
        run_event not in {"pull_request", "pull_request_review"}
        or run.get("conclusion") != "success"
    ):
        raise AssuranceError("workflow_run is not a successful pull request assurance run")
    pull_requests = run.get("pull_requests")
    if not isinstance(pull_requests, list) or len(pull_requests) > 1:
        raise AssuranceError("workflow_run must identify at most one pull request")
    event_pull_request_number = None
    if pull_requests:
        pull_request = pull_requests[0]
        if not isinstance(pull_request, dict):
            raise AssuranceError("workflow_run pull request identity is invalid")
        event_pull_request_number = _positive_int(
            pull_request.get("number"), field="pull_request.number"
        )
    resolved_number = None
    if resolved_pull_request_number is not None:
        resolved_number = _positive_int(
            resolved_pull_request_number, field="resolved_pull_request_number"
        )
    if event_pull_request_number is None and resolved_number is None:
        raise AssuranceError("workflow_run pull request identity is unavailable")
    if (
        event_pull_request_number is not None
        and resolved_number is not None
        and event_pull_request_number != resolved_number
    ):
        raise AssuranceError("resolved pull request does not match the workflow_run event")
    head_sha = run.get("head_sha")
    if not isinstance(head_sha, str):
        raise AssuranceError("workflow_run head SHA is missing")
    return {
        "repository": full_name,
        "workflow_name": WORKFLOW_NAME,
        "workflow_run_id": _positive_int(run.get("id"), field="id"),
        "run_attempt": _positive_int(run.get("run_attempt"), field="run_attempt"),
        "event": run_event,
        "conclusion": "success",
        "head_sha": head_sha,
        "pull_request_number": event_pull_request_number or resolved_number,
    }


def writer_url(base_url: str) -> str:
    """Accept HTTPS deployments and loopback HTTP for deterministic local verification."""
    parsed = urllib.parse.urlsplit(base_url.strip())
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AssuranceError("writer URL must not include credentials, query, or fragment")
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise AssuranceError("writer URL must use HTTPS outside loopback")
    if not parsed.netloc:
        raise AssuranceError("writer URL host is missing")
    base_path = parsed.path.rstrip("/")
    if base_path and base_path != WRITER_PATH:
        raise AssuranceError("writer URL must be an origin or the exact writer endpoint")
    path = base_path or WRITER_PATH
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def publish(
    envelope: dict[str, Any],
    *,
    url: str,
    token: str,
    timeout: float = 30.0,
    opener: Any = None,
) -> dict[str, Any]:
    if len(token) < 32:
        raise AssuranceError("writer token is not configured")
    request = urllib.request.Request(
        writer_url(url),
        data=canonical_bytes(envelope),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    transport = opener or urllib.request.build_opener(_NoRedirect())
    try:
        with transport.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            status = response.status
    except urllib.error.HTTPError as exc:
        exc.read(MAX_RESPONSE_BYTES + 1)
        raise AssuranceError(f"writer rejected the record with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise AssuranceError("writer endpoint is unavailable") from exc
    if status != 201 or len(raw) > MAX_RESPONSE_BYTES:
        raise AssuranceError("writer returned an invalid response")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssuranceError("writer response is not valid JSON") from exc
    if not isinstance(result, dict) or result.get("status") != "persisted":
        raise AssuranceError("writer did not confirm persistence")
    receipt_digest = result.get("receipt_digest")
    if not isinstance(receipt_digest, str) or re.fullmatch(
        r"sha256:[0-9a-f]{64}", receipt_digest
    ) is None:
        raise AssuranceError("writer response is missing the receipt digest")
    return result


def _parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--event", required=True, type=Path)
    value.add_argument("--artifacts", required=True, type=Path)
    value.add_argument("--repository", required=True)
    value.add_argument("--pull-request-number", required=True, type=int)
    value.add_argument("--url", required=True)
    return value


def main() -> int:
    args = _parser().parse_args()
    try:
        event = json.loads(args.event.read_text(encoding="utf-8"))
        source = source_from_event(
            event,
            expected_repository=args.repository,
            resolved_pull_request_number=args.pull_request_number,
        )
        envelope = load_change_envelope(args.artifacts, source=source)
        result = publish(
            envelope,
            url=args.url,
            token=os.getenv("ASSURANCE_WRITER_TOKEN", ""),
        )
    except (AssuranceError, OSError, json.JSONDecodeError) as exc:
        print(f"Change assurance publication failed: {exc}", file=sys.stderr)
        return 1
    print(
        "Change assurance record persisted: "
        f"{result['receipt_digest']} (idempotent={bool(result.get('idempotent'))})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
