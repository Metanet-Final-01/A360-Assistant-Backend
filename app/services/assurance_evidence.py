"""Append-only evidence receipts for Backend-owned assurance decisions."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import models
from app.core.masking import mask_pii

logger = logging.getLogger(__name__)

RECEIPT_SCHEMA_VERSION = "1.0"
ALLOWED_OUTPUT_DECISIONS = frozenset(("allow_candidate", "deny", "unassured"))
OUTPUT_EXPECTED_COMMON = (
    "source_observation_id",
    "candidate_id",
    "payload_digest",
    "request_id",
    "session_id",
    "validator_version",
    "policy_digest",
    "catalog_digest",
    "business_outcome",
)
OUTPUT_EXPECTED_AGENT = (
    "resolved_agent_version",
    "agent_registry_digest",
    "public_contract_version",
)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _safe_text(value: Any, *, limit: int = 512) -> str | None:
    if value is None:
        return None
    try:
        return mask_pii(str(value))[:limit]
    except Exception:
        return "[REDACTED]"


def _sanitize_findings(findings: Any) -> list[dict[str, str | None]]:
    if not isinstance(findings, list):
        return []
    result = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        result.append({
            "control": _safe_text(finding.get("control"), limit=80),
            "code": _safe_text(finding.get("code"), limit=80),
            "path": _safe_text(finding.get("path")),
            "message": _safe_text(finding.get("message")),
        })
    return result


def _observation_integrity(observation: dict[str, Any]) -> bool:
    claimed = observation.get("observation_id")
    if not isinstance(claimed, str):
        return False
    body = deepcopy(observation)
    body.pop("observation_id", None)
    try:
        return digest(body) == claimed
    except (TypeError, ValueError):
        return False


def _output_evidence_values(observation: dict[str, Any]) -> dict[str, Any]:
    provenance = observation.get("agent_provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    outcome = observation.get("business_outcome")
    outcome = outcome if isinstance(outcome, dict) else {}
    return {
        "source_observation_id": observation.get("observation_id"),
        "candidate_id": observation.get("candidate_id"),
        "payload_digest": observation.get("payload_digest"),
        "request_id": observation.get("request_id"),
        "session_id": observation.get("session_id"),
        "validator_version": observation.get("validator_version"),
        "policy_digest": observation.get("policy_digest"),
        "catalog_digest": observation.get("catalog_digest"),
        "business_outcome": outcome.get("persisted") if "persisted" in outcome else None,
        "resolved_agent_version": provenance.get("resolved_agent_version"),
        "agent_registry_digest": provenance.get("agent_registry_digest"),
        "public_contract_version": provenance.get("public_contract_version"),
    }


def build_output_receipt(
    observation: dict[str, Any], *, recommendation_id: uuid.UUID, recommendation_version: int,
) -> tuple[models.AssuranceReceipt, dict[str, Any]]:
    """Create a PII-minimized receipt and its response summary without writing it."""
    values = _output_evidence_values(observation)
    try:
        session_id = uuid.UUID(str(values["session_id"])) if values["session_id"] else None
    except ValueError:
        session_id = None
        values["session_id"] = None
    source = observation.get("source")
    expected = list(OUTPUT_EXPECTED_COMMON)
    if source != "drag":
        expected.extend(OUTPUT_EXPECTED_AGENT)
    missing = sorted(key for key in expected if values.get(key) is None)
    evidence_valid = _observation_integrity(observation)
    raw_decision = observation.get("decision")
    decision = (
        raw_decision
        if isinstance(raw_decision, str) and raw_decision in ALLOWED_OUTPUT_DECISIONS
        else "unassured"
    )
    if not evidence_valid or missing or decision == "unassured":
        verdict = "refused"
    elif decision == "deny":
        verdict = "deny"
    else:
        verdict = "observed"

    enforcement = observation.get("enforcement")
    enforcement = enforcement if isinstance(enforcement, dict) else {}
    mode = str(observation.get("rollout_mode") or "observe")
    if enforcement.get("blocks_persistence"):
        effect = "blocked"
    elif mode == "warn":
        effect = "warned"
    else:
        effect = "none"
    provenance = observation.get("agent_provenance")
    provenance = provenance if isinstance(provenance, dict) else {}

    receipt_payload = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "harness": "output",
        "record_kind": "output_observation",
        "writer_authority": "backend_boundary_observer",
        "source": str(source) if source is not None else None,
        "source_observation_id": values["source_observation_id"],
        "subject": {
            "recommendation_id": str(recommendation_id),
            "recommendation_version": recommendation_version,
            "session_id": values["session_id"],
            "request_id": values["request_id"],
            "candidate_id": values["candidate_id"],
            "payload_digest": values["payload_digest"],
        },
        "decision": decision,
        "assurance_verdict": verdict,
        "assurance_status": str(observation.get("assurance_status") or "unassured_observe"),
        "evidence_valid": evidence_valid,
        "completeness": {
            "status": "incomplete" if missing else "complete",
            "expected": expected,
            "observed": sorted(key for key in expected if key not in missing),
            "missing": missing,
        },
        "controls": deepcopy(observation.get("controls"))
        if isinstance(observation.get("controls"), list) else [],
        "boundary_findings": _sanitize_findings(observation.get("boundary_findings")),
        "boundary_finding_count": observation.get("boundary_finding_count", 0),
        "boundary_findings_truncated": bool(observation.get("boundary_findings_truncated")),
        "provenance": {
            "validator_version": values["validator_version"],
            "policy_digest": values["policy_digest"],
            "catalog_digest": values["catalog_digest"],
            "requested_agent_version": provenance.get("requested_agent_version"),
            "resolved_agent_version": values["resolved_agent_version"],
            "agent_registry_digest": values["agent_registry_digest"],
            "public_contract_version": values["public_contract_version"],
        },
        "enforcement": {"mode": mode, "effect": effect},
        "business_outcome": {"persisted": values["business_outcome"]},
    }
    receipt_digest = digest(receipt_payload)

    row = models.AssuranceReceipt(
        receipt_digest=receipt_digest,
        schema_version=RECEIPT_SCHEMA_VERSION,
        harness="output",
        record_kind="output_observation",
        writer_authority="backend_boundary_observer",
        source=str(source) if source is not None else None,
        request_id=values["request_id"],
        session_id=session_id,
        recommendation_id=recommendation_id,
        recommendation_version=recommendation_version,
        candidate_id=values["candidate_id"],
        payload_digest=values["payload_digest"],
        source_observation_id=values["source_observation_id"],
        evidence_valid=evidence_valid,
        completeness_status="incomplete" if missing else "complete",
        decision=decision,
        assurance_verdict=verdict,
        assurance_status=receipt_payload["assurance_status"],
        rollout_mode=mode,
        enforcement_effect=effect,
        business_persisted=values["business_outcome"],
        validator_version=values["validator_version"],
        policy_digest=values["policy_digest"],
        catalog_digest=values["catalog_digest"],
        requested_agent_version=provenance.get("requested_agent_version"),
        resolved_agent_version=values["resolved_agent_version"],
        receipt_payload=receipt_payload,
    )
    summary = {
        "status": "pending",
        "receipt_digest": receipt_digest,
        "evidence_valid": evidence_valid,
        "completeness_status": row.completeness_status,
        "assurance_verdict": verdict,
        "missing_evidence": missing,
    }
    return row, summary


def persist_output_receipt(
    observation: dict[str, Any], *, recommendation_id: uuid.UUID, recommendation_version: int,
) -> dict[str, Any]:
    """Persist one Output receipt; failure is explicit but does not change Observe business writes."""
    row, summary = build_output_receipt(
        observation,
        recommendation_id=recommendation_id,
        recommendation_version=recommendation_version,
    )
    from app import db as app_db

    try:
        with app_db.SessionLocal() as session:
            session.add(row)
            session.commit()
    except IntegrityError:
        # A response retry may replay the exact content-addressed receipt. Treat only an exact digest
        # already present as idempotent; any other integrity failure remains refused.
        try:
            with app_db.SessionLocal() as session:
                existing = session.execute(
                    select(models.AssuranceReceipt).where(
                        models.AssuranceReceipt.receipt_digest == row.receipt_digest
                    )
                ).scalar_one_or_none()
            if existing is not None:
                return {**summary, "status": "persisted", "idempotent": True}
        except Exception:
            pass
        logger.warning("보증 영수증 무결성 저장 실패", exc_info=True)
        return {**summary, "status": "refused", "error_type": "IntegrityError"}
    except Exception as exc:
        logger.warning("보증 영수증 저장 실패", exc_info=True)
        return {**summary, "status": "refused", "error_type": type(exc).__name__}
    return {**summary, "status": "persisted", "idempotent": False}


def receipt_integrity(row: models.AssuranceReceipt) -> bool:
    """Recompute content integrity and verify indexed columns still match the stored payload."""
    payload = row.receipt_payload
    if not isinstance(payload, dict):
        return False
    try:
        matches_digest = digest(payload) == row.receipt_digest
    except (TypeError, ValueError):
        return False
    if not matches_digest:
        return False
    subject = payload.get("subject") if isinstance(payload.get("subject"), dict) else {}
    provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
    completeness = payload.get("completeness") if isinstance(payload.get("completeness"), dict) else {}
    enforcement = payload.get("enforcement") if isinstance(payload.get("enforcement"), dict) else {}
    business_outcome = (
        payload.get("business_outcome") if isinstance(payload.get("business_outcome"), dict) else {}
    )
    return all((
        payload.get("schema_version") == row.schema_version,
        payload.get("harness") == row.harness,
        payload.get("record_kind") == row.record_kind,
        payload.get("writer_authority") == row.writer_authority,
        payload.get("source") == row.source,
        payload.get("source_observation_id") == row.source_observation_id,
        payload.get("decision") == row.decision,
        payload.get("assurance_verdict") == row.assurance_verdict,
        payload.get("assurance_status") == row.assurance_status,
        payload.get("evidence_valid") == row.evidence_valid,
        subject.get("recommendation_id")
        == (str(row.recommendation_id) if row.recommendation_id else None),
        subject.get("recommendation_version") == row.recommendation_version,
        subject.get("session_id") == (str(row.session_id) if row.session_id else None),
        subject.get("request_id") == row.request_id,
        subject.get("candidate_id") == row.candidate_id,
        subject.get("payload_digest") == row.payload_digest,
        completeness.get("status") == row.completeness_status,
        provenance.get("validator_version") == row.validator_version,
        provenance.get("policy_digest") == row.policy_digest,
        provenance.get("catalog_digest") == row.catalog_digest,
        provenance.get("requested_agent_version") == row.requested_agent_version,
        provenance.get("resolved_agent_version") == row.resolved_agent_version,
        enforcement.get("mode") == row.rollout_mode,
        enforcement.get("effect") == row.enforcement_effect,
        business_outcome.get("persisted") == row.business_persisted,
    ))
