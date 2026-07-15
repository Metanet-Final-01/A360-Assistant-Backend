"""Phase 0 v1.5 harness rules. Every rule returns RuleResult(ok, detail, reason_code).

A failure MUST carry a typed reason so a negative case can assert it failed for the INTENDED
reason (v1.4 P0-7: the weakened-stage case actually failed on policy version/digest, never on
the stage comparison it claimed to prove).
"""
from __future__ import annotations

import ast
import datetime as dt
import fnmatch
import re
from pathlib import Path

from core import (ATTESTOR_ID, ATTESTOR_VERSION, Artifact, ArtifactStore, PolicyResolver,
                  RuleResult, StoreReadError, change_receipt_id,
                  TrustedContext, rule, specified_only)

INSTALL_RE = re.compile(r"\b(?:pip3?|uv\s+pip|poetry|conda|npm|pnpm|yarn)\s+(?:install|add)\b", re.I)


def _match_any(path: str, globs) -> bool:
    for g in globs:
        if fnmatch.fnmatch(path, g):
            return True
        base = str(g).rstrip("*").rstrip("/")
        if base and path.startswith(base + "/"):
            return True
    return False


# ---------------------------------------------------------------- projection (P0-3, P0-5)
def agent_public_api(repo: Path, iface_policy) -> tuple[frozenset, str]:
    """P1-1: the public Agent API comes from an owner-approved interface policy, not a
    hard-coded set. Returns (names, provenance)."""
    names = frozenset(iface_policy["public_names"])
    return names, f"{iface_policy['policy_id']}@{iface_policy['version']}"


def silent_install_hits(changed, file_reader, dep_policy) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """P1-2: returns (hits, unreadable). An unreadable in-scope file is INDETERMINATE, never clean."""
    hits, unreadable = [], []
    for p in changed:
        if _match_any(p, dep_policy.get("content_scan_paths", ())):
            try:
                txt = file_reader(p)
            except Exception as e:                       # v1.4 swallowed this and reported clean
                unreadable.append(f"{p} ({type(e).__name__})")
                continue
            if txt is None:
                unreadable.append(f"{p} (unreadable)")
                continue
            if INSTALL_RE.search(txt):
                hits.append(p)
    return tuple(sorted(hits)), tuple(sorted(unreadable))


def derive_projection(ctx: TrustedContext, dep_policy, app_policy, changed, file_reader) -> dict:
    """The complete independent fact projection. P0-3: applicability comes from a protected
    versioned applicability policy, not just a CH-04 special case."""
    hits, unreadable = silent_install_hits(changed, file_reader, dep_policy)
    dependency_changed = any(_match_any(p, dep_policy["dependency_paths"]) for p in changed) or bool(hits)
    touches_agent = any(_match_any(p, dep_policy["agent_owned_paths"]) for p in changed)
    risk = sorted({prof for prof, globs in dep_policy["risk_profile_paths"].items()
                   for p in changed if _match_any(p, globs)})
    if not risk:
        risk = ["none"]          # P1-3: explicit, schema-consistent
    applicable = set()
    for entry in app_policy["applicability"]:
        trig = entry["trigger"]
        fire = ((trig == "dependency_changed" and dependency_changed)
                or (trig == "agent_owned_paths_touched" and touches_agent)
                or (trig == "any_change" and bool(changed))
                or (trig == "risk_profile" and entry.get("risk_profile") in risk)
                or (trig == "path_glob" and any(_match_any(p, entry.get("paths", ())) for p in changed)))
        if fire:
            applicable.update(entry["controls"])
    return {
        "repository_id": ctx.repository_id,
        "diff_baseline_sha": ctx.merge_base,
        "diff_head_sha": ctx.event_head_sha,
        "changed_paths": sorted(changed),
        "dependency_changed": dependency_changed,
        "silent_install_hits": list(hits),
        "unreadable_scanned_paths": list(unreadable),
        "touches_agent_owned_paths": touches_agent,
        "risk_profiles": risk,
        "applicable_controls": sorted(applicable),
        "git_state": ctx.git_state,
    }



# ------------------------------------------------------- policy decision (v1.5 P0-2)
# v1.5 defect: HL-03 only failed when the report CLAIMED verdict=pass. An honest report saying
# verdict=fail with a failed required gate produced RuleResult(ok=True), and Attestor then
# returned attested=True. A CI caller using that Boolean would have merged the failed change.
#
# Evidence validity ("is this report internally consistent and bound to the real subject?") and
# the policy decision ("may this change proceed?") are now separate outputs.

def policy_decision(report_obj, derived: dict, waiver_store, registry_art, now,
                    enforcement_mode: str) -> tuple:
    """Return (decision, reasons). decision in {allow, deny, observe}.

    ANY applicable control that is failed, skipped, warned, uncovered or backed by an
    unresolved waiver yields a non-allow decision, regardless of what the report claims.
    """
    reasons = []
    required = set(derived["applicable_controls"])
    passing = {g["control_id"] for g in report_obj["gates"] if g["status"] == "pass"}
    subject = {"repository_id": report_obj["subject"]["repository_id"],
               "commit": report_obj["subject"]["commit"]}
    for cid in sorted(required):
        gates = [g for g in report_obj["gates"] if g["control_id"] == cid]
        if not gates:
            reasons.append(f"{cid}: no gate result (uncovered)")
            continue
        if len(gates) > 1:
            reasons.append(f"{cid}: {len(gates)} gate results")
            continue
        g = gates[0]
        if g["status"] == "pass":
            continue
        if g["status"] == "waived":
            err = _resolve_waiver(waiver_store, registry_art, g.get("waiver_id"), cid, subject,
                                  now, passing)
            if err:
                reasons.append(err)
            continue
        reasons.append(f"{cid}: status={g['status']}")
    if derived.get("unreadable_scanned_paths"):
        reasons.append(f"in-scope files unreadable: {derived['unreadable_scanned_paths']}")
    if derived.get("git_state") != "clean":
        reasons.append(f"git_state={derived.get('git_state')}: a dirty checkout cannot be allowed")
    if reasons:
        return "deny", tuple(reasons)
    # An unapproved applicability/severity policy cannot legitimately enforce; it may only observe.
    if enforcement_mode != "enforce":
        return "observe", (f"applicability policy state is not approved (enforcement_mode="
                           f"{enforcement_mode}); recording without enforcing",)
    return "allow", ()


