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
the clone was blocked. The six distinguished refusal scopes are:

* ``reverse_clone_target_not_direct_grid`` — defense-in-depth guard at
  the clone function's own signature (Epic #982 SUB-7 §4.1). The target
  ``M1`` model does NOT classify as direct-grid under Change 4's single
  classifier (``workers.forcing_producer.direct_grid_contract.load_forcing_mapping_contract_from_manifest``);
  the contract is absent, malformed, or non-``direct_grid``. Enforces the
  one-way channel invariant (state flows legacy → direct-grid but NEVER
  direct-grid → legacy) at the clone signature so no future caller can
  bypass Change 4's legacy-reactivation guard. Fail-closed, no override.
* ``degenerate_gate_inputs`` — ``state_schema_bytes`` or
  ``solver_config_bytes`` is empty. Prevents a symmetric-empty degenerate
  fingerprint from false-passing the equality gate. In
  ``transfer_mode='recalibration'`` an OMITTED per-side ``m0_*`` override
  refuses here too: reusing the target's bytes for both sides would
  compare the ``state_schema`` surface against itself.
* ``missing_qualified_source`` — no ``(M0, source, t*)`` row exists.
* ``stale_latest_snapshot`` — a ``(M0, source, valid_time < t*)`` row
  exists but the ``valid_time == t*`` row does not (Gate G10 condition 4;
  the strict validator would reject a stale checkpoint anyway).
* ``unequal_fingerprint`` — ``verify_hydrologic_core_fingerprint_equal``
  raises ``HydrologicCoreFingerprintMismatchError``. Fingerprint-unequal
  ``M1 → M1'`` fix-forward candidates surface here; docs §11.3 clause 2
  routes this stable code into the explicit cold-start approval path.
* ``evidence_fingerprint_mismatch`` — the recomputed ``M1`` fingerprint
  passes the equality gate but does NOT match the value recorded in the
  ``M1`` mapping evidence package, so the core-invariance claim the clone
  relies on is not proven for the supplied inputs. In
  ``transfer_mode='fix_forward'`` an ABSENT or empty recorded value also
  refuses here — the fix-forward cross-check obligation does not weaken.

Change ``recalibration-state-carryover`` adds a seventh scope, reachable
only from the opt-in ``transfer_mode='recalibration'`` route:

* ``state_compatibility_unequal`` — the eight-surface
  ``STATE_COMPATIBILITY_SURFACES`` gate found the two packages unequal, or
  a hydrologic-core file declared by the enumeration is present on exactly
  one side (``MissingPackageFileError``). A file present on one side and
  absent on the other IS surface inequality, so both map to this one
  scope; the audit record carries the missing side and relative path so an
  operator can locate the file.

