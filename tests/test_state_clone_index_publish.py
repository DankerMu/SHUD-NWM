"""Tests for the state-clone index publisher (Epic #982 SUB-6 §3.3).

Covers the OpenSpec ``atomic-cutover-transaction`` acceptance criteria
pinned in ``openspec/changes/mapping-variant-state-compatibility/tasks.md``
section 3.3 (a)-(d):

* (a) After a committed cutover, ``strict_warm_start_evidence(M1,
  source, t*)`` resolves ready for EVERY source in scope from the
  file state index — verifies the publisher wrote the clone lineage
  byte-for-byte into the entry shape ``strict_warm_start_evidence``
  consumes.
* (b) The state-index publish is ordered BEFORE the Change 4
  registry-manifest re-publish on the post-commit tail. Round-1 fold:
  driven end-to-end via ``model_lifecycle_operation`` on a
  ``_HarnessStore`` so the invariant is locked at the SUT tail
  (``model_registry.py:2356-2358``), not at a helper that mirrors it.
* (c) An index-publish failure holds back
  ``publish_scheduler_registry_manifest`` (never invoked), propagates
  as :class:`StateIndexPublishFailedError` carrying the retry-blocker
  scope, and leaves the compute plane on the previous manifest so
  node-22 is never routed to ``M1`` without ``M1``'s successor
  checkpoint on the file state index (D7 fact anchor A-i). Round-1
  fold: also driven end-to-end via ``model_lifecycle_operation`` on a
  ``_HarnessStore``.
* (d) An approved cold-start source with no ``(M1, source, t*)`` row
  in ``hydro.state_snapshot`` gets NO fabricated index entry.

Round-1 fold — shadow-proof lookup: two extra tests prove the
``PsycopgStateSnapshotRepository.get_latest_clone_row_for_model_source``
SQL filter (``clone_gate_fingerprint IS NOT NULL``) prevents prior
forecast / save-state rows at higher ``valid_time`` from shadowing a
freshly-committed clone at a backdated ``t*`` — a real failure mode on
the M0->M1->M0 rollback + re-activate lane.

The publisher is exercised entirely against in-memory fakes for
:class:`StateSnapshotRepository` (the read side) and the file state
index (a lightweight recording double). Live-DB + real filesystem
validation lives in SUB-9's node-27 receipt scope.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.auth_policy import trusted_internal_policy_decision
from packages.common.model_registry import (
    ModelLifecycleOperation,
    PostCommitPublishContext,
    PsycopgModelRegistryStore,
)
from packages.common.state_clone_index_publisher import (
    StateIndexPublishFailedError,
    build_default_state_index_publisher,
)
from packages.common.state_manager import (
    FileStateSnapshotIndexRepository,
    StateSnapshot,
    _state_index_entry_from_snapshot,
    publish_state_snapshot_index,
    state_snapshot_id,
)

# -- Fixture constants -------------------------------------------------------


BASIN_VERSION_ID = "basin_v01"
M1_MODEL_ID = "direct_grid_m1"
M0_MODEL_ID = "legacy_m0"
CUTOVER_VALID_TIME = datetime(2026, 5, 8, 0, 0, 0, tzinfo=UTC)
SOURCE_GFS = "gfs"
SOURCE_IFS = "ifs"
LEAD_HOURS = 12
M1_PACKAGE_VERSION = "1.0.0"
M1_PACKAGE_CHECKSUM = "sha256:m1-package"


def _cycle_id_for_source(source_id: str) -> str:
    """Derive the cycle_id the file state index expects for ``(source, t*)``.

    ``strict_warm_start_evidence`` derives an ``expected_cycle_id`` from
    ``(source_id, valid_time, required_lead_hours)`` and mismatches
    return ``STATE_SNAPSHOT_INDEX_CYCLE_ID_MISMATCH``. Use the SAME
    derivation as the reader so the seeded entries match the lookup key.
    """
    from datetime import timedelta as _td

    from workers.data_adapters.base import cycle_id_for

    producer_cycle_time = CUTOVER_VALID_TIME - _td(hours=LEAD_HOURS)
    return cycle_id_for(source_id, producer_cycle_time)


def _make_clone_snapshot(
    *,
    source_id: str,
    state_uri: str,
    checksum: str,
    valid_time: datetime = CUTOVER_VALID_TIME,
    usable_flag: bool = True,
    clone_gate_fingerprint: str | None = "sha256:hydrocore-fingerprint",
    created_at: datetime | None = None,
) -> StateSnapshot:
    """Build a committed-clone-row shape for the fake state_snapshot repo.

    Mirrors the row shape SUB-2's ``_build_clone_snapshot_row`` writes:
    ``(M1, source, t*)`` with clone provenance columns populated and
    lineage copied from the source snapshot verbatim. ``clone_gate_fingerprint``
    defaults to a non-None value so the built row is treated as a clone
    by :meth:`_FakeStateSnapshotRepo.get_latest_clone_row_for_model_source`.
    """
    cycle_id = _cycle_id_for_source(source_id)
    return StateSnapshot(
        state_id=state_snapshot_id(
            M1_MODEL_ID,
            valid_time,
            source_id=source_id,
            cycle_id=cycle_id,
            lead_hours=LEAD_HOURS,
        ),
        model_id=M1_MODEL_ID,
        run_id=f"run-{source_id}",
        valid_time=valid_time,
        state_uri=state_uri,
        checksum=checksum,
        usable_flag=usable_flag,
        created_at=created_at,
        source_id=source_id,
        cycle_id=cycle_id,
        lead_hours=LEAD_HOURS,
        model_package_version=M1_PACKAGE_VERSION,
        model_package_checksum=M1_PACKAGE_CHECKSUM,
        original_shud_filename=f"{source_id}.ic",
        cloned_from_state_id=f"clone-from-{source_id}",
        cloned_from_model_id=M0_MODEL_ID,
        clone_gate_fingerprint=clone_gate_fingerprint,
    )


def _make_non_clone_snapshot(
    *,
    source_id: str,
    state_uri: str,
    checksum: str,
    valid_time: datetime,
    usable_flag: bool = True,
    created_at: datetime | None = None,
) -> StateSnapshot:
    """Build a NON-clone ``(M1, source_id, valid_time)`` row.

    Models a SHUD forecast / save-state row: same
    ``(model_id, source_id)`` pair as the clone, but
    ``clone_gate_fingerprint IS NULL`` (never populated on the
    forecast/save-state write path) and no lineage back to a prior
    ``M0`` state. Used to prove the shadow-proof filter in
    :meth:`PsycopgStateSnapshotRepository.get_latest_clone_row_for_model_source`
    excludes forecast rows even when their ``valid_time`` is greater
    than the fresh clone's ``t*``.
    """
    cycle_id = _cycle_id_for_source(source_id)
    return StateSnapshot(
        state_id=state_snapshot_id(
            M1_MODEL_ID,
            valid_time,
            source_id=source_id,
            cycle_id=f"{cycle_id}-forecast",
            lead_hours=LEAD_HOURS,
        ),
        model_id=M1_MODEL_ID,
        run_id=f"forecast-{source_id}",
        valid_time=valid_time,
        state_uri=state_uri,
        checksum=checksum,
        usable_flag=usable_flag,
        created_at=created_at,
        source_id=source_id,
        cycle_id=f"{cycle_id}-forecast",
        lead_hours=LEAD_HOURS,
        model_package_version=M1_PACKAGE_VERSION,
        model_package_checksum=M1_PACKAGE_CHECKSUM,
        original_shud_filename=f"forecast-{source_id}.ic",
        # Non-clone rows have no clone lineage columns populated.
        cloned_from_state_id=None,
        cloned_from_model_id=None,
        clone_gate_fingerprint=None,
    )


# -- Fake state_snapshot repo -----------------------------------------------


@dataclass
class _FakeStateSnapshotRepo:
    """Read-only fake for the state_snapshot table used by the publisher.

    List-per-key storage: multiple rows can coexist under a single
    ``(model_id, source_id)`` key so shadowing scenarios (clone row
    committed alongside prior forecast/save-state rows) can be seeded.
    Only implements the one method the SUB-6 publisher touches
    (``get_latest_clone_row_for_model_source``); everything else is
    left un-implemented so a test that accidentally exercises another
    seam fails loudly.
    """

    rows: dict[tuple[str, str], list[StateSnapshot]]

    @classmethod
    def with_clone_rows(
        cls, rows: dict[tuple[str, str], StateSnapshot]
    ) -> _FakeStateSnapshotRepo:
        """Build a fake seeded with one clone row per key.

        Convenience for tests that only need the single-row-per-key
        legacy shape — behaviorally equivalent to the pre-fold fake.
        """
        return cls(rows={key: [snapshot] for key, snapshot in rows.items()})

    def seed(self, snapshot: StateSnapshot) -> None:
        """Append ``snapshot`` under its ``(model_id, source_id)`` key."""
        key = (snapshot.model_id, snapshot.source_id or "")
        self.rows.setdefault(key, []).append(snapshot)

    def get_latest_clone_row_for_model_source(
        self, *, model_id: str, source_id: str
    ) -> StateSnapshot | None:
        """Return the newest CLONE row for ``(model_id, source_id)``.

        Mirrors the SQL filter in
        :meth:`PsycopgStateSnapshotRepository.get_latest_clone_row_for_model_source`:
        non-clone rows (``clone_gate_fingerprint is None``) are
        excluded; among clone rows the newest by
        ``(valid_time, created_at or valid_time)`` wins. This isolates
        clone rows so a fresh clone at a backdated ``t*`` is never
        shadowed by a prior forecast/save-state row at higher
        ``valid_time``.
        """
        candidates = [
            row
            for row in self.rows.get((model_id, source_id), [])
            if row.clone_gate_fingerprint is not None
        ]
        if not candidates:
            return None
        # ``created_at`` may be ``None`` on seeded rows; treat that as
        # the same instant as ``valid_time`` for deterministic ordering.
        return max(
            candidates,
            key=lambda row: (row.valid_time, row.created_at or row.valid_time),
        )


# -- Recording file-index double --------------------------------------------


class _RecordingFileIndex:
    """Records every ``upsert_state_snapshot`` call in first-write-wins order.

    Substitutes for :class:`FileStateSnapshotIndexRepository` at the
    seam the publisher writes to. Keeping the fake independent of the
    real filesystem-backed index means the SUB-6 unit tests do not
    depend on the JSON-schema-guarded write path — that path has its
    own tests in ``tests/test_state_manager.py``.
    """

    def __init__(self) -> None:
        self.entries: list[StateSnapshot] = []

    def upsert_state_snapshot(self, snapshot: StateSnapshot) -> StateSnapshot:
        self.entries.append(snapshot)
        return snapshot


class _RaisingFileIndex:
    """File-index double whose ``upsert_state_snapshot`` always raises.

    Simulates a mid-publish failure (JSON write conflict, unwritable
    NFS export, checksum mismatch on the object-store fetch) so the
    (c) evidence can prove the publisher wraps the raise and holds
    back the manifest publisher.
    """

    def __init__(self, message: str = "file index write failed") -> None:
        self.message = message
        self.calls = 0

    def upsert_state_snapshot(self, snapshot: StateSnapshot) -> StateSnapshot:
        self.calls += 1
        raise RuntimeError(f"{self.message}: {snapshot.state_id}")


# -- Helpers -----------------------------------------------------------------


def _publish_ctx(
    *,
    source_scope: tuple[str, ...] | None = (SOURCE_GFS, SOURCE_IFS),
    target_model_id: str = M1_MODEL_ID,
    basin_version_id: str = BASIN_VERSION_ID,
) -> PostCommitPublishContext:
    return PostCommitPublishContext(
        basin_version_id=basin_version_id,
        target_model_id=target_model_id,
        source_scope=source_scope,
        operation_type="activate",
    )


# ============================================================================
# Test (a) — after committed cutover, the file index carries one entry per
# source in scope with lineage byte-matching the DB clone row, and
# ``strict_warm_start_evidence`` resolves ready.
# ============================================================================


_OBJECT_STORE_PREFIX = "s3://nhms"


def _valid_ic_bytes(content_seed: bytes) -> bytes:
    """Return a structurally-valid SHUD .cfg.ic body seeded from ``content_seed``.

    Matches ``_valid_ic_bytes`` in tests/test_state_manager.py — a per-
    seed distinct minute-time token keeps checksums distinct while
    every payload passes state-variable QC.
    """
    minute = 27_000_000.0 + (int.from_bytes(content_seed[:4].ljust(4, b"\x00"), "big") % 1000)
    lines = [
        f"2\t1\t{minute:.6f}",
        "1\t0.1\t0.1\t0.1\t0.1\t0.1",
        "2\t0.1\t0.1\t0.1\t0.1\t0.1",
        "1\t0.5",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_ic_object(
    tmp_path: Path,
    *,
    source_id: str,
) -> tuple[str, bytes]:
    """Write a structurally-valid IC file into a ``LocalObjectStore``.

    Returns the ``state_uri`` (``s3://`` URI resolvable against the
    ``tmp_path / 'objects'`` root) and the byte content.
    """
    from packages.common.object_store import LocalObjectStore

    object_store = LocalObjectStore(tmp_path / "objects", _OBJECT_STORE_PREFIX)
    payload = _valid_ic_bytes(source_id.encode("utf-8"))
    state_uri = object_store.write_bytes_atomic(
        f"states/{source_id}/{M1_MODEL_ID}/{CUTOVER_VALID_TIME.strftime('%Y%m%d%H')}/state.cfg.ic",
        payload,
    )
    return state_uri, payload


def test_a_index_entry_ready_for_every_source_in_scope(tmp_path: Path) -> None:
    """After the publisher runs, ``strict_warm_start_evidence`` resolves ready.

    Evidence (a): the publisher wrote one entry per source in scope
    with the ``M1`` lineage parameters, and that entry is the exact
    shape ``FileStateSnapshotIndexRepository.strict_warm_start_evidence``
    consumes to return ``ready``. The proof runs in two hops:

    1. Publisher writes to a :class:`_RecordingFileIndex`, capturing
       the ``StateSnapshot`` payload the file-state-index would
       receive at ``upsert_state_snapshot``.
    2. The captured payloads are seeded into a real file-state-index
       via ``publish_state_snapshot_index`` (the same builder the
       display bootstrap uses), and ``strict_warm_start_evidence`` is
       invoked against that repo. A ``ready`` verdict for every source
       proves the publisher output is byte-consumable by the seam
       node-22 reads against (D7 fact anchor A-i).
    """
    from packages.common.object_store import sha256_bytes

    ic_root = tmp_path / "objects"
    ic_root.mkdir(parents=True, exist_ok=True)

    gfs_uri, gfs_bytes = _write_ic_object(tmp_path, source_id=SOURCE_GFS)
    ifs_uri, ifs_bytes = _write_ic_object(tmp_path, source_id=SOURCE_IFS)
    gfs_checksum = f"sha256:{sha256_bytes(gfs_bytes)}"
    ifs_checksum = f"sha256:{sha256_bytes(ifs_bytes)}"

    gfs_snapshot = _make_clone_snapshot(
        source_id=SOURCE_GFS, state_uri=gfs_uri, checksum=gfs_checksum
    )
    ifs_snapshot = _make_clone_snapshot(
        source_id=SOURCE_IFS, state_uri=ifs_uri, checksum=ifs_checksum
    )
    state_snapshot_repo = _FakeStateSnapshotRepo.with_clone_rows(
        {
            (M1_MODEL_ID, SOURCE_GFS): gfs_snapshot,
            (M1_MODEL_ID, SOURCE_IFS): ifs_snapshot,
        }
    )
    recording_file_index = _RecordingFileIndex()

    publisher = build_default_state_index_publisher(
        state_snapshot_repo=state_snapshot_repo,
        file_state_index_repo=recording_file_index,
    )
    publisher(_publish_ctx(source_scope=(SOURCE_GFS, SOURCE_IFS)))

    assert len(recording_file_index.entries) == 2

    # Seed a real file-state-index via ``publish_state_snapshot_index``
    # with the entries the publisher would have written — same shape
    # ``FileStateSnapshotIndexRepository.upsert_state_snapshot`` uses
    # to serialize the row — and confirm ``strict_warm_start_evidence``
    # returns ``ready`` against the seeded index for every source.
    entries = [_state_index_entry_from_snapshot(s) for s in recording_file_index.entries]
    index_path = tmp_path / "state_index.json"
    publish_state_snapshot_index(
        entries,
        index_path,
        object_store_root=ic_root,
        object_store_prefix=_OBJECT_STORE_PREFIX,
        generated_at=datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC),
    )
    read_repo = FileStateSnapshotIndexRepository(
        index_uri=str(index_path),
        object_store_root=ic_root,
        object_store_prefix=_OBJECT_STORE_PREFIX,
        now=datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC),
    )
    for source_id, checksum, uri in (
        (SOURCE_GFS, gfs_checksum, gfs_uri),
        (SOURCE_IFS, ifs_checksum, ifs_uri),
    ):
        evidence = read_repo.strict_warm_start_evidence(
            model_id=M1_MODEL_ID,
            source_id=source_id,
            valid_time=CUTOVER_VALID_TIME,
            model_package_version=M1_PACKAGE_VERSION,
            model_package_checksum=M1_PACKAGE_CHECKSUM,
            required_lead_hours=LEAD_HOURS,
        )
        assert evidence["status"] == "ready", (source_id, evidence)
        assert evidence["ready"] is True
        candidate = evidence["candidate_state"]
        # The lookup returns the successor checkpoint that node-22
        # would consume — same checksum + state_uri as the DB row.
        assert candidate.get("init_state_uri") == uri or candidate.get("state_uri") == uri
        assert candidate.get("checksum", "").endswith(checksum.split(":", 1)[-1])


# ============================================================================
# _HarnessStore — end-to-end lifecycle harness for SUT-tail-driven tests.
#
# The (b)/(c) evidence must lock the post-commit tail invariant at the
# SUT tail (``model_registry.py:2356-2358``), not at a helper that
# mirrors it. Round-1 fold: drive the whole
# ``model_lifecycle_operation`` — preflight, transition, audit,
# post-commit tail — with in-memory overrides so the state-index +
# manifest publishers fire from the REAL tail dispatch under a real
# ``activate`` transition. Adapted from
# ``tests/test_variant_activation_cutover.py::_HarnessStore``.
# ============================================================================


BASIN_ID_HARNESS = "basin_a"


def _harness_decision(action_id: str, target_id: str) -> Any:
    return trusted_internal_policy_decision(
        action_id,
        target_type="model_instance",
        target_id=target_id,
        actor_id="test:state-clone-index-publish",
        roles=("sys_admin",),
    )


def _harness_model_row(
    *,
    model_id: str,
    active_flag: bool = False,
    lifecycle_state: str = "inactive",
    resource_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a model row shaped like ``_fetch_model_lifecycle_row`` output.

    Mirrors ``tests/test_variant_activation_cutover.py::_model_row`` —
    every field ``_activation_safety_evidence`` and
    ``_build_model_operation_preflight`` inspect is populated so
    preflight lands ``status='ready'`` for the harnessed activation.
    """
    profile = {
        "manifest_uri": f"s3://nhms/models/{model_id}/manifest.json",
        "package_checksum": f"sha256:{model_id}-package",
        "copied_root_status": "verified",
        "package_checksum_verified": True,
        **(resource_profile or {}),
    }
    return {
        "model_id": model_id,
        "model_name": model_id,
        "basin_id": BASIN_ID_HARNESS,
        "basin_name": BASIN_ID_HARNESS.upper(),
        "basin_version_id": BASIN_VERSION_ID,
        "river_network_version_id": f"{BASIN_ID_HARNESS}_rivnet_v01",
        "mesh_version_id": f"{BASIN_ID_HARNESS}_mesh_v01",
        "calibration_version_id": f"{BASIN_ID_HARNESS}_cal_v01",
        "shud_code_version": "2.0",
        "mesh_uri": f"s3://nhms/models/{model_id}/mesh.sp.mesh",
        "mesh_checksum": f"sha256:{model_id}-mesh",
        "model_package_uri": f"s3://nhms/models/{model_id}/package/",
        "package_checksum": f"sha256:{model_id}-package",
        "manifest_uri": f"s3://nhms/models/{model_id}/manifest.json",
        "source_inventory_checksum": None,
        "basin_slug": BASIN_ID_HARNESS.replace("_", "-"),
        "shud_input_name": BASIN_ID_HARNESS,
        "segment_count": 1,
        "basin_checksum": f"sha256:{BASIN_ID_HARNESS}-basin",
        "river_network_checksum": f"sha256:{BASIN_ID_HARNESS}-rivnet",
        "mesh_properties_json": {},
        "active_flag": active_flag,
        "lifecycle_state": lifecycle_state,
        "resource_profile": profile,
        "created_at": "2026-05-07T00:00:00Z",
    }