# ------------------------------------------------------------------------------- HL-11
@rule("HL-11", "trusted-context",
      "manifest declarations must equal the projection derived from the TRUSTED event; a caller-"
      "selected commit pair or changed-path list is not certifiable",
      reasons=("context_not_authoritative", "declaration_mismatch", "scan_indeterminate"))
def hl11(ctx: TrustedContext, manifest_art: Artifact, derived: dict) -> RuleResult:
    if not ctx.authoritative:
        return RuleResult(False, f"context is {ctx.mode}: {ctx.downgrade_reason}", "context_not_authoritative")
    if derived["unreadable_scanned_paths"]:
        return RuleResult(False, f"in-scope files unreadable: {derived['unreadable_scanned_paths']}",
                          "scan_indeterminate")
    m = manifest_art.obj
    p = []
    if m["declared_repository_id"] != ctx.repository_id:
        p.append("declared repository_id != trusted repository id")
    if m["declared_baseline_sha"] != ctx.merge_base:
        p.append(f"declared baseline {m['declared_baseline_sha'][:8]} != trusted merge base {ctx.merge_base[:8]}")
    if m["declared_head_sha"] != ctx.event_head_sha:
        p.append(f"declared head {m['declared_head_sha'][:8]} != trusted event head {ctx.event_head_sha[:8]}")
    if sorted(m["declared_changed_paths"]) != derived["changed_paths"]:
        p.append(f"changed_paths declared={len(m['declared_changed_paths'])} derived={len(derived['changed_paths'])}")
    if m["declared_dependency_changed"] != derived["dependency_changed"]:
        p.append(f"dependency_changed declared={m['declared_dependency_changed']} derived={derived['dependency_changed']}")
    if m["declared_touches_agent_owned_paths"] != derived["touches_agent_owned_paths"]:
        p.append("touches_agent_owned_paths mismatch")
    if sorted(m["declared_risk_profiles"]) != sorted(derived["risk_profiles"]):
        p.append(f"risk_profiles declared={sorted(m['declared_risk_profiles'])} derived={derived['risk_profiles']}")
    # P0-3: EXACT equality, not merely "derived subset of declared".
    declared = sorted(m["declared_control_applicability"].get("controls_expected", []))
    if declared != derived["applicable_controls"]:
        p.append(f"applicable controls declared={declared} derived={derived['applicable_controls']}")
    return (RuleResult(True, f"declared == derived over {len(derived['changed_paths'])} trusted-diff paths")
            if not p else RuleResult(False, "; ".join(p), "declaration_mismatch"))


# ------------------------------------------------------------------------------- HL-12
@rule("HL-12", "read-once",
      "report bindings resolve against ONE read-once manifest artifact, the resolved policy/registry/"
      "schema digests, and the FULL independent projection",
      reasons=("manifest_binding_mismatch", "digest_unresolved", "derived_facts_mismatch",
               "context_not_authoritative"))
def hl12(ctx: TrustedContext, manifest_art: Artifact, report_art: Artifact, derived: dict,
         resolved_digests: dict) -> RuleResult:
    if not ctx.authoritative:
        return RuleResult(False, f"context is {ctx.mode}", "context_not_authoritative")
    r = report_art.obj
    mb, p = r["manifest_binding"], []
    if mb["change_manifest_digest"] != manifest_art.digest:
        p.append("change_manifest_digest != digest of the read-once manifest artifact")
    if mb["manifest_repository_id"] != manifest_art.obj["declared_repository_id"]:
        p.append("manifest_binding repository_id != manifest content")
    if mb["manifest_baseline_sha"] != manifest_art.obj["declared_baseline_sha"]:
        p.append("manifest_binding baseline != manifest content")
    if mb["manifest_head_sha"] != manifest_art.obj["declared_head_sha"]:
        p.append("manifest_binding head != manifest content")
    if p:
        return RuleResult(False, "; ".join(p), "manifest_binding_mismatch")
    # P0-5: every digest the report cites must equal a digest we resolved ourselves.
    for field, key in (("control_registry_digest", "registry"), ("schema_digest", "schemas")):
        if r[field] != resolved_digests[key]:
            p.append(f"{field} reported={r[field][:20]}… resolved={resolved_digests[key][:20]}…")
    if r["derived_facts"]["dependency_path_policy_digest"] != resolved_digests["dependency-path-policy"]:
        p.append("dependency_path_policy_digest != resolved policy bytes")
    if r["derived_facts"].get("applicability_policy_digest") != resolved_digests["applicability-policy"]:
        p.append("applicability_policy_digest != resolved policy bytes")
    if p:
        return RuleResult(False, "; ".join(p), "digest_unresolved")
    # trusted context embedded in the report must equal the runner's own context
    tc = r["trusted_context"]
    for k, v in (("mode", ctx.mode), ("repository_id", ctx.repository_id), ("event_head_sha", ctx.event_head_sha),
                 ("checkout_sha", ctx.checkout_sha), ("merge_base", ctx.merge_base), ("git_state", ctx.git_state)):
        if tc.get(k) != v:
            p.append(f"trusted_context.{k} reported={tc.get(k)!r} runner={v!r}")
    if r["subject"]["repository_id"] != ctx.repository_id:
        p.append("report repository_id != trusted repository id")
    if r["subject"]["commit"] != ctx.event_head_sha:
        p.append("report commit != trusted event head")
    if r["run"]["git_state"] != ctx.git_state:
        p.append(f"report git_state={r['run']['git_state']} != runner-derived {ctx.git_state}")
    # P0-5: the COMPLETE projection, including the two fields v1.4 silently omitted.
    rf = r["derived_facts"]
    for k in ("repository_id", "diff_baseline_sha", "diff_head_sha", "changed_paths", "dependency_changed",
              "silent_install_hits", "touches_agent_owned_paths", "risk_profiles", "applicable_controls",
              "git_state"):
        if list(rf.get(k)) != list(derived[k]) if isinstance(derived[k], list) else rf.get(k) != derived[k]:
            p.append(f"derived_facts.{k} reported={rf.get(k)!r} independent={derived[k]!r}")
    return (RuleResult(True, "report bound to read-once manifest, resolved digests and full projection")
            if not p else RuleResult(False, "; ".join(p), "derived_facts_mismatch"))


# ------------------------------------------------------------------------------- HL-01/02
@rule("HL-01", "cross-artifact", "registry control ids are unique", reasons=("duplicate_control_id",))
def hl01(registry_art: Artifact) -> RuleResult:
    ids = [c["id"] for c in registry_art.obj["controls"]]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    return (RuleResult(True, f"{len(ids)} unique control ids") if not dupes
            else RuleResult(False, f"duplicate ids {dupes}", "duplicate_control_id"))


