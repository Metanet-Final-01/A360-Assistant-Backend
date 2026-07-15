"""Regression for the six observations Codex reproduced against v1.8.

Every case below was FIRST reproduced against the untouched v1.8 source (6/6) and only then fixed.
Two of the six were positive confirmations that must keep holding; four were defects.

This is a contract self-test, not an attestation. See D-16.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from attest import Attestor  # noqa: E402
from core import ATTESTOR_VERSION, Artifact, TrustedContext, change_receipt_id  # noqa: E402
from store import StoreReadError  # noqa: E402
from vertical_paths import (NOW, authority, build_change_docs, build_output_docs,  # noqa: E402
                            make_subject_repo)

FIX = HERE / "policies_approved_fixture"
reg = HERE / "control-registry.yaml"
score = {"closed": 0, "held": 0, "open": 0}


def closed(name, ok, detail):
    score["closed" if ok else "open"] += 1
    print(f"{'CLOSED' if ok else 'OPEN':<7} | {name}\n           | {detail}")


def held(name, ok, detail):
    score["held" if ok else "open"] += 1
    print(f"{'HELD' if ok else 'BROKEN':<7} | {name}\n           | {detail}")


repo, base, head = make_subject_repo()
env = {"A360_REPO_ID": "R_synthetic_subject", "A360_EVENT": "pull_request", "A360_BASE_SHA": base,
       "A360_HEAD_SHA": head, "A360_CHECKOUT_SHA": head, "A360_MERGE_BASE": base,
       "A360_WORKFLOW_REF": "wf", "A360_CHECKOUT_MODEL": "head_checkout"}

# ---------------------------------------------------------------- #1 alias provenance bypass
# v1.8 refused ONE exact string, so any unrecognised label walked straight through.
try:
    a = Attestor(HERE, repo, NOW, authority(source="ci_fixture_alias"))
    state = a.resolver.resolve("operation-policy").obj["state"]
    closed("v1.8 #1: alias provenance reaches approved fixture via normal construction", False,
           f"constructor accepted it; policy state={state!r}")
except (ValueError, TypeError) as e:
    closed("v1.8 #1: alias provenance reaches approved fixture via normal construction", True,
           f"direct construction refused: {str(e)[:100]}")

# ...and the deeper fix: even through the RUNNER factory, an alias label plus a fixture policy_dir
# cannot steer the policy root, because the runner path reads neither.
at_runner = Attestor.for_runner(repo, NOW, authority(policy_dir=FIX, source="ci_fixture_alias"))
state = at_runner.resolver.resolve("operation-policy").obj["state"]
closed("v1.8 #1b: for_runner ignores caller policy_dir/source entirely", state == "unapproved",
       f"runner bound its own policies/ root -> state={state!r} (fixture tree was passed and ignored); "
       f"policy_root={at_runner.policy_root.name}")

# ---------------------------------------------------------------- build a genuine allowed Change
at = Attestor.for_testing_only(HERE, repo, NOW, authority())
ctx = TrustedContext.from_runner_env(env, repo)
man_p, rep_p, _ = build_change_docs(at, ctx, repo, tag="_c18")
res = at.attest_change(env, man_p, rep_p, reg)
assert res.allowed and res.receipt, f"expected an allowed change, got {res.decision}"
receipt = res.receipt
raw = json.dumps(receipt, sort_keys=True).encode()
rid = receipt["receipt_id"]

# ---------------------------------------------------------------- #2/#3 forged receipt
forged = dict(receipt)
forged["receipt_id"] = "CR-" + "f" * 32
forged["attestor"] = {"id": "not-the-phase0-attestor", "version": "99.0"}
forged_raw = json.dumps(forged, sort_keys=True).encode()
forged_auth = authority(receipts={forged["receipt_id"]: forged_raw})
fat = Attestor.for_testing_only(HERE, repo, NOW, forged_auth)
rep_art = Artifact.load(rep_p, fat.validators["assurance-report"])
fb, fr = build_output_docs(fat, ctx, rep_art, forged["receipt_id"])
fout = fat.attest_output(fb, fr, rep_p, man_p, "BR-1", reg)
closed("v1.8 #2: a schema-valid forged receipt in the trusted store authorised Output",
       not fout.allowed,
       f"decision={fout.decision} evidence_valid={fout.evidence_valid} :: "
       f"{fout.evidence_reasons[0][:120] if fout.evidence_reasons else ''}")

# ---------------------------------------------------------------- #4 unapproved version + fake digest
good_auth = authority(receipts={rid: raw})
oat = Attestor.for_testing_only(HERE, repo, NOW, good_auth)
rep_art = Artifact.load(rep_p, oat.validators["assurance-report"])
bp, rp = build_output_docs(oat, ctx, rep_art, rid)

rt = json.loads(Path(rp).read_text(encoding="utf-8"))
rt["agent_provenance"].update({
    "requested_agent_version": "v404", "boundary_derived_default": "v404",
    "producer_resolved_version": "v404",
    "resolved_version_observability": "observed_from_public_contract",
    "agent_version_policy_digest": "sha256:" + "9" * 64})
mrp = HERE / ".out" / "v18-runtime-unapproved.json"
mrp.write_text(json.dumps(rt), encoding="utf-8")
mra = Artifact.load(mrp, oat.validators["runtime-validation"])
bo = json.loads(Path(bp).read_text(encoding="utf-8"))
bo["linked_reports"]["runtime_validation_digest"] = mra.digest
mbp = HERE / ".out" / "v18-bundle-unapproved.json"
mbp.write_text(json.dumps(bo), encoding="utf-8")
vout = oat.attest_output(mbp, mrp, rep_p, man_p, "BR-1", reg)
closed("v1.8 #3: unapproved v404 + fabricated policy digest authorised Output", not vout.allowed,
       f"decision={vout.decision} :: {vout.evidence_reasons[0][:130] if vout.evidence_reasons else ''}")

# ...and with a TRUTHFUL policy digest, v404 must still be refused: discoverable != approved.
rt2 = json.loads(Path(rp).read_text(encoding="utf-8"))
rt2["agent_provenance"].update({
    "requested_agent_version": "v404", "producer_resolved_version": "v404",
    "resolved_version_observability": "observed_from_public_contract"})
mrp2 = HERE / ".out" / "v18-runtime-unapproved-truthful.json"
mrp2.write_text(json.dumps(rt2), encoding="utf-8")
mra2 = Artifact.load(mrp2, oat.validators["runtime-validation"])
bo2 = json.loads(Path(bp).read_text(encoding="utf-8"))
bo2["linked_reports"]["runtime_validation_digest"] = mra2.digest
mbp2 = HERE / ".out" / "v18-bundle-unapproved-truthful.json"
mbp2.write_text(json.dumps(bo2), encoding="utf-8")
vout2 = oat.attest_output(mbp2, mrp2, rep_p, man_p, "BR-1", reg)
closed("v1.8 #3b: v404 with a TRUTHFUL policy digest (discoverable != approved)", not vout2.allowed,
       f"decision={vout2.decision} :: {vout2.evidence_reasons[0][:130] if vout2.evidence_reasons else ''}")

# ...and the CURRENT product state must fail closed rather than guess (D-21).
rt3 = json.loads(Path(rp).read_text(encoding="utf-8"))
rt3["agent_provenance"].update({
    "producer_resolved_version": None,
    "resolved_version_observability": "not_observable_pending_D21"})
mrp3 = HERE / ".out" / "v18-runtime-current-product.json"
mrp3.write_text(json.dumps(rt3), encoding="utf-8")
mra3 = Artifact.load(mrp3, oat.validators["runtime-validation"])
bo3 = json.loads(Path(bp).read_text(encoding="utf-8"))
bo3["linked_reports"]["runtime_validation_digest"] = mra3.digest
mbp3 = HERE / ".out" / "v18-bundle-current-product.json"
mbp3.write_text(json.dumps(bo3), encoding="utf-8")
vout3 = oat.attest_output(mbp3, mrp3, rep_p, man_p, "BR-1", reg)
closed("v1.8 #3c: CURRENT product state (no observable version) cannot be assured", not vout3.allowed,
       f"decision={vout3.decision} :: {vout3.evidence_reasons[0][:130] if vout3.evidence_reasons else ''}")

# ---------------------------------------------------------------- positives that must keep holding
alias_store = authority(receipts={"CR-codex-alias": raw}).receipt_store
try:
    alias_store.resolve("CR-codex-alias")
    held("v1.8 positive: store key/payload mismatch is refused", False, "store returned a mismatch")
except StoreReadError as e:
    held("v1.8 positive: store key/payload mismatch is refused", True, str(e)[:120])

dg = "sha256:" + "1" * 64
id_a = change_receipt_id("R", head, dg, dg, {"p": dg}, ATTESTOR_VERSION, "allow", True)
id_b = change_receipt_id("R", head, dg, dg, {"p": "sha256:" + "2" * 64}, ATTESTOR_VERSION,
                         "allow", True)
held("v1.8 positive: a different authority yields a different receipt id", id_a != id_b,
     f"{id_a} != {id_b}")

print(f"\nSUMMARY | v1.8 defects closed / positives held = "
      f"{score['closed'] + score['held']}/{score['closed'] + score['held'] + score['open']}")
if score["open"]:
    sys.exit(1)
