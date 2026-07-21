from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

import assurance.change as change_api
from assurance.change.checker import (
    AssuranceError,
    _AssuranceRunner,
    canonical_digest,
    load_policy,
    write_error_report,
)
from assurance.change.dependency_checks import (
    _combine_rule_status,
    _license_matches,
    _license_status,
    _local_roots,
    _overall_dependency_status,
    derive_risk_profiles,
    parse_imports,
)
from assurance.change.foundation import CONTROL_ORDER, GitRepository
from assurance.change.schema_validation import SchemaValidationError, validate_json_schema
from assurance.change.cli import main as cli_main
from tests.change_assurance_adapter import FixtureDependencyEnvironment


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "change_assurance" / "scenarios.json"
MANIFEST_SCHEMA = ROOT / "assurance" / "change" / "schemas" / "change-manifest.schema.json"
REPORT_SCHEMA = ROOT / "assurance" / "change" / "schemas" / "assurance-report.schema.json"
POLICY_SCHEMA = ROOT / "assurance" / "change" / "schemas" / "dependency-policy.schema.json"
WORKFLOW = ROOT / ".github" / "workflows" / "change-assurance-observe.yml"
FIXED_NOW = datetime(2026, 7, 16, tzinfo=timezone.utc)
FIXED_GIT_DATE = "2026-07-16T00:00:00Z"


def _git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "A360 fixture",
            "GIT_AUTHOR_EMAIL": "fixture@invalid.local",
            "GIT_COMMITTER_NAME": "A360 fixture",
            "GIT_COMMITTER_EMAIL": "fixture@invalid.local",
            "GIT_AUTHOR_DATE": FIXED_GIT_DATE,
            "GIT_COMMITTER_DATE": FIXED_GIT_DATE,
        }
    )
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


def _replace_files(repo: Path, files: dict[str, str]) -> None:
    current = {
        path.relative_to(repo).as_posix()
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    for path in current - set(files):
        (repo / path).unlink()
    for relative, content in files.items():
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8", newline="\n")


def _fixture_repo(root: Path, scenario: dict) -> tuple[Path, str, str]:
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "--quiet")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "core.autocrlf", "false")
    _replace_files(repo, scenario["base_files"])
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _replace_files(repo, scenario["head_files"])
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "head")
    head = _git(repo, "rev-parse", "HEAD")
    return repo, base, head


def _policy(scenario: dict) -> dict:
    import_map = {
        root: distributions[0]
        for root, distributions in scenario["environment"].get("import_map", {}).items()
        if distributions
    }
    if not import_map:
        import_map["fixture_sentinel"] = "fixture-sentinel"
    return {
        "schema_version": "1.0",
        "rollout_mode": "observe",
        "policy_decision_state": "approved_fixture",
        "requirement_files": ["requirements.txt", "requirements-dev.txt"],
        "dependency_paths": ["requirements.txt", "requirements-dev.txt", "pyproject.toml"],
        "local_roots": ["app", "assurance", "scripts", "tests"],
        "protected_paths": [
            ".github/workflows/",
            ".github/CODEOWNERS",
            "assurance/change/",
            "tests/",
            "app/agent/",
        ],
        "import_distribution_map": import_map,
        "approved_additions": {},
        "license_policy": {
            "allowed_spdx": [
                "Apache-2.0",
                "BSD-2-Clause",
                "BSD-3-Clause",
                "ISC",
                "MIT",
                "MPL-2.0",
                "PSF-2.0",
            ],
            "approved_exceptions": {},
        },
        "vulnerability_policy": {
            "source": "deterministic fixture",
            "snapshot_generated_at": "2026-07-16T00:00:00Z",
            "max_age_days": 30,
            "deny_at_or_above": "high",
            "packages": scenario["snapshot"],
        },
    }


def _run_scenario(tmp_path: Path, scenario: dict, output_name: str = "out") -> tuple[dict, Path]:
    repo, base, head = _fixture_repo(tmp_path, scenario)
    policy = _policy(scenario)
    output = tmp_path / output_name
    report = _AssuranceRunner(
        repo_root=repo,
        base_sha=base,
        head_sha=head,
        repository="Metanet-Final-01/fixture",
        output=output,
        policy=policy,
        policy_uri="fixture-policy.json",
        policy_digest=canonical_digest(policy),
        environment=FixtureDependencyEnvironment(scenario["environment"]),
        now=FIXED_NOW,
    ).run()
    return report, output


