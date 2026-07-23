import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "infra" / "a360-backend-private.yml"


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
        "# Separated database runtime cutover is staged but not enabled.",
    ]
    assert "${DatabaseRuntimeBootstrapBlock}" in user_data
    assert "${SeparatedDatabaseEnvBlock}" in user_data

    assert 'EXTERNAL_OPENSEARCH_SECRET_ARN="${ExternalOpenSearchCredentialsSecretArn}"' in user_data
    assert "OPENSEARCH_SECRET_REGION=$(printf '%s'" in user_data
    assert '--region "$OPENSEARCH_SECRET_REGION"' in user_data
    assert "OPENSEARCH_USERNAME=$OPENSEARCH_USERNAME" in user_data
    assert "OPENSEARCH_PASSWORD=$OPENSEARCH_PASSWORD" in user_data
    assert "https://user:" not in user_data

    all_shell = "\n".join((user_data, separated_bootstrap, legacy_bootstrap))
    python_commands = re.findall(r"python3 -c '([^']+)'", all_shell)
    assert python_commands
    for command in python_commands:
        compile(command, "<cloudformation-user-data>", "exec")


def test_separated_database_runtime_requires_an_explicit_cutover():
    template = _template()

    parameter = template["Parameters"]["SeparatedDatabaseCutoverEnabled"]
    assert parameter["Default"] == "false"
    assert parameter["AllowedValues"] == ["true", "false"]
    assert template["Conditions"]["UseSeparatedDatabaseRuntime"] == [
        "SeparatedDatabaseCutoverEnabled",
        "true",
    ]


def test_external_opensearch_host_and_credentials_must_be_selected_together():
    template = _template()
    parameters = template["Parameters"]
    rules = template["Rules"]

    assert parameters["ExternalOpenSearchHost"]["AllowedPattern"] == "^$|^https://[^@/?#]+$"
    assert parameters["ExternalOpenSearchCredentialsSecretArn"]["Default"] == ""
    assert "secretsmanager" in parameters["ExternalOpenSearchCredentialsSecretArn"][
        "AllowedPattern"
    ]
    assert len(rules["ExternalOpenSearchConfiguration"]["Assertions"]) == 2
