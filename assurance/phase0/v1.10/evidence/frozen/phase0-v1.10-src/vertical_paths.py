"""Phase 0 v1.8 VERTICAL PATH TRANSCRIPT.

Both vertical paths end to end through the real Attestor, plus every v1.6-review case:

  CHANGE  positive : clean synthetic subject, all applicable gates pass -> allow, and a
                     ChangeAttestationReceipt is issued
          negatives: failed / skipped / uncovered gate -> deny (and the receipt records deny)
  OUTPUT  positive : resolved receipt + report + runtime + boundary record + trusted observations
                     -> assured, allow
          negatives: unrelated repository/commit report, internally inconsistent report,
                     unattested report, deny/observe receipt, receipt-subject mismatch,
                     wrong queried_at, wrong-subject rows, out-of-window rows, wrong-revision rows,
                     mixed valid+invalid rows -> each denies with its own typed reason

SUBJECT: the positive CHANGE path uses a deterministic SYNTHETIC git repo under `.out/`. The live
product repo is dirty and its CI workflow legitimately contains `pip install`, so HL-17/INV-AR-3
correctly refuse it; that refusal is shown too.

POLICY: positive paths use `policies_approved_fixture/` through the explicit
`Attestor.for_testing_only` factory (v1.6 review item 2). The shipped `policies/` stay
state=unapproved (D-9/D-11/D-20); the same input against them lands on `observe`, never `allow`.

Generated documents go to `.out/`, OUTSIDE the hashed source manifest.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import core
import rules
from attest import Attestor
from core import Artifact, TrustedContext, load_validators
from store import (AuthorityContext, BoundaryRecordStore, ChangeReceiptStore, FixtureEventStore,
                   FixturePublicContractAdapter, QueryEvidence, StoreRow, query_digest,
                   result_set_digest)

# TEST-ONLY runner-bound observer. The shipped runner uses NotConfiguredPublicBoundaryAdapter
# and fails closed: at d66fce1 the Backend boundary observes nothing (D-21).
PUBLIC_CONTRACT = FixturePublicContractAdapter(resolved_version="v2")

HERE = Path(__file__).parent
OUT = HERE / ".out"
LIVE_REPO = Path(r"D:\메타넷 최종\A360-Assistant-Backend")
NOW = dt.datetime(2026, 7, 15, 0, 0, 10, tzinfo=dt.timezone.utc)
FIX = HERE / "policies_approved_fixture"
D_A = core.sha256_bytes(b"a")
D_PAY = core.sha256_bytes(b"payload")
V = load_validators(HERE / "schemas")

ROWS = {"boundary_validation": (StoreRow("runtime_validation", "candidate_id", "cand-77",
                                         NOW - dt.timedelta(seconds=5), "obs-rev-42"),),
        "audit_logs": (StoreRow("audit_log", "request_id", "req-9",
                                NOW - dt.timedelta(seconds=5), "obs-rev-42"),)}
BOUNDARY = {"candidate_id": "cand-77", "candidate_payload_digest": D_PAY,
            "boundary_schema_digest": D_A, "catalog_snapshot_digest": D_A,
            "boundary_validator_version": "boundary@0.1.0"}


def git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True).stdout.strip()


def w(name, obj):
    OUT.mkdir(exist_ok=True)
    p = OUT / name
    p.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    return p


def authority(receipts=None, event_store=None, policy_dir=FIX, source="test_only_fixture",
              public_boundary=None):
    return AuthorityContext(
        event_store=event_store or FixtureEventStore(ROWS, queried_at=NOW),
        boundary_store=BoundaryRecordStore(
            records={"BR-1": json.dumps(BOUNDARY, sort_keys=True).encode()}),
        receipt_store=ChangeReceiptStore(records=receipts or {}, validator=V["change-receipt"]),
        policy_dir=policy_dir, source=source,
        public_boundary=public_boundary or PUBLIC_CONTRACT)


def make_subject_repo():
    repo = OUT / "subject"
    if repo.exists():
        import shutil
        shutil.rmtree(repo, ignore_errors=True)
    (repo / "app" / "api").mkdir(parents=True)
    git(repo, "init", "-q")
    subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=repo)
    subprocess.run(["git", "config", "user.name", "harness"], cwd=repo)
    (repo / "README.md").write_text("subject\n", encoding="utf-8")
    (repo / "app" / "api" / "sessions.py").write_text(
        "from app.agent import stream_agent_turn\n\n\ndef turn():\n    return stream_agent_turn\n",
        encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "baseline")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "app" / "api" / "sessions.py").write_text(
        "from app.agent import stream_agent_turn\n\n\ndef turn(x):\n    return stream_agent_turn\n",
        encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "benign change")
    return repo, base, git(repo, "rev-parse", "HEAD")


def build_change_docs(at, ctx, repo, gates_override=None, tag=""):
    reader = ctx.object_reader(repo)
    changed = list(ctx.changed_paths(repo))
    dep = at.resolver.resolve("dependency-path-policy").obj
    app = at.resolver.resolve("applicability-policy").obj
    d = rules.derive_projection(ctx, dep, app, changed, reader.read_text)
    registry = Artifact.load(HERE / "control-registry.yaml", at.validators["control-registry"])
    cm = {"schema_version": "1.8", "manifest_id": "CM-0a1b2c3d", "created_at": "2026-07-15T00:00:00Z",
          "declared_repository_id": ctx.repository_id, "jira": "RPA-201",
          "intent": "v1.7 vertical path transcript",
          "declared_baseline_sha": ctx.merge_base, "declared_head_sha": ctx.event_head_sha,
          "declared_changed_paths": sorted(changed),
          "declared_dependency_changed": d["dependency_changed"],
          "declared_touches_agent_owned_paths": d["touches_agent_owned_paths"],
          "declared_risk_profiles": d["risk_profiles"],
          "declared_control_applicability": ({"controls_expected": d["applicable_controls"]}
                                             if d["applicable_controls"]
                                             else {"no_applicable_reason": "none derived"}),
          "generated_env": {"python": "3.11.9", "os": "ubuntu-22.04", "dependency_digest": D_A},
          "rollback": "n/a"}
    if d["touches_agent_owned_paths"]:
        cm["agent_owner_approval_ref"] = "AP-1"
    cmp_ = w(f"manifest{tag}.json", cm)
    cm_art = Artifact.load(cmp_, at.validators["change-manifest"])
    DG = at.resolved_digests(registry)
    gates = gates_override if gates_override is not None else [
        {"control_id": c, "status": "pass",
         "evidence_ref": {"uri": f"ci://{c}", "type": "junit", "digest": D_A}}
        for c in d["applicable_controls"]]
    if not gates:
        gates = [{"control_id": "CH-08", "status": "pass",
                  "evidence_ref": {"uri": "ci://CH-08", "type": "junit", "digest": D_A}}]
    rep = {"schema_version": "1.8",
           "run": {"run_id": "vp", "started_at": "2026-07-15T00:00:00Z",
                   "ended_at": "2026-07-15T00:01:00Z", "git_state": ctx.git_state,
                   "tools": [{"name": "attestor", "version": "1.8.0"}]},
           "subject": {"repository_id": ctx.repository_id, "commit": ctx.event_head_sha},
           "trusted_context": {"mode": ctx.mode, "repository_id": ctx.repository_id,
                               "event_type": ctx.event_type, "pr_base_sha": ctx.pr_base_sha,
                               "event_head_sha": ctx.event_head_sha,
                               "checkout_sha": ctx.checkout_sha, "merge_base": ctx.merge_base,
                               "workflow_ref": ctx.workflow_ref, "git_state": ctx.git_state},
           "manifest_binding": {"change_manifest_digest": cm_art.digest,
                                "manifest_repository_id": ctx.repository_id,
                                "manifest_baseline_sha": ctx.merge_base,
                                "manifest_head_sha": ctx.event_head_sha},
           "derived_facts": {"derivation_tool": "attestor", "derivation_version": "1.8.0",
                             "dependency_path_policy_digest": DG["dependency-path-policy"],
                             "applicability_policy_digest": DG["applicability-policy"],
                             **{k: d[k] for k in ("repository_id", "diff_baseline_sha",
                                                  "diff_head_sha", "changed_paths",
                                                  "dependency_changed", "silent_install_hits",
                                                  "unreadable_scanned_paths",
                                                  "touches_agent_owned_paths", "risk_profiles",
                                                  "applicable_controls", "git_state")}},
           "control_registry_digest": DG["registry"], "schema_digest": DG["schemas"],
           "runtime_env": {"python_version": "3.11.9", "os": "ubuntu-22.04", "dependency_digest": D_A},
           "gates": gates,
           "verdict": ("blocked" if ctx.git_state != "clean"
                       else "fail" if any(g["status"] in ("fail", "skipped") for g in gates)
                       else "pass")}
    return cmp_, w(f"report{tag}.json", rep), d


def _observed_provenance(at):
    """Build the provenance block FROM the runner-bound observation, not from a guess."""
    obs = at.authority.observer().observe()
    return {"requested_agent_version": obs.resolved_version,
            "boundary_derived_default": "v2",
            "producer_resolved_version": obs.resolved_version,
            "resolved_version_observability": obs.observability,
            "agent_registry_snapshot_digest": obs.registry_snapshot_digest,
            "public_contract_version": obs.public_contract_version,
            "agent_version_policy_digest": at.resolver.resolve("agent-version-policy").digest}


def build_output_docs(at, ctx, report_art, receipt_id, provenance_override=None):
    registry = Artifact.load(HERE / "control-registry.yaml", at.validators["control-registry"])
    DG = at.resolved_digests(registry, report_art)
    rv = {"schema_version": "1.8", "event_kind": "candidate_validation", "origin": "manual_edit_api",
          "mutation_kind": "revise", "llm_invoked": False,
          "identity": {"candidate_id": "cand-77", "session_id": "s-1", "request_id": "req-9"},
          "binding": {"candidate_payload_digest": D_PAY, "canonicalization": "jcs-rfc8785",
                      "boundary_schema_digest": D_A, "catalog_snapshot_digest": D_A,
                      "boundary_validator_version": "boundary@0.1.0",
                      "validated_at": "2026-07-15T00:00:00Z"},
          "blocking_policy": {"policy_id": "sev.blocking", "version": "0.1.0-fixture",
                              "digest": at.resolver.resolve("severity-policy").digest,
                              "state": "approved", "blocking_severities": ["critical", "high"]},
          "producer_advisory": {"present": False, "availability": "de_facto_untyped"},
          "detectors": {"strict_schema": {"passed": True, "schema_id": "B@1"},
                        "catalog_closure": {"scope": "solution=a360", "passed": True}},
          "violations": [], "assurance_status": "validated",
          "enforcement": {"mode": "observe", "effect": "none"},
          "business_outcome": {"persisted": True, "recommendation_version": 4}, "reasons": [],
          # post-merge item 3: three distinct facts, never collapsed into agent_version=null.
          # v1.9 P0-2: HL-20 now READS this block. The resolved version comes from the test-only
          # public-contract adapter, never from a guess or from the boundary's own default.
          # v1.10: every field below must MATCH the runner-bound observation, and HL-20 compares.
          "agent_provenance": provenance_override or _observed_provenance(at)}
    rvp = w("runtime.json", rv)
    rv_art = Artifact.load(rvp, at.validators["runtime-validation"])
    brec = at.authority.boundary_store.resolve("BR-1")
    opd = at.resolver.resolve("operation-policy").digest
    eb = {"schema_version": "1.8", "trace_id": "t-1", "operation": "candidate_validation",
          "operation_policy": {"policy_id": "op.policy", "version": "0.1.0-fixture", "digest": opd,
                               "state": "approved"},
          "change_receipt_id": receipt_id,
          "subject": {"request_id": "req-9", "session_id": "s-1", "candidate_id": "cand-77",
                      "candidate_payload_digest": D_PAY, "recommendation_version": 4,
                      "provenance_status": "assured",
                      "repository_id": ctx.repository_id, "commit": ctx.event_head_sha},
          "runtime_env": {"python_version": "3.11.9", "os": "ubuntu-22.04", "dependency_digest": D_A},
          "assurer": {"id": "attestor", "version": "1.8.0", "build": "b1"},
          "generated_at": "2026-07-15T00:00:00Z",
          "hashing": {"algorithm": "sha256", "canonicalization": "jcs-rfc8785",
                      "registry_digest_method": "raw_bytes"},
          "control_registry_digest": DG["registry"], "schema_digest": DG["schemas"],
          "linked_reports": {"assurance_report_digest": report_art.digest,
                             "runtime_validation_digest": rv_art.digest},
          "dual_evidence": {"boundary_enforcement_digest": brec.digest,
                            "bound_candidate_id": "cand-77",
                            "bound_candidate_payload_digest": D_PAY, "bound_request_id": "req-9"},
          "completeness": {"policy_id": "op.policy", "policy_version": "0.1.0-fixture",
                           "policy_digest": opd, "operation_binding": "candidate_validation",
                           "derivation": {"attestor_id": "attestor", "attestor_version": "1.8.0",
                                          "source_revision": "obs-rev-42",
                                          "queried_at": "2026-07-15T00:00:10Z"},
                           "expected": [
                               {"stage_id": "boundary_validation", "event_kind": "runtime_validation",
                                "min_count": 1, "max_count": 1, "source_table": "boundary_validation",
                                "correlation_key": "candidate_id", "window_seconds": 60,
                                "condition": "always"},
                               {"stage_id": "audit", "event_kind": "audit_log", "min_count": 1,
                                "max_count": None, "source_table": "audit_logs",
                                "correlation_key": "request_id", "window_seconds": 60,
                                "condition": "always"}],
                           "observed": [{"stage_id": "boundary_validation", "count": 1},
                                        {"stage_id": "audit", "count": 1}],
                           "missing": [], "complete": True},
          "required_controls": ["OH-01", "OH-02", "OH-06", "EH-03", "EH-07"], "missing_controls": [],
          "controls": [{"control_id": c, "status": "pass",
                        "evidence_ref": {"uri": f"db://{c}", "type": "db_row", "digest": D_A}}
                       for c in ["OH-01", "OH-02", "OH-06", "EH-03", "EH-07"]],
          "assurance_verdict": "assured"}
    return w("bundle.json", eb), rvp


class WrongQueriedAtStore(FixtureEventStore):
    def query(self, *a, **k):
        rows, ev = super().query(*a, **k)
        return rows, replace(ev, queried_at=NOW - dt.timedelta(days=1))


class _CraftedStore:
    """Honest-looking metadata, rows the rule must reject itself (v1.6 review item 3)."""

    def __init__(self, mutate):
        self._mutate = mutate

    def query(self, source_table, subject_key, subject_value, window_start, window_end):
        kind = "runtime_validation" if source_table == "boundary_validation" else "audit_log"
        rows = self._mutate(kind, subject_key, subject_value, window_start, window_end)
        ev = QueryEvidence(source_table=source_table, subject_key=subject_key,
                           subject_value=subject_value, window_start=window_start,
                           window_end=window_end, source_revision="obs-rev-42", queried_at=NOW,
                           query_digest=query_digest(source_table, subject_key, subject_value,
                                                     window_start, window_end),
                           row_count=len(rows), result_set_digest=result_set_digest(rows))
        return rows, ev


def wrong_subject(kind, k, v, ws, we):
    return (StoreRow(kind, k, "different-subject", ws + dt.timedelta(seconds=1), "obs-rev-42"),)


def out_of_window(kind, k, v, ws, we):
    return (StoreRow(kind, k, v, ws - dt.timedelta(days=1), "obs-rev-42"),)


def wrong_revision(kind, k, v, ws, we):
    return (StoreRow(kind, k, v, ws + dt.timedelta(seconds=1), "someone-elses-revision"),)


def mixed_rows(kind, k, v, ws, we):
    return (StoreRow(kind, k, v, ws + dt.timedelta(seconds=1), "obs-rev-42"),
            StoreRow(kind, k, "different-subject", ws + dt.timedelta(seconds=2), "obs-rev-42"))


def main() -> int:
    print("=" * 100)
    print("v1.8 VERTICAL PATH TRANSCRIPT")
    print("=" * 100)
    OUT.mkdir(exist_ok=True)
    repo, base, head = make_subject_repo()
    env = {"A360_REPO_ID": "R_synthetic_subject", "A360_EVENT": "pull_request",
           "A360_BASE_SHA": base, "A360_HEAD_SHA": head, "A360_CHECKOUT_SHA": head,
           "A360_MERGE_BASE": base, "A360_WORKFLOW_REF": "wf",
           "A360_CHECKOUT_MODEL": "head_checkout"}
    at = Attestor.for_testing_only(HERE, repo, NOW, authority())
    ctx = TrustedContext.from_runner_env(env, repo)
    reg = HERE / "control-registry.yaml"
    ok = True
    print(f"subject: base={base[:8]} head={head[:8]} mode={ctx.mode} git_state={ctx.git_state}")

    print("\n--- CHANGE 1: all applicable gates pass -> expect allow + receipt --------------")
    cmp_, repp, d = build_change_docs(at, ctx, repo)
    r = at.attest_change(env, cmp_, repp, reg)
    print(f"  applicable={d['applicable_controls']}  evidence_valid={r.evidence_valid}  "
          f"DECISION={r.decision}")
    print(f"  receipt: id={r.receipt['receipt_id']} decision={r.receipt['decision']} "
          f"repo={r.receipt['repository_id']} commit={r.receipt['commit'][:8]}")
    ok &= (r.decision == "allow")
    good_receipt = r.receipt

    for label, gates in [
        ("2: required gate FAIL (honest fail report)",
         [{"control_id": c, "status": "fail",
           "evidence_ref": {"uri": "ci://x", "type": "junit", "digest": D_A}}
          for c in d["applicable_controls"]]),
        ("3: required gate SKIPPED",
         [{"control_id": c, "status": "skipped", "skipped_reason": "runner busy"}
          for c in d["applicable_controls"]]),
        ("4: required gate UNCOVERED",
         [{"control_id": "CH-08", "status": "pass",
           "evidence_ref": {"uri": "ci://CH-08", "type": "junit", "digest": D_A}}]),
    ]:
        c2, p2, _ = build_change_docs(at, ctx, repo, gates_override=gates, tag="_n")
        r2 = at.attest_change(env, c2, p2, reg)
        print(f"--- CHANGE {label}\n      evidence_valid={r2.evidence_valid} DECISION={r2.decision} "
              f"receipt.decision={r2.receipt['decision']}")
        ok &= (r2.decision != "allow" and r2.receipt["decision"] != "allow")

    print("\n--- CHANGE: SHIPPED unapproved policies -> expect observe ----------------------")
    # v1.10: for_runner takes NO installation root - it derives its own. The fixture policy_dir and
    # the alias label are still passed and still ignored; there is now no third way in either.
    at_real = Attestor.for_runner(repo, NOW, authority(policy_dir=FIX, source="ci_fixture_alias"))
    c3, p3, _ = build_change_docs(at_real, ctx, repo, tag="_s")
    r3 = at_real.attest_change(env, c3, p3, reg)
    print(f"  DECISION={r3.decision}  receipt.decision={r3.receipt['decision']}")
    ok &= (r3.decision == "observe")

    print("\n--- CHANGE: LIVE product repo -> expect refusal --------------------------------")
    lhead = git(LIVE_REPO, "rev-parse", "HEAD")
    lbase = git(LIVE_REPO, "merge-base", "ed1a1b0d62452f75212da4f78f3e8b9f990da42e", lhead)
    lenv = {**env, "A360_REPO_ID": "R_kgDO-a360-backend", "A360_BASE_SHA": lbase,
            "A360_HEAD_SHA": lhead, "A360_CHECKOUT_SHA": lhead, "A360_MERGE_BASE": lbase}
    at_live = Attestor.for_testing_only(HERE, LIVE_REPO, NOW, authority())
    lctx = TrustedContext.from_runner_env(lenv, LIVE_REPO)
    if lctx.authoritative:
        lc, lp, _ = build_change_docs(at_live, lctx, LIVE_REPO, tag="_live")
        rl = at_live.attest_change(lenv, lc, lp, reg)
        print(f"  git_state={lctx.git_state} evidence_valid={rl.evidence_valid} "
              f"DECISION={rl.decision}")
        print(f"    reason: {(rl.evidence_reasons or ('',))[0][:88]}")
        ok &= (rl.decision != "allow")

    print("\n--- OUTPUT: receipt + report + runtime + boundary + observations -> assured ----")
    report_art = Artifact.load(repp, at.validators["assurance-report"])
    rid = good_receipt["receipt_id"]
    at2 = Attestor.for_testing_only(HERE, repo, NOW, authority(
        receipts={rid: json.dumps(good_receipt, sort_keys=True).encode()}))
    ebp, rvp = build_output_docs(at2, ctx, report_art, rid)
    r4 = at2.attest_output(ebp, rvp, repp, cmp_, "BR-1", reg)
    print(f"  evidence_valid={r4.evidence_valid}  DECISION={r4.decision}")
    for rid_, res in r4.rule_results:
        print(f"    {rid_:<6} ok={str(res.ok):<5} {res.detail[:58]}")
    for a in r4.resolved_authorities:
        if a.startswith(("change_receipt", "assurance_report", "boundary_record")):
            print(f"    resolved {a[:86]}")
    ok &= (r4.decision == "allow")

    print("\n--- OUTPUT negatives (v1.6 review) --------------------------------------------")

    def out_neg(label, *, receipts=None, store=None, report=None, mutate=None, receipt_id=rid):
        a = Attestor.for_testing_only(HERE, repo, NOW, authority(
            receipts=receipts if receipts is not None
            else {rid: json.dumps(good_receipt, sort_keys=True).encode()}, event_store=store))
        p = ebp
        if mutate:
            b = json.loads(Path(ebp).read_text(encoding="utf-8"))
            mutate(b)
            p = w("bundle_neg.json", b)
        rr = a.attest_output(p, rvp, report if report is not None else repp, cmp_, "BR-1", reg)
        why = (rr.evidence_reasons or ("",))[0]
        print(f"  {label:<38} -> {rr.decision:<6} {why[:62]}")
        return rr.decision != "allow"

    rep_un = json.loads(Path(repp).read_text(encoding="utf-8"))
    rep_un["subject"]["repository_id"] = "R_unrelated_change"
    rep_un["subject"]["commit"] = "f" * 40
    rep_un["manifest_binding"]["manifest_repository_id"] = "R_unrelated_change"
    rep_un["manifest_binding"]["manifest_head_sha"] = "f" * 40
    p_un = w("report_unrelated.json", rep_un)
    un_art = Artifact.load(p_un, at.validators["assurance-report"])
    ok &= out_neg("unrelated repository/commit report", report=p_un,
                  mutate=lambda b: b["linked_reports"].__setitem__("assurance_report_digest",
                                                                   un_art.digest))
    rep_ic = json.loads(Path(repp).read_text(encoding="utf-8"))
    rep_ic["manifest_binding"]["manifest_head_sha"] = "e" * 40
    p_ic = w("report_inconsistent.json", rep_ic)
    ic_art = Artifact.load(p_ic, at.validators["assurance-report"])
    ok &= out_neg("internally inconsistent report", report=p_ic,
                  mutate=lambda b: b["linked_reports"].__setitem__("assurance_report_digest",
                                                                   ic_art.digest))
    ok &= out_neg("unattested report (no receipt resolves)", receipts={})
    for label, key, dec in [("receipt says deny", "CR-deny", "deny"),
                            ("receipt says observe", "CR-obs", "observe")]:
        rc = {**good_receipt, "receipt_id": key, "decision": dec, "evidence_valid": True}
        ok &= out_neg(label, receipts={key: json.dumps(rc, sort_keys=True).encode()},
                      receipt_id=key, mutate=lambda b, k=key: b.__setitem__("change_receipt_id", k))
    rc = {**good_receipt, "receipt_id": "CR-sub", "repository_id": "R_other"}
    ok &= out_neg("receipt subject != output subject",
                  receipts={"CR-sub": json.dumps(rc, sort_keys=True).encode()}, receipt_id="CR-sub",
                  mutate=lambda b: b.__setitem__("change_receipt_id", "CR-sub"))
    ok &= out_neg("QueryEvidence.queried_at mismatch", store=WrongQueriedAtStore(ROWS, queried_at=NOW))
    ok &= out_neg("rows with a different subject", store=_CraftedStore(wrong_subject))
    ok &= out_neg("rows outside the requested window", store=_CraftedStore(out_of_window))
    ok &= out_neg("rows with a different source revision", store=_CraftedStore(wrong_revision))
    ok &= out_neg("mixed valid + invalid rows", store=_CraftedStore(mixed_rows))

    print("\n" + "=" * 100)
    print(f"VERTICAL PATHS: {'BOTH POSITIVE PATHS REACHED A SUCCESS STATE' if ok else 'INCOMPLETE'}")
    print("Generated documents were written to .out/ (outside the hashed source manifest).")
    print("Positive paths used Attestor.for_testing_only + policies_approved_fixture (TEST ONLY).")
    print("Shipped policies/ remain state=unapproved: D-9/D-11/D-20 are human decisions.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
