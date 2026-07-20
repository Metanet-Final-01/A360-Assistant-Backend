"""Tests for the parts of infra/a360-backend-private.yml that changed in this PR:

- NetworkMode (private / public-lite) and the NAT-related conditions/resources
- New secret parameters (Voyage, JWT, Rag/Observability DB URLs, OpenSearch creds)
- OpenSearchMode (managed / container) and OpenSearchHost override
- RAG ingest scheduler nested stack wiring
- ALB idle timeout / CloudFront origin timeout bumps
- LaunchTemplate network interface + AutoScalingGroup subnet selection changes
- UserData env var additions

Pre-existing, unmodified resources (VPC, RDS, base security groups, etc.) are
intentionally not covered here since they are out of scope for this PR.
"""
from __future__ import annotations


def _params(template):
    return template["Parameters"]


def _conditions(template):
    return template["Conditions"]


def _resources(template):
    return template["Resources"]


def _outputs(template):
    return template["Outputs"]


# ---------------------------------------------------------------------------
# NetworkMode parameter + NAT conditions
# ---------------------------------------------------------------------------


def test_network_mode_parameter_defaults_to_private(backend_template):
    param = _params(backend_template)["NetworkMode"]
    assert param["Type"] == "String"
    assert param["Default"] == "private"
    assert param["AllowedValues"] == ["private", "public-lite"]


def test_is_public_lite_condition_checks_network_mode(backend_template):
    conditions = _conditions(backend_template)
    assert conditions["IsPublicLite"] == {
        "Fn::Equals": [{"Ref": "NetworkMode"}, "public-lite"]
    }


def test_needs_nat_condition_is_inverse_of_public_lite(backend_template):
    conditions = _conditions(backend_template)
    assert conditions["NeedsNat"] == {"Fn::Not": [{"Condition": "IsPublicLite"}]}


def test_nat_resources_are_conditioned_on_needs_nat(backend_template):
    resources = _resources(backend_template)
    for name in ("NatEip", "NatGateway", "PrivateDefaultRoute"):
        assert resources[name].get("Condition") == "NeedsNat", (
            f"{name} must only be created when a NAT Gateway is needed"
        )


def test_launch_template_uses_network_interfaces_not_top_level_sg(backend_template):
    launch_data = _resources(backend_template)["AppLaunchTemplate"]["Properties"][
        "LaunchTemplateData"
    ]
    assert "SecurityGroupIds" not in launch_data
    interfaces = launch_data["NetworkInterfaces"]
    assert len(interfaces) == 1
    iface = interfaces[0]
    assert iface["DeviceIndex"] == 0
    assert iface["Groups"] == [{"Ref": "AppSecurityGroup"}]
    assert iface["AssociatePublicIpAddress"] == {
        "Fn::If": ["IsPublicLite", True, False]
    }


def test_autoscaling_group_picks_subnets_based_on_network_mode(backend_template):
    asg_props = _resources(backend_template)["AppAutoScalingGroup"]["Properties"]
    assert asg_props["VPCZoneIdentifier"] == {
        "Fn::If": [
            "IsPublicLite",
            [{"Ref": "PublicSubnet1"}, {"Ref": "PublicSubnet2"}],
            [{"Ref": "PrivateAppSubnet1"}, {"Ref": "PrivateAppSubnet2"}],
        ]
    }


# ---------------------------------------------------------------------------
# New secret / config parameters
# ---------------------------------------------------------------------------


def test_new_secret_parameters_default_empty_and_no_echo(backend_template):
    params = _params(backend_template)
    secret_params = [
        "VoyageApiKey",
        "JwtSecret",
        "RagDatabaseUrl",
        "ObservabilityDatabaseUrl",
        "OpenSearchHost",
        "OpenSearchUsername",
        "OpenSearchPassword",
    ]
    for name in secret_params:
        param = params[name]
        assert param["Type"] == "String"
        assert param["Default"] == ""
        assert param.get("NoEcho") is True, f"{name} should be NoEcho to avoid leaking secrets"


def test_has_external_opensearch_condition_checks_host_param(backend_template):
    conditions = _conditions(backend_template)
    assert conditions["HasExternalOpenSearch"] == {
        "Fn::Not": [{"Fn::Equals": [{"Ref": "OpenSearchHost"}, ""]}]
    }


def test_app_secret_includes_all_new_keys(backend_template):
    secret_string = _resources(backend_template)["AppSecret"]["Properties"][
        "SecretString"
    ]
    rendered = secret_string["Fn::Sub"]
    for expected_line in (
        '"VOYAGE_API_KEY": "${VoyageApiKey}"',
        '"JWT_SECRET": "${JwtSecret}"',
        '"RAG_DATABASE_URL": "${RagDatabaseUrl}"',
        '"OBSERVABILITY_DATABASE_URL": "${ObservabilityDatabaseUrl}"',
        '"OPENSEARCH_USERNAME": "${OpenSearchUsername}"',
        '"OPENSEARCH_PASSWORD": "${OpenSearchPassword}"',
        '"OPENAI_API_KEY": "${OpenAiApiKey}"',
        '"GITHUB_TOKEN": "${GithubToken}"',
    ):
        assert expected_line in rendered


