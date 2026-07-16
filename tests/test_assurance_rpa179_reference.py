from __future__ import annotations

import importlib.util
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
REFERENCE = REPO / "assurance" / "reference" / "rpa179"


def load_materializer():
    spec = importlib.util.spec_from_file_location("rpa179_materialize", REFERENCE / "materialize.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_regression(source: Path, output: Path) -> dict:
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("COV_CORE_") or key in {"COVERAGE_FILE", "COVERAGE_PROCESS_START"}:
            env.pop(key, None)
    env.update({
        "A360_HARNESS_OUT": str(output / "generated"),
        "GIT_AUTHOR_DATE": "2026-07-15T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-07-15T00:00:00Z",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
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


def test_reference_materializes_deterministically_and_covers_all_findings():
    materializer = load_materializer()
    test_root = REPO / ".rpa179-test" / f"p{os.getpid():x}-{secrets.token_hex(3)}"
    first = test_root / "first" / "source"
    second = test_root / "second" / "source"
    try:
        first_metadata = materializer.materialize(first)
        second_metadata = materializer.materialize(second)

        assert first_metadata["source_integrity"]["verified_files"] == 45
        assert first_metadata["corrected_tree"]["file_count"] == 52
        assert first_metadata["corrected_tree"]["tree_digest"] == second_metadata["corrected_tree"]["tree_digest"]
        assert not (first / ".git").exists()
        assert not (second / ".git").exists()

        first_report = run_regression(first, test_root / "first")
        second_report = run_regression(second, test_root / "second")
        assert first_report == second_report
        assert first_report["coverage_complete"] is True
        assert len(first_report["actionable_findings"]) == 25
        assert len(first_report["cases"]) == 23
    finally:
        shutil.rmtree(test_root, ignore_errors=True)
