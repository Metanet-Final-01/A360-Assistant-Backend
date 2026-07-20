"""Tests for the .github/workflows/backend-deploy.yml GitHub Actions workflow
changes in this PR: new workflow_dispatch inputs, the push-triggers-deploy
behavior change, the new nested-template upload step, and the expanded
`--parameter-overrides` list (plus safe fallbacks for values that only exist
on workflow_dispatch but not on push).
"""
from __future__ import annotations

import re
from collections import Counter

PARAM_OVERRIDE_LINE_RE = re.compile(r"^\s*([A-Za-z0-9]+)=\"(.*)\"\s*\\?\s*$")


def _get_on_triggers(workflow: dict):
    """Return the ``on:`` trigger mapping.

    PyYAML resolves the bare word ``on`` to the boolean ``True`` under the
    YAML 1.1 spec, so the parsed workflow dict has a ``True`` key (not the
    string ``"on"``) for GitHub Actions' ``on:`` section.
    """
    if "on" in workflow:
        return workflow["on"]
    return workflow[True]


def _find_step(workflow, job_name, step_name):
    steps = workflow["jobs"][job_name]["steps"]
    return next(s for s in steps if s.get("name") == step_name)


def _parameter_override_lines(run_script: str):
    """Return (ordered_keys, key->value) parsed from the
    `aws cloudformation deploy --parameter-overrides ...` block."""
    keys = []
    values = {}
    in_overrides = False
    for line in run_script.splitlines():
        if "--parameter-overrides" in line:
            in_overrides = True
            continue
        if not in_overrides:
            continue
        match = PARAM_OVERRIDE_LINE_RE.match(line)
        if match:
            key, value = match.group(1), match.group(2)
            keys.append(key)
            values[key] = value
    return keys, values


EXPECTED_OVERRIDE_KEYS = {
    "Environment",
    "ContainerImage",
    "FrontendOrigins",
    "OpenAiApiKey",
    "VoyageApiKey",
    "JwtSecret",
    "GithubToken",
    "RagDatabaseUrl",
    "ObservabilityDatabaseUrl",
    "OpenSearchHost",
    "OpenSearchUsername",
    "OpenSearchPassword",
    "EnableOpenSearch",
    "OpenSearchMode",
    "NetworkMode",
    "InstanceType",
    "DesiredCapacity",
    "MaxCapacity",
    "EnableRagIngestScheduler",
    "RagIngestSchedulerTemplateUrl",
    "RagIngestScheduleExpression",
}


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


def test_workflow_dispatch_defines_new_inputs(deploy_workflow):
    inputs = _get_on_triggers(deploy_workflow)["workflow_dispatch"]["inputs"]

    network_mode = inputs["network_mode"]
    assert network_mode["type"] == "choice"
    assert network_mode["options"] == ["private", "public-lite"]
    assert network_mode["default"] == "private"
    assert network_mode["required"] is True

    assert inputs["instance_type"]["default"] == "t3.medium"
    assert inputs["desired_capacity"]["default"] == "1"
    assert inputs["max_capacity"]["default"] == "2"

    enable_scheduler = inputs["enable_rag_ingest_scheduler"]
    assert enable_scheduler["type"] == "boolean"
    assert enable_scheduler["default"] is False

    assert inputs["rag_ingest_schedule_expression"]["default"] == "rate(1 minute)"


def test_push_trigger_targets_main_branch(deploy_workflow):
    on_section = _get_on_triggers(deploy_workflow)
    assert on_section["push"]["branches"] == ["main"]


def test_deploy_job_runs_on_push_or_explicit_manual_deploy(deploy_workflow):
    deploy_job = deploy_workflow["jobs"]["deploy"]
    assert deploy_job["if"] == (
        "github.event_name == 'push' || "
        "(github.event_name == 'workflow_dispatch' && inputs.deploy_stack == true)"
    )


# ---------------------------------------------------------------------------
# Nested template upload step
# ---------------------------------------------------------------------------


def test_upload_nested_template_step_is_guarded_by_flag(deploy_workflow):
    step = _find_step(
        deploy_workflow, "deploy", "Upload nested CloudFormation templates"
    )
    assert step["id"] == "nested"
    assert step["if"] == "inputs.enable_rag_ingest_scheduler == true"


