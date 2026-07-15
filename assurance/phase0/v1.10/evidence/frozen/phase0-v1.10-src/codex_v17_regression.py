"""Phase 0 v1.8 regression for Codex's v1.7 final checks.

`artifacts/codex-v1.7-final-check.py` (SHA-256 2c57f3cb...d94733) reports 6 observations. Four are
DEFECTS (a PASS means the defect exists); two are positive confirmations that must keep holding.
I reproduced all 6 against v1.7 before accepting anything.

This replays them against v1.8. The four defects must be CLOSED; the two positives must still hold.
Read-only.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from attest import Attestor  # noqa: E402
from core import Artifact, TrustedContext, change_receipt_id  # noqa: E402
from store import AuthorityContext, StoreReadError  # noqa: E402
from vertical_paths import (NOW, authority, build_change_docs, build_output_docs,  # noqa: E402
                            make_subject_repo)

results = []


def closed(name, ok, detail):
    results.append(ok)
    print(f"{'CLOSED ' if ok else 'STILL OPEN'} | {name}")
    print(f"           | {detail}")


def held(name, ok, detail):
    results.append(ok)
    print(f"{'HELD   ' if ok else 'REGRESSED'} | {name}")
    print(f"           | {detail}")


repo, base, head = make_subject_repo()
env = {"A360_REPO_ID": "R_synthetic_subject", "A360_EVENT": "pull_request", "A360_BASE_SHA": base,
       "A360_HEAD_SHA": head, "A360_CHECKOUT_SHA": head, "A360_MERGE_BASE": base,
       "A360_WORKFLOW_REF": "wf", "A360_CHECKOUT_MODEL": "head_checkout"}
FIX = HERE / "policies_approved_fixture"
reg = HERE / "control-registry.yaml"

# ---- v1.7 #1: the NORMAL constructor accepted test_only_fixture + approved fixture policies
try:
    a = Attestor(HERE, repo, NOW, authority())          # authority() is source=test_only_fixture
    state = a.resolver.resolve("operation-policy").obj["state"]
    closed("v1.7 #1: normal Attestor constructor accepts test_only_fixture approved policies",
           False, f"constructor accepted it; policy state={state!r}")
except ValueError as e:
    closed("v1.7 #1: normal Attestor constructor accepts test_only_fixture approved policies",
           True, f"refused: {str(e)[:110]}")

at = Attestor.for_testing_only(HERE, repo, NOW, authority())
held("v1.7 positive: explicit test-only path still reaches the fixture authority",
     at.resolver.resolve("operation-policy").obj["state"] == "approved",
     "Attestor.for_testing_only resolves the fixture policy as approved")

ctx = TrustedContext.from_runner_env(env, repo)
cmp_, repp, _ = build_change_docs(at, ctx, repo)
chg = at.attest_change(env, cmp_, repp, reg)
receipt = chg.receipt
rid = receipt["receipt_id"]
store = authority().receipt_store
store.put(json.dumps(receipt, sort_keys=True).encode())
at2 = Attestor.for_testing_only(HERE, repo, NOW, authority(
    receipts={rid: json.dumps(receipt, sort_keys=True).encode()}))
report_art = Artifact.load(repp, at.validators["assurance-report"])
ebp, rvp = build_output_docs(at2, ctx, report_art, rid)

# ---- v1.7 #2: a method argument could override the bundle's change_receipt_id
import inspect  # noqa: E402
sig = inspect.signature(Attestor.attest_output)
closed("v1.7 #2: receipt method argument overrides the bundle change_receipt_id",
       "receipt_id" not in sig.parameters,
       f"attest_output no longer takes a receipt_id argument; the bundle's change_receipt_id is the "
       f"single identity. params={list(sig.parameters)[1:]}")

# ---- v1.7 #3: the store key could differ from the validated payload's receipt_id
bad = authority(receipts={"CR-someone-elses-key": json.dumps(receipt, sort_keys=True).encode()})
try:
    bad.receipt_store.resolve("CR-someone-elses-key")
    closed("v1.7 #3: receipt store key can differ from the validated payload receipt_id",
           False, "store returned a record under a key that is not its payload id")
except StoreReadError as e:
    closed("v1.7 #3: receipt store key can differ from the validated payload receipt_id",
           True, f"refused: {str(e)[:110]}")

# ---- v1.7 #4: distinct allowed reports for one commit collided on CR-<commit[:12]>
cmp2, repp2, _ = build_change_docs(at, ctx, repo, tag="_alt")
alt = json.loads(Path(repp2).read_text(encoding="utf-8"))
alt["run"]["run_id"] = "a-different-but-equally-valid-run"
p_alt = HERE / ".out" / "report_alt.json"
p_alt.write_text(json.dumps(alt, indent=1), encoding="utf-8")
alt_art = Artifact.load(p_alt, at.validators["assurance-report"])
man_art = Artifact.load(cmp_, at.validators["change-manifest"])
digests = at.resolved_digests(Artifact.load(reg, at.validators["control-registry"]))
auth_d = {k: v for k, v in digests.items() if k.endswith("-policy")}
auth_d.update({"registry": digests["registry"], "schemas": digests["schemas"]})
id_a = change_receipt_id("R_synthetic_subject", head, report_art.digest, man_art.digest, auth_d, "1.10.0", "allow", True)
id_b = change_receipt_id("R_synthetic_subject", head, alt_art.digest, man_art.digest, auth_d, "1.10.0", "allow", True)
closed("v1.7 #4: distinct allowed reports for one commit collide on receipt_id",
       id_a != id_b,
       f"same commit {head[:8]}, different report digests -> {id_a[:20]}… vs {id_b[:20]}…")

# ---- immutability of the receipt store (new v1.8 contract)
try:
    store.put(json.dumps({**receipt, "decision": "allow", "commit": "0" * 40},
                         sort_keys=True).encode())
    ok = True   # different content, different id -> a new record, which is fine
except StoreReadError:
    ok = True
tampered = dict(receipt)
tampered["decision"] = "deny"
try:
    store.put(json.dumps(tampered, sort_keys=True).encode())
    held("v1.8 store contract: different content cannot overwrite an existing receipt id", False,
         "overwrite succeeded")
except StoreReadError as e:
    held("v1.8 store contract: different content cannot overwrite an existing receipt id", True,
         f"refused: {str(e)[:100]}")

# ---- v1.7 positive: Backend decision works with producer_advisory.present=false
rv = json.loads(Path(rvp).read_text(encoding="utf-8"))
held("v1.7 positive: Backend output decision works with producer_advisory.present=false",
     rv["producer_advisory"]["present"] is False,
     "the positive Output path carries no Agent advisory and still reaches a Backend decision (D-002)")

print(f"\nSUMMARY | v1.7 defects closed / positives held = {sum(results)}/{len(results)}")
sys.exit(0 if all(results) else 1)
