"""Deterministic RPA-179 regression for the 25 actionable post-freeze findings."""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import yaml


ACTIONABLE = {"corrective", "qualified"}


@dataclass(frozen=True)
class Case:
    name: str
    findings: tuple[str, ...]
    fn: object


CASES: list[Case] = []


def case(name: str, *findings: str):
    def decorate(fn):
        CASES.append(Case(name, tuple(findings), fn))
        return fn

    return decorate


def require(condition: bool, detail: str) -> None:
    if not condition:
        raise AssertionError(detail)


def without_coverage_environment(env: dict[str, str]) -> dict[str, str]:
    """Do not let nested fixture processes join a parent pytest-cov run."""
    for key in tuple(env):
        if key.startswith("COV_CORE_") or key in {"COVERAGE_FILE", "COVERAGE_PROCESS_START"}:
            env.pop(key, None)
    return env


def git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=repo, input=input_bytes, capture_output=True
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {result.stderr.decode('utf-8', 'replace')[:160]}"
        )
    return result.stdout


def git_text(repo: Path, *args: str) -> str:
    return git(repo, *args).decode("utf-8").strip()


def validators_reject(validator, obj) -> bool:
    return bool(list(validator.iter_errors(obj)))


def configure(source: Path, matrix_path: Path) -> None:
    global SRC, MATRIX, TEMP_ROOT, core, rules, store, attest, vertical, cst
    SRC = source.resolve()
    MATRIX = yaml.safe_load(matrix_path.read_text(encoding="utf-8"))
    TEMP_ROOT = Path(os.environ.get("A360_HARNESS_OUT", str(SRC / ".out-rpa179"))).resolve() / "tmp"
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(SRC))
    import core as _core
    import rules as _rules
    import store as _store
    import attest as _attest
    import vertical_paths as _vertical
    import contract_self_test as _cst

    core, rules, store, attest, vertical, cst = (
        _core,
        _rules,
        _store,
        _attest,
        _vertical,
        _cst,
    )


@contextmanager
def case_temp(prefix: str):
    for index in range(1000):
        path = TEMP_ROOT / f"{prefix}{index:03d}"
        try:
            path.mkdir()
        except FileExistsError:
            continue
        yield str(path)
        return
    raise RuntimeError(f"no deterministic fixture directory available for {prefix}")


def policy_version_fixture(resolver, approval_store, resolved_version="v2", observer_version="v2"):
    policy = resolver.resolve("agent-version-policy")
    observer = store.FixturePublicContractAdapter(resolved_version=observer_version)
    observed = observer.observe()
    runtime = SimpleNamespace(
        obj={
            "agent_provenance": {
                "requested_agent_version": resolved_version,
                "boundary_derived_default": "v2",
                "producer_resolved_version": resolved_version,
                "resolved_version_observability": "observed_from_public_contract",
                "agent_registry_snapshot_digest": observed.registry_snapshot_digest,
                "public_contract_version": observed.public_contract_version,
                "agent_version_policy_digest": policy.digest,
            }
        }
    )
    return rules.hl20(
        resolver,
        runtime,
        {"agent-version-policy": policy.digest},
        approval_store,
        vertical.NOW,
        observer,
    )


