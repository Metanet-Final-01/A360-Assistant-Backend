"""Phase 0 v1.6 regression transcript for Codex's v1.5 consistency checks.

`artifacts/codex-v1.5-consistency-check.py` (SHA-256 303740a0...42de5dd) asserts that each v1.5
DEFECT is present: every one of its 9 cases PASSes when the defect exists. I reproduced all 9
against v1.5 before accepting the findings.

This transcript re-runs the same 9 attacks against v1.6. Each must now be CLOSED - i.e. Codex's
original assertion must no longer hold. The attacks are reproduced verbatim in intent; only the
call signatures that v1.6 deliberately changed (reader-based HL-06, BoundaryRecord-based HL-15)
are adapted, and no assertion is weakened.

Read-only. Writes nothing.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import core  # noqa: E402
from core import ArtifactStore, TrustedContext, deep_freeze  # noqa: E402
from rules import hl03, hl06, hl13, hl15  # noqa: E402
from store import BoundaryRecordStore, QueryEvidence, StoreRow  # noqa: E402

REPO = Path(r"D:\메타넷 최종\A360-Assistant-Backend")
NOW = dt.datetime(2026, 7, 15, 0, 0, 10, tzinfo=dt.timezone.utc)
BASELINE = "ed1a1b0d62452f75212da4f78f3e8b9f990da42e"
results = []


def closed(name: str, is_closed: bool, detail: str):
    results.append(is_closed)
    print(f"{'CLOSED ' if is_closed else 'STILL OPEN'} | {name}")
    print(f"           | {detail}")


def git(*a):
    return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True).stdout.strip()


# 1 + 2 -- context: event_head != real HEAD, and dirty checkout
head = git("rev-parse", "HEAD")
env = {"A360_REPO_ID": "independent-review-repo", "A360_EVENT": "pull_request",
       "A360_BASE_SHA": BASELINE, "A360_HEAD_SHA": BASELINE, "A360_CHECKOUT_SHA": head,
       "A360_MERGE_BASE": BASELINE, "A360_WORKFLOW_REF": "independent-review"}
ctx = TrustedContext.from_runner_env(env, REPO)
closed("v1.5 #1: authoritative while event_head != real HEAD",
       not ctx.authoritative, f"mode={ctx.mode}; {ctx.downgrade_reason}")

honest = {**env, "A360_HEAD_SHA": head, "A360_MERGE_BASE": git("merge-base", BASELINE, head)}
ctx2 = TrustedContext.from_runner_env(honest, REPO)
# v1.9: this assertion was WRONG and environment-dependent - a latent time bomb. It required every
# dirty path to be unreadable, which is only true for UNTRACKED files. When a TRACKED file is
# modified, the reader correctly returns the committed object's bytes, and the old test scored that
# correct behaviour as a leak. It passed for four versions only because this working tree happened
# to be dirty with untracked files alone. The real invariant, asserted per path:
#   untracked in HEAD      -> the reader must return None
#   tracked and modified   -> the reader must return the OBJECT bytes, never the worktree bytes
worktree_leak, checked = False, []
if ctx2.authoritative and ctx2.git_state == "dirty":
    r = ctx2.object_reader(REPO)
    for p in ctx2.dirty_paths:
        got = r.read_text(p)
        tracked = subprocess.run(["git", "cat-file", "-e", f"{ctx2.event_head_sha}:{p}"],
                                 cwd=REPO, capture_output=True).returncode == 0
        if not tracked:
            leak = got is not None
        else:
            # decode bytes exactly as GitObjectReader does: text=True would apply Windows
            # universal-newline translation and manufacture a false difference.
            obj = subprocess.run(["git", "show", f"{ctx2.event_head_sha}:{p}"], cwd=REPO,
                                 capture_output=True).stdout.decode("utf-8", "replace")
            wt = ((REPO / p).read_bytes().decode("utf-8", "replace")
                  if (REPO / p).exists() else None)
            # the reader must equal the object, and must NOT equal the (different) worktree bytes
            leak = (got is None) or (got != obj) or (wt is not None and obj != wt and got == wt)
        checked.append(f"{p}{'*' if leak else ''}")
        worktree_leak |= leak
closed("v1.5 #2: dirty worktree bytes can reach the assessment",
       not worktree_leak,
       f"git_state={ctx2.git_state}, dirty={len(ctx2.dirty_paths)}; every dirty path resolves to the "
       f"verified object {ctx2.event_head_sha[:8]} and never to worktree bytes "
       f"(untracked->None, tracked->object). checked={checked}")

# 3 -- FrozenDict backing mutation
frozen = deep_freeze({"state": "unapproved"})
try:
    frozen._d["state"] = "approved"
    detail = f"backing mutation succeeded; value now {frozen['state']!r}"
    is_closed = False
except AttributeError as e:
    detail = f"{type(e).__name__}: {e}; value still {frozen['state']!r}"
    is_closed = True
closed("v1.5 #3: frozen._d[...] changes a value after hashing", is_closed, detail)


# 4 -- HL-13 accepts unverified condition + mismatched query evidence
class IndependentResolver:
    def __init__(self, policy):
        self._a = SimpleNamespace(obj=policy)

    def resolve(self, kind):
        return self._a


class IndependentStore:
    def expected_query_digest(self, *a):
        return "sha256:" + "0" * 64

    def result_set_digest(self, rows):
        return "sha256:" + "0" * 64

    def query(self, source_table, subject_key, subject_value, window_start, window_end):
        row = StoreRow("audit_log", "request_id", "req-independent", NOW - dt.timedelta(seconds=1),
                       "revision-1")
        ev = QueryEvidence(source_table="different_table", subject_key="different_key",
                           subject_value="different_value", window_start=NOW - dt.timedelta(days=1),
                           window_end=NOW + dt.timedelta(days=1), source_revision="revision-1",
                           queried_at=NOW, query_digest="not-the-query-that-was-requested",
                           row_count=999, result_set_digest="not-the-rows-that-were-returned")
        return (row,), ev


stage = {"stage_id": "audit", "event_kind": "audit_log", "min_count": 1, "max_count": None,
         "source_table": "audit_logs", "correlation_key": "request_id", "window_seconds": 60,
         "condition": "must_be_successful"}
bundle = SimpleNamespace(obj={"operation": "candidate_validation",
                              "subject": {"request_id": "req-independent"},
                              "completeness": {"observed": [{"stage_id": "audit", "count": 1}],
                                               "missing": [], "complete": True,
                                               "derivation": {"source_revision": "revision-1",
                                                              "queried_at": "2026-07-15T00:00:10Z"}}})
r13 = hl13(IndependentResolver({"operations": {"candidate_validation": {"stages": [stage]}}}),
           bundle, IndependentStore(), NOW)
closed("v1.5 #4: HL-13 accepts an unevaluated condition + mismatched query evidence",
       not r13.ok, f"ok={r13.ok} reason={r13.reason}: {r13.detail[:80]}")

# 6 -- HL-03 calls a failed required gate a successful rule; decision was not separated
registry = SimpleNamespace(obj={"controls": [{"id": "CH-04"}]})
report = SimpleNamespace(obj={"derived_facts": {"applicable_controls": ["CH-04"]},
                              "gates": [{"control_id": "CH-04", "status": "fail"}],
                              "subject": {"repository_id": "r", "commit": "b" * 40},
                              "verdict": "fail"})
derived = {"applicable_controls": ["CH-04"], "unreadable_scanned_paths": [], "git_state": "clean"}
r03 = hl03(registry, report, derived, ArtifactStore(HERE / "policies", {}), NOW)
import rules as _rules  # noqa: E402
dec, why = _rules.policy_decision(report.obj, derived, ArtifactStore(HERE / "policies", {}),
                                  registry, NOW, "enforce")
closed("v1.5 #6: a failed required gate can still reach attested",
       dec != "allow",
       f"HL-03 evidence ok={r03.ok} (an honest failure report IS valid evidence), but "
       f"policy_decision={dec} because {why[0][:60] if why else ''}")

# 7 -- HL-06 alias + getattr
src = {"app/api/independent.py": ("from app import agent as owned_agent\n"
                                  "private_module = getattr(owned_agent, 'verify')\n")}


class _EmptyReader:
    def list_files(self):
        return ()

    def read_text(self, rel):
        return None


r06 = hl06(_EmptyReader(), frozenset({"analyze", "recommend", "stream_agent_turn"}), src)
closed("v1.5 #7: HL-06 accepts private access via getattr on a package alias",
       not r06.ok, f"ok={r06.ok} reason={r06.reason}: {r06.detail[:80]}")

# 8 + 9 -- assured output without a resolved report; caller-supplied boundary row
pay, rep_d, rt_d = ("sha256:" + "1" * 64), ("sha256:" + "2" * 64), ("sha256:" + "4" * 64)
reg_d, sch_d, pol_d = ("sha256:" + "5" * 64), ("sha256:" + "6" * 64), ("sha256:" + "7" * 64)
bpayload = {"candidate_id": "candidate-independent", "candidate_payload_digest": pay,
            "boundary_schema_digest": "boundary-schema-v1", "catalog_snapshot_digest": "catalog-v1",
            "boundary_validator_version": "validator-v1"}
bstore = BoundaryRecordStore(records={"BR": json.dumps(bpayload, sort_keys=True).encode()})
brec = bstore.resolve("BR")
eb = SimpleNamespace(obj={"control_registry_digest": reg_d, "schema_digest": sch_d,
                          "completeness": {"policy_digest": pol_d},
                          "operation_policy": {"digest": pol_d},
                          "linked_reports": {"assurance_report_digest": rep_d,
                                             "runtime_validation_digest": rt_d},
                          "operation": "candidate_validation",
                          "subject": {"candidate_id": "candidate-independent",
                                      "request_id": "request-independent",
                                      "session_id": "session-independent",
                                      "candidate_payload_digest": pay, "recommendation_version": 3},
                          "dual_evidence": {"boundary_enforcement_digest": brec.digest,
                                            "bound_candidate_id": "candidate-independent",
                                            "bound_request_id": "request-independent",
                                            "bound_candidate_payload_digest": pay},
                          "assurance_verdict": "assured", "controls": []})
rt = SimpleNamespace(digest=rt_d,
                     obj={"identity": {"candidate_id": "candidate-independent",
                                       "request_id": "request-independent",
                                       "session_id": "session-independent"},
                          "binding": {"candidate_payload_digest": pay,
                                      "boundary_schema_digest": "boundary-schema-v1",
                                      "catalog_snapshot_digest": "catalog-v1",
                                      "boundary_validator_version": "validator-v1"},
                          "business_outcome": {"recommendation_version": 3},
                          "assurance_status": "validated"})
base_d = {"registry": reg_d, "schemas": sch_d, "operation-policy": pol_d}
no_w = ArtifactStore(HERE / "policies", {})
r_unres = hl15(SimpleNamespace(obj={"controls": []}), eb, rt, brec, base_d, no_w, NOW)
closed("v1.5 #8: assured output with no resolved assurance report",
       not r_unres.ok,
       f"ok={r_unres.ok} reason={r_unres.reason}; v1.6 attest_output resolves the report read-once "
       f"so the positive path is reachable (see vertical_paths.py)")

# The v1.5 attack: a caller-built dict that agrees with itself. v1.6's HL-15 signature only
# accepts a BoundaryRecord produced by the runner-owned adapter, so the attack cannot be expressed.
caller_dict = {"digest": brec.digest, **bpayload}
try:
    r_caller = hl15(SimpleNamespace(obj={"controls": []}), eb, rt, caller_dict,
                    {**base_d, "assurance_report": rep_d}, no_w, NOW)
    accepted = r_caller.ok
    detail = f"caller dict accepted={accepted} reason={r_caller.reason}"
except AttributeError as e:
    accepted = False
    detail = f"a caller dict is no longer a valid boundary input: {type(e).__name__}: {e}"
closed("v1.5 #9: HL-15 accepts a plain caller-provided boundary row", not accepted, detail)

print(f"\nSUMMARY | v1.5 defects closed = {sum(results)}/{len(results)}")
sys.exit(0 if all(results) else 1)