def _load_scenarios() -> dict:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def _assert_artifact_digests(output: Path) -> None:
    for line in (output / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        expected, name = line.split("  ", 1)
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == expected


def test_normal_and_adversarial_fixtures_have_expected_decisions(tmp_path: Path) -> None:
    for index, (name, scenario) in enumerate(_load_scenarios().items()):
        report, output = _run_scenario(tmp_path / f"case-{index}", scenario)
        assert report["assurance_decision"] == scenario["expected_decision"], name
        assert report["business_outcome"] == {
            "decision": "not_evaluated",
            "changed_by_assurance": False,
        }
        assert report["enforcement"] == {"mode": "observe", "blocks_merge": False}
        _assert_artifact_digests(output)


def test_good_fixture_outputs_validate_against_json_schemas(tmp_path: Path) -> None:
    scenario = _load_scenarios()["good_import"]
    report, output = _run_scenario(tmp_path, scenario)
    manifest = json.loads((output / "change-manifest.json").read_text(encoding="utf-8"))
    manifest_schema = json.loads(MANIFEST_SCHEMA.read_text(encoding="utf-8"))
    report_schema = json.loads(REPORT_SCHEMA.read_text(encoding="utf-8"))
    policy_schema = json.loads(POLICY_SCHEMA.read_text(encoding="utf-8"))
    production_policy = json.loads(
        (ROOT / "assurance" / "change" / "policy" / "dependency-policy.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(manifest_schema, format_checker=FormatChecker()).validate(manifest)
    Draft202012Validator(report_schema, format_checker=FormatChecker()).validate(report)
    Draft202012Validator(policy_schema, format_checker=FormatChecker()).validate(production_policy)


def test_fixture_runs_are_byte_deterministic(tmp_path: Path) -> None:
    scenario = _load_scenarios()["good_import"]
    repo, base, head = _fixture_repo(tmp_path, scenario)
    policy = _policy(scenario)

    def run(output: Path) -> None:
        _AssuranceRunner(
            repo_root=repo,
            base_sha=base,
            head_sha=head,
            repository="Metanet-Final-01/fixture",
            output=output,
            policy=policy,
            policy_uri="fixture-policy.json",
            policy_digest=canonical_digest(policy),
            environment=FixtureDependencyEnvironment(scenario["environment"]),
            now=FIXED_NOW,
        ).run()

    first, second = tmp_path / "first", tmp_path / "second"
    run(first)
    run(second)
    first_files = {path.name: path.read_bytes() for path in first.iterdir() if path.is_file()}
    second_files = {path.name: path.read_bytes() for path in second.iterdir() if path.is_file()}
    assert first_files == second_files


def test_missing_snapshot_is_unassured_and_never_allow(tmp_path: Path) -> None:
    scenario = _load_scenarios()["good_import"]
    scenario["snapshot"] = {}
    report, _ = _run_scenario(tmp_path, scenario)
    assert report["assurance_decision"] == "unassured"
    ch04 = next(item for item in report["controls"] if item["control_id"] == "CH-04")
    assert ch04["status"] == "unassured"


def test_unapproved_operational_policy_cannot_allow_external_import(tmp_path: Path) -> None:
    scenario = _load_scenarios()["good_import"]
    repo, base, head = _fixture_repo(tmp_path, scenario)
    policy = _policy(scenario)
    policy["policy_decision_state"] = "decision_needed"
    report = _AssuranceRunner(
        repo_root=repo,
        base_sha=base,
        head_sha=head,
        repository="Metanet-Final-01/fixture",
        output=tmp_path / "out",
        policy=policy,
        policy_uri="fixture-policy.json",
        policy_digest=canonical_digest(policy),
        environment=FixtureDependencyEnvironment(scenario["environment"]),
        now=FIXED_NOW,
    ).run()
    assert report["assurance_decision"] == "unassured"
    ch04 = next(item for item in report["controls"] if item["control_id"] == "CH-04")
    assert ch04["status"] == "unassured"


def test_protected_oracle_change_requires_separate_review(tmp_path: Path) -> None:
    scenario = {
        "base_files": {
            "requirements.txt": "",
            "tests/test_example.py": "def test_value():\n    assert 1 == 1\n",
        },
        "head_files": {
            "requirements.txt": "",
            "tests/test_example.py": "def test_value():\n    assert 2 == 2\n",
        },
        "environment": {"inventory": {}, "import_map": {}, "imports": {}, "distributions": {}},
        "snapshot": {},
        "expected_decision": "unassured",
    }
    report, _ = _run_scenario(tmp_path, scenario)
    assert report["assurance_decision"] == "unassured"
    ch06 = next(item for item in report["controls"] if item["control_id"] == "CH-06")
    assert ch06["reason_code"] == "PROTECTED_ORACLE_REVIEW_REQUIRED"


def test_rename_out_of_protected_path_keeps_old_path_evidence(tmp_path: Path) -> None:
    content = "def test_value():\n    assert 1 == 1\n"
    scenario = {
        "base_files": {"requirements.txt": "", "tests/test_example.py": content},
        "head_files": {"requirements.txt": "", "app/example.py": content},
        "environment": {"inventory": {}, "import_map": {}, "imports": {}, "distributions": {}},
        "snapshot": {},
        "expected_decision": "unassured",
    }
    report, output = _run_scenario(tmp_path, scenario)
    protected = json.loads(
        (output / "protected-change-evidence.json").read_text(encoding="utf-8")
    )
    assert report["assurance_decision"] == "unassured"
    assert "tests/test_example.py" in protected["protected_paths_changed"]


def test_rename_keeps_old_path_risk_profile() -> None:
    profiles = derive_risk_profiles(
        [{"status": "R100", "old_path": "app/api/legacy.py", "path": "docs/legacy.md"}],
        _policy(_load_scenarios()["good_import"]),
    )
    assert "api_contract" in profiles
    assert "source" in profiles
    assert "docs" in profiles


def test_added_package_install_fallback_is_denied(tmp_path: Path) -> None:
    scenario = {
        "base_files": {
            "requirements.txt": "",
            "task.py": "def run():\n    return 'ready'\n",
        },
        "head_files": {
            "requirements.txt": "",
            "task.py": (
                "import subprocess\n\n"
                "def run():\n"
                "    return subprocess.run(['pip', 'install', 'invented-sdk'])\n"
            ),
        },
        "environment": {"inventory": {}, "import_map": {}, "imports": {}, "distributions": {}},
        "snapshot": {},
        "expected_decision": "deny",
    }
    report, output = _run_scenario(tmp_path, scenario)
    evidence = json.loads((output / "dependency-evidence.json").read_text(encoding="utf-8"))
    assert report["assurance_decision"] == "deny"
    assert evidence["rules"]["dep.no_silent_install"]["status"] == "fail"


@pytest.mark.parametrize("container_file", ["Dockerfile", "Containerfile.dev"])
def test_container_package_install_fallback_is_denied(
    tmp_path: Path, container_file: str
) -> None:
    scenario = {
        "base_files": {"requirements.txt": "", container_file: "FROM python:3.11\n"},
        "head_files": {
            "requirements.txt": "",
            container_file: "FROM python:3.11\nRUN pip install invented-sdk\n",
        },
        "environment": {"inventory": {}, "import_map": {}, "imports": {}, "distributions": {}},
        "snapshot": {},
        "expected_decision": "deny",
    }
    report, output = _run_scenario(tmp_path, scenario)
    evidence = json.loads((output / "dependency-evidence.json").read_text(encoding="utf-8"))
    assert report["assurance_decision"] == "deny"
    assert evidence["rules"]["dep.no_silent_install"]["status"] == "fail"


@pytest.mark.parametrize(
    "command",
    [
        "/usr/bin/pip install invented-sdk",
        ".venv/bin/pip3 install invented-sdk",
        r"C:\venv\Scripts\pip.exe install invented-sdk",
    ],
)
def test_path_qualified_pip_install_is_denied(tmp_path: Path, command: str) -> None:
    scenario = {
        "base_files": {"requirements.txt": "", "Dockerfile": "FROM python:3.11\n"},
        "head_files": {
            "requirements.txt": "",
            "Dockerfile": f"FROM python:3.11\nRUN {command}\n",
        },
        "environment": {"inventory": {}, "import_map": {}, "imports": {}, "distributions": {}},
        "snapshot": {},
        "expected_decision": "deny",
    }
    report, output = _run_scenario(tmp_path, scenario)
    evidence = json.loads((output / "dependency-evidence.json").read_text(encoding="utf-8"))
    assert report["assurance_decision"] == "deny"
    assert evidence["rules"]["dep.no_silent_install"]["status"] == "fail"


def test_rename_into_sensitive_path_scans_the_complete_head_blob(tmp_path: Path) -> None:
    content = "FROM python:3.11\nRUN pip install invented-sdk\n"
    scenario = {
        "base_files": {"requirements.txt": "", "notes.txt": content},
        "head_files": {"requirements.txt": "", "Dockerfile": content},
        "environment": {"inventory": {}, "import_map": {}, "imports": {}, "distributions": {}},
        "snapshot": {},
        "expected_decision": "deny",
    }
    report, output = _run_scenario(tmp_path, scenario)
    evidence = json.loads((output / "dependency-evidence.json").read_text(encoding="utf-8"))
    assert report["assurance_decision"] == "deny"
    assert evidence["rules"]["dep.no_silent_install"]["status"] == "fail"


def test_dependency_removal_without_remaining_import_is_allowed(tmp_path: Path) -> None:
    scenario = {
        "base_files": {
            "requirements.txt": "demo-sdk==1.0.0\n",
            "task.py": "import demo_sdk\n",
        },
        "head_files": {
            "requirements.txt": "",
            "task.py": "def run():\n    return 'ready'\n",
        },
        "environment": {
            "inventory": {},
            "import_map": {"demo_sdk": ["demo-sdk"]},
            "imports": {},
            "distributions": {},
        },
        "snapshot": {},
        "expected_decision": "allow_candidate",
    }
    report, _ = _run_scenario(tmp_path, scenario)
    assert report["assurance_decision"] == "allow_candidate"


def test_dependency_removal_ignores_a_same_named_local_package(tmp_path: Path) -> None:
    scenario = {
        "base_files": {
            "requirements.txt": "demo-sdk==1.0.0\n",
            "task.py": "import demo_sdk\n",
        },
        "head_files": {
            "requirements.txt": "",
            "task.py": "import demo_sdk\n",
            "demo_sdk/__init__.py": "def run():\n    return 'local'\n",
        },
        "environment": {
            "inventory": {},
            "import_map": {"demo_sdk": ["demo-sdk"]},
            "imports": {},
            "distributions": {},
        },
        "snapshot": {},
        "expected_decision": "allow_candidate",
    }
    report, _ = _run_scenario(tmp_path, scenario)
    assert report["assurance_decision"] == "allow_candidate"


def test_dependency_removal_with_remaining_import_is_denied(tmp_path: Path) -> None:
    scenario = {
        "base_files": {
            "requirements.txt": "demo-sdk==1.0.0\n",
            "task.py": "import demo_sdk\n",
        },
        "head_files": {
            "requirements.txt": "",
            "task.py": "import demo_sdk\n\ndef run():\n    return 'ready'\n",
        },
        "environment": {
            "inventory": {},
            "import_map": {"demo_sdk": ["demo-sdk"]},
            "imports": {},
            "distributions": {},
        },
        "snapshot": {},
        "expected_decision": "deny",
    }
    report, _ = _run_scenario(tmp_path, scenario)
    assert report["assurance_decision"] == "deny"


def test_mapped_external_import_without_requirement_is_denied(tmp_path: Path) -> None:
    scenario = {
        "base_files": {"requirements.txt": "", "task.py": "def run():\n    return 'ready'\n"},
        "head_files": {
            "requirements.txt": "",
            "task.py": "import demo_sdk\n\ndef run():\n    return demo_sdk.run()\n",
        },
        "environment": {
            "inventory": {},
            "import_map": {"demo_sdk": ["demo-sdk"]},
            "imports": {"demo_sdk:": True},
            "distributions": {},
        },
        "snapshot": {},
        "expected_decision": "deny",
    }
    report, output = _run_scenario(tmp_path, scenario)
    evidence = json.loads((output / "dependency-evidence.json").read_text(encoding="utf-8"))
    assert report["assurance_decision"] == "deny"
    assert evidence["package_checks"]["demo-sdk"]["allowlist"]["status"] == "fail"
    assert "not declared" in evidence["package_checks"]["demo-sdk"]["allowlist"]["detail"]


def test_aliased_builtin_dynamic_import_is_detected() -> None:
    imports, errors = parse_imports(
        "task.py", b"from builtins import __import__ as load\nload('demo_sdk')\n"
    )
    assert errors == []
    assert any(item.module == "demo_sdk" and item.kind == "dynamic_literal" for item in imports)


@pytest.mark.parametrize(
    "guarded_import",
    [
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import demo_sdk\n",
        "import typing as t\nif t.TYPE_CHECKING:\n    import demo_sdk\n",
        "if False:\n    import demo_sdk\n",
    ],
)
def test_import_moved_from_non_executing_block_to_runtime_is_new(
    tmp_path: Path, guarded_import: str
) -> None:
    scenario = {
        "base_files": {"requirements.txt": "", "task.py": guarded_import},
        "head_files": {"requirements.txt": "", "task.py": "import demo_sdk\n"},
        "environment": {
            "inventory": {},
            "import_map": {"demo_sdk": ["demo-sdk"]},
            "imports": {"demo_sdk:": True},
            "distributions": {},
        },
        "snapshot": {},
        "expected_decision": "deny",
    }
    report, output = _run_scenario(tmp_path, scenario)
    evidence = json.loads((output / "dependency-evidence.json").read_text(encoding="utf-8"))
    assert report["assurance_decision"] == "deny"
    assert any(
        item["module"] == "demo_sdk" and item["execution_context"] == "runtime"
        for item in evidence["new_imports"]
    )


def test_user_variable_named_type_checking_does_not_hide_runtime_import() -> None:
    imports, errors = parse_imports(
        "task.py",
        b"TYPE_CHECKING = True\nif TYPE_CHECKING:\n    import demo_sdk\n",
    )
    assert errors == []
    demo = next(item for item in imports if item.module == "demo_sdk")
    assert demo.execution_context == "runtime"


@pytest.mark.parametrize(
    "source",
    [
        "from importlib import import_module\nload = import_module\nload('demo_sdk')\n",
        "import importlib\nload: object = importlib.import_module\nload('demo_sdk')\n",
        "import builtins\nload = builtins.__import__\nload('demo_sdk')\n",
        "from builtins import __import__\nfirst = __import__\nload = first\nload('demo_sdk')\n",
    ],
)
def test_assigned_dynamic_import_alias_is_detected(source: str) -> None:
    imports, errors = parse_imports("task.py", source.encode())
    assert errors == []
    assert any(item.module == "demo_sdk" and item.kind == "dynamic_literal" for item in imports)


def test_assigned_dynamic_import_alias_with_nonliteral_target_is_unassured() -> None:
    _, errors = parse_imports(
        "task.py",
        b"from importlib import import_module\nload = import_module\nload(module_name)\n",
    )
    assert errors == ["task.py:3: non-literal dynamic import cannot be verified"]


def test_nested_python_file_does_not_create_a_local_import_root(tmp_path: Path) -> None:
    scenario = {
        "base_files": {"requirements.txt": "", "task.py": "pass\n"},
        "head_files": {
            "requirements.txt": "",
            "task.py": "pass\n",
            "requests/helper.py": "pass\n",
            "owned_package/__init__.py": "pass\n",
        },
    }
    repo_path, _, head = _fixture_repo(tmp_path, scenario)
    roots = _local_roots(GitRepository(repo_path), head, {"local_roots": []})
    assert "requests" not in roots
    assert "owned_package" in roots


@pytest.mark.parametrize("ambiguous", ["Apache Software License", "BSD License"])
def test_ambiguous_license_label_is_not_converted_to_spdx(ambiguous: str) -> None:
    status, _ = _license_matches(ambiguous, {"Apache-2.0", "BSD-3-Clause"})
    assert status == "fail"


def test_license_exception_requires_exact_package_version_and_expression() -> None:
    policy = {
        "allowed_spdx": ["MIT"],
        "approved_exceptions": {
            "psycopg-pool": {
                "version": "3.3.1",
                "license_expression": "LGPL-3.0-only",
                "approval_ref": "RPA-241",
                "conditions": ["version change requires a new approval"],
            }
        },
    }
    assert _license_status("psycopg-pool", "3.3.1", "LGPL-3.0-only", policy) == (
        "pass",
        "license LGPL-3.0-only is approved for psycopg-pool==3.3.1 by RPA-241",
        True,
    )
    status, _, approved = _license_status(
        "psycopg-pool", "3.3.2", "LGPL-3.0-only", policy
    )
    assert status == "fail"
    assert approved is False


@pytest.mark.parametrize(
    ("package", "version", "expression"),
    [
        ("psycopg", "3.2.3", "GNU Lesser General Public License v3 (LGPLv3)"),
        ("psycopg-binary", "3.2.3", "GNU Lesser General Public License v3 (LGPLv3)"),
        ("psycopg-pool", "3.3.1", "LGPL-3.0-only"),
    ],
)
def test_production_psycopg_license_exceptions_are_exact(
    package: str,
    version: str,
    expression: str,
) -> None:
    policy = json.loads(
        (ROOT / "assurance" / "change" / "policy" / "dependency-policy.json").read_text(
            encoding="utf-8"
        )
    )
    status, reason, approved = _license_status(
        package, version, expression, policy["license_policy"]
    )
    assert status == "pass"
    assert "RPA-241" in reason
    assert approved is True


def test_explicit_license_exception_does_not_approve_vulnerability_policy(
    tmp_path: Path,
) -> None:
    scenario = _load_scenarios()["good_import"]
    repo, base, head = _fixture_repo(tmp_path, scenario)
    policy = _policy(scenario)
    policy["policy_decision_state"] = "decision_needed"
    policy["license_policy"]["approved_exceptions"] = {
        "samplepkg": {
            "version": "1.0.0",
            "license_expression": "MIT",
            "approval_ref": "RPA-241-fixture",
            "conditions": ["fixture only"],
        }
    }
    output = tmp_path / "out"
    report = _AssuranceRunner(
        repo_root=repo,
        base_sha=base,
        head_sha=head,
        repository="Metanet-Final-01/fixture",
        output=output,
        policy=policy,
        policy_uri="fixture-policy.json",
        policy_digest=canonical_digest(policy),
        environment=FixtureDependencyEnvironment(scenario["environment"]),
        now=FIXED_NOW,
    ).run()
    evidence = json.loads((output / "dependency-evidence.json").read_text(encoding="utf-8"))
    assert evidence["rules"]["dep.license"]["status"] == "pass"
    assert evidence["rules"]["dep.vuln"]["status"] == "unassured"
    assert report["assurance_decision"] == "unassured"


def test_explicit_dependency_failure_takes_precedence_over_detector_error() -> None:
    evidence = {
        "rules": {
            "dep.allowlist": {"status": "fail"},
            "dep.manifest": {"status": "error"},
        },
        "package_checks": {},
    }
    assert _overall_dependency_status(evidence) == "fail"
    assert _combine_rule_status("fail", "error") == "fail"
    assert _combine_rule_status("error", "fail") == "fail"


@pytest.mark.parametrize(
    ("value", "schema"),
    [(1, {"const": True}), (True, {"const": 1}), (1, {"enum": [True]})],
)
def test_offline_schema_validator_uses_json_type_aware_equality(
    value: object, schema: dict
) -> None:
    with pytest.raises(SchemaValidationError):
        validate_json_schema(value, schema)


def test_allow_candidate_schema_requires_every_control(tmp_path: Path) -> None:
    report, output = _run_scenario(tmp_path, _load_scenarios()["good_import"])
    report["controls"] = [report["controls"][0]] * len(CONTROL_ORDER)
    schema = json.loads(REPORT_SCHEMA.read_text(encoding="utf-8"))
    with pytest.raises(ValidationError):
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(report)

    manifest = json.loads((output / "change-manifest.json").read_text(encoding="utf-8"))
    manifest["applicable_controls"] = list(CONTROL_ORDER[:-1])
    manifest_schema = json.loads(MANIFEST_SCHEMA.read_text(encoding="utf-8"))
    with pytest.raises(ValidationError):
        Draft202012Validator(
            manifest_schema, format_checker=FormatChecker()
        ).validate(manifest)


def test_non_python_to_python_rename_treats_import_as_new(tmp_path: Path) -> None:
    content = "import demo_sdk\n"
    scenario = {
        "base_files": {"requirements.txt": "", "task.txt": content},
        "head_files": {"requirements.txt": "", "task.py": content},
        "environment": {
            "inventory": {},
            "import_map": {"demo_sdk": ["demo-sdk"]},
            "imports": {"demo_sdk:": True},
            "distributions": {},
        },
        "snapshot": {},
        "expected_decision": "deny",
    }
    report, output = _run_scenario(tmp_path, scenario)
    evidence = json.loads((output / "dependency-evidence.json").read_text(encoding="utf-8"))
    assert report["assurance_decision"] == "deny"
    assert any(item["module"] == "demo_sdk" for item in evidence["new_imports"])


def test_unknown_stale_evidence_is_rejected(tmp_path: Path) -> None:
    scenario = _load_scenarios()["good_import"]
    repo, base, head = _fixture_repo(tmp_path, scenario)
    output = tmp_path / "out"
    output.mkdir()
    (output / "stale.json").write_text("{}", encoding="utf-8")

    with pytest.raises(AssuranceError, match="unknown entry"):
        _AssuranceRunner(
            repo_root=repo,
            base_sha=base,
            head_sha=head,
            repository="Metanet-Final-01/fixture",
            output=output,
            policy=_policy(scenario),
            policy_uri="fixture-policy.json",
            policy_digest=canonical_digest(_policy(scenario)),
            environment=FixtureDependencyEnvironment(scenario["environment"]),
            now=FIXED_NOW,
        ).run()


def test_observe_workflow_exposes_fatal_checker_failure() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    policy = json.loads(
        (ROOT / "assurance" / "change" / "policy" / "dependency-policy.json").read_text(
            encoding="utf-8"
        )
    )
    assert "continue-on-error: true" not in workflow
    assert "${{ runner.temp }}" in workflow
    assert "working-directory: trusted-runner" in workflow
    assert '--repo "$ASSURANCE_SUBJECT"' in workflow
    assert "BOOTSTRAP_UNASSURED.md" in workflow
    assert "-r requirements.txt -r requirements-dev.txt" in workflow
    assert "working-directory: subject" not in workflow
    assert "coverage" not in policy["import_distribution_map"]


def test_public_api_does_not_export_unvalidated_runner() -> None:
    assert not hasattr(change_api, "AssuranceRunner")
    assert "AssuranceRunner" not in change_api.__all__


def test_unparsed_dependency_manifest_change_is_unassured(tmp_path: Path) -> None:
    scenario = {
        "base_files": {
            "requirements.txt": "",
            "pyproject.toml": "[project]\ndependencies=[]\n",
        },
        "head_files": {
            "requirements.txt": "",
            "pyproject.toml": "[project]\ndependencies=['demo-sdk==1.0.0']\n",
        },
        "environment": {"inventory": {}, "import_map": {}, "imports": {}, "distributions": {}},
        "snapshot": {},
        "expected_decision": "unassured",
    }
    report, output = _run_scenario(tmp_path, scenario)
    evidence = json.loads((output / "dependency-evidence.json").read_text(encoding="utf-8"))
    ch04 = next(item for item in report["controls"] if item["control_id"] == "CH-04")
    assert report["assurance_decision"] == "unassured"
    assert ch04["status"] == "error"
    assert any("pyproject.toml" in detail for detail in evidence["parse_errors"])


def test_policy_loader_enforces_complete_schema(tmp_path: Path) -> None:
    valid = _policy(_load_scenarios()["good_import"])
    invalid_policies = []

    wrong_type = json.loads(json.dumps(valid))
    wrong_type["requirement_files"] = "requirements.txt"
    invalid_policies.append(wrong_type)

    extra_field = json.loads(json.dumps(valid))
    extra_field["unexpected"] = True
    invalid_policies.append(extra_field)

    invalid_age = json.loads(json.dumps(valid))
    invalid_age["vulnerability_policy"]["max_age_days"] = 0
    invalid_policies.append(invalid_age)

    invalid_timestamp = json.loads(json.dumps(valid))
    invalid_timestamp["vulnerability_policy"]["snapshot_generated_at"] = "not-a-date"
    invalid_policies.append(invalid_timestamp)

    for index, policy in enumerate(invalid_policies):
        path = tmp_path / f"invalid-{index}.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        with pytest.raises(AssuranceError, match="schema validation failed"):
            load_policy(path)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ('{"schema_version":"1.0","schema_version":"2.0"}', "duplicate JSON key"),
        ('{"schema_version":NaN}', "invalid JSON constant"),
    ],
)
def test_policy_loader_rejects_ambiguous_json(
    tmp_path: Path, raw: str, message: str
) -> None:
    path = tmp_path / "ambiguous.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(AssuranceError, match=message):
        load_policy(path)


def test_diverged_base_uses_merge_base_for_dependency_baseline(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "A360 fixture")
    _git(repo, "config", "user.email", "fixture@invalid.local")
    _replace_files(
        repo,
        {"requirements.txt": "demo-sdk==1.0.0\n", "task.py": "def run():\n    return 1\n"},
    )
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "common")
    common = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "--quiet", "-b", "head-branch")
    (repo / "task.py").write_text("import demo_sdk\n", encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "head")
    head = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "--quiet", "-b", "base-branch", common)
    (repo / "requirements.txt").write_text("demo-sdk==2.0.0\n", encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "base advanced")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "--quiet", "head-branch")

    scenario = {
        "environment": {
            "inventory": {"demo-sdk": "1.0.0"},
            "import_map": {"demo_sdk": ["demo-sdk"]},
            "imports": {"demo_sdk:": True},
            "distributions": {
                "demo-sdk": {
                    "installed": True,
                    "version": "1.0.0",
                    "license": "MIT",
                }
            },
        },
        "snapshot": {
            "demo-sdk": {
                "version": "1.0.0",
                "reviewed": True,
                "advisories": [],
            }
        },
    }
    policy = _policy(scenario)
    report = _AssuranceRunner(
        repo_root=repo,
        base_sha=base,
        head_sha=head,
        repository="Metanet-Final-01/fixture",
        output=tmp_path / "out",
        policy=policy,
        policy_uri="fixture-policy.json",
        policy_digest=canonical_digest(policy),
        environment=FixtureDependencyEnvironment(scenario["environment"]),
        now=FIXED_NOW,
    ).run()
    assert report["assurance_decision"] == "allow_candidate"
    manifest = json.loads((tmp_path / "out" / "change-manifest.json").read_text())
    assert manifest["subject"]["merge_base_sha"] == common


