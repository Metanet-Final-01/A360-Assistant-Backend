"""Phase 0 v1.8 regression for Codex's v1.6 confirmation checks.

`artifacts/codex-v1.6-confirmation-check.py` (SHA-256 adbde532...bc028c) asserts each v1.6 DEFECT is
present: every case PASSes when the defect exists. I reproduced all 4 against v1.6 before accepting.

This replays the same four attacks against v1.8 (call signatures updated for the v1.7
review item 2 change: attest_output no longer takes a receipt_id argument). Each must now be CLOSED. The attacks are
reproduced in intent; only the signatures v1.7 deliberately changed (runner-bound adapters,
receipt binding) are adapted, and no assertion is weakened.

Read-only.
"""
from __future__ import annotations

import datetime as dt
import gc
import json
import sys
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import rules  # noqa: E402
from attest import Attestor  # noqa: E402
from core import Artifact  # noqa: E402
from store import (AuthorityContext, BoundaryRecordStore, ChangeReceiptStore,  # noqa: E402
                   FixtureEventStore, QueryEvidence, StoreRow, query_digest, result_set_digest)
from vertical_paths import (BOUNDARY, D_A, D_PAY, NOW, ROWS, V, authority,  # noqa: E402
                            build_change_docs, build_output_docs, make_subject_repo)
from core import TrustedContext  # noqa: E402

OUT = HERE / ".out"
results = []


def closed(name, is_closed, detail):
    results.append(is_closed)
    print(f"{'CLOSED ' if is_closed else 'STILL OPEN'} | {name}")
    print(f"           | {detail}")


# Rebuild the positive chain so the attacks have something real to attack.
repo, base, head = make_subject_repo()
env = {"A360_REPO_ID": "R_synthetic_subject", "A360_EVENT": "pull_request", "A360_BASE_SHA": base,
       "A360_HEAD_SHA": head, "A360_CHECKOUT_SHA": head, "A360_MERGE_BASE": base,
       "A360_WORKFLOW_REF": "wf", "A360_CHECKOUT_MODEL": "head_checkout"}
at = Attestor.for_testing_only(HERE, repo, NOW, authority())
ctx = TrustedContext.from_runner_env(env, repo)
reg = HERE / "control-registry.yaml"
cmp_, repp, _ = build_change_docs(at, ctx, repo)
chg = at.attest_change(env, cmp_, repp, reg)
receipt = chg.receipt
rid = receipt["receipt_id"]
at2 = Attestor.for_testing_only(HERE, repo, NOW, authority(
    receipts={rid: json.dumps(receipt, sort_keys=True).encode()}))
report_art = Artifact.load(repp, at.validators["assurance-report"])
ebp, rvp = build_output_docs(at2, ctx, report_art, rid)

# ---- v1.6 #1: an unrelated, internally contradictory Change report authorised Output
rep = json.loads(Path(repp).read_text(encoding="utf-8"))
rep["subject"]["repository_id"] = "R_unrelated_change"
rep["subject"]["commit"] = "f" * 40
p_un = OUT / "regr_unrelated_report.json"
p_un.write_text(json.dumps(rep, indent=1), encoding="utf-8")
un_art = Artifact.load(p_un, at.validators["assurance-report"])
b = json.loads(Path(ebp).read_text(encoding="utf-8"))
b["linked_reports"]["assurance_report_digest"] = un_art.digest
p_b = OUT / "regr_bundle_unrelated.json"
p_b.write_text(json.dumps(b, indent=1), encoding="utf-8")
r1 = at2.attest_output(p_b, rvp, p_un, cmp_, "BR-1", reg)
closed("v1.6 #1: an unrelated/contradictory Change report can authorize Output",
       r1.decision != "allow",
       f"decision={r1.decision}; {(r1.evidence_reasons or ('',))[0][:96]}")


# ---- v1.6 #2: QueryEvidence.queried_at unrelated to the claimed anchor
class WrongTime(FixtureEventStore):
    def query(self, *a, **k):
        rows, ev = super().query(*a, **k)
        return rows, replace(ev, queried_at=NOW - dt.timedelta(days=1))


at3 = Attestor.for_testing_only(HERE, repo, NOW, authority(
    receipts={rid: json.dumps(receipt, sort_keys=True).encode()},
    event_store=WrongTime(ROWS, queried_at=NOW)))
r2 = at3.attest_output(ebp, rvp, repp, cmp_, "BR-1", reg)
closed("v1.6 #2: HL-13 accepts QueryEvidence.queried_at unrelated to the anchor",
       r2.decision != "allow",
       f"decision={r2.decision}; {(r2.evidence_reasons or ('',))[0][:96]}")


# ---- v1.6 #3: rows outside the requested subject and time window
class OutOfWindowRows:
    def query(self, source_table, subject_key, subject_value, window_start, window_end):
        kind = "runtime_validation" if source_table == "boundary_validation" else "audit_log"
        rows = (StoreRow(kind, subject_key, "different-subject",
                         window_start - dt.timedelta(days=1), "obs-rev-42"),)
        ev = QueryEvidence(source_table=source_table, subject_key=subject_key,
                           subject_value=subject_value, window_start=window_start,
                           window_end=window_end, source_revision="obs-rev-42", queried_at=NOW,
                           query_digest=query_digest(source_table, subject_key, subject_value,
                                                     window_start, window_end),
                           row_count=1, result_set_digest=result_set_digest(rows))
        return rows, ev


at4 = Attestor.for_testing_only(HERE, repo, NOW, authority(
    receipts={rid: json.dumps(receipt, sort_keys=True).encode()}, event_store=OutOfWindowRows()))
r3 = at4.attest_output(ebp, rvp, repp, cmp_, "BR-1", reg)
closed("v1.6 #3: HL-13 accepts rows outside the requested subject and window",
       r3.decision != "allow",
       f"decision={r3.decision}; {(r3.evidence_reasons or ('',))[0][:96]}")

# ---- v1.6 #4: same-process backing mutation changes an authority decision
bundle = Artifact.load(ebp, at.validators["evidence-bundle"])
claim = bundle.obj["operation_policy"]
policy = at.resolver.resolve("operation-policy")
before = at.resolver.require_approved("operation-policy", claim)[0]
backing = next(i for i in gc.get_referents(policy.obj) if isinstance(i, dict))
backing["state"] = "unapproved"
after = at.resolver.require_approved("operation-policy", claim)[0]
closed("v1.6 #4: same-process backing mutation changes an authority decision",
       before == after,
       f"before={before} after={after}; obj now reports state={policy.obj['state']!r} but the "
       f"decision re-parses the immutable raw bytes. NOTE: this defeats the demonstrated gc "
       f"aliasing attack only - it is NOT a same-process security boundary (D-16).")

print(f"\nSUMMARY | v1.6 defects closed = {sum(results)}/{len(results)}")
sys.exit(0 if all(results) else 1)