@rule("HL-02", "cross-artifact", "every referenced control_id is a registry member",
      reasons=("orphan_control_id",))
def hl02(registry_art: Artifact, referenced) -> RuleResult:
    members = {c["id"] for c in registry_art.obj["controls"]}
    orphans = sorted(set(referenced) - members)
    return (RuleResult(True, f"all {len(set(referenced))} references are registry members") if not orphans
            else RuleResult(False, f"orphan control ids {orphans}", "orphan_control_id"))


# ------------------------------------------------------------------- waiver resolution (P0-4)
def _resolve_waiver(store: ArtifactStore, registry_art: Artifact, waiver_id, control_id, subject, now,
                    passing_controls) -> str | None:
    art = store.waiver(waiver_id)
    if art is None:
        return f"{control_id}: waiver {waiver_id} does not resolve to a waiver artifact"
    w = art.obj
    if w["control_id"] != control_id:
        return f"{control_id}: waiver {waiver_id} covers {w['control_id']}"
    if w["status"] != "active":
        return f"{control_id}: waiver {waiver_id} status={w['status']}"
    if dt.datetime.fromisoformat(w["expires_at"].replace("Z", "+00:00")) < now:
        return f"{control_id}: waiver {waiver_id} expired {w['expires_at']}"
    if dt.datetime.fromisoformat(w["approved_at"].replace("Z", "+00:00")) > now:
        return f"{control_id}: waiver {waiver_id} approved in the future"
    max_days = 90
    life = (dt.datetime.fromisoformat(w["expires_at"].replace("Z", "+00:00"))
            - dt.datetime.fromisoformat(w["approved_at"].replace("Z", "+00:00"))).days
    if life > max_days:
        return f"{control_id}: waiver {waiver_id} lifetime {life}d exceeds {max_days}d"
    if w["subject"]["repository_id"] != subject["repository_id"] or w["subject"]["commit"] != subject["commit"]:
        return f"{control_id}: waiver {waiver_id} subject does not match this change"
    comp = w.get("compensating_control")
    members = {c["id"] for c in registry_art.obj["controls"]}
    if comp not in members:
        return f"{control_id}: compensating control {comp} is not a registry member"
    if comp not in passing_controls:
        return f"{control_id}: compensating control {comp} has no passing evidence"
    return None


# ------------------------------------------------------------------------------- HL-03
@rule("HL-03", "cross-artifact",
      "every applicable control has exactly one gate; absent/skipped/fail cannot pass; unrelated gates "
      "do not count as coverage; waived requires a resolved waiver artifact",
      reasons=("gate_uncovered", "gate_not_passing", "waiver_unresolved", "applicability_divergence"))
def hl03(registry_art: Artifact, report_art: Artifact, derived: dict, waiver_store: ArtifactStore,
         now) -> RuleResult:
    r = report_art.obj
    required = set(derived["applicable_controls"])
    # P0-3: the report's own applicable set must equal the independently derived set.
    if sorted(r["derived_facts"]["applicable_controls"]) != sorted(required):
        return RuleResult(False,
                          f"report applicable={sorted(r['derived_facts']['applicable_controls'])} "
                          f"derived={sorted(required)}", "applicability_divergence")
    passing = {g["control_id"] for g in r["gates"] if g["status"] == "pass"}
    subject = {"repository_id": r["subject"]["repository_id"], "commit": r["subject"]["commit"]}
    p = []
    for cid in sorted(required):
        gates = [g for g in r["gates"] if g["control_id"] == cid]
        if not gates:
            p.append(f"{cid}: NO gate result (uncovered)")
            continue
        if len(gates) > 1:
            p.append(f"{cid}: {len(gates)} gate results (must be exactly 1)")
            continue
        g = gates[0]
        if g["status"] in ("fail", "skipped"):
            p.append(f"{cid}: status={g['status']} cannot pass")
        elif g["status"] == "waived":
            err = _resolve_waiver(waiver_store, registry_art, g.get("waiver_id"), cid, subject, now, passing)
            if err:
                return RuleResult(False, err, "waiver_unresolved")
    if r["verdict"] == "pass" and p:
        code = "gate_uncovered" if any("uncovered" in x for x in p) else "gate_not_passing"
        return RuleResult(False, "; ".join(p), code)
    return RuleResult(True, f"required={sorted(required) or '[]'} exactly covered; verdict={r['verdict']}")


# ------------------------------------------------------------------------------- HL-07
@rule("HL-07", "cross-artifact",
      "a change touching app/agent/** requires declaration AND a resolved, digest-bound, subject-scoped "
      "Agent-owner approval artifact",
      reasons=("agent_change_undeclared", "approval_unresolved"))
def hl07(manifest_art: Artifact, derived: dict, store: ArtifactStore, subject, now) -> RuleResult:
    m = manifest_art.obj
    if not derived["touches_agent_owned_paths"]:
        return RuleResult(True, "no app/agent/** path touched")
    if not m.get("declared_touches_agent_owned_paths"):
        return RuleResult(False, "diff touches app/agent/** but declared_touches_agent_owned_paths=false",
                          "agent_change_undeclared")
    ref = m.get("agent_owner_approval_ref")
    if not ref:
        return RuleResult(False, "app/agent/** touched without agent_owner_approval_ref", "approval_unresolved")
    art = store.approval(ref)
    if art is None:
        return RuleResult(False, f"approval {ref} does not resolve to an approval artifact", "approval_unresolved")
    a = art.obj
    p = []
    if a["status"] != "approved":
        p.append(f"approval {ref} status={a['status']}")
    if a["approver_role"] != "agent-maintainer":
        p.append(f"approval {ref} approver_role={a['approver_role']}")
    if a["subject"]["repository_id"] != subject["repository_id"] or a["subject"]["commit"] != subject["commit"]:
        p.append(f"approval {ref} subject is not this repository/commit")
    if dt.datetime.fromisoformat(a["expires_at"].replace("Z", "+00:00")) < now:
        p.append(f"approval {ref} expired {a['expires_at']}")
    touched = [x for x in derived["changed_paths"] if _match_any(x, ["app/agent/**"])]
    covered = set(a["approved_paths"])
    uncovered = [x for x in touched if x not in covered]
    if uncovered:
        p.append(f"approval {ref} does not cover {uncovered}")
    return (RuleResult(True, f"approval {ref} resolved and scoped to {len(touched)} path(s)") if not p
            else RuleResult(False, "; ".join(p), "approval_unresolved"))