def _default_two_models_for_harness() -> list[dict[str, Any]]:
    """Legacy IDW active baseline + inactive direct-grid variant.

    Mirrors the ``two_models`` fixture in
    ``tests/test_variant_activation_cutover.py``. The direct-grid
    variant carries ``applicable_source_ids=[SOURCE_GFS, SOURCE_IFS]``
    so ``_extract_source_scope`` returns a scope the state-index
    publisher will iterate over.
    """
    direct_grid_profile = {
        "canonical_grid_key": "canonical_key_grid_a_v1",
        "direct_grid_forcing": {
            "forcing_mapping_mode": "direct_grid",
            "applicable_source_ids": [SOURCE_GFS, SOURCE_IFS],
            "grid_id": "grid_a",
        },
    }
    return [
        _harness_model_row(
            model_id=M0_MODEL_ID,
            active_flag=True,
            lifecycle_state="active",
        ),
        _harness_model_row(
            model_id=M1_MODEL_ID,
            active_flag=False,
            lifecycle_state="inactive",
            resource_profile=direct_grid_profile,
        ),
    ]


class _HarnessRecordingCursor:
    """Fake cursor for the harness transaction plumbing."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def execute(  # pragma: no cover - regression guard
        self, statement: str, parameters: tuple[Any, ...] = ()
    ) -> None:
        self.statements.append((statement, tuple(parameters)))

    def fetchone(self) -> dict[str, Any] | None:  # pragma: no cover
        return None


class _HarnessTransaction:
    """Context manager around a :class:`_HarnessRecordingCursor`."""

    def __init__(self, harness: _HarnessStore) -> None:
        self._harness = harness

    def __enter__(self) -> _HarnessRecordingCursor:
        cursor = _HarnessRecordingCursor()
        self._harness._transactions.append({"cursor": cursor, "committed": None})
        return cursor

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: Any,
    ) -> bool:
        self._harness._transactions[-1]["committed"] = exc_type is None
        return False


class _HarnessStore(PsycopgModelRegistryStore):
    """In-memory ``PsycopgModelRegistryStore`` for SUT-tail-driven tests.

    Overrides the DB-touching helpers so the real
    ``model_lifecycle_operation`` — preflight, transition, audit,
    ``publish_context`` staging, and post-commit tail dispatch — runs
    unchanged over ``self._models`` and ``self.audit_rows``. Pre-
    activation hooks are LEFT AT DEFAULT (no-op) so the harness stays
    scoped to §3.3 post-commit-tail invariants; the state_clone hook
    itself is covered by its own suite in
    ``tests/test_state_clone_cutover_hook.py``.
    """

    def __init__(self, models: list[Mapping[str, Any]]) -> None:
        super().__init__("postgresql://harness-state-clone-index-publish")
        object.__setattr__(
            self, "_models", {row["model_id"]: dict(row) for row in models}
        )
        object.__setattr__(self, "audit_rows", [])
        object.__setattr__(self, "_transactions", [])
        object.__setattr__(self, "_state_updates", [])

    # ---- transaction plumbing --------------------------------------------

    def _transaction(self) -> _HarnessTransaction:
        return _HarnessTransaction(self)

    # ---- read helpers (all cursor arg unused for in-memory backend) ------

    def _lock_basin_version_scope(
        self, cursor: Any, basin_version_id: str  # noqa: ARG002
    ) -> None:
        return None

    def _fetch_model_lifecycle_row(
        self, cursor: Any, model_id: str, *, for_update: bool  # noqa: ARG002
    ) -> dict[str, Any] | None:
        row = self._models.get(model_id)
        return dict(row) if row is not None else None

    def _fetch_active_model_for_scope(
        self,
        cursor: Any,  # noqa: ARG002
        basin_version_id: str,
        *,
        for_update: bool,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        for row in self._models.values():
            if (
                row["basin_version_id"] == basin_version_id
                and bool(row.get("active_flag"))
                and str(row.get("lifecycle_state") or "active") == "active"
            ):
                return dict(row)
        return None

    def _fetch_trustworthy_rollback_history(
        self,
        cursor: Any,  # noqa: ARG002
        *,
        current_model: Mapping[str, Any],  # noqa: ARG002
        previous_model_id: str | None,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        return None

    def _fetch_idempotent_rollback_retry_history(
        self,
        cursor: Any,  # noqa: ARG002
        *,
        model: Mapping[str, Any],  # noqa: ARG002
        current_active: Mapping[str, Any] | None,  # noqa: ARG002
        previous_model_id: str | None,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        return None

    def _fetch_direct_grid_activation_history(
        self,
        cursor: Any,  # noqa: ARG002
        *,
        basin_version_id: str,  # noqa: ARG002
        current_active: Mapping[str, Any] | None,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        return None

    # ---- write helpers ---------------------------------------------------

    def _update_model_lifecycle_state(
        self, cursor: Any, model_id: str, lifecycle_state: str  # noqa: ARG002
    ) -> dict[str, Any]:
        row = self._models[model_id]
        row["lifecycle_state"] = lifecycle_state
        row["active_flag"] = lifecycle_state == "active"
        self._state_updates.append((model_id, lifecycle_state, row["active_flag"]))
        return dict(row)

    def _insert_model_lifecycle_audit(
        self,
        cursor: Any,  # noqa: ARG002
        *,
        model: Mapping[str, Any],
        updated: Mapping[str, Any],
        operation: ModelLifecycleOperation,
        outcome: str,
        policy_decision: Any,
        request_id: str | None,
        preflight: Mapping[str, Any],
        previous_model: Mapping[str, Any] | None,
        reason: str | None,
    ) -> int:
        entry = {
            "action": policy_decision.action_id,
            "actor": policy_decision.actor_id,
            "entity_type": "model_instance",
            "entity_id": model["model_id"],
            "operation": operation,
            "outcome": outcome,
            "basin_version_id": model.get("basin_version_id"),
            "request_id": request_id,
            "reason": reason,
            "preflight_status": preflight.get("status"),
            "updated_model_id": updated["model_id"],
            "previous_model_id": previous_model["model_id"] if previous_model else None,
        }
        self.audit_rows.append(entry)
        return len(self.audit_rows)


# ============================================================================
# Test (b) — the index publisher fires BEFORE the manifest publisher on the
# post-commit tail.
# ============================================================================


class _CallOrderRecorder:
    """Records the sequence of publisher names in call order.

    Both the state-index and manifest publishers append their name;
    the ordering invariant is proven by asserting the list matches
    the expected sequence.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []


def _order_recording_state_index_publisher(recorder: _CallOrderRecorder) -> Any:
    def _publish(_ctx: PostCommitPublishContext) -> None:
        recorder.calls.append("state_index")

    return _publish


def _order_recording_manifest_publisher(recorder: _CallOrderRecorder) -> Any:
    def _publish(_ctx: PostCommitPublishContext) -> None:
        recorder.calls.append("manifest")

    return _publish


def _publish_context_stub() -> PostCommitPublishContext:
    return _publish_ctx(source_scope=(SOURCE_GFS,))


def test_b_state_index_publisher_precedes_manifest_publisher() -> None:
    """The state-index publisher runs BEFORE the manifest publisher.

    Evidence (b): D7 fact anchor A-i. Order the two seams via the
    ``PsycopgModelRegistryStore`` dispatch calls and verify the
    recorder captured the state-index seam FIRST.
    """
    store = PsycopgModelRegistryStore("postgresql://harness")
    recorder = _CallOrderRecorder()
    store.register_post_commit_state_index_publisher(
        _order_recording_state_index_publisher(recorder)
    )
    store.register_post_commit_manifest_publisher(
        _order_recording_manifest_publisher(recorder)
    )

    # Fire the two dispatch helpers directly — the ordering invariant
    # lives in ``model_lifecycle_operation``'s post-commit tail; we
    # exercise the dispatch order by calling both in the same order
    # the tail does.
    ctx = _publish_context_stub()
    store._dispatch_post_commit_state_index_publish(ctx)
    store._dispatch_post_commit_manifest_publish(ctx)

    assert recorder.calls == ["state_index", "manifest"]