# ---------------------------------------------------------------------------
# OpenSearchMode (managed / container)
# ---------------------------------------------------------------------------


def test_opensearch_mode_parameter_allows_managed_and_container(backend_template):
    param = _params(backend_template)["OpenSearchMode"]
    assert param["Default"] == "managed"
    assert param["AllowedValues"] == ["managed", "container"]


def test_use_managed_and_container_opensearch_conditions(backend_template):
    conditions = _conditions(backend_template)
    assert conditions["UseManagedOpenSearch"] == {
        "Fn::And": [
            {"Condition": "CreateOpenSearch"},
            {"Fn::Equals": [{"Ref": "OpenSearchMode"}, "managed"]},
        ]
    }
    assert conditions["UseContainerOpenSearch"] == {
        "Fn::And": [
            {"Condition": "CreateOpenSearch"},
            {"Fn::Equals": [{"Ref": "OpenSearchMode"}, "container"]},
        ]
    }


def test_opensearch_domain_and_sg_use_managed_condition_not_create(backend_template):
    resources = _resources(backend_template)
    assert resources["OpenSearchDomain"]["Condition"] == "UseManagedOpenSearch"
    assert resources["OpenSearchSecurityGroup"]["Condition"] == "UseManagedOpenSearch"


def test_opensearch_endpoint_output_uses_managed_condition(backend_template):
    output = _outputs(backend_template)["OpenSearchEndpoint"]
    assert output["Condition"] == "UseManagedOpenSearch"


# ---------------------------------------------------------------------------
# RAG ingest scheduler nested stack
# ---------------------------------------------------------------------------


def test_enable_rag_ingest_scheduler_parameter_is_boolean_string(backend_template):
    param = _params(backend_template)["EnableRagIngestScheduler"]
    assert param["Default"] == "false"
    assert param["AllowedValues"] == ["true", "false"]


def test_rag_ingest_option_parameter_bounds(backend_template):
    param = _params(backend_template)["RagIngestOption"]
    assert param["Type"] == "Number"
    assert param["Default"] == 3
    assert param["MinValue"] == 1
    assert param["MaxValue"] == 3


def test_create_rag_ingest_scheduler_condition(backend_template):
    conditions = _conditions(backend_template)
    assert conditions["CreateRagIngestScheduler"] == {
        "Fn::Equals": [{"Ref": "EnableRagIngestScheduler"}, "true"]
    }


def test_rag_ingest_scheduler_stack_resource(backend_template):
    resource = _resources(backend_template)["RagIngestSchedulerStack"]
    assert resource["Type"] == "AWS::CloudFormation::Stack"
    assert resource["Condition"] == "CreateRagIngestScheduler"
    props = resource["Properties"]
    assert props["TemplateURL"] == {"Ref": "RagIngestSchedulerTemplateUrl"}
    params = props["Parameters"]
    assert params["ScheduleExpression"] == {"Ref": "RagIngestScheduleExpression"}
    assert params["IngestOption"] == {"Ref": "RagIngestOption"}
    assert params["Clean"] == {"Ref": "RagIngestClean"}
    assert params["AppInstanceRoleName"] == {"Ref": "AppInstanceRole"}


def test_rag_ingest_outputs_reference_nested_stack(backend_template):
    outputs = _outputs(backend_template)
    for name, nested_output in (
        ("RagIngestQueueUrl", "Outputs.QueueUrl"),
        ("RagIngestQueueArn", "Outputs.QueueArn"),
        ("RagIngestSchedulerRoleArn", "Outputs.SchedulerRoleArn"),
    ):
        output = outputs[name]
        assert output["Condition"] == "CreateRagIngestScheduler"
        assert output["Value"] == {
            "Fn::GetAtt": ["RagIngestSchedulerStack", nested_output]
        }


# ---------------------------------------------------------------------------
# ALB idle timeout / CloudFront origin timeouts
# ---------------------------------------------------------------------------


def test_alb_idle_timeout_is_120_seconds(backend_template):
    alb_props = _resources(backend_template)["ApplicationLoadBalancer"]["Properties"]
    attrs = alb_props["LoadBalancerAttributes"]
    assert {"Key": "idle_timeout.timeout_seconds", "Value": "120"} in attrs


def test_cloudfront_origin_timeouts_raised_to_60_seconds(backend_template):
    dist_config = _resources(backend_template)["AppCloudFrontDistribution"][
        "Properties"
    ]["DistributionConfig"]
    origin = dist_config["Origins"][0]
    custom_origin = origin["CustomOriginConfig"]
    assert custom_origin["OriginReadTimeout"] == 60
    assert custom_origin["OriginKeepaliveTimeout"] == 60


