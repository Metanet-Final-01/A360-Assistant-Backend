"""Backend-owned validation for recommendation candidates at the persistence boundary.

This module consumes only the public recommendation payload and the Backend catalog service.
It deliberately does not import or reuse any Agent checker. RPA-181 runs in Observe mode: the
decision is returned and logged, while the existing business persistence outcome is unchanged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.schemas.recommendation import (
    ActionParameter,
    BotVariable,
    CardTarget,
    FlowSpec,
    QuestionCard,
    RagSource,
    Recommendation,
    RecommendedAction,
    SpecRequirement,
    SpecUnknown,
    StepRecommendation,
    VarRef,
)
from app.services.catalog import get_backend_catalog

SCHEMA_VERSION = "1.0"
VALIDATOR_VERSION = "output-boundary-observe-v1"
PUBLIC_CONTRACT_VERSION = "turn.done.v1"
MAX_FINDINGS = 100
MAX_CATALOG_NAME = 200
OUTPUT_POLICY = {
    "rollout_mode": "observe",
    "controls": ["strict_schema", "catalog_closure"],
    "fail_decision": "deny",
    "detector_error_decision": "unassured",
    "blocks_persistence": False,
}

_MODEL_FIELDS = {
    "recommendation": set(Recommendation.model_fields),
    "step": set(StepRecommendation.model_fields),
    "action": set(RecommendedAction.model_fields),
    "parameter": set(ActionParameter.model_fields),
    "source": set(RagSource.model_fields),
    "variable": set(BotVariable.model_fields),
    "var_ref": set(VarRef.model_fields),
    "question": set(QuestionCard.model_fields),
    "target": set(CardTarget.model_fields),
    "spec": set(FlowSpec.model_fields),
    "requirement": set(SpecRequirement.model_fields),
    "unknown": set(SpecUnknown.model_fields),
}

# These fields are a documented transport extension used by the flow editor. They are preserved
# in JSONB but do not participate in the executable recommendation contract.
_UI_FIELDS = {
    "step": {"x", "y", "collapsed"},
    "action": {"_uiKey", "x", "y", "collapsed"},
}


@dataclass(frozen=True)
class OutputBoundaryContext:
    session_id: str
    request_id: str | None
    source: str
    requested_agent_version: str | None = None
    resolved_agent_version: str | None = None
    agent_registry_snapshot: Any = None
    public_contract_version: str = PUBLIC_CONTRACT_VERSION
    producer_advisory: Any = None


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


OUTPUT_POLICY_DIGEST = _digest(OUTPUT_POLICY)


def _optional_digest(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    try:
        return _digest(value), None
    except (TypeError, ValueError) as exc:
        return None, type(exc).__name__


def _finding(control: str, code: str, path: str, message: str) -> dict[str, str]:
    return {"control": control, "code": code, "path": path, "message": message}


def _list_items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _unknown_field_findings(payload: Any) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []

    def check(obj: Any, kind: str, path: str) -> None:
        if not isinstance(obj, dict):
            return
        allowed = _MODEL_FIELDS[kind] | _UI_FIELDS.get(kind, set())
        for key in sorted(set(obj) - allowed):
            findings.append(_finding("strict_schema", "UNKNOWN_FIELD", f"{path}.{key}", "허용되지 않은 필드"))

    check(payload, "recommendation", "recommendation")
    root = payload if isinstance(payload, dict) else {}
    for si, step in enumerate(_list_items(root.get("steps"))):
        sp = f"recommendation.steps[{si}]"
        check(step, "step", sp)
        if not isinstance(step, dict):
            continue
        for _ in _walk_actions(step.get("actions", []), f"{sp}.actions", check):
            pass
    for vi, variable in enumerate(_list_items(root.get("variables"))):
        check(variable, "variable", f"recommendation.variables[{vi}]")
    for qi, question in enumerate(_list_items(root.get("needs_input"))):
        qp = f"recommendation.needs_input[{qi}]"
        check(question, "question", qp)
        if not isinstance(question, dict):
            continue
        for ti, target in enumerate(_list_items(question.get("targets"))):
            check(target, "target", f"{qp}.targets[{ti}]")
    spec = root.get("spec")
    if isinstance(spec, dict):
        check(spec, "spec", "recommendation.spec")
        for ri, requirement in enumerate(_list_items(spec.get("requirements"))):
            check(requirement, "requirement", f"recommendation.spec.requirements[{ri}]")
        for ui, unknown in enumerate(_list_items(spec.get("unknowns"))):
            check(unknown, "unknown", f"recommendation.spec.unknowns[{ui}]")
    return findings


def _walk_actions(actions: Any, path: str, check=None):
    if not isinstance(actions, list):
        return
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            continue
        action_path = f"{path}[{index}]"
        if check is not None:
            check(action, "action", action_path)
            for pi, parameter in enumerate(_list_items(action.get("parameters"))):
                check(parameter, "parameter", f"{action_path}.parameters[{pi}]")
            for si, source in enumerate(_list_items(action.get("sources"))):
                check(source, "source", f"{action_path}.sources[{si}]")
            for field in ("produces", "consumes"):
                for vi, ref in enumerate(_list_items(action.get(field))):
                    check(ref, "var_ref", f"{action_path}.{field}[{vi}]")
        yield action_path, action
        yield from _walk_actions(action.get("children", []), f"{action_path}.children", check)


def _strict_schema(payload: Any) -> tuple[str, list[dict[str, str]], int]:
    findings: list[dict[str, str]] = []
    validation_error_count = 0
    try:
        Recommendation.model_validate(payload, strict=True)
    except ValidationError as exc:
        validation_error_count = exc.error_count()
        for error in exc.errors()[:50]:
            path = "recommendation" + "".join(
                f"[{part}]" if isinstance(part, int) else f".{part}" for part in error["loc"]
            )
            findings.append(_finding("strict_schema", "SCHEMA_INVALID", path, error["msg"]))
    unknown_findings = _unknown_field_findings(payload)
    findings.extend(unknown_findings)
    total_count = validation_error_count + len(unknown_findings)
    return ("fail" if total_count else "pass"), findings, total_count


def _catalog_closure(payload: Any, catalog) -> tuple[str, str, list[dict[str, str]]]:
    snapshot = sorted(
        list(catalog.iter_action_schemas()),
        key=lambda item: (str(item.get("package", "")), str(item.get("action", ""))),
    )
    catalog_digest = _digest(snapshot)
    valid_actions = {
        (item.get("package"), item.get("action"))
        for item in snapshot
        if isinstance(item.get("package"), str) and isinstance(item.get("action"), str)
    }
    findings = []
    root = payload if isinstance(payload, dict) else {}
    for si, step in enumerate(_list_items(root.get("steps"))):
        if not isinstance(step, dict):
            continue
        for path, action in _walk_actions(step.get("actions", []), f"recommendation.steps[{si}].actions"):
            package, name = action.get("package"), action.get("action")
            if not isinstance(package, str) or not isinstance(name, str):
                continue  # strict_schema owns structural failures
            if (package, name) not in valid_actions:
                display_package = package[:MAX_CATALOG_NAME]
                display_name = name[:MAX_CATALOG_NAME]
                findings.append(
                    _finding(
                        "catalog_closure", "UNKNOWN_CATALOG_ACTION", path,
                        f"카탈로그에 없는 액션: {display_package}/{display_name}",
                    )
                )
    return ("fail" if findings else "pass"), catalog_digest, findings


def observe_recommendation_candidate(
    payload: dict,
    context: OutputBoundaryContext,
    *,
    catalog=None,
) -> dict[str, Any]:
    """Return an Observe-only boundary decision without changing the business outcome."""
    payload_digest = _digest(payload)
    candidate_id = _digest({
        "session_id": context.session_id,
        "request_id": context.request_id,
        "source": context.source,
        "payload_digest": payload_digest,
    })
    controls: list[dict[str, Any]] = []
    findings: list[dict[str, str]] = []
    finding_count = 0
    catalog_digest = None

    try:
        status, schema_findings, schema_finding_count = _strict_schema(payload)
        controls.append({"control_id": "strict_schema", "status": status})
        findings.extend(schema_findings)
        finding_count += schema_finding_count
    except Exception as exc:  # detector failure is evidence, never an allow
        controls.append({"control_id": "strict_schema", "status": "error", "error_type": type(exc).__name__})

    try:
        status, catalog_digest, catalog_findings = _catalog_closure(
            payload, catalog if catalog is not None else get_backend_catalog()
        )
        controls.append({"control_id": "catalog_closure", "status": status})
        findings.extend(catalog_findings)
        finding_count += len(catalog_findings)
    except Exception as exc:  # infrastructure/catalog absence is unassured, not pass
        controls.append({"control_id": "catalog_closure", "status": "error", "error_type": type(exc).__name__})

    statuses = {item["status"] for item in controls}
    if "fail" in statuses:
        decision = "deny"
    elif "error" in statuses or len(controls) != 2:
        decision = "unassured"
    else:
        decision = "allow_candidate"

    if context.source == "drag":
        version_observability = "not_applicable"
    elif context.resolved_agent_version:
        version_observability = "observed_from_public_contract"
    else:
        version_observability = "not_observable"
    advisory_digest, advisory_digest_error = _optional_digest(context.producer_advisory)
    registry_digest, registry_digest_error = _optional_digest(context.agent_registry_snapshot)
    producer_advisory = {
        "present": context.producer_advisory is not None,
        "raw_digest": advisory_digest,
        "digest_error": advisory_digest_error,
    }
    displayed_findings = findings[:MAX_FINDINGS]
    observation = {
        "schema_version": SCHEMA_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "policy_digest": OUTPUT_POLICY_DIGEST,
        "rollout_mode": "observe",
        "decision": decision,
        "assurance_status": "unassured_observe",
        "validated": False,
        "candidate_id": candidate_id,
        "payload_digest": payload_digest,
        "request_id": context.request_id,
        "session_id": context.session_id,
        "source": context.source,
        "controls": controls,
        "boundary_findings": displayed_findings,
        "boundary_finding_count": finding_count,
        "boundary_findings_truncated": finding_count > len(displayed_findings),
        "producer_advisory": producer_advisory,
        "catalog_digest": catalog_digest,
        "agent_provenance": {
            "requested_agent_version": context.requested_agent_version,
            "resolved_agent_version": context.resolved_agent_version,
            "resolved_version_observability": version_observability,
            "agent_registry_digest": registry_digest,
            "agent_registry_digest_error": registry_digest_error,
            "public_contract_version": context.public_contract_version,
        },
        "enforcement": {"mode": "observe", "blocks_persistence": False},
        "business_outcome": {"persisted": None},
    }
    observation["observation_id"] = _digest(observation)
    return observation


def finalize_persistence_observation(
    observation: dict[str, Any], *, persisted: bool, error_type: str | None = None,
) -> dict[str, Any]:
    """Bind the final database outcome and derive a new content-addressed observation ID."""
    outcome: dict[str, Any] = {"persisted": persisted}
    if error_type is not None:
        outcome["error_type"] = error_type
    finalized = {**observation, "business_outcome": outcome}
    finalized.pop("observation_id", None)
    finalized["observation_id"] = _digest(finalized)
    return finalized


def build_unassured_observation(
    context: OutputBoundaryContext, *, error_type: str,
) -> dict[str, Any]:
    """Build safe evidence when the Observe detector itself cannot complete."""
    registry_digest, registry_digest_error = _optional_digest(context.agent_registry_snapshot)
    observation = {
        "schema_version": SCHEMA_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "policy_digest": OUTPUT_POLICY_DIGEST,
        "rollout_mode": "observe",
        "decision": "unassured",
        "assurance_status": "unassured_observe",
        "validated": False,
        "candidate_id": None,
        "payload_digest": None,
        "request_id": context.request_id,
        "session_id": context.session_id,
        "source": context.source,
        "controls": [{
            "control_id": "output_boundary",
            "status": "error",
            "error_type": error_type,
        }],
        "boundary_findings": [],
        "boundary_finding_count": 0,
        "boundary_findings_truncated": False,
        "producer_advisory": {
            "present": context.producer_advisory is not None,
            "raw_digest": None,
            "digest_error": "observation_unavailable",
        },
        "catalog_digest": None,
        "agent_provenance": {
            "requested_agent_version": context.requested_agent_version,
            "resolved_agent_version": context.resolved_agent_version,
            "resolved_version_observability": "not_observable",
            "agent_registry_digest": registry_digest,
            "agent_registry_digest_error": registry_digest_error,
            "public_contract_version": context.public_contract_version,
        },
        "enforcement": {"mode": "observe", "blocks_persistence": False},
        "business_outcome": {"persisted": None},
    }
    observation["observation_id"] = _digest(observation)
    return observation