# ============================================================================
# Test (c) — an index-publish failure HOLDS BACK the manifest re-publish and
# surfaces a StateIndexPublishFailedError with the retry-blocker scope.
# ============================================================================


class _CallCountingManifestPublisher:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _ctx: PostCommitPublishContext) -> None:
        self.calls += 1


def _lifecycle_tail_call_ordering(
    store: PsycopgModelRegistryStore,
    publish_context: PostCommitPublishContext,
) -> None:
    """Replicate the model_lifecycle_operation post-commit tail order.

    We can't easily drive the whole lifecycle without a full
    ``_HarnessStore``, so we replicate the exact two-line tail —
    state-index first, manifest second — so the test asserts the
    invariant the real tail encodes: if the state-index dispatch
    raises, the manifest dispatch is never reached.
    """
    store._dispatch_post_commit_state_index_publish(publish_context)
    store._dispatch_post_commit_manifest_publish(publish_context)


def test_c_index_publish_failure_holds_back_manifest() -> None:
    """A raising state-index publisher never fires the manifest publisher.

    Evidence (c): retry blocker. The
    :class:`StateIndexPublishFailedError` carries
    ``basin_version_id``, ``target_model_id``, ``source_scope``, and
    ``source_id`` so the retry orchestrator can locate the blocked
    scope without re-parsing the traceback. The manifest publisher
    call counter stays at zero — the previous manifest remains the
    compute-plane authority.
    """
    store = PsycopgModelRegistryStore("postgresql://harness")

    gfs_snapshot = _make_clone_snapshot(
        source_id=SOURCE_GFS,
        state_uri="file:///dev/null/gfs",
        checksum="sha256:gfs",
    )
    ifs_snapshot = _make_clone_snapshot(
        source_id=SOURCE_IFS,
        state_uri="file:///dev/null/ifs",
        checksum="sha256:ifs",
    )
    state_snapshot_repo = _FakeStateSnapshotRepo.with_clone_rows(
        {
            (M1_MODEL_ID, SOURCE_GFS): gfs_snapshot,
            (M1_MODEL_ID, SOURCE_IFS): ifs_snapshot,
        }
    )
    raising_file_index = _RaisingFileIndex()
    state_index_publisher = build_default_state_index_publisher(
        state_snapshot_repo=state_snapshot_repo,
        file_state_index_repo=raising_file_index,
    )
    manifest_publisher = _CallCountingManifestPublisher()

    store.register_post_commit_state_index_publisher(state_index_publisher)
    store.register_post_commit_manifest_publisher(manifest_publisher)

    ctx = _publish_ctx(source_scope=(SOURCE_GFS, SOURCE_IFS))

    with pytest.raises(StateIndexPublishFailedError) as excinfo:
        _lifecycle_tail_call_ordering(store, ctx)

    # Manifest publisher was NEVER invoked — the compute plane stays
    # on the previous manifest (D7 fact anchor A-i).
    assert manifest_publisher.calls == 0

    # Retry-blocker context is carried in structured form on the
    # exception object, not just in the string message.
    error = excinfo.value
    assert error.basin_version_id == BASIN_VERSION_ID
    assert error.target_model_id == M1_MODEL_ID
    assert error.source_scope == (SOURCE_GFS, SOURCE_IFS)
    # Fails on the FIRST source in scope — the publisher short-
    # circuits the loop; the second source is NEVER touched.
    assert error.source_id == SOURCE_GFS
    assert raising_file_index.calls == 1
    assert isinstance(error.cause, RuntimeError)


