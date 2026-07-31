"""Orchestrate Change Assurance decisions and rollout-aware receipts."""
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
from .explanations import explain_control
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
from .review_evidence import load_review_evidence


POLICY_SCHEMA = Path(__file__).resolve().parent / "schemas" / "dependency-policy.schema.json"


def _review_decision_identity(review_evidence: dict[str, Any]) -> dict[str, Any]:
    """Return only review fields that can change the assurance decision identity.

    GitHub pull-request actions such as ``reopened`` and ``ready_for_review`` are useful raw
    evidence, but they must not create a new receipt when the subject and review decision are
    otherwise identical. Approval, dismissal, reviewer, and reviewed commit remain identity inputs.
    """
    return {
        key: review_evidence[key]
        for key in (
            "schema_version",
            "repository",
            "pull_request_number",
            "expected_head_sha",
            "observed_head_sha",
            "status",
            "reason_code",
            "review",
        )
    }


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SchemaValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise SchemaValidationError(f"invalid JSON constant: {value}")


def _strict_json_loads(value: bytes | str) -> Any:
    return json.loads(
        value,
        object_pairs_hook=_strict_json_object,
        parse_constant=_reject_json_constant,
    )


class _AssuranceRunner:
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
        review_evidence_path: Path | None = None,
        expected_mode: str | None = None,
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
        self.review_evidence_path = review_evidence_path
        self.expected_mode = expected_mode
        self.now = now or utc_now()

    def run(self) -> dict[str, Any]:
        if self.policy.get("schema_version") != SCHEMA_VERSION:
            raise AssuranceError("dependency policy schema version mismatch")
        rollout_mode = self.policy.get("rollout_mode")
        if rollout_mode not in {"warn", "enforce"}:
            raise AssuranceError("Change Assurance has an unsupported rollout mode")
        if self.expected_mode is not None and rollout_mode != self.expected_mode:
            raise AssuranceError(
                "trusted workflow mode does not match the protected policy rollout mode"
            )
        base = self.repo.commit(self.base_sha)
        head = self.repo.commit(self.head_sha)
        merge_base = self.repo.merge_base(base, head)
        changes = self.repo.changes(merge_base, head)
        if not changes:
            raise AssuranceError("the trusted Git diff is empty")
        diff_digest = digest_bytes(self.repo.diff_bytes(merge_base, head))
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
                "rollout_mode": rollout_mode,
                "decision_state": self.policy["policy_decision_state"],
            },
        }
        validate_manifest(manifest)

        protected = derive_protected_evidence(
            self.repo, merge_base, head, changes, self.policy
        )
        review_evidence = load_review_evidence(
            self.review_evidence_path,
            repository=self.repository,
            expected_head_sha=head,
        )
        protected["human_review"] = review_evidence
        dependency = derive_dependency_evidence(
            self.repo,
            merge_base,
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
        review_verified = review_evidence["status"] == "approved"
        review_required = protected_changed or sensitive
        controls.append(
            {
                "control_id": "CH-06",
                "status": "unassured" if review_required and not review_verified else "pass",
                "reason_code": (
                    "PROTECTED_ORACLE_REVIEW_VERIFIED"
                    if review_required and review_verified
                    else (
                        "PROTECTED_ORACLE_REVIEW_REQUIRED"
                        if review_required
                        else "PROTECTED_ORACLE_UNCHANGED"
                    )
                ),
                "reason": (
                    "A separate human approved the exact protected-change subject."
                    if review_required and review_verified
                    else (
                        "Tests, workflows, assurance policy, or Agent-owned paths changed; "
                        "separate human ownership review is required."
                        if review_required
                        else "No protected oracle or ownership path changed."
                    )
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
        evidence_by_control = {
            "CH-04": dependency,
            "CH-06": protected,
            "CH-11": subject_evidence,
            "CH-12": integrity,
        }
        for control in controls:
            explanation = explain_control(
                control["control_id"],
                status=control["status"],
                default_reason=control["reason"],
                evidence=evidence_by_control.get(control["control_id"]),
            )
            if explanation is not None:
                control["explanation"] = explanation
        evidence_complete = integrity_complete and all(writer.verify_ref(item["evidence"]) for item in controls)
        statuses = {item["status"] for item in controls}
        if "fail" in statuses:
            decision = "deny"
        elif not evidence_complete or statuses & {"error", "unassured"}:
            decision = "unassured"
        else:
            decision = "allow_candidate"

        run_id = "CA-" + hashlib.sha256(
            f"{self.repository}\0{base}\0{head}\0{diff_digest}\0{self.policy_digest}\0"
            f"{canonical_digest(_review_decision_identity(review_evidence))}".encode("utf-8")
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
            "enforcement": {
                "mode": rollout_mode,
                "blocks_merge": rollout_mode == "enforce",
            },
        }
        validate_report(report)
        writer.write_json("assurance-report.json", report)
        writer.finalize()
        return report


def load_policy(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    try:
        policy = _strict_json_loads(raw)
    except (json.JSONDecodeError, SchemaValidationError) as exc:
        raise AssuranceError(f"dependency policy is invalid JSON: {_safe_detail(str(exc))}") from exc
    try:
        schema = _strict_json_loads(POLICY_SCHEMA.read_text(encoding="utf-8"))
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
    review_evidence_path: Path | None = None,
    expected_mode: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    policy, policy_digest = load_policy(policy_path)
    try:
        policy_uri = policy_path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        policy_uri = policy_path.name
    runner = _AssuranceRunner(
        repo_root=repo_root,
        base_sha=base_sha,
        head_sha=head_sha,
        repository=repository,
        output=output,
        policy=policy,
        policy_uri=policy_uri,
        policy_digest=policy_digest,
        environment=environment or InstalledDependencyEnvironment(),
        review_evidence_path=review_evidence_path,
        expected_mode=expected_mode,
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
    enforcement_mode: str = "warn",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Best-effort rollout-aware receipt for detector failure; never claims PASS."""
    if enforcement_mode not in {"warn", "enforce"}:
        raise AssuranceError("unsupported error receipt enforcement mode")
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
                "explanation": explain_control(
                    "CH-01",
                    status="error",
                    default_reason="Trusted evidence derivation failed; no passing decision is possible.",
                ),
            }
        ],
        "evidence_complete": False,
        "assurance_decision": "unassured",
        "business_outcome": {"decision": "not_evaluated", "changed_by_assurance": False},
        "enforcement": {
            "mode": enforcement_mode,
            "blocks_merge": enforcement_mode == "enforce",
        },
    }
    validate_report(report)
    writer.write_json("assurance-report.json", report)
    writer.finalize()
    return report


def markdown_summary(report: dict[str, Any]) -> str:
    mode = report["enforcement"]["mode"]
    blocks_merge = report["enforcement"]["blocks_merge"]
    rows = [
        f"### Change Assurance ({mode.title()})",
        "",
        f"- 보증 판정: `{report['assurance_decision']}`",
        f"- 병합 차단: `{str(blocks_merge).lower()}`",
        f"- 판정 대상 커밋: `{report['subject']['head_sha']}`",
        f"- 증거 완전성: `{str(report['evidence_complete']).lower()}`",
        "",
        "| 통제 | 상태 | 사유 코드 |",
        "|---|---|---|",
    ]
    for control in report["controls"]:
        rows.append(
            f"| {control['control_id']} | `{control['status']}` | {control['reason_code']} |"
        )
    rows.append("")
    if mode == "warn":
        rows.append("> Warn은 비통과 판정을 경고하지만 PR 병합을 차단하지 않습니다.")
    else:
        rows.append("> Enforce는 `allow_candidate`가 아닌 판정의 PR 병합을 차단합니다.")
    non_passing = [
        control
        for control in report["controls"]
        if control["status"] not in {"pass", "not_applicable"}
    ]
    if non_passing:
        rows.extend(["", "#### 경고 상세"])
        for control in non_passing:
            explanation = control.get("explanation") or {}
            rows.extend(
                [
                    "",
                    f"**{control['control_id']} · {control['reason_code']}**",
                    f"- 발견 내용: {explanation.get('finding', control['reason'])}",
                    f"- 보증 영향: {explanation.get('impact', '전체 변경을 보증할 수 없습니다.')}",
                    f"- 확인/조치: {explanation.get('action', '원본 증거를 확인하세요.')}",
                    f"- 증거: `{control['evidence']['uri']}`",
                ]
            )
    return "\n".join(rows) + "\n"
