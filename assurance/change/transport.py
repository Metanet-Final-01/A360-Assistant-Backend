"""Fail-closed transport contract for publishing Change Assurance artifacts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .evidence import validate_manifest, validate_report
from .foundation import AssuranceError, GIT_SHA, SCHEMA_VERSION, canonical_bytes, digest_bytes
from .schema_validation import SchemaValidationError, validate_json_schema


REPORT_SCHEMA = Path(__file__).with_name("schemas") / "assurance-report.schema.json"
MANIFEST_SCHEMA = Path(__file__).with_name("schemas") / "change-manifest.schema.json"
KNOWN_JSON_ARTIFACTS = frozenset({
    "assurance-report.json",
    "change-manifest.json",
    "dependency-evidence.json",
    "protected-change-evidence.json",
    "runtime-environment.json",
    "subject-evidence.json",
    "evidence-integrity.json",
    "detector-error.json",
    "evidence-index.json",
})
KNOWN_FILES = KNOWN_JSON_ARTIFACTS | {"SHA256SUMS"}
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_ENVELOPE_BYTES = 8 * 1024 * 1024
_SOURCE_FIELDS = {
    "repository",
    "workflow_name",
    "workflow_run_id",
    "run_attempt",
    "event",
    "conclusion",
    "head_sha",
    "pull_request_number",
}


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AssuranceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(raw: bytes, *, name: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssuranceError(f"invalid JSON artifact: {name}") from exc
    if not isinstance(value, dict):
        raise AssuranceError(f"artifact root must be an object: {name}")
    if canonical_bytes(value) != raw:
        raise AssuranceError(f"artifact is not canonical JSON: {name}")
    return value


def load_change_envelope(directory: Path, *, source: dict[str, Any]) -> dict[str, Any]:
    """Load only the known artifact set and preserve canonical artifact bytes."""
    if directory.is_symlink():
        raise AssuranceError("change assurance artifact directory is unavailable or symbolic")
    root = directory.resolve()
    if not root.is_dir():
        raise AssuranceError("change assurance artifact directory is unavailable or symbolic")
    artifacts: dict[str, dict[str, Any]] = {}
    raw_artifacts: dict[str, bytes] = {}
    checksum_bytes: bytes | None = None
    total_bytes = 0
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file() or path.name not in KNOWN_FILES:
            raise AssuranceError(f"unknown or unsafe artifact entry: {path.name}")
        size = path.stat().st_size
        if size > MAX_ARTIFACT_BYTES:
            raise AssuranceError(f"change assurance artifact is too large: {path.name}")
        total_bytes += size
        if total_bytes > MAX_ENVELOPE_BYTES:
            raise AssuranceError("change assurance artifact bundle is too large")
        raw = path.read_bytes()
        if path.name.endswith(".json"):
            raw_artifacts[path.name] = raw
            artifacts[path.name] = _load_json(raw, name=path.name)
        else:
            checksum_bytes = raw
    if checksum_bytes is None:
        raise AssuranceError("change assurance checksum manifest is missing")
    expected_checksums = [
        f"{digest_bytes(raw).removeprefix('sha256:')}  {name}"
        for name, raw in sorted(raw_artifacts.items())
    ]
    try:
        observed_checksums = checksum_bytes.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise AssuranceError("change assurance checksum manifest is not ASCII") from exc
    if not checksum_bytes.endswith(b"\n") or observed_checksums != expected_checksums:
        raise AssuranceError("change assurance checksum manifest does not match")
    envelope = {"schema_version": SCHEMA_VERSION, "source": source, "artifacts": artifacts}
    validate_change_envelope(envelope)
    return envelope


def _artifact_digest(value: dict[str, Any]) -> str:
    return digest_bytes(canonical_bytes(value))


def _validate_ref(
    ref: Any,
    *,
    artifacts: dict[str, dict[str, Any]],
    digests: dict[str, str],
    head_sha: str,
) -> None:
    if not isinstance(ref, dict) or set(ref) != {"uri", "sha256", "subject_sha"}:
        raise AssuranceError("evidence reference fields do not match the transport contract")
    uri = ref.get("uri")
    if uri not in artifacts or uri == "evidence-index.json":
        raise AssuranceError("evidence reference does not resolve to a transported artifact")
    if ref.get("subject_sha") != head_sha or ref.get("sha256") != digests[uri]:
        raise AssuranceError("evidence reference subject or digest does not match")


def _validate_source(source: Any) -> dict[str, Any]:
    if not isinstance(source, dict) or set(source) != _SOURCE_FIELDS:
        raise AssuranceError("publisher source fields do not match the transport contract")
    if source.get("workflow_name") != "Change Assurance (Observe)":
        raise AssuranceError("publisher workflow is not authoritative")
    if (
        source.get("event") not in {"pull_request", "pull_request_review"}
        or source.get("conclusion") != "success"
    ):
        raise AssuranceError("publisher source is not a successful pull request workflow")
    repository = source.get("repository")
    if not isinstance(repository, str) or re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
    ) is None:
        raise AssuranceError("publisher repository is invalid")
    if not GIT_SHA.fullmatch(str(source.get("head_sha", ""))):
        raise AssuranceError("publisher head SHA is invalid")
    for key in ("workflow_run_id", "run_attempt", "pull_request_number"):
        value = source.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise AssuranceError(f"publisher {key} must be a positive integer")
    return source


def validate_change_envelope(envelope: Any) -> dict[str, Any]:
    """Recompute artifact binding and return the compact facts safe to persist."""
    if not isinstance(envelope, dict) or set(envelope) != {"schema_version", "source", "artifacts"}:
        raise AssuranceError("change publisher envelope fields do not match the contract")
    if envelope.get("schema_version") != SCHEMA_VERSION:
        raise AssuranceError("change publisher envelope schema version mismatch")
    source = _validate_source(envelope.get("source"))
    artifacts = envelope.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise AssuranceError("change publisher artifacts are missing")
    if not set(artifacts) <= KNOWN_JSON_ARTIFACTS:
        raise AssuranceError("change publisher contains an unknown artifact")
    if not all(isinstance(value, dict) for value in artifacts.values()):
        raise AssuranceError("change publisher artifact roots must be objects")
    envelope_size = sum(len(canonical_bytes(value)) for value in artifacts.values())
    if envelope_size > MAX_ENVELOPE_BYTES:
        raise AssuranceError("change publisher envelope is too large")
    required = {"assurance-report.json", "evidence-index.json"}
    if not required <= set(artifacts):
        raise AssuranceError("change publisher is missing its report or evidence index")

    report = artifacts["assurance-report.json"]
    try:
        report_schema = json.loads(REPORT_SCHEMA.read_text(encoding="utf-8"))
        validate_json_schema(report, report_schema)
        validate_report(report)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, SchemaValidationError) as exc:
        raise AssuranceError("change assurance report contract validation failed") from exc

    subject = report.get("subject")
    if not isinstance(subject, dict) or subject != {
        "repository": source["repository"],
        "base_sha": subject.get("base_sha"),
        "head_sha": source["head_sha"],
    }:
        raise AssuranceError("report subject does not match the trusted workflow source")
    if not GIT_SHA.fullmatch(str(subject.get("base_sha", ""))):
        raise AssuranceError("report base SHA is invalid")

    digests = {name: _artifact_digest(value) for name, value in artifacts.items()}
    index = artifacts["evidence-index.json"]
    if set(index) != {"schema_version", "subject_sha", "artifacts"}:
        raise AssuranceError("evidence index fields do not match the contract")
    if (
        index.get("schema_version") != SCHEMA_VERSION
        or index.get("subject_sha") != source["head_sha"]
    ):
        raise AssuranceError("evidence index subject is invalid")
    entries = index.get("artifacts")
    if not isinstance(entries, list):
        raise AssuranceError("evidence index artifact list is invalid")
    indexed: set[str] = set()
    for ref in entries:
        _validate_ref(ref, artifacts=artifacts, digests=digests, head_sha=source["head_sha"])
        uri = ref["uri"]
        if uri in indexed:
            raise AssuranceError("evidence index contains a duplicate artifact")
        indexed.add(uri)
    if indexed != set(artifacts) - {"evidence-index.json"}:
        raise AssuranceError("transported artifacts and evidence index differ")

    manifest = artifacts.get("change-manifest.json")
    manifest_ref = report.get("manifest_evidence")
    if manifest is None:
        if manifest_ref is not None:
            raise AssuranceError("report references a missing change manifest")
        policy_digest = None
        manifest_digest = None
        diff_digest = None
    else:
        try:
            manifest_schema = json.loads(MANIFEST_SCHEMA.read_text(encoding="utf-8"))
            validate_json_schema(manifest, manifest_schema)
            validate_manifest(manifest)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, SchemaValidationError) as exc:
            raise AssuranceError("change manifest contract validation failed") from exc
        _validate_ref(
            manifest_ref,
            artifacts=artifacts,
            digests=digests,
            head_sha=source["head_sha"],
        )
        if manifest.get("subject", {}).get("repository") != source["repository"]:
            raise AssuranceError("manifest repository does not match the workflow source")
        if manifest.get("subject", {}).get("base_sha") != subject["base_sha"]:
            raise AssuranceError("manifest base SHA does not match the report")
        if manifest.get("subject", {}).get("head_sha") != source["head_sha"]:
            raise AssuranceError("manifest head SHA does not match the workflow source")
        policy_digest = manifest.get("policy", {}).get("sha256")
        manifest_digest = digests["change-manifest.json"]
        diff_digest = manifest.get("subject", {}).get("diff_sha256")

    for control in report.get("controls", []):
        _validate_ref(
            control.get("evidence"),
            artifacts=artifacts,
            digests=digests,
            head_sha=source["head_sha"],
        )
    integrity = artifacts.get("evidence-integrity.json")
    if integrity is not None:
        if (
            integrity.get("subject_sha") != source["head_sha"]
            or integrity.get("all_verified") is not True
        ):
            raise AssuranceError("evidence integrity artifact is incomplete")
        for ref in integrity.get("evidence", []):
            _validate_ref(ref, artifacts=artifacts, digests=digests, head_sha=source["head_sha"])

    runtime = artifacts.get("runtime-environment.json")
    if runtime is not None:
        if runtime.get("subject_sha") != source["head_sha"]:
            raise AssuranceError("runtime evidence is bound to a different subject")
        for key, value in report.get("environment", {}).items():
            if runtime.get(key) != value:
                raise AssuranceError("runtime evidence does not match the report")

    return {
        "source": source,
        "report": report,
        "report_digest": digests["assurance-report.json"],
        "evidence_index_digest": digests["evidence-index.json"],
        "manifest_digest": manifest_digest,
        "runtime_digest": digests.get("runtime-environment.json"),
        "policy_digest": policy_digest,
        "diff_digest": diff_digest,
        "artifact_names": sorted(indexed),
    }