Transfer modes
--------------
``transfer_mode='fix_forward'`` (the default) is the ten-surface
package-identity gate — every existing caller keeps byte-identical
behavior. ``transfer_mode='recalibration'`` engages the eight-surface
state-TRANSFERABILITY gate: the ten G10 labels minus ``calibration``
(``cfg.calib`` + ``CALIB/*``) and ``solver_config`` (``cfg.para``), so a
calibration-parameter-only ``M1 -> M1'`` update carries its state over
instead of cold-starting. Every other surface — mesh, river, lake, soil,
geol, land, ``.sp.att`` non-``FORC``, and the ``cfg.ic`` state schema —
still refuses on any drift.

The refusal is fail-closed: no ``(M1, source, t*)`` row is written; no
physical file is touched.

Provenance columns
------------------
The three ``hydro.state_snapshot`` columns added by migration ``000046`` —
``cloned_from_state_id``, ``cloned_from_model_id``, ``clone_gate_fingerprint``
— are populated on a successful clone (Epic #982 SUB-3, §2.2):

* ``cloned_from_state_id`` = the source ``M0`` snapshot ``state_id``.
* ``cloned_from_model_id`` = the source ``M0`` ``model_id``.
* ``clone_gate_fingerprint`` = the fingerprint hash the equality gate
  accepted on for this clone (docs §Gate G10 authority for the
  ten-surface gate).

Migration ``000053`` adds a fourth: ``clone_gate_kind`` names WHICH gate
admitted the row — ``"hydrologic_core"`` on a ``fix_forward`` clone and
``"state_compatibility"`` on a ``recalibration`` clone — so an auditor can
tell from the row alone which surface set was proven equal, and so the two
kinds' ``clone_gate_fingerprint`` values are never compared to each other.

The MUST-level attribution rule: a warm-start-lineage read attributes the
state to ``M1`` via ``model_id`` + ``cloned_from_*`` — never via
``run_id`` alone, because ``run_id`` still points at the ``M0`` producing
run (docs §Decision 3). See ``tests/test_state_clone.py`` for the
attribution-rule test.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from packages.common.state_manager import (
    StateSnapshot,
    state_snapshot_id,
)
from workers.forcing_producer.direct_grid_contract import (
    DirectGridContractError,
    load_forcing_mapping_contract_from_manifest,
)
from workers.mapping_builder.rewrite import (
    HYDROLOGIC_CORE_FINGERPRINT_LABELS,
    STATE_COMPATIBILITY_SURFACES,
    HydrologicCoreFingerprintMismatchError,
    MissingPackageFileError,
    verify_hydrologic_core_fingerprint_equal,
)

__all__ = [
    "CLONE_GATE_KIND_HYDROLOGIC_CORE",
    "CLONE_GATE_KIND_STATE_COMPATIBILITY",
    "STATE_CLONE_COLD_START_APPROVAL_REQUIRED",
    "STATE_COMPATIBILITY_UNEQUAL",
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

# SUB-7 §4.1 defense-in-depth refusal scope. Kept as a module-level
# constant so downstream audit-consumer tests can key on the exact literal
# and cannot silently diverge on typo.
_REVERSE_CLONE_TARGET_NOT_DIRECT_GRID = "reverse_clone_target_not_direct_grid"

# Change `recalibration-state-carryover` refusal scope: the eight-surface
# state-compatibility gate found the two packages unequal (or a declared
# hydrologic-core file present on exactly one side). Module-level so
# downstream audit consumers key on the exact literal.
STATE_COMPATIBILITY_UNEQUAL = "state_compatibility_unequal"

# `clone_gate_kind` values (migration 000053 / D6). The clone row names the
# gate that admitted it so the recorded `clone_gate_fingerprint` is
# self-describing.
CLONE_GATE_KIND_HYDROLOGIC_CORE = "hydrologic_core"
CLONE_GATE_KIND_STATE_COMPATIBILITY = "state_compatibility"


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
    across the six refusal scopes. Wiring this to ``ops.audit_log`` is
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
    ``refusal_scope`` names one of the distinguished scopes documented in
    this module's docstring — the six fix-forward scopes plus
    :data:`STATE_COMPATIBILITY_UNEQUAL`, which is reachable only from
    ``transfer_mode='recalibration'``.
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
    m1_recorded_hydrologic_core_fingerprint: str | None,
    state_schema_bytes: bytes,
    solver_config_bytes: bytes,
    m1_forcing_mapping_manifest: Mapping[str, Any],
    repository: StateCloneRepository,
    audit_recorder: StateCloneAuditRecorder,
    transfer_mode: Literal["fix_forward", "recalibration"] = "fix_forward",
    m0_state_schema_bytes: bytes | None = None,
    m0_solver_config_bytes: bytes | None = None,
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
    * Populated per Epic #982 SUB-3: ``cloned_from_state_id`` = source
      ``M0`` ``state_id``; ``cloned_from_model_id`` = ``M0`` ``model_id``;
      ``clone_gate_fingerprint`` = the accepted equality-gate
      ``hydrologic_core_fingerprint`` hash. Attribution to ``M1`` reads
      ``model_id`` + ``cloned_from_*``, never ``run_id`` alone (docs
      §Decision 3).

    Refusal paths write no row and return a :class:`StateCloneResult`
    with ``refused=True`` and ``refusal_code`` set to
    :data:`STATE_CLONE_COLD_START_APPROVAL_REQUIRED` (docs §11.3 clause 2
    routes this into the explicit cold-start approval path).

    No-reverse-clone guard (SUB-7 §4.1)
    -----------------------------------
    The ``m1_forcing_mapping_manifest`` kwarg is the ``M1`` target's
    forcing-mapping manifest / resource-profile ``direct_grid_forcing``
    section. Before any other check, this function classifies the target
    through Change 4's single classifier
    (``workers.forcing_producer.direct_grid_contract.load_forcing_mapping_contract_from_manifest``);
    if the classifier returns ``None`` or raises
    :class:`DirectGridContractError` — i.e. the contract is absent,
    malformed, or non-``direct_grid`` — the clone refuses fail-closed
    with ``refusal_scope='reverse_clone_target_not_direct_grid'`` and no
    override. This defense-in-depth check at the clone signature
    guarantees that no future caller can bypass Change 4's
    legacy-reactivation guard by driving the clone at a legacy target.

    Transfer modes (change ``recalibration-state-carryover`` D2)
    -----------------------------------------------------------
    ``transfer_mode='fix_forward'`` (default) runs every check above, in
    the order above, over the ten-surface
    :data:`workers.mapping_builder.rewrite.HYDROLOGIC_CORE_FINGERPRINT_LABELS`.
    Behavior is byte-identical to the pre-change function for every
    existing caller.

    ``transfer_mode='recalibration'`` runs the same pre-gate checks
    unconditionally — the no-reverse-clone guard, the degenerate-inputs
    refusal (which still requires BOTH byte inputs non-empty even though
    ``solver_config`` does not enter the eight-surface hash: one
    unconditional check, no mode-conditional branch, no drift) and the
    qualified-source lookup — and then gates on
    :data:`workers.mapping_builder.rewrite.STATE_COMPATIBILITY_SURFACES`.
    A ``HydrologicCoreFingerprintMismatchError`` OR a
    :class:`~workers.mapping_builder.rewrite.MissingPackageFileError`
    refuses with ``refusal_scope='state_compatibility_unequal'``: a
    hydrologic-core file present on one side and absent on the other IS
    surface inequality. ``fix_forward`` keeps propagating
    ``MissingPackageFileError`` to its caller, byte-identically.

    Parameter reading under each mode (D8 naming note)
    --------------------------------------------------
    The ``m0_*`` / ``m1_*`` parameters originally meant "baseline" /
    "variant". Under ``transfer_mode='recalibration'`` they mean transfer
    **source** (``M1``) / transfer **target** (``M1'``). The parameters
    are NOT renamed, so both readings are stated here: ``m0_*`` is always
    the package the state comes FROM and ``m1_*`` is always the package
    the state is carried TO; the clone row takes the ``m1_*`` identity and
    records ``cloned_from_model_id = m0_model_id``.

    Per-side gate bytes (D3)
    ------------------------
    ``state_schema_bytes`` / ``solver_config_bytes`` are the ``m1``
    (target) side's bytes and stay required. ``m0_state_schema_bytes`` /
    ``m0_solver_config_bytes`` are optional per-side overrides for the
    ``m0`` side; when either is ``None`` the target's bytes are used for
    both sides, which is exactly today's behavior and is correct for the
    July baseline→variant cutover (the variant copies the baseline's
    ``cfg.ic`` verbatim, so the bytes are genuinely shared).

    Under ``M1 -> M1'`` that fallback would be a FALSE PASS: if ``M1'``
    ships a new ``cfg.ic``, feeding ``M1'``'s bytes to both sides makes
    the ``state_schema`` surface compare equal and admits a clone this
    change explicitly must refuse. In ``recalibration`` both overrides are
    therefore REQUIRED, read per package root: an omitted (``None``) or a
    supplied-but-empty override refuses with ``degenerate_gate_inputs``
    like the required inputs, so a caller cannot reach the false pass by
    leaving the argument at its default. The ``None``-means-reuse-the-
    target fallback survives unchanged in ``fix_forward``.

    Evidence cross-check waiver (D5)
    --------------------------------
    ``m1_recorded_hydrologic_core_fingerprint`` is the value recorded in
    the ``M1`` mapping evidence package. In ``fix_forward`` it stays
    REQUIRED: an absent or empty value refuses with the existing
    ``evidence_fingerprint_mismatch`` scope, so the fix-forward contract
    does not weaken. In ``recalibration`` it MAY be ``None`` — the
    direct-grid variants that route operates on are produced by
    ``scripts/provision_direct_grid_scheduler_registry.py``, which records
    no ``hydrologic_core_fingerprint`` at all — in which case the
    cross-check is SKIPPED rather than satisfied vacuously by the caller
    echoing back the value the gate just computed. A recalibration caller
    that does supply a recorded value has it cross-checked against this
    mode's own eight-surface recompute.

    ``clone_gate_kind``
    -------------------
    The persisted row records which gate admitted it:
    :data:`CLONE_GATE_KIND_HYDROLOGIC_CORE` for ``fix_forward`` and
    :data:`CLONE_GATE_KIND_STATE_COMPATIBILITY` for ``recalibration``.
    """

    recalibration = transfer_mode == "recalibration"
    gate_surfaces = (
        STATE_COMPATIBILITY_SURFACES
        if recalibration
        else HYDROLOGIC_CORE_FINGERPRINT_LABELS
    )
    clone_gate_kind = (
        CLONE_GATE_KIND_STATE_COMPATIBILITY
        if recalibration
        else CLONE_GATE_KIND_HYDROLOGIC_CORE
    )

    audit_context = _build_audit_context(
        m0_model_id=m0_model_id,
        m1_model_id=m1_model_id,
        source_id=source_id,
        cutover_valid_time=cutover_valid_time,
    )

    # 0. No-reverse-clone guard (SUB-7 §4.1). Classify the M1 target
    #    through Change 4's single classifier BEFORE any other gate check
    #    so the one-way channel invariant (state legacy → direct-grid,
    #    NEVER direct-grid → legacy) is enforced at the clone function's
    #    own signature. Absent, malformed, or non-`direct_grid` classifies
    #    as legacy-mapping — refuse fail-closed with no override so no
    #    future caller can bypass Change 4's legacy-reactivation guard.
    try:
        classified_contract = load_forcing_mapping_contract_from_manifest(
            dict(m1_forcing_mapping_manifest)
        )
    except DirectGridContractError:
        classified_contract = None
    if classified_contract is None:
        return _refuse(
            audit_recorder,
            audit_context,
            scope=_REVERSE_CLONE_TARGET_NOT_DIRECT_GRID,
        )

    # 1. Degenerate gate inputs. Empty state_schema_bytes / solver_config_bytes
    #    on both sides would collapse to a shared trivial hash (SHA-256 of
    #    the empty string) and false-pass the equality gate; refuse before
    #    invoking the fingerprint computation. The check is unconditional in
    #    both modes — solver_config does not enter the eight-surface hash,
    #    but one unconditional check cannot drift out of sync with a mode
    #    branch. A supplied-but-empty m0 override is degenerate for the same
    #    reason; ``None`` means "reuse the target bytes" and is not empty --
    #    but only in ``fix_forward``; see 1b for the recalibration rule.
    if not state_schema_bytes or not solver_config_bytes:
        return _refuse(
            audit_recorder,
            audit_context,
            scope="degenerate_gate_inputs",
        )
    if m0_state_schema_bytes is not None and not m0_state_schema_bytes:
        return _refuse(
            audit_recorder,
            audit_context,
            scope="degenerate_gate_inputs",
        )
    if m0_solver_config_bytes is not None and not m0_solver_config_bytes:
        return _refuse(
            audit_recorder,
            audit_context,
            scope="degenerate_gate_inputs",
        )
    # 1b. In ``recalibration`` an OMITTED m0 override is degenerate too. The
    #     ``None`` -> "reuse the target bytes" fallback is correct for
    #     ``fix_forward`` (the variant copies the baseline's cfg.ic verbatim)
    #     but under ``M1 -> M1'`` it would compare the ``state_schema`` surface
    #     against ITSELF and admit exactly the new-cfg.ic clone this mode must
    #     refuse -- the false pass this docstring declares out of bounds. The
    #     precondition "a recalibration caller always supplies both overrides"
    #     is therefore enforced here rather than trusted, so a future caller
    #     falls into a refusal by omission and not into a false pass.
    #     ``m0_solver_config_bytes`` is held to the same rule even though
    #     ``solver_config`` sits outside the eight-surface set: one rule for
    #     both per-side overrides cannot drift out of sync the way a
    #     surface-membership-conditional rule would. ``fix_forward`` is
    #     untouched -- its ``None``-means-reuse-the-target semantics are
    #     byte-identical to before.
    if recalibration and (m0_state_schema_bytes is None or m0_solver_config_bytes is None):
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

    # 3. Fingerprint equality gate over the mode's surface set. Reuse the
    #    pinned guard — never reimplement the fingerprint rule (docs §Gate
    #    G10 authority). The m0 side takes its own bytes when the caller
    #    supplied per-side overrides (D3); otherwise both sides take the
    #    target's bytes, which is the pre-change behavior.
    try:
        shared_fingerprint = verify_hydrologic_core_fingerprint_equal(
            m0_package_root,
            m1_package_root,
            baseline_sp_att_path=m0_sp_att_path,
            variant_sp_att_path=m1_sp_att_path,
            category_files=m1_category_files,
            baseline_state_schema_bytes=(
                state_schema_bytes
                if m0_state_schema_bytes is None
                else m0_state_schema_bytes
            ),
            variant_state_schema_bytes=state_schema_bytes,
            baseline_solver_config_bytes=(
                solver_config_bytes
                if m0_solver_config_bytes is None
                else m0_solver_config_bytes
            ),
            variant_solver_config_bytes=solver_config_bytes,
            surfaces=gate_surfaces,
        )
    except HydrologicCoreFingerprintMismatchError:
        if recalibration:
            return _refuse(
                audit_recorder,
                audit_context,
                scope=STATE_COMPATIBILITY_UNEQUAL,
            )
        return _refuse(
            audit_recorder,
            audit_context,
            scope="unequal_fingerprint",
        )
    except MissingPackageFileError as error:
        # A hydrologic-core file declared by the enumeration but present on
        # exactly one side IS surface inequality — the recalibration route
        # enumerates the union of both package roots precisely so an added
        # or removed file lands here. Fix-forward keeps propagating this to
        # its caller, byte-identically: there the enumeration comes from the
        # M1 mapping evidence package and a missing file is a caller /
        # evidence bug, not a gate outcome.
        if not recalibration:
            raise
        return _refuse(
            audit_recorder,
            audit_context,
            scope=STATE_COMPATIBILITY_UNEQUAL,
            extra={
                "missing_category": error.category,
                "missing_relative_path": error.relative_path,
                "missing_side": error.missing_side,
                "missing_package_root": str(error.package_root),
            },
        )

    # 4. Cross-check the recomputed variant fingerprint against the
    #    evidence-recorded value. A gate that passes with equal-but-drifted
    #    inputs would silently break the core-invariance claim; refuse
    #    fail-closed instead. In fix_forward an absent/empty recorded value
    #    refuses here too (``hash != None`` is True), so the cross-check
    #    obligation cannot be waived by simply omitting the argument. In
    #    recalibration ``None`` skips the cross-check explicitly (D5): the
    #    dg variants this route operates on carry no recorded value, and
    #    accepting the caller's echo of the value the gate just computed
    #    would be a vacuous self-supply, not evidence.
    skip_evidence_cross_check = (
        recalibration and m1_recorded_hydrologic_core_fingerprint is None
    )
    if (
        not skip_evidence_cross_check
        and shared_fingerprint.hash != m1_recorded_hydrologic_core_fingerprint
    ):
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
        clone_gate_fingerprint=shared_fingerprint.hash,
        clone_gate_kind=clone_gate_kind,
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
    clone_gate_fingerprint: str,
    clone_gate_kind: str = CLONE_GATE_KIND_HYDROLOGIC_CORE,
) -> StateSnapshot:
    """Compose the ``(M1, source, t*)`` row with the pinned column disposition.

    ``state_id`` uses the ``state_snapshot_id`` convention under ``M1``'s
    identity + the preserved lineage inputs so the ID embeds ``M1`` and
    is collision-free against the source row's ID. The three provenance
    columns added by migration ``000046`` are populated per Epic #982
    SUB-3: ``cloned_from_state_id`` / ``cloned_from_model_id`` name the
    ``M0`` origin row + model, and ``clone_gate_fingerprint`` records the
    accepted equality-gate hash so downstream audit can pin exactly which
    fingerprint proved core-invariance for this row. Migration ``000053``'s
    ``clone_gate_kind`` names the gate that hash belongs to, so an auditor
    never compares a ten-surface value against an eight-surface one.
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
        cloned_from_state_id=source_snapshot.state_id,
        cloned_from_model_id=source_snapshot.model_id,
        clone_gate_fingerprint=clone_gate_fingerprint,
        clone_gate_kind=clone_gate_kind,
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
    extra: Mapping[str, Any] | None = None,
) -> StateCloneResult:
    """Record a refusal audit record and return the fail-closed result.

    ``extra`` is merged into the record so a scope that has locating
    information to offer — the state-compatibility scope carries the
    missing side and relative path — can supply it WITHOUT changing the
    record shape of any existing refusal, all of which pass ``extra=None``
    and therefore emit exactly the record they emitted before.
    """

    audit_recorder.record_refusal(
        {
            "refusal_code": STATE_CLONE_COLD_START_APPROVAL_REQUIRED,
            "refusal_scope": scope,
            **audit_context,
            **(dict(extra) if extra else {}),
        }
    )
    return StateCloneResult(
        cloned_row=None,
        refused=True,
        refusal_code=STATE_CLONE_COLD_START_APPROVAL_REQUIRED,
        refusal_scope=scope,
    )
