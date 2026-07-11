"""Fingerprint-gated state clone (Epic #982 SUB-2).

At cutover from a legacy model ``M0`` to a direct-grid variant ``M1`` this
module clones the latest qualified ``(M0, source, t*)`` snapshot row in
``hydro.state_snapshot`` into ``(M1, source, t*)`` — but only when the
``M0`` and ``M1`` model packages have byte-identical
``hydrologic_core_fingerprint`` values under docs §Gate G10. The physical
SHUD state file is NOT copied (hydrologic core and mesh are identical under
INV-2, so the same on-NFS file is legally reusable by ``M1``); only the DB
index row is duplicated with ``M1`` model identity + package version.

Spec authority
--------------
``openspec/changes/mapping-variant-state-compatibility/specs/fingerprint-gated-state-clone/spec.md``
requirements ``Fingerprint-gated state clone at cutover``,
``The clone executes per source across the activation source scope``, and
``Fingerprint gate inputs are pinned to package and evidence authorities``.

Refusal contract
----------------
Every rejection surfaces the stable error code
``state_clone_cold_start_approval_required`` (docs §11.3 clause 2) and
records a compact refusal audit record whose ``refusal_scope`` names WHY
the clone was blocked. The five distinguished refusal scopes are:

* ``degenerate_gate_inputs`` — ``state_schema_bytes`` or
  ``solver_config_bytes`` is empty. Prevents a symmetric-empty degenerate
  fingerprint from false-passing the equality gate.
* ``missing_qualified_source`` — no ``(M0, source, t*)`` row exists.
* ``stale_latest_snapshot`` — a ``(M0, source, valid_time < t*)`` row
  exists but the ``valid_time == t*`` row does not (Gate G10 condition 4;
  the strict validator would reject a stale checkpoint anyway).
* ``unequal_fingerprint`` — ``verify_hydrologic_core_fingerprint_equal``
  raises ``HydrologicCoreFingerprintMismatchError``.
* ``evidence_fingerprint_mismatch`` — the recomputed ``M1`` fingerprint
  passes the equality gate but does NOT match the value recorded in the
  ``M1`` mapping evidence package, so the core-invariance claim the clone
  relies on is not proven for the supplied inputs.

The refusal is fail-closed: no ``(M1, source, t*)`` row is written; no
physical file is touched.

Provenance columns
------------------
The three ``hydro.state_snapshot`` columns added by migration ``000046`` —
``cloned_from_state_id``, ``cloned_from_model_id``, ``clone_gate_fingerprint``
— stay ``NULL`` on this write. Populating them is the SUB-3 task per Epic
#982; SUB-2's contract is the fingerprint gate + column-disposition write
only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from packages.common.state_manager import (
    StateSnapshot,
    state_snapshot_id,
)
from workers.mapping_builder.rewrite import (
    HydrologicCoreFingerprintMismatchError,
    verify_hydrologic_core_fingerprint_equal,
)

__all__ = [
    "STATE_CLONE_COLD_START_APPROVAL_REQUIRED",
    "StateCloneAuditRecorder",
    "StateCloneRepository",
    "StateCloneResult",
    "fingerprint_gated_state_clone",
]


# Stable error code the refusal path surfaces (spec §
# "Unequal fingerprint refuses the clone fail-closed"). Downstream evidence
# and cold-start approval routing key on this exact string; do not rename.
STATE_CLONE_COLD_START_APPROVAL_REQUIRED = "state_clone_cold_start_approval_required"

# Gate G10 condition 4: the qualified source snapshot is the +12h successor
# checkpoint. Pinned here so the qualification check cannot silently drift.
_QUALIFIED_LEAD_HOURS = 12


class StateCloneRepository(Protocol):
    """Repository shape the clone needs to lookup + write state_snapshot rows.

    Deliberately narrower than
    :class:`packages.common.state_manager.StateSnapshotRepository` — this
    protocol covers exactly the three operations the fingerprint-gated
    clone performs, plus a source-scoped "latest before t*" lookup so the
    stale-latest-snapshot refusal path can distinguish itself from the
    missing-source path (docs §Gate G10 condition 4).
    """

    def get_state_snapshot_by_model_time(
        self,
        *,
        model_id: str,
        valid_time: datetime,
        source_id: str | None = None,
        cycle_id: str | None = None,
        lead_hours: int | None = None,
    ) -> StateSnapshot | None: ...

    def get_latest_state_before(
        self,
        *,
        model_id: str,
        source_id: str,
        before_time: datetime,
    ) -> StateSnapshot | None: ...

    def upsert_state_snapshot(self, snapshot: StateSnapshot) -> StateSnapshot: ...


class StateCloneAuditRecorder(Protocol):
    """Sink for refusal audit records.

    A single ``record_refusal(mapping)`` entry point keeps the shape stable
    across the five refusal scopes. Wiring this to ``ops.audit_log`` is
    the caller's responsibility (SUB-4 / atomic-cutover-transaction owns
    the transaction plumbing); this module only emits records.
    """

    def record_refusal(self, record: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True)
class StateCloneResult:
    """Outcome of a :func:`fingerprint_gated_state_clone` call.

    On success ``cloned_row`` is the persisted ``(M1, source, t*)`` row
    returned by the repository's ``upsert_state_snapshot`` and ``refused``
    is ``False``. On refusal ``cloned_row`` is ``None``, ``refused`` is
    ``True``, ``refusal_code`` is
    :data:`STATE_CLONE_COLD_START_APPROVAL_REQUIRED`, and
    ``refusal_scope`` names one of the five distinguished scopes
    documented in this module's docstring.
    """

    cloned_row: StateSnapshot | None
    refused: bool
    refusal_code: str | None
    refusal_scope: str | None


def fingerprint_gated_state_clone(
    *,
    m0_model_id: str,
    m1_model_id: str,
    m1_model_package_version: str,
    m1_model_package_checksum: str,
    source_id: str,
    cutover_valid_time: datetime,
    m0_package_root: Path,
    m1_package_root: Path,
    m0_sp_att_path: Path,
    m1_sp_att_path: Path,
    m1_category_files: Mapping[str, Sequence[str]],
    m1_recorded_hydrologic_core_fingerprint: str,
    state_schema_bytes: bytes,
    solver_config_bytes: bytes,
    repository: StateCloneRepository,
    audit_recorder: StateCloneAuditRecorder,
) -> StateCloneResult:
    """Clone the qualified ``(M0, source, t*)`` snapshot into ``(M1, source, t*)``.

    Gates the clone on the ten-surface ``hydrologic_core_fingerprint`` gate
    (``workers/mapping_builder/rewrite.py::verify_hydrologic_core_fingerprint_equal``)
    with pinned input authorities (docs §Gate G10 clauses):

    * Both package roots resolved from each model's
      ``core.model_instance.model_package_uri`` NFS path (the caller does
      the resolution).
    * ``category_files`` and both ``.sp.att`` paths from the ``M1``
      variant's mapping manifest / mapping evidence package — same inputs
      that produced the build-time G4 fingerprint.
    * Real platform-level ``state_schema_bytes`` / ``solver_config_bytes``
      (empty bytes refused fail-closed so a symmetric-degenerate input
      cannot false-pass the gate).
    * Cross-check the recomputed ``M1`` fingerprint against the evidence
      package's recorded ``hydrologic_core_fingerprint`` value.

    The physical SHUD state file is NEVER read or copied. The clone row
    preserves ``state_uri`` and ``checksum`` verbatim from the source row
    (INV-2: hydrologic core and mesh are identical, so the same on-NFS
    file is naturally legal under both model identities); this function
    therefore takes no filesystem / object-store handles for the state
    file itself.

    Column disposition on a successful clone (spec §
    "The clone row's full column disposition is pinned"):

    * Preserved verbatim from the source: ``state_uri``, ``checksum``,
      ``source_id``, ``valid_time``, ``cycle_id``, ``lead_hours``,
      ``usable_flag``, ``original_shud_filename``, ``run_id`` (physical
      producer identity per docs §Decision 3 — attribution to ``M1`` is
      via ``model_id`` + ``cloned_from_*``, never ``run_id`` alone).
    * Overwritten to the target: ``model_id`` = ``M1``;
      ``model_package_version`` = ``M1`` package version (the value the
      strict validators compare — chain_forecast_state /
      state_manager reject on version mismatch);
      ``model_package_checksum`` = ``M1`` package checksum.
    * Minted new: ``state_id`` via
      ``packages.common.state_manager.state_snapshot_id`` under the
      ``M1`` identity + preserved lineage inputs, so the clone row's ID
      embeds the new model identity and cannot collide with the source
      row's ID.
    * Left ``NULL`` in this SUB: ``cloned_from_state_id``,
      ``cloned_from_model_id``, ``clone_gate_fingerprint`` — provenance
      write is SUB-3's contract per Epic #982.

    Refusal paths write no row and return a :class:`StateCloneResult`
    with ``refused=True`` and ``refusal_code`` set to
    :data:`STATE_CLONE_COLD_START_APPROVAL_REQUIRED` (docs §11.3 clause 2
    routes this into the explicit cold-start approval path).
    """

    audit_context = _build_audit_context(
        m0_model_id=m0_model_id,
        m1_model_id=m1_model_id,
        source_id=source_id,
        cutover_valid_time=cutover_valid_time,
    )

    # 1. Degenerate gate inputs. Empty state_schema_bytes / solver_config_bytes
    #    on both sides would collapse to a shared trivial hash (SHA-256 of
    #    the empty string) and false-pass the equality gate; refuse before
    #    invoking the fingerprint computation.
    if not state_schema_bytes or not solver_config_bytes:
        return _refuse(
            audit_recorder,
            audit_context,
            scope="degenerate_gate_inputs",
        )

    # 2. Look up the exact-time qualified source snapshot and, if it is
    #    absent, distinguish stale-latest from truly-missing so the audit
    #    record can name the specific G10 clause the caller violated.
    source_snapshot = repository.get_state_snapshot_by_model_time(
        model_id=m0_model_id,
        valid_time=cutover_valid_time,
        source_id=source_id,
        lead_hours=_QUALIFIED_LEAD_HOURS,
    )
    if source_snapshot is None or not _is_qualified_source(source_snapshot):
        latest_before = repository.get_latest_state_before(
            model_id=m0_model_id,
            source_id=source_id,
            before_time=cutover_valid_time,
        )
        if latest_before is not None:
            return _refuse(
                audit_recorder,
                audit_context,
                scope="stale_latest_snapshot",
            )
        return _refuse(
            audit_recorder,
            audit_context,
            scope="missing_qualified_source",
        )

    # 3. Fingerprint equality gate. Reuse the pinned guard — never
    #    reimplement the fingerprint rule (docs §Gate G10 authority).
    try:
        shared_fingerprint = verify_hydrologic_core_fingerprint_equal(
            m0_package_root,
            m1_package_root,
            baseline_sp_att_path=m0_sp_att_path,
            variant_sp_att_path=m1_sp_att_path,
            category_files=m1_category_files,
            baseline_state_schema_bytes=state_schema_bytes,
            variant_state_schema_bytes=state_schema_bytes,
            baseline_solver_config_bytes=solver_config_bytes,
            variant_solver_config_bytes=solver_config_bytes,
        )
    except HydrologicCoreFingerprintMismatchError:
        return _refuse(
            audit_recorder,
            audit_context,
            scope="unequal_fingerprint",
        )

    # 4. Cross-check the recomputed variant fingerprint against the
    #    evidence-recorded value. A gate that passes with equal-but-drifted
    #    inputs would silently break the core-invariance claim; refuse
    #    fail-closed instead.
    if shared_fingerprint.hash != m1_recorded_hydrologic_core_fingerprint:
        return _refuse(
            audit_recorder,
            audit_context,
            scope="evidence_fingerprint_mismatch",
        )

    # 5. Compose and persist the clone row with the pinned column disposition.
    clone_row = _build_clone_row(
        source_snapshot=source_snapshot,
        m1_model_id=m1_model_id,
        m1_model_package_version=m1_model_package_version,
        m1_model_package_checksum=m1_model_package_checksum,
    )
    persisted = repository.upsert_state_snapshot(clone_row)
    return StateCloneResult(
        cloned_row=persisted,
        refused=False,
        refusal_code=None,
        refusal_scope=None,
    )


# --- Internals --------------------------------------------------------------


def _is_qualified_source(snapshot: StateSnapshot) -> bool:
    """Gate G10 qualified predicate: usable + QC-pass + checksum + +12h.

    ``usable_flag == True`` is the QC-passing signal in the current
    schema — ``packages.common.state_manager.StateManager.run_qc`` only
    sets ``usable_flag`` true after QC passes, so a usable snapshot has
    passed the state-variable QC path. ``valid_time == t*`` is enforced
    upstream by the exact-time lookup; here we still check ``lead_hours``
    defensively so a row with a wrong lead never sneaks through.
    """

    if not snapshot.usable_flag:
        return False
    if snapshot.checksum in (None, ""):
        return False
    if snapshot.lead_hours != _QUALIFIED_LEAD_HOURS:
        return False
    return True


def _build_clone_row(
    *,
    source_snapshot: StateSnapshot,
    m1_model_id: str,
    m1_model_package_version: str,
    m1_model_package_checksum: str,
) -> StateSnapshot:
    """Compose the ``(M1, source, t*)`` row with the pinned column disposition.

    ``state_id`` uses the ``state_snapshot_id`` convention under ``M1``'s
    identity + the preserved lineage inputs so the ID embeds ``M1`` and
    is collision-free against the source row's ID. The provenance columns
    added by migration ``000046`` remain ``NULL`` on this write (SUB-3
    populates them).
    """

    return StateSnapshot(
        state_id=state_snapshot_id(
            m1_model_id,
            source_snapshot.valid_time,
            source_id=source_snapshot.source_id,
            cycle_id=source_snapshot.cycle_id,
            lead_hours=source_snapshot.lead_hours,
        ),
        model_id=m1_model_id,
        run_id=source_snapshot.run_id,
        valid_time=source_snapshot.valid_time,
        state_uri=source_snapshot.state_uri,
        checksum=source_snapshot.checksum,
        usable_flag=source_snapshot.usable_flag,
        created_at=None,
        source_id=source_snapshot.source_id,
        cycle_id=source_snapshot.cycle_id,
        lead_hours=source_snapshot.lead_hours,
        model_package_version=m1_model_package_version,
        model_package_checksum=m1_model_package_checksum,
        original_shud_filename=source_snapshot.original_shud_filename,
    )


def _build_audit_context(
    *,
    m0_model_id: str,
    m1_model_id: str,
    source_id: str,
    cutover_valid_time: datetime,
) -> dict[str, Any]:
    return {
        "m0_model_id": m0_model_id,
        "m1_model_id": m1_model_id,
        "source_id": source_id,
        "cutover_valid_time": cutover_valid_time,
    }


def _refuse(
    audit_recorder: StateCloneAuditRecorder,
    audit_context: Mapping[str, Any],
    *,
    scope: str,
) -> StateCloneResult:
    audit_recorder.record_refusal(
        {
            "refusal_code": STATE_CLONE_COLD_START_APPROVAL_REQUIRED,
            "refusal_scope": scope,
            **audit_context,
        }
    )
    return StateCloneResult(
        cloned_row=None,
        refused=True,
        refusal_code=STATE_CLONE_COLD_START_APPROVAL_REQUIRED,
        refusal_scope=scope,
    )
