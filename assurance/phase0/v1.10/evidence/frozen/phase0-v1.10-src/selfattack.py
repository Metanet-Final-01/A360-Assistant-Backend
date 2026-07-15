"""Attack v1.10's OWN claims before publishing them.

The pattern to beat is my own. In v1.8 I guarded ONE provenance string and called it a separation;
Codex used an alias. In v1.9 I stopped reading the label entirely and called THAT mechanical; Codex
handed `for_runner` an ordinary Path, because I had left the installation root as a caller argument.
Twice I fixed the input I happened to be looking at instead of the CLASS of input.

So this file asks the question I kept failing to ask: what OTHER caller-supplied value still selects
an authority? Everything it finds is published, including what does not survive.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import attest  # noqa: E402
from attest import Attestor  # noqa: E402
from core import ATTESTOR_VERSION, Artifact, TrustedContext, change_receipt_id  # noqa: E402
from vertical_paths import (NOW, authority, build_change_docs, build_output_docs,  # noqa: E402
                            make_subject_repo)

FIX = HERE / "policies_approved_fixture"
reg = HERE / "control-registry.yaml"
repo, base, head = make_subject_repo()
env = {"A360_REPO_ID": "R_synthetic_subject", "A360_EVENT": "pull_request", "A360_BASE_SHA": base,
       "A360_HEAD_SHA": head, "A360_CHECKOUT_SHA": head, "A360_MERGE_BASE": base,
       "A360_WORKFLOW_REF": "wf", "A360_CHECKOUT_MODEL": "head_checkout"}
findings = []


def attack(name, bypassed, detail):
    findings.append((name, bypassed))
    print(f"  [{'BYPASSED' if bypassed else 'held'}] {name}\n      {detail}")


print("=" * 100)
print("CLAIM 1: 'no caller-supplied value can steer the runner at a fixture policy tree'")
print("=" * 100)

# 1a. v1.8's bypass, retried.
try:
    Attestor(HERE, repo, NOW, authority(), _test_path=True)
    attack("v1.8 attack A replayed: _test_path=True on the normal constructor", True, "accepted")
except TypeError as e:
    attack("v1.8 attack A replayed: _test_path=True on the normal constructor", False,
           f"the field no longer exists: {e}")

# 1b. Every label: the runner path reads no label at all.
for label in ("runner", "ci_fixture_alias", "test_only_fixture", "", "TEST_ONLY_FIXTURE"):
    at = Attestor.for_runner(repo, NOW, authority(policy_dir=FIX, source=label))
    st = at.resolver.resolve("operation-policy").obj["state"]
    attack(f"for_runner with source={label!r} + fixture policy_dir", st == "approved",
           f"resolved the shipped policies/ root -> state={st!r}")

# 1b2. Codex's v1.9 kill: an ordinary Path as the installation root. The parameter is gone.
import inspect as _inspect  # noqa: E402

attack("pass an installation root to for_runner (Codex's v1.9 attack)",
       "src_dir" in _inspect.signature(Attestor.for_runner).parameters,
       f"for_runner{_inspect.signature(Attestor.for_runner)}: the parameter no longer exists; the "
       f"root comes from this module's own location")

# 1b3. THE QUESTION I KEPT NOT ASKING: what ELSE does the runner take from its caller? `repo`, `now`
#      and `authority` all are. Can any of them select an authority the way src_dir did?
_crafted = HERE / ".out" / "sa110-crafted-root"
(_crafted / "policies").mkdir(parents=True, exist_ok=True)
for _f in FIX.iterdir():
    if _f.is_file():
        (_crafted / "policies" / _f.name).write_bytes(_f.read_bytes())
at = Attestor.for_runner(_crafted, NOW, authority(policy_dir=_crafted / "policies"))
st = at.resolver.resolve("operation-policy").obj["state"]
attack("hand the crafted root in as `repo` instead - does the subject leak into policy resolution?",
       st == "approved",
       f"`repo` is the SUBJECT under assessment and is never an authority source; resolved "
       f"state={st!r} from {at.policy_root}")

# 1c. The honest attack: the factories are public. Anyone who can import attest can call the test one.
at = Attestor.for_testing_only(HERE, repo, NOW, authority())
st = at.resolver.resolve("operation-policy").obj["state"]
attack("call Attestor.for_testing_only directly (it is a public classmethod)", st == "approved",
       f"resolved the fixture tree -> state={st!r}. EXPECTED: the test factory is not a lock, it is "
       f"a signpost. Import access == full access. D-16, not something Python can fix.")

# 1d. Reach past the factories entirely by importing the private construction sentinel.
try:
    a = Attestor(FIX.parent, repo, NOW, authority(policy_dir=FIX),
                 _construction=attest._TEST_CONSTRUCTION)
    st = a.resolver.resolve("operation-policy").obj["state"]
    attack("import the private _TEST_CONSTRUCTION sentinel and pass it", st == "approved",
           f"resolved the fixture tree -> state={st!r}. EXPECTED, same reason as 1c.")
except Exception as e:
    attack("import the private _TEST_CONSTRUCTION sentinel and pass it", False, str(e)[:100])

print()
print("=" * 100)
print("CLAIM 2: 'an assured Output must bind an observed, approved Agent version'")
print("=" * 100)

at = Attestor.for_testing_only(HERE, repo, NOW, authority())
ctx = TrustedContext.from_runner_env(env, repo)
man_p, rep_p, _derived = build_change_docs(at, ctx, repo, tag="_sa19")
res = at.attest_change(env, man_p, rep_p, reg)
receipt, raw = res.receipt, json.dumps(res.receipt, sort_keys=True).encode()
rid = receipt["receipt_id"]
oat = Attestor.for_testing_only(HERE, repo, NOW, authority(receipts={rid: raw}))
rep_art = Artifact.load(rep_p, oat.validators["assurance-report"])
bp, rp = build_output_docs(oat, ctx, rep_art, rid)


def output_with(prov_update, tag):
    rt = json.loads(Path(rp).read_text(encoding="utf-8"))
    rt["agent_provenance"].update(prov_update)
    p = HERE / ".out" / f"sa19-runtime-{tag}.json"
    p.write_text(json.dumps(rt), encoding="utf-8")
    art = Artifact.load(p, oat.validators["runtime-validation"])
    bo = json.loads(Path(bp).read_text(encoding="utf-8"))
    bo["linked_reports"]["runtime_validation_digest"] = art.digest
    bpp = HERE / ".out" / f"sa19-bundle-{tag}.json"
    bpp.write_text(json.dumps(bo), encoding="utf-8")
    return oat.attest_output(bpp, p, rep_p, man_p, "BR-1", reg)


# 2a. v1.9's own UNCLOSED finding, retried: CLAIM the version was observed when it was not. v1.10
#     compares the claim against a runner-bound observation, so the lie should now be visible.
from store import FixturePublicContractAdapter as _FPCA  # noqa: E402

_seen = oat.authority.observer().observe()
_blind = Attestor.for_testing_only(
    HERE, repo, NOW,
    authority(receipts={rid: raw},
              public_boundary=_FPCA(resolved_version=None,
                                    registry_snapshot_digest=_seen.registry_snapshot_digest)))
_ra = Artifact.load(rep_p, _blind.validators["assurance-report"])
_bp, _rp = build_output_docs(
    _blind, ctx, _ra, rid,
    provenance_override={
        "requested_agent_version": "v2", "boundary_derived_default": "v2",
        "producer_resolved_version": "v2",
        "resolved_version_observability": "observed_from_public_contract",
        "agent_registry_snapshot_digest": _seen.registry_snapshot_digest,
        "public_contract_version": _seen.public_contract_version,
        "agent_version_policy_digest": _blind.resolver.resolve("agent-version-policy").digest})
_r = _blind.attest_output(_bp, _rp, rep_p, man_p, "BR-1", reg)
attack("CLAIM observed_from_public_contract while the observer saw nothing (v1.9's open finding)",
       _r.allowed,
       f"decision={_r.decision} :: "
       f"{_r.evidence_reasons[0][:110] if _r.evidence_reasons else 'ALLOWED'}")

# 2a2. The residual I will not paper over: the ADAPTER itself is trusted. A runner wired to a lying
#      observer is believed - but that is the same D-16 assumption every store already rests on.
_liar = Attestor.for_testing_only(
    HERE, repo, NOW,
    authority(receipts={rid: raw}, public_boundary=_FPCA(resolved_version="v2")))
_ra2 = Artifact.load(rep_p, _liar.validators["assurance-report"])
_bp2, _rp2 = build_output_docs(_liar, ctx, _ra2, rid)
_r2 = _liar.attest_output(_bp2, _rp2, rep_p, man_p, "BR-1", reg)
attack("wire the runner to an adapter that fabricates the entire observation", _r2.allowed,
       f"decision={_r2.decision}. EXPECTED AND UNCLOSED: the observer is runner-bound, so this is "
       f"the D-16 assumption, identical to the event/boundary/receipt stores. v1.10 removes the "
       f"ability to lie in the ASSESSED DOCUMENT; it does not make the runner trustworthy.")

# 2b. Does the shipped (unapproved) policy really refuse everything?
sat = Attestor.for_runner(repo, NOW, authority(receipts={rid: raw}))
shipped = sat.resolver.resolve("agent-version-policy")
attack("shipped agent-version-policy authorises some version",
       bool(shipped.obj.get("approved_versions")) and shipped.obj["state"] == "approved",
       f"state={shipped.obj['state']!r} approved_versions={shipped.obj['approved_versions']} -> "
       f"nothing is approvable through the real tree today (D-22)")

print()
print("=" * 100)
print("CLAIM 3: 'HL-19 recomputes the receipt binding'")
print("=" * 100)

# 3a. The real question - not "can a mismatched forgery be caught" (it can, on subject grounds), but
# "can a forger MINT an allow receipt for a change this attestor actually DENIED?" Take a genuinely
# failing change, then hand-mint an allow receipt over its report with a correctly recomputed
# address. If Output accepts it, the receipt proves nothing about what the Change harness decided.
_applicable = _derived["applicable_controls"]
bad_man_p, bad_rep_p, _ = build_change_docs(
    at, ctx, repo, tag="_sa19bad",
    gates_override=[{"control_id": c, "status": "skipped", "skipped_reason": "runner busy"}
                    for c in _applicable])
denied = at.attest_change(env, bad_man_p, bad_rep_p, reg)
bad_rep_art = Artifact.load(bad_rep_p, at.validators["assurance-report"])
bad_man_art = Artifact.load(bad_man_p, at.validators["change-manifest"])

auth_d = dict(receipt["policy_digests"])
auth_d["registry"] = receipt["control_registry_digest"]
auth_d["schemas"] = receipt["schema_digest"]
minted = {**receipt, "decision": "allow", "evidence_valid": True,
          "assurance_report_digest": bad_rep_art.digest,
          "change_manifest_digest": bad_man_art.digest}
minted["receipt_id"] = change_receipt_id(receipt["repository_id"], receipt["commit"],
                                         bad_rep_art.digest, bad_man_art.digest, auth_d,
                                         ATTESTOR_VERSION, "allow", True)
mraw = json.dumps(minted, sort_keys=True).encode()
fat = Attestor.for_testing_only(HERE, repo, NOW, authority(receipts={minted["receipt_id"]: mraw}))
fb, fr = build_output_docs(fat, ctx, bad_rep_art, minted["receipt_id"])
fout = fat.attest_output(fb, fr, bad_rep_p, bad_man_p, "BR-1", reg)
attack("hand-mint an ALLOW receipt for a change the attestor DENIED, address recomputed correctly",
       fout.allowed,
       f"attest_change said decision={denied.decision!r}; the minted receipt says 'allow' and its "
       f"content address verifies. Output decision={fout.decision} :: "
       f"{(fout.evidence_reasons[0][:100] + '...') if fout.evidence_reasons else 'ALLOWED'}")

print("\n" + "=" * 100)
print("CONCLUSION (published, not hidden):")
print("""
  Criterion 5: the runner factory now takes NO value that can select an authority - not a label, not
  a policy dir, not an installation root (1b/1b2), and the subject repo does not leak into policy
  resolution (1b3). Twice before I fixed the specific input under my nose; this time I went looking
  for the class. But 1c/1d are unchanged and unclosable in Python: the test factory and the
  construction sentinel are importable, so anything inside this process reaches the fixture tree.
  That is D-16/D-17, and I make no claim beyond "no caller ARGUMENT selects an authority".

  HL-20: v1.9's own open finding is now closed (2a) - a runtime document that CLAIMS an observation
  the runner-bound observer did not make is refused, so provenance is no longer self-reported in the
  assessed artifact. 2a2 is the honest residual: the adapter itself is trusted, so a runner wired to
  a lying observer is believed. That is not a new hole - it is the same D-16 runner-binding
  assumption the event, boundary and receipt stores already rest on - but it is the reason
  "observed" means "observed BY A TRUSTED RUNNER", and D-16 is what makes that phrase mean anything.
  The shipped runner has no observer at all and fails closed, which is the truthful state at d66fce1
  (D-21/D-22).

  HL-19's recomputation catches an INCONSISTENT receipt: a stale id, an unknown attestor, or a
  flipped verdict - the last of which v1.8 did not bind at all, and which I found while writing
  v1.9's own deny fixture rather than from any review. But 3a is the finding that matters, and it
  is worse than "a caveat": I took a change this attestor DENIED, hand-minted a receipt saying
  "allow" over its report, recomputed the content address correctly, and Output accepted it. So the
  receipt does not prove what the Change harness decided - it proves only that someone who can write
  to the receipt store can compute a hash. Codex qualified exactly this ("content addressing is not
  origin authentication") and it is confirmed. Criterion 4 therefore holds only UNDER the D-16
  assumption of a protected store writer. Signing is the fix, and it is not in v1.9.
""")
bypassed = [n for n, b in findings if b]
print(f"attacks that bypassed a v1.10 claim: {len(bypassed)}/{len(findings)}")
for n in bypassed:
    print(f"  - {n}")
print("\nAll of the above are DISCLOSED in the contract, not fixed by it.")
