"""Real PostgreSQL verification for protected Change Assurance records (RPA-207)."""

from sqlalchemy import func, select

from app import models
from app.services.assurance_evidence import persist_change_receipt, receipt_integrity
from tests.test_change_assurance_receipts import _envelope


def test_change_record_round_trip_and_retry_are_idempotent(db_session, tmp_path):
    envelope = _envelope(tmp_path)

    first = persist_change_receipt(envelope, db_session)
    second = persist_change_receipt(envelope, db_session)

    assert first["status"] == "persisted"
    assert first["idempotent"] is False
    assert second["status"] == "persisted"
    assert second["idempotent"] is True
    assert db_session.scalar(select(func.count()).select_from(models.AssuranceReceipt)) == 1
    stored = db_session.scalar(select(models.AssuranceReceipt))
    assert stored is not None
    assert stored.harness == "change"
    assert stored.receipt_payload["subject"]["pull_request_number"] == 281
    assert receipt_integrity(stored) is True
