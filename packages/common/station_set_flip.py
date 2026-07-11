"""Station-flag flip hook mounted on the Change 4 pre-activation extension point.

Epic #992 SUB-1 (``direct-grid-display-cutover`` tasks.md §1.1) wires the
target-identity-scoped ``met.met_station.active_flag`` re-pointer into the
ordered pre-activation hook chain committed by Change 4
(``source-specific-model-variant-routing``). The hook runs INSIDE the same
lifecycle transaction that marks ``M1`` active and the previous active model
``M0`` ``superseded``; any refusal it raises rolls back the WHOLE transaction,
so at no committed instant is the visible station set the union of two
generations (design §Decision 1 "single-track atomic flip").

Extension-point contract
------------------------
* Consumer of :class:`packages.common.model_registry.PreActivationHook`
  +
  :class:`packages.common.model_registry.ModelActivationContext`
  + :data:`packages.common.model_registry.PRE_ACTIVATION_HOOK_MOUNT_POINTS`.
  This module DEFINES no extension point; it only registers on the
  reserved ``station_flag_flip`` mount point Change 4 committed
  (``PRE_ACTIVATION_HOOK_MOUNT_POINTS[1] == "station_flag_flip"``,
  ``packages/common/model_registry.py:272``).
* Ordering invariant with Epic #982 SUB-4
  (:mod:`packages.common.state_clone_hook`): the ``state_clone`` mount
  runs FIRST, ``station_flag_flip`` runs AFTER. Both share the same
  cursor. If either raises, Change 4's dispatcher aborts the whole
  transaction, so this hook's UPDATEs and the state-clone hook's inserts
  either commit together or roll back together (D7).

Reference exemplar
------------------
:mod:`packages.common.state_clone_hook` is the exact pattern this module
mirrors — same closure-injected audit recorder, same audited-skip
protocol, same self-gating engagement classifier, same "raise inside the
tx rolls back the whole tx" contract.

Applicability predicate (audited skip semantics)
------------------------------------------------
The hook fires per activation but engages the flip only when both:

* the target classifies as direct-grid under Change 4's single classifier
  (:func:`workers.forcing_producer.direct_grid_contract.load_forcing_mapping_contract_from_manifest`
  returns a valid :class:`DirectGridForcingContract` from
  ``target.resource_profile.direct_grid_forcing``); AND
* the activation context carries a ``previous_active_model`` (there is a
  legacy ``M0`` display set to cut over from).

Otherwise the hook records an audited SKIP and returns without touching
any ``met.met_station`` row:

* :data:`SKIP_REASON_TARGET_NOT_DIRECT_GRID` — target is legacy IDW OR
  the resource profile declares direct-grid intent but the classifier
  fails-closed (``load_forcing_mapping_contract_from_manifest`` returns
  ``None`` OR raises :class:`DirectGridContractError`). Existing legacy
  lifecycle behavior on the 13 production basins is left byte-for-byte
  unchanged — a routine ``activate`` / ``switch_version`` /
  ``rollback_version`` never re-flips their station rows.
* :data:`SKIP_REASON_NO_PREVIOUS_ACTIVE_MODEL` — fresh-basin activation
  (docs §12; a first activation for a scope has no display set to cut
  over from). Fresh-basin display bring-up is owned by Change 7.

Two-step flip semantics
-----------------------
When engaged, the hook issues TWO SQL statements in order on the caller's
transaction cursor:

1. ``UPDATE met.met_station SET active_flag = false``
   ``WHERE basin_version_id = %s AND active_flag = true`` —
   deterministic starting point: turn EVERY currently-on row of the
   basin off. This covers legacy rows (``grid_snapshot_id IS NULL``)
   AND every non-target direct-grid generation's mirrors, which is what
   makes the direct→direct′ fix-forward re-flip correct: the committed
   set is NEVER ``M1 ∪ M1′``.
2. ``UPDATE met.met_station SET active_flag = true`` matched against
   the target's built mapping-asset identity: ``basin_version_id``,
   ``station_role='direct_grid_cache'``, ``properties_json->>'model_input_package_id'``,
   ``properties_json->>'binding_checksum'``, ``grid_snapshot_id``. These
   are the discriminators Change 4 (Epic #961 SUB-2, #963) MINTED at
   registration in
   :func:`workers.model_registry.direct_grid_variant_registration._upsert_direct_grid_mirror`
   (``properties_json`` binding-identity fields + ``grid_snapshot_id``
   FK); reusing them here is the identity symmetry the design pins.

Invariant: after commit, exactly ONE target's mirror set is
``active_flag=true`` and every other row of the ``basin_version`` is
``active_flag=false``. The station-MVT source query
(``apps/api/routes/hydro_display.py::_station_source_version``) stays
byte-for-byte unchanged; single-track visibility is delivered by row
selection, not by adding a ``model_id`` filter to the query
(design §Decision 1 rejected alternative).

Fail-closed guarantee
---------------------
If the target's registered mirror rows are somehow missing (rowcount of
step 2 == 0), the hook raises :class:`StationFlagFlipError` — a direct-
grid target with no registered mirrors is a Change-4 registration
invariant violation (Epic #961 SUB-2 registers ``active_flag=false``
mirrors atomically with the ``core.model_instance`` row insert). Raising
here rolls back the whole activation transaction so no
"previous set off / target not on" empty-display window ever commits.

The docstring at the top of this module intentionally cross-references
the reserved mount point (``PRE_ACTIVATION_HOOK_MOUNT_POINTS[1]``) and
the exemplar (``state_clone_hook.py``) so a grep from either side lands
back here.

Testability notes
-----------------
The hook takes an ``audit_recorder`` in its closure so tests can inject
in-memory fakes (no live DB). Row-selection SQL is exercised end-to-end
through a fake cursor + a subclass of
:class:`packages.common.model_registry.PsycopgModelRegistryStore`
following the ``tests/test_variant_activation_cutover.py`` harness
pattern.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from packages.common.model_registry import (
    ModelActivationContext,
    PreActivationHook,
)
from workers.forcing_producer.direct_grid_contract import (
    DirectGridContractError,
    load_forcing_mapping_contract_from_manifest,
)

__all__ = [
    "SKIP_REASON_NO_PREVIOUS_ACTIVE_MODEL",
    "SKIP_REASON_TARGET_NOT_DIRECT_GRID",
    "StationFlagFlipError",
    "StationSetFlipAuditRecorder",
    "build_station_flag_flip_hook",
]


# Audited skip reasons the hook records when the flip is NOT engaged.
# Pinned as module-level constants so downstream audit-consumer tests can
# import them and cannot silently diverge on typo. Values kept in lockstep
# with :mod:`packages.common.state_clone_hook` because the two hooks share
# the same applicability-classifier vocabulary at the extension-point
# boundary (Change 4 owns the classifier; hooks reuse its verdict).
SKIP_REASON_NO_PREVIOUS_ACTIVE_MODEL = "no_previous_active_model"
SKIP_REASON_TARGET_NOT_DIRECT_GRID = "target_not_direct_grid"


class StationFlagFlipError(RuntimeError):
    """Raised inside the pre-activation transaction to force rollback.

    The hook raises this when the target-identity flip UPDATE (step 2)
    matches zero rows — a direct-grid target with no registered mirror
    rows is a Change-4 registration invariant violation (Epic #961 SUB-2
    registers mirror rows atomically with the ``core.model_instance``
    insert). Change 4's dispatcher lets this propagate and the whole
    transaction rolls back with no ``active_flag`` change persisted
    (no "previous set off / target not on" empty-display intermediate
    state ever commits).
    """

    def __init__(
        self,
        *,
        basin_version_id: str,
        target_model_id: str,
        model_input_package_id: str,
        binding_checksum: str,
        grid_snapshot_id: str,
    ) -> None:
        super().__init__(
            "Direct-grid target flip matched zero mirror rows "
            f"(basin_version_id={basin_version_id!r}, "
            f"target_model_id={target_model_id!r}, "
            f"model_input_package_id={model_input_package_id!r}, "
            f"binding_checksum={binding_checksum!r}, "
            f"grid_snapshot_id={grid_snapshot_id!r}); "
            "expected at least one Change-4-registered mirror row."
        )
        self.basin_version_id = basin_version_id
        self.target_model_id = target_model_id
        self.model_input_package_id = model_input_package_id
        self.binding_checksum = binding_checksum
        self.grid_snapshot_id = grid_snapshot_id


class StationSetFlipAuditRecorder(Protocol):
    """Sink for skip audit records the hook emits.

    Called only on the two applicability-predicate misses
    (fresh-basin path, legacy-target path). Wiring this to
    ``ops.audit_log`` is the caller's responsibility (Change 4 owns
    transaction plumbing); this module only emits records. On the
    engaged path the hook writes NO audit record — the lifecycle audit
    row already committed by
    :meth:`packages.common.model_registry.PsycopgModelRegistryStore._insert_model_lifecycle_audit`
    is the append-only evidence of the successful cutover.
    """

    def record_skip(self, reason: str, ctx: ModelActivationContext) -> None: ...


# SQL statement 1: turn EVERY currently-on row of the basin OFF.
# Deterministic starting point — covers legacy rows and every non-target
# direct-grid generation's mirrors so the committed set can never be the
# union of two generations (design §Decision 1). The ``active_flag = true``
# predicate keeps the UPDATE from touching rows that are already false
# (no-op savings on large basins; also keeps the audit-observable rowcount
# meaningful).
_TURN_OFF_ALL_SQL = """
UPDATE met.met_station
   SET active_flag = false
 WHERE basin_version_id = %s
   AND active_flag = true