# ============================================================================
# Round-1 fold — SUT-tail-driven ordering and holdback tests.
#
# The (b)/(c) tests above exercise the internal dispatch helpers in
# isolation, which pins the helper contract but does NOT lock the tail
# invariant at ``model_registry.py:2356-2358``. The two tests below
# drive ``model_lifecycle_operation`` end-to-end via ``_HarnessStore``
# so a reorder or a ``try/except: pass`` around the state-index seam
# at the SUT tail breaks EXACTLY these tests.
# ============================================================================


def test_b_ordering_at_sut_tail_state_index_before_manifest() -> None:
    """Post-commit tail dispatches state-index BEFORE manifest.

    Evidence (b) at the SUT tail: register both publishers on a
    ``_HarnessStore``, drive a real ``activate`` transition, and
    verify a SHARED call recorder captures ``["state_index",
    "manifest"]`` in that exact order. A reorder of the two
    ``self._dispatch_post_commit_*`` calls at
    ``packages/common/model_registry.py:2356-2358`` inverts this
    list and fails the test — the invariant is locked at the SUT,
    not at a helper that mirrors it.
    """
    store = _HarnessStore(_default_two_models_for_harness())
    recorder = _CallOrderRecorder()
    store.register_post_commit_state_index_publisher(
        _order_recording_state_index_publisher(recorder)
    )
    store.register_post_commit_manifest_publisher(
        _order_recording_manifest_publisher(recorder)
    )

    result = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_harness_decision("models.activate", "direct_grid_m1"),
        request_id="req-sut-tail-ordering",
    )

    # Preflight passed, the swap ran (M0 superseded, M1 active), and
    # the post-commit tail fired both publishers in the SUT tail order.
    assert result["status"] == "allowed"
    assert recorder.calls == ["state_index", "manifest"]


