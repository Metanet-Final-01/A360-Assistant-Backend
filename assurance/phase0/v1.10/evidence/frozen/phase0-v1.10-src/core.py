"""Phase 0 v1.5 trusted-authority core.

Answers the v1.4 review: an assessed producer must not be able to CHOOSE or MUTATE the
authority that certifies it.

v1.4 defects fixed here:
  P0-1  TrustedContext.from_runner_env returned authoritative with git_state=unknown.
        Now: authoritative REQUIRES a real repo, a derived git state, and Git-verified
        checkout SHA + merge base.
  P0-2  Artifact.obj / cached policy objects were mutable after hashing; mutating the parsed
        policy to state=approved made require_approved() return True while the bytes said
        unapproved. Now: deep_freeze() makes parsed data recursively immutable.
  P0-7  Coverage counted case labels. Now: the registry wraps every rule fn and counts real
        invocations; RuleResult carries a typed reason code so a case must fail for the
        INTENDED reason.

Read-only. Never writes to the product tree.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping
from types import MappingProxyType
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml
from jsonschema import Draft202012Validator, FormatChecker


def sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


# ------------------------------------------------------- immutability scope (v1.6 review item 4)
# v1.5 exposed a plain dict in the public slot `_d`; v1.6 replaced it with MappingProxyType.
#
# v1.6 THEN OVERCLAIMED. It said: "every authority DECISION is derived from the read-once canonical
# bytes, so even a successful in-process mutation cannot change what was hashed and compared."
# That was FALSE. `require_approved` read `art.obj` (the proxy), and Codex reproduced:
#     backing = next(i for i in gc.get_referents(policy.obj) if isinstance(i, dict))
#     backing["state"] = "unapproved"      -> the approval decision flipped
# The claim is RETRACTED.
#
# HONEST SCOPE, v1.7:
#   * MappingProxyType gives ACCIDENTAL-MUTATION RESISTANCE only. Rule code that carelessly writes
#     to parsed data raises instead of silently corrupting an authority value.
#   * It is NOT a security boundary. Any code running in this process can reach the backing store
#     through gc or ctypes. Python cannot defend against code that is already executing beside it.
#   * SAME-PROCESS INTEGRITY IS A RUNNER PROPERTY (D-16), not a property of this module.
#
# What v1.7 does add, narrowly: `PolicyResolver.require_approved` re-parses the policy from the
# artifact's immutable `raw` bytes AT DECISION TIME (see `_decide_from_raw`). This defeats the
# specific aliasing attack Codex demonstrated, because `bytes` cannot be mutated in place from
# ordinary Python. It does NOT make the process trustworthy: ctypes can still rewrite a bytes
# buffer. Cost: one json/yaml parse per approval decision on a policy file of a few KB; measured
# in the self-test, and negligible next to the git and schema work already performed.


def deep_freeze(x: Any) -> Any:
    if isinstance(x, MappingProxyType):
        return x
    if isinstance(x, Mapping):
        return MappingProxyType({k: deep_freeze(v) for k, v in x.items()})
    if isinstance(x, (list, tuple)):
        return tuple(deep_freeze(v) for v in x)
    return x


def thaw(x: Any) -> Any:
    if isinstance(x, Mapping):
        return {k: thaw(v) for k, v in x.items()}
    if isinstance(x, tuple):
        return [thaw(v) for v in x]
    return x


# --------------------------------------------------------------------------- Artifact
class ArtifactError(Exception):
    pass


class StoreReadError(Exception):
    """Raised when in-scope evidence cannot be read. Callers MUST fail closed (P1-2)."""


@dataclass(frozen=True)
class Artifact:
    """A document read exactly once. bytes, digest and parsed object cannot disagree, and the
    parsed object is recursively immutable so it cannot drift from the bytes afterwards."""

    uri: str
    raw: bytes
    digest: str
    obj: Any  # always deep-frozen

    @classmethod
    def load(cls, path, validator: Draft202012Validator | None = None) -> "Artifact":
        p = Path(path)
        if not p.is_file():
            raise ArtifactError(f"artifact not found: {p}")
        return cls.from_bytes(str(p), p.read_bytes(), validator)

    @classmethod
    def from_bytes(cls, uri: str, raw: bytes, validator: Draft202012Validator | None = None) -> "Artifact":
        text = raw.decode("utf-8")
        parsed = json.loads(text) if uri.endswith(".json") else yaml.safe_load(text)
        if parsed is None:
            raise ArtifactError(f"empty artifact: {uri}")
        if validator is not None:
            errs = sorted(validator.iter_errors(parsed), key=lambda e: list(e.path))
            if errs:
                raise ArtifactError(f"{uri} failed schema: {errs[0].message[:160]}")
        return cls(uri=uri, raw=raw, digest=sha256_bytes(raw), obj=deep_freeze(parsed))


def load_validators(schema_dir) -> dict[str, Draft202012Validator]:
    from referencing import Registry, Resource

    schemas = {}
    for p in sorted(Path(schema_dir).glob("*.schema.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(s)
        schemas[p.name.replace(".schema.json", "")] = s
    registry = Registry().with_resources([(s["$id"], Resource.from_contents(s)) for s in schemas.values()])
    return {n: Draft202012Validator(s, registry=registry, format_checker=FormatChecker())
            for n, s in schemas.items()}


def schema_set_digest(schema_dir) -> str:
    h = hashlib.sha256()
    for p in sorted(Path(schema_dir).glob("*.schema.json")):
        h.update(p.read_bytes())
    return "sha256:" + h.hexdigest()


# --------------------------------------------------------------------- TrustedContext (P0-1)
class ContextError(Exception):
    pass


def _git(repo: Path, *args: str, strip: bool = True) -> str:
    """v1.9 self-found bug: this stripped EVERY git output, including `status --porcelain`, whose
    format is a two-column status field then a space (`" M path"`). Stripping removed the leading
    space of the FIRST line only, so the `l[3:]` path slice ate that path's first character:
    `.env.example` was recorded as `env.example`. It hid for four versions because a working tree
    whose first porcelain line is untracked (`"?? path"`) has no leading space to lose - which is
    exactly the shape of latent bug this harness exists to catch, in the harness itself.
    Callers that parse column-oriented output MUST pass strip=False.
    """
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if r.returncode != 0:
        raise ContextError(f"git {' '.join(args)} failed: {r.stderr.strip()[:120]}")
    return r.stdout.strip() if strip else r.stdout


@dataclass(frozen=True)
class GitObjectReader:
    """Reads assessed content from a VERIFIED Git object, never from the mutable working tree.

    v1.5 P0-1: changed paths were computed from committed objects while file CONTENT was read
    from the worktree, so one commit's identity could be combined with another state's bytes.
    """

    repo: Path
    sha: str

    def list_files(self) -> tuple:
        out = _git(self.repo, "ls-tree", "-r", "--name-only", self.sha)
        return tuple(sorted(l for l in out.splitlines() if l.strip()))

    def read_text(self, rel: str):
        """Text at the verified commit, or None if absent. Raises ContextError on read failure."""
        r = subprocess.run(["git", "show", f"{self.sha}:{rel}"], cwd=self.repo, capture_output=True)
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", "replace")
            if "does not exist" in err or "exists on disk, but not in" in err:
                return None
            raise ContextError(f"git show failed for {rel}: {err.strip()[:100]}")
        try:
            return r.stdout.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ContextError(f"{rel} is not utf-8 at {self.sha[:8]}: {e}") from None


@dataclass(frozen=True)
class TrustedContext:
    """Git/CI identity supplied by the RUNNER and verified against the real checkout.

    v1.5 P0-1 fixes:
      * checkout_model is explicit. For head_checkout we require
        real HEAD == event_head_sha == checkout_sha. v1.5 only compared HEAD to checkout_sha, so
        an event head pointing at a different commit was still called authoritative.
      * merge_ref is NOT supported: it needs its own verification rules, and treating two SHAs as
        interchangeable is the defect being fixed.
      * assessed content is read via GitObjectReader from the verified commit, so a dirty worktree
        cannot contribute bytes. dirty_paths are still recorded and still block a pass verdict.
    """

    mode: str
    checkout_model: str
    repository_id: str
    repository_name: str
    event_type: str
    pr_base_sha: str
    event_head_sha: str
    checkout_sha: str
    merge_base: str
    workflow_ref: str
    git_state: str
    dirty_paths: tuple = ()
    downgrade_reason: str = None

    @property
    def authoritative(self) -> bool:
        return self.mode == "authoritative"

    @classmethod
    def local(cls, reason: str) -> "TrustedContext":
        return cls(mode="local_nonauthoritative", checkout_model="unknown", repository_id="",
                   repository_name="", event_type="", pr_base_sha="", event_head_sha="",
                   checkout_sha="", merge_base="", workflow_ref="", git_state="unknown",
                   downgrade_reason=reason)

    @classmethod
    def from_runner_env(cls, env: Mapping, repo: Path = None) -> "TrustedContext":
        required = ["A360_REPO_ID", "A360_EVENT", "A360_BASE_SHA", "A360_HEAD_SHA",
                    "A360_CHECKOUT_SHA", "A360_MERGE_BASE", "A360_WORKFLOW_REF"]
        missing = [k for k in required if not env.get(k)]
        if missing:
            return cls.local(f"runner env missing {missing}")
        model = env.get("A360_CHECKOUT_MODEL", "head_checkout")
        if model != "head_checkout":
            return cls.local(f"unsupported checkout model {model!r}: only head_checkout is verified")
        if repo is None:
            return cls.local("no repository provided: git state and checkout cannot be verified")
        try:
            head = _git(repo, "rev-parse", "HEAD")
            status = _git(repo, "status", "--porcelain", strip=False)
            mb = _git(repo, "merge-base", env["A360_BASE_SHA"], env["A360_HEAD_SHA"])
        except ContextError as e:
            return cls.local(f"git verification failed: {e}")
        if head != env["A360_CHECKOUT_SHA"]:
            return cls.local(f"declared checkout {env['A360_CHECKOUT_SHA'][:8]} != real HEAD {head[:8]}")
        if head != env["A360_HEAD_SHA"]:
            return cls.local(f"declared event head {env['A360_HEAD_SHA'][:8]} != real HEAD {head[:8]} "
                             "(head_checkout requires HEAD == event_head == checkout)")
        if mb != env["A360_MERGE_BASE"]:
            return cls.local(f"declared merge base {env['A360_MERGE_BASE'][:8]} != real {mb[:8]}")
        dirty = tuple(sorted(l[3:] for l in status.splitlines() if l.strip()))
        # NOTE: l[3:] is only correct because status is NOT stripped (see _git).
        return cls(mode="authoritative", checkout_model=model, repository_id=env["A360_REPO_ID"],
                   repository_name=env.get("A360_REPO_NAME", ""), event_type=env["A360_EVENT"],
                   pr_base_sha=env["A360_BASE_SHA"], event_head_sha=env["A360_HEAD_SHA"],
                   checkout_sha=env["A360_CHECKOUT_SHA"], merge_base=mb,
                   workflow_ref=env["A360_WORKFLOW_REF"],
                   git_state="clean" if not dirty else "dirty", dirty_paths=dirty)

    def changed_paths(self, repo: Path) -> tuple:
        """Derived INTERNALLY from the trusted event. Never accepted from a caller."""
        if not self.authoritative:
            raise ContextError("cannot derive changed paths from a non-authoritative context")
        out = _git(repo, "diff", "--name-only", f"{self.merge_base}..{self.event_head_sha}")
        return tuple(sorted(l for l in out.splitlines() if l.strip()))

    def object_reader(self, repo: Path) -> GitObjectReader:
        if not self.authoritative:
            raise ContextError("cannot read verified objects from a non-authoritative context")
        return GitObjectReader(repo, self.event_head_sha)


# --------------------------------------------------------------------- PolicyResolver (P0-2)
@dataclass
class PolicyResolver:
    """Policy bytes from a PINNED protected directory. Parsed data is deep-frozen, so the
    v1.4 attack (mutate cached obj -> state=approved) now raises instead of succeeding."""

    policy_dir: Path
    validators: dict
    _cache: dict = field(default_factory=dict)

    def resolve(self, kind: str) -> Artifact:
        if kind not in self._cache:
            self._cache[kind] = Artifact.load(Path(self.policy_dir) / f"{kind}.yaml",
                                              self.validators.get("policy"))
        return self._cache[kind]

    @staticmethod
    def _decide_from_raw(art: "Artifact") -> dict:
        """Re-parse the policy from its immutable `raw` bytes at DECISION time.

        v1.6 review item 4: reading `art.obj` let a same-process aliasing attack (gc.get_referents
        on the mappingproxy) flip an approval decision. `raw` is a `bytes` object and cannot be
        mutated in place from ordinary Python, so the demonstrated attack no longer changes the
        outcome. This is NOT a security boundary (ctypes can still rewrite a buffer); it removes one
        concrete aliasing hazard. Same-process integrity remains D-16.
        """
        text = art.raw.decode("utf-8")
        return json.loads(text) if art.uri.endswith(".json") else yaml.safe_load(text)

    def require_approved(self, kind: str, claim: Mapping) -> tuple:
        try:
            art = self.resolve(kind)
        except ArtifactError as e:
            return False, f"policy {kind} unresolvable: {e}", "policy_unresolvable"
        p = self._decide_from_raw(art)          # v1.6 review item 4: decide from bytes, not from obj
        problems = []
        if claim.get("policy_id") != p["policy_id"]:
            problems.append(f"policy_id claimed={claim.get('policy_id')} actual={p['policy_id']}")
        if claim.get("version") != p["version"]:
            problems.append(f"version claimed={claim.get('version')} actual={p['version']}")
        if claim.get("digest") != art.digest:
            problems.append("digest does not match the protected policy bytes")
        if claim.get("state") != p.get("state"):
            problems.append(f"state claimed={claim.get('state')} actual={p.get('state')}")
        if problems:
            return False, "; ".join(problems), "policy_mismatch"
        if p.get("state") != "approved":
            return False, f"policy {p['policy_id']} state={p.get('state')} (human approval absent)", "policy_unapproved"
        appr = p.get("approval") or {}
        if not (appr and appr.get("approver_role") and appr.get("approval_ref")):
            return False, "approved policy carries no human approval evidence", "policy_no_approval_evidence"
        return True, f"{p['policy_id']}@{p['version']} approved, digest bound", None


# ----------------------------------------------------------- approval / waiver resolver (P0-4)
@dataclass
class ArtifactStore:
    """Resolves approval and waiver ARTIFACTS from a protected directory.

    v1.4 accepted caller-supplied dictionaries. Here every record must exist on disk, validate
    against its schema, and be digest-addressable.
    """

    root: Path
    validators: dict

    def _load(self, sub: str, ident: str, schema: str) -> Artifact | None:
        p = Path(self.root) / sub / f"{ident}.yaml"
        if not p.is_file():
            return None
        return Artifact.load(p, self.validators.get(schema))

    def approval(self, ident: str) -> Artifact | None:
        return self._load("approvals", ident, "approval")

    def waiver(self, ident: str) -> Artifact | None:
        return self._load("waivers", ident, "waiver")


# ----------------------------------------------------------------- rule registry (P0-7)
@dataclass(frozen=True)
class RuleResult:
    ok: bool
    detail: str
    reason: str | None = None      # typed reason code; required when ok is False


@dataclass
class Rule:
    rid: str
    category: str
    executable: bool
    statement: str
    reasons: tuple[str, ...]
    fn: Callable | None
    calls: int = 0                 # real invocation counter (P0-7)


REGISTRY: dict[str, Rule] = {}


def rule(rid: str, category: str, statement: str, reasons: tuple[str, ...] = ()):
    def deco(fn):
        r = Rule(rid, category, True, statement, reasons, None)

        def wrapper(*a, **k):
            r.calls += 1                      # instrument the REAL call, not a case label
            res = fn(*a, **k)
            if not isinstance(res, RuleResult):
                raise TypeError(f"{rid} must return RuleResult")
            if not res.ok and res.reason is None:
                raise TypeError(f"{rid} returned a failure with no typed reason code")
            if not res.ok and r.reasons and res.reason not in r.reasons:
                raise TypeError(f"{rid} returned undeclared reason {res.reason!r}")
            return res

        r.fn = wrapper
        REGISTRY[rid] = r
        return wrapper
    return deco


def specified_only(rid: str, category: str, statement: str, governance: str):
    """A rule that is NOT executable. Downgrading a rule to this state is itself a human
    decision (D-19); the note must say who may approve it."""
    REGISTRY[rid] = Rule(rid, category, False, f"{statement} | GOVERNANCE: {governance}", (), None)


# ------------------------------------------------- receipt identity (v1.7 review item 3)
ATTESTOR_ID = "a360-phase0-attestor"
ATTESTOR_VERSION = "1.10.0"


def change_receipt_id(repository_id: str, commit: str, report_digest: str, manifest_digest: str,
                      authority_digests: Mapping, attestor_version: str,
                      decision: str, evidence_valid: bool) -> str:
    """Content-addressed receipt id.

    v1.7 used `CR-{commit[:12]}`. Codex reproduced the collision: two DIFFERENT valid attestations of
    the same commit (different report/manifest/authority) received the same id, so one could stand in
    for the other. The id now binds everything that makes the attestation what it is, which also
    makes re-issuing the same attestation idempotent (same inputs -> same id).

    v1.9 (found while writing v1.9's own deny fixture, not reported by Codex): v1.8 hashed the
    SUBJECT and the AUTHORITY but not the VERDICT. Two receipts for one change that differed only in
    `decision` shared an id, so flipping a deny receipt's decision to "allow" left the content
    address valid and HL-19's recomputation could not detect it - defeating the point of recomputing
    at all. The verdict is now part of the address.
    """
    payload = json.dumps({
        "repository_id": repository_id,
        "commit": commit,
        "report_digest": report_digest,
        "manifest_digest": manifest_digest,
        "authority_digests": {k: v for k, v in sorted(dict(authority_digests).items())},
        "attestor_version": attestor_version,
        "decision": decision,
        "evidence_valid": bool(evidence_valid),
    }, sort_keys=True, separators=(",", ":")).encode()
    return "CR-" + hashlib.sha256(payload).hexdigest()[:32]
