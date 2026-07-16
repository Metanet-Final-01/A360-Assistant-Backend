from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from assurance.change.checker import (
    AssuranceError,
    AssuranceRunner,
    canonical_digest,
    write_error_report,
)
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
            ]
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
    report = AssuranceRunner(
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
        AssuranceRunner(
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
    report = AssuranceRunner(
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


def test_unknown_stale_evidence_is_rejected(tmp_path: Path) -> None:
    scenario = _load_scenarios()["good_import"]
    repo, base, head = _fixture_repo(tmp_path, scenario)
    output = tmp_path / "out"
    output.mkdir()
    (output / "stale.json").write_text("{}", encoding="utf-8")

    with pytest.raises(AssuranceError, match="unknown entry"):
        AssuranceRunner(
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
    assert "coverage" not in policy["import_distribution_map"]


def test_detector_error_receipt_is_nonpassing_and_schema_valid(tmp_path: Path) -> None:
    report = write_error_report(
        output=tmp_path,
        repository="Metanet-Final-01/fixture",
        base_sha="a" * 40,
        head_sha="b" * 40,
        error=RuntimeError(
            "injected failure password=visible-secret Bearer abc.def "
            "postgresql://service:database-secret@example.invalid/db"
        ),
        now=FIXED_NOW,
    )
    assert report["assurance_decision"] == "unassured"
    assert report["evidence_complete"] is False
    error_evidence = (tmp_path / "detector-error.json").read_text(encoding="utf-8")
    assert "visible-secret" not in error_evidence
    assert "abc.def" not in error_evidence
    assert "database-secret" not in error_evidence
    assert error_evidence.count("<redacted>") == 3
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