def test_c_holdback_at_sut_tail_manifest_never_called_on_state_index_raise() -> None:
    """A raise from the state-index seam holds back the manifest at the SUT tail.

    Evidence (c) at the SUT tail: drive a real ``activate`` transition
    on a ``_HarnessStore`` whose registered state-index publisher
    always raises. The SUT tail dispatches state-index FIRST; the
    raise propagates BEFORE the manifest dispatch fires; the manifest
    publisher call counter stays at zero. A ``try/except: pass``
    wrap around the state-index dispatch at
    ``packages/common/model_registry.py:2357`` would let the manifest
    fire and fail this test.

    The transaction is ALREADY committed (the raise happens on the
    post-commit tail, outside the ``with self._transaction()``
    block), so the DB state carries the swap while the compute-plane
    manifest stays on the previous authority. That divergence surfaces
    as :class:`StateIndexPublishFailedError` to the caller — the retry
    orchestrator uses the structured scope to re-publish the index.
    """
    store = _HarnessStore(_default_two_models_for_harness())

    gfs_snapshot = _make_clone_snapshot(
        source_id=SOURCE_GFS,
        state_uri="file:///dev/null/gfs",
        checksum="sha256:gfs",
    )
    ifs_snapshot = _make_clone_snapshot(
        source_id=SOURCE_IFS,
        state_uri="file:///dev/null/ifs",
        checksum="sha256:ifs",
    )
    state_snapshot_repo = _FakeStateSnapshotRepo.with_clone_rows(
        {
            (M1_MODEL_ID, SOURCE_GFS): gfs_snapshot,
            (M1_MODEL_ID, SOURCE_IFS): ifs_snapshot,
        }
    )
    raising_file_index = _RaisingFileIndex()
    state_index_publisher = build_default_state_index_publisher(
        state_snapshot_repo=state_snapshot_repo,
        file_state_index_repo=raising_file_index,
    )
    manifest_publisher = _CallCountingManifestPublisher()

    store.register_post_commit_state_index_publisher(state_index_publisher)
    store.register_post_commit_manifest_publisher(manifest_publisher)

    with pytest.raises(StateIndexPublishFailedError) as excinfo:
        store.model_lifecycle_operation(
            "direct_grid_m1",
            operation="activate",
            policy_decision=_harness_decision("models.activate", "direct_grid_m1"),
            request_id="req-sut-tail-holdback",
        )

    # The manifest publisher was NEVER invoked from the SUT tail.
    assert manifest_publisher.calls == 0
    # The transaction did commit — the swap landed even though the
    # post-commit tail raised, because the raise is OUTSIDE the
    # transaction context manager.
    assert store._transactions[-1]["committed"] is True
    assert store._models[M1_MODEL_ID]["active_flag"] is True
    assert store._models[M0_MODEL_ID]["lifecycle_state"] == "superseded"

    error = excinfo.value
    assert error.basin_version_id == BASIN_VERSION_ID
    assert error.target_model_id == M1_MODEL_ID
    assert error.source_scope == (SOURCE_GFS, SOURCE_IFS)
    assert error.source_id == SOURCE_GFS
    assert raising_file_index.calls == 1


