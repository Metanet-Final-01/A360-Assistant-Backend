"""Final independent checks for the Phase 0 v1.10 convergence conditions.

Writes disposable fixtures only below phase0-v1.10-src/.out. No product or
published v1.10 source file is modified.
"""
from __future__ import annotations

import ast
import inspect
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path


HERE = Path(__file__).resolve().parent
SRC = HERE / "phase0-v1.10-src"
sys.path.insert(0, str(SRC))

import core  # noqa: E402
import rules  # noqa: E402
import vertical_paths as vp  # noqa: E402
from attest import Attestor  # noqa: E402
from core import (ATTESTOR_VERSION, Artifact, StoreReadError, TrustedContext,  # noqa: E402
                  change_receipt_id)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def unique_dir(stem: str) -> Path:
    root = SRC / ".out"
    root.mkdir(exist_ok=True)
    index = 0
    candidate = root / stem
    while candidate.exists():
        index += 1
        candidate = root / f"{stem}-{index}"
    return candidate


def make_subject_repo(stem: str = "codex-v110-subject"):
    repo = unique_dir(stem)
    (repo / "app" / "api").mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "codex@example.invalid")
    git(repo, "config", "user.name", "codex")
    (repo / "README.md").write_text("subject\n", encoding="utf-8")
    target = repo / "app" / "api" / "sessions.py"
    target.write_text(
        "from app.agent import stream_agent_turn\n\n\ndef turn():\n    return stream_agent_turn\n",
        encoding="utf-8",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "baseline")
    base = git(repo, "rev-parse", "HEAD")
    target.write_text(
        "from app.agent import stream_agent_turn\n\n\ndef turn(x):\n    return stream_agent_turn\n",
        encoding="utf-8",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "head")
    return repo, base, git(repo, "rev-parse", "HEAD")


def runner_env(repo_id: str, base: str, head: str) -> dict:
    return {
        "A360_REPO_ID": repo_id,
        "A360_REPO_NAME": "codex-independent",
        "A360_EVENT": "pull_request",
        "A360_BASE_SHA": base,
        "A360_HEAD_SHA": head,
        "A360_CHECKOUT_SHA": head,
        "A360_MERGE_BASE": base,
        "A360_WORKFLOW_REF": "codex/final-v110",
        "A360_CHECKOUT_MODEL": "head_checkout",
    }


def prepare_allowed_change():
    repo, base, head = make_subject_repo()
    env = runner_env("R_codex_v110", base, head)
    attestor = Attestor.for_testing_only(SRC, repo, vp.NOW, vp.authority())
    context = TrustedContext.from_runner_env(env, repo)
    manifest, report, _ = vp.build_change_docs(attestor, context, repo, tag="_codex_v110")
    result = attestor.attest_change(
        env, manifest, report, SRC / "control-registry.yaml"
    )
    assert result.allowed and result.receipt
    return repo, context, manifest, report, result.receipt


def check_runner_construction(repo: Path) -> tuple[bool, str]:
    signature = inspect.signature(Attestor.for_runner)
    rejected_keyword = False
    try:
        Attestor.for_runner(
            repo=repo,
            now=vp.NOW,
            authority=vp.authority(
                policy_dir=SRC / "policies_approved_fixture", source="ci_fixture_alias"
            ),
            src_dir=SRC / "policies_approved_fixture",
        )
    except TypeError:
        rejected_keyword = True

    runner = Attestor.for_runner(
        repo,
        vp.NOW,
        vp.authority(
            policy_dir=SRC / "policies_approved_fixture", source="ci_fixture_alias"
        ),
    )
    state = runner.resolver.resolve("operation-policy").obj["state"]
    ok = (
        "src_dir" not in signature.parameters
        and rejected_keyword
        and runner.policy_root == SRC / "policies"
        and state == "unapproved"
    )
    return ok, f"signature={signature}, root={runner.policy_root}, state={state}"


def check_generic_receipt_policy_set(repo: Path, context: TrustedContext,
                                     manifest: Path, report: Path,
                                     receipt: dict) -> tuple[bool, str]:
    forged = json.loads(json.dumps(receipt))
    forged["policy_digests"].pop("severity-policy")
    authority_digests = dict(forged["policy_digests"])
    authority_digests["registry"] = forged["control_registry_digest"]
    authority_digests["schemas"] = forged["schema_digest"]
    forged["receipt_id"] = change_receipt_id(
        forged["repository_id"],
        forged["commit"],
        forged["assurance_report_digest"],
        forged["change_manifest_digest"],
        authority_digests,
        ATTESTOR_VERSION,
        forged["decision"],
        forged["evidence_valid"],
    )
    raw = json.dumps(forged, sort_keys=True).encode()
    attestor = Attestor.for_testing_only(
        SRC,
        repo,
        vp.NOW,
        vp.authority(receipts={forged["receipt_id"]: raw}),
    )
    report_artifact = Artifact.load(report, attestor.validators["assurance-report"])
    bundle, runtime = vp.build_output_docs(
        attestor, context, report_artifact, forged["receipt_id"]
    )
    result = attestor.attest_output(
        bundle,
        runtime,
        report,
        manifest,
        "BR-1",
        SRC / "control-registry.yaml",
    )
    reasons = " | ".join(result.evidence_reasons)
    return (
        not result.allowed and "receipt_policy_set_mismatch" in reasons,
        reasons[:240],
    )


