"""RPA-207 protected Change receipt transport and persistence contract tests."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

import app.api.assurance_writer as writer_api
from app.api.admin import _assurance_receipt_out
from app.db import get_db
from app.main import app
from app.services.assurance_evidence import (
    _sanitize_human_review,
    build_change_receipt,
    persist_change_receipt,
    receipt_integrity,
)
from assurance.change.foundation import AssuranceError, canonical_digest
from assurance.change.transport import load_change_envelope, validate_change_envelope
from scripts.publish_change_assurance import publish, source_from_event, writer_url
from tests.test_change_assurance import _load_scenarios, _run_scenario


ROOT = Path(__file__).resolve().parents[1]


def test_human_review_receipt_projection_keeps_only_display_contract():
    projected = _sanitize_human_review({
        "status": "approved",
        "reason_code": "HUMAN_REVIEW_VERIFIED",
        "pull_request_number": 329,
        "expected_head_sha": "a" * 40,
        "event_name": "pull_request_review",
        "unexpected": {"large": "value"},
        "review": {
            "reviewer_login": "reviewer",
            "submitted_at": "2026-07-21T08:00:00Z",
            "commit_id": "a" * 40,
            "body": "must not be persisted",
        },
    })

    assert set(projected) == {
        "status",
        "reason_code",
        "pull_request_number",
        "expected_head_sha",
        "review",
    }
    assert set(projected["review"]) == {"reviewer_login", "submitted_at", "commit_id"}


class _CloudFormationLoader(yaml.SafeLoader):
    """Parse CloudFormation tags as data without resolving or executing them."""


def _construct_cloudformation_tag(loader, _tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_CloudFormationLoader.add_multi_constructor("!", _construct_cloudformation_tag)


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


def test_missing_expected_artifact_cannot_be_promoted_to_observed(tmp_path):
    envelope = _envelope(tmp_path)
    envelope["artifacts"].pop("runtime-environment.json")
    integrity = envelope["artifacts"]["evidence-integrity.json"]
    integrity["evidence"] = [
        ref for ref in integrity["evidence"] if ref["uri"] != "runtime-environment.json"
    ]
    integrity_digest = canonical_digest(integrity)
    report = envelope["artifacts"]["assurance-report.json"]
    for control in report["controls"]:
        if control["evidence"]["uri"] == "evidence-integrity.json":
            control["evidence"]["sha256"] = integrity_digest
    report_digest = canonical_digest(report)
    index = envelope["artifacts"]["evidence-index.json"]
    index["artifacts"] = [
        ref for ref in index["artifacts"] if ref["uri"] != "runtime-environment.json"
    ]
    for ref in index["artifacts"]:
        if ref["uri"] == "evidence-integrity.json":
            ref["sha256"] = integrity_digest
        elif ref["uri"] == "assurance-report.json":
            ref["sha256"] = report_digest

    row, summary = build_change_receipt(envelope)

    assert row.decision == "allow_candidate"
    assert summary["assurance_verdict"] == "refused"
    assert row.assurance_status == "refused_unassured"
    assert summary["completeness_status"] == "incomplete"
    assert summary["missing_evidence"] == ["runtime-environment.json"]


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


def test_top_level_symbolic_artifact_directory_is_rejected(tmp_path, monkeypatch):
    scenario = _load_scenarios()["good_import"]
    report, output = _run_scenario(tmp_path, scenario)
    alias = tmp_path / "artifact-link"
    original_is_symlink = Path.is_symlink
    original_resolve = Path.resolve

    def fake_is_symlink(path):
        return path == alias or original_is_symlink(path)

    def fake_resolve(path, *args, **kwargs):
        if path == alias:
            return output
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    monkeypatch.setattr(Path, "resolve", fake_resolve)

    with pytest.raises(AssuranceError, match="unavailable or symbolic"):
        load_change_envelope(alias, source={
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
    assert response["human_review"]["status"] == "missing"
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


@pytest.mark.parametrize(
    "token",
    [
        "w" * 31,
        "w" * 129,
        "w" * 31 + ".",
        "w" * 31 + "=",
    ],
)
def test_writer_endpoint_fails_closed_for_non_base64url_token(monkeypatch, token):
    monkeypatch.setenv("ASSURANCE_WRITER_TOKEN", token)
    monkeypatch.setenv("ASSURANCE_WRITER_REPOSITORY", "Metanet-Final-01/A360-Assistant-Backend")
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


def test_publisher_accepts_review_triggered_assurance_run():
    event = {
        "repository": {"full_name": "Metanet-Final-01/A360-Assistant-Backend"},
        "workflow_run": {
            "id": 12346,
            "run_attempt": 1,
            "name": "Change Assurance (Observe)",
            "event": "pull_request_review",
            "conclusion": "success",
            "head_sha": "a" * 40,
            "repository": {"full_name": "Metanet-Final-01/A360-Assistant-Backend"},
            "pull_requests": [{"number": 329}],
        },
    }

    source = source_from_event(
        event, expected_repository="Metanet-Final-01/A360-Assistant-Backend"
    )
    assert source["event"] == "pull_request_review"
    assert source["pull_request_number"] == 329


def test_transport_accepts_review_triggered_follow_up_record(tmp_path):
    envelope = _envelope(tmp_path)
    envelope["source"]["event"] = "pull_request_review"

    facts = validate_change_envelope(envelope)

    assert facts["source"]["event"] == "pull_request_review"


def test_publisher_accepts_one_sha_resolved_pull_request_when_event_list_is_empty():
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
            "pull_requests": [],
        },
    }

    source = source_from_event(
        event,
        expected_repository="Metanet-Final-01/A360-Assistant-Backend",
        resolved_pull_request_number=281,
    )

    assert source["pull_request_number"] == 281
    with pytest.raises(AssuranceError, match="identity is unavailable"):
        source_from_event(event, expected_repository="Metanet-Final-01/A360-Assistant-Backend")


def test_publisher_rejects_resolved_pull_request_that_disagrees_with_event():
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

    with pytest.raises(AssuranceError, match="does not match"):
        source_from_event(
            event,
            expected_repository="Metanet-Final-01/A360-Assistant-Backend",
            resolved_pull_request_number=282,
        )


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
    workflow_header, jobs = publisher.split("jobs:", 1)
    resolve_job, publish_job = jobs.split("\n  publish:", 1)

    assert "workflow_run:" in workflow_header
    assert "pull_request_target" not in publisher
    assert "pull_request_review:" in observe
    assert "types: [submitted, dismissed]" in observe
    assert "python -m assurance.change.review_evidence" in observe
    assert "--review-evidence" in observe
    assert "First rollout: execute only the checker already trusted" in observe
    assert "grep -q -- '--review-evidence' assurance/change/cli.py" in observe
    assert "actions: read" in workflow_header
    assert "contents: read" in workflow_header
    assert "pull-requests: read" in workflow_header
    assert "ASSURANCE_WRITER_ENABLED == 'true'" in resolve_job
    assert "pull_request_review" in resolve_job
    assert "/commits/${HEAD_SHA}/pulls" in resolve_job
    assert 'api_output="$(mktemp)"' in resolve_job
    assert '> "$api_output"' in resolve_job
    assert 'mapfile -t matches < "$api_output"' in resolve_job
    assert "< <(" not in resolve_job
    assert "secrets." not in resolve_job
    assert "environment: change-assurance-writer" in publish_job
    assert "needs.resolve_pr.outputs.pull_request_number" in publish_job
    assert "github.event.repository.default_branch" in publish_job
    assert "secrets.ASSURANCE_WRITER_TOKEN" in publish_job
    assert 'entries=("$artifact_dir"/*)' in publish_job
    assert '${#entries[@]} -eq 1' in publish_job
    assert '! -L "$bootstrap"' in publish_job
    assert "required=(SHA256SUMS assurance-report.json evidence-index.json)" in publish_job
    assert "Incomplete Change Assurance bundle" in publish_job
    assert "steps.evidence.outputs.publish == 'true'" in publish_job
    assert "python -m scripts.publish_change_assurance" in publish_job
    assert "python scripts/publish_change_assurance.py" not in publish_job
    assert "ASSURANCE_WRITER_TOKEN" not in observe
    assert "secrets." not in observe


def test_backend_deploy_injects_writer_credentials_from_protected_environment():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/backend-deploy.yml").read_text(encoding="utf-8")
    )
    template = yaml.load(
        (ROOT / "infra/a360-backend-private.yml").read_text(encoding="utf-8"),
        Loader=_CloudFormationLoader,
    )
    build_job = workflow["jobs"]["build"]
    deploy_job = workflow["jobs"]["deploy"]
    deploy_uses = {step["uses"] for step in deploy_job["steps"] if "uses" in step}
    deploy_script = next(
        step["run"]
        for step in deploy_job["steps"]
        if step.get("name") == "Deploy CloudFormation"
    )
    opensearch_step = next(
        step for step in deploy_job["steps"] if step.get("name") == "Validate OpenSearch host"
    )
    token_parameter = template["Parameters"]["AssuranceWriterToken"]
    opensearch_parameter = template["Parameters"]["ExternalOpenSearchHost"]
    app_secret = template["Resources"]["AppSecret"]["Properties"]["SecretString"]
    user_data = template["Resources"]["AppLaunchTemplate"]["Properties"][
        "LaunchTemplateData"
    ]["UserData"]["Fn::Base64"][0]

    assert "ASSURANCE_WRITER_TOKEN" not in str(build_job)
    assert deploy_job["environment"] == "backend-deploy-${{ inputs.environment }}"
    assert "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" in deploy_uses
    assert (
        "aws-actions/configure-aws-credentials@7474bc4690e29a8392af63c5b98e7449536d5c3a"
        in deploy_uses
    )
    assert deploy_job["env"]["STACK_NAME"] == "a360-assistant-${{ inputs.environment }}-backend"
    assert 'AssuranceWriterToken="${{ secrets.ASSURANCE_WRITER_TOKEN }}"' in deploy_script
    assert 'AssuranceWriterRepository="${{ github.repository }}"' in deploy_script
    assert 'ExternalOpenSearchHost="${{ secrets.OPENSEARCH_HOST }}"' in deploy_script
    assert opensearch_step["env"]["OPENSEARCH_HOST"] == "${{ secrets.OPENSEARCH_HOST }}"
    assert "^https?://[^[:space:]]+$" in opensearch_step["run"]

    assert token_parameter["NoEcho"] is True
    assert token_parameter["AllowedPattern"] == "^$|^[A-Za-z0-9_-]{32,128}$"
    assert opensearch_parameter["NoEcho"] is True
    assert opensearch_parameter["AllowedPattern"] == "^https?://[^\\s]+$"
    assert '"ASSURANCE_WRITER_TOKEN": "${AssuranceWriterToken}"' in app_secret
    assert '"ASSURANCE_WRITER_REPOSITORY": "${AssuranceWriterRepository}"' in app_secret
    assert "jq -r '.ASSURANCE_WRITER_TOKEN // \"\"'" in user_data
    assert "jq -r '.ASSURANCE_WRITER_REPOSITORY // \"\"'" in user_data
    assert "ASSURANCE_WRITER_TOKEN=$ASSURANCE_WRITER_TOKEN" in user_data
    assert "ASSURANCE_WRITER_REPOSITORY=$ASSURANCE_WRITER_REPOSITORY" in user_data
    assert "OPENSEARCH_HOST=${ExternalOpenSearchHost}" in user_data
    assert "python3" not in user_data
    assert user_data.startswith("#!/bin/bash -eu\n")
    assert "#!/bin/bash -eux" not in user_data
    env_mode = user_data.index("install -m 600 /dev/null /opt/a360/.env")
    env_write = user_data.index("cat > /opt/a360/.env <<EOF")
    assert env_mode < env_write


def test_backend_deploy_injects_ops_api_key_from_protected_environment():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/backend-deploy.yml").read_text(encoding="utf-8")
    )
    template = yaml.load(
        (ROOT / "infra/a360-backend-private.yml").read_text(encoding="utf-8"),
        Loader=_CloudFormationLoader,
    )
    build_job = workflow["jobs"]["build"]
    deploy_job = workflow["jobs"]["deploy"]
    validate_step = next(
        step for step in deploy_job["steps"] if step.get("name") == "Validate Ops API credential"
    )
    deploy_script = next(
        step["run"]
        for step in deploy_job["steps"]
        if step.get("name") == "Deploy CloudFormation"
    )
    token_parameter = template["Parameters"]["OpsApiKey"]
    app_secret = template["Resources"]["AppSecret"]["Properties"]["SecretString"]
    user_data = template["Resources"]["AppLaunchTemplate"]["Properties"][
        "LaunchTemplateData"
    ]["UserData"]["Fn::Base64"][0]

    assert "OPS_API_KEY" not in str(build_job)
    assert deploy_job["environment"] == "backend-deploy-${{ inputs.environment }}"
    assert validate_step["env"]["OPS_API_KEY"] == "${{ secrets.OPS_API_KEY }}"
    assert '[ -z "$OPS_API_KEY" ]' in validate_step["run"]
    assert "exit 1" in validate_step["run"]
    assert "secrets.OPS_API_KEY" not in validate_step["run"]
    assert 'OpsApiKey="${{ secrets.OPS_API_KEY }}"' in deploy_script
    assert token_parameter["NoEcho"] is True
    assert token_parameter["AllowedPattern"] == "^$|^[A-Za-z0-9_-]{32,128}$"
    assert '"OPS_API_KEY": "${OpsApiKey}"' in app_secret
    assert "jq -r '.OPS_API_KEY // \"\"'" in user_data
    assert "OPS_API_KEY=$OPS_API_KEY" in user_data
    assert "python3" not in user_data
    assert user_data.startswith("#!/bin/bash -eu\n")
    assert "#!/bin/bash -eux" not in user_data


def test_backend_bootstrap_mode_uses_ec2_health_without_target_registration():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/backend-deploy.yml").read_text(encoding="utf-8")
    )
    template = yaml.load(
        (ROOT / "infra/a360-backend-private.yml").read_text(encoding="utf-8"),
        Loader=_CloudFormationLoader,
    )
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    asg_properties = template["Resources"]["AppAutoScalingGroup"]["Properties"]

    assert inputs["start_backend_container"]["default"] is True
    assert template["Conditions"]["StartsBackendContainer"] == [
        "StartBackendContainer",
        "true",
    ]
    assert asg_properties["TargetGroupARNs"] == [
        "StartsBackendContainer",
        ["BackendTargetGroup"],
        "AWS::NoValue",
    ]
    assert asg_properties["HealthCheckType"] == ["StartsBackendContainer", "ELB", "EC2"]
    assert asg_properties["HealthCheckGracePeriod"] == [
        "StartsBackendContainer",
        300,
        "AWS::NoValue",
    ]
