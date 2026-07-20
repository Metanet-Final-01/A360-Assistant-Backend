"""Phase 0 v1.5 trusted event store contract (P0-6).

v1.4 defect: the store mapped (source_table, correlation value) -> int. It carried no event
kind, occurrence time, source revision or query identity, so the 60s policy window could not
be evaluated and duplicate/unknown observations were ignored.

v1.5: a read-only interface returning TYPED rows plus immutable query evidence. The attestor
evaluates event kind, correlation, condition and time window itself, and fails closed on read
errors.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from core import StoreReadError


@dataclass(frozen=True)
class PublicBoundaryObservation:
    """What a runner-owned adapter actually SAW at the Agent's public boundary.

    Backend-side only. An adapter may call the Agent team's PUBLIC symbols
    (`available_versions()`, `default_version()`) and nothing else: no `app/agent/**` edit, no
    private import, no interference with prompt/graph/model/tool/repair/retry/eval (D-002).
    """

    registry_snapshot_digest: str
    public_contract_version: str
    resolved_version: str = None            # None until D-21 makes it observable
    observability: str = "not_observable_pending_D21"


class PublicBoundaryAdapter(Protocol):
    def observe(self) -> PublicBoundaryObservation:
        """MUST raise StoreReadError when the public boundary cannot be observed."""


class NotConfiguredPublicBoundaryAdapter:
    """The SHIPPED runner adapter: there is no configured public-boundary observer, so it fails
    closed. This is the honest state at `d66fce1` - the harness does not observe the Agent's public
    boundary at all yet, and must not pretend a self-reported runtime block is an observation.
    """

    def observe(self) -> PublicBoundaryObservation:
        raise StoreReadError(
            "no public-boundary observation adapter is configured: the Backend boundary cannot "
            "verify agent registry/contract provenance (D-21/D-22 are unresolved)")


class FixturePublicContractAdapter:
    """TEST-ONLY stand-in for a runner-owned public-boundary observer.

    v1.9 defect (Codex): HL-20 checked the policy digest and the approved list but never READ
    `agent_registry_snapshot_digest` or `public_contract_version`, so Codex forged the registry
    digest, relinked the runtime digest, and still reached allow. Those two fields were decoration.
    v1.10 makes an adapter observe them and HL-20 compare.

    At `d66fce1` `app/agent/vN/orchestrator/graph.py::_done_data` returns no version, so Backend
    genuinely CANNOT observe which version the producer ran. This adapter does not change that fact
    and is not evidence that it is observable: it exists so the positive Output path can exercise the
    approved branch of HL-20 at all. The shipped runner uses NotConfiguredPublicBoundaryAdapter.
    """

    def __init__(self, resolved_version: str = None, contract_version: str = "0.2.0-fixture",
                 registry_snapshot_digest: str = None,
                 observability: str = "observed_from_public_contract"):
        self._obs = PublicBoundaryObservation(
            registry_snapshot_digest=registry_snapshot_digest or ("sha256:" + "a" * 64),
            public_contract_version=contract_version,
            resolved_version=resolved_version,
            observability=observability if resolved_version else "not_observable_pending_D21")

    def observe(self) -> PublicBoundaryObservation:
        return self._obs

    def observe_resolved_version(self) -> tuple:
        """Back-compat shim for v1.9 fixtures: (resolved_version, observability, contract_version)."""
        return (self._obs.resolved_version, self._obs.observability,
                self._obs.public_contract_version)


@dataclass(frozen=True)
class StoreRow:
    event_kind: str
    subject_key: str
    subject_value: str
    occurred_at: dt.datetime
    source_revision: str


@dataclass(frozen=True)
class QueryEvidence:
    source_table: str
    subject_key: str
    subject_value: str
    window_start: dt.datetime
    window_end: dt.datetime
    source_revision: str
    queried_at: dt.datetime
    query_digest: str
    row_count: int
    result_set_digest: str = ""   # v1.5 P0-5: binds the actual rows, not just their count


def query_digest(source_table, subject_key, subject_value, window_start, window_end) -> str:
    """Canonical digest of a QUERY REQUEST. Both the store and the rule compute it independently,
    so evidence that does not bind this exact request is detectable (v1.5 P0-5)."""
    return "sha256:" + hashlib.sha256(json.dumps(
        [source_table, subject_key, subject_value, window_start.isoformat(), window_end.isoformat()],
        sort_keys=True).encode()).hexdigest()


def result_set_digest(rows) -> str:
    """Canonical digest of a RESULT SET, so evidence is bound to the rows actually returned."""
    return "sha256:" + hashlib.sha256(json.dumps(
        [[r.event_kind, r.subject_key, r.subject_value, r.occurred_at.isoformat(), r.source_revision]
         for r in rows], sort_keys=True).encode()).hexdigest()


class TrustedEventStore(Protocol):
    def query(self, source_table: str, subject_key: str, subject_value: str,
              window_start: dt.datetime, window_end: dt.datetime) -> tuple:
        """Return matching rows + immutable query evidence. MUST raise StoreReadError on failure."""

    def expected_query_digest(self, source_table, subject_key, subject_value, ws, we) -> str: ...

    def result_set_digest(self, rows) -> str: ...


class FixtureEventStore:
    """Deterministic in-memory store for contract self-tests.

    NOT the production observability adapter. Any claim derived from it is a contract
    self-test result, never live_operational evidence.
    """

    def __init__(self, rows: dict[str, tuple[StoreRow, ...]] | None = None,
                 source_revision: str = "obs-rev-42",
                 unreadable: frozenset[str] = frozenset(),
                 queried_at: dt.datetime | None = None):
        self._rows = rows or {}
        self._rev = source_revision
        self._unreadable = unreadable
        self._queried_at = queried_at or dt.datetime(2026, 7, 15, 0, 0, 10, tzinfo=dt.timezone.utc)

    def expected_query_digest(self, source_table, subject_key, subject_value, ws, we):
        return query_digest(source_table, subject_key, subject_value, ws, we)

    def result_set_digest(self, rows):
        return result_set_digest(rows)

    def query(self, source_table, subject_key, subject_value, window_start, window_end):
        if source_table in self._unreadable:
            raise StoreReadError(f"trusted store unreadable: {source_table}")
        rows = tuple(
            r for r in self._rows.get(source_table, ())
            if r.subject_key == subject_key and r.subject_value == subject_value
            and window_start <= r.occurred_at <= window_end
        )
        ev = QueryEvidence(source_table=source_table, subject_key=subject_key, subject_value=subject_value,
                           window_start=window_start, window_end=window_end, source_revision=self._rev,
                           queried_at=self._queried_at,
                           query_digest=query_digest(source_table, subject_key, subject_value,
                                                     window_start, window_end),
                           row_count=len(rows), result_set_digest=result_set_digest(rows))
        return rows, ev


# ------------------------------------------------------------- boundary records (v1.5 P0-4)
# v1.5 defect: attest_output took a raw `boundary_row` dict from its caller. HL-15 compared that
# dict to the bundle, so a caller that produced both always agreed with itself. v1.6 takes a
# RECORD ID and resolves it through a runner-owned adapter that validates, reads once and hashes.

@dataclass(frozen=True)
class BoundaryRecord:
    """A boundary record RESOLVED by the runner-owned adapter. Rule code can only ever receive
    this type, never a caller-built dictionary (v1.5 P0-4)."""

    record_id: str
    obj: object
    digest: str
    revision: str


class BoundaryRecordStore:
    """Runner-owned adapter resolving an authoritative boundary record by ID."""

    def __init__(self, records: dict = None, validator=None, unreadable: frozenset = frozenset(),
                 revision: str = "boundary-rev-7"):
        self._records = records or {}
        self._validator = validator
        self._unreadable = unreadable
        self._revision = revision

    def resolve(self, record_id: str) -> "BoundaryRecord":
        """Resolve, validate, read once and hash. Never accepts a caller-supplied row."""
        if record_id in self._unreadable:
            raise StoreReadError(f"boundary record unreadable: {record_id}")
        raw = self._records.get(record_id)
        if raw is None:
            raise StoreReadError(f"boundary record {record_id} does not resolve")
        from core import Artifact
        art = Artifact.from_bytes(f"boundary://{record_id}.json", raw, self._validator)
        return BoundaryRecord(record_id=record_id, obj=art.obj, digest=art.digest,
                              revision=self._revision)


# ------------------------------------------------ change receipts (v1.6 review item 1)
class ChangeReceiptStore:
    """Runner-owned adapter resolving a ChangeAttestationReceipt by ID.

    Codex reproduced: an unrelated, internally contradictory Change report authorised Output with
    decision=allow, because a digest only proves a report exists. Output must instead bind to a
    receipt that records what the Change attestor actually DECIDED.
    """

    def __init__(self, records: dict = None, validator=None, unreadable: frozenset = frozenset(),
                 revision: str = "receipt-rev-3"):
        self._records = records or {}
        self._validator = validator
        self._unreadable = unreadable
        self._revision = revision

    def put(self, raw: bytes) -> str:
        """Immutable, idempotent store contract (v1.7 review item 3).

        The key IS the payload's receipt_id, so a lookup key can never disagree with the record it
        returns. Re-storing identical bytes is a no-op; storing DIFFERENT bytes under an existing id
        raises instead of silently overwriting an attestation.
        """
        import json as _json
        rid = _json.loads(raw.decode("utf-8"))["receipt_id"]
        existing = self._records.get(rid)
        if existing is not None and existing != raw:
            raise StoreReadError(
                f"receipt {rid} already exists with different content: receipts are immutable")
        self._records[rid] = raw
        return rid

    def resolve(self, receipt_id: str) -> "ChangeReceipt":
        if receipt_id in self._unreadable:
            raise StoreReadError(f"change receipt unreadable: {receipt_id}")
        raw = self._records.get(receipt_id)
        if raw is None:
            raise StoreReadError(f"change receipt {receipt_id} does not resolve")
        from core import Artifact
        art = Artifact.from_bytes(f"receipt://{receipt_id}.json", raw, self._validator)
        # v1.7 defect (Codex): the store key could differ from the validated payload's receipt_id,
        # and an allow decision still followed. The key and the payload are now one identity.
        payload_id = art.obj["receipt_id"]
        if payload_id != receipt_id:
            raise StoreReadError(
                f"receipt store key {receipt_id!r} != validated payload receipt_id {payload_id!r}")
        return ChangeReceipt(receipt_id=payload_id, obj=art.obj, digest=art.digest,
                             revision=self._revision)


@dataclass(frozen=True)
class ChangeReceipt:
    receipt_id: str
    obj: object
    digest: str
    revision: str


# ------------------------------------------------ runner authority (v1.6 review item 2)
@dataclass(frozen=True)
class AuthorityContext:
    """Adapters bound by the protected RUNNER when the Attestor is constructed.

    v1.6 defect: adapters were call arguments, so the assessed caller supplied its own authorities.
    Public verification calls now accept only artifact/record IDs; the adapters live here.

    v1.8 defect (Codex): refusing ONE provenance string was the wrong shape of fix. `source` is a
    caller-declared label, so `source="ci_fixture_alias"` was not "test_only_fixture", passed the
    check, and resolved the approved fixture tree through the normal constructor. v1.9 stops reading
    the label on the runner path altogether: `Attestor.for_runner` binds `src_dir/policies` itself
    and never consults `policy_dir` or `source`. Only `Attestor.for_testing_only` honours them.

    So on the runner path these two fields are INERT. They exist for the test path and for evidence
    labelling; they are not, and never were, an authority.

    D-16 unchanged: whether the runner is actually protected is a human/operational decision. This
    type does not pretend Python can enforce it.
    """

    event_store: object
    boundary_store: object
    receipt_store: object
    policy_dir: Path                # READ ONLY BY for_testing_only; the runner binds its own root
    source: str = "runner"          # a declared label for evidence, never an authority
    # v1.9 defect (Codex): registry/contract provenance was self-reported in the runtime document and
    # never observed. The observer is an ADAPTER bound here by the runner, exactly like the stores -
    # never a verification-call argument. The shipped default fails closed.
    public_boundary: object = None

    def observer(self):
        return self.public_boundary or NotConfiguredPublicBoundaryAdapter()

    @property
    def is_test_provenance(self) -> bool:
        return self.source == "test_only_fixture"
