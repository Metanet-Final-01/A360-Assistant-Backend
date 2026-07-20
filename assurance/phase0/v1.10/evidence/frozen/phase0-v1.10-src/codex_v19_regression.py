"""Regression for the five observations Codex reproduced against v1.9, plus a deterministic Git case.

All five were FIRST reproduced against the untouched v1.9 source (5/5) and only then fixed.

Case 5 is the one Codex asked for explicitly: v1.9's Git evidence lived in `codex_v15_regression.py`,
which reads the developer's real product worktree. If that tree is clean the dirty-path loop is empty
and the assertion passes without checking anything - the test's strength depended on who had edited
what. Here the repository is built by this file, so the first porcelain line is ALWAYS `" M .env.sample"`
(a modified tracked dotfile: the exact shape that ate the leading character) with an untracked file
behind it.

This is a contract self-test, not an attestation. See D-16.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from attest import Attestor  # noqa: E402
from core import ATTESTOR_VERSION, Artifact, TrustedContext, change_receipt_id  # noqa: E402
from vertical_paths import (NOW, authority, build_change_docs, build_output_docs,  # noqa: E402
                            make_subject_repo)

FIX = HERE / "policies_approved_fixture"
reg = HERE / "control-registry.yaml"
OUT = HERE / ".out"
score = {"closed": 0, "held": 0, "open": 0}


def closed(name, ok, detail):
    score["closed" if ok else "open"] += 1
    print(f"{'CLOSED' if ok else 'OPEN':<7} | {name}\n           | {detail}")


def held(name, ok, detail):
    score["held" if ok else "BROKEN"] = score.get("held", 0) + (1 if ok else 0)
    if not ok:
        score["open"] += 1
    print(f"{'HELD' if ok else 'BROKEN':<7} | {name}\n           | {detail}")


repo, base, head = make_subject_repo()
env = {"A360_REPO_ID": "R_synthetic_subject", "A360_EVENT": "pull_request", "A360_BASE_SHA": base,
       "A360_HEAD_SHA": head, "A360_CHECKOUT_SHA": head, "A360_MERGE_BASE": base,
       "A360_WORKFLOW_REF": "wf", "A360_CHECKOUT_MODEL": "head_checkout"}

# ================================================================ #1 caller-selected runner root
# v1.9 stopped reading authority.policy_dir/source and called that mechanical. But `for_runner`
# still took `src_dir` FROM THE CALLER. Codex handed it an ordinary Path - no sentinel, no subclass,
# no monkey patch - and the approved fixture policies resolved. The caller-controlled input had
# simply moved parameters.
crafted = OUT / "v19-crafted-runner-root"
(crafted / "policies").mkdir(parents=True, exist_ok=True)
(crafted / "schemas").mkdir(parents=True, exist_ok=True)
for src in (FIX).iterdir():
    if src.is_file():
        (crafted / "policies" / src.name).write_bytes(src.read_bytes())
for src in (HERE / "schemas").iterdir():
    if src.is_file():
        (crafted / "schemas" / src.name).write_bytes(src.read_bytes())

import inspect  # noqa: E402

sig = inspect.signature(Attestor.for_runner)
closed("v1.9 #1a: for_runner accepts a caller-selected installation root",
       "src_dir" not in sig.parameters,
       f"for_runner{sig} takes no installation root; it derives one from its own module location")

at_runner = Attestor.for_runner(repo, NOW, authority(policy_dir=crafted / "policies",
                                                     source="ci_fixture_alias"))
state = at_runner.resolver.resolve("operation-policy").obj["state"]
closed("v1.9 #1b: an ordinary alternate Path reaches approved fixture authority",
       state == "unapproved" and at_runner.policy_root == HERE / "policies",
       f"a crafted root full of approved policies was offered and ignored; resolved "
       f"state={state!r} from policy_root={at_runner.policy_root.name}/ under the attestor's own "
       f"install location")

# ================================================================ build a genuine allowed Change
at = Attestor.for_testing_only(HERE, repo, NOW, authority())
ctx = TrustedContext.from_runner_env(env, repo)
man_p, rep_p, _ = build_change_docs(at, ctx, repo, tag="_c19")
res = at.attest_change(env, man_p, rep_p, reg)
assert res.allowed and res.receipt, f"expected an allowed change, got {res.decision}"
receipt = res.receipt
rid = receipt["receipt_id"]
raw = json.dumps(receipt, sort_keys=True).encode()

# ================================================================ #2 receipt omits an authority
reduced = {k: v for k, v in receipt["policy_digests"].items() if k != "agent-version-policy"}
auth_d = dict(reduced, registry=receipt["control_registry_digest"],
              schemas=receipt["schema_digest"])
omitted = {**receipt, "policy_digests": reduced,
           "receipt_id": change_receipt_id(receipt["repository_id"], receipt["commit"],
                                           receipt["assurance_report_digest"],
                                           receipt["change_manifest_digest"], auth_d,
                                           ATTESTOR_VERSION, "allow", True)}
oraw = json.dumps(omitted, sort_keys=True).encode()
oat = Attestor.for_testing_only(HERE, repo, NOW,
                                authority(receipts={omitted["receipt_id"]: oraw}))
rep_art = Artifact.load(rep_p, oat.validators["assurance-report"])
ob, orp = build_output_docs(oat, ctx, rep_art, omitted["receipt_id"])
oout = oat.attest_output(ob, orp, rep_p, man_p, "BR-1", reg)
closed("v1.9 #2: receipt omitting agent-version-policy, correctly re-addressed, still allowed",
       not oout.allowed,
       f"decision={oout.decision} :: "
       f"{oout.evidence_reasons[0][:120] if oout.evidence_reasons else 'ALLOWED'}")

# ================================================================ #3 forged registry snapshot
gat = Attestor.for_testing_only(HERE, repo, NOW, authority(receipts={rid: raw}))
rep_art = Artifact.load(rep_p, gat.validators["assurance-report"])
bp, rp = build_output_docs(gat, ctx, rep_art, rid)


def relinked_output(prov_update, tag):
    rt = json.loads(Path(rp).read_text(encoding="utf-8"))
    rt["agent_provenance"].update(prov_update)
    p = OUT / f"v19-runtime-{tag}.json"
    p.write_text(json.dumps(rt), encoding="utf-8")
    art = Artifact.load(p, gat.validators["runtime-validation"])
    bo = json.loads(Path(bp).read_text(encoding="utf-8"))
    bo["linked_reports"]["runtime_validation_digest"] = art.digest
    bpp = OUT / f"v19-bundle-{tag}.json"
    bpp.write_text(json.dumps(bo), encoding="utf-8")
    return gat.attest_output(bpp, p, rep_p, man_p, "BR-1", reg)


r3 = relinked_output({"agent_registry_snapshot_digest": "sha256:" + "c" * 64}, "forged-registry")
closed("v1.9 #3: fabricated agent_registry_snapshot_digest still allowed Output", not r3.allowed,
       f"decision={r3.decision} :: "
       f"{r3.evidence_reasons[0][:120] if r3.evidence_reasons else 'ALLOWED'}")

r3b = relinked_output({"public_contract_version": "9.9.9-invented"}, "forged-contract")
closed("v1.9 #3b: fabricated public_contract_version", not r3b.allowed,
       f"decision={r3b.decision} :: "
       f"{r3b.evidence_reasons[0][:120] if r3b.evidence_reasons else 'ALLOWED'}")

# ================================================================ #4 shipped policy fails closed
sat = Attestor.for_runner(repo, NOW, authority(receipts={rid: raw}))
shipped = sat.resolver.resolve("agent-version-policy")
held("v1.9 #4 positive: shipped agent-version-policy fails closed",
     shipped.obj["state"] == "unapproved" and not shipped.obj["approved_versions"],
     f"state={shipped.obj['state']!r} approved_versions={shipped.obj['approved_versions']} (D-22)")

# ================================================================ #5 DETERMINISTIC porcelain case
# The first porcelain line is a MODIFIED TRACKED DOTFILE - the exact shape whose leading space was
# stripped, eating the '.' - with an untracked file behind it. No dependency on any real worktree.
gitrepo = OUT / "v19-porcelain-subject"
if gitrepo.exists():
    import shutil
    shutil.rmtree(gitrepo, ignore_errors=True)
gitrepo.mkdir(parents=True)


def g(*a):
    return subprocess.run(["git", *a], cwd=gitrepo, capture_output=True, text=True).stdout.strip()


g("init", "-q")
subprocess.run(["git", "config", "user.email", "h@example.invalid"], cwd=gitrepo)
subprocess.run(["git", "config", "user.name", "h"], cwd=gitrepo)
(gitrepo / ".env.sample").write_text("COMMITTED=1\n", encoding="utf-8")
(gitrepo / "keep.txt").write_text("keep\n", encoding="utf-8")
g("add", "-A")
g("commit", "-q", "-m", "baseline")
gbase = g("rev-parse", "HEAD")
(gitrepo / "keep.txt").write_text("keep2\n", encoding="utf-8")
g("add", "-A")
g("commit", "-q", "-m", "head")
ghead = g("rev-parse", "HEAD")
(gitrepo / ".env.sample").write_text("WORKTREE_ONLY=999\n", encoding="utf-8")   # -> " M .env.sample"
(gitrepo / "z.tmp").write_text("untracked\n", encoding="utf-8")                 # -> "?? z.tmp"

porcelain = subprocess.run(["git", "status", "--porcelain"], cwd=gitrepo,
                           capture_output=True, text=True).stdout
gctx = TrustedContext.from_runner_env(
    {"A360_REPO_ID": "R_porcelain", "A360_EVENT": "pull_request", "A360_BASE_SHA": gbase,
     "A360_HEAD_SHA": ghead, "A360_CHECKOUT_SHA": ghead, "A360_MERGE_BASE": gbase,
     "A360_WORKFLOW_REF": "wf", "A360_CHECKOUT_MODEL": "head_checkout"}, gitrepo)
first_line = porcelain.splitlines()[0]
paths_ok = gctx.dirty_paths == (".env.sample", "z.tmp")
closed("v1.9 #5a: porcelain first line loses its leading space (path first char eaten)",
       paths_ok and first_line.startswith(" M .env.sample"),
       f"first porcelain line={first_line!r} -> dirty_paths={gctx.dirty_paths}")

reader = gctx.object_reader(gitrepo)
env_obj = reader.read_text(".env.sample")
env_wt = (gitrepo / ".env.sample").read_text(encoding="utf-8")
closed("v1.9 #5b: object reader must return committed bytes for a modified tracked file",
       env_obj == "COMMITTED=1\n" and env_obj != env_wt,
       f"reader={env_obj!r} (committed) != worktree={env_wt!r}")
closed("v1.9 #5c: object reader must not see an untracked file",
       reader.read_text("z.tmp") is None,
       "untracked z.tmp is invisible at the verified commit")

total = score["closed"] + score.get("held", 0)
print(f"\nSUMMARY | v1.9 defects closed / positives held = {total}/{total + score['open']}")
if score["open"]:
    sys.exit(1)