# ------------------------------------------------------------------------------- HL-14
@rule("HL-14", "policy-resolution",
      "required controls and FULL stage definitions equal the approved operation policy resolved from "
      "protected bytes",
      reasons=("policy_unresolvable", "policy_mismatch", "policy_unapproved",
               "policy_no_approval_evidence", "operation_unsupported", "projection_mismatch"))
def hl14(resolver: PolicyResolver, bundle_art: Artifact) -> RuleResult:
    b = bundle_art.obj
    ok, detail, reason = resolver.require_approved("operation-policy", b["operation_policy"])
    if not ok:
        return RuleResult(False, detail, reason)
    pol = resolver.resolve("operation-policy").obj
    op = b["operation"]
    if op in pol.get("unsupported_operations", ()):
        return RuleResult(False, f"operation {op} is explicitly unsupported by the approved policy",
                          "operation_unsupported")
    spec = pol["operations"].get(op)
    if spec is None:
        return RuleResult(False, f"operation {op} absent from the approved policy", "operation_unsupported")
    p = []
    if sorted(spec["required_controls"]) != sorted(b["required_controls"]):
        p.append(f"required_controls policy={sorted(spec['required_controls'])} bundle={sorted(b['required_controls'])}")
    pol_stages = {s["stage_id"]: s for s in spec["stages"]}
    bun_stages = {s["stage_id"]: s for s in b["completeness"]["expected"]}
    if set(pol_stages) != set(bun_stages):
        p.append(f"stage ids policy={sorted(pol_stages)} bundle={sorted(bun_stages)}")
    for sid, ps in pol_stages.items():
        bs = bun_stages.get(sid)
        if bs is None:
            continue
        for k in ("event_kind", "min_count", "max_count", "source_table", "correlation_key",
                  "window_seconds", "condition"):
            if ps.get(k) != bs.get(k):
                p.append(f"stage {sid}.{k} policy={ps.get(k)!r} bundle={bs.get(k)!r}")
    for req in spec.get("subject_requirements", ()):
        if req not in b["subject"]:
            p.append(f"subject missing policy-required field {req}")
    if b["assurance_verdict"] == "assured":
        seen = [c["control_id"] for c in b["controls"]]
        for cid in spec["required_controls"]:
            if seen.count(cid) != 1:
                p.append(f"required {cid} appears {seen.count(cid)}x")
    return (RuleResult(True, f"policy projection bound ({len(spec['required_controls'])} controls, "
                             f"{len(pol_stages)} stages)")
            if not p else RuleResult(False, "; ".join(p), "projection_mismatch"))


# ------------------------------------------------------------------------------- HL-13
@rule("HL-13", "trusted-store",
      "observations come from the runner-bound store; the rule itself validates every returned row "
      "(event kind, subject, window, revision) and recomputes both digests from canonical values",
      reasons=("store_read_failed", "observation_mismatch", "unknown_stage_claim",
               "duplicate_stage_claim", "derivation_unbound", "completeness_mismatch",
               "query_evidence_unbound", "unsupported_condition"))