def check_policy_set_is_current(repo: Path, receipt: dict) -> tuple[bool, str]:
    attestor = Attestor.for_testing_only(SRC, repo, vp.NOW, vp.authority())
    registry = Artifact.load(
        SRC / "control-registry.yaml", attestor.validators["control-registry"]
    )
    resolved = attestor.resolved_digests(registry)
    expected = {key for key in resolved if key.endswith("-policy")}
    actual = set(receipt["policy_digests"])

    # The rule derives its expected set from resolved_digests, so a future policy is fail-closed.
    future = dict(resolved, **{"future-policy": core.sha256_bytes(b"future")})
    receipt_artifact = type("Receipt", (), {
        "obj": core.deep_freeze(receipt),
        "receipt_id": receipt["receipt_id"],
    })()
    bundle = type("Bundle", (), {
        "obj": core.deep_freeze({
            "subject": {
                "repository_id": receipt["repository_id"],
                "commit": receipt["commit"],
            }
        })
    })()
    report = type("Report", (), {
        "digest": receipt["assurance_report_digest"],
        "obj": core.deep_freeze({
            "manifest_binding": {
                "manifest_head_sha": receipt["commit"],
                "manifest_repository_id": receipt["repository_id"],
            },
            "subject": {
                "repository_id": receipt["repository_id"],
                "commit": receipt["commit"],
            },
        }),
    })()
    future_result = rules.hl19(
        bundle,
        receipt_artifact,
        report,
        receipt["change_manifest_digest"],
        future,
    )
    ok = (
        actual == expected
        and not future_result.ok
        and future_result.reason == "receipt_policy_set_mismatch"
    )
    return ok, f"current={sorted(actual)}, future_reason={future_result.reason}"


def selftest_has_manual_policy_list() -> tuple[bool, str]:
    source = (SRC / "contract_self_test.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    policy_values = set()
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "DIGESTS" for target in node.targets
        ):
            found = True
            for child in ast.walk(node.value):
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    if child.value.endswith("-policy"):
                        policy_values.add(child.value)
    return found and bool(policy_values), f"manual_policy_ids={sorted(policy_values)}"


def check_observer_boundary() -> tuple[bool, str]:
    output_parameters = set(inspect.signature(Attestor.attest_output).parameters)
    default_observer = replace(vp.authority(), public_boundary=None).observer()
    failed_closed = False
    try:
        default_observer.observe()
    except StoreReadError:
        failed_closed = True
    ok = (
        "observer" not in output_parameters
        and "public_boundary" not in output_parameters
        and failed_closed
    )
    return ok, f"attest_output_params={sorted(output_parameters)}, default_failed_closed={failed_closed}"


def check_porcelain_old_vs_new() -> tuple[bool, str]:
    repo, base, head = make_subject_repo("codex-v110-porcelain")
    dotfile = repo / ".env.sample"
    dotfile.write_text("COMMITTED=1\n", encoding="utf-8")
    git(repo, "add", ".env.sample")
    git(repo, "commit", "-q", "-m", "add dotfile")
    head = git(repo, "rev-parse", "HEAD")
    base = git(repo, "rev-parse", "HEAD~1")
    dotfile.write_text("WORKTREE_ONLY=999\n", encoding="utf-8")
    (repo / "z.tmp").write_text("untracked\n", encoding="utf-8")

    raw = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    old_paths = tuple(sorted(line[3:] for line in raw.strip().splitlines() if line.strip()))
    context = TrustedContext.from_runner_env(
        runner_env("R_codex_porcelain", base, head), repo
    )
    reader = context.object_reader(repo)
    ok = (
        raw.splitlines()[0] == " M .env.sample"
        and old_paths[0] == "env.sample"
        and context.dirty_paths == (".env.sample", "z.tmp")
        and reader.read_text(".env.sample") == "COMMITTED=1\n"
        and reader.read_text("z.tmp") is None
    )
    return ok, f"old={old_paths}, fixed={context.dirty_paths}"


def main() -> int:
    repo, context, manifest, report, receipt = prepare_allowed_change()
    checks = [
        ("runner_root_not_an_assessed_call_argument", *check_runner_construction(repo)),
        ("receipt_exact_set_is_generic", *check_generic_receipt_policy_set(
            repo, context, manifest, report, receipt
        )),
        ("resolved_policy_set_is_current_and_future_fail_closed", *check_policy_set_is_current(
            repo, receipt
        )),
        ("observer_is_construction_bound_and_default_fail_closed", *check_observer_boundary()),
        ("pre_fix_porcelain_fails_while_v110_passes", *check_porcelain_old_vs_new()),
    ]
    duplicate_found, duplicate_detail = selftest_has_manual_policy_list()

    for name, ok, detail in checks:
        print(f"{name}: ok={ok} {detail}")
    print(
        "selftest_policy_authority_list_is_still_manual_duplicate: "
        f"observed={duplicate_found} {duplicate_detail}"
    )
    print(f"checks={sum(1 for _, ok, _ in checks if ok)}/{len(checks)}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