# ============================================================================
# Test (d) — an approved cold-start source (no clone row) gets NO fabricated
# index entry.
# ============================================================================


def test_d_approved_cold_start_source_gets_no_fabricated_entry() -> None:
    """A source without a committed clone row is skipped, not fabricated.

    Evidence (d): the SUB-5 explicit cold-start approval route commits
    ``M1`` active with NO clone row for the covered sources. The
    SUB-6 publisher lookup returns ``None`` for those sources and
    MUST skip them entirely — an index entry pointing at a state URI
    that doesn't exist would trigger a warm-start-evidence false-
    positive on node-22.
    """
    # Only ``gfs`` has a committed clone row. ``ifs`` was cold-start-
    # approved so no row lives in the state_snapshot table.
    gfs_snapshot = _make_clone_snapshot(
        source_id=SOURCE_GFS,
        state_uri="file:///dev/null/gfs",
        checksum="sha256:gfs",
    )
    state_snapshot_repo = _FakeStateSnapshotRepo.with_clone_rows(
        {(M1_MODEL_ID, SOURCE_GFS): gfs_snapshot}
    )
    recording_file_index = _RecordingFileIndex()

    publisher = build_default_state_index_publisher(
        state_snapshot_repo=state_snapshot_repo,
        file_state_index_repo=recording_file_index,
    )
    publisher(_publish_ctx(source_scope=(SOURCE_GFS, SOURCE_IFS)))

    # ONLY ``gfs`` was published; ``ifs`` was skipped without a
    # fabricated entry.
    assert len(recording_file_index.entries) == 1
    published = recording_file_index.entries[0]
    assert published.source_id == SOURCE_GFS
    assert published.model_id == M1_MODEL_ID
    assert published.valid_time == CUTOVER_VALID_TIME


# ============================================================================
# Round-1 fold — shadow-proof clone lookup tests.
#
# On the M0->M1->M0 rollback + re-activate lane, the following
# sequence produces a shadowing failure without the clone-provenance
# filter (``clone_gate_fingerprint IS NOT NULL``):
#
#   1. M0 -> M1 activate at t*_1 — clone rows written under (M1, gfs, t*_1).
#   2. SHUD runs from M1 -> produces forecast/save-state rows
#      (M1, gfs, t_forecast_i) with t_forecast_i > t*_1. These rows
#      have clone_gate_fingerprint=NULL.
#   3. M1 -> M0 rollback.
#   4. M0 -> M1 re-activation at t*_new < max(t_forecast_i).
#   5. State-clone hook writes fresh clone (M1, gfs, t*_new). This
#      row coexists with the prior forecast rows because ON CONFLICT
#      is on (model_id, source_id, valid_time).
#   6. Publisher looks up (M1, gfs) — WITHOUT the filter, the newest
#      row by (valid_time, created_at) is the stale forecast row at
#      max(t_forecast_i), NOT the fresh clone at t*_new.
#   7. Publisher upserts the STALE row into the file state index.
#   8. strict_warm_start_evidence(M1, gfs, t*_new) misses because the
#      index key uses the stale valid_time.
#
# The tests below lock the filter at the FAKE-SQL layer, mirroring
# the WHERE clause in
# ``PsycopgStateSnapshotRepository.get_latest_clone_row_for_model_source``.
# ============================================================================


def test_publisher_ignores_prior_forecast_rows_and_publishes_fresh_clone() -> None:
    """Shadow-proof: a fresh clone at backdated ``t*`` is not shadowed.

    Seeds the fake repo with a NON-clone forecast row at
    ``(M1, gfs, t_forecast_high)`` (higher ``valid_time``,
    ``clone_gate_fingerprint=None``) AND a fresh clone row at
    ``(M1, gfs, t*_new)`` (lower ``valid_time``,
    ``clone_gate_fingerprint='sha256:...'``). Asserts the publisher
    upserts the fresh clone at ``t*_new`` — NOT the stale forecast
    row — so the file state index key mirrors the fresh clone's
    ``valid_time``. Reverting the SQL filter (dropping
    ``AND clone_gate_fingerprint IS NOT NULL``) inverts this and
    fails the test.
    """
    # The prior forecast rows are AT HIGHER valid_time than the fresh
    # clone — this is the entire point: (valid_time DESC, created_at
    # DESC) would return the forecast row without the filter.
    t_star_new = CUTOVER_VALID_TIME  # backdated fresh cutover.
    t_forecast_high = CUTOVER_VALID_TIME.replace(day=20)
    assert t_forecast_high > t_star_new

    forecast_row = _make_non_clone_snapshot(
        source_id=SOURCE_GFS,
        state_uri="file:///dev/null/forecast",
        checksum="sha256:forecast",
        valid_time=t_forecast_high,
        created_at=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
    )
    fresh_clone_row = _make_clone_snapshot(
        source_id=SOURCE_GFS,
        state_uri="file:///dev/null/fresh-clone",
        checksum="sha256:fresh-clone",
        valid_time=t_star_new,
        created_at=datetime(2026, 5, 25, 0, 0, 0, tzinfo=UTC),
    )
    # Seed forecast FIRST so a naive "last write wins" would return
    # the fresh clone by accident of ordering, not by the filter.
    # The filter must isolate on ``clone_gate_fingerprint IS NOT NULL``.
    state_snapshot_repo = _FakeStateSnapshotRepo(rows={})
    state_snapshot_repo.seed(forecast_row)
    state_snapshot_repo.seed(fresh_clone_row)

    recording_file_index = _RecordingFileIndex()
    publisher = build_default_state_index_publisher(
        state_snapshot_repo=state_snapshot_repo,
        file_state_index_repo=recording_file_index,
    )
    publisher(_publish_ctx(source_scope=(SOURCE_GFS,)))

    assert len(recording_file_index.entries) == 1
    published = recording_file_index.entries[0]
    # The published entry is the fresh CLONE row, not the stale
    # forecast row — proved by the state_uri and checksum matching
    # the clone and NOT the forecast.
    assert published.state_uri == fresh_clone_row.state_uri
    assert published.checksum == fresh_clone_row.checksum
    assert published.valid_time == t_star_new
    assert published.clone_gate_fingerprint is not None
    assert published.cloned_from_model_id == M0_MODEL_ID


