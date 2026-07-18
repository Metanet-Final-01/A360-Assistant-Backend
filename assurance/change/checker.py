"""Orchestrate Change Assurance decisions and Observe-mode receipts."""
from __future__ import annotations

import hashlib
import json
import platform
from datetime import datetime
from pathlib import Path
from typing import Any

from .dependency_checks import (
    _overall_dependency_status,
    derive_dependency_evidence,
    derive_protected_evidence,
    derive_risk_profiles,
)
from .evidence import ArtifactWriter, _environment_evidence, validate_manifest, validate_report
from .foundation import (
    CONTROL_ORDER,
    GIT_SHA,
    SCHEMA_VERSION,
    AssuranceError,
    DependencyEnvironment,
    DistributionInspection,
    GitRepository,
    ImportInspection,
    InstalledDependencyEnvironment,
    _safe_detail,
    canonical_digest,
    digest_bytes,
    isoformat,
    utc_now,
)
from .schema_validation import SchemaValidationError, validate_json_schema


POLICY_SCHEMA = Path(__file__).resolve().parent / "schemas" / "dependency-policy.schema.json"


class AssuranceRunner:
    def __init__(
        self,
        *,
        repo_root: Path,
        base_sha: str,
        head_sha: str,
        repository: str,
        output: Path,
        policy: dict[str, Any],
        policy_uri: str,
        policy_digest: str,
        environment: DependencyEnvironment,
        now: datetime | None = None,
    ):
        self.repo = GitRepository(repo_root)
        self.base_sha = base_sha
        self.head_sha = head_sha
        self.repository = repository
        self.output = output
        self.policy = policy
        self.policy_uri = policy_uri
        self.policy_digest = policy_digest
        self.environment = environment
        self.now = now or utc_now()

    def run(self) -> dict[str, Any]:
        if self.policy.get("schema_version") != SCHEMA_VERSION:
            raise AssuranceError("dependency policy schema version mismatch")
        if self.policy.get("rollout_mode") != "observe":
            raise AssuranceError("RPA-180 may only run in Observe mode")
        base = self.repo.commit(self.base_sha)
        head = self.repo.commit(self.head_sha)
        merge_base = self.repo.merge_base(base, head)
        changes = self.repo.changes(base, head)
        if not changes:
            raise AssuranceError("the trusted Git diff is empty")
        diff_digest = digest_bytes(self.repo.diff_bytes(base, head))
        actual_head = self.repo.head()
        tracked_clean = self.repo.tracked_clean()
        environment_evidence = _environment_evidence(self.repo, head, self.environment)

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": isoformat(self.now),
            "subject": {
                "repository": self.repository,
                "base_sha": base,
                "merge_base_sha": merge_base,
                "head_sha": head,
                "diff_sha256": diff_digest,
            },
            "changes": changes,
            "risk_profiles": derive_risk_profiles(changes, self.policy),
            "applicable_controls": list(CONTROL_ORDER),
            "policy": {
                "uri": self.policy_uri,
                "sha256": self.policy_digest,
                "rollout_mode": "observe",
                "decision_state": self.policy["policy_decision_state"],
            },
        }
        validate_manifest(manifest)

        protected = derive_protected_evidence(
            self.repo, base, head, changes, self.policy
        )
        dependency = derive_dependency_evidence(
            self.repo,
            base,
            head,
            changes,
            self.policy,
            self.environment,
            protected,
            self.now,
        )
        subject_evidence = {
            "schema_version": SCHEMA_VERSION,
            "repository": self.repository,
            "expected_head_sha": head,
            "actual_checkout_sha": actual_head,
            "tracked_worktree_clean": tracked_clean,
            "merge_base_sha": merge_base,
            "diff_sha256": diff_digest,
        }

        writer = ArtifactWriter(self.output, head)
        manifest_ref = writer.write_json("change-manifest.json", manifest)
        dependency_ref = writer.write_json("dependency-evidence.json", dependency)
        protected_ref = writer.write_json("protected-change-evidence.json", protected)
        environment_ref = writer.write_json("runtime-environment.json", environment_evidence)
        subject_ref = writer.write_json("subject-evidence.json", subject_evidence)
        pre_integrity_refs = [
            manifest_ref,
            dependency_ref,
            protected_ref,
            environment_ref,
            subject_ref,
        ]
        integrity = {
            "schema_version": SCHEMA_VERSION,
            "subject_sha": head,
            "all_verified": all(writer.verify_ref(ref) for ref in pre_integrity_refs),
            "evidence": pre_integrity_refs,
        }
        integrity_ref = writer.write_json("evidence-integrity.json", integrity)

        controls: list[dict[str, Any]] = [
            {
                "control_id": "CH-01",
                "status": "pass",
                "reason_code": "MANIFEST_DERIVED_FROM_GIT",
                "reason": "Change manifest was derived from Git objects and validated.",
                "evidence": manifest_ref,
            },
            {
                "control_id": "CH-02",
                "status": "pass",
                "reason_code": "RISK_PROFILE_DERIVED",
                "reason": "Risk profiles were selected from the trusted changed-path set.",
                "evidence": manifest_ref,
            },
        ]
        dependency_status = _overall_dependency_status(dependency)
        dependency_reason_codes = {
            "pass": "DEPENDENCY_CLOSURE_VERIFIED",
            "fail": "DEPENDENCY_CLOSURE_DENIED",
            "error": "DEPENDENCY_DETECTOR_ERROR",
            "unassured": "DEPENDENCY_EVIDENCE_INCOMPLETE",
            "not_applicable": "DEPENDENCY_CHANGE_NOT_APPLICABLE",
        }
        controls.append(
            {
                "control_id": "CH-04",
                "status": dependency_status,
                "reason_code": dependency_reason_codes[dependency_status],
                "reason": (
                    "Dependency allowlist, exact pins, imports, vulnerabilities, licenses, "
                    "and install paths were evaluated."
                ),
                "evidence": dependency_ref,
            }
        )
        protected_changed = bool(protected["protected_paths_changed"])
        sensitive = bool(protected["sensitive_indicators"])
        controls.append(
            {
                "control_id": "CH-06",
                "status": "unassured" if protected_changed or sensitive else "pass",
                "reason_code": (
                    "PROTECTED_ORACLE_REVIEW_REQUIRED"
                    if protected_changed or sensitive
                    else "PROTECTED_ORACLE_UNCHANGED"
                ),
                "reason": (
                    "Tests, workflows, assurance policy, or Agent-owned paths changed; "
                    "separate human ownership review is required."
                    if protected_changed or sensitive
                    else "No protected oracle or ownership path changed."
                ),
                "evidence": protected_ref,
            }
        )
        subject_bound = actual_head == head and tracked_clean
        controls.append(
            {
                "control_id": "CH-11",
                "status": "pass" if subject_bound else "unassured",
                "reason_code": "SUBJECT_BOUND" if subject_bound else "SUBJECT_BINDING_INCOMPLETE",
                "reason": (
                    "Checked-out clean commit matches the trusted PR head."
                    if subject_bound
                    else "Checkout SHA or tracked worktree state does not match the trusted subject."
                ),
                "evidence": subject_ref,
            }
        )
        integrity_complete = integrity["all_verified"] and writer.verify_ref(integrity_ref)
        controls.append(
            {
                "control_id": "CH-12",
                "status": "pass" if integrity_complete else "error",
                "reason_code": "EVIDENCE_DIGESTS_VERIFIED" if integrity_complete else "EVIDENCE_DIGEST_MISMATCH",
                "reason": (
                    "Every referenced evidence artifact exists and matches its SHA-256 digest."
                    if integrity_complete
                    else "At least one evidence artifact is missing or has a digest mismatch."
                ),
                "evidence": integrity_ref,
            }
        )
        controls.sort(key=lambda item: CONTROL_ORDER.index(item["control_id"]))
        evidence_complete = integrity_complete and all(writer.verify_ref(item["evidence"]) for item in controls)
        statuses = {item["status"] for item in controls}
        if "fail" in statuses:
            decision = "deny"
        elif not evidence_complete or statuses & {"error", "unassured"}:
            decision = "unassured"
        else:
            decision = "allow_candidate"

        run_id = "CA-" + hashlib.sha256(
            f"{self.repository}\0{base}\0{head}\0{diff_digest}\0{self.policy_digest}".encode("utf-8")
        ).hexdigest()[:16]
        report = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "generated_at": isoformat(self.now),
            "subject": {"repository": self.repository, "base_sha": base, "head_sha": head},
            "environment": {
                key: environment_evidence[key]
                for key in (
                    "python_version",
                    "python_implementation",
                    "os",
                    "platform",
                    "dependency_sha256",
                    "git_version",
                )
            },
            "manifest_evidence": manifest_ref,
            "controls": controls,
            "evidence_complete": evidence_complete,
            "assurance_decision": decision,
            "business_outcome": {"decision": "not_evaluated", "changed_by_assurance": False},
            "enforcement": {"mode": "observe", "blocks_merge": False},
        }
        validate_report(report)
        writer.write_json("assurance-report.json", report)
        writer.finalize()
        return report


