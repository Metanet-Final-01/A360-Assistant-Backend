"""Phase 0 v1.10 CONTRACT SELF-TEST (renamed per v1.4 review P0-1 / acceptance 1).

This is NOT an attestation. It exercises the contract against deterministic fixtures.
Fixture output is never authoritative. The only thing that may certify a real change is
attest.py, and even that is not proved to run on an isolated CI runner (HL-18, D-16).

Coverage (P0-7): the registry instruments REAL rule invocations, and every executable rule must
have a negative case that fails with the INTENDED reason code. A case whose lambda returns
failure without calling the rule can no longer satisfy coverage.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import platform
import sys
from importlib.metadata import version as pkgver
from pathlib import Path

import core
import rules
from core import Artifact, ArtifactStore, PolicyResolver, TrustedContext, load_validators, schema_set_digest
from store import BoundaryRecordStore, FixtureEventStore, StoreRow

HERE = Path(__file__).parent
REPO = Path(r"D:\메타넷 최종\A360-Assistant-Backend")
NOW = dt.datetime(2026, 7, 15, 0, 0, 10, tzinfo=dt.timezone.utc)

V = load_validators(HERE / "schemas")
R = PolicyResolver(HERE / "policies", V)
AS = ArtifactStore(HERE / "policies", V)
DEP = R.resolve("dependency-path-policy").obj
APP = R.resolve("applicability-policy").obj
IFACE = R.resolve("interface-policy").obj
REG = Artifact.load(HERE / "control-registry.yaml", V["control-registry"])

REPO_ID = "R_kgDO-a360-backend"
BASE = "ed1a1b0d62452f75212da4f78f3e8b9f990da42e"
HEAD = "50687eb26144ebc3dfd6c6aa22262b09199986a2"
SUBJECT = {"repository_id": REPO_ID, "commit": HEAD}
D_A = core.sha256_bytes(b"a")
D_PAY = core.sha256_bytes(b"payload")

CTX = TrustedContext(mode="authoritative", checkout_model="head_checkout", repository_id=REPO_ID, repository_name="A360-Assistant-Backend",
                     event_type="pull_request", pr_base_sha=BASE, event_head_sha=HEAD, checkout_sha=HEAD,
                     merge_base=BASE, workflow_ref="wf@refs/heads/dev", git_state="clean")
CTX_LOCAL = TrustedContext.local("runner env missing ['A360_REPO_ID']")

CASES: list[tuple] = []


def case(rid, name, expect_ok, expect_reason, fn):
    CASES.append((rid, name, expect_ok, expect_reason, fn))


def art(obj, name, schema):
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()
    return Artifact.from_bytes(f"mem://{name}.json", raw, V[schema])


def reader_ok(rel):
    p = REPO / rel
    return p.read_text(encoding="utf-8", errors="ignore") if p.is_file() else None


def reader_install(rel):
    return "pip install ghostlib==1.0\n"


def reader_boom(rel):
    raise OSError("permission denied")


# v1.10: this list must stay identical to Attestor.resolved_digests(). It had drifted - it omitted
# agent-version-policy, so the self-test's idea of "the resolved authority" was staler than the
# attestor's, and the receipt-omission negative silently passed against a set that never had the key.
DIGESTS = {"registry": REG.digest, "schemas": schema_set_digest(HERE / "schemas"),
           **{k: R.resolve(k).digest for k in ("operation-policy", "severity-policy",
                                               "dependency-path-policy", "applicability-policy",
                                               "interface-policy", "agent-version-policy")}}


def proj(changed, reader=reader_ok, ctx=CTX):
    return rules.derive_projection(ctx, DEP, APP, changed, reader)


# ---------------------------------------------------------------- manifest / report builders
def cm(changed, **over):
    d = proj(changed)
    o = {"schema_version": "1.8", "manifest_id": "CM-0a1b2c3d", "created_at": "2026-07-15T00:00:00Z",
         "declared_repository_id": REPO_ID, "jira": "RPA-201", "intent": "boundary gate",
         "declared_baseline_sha": BASE, "declared_head_sha": HEAD,
         "declared_changed_paths": sorted(changed),
         "declared_dependency_changed": d["dependency_changed"],
         "declared_touches_agent_owned_paths": d["touches_agent_owned_paths"],
         "declared_risk_profiles": d["risk_profiles"],
         "declared_control_applicability": ({"controls_expected": d["applicable_controls"]}
                                            if d["applicable_controls"]
                                            else {"no_applicable_reason": "no applicable control derived"}),
         "generated_env": {"python": "3.11.9", "os": "ubuntu-22.04", "dependency_digest": D_A},
         "rollback": "flag off"}
    o.update(over)
    return o


def rep(changed, gates=None, verdict="pass", **over):
    d = proj(changed)
    o = {"schema_version": "1.8",
         "run": {"run_id": "gha-1", "started_at": "2026-07-15T00:00:00Z", "ended_at": "2026-07-15T00:05:00Z",
                 "git_state": "clean", "tools": [{"name": "attestor", "version": "1.5.0"}]},
         "subject": {"repository_id": REPO_ID, "commit": HEAD},
         "trusted_context": {"mode": "authoritative", "repository_id": REPO_ID, "event_type": "pull_request",
                             "pr_base_sha": BASE, "event_head_sha": HEAD, "checkout_sha": HEAD,
                             "merge_base": BASE, "workflow_ref": "wf@refs/heads/dev", "git_state": "clean"},
         "manifest_binding": {"change_manifest_digest": D_A, "manifest_repository_id": REPO_ID,
                              "manifest_baseline_sha": BASE, "manifest_head_sha": HEAD},
         "derived_facts": {"derivation_tool": "attestor", "derivation_version": "1.5.0",
                           "dependency_path_policy_digest": DIGESTS["dependency-path-policy"],
                           "applicability_policy_digest": DIGESTS["applicability-policy"],
                           **{k: d[k] for k in ("repository_id", "diff_baseline_sha", "diff_head_sha",
                                                "changed_paths", "dependency_changed", "silent_install_hits",
                                                "unreadable_scanned_paths", "touches_agent_owned_paths",
                                                "risk_profiles", "applicable_controls", "git_state")}},
         "control_registry_digest": DIGESTS["registry"], "schema_digest": DIGESTS["schemas"],
         "runtime_env": {"python_version": "3.11.9", "os": "ubuntu-22.04", "dependency_digest": D_A},
         "gates": gates if gates is not None else [
             {"control_id": c, "status": "pass",
              "evidence_ref": {"uri": f"ci://{c}", "type": "junit", "digest": D_A}}
             for c in d["applicable_controls"]] or [
             {"control_id": "CH-08", "status": "pass",
              "evidence_ref": {"uri": "ci://CH-08", "type": "junit", "digest": D_A}}],
         "verdict": verdict}
    o.update(over)
    return o


SRC_ONLY = ["app/api/sessions.py"]          # -> OH-02 applicable
DEP_CHG = ["requirements.txt"]              # -> CH-04 applicable
AGENT_CHG = ["app/agent/verify/checker.py"]  # -> CH-06 applicable

_cm_src = art(cm(SRC_ONLY), "cm_src", "change-manifest")
_rep_src = rep(SRC_ONLY)
_rep_src["manifest_binding"]["change_manifest_digest"] = _cm_src.digest
_rep_src_art = art(_rep_src, "rep_src", "assurance-report")

# ---------------------------------------------------------------- HL-11
case("HL-11", "manifest equals trusted-event projection", True, None,
     lambda: rules.hl11(CTX, _cm_src, proj(SRC_ONLY)))
case("HL-11", "P0-1: non-authoritative context cannot certify", False, "context_not_authoritative",
     lambda: rules.hl11(CTX_LOCAL, _cm_src, proj(SRC_ONLY, ctx=CTX_LOCAL)))
case("HL-11", "P0-3: manifest declares CH-04 while the derived set is OH-02 (exact equality)", False,
     "declaration_mismatch",
     lambda: rules.hl11(CTX, art(cm(SRC_ONLY, declared_control_applicability={"controls_expected": ["CH-04"]}),
                                 "cm_div", "change-manifest"), proj(SRC_ONLY)))
case("HL-11", "P1-2: unreadable in-scope file is indeterminate, not clean", False, "scan_indeterminate",
     lambda: rules.hl11(CTX, art(cm(["scripts/x.sh"]), "cm_un", "change-manifest"),
                        proj(["scripts/x.sh"], reader=reader_boom)))

# ---------------------------------------------------------------- HL-12
case("HL-12", "report bound to read-once manifest + resolved digests + full projection", True, None,
     lambda: rules.hl12(CTX, _cm_src, _rep_src_art, proj(SRC_ONLY), DIGESTS))
case("HL-12", "P0-2/P0-5: manifest digest != read-once artifact", False, "manifest_binding_mismatch",
     lambda: rules.hl12(CTX, _cm_src, art(rep(SRC_ONLY), "rep_bd", "assurance-report"),
                        proj(SRC_ONLY), DIGESTS))
case("HL-12", "P0-5: registry digest not resolved to real bytes", False, "digest_unresolved",
     lambda: rules.hl12(CTX, _cm_src, _rep_src_art, proj(SRC_ONLY), {**DIGESTS, "registry": D_A}))


def _rep_false_facts():
    r = rep(DEP_CHG)
    r["manifest_binding"]["change_manifest_digest"] = art(cm(DEP_CHG), "cmx", "change-manifest").digest
    r["derived_facts"]["dependency_changed"] = False
    r["derived_facts"]["applicable_controls"] = []
    return r


case("HL-12", "P0-5: report states false derived facts for a dependency diff", False, "derived_facts_mismatch",
     lambda: rules.hl12(CTX, art(cm(DEP_CHG), "cmx", "change-manifest"),
                        art(_rep_false_facts(), "rep_ff", "assurance-report"), proj(DEP_CHG), DIGESTS))

# ---------------------------------------------------------------- HL-01/02
case("HL-01", "registry ids unique", True, None, lambda: rules.hl01(REG))
case("HL-01", "duplicate control id", False, "duplicate_control_id",
     lambda: rules.hl01(Artifact.from_bytes("mem://d.json",
                                            json.dumps({"controls": [{"id": "OH-02"}, {"id": "OH-02"}]}).encode())))
case("HL-02", "all references are registry members", True, None, lambda: rules.hl02(REG, ["CH-04", "OH-02"]))
case("HL-02", "orphan control id CH-99", False, "orphan_control_id", lambda: rules.hl02(REG, ["CH-99"]))

# ---------------------------------------------------------------- HL-03
_rep_dep = rep(DEP_CHG)
case("HL-03", "applicable CH-04 gate present and passing", True, None,
     lambda: rules.hl03(REG, art(_rep_dep, "rd1", "assurance-report"), proj(DEP_CHG), AS, NOW))
case("HL-03", "P0-3: report drops applicability while manifest declared it", False, "applicability_divergence",
     lambda: rules.hl03(REG, art(_rep_false_facts(), "rd2", "assurance-report"), proj(DEP_CHG), AS, NOW))


def _rep_unrelated_gate():
    return rep(SRC_ONLY, gates=[{"control_id": "CH-08", "status": "pass",
                                 "evidence_ref": {"uri": "ci://CH-08", "type": "junit", "digest": D_A}}])


case("HL-03", "P0-3: unrelated passing gate does not cover the applicable OH-02", False, "gate_uncovered",
     lambda: rules.hl03(REG, art(_rep_unrelated_gate(), "rd3", "assurance-report"), proj(SRC_ONLY), AS, NOW))


def _rep_waived(wid):
    return rep(DEP_CHG, gates=[
        {"control_id": "CH-04", "status": "waived", "waiver_id": wid, "compensating_control": "CH-08",
         "evidence_ref": {"uri": "ci://w", "type": "report", "digest": D_A}},
        {"control_id": "CH-08", "status": "pass", "evidence_ref": {"uri": "ci://CH-08", "type": "junit",
                                                                   "digest": D_A}}])


case("HL-03", "P0-4: waiver id that resolves to no artifact", False, "waiver_unresolved",
     lambda: rules.hl03(REG, art(_rep_waived("WV-404"), "rw1", "assurance-report"), proj(DEP_CHG), AS, NOW))
case("HL-03", "P0-4: expired waiver artifact", False, "waiver_unresolved",
     lambda: rules.hl03(REG, art(_rep_waived("WV-2"), "rw2", "assurance-report"), proj(DEP_CHG), AS, NOW))
case("HL-03", "P0-4: compensating control not in registry / no passing evidence", False, "waiver_unresolved",
     lambda: rules.hl03(REG, art(_rep_waived("WV-3"), "rw3", "assurance-report"), proj(DEP_CHG), AS, NOW))
case("HL-03", "valid waiver artifact with a passing compensating control", True, None,
     lambda: rules.hl03(REG, art(_rep_waived("WV-1"), "rw4", "assurance-report"), proj(DEP_CHG), AS, NOW))

# ---------------------------------------------------------------- HL-07
case("HL-07", "no agent path touched", True, None,
     lambda: rules.hl07(_cm_src, proj(SRC_ONLY), AS, SUBJECT, NOW))
case("HL-07", "P0-4: agent path touched but undeclared", False, "agent_change_undeclared",
     lambda: rules.hl07(art(cm(AGENT_CHG, declared_touches_agent_owned_paths=False), "ca1", "change-manifest"),
                        proj(AGENT_CHG), AS, SUBJECT, NOW))
case("HL-07", "P0-4: approval ref resolves to no artifact", False, "approval_unresolved",
     lambda: rules.hl07(art(cm(AGENT_CHG, agent_owner_approval_ref="AP-404"), "ca2", "change-manifest"),
                        proj(AGENT_CHG), AS, SUBJECT, NOW))
case("HL-07", "P0-4: approval signed by the wrong role", False, "approval_unresolved",
     lambda: rules.hl07(art(cm(AGENT_CHG, agent_owner_approval_ref="AP-WRONGROLE"), "ca3", "change-manifest"),
                        proj(AGENT_CHG), AS, SUBJECT, NOW))
case("HL-07", "resolved, role-scoped, path-scoped approval artifact", True, None,
     lambda: rules.hl07(art(cm(AGENT_CHG, agent_owner_approval_ref="AP-1"), "ca4", "change-manifest"),
                        proj(AGENT_CHG), AS, SUBJECT, NOW))

# ---------------------------------------------------------------- evidence bundle / runtime
def rv(**over):
    o = {"schema_version": "1.8", "event_kind": "candidate_validation", "origin": "manual_edit_api",
         "mutation_kind": "revise", "llm_invoked": False,
         "identity": {"candidate_id": "cand-77", "session_id": "s-1", "request_id": "req-9"},
         "binding": {"candidate_payload_digest": D_PAY, "canonicalization": "jcs-rfc8785",
                     "boundary_schema_digest": D_A, "catalog_snapshot_digest": D_A,
                     "boundary_validator_version": "boundary@0.1.0", "validated_at": "2026-07-15T00:00:00Z"},
         "blocking_policy": {"policy_id": "sev.blocking", "version": "0.1.0-proposed",
                             "digest": DIGESTS["severity-policy"], "state": "unapproved",
                             "blocking_severities": ["critical", "high"]},
         "producer_advisory": {"present": False, "availability": "de_facto_untyped"},
         "detectors": {"strict_schema": {"passed": True, "schema_id": "B@1"},
                       "catalog_closure": {"scope": "solution=a360", "passed": True}},
         "violations": [], "assurance_status": "validated",
         "enforcement": {"mode": "observe", "effect": "none"},
         "business_outcome": {"persisted": True, "recommendation_version": 4}, "reasons": [],
         "agent_provenance": {"requested_agent_version": None, "boundary_derived_default": "v2",
                              "producer_resolved_version": None,
                              "resolved_version_observability": "not_observable_pending_D21",
                              "agent_registry_snapshot_digest": D_A,
                              "public_contract_version": "0.2.0-fixture"}}
    o.update(over)
    return o


_rv_art = art(rv(), "rv", "runtime-validation")
# v1.5 P0-4: rules can only ever receive a BoundaryRecord resolved by the runner-owned adapter.
_BSTORE = BoundaryRecordStore(records={"BR-1": json.dumps(
    {"candidate_id": "cand-77", "candidate_payload_digest": D_PAY, "boundary_schema_digest": D_A,
     "catalog_snapshot_digest": D_A, "boundary_validator_version": "boundary@0.1.0"},
    sort_keys=True).encode()})
_BREC = _BSTORE.resolve("BR-1")
OPD = DIGESTS["operation-policy"]


def eb(**over):
    o = {"schema_version": "1.8", "trace_id": "t-1", "operation": "candidate_validation",
         "operation_policy": {"policy_id": "op.policy", "version": "0.1.0-proposed", "digest": OPD,
                              "state": "unapproved"},
         "subject": {"request_id": "req-9", "session_id": "s-1", "candidate_id": "cand-77",
                     "candidate_payload_digest": D_PAY, "recommendation_version": 4,
                     "provenance_status": "assured"},
         "runtime_env": {"python_version": "3.11.9", "os": "ubuntu-22.04", "dependency_digest": D_A},
         "assurer": {"id": "attestor", "version": "1.5.0", "build": "b1"},
         "generated_at": "2026-07-15T00:00:00Z",
         "hashing": {"algorithm": "sha256", "canonicalization": "jcs-rfc8785",
                     "registry_digest_method": "raw_bytes"},
         "control_registry_digest": DIGESTS["registry"], "schema_digest": DIGESTS["schemas"],
         "linked_reports": {"runtime_validation_digest": _rv_art.digest},
         "dual_evidence": {"boundary_enforcement_digest": _BREC.digest, "bound_candidate_id": "cand-77",
                           "bound_candidate_payload_digest": D_PAY, "bound_request_id": "req-9"},
         "completeness": {"policy_id": "op.policy", "policy_version": "0.1.0-proposed", "policy_digest": OPD,
                          "operation_binding": "candidate_validation",
                          "derivation": {"attestor_id": "attestor", "attestor_version": "1.5.0",
                                         "source_revision": "obs-rev-42",
                                         "queried_at": "2026-07-15T00:00:10Z"},
                          "expected": [
                              {"stage_id": "boundary_validation", "event_kind": "runtime_validation",
                               "min_count": 1, "max_count": 1, "source_table": "boundary_validation",
                               "correlation_key": "candidate_id", "window_seconds": 60, "condition": "always"},
                              {"stage_id": "audit", "event_kind": "audit_log", "min_count": 1, "max_count": None,
                               "source_table": "audit_logs", "correlation_key": "request_id",
                               "window_seconds": 60, "condition": "always"}],
                          "observed": [{"stage_id": "boundary_validation", "count": 1},
                                       {"stage_id": "audit", "count": 1}],
                          "missing": [], "complete": True},
         "required_controls": ["OH-01", "OH-02", "OH-06", "EH-03", "EH-07"], "missing_controls": [],
         "controls": [{"control_id": c, "status": "pass",
                       "evidence_ref": {"uri": f"db://{c}", "type": "db_row", "digest": D_A}}
                      for c in ["OH-01", "OH-02", "OH-06", "EH-03", "EH-07"]],
         "assurance_verdict": "refused",
         "refused_reasons": ["operation policy is unapproved (D-11)"]}
    o.update(over)
    return o


ROWS = {"boundary_validation": (StoreRow("runtime_validation", "candidate_id", "cand-77",
                                         dt.datetime(2026, 7, 15, 0, 0, 5, tzinfo=dt.timezone.utc), "obs-rev-42"),),
        "audit_logs": (StoreRow("audit_log", "request_id", "req-9",
                                dt.datetime(2026, 7, 15, 0, 0, 5, tzinfo=dt.timezone.utc), "obs-rev-42"),)}
STORE = FixtureEventStore(ROWS)
STORE_EMPTY = FixtureEventStore({})
STORE_BROKEN = FixtureEventStore(ROWS, unreadable=frozenset({"audit_logs"}))

# ---------------------------------------------------------------- HL-14
case("HL-14", "P0-3: real operation policy is unapproved -> cannot authorise", False, "policy_unapproved",
     lambda: rules.hl14(R, art(eb(), "eb1", "evidence-bundle")))
case("HL-14", "P0-3: bundle cites a fake policy id/version/digest", False, "policy_mismatch",
     lambda: rules.hl14(R, art(eb(operation_policy={"policy_id": "op.fake", "version": "9.9",
                                                    "digest": D_A, "state": "approved"}),
                               "eb2", "evidence-bundle")))

# ---------------------------------------------------------------- HL-13
case("HL-13", "observations reconcile with the trusted store", True, None,
     lambda: rules.hl13(R, art(eb(), "eb3", "evidence-bundle"), STORE, NOW))
case("HL-13", "P0-6: store has no rows but the bundle claims complete", False, "observation_mismatch",
     lambda: rules.hl13(R, art(eb(), "eb4", "evidence-bundle"), STORE_EMPTY, NOW))
case("HL-13", "P0-6: store read failure fails closed", False, "store_read_failed",
     lambda: rules.hl13(R, art(eb(), "eb5", "evidence-bundle"), STORE_BROKEN, NOW))


def _eb_dup():
    o = eb()
    o["completeness"]["observed"] = [{"stage_id": "audit", "count": 1}, {"stage_id": "audit", "count": 1}]
    return o


case("HL-13", "P0-6: duplicate observed stage claim", False, "duplicate_stage_claim",
     lambda: rules.hl13(R, art(_eb_dup(), "eb6", "evidence-bundle"), STORE, NOW))


def _eb_unknown():
    o = eb()
    o["completeness"]["observed"] = [{"stage_id": "ghost_stage", "count": 1}]
    return o


case("HL-13", "P0-6: observed stage not in the approved policy", False, "unknown_stage_claim",
     lambda: rules.hl13(R, art(_eb_unknown(), "eb7", "evidence-bundle"), STORE, NOW))


def _eb_rev():
    o = eb()
    o["completeness"]["derivation"]["source_revision"] = "made-up-revision"
    return o


case("HL-13", "P0-6: derivation source_revision not bound to store query evidence", False, "derivation_unbound",
     lambda: rules.hl13(R, art(_eb_rev(), "eb8", "evidence-bundle"), STORE, NOW))

# ---------------------------------------------------------------- HL-15
case("HL-15", "full chain resolved and subject-bound", True, None,
     lambda: rules.hl15(REG, art(eb(), "eb9", "evidence-bundle"), _rv_art, _BREC, DIGESTS, AS, NOW))
case("HL-15", "P0-5: candidate assurance with no runtime artifact", False, "runtime_unresolved",
     lambda: rules.hl15(REG, art(eb(), "eb10", "evidence-bundle"), None, _BREC, DIGESTS, AS, NOW))
case("HL-15", "P0-5: schema digest not resolved to real bytes", False, "digest_mismatch",
     lambda: rules.hl15(REG, art(eb(), "eb11", "evidence-bundle"), _rv_art, _BREC,
                        {**DIGESTS, "schemas": D_A}, AS, NOW))
_rv_other = art(rv(identity={"candidate_id": "OTHER", "session_id": "s-1", "request_id": "req-9"}),
                "rvo", "runtime-validation")
case("HL-15", "P0-5: runtime describes a different candidate (digest links, subject diverges)", False,
     "subject_mismatch",
     lambda: rules.hl15(REG, art(eb(linked_reports={"runtime_validation_digest": _rv_other.digest}),
                                 "eb12", "evidence-bundle"),
                        _rv_other, {"digest": D_A}, DIGESTS, AS, NOW))

# ---------------------------------------------------------------- HL-16
case("HL-16", "P0-7: observe under the unapproved policy is legitimate", True, None,
     lambda: rules.hl16(R, _rv_art))
case("HL-16", "P0-3: enforce mode citing an approval the artifact does not have", False,
     "blocking_policy_unapproved",
     lambda: rules.hl16(R, art(rv(blocking_policy={"policy_id": "sev.blocking", "version": "1.0.0",
                                                   "digest": D_A, "state": "approved",
                                                   "blocking_severities": ["critical"]},
                                  detectors={"strict_schema": {"passed": True, "schema_id": "s"},
                                             "catalog_closure": {"scope": "solution=a360", "passed": False}},
                                  violations=[{"violation_id": "v1", "rule": "catalog.action_exists",
                                               "control_id": "OH-02", "severity": "critical", "blocking": True,
                                               "location": "l", "resolved": False}],
                                  assurance_status="invalid",
                                  enforcement={"mode": "enforce", "effect": "blocked"},
                                  business_outcome={"persisted": False},
                                  reasons=[{"code": "catalog_closure_failed"}]),
                               "rve", "runtime-validation")))

# ---------------------------------------------------------------- HL-20 (v1.8 review P0-2)
# Codex drove an unapproved `v404` with a fabricated policy digest to Output allow, because nothing
# resolved agent-version-policy. These cases exist so that can never silently return.
R_FIX = PolicyResolver(HERE / "policies_approved_fixture", V)  # TEST ONLY
_AVP = R_FIX.resolve("agent-version-policy")      # approved fixture: approved_versions [v1, v2]
_AVP_SHIPPED = R.resolve("agent-version-policy")  # SHIPPED: state=unapproved, approved_versions []


from store import (FixturePublicContractAdapter as _FPCA,  # noqa: E402
                   NotConfiguredPublicBoundaryAdapter as _NoObs)

# v1.9 defect (Codex): registry snapshot digest and contract version were never read, so a forged
# registry digest reached allow. HL-20 now compares them against a runner-bound OBSERVATION.
_OBS = _FPCA(resolved_version="v2")
_SEEN = _OBS.observe()


def _prov(**over):
    p = {"requested_agent_version": "v2", "boundary_derived_default": "v2",
         "producer_resolved_version": _SEEN.resolved_version,
         "resolved_version_observability": _SEEN.observability,
         "agent_registry_snapshot_digest": _SEEN.registry_snapshot_digest,
         "public_contract_version": _SEEN.public_contract_version,
         "agent_version_policy_digest": _AVP.digest}
    p.update(over)
    return type("RT", (), {"obj": {"agent_provenance": p}})()


_DG_AVP = dict(DIGESTS, **{"agent-version-policy": _AVP.digest})

case("HL-20", "observed producer version is listed in the approved policy", True, None,
     lambda: rules.hl20(R_FIX, _prov(), _DG_AVP, _OBS))
case("HL-20", "P0(v1.8 Codex): fabricated agent_version_policy_digest", False,
     "agent_version_policy_digest_mismatch",
     lambda: rules.hl20(R_FIX, _prov(agent_version_policy_digest="sha256:" + "9" * 64), _DG_AVP,
                        _OBS))
case("HL-20", "P0(v1.8 Codex): unapproved version v404 with a truthful digest", False,
     "agent_version_unapproved",
     lambda: rules.hl20(R_FIX, _prov(requested_agent_version="v404",
                                     producer_resolved_version="v404"), _DG_AVP,
                        _FPCA(resolved_version="v404",
                              registry_snapshot_digest=_SEEN.registry_snapshot_digest)))
# The SHIPPED policy is state=unapproved with approved_versions: [] - so at d66fce1 nothing can be
# assured through the real policy tree. That is D-22, and it is the correct answer today.
case("HL-20", "P0: shipped agent-version-policy is unapproved -> no version may be assured", False,
     "agent_version_policy_unapproved",
     lambda: rules.hl20(R, _prov(agent_version_policy_digest=_AVP_SHIPPED.digest),
                        dict(DIGESTS, **{"agent-version-policy": _AVP_SHIPPED.digest}), _OBS))
# The CURRENT product state: _done_data carries no version, so a truthful report says so. It must
# fail closed rather than guess (D-21).
case("HL-20", "P0: current product state (null producer version) cannot be assured", False,
     "agent_version_not_observable",
     lambda: rules.hl20(R_FIX, _prov(producer_resolved_version=None,
                                     resolved_version_observability="not_observable_pending_D21"),
                        _DG_AVP, _FPCA(resolved_version=None,
                                       registry_snapshot_digest=_SEEN.registry_snapshot_digest)))
case("HL-20", "P1: requested version != the version the producer actually resolved", False,
     "agent_version_requested_mismatch",
     lambda: rules.hl20(R_FIX, _prov(requested_agent_version="v1"), _DG_AVP, _OBS))
case("HL-20", "P1: assured output with no runtime evidence at all", False,
     "agent_provenance_missing",
     lambda: rules.hl20(R_FIX, None, _DG_AVP, _OBS))


case("HL-20", "P0(v1.9 Codex): fabricated agent_registry_snapshot_digest", False,
     "agent_registry_snapshot_unbound",
     lambda: rules.hl20(R_FIX, _prov(agent_registry_snapshot_digest="sha256:" + "b" * 64), _DG_AVP,
                        _OBS))
case("HL-20", "P0(v1.9 Codex): fabricated public_contract_version", False,
     "public_contract_version_unbound",
     lambda: rules.hl20(R_FIX, _prov(public_contract_version="9.9.9-invented"), _DG_AVP, _OBS))
# The SHIPPED runner has no observer: it fails closed rather than trust a self-report.
case("HL-20", "P0: shipped runner has no public-boundary observer -> fail closed", False,
     "public_boundary_unobserved",
     lambda: rules.hl20(R_FIX, _prov(), _DG_AVP, _NoObs()))
case("HL-20", "P0: no observer supplied at all -> fail closed", False, "public_boundary_unobserved",
     lambda: rules.hl20(R_FIX, _prov(), _DG_AVP, None))
# The self-report lie from v1.9's own self-attack: claiming an observation the observer did not make.
case("HL-20", "P0(v1.9 self-attack): claims observed_from_public_contract, observer saw nothing",
     False, "agent_version_not_observable",
     lambda: rules.hl20(R_FIX, _prov(producer_resolved_version="v2",
                                     resolved_version_observability="observed_from_public_contract"),
                        _DG_AVP, _FPCA(resolved_version=None,
                                       registry_snapshot_digest=_SEEN.registry_snapshot_digest)))


# ---------------------------------------------------------------- HL-19 (v1.6 review item 1)
from store import ChangeReceiptStore as _CRS  # noqa: E402

from core import ATTESTOR_ID, ATTESTOR_VERSION, change_receipt_id  # noqa: E402

# v1.9 defect (Codex): a receipt could omit an authority and be re-addressed around it. The set must
# be exactly the policies the attestor resolved.
_RECEIPT_POLICIES = {k: v for k, v in DIGESTS.items() if k.endswith("-policy")}
_RECEIPT_AUTHORITY = dict(_RECEIPT_POLICIES,
                          registry=DIGESTS["registry"], schemas=DIGESTS["schemas"])
_GOOD_RECEIPT = {"kind": "change-receipt",
                 # v1.9: the id IS the content address of the fields below, and HL-19 recomputes it.
                 "receipt_id": change_receipt_id(REPO_ID, HEAD, D_A, D_A, _RECEIPT_AUTHORITY,
                                                 ATTESTOR_VERSION, "allow", True),
                 "generated_at": "2026-07-15T00:00:00Z", "evidence_valid": True, "decision": "allow",
                 "repository_id": REPO_ID, "commit": HEAD, "assurance_report_digest": D_A,
                 "change_manifest_digest": D_A, "control_registry_digest": DIGESTS["registry"],
                 "schema_digest": DIGESTS["schemas"],
                 "policy_digests": _RECEIPT_POLICIES,
                 "attestor": {"id": ATTESTOR_ID, "version": ATTESTOR_VERSION}}
_GOOD_RID = _GOOD_RECEIPT["receipt_id"]
_RSTORE = _CRS(records={_GOOD_RID: json.dumps(_GOOD_RECEIPT, sort_keys=True).encode()},
               validator=V["change-receipt"])
_RECEIPT = _RSTORE.resolve(_GOOD_RID)


def _receipt_variant(**over):
    """A receipt whose payload is mutated AFTER its id was computed - i.e. a forgery."""
    r = dict(_GOOD_RECEIPT)
    r.update(over)
    raw = json.dumps(r, sort_keys=True).encode()
    return _CRS(records={r["receipt_id"]: raw}, validator=V["change-receipt"]).resolve(
        r["receipt_id"])


class _RepArt:
    digest = D_A
    obj = {"manifest_binding": {"manifest_head_sha": HEAD, "manifest_repository_id": REPO_ID},
           "subject": {"commit": HEAD, "repository_id": REPO_ID}}


def _eb19(**over):
    o = {"subject": {"repository_id": REPO_ID, "commit": HEAD}}
    o.update(over)
    return type("B", (), {"obj": o})()


case("HL-19", "receipt allowed the same repository/commit as the output", True, None,
     lambda: rules.hl19(_eb19(), _RECEIPT, _RepArt(), D_A, DIGESTS))
# v1.8 defect (Codex): a schema-valid FORGED receipt - arbitrary id, unknown attestor - resolved
# from the trusted store and authorised Output. HL-19 now recomputes the content address and the
# issuer. HONEST SCOPE: this catches an inconsistent receipt, not a well-formed forgery from a
# writer that can compute a correct address. That needs a signature or a protected writer: D-16.
case("HL-19", "P0(v1.8 Codex): forged receipt id is not its own content address", False,
     "receipt_content_address_mismatch",
     lambda: rules.hl19(_eb19(), _receipt_variant(receipt_id="CR-" + "f" * 32), _RepArt(), D_A,
                        DIGESTS))
case("HL-19", "P0(v1.8 Codex): receipt issued by an unknown attestor", False,
     "receipt_attestor_unknown",
     lambda: rules.hl19(_eb19(), _receipt_variant(
         attestor={"id": "not-the-phase0-attestor", "version": "99.0"}), _RepArt(), D_A, DIGESTS))
# v1.9 defect (Codex): dropping agent-version-policy and recomputing the smaller address reached
# allow. Omitting an authority is now itself the failure, checked before any digest comparison.
case("HL-19", "P0(v1.9 Codex): receipt omits agent-version-policy, re-addressed correctly", False,
     "receipt_policy_set_mismatch",
     lambda: rules.hl19(_eb19(), _receipt_variant(
         policy_digests={k: v for k, v in _RECEIPT_POLICIES.items() if k != "agent-version-policy"},
         receipt_id=change_receipt_id(
             REPO_ID, HEAD, D_A, D_A,
             dict({k: v for k, v in _RECEIPT_POLICIES.items() if k != "agent-version-policy"},
                  registry=DIGESTS["registry"], schemas=DIGESTS["schemas"]),
             ATTESTOR_VERSION, "allow", True)),
         _RepArt(), D_A, DIGESTS))
case("HL-19", "P1: receipt carries an unknown extra policy authority", False,
     "receipt_policy_set_mismatch",
     lambda: rules.hl19(_eb19(), _receipt_variant(
         policy_digests=dict(_RECEIPT_POLICIES, **{"invented-policy": D_A})), _RepArt(), D_A,
         DIGESTS))
case("HL-19", "P1: receipt policy digest != currently resolved authority", False,
     "receipt_policy_digest_mismatch",
     lambda: rules.hl19(_eb19(), _RECEIPT, _RepArt(), D_A,
                        dict(DIGESTS, **{"operation-policy": "sha256:" + "e" * 64})))
case("HL-19", "P1: assured output with no resolved receipt", False, "receipt_unresolved",
     lambda: rules.hl19(_eb19(), None, _RepArt(), D_A, DIGESTS))
# A GENUINE deny receipt: correctly content-addressed, honestly reporting deny. Valid evidence,
# refused decision - the v1.6 P0-2 separation, now at receipt level.
_DENY_RECEIPT = {**_GOOD_RECEIPT, "decision": "deny",
                 "receipt_id": change_receipt_id(REPO_ID, HEAD, D_A, D_A, _RECEIPT_AUTHORITY,
                                                 ATTESTOR_VERSION, "deny", True)}
case("HL-19", "P1: a correctly addressed receipt honestly reporting deny", False,
     "receipt_not_allow",
     lambda: rules.hl19(_eb19(), type("R", (), {"receipt_id": _DENY_RECEIPT["receipt_id"],
                                                "obj": _DENY_RECEIPT})(),
                        _RepArt(), D_A, DIGESTS))
# v1.9 self-found: v1.8's address hashed the subject and authority but NOT the verdict, so this
# flipped receipt kept a valid id and recomputation could not see it. The verdict is now addressed.
case("HL-19", "P0(v1.9 self-found): deny receipt flipped to allow keeps the deny address", False,
     "receipt_content_address_mismatch",
     lambda: rules.hl19(_eb19(), type("R", (), {"receipt_id": _DENY_RECEIPT["receipt_id"],
                                                "obj": {**_DENY_RECEIPT, "decision": "allow"}})(),
                        _RepArt(), D_A, DIGESTS))
case("HL-19", "P1: output subject != receipt subject", False, "receipt_subject_mismatch",
     lambda: rules.hl19(_eb19(subject={"repository_id": "R_other", "commit": HEAD}), _RECEIPT,
                        _RepArt(), D_A, DIGESTS))

# ---------------------------------------------------------------- HL-06 / HL-17
PUBLIC, _ = rules.agent_public_api(REPO, IFACE)
BAD_SRC = {"app/api/a.py": "from app.agent.verify import checker\n",
           "app/api/b.py": "from app.agent import verify\n",
           "app/api/c.py": "from app import agent\n\ndef f():\n    return agent.verify.checker\n",
           "app/api/d.py": "import importlib\nm = importlib.import_module('app.agent.verify')\n",
           # v1.5 P1-1: this exact form was ACCEPTED by v1.5 (Codex reproduction).
           "app/api/e.py": ("from app import agent as owned_agent\n"
                            "private_module = getattr(owned_agent, 'verify')\n")}
INDET_SRC = {"app/api/broken.py": "def f(:\n"}


class _EmptyReader:
    """v1.6 reads assessed sources from a VERIFIED Git object, so HL-06 takes a reader."""

    def list_files(self):
        return ()

    def read_text(self, rel):
        return None


case("HL-06", "no private app.agent import over an empty verified object set", True, None,
     lambda: rules.hl06(_EmptyReader(), PUBLIC))
case("HL-06", "P1-1: direct/parent/alias+getattr/dynamic private imports", False,
     "private_agent_import", lambda: rules.hl06(_EmptyReader(), PUBLIC, BAD_SRC))
case("HL-06", "P1-1: unparsable in-scope source is indeterminate, not clean", False,
     "source_indeterminate", lambda: rules.hl06(_EmptyReader(), PUBLIC, INDET_SRC))
case("HL-17", "no install command in changed files", True, None,
     lambda: rules.hl17(DEP, SRC_ONLY, reader_ok))
case("HL-17", "P1-1: install command hidden in a non-dependency script", False, "silent_install_detected",
     lambda: rules.hl17(DEP, ["scripts/bootstrap.ps1"], reader_install))
case("HL-17", "P1-2: unreadable in-scope file is indeterminate", False, "scan_indeterminate",
     lambda: rules.hl17(DEP, ["scripts/bootstrap.ps1"], reader_boom))


# ---------------------------------------------------------------- runner
def main() -> int:
    print("=" * 104)
    print("Phase 0 v1.10 CONTRACT SELF-TEST  (fixture-driven; NOT an attestation)")
    print(f"python={sys.version.split()[0]}  platform={platform.platform()}")
    print(f"jsonschema={pkgver('jsonschema')}  pyyaml={pkgver('PyYAML')}")
    for k in ("operation-policy", "severity-policy", "dependency-path-policy",
              "applicability-policy", "interface-policy"):
        a = R.resolve(k)
        print(f"policy {k:<26} state={a.obj['state']:<11} {a.digest}")
    print(f"registry {REG.digest}")
    print(f"schema-set {DIGESTS['schemas']}")
    print("=" * 104)
    print(f"{'rule':<7} {'ok':<6} {'want':<6} {'reason':<28} case")
    print("-" * 104)
    bad = 0
    observed_reasons: dict[str, set] = {}
    for rid, name, want_ok, want_reason, fn in CASES:
        try:
            res = fn()
            got_ok, got_reason = res.ok, res.reason
        except Exception as e:
            got_ok, got_reason = None, f"EXC {type(e).__name__}: {e}"
        agree = (got_ok == want_ok) and (want_ok or got_reason == want_reason)
        bad += 0 if agree else 1
        if got_ok is False and isinstance(got_reason, str):
            observed_reasons.setdefault(rid, set()).add(got_reason)
        print(f"{rid:<7} {str(got_ok):<6} {str(want_ok):<6} {str(got_reason or '-')[:28]:<28} "
              f"{'' if agree else 'MISMATCH '}{name}")
    print("-" * 104)
    print(f"cases={len(CASES)}  mismatches={bad}")

    # ---- coverage gate (P0-7): real invocations + intended reason codes
    executable = {r.rid for r in core.REGISTRY.values() if r.executable}
    never_called = sorted(rid for rid in executable if core.REGISTRY[rid].calls == 0)
    no_negative = sorted(rid for rid in executable if not observed_reasons.get(rid))
    undeclared = sorted(f"{rid}:{r}" for rid, rs in observed_reasons.items()
                        for r in rs if core.REGISTRY[rid].reasons and r not in core.REGISTRY[rid].reasons)
    print("\ncoverage gate (P0-7): real invocation + intended reason code")
    print(f"  executable rules      : {len(executable)}")
    print("  invocation counts     : " + str({rid: core.REGISTRY[rid].calls for rid in sorted(executable)}))
    print(f"  never invoked         : {never_called or 'none'}")
    print(f"  no negative w/ reason : {no_negative or 'none'}")
    print(f"  undeclared reasons    : {undeclared or 'none'}")
    print(f"  specified_only        : {sorted(r.rid for r in core.REGISTRY.values() if not r.executable)} "
          f"(downgrade requires human decision D-19)")
    gate_fail = bool(never_called or no_negative or undeclared)
    print(f"  COVERAGE GATE: {'FAIL' if gate_fail else 'PASS'}")
    print("\nNOTE: these are contract self-test results against fixtures. They are NOT a trusted "
          "attestation of any real change. Only attest.py may certify, and it is not proved to run "
          "on an isolated CI runner (HL-18, D-16).")
    return 1 if (bad or gate_fail) else 0


if __name__ == "__main__":
    sys.exit(main())
