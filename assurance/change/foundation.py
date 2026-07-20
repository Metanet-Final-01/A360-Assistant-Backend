"""Shared contracts plus trusted Git and runtime evidence readers."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


SCHEMA_VERSION = "1.0"
CONTROL_ORDER = ("CH-01", "CH-02", "CH-04", "CH-06", "CH-11", "CH-12")
SEVERITY_RANK = {"unknown": 5, "low": 1, "medium": 2, "high": 3, "critical": 4}
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)(?:\[[^\]]+\])?==(?P<version>[^\s;]+)(?:\s*;.*)?$"
)
RISK_ORDER = (
    "auth",
    "database_contract",
    "api_contract",
    "dependency",
    "test_oracle",
    "workflow",
    "assurance_policy",
    "agent_owned",
    "rag",
    "infra",
    "source",
    "docs",
)
SENSITIVE_SUFFIXES = (".py", ".yml", ".yaml", ".sh", ".ps1")
DETAIL_SECRET = re.compile(
    r"(?i)(?P<key>[A-Za-z0-9_.-]*(?:api[_-]?key|access[_-]?token|token|secret|password)"
    r"[A-Za-z0-9_.-]*)(?P<separator>\s*[:=]\s*)(?P<value>[^\s,;]+)"
)
URL_CREDENTIAL = re.compile(r"(://[^:/@\s]+:)[^@\s]+@")
BEARER_CREDENTIAL = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


class AssuranceError(RuntimeError):
    """Raised when trusted evidence cannot be derived."""


@dataclass(frozen=True)
class ImportInspection:
    resolves: bool
    detail: str


@dataclass(frozen=True)
class DistributionInspection:
    installed: bool
    version: str | None
    license_expression: str | None
    detail: str


class DependencyEnvironment(Protocol):
    """Read-only dependency inspector; deliberately has no install operation."""

    def inventory(self) -> dict[str, str]: ...

    def distributions_for_import(self, module: str) -> list[str]: ...

    def inspect_import(self, module: str, symbol: str | None) -> ImportInspection: ...

    def inspect_distribution(self, distribution: str) -> DistributionInspection: ...


@dataclass(frozen=True)
class ImportSpec:
    path: str
    module: str
    symbol: str | None
    line: int
    kind: str
    execution_context: str = "runtime"

    @property
    def key(self) -> tuple[str, str | None, str, str]:
        return self.module, self.symbol, self.kind, self.execution_context

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "module": self.module,
            "symbol": self.symbol,
            "line": self.line,
            "kind": self.kind,
            "execution_context": self.execution_context,
        }


@dataclass(frozen=True)
class RequirementRecord:
    name: str
    normalized_name: str
    version: str
    path: str
    line: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "normalized_name": self.normalized_name,
            "version": self.version,
            "path": self.path,
            "line": self.line,
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def canonical_digest(value: Any) -> str:
    return digest_bytes(canonical_bytes(value))


def normalize_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _safe_detail(value: str, limit: int = 300) -> str:
    redacted = URL_CREDENTIAL.sub(r"\1<redacted>@", value.replace("\x00", ""))
    redacted = BEARER_CREDENTIAL.sub("Bearer <redacted>", redacted)
    redacted = DETAIL_SECRET.sub(
        lambda match: f"{match.group('key')}{match.group('separator')}<redacted>",
        redacted,
    )
    compact = " ".join(redacted.split())
    return compact[:limit] or "no detail"


class GitRepository:
    """Read Git objects directly; working-tree file bytes are never trusted."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self._paths_cache: dict[str, list[str]] = {}

    @staticmethod
    def _environment() -> dict[str, str]:
        env = os.environ.copy()
        selectors = {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_DIR",
            "GIT_INDEX_FILE",
            "GIT_NAMESPACE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_SHALLOW_FILE",
            "GIT_WORK_TREE",
        }
        for key in tuple(env):
            upper = key.upper()
            if upper in selectors or upper.startswith("GIT_CONFIG_"):
                env.pop(key, None)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["LC_ALL"] = "C"
        return env

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(
            ["git", "-c", "core.quotepath=false", *args],
            cwd=self.root,
            env=self._environment(),
            capture_output=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise AssuranceError(
                f"git {' '.join(args[:3])} failed: {_safe_detail(result.stderr.decode('utf-8', 'replace'))}"
            )
        return result

    def commit(self, value: str) -> str:
        if not GIT_SHA.fullmatch(value):
            raise AssuranceError("base/head must be a full 40-character lowercase Git SHA")
        resolved = self.run("rev-parse", "--verify", f"{value}^{{commit}}").stdout.decode().strip()
        if resolved != value:
            raise AssuranceError(f"commit binding mismatch for {value}")
        return resolved

    def head(self) -> str:
        return self.run("rev-parse", "HEAD").stdout.decode().strip()

    def merge_base(self, base: str, head: str) -> str:
        return self.run("merge-base", base, head).stdout.decode().strip()

    def tracked_clean(self) -> bool:
        status = self.run("status", "--porcelain=v1", "--untracked-files=no").stdout
        return not status.strip()

    def git_version(self) -> str:
        return self.run("--version").stdout.decode("utf-8", "replace").strip()

    def diff_bytes(self, base: str, head: str) -> bytes:
        return self.run("diff", "--binary", f"{base}...{head}").stdout

    def changes(self, base: str, head: str) -> list[dict[str, str]]:
        raw = self.run("diff", "--name-status", "-z", "--find-renames", f"{base}...{head}").stdout
        fields = raw.split(b"\x00")
        if fields and fields[-1] == b"":
            fields.pop()
        changes: list[dict[str, str]] = []
        index = 0
        while index < len(fields):
            status = fields[index].decode("ascii", "replace")
            index += 1
            if status.startswith(("R", "C")):
                if index + 1 >= len(fields):
                    raise AssuranceError("malformed NUL-delimited Git rename record")
                old_path = fields[index].decode("utf-8", "surrogateescape")
                path = fields[index + 1].decode("utf-8", "surrogateescape")
                index += 2
                changes.append({"status": status, "path": path, "old_path": old_path})
            else:
                if index >= len(fields):
                    raise AssuranceError("malformed NUL-delimited Git change record")
                path = fields[index].decode("utf-8", "surrogateescape")
                index += 1
                changes.append({"status": status, "path": path})
        return sorted(changes, key=lambda item: (item["path"], item["status"]))

    def show(self, commit: str, path: str) -> bytes | None:
        if path not in set(self.paths(commit)):
            return None
        return self.run("show", f"{commit}:{path}").stdout

    def paths(self, commit: str) -> list[str]:
        if commit not in self._paths_cache:
            raw = self.run("ls-tree", "-r", "--name-only", "-z", commit).stdout
            self._paths_cache[commit] = sorted(
                field.decode("utf-8", "surrogateescape")
                for field in raw.split(b"\x00")
                if field
            )
        return list(self._paths_cache[commit])

    def added_lines(self, base: str, head: str, path: str) -> list[str]:
        if path not in set(self.paths(base)) and path not in set(self.paths(head)):
            return []
        result = self.run(
            "diff", "--unified=0", "--no-color", f"{base}...{head}", "--", path
        )
        lines = result.stdout.decode("utf-8", "replace").splitlines()
        return [line[1:] for line in lines if line.startswith("+") and not line.startswith("+++")]


class InstalledDependencyEnvironment:
    """Inspect only packages already present on the runner.

    Import checks run in an isolated child interpreter with a scrubbed
    environment and a best-effort socket deny. No dependency is installed.
    """

    _RESULT_PREFIX = "A360_IMPORT_RESULT="

    def __init__(self, timeout_seconds: int = 10):
        self.timeout_seconds = timeout_seconds
        self._inventory: dict[str, str] | None = None
        self._package_map: dict[str, list[str]] | None = None

    def inventory(self) -> dict[str, str]:
        if self._inventory is None:
            inventory: dict[str, str] = {}
            for dist in importlib.metadata.distributions():
                name = dist.metadata.get("Name")
                if name:
                    inventory[normalize_distribution(name)] = dist.version
            self._inventory = dict(sorted(inventory.items()))
        return dict(self._inventory)

    def distributions_for_import(self, module: str) -> list[str]:
        if self._package_map is None:
            mapping = importlib.metadata.packages_distributions()
            self._package_map = {
                key: sorted({normalize_distribution(value) for value in values})
                for key, values in mapping.items()
            }
        return list(self._package_map.get(module.split(".", 1)[0], []))

    def inspect_distribution(self, distribution: str) -> DistributionInspection:
        try:
            dist = importlib.metadata.distribution(distribution)
        except importlib.metadata.PackageNotFoundError:
            return DistributionInspection(False, None, None, "distribution is not installed")
        expression = dist.metadata.get("License-Expression")
        if not expression:
            license_value = (dist.metadata.get("License") or "").strip()
            expression = license_value if license_value and license_value != "UNKNOWN" else None
        if not expression:
            classifiers = dist.metadata.get_all("Classifier") or []
            for classifier in classifiers:
                if classifier.startswith("License ::"):
                    expression = classifier.rsplit("::", 1)[-1].strip()
                    break
        return DistributionInspection(True, dist.version, expression, "installed metadata inspected")

    def inspect_import(self, module: str, symbol: str | None) -> ImportInspection:
        helper = r'''
import importlib
import importlib.util
import json
import socket
import sys

def denied(*args, **kwargs):
    raise OSError("network disabled by Change Assurance")

class DeniedSocket(socket.socket):
    def connect(self, *args, **kwargs):
        return denied(*args, **kwargs)
    def connect_ex(self, *args, **kwargs):
        denied(*args, **kwargs)

socket.socket = DeniedSocket
socket.create_connection = denied
module, symbol = sys.argv[1], sys.argv[2] or None
try:
    loaded = importlib.import_module(module)
    resolves = True
    if symbol:
        resolves = hasattr(loaded, symbol)
        if not resolves:
            try:
                resolves = importlib.util.find_spec(module + "." + symbol) is not None
            except (ImportError, ModuleNotFoundError, AttributeError, ValueError):
                resolves = False
    result = {"resolves": resolves, "detail": "isolated import resolved" if resolves else "symbol missing"}
except BaseException as exc:
    result = {"resolves": False, "detail": type(exc).__name__ + ": " + str(exc)[:160]}
print("A360_IMPORT_RESULT=" + json.dumps(result, sort_keys=True))
'''
        allowed_env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}
        }
        allowed_env.update(
            {
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONHASHSEED": "0",
                "HTTP_PROXY": "",
                "HTTPS_PROXY": "",
                "ALL_PROXY": "",
                "NO_PROXY": "*",
            }
        )
        try:
            with tempfile.TemporaryDirectory(prefix="a360-import-check-") as workdir:
                result = subprocess.run(
                    [sys.executable, "-I", "-c", helper, module, symbol or ""],
                    cwd=workdir,
                    env=allowed_env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_seconds,
                    check=False,
                )
        except subprocess.TimeoutExpired:
            return ImportInspection(False, "isolated import timed out")
        marker = next(
            (line for line in reversed(result.stdout.splitlines()) if line.startswith(self._RESULT_PREFIX)),
            None,
        )
        if marker is None:
            return ImportInspection(
                False,
                f"isolated import produced no result (exit={result.returncode})",
            )
        try:
            payload = json.loads(marker[len(self._RESULT_PREFIX) :])
        except json.JSONDecodeError:
            return ImportInspection(False, "isolated import returned invalid JSON")
        return ImportInspection(bool(payload.get("resolves")), _safe_detail(str(payload.get("detail"))))
