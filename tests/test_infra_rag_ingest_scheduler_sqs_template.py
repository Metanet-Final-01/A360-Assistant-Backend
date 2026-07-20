"""Tests for the new infra/rag-ingest-scheduler-sqs.yaml nested CloudFormation
template (EventBridge Scheduler -> SQS trigger for the RAG ingest consumer).
"""
from __future__ import annotations


def _params(template):
    return template["Parameters"]


def _resources(template):
    return template["Resources"]


def _outputs(template):
    return template["Outputs"]


def test_template_has_expected_top_level_sections(rag_scheduler_template):
    assert rag_scheduler_template["AWSTemplateFormatVersion"] == "2010-09-09"
    assert "EventBridge Scheduler" in rag_scheduler_template["Description"]
    for section in ("Parameters", "Conditions", "Resources", "Outputs"):
        assert section in rag_scheduler_template


def test_parameter_defaults(rag_scheduler_template):
    params = _params(rag_scheduler_template)

    assert params["ScheduleName"]["Default"] == "a360-rag-ingest-test"
    assert params["ScheduleExpression"]["Default"] == "rate(1 minute)"
    assert params["ScheduleTimezone"]["Default"] == "Asia/Seoul"

    ingest_option = params["IngestOption"]
    assert ingest_option["Type"] == "Number"
    assert ingest_option["Default"] == 3
    assert ingest_option["MinValue"] == 1
    assert ingest_option["MaxValue"] == 3

    clean = params["Clean"]
    assert clean["Default"] == "false"
    assert clean["AllowedValues"] == ["true", "false"]

    role_name = params["AppInstanceRoleName"]
    assert role_name["Default"] == ""


def test_has_app_instance_role_name_condition(rag_scheduler_template):
    conditions = rag_scheduler_template["Conditions"]
    assert conditions["HasAppInstanceRoleName"] == {
        "Fn::Not": [{"Fn::Equals": [{"Ref": "AppInstanceRoleName"}, ""]}]
    }


def test_queue_resource_configuration(rag_scheduler_template):
    queue = _resources(rag_scheduler_template)["RagIngestQueue"]
    assert queue["Type"] == "AWS::SQS::Queue"
    props = queue["Properties"]
    assert props["QueueName"] == {"Fn::Sub": "${ScheduleName}-queue"}
    assert props["VisibilityTimeout"] == 300
    assert props["MessageRetentionPeriod"] == 1209600


def test_scheduler_role_trusts_scheduler_service_and_can_send_message(
    rag_scheduler_template,
):
    role = _resources(rag_scheduler_template)["RagIngestSchedulerRole"]
    assert role["Type"] == "AWS::IAM::Role"
    props = role["Properties"]
    assert props["RoleName"] == {"Fn::Sub": "${ScheduleName}-scheduler-role"}

    assume_statement = props["AssumeRolePolicyDocument"]["Statement"][0]
    assert assume_statement["Effect"] == "Allow"
    assert assume_statement["Principal"] == {"Service": "scheduler.amazonaws.com"}
    assert assume_statement["Action"] == "sts:AssumeRole"

    send_statement = props["Policies"][0]["PolicyDocument"]["Statement"][0]
    assert send_statement["Effect"] == "Allow"
    assert send_statement["Action"] == "sqs:SendMessage"
    assert send_statement["Resource"] == {"Fn::GetAtt": ["RagIngestQueue", "Arn"]}


def test_consumer_policy_is_conditional_and_grants_consume_permissions(
    rag_scheduler_template,
):
    resources = _resources(rag_scheduler_template)
    policy = resources["RagIngestConsumerPolicy"]
    assert policy["Type"] == "AWS::IAM::Policy"
    assert policy["Condition"] == "HasAppInstanceRoleName"

    props = policy["Properties"]
    assert props["Roles"] == [{"Ref": "AppInstanceRoleName"}]

    statement = props["PolicyDocument"]["Statement"][0]
    assert statement["Effect"] == "Allow"
    assert set(statement["Action"]) == {
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes",
        "sqs:ChangeMessageVisibility",
    }
    assert statement["Resource"] == {"Fn::GetAtt": ["RagIngestQueue", "Arn"]}


def test_schedule_resource_targets_queue_with_retry_policy(rag_scheduler_template):
    schedule = _resources(rag_scheduler_template)["RagIngestSchedule"]
    assert schedule["Type"] == "AWS::Scheduler::Schedule"
    props = schedule["Properties"]
    assert props["Name"] == {"Ref": "ScheduleName"}
    assert props["ScheduleExpression"] == {"Ref": "ScheduleExpression"}
    assert props["ScheduleExpressionTimezone"] == {"Ref": "ScheduleTimezone"}
    assert props["FlexibleTimeWindow"] == {"Mode": "OFF"}
    assert props["State"] == "ENABLED"

    target = props["Target"]
    assert target["Arn"] == {"Fn::GetAtt": ["RagIngestQueue", "Arn"]}
    assert target["RoleArn"] == {"Fn::GetAtt": ["RagIngestSchedulerRole", "Arn"]}
    assert target["RetryPolicy"] == {
        "MaximumEventAgeInSeconds": 3600,
        "MaximumRetryAttempts": 2,
    }


def test_schedule_target_input_payload_shape(rag_scheduler_template):
    schedule = _resources(rag_scheduler_template)["RagIngestSchedule"]
    input_template = schedule["Properties"]["Target"]["Input"]["Fn::Sub"]
    assert '"type": "rag_ingest"' in input_template
    assert '"schedule_id": "${ScheduleName}"' in input_template
    assert '"option": ${IngestOption}' in input_template
    assert '"clean": ${Clean}' in input_template


def test_outputs_reference_expected_resources(rag_scheduler_template):
    outputs = _outputs(rag_scheduler_template)
    assert outputs["QueueUrl"]["Value"] == {"Ref": "RagIngestQueue"}
    assert outputs["QueueArn"]["Value"] == {"Fn::GetAtt": ["RagIngestQueue", "Arn"]}
    assert outputs["SchedulerRoleArn"]["Value"] == {
        "Fn::GetAtt": ["RagIngestSchedulerRole", "Arn"]
    }
    assert outputs["ScheduleName"]["Value"] == {"Ref": "RagIngestSchedule"}


def test_no_unconditioned_resource_depends_on_missing_role_name(rag_scheduler_template):
    # Sanity/negative check: only the consumer policy should require
    # AppInstanceRoleName; the queue, scheduler role and schedule must be
    # creatable even when AppInstanceRoleName is left blank (default).
    resources = _resources(rag_scheduler_template)
    conditioned = {
        name for name, res in resources.items() if res.get("Condition")
    }
    assert conditioned == {"RagIngestConsumerPolicy"}