# ---------------------------------------------------------------------------
# UserData: new env vars + OpenSearchHost priority + compose block injection
# ---------------------------------------------------------------------------


def _user_data_fn_sub(backend_template):
    user_data = _resources(backend_template)["AppLaunchTemplate"]["Properties"][
        "LaunchTemplateData"
    ]["UserData"]
    return user_data["Fn::Base64"]["Fn::Sub"]


def test_user_data_is_fn_sub_with_script_and_substitution_map(backend_template):
    fn_sub = _user_data_fn_sub(backend_template)
    assert isinstance(fn_sub, list)
    assert len(fn_sub) == 2
    script, substitutions = fn_sub
    assert isinstance(script, str)
    assert isinstance(substitutions, dict)
    assert set(substitutions) == {
        "OpenSearchHost",
        "RagSchedulerProvider",
        "RagIngestQueueUrl",
        "RagIngestQueueArn",
        "OpenSearchComposeBlock",
    }


def test_user_data_opensearch_host_priority_order(backend_template):
    _, substitutions = _user_data_fn_sub(backend_template)
    assert substitutions["OpenSearchHost"] == {
        "Fn::If": [
            "HasExternalOpenSearch",
            {"Ref": "OpenSearchHost"},
            {
                "Fn::If": [
                    "UseManagedOpenSearch",
                    {"Fn::Sub": "https://${OpenSearchDomain.DomainEndpoint}"},
                    {"Fn::If": ["UseContainerOpenSearch", "http://opensearch:9200", ""]},
                ]
            },
        ]
    }


def test_user_data_rag_scheduler_provider_and_queue_refs(backend_template):
    _, substitutions = _user_data_fn_sub(backend_template)
    assert substitutions["RagSchedulerProvider"] == {
        "Fn::If": ["CreateRagIngestScheduler", "eventbridge-sqs", "local"]
    }
    assert substitutions["RagIngestQueueUrl"] == {
        "Fn::If": [
            "CreateRagIngestScheduler",
            {"Fn::GetAtt": ["RagIngestSchedulerStack", "Outputs.QueueUrl"]},
            "",
        ]
    }
    assert substitutions["RagIngestQueueArn"] == {
        "Fn::If": [
            "CreateRagIngestScheduler",
            {"Fn::GetAtt": ["RagIngestSchedulerStack", "Outputs.QueueArn"]},
            "",
        ]
    }


def test_user_data_opensearch_compose_block_only_when_container_mode(backend_template):
    _, substitutions = _user_data_fn_sub(backend_template)
    compose_block = substitutions["OpenSearchComposeBlock"]
    assert compose_block["Fn::If"][0] == "UseContainerOpenSearch"
    assert compose_block["Fn::If"][2] == ""
    container_block = compose_block["Fn::If"][1]
    assert "opensearchproject/opensearch:2.13.0" in container_block
    assert "OPENSEARCH_INITIAL_ADMIN_PASSWORD" in container_block


def test_user_data_script_exports_new_env_vars(backend_template):
    script, _ = _user_data_fn_sub(backend_template)
    for expected in (
        "VOYAGE_API_KEY=$VOYAGE_API_KEY",
        "JWT_SECRET=$JWT_SECRET",
        "RAG_DATABASE_URL=$RAG_DATABASE_URL",
        "OBSERVABILITY_DATABASE_URL=$OBSERVABILITY_DATABASE_URL",
        "OPENSEARCH_USERNAME=$OPENSEARCH_USERNAME",
        "OPENSEARCH_PASSWORD=$OPENSEARCH_PASSWORD",
        "RAG_SCHEDULER_PROVIDER=${RagSchedulerProvider}",
        "RAG_INGEST_SQS_QUEUE_URL=${RagIngestQueueUrl}",
        "RAG_INGEST_SQS_QUEUE_ARN=${RagIngestQueueArn}",
        "RAG_INGEST_OPTION=${RagIngestOption}",
        "${OpenSearchComposeBlock}",
    ):
        assert expected in script, f"expected {expected!r} in generated UserData script"


def test_user_data_script_no_longer_injects_logs_bucket_env_var(backend_template):
    script, _ = _user_data_fn_sub(backend_template)
    assert "LOGS_BUCKET=${LogsBucket}" not in script


def test_logs_bucket_resource_and_iam_permissions_still_exist(backend_template):
    # The env var injection was removed, but the bucket resource and the app
    # role's S3 permissions on it must still exist (see inline comment in the
    # template - deleting the bucket itself would be destructive).
    resources = _resources(backend_template)
    assert resources["LogsBucket"]["Type"] == "AWS::S3::Bucket"
    statements = resources["AppInstanceRole"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]["Statement"]
    s3_statement = next(s for s in statements if "s3:GetObject" in s["Action"])
    assert {"Fn::GetAtt": ["LogsBucket", "Arn"]} in s3_statement["Resource"]