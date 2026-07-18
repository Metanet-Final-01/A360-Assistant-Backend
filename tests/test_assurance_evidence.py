"""RPA-182 append-only assurance receipt contract tests."""

import uuid
from copy import deepcopy

from app.services.assurance_evidence import (
    build_output_receipt,
    persist_output_receipt,
    receipt_integrity,
)
from app.services.output_assurance import (
    OutputBoundaryContext,
    finalize_persistence_observation,
    observe_recommendation_candidate,
)


class FixtureCatalog:
    def iter_action_schemas(self):
        yield {"package": "Excel_MS", "action": "GoToCell", "parameters": []}


def _payload(package="Excel_MS", action="GoToCell"):
    return {
        "schema_version": "1.0",
        "steps": [{
            "step_id": "step-1",
            "actions": [{
                "order": 1,
                "package": package,
                "action": action,
                "parameters": [],
                "children": [],
            }],
        }],
        "variables": [],
    }


def _observation(*, source="drag", resolved="v3", package="Excel_MS", action="GoToCell"):
    sid = uuid.uuid4()
    observed = observe_recommendation_candidate(
        _payload(package, action),
        OutputBoundaryContext(
            session_id=str(sid),
            request_id="request-1",
            source=source,
            requested_agent_version="v3" if source != "drag" else None,
            resolved_agent_version=resolved if source != "drag" else None,
            agent_registry_snapshot={"versions": [{"id": "v3"}], "default": "v3"},
        ),
        catalog=FixtureCatalog(),
    )
    return finalize_persistence_observation(observed, persisted=True)


def test_drag_receipt_is_complete_and_content_addressed():
    row, summary = build_output_receipt(
        _observation(), recommendation_id=uuid.uuid4(), recommendation_version=2
    )

    assert summary == {
        "status": "pending",
        "receipt_digest": row.receipt_digest,
        "evidence_valid": True,
        "completeness_status": "complete",
        "assurance_verdict": "observed",
        "missing_evidence": [],
    }
    assert receipt_integrity(row) is True
    assert "steps" not in row.receipt_payload


def test_valid_deny_receipt_masks_finding_text_without_losing_decision():
    observation = _observation(
        package="user@example.com", action="InventedAction1234567"
    )
    row, summary = build_output_receipt(
        observation, recommendation_id=uuid.uuid4(), recommendation_version=1
    )

    assert summary["evidence_valid"] is True
    assert summary["assurance_verdict"] == "deny"
    serialized = str(row.receipt_payload)
    assert "user@example.com" not in serialized
    assert "[EMAIL]" in serialized
    assert "1234567" not in serialized


def test_missing_resolved_agent_version_is_refused_not_guessed():
    row, summary = build_output_receipt(
        _observation(source="agent", resolved=None),
        recommendation_id=uuid.uuid4(),
        recommendation_version=1,
    )

    assert row.evidence_valid is True
    assert summary["assurance_verdict"] == "refused"
    assert "resolved_agent_version" in summary["missing_evidence"]
    assert row.resolved_agent_version is None


def test_tampered_observation_cannot_produce_valid_evidence():
    observation = _observation()
    observation["decision"] = "allow_candidate" if observation["decision"] != "allow_candidate" else "deny"

    row, summary = build_output_receipt(
        observation, recommendation_id=uuid.uuid4(), recommendation_version=1
    )

    assert row.evidence_valid is False
    assert summary["assurance_verdict"] == "refused"


def test_receipt_integrity_detects_payload_tampering():
    row, _ = build_output_receipt(
        _observation(), recommendation_id=uuid.uuid4(), recommendation_version=1
    )
    row.receipt_payload = deepcopy(row.receipt_payload)
    row.receipt_payload["decision"] = "deny"

    assert receipt_integrity(row) is False


def test_persistence_failure_is_explicit_refused_and_does_not_raise(monkeypatch):
    class BrokenSession:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def add(self, row): pass
        def commit(self): raise RuntimeError("database unavailable")

    monkeypatch.setattr("app.db.SessionLocal", BrokenSession)
    result = persist_output_receipt(
        _observation(), recommendation_id=uuid.uuid4(), recommendation_version=1
    )

    assert result["status"] == "refused"
    assert result["error_type"] == "RuntimeError"
