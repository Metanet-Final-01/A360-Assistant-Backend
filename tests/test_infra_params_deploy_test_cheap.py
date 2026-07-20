"""Tests for the new infra/params.deploy-test-cheap.example.json parameter
overrides file, including cross-checks against the CloudFormation template's
declared Parameters so a typo'd ParameterKey doesn't silently no-op.
"""
from __future__ import annotations


def _as_dict(params_list):
    return {item["ParameterKey"]: item["ParameterValue"] for item in params_list}


def test_is_a_list_of_key_value_objects(cheap_params):
    assert isinstance(cheap_params, list)
    assert len(cheap_params) > 0
    for item in cheap_params:
        assert set(item.keys()) == {"ParameterKey", "ParameterValue"}
        assert isinstance(item["ParameterKey"], str) and item["ParameterKey"]
        assert isinstance(item["ParameterValue"], str)


def test_no_duplicate_parameter_keys(cheap_params):
    keys = [item["ParameterKey"] for item in cheap_params]
    assert len(keys) == len(set(keys)), f"duplicate ParameterKeys found: {keys}"


def test_expected_cheap_deploy_overrides(cheap_params):
    values = _as_dict(cheap_params)
    assert values["ProjectName"] == "a360-assistant"
    assert values["Environment"] == "dev"
    assert values["NetworkMode"] == "public-lite"
    assert values["InstanceType"] == "t3.small"
    assert values["DesiredCapacity"] == "1"
    assert values["MaxCapacity"] == "1"
    assert values["DatabaseInstanceClass"] == "db.t4g.micro"
    assert values["DatabaseAllocatedStorage"] == "20"
    assert values["EnableOpenSearch"] == "false"
    assert values["EnableRagIngestScheduler"] == "false"
    assert values["RagIngestSchedulerTemplateUrl"] == ""
    assert values["RagIngestScheduleExpression"] == "rate(1 minute)"
    assert values["FrontendOrigins"]


def test_desired_capacity_does_not_exceed_max_capacity(cheap_params):
    values = _as_dict(cheap_params)
    assert int(values["DesiredCapacity"]) <= int(values["MaxCapacity"])


def test_container_image_is_a_placeholder_to_be_replaced(cheap_params):
    values = _as_dict(cheap_params)
    assert "ContainerImage" in values
    assert "OWNER" in values["ContainerImage"] or "REPO" in values["ContainerImage"]


def test_all_parameter_keys_exist_in_backend_template(cheap_params, backend_template):
    template_params = set(backend_template["Parameters"])
    for item in cheap_params:
        key = item["ParameterKey"]
        assert key in template_params, (
            f"{key!r} in params.deploy-test-cheap.example.json is not a declared "
            "Parameter in infra/a360-backend-private.yml"
        )


def test_network_mode_value_is_allowed_by_template(cheap_params, backend_template):
    values = _as_dict(cheap_params)
    allowed = backend_template["Parameters"]["NetworkMode"]["AllowedValues"]
    assert values["NetworkMode"] in allowed


def test_enable_rag_ingest_scheduler_value_is_allowed_by_template(
    cheap_params, backend_template
):
    values = _as_dict(cheap_params)
    allowed = backend_template["Parameters"]["EnableRagIngestScheduler"]["AllowedValues"]
    assert values["EnableRagIngestScheduler"] in allowed