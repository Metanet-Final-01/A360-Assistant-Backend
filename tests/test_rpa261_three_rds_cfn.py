import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "infra" / "a360-backend-private.yml"
DEPLOY_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "backend-deploy.yml"


class _CloudFormationLoader(yaml.SafeLoader):
    pass


def _construct_cloudformation_tag(loader, _tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_CloudFormationLoader.add_multi_constructor("!", _construct_cloudformation_tag)


def _template() -> dict:
    return yaml.load(TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_CloudFormationLoader)


def _deploy_workflow() -> dict:
    return yaml.safe_load(DEPLOY_WORKFLOW_PATH.read_text(encoding="utf-8"))


def _scalar_values(value) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return set().union(*(_scalar_values(item) for item in value))
    if isinstance(value, dict):
        return set().union(*(_scalar_values(item) for item in value.values()))
    return set()


def test_three_private_rds_instances_keep_service_database_logical_id():
    template = _template()
    resources = template["Resources"]
    assert template["Parameters"]["DatabaseInstanceClass"]["AllowedValues"] == [
        "db.t4g.micro"
    ]
    databases = {
        name: resource
        for name, resource in resources.items()
        if resource.get("Type") == "AWS::RDS::DBInstance"
    }

    assert set(databases) == {"Database", "ObservabilityDatabase", "RagDatabase"}
    assert databases["Database"]["Properties"]["DBInstanceIdentifier"] == (
        "${ProjectName}-${Environment}-postgres"
    )

    expected = {
        "Database": ("DatabaseName", "DatabaseSecurityGroup", "ServiceDatabaseParameterGroup"),
        "ObservabilityDatabase": (
            "ObservabilityDatabaseName",
            "ObservabilityDatabaseSecurityGroup",
            "ObservabilityDatabaseParameterGroup",
        ),
        "RagDatabase": ("RagDatabaseName", "RagDatabaseSecurityGroup", "RagDatabaseParameterGroup"),
    }
    for logical_id, (db_name, security_group, parameter_group) in expected.items():
        resource = databases[logical_id]
        properties = resource["Properties"]
        assert resource["DeletionPolicy"] == "Snapshot"
        assert resource["UpdateReplacePolicy"] == "Snapshot"
        assert properties["Engine"] == "postgres"
        assert properties["EngineVersion"] == "DatabaseEngineVersion"
        assert properties["DBInstanceClass"] == "DatabaseInstanceClass"
        assert properties["AllocatedStorage"] == "DatabaseAllocatedStorage"
        assert properties["StorageType"] == "gp3"
        assert properties["StorageEncrypted"] is True
        assert properties["DBName"] == db_name
        assert properties["DBSubnetGroupName"] == "DatabaseSubnetGroup"
        assert properties["DBParameterGroupName"] == parameter_group
        assert properties["VPCSecurityGroups"] == [security_group]
        assert properties["PubliclyAccessible"] is False
        assert properties["MultiAZ"] is False
        assert properties["DeletionProtection"] is True
        assert properties["CopyTagsToSnapshot"] is True


def test_each_rds_has_a_postgres18_force_ssl_parameter_group():
    resources = _template()["Resources"]

    for logical_id in (
        "ServiceDatabaseParameterGroup",
        "ObservabilityDatabaseParameterGroup",
        "RagDatabaseParameterGroup",
    ):
        resource = resources[logical_id]
        assert resource["Type"] == "AWS::RDS::DBParameterGroup"
        assert resource["Properties"]["Family"] == "postgres18"
        assert resource["Properties"]["Parameters"]["rds.force_ssl"] == "1"


def test_database_network_access_is_split_by_runtime_role():
    resources = _template()["Resources"]

    service_ingress = resources["DatabaseSecurityGroup"]["Properties"]["SecurityGroupIngress"]
    observability_ingress = resources["ObservabilityDatabaseSecurityGroup"]["Properties"][
        "SecurityGroupIngress"
    ]
    rag_ingress = resources["RagDatabaseSecurityGroup"]["Properties"]["SecurityGroupIngress"]

    assert [rule["SourceSecurityGroupId"] for rule in service_ingress] == ["AppSecurityGroup"]
    assert [rule["SourceSecurityGroupId"] for rule in observability_ingress] == [
        "AppSecurityGroup"
    ]
    assert [rule["SourceSecurityGroupId"] for rule in rag_ingress] == ["AppSecurityGroup"]

    backoffice = resources["ObservabilityDatabaseBackofficeIngress"]
    assert backoffice["Condition"] == "HasBackofficeOpsSecurityGroup"
    assert backoffice["Properties"]["SourceSecurityGroupId"] == "BackofficeOpsSecurityGroupId"
    assert backoffice["Properties"]["GroupId"] == "ObservabilityDatabaseSecurityGroup"

    ingest = resources["RagDatabaseIngestIngress"]
    assert ingest["Condition"] == "HasRagIngestSecurityGroup"
    assert ingest["Properties"]["SourceSecurityGroupId"] == "RagIngestSecurityGroupId"
    assert ingest["Properties"]["GroupId"] == "RagDatabaseSecurityGroup"


def test_backend_reads_runtime_database_secrets_but_not_master_secrets():
    resources = _template()["Resources"]
    statements = resources["AppInstanceRole"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"
    ]
    secret_statement = next(
        statement
        for statement in statements
        if "secretsmanager:GetSecretValue" in statement["Action"]
    )
    allowed_resources = secret_statement["Resource"]

    assert allowed_resources[:3] == [
        ["UseSeparatedDatabaseRuntime", "ServiceAppDatabaseSecret", "DatabaseSecret"],
        [
            "UseSeparatedDatabaseRuntime",
            "ObservabilityWriterDatabaseSecret",
            "AWS::NoValue",
        ],
        ["UseSeparatedDatabaseRuntime", "RagRuntimeDatabaseSecret", "AWS::NoValue"],
    ]

    scalar_resources = _scalar_values(allowed_resources)
    for runtime_secret in (
        "ServiceAppDatabaseSecret",
        "ObservabilityWriterDatabaseSecret",
        "RagRuntimeDatabaseSecret",
    ):
        assert runtime_secret in scalar_resources
        assert resources[runtime_secret]["Properties"]["GenerateSecretString"][
            "SecretStringTemplate"
        ]

    assert "DatabaseSecret" in scalar_resources
    for master_secret in ("ObservabilityDatabaseSecret", "RagDatabaseSecret"):
        assert master_secret not in scalar_resources

    assert "DeletionPolicy" not in resources["DatabaseSecret"]
    assert "UpdateReplacePolicy" not in resources["DatabaseSecret"]
    for master_secret in ("ObservabilityDatabaseSecret", "RagDatabaseSecret"):
        assert resources[master_secret]["DeletionPolicy"] == "Retain"

    for generated_secret in (
        "ObservabilityDatabaseSecret",
        "RagDatabaseSecret",
        "ServiceAppDatabaseSecret",
        "ObservabilityWriterDatabaseSecret",
        "RagRuntimeDatabaseSecret",
    ):
        assert "Name" not in resources[generated_secret]["Properties"]


def test_user_data_injects_three_database_boundaries_and_external_opensearch_secret():
    template = _template()
    fn_sub = template["Resources"]["AppLaunchTemplate"]["Properties"]["LaunchTemplateData"][
        "UserData"
    ]["Fn::Base64"]
    user_data, substitutions = fn_sub
    bootstrap = substitutions["DatabaseRuntimeBootstrapBlock"]
    env_block = substitutions["SeparatedDatabaseEnvBlock"]

    assert bootstrap[0] == "UseSeparatedDatabaseRuntime"
    separated_bootstrap = bootstrap[1]
    legacy_bootstrap = bootstrap[2]
    assert "--secret-id ${ServiceAppDatabaseSecret}" in separated_bootstrap
    assert "--secret-id ${ObservabilityWriterDatabaseSecret}" in separated_bootstrap
    assert "--secret-id ${RagRuntimeDatabaseSecret}" in separated_bootstrap
    assert "--secret-id ${DatabaseSecret}" not in separated_bootstrap
    assert "?sslmode=require" in separated_bootstrap
    assert "--secret-id ${DatabaseSecret}" in legacy_bootstrap
    assert "--secret-id ${ObservabilityWriterDatabaseSecret}" not in legacy_bootstrap
    assert "--secret-id ${RagRuntimeDatabaseSecret}" not in legacy_bootstrap

    assert env_block == [
        "UseSeparatedDatabaseRuntime",
        "OBSERVABILITY_DATABASE_URL=$OBSERVABILITY_DATABASE_URL\n"
        "RAG_DATABASE_URL=$RAG_DATABASE_URL\n",
        "OBSERVABILITY_DATABASE_URL=$OBSERVABILITY_DATABASE_URL\n"
        "RAG_DATABASE_URL=$RAG_DATABASE_URL\n",
    ]
    assert "${DatabaseRuntimeBootstrapBlock}" in user_data
    assert "${SeparatedDatabaseEnvBlock}" in user_data

    assert 'EXTERNAL_OPENSEARCH_SECRET_ARN="${ExternalOpenSearchCredentialsSecretArn}"' in user_data
    assert "OPENSEARCH_SECRET_REGION=$(printf '%s'" in user_data
    assert '--region "$OPENSEARCH_SECRET_REGION"' in user_data
    assert "OPENSEARCH_USERNAME=$OPENSEARCH_USERNAME" in user_data
    assert "OPENSEARCH_PASSWORD=$OPENSEARCH_PASSWORD" in user_data
    assert "https://user:" not in user_data
    assert '["LEGACY_OBSERVABILITY_DATABASE_URL"]' in legacy_bootstrap
    assert '["LEGACY_RAG_DATABASE_URL"]' in legacy_bootstrap
    assert "${LegacyObservabilityDatabaseUrl}" not in user_data
    assert "${LegacyRagDatabaseUrl}" not in user_data

    all_shell = "\n".join((user_data, separated_bootstrap, legacy_bootstrap))
    python_commands = re.findall(r"python3 -c '([^']+)'", all_shell)
    assert python_commands
    for command in python_commands:
        compile(command, "<cloudformation-user-data>", "exec")


def test_separated_database_runtime_requires_an_explicit_cutover():
    template = _template()

    parameter = template["Parameters"]["SeparatedDatabaseCutoverEnabled"]
    parameters = template["Parameters"]
    assert parameter["Default"] == "false"
    assert parameter["AllowedValues"] == ["true", "false"]
    assert template["Conditions"]["UseSeparatedDatabaseRuntime"] == [
        "SeparatedDatabaseCutoverEnabled",
        "true",
    ]
    for name in ("LegacyObservabilityDatabaseUrl", "LegacyRagDatabaseUrl"):
        assert parameters[name]["Default"] == ""
        assert parameters[name]["NoEcho"] is True

    rule = template["Rules"]["LegacyDatabaseConfiguration"]["Assertions"][0]
    assert {
        "SeparatedDatabaseCutoverEnabled",
        "LegacyObservabilityDatabaseUrl",
        "LegacyRagDatabaseUrl",
        "true",
    }.issubset(_scalar_values(rule["Assert"]))
    assert "required" in rule["AssertDescription"]

    app_secret = template["Resources"]["AppSecret"]["Properties"]["SecretString"]
    assert '"LEGACY_OBSERVABILITY_DATABASE_URL": "${LegacyObservabilityDatabaseUrl}"' in app_secret
    assert '"LEGACY_RAG_DATABASE_URL": "${LegacyRagDatabaseUrl}"' in app_secret


def test_backend_deploy_wires_staged_database_and_external_opensearch_inputs():
    workflow = _deploy_workflow()
    deploy_job = workflow["jobs"]["deploy"]
    build_job = workflow["jobs"]["build"]
    validation = next(
        step
        for step in deploy_job["steps"]
        if step.get("name") == "Validate database cutover and external OpenSearch inputs"
    )
    deploy_script = next(
        step["run"]
        for step in deploy_job["steps"]
        if step.get("name") == "Deploy CloudFormation"
    )
    deploy_step = next(
        step
        for step in deploy_job["steps"]
        if step.get("name") == "Deploy CloudFormation"
    )

    assert "RAG_DATABASE_URL" not in str(build_job)
    assert "OBSERVABILITY_DATABASE_URL" not in str(build_job)
    assert validation["env"]["LEGACY_RAG_DATABASE_URL"] == "${{ secrets.RAG_DATABASE_URL }}"
    assert validation["env"]["LEGACY_OBSERVABILITY_DATABASE_URL"] == (
        "${{ secrets.OBSERVABILITY_DATABASE_URL }}"
    )
    assert validation["env"]["CUTOVER_ENABLED"] == (
        "${{ vars.SEPARATED_DATABASE_CUTOVER_ENABLED || 'false' }}"
    )
    assert '[ "$CUTOVER_ENABLED" = "false" ]' in validation["run"]
    assert 'ENABLE_OPENSEARCH must be false' in validation["run"]

    assert deploy_step["env"]["LEGACY_RAG_DATABASE_URL"] == "${{ secrets.RAG_DATABASE_URL }}"
    assert deploy_step["env"]["LEGACY_OBSERVABILITY_DATABASE_URL"] == (
        "${{ secrets.OBSERVABILITY_DATABASE_URL }}"
    )
    assert "${{ secrets.RAG_DATABASE_URL }}" not in deploy_script
    assert "${{ secrets.OBSERVABILITY_DATABASE_URL }}" not in deploy_script
    expected_parameters = (
        'SeparatedDatabaseCutoverEnabled="$CUTOVER_ENABLED"',
        'LegacyObservabilityDatabaseUrl="$LEGACY_OBSERVABILITY_DATABASE_URL"',
        'LegacyRagDatabaseUrl="$LEGACY_RAG_DATABASE_URL"',
        'ExternalOpenSearchHost="$EXTERNAL_OPENSEARCH_HOST"',
        'ExternalOpenSearchCredentialsSecretArn="$EXTERNAL_OPENSEARCH_CREDENTIALS_SECRET_ARN"',
        'BackofficeOpsSecurityGroupId="$BACKOFFICE_OPS_SECURITY_GROUP_ID"',
        'RagIngestSecurityGroupId="$RAG_INGEST_SECURITY_GROUP_ID"',
    )
    for expected in expected_parameters:
        assert expected in deploy_script


def test_external_opensearch_host_and_credentials_must_be_selected_together():
    template = _template()
    parameters = template["Parameters"]
    rules = template["Rules"]

    host_pattern = parameters["ExternalOpenSearchHost"]["AllowedPattern"]
    secret_pattern = parameters["ExternalOpenSearchCredentialsSecretArn"]["AllowedPattern"]
    assert re.fullmatch(host_pattern, "https://example.us-east-1.bonsaisearch.net")
    assert re.fullmatch(host_pattern, "https://search.internal:443")
    assert not re.fullmatch(host_pattern, "https://example.com/path")
    assert not re.fullmatch(host_pattern, "https://bad host.example")

    valid_secret_arn = (
        "arn:aws:secretsmanager:us-east-1:123456789012:"
        "secret:a360/opensearch-credentials-AbCd12"
    )
    assert parameters["ExternalOpenSearchCredentialsSecretArn"]["Default"] == ""
    assert re.fullmatch(secret_pattern, valid_secret_arn)
    assert not re.fullmatch(
        secret_pattern,
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:*",
    )
    assert not re.fullmatch(
        secret_pattern,
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:bad name",
    )
    assert not re.fullmatch(
        secret_pattern,
        "arn:aws:secretsmanager:us-east-1:123456789012:"
        "secret:a360/opensearch-credentials",
    )
    assert len(rules["ExternalOpenSearchConfiguration"]["Assertions"]) == 2