"""


# SQL statement 2: turn ON exactly the target's mirror rows, matched by
# Change 4's built mapping-asset identity discriminators. The
# ``properties_json`` binding-identity fields
# (``model_input_package_id`` + ``binding_checksum``) plus the
# ``grid_snapshot_id`` FK are the SAME discriminators
# :func:`workers.model_registry.direct_grid_variant_registration._upsert_direct_grid_mirror`
# populates at registration; matching on them here IS the identity
# symmetry the design pins.
_TURN_ON_TARGET_SQL = """
UPDATE met.met_station
   SET active_flag = true
 WHERE basin_version_id = %s
   AND station_role = 'direct_grid_cache'
   AND properties_json->>'model_input_package_id' = %s
   AND properties_json->>'binding_checksum' = %s
   AND grid_snapshot_id = %s
"""


def build_station_flag_flip_hook(
    *,
    audit_recorder: StationSetFlipAuditRecorder,
    classifier: Callable[
        [Mapping[str, Any] | None], Any
    ] = load_forcing_mapping_contract_from_manifest,
) -> PreActivationHook:
    """Return the pre-activation hook closure to register at ``station_flag_flip``.

    The returned callable is registered via
    :meth:`packages.common.model_registry.PsycopgModelRegistryStore.register_pre_activation_hook`
    (``"station_flag_flip"``) at bootstrap time. Dependencies are injected
    here as a closure so the module remains free of I/O knowledge and
    stays fake-friendly for unit tests.

    ``classifier`` defaults to
    :func:`workers.forcing_producer.direct_grid_contract.load_forcing_mapping_contract_from_manifest`
    — Change 4's single classifier. Tests override this seam only to
    exercise the fail-closed
    :class:`workers.forcing_producer.direct_grid_contract.DirectGridContractError`
    path deterministically; production callers use the default.
    """

    def _hook(cursor: Any, ctx: ModelActivationContext) -> None:
        # Applicability gate 1: fresh-basin activation is legitimate
        # under docs §12 — no display set to cut over from. Change 7
        # owns fresh-basin bring-up; here we skip audibly and return.
        if ctx.previous_active_model is None:
            audit_recorder.record_skip(SKIP_REASON_NO_PREVIOUS_ACTIVE_MODEL, ctx)
            return

        # Applicability gate 2: run the target through Change 4's single
        # classifier fail-closed. Both a ``None`` return (legacy IDW OR
        # missing/malformed direct-grid section) AND a raised
        # ``DirectGridContractError`` (declared direct-grid intent that
        # failed the parser) are treated as "not direct-grid" — routine
        # legacy lifecycle operations must leave every ``met.met_station``
        # row untouched.
        target_manifest = _extract_target_direct_grid_manifest(ctx.target_model)
        try:
            contract = classifier(target_manifest)
        except DirectGridContractError:
            audit_recorder.record_skip(SKIP_REASON_TARGET_NOT_DIRECT_GRID, ctx)
            return
        if contract is None:
            audit_recorder.record_skip(SKIP_REASON_TARGET_NOT_DIRECT_GRID, ctx)
            return

        # Engaged path: extract the target's registration-side
        # discriminators and issue the two-step flip on the shared cursor.
        model_input_package_id = str(contract.model_input_package_id)
        binding_checksum = str(contract.binding_checksum)
        grid_snapshot_id = _extract_target_grid_snapshot_id(ctx.target_model)

        # Step 1: deterministic turn-off. The whole "currently on" set
        # of the basin is flipped off in one statement; no per-row
        # iteration, no ORDER BY needed (set semantics).
        cursor.execute(_TURN_OFF_ALL_SQL, (ctx.basin_version_id,))

        # Step 2: turn on exactly the target's mirrors.
        cursor.execute(
            _TURN_ON_TARGET_SQL,
            (
                ctx.basin_version_id,
                model_input_package_id,
                binding_checksum,
                grid_snapshot_id,
            ),
        )
        rowcount = getattr(cursor, "rowcount", None)
        if rowcount is None or rowcount < 1:
            # Fail-closed: a direct-grid target with no registered
            # mirror rows is a Change-4 registration invariant violation
            # (SUB-2 registers mirrors atomically with the model row
            # insert). Raise so Change 4's dispatcher rolls back the
            # whole transaction — no empty-display window ever commits.
            raise StationFlagFlipError(
                basin_version_id=str(ctx.basin_version_id),
                target_model_id=str(ctx.target_model.get("model_id")),
                model_input_package_id=model_input_package_id,
                binding_checksum=binding_checksum,
                grid_snapshot_id=grid_snapshot_id,
            )

    return _hook


def _extract_target_direct_grid_manifest(
    target_model: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return ``target.resource_profile.direct_grid_forcing`` or ``None``.

    A ``None`` return is the "no direct-grid section" signal the
    classifier reads as "legacy IDW" — the hook then skips with
    :data:`SKIP_REASON_TARGET_NOT_DIRECT_GRID`. A malformed structure
    (non-mapping ``resource_profile``, non-mapping ``direct_grid_forcing``)
    is treated the same way; the parser's fail-closed contract owns the
    "declared direct-grid intent but broken" classification.
    """
    resource_profile = target_model.get("resource_profile")
    if not isinstance(resource_profile, Mapping):
        return None
    direct_grid = resource_profile.get("direct_grid_forcing")
    if not isinstance(direct_grid, Mapping):
        return None
    return direct_grid


def _extract_target_grid_snapshot_id(target_model: Mapping[str, Any]) -> str:
    """Return ``target.resource_profile.grid_snapshot_id`` as a string.

    :func:`workers.model_registry.direct_grid_variant_registration._build_resource_profile`
    stores ``grid_snapshot_id`` at the TOP LEVEL of ``resource_profile``
    (design D8; alongside — not inside — the parser-validated
    ``direct_grid_forcing`` block). Missing / non-string values fail
    the flip fast with a :class:`StationFlagFlipError` at the row-count
    check (the WHERE clause will match zero rows), which is the correct
    behavior — a registered direct-grid target must carry its snapshot
    FK by Change 4 invariant.
    """
    resource_profile = target_model.get("resource_profile")
    if isinstance(resource_profile, Mapping):
        value = resource_profile.get("grid_snapshot_id")
        if value is not None:
            return str(value)
    return ""