def load_policy(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    try:
        policy = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AssuranceError(f"dependency policy is invalid JSON: {_safe_detail(str(exc))}") from exc
    try:
        schema = json.loads(POLICY_SCHEMA.read_text(encoding="utf-8"))
        validate_json_schema(policy, schema)
    except (OSError, json.JSONDecodeError, SchemaValidationError) as exc:
        raise AssuranceError(
            f"dependency policy schema validation failed: {_safe_detail(str(exc))}"
        ) from exc
    return policy, digest_bytes(raw)


def run_assurance(
    *,
    repo_root: Path,
    base_sha: str,
    head_sha: str,
    repository: str,
    output: Path,
    policy_path: Path,
    environment: DependencyEnvironment | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    policy, policy_digest = load_policy(policy_path)
    try:
        policy_uri = policy_path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        policy_uri = policy_path.name
    runner = AssuranceRunner(
        repo_root=repo_root,
        base_sha=base_sha,
        head_sha=head_sha,
        repository=repository,
        output=output,
        policy=policy,
        policy_uri=policy_uri,
        policy_digest=policy_digest,
        environment=environment or InstalledDependencyEnvironment(),
        now=now,
    )
    return runner.run()


def write_error_report(
    *,
    output: Path,
    repository: str,
    base_sha: str,
    head_sha: str,
    error: BaseException,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Best-effort Observe receipt for detector failure; never claims PASS."""
    generated_at = isoformat(now or utc_now())
    subject_head = head_sha if GIT_SHA.fullmatch(head_sha) else "unknown"
    subject_base = base_sha if GIT_SHA.fullmatch(base_sha) else "unknown"
    writer = ArtifactWriter(output, subject_head)
    error_evidence = {
        "schema_version": SCHEMA_VERSION,
        "subject_sha": subject_head,
        "error_type": type(error).__name__,
        "message": _safe_detail(str(error), 500),
    }
    error_ref = writer.write_json("detector-error.json", error_evidence)
    inventory: dict[str, str] = {}
    environment = {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "os": platform.system(),
        "platform": platform.platform(),
        "dependency_sha256": canonical_digest(inventory),
        "git_version": "unknown",
    }
    run_id = "CA-" + hashlib.sha256(
        f"{repository}\0{subject_base}\0{subject_head}\0{type(error).__name__}".encode()
    ).hexdigest()[:16]
    report = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at": generated_at,
        "subject": {"repository": repository or "unknown", "base_sha": subject_base, "head_sha": subject_head},
        "environment": environment,
        "manifest_evidence": None,
        "controls": [
            {
                "control_id": "CH-01",
                "status": "error",
                "reason_code": "DETECTOR_EXECUTION_ERROR",
                "reason": "Trusted evidence derivation failed; no passing decision is possible.",
                "evidence": error_ref,
            }
        ],
        "evidence_complete": False,
        "assurance_decision": "unassured",
        "business_outcome": {"decision": "not_evaluated", "changed_by_assurance": False},
        "enforcement": {"mode": "observe", "blocks_merge": False},
    }
    validate_report(report)
    writer.write_json("assurance-report.json", report)
    writer.finalize()
    return report


def markdown_summary(report: dict[str, Any]) -> str:
    rows = [
        "### Change Assurance (Observe)",
        "",
        f"- Assurance decision: `{report['assurance_decision']}`",
        "- Merge blocking effect: `false`",
        f"- Subject: `{report['subject']['head_sha']}`",
        f"- Evidence complete: `{str(report['evidence_complete']).lower()}`",
        "",
        "| Control | Status | Reason |",
        "|---|---|---|",
    ]
    for control in report["controls"]:
        rows.append(
            f"| {control['control_id']} | `{control['status']}` | {control['reason_code']} |"
        )
    rows.extend(
        [
            "",
            "> Observe records an assurance decision only. It does not approve or block the business merge.",
        ]
    )
    return "\n".join(rows) + "\n"