def test_git_read_failures_are_not_converted_to_missing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = {
        "base_files": {"requirements.txt": "", "task.py": "def run():\n    return 1\n"},
        "head_files": {"requirements.txt": "", "task.py": "def run():\n    return 2\n"},
        "environment": {"inventory": {}, "import_map": {}, "imports": {}, "distributions": {}},
        "snapshot": {},
        "expected_decision": "allow_candidate",
    }
    repo_path, base, head = _fixture_repo(tmp_path, scenario)
    repo = GitRepository(repo_path)
    repo.paths(base)
    repo.paths(head)
    original_run = repo.run

    def injected_failure(*args: str, check: bool = True):
        if args[0] in {"show", "diff"}:
            assert check is True
            raise AssuranceError("injected Git read failure")
        return original_run(*args, check=check)

    monkeypatch.setattr(repo, "run", injected_failure)
    with pytest.raises(AssuranceError, match="injected Git read failure"):
        repo.show(head, "task.py")
    with pytest.raises(AssuranceError, match="injected Git read failure"):
        repo.added_lines(base, head, "task.py")


def test_detector_error_receipt_is_nonpassing_and_schema_valid(tmp_path: Path) -> None:
    report = write_error_report(
        output=tmp_path,
        repository="Metanet-Final-01/fixture",
        base_sha="a" * 40,
        head_sha="b" * 40,
        error=RuntimeError(
            "injected failure password=visible-secret Bearer abc.def "
            "postgresql://service:database-secret@example.invalid/db "
            "AWS_SECRET_ACCESS_KEY=visible-aws CLIENT_SECRET=visible-client "
            "GITHUB_TOKEN=visible-github"
        ),
        now=FIXED_NOW,
    )
    assert report["assurance_decision"] == "unassured"
    assert report["evidence_complete"] is False
    error_evidence = (tmp_path / "detector-error.json").read_text(encoding="utf-8")
    assert "visible-secret" not in error_evidence
    assert "abc.def" not in error_evidence
    assert "database-secret" not in error_evidence
    assert "visible-aws" not in error_evidence
    assert "visible-client" not in error_evidence
    assert "visible-github" not in error_evidence
    assert error_evidence.count("<redacted>") == 6
    schema = json.loads(REPORT_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(report)


def test_cli_error_stays_nonblocking_in_observe(tmp_path: Path) -> None:
    exit_code = cli_main(
        [
            "--repo",
            str(tmp_path),
            "--base-sha",
            "invalid",
            "--head-sha",
            "invalid",
            "--repository",
            "Metanet-Final-01/fixture",
            "--output",
            str(tmp_path / "out"),
        ]
    )
    report = json.loads((tmp_path / "out" / "assurance-report.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["assurance_decision"] == "unassured"
    assert report["enforcement"]["blocks_merge"] is False


def test_cli_success_derives_standard_library_change(tmp_path: Path) -> None:
    scenario = {
        "base_files": {
            "requirements.txt": "",
            "worker.py": "def run():\n    return 'ready'\n",
        },
        "head_files": {
            "requirements.txt": "",
            "worker.py": "import json\n\ndef run():\n    return json.dumps({'status': 'ready'})\n",
        },
        "environment": {"inventory": {}, "import_map": {}, "imports": {}, "distributions": {}},
        "snapshot": {},
        "expected_decision": "allow_candidate",
    }
    repo, base, head = _fixture_repo(tmp_path, scenario)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(_policy(scenario)), encoding="utf-8")
    output = tmp_path / "out"
    exit_code = cli_main(
        [
            "--repo",
            str(repo),
            "--base-sha",
            base,
            "--head-sha",
            head,
            "--repository",
            "Metanet-Final-01/fixture",
            "--policy",
            str(policy_path),
            "--output",
            str(output),
        ]
    )
    report = json.loads((output / "assurance-report.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["assurance_decision"] == "allow_candidate"