def hl13(resolver: PolicyResolver, bundle_art: Artifact, store, now,
         clock_skew_seconds: int = 5) -> RuleResult:
    """v1.6 review item 3. Codex reproduced two holes:
      * QueryEvidence.queried_at was never compared to the bundle anchor, and
      * returned rows outside the requested subject/window were counted, because the STORE computed
        result_set_digest over whatever it chose to return.
    v1.7: the rule validates each row itself and computes both digests from canonical values via
    trusted library code, never via a store method.
    """
    from store import query_digest as _qd, result_set_digest as _rsd

    b = bundle_art.obj
    c = b["completeness"]
    pol = resolver.resolve("operation-policy").obj["operations"].get(b["operation"])
    if pol is None:
        return RuleResult(False, f"operation {b['operation']} unsupported", "observation_mismatch")
    claimed_ids = [o["stage_id"] for o in c["observed"]]
    if len(claimed_ids) != len(set(claimed_ids)):
        return RuleResult(False, f"duplicate observed stage ids {claimed_ids}", "duplicate_stage_claim")
    policy_ids = {s["stage_id"] for s in pol["stages"]}
    unknown = sorted(set(claimed_ids) - policy_ids)
    if unknown:
        return RuleResult(False, f"observed stages not in the approved policy: {unknown}",
                          "unknown_stage_claim")
    anchor = dt.datetime.fromisoformat(c["derivation"]["queried_at"].replace("Z", "+00:00"))
    if abs((anchor - now).total_seconds()) > clock_skew_seconds:
        return RuleResult(False, f"derivation queried_at {c['derivation']['queried_at']} is outside the "
                                 f"{clock_skew_seconds}s clock-skew allowance", "derivation_unbound")
    derived_missing = []
    for st in pol["stages"]:
        if st["condition"] != "always":
            return RuleResult(False, f"stage {st['stage_id']}: condition {st['condition']!r} has no "
                                     f"typed evaluator", "unsupported_condition")
        subject_value = b["subject"].get(st["correlation_key"])
        if subject_value is None:
            return RuleResult(False, f"stage {st['stage_id']}: subject has no {st['correlation_key']}",
                              "observation_mismatch")
        window_end = anchor
        window_start = anchor - dt.timedelta(seconds=st["window_seconds"])
        try:
            rows, ev = store.query(st["source_table"], st["correlation_key"], str(subject_value),
                                   window_start, window_end)
        except StoreReadError as e:
            return RuleResult(False, f"stage {st['stage_id']}: {e}", "store_read_failed")
        want = {"source_table": st["source_table"], "subject_key": st["correlation_key"],
                "subject_value": str(subject_value), "window_start": window_start,
                "window_end": window_end}
        for k, v in want.items():
            if getattr(ev, k) != v:
                return RuleResult(False, f"stage {st['stage_id']}: query evidence {k}={getattr(ev, k)!r} "
                                         f"!= requested {v!r}", "query_evidence_unbound")
        # v1.6 item 3: the evidence's own timestamp must match the anchor the bundle claims.
        if abs((ev.queried_at - anchor).total_seconds()) > clock_skew_seconds:
            return RuleResult(False, f"stage {st['stage_id']}: query evidence queried_at {ev.queried_at} "
                                     f"is not within {clock_skew_seconds}s of the bundle anchor {anchor}",
                              "query_evidence_unbound")
        if ev.row_count != len(rows):
            return RuleResult(False, f"stage {st['stage_id']}: evidence row_count={ev.row_count} != "
                                     f"returned {len(rows)}", "query_evidence_unbound")
        # v1.6 item 3: digests are computed by TRUSTED library code from canonical values, never by
        # asking the store to grade its own homework.
        if ev.query_digest != _qd(st["source_table"], st["correlation_key"], str(subject_value),
                                  window_start, window_end):
            return RuleResult(False, f"stage {st['stage_id']}: query_digest does not bind this request",
                              "query_evidence_unbound")
        if ev.result_set_digest != _rsd(rows):
            return RuleResult(False, f"stage {st['stage_id']}: result_set_digest does not bind the "
                                     f"returned rows", "query_evidence_unbound")
        if c["derivation"]["source_revision"] != ev.source_revision:
            return RuleResult(False, f"derivation source_revision={c['derivation']['source_revision']} "
                                     f"!= store {ev.source_revision}", "derivation_unbound")
        # v1.6 item 3: validate EVERY row here. A mixed result set is denied, never silently filtered.
        for r in rows:
            if r.subject_key != st["correlation_key"] or r.subject_value != str(subject_value):
                return RuleResult(False, f"stage {st['stage_id']}: returned row subject "
                                         f"{r.subject_key}={r.subject_value!r} is not the requested "
                                         f"{st['correlation_key']}={subject_value!r}",
                                  "query_evidence_unbound")
            if not (window_start <= r.occurred_at <= window_end):
                return RuleResult(False, f"stage {st['stage_id']}: returned row occurred_at "
                                         f"{r.occurred_at} is outside the requested window "
                                         f"[{window_start}, {window_end}]", "query_evidence_unbound")
            if r.source_revision != ev.source_revision:
                return RuleResult(False, f"stage {st['stage_id']}: returned row source_revision "
                                         f"{r.source_revision!r} != evidence {ev.source_revision!r}",
                                  "query_evidence_unbound")
            if r.event_kind != st["event_kind"]:
                return RuleResult(False, f"stage {st['stage_id']}: returned row event_kind "
                                         f"{r.event_kind!r} != policy {st['event_kind']!r}",
                                  "query_evidence_unbound")
        n = len(rows)
        if n < st["min_count"] or (st["max_count"] is not None and n > st["max_count"]):
            derived_missing.append(st["stage_id"])
        claimed = next((o["count"] for o in c["observed"] if o["stage_id"] == st["stage_id"]), None)
        if claimed is not None and claimed != n:
            return RuleResult(False, f"stage {st['stage_id']}: bundle claims {claimed}, store has {n}",
                              "observation_mismatch")
    if {x["stage_id"] for x in c["missing"]} != set(derived_missing):
        return RuleResult(False, f"missing bundle={sorted(x['stage_id'] for x in c['missing'])} "
                                 f"store-derived={sorted(derived_missing)}", "completeness_mismatch")
    if c["complete"] != (not derived_missing):
        return RuleResult(False, f"complete={c['complete']} but store-derived missing="
                                 f"{sorted(derived_missing)}", "completeness_mismatch")
    return RuleResult(True, f"{len(pol['stages'])} stages reconciled; every row validated by the rule")


@rule("HL-20", "cross-artifact",
      "an assured Output must bind a producer-resolved Agent version that was OBSERVED at the public "
      "contract and that an approved agent-version policy lists as approved",
      reasons=("agent_provenance_missing", "agent_version_policy_digest_mismatch",
               "agent_version_policy_unapproved", "agent_version_not_observable",
               "agent_version_unapproved", "agent_version_requested_mismatch",
               "public_boundary_unobserved", "agent_registry_snapshot_unbound",
               "public_contract_version_unbound"))
def hl20(resolver, runtime_art, resolved_digests: dict, observer=None) -> RuleResult:
    """v1.8 defect (Codex): `agent-version-policy.yaml` shipped, and `runtime.agent_provenance`
    carried version fields, but NO rule read either. Codex independently drove an unapproved `v404`
    with a fabricated policy digest to Output `allow`. A policy nothing resolves is a document, not
    an authority - the exact failure this whole harness exists to catch.

    Fail-closed by design. At `d66fce1` the public done-data carries no version, so a truthful
    report says `producer_resolved_version=null` / `not_observable_pending_D21` and CANNOT be
    assured. That is the correct answer today, not a bug: see D-21 (make it observable) and D-22
    (approve versions). Until then the boundary may observe and record, never assure.
    """
    if runtime_art is None:
        return RuleResult(False, "assured output without runtime-validation evidence to carry agent "
                                 "provenance", "agent_provenance_missing")
    prov = runtime_art.obj.get("agent_provenance")
    if not prov:
        return RuleResult(False, "runtime evidence carries no agent_provenance block",
                          "agent_provenance_missing")

    policy = resolver.resolve("agent-version-policy")
    claimed = prov.get("agent_version_policy_digest")
    if claimed != policy.digest:
        return RuleResult(False, f"agent_version_policy_digest {claimed} is not the digest of the "
                                 f"resolved policy ({policy.digest}): the version claim is not bound "
                                 f"to any authority this attestor resolved",
                          "agent_version_policy_digest_mismatch")
    if resolved_digests.get("agent-version-policy") != policy.digest:
        return RuleResult(False, "agent-version-policy is not in the resolved authority chain",
                          "agent_version_policy_digest_mismatch")
    if policy.obj.get("state") != "approved":
        return RuleResult(False, f"agent-version-policy state={policy.obj.get('state')!r}: an "
                                 f"unapproved policy cannot authorise an assured version (D-22)",
                          "agent_version_policy_unapproved")

    # v1.9 defect (Codex): `agent_registry_snapshot_digest` and `public_contract_version` were in the
    # runtime document and in the schema, and NOTHING read them. Codex forged the registry digest,
    # relinked the runtime digest, and reached allow. A field nobody verifies is decoration - the
    # same failure as an unresolved policy, one layer down. v1.10 makes a runner-bound adapter
    # OBSERVE the public boundary and compares. The shipped adapter is NotConfigured and fails
    # closed, which is the truthful state at d66fce1.
    if observer is None:
        return RuleResult(False, "no runner-bound public-boundary observer: registry/contract "
                                 "provenance cannot be verified", "public_boundary_unobserved")
    try:
        obs = observer.observe()
    except StoreReadError as e:
        return RuleResult(False, f"public boundary unobservable: {e}", "public_boundary_unobserved")
    if prov.get("agent_registry_snapshot_digest") != obs.registry_snapshot_digest:
        return RuleResult(False, f"runtime agent_registry_snapshot_digest "
                                 f"{prov.get('agent_registry_snapshot_digest')} != observed "
                                 f"{obs.registry_snapshot_digest}", "agent_registry_snapshot_unbound")
    if prov.get("public_contract_version") != obs.public_contract_version:
        return RuleResult(False, f"runtime public_contract_version "
                                 f"{prov.get('public_contract_version')!r} != observed "
                                 f"{obs.public_contract_version!r}", "public_contract_version_unbound")

    resolved = prov.get("producer_resolved_version")
    observability = prov.get("resolved_version_observability")
    # The claim must match what the adapter actually saw, not merely assert itself (D-21).
    if resolved != obs.resolved_version or observability != obs.observability:
        return RuleResult(False, f"runtime claims producer_resolved_version={resolved!r}/"
                                 f"{observability!r} but the observer saw "
                                 f"{obs.resolved_version!r}/{obs.observability!r}: a self-reported "
                                 f"observation is not an observation (D-21)",
                          "agent_version_not_observable")
    if resolved is None or observability != "observed_from_public_contract":
        return RuleResult(False, f"producer_resolved_version={resolved!r} "
                                 f"observability={observability!r}: the version the producer ACTUALLY "
                                 f"ran was not observed at the public contract, so an assured verdict "
                                 f"would be a guess (D-21)", "agent_version_not_observable")
    approved = tuple(policy.obj.get("approved_versions") or ())
    if resolved not in approved:
        return RuleResult(False, f"producer_resolved_version={resolved!r} is not in approved_versions"
                                 f"={list(approved)!r}: discoverable is not approved (D-22)",
                          "agent_version_unapproved")
    requested = prov.get("requested_agent_version")
    if requested is not None and requested != resolved:
        return RuleResult(False, f"requested_agent_version={requested!r} but producer resolved "
                                 f"{resolved!r}: the boundary must not record a version the producer "
                                 f"did not run", "agent_version_requested_mismatch")
    return RuleResult(True, f"observed producer version {resolved!r} is approved by "
                            f"{policy.obj['policy_id']}@{policy.obj['version']}")


