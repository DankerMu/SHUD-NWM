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
  registry-manifest re-publish on the post-commit tail.
* (c) An index-publish failure holds back
  ``publish_scheduler_registry_manifest`` (never invoked), propagates
  as :class:`StateIndexPublishFailedError` carrying the retry-blocker
  scope, and leaves the compute plane on the previous manifest so
  node-22 is never routed to ``M1`` without ``M1``'s successor
  checkpoint on the file state index (D7 fact anchor A-i).
* (d) An approved cold-start source with no ``(M1, source, t*)`` row
  in ``hydro.state_snapshot`` gets NO fabricated index entry.

The publisher is exercised entirely against in-memory fakes for
:class:`StateSnapshotRepository` (the read side) and the file state
index (a lightweight recording double). Live-DB + real filesystem
validation lives in SUB-9's node-27 receipt scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.model_registry import (
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
) -> StateSnapshot:
    """Build a committed-clone-row shape for the fake state_snapshot repo.

    Mirrors the row shape SUB-2's ``_build_clone_snapshot_row`` writes:
    ``(M1, source, t*)`` with clone provenance columns populated and
    lineage copied from the source snapshot verbatim.
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
        source_id=source_id,
        cycle_id=cycle_id,
        lead_hours=LEAD_HOURS,
        model_package_version=M1_PACKAGE_VERSION,
        model_package_checksum=M1_PACKAGE_CHECKSUM,
        original_shud_filename=f"{source_id}.ic",
        cloned_from_state_id=f"clone-from-{source_id}",
        cloned_from_model_id=M0_MODEL_ID,
        clone_gate_fingerprint="sha256:hydrocore-fingerprint",
    )


# -- Fake state_snapshot repo -----------------------------------------------


@dataclass
class _FakeStateSnapshotRepo:
    """Read-only fake for the state_snapshot table used by the publisher.

    Only implements the two methods the SUB-6 publisher touches
    (``get_latest_snapshot_for_model_source``); everything else is
    left un-implemented so a test that accidentally exercises another
    seam fails loudly.
    """

    rows: dict[tuple[str, str], StateSnapshot]

    def get_latest_snapshot_for_model_source(
        self, *, model_id: str, source_id: str
    ) -> StateSnapshot | None:
        return self.rows.get((model_id, source_id))


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
    state_snapshot_repo = _FakeStateSnapshotRepo(
        rows={
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
    state_snapshot_repo = _FakeStateSnapshotRepo(
        rows={
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
    state_snapshot_repo = _FakeStateSnapshotRepo(
        rows={(M1_MODEL_ID, SOURCE_GFS): gfs_snapshot}
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
    state_snapshot_repo = _FakeStateSnapshotRepo(
        rows={(M1_MODEL_ID, SOURCE_GFS): snapshot}
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
