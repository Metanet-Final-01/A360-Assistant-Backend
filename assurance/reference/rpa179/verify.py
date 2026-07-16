"""Verify the corrected RPA-179 reference twice in independent directories."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from materialize import CORRECTIONS, HERE, REPO_ROOT, materialize, sha256


MATRIX = HERE / "finding-matrix.yaml"
EVIDENCE_SCHEMA = HERE / "verification-result.schema.json"
FIXED_GIT_DATE = "2026-07-15T00:00:00Z"
COMMANDS = (
    ("contract", ("contract_self_test.py",),
     ("cases=68  mismatches=0", "executable rules      : 15", "COVERAGE GATE: PASS")),
    ("vertical", ("vertical_paths.py",),
     ("HL-21  ok=True", "VERTICAL PATHS: BOTH POSITIVE PATHS REACHED A SUCCESS STATE")),
    ("selfattack", ("selfattack.py",),
     ("attacks that bypassed a v1.10 claim: 4/14",
      "SELF-ATTACK ALLOWLIST: exact D-16 residual set matched")),
)


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def canonical_digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return digest_bytes(raw)


def without_coverage_environment(env: dict[str, str]) -> dict[str, str]:
    """Keep fixture subprocesses out of a parent pytest-cov data session."""
    for key in tuple(env):
        if key.startswith("COV_CORE_") or key in {"COVERAGE_FILE", "COVERAGE_PROCESS_START"}:
            env.pop(key, None)
    return env


def command_environment(source: Path, output: Path) -> dict[str, str]:
    env = without_coverage_environment(os.environ.copy())
    env.update({
        "A360_HARNESS_OUT": str(output),
        "A360_TEST_SUBJECT_REPO": str(source),
        "GIT_AUTHOR_DATE": FIXED_GIT_DATE,
        "GIT_COMMITTER_DATE": FIXED_GIT_DATE,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def normalized_transcript(value: str, source: Path, output: Path) -> str:
    normalized = value
    replacements = {
        str(source): "<MATERIALIZED_SOURCE>",
        source.as_posix(): "<MATERIALIZED_SOURCE>",
        str(output): "<COMMAND_OUTPUT>",
        output.as_posix(): "<COMMAND_OUTPUT>",
    }
    for current, token in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        normalized = normalized.replace(current, token)
    return normalized


def run_command(
    name: str,
    argv: list[str],
    source: Path,
    output: Path,
    log_dir: Path,
    required_markers: tuple[str, ...],
    display_argv: list[str] | None = None,
) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        argv,
        cwd=source,
        env=command_environment(source, output),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{name}.stdout.log").write_text(result.stdout, encoding="utf-8")
    (log_dir / f"{name}.stderr.log").write_text(result.stderr, encoding="utf-8")
    markers = {marker: marker in result.stdout for marker in required_markers}
    passed = result.returncode == 0 and all(markers.values())
    normalized_stdout = normalized_transcript(result.stdout, source, output)
    normalized_stderr = normalized_transcript(result.stderr, source, output)
    record = {
        "name": name,
        "argv": display_argv or ["python", *argv[1:]],
        "exit_code": result.returncode,
        "stdout_sha256": digest_bytes(result.stdout.encode("utf-8")),
        "stderr_sha256": digest_bytes(result.stderr.encode("utf-8")),
        "normalized_stdout_sha256": digest_bytes(normalized_stdout.encode("utf-8")),
        "normalized_stderr_sha256": digest_bytes(normalized_stderr.encode("utf-8")),
        "markers": markers,
        "passed": passed,
    }
    if not passed:
        missing = [marker for marker, found in markers.items() if not found]
        raise RuntimeError(
            f"{name} verification failed: exit={result.returncode}, missing_markers={missing}, "
            f"logs={log_dir}"
        )
    return record


def run_once(run_id: str, run_root: Path) -> dict:
    source = run_root / "source"
    metadata = materialize(source)
    commands = []
    for name, relative_argv, markers in COMMANDS:
        commands.append(run_command(
            name,
            [sys.executable, "-B", *relative_argv],
            source,
            run_root / "outputs" / name,
            run_root / "logs",
            markers,
        ))

    regression_path = run_root / "regression.json"
    regression_argv = [
        sys.executable,
        "-B",
        str(HERE / "regression.py"),
        "--source",
        str(source),
        "--matrix",
        str(MATRIX),
        "--json",
        str(regression_path),
    ]
    commands.append(run_command(
        "regression",
        regression_argv,
        source,
        run_root / "outputs" / "regression",
        run_root / "logs",
        ('"coverage_complete": true',),
        [
            "python",
            "-B",
            "assurance/reference/rpa179/regression.py",
            "--source",
            "<MATERIALIZED_SOURCE>",
            "--matrix",
            "assurance/reference/rpa179/finding-matrix.yaml",
            "--json",
            "<RUN_ROOT>/regression.json",
        ],
    ))
    regression = json.loads(regression_path.read_text(encoding="utf-8"))
    if len(regression["actionable_findings"]) != 25 or len(regression["cases"]) != 23:
        raise RuntimeError("regression cardinality changed from the reviewed RPA-179 contract")

    return {
        "run_id": run_id,
        "tree_digest": metadata["corrected_tree"]["tree_digest"],
        "commands": commands,
        "regression_digest": canonical_digest(regression),
        "regression": regression,
    }


def environment_record() -> dict:
    dependencies = {name: version(name) for name in ("jsonschema", "PyYAML")}
    git = subprocess.run(
        ["git", "--version"], capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout.strip()
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "platform": platform.platform(),
        "git_version": git,
        "dependencies": dependencies,
        "dependency_digest": canonical_digest(dependencies),
    }


def repository_head(repo_root: Path = REPO_ROOT) -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if status.returncode != 0:
        raise RuntimeError(f"git status failed: {status.stderr.strip()[:400]}")
    if status.stdout.strip():
        raise RuntimeError(
            "repository must be clean before verification; commit code and policy changes first"
        )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()


def verify(work_root: Path) -> dict:
    baseline_commit = repository_head()
    if work_root.exists() and any(work_root.iterdir()):
        raise RuntimeError(f"work root must not exist or must be empty: {work_root}")
    work_root.mkdir(parents=True, exist_ok=True)

    runs = [run_once(f"run-{index}", work_root / f"run-{index}") for index in (1, 2)]
    def transcript_projection(run: dict) -> list[dict]:
        return [
            {
                "name": command["name"],
                "exit_code": command["exit_code"],
                "normalized_stdout_sha256": command["normalized_stdout_sha256"],
                "normalized_stderr_sha256": command["normalized_stderr_sha256"],
                "markers": command["markers"],
            }
            for command in run["commands"]
        ]

    determinism = {
        "tree_digest_equal": runs[0]["tree_digest"] == runs[1]["tree_digest"],
        "regression_equal": runs[0]["regression_digest"] == runs[1]["regression_digest"],
        "transcripts_equal": transcript_projection(runs[0]) == transcript_projection(runs[1]),
    }
    if not all(determinism.values()):
        raise RuntimeError(f"independent verification runs diverged: {determinism}")

    first_metadata = json.loads(
        (work_root / "run-1" / "source" / "MATERIALIZED.json").read_text(encoding="utf-8")
    )
    regression = runs[0]["regression"]
    evidence_runs = [
        {key: value for key, value in run.items() if key != "regression"}
        for run in runs
    ]
    evidence = {
        "schema_version": "rpa179.verification.1",
        "verified_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "result": "pass",
        "baseline_commit": baseline_commit,
        "reference": {
            "source": first_metadata["source"],
            "source_manifest_sha256": first_metadata["source_integrity"]["manifest_sha256"],
            "corrections_sha256": "sha256:" + sha256(CORRECTIONS),
            "corrected_tree_digest": runs[0]["tree_digest"],
            "corrected_file_count": first_metadata["corrected_tree"]["file_count"],
            "verified_source_files": first_metadata["source_integrity"]["verified_files"],
            "finding_matrix_sha256": "sha256:" + sha256(MATRIX),
            "verification_tools": {
                "materialize.py": "sha256:" + sha256(HERE / "materialize.py"),
                "regression.py": "sha256:" + sha256(HERE / "regression.py"),
                "verify.py": "sha256:" + sha256(HERE / "verify.py"),
                "verification-result.schema.json": "sha256:" + sha256(EVIDENCE_SCHEMA),
            },
        },
        "environment": environment_record(),
        "runs": evidence_runs,
        "regression": regression,
        "determinism": determinism,
        "summary": {
            "contract_cases": 68,
            "executable_rules": 15,
            "regression_cases": len(regression["cases"]),
            "actionable_findings": len(regression["actionable_findings"]),
            "historical_findings": 3,
            "selfattack_expected_residuals": 4,
        },
        "limitations": [
            "fixture-only reference; no product path, database, network, or paid LLM was used",
            "the four disclosed D-16 residuals require a protected CI writer and are not closed in Python",
            "this result is not an operational security certification or Warn/Enforce approval",
        ],
    }
    schema = json.loads(EVIDENCE_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(evidence)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    evidence = verify(args.work_root.resolve())
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode("utf-8")
        args.evidence.write_bytes(payload)
        args.evidence.with_suffix(".sha256").write_bytes(
            f"{hashlib.sha256(payload).hexdigest()} *{args.evidence.name}\n".encode("ascii")
        )
    print(json.dumps({
        "result": evidence["result"],
        "tree_digest": evidence["reference"]["corrected_tree_digest"],
        "actionable_findings": evidence["summary"]["actionable_findings"],
        "determinism": evidence["determinism"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
