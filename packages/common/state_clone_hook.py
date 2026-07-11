"""Clone hook mounted on the Change 4 pre-activation extension point.

Epic #982 SUB-4 (``mapping-variant-state-compatibility`` task 3.1) wires
the fingerprint-gated state clone (``packages.common.state_clone``) into
the ordered pre-activation hook chain committed by change
``source-specific-model-variant-routing`` (Change 4). The hook runs
INSIDE the same lifecycle transaction that marks ``M1`` active and the
previous active model ``M0`` ``superseded``; any refusal it raises rolls
back the WHOLE transaction, so no ``activated-but-not-transferred``
intermediate state is ever committed.

Spec authority
--------------
* Consumer of the Change 4 contract at
  ``packages/common/model_registry.py`` — ``PreActivationHook`` +
  ``ModelActivationContext`` + ``PRE_ACTIVATION_HOOK_MOUNT_POINTS``
  ``state_clone`` slot. This module DEFINES no extension point; it only
  registers on the one Change 4 committed.
* Consumer of the SUB-2 core at ``packages/common/state_clone.py``. The
  fingerprint gate, refusal-scope namespace, and clone-row column
  disposition all live there.
* Requirement text:
  ``openspec/changes/mapping-variant-state-compatibility/specs/atomic-cutover-transaction/spec.md``
  and ``openspec/changes/mapping-variant-state-compatibility/tasks.md``
  section 3.1.

Applicability predicate (audited skip semantics)
------------------------------------------------
The hook fires per activation but engages the clone only when both:

* the activation context carries a ``previous_active_model`` (there is
  a legacy ``M0`` to copy from); and
* the target classifies as direct-grid — Change 4 delivers this as a
  non-``None`` ``source_scope`` on the activation context.

Otherwise the hook records an audited SKIP and returns without touching
any state:

* ``no_previous_active_model`` — fresh-basin path (docs §12); a first
  activation for a scope has no ``M0`` state to clone.
* ``target_not_direct_grid`` — the target is legacy-mapping; existing
  lifecycle behavior is left byte-for-byte unchanged.

Per-source engagement + rollback contract
-----------------------------------------
When engaged, the hook iterates ``ctx.source_scope`` in declaration order
and calls :func:`packages.common.state_clone.fingerprint_gated_state_clone`
once per source, using an in-transaction repository adapter
(:class:`_CursorBoundStateSnapshotRepository`) so every write lands under
the SAME cursor Change 4 handed us. On the first refusal the hook raises
:class:`StateCloneCutoverRefusedError` — Change 4's
``_dispatch_pre_activation_hooks`` lets that propagate and the whole
transaction rolls back (D7). No intermediate ``(M1, source, t*)`` row is
committed for any source in scope.

Testability notes
-----------------
The hook takes an ``audit_recorder`` and a
``fingerprint_inputs_provider`` in its closure so tests can inject in-
memory fakes (no live DB). ``_CursorBoundStateSnapshotRepository`` mirrors
the SQL used by :class:`packages.common.state_manager.PsycopgStateSnapshotRepository`
verbatim (same three columns, same ``(model_id, COALESCE(source_id, ''),
valid_time)`` unique-key) so an in-transaction call has identical
semantics to the connection-owning path. The clone-row row hydration
reuses ``state_manager._snapshot_from_row`` — the SUB-2 SQL contract's
single source of truth — to avoid drift.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from packages.common.model_registry import (
    ModelActivationContext,
    PreActivationHook,
)
from packages.common.state_clone import (
    STATE_CLONE_COLD_START_APPROVAL_REQUIRED,
    StateCloneRepository,
    fingerprint_gated_state_clone,
)
from packages.common.state_manager import (
    StateSnapshot,
    _ensure_utc,
    _snapshot_from_row,
)

__all__ = [
    "SKIP_REASON_NO_PREVIOUS_ACTIVE_MODEL",
    "SKIP_REASON_TARGET_NOT_DIRECT_GRID",
    "FingerprintInputsProvider",
    "StateCloneCutoverRefusedError",
    "StateCloneFingerprintInputs",
    "StateCloneHookAuditRecorder",
    "build_state_clone_cutover_hook",
]


# Audited skip reasons the hook records when the clone is NOT engaged.
# Pinned as module-level constants so downstream audit-consumer tests can
# import them and cannot silently diverge on typo.
SKIP_REASON_NO_PREVIOUS_ACTIVE_MODEL = "no_previous_active_model"
SKIP_REASON_TARGET_NOT_DIRECT_GRID = "target_not_direct_grid"


class StateCloneCutoverRefusedError(RuntimeError):
    """Raised inside the pre-activation transaction to force rollback.

    The hook raises this on the FIRST refused source, so the Change 4
    dispatcher aborts the whole transaction with the blocking source
    named on ``.source_id``. The stable refusal code (module constant
    :data:`packages.common.state_clone.STATE_CLONE_COLD_START_APPROVAL_REQUIRED`)
    and the specific ``.refusal_scope`` are carried through so the
    caller's exception handler can route the failure (cold-start
    approval routing lives in SUB-5 task 3.2; this module only signals).
    """

    def __init__(
        self,
        *,
        source_id: str,
        refusal_scope: str,
        refusal_code: str = STATE_CLONE_COLD_START_APPROVAL_REQUIRED,
    ) -> None:
        super().__init__(
            f"state clone refused for source_id={source_id!r} "
            f"scope={refusal_scope!r} code={refusal_code!r}"
        )
        self.source_id = source_id
        self.refusal_scope = refusal_scope
        self.refusal_code = refusal_code


@dataclass(frozen=True)
class StateCloneFingerprintInputs:
    """The 12 fingerprint-gate inputs the clone needs for one source.

    Constructed by the caller-supplied ``fingerprint_inputs_provider``.
    Mirrors the ``fingerprint_gated_state_clone`` kwargs verbatim so a
    provider maps 1:1 onto the SUB-2 gate contract, and the hook itself
    never inspects the values — it only forwards them.

    ``m0_model_id`` / ``m1_model_id`` / ``m1_model_package_version`` /
    ``m1_model_package_checksum`` are derived from the activation
    context's ``previous_active_model`` and ``target_model`` mapping
    rows — the provider is the seam that reads them so the hook stays
    agnostic to model-row shape.
    """

    m0_model_id: str
    m1_model_id: str
    m1_model_package_version: str
    m1_model_package_checksum: str
    m0_package_root: Path
    m1_package_root: Path
    m0_sp_att_path: Path
    m1_sp_att_path: Path
    m1_category_files: Mapping[str, Sequence[str]]
    m1_recorded_hydrologic_core_fingerprint: str
    state_schema_bytes: bytes
    solver_config_bytes: bytes
    cutover_valid_time: datetime


class StateCloneHookAuditRecorder(Protocol):
    """Sink for skip + refusal audit records the hook emits.

    Deliberately a superset of
    :class:`packages.common.state_clone.StateCloneAuditRecorder` — the
    same recorder instance is forwarded to the SUB-2 clone core (which
    calls ``record_refusal``), and the hook calls ``record_skip`` on the
    two applicability-predicate misses. Wiring this to ``ops.audit_log``
    is the caller's responsibility (Change 4 owns transaction plumbing);
    this module only emits records.
    """

    def record_skip(self, reason: str, ctx: ModelActivationContext) -> None: ...

    def record_refusal(self, record: Mapping[str, Any]) -> None: ...


FingerprintInputsProvider = Callable[
    [ModelActivationContext, str], StateCloneFingerprintInputs
]


def build_state_clone_cutover_hook(
    *,
    audit_recorder: StateCloneHookAuditRecorder,
    fingerprint_inputs_provider: FingerprintInputsProvider,
    repository_factory: Callable[[Any], StateCloneRepository] | None = None,
) -> PreActivationHook:
    """Return the pre-activation hook closure to register at ``state_clone``.

    The returned callable is registered via
    ``PsycopgModelRegistryStore.register_pre_activation_hook("state_clone", ...)``
    at bootstrap time (wiring lives outside this module). Dependencies
    are injected here as a closure so the module remains free of I/O
    knowledge and stays fake-friendly for unit tests.

    ``repository_factory`` defaults to
    :class:`_CursorBoundStateSnapshotRepository` — the SQL adapter that
    runs against the hook's transaction cursor. Tests override this seam
    to inject an in-memory fake repository directly and avoid replicating
    SQL semantics; production callers use the default.
    """

    factory = repository_factory or _CursorBoundStateSnapshotRepository

    def _hook(cursor: Any, ctx: ModelActivationContext) -> None:
        # Applicability gate 1: no previous active model to clone from.
        # Fresh-basin activation is legitimate under docs §12 — record
        # the skip and let the transaction continue.
        if ctx.previous_active_model is None:
            audit_recorder.record_skip(SKIP_REASON_NO_PREVIOUS_ACTIVE_MODEL, ctx)
            return

        # Applicability gate 2: target is not direct-grid. Change 4's
        # ``_extract_source_scope`` returns ``None`` for a legacy IDW
        # target; keep the existing lifecycle behavior untouched.
        if ctx.source_scope is None:
            audit_recorder.record_skip(SKIP_REASON_TARGET_NOT_DIRECT_GRID, ctx)
            return

        repository = factory(cursor)

        # Engage the clone per source in declaration order. On the first
        # refusal we raise so Change 4's dispatcher rolls back the whole
        # transaction; no ``(M1, source, t*)`` row commits for any
        # source in scope.
        for source_id in ctx.source_scope:
            inputs = fingerprint_inputs_provider(ctx, source_id)
            result = fingerprint_gated_state_clone(
                repository=repository,
                audit_recorder=audit_recorder,
                m0_model_id=inputs.m0_model_id,
                m1_model_id=inputs.m1_model_id,
                m1_model_package_version=inputs.m1_model_package_version,
                m1_model_package_checksum=inputs.m1_model_package_checksum,
                source_id=source_id,
                cutover_valid_time=inputs.cutover_valid_time,
                m0_package_root=inputs.m0_package_root,
                m1_package_root=inputs.m1_package_root,
                m0_sp_att_path=inputs.m0_sp_att_path,
                m1_sp_att_path=inputs.m1_sp_att_path,
                m1_category_files=inputs.m1_category_files,
                m1_recorded_hydrologic_core_fingerprint=inputs.m1_recorded_hydrologic_core_fingerprint,
                state_schema_bytes=inputs.state_schema_bytes,
                solver_config_bytes=inputs.solver_config_bytes,
            )
            if result.refused:
                # ``fingerprint_gated_state_clone`` already emitted a
                # ``record_refusal`` audit entry before returning; we
                # only need to translate that into the transaction-
                # aborting raise the extension-point contract expects.
                raise StateCloneCutoverRefusedError(
                    source_id=source_id,
                    refusal_scope=result.refusal_scope or "unknown",
                    refusal_code=(
                        result.refusal_code or STATE_CLONE_COLD_START_APPROVAL_REQUIRED
                    ),
                )

    return _hook


# --- Cursor-bound repository adapter ---------------------------------------


class _CursorBoundStateSnapshotRepository:
    """StateCloneRepository adapter that drives the caller's transaction cursor.

    Mirrors the SQL used by
    :class:`packages.common.state_manager.PsycopgStateSnapshotRepository`
    verbatim — same three ``hydro.state_snapshot`` operations, same
    columns, same ``(model_id, COALESCE(source_id, ''::text), valid_time)``
    unique-key clause — so an in-transaction call has identical semantics
    to the connection-owning path. We deliberately DO NOT open a fresh
    connection here: the whole point of the hook is to write on the
    activation transaction's cursor so the clone commits atomically with
    the supersede + activate swap (D7).

    Row hydration delegates to ``_snapshot_from_row`` — the SUB-2 SQL
    contract's single source of truth — so a future column addition
    surfaces uniformly on both paths and we can never silently drop a
    provenance column on the hook side.
    """

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def get_state_snapshot_by_model_time(
        self,
        *,
        model_id: str,
        valid_time: datetime,
        source_id: str | None = None,
        cycle_id: str | None = None,
        lead_hours: int | None = None,
    ) -> StateSnapshot | None:
        # The SUB-2 clone core supplies ``source_id`` + ``lead_hours``
        # explicitly. ``cycle_id`` and ``lead_hours`` are intentionally
        # unused in the DB query — the unique-key columns are
        # ``(model_id, source_id, valid_time)`` and Gate G10's lead
        # check is enforced by ``_is_qualified_source`` in the caller.
        del cycle_id, lead_hours
        if source_id is not None:
            self._cursor.execute(
                """
                SELECT *
                FROM hydro.state_snapshot
                WHERE model_id = %s
                  AND source_id = %s
                  AND valid_time = %s
                """,
                (model_id, source_id, _ensure_utc(valid_time)),
            )
        else:
            self._cursor.execute(
                """
                SELECT *
                FROM hydro.state_snapshot
                WHERE model_id = %s
                  AND valid_time = %s
                """,
                (model_id, _ensure_utc(valid_time)),
            )
        row = self._cursor.fetchone()
        return _snapshot_from_row(row) if row is not None else None

    def get_latest_state_before(
        self,
        *,
        model_id: str,
        source_id: str,
        before_time: datetime,
    ) -> StateSnapshot | None:
        # Source-scoped so the SUB-2 refusal path can distinguish
        # ``stale_latest_snapshot`` from ``missing_qualified_source``
        # per docs §Gate G10 condition 4.
        self._cursor.execute(
            """
            SELECT *
            FROM hydro.state_snapshot
            WHERE model_id = %s
              AND source_id = %s
              AND valid_time < %s
            ORDER BY valid_time DESC
            LIMIT 1
            """,
            (model_id, source_id, _ensure_utc(before_time)),
        )
        row = self._cursor.fetchone()
        return _snapshot_from_row(row) if row is not None else None

    def upsert_state_snapshot(self, snapshot: StateSnapshot) -> StateSnapshot:
        # Same INSERT ... ON CONFLICT template as
        # ``PsycopgStateSnapshotRepository.upsert_state_snapshot`` (SUB-1
        # migration 000046 columns + SUB-2 SQL contract). The upsert
        # branch flips ``usable_flag`` to false to mirror the QC
        # re-derivation semantics of the connection-owning path — the
        # clone core supplies ``usable_flag=True`` on a fresh row, so
        # this branch fires only on a re-insert conflict.
        self._cursor.execute(
            """
            INSERT INTO hydro.state_snapshot (
                state_id,
                model_id,
                run_id,
                valid_time,
                state_uri,
                checksum,
                usable_flag,
                source_id,
                cycle_id,
                lead_hours,
                model_package_version,
                model_package_checksum,
                original_shud_filename,
                cloned_from_state_id,
                cloned_from_model_id,
                clone_gate_fingerprint
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (model_id, (COALESCE(source_id, ''::text)), valid_time) DO UPDATE SET
                state_id = EXCLUDED.state_id,
                run_id = EXCLUDED.run_id,
                state_uri = EXCLUDED.state_uri,
                checksum = EXCLUDED.checksum,
                usable_flag = false,
                source_id = EXCLUDED.source_id,
                cycle_id = EXCLUDED.cycle_id,
                lead_hours = EXCLUDED.lead_hours,
                model_package_version = EXCLUDED.model_package_version,
                model_package_checksum = EXCLUDED.model_package_checksum,
                original_shud_filename = EXCLUDED.original_shud_filename,
                cloned_from_state_id = EXCLUDED.cloned_from_state_id,
                cloned_from_model_id = EXCLUDED.cloned_from_model_id,
                clone_gate_fingerprint = EXCLUDED.clone_gate_fingerprint,
                created_at = now()
            RETURNING *
            """,
            (
                snapshot.state_id,
                snapshot.model_id,
                snapshot.run_id,
                _ensure_utc(snapshot.valid_time),
                snapshot.state_uri,
                snapshot.checksum,
                snapshot.usable_flag,
                snapshot.source_id,
                snapshot.cycle_id,
                snapshot.lead_hours,
                snapshot.model_package_version,
                snapshot.model_package_checksum,
                snapshot.original_shud_filename,
                snapshot.cloned_from_state_id,
                snapshot.cloned_from_model_id,
                snapshot.clone_gate_fingerprint,
            ),
        )
        row = self._cursor.fetchone()
        if row is None:  # pragma: no cover - RETURNING * always yields a row
            raise RuntimeError(
                "hydro.state_snapshot upsert returned no row; expected RETURNING *."
            )
        return _snapshot_from_row(row)
