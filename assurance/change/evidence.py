"""Evidence contract validation and deterministic artifact writing."""
from __future__ import annotations

import hashlib
import platform
import re
from pathlib import Path
from typing import Any

from .foundation import (
    GIT_SHA,
    SCHEMA_VERSION,
    AssuranceError,
    DependencyEnvironment,
    GitRepository,
    canonical_bytes,
    canonical_digest,
    digest_bytes,
)


def _environment_evidence(
    repo: GitRepository, head: str, environment: DependencyEnvironment
) -> dict[str, Any]:
    inventory = environment.inventory()
    return {
        "schema_version": SCHEMA_VERSION,
        "subject_sha": head,
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "os": platform.system(),
        "platform": platform.platform(),
        "git_version": repo.git_version(),
        "installed_distributions": inventory,
        "dependency_sha256": canonical_digest(inventory),
    }


def validate_manifest(manifest: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "generated_at",
        "subject",
        "changes",
        "risk_profiles",
        "applicable_controls",
        "policy",
    }
    if set(manifest) != required:
        raise AssuranceError("change manifest fields do not match the v1.0 contract")
    if manifest["schema_version"] != SCHEMA_VERSION or not manifest["changes"]:
        raise AssuranceError("change manifest is empty or has the wrong schema version")
    subject = manifest["subject"]
    for key in ("base_sha", "merge_base_sha", "head_sha"):
        if not GIT_SHA.fullmatch(subject.get(key, "")):
            raise AssuranceError(f"manifest subject.{key} is not a full Git SHA")
    if not subject.get("diff_sha256", "").startswith("sha256:"):
        raise AssuranceError("manifest diff digest is missing")
    if manifest["policy"].get("rollout_mode") != "observe":
        raise AssuranceError("RPA-180 only permits Observe rollout")


def validate_report(report: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "run_id",
        "generated_at",
        "subject",
        "environment",
        "manifest_evidence",
        "controls",
        "evidence_complete",
        "assurance_decision",
        "business_outcome",
        "enforcement",
    }
    if set(report) != required:
        raise AssuranceError("assurance report fields do not match the v1.0 contract")
    if report["schema_version"] != SCHEMA_VERSION:
        raise AssuranceError("assurance report schema version mismatch")
    if report["enforcement"] != {"mode": "observe", "blocks_merge": False}:
        raise AssuranceError("Observe report cannot claim a merge blocking effect")
    allowed_statuses = {"pass", "fail", "error", "unassured", "not_applicable"}
    statuses = {control["status"] for control in report["controls"]}
    if not statuses <= allowed_statuses:
        raise AssuranceError("assurance report contains an unknown control status")
    control_ids = [control["control_id"] for control in report["controls"]]
    if len(control_ids) != len(set(control_ids)):
        raise AssuranceError("assurance report contains duplicate control results")
    for control in report["controls"]:
        evidence = control["evidence"]
        if evidence["subject_sha"] != report["subject"]["head_sha"]:
            raise AssuranceError("control evidence is bound to a different subject")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", evidence["sha256"]):
            raise AssuranceError("control evidence digest is malformed")
    if report["assurance_decision"] == "allow_candidate":
        if not report["evidence_complete"] or report["manifest_evidence"] is None:
            raise AssuranceError("allow_candidate requires complete evidence")
        if statuses & {"fail", "error", "unassured"}:
            raise AssuranceError("allow_candidate cannot contain a non-passing control")
        for key in ("base_sha", "head_sha"):
            if not GIT_SHA.fullmatch(report["subject"].get(key, "")):
                raise AssuranceError("allow_candidate requires full subject binding")
    elif report["assurance_decision"] == "deny":
        if "fail" not in statuses:
            raise AssuranceError("deny requires at least one failed control")
    elif report["assurance_decision"] == "unassured":
        if report["evidence_complete"] and not statuses & {"error", "unassured"}:
            raise AssuranceError("unassured requires incomplete evidence or an indeterminate control")


class ArtifactWriter:
    _KNOWN_OUTPUTS = {
        "change-manifest.json",
        "dependency-evidence.json",
        "protected-change-evidence.json",
        "runtime-environment.json",
        "subject-evidence.json",
        "evidence-integrity.json",
        "assurance-report.json",
        "detector-error.json",
        "evidence-index.json",
        "SHA256SUMS",
    }

    def __init__(self, output: Path, subject_sha: str):
        self.output = output.absolute()
        self.subject_sha = subject_sha
        for candidate in (self.output, *self.output.parents):
            if candidate.exists() and candidate.is_symlink():
                raise AssuranceError(f"evidence output traverses a symbolic link: {candidate}")
        if self.output.exists() and not self.output.is_dir():
            raise AssuranceError("evidence output exists and is not a directory")
        self.output.mkdir(parents=True, exist_ok=True)
        for path in self.output.iterdir():
            if path.is_symlink() or not path.is_file() or path.name not in self._KNOWN_OUTPUTS:
                raise AssuranceError(f"evidence output contains an unknown entry: {path.name}")
        for path in self.output.iterdir():
            path.unlink()
        self.refs: dict[str, dict[str, str]] = {}

    def write_json(self, name: str, value: Any) -> dict[str, str]:
        if Path(name).name != name or not name.endswith(".json"):
            raise AssuranceError(f"invalid evidence filename: {name}")
        raw = canonical_bytes(value)
        (self.output / name).write_bytes(raw)
        ref = {"uri": name, "sha256": digest_bytes(raw), "subject_sha": self.subject_sha}
        self.refs[name] = ref
        return ref

    def verify_ref(self, ref: dict[str, str]) -> bool:
        path = self.output / ref["uri"]
        return path.is_file() and digest_bytes(path.read_bytes()) == ref["sha256"]

    def finalize(self) -> None:
        entries = []
        for path in sorted(self.output.glob("*")):
            if path.is_file() and path.name not in {"evidence-index.json", "SHA256SUMS"}:
                entries.append(
                    {"uri": path.name, "sha256": digest_bytes(path.read_bytes()), "subject_sha": self.subject_sha}
                )
        self.write_json(
            "evidence-index.json",
            {"schema_version": SCHEMA_VERSION, "subject_sha": self.subject_sha, "artifacts": entries},
        )
        lines = []
        for path in sorted(self.output.glob("*")):
            if path.is_file() and path.name != "SHA256SUMS":
                lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
        (self.output / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="ascii")
