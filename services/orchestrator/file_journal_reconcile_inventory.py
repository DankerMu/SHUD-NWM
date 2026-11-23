"""Reconcile-inventory cluster of the file orchestration journal, as a repository mixin.

The durable reconcile inventory keeps one anchor per pipeline-job row that
blocks rollback quiescence, so restart reconcile reads the anchors instead of
the whole journal.  ``_iter_reconcile_pipeline_job_records`` and
``_iter_reconcile_inventory_records`` yield that restart work;
``_ensure_reconcile_inventory_migrated`` backfills the inventory once from the
flat directs, the legacy active tree and the cycle journals, under a stable
authority fingerprint, and writes the migration marker.
``_sync_reconcile_inventory_for_row_unlocked``,
``_sync_reconcile_inventory_migration_row_unlocked`` and
``_remove_reconcile_inventory_anchor_unlocked`` keep one anchor in step with
its row, and ``_reconcile_inventory_jobs_matching_unlocked`` answers the
occupancy and durable-claimant queries.
``_prepare_reconcile_inventory_rollback_under_scheduler_lease`` and
``_complete_reconcile_inventory_rollforward_under_scheduler_lease`` write the
rollback-preparation and roll-forward receipts under the scheduler lease, bound
to the journal root and lease identities, and
``_require_reconcile_inventory_rollback_prepared`` /
``current_generation_scheduler_rollback_blocker`` check the prepared rollback fence.
The locks (the reconcile-inventory file lock included), the reads, the atomic
writes and the direct-row writers are facade-class methods reached through ``self``.

The journal's error type, the ``_RECONCILE_INVENTORY_*`` constants and the
other facade-defined patterns, sets and row types stay in the facade and are
imported inside the methods that read them.  The facade-resident patched names
(the clock, ``stat_no_follow``, ``unlink_no_follow``,
``list_directory_no_follow_limited`` and ``_stat_signature``) are looked up on
the facade module at call time.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from packages.common.contracts.time_normalization import format_cycle_time
from packages.common.safe_fs import SafeFilesystemError
from services.orchestrator.accepted_submit_identity import (
    accepted_submit_contract_is_current,
    accepted_submit_row_kind,
    is_forecast_cohort_stage_name,
)
from services.orchestrator.chain_types import OrchestratorError
from services.orchestrator.file_journal_candidates import (
    _accepted_submit_source_cycle_from_job_id,
    _cycle_time_from_job,
    _source_id_from_job,
)
from services.orchestrator.file_journal_paths import (
    _UNSET,
    _iter_discovered_files,
    _journal_segment_identity_from_path,
    _journal_segment_names,
    _journal_segment_sort_key,
    _relative_evidence,
    _required_source_id,
    _safe_segment,
    _validated_actual_writer_generation,
    _validated_git_writer_generation,
)
from services.orchestrator.file_journal_primitives import (
    _SAFE_SEGMENT_RE,
    _normalize_file_source_id,
    _parse_cycle_time_field,
    _required_safe_identity,
    _safe_identity_text,
)
from services.orchestrator.file_journal_read_attribution import _install_public_read_attribution, journal_read_lane
from services.orchestrator.file_journal_retry_helpers import (
    _coerce_cycle_or_iso_datetime,
    _job_blocks_rollback_quiescence,
    _job_needs_restart_reconcile,
    _reconcile_inventory_row_kind,
    _settled_incarnation_matches_candidate,
    _strict_utc_datetime,
)
from services.orchestrator.reservation import SLURM_COMMENT_PREFIX
from services.orchestrator.scheduler_state import _ensure_utc, _format_utc


@_install_public_read_attribution
class _FileJournalReconcileInventoryMixin:
    def _iter_reconcile_pipeline_job_records(self) -> Iterable[dict[str, Any]]:
        """Yield restart work from the durable inventory only.

        A one-time, crash-resumable migration backfills deployments created
        before the inventory existed. Once its marker is durable, normal passes
        never enumerate flat master history, cycle journals, or candidate trees.
        """

        self._ensure_reconcile_inventory_migrated()
        yield from self._iter_reconcile_inventory_records()

    def _iter_reconcile_inventory_records(
        self,
        *,
        quiescence: bool = False,
        strict_disappearance: bool = False,
    ) -> Iterable[dict[str, Any]]:
        from services.orchestrator.file_orchestration_journal import (
            _RECONCILE_INVENTORY_DIRECTORY,
            FileOrchestrationJournalError,
        )

        with self._write_lock:
            self._ensure_root_unlocked()
            with self._reconcile_inventory_file_lock_unlocked():
                entry_names = self._reconcile_inventory_entry_names_unlocked()
        directory = self.root / _RECONCILE_INVENTORY_DIRECTORY
        for entry_name in sorted(entry_names):
            path = directory / entry_name
            # #1734 D11: per-anchor and eager, so no tag spans this generator's
            # ``yield`` (a token reset from an abandoned generator can land in a
            # foreign context, and an instrument must not fail a read path).
            with journal_read_lane("reconcile_inventory_scan"):
                anchor = self._read_optional_json(path)
            if anchor is None:
                if strict_disappearance:
                    raise FileOrchestrationJournalError(
                        "file_journal_quiescence_authority_changed",
                        field="reconcile_inventory",
                    )
                continue
            expected_job_id = _safe_segment(entry_name.removesuffix(".json"))
            source_id, cycle_time = self._validated_reconcile_inventory_anchor(
                anchor,
                expected_job_id=expected_job_id,
            )
            if strict_disappearance:
                canonical = self._canonical_reconcile_job_unlocked(
                    expected_job_id,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    strict_disappearance=strict_disappearance,
                )
                blocking = canonical is not None and _job_blocks_rollback_quiescence(canonical)
                if canonical is None or not blocking:
                    if canonical is None:
                        raise FileOrchestrationJournalError(
                            "file_journal_quiescence_authority_changed",
                            field="reconcile_inventory",
                        )
                    # strict_disappearance never prunes; skip the settled row.
                    continue
            else:
                should_yield = False
                with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
                    canonical = self._canonical_reconcile_job_unlocked(
                        expected_job_id,
                        source_id=source_id,
                        cycle_time=cycle_time,
                    )
                    blocking = canonical is not None and _job_blocks_rollback_quiescence(canonical)
                    if canonical is None or not blocking:
                        # Phase 6h handoff: never prune a stale anchor before a
                        # missing derived flat master direct is restored from
                        # canonical authority. The direct is the bounded flat
                        # scan's only locator for this cycle; pruning first
                        # would let a fallback read the same accounting
                        # incarnation as vacant. The restore writes the direct
                        # and syncs the anchor (removing it for a settled row)
                        # atomically under the reentrant global inventory lock,
                        # all inside this cycle lock. If the restore fails the
                        # anchor is KEPT (fail closed).
                        if canonical is not None:
                            self._restore_derived_master_direct_unlocked(
                                canonical,
                                source_id=source_id,
                                cycle_time=cycle_time,
                            )
                        self._remove_reconcile_inventory_anchor_unlocked(expected_job_id)
                        continue
                    canonical_kind = _reconcile_inventory_row_kind(canonical)
                    if canonical_kind is None or canonical_kind != anchor.get("row_kind"):
                        raise FileOrchestrationJournalError(
                            "file_journal_reconcile_inventory_invalid",
                            field="reconcile_inventory",
                        )
                    should_yield = quiescence or _job_needs_restart_reconcile(canonical)
                # Yield outside the cycle lock so a consumer that abandons the
                # generator cannot strand the lock.
                if should_yield:
                    yield canonical
                continue
            canonical_kind = _reconcile_inventory_row_kind(canonical)
            if canonical_kind is None or canonical_kind != anchor.get("row_kind"):
                raise FileOrchestrationJournalError(
                    "file_journal_reconcile_inventory_invalid",
                    field="reconcile_inventory",
                )
            if quiescence or _job_needs_restart_reconcile(canonical):
                yield canonical

    def _ensure_reconcile_inventory_migrated(self) -> None:
        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import (
            _RECONCILE_INVENTORY_MIGRATION_MARKER,
            _RECONCILE_INVENTORY_MIGRATION_SCHEMA_VERSION,
            _RECONCILE_INVENTORY_ROLLBACK_PREP_RECEIPT,
            FileOrchestrationJournalError,
        )

        if self._reconcile_inventory_migration_checked:
            return
        with self._write_lock:
            self._ensure_root_unlocked()
            with self._reconcile_inventory_file_lock_unlocked():
                self._cleanup_reconcile_migration_temp_residues_unlocked()
                marker_path = self.root / _RECONCILE_INVENTORY_MIGRATION_MARKER
                rollback_fence = self._read_optional_json(
                    self.root / _RECONCILE_INVENTORY_ROLLBACK_PREP_RECEIPT
                )
                if rollback_fence is not None:
                    self._validated_reconcile_inventory_rollback_receipt(rollback_fence)
                    raise FileOrchestrationJournalError(
                        "file_journal_rollforward_required",
                        field="reconcile_inventory_rollback_receipt",
                    )
                marker = self._read_optional_json(marker_path)
                if marker is not None:
                    self._validate_reconcile_inventory_migration_marker(marker)
                    self._reconcile_inventory_migration_checked = True
                    return
                self._stable_backfill_reconcile_inventory_unlocked()
                self._atomic_write_json_unlocked(
                    marker_path,
                    {
                        "schema_version": _RECONCILE_INVENTORY_MIGRATION_SCHEMA_VERSION,
                        "completed_at": _format_utc(file_orchestration_journal._utcnow()),
                    },
                )
                self._cleanup_reconcile_migration_temp_residues_unlocked()
                self._reconcile_inventory_migration_checked = True

    def _prepare_reconcile_inventory_rollback_under_scheduler_lease(
        self,
        *,
        scheduler_lease_identity: Mapping[str, Any],
        scheduler_lease_guard: Callable[[], bool],
        scheduler_state: str,
        active_scheduler_processes: int,
        checked_at: datetime,
        checked_by: str,
        target_writer_generation: str,
    ) -> dict[str, Any]:
        """Invalidate migration completion while the production lease is held."""

        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import (
            _RECONCILE_INVENTORY_MIGRATION_MARKER,
            _RECONCILE_INVENTORY_ROLLBACK_FENCE_SIDECAR,
            _RECONCILE_INVENTORY_ROLLBACK_PREP_RECEIPT,
            _RECONCILE_INVENTORY_ROLLBACK_PREP_SCHEMA_VERSION,
            _RECONCILE_INVENTORY_ROLLFORWARD_RECEIPT,
            FileOrchestrationJournalError,
        )

        if scheduler_state != "stopped" or type(active_scheduler_processes) is not int:
            raise FileOrchestrationJournalError(
                "file_journal_rollback_preflight_invalid",
                field="scheduler_state",
            )
        if active_scheduler_processes != 0:
            raise FileOrchestrationJournalError(
                "file_journal_rollback_preflight_active",
                field="active_scheduler_processes",
            )
        checked_at = _ensure_utc(checked_at)
        checked_by = _safe_identity_text(checked_by, field="checked_by")
        target_writer_generation = _validated_git_writer_generation(
            target_writer_generation,
            field="target_writer_generation",
            invalid_reason="file_journal_rollback_target_writer_generation_invalid",
        )
        lease_identity = self._validated_scheduler_lease_identity(scheduler_lease_identity)
        with self._write_lock:
            self._ensure_root_unlocked()
            with self._reconcile_inventory_file_lock_unlocked():
                self._cleanup_reconcile_migration_temp_residues_unlocked()
                marker_path = self.root / _RECONCILE_INVENTORY_MIGRATION_MARKER
                marker = self._read_optional_json(marker_path)
                receipt_path = self.root / _RECONCILE_INVENTORY_ROLLBACK_PREP_RECEIPT
                prior_receipt = self._read_optional_json(receipt_path)
                rollforward_path = self.root / _RECONCILE_INVENTORY_ROLLFORWARD_RECEIPT
                prior_rollforward = self._read_optional_json(rollforward_path)
                validated_prior: dict[str, Any] | None = None
                if prior_receipt is not None:
                    validated_prior = self._validated_reconcile_inventory_rollback_receipt(
                        prior_receipt
                    )
                    if (
                        validated_prior["scheduler_lease_identity"] != lease_identity
                        or validated_prior["preflight"]["target_writer_generation"]
                        != target_writer_generation
                    ):
                        raise FileOrchestrationJournalError(
                            "file_journal_rollback_fence_conflict",
                            field="reconcile_inventory_rollback_receipt",
                        )
                if marker is not None and validated_prior is not None:
                    if (
                        validated_prior["status"] != "preparing"
                        or validated_prior["invalidated_marker"] != marker
                        or prior_rollforward is not None
                    ):
                        raise FileOrchestrationJournalError(
                            "file_journal_rollback_fence_conflict",
                            field="reconcile_inventory_rollback_receipt",
                        )
                    # Crash-resume boundary: the preparing receipt was made
                    # durable but the migration marker was not yet consumed.
                    # Re-acquiring the exact production lease is sufficient
                    # authority to finish that same root/generation operation.
                    self._require_scheduler_lease_guard(scheduler_lease_guard)
                    try:
                        file_orchestration_journal.unlink_no_follow(marker_path, containment_root=self.root)
                    except (FileNotFoundError, OSError, SafeFilesystemError) as error:
                        raise FileOrchestrationJournalError(
                            "file_journal_rollback_preparation_unavailable",
                            field="reconcile_inventory_migration",
                        ) from error
                    validated_prior["status"] = "prepared"
                    self._require_scheduler_lease_guard(scheduler_lease_guard)
                    self._atomic_write_json_unlocked(receipt_path, validated_prior)
                    self._cleanup_reconcile_migration_temp_residues_unlocked()
                    self._reconcile_inventory_migration_checked = False
                    return dict(validated_prior)
                if marker is None:
                    if validated_prior is not None:
                        validated = validated_prior
                        if validated["status"] == "preparing":
                            validated["status"] = "prepared"
                            self._require_scheduler_lease_guard(scheduler_lease_guard)
                            self._atomic_write_json_unlocked(receipt_path, validated)
                        if validated["status"] == "prepared":
                            self._reconcile_inventory_migration_checked = False
                            return validated
                    raise FileOrchestrationJournalError(
                        "file_journal_rollback_preparation_authority_missing",
                        field="reconcile_inventory_migration",
                    )
                self._validate_reconcile_inventory_migration_marker(marker)
                if validated_prior is not None:
                    raise FileOrchestrationJournalError(
                        "file_journal_rollback_fence_conflict",
                        field="reconcile_inventory_rollback_receipt",
                    )
                if prior_rollforward is not None:
                    completed = self._validated_reconcile_inventory_rollforward_receipt(
                        prior_rollforward
                    )
                    if completed["restored_marker"] != marker:
                        raise FileOrchestrationJournalError(
                            "file_journal_rollforward_state_invalid",
                            field="reconcile_inventory_migration",
                        )
                    self._require_scheduler_lease_guard(scheduler_lease_guard)
                    try:
                        file_orchestration_journal.unlink_no_follow(rollforward_path, containment_root=self.root)
                    except (FileNotFoundError, OSError, SafeFilesystemError) as error:
                        raise FileOrchestrationJournalError(
                            "file_journal_rollback_preparation_unavailable",
                            field="reconcile_inventory_rollforward_receipt",
                        ) from error
                # #2660: only this fresh path takes a fence.  Replace any orphan
                # sidecar (it would pin the fence at an old instant) immediately
                # before the wall-clock instant, so the fence never trails it.
                self._require_scheduler_lease_guard(scheduler_lease_guard)
                self._atomic_write_bytes_unlocked(
                    self.root / _RECONCILE_INVENTORY_ROLLBACK_FENCE_SIDECAR,
                    b"",
                )
                prepared_at = _format_utc(file_orchestration_journal._utcnow())
                preflight = {
                    "scheduler_state": scheduler_state,
                    "active_scheduler_processes": active_scheduler_processes,
                    "checked_at": _format_utc(checked_at),
                    "checked_by": checked_by,
                    "target_writer_generation": target_writer_generation,
                    "dry_run": False,
                }
                root_identity = self._journal_root_identity_unlocked()
                signed = {
                    "journal_root_identity": root_identity,
                    "marker": marker,
                    "preflight": preflight,
                    "prepared_at": prepared_at,
                    "scheduler_lease_identity": lease_identity,
                }
                receipt = {
                    "schema_version": _RECONCILE_INVENTORY_ROLLBACK_PREP_SCHEMA_VERSION,
                    "receipt_id": hashlib.sha256(
                        json.dumps(signed, separators=(",", ":"), sort_keys=True).encode("utf-8")
                    ).hexdigest(),
                    "status": "preparing",
                    "prepared_at": prepared_at,
                    "preflight": preflight,
                    "invalidated_marker": dict(marker),
                    "journal_root_identity": root_identity,
                    "scheduler_lease_identity": lease_identity,
                }
                self._require_scheduler_lease_guard(scheduler_lease_guard)
                self._atomic_write_json_unlocked(receipt_path, receipt)
                self._require_scheduler_lease_guard(scheduler_lease_guard)
                try:
                    file_orchestration_journal.unlink_no_follow(marker_path, containment_root=self.root)
                except (FileNotFoundError, OSError, SafeFilesystemError) as error:
                    raise FileOrchestrationJournalError(
                        "file_journal_rollback_preparation_unavailable",
                        field="reconcile_inventory_migration",
                    ) from error
                receipt["status"] = "prepared"
                self._require_scheduler_lease_guard(scheduler_lease_guard)
                self._atomic_write_json_unlocked(receipt_path, receipt)
                self._cleanup_reconcile_migration_temp_residues_unlocked()
                self._reconcile_inventory_migration_checked = False
                return dict(receipt)

    def _require_reconcile_inventory_rollback_prepared(
        self,
        *,
        receipt_id: str,
        scheduler_lease_identity: Mapping[str, Any],
        actual_writer_generation: str,
    ) -> dict[str, Any]:
        """Fail closed unless the old-writer launch boundary was prepared."""

        from services.orchestrator.file_orchestration_journal import (
            _RECONCILE_INVENTORY_MIGRATION_MARKER,
            _RECONCILE_INVENTORY_ROLLBACK_PREP_RECEIPT,
            FileOrchestrationJournalError,
        )

        expected_lease_identity = self._validated_scheduler_lease_identity(
            scheduler_lease_identity
        )
        actual_writer_generation = _validated_actual_writer_generation(
            actual_writer_generation
        )
        with self._write_lock:
            self._ensure_root_unlocked()
            with self._reconcile_inventory_file_lock_unlocked():
                marker = self._read_optional_json(
                    self.root / _RECONCILE_INVENTORY_MIGRATION_MARKER
                )
                receipt = self._read_optional_json(
                    self.root / _RECONCILE_INVENTORY_ROLLBACK_PREP_RECEIPT
                )
                if marker is not None or receipt is None:
                    raise FileOrchestrationJournalError(
                        "file_journal_rollback_not_prepared",
                        field="reconcile_inventory_migration",
                    )
                validated = self._validated_reconcile_inventory_rollback_receipt(receipt)
                if (
                    validated["status"] != "prepared"
                    or validated["receipt_id"] != receipt_id
                    or validated["scheduler_lease_identity"] != expected_lease_identity
                    or validated["preflight"]["target_writer_generation"]
                    != actual_writer_generation
                ):
                    raise FileOrchestrationJournalError(
                        "file_journal_rollback_not_prepared",
                        field="reconcile_inventory_migration",
                    )
                return validated

    def current_generation_scheduler_rollback_blocker(self) -> dict[str, Any] | None:
        """Return a bounded blocker while a prepared rollback fence is live."""

        from services.orchestrator.file_orchestration_journal import (
            _RECONCILE_INVENTORY_ROLLBACK_PREP_RECEIPT,
            FileOrchestrationJournalError,
        )

        with self._write_lock:
            self._ensure_root_unlocked()
            with self._reconcile_inventory_file_lock_unlocked():
                receipt = self._read_optional_json(
                    self.root / _RECONCILE_INVENTORY_ROLLBACK_PREP_RECEIPT
                )
                if receipt is None:
                    return None
                try:
                    validated = self._validated_reconcile_inventory_rollback_receipt(receipt)
                except FileOrchestrationJournalError:
                    return {"reason": "file_journal_rollback_fence_invalid", "receipt_id": None}
                return {
                    "reason": (
                        "file_journal_rollback_fence_prepared"
                        if validated["status"] in {"prepared", "rolling_forward"}
                        else "file_journal_rollback_fence_invalid"
                    ),
                    "receipt_id": validated["receipt_id"],
                }

    def _complete_reconcile_inventory_rollforward_under_scheduler_lease(
        self,
        *,
        preparation_receipt_id: str,
        scheduler_lease_identity: Mapping[str, Any],
        scheduler_lease_guard: Callable[[], bool],
    ) -> dict[str, Any]:
        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import (
            _RECONCILE_INVENTORY_MIGRATION_MARKER,
            _RECONCILE_INVENTORY_MIGRATION_SCHEMA_VERSION,
            _RECONCILE_INVENTORY_ROLLBACK_FENCE_SIDECAR,
            _RECONCILE_INVENTORY_ROLLBACK_PREP_RECEIPT,
            _RECONCILE_INVENTORY_ROLLFORWARD_RECEIPT,
            _RECONCILE_INVENTORY_ROLLFORWARD_SCHEMA_VERSION,
            FileOrchestrationJournalError,
        )

        expected_lease_identity = self._validated_scheduler_lease_identity(
            scheduler_lease_identity
        )
        with self._write_lock:
            self._ensure_root_unlocked()
            with self._reconcile_inventory_file_lock_unlocked():
                self._cleanup_reconcile_migration_temp_residues_unlocked()
                marker_path = self.root / _RECONCILE_INVENTORY_MIGRATION_MARKER
                fence_path = self.root / _RECONCILE_INVENTORY_ROLLBACK_PREP_RECEIPT
                rollforward_path = self.root / _RECONCILE_INVENTORY_ROLLFORWARD_RECEIPT
                fence = self._read_optional_json(fence_path)
                prior_rollforward = self._read_optional_json(rollforward_path)
                if fence is None:
                    if prior_rollforward is not None:
                        completed = self._validated_reconcile_inventory_rollforward_receipt(
                            prior_rollforward
                        )
                        if (
                            completed["preparation_receipt_id"] == preparation_receipt_id
                            and completed["scheduler_lease_identity"] == expected_lease_identity
                        ):
                            completed_marker = self._read_optional_json(marker_path)
                            if completed_marker != completed["restored_marker"]:
                                raise FileOrchestrationJournalError(
                                    "file_journal_rollforward_state_invalid",
                                    field="reconcile_inventory_migration",
                                )
                            self._reconcile_inventory_migration_checked = True
                            return completed
                    raise FileOrchestrationJournalError(
                        "file_journal_rollforward_not_prepared",
                        field="reconcile_inventory_rollback_receipt",
                    )
                prepared = self._validated_reconcile_inventory_rollback_receipt(fence)
                if (
                    prepared["receipt_id"] != preparation_receipt_id
                    or prepared["scheduler_lease_identity"] != expected_lease_identity
                    or prepared["status"] not in {"prepared", "rolling_forward"}
                ):
                    raise FileOrchestrationJournalError(
                        "file_journal_rollforward_not_prepared",
                        field="reconcile_inventory_rollback_receipt",
                    )
                marker = self._read_optional_json(marker_path)
                if prepared["status"] == "prepared":
                    if marker is not None:
                        raise FileOrchestrationJournalError(
                            "file_journal_rollforward_state_invalid",
                            field="reconcile_inventory_migration",
                        )
                    prepared["status"] = "rolling_forward"
                    self._require_scheduler_lease_guard(scheduler_lease_guard)
                    self._atomic_write_json_unlocked(fence_path, prepared)
                if marker is None:
                    self._stable_backfill_reconcile_inventory_unlocked()
                    self._require_scheduler_lease_guard(scheduler_lease_guard)
                    marker = {
                        "schema_version": _RECONCILE_INVENTORY_MIGRATION_SCHEMA_VERSION,
                        "completed_at": _format_utc(file_orchestration_journal._utcnow()),
                    }
                    self._atomic_write_json_unlocked(marker_path, marker)
                else:
                    self._validate_reconcile_inventory_migration_marker(marker)
                if prior_rollforward is not None:
                    completed = self._validated_reconcile_inventory_rollforward_receipt(
                        prior_rollforward
                    )
                    if (
                        completed["preparation_receipt_id"] != preparation_receipt_id
                        or completed["scheduler_lease_identity"] != expected_lease_identity
                        or completed["restored_marker"] != marker
                    ):
                        raise FileOrchestrationJournalError(
                            "file_journal_rollforward_receipt_conflict",
                            field="reconcile_inventory_rollforward_receipt",
                        )
                else:
                    completed = {
                        "schema_version": _RECONCILE_INVENTORY_ROLLFORWARD_SCHEMA_VERSION,
                        "preparation_receipt_id": preparation_receipt_id,
                        "completed_at": _format_utc(file_orchestration_journal._utcnow()),
                        "journal_root_identity": self._journal_root_identity_unlocked(),
                        "scheduler_lease_identity": expected_lease_identity,
                        "restored_marker": dict(marker),
                    }
                    completed["receipt_id"] = hashlib.sha256(
                        json.dumps(completed, separators=(",", ":"), sort_keys=True).encode("utf-8")
                    ).hexdigest()
                    self._require_scheduler_lease_guard(scheduler_lease_guard)
                    self._atomic_write_json_unlocked(rollforward_path, completed)
                self._require_scheduler_lease_guard(scheduler_lease_guard)
                try:
                    file_orchestration_journal.unlink_no_follow(fence_path, containment_root=self.root)
                    # Receipts prepared by older code have no sidecar.
                    file_orchestration_journal.unlink_no_follow(
                        self.root / _RECONCILE_INVENTORY_ROLLBACK_FENCE_SIDECAR,
                        containment_root=self.root,
                        missing_ok=True,
                    )
                except (FileNotFoundError, OSError, SafeFilesystemError) as error:
                    raise FileOrchestrationJournalError(
                        "file_journal_rollforward_fence_consume_failed",
                        field="reconcile_inventory_rollback_receipt",
                    ) from error
                self._cleanup_reconcile_migration_temp_residues_unlocked()
                self._reconcile_inventory_migration_checked = True
                return dict(completed)

    def _require_scheduler_lease_guard(self, guard: Callable[[], bool]) -> None:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        try:
            held = guard()
        except Exception as error:
            raise FileOrchestrationJournalError(
                "file_journal_scheduler_lease_lost",
                field="scheduler_lock",
            ) from error
        if not held:
            raise FileOrchestrationJournalError(
                "file_journal_scheduler_lease_lost",
                field="scheduler_lock",
            )

    def _validated_reconcile_inventory_rollback_receipt(
        self,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        from services.orchestrator.file_orchestration_journal import (
            _RECONCILE_INVENTORY_ROLLBACK_PREP_SCHEMA_VERSION,
            FileOrchestrationJournalError,
        )

        required = {
            "schema_version",
            "receipt_id",
            "status",
            "prepared_at",
            "preflight",
            "invalidated_marker",
            "journal_root_identity",
            "scheduler_lease_identity",
        }
        if (
            set(receipt) != required
            or receipt.get("schema_version")
            != _RECONCILE_INVENTORY_ROLLBACK_PREP_SCHEMA_VERSION
        ):
            raise FileOrchestrationJournalError(
                "file_journal_rollback_receipt_invalid",
                field="reconcile_inventory_rollback_receipt",
            )
        receipt_id = receipt.get("receipt_id")
        preflight = receipt.get("preflight")
        invalidated_marker = receipt.get("invalidated_marker")
        if (
            not isinstance(receipt_id, str)
            or receipt.get("status") not in {"preparing", "prepared", "rolling_forward"}
            or not isinstance(preflight, Mapping)
            or set(preflight)
            != {
                "scheduler_state",
                "active_scheduler_processes",
                "checked_at",
                "checked_by",
                "target_writer_generation",
                "dry_run",
            }
            or preflight.get("scheduler_state") != "stopped"
            or type(preflight.get("active_scheduler_processes")) is not int
            or preflight.get("active_scheduler_processes") != 0
            or not isinstance(preflight.get("checked_by"), str)
            or not isinstance(preflight.get("target_writer_generation"), str)
            or preflight.get("dry_run") is not False
            or not isinstance(invalidated_marker, Mapping)
        ):
            raise FileOrchestrationJournalError(
                "file_journal_rollback_receipt_invalid",
                field="reconcile_inventory_rollback_receipt",
            )
        self._validate_reconcile_inventory_migration_marker(invalidated_marker)
        root_identity = self._validated_journal_root_identity(
            receipt.get("journal_root_identity")
        )
        if root_identity != self._journal_root_identity_unlocked():
            raise FileOrchestrationJournalError(
                "file_journal_rollback_receipt_wrong_root",
                field="reconcile_inventory_rollback_receipt",
            )
        lease_identity = self._validated_scheduler_lease_identity(
            receipt.get("scheduler_lease_identity")
        )
        _coerce_cycle_or_iso_datetime(receipt.get("prepared_at"), field="prepared_at")
        _coerce_cycle_or_iso_datetime(preflight.get("checked_at"), field="checked_at")
        _safe_identity_text(preflight["checked_by"], field="checked_by")
        target_writer_generation = _validated_git_writer_generation(
            preflight["target_writer_generation"],
            field="target_writer_generation",
            invalid_reason="file_journal_rollback_receipt_invalid",
        )
        signed = {
            "journal_root_identity": root_identity,
            "marker": invalidated_marker,
            "preflight": preflight,
            "prepared_at": receipt.get("prepared_at"),
            "scheduler_lease_identity": lease_identity,
        }
        expected_receipt_id = hashlib.sha256(
            json.dumps(signed, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        if not re.fullmatch(r"[0-9a-f]{64}", receipt_id) or receipt_id != expected_receipt_id:
            raise FileOrchestrationJournalError(
                "file_journal_rollback_receipt_invalid",
                field="reconcile_inventory_rollback_receipt",
            )
        return {
            **dict(receipt),
            "preflight": {
                **dict(preflight),
                "target_writer_generation": target_writer_generation,
            },
            "journal_root_identity": root_identity,
            "scheduler_lease_identity": lease_identity,
        }

    def _validated_reconcile_inventory_rollforward_receipt(
        self,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        from services.orchestrator.file_orchestration_journal import (
            _RECONCILE_INVENTORY_ROLLFORWARD_SCHEMA_VERSION,
            FileOrchestrationJournalError,
        )

        required = {
            "schema_version",
            "receipt_id",
            "preparation_receipt_id",
            "completed_at",
            "journal_root_identity",
            "scheduler_lease_identity",
            "restored_marker",
        }
        if set(receipt) != required or receipt.get("schema_version") != _RECONCILE_INVENTORY_ROLLFORWARD_SCHEMA_VERSION:
            raise FileOrchestrationJournalError(
                "file_journal_rollforward_receipt_invalid",
                field="reconcile_inventory_rollforward_receipt",
            )
        receipt_id = receipt.get("receipt_id")
        preparation_receipt_id = receipt.get("preparation_receipt_id")
        if (
            not isinstance(receipt_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", receipt_id) is None
            or not isinstance(preparation_receipt_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", preparation_receipt_id) is None
        ):
            raise FileOrchestrationJournalError(
                "file_journal_rollforward_receipt_invalid",
                field="reconcile_inventory_rollforward_receipt",
            )
        _coerce_cycle_or_iso_datetime(receipt.get("completed_at"), field="completed_at")
        root_identity = self._validated_journal_root_identity(
            receipt.get("journal_root_identity")
        )
        if root_identity != self._journal_root_identity_unlocked():
            raise FileOrchestrationJournalError(
                "file_journal_rollforward_receipt_wrong_root",
                field="reconcile_inventory_rollforward_receipt",
            )
        lease_identity = self._validated_scheduler_lease_identity(
            receipt.get("scheduler_lease_identity")
        )
        restored_marker = receipt.get("restored_marker")
        if not isinstance(restored_marker, Mapping):
            raise FileOrchestrationJournalError(
                "file_journal_rollforward_receipt_invalid",
                field="reconcile_inventory_rollforward_receipt",
            )
        self._validate_reconcile_inventory_migration_marker(restored_marker)
        unsigned = {key: value for key, value in receipt.items() if key != "receipt_id"}
        expected = hashlib.sha256(
            json.dumps(unsigned, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        if receipt_id != expected:
            raise FileOrchestrationJournalError(
                "file_journal_rollforward_receipt_invalid",
                field="reconcile_inventory_rollforward_receipt",
            )
        return {
            **dict(receipt),
            "journal_root_identity": root_identity,
            "scheduler_lease_identity": lease_identity,
            "restored_marker": dict(restored_marker),
        }

    def _journal_root_identity_unlocked(self) -> dict[str, Any]:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        try:
            metadata = os.stat(self.root, follow_symlinks=False)
        except OSError as error:
            raise FileOrchestrationJournalError(
                "file_journal_root_identity_unavailable",
                field="journal_root",
            ) from error
        if not stat.S_ISDIR(metadata.st_mode):
            raise FileOrchestrationJournalError(
                "file_journal_root_identity_invalid",
                field="journal_root",
            )
        return {
            "path_digest": hashlib.sha256(str(self.root.absolute()).encode("utf-8")).hexdigest(),
            "device": int(metadata.st_dev),
            "inode": int(metadata.st_ino),
        }

    def _validated_journal_root_identity(self, value: Any) -> dict[str, Any]:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        if (
            not isinstance(value, Mapping)
            or set(value) != {"path_digest", "device", "inode"}
            or not isinstance(value.get("path_digest"), str)
            or re.fullmatch(r"[0-9a-f]{64}", value["path_digest"]) is None
            or type(value.get("device")) is not int
            or type(value.get("inode")) is not int
        ):
            raise FileOrchestrationJournalError(
                "file_journal_root_identity_invalid",
                field="journal_root_identity",
            )
        return dict(value)

    def _validated_scheduler_lease_identity(self, value: Any) -> dict[str, str]:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        if (
            not isinstance(value, Mapping)
            or set(value) != {"backend", "lock_path_digest", "workspace_root_digest"}
            or value.get("backend") != "file"
            or not isinstance(value.get("lock_path_digest"), str)
            or re.fullmatch(r"[0-9a-f]{64}", value["lock_path_digest"]) is None
            or not isinstance(value.get("workspace_root_digest"), str)
            or re.fullmatch(r"[0-9a-f]{64}", value["workspace_root_digest"]) is None
        ):
            raise FileOrchestrationJournalError(
                "file_journal_scheduler_lease_identity_invalid",
                field="scheduler_lease_identity",
            )
        return {
            "backend": "file",
            "lock_path_digest": value["lock_path_digest"],
            "workspace_root_digest": value["workspace_root_digest"],
        }

    def _stable_backfill_reconcile_inventory_unlocked(self) -> str:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        before = self._reconcile_authority_fingerprint_unlocked()
        self._backfill_reconcile_inventory_unlocked()
        after = self._reconcile_authority_fingerprint_unlocked()
        if before != after:
            raise FileOrchestrationJournalError(
                "file_journal_reconcile_inventory_migration_unavailable",
                field="reconcile_inventory_migration",
            )
        return after

    def _reconcile_authority_fingerprint_unlocked(self) -> str:
        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        paths = [
            *self._iter_reconcile_direct_pipeline_job_paths(),
            *self._iter_migration_legacy_active_paths(),
            *self._iter_migration_journal_paths(),
        ]
        entries: list[tuple[str, int, int]] = []
        for path in sorted(paths):
            try:
                metadata = file_orchestration_journal.stat_no_follow(path, containment_root=self.root)
            except (FileNotFoundError, OSError, SafeFilesystemError) as error:
                raise FileOrchestrationJournalError(
                    "file_journal_reconcile_inventory_migration_invalid",
                    field="reconcile_inventory_migration",
                ) from error
            if not stat.S_ISREG(metadata.st_mode):
                raise FileOrchestrationJournalError(
                    "file_journal_reconcile_inventory_migration_invalid",
                    field="reconcile_inventory_migration",
                )
            entries.append(
                (
                    str(_relative_evidence(path, self.root)),
                    int(metadata.st_size),
                    int(metadata.st_mtime_ns),
                )
            )
        return hashlib.sha256(
            json.dumps(entries, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest()

    def _reconcile_inventory_entry_names_unlocked(self) -> list[str]:
        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import (
            _RECONCILE_INVENTORY_DIRECTORY,
            _RECONCILE_INVENTORY_TEMP_RE,
            FileOrchestrationJournalError,
        )

        directory = self.root / _RECONCILE_INVENTORY_DIRECTORY
        try:
            entry_names = file_orchestration_journal.list_directory_no_follow_limited(
                directory,
                containment_root=self.root,
                max_entries=self.max_files,
            )
        except FileNotFoundError:
            return []
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_reconcile_inventory_unavailable",
                field="reconcile_inventory",
            ) from error
        if len(entry_names) > self.max_files:
            raise FileOrchestrationJournalError(
                "file_journal_record_limit_exceeded",
                field="reconcile_inventory",
            )
        canonical: list[str] = []
        for entry_name in sorted(entry_names):
            if entry_name.endswith(".json") and _SAFE_SEGMENT_RE.fullmatch(entry_name) is not None:
                canonical.append(entry_name)
                continue
            match = _RECONCILE_INVENTORY_TEMP_RE.fullmatch(entry_name)
            if match is not None and _SAFE_SEGMENT_RE.fullmatch(match.group("target")) is not None:
                self._remove_reconcile_atomic_residue_unlocked(
                    directory / entry_name,
                    field="reconcile_inventory",
                )
                continue
            raise FileOrchestrationJournalError(
                "file_journal_reconcile_inventory_invalid",
                field="reconcile_inventory",
            )
        return canonical

    def _operator_bind_slurm_id_claimed_in_cycle_unlocked(
        self,
        requested_id: str,
        *,
        include_job_id: str,
        source_id: str,
        cycle_time: datetime,
    ) -> bool:
        """#2668: whether any row of the bound row's cycle other than ``include_job_id`` claims ``requested_id``.

        The same-cycle half of the operator bind's strict exclusivity: the
        bounded cycle-scoped replay of the source/cycle whose lock the caller
        holds -- every row of it regardless of stage, kind, contract version,
        or status (forcing and downstream rows, legacy unversioned rows,
        member task rows).  The cross-cycle half is the ``fallback_unique``
        occupancy scan in ``_reconcile_inventory_jobs_matching_unlocked``: the
        reconcile-inventory anchors, then the flat ``pipeline-jobs/``
        settled-master scan, so every current accepted-submit forecast master
        of any cycle, settled or active, is covered.  A whole-tree replay is
        deliberately NOT used, because it exceeds the record budget on a
        production journal and would refuse every bind.  Not scanned: non-master
        rows of other cycles (forcing, downstream, legacy, member task), legacy
        unversioned forecast masters of other cycles (both cross-cycle scans
        require the current contract), and ``nhms_forecast`` jobs outside this
        journal root.  (A current master with neither anchor nor flat direct is
        excluded by invariant: ``_restore_derived_master_direct_unlocked`` and
        ``_sync_reconcile_inventory_migration_row_unlocked``.)  The SubmitLine
        key check rules out a SubmitLine pasted from another lane or row, but
        not a mistyped ``--slurm-job-id``; landing on a forcing or downstream
        job fails inflight JobName identity, and the rest is covered
        procedurally by the runbook's ``sacct --jobs=<id>`` re-read (design
        Decision 3).

        A row claims the id when the master part (leading decimal digits, so
        ``57553_18`` and ``57553.batch`` read as ``57553``) of its
        ``slurm_job_id`` or ``matched_slurm_job_id`` equals it as an integer.
        The ``matched_slurm_job_id`` arm is defensive for current-contract
        rows (their validator forces it equal to ``slurm_job_id`` or null); a
        legacy row can carry it alone.  A replay failure raises -- never read
        as "unclaimed".
        """

        from services.orchestrator.file_orchestration_journal import _SLURM_MASTER_PART_RE

        requested = int(requested_id)
        for job in self._iter_pipeline_job_records_for_cycle(source_id=source_id, cycle_time=cycle_time):
            if str(job.get("job_id") or "") == include_job_id:
                continue
            for id_field in ("slurm_job_id", "matched_slurm_job_id"):
                master_part = _SLURM_MASTER_PART_RE.match(str(job.get(id_field) or ""))
                if master_part is not None and int(master_part.group(0)) == requested:
                    return True
        return False

    def _reconcile_inventory_jobs_matching_unlocked(
        self,
        entry_names: Sequence[str],
        *,
        expected_user: str,
        expected_account: str,
        candidate_submit: datetime,
        active_slurm_job_id: str,
        include_job_id: str | None = None,
        fallback_unique: bool = False,
        submitline_key: str | None = None,
        strict_slurm_id_exclusivity: bool = False,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Scan the reconcile inventory for occupancy and durable-claimant overlap.

        #2655: with a proven ``submitline_key`` (already re-verified against the
        committing row), a reserved-unbound sibling is a durable claimant only
        when its own ``nhms_idem:<idempotency_key>`` comment equals the key; an
        overlapping-window sibling with a different key provably did not submit
        this candidate. Without a key the window-overlap rule below is
        unchanged. Active-owner and same-incarnation occupancy never consult
        the key.

        Returns ``(matching, ambiguous)``. For EVERY commit source (Fix A),
        ``matching`` lists every other ACTIVE current accepted-submit forecast
        master that already owns ``active_slurm_job_id``. For the name-window
        fallback producer only (``fallback_unique``), it additionally lists
        every current reserved-unbound forecast master whose attempt window
        contains ``candidate_submit`` for the same expected Slurm user/account
        (durable claimants), and ``ambiguous`` is True when this caller plus at
        least one other claimant share the window/owner — the multi-claimant
        fail-closed condition.

        Fail-closed integrity (Fix B): a listed inventory anchor that is
        missing, malformed, or resolves to no canonical authority raises a
        stable ``FileOrchestrationJournalError`` — tampered/disappearing
        authority is NEVER interpreted as "free". For the name-window fallback
        producer (``fallback_unique``), a SETTLED canonical current forecast
        master whose ``slurm_job_id`` equals the candidate's is adjudicated by
        canonical ``submitted_at`` BEFORE the quiescence skip: same (id,
        Submit) is the exact accounting incarnation and blocks, different
        Submit permits recycle, and a settled row without a comparable Submit
        stays free (Fix G — a stale anchor left by a batch terminal projection
        whose direct write failed cannot hide the same-incarnation owner). For
        every non-fallback commit source, a valid canonical row whose
        ``_job_blocks_rollback_quiescence`` is false (settled history) is
        ignored, exactly as occupancy must never be claimed from settled
        terminal history.

        #2668 ``strict_slurm_id_exclusivity`` (operator-verified bind only,
        always with ``fallback_unique``): a same-id current forecast master is
        an occupant whether the id sits in its ``slurm_job_id`` or its
        ``matched_slurm_job_id``, and a SETTLED same-id master blocks even when
        its canonical Submit proves a recycled incarnation -- the operator path
        accepts no recycle ambiguity. Every other caller passes ``False`` and
        keeps the rules above unchanged.  This is the cross-cycle half of the operator bind's
        exclusivity; every row of the bound row's own cycle (any stage, kind,
        or contract) is checked by
        :meth:`_operator_bind_slurm_id_claimed_in_cycle_unlocked`.

        Reads only reconcile-inventory anchors and each anchor's exact
        canonical row; never enumerates the whole tree (#1850 D3). The caller
        must hold ``_write_lock`` AND ``_reconcile_inventory_file_lock_unlocked``
        through this scan and the subsequent bind write, so the scan+bind is
        atomic across processes (cycle lock -> inventory lock order).
        """

        from services.orchestrator.file_orchestration_journal import (
            _RECONCILE_INVENTORY_DIRECTORY,
            TERMINAL_PIPELINE_STATUSES,
            FileOrchestrationJournalError,
            _CycleRows,
        )

        matching: list[dict[str, Any]] = []
        seen_claimant_keys: set[tuple[str, str, str]] = set()
        ambiguous = False
        for entry_name in sorted(entry_names):
            anchor_path = self.root / _RECONCILE_INVENTORY_DIRECTORY / entry_name
            with journal_read_lane("reconcile_inventory_scan"):
                anchor = self._read_optional_json(anchor_path)
            if anchor is None:
                # Fix B: an entry listed by the directory enumeration but
                # missing under the held inventory lock is corrupt authority.
                raise FileOrchestrationJournalError(
                    "file_journal_reconcile_inventory_invalid",
                    field="reconcile_inventory",
                )
            expected_job_id = _safe_segment(entry_name.removesuffix(".json"))
            try:
                anchor_source, anchor_cycle = self._validated_reconcile_inventory_anchor(
                    anchor,
                    expected_job_id=expected_job_id,
                )
            except FileOrchestrationJournalError:
                # Fix B: an anchor whose schema/source/cycle is invalid fails
                # closed — it cannot be read as an unoccupied slot.
                raise FileOrchestrationJournalError(
                    "file_journal_reconcile_inventory_invalid",
                    field="reconcile_inventory",
                )
            canonical = self._canonical_reconcile_job_unlocked(
                expected_job_id,
                source_id=anchor_source,
                cycle_time=anchor_cycle,
            )
            if canonical is None:
                # Fix B: a listed anchor that resolves to no canonical
                # authority under the held lock is an orphan — fail closed.
                raise FileOrchestrationJournalError(
                    "file_journal_reconcile_inventory_invalid",
                    field="reconcile_inventory",
                )
            # Fix G (accounting incarnation at the ANCHOR surface): a stale
            # reconcile-inventory anchor can outlive its row's active period
            # (a batch terminal projection commits the canonical journal and
            # then fails the direct projection, leaving the anchor listed but
            # the canonical row settled). For the name-window fallback
            # producer only, a terminal canonical current forecast master
            # whose slurm_job_id equals the candidate's must be adjudicated
            # by canonical submitted_at BEFORE the settled/quiescent skip:
            # same (id, Submit) is the exact accounting incarnation and
            # blocks; different Submit is a legitimately recycled id and does
            # not block; terminal without a comparable durable Submit keeps
            # the documented free-for-recycle behavior. Normal/exact-comment
            # commits carry no candidate Submit, so settled numeric history
            # never blocks them.
            row_kind = _reconcile_inventory_row_kind(canonical)
            other_owner = str(canonical.get("slurm_job_id") or "")
            claims_active_id = other_owner == active_slurm_job_id or (
                strict_slurm_id_exclusivity
                and str(canonical.get("matched_slurm_job_id") or "") == active_slurm_job_id
            )
            if (
                fallback_unique
                and claims_active_id
                and row_kind == "current_master"
                and is_forecast_cohort_stage_name(
                    str(canonical.get("stage") or ""),
                    str(canonical.get("job_type") or ""),
                )
                and str(canonical.get("job_id") or "") != include_job_id
                and not _job_blocks_rollback_quiescence(canonical)
            ):
                # #1850 Fix C: only a provenance-compatible canonical
                # ``slurm_accounting_submitted_at`` may prove recycle. Missing/
                # malformed canonical Submit, gateway-only provenance,
                # exact-comment-without-Submit, and legacy pre-change rows all
                # block fail-closed even when the legacy ``submitted_at``
                # differs; the gateway acceptance/commit timestamp is never
                # incarnation proof.
                if strict_slurm_id_exclusivity or _settled_incarnation_matches_candidate(
                    canonical, candidate_submit
                ):
                    # Same accounting incarnation (or unprovable recycle): the
                    # exact accounting row the fallback query returned, settled
                    # or not. The stale anchor must not let the fallback reuse
                    # it. (#2668 strict: any settled same-id master blocks.)
                    matching.append(canonical)
                continue
            if not _job_blocks_rollback_quiescence(canonical):
                # Settled terminal history: never occupies a Slurm id.
                continue
            if str(canonical.get("job_id") or "") == include_job_id:
                continue
            if row_kind != "current_master":
                continue
            if not is_forecast_cohort_stage_name(
                str(canonical.get("stage") or ""),
                str(canonical.get("job_type") or ""),
            ):
                continue
            if claims_active_id and (other_owner or strict_slurm_id_exclusivity):
                # Fix A: another active master already owns this id in any
                # source/cycle — every commit source refuses the bind.
                matching.append(canonical)
                continue
            if not fallback_unique:
                continue
            if str(canonical.get("status") or "") != "reserved":
                continue
            if canonical.get("slurm_job_id") not in (None, ""):
                continue
            window_start = _strict_utc_datetime(canonical.get("submission_attempt_started_at"))
            # #1850: a current reserved-unbound sibling claims the candidate
            # when its immutable attempt anchor is at or before the candidate's
            # submit instant. Its durable row carries no window end (reserve
            # nulls ``submitted_at`` until the bind), and a same-pass sibling's
            # fallback window always closes at the shared frozen query-end that
            # already admitted this candidate, so the lower bound alone decides
            # the overlap -- conservative by construction (fail-closed can only
            # over-reject, never double-bind).
            if window_start is None or window_start > candidate_submit:
                continue
            other_user = str(canonical.get("expected_slurm_user") or "")
            other_account = str(canonical.get("expected_slurm_account") or "")
            if expected_user and other_user != expected_user:
                continue
            if expected_account and other_account != expected_account:
                continue
            if submitline_key is not None and submitline_key != (
                f"{SLURM_COMMENT_PREFIX}{canonical.get('idempotency_key') or ''}"
            ):
                continue
            claimant_key = (
                str(canonical.get("source_id") or ""),
                _format_utc(anchor_cycle),
                str(canonical.get("idempotency_key") or ""),
            )
            if claimant_key in seen_claimant_keys:
                continue
            seen_claimant_keys.add(claimant_key)
            matching.append(canonical)
        # Fix A + Fix 2 + Fix E (accounting incarnation, not history-blind
        # occupancy): the flat settled-master scan runs ONLY for the
        # name-window fallback producer (``fallback_unique``) — the only lane
        # that carries a candidate Submit and needs recycled-id
        # disambiguation. A fresh normal stage submit must never pay the cost
        # of reading thousands of historical direct files; its active-id
        # occupancy comes entirely from the bounded reconcile-inventory
        # canonical rows above.
        #
        # The reconcile inventory prunes its anchor the moment a row stops
        # blocking rollback quiescence, so a SETTLED terminal sibling with the
        # same id is invisible to the anchor scan. The flat current-master
        # direct files are bounded by ``max_files`` (candidates live under
        # by-cycle, never here), so scan them under the same held inventory
        # lock for same-id masters.
        #
        # Canonical cycle authority decides BOTH directions of occupancy —
        # never the flat direct projection. The flat file is a candidate
        # filter only: it names the source/cycle identities worth replaying.
        # The journal replay for each distinct (source, cycle) is replayed at
        # most once and yields the authoritative rows; a STALE direct cannot
        # FABRICATE an owner (its forged id/status is ignored in favor of the
        # replayed row) and cannot HIDE an owner either (the replayed row is
        # evaluated on its own terms, not gated on the projection agreeing
        # with it). Same numeric id + same canonical durable submitted_at is
        # the exact accounting incarnation and blocks regardless of
        # terminal/work status; same id + different Submit instant is a
        # legitimately recycled id and does not block. An ACTIVE same-id row
        # (or one without a durable submitted_at) always blocks (fail
        # closed).
        #
        # Fix B + Fix F: the flat file is a CANDIDATE filter only. The
        # eligible (source, cycle) identity is derived from the SAFE filename
        # stem whenever it parses as an accepted-submit master identity
        # (``job_cycle_<source>_<cycle>_...``), INDEPENDENTLY of whether the
        # payload decodes or validates. Each distinct cycle journal is
        # replayed at most once and its replayed canonical rows decide
        # occupancy — never the direct projection. A malformed/stale direct
        # can neither FABRICATE an owner (the replayed row's own
        # id/status/submitted_at decide) nor HIDE one (a damaged projection
        # still names its cycle for replay). A master-looking filename with
        # no valid cycle lineage and no decodable/resolvable direct authority
        # fails closed rather than reading vacancy; unrelated malformed
        # filenames that do not parse as the accepted-submit master surface
        # never enter this authority path. Replayed canonical rows are
        # filtered to CURRENT accepted-submit forecast MASTERS only: task
        # candidate rows and legacy rows are never owners.
        if fallback_unique and active_slurm_job_id and not any(
            str(master.get("slurm_job_id") or "") == active_slurm_job_id
            or (
                strict_slurm_id_exclusivity
                and str(master.get("matched_slurm_job_id") or "") == active_slurm_job_id
            )
            for master in matching
        ):
            # Group master-looking flat identities by (source_id, cycle_time)
            # so a cycle journal is replayed at most once per distinct
            # source/cycle. The filename stem is the identity source: it is
            # already ``_SAFE_SEGMENT``-validated by the flat enumeration and
            # survives a damaged payload.
            flat_cycles: dict[tuple[str, datetime], list[str]] = {}
            for direct_path in self._iter_reconcile_direct_pipeline_job_paths():
                stem = direct_path.stem
                try:
                    source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(stem)
                except FileOrchestrationJournalError:
                    # Not an accepted-submit master filename: unrelated
                    # malformed history files never enter the authority path.
                    continue
                if stem == include_job_id:
                    continue
                # #1850 Fix D: source/cycle discovery comes UNCONDITIONALLY
                # from the safe master filename, BEFORE any payload decode or
                # row-kind/stage check. A decoded payload may assist only the
                # no-lineage compatibility branch below; it must never
                # suppress canonical replay. This is what keeps a
                # validator-accepted wrong-kind payload (a valid legacy or
                # candidate row planted under the settled master's filename)
                # from hiding the canonical owner.
                flat_cycles.setdefault((source_id, cycle_time), []).append(stem)
                # Lenient decode: a flat file that fails to decode or validate
                # as a pipeline-job record still names its cycle (the damaged
                # projection cannot hide the canonical owner). The decoded
                # row is used only to narrow the no-lineage direct probe; a
                # decoded payload that is NOT a current forecast master (a
                # valid wrong-kind legacy/candidate row) narrows nothing, but
                # the filename-derived cycle above is already retained.
                try:
                    payload = self._read_optional_json(direct_path)
                    if payload is not None:
                        direct = self._validated_direct_pipeline_job_record(
                            payload,
                            expected_job_id=_safe_segment(direct_path.stem),
                        )
                        if (
                            _reconcile_inventory_row_kind(direct) != "current_master"
                            or not is_forecast_cohort_stage_name(
                                str(direct.get("stage") or ""),
                                str(direct.get("job_type") or ""),
                            )
                        ):
                            # Wrong-kind decoded payload: keep the no-lineage
                            # probe on the safe filename identity, never on the
                            # decoded row.
                            direct = None
                except FileOrchestrationJournalError:
                    direct = None
            for (source_id, cycle_time), candidate_job_ids in sorted(
                flat_cycles.items()
            ):
                # Grouped canonical authority: replay the ONE cycle journal
                # once per distinct (source, cycle) and evaluate occupancy
                # from the replayed canonical rows — never from the flat
                # projection. The journal replay is authoritative for this
                # source/cycle, so a stale or damaged direct can neither
                # FABRICATE an owner (the replayed row's own id/status
                # decide) nor HIDE one (every replayed row is evaluated on
                # its own terms, not gated on the projection agreeing with
                # it).
                canonical_source = _normalize_file_source_id(source_id, field="source_id")
                journal_records = self._cycle_journal_records(
                    source_id=canonical_source, cycle_time=cycle_time
                )
                if journal_records:
                    rows = _CycleRows()
                    for record in journal_records:
                        self._apply_journal_record(
                            rows,
                            record,
                            source_id=canonical_source,
                            cycle_time=cycle_time,
                        )
                    canonical_rows = list(rows.pipeline_jobs.values())
                else:
                    # No journal lineage at all: the flat direct is the only
                    # authority for this master, resolved via the one direct
                    # probe (never latest/global). A master-looking filename
                    # whose direct cannot be decoded/resolved as a valid
                    # authority fails closed rather than assuming vacancy.
                    canonical_rows = []
                    for direct_job_id in sorted(set(candidate_job_ids)):
                        canonical = self._accepted_submit_job_for_id_unlocked(
                            direct_job_id,
                            source_id=source_id,
                            cycle_time=cycle_time,
                        )
                        if canonical is None:
                            raise FileOrchestrationJournalError(
                                "file_journal_reconcile_inventory_invalid",
                                field="reconcile_inventory",
                            )
                        canonical_rows.append(canonical)
                for canonical in canonical_rows:
                    # Only current accepted-submit forecast MASTERS are
                    # owners: task candidate rows and legacy rows never occupy
                    # a Slurm id.
                    try:
                        is_owner_shape = bool(
                            accepted_submit_contract_is_current(canonical)
                            and accepted_submit_row_kind(canonical) == "master"
                            and is_forecast_cohort_stage_name(
                                str(canonical.get("stage") or ""),
                                str(canonical.get("job_type") or ""),
                            )
                        )
                    except FileOrchestrationJournalError:
                        raise FileOrchestrationJournalError(
                            "file_journal_reconcile_inventory_invalid",
                            field="reconcile_inventory",
                        )
                    if not is_owner_shape:
                        continue
                    if str(canonical.get("job_id") or "") == include_job_id:
                        continue
                    if str(canonical.get("slurm_job_id") or "") != active_slurm_job_id and not (
                        strict_slurm_id_exclusivity
                        and str(canonical.get("matched_slurm_job_id") or "") == active_slurm_job_id
                    ):
                        # Stale projection (either direction): the canonical
                        # authority holds a different id — no same-id
                        # occupancy from this cycle.
                        continue
                    if str(canonical.get("status") or "") in TERMINAL_PIPELINE_STATUSES:
                        # #1850 Fix C: the SAME centralized incarnation
                        # adjudication as the anchor surface, so the two can
                        # never drift. Missing/malformed canonical Submit and
                        # every non-name-window provenance block fail-closed.
                        # #2668 strict: the operator bind refuses any settled
                        # same-id master, recycled incarnation or not.
                        if strict_slurm_id_exclusivity or _settled_incarnation_matches_candidate(
                            canonical, candidate_submit
                        ):
                            # Same accounting incarnation (or unprovable
                            # recycle): the exact accounting row the fallback
                            # query returned, settled or not.
                            matching.append(canonical)
                        continue
                    # Active same-id row, submitted_at available or not: fail
                    # closed.
                    matching.append(canonical)
        # This caller plus at least one other current reserved-unbound claimant
        # for the same window/owner is ambiguity.
        if seen_claimant_keys:
            ambiguous = True
        return matching, ambiguous

    def _cleanup_reconcile_migration_temp_residues_unlocked(self) -> None:
        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import (
            _RECONCILE_INVENTORY_MIGRATION_MARKER,
            _RECONCILE_INVENTORY_ROLLBACK_PREP_RECEIPT,
            _RECONCILE_INVENTORY_ROLLFORWARD_RECEIPT,
            _RECONCILE_MIGRATION_TEMP_RE,
            FileOrchestrationJournalError,
        )

        prefixes = (
            f".{_RECONCILE_INVENTORY_MIGRATION_MARKER}.",
            f".{_RECONCILE_INVENTORY_ROLLBACK_PREP_RECEIPT}.",
            f".{_RECONCILE_INVENTORY_ROLLFORWARD_RECEIPT}.",
        )
        try:
            entry_names = file_orchestration_journal.list_directory_no_follow_limited(
                self.root,
                containment_root=self.root,
                max_entries=self.max_files,
            )
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_reconcile_inventory_migration_unavailable",
                field="reconcile_inventory_migration",
            ) from error
        if len(entry_names) > self.max_files:
            raise FileOrchestrationJournalError(
                "file_journal_record_limit_exceeded",
                field="reconcile_inventory_migration",
            )
        for entry_name in entry_names:
            if not entry_name.startswith(prefixes):
                continue
            if _RECONCILE_MIGRATION_TEMP_RE.fullmatch(entry_name) is None:
                raise FileOrchestrationJournalError(
                    "file_journal_reconcile_inventory_migration_invalid",
                    field="reconcile_inventory_migration",
                )
            self._remove_reconcile_atomic_residue_unlocked(
                self.root / entry_name,
                field="reconcile_inventory_migration",
            )

    def _remove_reconcile_atomic_residue_unlocked(self, path: Path, *, field: str) -> None:
        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        try:
            mode = file_orchestration_journal.stat_no_follow(path, containment_root=self.root).st_mode
            if not stat.S_ISREG(mode):
                raise SafeFilesystemError(f"Atomic residue must be a regular file: {path}")
            file_orchestration_journal.unlink_no_follow(path, containment_root=self.root)
        except FileNotFoundError:
            return
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_reconcile_inventory_unavailable",
                field=field,
            ) from error

    def _backfill_reconcile_inventory_unlocked(self) -> None:
        from services.orchestrator.file_orchestration_journal import _CycleRows

        # #1734 D11: the one-time inventory backfill is a lane of its own —
        # it is bounded and non-recurring, and folding it into a scan lane
        # would make a migration pass look like steady-state read growth.
        #
        # Phase 6i two-stage migration handoff: a batch terminal projection can
        # leave a settled current forecast master with a stale anchor and a
        # MISSING flat derived direct. This lane must NOT prune that anchor
        # (it is the only locator a fallback could use), so the journal-replay
        # sync below routes settled missing-direct masters through
        # ``_sync_reconcile_inventory_migration_row_unlocked``, which keeps a
        # handoff anchor instead of deleting it. The authority fingerprint
        # covers flat directs + legacy + journals but deliberately excludes
        # inventory anchors, so the migration before/after fingerprint stays
        # byte-identical. The marker is then written under the same global
        # inventory lock, and the steady-state ``_iter_reconcile_inventory_\
        # records`` handoff (restore-direct-then-prune) completes the locator.
        with journal_read_lane("reconcile_inventory_migration"):
            # Flat direct jobs contain masters/legacy rows only for current
            # accepted-submit cohorts; current candidate history lives under
            # pipeline-jobs/by-cycle and is deliberately never traversed here.
            for job in self._iter_reconcile_direct_pipeline_job_records():
                self._sync_reconcile_inventory_for_row_unlocked(job)
            for job in self._iter_legacy_active_reconcile_records():
                self._sync_reconcile_inventory_for_row_unlocked(job)
            # Every segment of one cycle replays through a SINGLE _CycleRows in
            # segment order: the inventory sync below is last-write-wins with no
            # replay arbitration, so a per-path _CycleRows would let the frozen
            # base segment resurrect anchors a continuation segment terminated
            # (or delete anchors it still owns).
            segments_by_cycle: dict[tuple[str, datetime], list[tuple[int, Path]]] = {}
            for path in self._iter_migration_journal_paths():
                source_id, cycle_time, segment_index = _journal_segment_identity_from_path(
                    path,
                    root=self.root,
                    surface="journal",
                )
                segments_by_cycle.setdefault((source_id, cycle_time), []).append((segment_index, path))
            for (source_id, cycle_time), segments in segments_by_cycle.items():
                rows = _CycleRows()
                for segment_index, path in sorted(segments):
                    for record in self._read_migration_jsonl(path, segment_index=segment_index):
                        self._apply_journal_record(
                            rows,
                            record,
                            source_id=source_id,
                            cycle_time=cycle_time,
                        )
                for job in rows.pipeline_jobs.values():
                    # Phase 6i: the migration lane preserves a handoff anchor for
                    # a settled current forecast master whose flat derived direct
                    # is missing, instead of pruning the only locator a fallback
                    # could use before the steady-state restore. Healthy rows
                    # (direct present) keep the ordinary prune semantics.
                    self._sync_reconcile_inventory_migration_row_unlocked(
                        job,
                        source_id=source_id,
                        cycle_time=cycle_time,
                    )

    def _iter_legacy_active_reconcile_records(
        self,
        *,
        expected_root_signature: Any = _UNSET,
    ) -> Iterable[dict[str, Any]]:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        for path in self._iter_migration_legacy_active_paths(
            expected_root_signature=expected_root_signature,
        ):
            payload = self._read_optional_json(path)
            if payload is None:
                raise FileOrchestrationJournalError(
                    "file_journal_reconcile_inventory_migration_invalid",
                    field="active_reconcile",
                )
            yield self._validated_direct_pipeline_job_record(
                payload,
                expected_job_id=_safe_segment(path.stem),
            )

    def _iter_migration_legacy_active_paths(
        self,
        *,
        expected_root_signature: Any = _UNSET,
    ) -> list[Path]:
        from services.orchestrator.file_orchestration_journal import _LEGACY_ACTIVE_RECONCILE_DIRECTORY

        return sorted(
            _iter_discovered_files(
                self.root / _LEGACY_ACTIVE_RECONCILE_DIRECTORY,
                root=self.root,
                suffix=".json",
                recursive=False,
                max_files=self.max_files,
                max_depth=1,
                strict_disappearance=True,
                expected_root_signature=expected_root_signature,
            )
        )

    def _iter_migration_journal_paths(
        self,
        *,
        expected_root_signature: Any = _UNSET,
    ) -> list[Path]:
        """Journal paths in parsed (source, cycle, segment) order.

        Never bare path order: lexicographically ``<cycle>.1.jsonl`` sorts
        BEFORE ``<cycle>.jsonl``, so a path-sorted replay would let the frozen
        base segment overwrite terminal states a continuation segment recorded.
        """
        return sorted(
            _iter_discovered_files(
                self.root / "journal",
                root=self.root,
                suffix=".jsonl",
                recursive=True,
                max_files=self.max_files,
                max_depth=self.max_depth,
                strict_disappearance=True,
                expected_root_signature=expected_root_signature,
            ),
            key=lambda path: _journal_segment_sort_key(path, root=self.root, surface="journal"),
        )

    def _read_migration_jsonl(self, path: Path, *, segment_index: int = 0) -> list[dict[str, Any]]:
        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        records = self._read_jsonl(path, segment_index=segment_index)
        try:
            mode = file_orchestration_journal.stat_no_follow(path, containment_root=self.root).st_mode
        except (FileNotFoundError, OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_reconcile_inventory_migration_invalid",
                field="journal",
            ) from error
        if not stat.S_ISREG(mode):
            raise FileOrchestrationJournalError(
                "file_journal_reconcile_inventory_migration_invalid",
                field="journal",
            )
        return records

    def _validated_reconcile_inventory_anchor(
        self,
        anchor: Mapping[str, Any],
        *,
        expected_job_id: str,
    ) -> tuple[str, datetime]:
        from services.orchestrator.file_orchestration_journal import (
            _RECONCILE_INVENTORY_SCHEMA_VERSION,
            FileOrchestrationJournalError,
        )

        if (
            anchor.get("schema_version") != _RECONCILE_INVENTORY_SCHEMA_VERSION
            or anchor.get("job_id") != expected_job_id
            or anchor.get("row_kind") not in {"current_master", "legacy"}
        ):
            raise FileOrchestrationJournalError(
                "file_journal_reconcile_inventory_invalid",
                field="reconcile_inventory",
            )
        source_id = _required_source_id(anchor, "source_id")
        cycle_time = _parse_cycle_time_field(anchor, "cycle_time")
        return source_id, cycle_time

    def _validate_reconcile_inventory_migration_marker(self, marker: Mapping[str, Any]) -> None:
        from services.orchestrator.file_orchestration_journal import (
            _RECONCILE_INVENTORY_MIGRATION_SCHEMA_VERSION,
            FileOrchestrationJournalError,
        )

        if set(marker) != {"schema_version", "completed_at"} or marker.get(
            "schema_version"
        ) != _RECONCILE_INVENTORY_MIGRATION_SCHEMA_VERSION:
            raise FileOrchestrationJournalError(
                "file_journal_reconcile_inventory_migration_invalid",
                field="reconcile_inventory_migration",
            )
        completed_at = marker.get("completed_at")
        if type(completed_at) is not str or not completed_at.endswith("Z"):
            raise FileOrchestrationJournalError(
                "file_journal_reconcile_inventory_migration_invalid",
                field="reconcile_inventory_migration",
            )
        try:
            parsed = datetime.fromisoformat(completed_at.removesuffix("Z") + "+00:00")
            canonical = _format_utc(_ensure_utc(parsed))
        except (TypeError, ValueError, OverflowError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_reconcile_inventory_migration_invalid",
                field="reconcile_inventory_migration",
            ) from error
        if canonical != completed_at:
            raise FileOrchestrationJournalError(
                "file_journal_reconcile_inventory_migration_invalid",
                field="reconcile_inventory_migration",
            )

    def _canonical_reconcile_job_unlocked(
        self,
        job_id: str,
        *,
        source_id: str,
        cycle_time: datetime,
        strict_disappearance: bool = False,
    ) -> dict[str, Any] | None:
        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import (
            _CANDIDATE_JOB_ID_RE,
            _LEGACY_ACTIVE_RECONCILE_DIRECTORY,
            FileOrchestrationJournalError,
            _CycleRows,
        )

        direct_path = self.root / "pipeline-jobs" / f"{job_id}.json"
        direct_paths = [direct_path]
        candidate_match = _CANDIDATE_JOB_ID_RE.fullmatch(job_id)
        if candidate_match is not None:
            direct_paths.append(
                self.root
                / "pipeline-jobs"
                / "by-cycle"
                / _safe_segment(_normalize_file_source_id(source_id, field="source_id"))
                / format_cycle_time(cycle_time)
                / f"{job_id}.json"
            )
        legacy_path = self.root / _LEGACY_ACTIVE_RECONCILE_DIRECTORY / f"{_safe_segment(job_id)}.json"
        # Watch every segment slot, present or not: a rollover appears as a new
        # file while the previously watched base stops changing.
        journal_directory = self._journal_directory(source_id=source_id)
        journal_paths = tuple(
            journal_directory / name for name in _journal_segment_names(format_cycle_time(cycle_time))
        )
        latest_directory = self.root / "latest" / _safe_segment(source_id) / format_cycle_time(cycle_time)
        latest_paths = (
            self._latest_paths(
                _safe_segment(source_id),
                format_cycle_time(cycle_time),
                model_id=None,
            )
            if strict_disappearance
            else []
        )
        watched_paths = (*direct_paths, legacy_path, *journal_paths, *latest_paths)
        signatures = {path: file_orchestration_journal._stat_signature(path) for path in watched_paths}
        latest_directory_signature = file_orchestration_journal._stat_signature(latest_directory)
        direct = self._direct_pipeline_job_record(job_id)
        legacy_payload = self._read_optional_json(legacy_path)
        legacy = (
            self._validated_direct_pipeline_job_record(legacy_payload, expected_job_id=job_id)
            if legacy_payload is not None
            else None
        )
        rows = _CycleRows()
        latest_rows = _CycleRows()
        for latest_path in latest_paths:
            payload = self._read_optional_json(latest_path)
            if payload is None:
                continue
            self._apply_latest_view(
                latest_rows,
                payload,
                source_id=source_id,
                cycle_time=cycle_time,
                expected_model_id=_safe_segment(latest_path.stem),
            )
        for record in self._cycle_journal_records(source_id=source_id, cycle_time=cycle_time):
            self._apply_journal_record(rows, record, source_id=source_id, cycle_time=cycle_time)
        if strict_disappearance and (
            any(signature != file_orchestration_journal._stat_signature(path) for path, signature in signatures.items())
            or latest_directory_signature != file_orchestration_journal._stat_signature(latest_directory)
        ):
            raise FileOrchestrationJournalError(
                "file_journal_quiescence_authority_changed",
                field="rollback_jobs",
            )
        replayed = rows.pipeline_jobs.get(job_id)
        latest = latest_rows.pipeline_jobs.get(job_id)
        authority = replayed or latest or direct or legacy
        canonical = dict(authority) if authority is not None else None
        if canonical is None:
            return None
        if _source_id_from_job(canonical) != source_id or _cycle_time_from_job(canonical) != cycle_time:
            raise FileOrchestrationJournalError(
                "file_journal_identity_mismatch",
                field="reconcile_inventory",
            )
        return canonical

    def _sync_reconcile_inventory_for_row_unlocked(self, row: Mapping[str, Any]) -> bool:
        from services.orchestrator.file_orchestration_journal import (
            _RECONCILE_INVENTORY_DIRECTORY,
            _RECONCILE_INVENTORY_SCHEMA_VERSION,
        )

        with self._reconcile_inventory_file_lock_unlocked():
            row_kind = _reconcile_inventory_row_kind(row)
            job_id = _required_safe_identity(row, "job_id")
            if row_kind is None or not _job_blocks_rollback_quiescence(row):
                self._remove_reconcile_inventory_anchor_unlocked(job_id)
                return False
            self._atomic_write_json_unlocked(
                self.root / _RECONCILE_INVENTORY_DIRECTORY / f"{job_id}.json",
                {
                    "schema_version": _RECONCILE_INVENTORY_SCHEMA_VERSION,
                    "job_id": job_id,
                    "source_id": _source_id_from_job(row),
                    "cycle_time": _format_utc(_cycle_time_from_job(row)),
                    "row_kind": row_kind,
                },
            )
            return True

    def _sync_reconcile_inventory_migration_row_unlocked(
        self,
        row: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> bool:
        """Migration-lane row sync (Phase 6i): preserve a canonical handoff
        anchor for a SETTLED current accepted-submit forecast master whose flat
        derived direct is MISSING.

        The one-time backfill replays each cycle journal to its final canonical
        row. Deleting the anchor of a settled master with a missing derived
        direct would remove the ONLY locator a fallback could use before the
        steady-state handoff restores the direct (the marker-absent wrong bind).
        So this lane keeps/establishes the anchor instead of pruning it; the
        authority fingerprint deliberately covers flat directs + legacy +
        journals but NOT inventory anchors, so writing an anchor leaves the
        migration before/after fingerprint byte-identical.

        Ordinary ``_sync_reconcile_inventory_for_row_unlocked`` semantics are
        unchanged: healthy terminal rows (direct present) still prune, active
        rows still publish, and legacy/candidate rows are unaffected. Only
        current forecast masters with a missing flat direct leave the handoff
        anchor. The caller holds the global inventory lock (never acquires a
        cycle lock here, preserving cycle -> inventory order and avoiding
        ABBA); the steady-state iterator later performs the cycle-locked
        restore-direct-then-prune (Phase 6h).
        """

        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import (
            _RECONCILE_INVENTORY_DIRECTORY,
            _RECONCILE_INVENTORY_SCHEMA_VERSION,
        )

        row_kind = _reconcile_inventory_row_kind(row)
        if row_kind != "current_master":
            return self._sync_reconcile_inventory_for_row_unlocked(row)
        if _job_blocks_rollback_quiescence(row):
            return self._sync_reconcile_inventory_for_row_unlocked(row)
        if not is_forecast_cohort_stage_name(
            str(row.get("stage") or ""),
            str(row.get("job_type") or ""),
        ):
            return self._sync_reconcile_inventory_for_row_unlocked(row)
        job_id = _required_safe_identity(row, "job_id")
        direct_path = self.root / "pipeline-jobs" / f"{job_id}.json"
        try:
            direct_missing = file_orchestration_journal.stat_no_follow(direct_path, containment_root=self.root) is None
        except FileNotFoundError:
            direct_missing = True
        if not direct_missing:
            # Healthy terminal row: prune normally (steady state stays bounded).
            return self._sync_reconcile_inventory_for_row_unlocked(row)
        # Missing derived direct: establish/preserve the handoff anchor.
        with self._reconcile_inventory_file_lock_unlocked():
            self._atomic_write_json_unlocked(
                self.root / _RECONCILE_INVENTORY_DIRECTORY / f"{job_id}.json",
                {
                    "schema_version": _RECONCILE_INVENTORY_SCHEMA_VERSION,
                    "job_id": job_id,
                    "source_id": _source_id_from_job(row),
                    "cycle_time": _format_utc(_cycle_time_from_job(row)),
                    "row_kind": row_kind,
                },
            )
            return True

    def _remove_reconcile_inventory_anchor_unlocked(self, job_id: str) -> None:
        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import _RECONCILE_INVENTORY_DIRECTORY

        with self._reconcile_inventory_file_lock_unlocked():
            try:
                file_orchestration_journal.unlink_no_follow(
                    self.root / _RECONCILE_INVENTORY_DIRECTORY / f"{_safe_segment(job_id)}.json",
                    containment_root=self.root,
                    missing_ok=True,
                )
            except (OSError, SafeFilesystemError) as error:
                raise OrchestratorError(
                    "FILE_JOURNAL_WRITE_FAILED",
                    "failed to update reconcile inventory",
                    {"error_type": type(error).__name__},
                ) from error

    def _iter_reconcile_direct_pipeline_job_records(self) -> Iterable[dict[str, Any]]:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        for path in self._iter_reconcile_direct_pipeline_job_paths():
            expected_job_id = _safe_segment(path.stem)
            payload = self._read_optional_json(path)
            if payload is None:
                raise FileOrchestrationJournalError(
                    "file_journal_reconcile_inventory_migration_invalid",
                    field="pipeline_jobs",
                )
            yield self._validated_direct_pipeline_job_record(payload, expected_job_id=expected_job_id)

    def _iter_reconcile_direct_pipeline_job_paths(
        self,
        *,
        strict_disappearance: bool = False,
        expected_root_signature: Any = _UNSET,
    ) -> Iterable[Path]:
        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        directory = self.root / "pipeline-jobs"
        if self.max_files <= 0 or self.max_depth < 0:
            return
        if (
            expected_root_signature is not _UNSET
            and file_orchestration_journal._stat_signature(directory) != expected_root_signature
        ):
            raise FileOrchestrationJournalError(
                "file_journal_quiescence_authority_changed",
                field="pipeline_jobs",
            )
        try:
            directory_mode = file_orchestration_journal.stat_no_follow(directory, containment_root=self.root).st_mode
        except FileNotFoundError:
            if strict_disappearance and expected_root_signature is not None:
                raise FileOrchestrationJournalError(
                    "file_journal_quiescence_authority_changed",
                    field="pipeline_jobs",
                )
            return
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_reconcile_inventory_migration_unavailable",
                field="pipeline_jobs",
            ) from error
        if not stat.S_ISDIR(directory_mode):
            raise FileOrchestrationJournalError(
                "file_journal_reconcile_inventory_migration_invalid",
                field="pipeline_jobs",
            )
        try:
            entry_names = file_orchestration_journal.list_directory_no_follow_limited(
                directory,
                containment_root=self.root,
                max_entries=self.max_files,
            )
        except FileNotFoundError:
            if strict_disappearance:
                raise FileOrchestrationJournalError(
                    "file_journal_quiescence_authority_changed",
                    field="pipeline_jobs",
                )
            return
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_reconcile_inventory_migration_unavailable",
                field="pipeline_jobs",
            ) from error
        if len(entry_names) > self.max_files:
            raise FileOrchestrationJournalError(
                "file_journal_record_limit_exceeded",
                field="pipeline_jobs",
            )
        if (
            expected_root_signature is not _UNSET
            and file_orchestration_journal._stat_signature(directory) != expected_root_signature
        ):
            raise FileOrchestrationJournalError(
                "file_journal_quiescence_authority_changed",
                field="pipeline_jobs",
            )
        for entry_name in sorted(entry_names):
            if entry_name == "by-cycle":
                try:
                    mode = file_orchestration_journal.stat_no_follow(
                        directory / entry_name, containment_root=self.root
                    ).st_mode
                except (FileNotFoundError, OSError, SafeFilesystemError) as error:
                    raise FileOrchestrationJournalError(
                        "file_journal_reconcile_inventory_migration_invalid",
                        field="pipeline_jobs",
                    ) from error
                if not stat.S_ISDIR(mode):
                    raise FileOrchestrationJournalError(
                        "file_journal_reconcile_inventory_migration_invalid",
                        field="pipeline_jobs",
                    )
                continue
            if not entry_name.endswith(".json"):
                raise FileOrchestrationJournalError(
                    "file_journal_reconcile_inventory_migration_invalid",
                    field="pipeline_jobs",
                )
            if _SAFE_SEGMENT_RE.fullmatch(entry_name) is None:
                raise FileOrchestrationJournalError(
                    "file_journal_reconcile_inventory_migration_invalid",
                    field="pipeline_jobs",
                )
            path = directory / entry_name
            try:
                mode = file_orchestration_journal.stat_no_follow(path, containment_root=self.root).st_mode
            except FileNotFoundError:
                raise FileOrchestrationJournalError(
                    "file_journal_reconcile_inventory_migration_invalid",
                    field="pipeline_jobs",
                )
            except (OSError, SafeFilesystemError) as error:
                raise FileOrchestrationJournalError(
                    "file_journal_reconcile_inventory_migration_unavailable",
                    field="pipeline_jobs",
                ) from error
            if not stat.S_ISREG(mode):
                raise FileOrchestrationJournalError(
                    "file_journal_reconcile_inventory_migration_invalid",
                    field="pipeline_jobs",
                )
            yield path

    def _reconcile_inventory_anchor_exists_unlocked(self, path: Path) -> bool:
        from services.orchestrator import file_orchestration_journal

        try:
            mode = file_orchestration_journal.stat_no_follow(path, containment_root=self.root).st_mode
        except FileNotFoundError:
            return False
        except (OSError, SafeFilesystemError) as error:
            raise OrchestratorError(
                "FILE_JOURNAL_WRITE_FAILED",
                "failed to inspect reconcile inventory anchor",
                {"error_type": type(error).__name__},
            ) from error
        if not stat.S_ISREG(mode):
            raise OrchestratorError(
                "FILE_JOURNAL_WRITE_FAILED",
                "reconcile inventory anchor must be a regular file",
            )
        return True