def test_upload_nested_template_step_requires_artifact_bucket_secret(deploy_workflow):
    step = _find_step(
        deploy_workflow, "deploy", "Upload nested CloudFormation templates"
    )
    run_script = step["run"]
    assert "CFN_ARTIFACT_BUCKET" in run_script
    assert "exit 1" in run_script
    assert "infra/rag-ingest-scheduler-sqs.yaml" in run_script
    assert "rag_scheduler_template_url" in run_script
    assert "$GITHUB_OUTPUT" in run_script


# ---------------------------------------------------------------------------
# Deploy CloudFormation step: parameter overrides
# ---------------------------------------------------------------------------


def test_deploy_step_parameter_overrides_cover_all_expected_keys(deploy_workflow):
    step = _find_step(deploy_workflow, "deploy", "Deploy CloudFormation")
    keys, _ = _parameter_override_lines(step["run"])
    assert set(keys) == EXPECTED_OVERRIDE_KEYS


def test_deploy_step_parameter_overrides_have_no_duplicate_keys(deploy_workflow):
    step = _find_step(deploy_workflow, "deploy", "Deploy CloudFormation")
    keys, _ = _parameter_override_lines(step["run"])
    duplicates = [key for key, count in Counter(keys).items() if count > 1]
    assert not duplicates, f"duplicate --parameter-overrides keys: {duplicates}"


def test_deploy_step_falls_back_to_defaults_for_push_only_inputs(deploy_workflow):
    # These parameters are sourced from workflow_dispatch `inputs.*`, which are
    # unset on a plain `push` event, so each must carry a `|| <default>`
    # fallback (or an equivalent ternary) to avoid deploying with empty values.
    step = _find_step(deploy_workflow, "deploy", "Deploy CloudFormation")
    _, values = _parameter_override_lines(step["run"])

    assert values["Environment"] == "${{ inputs.environment || 'dev' }}"
    assert values["NetworkMode"] == "${{ inputs.network_mode || 'public-lite' }}"
    assert values["InstanceType"] == "${{ inputs.instance_type || 't3.medium' }}"
    assert values["DesiredCapacity"] == "${{ inputs.desired_capacity || '1' }}"
    assert values["MaxCapacity"] == "${{ inputs.max_capacity || '2' }}"
    assert values["RagIngestScheduleExpression"] == (
        "${{ inputs.rag_ingest_schedule_expression || 'rate(1 minute)' }}"
    )
    assert values["EnableRagIngestScheduler"] == (
        "${{ inputs.enable_rag_ingest_scheduler && 'true' || 'false' }}"
    )


def test_deploy_step_wires_nested_template_url_from_upload_step_output(
    deploy_workflow,
):
    step = _find_step(deploy_workflow, "deploy", "Deploy CloudFormation")
    _, values = _parameter_override_lines(step["run"])
    assert values["RagIngestSchedulerTemplateUrl"] == (
        "${{ steps.nested.outputs.rag_scheduler_template_url }}"
    )


def test_deploy_step_secrets_are_not_hardcoded(deploy_workflow):
    step = _find_step(deploy_workflow, "deploy", "Deploy CloudFormation")
    _, values = _parameter_override_lines(step["run"])
    for key in (
        "OpenAiApiKey",
        "VoyageApiKey",
        "JwtSecret",
        "GithubToken",
        "RagDatabaseUrl",
        "ObservabilityDatabaseUrl",
        "OpenSearchHost",
        "OpenSearchUsername",
        "OpenSearchPassword",
    ):
        assert values[key].startswith("${{ secrets.")


def test_deploy_step_override_keys_are_declared_parameters_in_backend_template(
    deploy_workflow, backend_template
):
    step = _find_step(deploy_workflow, "deploy", "Deploy CloudFormation")
    keys, _ = _parameter_override_lines(step["run"])
    template_params = set(backend_template["Parameters"])
    for key in keys:
        assert key in template_params, (
            f"workflow overrides parameter {key!r} which is not declared in "
            "infra/a360-backend-private.yml"
        )