@case("typed_deny", "CR-01")
def typed_deny():
    repo, base, head = vertical.make_subject_repo()
    env = {
        "A360_REPO_ID": "R_synthetic_subject",
        "A360_EVENT": "pull_request",
        "A360_BASE_SHA": base,
        "A360_HEAD_SHA": head,
        "A360_CHECKOUT_SHA": head,
        "A360_MERGE_BASE": base,
        "A360_WORKFLOW_REF": "wf",
        "A360_CHECKOUT_MODEL": "head_checkout",
    }
    authority = vertical.authority()
    at = attest.Attestor.for_testing_only(SRC, repo, vertical.NOW, authority)
    ctx = core.TrustedContext.from_runner_env(env, repo)
    manifest, report, _ = vertical.build_change_docs(at, ctx, repo, tag="_rpa179")
    with case_temp("rpa179-typed-") as td:
        bad = Path(td) / "bad.json"
        bad.write_bytes(b"\xff\xfe")
        change = at.attest_change(env, bad, report, SRC / "control-registry.yaml")
        output = at.attest_output(bad, None, None, None, None, SRC / "control-registry.yaml")
        require(change.decision == "deny" and output.decision == "deny", "invalid artifacts escaped deny")
        copied = Path(td) / "source"
        shutil.copytree(
            SRC, copied, ignore=shutil.ignore_patterns(".git", ".out*", "__pycache__")
        )
        (copied / "policies_approved_fixture" / "dependency-path-policy.yaml").unlink()
        missing = attest.Attestor.for_testing_only(
            copied,
            repo,
            vertical.NOW,
            vertical.authority(policy_dir=copied / "policies_approved_fixture"),
        )
        result = missing.attest_change(env, manifest, report, copied / "control-registry.yaml")
        require(result.decision == "deny", "unresolvable policy escaped typed deny")
        require(any("input_unresolvable" in reason for reason in result.evidence_reasons),
                "policy failure was not normalized")


@case("synthetic_git_paths", "CR-02", "CR-26")
def synthetic_git_paths():
    with case_temp("rpa179-git-") as td:
        # Keep Git object paths below MAX_PATH while making its optional
        # revision-as-path probe cross the boundary on Windows. A long caller
        # path needs no extra padding.
        base_repo = Path(td)
        padding = max(0, 180 - len(str(base_repo)) - len("long-"))
        repo = base_repo / ("long-" + "x" * padding) if padding else base_repo
        if repo != base_repo:
            repo.mkdir()
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "harness@example.invalid")
        git(repo, "config", "user.name", "harness")
        git(repo, "commit", "--allow-empty", "-q", "-m", "base")
        base = git_text(repo, "rev-parse", "HEAD")
        blob = git(repo, "hash-object", "-w", "--stdin", input_bytes=b"content\n").decode().strip()
        special = "odd\nname.py"
        tree_record = f"100644 blob {blob}\t".encode() + special.encode() + b"\0"
        tree = git(repo, "mktree", "-z", input_bytes=tree_record).decode().strip()
        head = git(repo, "commit-tree", tree, "-p", base, input_bytes=b"head\n").decode().strip()
        git(repo, "update-ref", "HEAD", head)
        env = {
            "A360_REPO_ID": "R_path_fixture",
            "A360_EVENT": "pull_request",
            "A360_BASE_SHA": base,
            "A360_HEAD_SHA": head,
            "A360_CHECKOUT_SHA": head,
            "A360_MERGE_BASE": base,
            "A360_WORKFLOW_REF": "wf",
            "A360_CHECKOUT_MODEL": "head_checkout",
        }
        ctx = core.TrustedContext.from_runner_env(env, repo)
        require(ctx.authoritative, f"synthetic context downgraded: {ctx.downgrade_reason}")
        require(ctx.dirty_paths and special in ctx.dirty_paths, "dirty-path check was vacuous")
        require(ctx.changed_paths(repo) == (special,), f"NUL diff parsing lost path: {ctx.changed_paths(repo)!r}")
        require(special in ctx.object_reader(repo).list_files(), "NUL tree parsing lost path")


@case("coverage_matrix", "CR-03")
def coverage_matrix():
    actionable = [item for item in MATRIX["findings"] if item["disposition"] in ACTIONABLE]
    require(len(actionable) == 25, f"expected 25 actionable findings, got {len(actionable)}")
    require(all(item.get("test") for item in actionable), "an actionable finding has no regression")
    expected: dict[str, set[str]] = {}
    for item in actionable:
        expected.setdefault(item["test"], set()).add(item["id"])
    registered = {item.name: set(item.findings) for item in CASES}
    require(len(registered) == len(CASES), "duplicate regression case name")
    require(expected == registered, f"matrix/case mapping drift: expected={expected} registered={registered}")