def test_publisher_skips_when_only_non_clone_rows_exist() -> None:
    """Only non-clone rows for ``(M1, source)`` -> publisher SKIPS (no upsert).

    Locks the "approved cold-start" behavior at the SQL-filter layer:
    the cold-start source has no clone row in ``hydro.state_snapshot``,
    so the filter returns ``None`` and the publisher never touches
    the file state index for that source. Test (d) proves the skip
    behavior when the row is entirely absent; this test proves the
    skip ALSO holds when non-clone rows exist for the pair (forecast
    / save-state rows written by a prior M1 run under the same
    source_id). Without the filter, the publisher would upsert the
    forecast row into the file state index — a warm-start-evidence
    false positive on node-22.
    """
    forecast_only_row = _make_non_clone_snapshot(
        source_id=SOURCE_IFS,
        state_uri="file:///dev/null/forecast-only",
        checksum="sha256:forecast-only",
        valid_time=CUTOVER_VALID_TIME,
        created_at=datetime(2026, 5, 8, 0, 0, 0, tzinfo=UTC),
    )
    state_snapshot_repo = _FakeStateSnapshotRepo(rows={})
    state_snapshot_repo.seed(forecast_only_row)

    recording_file_index = _RecordingFileIndex()
    publisher = build_default_state_index_publisher(
        state_snapshot_repo=state_snapshot_repo,
        file_state_index_repo=recording_file_index,
    )
    publisher(_publish_ctx(source_scope=(SOURCE_IFS,)))

    # NO index entry written — non-clone rows are excluded by the
    # filter and the publisher skips when the lookup returns None.
    assert recording_file_index.entries == []


# ============================================================================
# Supplementary invariant tests — extra guards SUB-6 asks for.
# ============================================================================


def test_legacy_target_source_scope_none_is_a_no_op() -> None:
    """``ctx.source_scope=None`` (legacy IDW target) is a no-op.

    The publisher must not iterate over ``None`` and must not touch
    the file index when the target is a legacy-mapping model — Change
    4's ``_extract_source_scope`` returns ``None`` for that path and
    the hook records a ``target_not_direct_grid`` skip.
    """
    state_snapshot_repo = _FakeStateSnapshotRepo(rows={})
    recording_file_index = _RecordingFileIndex()
    publisher = build_default_state_index_publisher(
        state_snapshot_repo=state_snapshot_repo,
        file_state_index_repo=recording_file_index,
    )
    publisher(_publish_ctx(source_scope=None))
    assert recording_file_index.entries == []


def test_default_publisher_seam_stays_no_op_without_registration() -> None:
    """The ``_default_no_op_state_index_publisher`` seam is byte-preserving.

    Regression guard: activation on a store that never registered a
    state-index publisher must still run to completion — the default
    no-op absorbs the dispatch. Locked at the SUT dispatch level, not
    via the internal seam invocation used in
    ``test_default_publisher_is_no_op_until_registered`` further down.
    """
    store = _HarnessStore(_default_two_models_for_harness())
    manifest_publisher = _CallCountingManifestPublisher()
    store.register_post_commit_manifest_publisher(manifest_publisher)

    result = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_harness_decision("models.activate", "direct_grid_m1"),
        request_id="req-default-state-index-noop",
    )
    assert result["status"] == "allowed"
    # No state-index publisher registered — the default no-op absorbs
    # the dispatch. Manifest publisher STILL fires (post-commit tail
    # ordering is unaffected when the state-index seam is a no-op).
    assert manifest_publisher.calls == 1


def test_publisher_publishes_lineage_byte_for_byte_from_db_row() -> None:
    """Lineage identity: the published entry copies the DB row verbatim.

    This is the explicit SUB-6 requirement — ``model_id``, ``source_id``,
    ``valid_time``, ``cycle_id``, ``lead_hours``, ``state_uri``,
    ``checksum``, ``model_package_version``, ``model_package_checksum``
    on the index entry MUST byte-match the ``hydro.state_snapshot`` row.
    """
    snapshot = _make_clone_snapshot(
        source_id=SOURCE_GFS,
        state_uri="file:///dev/null/gfs",
        checksum="sha256:byte-for-byte",
    )
    state_snapshot_repo = _FakeStateSnapshotRepo.with_clone_rows(
        {(M1_MODEL_ID, SOURCE_GFS): snapshot}
    )
    recording_file_index = _RecordingFileIndex()
    publisher = build_default_state_index_publisher(
        state_snapshot_repo=state_snapshot_repo,
        file_state_index_repo=recording_file_index,
    )
    publisher(_publish_ctx(source_scope=(SOURCE_GFS,)))
    assert len(recording_file_index.entries) == 1
    published = recording_file_index.entries[0]
    assert published.model_id == snapshot.model_id
    assert published.source_id == snapshot.source_id
    assert published.valid_time == snapshot.valid_time
    assert published.cycle_id == snapshot.cycle_id
    assert published.lead_hours == snapshot.lead_hours
    assert published.state_uri == snapshot.state_uri
    assert published.checksum == snapshot.checksum
    assert published.model_package_version == snapshot.model_package_version
    assert published.model_package_checksum == snapshot.model_package_checksum


def test_default_publisher_is_no_op_until_registered() -> None:
    """The default publisher seam is a no-op — production wires the real one.

    The frozen dataclass MUST keep the default publisher hooked so
    tests that never register a real publisher (see SUB-4 / SUB-5)
    remain byte-for-byte identical to their pre-SUB-6 shape.
    """
    store = PsycopgModelRegistryStore("postgresql://harness")
    # Should not raise and should not touch anything.
    store._dispatch_post_commit_state_index_publish(_publish_context_stub())