@rule("HL-19", "cross-artifact",
      "an assured Output must bind to a resolved Change attestation RECEIPT that allowed the same "
      "repository/commit, and to the report that receipt covers",
      reasons=("receipt_unresolved", "receipt_not_allow", "receipt_subject_mismatch",
               "receipt_digest_mismatch", "report_internally_inconsistent",
               "receipt_content_address_mismatch", "receipt_attestor_unknown",
               "receipt_policy_digest_mismatch", "receipt_policy_set_mismatch"))
def hl19(bundle_art: Artifact, receipt, report_art, manifest_digest, resolved_digests: dict) -> RuleResult:
    """v1.6 review item 1: a digest proves a report exists, not that the Change was allowed."""
    b = bundle_art.obj
    if receipt is None:
        return RuleResult(False, "assured output without a resolved change-attestation receipt",
                          "receipt_unresolved")
    r = receipt.obj
    # v1.8 defect (Codex): the store validated the schema and that key == payload id, but nobody
    # RECOMPUTED the claimed content address or checked who issued the receipt. A schema-valid
    # forged receipt (arbitrary id, attestor "not-the-phase0-attestor" v99.0) therefore authorised
    # Output. The id is a content address, so the trusted rule can recompute it from the receipt's
    # own bound fields and refuse anything that does not hash to what it claims.
    # HONEST SCOPE: this detects an INCONSISTENT receipt. It is NOT origin authentication - a writer
    # that can compute a correct content address still produces an acceptable receipt. Only a signed
    # receipt or a protected writer closes that, and both are D-16.
    attestor = r.get("attestor") or {}
    if attestor.get("id") != ATTESTOR_ID or attestor.get("version") != ATTESTOR_VERSION:
        return RuleResult(False, f"receipt attestor={attestor.get('id')!r}/"
                                 f"{attestor.get('version')!r} is not this attestor "
                                 f"({ATTESTOR_ID}/{ATTESTOR_VERSION})", "receipt_attestor_unknown")
    # v1.9 defect (Codex): v1.9 compared only the keys the receipt HAPPENED to carry, and the schema
    # asked for `minProperties: 1`. Codex dropped `agent-version-policy` from a genuine allow receipt,
    # recomputed the (now smaller) content address correctly, and reached allow - a receipt could
    # simply omit the authority it did not want to be bound by. The key SET must match exactly, and
    # be checked BEFORE the values.
    expected_keys = {k for k in resolved_digests if k.endswith("-policy")}
    got_keys = set(r["policy_digests"])
    if got_keys != expected_keys:
        missing = sorted(expected_keys - got_keys)
        unknown = sorted(got_keys - expected_keys)
        return RuleResult(False, f"receipt policy authority set != the set this attestor resolved: "
                                 f"missing={missing} unknown={unknown}", "receipt_policy_set_mismatch")

    rebuilt_authority = dict(r["policy_digests"])
    rebuilt_authority["registry"] = r["control_registry_digest"]
    rebuilt_authority["schemas"] = r["schema_digest"]
    recomputed = change_receipt_id(r["repository_id"], r["commit"], r["assurance_report_digest"],
                                   r["change_manifest_digest"], rebuilt_authority,
                                   attestor["version"], r["decision"], r["evidence_valid"])
    if recomputed != receipt.receipt_id:
        return RuleResult(False, f"receipt id {receipt.receipt_id} is not the content address of its "
                                 f"own bound fields (recomputed {recomputed})",
                          "receipt_content_address_mismatch")
    for k, v in r["policy_digests"].items():
        if resolved_digests.get(k) != v:
            return RuleResult(False, f"receipt policy_digests[{k}] != currently resolved authority",
                              "receipt_policy_digest_mismatch")
    if not (r["decision"] == "allow" and r["evidence_valid"] is True):
        return RuleResult(False, f"receipt {receipt.receipt_id} decision={r['decision']} "
                                 f"evidence_valid={r['evidence_valid']}: only an allowed change may "
                                 f"support an assured output", "receipt_not_allow")
    if report_art is None:
        return RuleResult(False, "assured output without a resolved assurance report",
                          "receipt_unresolved")
    rep = report_art.obj
    # the receipt must cover THIS report and THIS manifest
    if r["assurance_report_digest"] != report_art.digest:
        return RuleResult(False, "receipt assurance_report_digest != digest of the resolved report",
                          "receipt_digest_mismatch")
    if r["change_manifest_digest"] != manifest_digest:
        return RuleResult(False, "receipt change_manifest_digest != resolved manifest digest",
                          "receipt_digest_mismatch")
    for k in ("control_registry_digest", "schema_digest"):
        if r[k] != resolved_digests[{"control_registry_digest": "registry",
                                     "schema_digest": "schemas"}[k]]:
            return RuleResult(False, f"receipt {k} != resolved authority", "receipt_digest_mismatch")
    # the report must be internally consistent about its own subject
    if rep["manifest_binding"]["manifest_head_sha"] != rep["subject"]["commit"] or \
       rep["manifest_binding"]["manifest_repository_id"] != rep["subject"]["repository_id"]:
        return RuleResult(False, "report subject does not match its own manifest_binding",
                          "report_internally_inconsistent")
    # receipt, report and output must describe ONE repository/commit
    subj = b["subject"]
    for label, a, bb in (("repository_id", r["repository_id"], rep["subject"]["repository_id"]),
                         ("commit", r["commit"], rep["subject"]["commit"])):
        if a != bb:
            return RuleResult(False, f"receipt {label}={a!r} != report {label}={bb!r}",
                              "receipt_subject_mismatch")
    if subj.get("repository_id") is not None and subj["repository_id"] != r["repository_id"]:
        return RuleResult(False, f"output repository_id={subj['repository_id']!r} != receipt "
                                 f"{r['repository_id']!r}", "receipt_subject_mismatch")
    if subj.get("commit") is not None and subj["commit"] != r["commit"]:
        return RuleResult(False, f"output commit={subj['commit']!r} != receipt {r['commit']!r}",
                          "receipt_subject_mismatch")
    return RuleResult(True, f"receipt {receipt.receipt_id} allowed {r['repository_id']}@"
                            f"{r['commit'][:8]}; report and output bound to the same subject")