@case("portable_paths", "CR-04", "CR-18")
def portable_paths():
    maintained = ["contract_self_test.py", "vertical_paths.py", "selfattack.py"]
    for name in maintained:
        text = (SRC / name).read_text(encoding="utf-8")
        require("D:\\" not in text and "C:\\Users\\" not in text,
                f"author-specific absolute path remains in {name}")


@case("registry_evidence_scope", "CR-05")
def registry_evidence_scope():
    text = (SRC / "control-registry.yaml").read_text(encoding="utf-8")
    require("git diff ed1a1b0..HEAD -- app/agent -> empty" not in text, "stale empty-diff claim remains")
    require("app/agent unchanged baseline..HEAD" not in text, "stale unchanged claim remains")
    require("8 changed files" in text, "corrected scope is not explicit")


@case("artifact_errors", "CR-06")
def artifact_errors():
    for uri, raw in (("bad.json", b"\xff"), ("bad.json", b"{"), ("bad.yaml", b"x: [")):
        try:
            core.Artifact.from_bytes(uri, raw)
        except core.ArtifactError:
            continue
        raise AssertionError(f"{uri} did not raise ArtifactError")


@case("root_script_scan", "CR-07")
def root_script_scan():
    dep = cst.R.resolve("dependency-path-policy").obj
    hits, unreadable = rules.silent_install_hits(
        ["bootstrap.sh"], lambda _: "python -m pip install invented-package\n", dep
    )
    require(hits == ("bootstrap.sh",) and not unreadable, "root shell script escaped scan")


@case("waiver_duration", "CR-08")
def waiver_duration():
    with case_temp("rpa179-waiver-") as td:
        root = Path(td) / "policies"
        shutil.copytree(SRC / "policies", root)
        waiver_path = root / "waivers" / "WV-1.yaml"
        waiver = yaml.safe_load(waiver_path.read_text(encoding="utf-8"))
        waiver["expires_at"] = "2026-10-08T23:00:00Z"
        waiver_path.write_text(yaml.safe_dump(waiver, sort_keys=False), encoding="utf-8")
        artifact_store = core.ArtifactStore(root, cst.V)
        error = rules._resolve_waiver(
            artifact_store, cst.REG, "WV-1", "CH-04", cst.SUBJECT, cst.NOW, {"CH-08"}
        )
        require(error is not None and "lifetime" in error, "90d23h waiver was accepted")


@case("agent_policy_approval", "CR-09")
def agent_policy_approval():
    fixture = SRC / "policies_approved_fixture"
    resolver = core.PolicyResolver(fixture, cst.V)
    approval_store = core.ArtifactStore(fixture, cst.V)
    require(policy_version_fixture(resolver, approval_store).ok, "valid protected policy approval failed")
    with case_temp("rpa179-policy-approval-") as td:
        root = Path(td) / "policies"
        shutil.copytree(fixture, root)
        approval_path = root / "policy-approvals" / "PA-1.yaml"
        approval = yaml.safe_load(approval_path.read_text(encoding="utf-8"))
        approval["subject"]["policy_digest"] = "sha256:" + "0" * 64
        approval_path.write_text(yaml.safe_dump(approval, sort_keys=False), encoding="utf-8")
        result = policy_version_fixture(
            core.PolicyResolver(root, cst.V), core.ArtifactStore(root, cst.V)
        )
        require(not result.ok and result.reason == "agent_version_policy_approval_unresolved",
                "wrong policy approval subject was accepted")


@case("control_state", "CR-10")
def control_state():
    controls = copy.deepcopy(cst.eb()["controls"])
    controls[0]["status"] = "warn"
    controls[0].pop("evidence_ref", None)
    bundle = cst.art(cst.eb(controls=controls), "cr10", "evidence-bundle")
    result = rules.hl15(cst.REG, bundle, cst._rv_art, cst._BREC, cst.DIGESTS, cst.AS, cst.NOW)
    require(not result.ok and result.reason == "control_not_passing", "HL-15 accepted warn control")


