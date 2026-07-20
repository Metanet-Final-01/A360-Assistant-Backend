"""Shared fixtures and helpers for testing the infra/ CloudFormation templates,
the params.*.json parameter files, and the .github/workflows/backend-deploy.yml
GitHub Actions workflow that were added/changed in this PR.

CloudFormation YAML uses short-form intrinsic function tags (``!Ref``, ``!Sub``,
``!GetAtt``, ``!If``, ...) that plain PyYAML does not understand out of the box.
``CfnLoader`` below teaches PyYAML to translate those tags into their standard
long-form ``Fn::*``/``Ref``/``Condition`` dict equivalents, exactly as
CloudFormation itself interprets them, so tests can make plain dict/list
assertions against the parsed template.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
INFRA_DIR = REPO_ROOT / "infra"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

BACKEND_TEMPLATE_PATH = INFRA_DIR / "a360-backend-private.yml"
RAG_SCHEDULER_TEMPLATE_PATH = INFRA_DIR / "rag-ingest-scheduler-sqs.yaml"
CHEAP_PARAMS_PATH = INFRA_DIR / "params.deploy-test-cheap.example.json"
DEFAULT_PARAMS_PATH = INFRA_DIR / "params.example.json"
DEPLOY_WORKFLOW_PATH = WORKFLOWS_DIR / "backend-deploy.yml"


class CfnLoader(yaml.SafeLoader):
    """YAML SafeLoader that resolves CloudFormation short-form intrinsic
    function tags into their long-form dict representation."""


def _cfn_multi_constructor(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node):
    if tag_suffix == "Ref":
        return {"Ref": loader.construct_scalar(node)}
    if tag_suffix == "Condition":
        return {"Condition": loader.construct_scalar(node)}
    if tag_suffix == "GetAtt":
        if isinstance(node, yaml.ScalarNode):
            value = loader.construct_scalar(node)
            return {"Fn::GetAtt": value.split(".", 1)}
        return {"Fn::GetAtt": loader.construct_sequence(node, deep=True)}

    fn_name = f"Fn::{tag_suffix}"
    if isinstance(node, yaml.ScalarNode):
        return {fn_name: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {fn_name: loader.construct_sequence(node, deep=True)}
    return {fn_name: loader.construct_mapping(node, deep=True)}


CfnLoader.add_multi_constructor("!", _cfn_multi_constructor)


def load_cfn_yaml(path: Path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=CfnLoader)


def get_on_triggers(workflow_doc: dict):
    """Return the ``on:`` trigger mapping.

    PyYAML resolves the bare word ``on`` to the boolean ``True`` under the
    YAML 1.1 spec that (Safe)Loader implements, so the parsed workflow dict
    has a ``True`` key (not the string ``"on"``) for GitHub Actions' ``on:``
    section. This helper hides that gotcha from the tests.
    """
    if "on" in workflow_doc:
        return workflow_doc["on"]
    return workflow_doc[True]


@pytest.fixture(scope="session")
def backend_template() -> dict:
    return load_cfn_yaml(BACKEND_TEMPLATE_PATH)


@pytest.fixture(scope="session")
def backend_template_text() -> str:
    return BACKEND_TEMPLATE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def rag_scheduler_template() -> dict:
    return load_cfn_yaml(RAG_SCHEDULER_TEMPLATE_PATH)


@pytest.fixture(scope="session")
def cheap_params() -> list:
    return json.loads(CHEAP_PARAMS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def default_params() -> list:
    return json.loads(DEFAULT_PARAMS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def deploy_workflow() -> dict:
    return load_cfn_yaml(DEPLOY_WORKFLOW_PATH)


@pytest.fixture(scope="session")
def deploy_workflow_text() -> str:
    return DEPLOY_WORKFLOW_PATH.read_text(encoding="utf-8")