# ------------------------------------------------------------------------------- HL-15
@rule("HL-15", "cross-artifact",
      "candidate assurance resolves the runtime report, boundary row and every linked digest, and matches "
      "the full subject key",
      reasons=("runtime_unresolved", "digest_mismatch", "subject_mismatch", "runtime_not_validated",
               "boundary_unresolved", "waiver_unresolved"))
def hl15(registry_art: Artifact, bundle_art: Artifact, runtime_art, boundary_record,
         resolved_digests: dict, waiver_store: ArtifactStore, now) -> RuleResult:
    b = bundle_art.obj
    p = []
    for field, key in (("control_registry_digest", "registry"), ("schema_digest", "schemas")):
        if b[field] != resolved_digests[key]:
            p.append(f"{field} does not match resolved authority")
    if b["completeness"]["policy_digest"] != resolved_digests["operation-policy"]:
        p.append("completeness.policy_digest does not match the resolved operation policy bytes")
    if b["operation_policy"]["digest"] != resolved_digests["operation-policy"]:
        p.append("operation_policy.digest does not match the resolved operation policy bytes")
    if b.get("linked_reports", {}).get("assurance_report_digest") not in (None, resolved_digests.get("assurance_report")):
        p.append("assurance_report_digest does not match the resolved report artifact")
    if p:
        return RuleResult(False, "; ".join(p), "digest_mismatch")
    if b["operation"] == "candidate_validation":
        if runtime_art is None:
            return RuleResult(False, "candidate assurance with no resolvable runtime-validation artifact",
                              "runtime_unresolved")
        rv = runtime_art.obj
        if b["linked_reports"].get("runtime_validation_digest") != runtime_art.digest:
            return RuleResult(False, "linked runtime_validation_digest != digest of the resolved artifact",
                              "digest_mismatch")
        sub, de = b["subject"], b["dual_evidence"]
        for k, x, y in (("candidate_id", sub.get("candidate_id"), rv["identity"]["candidate_id"]),
                        ("request_id", sub.get("request_id"), rv["identity"]["request_id"]),
                        ("session_id", sub.get("session_id"), rv["identity"]["session_id"]),
                        ("payload_digest", sub.get("candidate_payload_digest"),
                         rv["binding"]["candidate_payload_digest"]),
                        ("bound_candidate_id", de.get("bound_candidate_id"), rv["identity"]["candidate_id"]),
                        ("bound_request_id", de.get("bound_request_id"), rv["identity"]["request_id"]),
                        ("bound_payload", de.get("bound_candidate_payload_digest"),
                         rv["binding"]["candidate_payload_digest"]),
                        ("recommendation_version", sub.get("recommendation_version"),
                         rv["business_outcome"].get("recommendation_version"))):
            if x != y:
                p.append(f"{k}: bundle={x!r} runtime={y!r}")
        if p:
            return RuleResult(False, "; ".join(p), "subject_mismatch")
        if b["assurance_verdict"] == "assured":
            if rv["assurance_status"] != "validated":
                return RuleResult(False, f"assured bundle linked to runtime assurance_status="
                                         f"{rv['assurance_status']}", "runtime_not_validated")
            if boundary_record is None:
                return RuleResult(False, "assured candidate without a resolvable authoritative boundary "
                                         "record", "boundary_unresolved")
            # v1.5 P0-4: the digest comes from the runner-owned adapter that hashed the record's
            # bytes, not from a "digest" key inside a caller-shaped dictionary.
            if de["boundary_enforcement_digest"] != boundary_record.digest:
                return RuleResult(False, "boundary_enforcement_digest != digest of the record resolved "
                                         "by the runner-owned adapter", "digest_mismatch")
            row = boundary_record.obj
            for k in ("candidate_id", "candidate_payload_digest", "boundary_schema_digest",
                      "catalog_snapshot_digest", "boundary_validator_version"):
                want = rv["identity"].get(k) or rv["binding"].get(k)
                if row.get(k) != want:
                    p.append(f"boundary record {k} != runtime {k}")
            if p:
                return RuleResult(False, "; ".join(p), "subject_mismatch")
    subject = {"repository_id": b["subject"].get("repository_id", ""), "commit": b["subject"].get("commit", "")}
    passing = {c["control_id"] for c in b["controls"] if c["status"] == "pass"}
    for c in b["controls"]:
        if c["status"] == "waived":
            err = _resolve_waiver(waiver_store, registry_art, c.get("waiver_id"), c["control_id"],
                                  subject, now, passing)
            if err:
                return RuleResult(False, err, "waiver_unresolved")
    return RuleResult(True, "full evidence chain resolved and subject-bound")


