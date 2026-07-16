"""Materialize the corrected RPA-179 reference from the immutable v1.10 evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
FROZEN = REPO_ROOT / "assurance" / "phase0" / "v1.10" / "evidence" / "frozen" / "phase0-v1.10-src"
BASE_MANIFEST = FROZEN / "SHA256SUMS.txt"
CORRECTIONS = HERE / "corrections.patch"
EXPECTED_BASE_MANIFEST_SHA256 = "d0ad43c2a705d0522cba8e287b31fe040c953bb2ba89a5d3d9788cc0b07a666f"
EXPECTED_BASE_FILES = 45
HISTORICAL_REGRESSIONS = {
    "codex_v15_regression.py",
    "codex_v16_regression.py",
    "codex_v17_regression.py",
    "codex_v18_regression.py",
    "codex_v19_regression.py",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_entries() -> list[tuple[str, Path]]:
    actual_manifest = sha256(BASE_MANIFEST)
    if actual_manifest != EXPECTED_BASE_MANIFEST_SHA256:
        raise RuntimeError(
            "frozen manifest digest changed: "
            f"expected={EXPECTED_BASE_MANIFEST_SHA256} actual={actual_manifest}"
        )
    entries = []
    seen = set()
    for line in BASE_MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        digest, token = line.split(maxsplit=1)
        relative = token.lstrip("*")
        if relative.startswith("./"):
            relative = relative[2:]
        path = Path(relative)
        if not relative or path.is_absolute() or ".." in path.parts:
            raise RuntimeError(f"unsafe frozen manifest path: {relative!r}")
        normalized = path.as_posix()
        if normalized in seen:
            raise RuntimeError(f"duplicate frozen manifest path: {normalized}")
        seen.add(normalized)
        entries.append((digest, path))
    if len(entries) != EXPECTED_BASE_FILES:
        raise RuntimeError(
            f"frozen manifest entry count changed: expected={EXPECTED_BASE_FILES} actual={len(entries)}"
        )
    return entries


def verify_frozen() -> dict:
    entries = manifest_entries()
    failures = []
    for expected, relative in entries:
        path = FROZEN / relative
        actual = sha256(path) if path.is_file() else "missing"
        if actual != expected:
            failures.append({"path": relative.as_posix(), "expected": expected, "actual": actual})
    if failures:
        raise RuntimeError(f"frozen v1.10 integrity failed: {failures[:3]}")
    return {
        "verified_files": len(entries),
        "manifest_sha256": "sha256:" + sha256(BASE_MANIFEST),
    }


def corrected_tree(destination: Path) -> dict:
    files = []
    for path in sorted(destination.rglob("*")):
        if not path.is_file() or path.name == "MATERIALIZED.json" or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(destination).as_posix()
        files.append({"path": relative, "sha256": "sha256:" + sha256(path)})
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return {
        "file_count": len(files),
        "tree_digest": "sha256:" + hashlib.sha256(canonical).hexdigest(),
        "files": files,
    }


def materialize(destination: Path) -> dict:
    frozen = verify_frozen()
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError(f"destination must not exist or must be empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    for _, relative in manifest_entries():
        if relative.name in HISTORICAL_REGRESSIONS:
            continue
        source = FROZEN / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    # A destination below another worktree makes `git apply` silently filter every
    # root-relative patch path. A disposable nested repository pins destination as
    # the patch root; its metadata is removed before the tree is measured.
    git_dir = destination / ".git"
    init = subprocess.run(
        ["git", "init", "--quiet"], cwd=destination, capture_output=True, text=True
    )
    if init.returncode != 0:
        raise RuntimeError(f"temporary git init failed: {init.stderr.strip()[:400]}")
    try:
        for mode in ("check", "apply"):
            command = ["git", "apply", "--whitespace=nowarn"]
            if mode == "check":
                command.append("--check")
            command.append(str(CORRECTIONS))
            result = subprocess.run(command, cwd=destination, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"git apply {mode} failed: {result.stderr.strip()[:400]}")

        reverse_check = subprocess.run(
            ["git", "apply", "--reverse", "--check", str(CORRECTIONS)],
            cwd=destination,
            capture_output=True,
            text=True,
        )
        if reverse_check.returncode != 0:
            raise RuntimeError(
                "correction patch did not materialize completely: "
                + reverse_check.stderr.strip()[:400]
            )
    finally:
        shutil.rmtree(git_dir, ignore_errors=False)

    tree = corrected_tree(destination)
    metadata = {
        "schema_version": "rpa179.1",
        "source": "assurance/phase0/v1.10/evidence/frozen/phase0-v1.10-src",
        "source_integrity": frozen,
        "corrections_sha256": "sha256:" + sha256(CORRECTIONS),
        "excluded_historical_regressions": sorted(HISTORICAL_REGRESSIONS),
        "corrected_tree": tree,
    }
    (destination / "MATERIALIZED.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    metadata = materialize(args.destination.resolve())
    print(json.dumps({
        "schema_version": metadata["schema_version"],
        "verified_files": metadata["source_integrity"]["verified_files"],
        "corrected_files": metadata["corrected_tree"]["file_count"],
        "tree_digest": metadata["corrected_tree"]["tree_digest"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
