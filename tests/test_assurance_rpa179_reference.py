from __future__ import annotations

import copy
import importlib.util
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError


REPO = Path(__file__).resolve().parents[1]
REFERENCE = REPO / "assurance" / "reference" / "rpa179"


def load_materializer():
    spec = importlib.util.spec_from_file_location("rpa179_materialize", REFERENCE / "materialize.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_verifier():
    spec = importlib.util.spec_from_file_location("rpa179_verifier", REFERENCE / "verify.py")
    assert spec and spec.loader
    sys.path.insert(0, str(REFERENCE))
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


def load_regression():
    spec = importlib.util.spec_from_file_location("rpa179_regression", REFERENCE / "regression.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_regression(source: Path, output: Path) -> dict:
    env = os.environ.copy()
    for key in tuple(env):
        upper = key.upper()
        if (
            upper.startswith("PYTHON")
            or key.startswith("COV_CORE_")
            or key in {"COVERAGE_FILE", "COVERAGE_PROCESS_START"}
            or upper in {
                "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_CEILING_DIRECTORIES",
                "GIT_COMMON_DIR",
                "GIT_DIR",
                "GIT_INDEX_FILE",
                "GIT_NAMESPACE",
                "GIT_OBJECT_DIRECTORY",
                "GIT_SHALLOW_FILE",
                "GIT_WORK_TREE",
            }
            or upper.startswith("GIT_CONFIG_")
        ):
            env.pop(key, None)
    env.update({
        "A360_HARNESS_OUT": str(output / "generated"),
        "GIT_AUTHOR_DATE": "2026-07-15T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-07-15T00:00:00Z",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
    })
    report = output / "regression.json"
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(REFERENCE / "regression.py"),
            "--source",
            str(source),
            "--matrix",
            str(REFERENCE / "finding-matrix.yaml"),
            "--json",
            str(report),
        ],
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(report.read_text(encoding="utf-8"))


def test_reference_materializes_deterministically_and_covers_all_findings(
    monkeypatch: pytest.MonkeyPatch,
):
    test_root = REPO / ".rpa179-test" / f"p{os.getpid():x}-{secrets.token_hex(3)}"
    fake_home = test_root / "home"
    first = test_root / "first" / "source"
    second = test_root / "second" / "source"
    try:
        fake_home.mkdir(parents=True)
        (fake_home / ".gitconfig").write_text(
            "[core]\n\tautocrlf = true\n\tsafecrlf = true\n", encoding="utf-8"
        )
        with monkeypatch.context() as git_config:
            git_config.setenv("HOME", str(fake_home))
            git_config.setenv("USERPROFILE", str(fake_home))
            materializer = load_materializer()
            first_metadata = materializer.materialize(first)
            second_metadata = materializer.materialize(second)

        assert first_metadata["source_integrity"]["verified_files"] == 45
        assert first_metadata["corrected_tree"]["file_count"] == 52
        assert first_metadata["corrected_tree"]["tree_digest"] == second_metadata["corrected_tree"]["tree_digest"]
        assert not (first / ".git").exists()
        assert not (second / ".git").exists()

        poison = test_root / "poison"
        poison_home = test_root / "poison-home"
        poison.mkdir()
        poison_home.mkdir()
        marker = test_root / "sitecustomize-loaded"
        (poison / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('loaded')\n",
            encoding="utf-8",
        )
        with monkeypatch.context() as poisoned:
            poisoned.setenv("PYTHONPATH", str(poison))
            poisoned.setenv("PYTHONHOME", str(poison_home))
            first_report = run_regression(first, test_root / "first")
            second_report = run_regression(second, test_root / "second")
        assert not marker.exists()
        assert first_report == second_report
        assert first_report["coverage_complete"] is True
        assert len(first_report["actionable_findings"]) == 25
        assert len(first_report["cases"]) == 23
    finally:
        shutil.rmtree(test_root, ignore_errors=True)


def test_evidence_schema_rejects_incomplete_composition():
    schema = json.loads(
        (REFERENCE / "verification-result.schema.json").read_text(encoding="utf-8")
    )
    evidence = json.loads(
        (REFERENCE / "evidence" / "verification.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(evidence)

    mutations = []
    duplicate_run = copy.deepcopy(evidence)
    duplicate_run["runs"][1] = copy.deepcopy(duplicate_run["runs"][0])
    mutations.append(duplicate_run)

    duplicate_command = copy.deepcopy(evidence)
    duplicate_command["runs"][0]["commands"][-1] = copy.deepcopy(
        duplicate_command["runs"][0]["commands"][0]
    )
    mutations.append(duplicate_command)

    mismatched_command_payload = copy.deepcopy(evidence)
    first_command = mismatched_command_payload["runs"][0]["commands"][0]
    second_command = mismatched_command_payload["runs"][0]["commands"][1]
    first_command["argv"], second_command["argv"] = second_command["argv"], first_command["argv"]
    first_command["markers"], second_command["markers"] = (
        second_command["markers"],
        first_command["markers"],
    )
    mutations.append(mismatched_command_payload)

    duplicate_case = copy.deepcopy(evidence)
    duplicate_case["regression"]["cases"][-1] = copy.deepcopy(
        duplicate_case["regression"]["cases"][0]
    )
    mutations.append(duplicate_case)

    mismatched_case_payload = copy.deepcopy(evidence)
    first_case = mismatched_case_payload["regression"]["cases"][0]
    second_case = mismatched_case_payload["regression"]["cases"][2]
    first_case["findings"], second_case["findings"] = (
        second_case["findings"],
        first_case["findings"],
    )
    mutations.append(mismatched_case_payload)

    divergent_findings = copy.deepcopy(evidence)
    divergent_findings["regression"]["covered_findings"][-1] = "CR-22"
    mutations.append(divergent_findings)

    for mutation in mutations:
        with pytest.raises(ValidationError):
            validator.validate(mutation)


def test_repository_head_rejects_dirty_tree():
    verifier = load_verifier()
    repository = REPO / ".rpa179-test" / f"git-{os.getpid():x}-{secrets.token_hex(3)}"
    try:
        repository.mkdir(parents=True)
        subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
        tracked = repository / "tracked.txt"
        tracked.write_text("clean\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=RPA-179 Test",
                "-c",
                "user.email=rpa179-test@invalid.local",
                "-c",
                "commit.gpgSign=false",
                "commit",
                "--quiet",
                "-m",
                "fixture",
            ],
            cwd=repository,
            check=True,
        )

        assert len(verifier.repository_head(repository)) == 40
        tracked.write_text("dirty\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="repository must be clean"):
            verifier.repository_head(repository)
    finally:
        shutil.rmtree(repository, ignore_errors=True)


def test_materialize_writes_the_verified_bytes(monkeypatch: pytest.MonkeyPatch):
    materializer = load_materializer()
    destination = REPO / ".rpa179-test" / f"bytes-{os.getpid():x}-{secrets.token_hex(3)}"
    expected = {relative.as_posix() for _, relative in materializer.manifest_entries()}
    reads: dict[str, int] = {}
    corrections_reads = 0
    real_read_bytes = Path.read_bytes

    def counted_read_bytes(path: Path) -> bytes:
        nonlocal corrections_reads
        if path == materializer.CORRECTIONS:
            corrections_reads += 1
        try:
            relative = path.relative_to(materializer.FROZEN).as_posix()
        except ValueError:
            pass
        else:
            if path != materializer.BASE_MANIFEST:
                reads[relative] = reads.get(relative, 0) + 1
        return real_read_bytes(path)

    try:
        with monkeypatch.context() as counted:
            counted.setattr(Path, "read_bytes", counted_read_bytes)
            counted.setenv("GIT_DIR", "poison-git-dir")
            counted.setenv("GIT_WORK_TREE", "poison-work-tree")
            counted.setenv("GIT_CONFIG_COUNT", "1")
            counted.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
            counted.setenv("GIT_CONFIG_VALUE_0", "poison-hooks")
            materializer.materialize(destination)
        assert set(reads) == expected
        assert set(reads.values()) == {1}
        assert corrections_reads == 1
    finally:
        shutil.rmtree(destination, ignore_errors=True)


def test_materialize_removes_destination_after_metadata_failure(monkeypatch: pytest.MonkeyPatch):
    materializer = load_materializer()
    destination = REPO / ".rpa179-test" / f"metadata-failure-{os.getpid():x}-{secrets.token_hex(3)}"
    real_write_text = Path.write_text

    def fail_metadata(path: Path, *args, **kwargs):
        if path == destination / "MATERIALIZED.json":
            raise OSError("synthetic metadata write failure")
        return real_write_text(path, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(Path, "write_text", fail_metadata)
        with pytest.raises(OSError, match="synthetic metadata write failure"):
            materializer.materialize(destination)
    assert not destination.exists()


def test_verifier_strips_inherited_python_environment(monkeypatch: pytest.MonkeyPatch):
    with monkeypatch.context() as poisoned:
        poisoned.setenv("PYTHONPATH", "poison-path")
        poisoned.setenv("PYTHONHOME", "poison-home")
        poisoned.setenv("PYTHONWARNINGS", "error")
        poisoned.setenv("GIT_DIR", "poison-git-dir")
        poisoned.setenv("GIT_WORK_TREE", "poison-work-tree")
        poisoned.setenv("GIT_CONFIG_COUNT", "1")
        verifier = load_verifier()
        env = verifier.command_environment(REFERENCE, REFERENCE / ".work" / "env-test")

    assert "PYTHONPATH" not in env
    assert "PYTHONHOME" not in env
    assert "PYTHONWARNINGS" not in env
    assert "GIT_DIR" not in env
    assert "GIT_WORK_TREE" not in env
    assert "GIT_CONFIG_COUNT" not in env
    assert env["PYTHONNOUSERSITE"] == "1"


def test_git_commands_ignore_inherited_repository_selectors(monkeypatch: pytest.MonkeyPatch):
    verifier = load_verifier()
    regression = load_regression()
    test_root = REPO / ".rpa179-test" / f"git-env-{os.getpid():x}-{secrets.token_hex(3)}"
    actual = test_root / "actual"
    redirected = test_root / "redirected"

    def initialize(repository: Path, value: str) -> str:
        repository.mkdir(parents=True)
        subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repository, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=repository,
            check=True,
        )
        (repository / "value.txt").write_text(value, encoding="utf-8")
        subprocess.run(["git", "add", "value.txt"], cwd=repository, check=True)
        subprocess.run(
            ["git", "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "fixture"],
            cwd=repository,
            check=True,
        )
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    try:
        actual_head = initialize(actual, "actual")
        initialize(redirected, "redirected")
        with monkeypatch.context() as poisoned:
            poisoned.setenv("GIT_DIR", str(redirected / ".git"))
            poisoned.setenv("GIT_WORK_TREE", str(redirected))
            poisoned.setenv("GIT_CONFIG_COUNT", "1")
            poisoned.setenv("GIT_CONFIG_KEY_0", "core.bare")
            poisoned.setenv("GIT_CONFIG_VALUE_0", "true")
            assert verifier.repository_head(actual) == actual_head
            assert regression.git(actual, "rev-parse", "HEAD").decode().strip() == actual_head
    finally:
        shutil.rmtree(test_root, ignore_errors=True)


def test_coverage_matrix_rejects_historical_drift():
    regression = load_regression()
    matrix = regression.yaml.safe_load(
        (REFERENCE / "finding-matrix.yaml").read_text(encoding="utf-8")
    )
    mutations = []

    duplicate = copy.deepcopy(matrix)
    duplicate["findings"].append(copy.deepcopy(duplicate["findings"][16]))
    mutations.append(duplicate)

    tested_historical = copy.deepcopy(matrix)
    tested_historical["findings"][16]["test"] = "coverage_matrix"
    mutations.append(tested_historical)

    wrong_disposition = copy.deepcopy(matrix)
    wrong_disposition["findings"][21]["disposition"] = "corrective"
    mutations.append(wrong_disposition)

    for mutation in mutations:
        regression.MATRIX = mutation
        with pytest.raises(AssertionError):
            regression.coverage_matrix()


def test_materialize_failure_can_retry(monkeypatch: pytest.MonkeyPatch):
    materializer = load_materializer()
    destination = REPO / ".rpa179-test" / f"retry-{os.getpid():x}-{secrets.token_hex(3)}"
    real_run = materializer.subprocess.run
    failed = False

    def fail_first_config(command, *args, **kwargs):
        nonlocal failed
        if command[:3] == ["git", "config", "--local"] and not failed:
            failed = True
            return subprocess.CompletedProcess(
                command, returncode=1, stdout="", stderr="forced config failure"
            )
        return real_run(command, *args, **kwargs)

    try:
        with monkeypatch.context() as injected:
            injected.setattr(materializer.subprocess, "run", fail_first_config)
            with pytest.raises(RuntimeError, match="forced config failure"):
                materializer.materialize(destination)
        assert not destination.exists()
        assert materializer.materialize(destination)["corrected_tree"]["file_count"] == 52
    finally:
        shutil.rmtree(destination, ignore_errors=True)
