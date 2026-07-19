"""RPA-207 protected Change receipt transport and persistence contract tests."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

import app.api.assurance_writer as writer_api
from app.api.admin import _assurance_receipt_out
from app.db import get_db
from app.main import app
from app.services.assurance_evidence import (
    build_change_receipt,
    persist_change_receipt,
    receipt_integrity,
)
from assurance.change.foundation import AssuranceError
from assurance.change.transport import load_change_envelope, validate_change_envelope
from scripts.publish_change_assurance import publish, source_from_event, writer_url
from tests.test_change_assurance import _load_scenarios, _run_scenario


ROOT = Path(__file__).resolve().parents[1]


def _envelope(tmp_path, scenario_name: str = "good_import"):
    scenario = _load_scenarios()[scenario_name]
    report, output = _run_scenario(tmp_path, scenario)
    source = {
        "repository": report["subject"]["repository"],
        "workflow_name": "Change Assurance (Observe)",
        "workflow_run_id": 12345,
        "run_attempt": 1,
        "event": "pull_request",
        "conclusion": "success",
        "head_sha": report["subject"]["head_sha"],
        "pull_request_number": 281,
    }
    return load_change_envelope(output, source=source)


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides.clear()


def test_valid_change_artifacts_build_content_addressed_receipt(tmp_path):
    envelope = _envelope(tmp_path)
    row, summary = build_change_receipt(envelope)

    assert row.harness == "change"
    assert row.writer_authority == "github_actions_workflow_run"
    assert row.decision == "allow_candidate"
    assert summary["assurance_verdict"] == "observed"
    assert summary["completeness_status"] == "complete"
    assert receipt_integrity(row) is True
    serialized = str(row.receipt_payload)
    assert "installed_distributions" not in serialized
    assert "changes" not in serialized


def test_valid_unassured_report_is_refused_not_promoted(tmp_path):
    scenario = _load_scenarios()["good_import"]
    scenario["snapshot"] = {}
    report, output = _run_scenario(tmp_path, scenario)
    envelope = load_change_envelope(output, source={
        "repository": report["subject"]["repository"],
        "workflow_name": "Change Assurance (Observe)",
        "workflow_run_id": 12346,
        "run_attempt": 1,
        "event": "pull_request",
        "conclusion": "success",
        "head_sha": report["subject"]["head_sha"],
        "pull_request_number": 282,
    })

    row, summary = build_change_receipt(envelope)

    assert row.decision == "unassured"
    assert summary["assurance_verdict"] == "refused"
    assert receipt_integrity(row) is True


def test_valid_deny_report_is_preserved_as_deny(tmp_path):
    row, summary = build_change_receipt(_envelope(tmp_path, "fake_dependency"))

    assert row.decision == "deny"
    assert summary["assurance_verdict"] == "deny"
    assert receipt_integrity(row) is True


def test_tampered_report_cannot_be_relabelled_allow(tmp_path):
    envelope = _envelope(tmp_path)
    tampered = deepcopy(envelope)
    tampered["artifacts"]["assurance-report.json"]["controls"][0]["status"] = "fail"

    with pytest.raises(AssuranceError):
        validate_change_envelope(tampered)


def test_workflow_source_must_match_report_head(tmp_path):
    envelope = _envelope(tmp_path)
    envelope["source"]["head_sha"] = "f" * 40

    with pytest.raises(AssuranceError):
        validate_change_envelope(envelope)


def test_checksum_manifest_tampering_is_rejected(tmp_path):
    scenario = _load_scenarios()["good_import"]
    report, output = _run_scenario(tmp_path, scenario)
    (output / "SHA256SUMS").write_text("0" * 64 + "  assurance-report.json\n", encoding="ascii")

    with pytest.raises(AssuranceError):
        load_change_envelope(output, source={
            "repository": report["subject"]["repository"],
            "workflow_name": "Change Assurance (Observe)",
            "workflow_run_id": 12345,
            "run_attempt": 1,
            "event": "pull_request",
            "conclusion": "success",
            "head_sha": report["subject"]["head_sha"],
            "pull_request_number": 281,
        })


def test_receipt_integrity_detects_change_subject_tampering(tmp_path):
    row, _ = build_change_receipt(_envelope(tmp_path))
    row.receipt_payload = deepcopy(row.receipt_payload)
    row.receipt_payload["subject"]["head_sha"] = "0" * 40

    assert receipt_integrity(row) is False


def test_change_receipt_is_visible_through_existing_admin_contract(tmp_path):
    row, _ = build_change_receipt(_envelope(tmp_path))
    response = _assurance_receipt_out(row, detail=True)

    assert response["harness"] == "change"
    assert response["integrity_valid"] is True
    assert response["receipt_payload"]["subject"]["pull_request_number"] == 281


def test_exact_change_receipt_retry_is_idempotent(tmp_path):
    envelope = _envelope(tmp_path)
    existing, _ = build_change_receipt(envelope)

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return existing

    class DuplicateDB:
        rolled_back = False

        @staticmethod
        def add(row):
            return None

        @staticmethod
        def commit():
            raise IntegrityError("insert", {}, Exception("duplicate"))

        def rollback(self):
            self.rolled_back = True

        @staticmethod
        def execute(statement):
            return Result()

    db = DuplicateDB()
    result = persist_change_receipt(envelope, db)

    assert result["status"] == "persisted"
    assert result["idempotent"] is True
    assert db.rolled_back is True


def test_writer_endpoint_fails_closed_when_token_is_unset(monkeypatch):
    monkeypatch.delenv("ASSURANCE_WRITER_TOKEN", raising=False)
    monkeypatch.delenv("ASSURANCE_WRITER_REPOSITORY", raising=False)
    app.dependency_overrides[get_db] = lambda: object()
    with TestClient(app) as client:
        response = client.post(
            "/api/internal/assurance/change-receipts",
            json={"schema_version": "1.0", "source": {}, "artifacts": {}},
        )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ASSURANCE_WRITER_NOT_CONFIGURED"


def test_writer_endpoint_rejects_admin_or_wrong_bearer(monkeypatch):
    monkeypatch.setenv("ASSURANCE_WRITER_TOKEN", "w" * 32)
    monkeypatch.setenv("ASSURANCE_WRITER_REPOSITORY", "Metanet-Final-01/A360-Assistant-Backend")
    app.dependency_overrides[get_db] = lambda: object()
    with TestClient(app) as client:
        response = client.post(
            "/api/internal/assurance/change-receipts",
            headers={"Authorization": "Bearer " + "x" * 32, "X-API-Key": "ops-key"},
            json={"schema_version": "1.0", "source": {}, "artifacts": {}},
        )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "INVALID_ASSURANCE_WRITER"


def test_writer_endpoint_accepts_only_validated_envelope(tmp_path, monkeypatch):
    envelope = _envelope(tmp_path)
    expected = {
        "status": "persisted",
        "receipt_digest": "sha256:" + "a" * 64,
        "idempotent": False,
    }
    captured = {}

    def persist(payload, db):
        captured["payload"] = payload
        captured["db"] = db
        return expected

    fake_db = object()
    monkeypatch.setenv("ASSURANCE_WRITER_TOKEN", "w" * 32)
    monkeypatch.setenv("ASSURANCE_WRITER_REPOSITORY", envelope["source"]["repository"])
    monkeypatch.setattr(writer_api, "persist_change_receipt", persist)
    app.dependency_overrides[get_db] = lambda: fake_db
    with TestClient(app) as client:
        response = client.post(
            "/api/internal/assurance/change-receipts",
            headers={"Authorization": "Bearer " + "w" * 32},
            json=envelope,
        )

    assert response.status_code == 201
    assert response.json() == expected
    assert captured == {"payload": envelope, "db": fake_db}


def test_writer_endpoint_rejects_a_different_repository(tmp_path, monkeypatch):
    envelope = _envelope(tmp_path)
    called = False

    def persist(payload, db):
        nonlocal called
        called = True

    monkeypatch.setenv("ASSURANCE_WRITER_TOKEN", "w" * 32)
    monkeypatch.setenv("ASSURANCE_WRITER_REPOSITORY", "different/repository")
    monkeypatch.setattr(writer_api, "persist_change_receipt", persist)
    app.dependency_overrides[get_db] = lambda: object()
    with TestClient(app) as client:
        response = client.post(
            "/api/internal/assurance/change-receipts",
            headers={"Authorization": "Bearer " + "w" * 32},
            json=envelope,
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_ASSURANCE_EVIDENCE"
    assert called is False


def test_writer_endpoint_rejects_tampered_artifact_without_db_write(tmp_path, monkeypatch):
    envelope = _envelope(tmp_path)
    envelope["artifacts"]["assurance-report.json"]["assurance_decision"] = "deny"
    called = False

    def persist(payload, db):
        nonlocal called
        called = True
        build_change_receipt(payload)
        raise AssertionError("invalid evidence must fail before persistence")

    monkeypatch.setenv("ASSURANCE_WRITER_TOKEN", "w" * 32)
    monkeypatch.setenv("ASSURANCE_WRITER_REPOSITORY", envelope["source"]["repository"])
    monkeypatch.setattr(writer_api, "persist_change_receipt", persist)
    app.dependency_overrides[get_db] = lambda: object()
    with TestClient(app) as client:
        response = client.post(
            "/api/internal/assurance/change-receipts",
            headers={"Authorization": "Bearer " + "w" * 32},
            json=envelope,
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_ASSURANCE_EVIDENCE"
    assert called is True  # service was entered, but validation failed before Session.add


def test_publisher_derives_identity_only_from_matching_workflow_run():
    event = {
        "repository": {"full_name": "Metanet-Final-01/A360-Assistant-Backend"},
        "workflow_run": {
            "id": 12345,
            "run_attempt": 2,
            "name": "Change Assurance (Observe)",
            "event": "pull_request",
            "conclusion": "success",
            "head_sha": "a" * 40,
            "repository": {"full_name": "Metanet-Final-01/A360-Assistant-Backend"},
            "pull_requests": [{"number": 281}],
        },
    }

    source = source_from_event(
        event,
        expected_repository="Metanet-Final-01/A360-Assistant-Backend",
    )
    assert source["workflow_run_id"] == 12345
    assert source["pull_request_number"] == 281

    event["workflow_run"]["pull_requests"].append({"number": 282})
    with pytest.raises(AssuranceError):
        source_from_event(event, expected_repository="Metanet-Final-01/A360-Assistant-Backend")


def test_publisher_requires_https_outside_loopback():
    assert writer_url("https://backend.example.com") == (
        "https://backend.example.com/api/internal/assurance/change-receipts"
    )
    assert writer_url("http://127.0.0.1:8000") == (
        "http://127.0.0.1:8000/api/internal/assurance/change-receipts"
    )
    with pytest.raises(AssuranceError):
        writer_url("http://backend.example.com")


def test_publisher_posts_validated_envelope_without_logging_token(tmp_path, monkeypatch):
    envelope = _envelope(tmp_path)
    captured = {}

    class Response:
        status = 201

        @staticmethod
        def read(limit):
            return json.dumps({
                "status": "persisted",
                "receipt_digest": "sha256:" + "a" * 64,
                "idempotent": False,
            }).encode()

        def __enter__(self):
            return self

        @staticmethod
        def __exit__(*args):
            return False

    class Opener:
        @staticmethod
        def open(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

    def unexpected_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        raise AssertionError("publish must use the no-redirect opener")

    monkeypatch.setattr("urllib.request.urlopen", unexpected_urlopen)
    result = publish(
        envelope,
        url="https://backend.example.com",
        token="w" * 32,
        opener=Opener(),
    )

    assert result["status"] == "persisted"
    assert captured["request"].full_url.endswith("/api/internal/assurance/change-receipts")
    assert captured["request"].get_header("Authorization") == "Bearer " + "w" * 32
    assert json.loads(captured["request"].data) == envelope


def test_publisher_workflow_keeps_writer_secret_out_of_pr_workflow():
    observe = (ROOT / ".github/workflows/change-assurance-observe.yml").read_text(encoding="utf-8")
    publisher = (ROOT / ".github/workflows/change-assurance-publish.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_run:" in publisher
    assert "pull_request_target" not in publisher
    assert "actions: read" in publisher
    assert "contents: read" in publisher
    assert "environment: change-assurance-writer" in publisher
    assert "ASSURANCE_WRITER_ENABLED == 'true'" in publisher
    assert "github.event.repository.default_branch" in publisher
    assert "secrets.ASSURANCE_WRITER_TOKEN" in publisher
    assert "ASSURANCE_WRITER_TOKEN" not in observe
    assert "secrets." not in observe