# ------------------------------------------------------------------------------- HL-16
@rule("HL-16", "policy-resolution",
      "blocking flags and any blocking effect resolve to the approved severity policy",
      reasons=("blocking_policy_unapproved", "blocking_flag_mismatch"))
def hl16(resolver: PolicyResolver, runtime_art: Artifact) -> RuleResult:
    rv = runtime_art.obj
    claim, enf = rv["blocking_policy"], rv["enforcement"]
    ok, detail, _ = resolver.require_approved("severity-policy", claim)
    if enf["effect"] == "blocked" and not ok:
        return RuleResult(False, f"blocking effect under a non-approved policy: {detail}",
                          "blocking_policy_unapproved")
    if enf["mode"] == "enforce" and not ok:
        return RuleResult(False, f"enforce mode under a non-approved policy: {detail}",
                          "blocking_policy_unapproved")
    pol = resolver.resolve("severity-policy").obj
    active = pol["blocking_severities"] if pol.get("state") == "approved" else claim["blocking_severities"]
    for v in rv["violations"]:
        if v["blocking"] != (v["severity"] in active):
            return RuleResult(False, f"{v['violation_id']}: blocking={v['blocking']} but policy says "
                                     f"{v['severity'] in active} for {v['severity']}", "blocking_flag_mismatch")
    return RuleResult(True, f"mode={enf['mode']} effect={enf['effect']} consistent with policy state")


# ------------------------------------------------------------------------------- HL-06
@rule("HL-06", "static-source",
      "outside app/agent/** only owner-approved public names may be imported from app.agent; package "
      "aliases, private submodules, dynamic imports and indeterminate sources are rejected",
      reasons=("private_agent_import", "source_indeterminate"))
def hl06(reader, public_names: frozenset, extra_sources: dict = None) -> RuleResult:
    """v1.5 P1-1: `from app import agent as x` + `getattr(x, "verify")` was accepted, and parse
    failures were treated as clean. v1.6 uses a closed rule: any binding of the `app.agent` package
    itself is rejected, so no alias can exist to call getattr on. Sources are read from the VERIFIED
    Git object; an unreadable or unparsable in-scope file is indeterminate, never clean.
    """
    bad: list = []
    indeterminate: list = []

    def scan(rel: str, src: str):
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            indeterminate.append(f"{rel} (unparsable: {e.msg})")
            return
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "app.agent":
                    for a in node.names:
                        if a.name not in public_names:
                            bad.append(f"{rel}: from app.agent import {a.name} (private submodule)")
                elif node.module.startswith("app.agent."):
                    bad.append(f"{rel}: from {node.module} import ... (private module)")
                elif node.module == "app":
                    for a in node.names:
                        if a.name == "agent":
                            # closed rule: binding the package at all is rejected, alias or not
                            bad.append(f"{rel}: from app import agent"
                                       f"{' as ' + a.asname if a.asname else ''} "
                                       f"(package binding enables private access via attribute/getattr)")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("app.agent."):
                        bad.append(f"{rel}: import {a.name} (private module)")
                    elif a.name == "app.agent":
                        bad.append(f"{rel}: import app.agent"
                                   f"{' as ' + a.asname if a.asname else ''} (package binding)")
            elif isinstance(node, ast.Call):
                fn = node.func
                name = (fn.attr if isinstance(fn, ast.Attribute) else
                        fn.id if isinstance(fn, ast.Name) else "")
                if name in ("import_module", "__import__"):
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                           and arg.value.startswith("app.agent"):
                            bad.append(f"{rel}: dynamic import of {arg.value}")
                        elif not isinstance(arg, ast.Constant):
                            indeterminate.append(f"{rel} (computed import target)")

    for rel in reader.list_files():
        if not rel.endswith(".py") or rel.startswith("app/agent/") or rel.startswith("tests/"):
            continue
        try:
            src = reader.read_text(rel)
        except Exception as e:
            indeterminate.append(f"{rel} (unreadable: {type(e).__name__})")
            continue
        if src is None:
            indeterminate.append(f"{rel} (absent from the verified object)")
            continue
        scan(rel, src)
    for rel, src in (extra_sources or {}).items():
        scan(rel, src)
    if indeterminate:
        return RuleResult(False, f"indeterminate sources: {indeterminate[:5]}", "source_indeterminate")
    return (RuleResult(True, f"no private app.agent import outside app/agent/** "
                             f"(closed rule over verified objects)") if not bad
            else RuleResult(False, f"violations={bad}", "private_agent_import"))


# ------------------------------------------------------------------------------- HL-17
@rule("HL-17", "trusted-context",
      "silent dependency installation detected by deterministic path AND content rules; an unreadable "
      "in-scope file is indeterminate, never clean",
      reasons=("silent_install_detected", "scan_indeterminate"))
def hl17(dep_policy, changed, file_reader) -> RuleResult:
    hits, unreadable = silent_install_hits(changed, file_reader, dep_policy)
    if unreadable:
        return RuleResult(False, f"in-scope files could not be read: {list(unreadable)}", "scan_indeterminate")
    if hits:
        return RuleResult(False, f"install commands found in changed files: {list(hits)}",
                          "silent_install_detected")
    return RuleResult(True, f"no install command in {len(changed)} changed files "
                            f"(bounded to {len(dep_policy['content_scan_paths'])} scan globs)")


# ------------------------------------------------------------------ specified_only (P0-7 governance)
specified_only(
    "HL-10", "runtime-only",
    "active RecommendationVersion insert and the authoritative boundary row commit in ONE primary-DB "
    "transaction, else fail closed. NOT EXECUTABLE here: needs product code + DB, both prohibited.",
    governance="Moving a rule to or from specified_only is a human decision (D-19); an AI may not "
               "downgrade an executable rule to silence it.")
specified_only(
    "HL-18", "runtime-only",
    "the attestation runner executes on an isolated CI runner enforced by branch protection. NOT "
    "EXECUTABLE here: needs live_operational evidence (GitHub settings).",
    governance="Human decision D-16/D-19; requires live_operational evidence before it may be claimed.")
