"""Tests for the pre-activation state-clone hook (Epic #982 SUB-4 §3.1).

Covers the OpenSpec ``atomic-cutover-transaction`` acceptance criteria
pinned in ``openspec/changes/mapping-variant-state-compatibility/tasks.md``
section 3.1 (a)-(g):

* (a) Activation + clone commit atomically on the happy path: the hook
  runs to completion, writes one ``(M1, source, t*)`` clone row per
  source in scope, and never raises.
* (b) Clone failure rolls back: an engaged clone that gets refused
  causes the hook to raise ``StateCloneCutoverRefusedError`` so the
  Change 4 dispatcher aborts the whole transaction.
* (c) No intermediate ``activated-but-not-transferred`` state observable
  mid-transaction: after a rollback simulated on the fake cursor no
  clone row is present for ANY source in scope, including sources
  processed before the refusing one.
* (d) Dual-source scope ``[gfs, ifs]``: happy-path variant writes two
  clone rows; unqualified-one-source variant refuses whole scope and
  the audit record names the blocking source id.
* (e) Fresh basin path: ``previous_active_model=None`` short-circuits
  to a ``no_previous_active_model`` skip with no clone attempt, no
  raise, and no writes.
* (f) Legacy target path: ``source_scope=None`` short-circuits to a
  ``target_not_direct_grid`` skip with no clone attempt, no raise, and
  no writes.
* (g) Already-current short-circuit is owned by Change 4's dispatcher
  (``_would_be_already_current`` in ``model_registry.py``); this test
  module documents the boundary by NOT invoking the hook for that path.
  See ``test_hook_boundary_documented_for_already_current``.

The hook is exercised entirely against an in-memory ``_FakeCursor`` +
``_FakeAuditRecorder``; live-DB validation lives in SUB-9's node-27
receipt scope.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common.model_registry import (
    ColdStartApprovalInput,
    ModelActivationContext,
    PsycopgModelRegistryStore,
    _build_activation_result_approval_block,
)
from packages.common.state_clone import STATE_CLONE_COLD_START_APPROVAL_REQUIRED
from packages.common.state_clone_hook import (
    SKIP_REASON_NO_PREVIOUS_ACTIVE_MODEL,
    SKIP_REASON_TARGET_NOT_DIRECT_GRID,
    STATE_CLONE_APPROVAL_ACTION,
    STATE_CLONE_SPIN_UP_DISTORTION_ANNOUNCEMENT_MARKER,
    StateCloneCutoverRefusedError,
    StateCloneFingerprintInputs,
    _CursorBoundStateSnapshotRepository,
    build_state_clone_cutover_hook,
)
from packages.common.state_manager import StateSnapshot, state_snapshot_id
from tests.test_state_clone import (
    _DEFAULT_NON_SP_ATT_STUBS,
    CUTOVER_VALID_TIME,
    CYCLE_ID,
    DEFAULT_SOLVER_CONFIG_BYTES,
    DEFAULT_STATE_SCHEMA_BYTES,
    M0_MODEL_ID,
    M0_PACKAGE_CHECKSUM,
    M0_PACKAGE_VERSION,
    M1_MODEL_ID,
    M1_PACKAGE_CHECKSUM,
    M1_PACKAGE_VERSION,
    SOURCE_ID,
    _default_category_files,
    _make_source_snapshot,
    _write_package,
    _write_sp_att,
)
from workers.data_adapters.base import cycle_id_for
from workers.mapping_builder.rewrite import compute_hydrologic_core_fingerprint

# --- Local package fixtures (duplicated cleanly from tests.test_state_clone) ---
#
# ``pytest`` does not treat a module-level ``from tests.test_state_clone
# import m0_m1_equal_packages`` as a fixture registration — the imported
# name collides with the function-parameter name in every consumer. We
# duplicate the two fixture *bodies* here and reuse the private helpers
# to avoid drift with the SUB-2 fixture definitions.


@pytest.fixture
def m0_m1_equal_packages(tmp_path: Path) -> dict[str, Any]:
    """Two byte-equal packages so ``verify_...equal`` returns a shared hash."""
    m0_root = _write_package(tmp_path / "m0")
    m1_root = _write_package(tmp_path / "m1")
    m0_sp_att = _write_sp_att(m0_root / "basin.sp.att", forc_values=(1, 2, 3, 4))
    m1_sp_att = _write_sp_att(m1_root / "basin.sp.att", forc_values=(4, 3, 2, 1))
    fp = compute_hydrologic_core_fingerprint(
        m0_root,
        sp_att_path=m0_sp_att,
        category_files=_default_category_files(),
        state_schema_bytes=DEFAULT_STATE_SCHEMA_BYTES,
        solver_config_bytes=DEFAULT_SOLVER_CONFIG_BYTES,
    )
    return {
        "m0_root": m0_root,
        "m1_root": m1_root,
        "m0_sp_att": m0_sp_att,
        "m1_sp_att": m1_sp_att,
        "category_files": _default_category_files(),
        "fingerprint_hash": fp.hash,
    }


@pytest.fixture
def m0_m1_unequal_packages(tmp_path: Path) -> dict[str, Any]:
    """Two packages differing on ONE non-FORC surface — fingerprint drifts."""
    drifted = dict(_DEFAULT_NON_SP_ATT_STUBS)
    drifted["soil"] = ("basin.soil", b"soil-payload-v2-drifted\n")
    m0_root = _write_package(tmp_path / "m0")
    m1_root = _write_package(tmp_path / "m1", stubs=drifted)
    m0_sp_att = _write_sp_att(m0_root / "basin.sp.att")
    m1_sp_att = _write_sp_att(m1_root / "basin.sp.att")
    m0_fp = compute_hydrologic_core_fingerprint(
        m0_root,
        sp_att_path=m0_sp_att,
        category_files=_default_category_files(),
        state_schema_bytes=DEFAULT_STATE_SCHEMA_BYTES,
        solver_config_bytes=DEFAULT_SOLVER_CONFIG_BYTES,
    )
    return {
        "m0_root": m0_root,
        "m1_root": m1_root,
        "m0_sp_att": m0_sp_att,
        "m1_sp_att": m1_sp_att,
        "category_files": _default_category_files(),
        # The unequal-fingerprint refusal path fires before the evidence
        # cross-check runs, so any hash suffices here.
        "fingerprint_hash": m0_fp.hash,
    }


# --- FakeCursor with transaction-like staging ------------------------------


class _FakeCursor:
    """In-memory cursor that mirrors psycopg RealDictCursor semantics.

    Recognizes the three SQL statements the
    ``_CursorBoundStateSnapshotRepository`` adapter emits (point-lookup
    SELECT, source-scoped ``latest-before`` SELECT, and the ``INSERT ...
    ON CONFLICT ... RETURNING *`` upsert) and dispatches each to an in-
    memory ``hydro.state_snapshot`` model keyed on the same
    ``(model_id, COALESCE(source_id, ''), valid_time)`` tuple as the
    production unique index.

    Adds ``commit`` / ``rollback`` seams the tests use to simulate the
    Change 4 lifecycle transaction's atomic-rollback guarantee — writes
    land in a ``_staged`` map first, ``commit`` promotes them into
    ``_committed``, and ``rollback`` discards ``_staged``. Without this
    the "no intermediate state observable" property in acceptance test
    (c) would be untestable against a fake.
    """

    def __init__(self, initial_rows: Sequence[Mapping[str, Any]] | None = None) -> None:
        self._committed: dict[tuple[str, str, datetime], dict[str, Any]] = {}
        for row in initial_rows or ():
            self._committed[self._key(row)] = dict(row)
        self._staged: dict[tuple[str, str, datetime], dict[str, Any]] = {}
        self._last_row: dict[str, Any] | None = None
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    # --- transaction-boundary hooks tests drive explicitly ----------------

    def commit(self) -> None:
        self._committed.update(self._staged)
        self._staged.clear()

    def rollback(self) -> None:
        self._staged.clear()

    # --- row-visibility helpers used by test assertions -------------------

    def all_committed_rows(self) -> list[dict[str, Any]]:
        return list(self._committed.values())

    def all_staged_rows(self) -> list[dict[str, Any]]:
        return list(self._staged.values())

    def all_rows(self) -> list[dict[str, Any]]:
        merged = {**self._committed, **self._staged}
        return list(merged.values())

    # --- psycopg-compatible surface ---------------------------------------

    def execute(self, sql: str, params: Sequence[Any]) -> None:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, tuple(params)))
        if normalized.startswith("INSERT INTO hydro.state_snapshot"):
            self._handle_upsert(tuple(params))
            return
        if "AND valid_time <" in normalized and "ORDER BY valid_time DESC" in normalized:
            self._handle_latest_before(tuple(params))
            return
        if "AND source_id = %s" in normalized and "AND valid_time = %s" in normalized:
            self._handle_point_select_with_source(tuple(params))
            return
        if "AND valid_time = %s" in normalized:
            self._handle_point_select_no_source(tuple(params))
            return
        raise NotImplementedError(f"FakeCursor: unsupported SQL: {sql!r}")

    def fetchone(self) -> dict[str, Any] | None:
        return self._last_row

    # --- SQL handlers -----------------------------------------------------

    def _handle_upsert(self, params: tuple[Any, ...]) -> None:
        row = {
            "state_id": params[0],
            "model_id": params[1],
            "run_id": params[2],
            "valid_time": _ensure_utc(params[3]),
            "state_uri": params[4],
            "checksum": params[5],
            "usable_flag": bool(params[6]),
            "source_id": params[7],
            "cycle_id": params[8],
            "lead_hours": params[9],
            "model_package_version": params[10],
            "model_package_checksum": params[11],
            "original_shud_filename": params[12],
            "cloned_from_state_id": params[13],
            "cloned_from_model_id": params[14],
            "clone_gate_fingerprint": params[15],
            "created_at": datetime(2026, 7, 10, 0, 0, tzinfo=UTC),
        }
        self._staged[self._key(row)] = row
        self._last_row = row

    def _handle_point_select_with_source(self, params: tuple[Any, ...]) -> None:
        model_id, source_id, valid_time = params
        key = (str(model_id), _source_key(source_id), _ensure_utc(valid_time))
        merged = {**self._committed, **self._staged}
        self._last_row = merged.get(key)

    def _handle_point_select_no_source(self, params: tuple[Any, ...]) -> None:
        model_id, valid_time = params
        wanted_time = _ensure_utc(valid_time)
        candidates = [
            row
            for row in {**self._committed, **self._staged}.values()
            if row["model_id"] == model_id
            and _ensure_utc(row["valid_time"]) == wanted_time
        ]
        self._last_row = candidates[0] if candidates else None

    def _handle_latest_before(self, params: tuple[Any, ...]) -> None:
        model_id, source_id, before_time = params
        wanted_source = _source_key(source_id)
        wanted_before = _ensure_utc(before_time)
        candidates = [
            row
            for row in {**self._committed, **self._staged}.values()
            if row["model_id"] == model_id
            and _source_key(row.get("source_id")) == wanted_source
            and _ensure_utc(row["valid_time"]) < wanted_before
        ]
        if not candidates:
            self._last_row = None
            return
        self._last_row = max(
            candidates,
            key=lambda row: _ensure_utc(row["valid_time"]),
        )

    # --- helpers ----------------------------------------------------------

    @staticmethod
    def _key(row: Mapping[str, Any]) -> tuple[str, str, datetime]:
        return (
            str(row["model_id"]),
            _source_key(row.get("source_id")),
            _ensure_utc(row["valid_time"]),
        )


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _source_key(source_id: Any) -> str:
    return "" if source_id in (None, "") else str(source_id)


# --- Fake audit recorder ---------------------------------------------------


class _FakeAuditRecorder:
    """Records skip / refusal / approval events from hook + SUB-2 core.

    The hook forwards this recorder into
    ``fingerprint_gated_state_clone`` (which invokes ``record_refusal``),
    calls ``record_skip`` itself on the two applicability-predicate
    misses, and calls ``record_approval`` on each source skipped through
    the explicit cold-start approval route (SUB-5 task 3.2), so a single
    instance sees the full audit stream.
    """

    def __init__(self) -> None:
        self.skips: list[dict[str, Any]] = []
        self.refusals: list[dict[str, Any]] = []
        self.approvals: list[dict[str, Any]] = []

    def record_skip(self, reason: str, ctx: ModelActivationContext) -> None:
        self.skips.append(
            {
                "reason": reason,
                "basin_version_id": ctx.basin_version_id,
                "target_model_id": ctx.target_model.get("model_id"),
            }
        )

    def record_refusal(self, record: Mapping[str, Any]) -> None:
        self.refusals.append(dict(record))

    def record_approval(self, record: Mapping[str, Any]) -> None:
        self.approvals.append(dict(record))


# --- Provider + activation-context builders --------------------------------


BASIN_VERSION_ID = "basin_v1"


def _target_model_row() -> dict[str, Any]:
    return {"model_id": M1_MODEL_ID, "model_package_version": M1_PACKAGE_VERSION}


def _previous_active_row() -> dict[str, Any]:
    return {"model_id": M0_MODEL_ID, "model_package_version": M0_PACKAGE_VERSION}


def _make_ctx(
    *,
    source_scope: tuple[str, ...] | None,
    previous_active_model: Mapping[str, Any] | None,
) -> ModelActivationContext:
    return ModelActivationContext(
        basin_version_id=BASIN_VERSION_ID,
        previous_active_model=previous_active_model,
        target_model=_target_model_row(),
        source_scope=source_scope,
    )


def _make_fingerprint_inputs(
    pkg: Mapping[str, Any],
    *,
    m1_recorded_hydrologic_core_fingerprint: str | None = None,
    state_schema_bytes: bytes = DEFAULT_STATE_SCHEMA_BYTES,
    solver_config_bytes: bytes = DEFAULT_SOLVER_CONFIG_BYTES,
) -> StateCloneFingerprintInputs:
    return StateCloneFingerprintInputs(
        m0_model_id=M0_MODEL_ID,
        m1_model_id=M1_MODEL_ID,
        m1_model_package_version=M1_PACKAGE_VERSION,
        m1_model_package_checksum=M1_PACKAGE_CHECKSUM,
        m0_package_root=pkg["m0_root"],
        m1_package_root=pkg["m1_root"],
        m0_sp_att_path=pkg["m0_sp_att"],
        m1_sp_att_path=pkg["m1_sp_att"],
        m1_category_files=pkg["category_files"],
        m1_recorded_hydrologic_core_fingerprint=(
            m1_recorded_hydrologic_core_fingerprint or pkg["fingerprint_hash"]
        ),
        state_schema_bytes=state_schema_bytes,
        solver_config_bytes=solver_config_bytes,
        cutover_valid_time=CUTOVER_VALID_TIME,
    )


def _snapshot_row_dict(snapshot: StateSnapshot) -> dict[str, Any]:
    """Convert a StateSnapshot into a dict shaped like a RealDictCursor row."""
    return {
        "state_id": snapshot.state_id,
        "model_id": snapshot.model_id,
        "run_id": snapshot.run_id,
        "valid_time": _ensure_utc(snapshot.valid_time),
        "state_uri": snapshot.state_uri,
        "checksum": snapshot.checksum,
        "usable_flag": snapshot.usable_flag,
        "created_at": snapshot.created_at or datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        "source_id": snapshot.source_id,
        "cycle_id": snapshot.cycle_id,
        "lead_hours": snapshot.lead_hours,
        "model_package_version": snapshot.model_package_version,
        "model_package_checksum": snapshot.model_package_checksum,
        "original_shud_filename": snapshot.original_shud_filename,
        "cloned_from_state_id": snapshot.cloned_from_state_id,
        "cloned_from_model_id": snapshot.cloned_from_model_id,
        "clone_gate_fingerprint": snapshot.clone_gate_fingerprint,
    }


def _seed_source_snapshot(
    *,
    source_id: str = SOURCE_ID,
    valid_time: datetime = CUTOVER_VALID_TIME,
    usable_flag: bool = True,
) -> dict[str, Any]:
    cycle = cycle_id_for(source_id, valid_time - timedelta(hours=12))
    snapshot = replace(
        _make_source_snapshot(valid_time=valid_time, usable_flag=usable_flag),
        state_id=state_snapshot_id(
            M0_MODEL_ID,
            valid_time,
            source_id=source_id,
            cycle_id=cycle,
            lead_hours=12,
        ),
        source_id=source_id,
        cycle_id=cycle,
        state_uri=f"states/{source_id}/{M0_MODEL_ID}/2026061506/state.cfg.ic",
    )
    return _snapshot_row_dict(snapshot)


# --- (a) atomic commit -----------------------------------------------------


def test_a_happy_path_commits_source_row_plus_clone_row(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """Hook writes one clone row per source without raising; commit persists."""

    cursor = _FakeCursor(initial_rows=[_seed_source_snapshot(source_id="gfs")])
    audit = _FakeAuditRecorder()
    hook = build_state_clone_cutover_hook(
        audit_recorder=audit,
        fingerprint_inputs_provider=lambda _ctx, _source: _make_fingerprint_inputs(
            m0_m1_equal_packages
        ),
    )
    ctx = _make_ctx(source_scope=("gfs",), previous_active_model=_previous_active_row())

    hook(cursor, ctx)
    cursor.commit()

    committed = cursor.all_committed_rows()
    # Original M0 source row + one M1 clone row are both persisted.
    assert len(committed) == 2
    m0_rows = [row for row in committed if row["model_id"] == M0_MODEL_ID]
    m1_rows = [row for row in committed if row["model_id"] == M1_MODEL_ID]
    assert len(m0_rows) == 1
    assert len(m1_rows) == 1
    clone = m1_rows[0]
    assert clone["source_id"] == "gfs"
    assert clone["state_uri"] == m0_rows[0]["state_uri"]
    assert clone["checksum"] == m0_rows[0]["checksum"]
    assert clone["cloned_from_model_id"] == M0_MODEL_ID
    assert clone["cloned_from_state_id"] == m0_rows[0]["state_id"]
    assert clone["clone_gate_fingerprint"] == m0_m1_equal_packages["fingerprint_hash"]
    # No skip and no refusal emitted on the engaged happy path.
    assert audit.skips == []
    assert audit.refusals == []


# --- (b) clone failure raises + rolls back --------------------------------


def test_b_unequal_fingerprint_raises_refused_error_no_row_committed(
    m0_m1_unequal_packages: dict[str, Any],
) -> None:
    """An engaged clone refusal surfaces as StateCloneCutoverRefusedError."""

    cursor = _FakeCursor(initial_rows=[_seed_source_snapshot(source_id="gfs")])
    audit = _FakeAuditRecorder()
    hook = build_state_clone_cutover_hook(
        audit_recorder=audit,
        fingerprint_inputs_provider=lambda _ctx, _source: _make_fingerprint_inputs(
            m0_m1_unequal_packages
        ),
    )
    ctx = _make_ctx(source_scope=("gfs",), previous_active_model=_previous_active_row())

    with pytest.raises(StateCloneCutoverRefusedError) as raised:
        hook(cursor, ctx)
    cursor.rollback()

    assert raised.value.source_id == "gfs"
    assert raised.value.refusal_scope == "unequal_fingerprint"
    assert raised.value.refusal_code == STATE_CLONE_COLD_START_APPROVAL_REQUIRED
    # No clone row committed or staged; the source row remains untouched.
    assert cursor.all_staged_rows() == []
    assert all(row["model_id"] == M0_MODEL_ID for row in cursor.all_committed_rows())
    # SUB-2 core recorded the refusal before raising propagated up.
    assert audit.refusals == [
        {
            "refusal_code": STATE_CLONE_COLD_START_APPROVAL_REQUIRED,
            "refusal_scope": "unequal_fingerprint",
            "m0_model_id": M0_MODEL_ID,
            "m1_model_id": M1_MODEL_ID,
            "source_id": "gfs",
            "cutover_valid_time": CUTOVER_VALID_TIME,
        }
    ]
    assert audit.skips == []


# --- (c) no intermediate state observable mid-transaction -----------------


def test_c_mid_transaction_refusal_leaves_no_clone_row_persisted(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """After a refusal in a multi-source scope no clone row commits for any source.

    Scenario: scope=[gfs, ifs]. gfs is qualified (clone succeeds and
    stages a clone row). ifs is missing (clone refuses on the second
    iteration). Because the hook raises after the first failure, the
    Change 4 dispatcher rolls back the transaction — modeled here by
    ``cursor.rollback()`` — and the staged gfs clone row is discarded.
    """

    cursor = _FakeCursor(initial_rows=[_seed_source_snapshot(source_id="gfs")])
    audit = _FakeAuditRecorder()
    hook = build_state_clone_cutover_hook(
        audit_recorder=audit,
        fingerprint_inputs_provider=lambda _ctx, _source: _make_fingerprint_inputs(
            m0_m1_equal_packages
        ),
    )
    ctx = _make_ctx(
        source_scope=("gfs", "ifs"),
        previous_active_model=_previous_active_row(),
    )

    with pytest.raises(StateCloneCutoverRefusedError) as raised:
        hook(cursor, ctx)
    # Simulate the Change 4 dispatcher rolling back the transaction on
    # the raised exception; no committed row appears for either source.
    cursor.rollback()

    assert raised.value.source_id == "ifs"
    assert raised.value.refusal_scope == "missing_qualified_source"
    committed = cursor.all_committed_rows()
    # Only the pre-existing M0 gfs source row is committed; no clone row
    # for gfs or ifs made it through the transaction boundary.
    assert len(committed) == 1
    assert committed[0]["model_id"] == M0_MODEL_ID
    assert cursor.all_staged_rows() == []
    # Refusal audit names the blocking source (ifs).
    assert audit.refusals == [
        {
            "refusal_code": STATE_CLONE_COLD_START_APPROVAL_REQUIRED,
            "refusal_scope": "missing_qualified_source",
            "m0_model_id": M0_MODEL_ID,
            "m1_model_id": M1_MODEL_ID,
            "source_id": "ifs",
            "cutover_valid_time": CUTOVER_VALID_TIME,
        }
    ]


# --- (d) dual-source scope: happy + blocked -------------------------------


def test_d1_dual_source_scope_writes_two_clone_rows_atomically(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """scope=[gfs, ifs] with both qualified: two clone rows commit atomically."""

    cursor = _FakeCursor(
        initial_rows=[
            _seed_source_snapshot(source_id="gfs"),
            _seed_source_snapshot(source_id="ifs"),
        ]
    )
    audit = _FakeAuditRecorder()
    hook = build_state_clone_cutover_hook(
        audit_recorder=audit,
        fingerprint_inputs_provider=lambda _ctx, _source: _make_fingerprint_inputs(
            m0_m1_equal_packages
        ),
    )
    ctx = _make_ctx(
        source_scope=("gfs", "ifs"),
        previous_active_model=_previous_active_row(),
    )

    hook(cursor, ctx)
    cursor.commit()

    m1_rows = [row for row in cursor.all_committed_rows() if row["model_id"] == M1_MODEL_ID]
    assert {row["source_id"] for row in m1_rows} == {"gfs", "ifs"}
    assert all(row["cloned_from_model_id"] == M0_MODEL_ID for row in m1_rows)
    assert all(
        row["clone_gate_fingerprint"] == m0_m1_equal_packages["fingerprint_hash"]
        for row in m1_rows
    )
    assert audit.skips == []
    assert audit.refusals == []


def test_d2_dual_source_scope_with_blocking_source_rolls_back(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """scope=[gfs, ifs] with ifs unqualified: whole scope rolls back and audit names ifs."""

    cursor = _FakeCursor(initial_rows=[_seed_source_snapshot(source_id="gfs")])
    audit = _FakeAuditRecorder()
    hook = build_state_clone_cutover_hook(
        audit_recorder=audit,
        fingerprint_inputs_provider=lambda _ctx, _source: _make_fingerprint_inputs(
            m0_m1_equal_packages
        ),
    )
    ctx = _make_ctx(
        source_scope=("gfs", "ifs"),
        previous_active_model=_previous_active_row(),
    )

    with pytest.raises(StateCloneCutoverRefusedError) as raised:
        hook(cursor, ctx)
    cursor.rollback()

    assert raised.value.source_id == "ifs"
    assert raised.value.refusal_scope == "missing_qualified_source"
    committed = cursor.all_committed_rows()
    assert len(committed) == 1
    assert committed[0]["model_id"] == M0_MODEL_ID
    # Audit refusal explicitly names ifs — operator sees exactly which
    # source needs remediation.
    assert audit.refusals[-1]["source_id"] == "ifs"


# --- (e) fresh basin: no previous active model ----------------------------


def test_e_no_previous_active_model_skips_without_engaging_clone(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """Fresh basin activation: hook records skip and returns without any writes."""

    cursor = _FakeCursor()
    audit = _FakeAuditRecorder()

    def _never_called_provider(
        _ctx: ModelActivationContext, _source: str
    ) -> StateCloneFingerprintInputs:
        raise AssertionError(
            "fingerprint_inputs_provider must not be invoked on the skip path"
        )

    hook = build_state_clone_cutover_hook(
        audit_recorder=audit,
        fingerprint_inputs_provider=_never_called_provider,
    )
    ctx = _make_ctx(source_scope=("gfs",), previous_active_model=None)

    hook(cursor, ctx)  # Must NOT raise.

    assert cursor.all_staged_rows() == []
    assert cursor.all_committed_rows() == []
    # No SQL was executed at all — no repository was ever built.
    assert cursor.executed == []
    assert audit.skips == [
        {
            "reason": SKIP_REASON_NO_PREVIOUS_ACTIVE_MODEL,
            "basin_version_id": BASIN_VERSION_ID,
            "target_model_id": M1_MODEL_ID,
        }
    ]
    assert audit.refusals == []


# --- (f) legacy target: source_scope is None ------------------------------


def test_f_legacy_target_skips_without_engaging_clone(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """Legacy IDW target (source_scope=None): hook records skip and returns."""

    cursor = _FakeCursor()
    audit = _FakeAuditRecorder()

    def _never_called_provider(
        _ctx: ModelActivationContext, _source: str
    ) -> StateCloneFingerprintInputs:
        raise AssertionError(
            "fingerprint_inputs_provider must not be invoked on the skip path"
        )

    hook = build_state_clone_cutover_hook(
        audit_recorder=audit,
        fingerprint_inputs_provider=_never_called_provider,
    )
    ctx = _make_ctx(source_scope=None, previous_active_model=_previous_active_row())

    hook(cursor, ctx)

    assert cursor.all_staged_rows() == []
    assert cursor.all_committed_rows() == []
    assert cursor.executed == []
    assert audit.skips == [
        {
            "reason": SKIP_REASON_TARGET_NOT_DIRECT_GRID,
            "basin_version_id": BASIN_VERSION_ID,
            "target_model_id": M1_MODEL_ID,
        }
    ]
    assert audit.refusals == []


# --- (g) already-current boundary documentation ---------------------------


def test_hook_boundary_documented_for_already_current(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """Change 4's dispatcher owns the already-current short-circuit.

    The pre-activation hook chain is invoked from
    ``PsycopgModelRegistryStore._dispatch_pre_activation_hooks``; upstream
    of that, ``_apply_model_lifecycle_transition`` (activate/switch_version)
    and ``model_lifecycle_operation`` (rollback_version) short-circuit
    an activation whose target is already active and never call the
    dispatcher — so this hook is not invoked in the already-current
    path (docs §Decision D7). This test documents the boundary: SUB-4
    does not need to add special handling for that case; the assertion
    below is a boundary-pin, not an execution of the short-circuit.
    """

    # The hook is a plain callable — its own contract does not detect
    # or handle already-current; that is Change 4's contract. If we
    # DID invoke the hook against an already-active target with a
    # populated source scope, it would engage the clone loop like any
    # other activation. Confirming that behaviour without touching
    # model_registry.py is intentional: SUB-4's scope stops at the
    # extension-point boundary. Change 4's tests
    # (`_would_be_already_current`) own the short-circuit path.
    cursor = _FakeCursor(initial_rows=[_seed_source_snapshot(source_id="gfs")])
    audit = _FakeAuditRecorder()
    hook = build_state_clone_cutover_hook(
        audit_recorder=audit,
        fingerprint_inputs_provider=lambda _ctx, _source: _make_fingerprint_inputs(
            m0_m1_equal_packages
        ),
    )
    already_active_target = {
        "model_id": M1_MODEL_ID,
        "active_flag": True,
        "lifecycle_state": "active",
    }
    ctx = ModelActivationContext(
        basin_version_id=BASIN_VERSION_ID,
        previous_active_model=_previous_active_row(),
        target_model=already_active_target,
        source_scope=("gfs",),
    )

    # If Change 4 ever routes an already-current activation into the
    # hook chain in the future, this hook would still engage (SUB-4
    # scope). This is the current behavior of the SUB-4 module; the
    # short-circuit protection lives in Change 4.
    hook(cursor, ctx)
    assert any(
        row["model_id"] == M1_MODEL_ID and row["source_id"] == "gfs"
        for row in cursor.all_staged_rows()
    )


# --- Adapter contract: cursor-bound SQL round-trip ------------------------


def test_cursor_bound_adapter_round_trips_source_snapshot_row(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """Adapter's SELECT + UPSERT round-trip through the fake cursor.

    Verifies the ``_CursorBoundStateSnapshotRepository`` SQL statements
    are shaped to match ``_snapshot_from_row``'s dict contract: a
    round-tripped read yields the same identity, and a persist of the
    result reproduces every column. Guards against a future SQL edit
    silently dropping one of the SUB-1 provenance columns.
    """

    cursor = _FakeCursor(initial_rows=[_seed_source_snapshot(source_id="gfs")])
    repository = _CursorBoundStateSnapshotRepository(cursor)

    fetched = repository.get_state_snapshot_by_model_time(
        model_id=M0_MODEL_ID,
        valid_time=CUTOVER_VALID_TIME,
        source_id="gfs",
        lead_hours=12,
    )
    assert fetched is not None
    assert fetched.model_id == M0_MODEL_ID
    assert fetched.source_id == "gfs"
    assert fetched.checksum
    assert fetched.model_package_version == M0_PACKAGE_VERSION
    assert fetched.model_package_checksum == M0_PACKAGE_CHECKSUM
    assert fetched.cycle_id == cycle_id_for(
        "gfs", CUTOVER_VALID_TIME - timedelta(hours=12)
    )
    assert fetched.lead_hours == 12

    stale = repository.get_latest_state_before(
        model_id=M0_MODEL_ID,
        source_id="gfs",
        before_time=CUTOVER_VALID_TIME,
    )
    # No earlier row is seeded, so the ``latest-before`` lookup is None.
    assert stale is None

    # Upsert a synthetic clone row through the adapter and prove
    # RETURNING * hydrates back to the same StateSnapshot.
    synthetic_clone = replace(
        fetched,
        state_id=state_snapshot_id(
            M1_MODEL_ID,
            fetched.valid_time,
            source_id=fetched.source_id,
            cycle_id=fetched.cycle_id,
            lead_hours=fetched.lead_hours,
        ),
        model_id=M1_MODEL_ID,
        model_package_version=M1_PACKAGE_VERSION,
        model_package_checksum=M1_PACKAGE_CHECKSUM,
        cloned_from_state_id=fetched.state_id,
        cloned_from_model_id=M0_MODEL_ID,
        clone_gate_fingerprint="f" * 64,
    )
    persisted = repository.upsert_state_snapshot(synthetic_clone)
    assert persisted.state_id == synthetic_clone.state_id
    assert persisted.model_id == M1_MODEL_ID
    assert persisted.cloned_from_state_id == fetched.state_id
    assert persisted.cloned_from_model_id == M0_MODEL_ID
    assert persisted.clone_gate_fingerprint == "f" * 64
    assert cursor.all_staged_rows(), "upsert must land in the staging area"


# --- Regression pin: legacy-target skip does NOT touch DB -----------------


def test_legacy_target_skip_never_opens_a_repository(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """The skip paths must run before ``repository_factory`` is invoked.

    If the applicability predicate ever regressed to build the adapter
    before the skip check, a legacy-target activation would briefly hold
    a cursor-bound repository — harmless in practice but wasted work
    that also complicates future factory injection. Pin the ordering by
    injecting a factory that raises when called.
    """

    def _factory_that_must_not_be_called(_cursor: Any) -> Any:
        raise AssertionError("repository_factory must not run on the skip path")

    cursor = _FakeCursor()
    audit = _FakeAuditRecorder()

    hook = build_state_clone_cutover_hook(
        audit_recorder=audit,
        fingerprint_inputs_provider=lambda _ctx, _source: _make_fingerprint_inputs(
            m0_m1_equal_packages
        ),
        repository_factory=_factory_that_must_not_be_called,
    )
    ctx = _make_ctx(source_scope=None, previous_active_model=_previous_active_row())
    hook(cursor, ctx)  # must NOT raise
    assert audit.skips[0]["reason"] == SKIP_REASON_TARGET_NOT_DIRECT_GRID


# --- SUB-5 §3.2: explicit cold-start approval route -----------------------


def _make_ctx_with_approval(
    *,
    source_scope: tuple[str, ...],
    previous_active_model: Mapping[str, Any] | None,
    approval: ColdStartApprovalInput | None,
) -> ModelActivationContext:
    return ModelActivationContext(
        basin_version_id=BASIN_VERSION_ID,
        previous_active_model=previous_active_model,
        target_model=_target_model_row(),
        source_scope=source_scope,
        cold_start_approval=approval,
    )


def test_refusal_without_approval_rolls_back_and_records_stable_code_in_audit(
    m0_m1_unequal_packages: dict[str, Any],
) -> None:
    """SUB-5 §3.2 (a): refusal-without-approval surfaces the stable code + audit.

    An engaged clone refusal without a covering ``cold_start_approval``
    on the activation context must:

    * raise :class:`StateCloneCutoverRefusedError` so Change 4's
      dispatcher rolls the whole transaction back (no clone row committed);
    * carry the stable error code
      ``state_clone_cold_start_approval_required`` on the raised
      exception (the code the outside-tx audit-log write in
      ``PsycopgModelRegistryStore.model_lifecycle_operation`` keys off);
    * emit a refusal audit record naming the blocked ``source_id`` and
      the refusal cause (``refusal_scope``);
    * emit NO approval audit record.

    This locks §3.2 clause "an engaged-clone refusal surfaces the stable
    error code ... plus an ops.audit_log record naming the blocked
    scope and cause" at the hook boundary. The outside-tx persistence is
    exercised by the model_registry-level integration test.
    """

    cursor = _FakeCursor(initial_rows=[_seed_source_snapshot(source_id="gfs")])
    audit = _FakeAuditRecorder()
    hook = build_state_clone_cutover_hook(
        audit_recorder=audit,
        fingerprint_inputs_provider=lambda _ctx, _source: _make_fingerprint_inputs(
            m0_m1_unequal_packages
        ),
    )
    # Approval explicitly absent — SUB-4 default preserved.
    ctx = _make_ctx_with_approval(
        source_scope=("gfs",),
        previous_active_model=_previous_active_row(),
        approval=None,
    )

    with pytest.raises(StateCloneCutoverRefusedError) as raised:
        hook(cursor, ctx)
    cursor.rollback()

    # Stable code surfaces on the exception — the outside-tx audit write
    # in ``PsycopgModelRegistryStore.model_lifecycle_operation`` keys off
    # exactly this constant.
    assert raised.value.refusal_code == STATE_CLONE_COLD_START_APPROVAL_REQUIRED
    assert raised.value.source_id == "gfs"
    assert raised.value.refusal_scope == "unequal_fingerprint"

    # No clone row committed and none staged — the transaction rolled
    # back completely, matching §3.2 clause "rolls back and surfaces the
    # stable code".
    assert cursor.all_staged_rows() == []
    assert all(row["model_id"] == M0_MODEL_ID for row in cursor.all_committed_rows())

    # Refusal audit record present and shape-locked. The SUB-2 clone
    # core populates the record before returning, so the hook sees the
    # blocked source id + refusal cause verbatim.
    assert audit.refusals == [
        {
            "refusal_code": STATE_CLONE_COLD_START_APPROVAL_REQUIRED,
            "refusal_scope": "unequal_fingerprint",
            "m0_model_id": M0_MODEL_ID,
            "m1_model_id": M1_MODEL_ID,
            "source_id": "gfs",
            "cutover_valid_time": CUTOVER_VALID_TIME,
        }
    ]
    # No approval fired on the refusal path.
    assert audit.approvals == []


def test_approval_committed_skips_covered_sources_and_records_marker(
    m0_m1_unequal_packages: dict[str, Any],
) -> None:
    """SUB-5 §3.2 (b): approval-covered source is skipped with obligation marker.

    An activation request carrying a ``cold_start_approval`` whose
    ``covered_source_ids`` names the only source in scope must:

    * NOT raise (the fingerprint gate is bypassed for covered sources);
    * NOT write a clone row for the covered source (approval = cold
      start; no lineage carries over);
    * NOT invoke the fingerprint-inputs provider for the covered source
      (bypassing the gate means we never read package roots either);
    * record the approval on the audit recorder with the exact shape
      pinned by SUB-5: ``action``, ``approver``, ``reason``,
      ``covered_source_ids``, and the spin-up-distortion-announcement
      obligation marker (docs §11.3 clause 3).

    The unequal-fingerprint fixture is used deliberately — it guarantees
    the fingerprint gate WOULD refuse this source if the hook fell
    through to it; a passing test proves the approval short-circuit
    fires BEFORE the gate.
    """

    cursor = _FakeCursor(initial_rows=[_seed_source_snapshot(source_id="gfs")])
    audit = _FakeAuditRecorder()

    def _never_called_provider(
        _ctx: ModelActivationContext, _source: str
    ) -> StateCloneFingerprintInputs:
        raise AssertionError(
            "fingerprint_inputs_provider must not be invoked for approval-covered sources"
        )

    hook = build_state_clone_cutover_hook(
        audit_recorder=audit,
        fingerprint_inputs_provider=_never_called_provider,
    )
    approval = ColdStartApprovalInput(
        approver="ops.operator@example.org",
        reason="M1 rolls out onto a new soil layer; cold-start acknowledged.",
        covered_source_ids=("gfs",),
    )
    ctx = _make_ctx_with_approval(
        source_scope=("gfs",),
        previous_active_model=_previous_active_row(),
        approval=approval,
    )

    hook(cursor, ctx)  # Must NOT raise — approval covers gfs.
    cursor.commit()

    # No clone row committed for the covered source — approval = cold
    # start, no lineage carries over.
    m1_rows = [
        row for row in cursor.all_committed_rows() if row["model_id"] == M1_MODEL_ID
    ]
    assert m1_rows == []
    # The pre-existing M0 source row is untouched.
    assert all(row["model_id"] == M0_MODEL_ID for row in cursor.all_committed_rows())

    # No refusal fired — approval short-circuited before the fingerprint
    # gate would have refused.
    assert audit.refusals == []
    # No SKIP fired either — the hook engaged (previous active model
    # present, target is direct-grid).
    assert audit.skips == []

    # Approval record shape-locked. The obligation marker constant is a
    # module literal both here and in the hook, so a silent rename would
    # break this assertion (that is the point).
    assert audit.approvals == [
        {
            "action": STATE_CLONE_APPROVAL_ACTION,
            "basin_version_id": BASIN_VERSION_ID,
            "source_id": "gfs",
            "target_model_id": M1_MODEL_ID,
            "approver": "ops.operator@example.org",
            "reason": "M1 rolls out onto a new soil layer; cold-start acknowledged.",
            "covered_source_ids": ("gfs",),
            "spin_up_distortion_announcement_obligation": (
                STATE_CLONE_SPIN_UP_DISTORTION_ANNOUNCEMENT_MARKER
            ),
        }
    ]


def test_approval_scoped_to_named_sources_only_gfs_still_clones(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """SUB-5 §3.2 (c): approval scope is exact — uncovered sources still gate.

    Scope is ``(gfs, ifs)``. Approval covers ONLY ``ifs``. The hook must:

    * clone ``gfs`` through the fingerprint gate normally (equal-fingerprint
      fixture guarantees the gate passes);
    * skip ``ifs`` with an approval audit record + obligation marker;
    * emit no refusal;
    * write ONE clone row (for gfs) — none for ifs.

    Locks §3.2 clause "the approval is scoped to its named sources only".
    An approval never widens beyond its ``covered_source_ids`` — this is
    the operator's contract, not the hook's discretion.
    """

    # Both sources have M0 rows seeded so the fingerprint gate for gfs
    # can find its qualified snapshot; ifs is present but the approval
    # bypasses it before the gate touches it.
    cursor = _FakeCursor(
        initial_rows=[
            _seed_source_snapshot(source_id="gfs"),
            _seed_source_snapshot(source_id="ifs"),
        ]
    )
    audit = _FakeAuditRecorder()

    provider_calls: list[str] = []

    def _spy_provider(
        _ctx: ModelActivationContext, source_id: str
    ) -> StateCloneFingerprintInputs:
        # Approval-covered sources never touch the provider; unapproved
        # sources do. Recording the call list lets us prove exactly one
        # call, for ``gfs``.
        provider_calls.append(source_id)
        return _make_fingerprint_inputs(m0_m1_equal_packages)

    hook = build_state_clone_cutover_hook(
        audit_recorder=audit,
        fingerprint_inputs_provider=_spy_provider,
    )
    approval = ColdStartApprovalInput(
        approver="ops.operator@example.org",
        reason="ifs feed rebuilt from scratch — cold-start acknowledged.",
        covered_source_ids=("ifs",),
    )
    ctx = _make_ctx_with_approval(
        source_scope=("gfs", "ifs"),
        previous_active_model=_previous_active_row(),
        approval=approval,
    )

    hook(cursor, ctx)  # Must NOT raise — gfs qualifies, ifs is approved.
    cursor.commit()

    # Exactly one clone row committed — for gfs.
    m1_rows = [
        row for row in cursor.all_committed_rows() if row["model_id"] == M1_MODEL_ID
    ]
    assert len(m1_rows) == 1
    assert m1_rows[0]["source_id"] == "gfs"
    assert m1_rows[0]["cloned_from_model_id"] == M0_MODEL_ID
    assert m1_rows[0]["clone_gate_fingerprint"] == m0_m1_equal_packages["fingerprint_hash"]

    # Provider was invoked exactly once, for gfs — approval-covered
    # sources bypass the gate before the provider is asked.
    assert provider_calls == ["gfs"]

    # No refusal; no applicability skip.
    assert audit.refusals == []
    assert audit.skips == []

    # Exactly one approval record — for ifs. Shape-locked as in (b).
    assert audit.approvals == [
        {
            "action": STATE_CLONE_APPROVAL_ACTION,
            "basin_version_id": BASIN_VERSION_ID,
            "source_id": "ifs",
            "target_model_id": M1_MODEL_ID,
            "approver": "ops.operator@example.org",
            "reason": "ifs feed rebuilt from scratch — cold-start acknowledged.",
            "covered_source_ids": ("ifs",),
            "spin_up_distortion_announcement_obligation": (
                STATE_CLONE_SPIN_UP_DISTORTION_ANNOUNCEMENT_MARKER
            ),
        }
    ]


# --- SUB-5 §3.2 fold-at-intro: activation-result marker predicate ---------
#
# Round-1 review found the activation-result marker (built by
# ``_build_activation_result_approval_block`` in ``model_registry.py``)
# would emit whenever an in-scope covered source existed, but the
# state-clone hook actually SKIPS its per-source approval-consumption
# loop (via ``no_previous_active_model`` / ``target_not_direct_grid``)
# whenever ``previous_active_model is None`` OR ``source_scope is None``.
# The asymmetry meant a fresh-basin or legacy-target activation could
# ship the ``spin_up_distortion_announcement_obligation`` marker on the
# result WITHOUT any backing ``record_approval`` audit row — an
# unauditable obligation. The fold tightens the predicate to match the
# hook and locks it here on both sides (result + hook).


def _happy_path_approval_ctx() -> ModelActivationContext:
    """Fully engaged activation ctx: prior active + direct-grid + approval."""
    approval = ColdStartApprovalInput(
        approver="ops.operator@example.org",
        reason="M1 rolls out onto a new soil layer; cold-start acknowledged.",
        covered_source_ids=("gfs",),
    )
    return _make_ctx_with_approval(
        source_scope=("gfs",),
        previous_active_model=_previous_active_row(),
        approval=approval,
    )


def test_build_activation_result_approval_block_emits_marker_on_result_side_happy_path() -> (
    None
):
    """Happy path: the block is emitted with the exact obligation shape.

    Locks the result-side contract: when the hook engages (prior active
    row present, direct-grid target, approval covers an in-scope
    source), the activation result carries approver + reason +
    covered_source_ids + the spin-up-distortion-announcement obligation
    marker. This is the only shape API consumers key off.
    """

    block = _build_activation_result_approval_block(_happy_path_approval_ctx())

    assert block == {
        "approver": "ops.operator@example.org",
        "reason": "M1 rolls out onto a new soil layer; cold-start acknowledged.",
        "covered_source_ids": ["gfs"],
        "spin_up_distortion_announcement_obligation": (
            STATE_CLONE_SPIN_UP_DISTORTION_ANNOUNCEMENT_MARKER
        ),
    }


def test_build_activation_result_approval_block_fresh_basin_returns_none() -> None:
    """Fresh basin (previous_active_model is None) suppresses the marker.

    The hook takes the ``no_previous_active_model`` skip path BEFORE its
    approval loop, so no ``record_approval`` audit row is written. The
    result-side marker must mirror that: no marker on the result when
    the hook would not have recorded the approval.

    Fold-at-intro P2 (Epic #982 SUB-5 round-1 correctness review).
    """

    approval = ColdStartApprovalInput(
        approver="ops.operator@example.org",
        reason="cold-start acknowledged.",
        covered_source_ids=("gfs",),
    )
    ctx = _make_ctx_with_approval(
        source_scope=("gfs",),
        previous_active_model=None,  # fresh basin
        approval=approval,
    )

    assert _build_activation_result_approval_block(ctx) is None


def test_build_activation_result_approval_block_legacy_target_returns_none() -> None:
    """Legacy target (source_scope is None) suppresses the marker.

    The hook takes the ``target_not_direct_grid`` skip path BEFORE its
    approval loop, so no ``record_approval`` audit row is written.
    Result-side marker must mirror that suppression symmetrically.

    Fold-at-intro P2 (Epic #982 SUB-5 round-1 correctness review).
    """

    approval = ColdStartApprovalInput(
        approver="ops.operator@example.org",
        reason="cold-start acknowledged.",
        covered_source_ids=("gfs",),
    )
    ctx = _make_ctx_with_approval(
        source_scope=None,  # legacy IDW target
        previous_active_model=_previous_active_row(),
        approval=approval,
    )

    assert _build_activation_result_approval_block(ctx) is None


def test_build_activation_result_approval_block_stray_approval_covers_no_in_scope_source_returns_none() -> (
    None
):
    """Stray approval that covers no in-scope source suppresses the marker.

    The hook only fires ``record_approval`` for a source that BOTH
    appears in ``source_scope`` AND is named by
    ``approval.covered_source_ids``. An approval whose covered set does
    not intersect the scope records nothing on the audit stream, so
    the result must not surface an obligation marker either.
    """

    approval = ColdStartApprovalInput(
        approver="ops.operator@example.org",
        reason="ifs cold-start acknowledged — but ifs is not in scope here.",
        covered_source_ids=("ifs",),
    )
    ctx = _make_ctx_with_approval(
        source_scope=("gfs",),
        previous_active_model=_previous_active_row(),
        approval=approval,
    )

    assert _build_activation_result_approval_block(ctx) is None


def test_hook_stray_approval_covers_no_in_scope_source_records_no_approval(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """Hook-side pairing: stray approval + qualifying gate produces no approval.

    Same ctx as
    ``test_build_activation_result_approval_block_stray_approval_covers_no_in_scope_source_returns_none``,
    but driven through the hook: the fingerprint gate for ``gfs`` passes
    (equal packages), one clone row commits, the stray approval for
    ``ifs`` never crosses the scope, and ``audit.approvals`` stays
    empty. Locks the hook↔result symmetry from the hook side.
    """

    cursor = _FakeCursor(initial_rows=[_seed_source_snapshot(source_id="gfs")])
    audit = _FakeAuditRecorder()

    hook = build_state_clone_cutover_hook(
        audit_recorder=audit,
        fingerprint_inputs_provider=lambda _ctx, _source: _make_fingerprint_inputs(
            m0_m1_equal_packages
        ),
    )
    approval = ColdStartApprovalInput(
        approver="ops.operator@example.org",
        reason="ifs cold-start acknowledged — but ifs is not in scope here.",
        covered_source_ids=("ifs",),
    )
    ctx = _make_ctx_with_approval(
        source_scope=("gfs",),
        previous_active_model=_previous_active_row(),
        approval=approval,
    )

    hook(cursor, ctx)  # Must NOT raise — gfs qualifies through the gate.
    cursor.commit()

    m1_rows = [
        row for row in cursor.all_committed_rows() if row["model_id"] == M1_MODEL_ID
    ]
    assert len(m1_rows) == 1
    assert m1_rows[0]["source_id"] == "gfs"

    # Stray approval never crossed the scope — nothing recorded.
    assert audit.approvals == []
    assert audit.refusals == []
    assert audit.skips == []


def test_hook_fresh_basin_with_stray_approval_still_records_only_skip() -> None:
    """Fresh basin + non-None approval records ONLY the applicability skip.

    Applicability gate 1 (``no_previous_active_model``) fires BEFORE the
    approval loop. Even with a non-None approval on ctx, no
    ``record_approval`` call must be issued; only the ``skip`` audit
    record for the fresh-basin reason. This pins the applicability-
    before-approval ordering that the result-side predicate now mirrors.
    """

    cursor = _FakeCursor()
    audit = _FakeAuditRecorder()

    def _never_called_provider(
        _ctx: ModelActivationContext, _source: str
    ) -> StateCloneFingerprintInputs:
        raise AssertionError(
            "fingerprint_inputs_provider must not be invoked on the skip path"
        )

    hook = build_state_clone_cutover_hook(
        audit_recorder=audit,
        fingerprint_inputs_provider=_never_called_provider,
    )
    approval = ColdStartApprovalInput(
        approver="ops.operator@example.org",
        reason="cold-start acknowledged.",
        covered_source_ids=("gfs",),
    )
    ctx = _make_ctx_with_approval(
        source_scope=("gfs",),
        previous_active_model=None,  # fresh basin — skip fires first
        approval=approval,
    )

    hook(cursor, ctx)  # Must NOT raise.

    # No SQL executed — the repository was never built.
    assert cursor.executed == []
    assert cursor.all_staged_rows() == []
    assert cursor.all_committed_rows() == []
    # Applicability-before-approval: only the skip fires; approval loop
    # is unreachable when previous_active_model is None.
    assert audit.skips == [
        {
            "reason": SKIP_REASON_NO_PREVIOUS_ACTIVE_MODEL,
            "basin_version_id": BASIN_VERSION_ID,
            "target_model_id": M1_MODEL_ID,
        }
    ]
    assert audit.approvals == []
    assert audit.refusals == []


# --- SUB-5 §3.2 fold-at-intro: outside-tx refusal audit unit coverage -----


class _RecordingRefusalCursor:
    """Cursor stand-in that captures INSERT params and returns a log_id."""

    def __init__(self, log_id: int) -> None:
        self._log_id = log_id
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: Sequence[Any]) -> None:
        self.statements.append((sql, tuple(params)))

    def fetchone(self) -> dict[str, Any]:
        # Mirrors psycopg2 RealDictCursor: dict-shaped row with the
        # RETURNING column name as key.
        return {"log_id": self._log_id}


class _RecordingRefusalTransactionContext:
    """Context manager that yields a ``_RecordingRefusalCursor``."""

    def __init__(self, cursor: _RecordingRefusalCursor) -> None:
        self._cursor = cursor
        self.entered = False
        self.exited = False

    def __enter__(self) -> _RecordingRefusalCursor:
        self.entered = True
        return self._cursor

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: Any,
    ) -> bool:
        self.exited = True
        return False


class _RefusalCaptureStore(PsycopgModelRegistryStore):
    """Store harness that captures every ``_transaction()`` open.

    Overrides ``_transaction()`` alone so the SUT's own
    ``_record_state_clone_refusal_audit`` control flow runs verbatim
    against an in-memory cursor. The list of yielded transactions
    proves the outside-tx invariant: exactly ONE new transaction opens
    to persist the refusal audit row (autonomous sub-tx are unavailable
    in PostgreSQL, so this fresh tx is the only durability path).
    """

    def __init__(self) -> None:
        super().__init__("postgresql://harness")
        object.__setattr__(self, "opened_transactions", [])
        object.__setattr__(self, "refusal_log_id", 424242)

    def _transaction(self) -> _RecordingRefusalTransactionContext:
        cursor = _RecordingRefusalCursor(log_id=self.refusal_log_id)
        ctx = _RecordingRefusalTransactionContext(cursor)
        self.opened_transactions.append(ctx)
        return ctx


def _refusal_policy_decision() -> Any:
    from packages.common.auth_policy import PolicyDecision

    return PolicyDecision(
        action_id="model.activate",
        decision="permit",
        required_roles=("sys_admin",),
        matched_roles=("sys_admin",),
        actor_id="ops.operator@example.org",
        target_type="model_instance",
        target_id=M1_MODEL_ID,
        reason="matched sys_admin",
        reason_code="policy.matched",
        roles=("sys_admin",),
        execution_mode="backend_route_executed",
        no_mutation_expected=False,
        auth_mode="live_idp",
        live_backend_auth_executed=True,
        provider_metadata=None,
        role_mapping_result=None,
    )


def _refusal_from_hook() -> StateCloneCutoverRefusedError:
    """Refusal shaped like the hook's raise on an unequal-fingerprint gate."""
    return StateCloneCutoverRefusedError(
        source_id="gfs",
        refusal_scope="unequal_fingerprint",
        refusal_code=STATE_CLONE_COLD_START_APPROVAL_REQUIRED,
    )


def test_record_state_clone_refusal_audit_writes_outside_tx_and_returns_stable_code_result() -> (
    None
):
    """Locks the outside-tx refusal-audit contract at the helper boundary.

    ``PsycopgModelRegistryStore._record_state_clone_refusal_audit`` is
    invoked from ``model_lifecycle_operation`` AFTER the lifecycle
    transaction has already rolled back (StateCloneCutoverRefusedError
    propagated out of ``_dispatch_pre_activation_hooks``). It must:

    * open exactly ONE fresh transaction (PostgreSQL has no autonomous
      sub-transactions, so this fresh tx is the only durability path);
    * emit an ``INSERT INTO ops.audit_log`` with the stable action code
      ``state_clone_cold_start_approval_required``, the target model
      as ``entity_id``, and details naming the blocked scope;
    * return a refused-shape result whose ``error.code`` /
      ``error.details`` / ``audit_reference.log_id`` mirror the audit
      row so downstream API consumers key uniformly off ``status``.

    Fold-at-intro (Epic #982 SUB-5 round-1 test-coverage note 1): the
    outside-tx audit surface was previously only exercised through the
    hook boundary; this test locks the helper's return-shape contract
    and the fresh-tx invariant directly.
    """

    store = _RefusalCaptureStore()
    ctx = _make_ctx_with_approval(
        source_scope=("gfs",),
        previous_active_model=_previous_active_row(),
        approval=None,
    )
    refusal = _refusal_from_hook()
    policy_decision = _refusal_policy_decision()
    preflight = {"prior_audit_log_id": 999}

    result = store._record_state_clone_refusal_audit(
        activation_context=ctx,
        refusal=refusal,
        policy_decision=policy_decision,
        request_id="req-abc-123",
        operation="activate",
        preflight=preflight,
    )

    # Outside-tx invariant: exactly ONE fresh _transaction() opened AND
    # entered AND exited. Proves the "PostgreSQL has no autonomous
    # sub-transactions, so open a new tx after the rollback" contract.
    assert len(store.opened_transactions) == 1
    tx = store.opened_transactions[0]
    assert tx.entered is True
    assert tx.exited is True

    # The audit INSERT hit the fresh cursor with the expected stable
    # code and target identity. We assert on the audit_log INSERT
    # keywords rather than exact SQL text to keep the assertion robust
    # against whitespace / formatting drift.
    executed = tx._cursor.statements  # noqa: SLF001 - test introspection
    assert len(executed) == 1
    sql_text, params = executed[0]
    normalized = " ".join(sql_text.split())
    assert "INSERT INTO ops.audit_log" in normalized
    assert "RETURNING log_id" in normalized

    (
        actor_id,
        actor_role,
        action_code,
        entity_id,
        details_json,
    ) = params
    assert actor_id == policy_decision.actor_id
    assert actor_role == "sys_admin"
    assert action_code == STATE_CLONE_COLD_START_APPROVAL_REQUIRED
    assert entity_id == M1_MODEL_ID
    # Details are wrapped in psycopg2's Json adapter; render via str().
    from psycopg2.extras import Json as _Json

    assert isinstance(details_json, _Json)
    details_text = str(details_json)
    for expected in (
        BASIN_VERSION_ID,
        "gfs",
        "unequal_fingerprint",
        STATE_CLONE_COLD_START_APPROVAL_REQUIRED,
        M1_MODEL_ID,
    ):
        assert expected in details_text, (
            f"expected {expected!r} inside audit details JSON, got {details_text!r}"
        )

    # Returned result: refused-shape, stable code on error.code,
    # blocked scope on error.details, audit_reference.log_id present
    # and matching the RETURNING row.
    assert result["status"] == "refused"
    assert result["operation"] == "activate"
    assert result["error"]["code"] == STATE_CLONE_COLD_START_APPROVAL_REQUIRED
    assert result["error"]["message"]  # non-empty message
    assert result["error"]["details"] == {
        "basin_version_id": BASIN_VERSION_ID,
        "source_id": "gfs",
        "refusal_scope": "unequal_fingerprint",
    }
    assert result["audit_reference"] == {
        "entity_type": "model_instance",
        "entity_id": M1_MODEL_ID,
        "log_id": store.refusal_log_id,
    }
    assert result["preflight"] is preflight


# --- Module import guard for CYCLE_ID (used only in docstrings) -----------


def test_module_imports_shared_constants() -> None:
    """Sanity: the fixtures share the same constants as SUB-2 tests.

    Guarding this trivial re-export catches an accidental rename that
    would silently divert this test module from the SUB-2 fixtures.
    """
    assert isinstance(CYCLE_ID, str) and CYCLE_ID
    assert isinstance(Path(str(CUTOVER_VALID_TIME)), Path)