@case("dynamic_import_alias", "CR-11")
def dynamic_import_alias():
    source = {"app/api/alias.py": "from importlib import import_module as load\nload('app.agent.verify')\n"}
    result = rules.hl06(cst._EmptyReader(), cst.PUBLIC, source)
    require(not result.ok and result.reason == "private_agent_import", "aliased import escaped HL-06")
    rebound = {
        "app/api/rebound.py": (
            "import importlib\nmod = importlib\nmod.import_module('app.agent.verify')\n"
        )
    }
    result = rules.hl06(cst._EmptyReader(), cst.PUBLIC, rebound)
    require(not result.ok and result.reason == "private_agent_import",
            "rebound importlib module escaped HL-06")
    computed = {"app/api/computed.py": "from importlib import import_module as load\nload(target)\n"}
    result = rules.hl06(cst._EmptyReader(), cst.PUBLIC, computed)
    require(not result.ok and result.reason == "source_indeterminate", "computed import did not fail closed")


@case("confirmed_evidence", "CR-12")
def confirmed_evidence():
    registry = core.thaw(cst.REG.obj)
    def mappings(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from mappings(child)
        elif isinstance(value, list):
            for child in value:
                yield from mappings(child)

    claim = next(item for item in mappings(registry)
                 if item.get("status") in {"Baseline-Confirmed", "Current-Confirmed"}
                 and "evidence_kind" in item)
    claim["evidence_kind"] = "none"
    require(validators_reject(cst.V["control-registry"], registry),
            "confirmed evidence_kind=none passed schema")


@case("rule_versions", "CR-13")
def rule_versions():
    missing = cst.eb()
    missing.pop("rule_versions")
    empty = cst.eb(rule_versions=[])
    require(validators_reject(cst.V["evidence-bundle"], missing), "missing rule_versions passed")
    require(validators_reject(cst.V["evidence-bundle"], empty), "empty rule_versions passed")
    bundle = cst.art(cst.eb(), "cr13", "evidence-bundle")
    result = rules.hl21(bundle, ["HL-13", "HL-14", "HL-15", "HL-16", "HL-19", "HL-21"])
    require(not result.ok and result.reason == "rule_version_mismatch", "runtime rule drift passed")


@case("runtime_provenance", "CR-14")
def runtime_provenance():
    runtime = cst.rv()
    runtime["agent_provenance"]["producer_resolved_version"] = "v2"
    runtime["agent_provenance"]["resolved_version_observability"] = "not_observable_pending_D21"
    require(validators_reject(cst.V["runtime-validation"], runtime),
            "unobservable provenance carried a resolved version")


@case("waiver_approval", "CR-15")
def waiver_approval():
    with case_temp("rpa179-waiver-approval-") as td:
        root = Path(td) / "policies"
        shutil.copytree(SRC / "policies", root)
        (root / "waiver-approvals" / "WA-1.yaml").unlink()
        error = rules._resolve_waiver(
            core.ArtifactStore(root, cst.V), cst.REG, "WV-1", "CH-04", cst.SUBJECT,
            cst.NOW, {"CH-08"}
        )
        require(error is not None and "protected waiver approval" in error,
                "unresolved waiver approval_ref was accepted")


@case("selfcheck_exit", "CR-16")
def selfcheck_exit():
    with case_temp("rpa179-selfcheck-") as td:
        env = without_coverage_environment(os.environ.copy())
        env["A360_HARNESS_OUT"] = str(Path(td) / "generated")
        result = subprocess.run(
            [sys.executable, "-B", "selfattack.py"], cwd=SRC, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
        )
        require(
            result.returncode == 0,
            f"selfcheck failed: stdout={result.stdout[-300:]} stderr={result.stderr[-300:]}",
        )
        require("SELF-ATTACK ALLOWLIST: exact D-16 residual set matched" in result.stdout,
                "selfcheck did not enforce the published residual set")


@case("git_failure", "CR-19")
def git_failure():
    with case_temp("rpa179-not-git-") as td:
        try:
            vertical.git(Path(td), "rev-parse", "--verify", "refs/heads/definitely-missing-rpa179")
        except RuntimeError:
            return
        raise AssertionError("Git helper ignored a non-zero exit")


@case("reason_assertion", "CR-20")
def reason_assertion():
    fixture = SRC / "policies_approved_fixture"
    result = policy_version_fixture(
        core.PolicyResolver(fixture, cst.V), core.ArtifactStore(fixture, cst.V),
        resolved_version="v404", observer_version="v2"
    )
    require(not result.ok and result.reason == "agent_version_not_observable",
            f"wrong deny reason accepted: {result.reason}")


@case("full_projection", "CR-21")
def full_projection():
    derived = copy.deepcopy(cst.proj(cst.SRC_ONLY))
    derived["unreadable_scanned_paths"] = ["scripts/unreadable.ps1"]
    result = rules.hl12(cst.CTX, cst._cm_src, cst._rep_src_art, derived, cst.DIGESTS)
    require(not result.ok and result.reason == "derived_facts_mismatch",
            "unreadable_scanned_paths was not part of HL-12 projection")


@case("policy_closed", "CR-24")
def policy_closed():
    policy = yaml.safe_load((SRC / "policies" / "operation-policy.yaml").read_text(encoding="utf-8"))
    policy["approval_typo"] = None
    require(validators_reject(cst.V["policy"], policy), "unknown top-level policy field passed")


@case("receipt_positive", "CR-25")
def receipt_positive():
    raw = json.dumps(cst._GOOD_RECEIPT, sort_keys=True).encode()
    receipt_store = store.ChangeReceiptStore(validator=cst.V["change-receipt"])
    receipt_id = receipt_store.put(raw)
    resolved = receipt_store.resolve(receipt_id)
    require(resolved.receipt_id == receipt_id and resolved.obj["decision"] == "allow",
            "new valid receipt did not survive put/resolve")


@case("applicability_companions", "CR-27")
def applicability_companions():
    policy = yaml.safe_load((SRC / "policies" / "applicability-policy.yaml").read_text(encoding="utf-8"))
    risk = copy.deepcopy(policy)
    risk_entry = next(item for item in risk["applicability"] if item["trigger"] == "risk_profile")
    risk_entry.pop("risk_profile")
    path = copy.deepcopy(policy)
    path_entry = next(item for item in path["applicability"] if item["trigger"] == "path_glob")
    path_entry.pop("paths")
    require(validators_reject(cst.V["policy"], risk), "risk_profile trigger lacked risk_profile")
    require(validators_reject(cst.V["policy"], path), "path_glob trigger lacked paths")


@case("receipt_poison", "CR-28")
def receipt_poison():
    receipt_store = store.ChangeReceiptStore(validator=cst.V["change-receipt"])
    poisoned = json.dumps({"receipt_id": cst._GOOD_RID}).encode()
    try:
        receipt_store.put(poisoned)
    except core.StoreReadError:
        pass
    else:
        raise AssertionError("malformed receipt was stored")
    raw = json.dumps(cst._GOOD_RECEIPT, sort_keys=True).encode()
    require(receipt_store.put(raw) == cst._GOOD_RID, "malformed receipt poisoned an immutable key")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    configure(args.source, args.matrix)

    results = []
    for entry in CASES:
        try:
            entry.fn()
            results.append({"name": entry.name, "findings": list(entry.findings), "passed": True})
        except Exception as exc:
            results.append({
                "name": entry.name,
                "findings": list(entry.findings),
                "passed": False,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            })

    actionable = {
        item["id"] for item in MATRIX["findings"] if item["disposition"] in ACTIONABLE
    }
    covered = {finding for result in results if result["passed"] for finding in result["findings"]}
    report = {
        "schema_version": "rpa179.1",
        "cases": results,
        "actionable_findings": sorted(actionable),
        "covered_findings": sorted(covered),
        "coverage_complete": covered == actionable,
    }
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["coverage_complete"] and all(item["passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
