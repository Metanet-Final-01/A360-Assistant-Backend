"""Dependency, import, risk-profile, and protected-path checks."""
from __future__ import annotations

import ast
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .foundation import (
    REQUIREMENT,
    RISK_ORDER,
    SCHEMA_VERSION,
    SENSITIVE_SUFFIXES,
    SEVERITY_RANK,
    DependencyEnvironment,
    GitRepository,
    ImportSpec,
    RequirementRecord,
    _safe_detail,
    digest_bytes,
    normalize_distribution,
)


def parse_imports(path: str, content: bytes | None) -> tuple[list[ImportSpec], list[str]]:
    if content is None:
        return [], []
    try:
        source = content.decode("utf-8-sig")
        tree = ast.parse(source, filename=path)
    except (UnicodeDecodeError, SyntaxError) as exc:
        return [], [f"{path}: {_safe_detail(str(exc))}"]

    imports: list[ImportSpec] = []
    importlib_aliases = {"importlib"}
    import_module_aliases: set[str] = set()
    builtin_import_aliases = {"__import__"}
    builtins_aliases = {"builtins"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_aliases.add(alias.asname or alias.name)
                elif alias.name == "builtins":
                    builtins_aliases.add(alias.asname or alias.name)
                imports.append(ImportSpec(path, alias.name, None, node.lineno, "import"))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            module = node.module or ""
            if module == "importlib":
                for alias in node.names:
                    if alias.name == "import_module":
                        import_module_aliases.add(alias.asname or alias.name)
            elif module == "builtins":
                for alias in node.names:
                    if alias.name == "__import__":
                        builtin_import_aliases.add(alias.asname or alias.name)
            for alias in node.names:
                imports.append(
                    ImportSpec(path, module, None if alias.name == "*" else alias.name, node.lineno, "from")
                )

    assignments: list[tuple[list[ast.expr], ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            assignments.append((list(node.targets), node.value))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            assignments.append(([node.target], node.value))

    changed = True
    while changed:
        changed = False
        for targets, value in assignments:
            importer_kind: str | None = None
            if isinstance(value, ast.Name):
                if value.id in import_module_aliases:
                    importer_kind = "import_module"
                elif value.id in builtin_import_aliases:
                    importer_kind = "builtin"
            elif isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
                if value.attr == "import_module" and value.value.id in importlib_aliases:
                    importer_kind = "import_module"
                elif value.attr == "__import__" and value.value.id in builtins_aliases:
                    importer_kind = "builtin"
            if importer_kind is None:
                continue
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                aliases = (
                    import_module_aliases
                    if importer_kind == "import_module"
                    else builtin_import_aliases
                )
                if target.id not in aliases:
                    aliases.add(target.id)
                    changed = True

    dynamic_errors: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        is_dynamic = False
        if isinstance(node.func, ast.Name):
            is_dynamic = (
                node.func.id in builtin_import_aliases
                or node.func.id in import_module_aliases
            )
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            is_dynamic = (
                node.func.attr == "import_module" and node.func.value.id in importlib_aliases
            ) or (node.func.attr == "__import__" and node.func.value.id in builtins_aliases)
        if not is_dynamic:
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            imports.append(ImportSpec(path, node.args[0].value, None, node.lineno, "dynamic_literal"))
        else:
            dynamic_errors.append(f"{path}:{node.lineno}: non-literal dynamic import cannot be verified")

    deduped = {item.key: item for item in imports if item.module}
    return sorted(deduped.values(), key=lambda item: (item.module, item.symbol or "", item.kind)), dynamic_errors


def parse_requirements(
    repo: GitRepository, commit: str, files: list[str]
) -> tuple[dict[str, RequirementRecord], list[str]]:
    records: dict[str, RequirementRecord] = {}
    errors: list[str] = []
    for path in files:
        content = repo.show(commit, path)
        if content is None:
            continue
        try:
            lines = content.decode("utf-8-sig").splitlines()
        except UnicodeDecodeError as exc:
            errors.append(f"{path}: {_safe_detail(str(exc))}")
            continue
        for number, raw in enumerate(lines, 1):
            value = raw.split("#", 1)[0].strip()
            if not value:
                continue
            match = REQUIREMENT.fullmatch(value)
            if match is None:
                errors.append(f"{path}:{number}: dependency is not an exact == pin: {value[:120]}")
                continue
            name = match.group("name")
            normalized = normalize_distribution(name)
            record = RequirementRecord(name, normalized, match.group("version"), path, number)
            previous = records.get(normalized)
            if previous and previous.version != record.version:
                errors.append(
                    f"{normalized}: conflicting pins {previous.version} and {record.version}"
                )
                continue
            records[normalized] = record
    return records, sorted(set(errors))


def requirement_changes(
    base: dict[str, RequirementRecord], head: dict[str, RequirementRecord]
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for name in sorted(set(base) | set(head)):
        before = base.get(name)
        after = head.get(name)
        if before and after and before.version == after.version:
            continue
        kind = "added" if before is None else "removed" if after is None else "version_changed"
        changes.append(
            {
                "package": name,
                "kind": kind,
                "base": before.as_dict() if before else None,
                "head": after.as_dict() if after else None,
            }
        )
    return changes


def derive_risk_profiles(changes: list[dict[str, str]], policy: dict[str, Any]) -> list[str]:
    profiles: set[str] = set()
    dependency_paths = set(policy["dependency_paths"])
    for change in changes:
        for candidate in (change["path"], change.get("old_path")):
            if not candidate:
                continue
            path = candidate.replace("\\", "/")
            lower = path.lower()
            if path in dependency_paths:
                profiles.add("dependency")
            if path.startswith(".github/workflows/"):
                profiles.add("workflow")
            if path.startswith("tests/"):
                profiles.add("test_oracle")
            if path.startswith("assurance/change/") or path == ".github/CODEOWNERS":
                profiles.add("assurance_policy")
            if path.startswith("app/agent/"):
                profiles.add("agent_owned")
            if path.startswith("migrations/") or path == "app/models.py":
                profiles.add("database_contract")
            if path.startswith(("app/api/", "app/schemas/")) or "interface" in lower:
                profiles.add("api_contract")
            if "auth" in lower or "security" in lower:
                profiles.add("auth")
            if path.startswith("app/rag/"):
                profiles.add("rag")
            if path.startswith("infra/") or path.startswith("docker"):
                profiles.add("infra")
            if path.endswith(".py"):
                profiles.add("source")
            if path.startswith("docs/") or path.endswith(".md"):
                profiles.add("docs")
    return [profile for profile in RISK_ORDER if profile in profiles] or ["docs"]


def _matches_prefix(path: str, prefix: str) -> bool:
    return path == prefix or (prefix.endswith("/") and path.startswith(prefix))


def derive_protected_evidence(
    repo: GitRepository,
    base: str,
    head: str,
    changes: list[dict[str, str]],
    policy: dict[str, Any],
) -> dict[str, Any]:
    protected = sorted(
        {
            candidate
            for change in changes
            for candidate in (change["path"], change.get("old_path"))
            if candidate
            and any(
                _matches_prefix(candidate, prefix) for prefix in policy["protected_paths"]
            )
        }
    )
    indicators: list[dict[str, str]] = []
    silent_install_hits: list[dict[str, str]] = []
    indicator_patterns = (
        (re.compile(r"pytest\.(?:skip|xfail)|@pytest\.mark\.(?:skip|skipif|xfail)"), "TEST_WEAKENING"),
        (re.compile(r"continue-on-error\s*:\s*true", re.I), "CI_FAILURE_IGNORED"),
        (re.compile(r"pull_request_target\s*:"), "PR_TARGET_PRIVILEGE"),
        (re.compile(r"permissions\s*:\s*write-all", re.I), "CI_WRITE_ALL"),
    )
    install_pattern = re.compile(
        r"(?:^|[\s'\"\[])\b(?:pip|pip3)\b(?:[\s'\",]+)install\b|python\s+-m\s+pip\s+install",
        re.I,
    )

    def is_sensitive(candidate: str) -> bool:
        filename = Path(candidate).name
        return candidate.endswith(SENSITIVE_SUFFIXES) or filename.startswith(
            ("Dockerfile", "Containerfile")
        )

    for change in changes:
        path = change["path"]
        if not is_sensitive(path):
            continue
        old_path = change.get("old_path")
        renamed_into_sensitive_path = (
            change["status"].startswith(("R", "C"))
            and old_path is not None
            and not is_sensitive(old_path)
        )
        if renamed_into_sensitive_path:
            content = repo.show(head, path)
            lines = content.decode("utf-8", "replace").splitlines() if content else []
        else:
            lines = repo.added_lines(base, head, path)
        for line in lines:
            for pattern, code in indicator_patterns:
                if pattern.search(line):
                    indicators.append({"path": path, "code": code, "line_sha256": digest_bytes(line.encode())})
            if install_pattern.search(line):
                silent_install_hits.append(
                    {"path": path, "code": "SILENT_INSTALL_PATH", "line_sha256": digest_bytes(line.encode())}
                )
    return {
        "schema_version": SCHEMA_VERSION,
        "subject_sha": head,
        "protected_paths_changed": protected,
        "sensitive_indicators": sorted(indicators, key=lambda item: (item["path"], item["code"])),
        "silent_install_hits": sorted(
            silent_install_hits, key=lambda item: (item["path"], item["line_sha256"])
        ),
    }


def _local_roots(repo: GitRepository, head: str, policy: dict[str, Any]) -> set[str]:
    roots = set(policy["local_roots"])
    for path in repo.paths(head):
        if path.endswith(".py") and "/" not in path:
            roots.add(Path(path).stem)
        elif path.endswith("/__init__.py") and path.count("/") == 1:
            roots.add(path.split("/", 1)[0])
    return roots


def _distribution_for_import(
    spec: ImportSpec,
    policy: dict[str, Any],
    environment: DependencyEnvironment,
    head_requirements: dict[str, RequirementRecord],
) -> tuple[str | None, str]:
    mapping = policy["import_distribution_map"]
    candidates = [
        (prefix, normalize_distribution(distribution))
        for prefix, distribution in mapping.items()
        if spec.module == prefix or spec.module.startswith(prefix + ".")
    ]
    if candidates:
        prefix, distribution = max(candidates, key=lambda item: len(item[0]))
        return distribution, f"policy map {prefix} -> {distribution}"
    runtime = [normalize_distribution(item) for item in environment.distributions_for_import(spec.module)]
    direct = sorted({item for item in runtime if item in head_requirements})
    if len(direct) == 1:
        return direct[0], "runtime package-to-distribution map"
    if len(direct) > 1:
        return None, f"ambiguous distributions: {', '.join(direct)}"
    return None, "no direct requirement mapping for import"


def _new_imports(
    repo: GitRepository,
    base: str,
    head: str,
    changes: list[dict[str, str]],
) -> tuple[list[ImportSpec], list[str]]:
    additions: list[ImportSpec] = []
    errors: list[str] = []
    for change in changes:
        path = change["path"]
        if not path.endswith(".py") or change["status"].startswith("D"):
            continue
        old_path = change.get("old_path", path)
        head_imports, head_errors = parse_imports(path, repo.show(head, path))
        if old_path.endswith(".py"):
            base_imports, base_errors = parse_imports(
                old_path, repo.show(base, old_path)
            )
        else:
            base_imports, base_errors = [], []
        errors.extend(head_errors)
        errors.extend(base_errors)
        existing = {item.key for item in base_imports}
        additions.extend(item for item in head_imports if item.key not in existing)
    deduped = {(item.path, *item.key): item for item in additions}
    return sorted(
        deduped.values(), key=lambda item: (item.path, item.module, item.symbol or "", item.kind)
    ), sorted(set(errors))


def _head_import_inventory(
    repo: GitRepository,
    head: str,
) -> tuple[list[ImportSpec], list[str]]:
    """Parse every tracked Python module when dependency removal needs closure proof."""
    imports: list[ImportSpec] = []
    errors: list[str] = []
    for path in repo.paths(head):
        if not path.endswith(".py"):
            continue
        parsed, parse_errors = parse_imports(path, repo.show(head, path))
        imports.extend(parsed)
        errors.extend(parse_errors)
    deduped = {(item.path, *item.key): item for item in imports}
    return sorted(
        deduped.values(), key=lambda item: (item.path, item.module, item.symbol or "", item.kind)
    ), sorted(set(errors))


def _imports_for_distribution(
    imports: list[ImportSpec],
    distribution: str,
    policy: dict[str, Any],
    environment: DependencyEnvironment,
) -> tuple[list[ImportSpec], bool]:
    """Return proven references and whether policy has a stable import-root mapping."""
    matches: list[ImportSpec] = []
    mapping = policy["import_distribution_map"]
    mapped_roots = {
        prefix
        for prefix, value in mapping.items()
        if normalize_distribution(value) == distribution
    }
    for spec in imports:
        if spec.module.split(".", 1)[0] in sys.stdlib_module_names:
            continue
        candidates = {
            normalize_distribution(value)
            for prefix, value in mapping.items()
            if spec.module == prefix or spec.module.startswith(prefix + ".")
        }
        if not candidates:
            candidates = {
                normalize_distribution(value)
                for value in environment.distributions_for_import(spec.module)
        }
        if distribution in candidates:
            matches.append(spec)
    return matches, bool(mapped_roots)


def _rule(status: str, reasons: list[str] | None = None) -> dict[str, Any]:
    return {"status": status, "reasons": sorted(set(reasons or []))}


def _combine_rule_status(current: str, candidate: str) -> str:
    rank = {"not_applicable": 0, "pass": 1, "unassured": 2, "error": 3, "fail": 4}
    return candidate if rank[candidate] > rank[current] else current


def _license_matches(expression: str | None, allowed: set[str]) -> tuple[str, str]:
    if not expression:
        return "unassured", "installed distribution has no usable license metadata"
    normalized = expression.strip()
    aliases = {
        "MIT License": "MIT",
        "Python Software Foundation License": "PSF-2.0",
        "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized in allowed:
        return "pass", f"license {normalized} is allowed"
    return "fail", f"license is outside the allowlist: {normalized[:100]}"


def _vulnerability_status(
    package: str,
    version: str,
    policy: dict[str, Any],
    now: datetime,
) -> tuple[str, str]:
    vuln_policy = policy["vulnerability_policy"]
    try:
        generated = datetime.fromisoformat(vuln_policy["snapshot_generated_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return "error", "vulnerability snapshot timestamp is invalid"
    age_days = (now.astimezone(timezone.utc) - generated.astimezone(timezone.utc)).total_seconds() / 86400
    if age_days < 0 or age_days > int(vuln_policy["max_age_days"]):
        return "unassured", f"vulnerability snapshot is outside freshness policy ({age_days:.1f} days)"
    record = vuln_policy.get("packages", {}).get(package)
    if not isinstance(record, dict) or record.get("version") != version or record.get("reviewed") is not True:
        return "unassured", "no reviewed pinned vulnerability snapshot for this package version"
    threshold = str(vuln_policy["deny_at_or_above"]).lower()
    threshold_rank = SEVERITY_RANK.get(threshold)
    if threshold_rank is None:
        return "error", f"unknown vulnerability threshold: {threshold}"
    blocked: list[str] = []
    for advisory in record.get("advisories", []):
        severity = str(advisory.get("severity", "unknown")).lower()
        if SEVERITY_RANK.get(severity, SEVERITY_RANK["unknown"]) >= threshold_rank:
            blocked.append(str(advisory.get("id", "unknown")))
    if blocked:
        return "fail", f"advisories meet the {threshold} threshold: {', '.join(sorted(blocked))}"
    return "pass", "pinned vulnerability snapshot has no advisory at the deny threshold"


def derive_dependency_evidence(
    repo: GitRepository,
    base: str,
    head: str,
    changes: list[dict[str, str]],
    policy: dict[str, Any],
    environment: DependencyEnvironment,
    protected_evidence: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    files = list(policy["requirement_files"])
    parsed_files = set(files)
    dependency_paths = set(policy["dependency_paths"])
    unparsed_manifest_errors = [
        f"{path}: changed dependency manifest has no trusted parser"
        for path in sorted(
            {
                candidate
                for change in changes
                for candidate in (change["path"], change.get("old_path"))
                if candidate in dependency_paths and candidate not in parsed_files
            }
        )
    ]
    base_requirements, base_errors = parse_requirements(repo, base, files)
    head_requirements, head_errors = parse_requirements(repo, head, files)
    requirement_delta = requirement_changes(base_requirements, head_requirements)
    removed_packages = {
        item["package"] for item in requirement_delta if item["kind"] == "removed"
    }
    imports, import_errors = _new_imports(repo, base, head, changes)
    head_imports: list[ImportSpec] = []
    head_inventory_errors: list[str] = []
    if removed_packages:
        head_imports, head_inventory_errors = _head_import_inventory(repo, head)
    local_roots = _local_roots(repo, head, policy)
    external_imports = [
        item
        for item in imports
        if item.module.split(".", 1)[0] not in sys.stdlib_module_names
        and item.module.split(".", 1)[0] not in local_roots
    ]

    rules = {
        "dep.allowlist": _rule("not_applicable"),
        "dep.lock_match": _rule("not_applicable"),
        "dep.import_symbol": _rule("not_applicable"),
        "dep.vuln": _rule("not_applicable"),
        "dep.license": _rule("not_applicable"),
        "dep.no_silent_install": _rule("pass"),
    }
    package_checks: dict[str, dict[str, Any]] = {}
    unmapped_imports: list[dict[str, Any]] = []

    if protected_evidence["silent_install_hits"]:
        rules["dep.no_silent_install"] = _rule(
            "fail", ["PR adds a package installation path outside the trusted runner"]
        )

    parse_errors = sorted(
        set(
            base_errors
            + head_errors
            + import_errors
            + head_inventory_errors
            + unparsed_manifest_errors
        )
    )
    if parse_errors:
        for rule_id in ("dep.allowlist", "dep.lock_match", "dep.import_symbol"):
            rules[rule_id] = _rule("error", parse_errors)

    imports_by_package: dict[str, list[ImportSpec]] = {}
    for spec in external_imports:
        distribution, mapping_detail = _distribution_for_import(
            spec, policy, environment, head_requirements
        )
        if distribution is None:
            unmapped = spec.as_dict()
            unmapped["detail"] = mapping_detail
            unmapped_imports.append(unmapped)
            current = rules["dep.import_symbol"]
            current["status"] = _combine_rule_status(current["status"], "fail")
            current["reasons"].append(
                f"{spec.path}:{spec.line} {spec.module}: {mapping_detail}"
            )
            continue
        imports_by_package.setdefault(distribution, []).append(spec)

    changed_packages = {item["package"] for item in requirement_delta}
    candidate_packages = sorted(changed_packages | set(imports_by_package))
    allowed_licenses = set(policy["license_policy"]["allowed_spdx"])
    approved_additions = policy.get("approved_additions", {})

    for package in candidate_packages:
        base_record = base_requirements.get(package)
        head_record = head_requirements.get(package)
        checks: dict[str, Any] = {"imports": []}
        package_checks[package] = checks

        if head_record is None:
            if base_record is None:
                allow_status = "fail"
                allow_reason = "imported dependency is not declared with an exact requirement"
            else:
                remaining, mapping_known = _imports_for_distribution(
                    head_imports, package, policy, environment
                )
                if remaining:
                    locations = ", ".join(
                        f"{item.path}:{item.line}" for item in remaining[:5]
                    )
                    allow_status = "fail"
                    allow_reason = f"dependency was removed while imports remain at {locations}"
                elif head_inventory_errors or not mapping_known:
                    allow_status = "unassured"
                    allow_reason = "dependency removal could not prove that no imports remain"
                else:
                    allow_status = "pass"
                    allow_reason = "dependency was removed and no tracked Python import remains"
        elif base_record and base_record.version == head_record.version:
            allow_status, allow_reason = "pass", "exact version is present in trusted base requirements"
        else:
            approval = approved_additions.get(package, {})
            if (
                approval.get("version") == head_record.version
                and isinstance(approval.get("approval_ref"), str)
                and approval["approval_ref"].strip()
            ):
                allow_status, allow_reason = "pass", "exact version has an explicit policy approval"
            else:
                allow_status, allow_reason = "fail", "new or changed dependency lacks exact policy approval"
        checks["allowlist"] = {"status": allow_status, "detail": allow_reason}
        rules["dep.allowlist"]["status"] = _combine_rule_status(
            rules["dep.allowlist"]["status"], allow_status
        )
        rules["dep.allowlist"]["reasons"].append(f"{package}: {allow_reason}")

        if head_record is None:
            removal_status = allow_status
            rules["dep.import_symbol"]["status"] = _combine_rule_status(
                rules["dep.import_symbol"]["status"], removal_status
            )
            rules["dep.import_symbol"]["reasons"].append(f"{package}: {allow_reason}")
            checks["lock"] = {
                "status": "not_applicable",
                "detail": "removed dependencies have no head version to match",
            }
            checks["vulnerability"] = {
                "status": "not_applicable",
                "detail": "removed dependencies have no shipped version to scan",
            }
            checks["license"] = {
                "status": "not_applicable",
                "detail": "removed dependencies have no shipped license to approve",
            }
            continue

        distribution = environment.inspect_distribution(package)
        if not distribution.installed:
            lock_status, lock_reason = "unassured", distribution.detail
        elif distribution.version != head_record.version:
            lock_status = "fail"
            lock_reason = (
                f"installed version {distribution.version} does not match exact pin {head_record.version}"
            )
        else:
            lock_status, lock_reason = "pass", "installed version matches exact requirement pin"
        checks["lock"] = {"status": lock_status, "detail": lock_reason}
        rules["dep.lock_match"]["status"] = _combine_rule_status(
            rules["dep.lock_match"]["status"], lock_status
        )
        rules["dep.lock_match"]["reasons"].append(f"{package}: {lock_reason}")

        specs = imports_by_package.get(package, [])
        if not specs:
            approval_roots = approved_additions.get(package, {}).get("import_roots", [])
            specs = [ImportSpec("<policy>", root, None, 0, "policy_root") for root in approval_roots]
        if not specs:
            import_status, import_reason = "unassured", "no import root is available for resolution"
            rules["dep.import_symbol"]["status"] = _combine_rule_status(
                rules["dep.import_symbol"]["status"], import_status
            )
            rules["dep.import_symbol"]["reasons"].append(f"{package}: {import_reason}")
        else:
            import_status = "pass"
            for spec in specs:
                inspection = environment.inspect_import(spec.module, spec.symbol)
                status = "pass" if inspection.resolves else "fail"
                import_status = _combine_rule_status(import_status, status)
                checks["imports"].append(
                    {**spec.as_dict(), "status": status, "detail": inspection.detail}
                )
                rules["dep.import_symbol"]["reasons"].append(
                    f"{spec.module}{'.' + spec.symbol if spec.symbol else ''}: {inspection.detail}"
                )
            rules["dep.import_symbol"]["status"] = _combine_rule_status(
                rules["dep.import_symbol"]["status"], import_status
            )

        vuln_status, vuln_reason = _vulnerability_status(
            package, head_record.version, policy, now
        )
        checks["vulnerability"] = {"status": vuln_status, "detail": vuln_reason}
        rules["dep.vuln"]["status"] = _combine_rule_status(
            rules["dep.vuln"]["status"], vuln_status
        )
        rules["dep.vuln"]["reasons"].append(f"{package}: {vuln_reason}")

        license_status, license_reason = _license_matches(
            distribution.license_expression if distribution.installed else None,
            allowed_licenses,
        )
        checks["license"] = {"status": license_status, "detail": license_reason}
        rules["dep.license"]["status"] = _combine_rule_status(
            rules["dep.license"]["status"], license_status
        )
        rules["dep.license"]["reasons"].append(f"{package}: {license_reason}")

    for value in rules.values():
        value["reasons"] = sorted(set(value["reasons"]))

    decision_state = policy["policy_decision_state"]
    active_candidate_packages = [
        package for package in candidate_packages if package in head_requirements
    ]
    if active_candidate_packages and decision_state not in {"approved", "approved_fixture"}:
        reason = "vulnerability and license policy still requires a human approval decision"
        for rule_id in ("dep.vuln", "dep.license"):
            rules[rule_id]["status"] = _combine_rule_status(
                rules[rule_id]["status"], "unassured"
            )
            rules[rule_id]["reasons"] = sorted(set([*rules[rule_id]["reasons"], reason]))

    return {
        "schema_version": SCHEMA_VERSION,
        "subject_sha": head,
        "policy_decision_state": decision_state,
        "baseline_allowlist_source": {
            "type": "trusted_base_exact_requirements",
            "base_sha": base,
            "packages": {
                name: record.version for name, record in sorted(base_requirements.items())
            },
        },
        "head_requirements": {
            name: record.as_dict() for name, record in sorted(head_requirements.items())
        },
        "requirement_changes": requirement_delta,
        "new_imports": [item.as_dict() for item in imports],
        "external_imports": [item.as_dict() for item in external_imports],
        "unmapped_imports": unmapped_imports,
        "parse_errors": parse_errors,
        "package_checks": package_checks,
        "rules": rules,
        "silent_install_hits": protected_evidence["silent_install_hits"],
    }


def _overall_dependency_status(evidence: dict[str, Any]) -> str:
    statuses = {value["status"] for value in evidence["rules"].values()}
    if "fail" in statuses:
        return "fail"
    if "error" in statuses:
        return "error"
    if "unassured" in statuses:
        return "unassured"
    if statuses == {"not_applicable", "pass"} and not evidence["package_checks"]:
        return "not_applicable"
    return "pass"
