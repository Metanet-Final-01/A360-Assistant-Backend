"""Phase 0 v1.10 attestation orchestration.

The ONLY thing that may certify a change or an output.

v1.6 review corrections applied here:
  item 1  Output bound only to an `assurance_report_digest`, which proves a report EXISTS, not that
          the Change was attested and ALLOWED. Codex reproduced an unrelated, internally
          contradictory Change report authorising Output with decision=allow. v1.7 issues a
          ChangeAttestationReceipt from `attest_change` and requires `attest_output` to resolve it
          by ID and bind repository/commit/report/manifest/authority digests (HL-19).
  item 2  `attest_output(..., event_store, boundary_store)` took adapters as CALL arguments, so the
          assessed caller supplied its own authorities. Adapters are now bound once in an
          `AuthorityContext` at construction; public verification calls take only IDs and paths.
          Fixture wiring is only reachable through an explicit `for_testing_only` factory.

D-16 unchanged: whether the runner is actually protected is a human/operational decision. Nothing
here pretends Python can enforce that.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import rules
from core import (ATTESTOR_ID, ATTESTOR_VERSION, Artifact, ArtifactError, ArtifactStore,
                  PolicyResolver, TrustedContext, change_receipt_id, load_validators,
                  schema_set_digest)
from store import AuthorityContext, StoreReadError

# v1.8 defect (Codex, independently reproduced): `_test_path` was an ordinary dataclass field and the
# refusal compared ONE exact provenance string, so `source="ci_fixture_alias"` sailed through the
# normal constructor and resolved the approved fixture tree. Guarding a string was the wrong SHAPE of
# fix. v1.9 removes the caller-settable flag and makes the two construction paths MECHANICALLY
# different: the runner path binds ITS OWN policy root and never reads the caller's provenance label
# or policy directory, so no label - known or aliased - can steer it at a fixture tree.
_RUNNER_CONSTRUCTION = object()
_TEST_CONSTRUCTION = object()

# v1.9 defect (Codex, independently reproduced): v1.9 stopped reading `authority.policy_dir` and
# `authority.source` and called that a mechanical separation - but `for_runner(src_dir, ...)` still
# took the installation root FROM THE CALLER and resolved `src_dir/policies`. Codex handed it an
# ordinary `Path` - no sentinel, no subclass, no monkey patch, no test factory - and the approved
# fixture policies resolved. I had not fixed the defect; I had moved the caller-controlled input from
# one parameter to another. v1.10 derives the root from the attestor's own module location, so the
# runner path takes NO caller input that can select an authority.
_INSTALL_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class AttestationResult:
    """Evidence validity and the policy decision are SEPARATE outputs (v1.5 P0-2)."""

    evidence_valid: bool
    decision: str                       # allow | deny | observe
    evidence_reasons: tuple
    decision_reasons: tuple
    rule_results: tuple
    context_mode: str
    checkout_model: str = "unknown"
    changed_paths: tuple = ()
    resolved_authorities: tuple = ()
    receipt: dict = None                # v1.7: the Change hand-off document, when issued

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


@dataclass
class Attestor:
    """Constructed by the protected runner. Adapters and the policy directory come from the
    AuthorityContext, never from a verification call argument (v1.6 review item 2)."""

    src_dir: Path
    repo: Path
    now: dt.datetime
    authority: AuthorityContext
    _construction: object = None     # set ONLY by for_runner / for_testing_only
    policy_root: Path = field(init=False)
    validators: dict = field(init=False)
    resolver: PolicyResolver = field(init=False)
    artifacts: ArtifactStore = field(init=False)

    def __post_init__(self):
        if self._construction is _RUNNER_CONSTRUCTION:
            # The runner path DERIVES its policy root from the attestor's own installation and
            # ignores authority.policy_dir and authority.source entirely. An alias provenance label
            # cannot reach the fixture tree because no label is consulted on this path at all.
            self.policy_root = self.src_dir / "policies"
        elif self._construction is _TEST_CONSTRUCTION:
            self.policy_root = Path(self.authority.policy_dir)
        else:
            raise ValueError(
                "Attestor(...) direct construction is not a supported path. Use "
                "Attestor.for_runner(...) or Attestor.for_testing_only(...). NOTE: this is a "
                "construction separation, not a Python security boundary - anyone who can import "
                "this module can call either factory. Real runner and policy-tree protection are "
                "D-16/D-17.")
        self.validators = load_validators(self.src_dir / "schemas")
        self.resolver = PolicyResolver(self.policy_root, self.validators)
        self.artifacts = ArtifactStore(self.policy_root, self.validators)

    # ---------------------------------------------------------------- runner wiring
    @classmethod
    def for_runner(cls, repo: Path, now: dt.datetime,
                   authority: AuthorityContext) -> "Attestor":
        """The production construction path. Takes NO installation root.

        There is deliberately no `src_dir` parameter. The root is `_INSTALL_ROOT`, derived from this
        module's own location, so schemas and policies are always the ones shipped alongside the
        attestor that is running. `authority.policy_dir` and `authority.source` are still not read.

        HONEST SCOPE: this removes the caller's ability to SELECT an authority; it is not a security
        boundary. Anything that can rewrite this module or its policy tree still wins - D-16/D-17.
        """
        return cls(src_dir=_INSTALL_ROOT, repo=repo, now=now, authority=authority,
                   _construction=_RUNNER_CONSTRUCTION)

    # ---------------------------------------------------------------- test-only wiring
    @classmethod
    def for_testing_only(cls, src_dir: Path, repo: Path, now: dt.datetime,
                         authority: AuthorityContext) -> "Attestor":
        """Explicit fixture entry point (v1.6 review item 2).

        Unlike `for_runner`, the TEST path may name its own root: that is the whole point of the
        separation. Naming a root is how a caller declares "I am a fixture", not a way to steer the
        runner.

        The normal constructor cannot be steered at `policies_approved_fixture/` by accident: a
        caller must ask for a test-only Attestor by name, and the AuthorityContext must say so.
        """
        if not authority.is_test_provenance:
            raise ValueError("for_testing_only requires AuthorityContext(source='test_only_fixture')")
        return cls(src_dir=src_dir, repo=repo, now=now, authority=authority,
                   _construction=_TEST_CONSTRUCTION)

    # ---------------------------------------------------------------- resolved authority
    def resolved_digests(self, registry: Artifact, report: Artifact = None) -> dict:
        d = {"registry": registry.digest, "schemas": schema_set_digest(self.src_dir / "schemas")}
        # v1.8 defect (Codex): agent-version-policy existed as a FILE but no resolved authority and
        # no rule read it, so an unapproved v404 plus a fake policy digest reached Output allow.
        for k in ("operation-policy", "severity-policy", "dependency-path-policy",
                  "applicability-policy", "interface-policy", "agent-version-policy"):
            d[k] = self.resolver.resolve(k).digest
        if report is not None:
            d["assurance_report"] = report.digest
        return d

    @staticmethod
    def _authority_list(digests: dict, extra: dict = None) -> tuple:
        items = [f"{k}={v}" for k, v in sorted(digests.items())]
        items += [f"{k}={v}" for k, v in sorted((extra or {}).items())]
        return tuple(items)

    # ---------------------------------------------------------------- Change
    def attest_change(self, runner_env, manifest_path, report_path, registry_path) -> AttestationResult:
        results: list = []
        ev_reasons: list = []

        ctx = TrustedContext.from_runner_env(runner_env, self.repo)
        if not ctx.authoritative:
            r = (f"context: {ctx.downgrade_reason}",)
            return AttestationResult(False, "deny", r, r, (), ctx.mode, ctx.checkout_model)

        changed = ctx.changed_paths(self.repo)
        reader = ctx.object_reader(self.repo)

        try:
            manifest = Artifact.load(manifest_path, self.validators["change-manifest"])
            report = Artifact.load(report_path, self.validators["assurance-report"])
            registry = Artifact.load(registry_path, self.validators["control-registry"])
        except ArtifactError as e:
            r = (f"artifact load failed: {e}",)
            return AttestationResult(False, "deny", r, r, (), ctx.mode, ctx.checkout_model)

        dep = self.resolver.resolve("dependency-path-policy").obj
        app = self.resolver.resolve("applicability-policy").obj
        iface = self.resolver.resolve("interface-policy").obj
        digests = self.resolved_digests(registry, report)
        derived = rules.derive_projection(ctx, dep, app, changed, reader.read_text)
        subject = {"repository_id": ctx.repository_id, "commit": ctx.event_head_sha}
        public_names, _ = rules.agent_public_api(self.repo, iface)

        for rid, res in (
            ("HL-17", rules.hl17(dep, changed, reader.read_text)),
            ("HL-11", rules.hl11(ctx, manifest, derived)),
            ("HL-12", rules.hl12(ctx, manifest, report, derived, digests)),
            ("HL-01", rules.hl01(registry)),
            ("HL-02", rules.hl02(registry, [g["control_id"] for g in report.obj["gates"]])),
            ("HL-07", rules.hl07(manifest, derived, self.artifacts, subject, self.now)),
            ("HL-03", rules.hl03(registry, report, derived, self.artifacts, self.now)),
            ("HL-06", rules.hl06(reader, public_names)),
        ):
            results.append((rid, res))
            if not res.ok:
                ev_reasons.append(f"{rid}[{res.reason}]: {res.detail}")

        evidence_valid = not ev_reasons
        enforcement_mode = "enforce" if app.get("state") == "approved" else "observe"
        if not evidence_valid:
            decision, dec_reasons = "deny", tuple(ev_reasons)
        else:
            decision, dec_reasons = rules.policy_decision(report.obj, derived, self.artifacts,
                                                          registry, self.now, enforcement_mode)
        # v1.6 review item 1: emit the hand-off document Output must bind to.
        authority_digests = {k: v for k, v in digests.items() if k.endswith("-policy")}
        authority_digests.update({"registry": digests["registry"], "schemas": digests["schemas"]})
        receipt = {
            "kind": "change-receipt",
            # v1.7 review item 3: content-addressed, so two different valid attestations of the same
            # commit cannot collide, and re-issuing the same attestation is idempotent.
            "receipt_id": change_receipt_id(ctx.repository_id, ctx.event_head_sha, report.digest,
                                            manifest.digest, authority_digests, ATTESTOR_VERSION,
                                            decision, evidence_valid),
            "generated_at": self.now.isoformat().replace("+00:00", "Z"),
            "evidence_valid": evidence_valid,
            "decision": decision,
            "decision_reasons": list(dec_reasons),
            "repository_id": ctx.repository_id,
            "commit": ctx.event_head_sha,
            "assurance_report_digest": report.digest,
            "change_manifest_digest": manifest.digest,
            "control_registry_digest": digests["registry"],
            "schema_digest": digests["schemas"],
            "policy_digests": {k: v for k, v in digests.items()
                               if k.endswith("-policy")},
            "attestor": {"id": ATTESTOR_ID, "version": ATTESTOR_VERSION},
        }
        return AttestationResult(evidence_valid, decision, tuple(ev_reasons), dec_reasons,
                                 tuple(results), ctx.mode, ctx.checkout_model, tuple(changed),
                                 self._authority_list(digests), receipt)

    # ---------------------------------------------------------------- Output
    def attest_output(self, bundle_path, runtime_path, report_path, manifest_path,
                      boundary_record_id, registry_path) -> AttestationResult:
        """v1.6 review item 2: only IDs and artifact paths; adapters come from the AuthorityContext.

        v1.7 review item 2: the separate `receipt_id` argument is REMOVED. Codex reproduced that a
        method argument could override the bundle's own `change_receipt_id` and still reach allow.
        The bundle's `change_receipt_id` is now the single canonical identity for the lookup.
        """
        results: list = []
        ev_reasons: list = []
        try:
            bundle = Artifact.load(bundle_path, self.validators["evidence-bundle"])
            registry = Artifact.load(registry_path, self.validators["control-registry"])
            runtime = (Artifact.load(runtime_path, self.validators["runtime-validation"])
                       if runtime_path else None)
            report = (Artifact.load(report_path, self.validators["assurance-report"])
                      if report_path else None)
            manifest = (Artifact.load(manifest_path, self.validators["change-manifest"])
                        if manifest_path else None)
        except ArtifactError as e:
            r = (f"artifact load failed: {e}",)
            return AttestationResult(False, "deny", r, r, (), "n/a")

        digests = self.resolved_digests(registry, report)

        boundary_rec = None
        extra = {}
        if boundary_record_id is not None:
            try:
                boundary_rec = self.authority.boundary_store.resolve(boundary_record_id)
                extra.update({"boundary_record_id": boundary_rec.record_id,
                              "boundary_record_digest": boundary_rec.digest,
                              "boundary_record_revision": boundary_rec.revision})
            except StoreReadError as e:
                ev_reasons.append(f"HL-15[boundary_unresolved]: {e}")

        receipt = None
        rid_claim = bundle.obj.get("change_receipt_id")   # single canonical identity
        if rid_claim is not None:
            try:
                receipt = self.authority.receipt_store.resolve(rid_claim)
                extra.update({"change_receipt_id": receipt.receipt_id,
                              "change_receipt_digest": receipt.digest,
                              "change_receipt_revision": receipt.revision})
            except StoreReadError as e:
                ev_reasons.append(f"HL-19[receipt_unresolved]: {e}")

        checks = [
            ("HL-14", rules.hl14(self.resolver, bundle)),
            ("HL-13", rules.hl13(self.resolver, bundle, self.authority.event_store, self.now)),
            ("HL-15", rules.hl15(registry, bundle, runtime, boundary_rec, digests,
                                 self.artifacts, self.now)),
        ]
        if bundle.obj["assurance_verdict"] == "assured":
            checks.append(("HL-19", rules.hl19(bundle, receipt, report,
                                               manifest.digest if manifest else None, digests)))
            # v1.9 P0-2: an assured Output must bind an OBSERVED, APPROVED producer Agent version.
            # At d66fce1 the real product cannot satisfy this (no version field in _done_data), so
            # the honest outcome today is fail-closed. See D-21/D-22.
            checks.append(("HL-20", rules.hl20(self.resolver, runtime, digests,
                                               self.authority.observer())))
        if runtime:
            checks.append(("HL-16", rules.hl16(self.resolver, runtime)))

        for rid, res in checks:
            results.append((rid, res))
            if not res.ok:
                ev_reasons.append(f"{rid}[{res.reason}]: {res.detail}")

        evidence_valid = not ev_reasons
        verdict = bundle.obj["assurance_verdict"]
        if not evidence_valid:
            decision, dec_reasons = "deny", tuple(ev_reasons)
        elif verdict == "assured":
            decision, dec_reasons = "allow", ()
        else:
            decision, dec_reasons = "deny", (f"bundle assurance_verdict={verdict}",)
        return AttestationResult(evidence_valid, decision, tuple(ev_reasons), dec_reasons,
                                 tuple(results), "n/a", "n/a", (),
                                 self._authority_list(digests, extra))
