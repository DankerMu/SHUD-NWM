from __future__ import annotations

import contextvars
import functools
import hashlib
import inspect
import json
import logging
import os
import re
import stat
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar

from packages.common.auth_policy import PolicyDecision, require_policy_evidence, trusted_internal_policy_decision
from packages.common.redaction import is_sensitive_key
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    list_directory_no_follow_limited,
    read_bytes_durable_no_follow,
    read_bytes_limited_no_follow,
    stat_no_follow,
    unlink_no_follow,
    unlink_no_follow_durable,
)
from packages.common.slurm_env import secret_manifest_value_reason
from packages.common.source_identity import normalize_source_id
from services.orchestrator import chain_repository_state
from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_PROJECTION_FIELDS,
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD,
    IDENTITY_MISMATCH_RELEASED_DECISION,
    INIT_STATE_IDENTITY_FIELD,
    MAX_FORECAST_COHORT_MEMBERS,
    OPERATOR_VERIFIED_ABSENCE_DECISION,
    QUARANTINE_RERUN_PROVENANCE_FIELD,
    SLURM_ACCOUNTING_SUBMITTED_AT_FIELD,
    SLURM_BINDING_SOURCE_FIELD,
    AcceptedSubmitCommitResult,
    AcceptedSubmitEvidenceError,
    AcceptedSubmitTransition,
    accepted_submit_candidate_immutable_evidence,
    accepted_submit_contract_is_current,
    accepted_submit_master_identity_is_structural,
    accepted_submit_master_immutable_identity,
    accepted_submit_master_ordinary_upsert_state,
    accepted_submit_row_kind,
    apply_accepted_submit_transition,
    bind_replay_is_idempotent,
    binding_source_for_transition,
    init_state_identity_for_task,
    is_forecast_cohort_stage_name,
    matched_bound_reconciliation_source,
    normalize_accepted_submit_attempt_anchor,
    normalize_accepted_submit_evidence,
    normalize_candidate_projections,
    normalize_init_state_identities,
    normalize_quarantine_rerun_model_ids,
    normalize_slurm_accounting_submitted_at,
    ordered_cohort_members,
)
from services.orchestrator.chain_repository import (
    ACTIVE_HYDRO_STATUSES,
    COMPLETED_HYDRO_STATUSES,
    DEFAULT_CANDIDATE_STATE_EVENT_LIMIT,
    DEFAULT_CANDIDATE_STATE_JOB_LIMIT,
)
from services.orchestrator.chain_source_cycle import _datetime_sort_key, _pipeline_job_truth_sort_key
from services.orchestrator.chain_types import ForcingContext, ModelContext, OrchestratorError
from services.orchestrator.retry import (
    _DB_FREE_REQUIRED_SELECTOR_FIELDS,
    _DB_FREE_RUNTIME_FIELDS,
    _REQUIRED_RUNTIME_ROOT_FIELDS,
    _RUNTIME_ROOT_EVENT_CANDIDATE_LIMIT,
    _RUNTIME_ROOT_EVENT_ROW_SCAN_LIMIT,
    _RUNTIME_ROOT_FIELDS,
    _RUNTIME_ROOT_REJECTION_EVIDENCE_LIMIT,
    ACTIVE_RETRY_STATUSES,
    DOWNLOAD_SOURCE_CYCLE_JOB_TYPE,
    MANUAL_RETRY_DURABLE_SUCCESS_STATUSES,
    MANUAL_RETRY_SOURCE_STATUSES,
    PARTIAL_OR_FAILED_HYDRO_STATUSES,
    RETRY_RUNTIME_ROOTS_SECRET_BEARING,
    RETRY_RUNTIME_ROOTS_UNRESOLVED,
    RETRY_RUNTIME_ROOTS_UNSAFE,
    TERMINAL_SUCCESS_RETRY_STATUSES,
    RetryConfig,
    RetryConflictError,
    RetryError,
    RetryNotFoundError,
    _attach_retry_runtime_root_contract,
    _attach_retry_runtime_root_resolution,
    _candidate_batch_db_free_required,
    _event_details_is_manual_retry_submission,
    _has_runtime_root_field,
    _mapping_at,
    _resolve_db_free_runtime_candidate,
    _resolve_runtime_root_candidate,
    _retry_submission_error_code,
    _retry_submission_manifest,
    _RetryRuntimeRootResolutionError,
    _RetrySubmissionJob,
    _runtime_root_contract_from_error,
    _runtime_root_env_candidate,
    _runtime_root_resolution_evidence,
    _runtime_root_resolution_from_error,
    _RuntimeRootCandidate,
    _RuntimeRootCandidateBatch,
    _safe_error_message,
    auto_retry_skipped_details,
    classify_failure,
    compute_backoff_seconds,
    warn_unknown_error_code,
)
from services.orchestrator.retry_identity import (
    RETRY_JOB_ID_MARKER,
    effective_retry_attempt,
    split_retry_job_identity,
)
from services.orchestrator.run_identity import (
    ANALYSIS_RUN_ID_RE as _ANALYSIS_RUN_ID_RE,
)
from services.orchestrator.run_identity import (
    CYCLE_COHORT_RUN_ID_RE as _CYCLE_COHORT_RUN_ID_RE,
)
from services.orchestrator.run_identity import (
    FORECAST_RUN_ID_RE as _FORECAST_RUN_ID_RE,
)
from services.orchestrator.scheduler_file_providers import (
    _public_raw_manifest_evidence,
    _sanitize_file_provider_evidence_scalar,
)
from services.orchestrator.scheduler_init_state_match import (
    INIT_STATE_IDENTITY_FIELDS,
    init_state_field,
)
from services.orchestrator.scheduler_state import _ensure_utc, _evidence_safe, _format_utc
from services.orchestrator.scheduler_state_manual_retry import MARKER_TARGET_ROW_DETAIL_FIELDS
from services.slurm_gateway.models import SubmitJobRequest
from workers.data_adapters.base import cycle_id_for, format_cycle_time, parse_cycle_time

LOGGER = logging.getLogger(__name__)

FILE_JOURNAL_LOCK_GUARD_MODE_ENV = "NHMS_SCHEDULER_JOURNAL_LOCK_GUARD_MODE"
LEGACY_FILE_LOCK_GUARD_MODE_ENV = "NHMS_SCHEDULER_FILE_LOCK_GUARD_MODE"
FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION = "nhms.scheduler.file_orchestration_journal.v1"
FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION = "nhms.scheduler.file_orchestration_latest.v1"
FILE_ORCHESTRATION_PRIVATE_RECOVERY_SCHEMA_VERSION = "nhms.scheduler.file_orchestration_private_recovery.v1"
MAX_FILE_JOURNAL_JSON_BYTES = 16 * 1024 * 1024
MAX_FILE_JOURNAL_RECORDS = 100_000
#: Total per-cycle event-log segments (base ``<cycle>.jsonl`` plus continuation
#: ``<cycle>.<n>.jsonl``).  Bounded at 3 because a replay reads every segment:
#: 3 x MAX_FILE_JOURNAL_JSON_BYTES = 48 MiB stays under
#: MAX_FILE_JOURNAL_READ_CACHE_BYTES (64 MiB) with headroom for other cycles,
#: while a 4th segment would evict the whole process read cache on every
#: replay.  The retry cap (#1163) remains the primary growth guard.
MAX_FILE_JOURNAL_CYCLE_SEGMENTS = 3
MAX_FILE_JOURNAL_DISCOVERED_FILES = 100_000
MAX_FILE_JOURNAL_SCAN_DEPTH = 32
MAX_FILE_JOURNAL_JSON_DEPTH = 64
MAX_FILE_JOURNAL_JSON_NODES = 300_000
MAX_FILE_JOURNAL_PATH_SEGMENT_CHARS = 255
MAX_FILE_JOURNAL_CYCLE_ROWS_CACHE_ENTRIES = 512
#: Total attempts (initial plus retries) the journal's cached read chokepoint
#: makes against a mid-open atomic replacement of the file it is reading.  An
#: ``os.replace`` leaves the new inode stable immediately, so one re-read
#: suffices; the cap keeps a relentless writer failing closed (design D5).
MAX_FILE_JOURNAL_IDENTITY_RETRY_ATTEMPTS = 3
MAX_FILE_JOURNAL_READ_CACHE_ENTRIES = 4096
MAX_FILE_JOURNAL_READ_CACHE_BYTES = 64 * 1024 * 1024

# ---------------------------------------------------------------------------
# #1734 D11: always-on per-entrypoint journal read attribution.
#
# The 2026-08-23 node-22 receipt left three candidate mechanisms (A full-tree
# replay, B flat-directory re-read, C absent memo) and traced none of them.
# node-22 pulls from GitHub, so a monkeypatched probe is not an option: the
# counter has to ship in the repo and be on by default. It is a dict increment
# per read — no new IO, and its own dedicated lock, which is increment-only and
# never held while acquiring anything else, so the journal's single lock order
# (`_write_lock` -> `_cache_lock`) is untouched (spec
# `pipeline-job-persistence` L550).
# ---------------------------------------------------------------------------
JOURNAL_READ_ATTRIBUTION_SCHEMA_VERSION = "nhms.file_journal.read_attribution.v1"
#: Guard against a pathological tag explosion in a single pass; the live tag
#: space is entrypoint x lane, measured on the 308-test reference fixture at
#: ~51 live tags per pass window and 72 distinct across the whole session,
#: against this cap of 256, with ``tags_dropped`` observed 0.
MAX_JOURNAL_READ_ATTRIBUTION_TAGS = 256
_JOURNAL_READ_UNATTRIBUTED = "unattributed"
_ClassT = TypeVar("_ClassT", bound=type)
_journal_read_counter_lock = threading.Lock()
_journal_read_counters: dict[str, dict[str, int]] = {}
_journal_read_counter_tags_dropped = 0
_journal_read_entrypoint: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nhms_journal_read_entrypoint",
    default=_JOURNAL_READ_UNATTRIBUTED,
)
_journal_read_lane: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nhms_journal_read_lane",
    default=_JOURNAL_READ_UNATTRIBUTED,
)


_flat_direct_job_listing_memo: contextvars.ContextVar[dict[str, list[Path]] | None] = contextvars.ContextVar(
    "nhms_flat_direct_job_listing_memo",
    default=None,
)


@contextmanager
def _flat_direct_job_listing_memo_scope() -> Iterable[None]:
    """Memoize the flat ``pipeline-jobs/`` LISTING for one read-only call.

    Scope is deliberately narrow (#1810 design D14). The listing is
    point-in-time on a live journal, which is only safe because the mutating
    paths re-read under ``_locked_cycle_write`` and compare there; a memo that
    outlived one query, or that any write path entered, would turn "at most a
    refused invocation" into a bad write. So this is a ``ContextVar`` bound to
    one call frame, never an instance cache, and it is entered from exactly one
    read-only caller.

    Only the raw, unfiltered directory listing is memoized — the per-cycle
    filename filter still runs per call, so
    ``_flat_direct_pipeline_job_paths_for_cycle`` stays the ONE definition of
    that filter.
    """

    token = _flat_direct_job_listing_memo.set({})
    try:
        yield
    finally:
        _flat_direct_job_listing_memo.reset(token)


@contextmanager
def journal_read_lane(lane: str) -> Iterable[None]:
    """Tag every journal read made in this context with its reader lane.

    The lanes are design D11's candidate mechanisms: ``full_tree_replay`` (A),
    ``direct_flat_scan`` (B) and ``cycle_replay`` (C). Nesting is honoured —
    the innermost lane wins — so a cycle replay reached from a full-tree
    fall-open is still attributed to the lane that actually opened the file.
    """

    token = _journal_read_lane.set(lane)
    try:
        yield
    finally:
        _journal_read_lane.reset(token)


def _attributed_public_method(name: str, func: Callable[..., Any]) -> Callable[..., Any]:
    """Tag one public method as the entrypoint for every read beneath it.

    OUTERMOST wins, unlike ``journal_read_entrypoint``: the public API call the
    caller actually made is the attribution answer, so a public method reached
    from another public method (``upsert_pipeline_job`` -> ``get_pipeline_job``,
    the retry service -> the repository) does not re-tag and steal its caller's
    bytes.
    """

    @functools.wraps(func)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        if _journal_read_entrypoint.get() != _JOURNAL_READ_UNATTRIBUTED:
            return func(self, *args, **kwargs)
        token = _journal_read_entrypoint.set(name)
        try:
            return func(self, *args, **kwargs)
        finally:
            _journal_read_entrypoint.reset(token)

    return wrapper


def _install_public_read_attribution(cls: _ClassT) -> _ClassT:
    """Tag the whole public surface of ``cls``, by boundary rather than by list.

    Round 1 measured why this is not an enumerated list: six methods carried a
    hand-written tag, the design claimed eight, and 80.6% of a real fixture's
    bytes carried no entrypoint at all. ``query_inflight_jobs``, the
    cycle-status predicates and every write path that reads before it writes
    were all silently untagged. A list drifts; a boundary cannot.

    Generators are skipped deliberately: a tag spanning a ``yield`` can have
    its ``ContextVar`` token reset from a context that never set it (design
    D11), and an instrument must never be able to fail a read path. There are
    none on this surface today; the guard is what keeps it that way.
    """

    for name, value in list(vars(cls).items()):
        if name.startswith("_") or not inspect.isfunction(value):
            continue
        if inspect.isgeneratorfunction(value) or getattr(value, "__wrapped__", None) is not None:
            continue
        setattr(cls, name, _attributed_public_method(name, value))
    return cls


def _record_journal_read(*, byte_count: int, cached: bool) -> None:
    """Account one read-primitive call against the current ``(entrypoint, lane)``.

    ``cached`` reads are byte-cache hits: they cost no ``rchar``, so they are
    counted separately and never folded into ``bytes``, which must reconcile
    against the ``/proc`` figure the receipt reports.
    """

    tag = f"{_journal_read_entrypoint.get()}|{_journal_read_lane.get()}"
    global _journal_read_counter_tags_dropped
    with _journal_read_counter_lock:
        entry = _journal_read_counters.get(tag)
        if entry is None:
            if len(_journal_read_counters) >= MAX_JOURNAL_READ_ATTRIBUTION_TAGS:
                _journal_read_counter_tags_dropped += 1
                return
            entry = {"calls": 0, "bytes": 0, "cache_hit_calls": 0, "cache_hit_bytes": 0}
            _journal_read_counters[tag] = entry
        if cached:
            entry["cache_hit_calls"] += 1
            entry["cache_hit_bytes"] += byte_count
        else:
            entry["calls"] += 1
            entry["bytes"] += byte_count


def reset_journal_read_counters() -> None:
    """Zero the counters at scheduler pass entry so totals are per-pass."""

    global _journal_read_counter_tags_dropped
    with _journal_read_counter_lock:
        _journal_read_counters.clear()
        _journal_read_counter_tags_dropped = 0


def journal_read_attribution() -> dict[str, Any]:
    """Snapshot the counters as an evidence-shaped payload.

    Snapshotting copies every counter under the lock before returning, so the
    caller can serialise it while other threads keep reading; handing out the
    live dicts would let a row change mid-serialisation.
    """

    with _journal_read_counter_lock:
        rows = [
            {"tag": tag, **{field_name: int(value) for field_name, value in entry.items()}}
            for tag, entry in _journal_read_counters.items()
        ]
        tags_dropped = _journal_read_counter_tags_dropped
    rows.sort(key=lambda row: (-int(row["bytes"]), str(row["tag"])))
    totals = {
        "calls": sum(int(row["calls"]) for row in rows),
        "bytes": sum(int(row["bytes"]) for row in rows),
        "cache_hit_calls": sum(int(row["cache_hit_calls"]) for row in rows),
        "cache_hit_bytes": sum(int(row["cache_hit_bytes"]) for row in rows),
    }
    return {
        "schema_version": JOURNAL_READ_ATTRIBUTION_SCHEMA_VERSION,
        "github_issue": 1734,
        "interpretation": (
            "bytes are filesystem reads only (rchar-contributing); cache_hit_* are "
            "in-process byte-cache hits and cost no syscall"
        ),
        "totals": totals,
        "tags": rows,
        "tags_dropped": tags_dropped,
    }
# #1748: the searchable token an operator greps for.  Used verbatim as the
# ``event_type`` (not sanitized on the public read path) AND inside the message,
# so ``grep`` finds the wedge in either rendering.
IDENTITY_RELEASED_OPERATOR_SIGNAL = "IDENTITY_RELEASED_RESERVATION_NEEDS_OPERATOR"
# The degraded trace emitted when that signal itself cannot be written.
IDENTITY_RELEASED_SIGNAL_FAILED_EVENT = "identity_released_operator_signal_failed"
# #1748: the ``nhms-pipeline`` subcommand that actually recovers the wedge.
# Defined HERE rather than in ``cli.py`` because the signal has to name it and
# ``cli`` already imports this module -- the other direction would be a cycle.
# ``cli`` registers the subcommand from this same constant, so the name in the
# journal record cannot drift away from the name a shell will accept.
RELEASED_RESERVATION_RECOVERY_COMMAND = "recover-released-identity-blocked-reservation"
#: #1796: durable best-effort event for a committed reservation/reclaim whose
#: derived direct/inventory/latest projection failed AFTER the authority append.
#: Every field is a fixed non-secret token or validated projection/model identity
#: -- never exception text, class name, path, ``.error_code``, ``.reason``, repr,
#: or any secret-shaped detail.  A failure to emit it never reverses the
#: committed reservation.
COMMITTED_PROJECTION_FAULT_EVENT = "committed_projection_fault"


def _released_reservation_recovery_command(job_id: str) -> str:
    """The runnable invocation, not the discovery form.

    A human reading this record needs the form that ACTS: the round-3 defect was
    a signal naming something its reader could not invoke, and half-closing that
    with a command they still have to figure out how to arm would repeat it.
    ``--journal-root`` is a placeholder because the record cannot know the
    operator's deployment root; the shape makes the missing piece obvious.
    """

    return (
        f"nhms-pipeline {RELEASED_RESERVATION_RECOVERY_COMMAND} "
        f"--journal-root <journal-root> --job-id {job_id} --attest"
    )
# #1748: the durable operator-recovery attestation.  It is a MARKER on the
# released row, never a pre-materialized successor: writing the successor eagerly
# occupies the very job_id/idempotency key the ordinary retry path would mint,
# and the ordinary path refuses to submit a row it did not itself reserve.
OPERATOR_RECOVERY_ATTESTATION_FIELD = "operator_recovery_attested_at"
_RECONCILE_INVENTORY_DIRECTORY = "reconcile-inventory"
_RECONCILE_INVENTORY_SCHEMA_VERSION = "nhms.scheduler.reconcile_inventory.v1"
_RECONCILE_INVENTORY_MIGRATION_SCHEMA_VERSION = "nhms.scheduler.reconcile_inventory_migration.v1"
_RECONCILE_INVENTORY_MIGRATION_MARKER = "reconcile-inventory-migration-v1.json"
_RECONCILE_INVENTORY_ROLLBACK_PREP_SCHEMA_VERSION = (
    "nhms.scheduler.reconcile_inventory_rollback_preparation.v2"
)
_RECONCILE_INVENTORY_ROLLBACK_PREP_RECEIPT = "reconcile-inventory-rollback-preparation-v2.json"
_RECONCILE_INVENTORY_ROLLFORWARD_SCHEMA_VERSION = (
    "nhms.scheduler.reconcile_inventory_rollforward.v1"
)
_RECONCILE_INVENTORY_ROLLFORWARD_RECEIPT = "reconcile-inventory-rollforward-v1.json"
_LEGACY_ACTIVE_RECONCILE_DIRECTORY = "active-reconcile"
_ATOMIC_TEMP_NONCE_RE = r"[0-9a-f]{32}"
_RECONCILE_INVENTORY_TEMP_RE = re.compile(
    rf"^\.(?P<target>[A-Za-z0-9_.-]+\.json)\.{_ATOMIC_TEMP_NONCE_RE}\.tmp$"
)
_RECONCILE_MIGRATION_TEMP_RE = re.compile(
    rf"^\.(?:{re.escape(_RECONCILE_INVENTORY_MIGRATION_MARKER)}|"
    rf"{re.escape(_RECONCILE_INVENTORY_ROLLBACK_PREP_RECEIPT)}|"
    rf"{re.escape(_RECONCILE_INVENTORY_ROLLFORWARD_RECEIPT)})\.{_ATOMIC_TEMP_NONCE_RE}\.tmp$"
)
# Journal replay order is a fixed stride of MAX_FILE_JOURNAL_RECORDS per
# segment, so the latest view must sit above every reachable journal line to
# keep winning same-sequence ties in _replay_order_key.  Raised in lockstep
# with MAX_FILE_JOURNAL_CYCLE_SEGMENTS.
_LATEST_REPLAY_ORDER_SENTINEL = MAX_FILE_JOURNAL_CYCLE_SEGMENTS * MAX_FILE_JOURNAL_RECORDS + 1
_UNSET = object()
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_JOURNAL_SEGMENT_INDEX_RE = re.compile(r"[1-9][0-9]*")
# The forecast and cohort shapes now live in run_identity (#1405) so retention
# adjudicates deletions against the same canonical identities; they are
# imported above under their historical private names. This strict cohort
# variant stays here — its only consumer, the cohort branch of
# `_model_id_from_run_identity`, needs an exact cohort id.
_CYCLE_RUN_ID_RE = re.compile(r"^cycle_([^_]+)_(\d{10})$")
_CANDIDATE_JOB_ID_RE = re.compile(r"^job_fcst_([^_]+)_(\d{10})_.+$")
_ACCEPTED_SUBMIT_MASTER_JOB_ID_RE = re.compile(r"^job_cycle_([^_]+)_(\d{10})_.+$")
_REPLAY_SEQUENCE_FIELD = "_file_journal_replay_sequence"
_REPLAY_ORDER_FIELD = "_file_journal_replay_order"
_PIPELINE_JOB_UPSERT_MUTABLE_FIELDS = (
    "accepted_submit_contract_version",
    "slurm_job_id",
    "array_task_id",
    "model_id",
    "status",
    "stage",
    "idempotency_key",
    "candidate_id",
    "submitted_at",
    "started_at",
    "finished_at",
    "exit_code",
    "retry_count",
    "manual_retry_marker",
    "previous_job_id",
    "error_code",
    "error_message",
    "log_uri",
    "submit_outcome",
    "slurm_comment",
    "cohort_members",
    "cohort_digest",
    # Merged like its cohort siblings so a divergent incoming map actually
    # REACHES the frozen ordinary-upsert check below and is rejected (#1183).
    # Omitting it here would make the merge keep the persisted value and the
    # frozen check compare persisted-against-persisted — a silent drop instead
    # of the required ``file_journal_evidence_invariant_invalid``.
    INIT_STATE_IDENTITY_FIELD,
    # Merged for the same reason (#1157): a divergent incoming provenance list
    # must REACH the frozen check and be rejected, never silently keep the
    # persisted one — retroactively arming the breaker is the failure this
    # guards against.
    QUARANTINE_RERUN_PROVENANCE_FIELD,
    "restart_stage",
    "submission_attempt",
    "submission_attempt_started_at",
    "expected_slurm_user",
    "expected_slurm_account",
    "slurm_ownership_required",
    "reconciliation_source",
    "reconciliation_decision",
    "reconciliation_reason_class",
    "matched_slurm_job_id",
    "candidate_projections",
    "cancellation_receipt_recorded",
    "identity_blocked_streak",
    # #1850 Fix A: merged like the other closed master state so a divergent
    # incoming binding-provenance value actually REACHES the frozen ordinary-
    # upsert check and is rejected, never silently kept/dropped.
    SLURM_BINDING_SOURCE_FIELD,
    SLURM_ACCOUNTING_SUBMITTED_AT_FIELD,
    "native_shud_resubmitted",
)
_RUNTIME_ROOT_EVENT_CANDIDATE_PATHS = (
    ("runtime_root_contract",),
    ("submission_manifest",),
    ("submitted_manifest",),
    ("request_manifest",),
    ("slurm_submission_manifest",),
    ("manifest",),
    ("gateway_response", "manifest"),
    ("slurm", "manifest"),
)
_PRIVATE_RUNTIME_ROOT_RECOVERY_RECORD_TYPE = "pipeline_event_runtime_root_recovery"
_RUNTIME_ROOT_SAME_RUN_JOB_SCAN_LIMIT = 32
_SUPPORTED_PIPELINE_EVENT_ENTITY_TYPES = {"pipeline_job", "forecast_cycle"}
_ARRAY_MANUAL_RETRY_JOB_TYPES = frozenset(
    {
        "hindcast",
        "produce_forcing_array",
        "run_shud_forecast_array",
        "parse_output_array",
    }
)
_ARRAY_MANUAL_RETRY_MANIFEST_INDEX_NAMES = {
    "produce_forcing_array": "forcing_manifest_index.json",
    "run_shud_forecast_array": "forecast_manifest_index.json",
    "parse_output_array": "parse_manifest_index.json",
    "hindcast": "hindcast_manifest_index.json",
}

TERMINAL_PIPELINE_STATUSES = {
    "succeeded",
    "partially_failed",
    "failed",
    "cancelled",
    "submission_failed",
    "reservation_lost",
    "permanently_failed",
}
#: Exactly the master statuses the cohort task projection can DERIVE from task
#: outcomes.  A persisted status inside this set is projection-owned and keeps
#: being overwritten by the current pass; a persisted status outside it cannot
#: be derived here, so the projection preserves it (status stickiness).  Under
#: the routed domain that means ``permanently_failed`` and ``cancelled``.
#: ``submission_failed`` and ``reservation_lost`` never reach this projection:
#: their submit-outcome/inventory gates reject them before the accounting
#: tuple could be rewritten as ``matched_bound``, so they are deliberately NOT
#: listed here -- the derived-domain predicate must not be widened to accept
#: an accounting tuple this function cannot legally write.
PROJECTION_DERIVED_MASTER_STATUSES = frozenset(
    {
        "succeeded",
        "partially_failed",
        "failed",
    }
)
#: The persisted master statuses a permanent-failure mark may transition FROM
#: (#1312).  A live row (``reserved``/``running``/...) is a stale caller, never
#: an error: the retry decline that owns the mark runs off its own snapshot and
#: must never raise into the orchestration cycle.
#:
#: ``reservation_lost`` is deliberately EXCLUDED.  A lost reservation means "to
#: be reclaimed", not "permanently failed": marking it would slam shut both
#: recovery doors that key off the literal status (the reclaim predicate in
#: ``reclaim_pipeline_job_reservation`` and the reconcile-verified retry
#: shortcut), and its ``identity_mismatch_released`` sub-shape may not coexist
#: with any other status at all (``accepted_submit_identity``).  Declining is
#: the liveness-fail-safe direction.
#:
#: ``partially_failed`` is deliberately EXCLUDED for the same family of reason.
#: A partially failed master's cohort keeps ADVANCING downstream under the
#: #1202 partial-advance contract — its succeeded members are still owed the
#: parse/state-save/publish stages — and the only decline entrance that can
#: reach the mark with that status is the nested partial-array-retry exit
#: (``chain_forecast_execution`` :547), not the main decline arm.  Marking it
#: would flip the next pass's resume from ``parsed_partial`` to ``failed_run``
#: with an error code and skip every downstream stage: "part of the cohort
#: failed" is not "the whole job is permanently dead".
PERMANENT_FAILURE_SOURCE_STATUSES = frozenset(
    {
        "failed",
        "submission_failed",
    }
)
_ACCEPTED_RUNTIME_TRANSITIONS = {
    "submitted": frozenset({"submitted", "pending", "queued", "running", "reconcile_unverified"}),
    "pending": frozenset({"pending", "queued", "running", "reconcile_unverified"}),
    "queued": frozenset({"queued", "running", "reconcile_unverified"}),
    "running": frozenset({"running", "reconcile_unverified"}),
    "cancellation_pending": frozenset({"cancellation_pending"}),
    "reconcile_unverified": frozenset({"reconcile_unverified"}),
}
_GENERIC_VERSIONED_RECONCILIATION_DECISIONS = frozenset(
    {
        None,
        "accounting_unavailable",
        "absence_deferred",
        "identity_mismatch_blocked",
        "multiple_matches_blocked",
    }
)
_TERMINAL_FORECAST_CYCLE_SUCCESS_STATUSES = {"complete", "succeeded", "parsed", "published"}
_STAGE_STATUS_ORDER = {
    "download": 1,
    "download_gfs": 1,
    "download_source_cycle": 1,
    "convert": 2,
    "convert_canonical": 2,
    "forcing": 3,
    "produce_forcing": 3,
    "forecast": 4,
    "run_shud_forecast": 4,
    "parse": 5,
    "state_save_qc": 6,
    "publish": 8,
    "era5_download": 11,
    "canonical_convert": 12,
    "forcing_produce": 13,
    "analysis_run": 14,
    "parse_output": 15,
}
_UNKNOWN_STAGE_STATUS_ORDER = 99

__all__ = (
    "FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION",
    "FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION",
    "FileJournalRetentionCycle",
    "FileJournalRetentionCycleInspection",
    "FileJournalRetentionDiscovery",
    "FileJournalRetentionMember",
    "FileJournalRetentionRemoval",
    "FileJournalRetryService",
    "FileOrchestrationJournalError",
    "FileOrchestrationJournalRepository",
    "RetryEvidenceInvalidError",
)


class FileOrchestrationJournalError(RuntimeError):
    def __init__(self, reason: str, *, field: str, evidence: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.field = field
        self.evidence = dict(evidence or {})


class RetryEvidenceInvalidError(RetryError):
    """Durable retry evidence failed file-journal identity validation (409).

    Raised at the file retry service boundary while CONSTRUCTING a pending
    manual retry: the private durable predecessor cannot satisfy the journal's
    normalization contract, so no retry row is written.  Details carry only the
    run id and the journal's stable reason/field tokens -- never the raw
    journal evidence, which may embed private paths or URIs.
    """

    status_code = 409

    def __init__(self, run_id: str, *, reason: str, field: str) -> None:
        super().__init__(
            "RETRY_EVIDENCE_INVALID",
            "Retry evidence failed file-journal validation.",
            {"run_id": run_id, "journal_reason": reason, "journal_field": field},
        )


@dataclass(frozen=True)
class _PendingManualRetryResult:
    """Lock-held producer result: caller-facing row plus trusted private copy.

    ``public_job`` keeps the pre-existing caller-facing retry namespace; the
    ``private_snapshot`` is a copy of the already strict-validated private
    ``retry_row`` the producer actually wrote under the cycle lock, so the
    caller never needs (and must not perform) a new lock-outside durable
    lookup to source exact lineage.
    """

    public_job: SimpleNamespace
    private_snapshot: dict[str, Any]


class _FingerprintContainmentFault:
    """The cycle-rows fingerprint's containment-fault marker (#1567 D1).

    Returned by :meth:`FileOrchestrationJournalRepository._containment_stat_signature`
    when a path it stats is unreachable under the hardened readers' containment
    rules (a symlinked parent component, a symlink in the slot itself, or any
    other stat fault).  It is deliberately NOT ``None`` — ``None`` is genuine
    absence for one leg and, at ``_cycle_rows``, the write-window owner's
    "no fingerprint computed" signal — and it never compares equal to a real
    ``(mtime_ns, size, inode)`` signature, so a fingerprint carrying it can
    neither match a stored entry nor be stored itself.  The recompute it forces
    reaches the existing probe fault in ``_read_cycle_segments`` and raises
    ``file_journal_unreadable`` exactly as a cold instance does; the fingerprint
    itself never raises, so the public contract frame stays where it is.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "<cycle-rows fingerprint containment fault>"


_FINGERPRINT_CONTAINMENT_FAULT = _FingerprintContainmentFault()


def _signature_has_containment_fault(value: Any) -> bool:
    """True when any leg of a (possibly nested) fingerprint carries the marker."""

    if value is _FINGERPRINT_CONTAINMENT_FAULT:
        return True
    if isinstance(value, tuple):
        return any(_signature_has_containment_fault(item) for item in value)
    return False


class _JournalProbeContainmentError(Exception):
    """A containment fault raised by an existence probe, carried to a choke frame.

    Deliberately NOT a ``FileOrchestrationJournalError`` subclass: the broad
    ``except FileOrchestrationJournalError`` handlers sitting between the
    probes and their choke frames would swallow it back into the silent empty
    result this probe exists to eliminate.
    """

    def __init__(self, *, field: str, error_type: str) -> None:
        super().__init__(field)
        self.field = field
        self.error_type = error_type


def _probe_containment_failure(error: _JournalProbeContainmentError) -> FileOrchestrationJournalError:
    """Convert a probe containment fault to the lane's reader-fault type."""

    return FileOrchestrationJournalError(
        "file_journal_unreadable",
        field=error.field,
        evidence={"error_type": error.error_type},
    )


def _submit_file_manual_retry_job(gateway: Any, request: SubmitJobRequest) -> Any:
    job_type = request.resolved_job_type()
    if job_type in _ARRAY_MANUAL_RETRY_JOB_TYPES:
        submit_job_array = getattr(gateway, "submit_job_array", None)
        if callable(submit_job_array):
            return submit_job_array(request)
    return gateway.submit_job(request)


def _file_manual_retry_array_tasks(
    retry_job: _RetrySubmissionJob,
    runtime_root_fields: Mapping[str, str] | None,
) -> list[dict[str, Any]] | None:
    filename = _ARRAY_MANUAL_RETRY_MANIFEST_INDEX_NAMES.get(str(retry_job.job_type or ""))
    if filename is None:
        return None
    run_id = str(retry_job.run_id or "")
    if not run_id or _SAFE_SEGMENT_RE.fullmatch(run_id) is None:
        return None
    workspace_dir = str((runtime_root_fields or {}).get("workspace_dir") or os.getenv("WORKSPACE_ROOT") or "")
    if not workspace_dir:
        return None
    workspace_root = Path(workspace_dir).expanduser().resolve()
    manifest_index_path = workspace_root / "runs" / run_id / "input" / filename
    try:
        payload = json.loads(
            read_bytes_limited_no_follow(
                manifest_index_path,
                max_bytes=MAX_FILE_JOURNAL_JSON_BYTES,
                containment_root=workspace_root,
            ).decode("utf-8")
        )
    except (FileNotFoundError, OSError, SafeFilesystemError, json.JSONDecodeError, ValueError):
        return None
    if isinstance(payload, list):
        tasks = payload
    elif isinstance(payload, Mapping):
        tasks = payload.get("tasks") or payload.get("manifests") or payload.get("basins")
    else:
        return None
    if not isinstance(tasks, Sequence) or isinstance(tasks, str | bytes | bytearray):
        return None
    return [dict(task) for task in tasks if isinstance(task, Mapping)]


@dataclass
class _CycleRows:
    hydro_run: dict[str, Any] | None = None
    forecast_cycle: dict[str, Any] | None = None
    forcing_version: dict[str, Any] | None = None
    model_context: dict[str, Any] | None = None
    pipeline_jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    pipeline_events: list[dict[str, Any]] = field(default_factory=list)
    replay: dict[str, Any] = field(default_factory=dict)


def _clone_cycle_rows(rows: _CycleRows) -> _CycleRows:
    return _CycleRows(
        hydro_run=dict(rows.hydro_run) if isinstance(rows.hydro_run, Mapping) else None,
        forecast_cycle=dict(rows.forecast_cycle) if isinstance(rows.forecast_cycle, Mapping) else None,
        forcing_version=dict(rows.forcing_version) if isinstance(rows.forcing_version, Mapping) else None,
        model_context=dict(rows.model_context) if isinstance(rows.model_context, Mapping) else None,
        pipeline_jobs={str(job_id): dict(job) for job_id, job in rows.pipeline_jobs.items()},
        pipeline_events=[dict(event) for event in rows.pipeline_events],
        replay=dict(rows.replay),
    )


def _filter_cycle_rows_for_model(
    rows: _CycleRows,
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
) -> None:
    scoped_forecast_cycle = _candidate_scoped_forecast_cycle(rows.forecast_cycle)
    cycle_terminated = rows.forecast_cycle is not None and scoped_forecast_cycle is None
    rows.forecast_cycle = scoped_forecast_cycle
    rows.hydro_run = (
        rows.hydro_run
        if _row_matches_candidate(rows.hydro_run, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        else None
    )
    rows.forcing_version = (
        rows.forcing_version
        if _row_matches_candidate(rows.forcing_version, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        else None
    )
    rows.model_context = (
        rows.model_context
        if _row_matches_candidate(rows.model_context, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        else None
    )
    rows.pipeline_jobs = {
        job_id: job
        for job_id, job in rows.pipeline_jobs.items()
        if _job_matches_candidate(job, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
    }
    rows.pipeline_events = [
        event
        for event in rows.pipeline_events
        if _event_matches_candidate_rows(
            event,
            source_id=source_id,
            cycle_time=cycle_time,
            pipeline_jobs=rows.pipeline_jobs,
            forecast_cycle=rows.forecast_cycle,
            cycle_terminated=cycle_terminated,
        )
    ]


@dataclass(frozen=True)
class _CycleSourceDiscovery:
    source_id: str
    source_segments: tuple[str, ...]

    @property
    def source_segment(self) -> str:
        return self.source_segments[0]


@dataclass(frozen=True)
class FileJournalRetentionMember:
    """One recognized hot authority member held for a retention transaction.

    ``sha256`` is optional only for the pre-archive inspection inventory.  The
    retention command supplies the archive manifest's digest back to
    :meth:`FileJournalRetentionCycle.remove_members`; the owner then performs a
    durable no-follow byte read before unlinking that exact named entry.
    """

    relative_path: str
    size_bytes: int
    sha256: str | None = None


@dataclass(frozen=True)
class FileJournalRetentionCycleInspection:
    """Canonical replay classification performed while a cycle flock is held."""

    status: str
    source_id: str
    cycle_time: datetime
    members: tuple[FileJournalRetentionMember, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class FileJournalRetentionDiscovery:
    """Complete candidate discovery result for the three hot journal surfaces."""

    status: str
    cycles: tuple[tuple[str, datetime], ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class FileJournalRetentionRemoval:
    """Exact hot-member unlink outcome from a lock-held retention transaction."""

    status: str
    removed_paths: tuple[str, ...] = ()
    reason: str | None = None


class FileJournalRetentionCycle:
    """One non-blocking retention lock window owned by a journal repository.

    The object is only constructed by :meth:`FileOrchestrationJournalRepository
    .open_retention_cycle`; its inspection and mutation methods retain the same
    cross-process cycle flock as journal writers for their complete lifetime.
    """

    def __init__(
        self,
        repository: FileOrchestrationJournalRepository | None,
        *,
        source_id: str,
        cycle_time: datetime,
        status: str,
        reason: str | None = None,
    ) -> None:
        self._repository = repository
        self.source_id = source_id
        self.cycle_time = cycle_time
        self.status = status
        self.reason = reason
        self._closed = False

    def inspect(self) -> FileJournalRetentionCycleInspection:
        """Replay and classify this complete hot slice while the flock is held."""

        if self.status != "locked":
            return FileJournalRetentionCycleInspection(
                status=self.status,
                source_id=self.source_id,
                cycle_time=self.cycle_time,
                reason=self.reason,
            )
        if self._closed or self._repository is None:
            return FileJournalRetentionCycleInspection(
                status="unavailable",
                source_id=self.source_id,
                cycle_time=self.cycle_time,
                reason="cycle_window_closed",
            )
        return self._repository._inspect_retention_cycle_unlocked(
            source_id=self.source_id,
            cycle_time=self.cycle_time,
        )

    def remove_members(
        self,
        members: Sequence[FileJournalRetentionMember],
    ) -> FileJournalRetentionRemoval:
        """Unlink only currently hot, manifest-bound recognized members."""

        if self.status != "locked":
            return FileJournalRetentionRemoval(status=self.status, reason=self.reason)
        if self._closed or self._repository is None:
            return FileJournalRetentionRemoval(status="unavailable", reason="cycle_window_closed")
        return self._repository._remove_retention_members_unlocked(
            source_id=self.source_id,
            cycle_time=self.cycle_time,
            members=members,
        )

    def close(self) -> None:
        self._closed = True


@dataclass(frozen=True)
class ProjectionWarning:
    """One bounded non-secret post-commit projection fault on an #1564 demotion.

    The authority journal batch is already durable when these are recorded, so
    the demotion is committed regardless.  Only the stable projection name,
    model id, and the typed error token are carried: never the exception text,
    filesystem path, or any secret-shaped detail.  ``model_id`` is ``None`` for
    the master's direct-job projection and the model id for a latest-file
    projection.
    """

    projection: str
    model_id: str | None
    error_type: str
    reason: str


@dataclass(frozen=True)
class OperatorDemoteReceipt:
    """Typed success receipt for one #1564 operator-verified demotion.

    The carried operator strings are exactly the normalized, secret-redacted
    values the durable audit event recorded (single authority: the journal's
    own ``_operator_evidence_text`` and anchor normalization).  Callers print
    the receipt, never the raw CLI arguments, so success stdout cannot leak a
    secret that the durable event already redacted.

    ``warnings`` is empty on a clean projection pass.  Any non-empty entry
    means the authority append committed but a derived direct/latest projection
    failed afterwards; the demotion is still committed and journal replay stays
    authoritative.
    """

    job_id: str
    journal_root: str
    status_from: str
    status_to: str
    reconciliation_decision: str
    submission_attempt: int
    submission_attempt_started_at: str
    checked_by: str
    checked_at: str
    verification_note: str
    written_record_count: int
    warnings: tuple[ProjectionWarning, ...] = ()


@dataclass
class _RecordBudget:
    limit: int
    field: str
    count: int = 0

    def consume(self, amount: int = 1) -> None:
        self.count += amount
        if self.count > self.limit:
            raise FileOrchestrationJournalError("file_journal_record_limit_exceeded", field=self.field)


@dataclass
class _RetentionDiscoveryBudget:
    """Aggregate entry count shared by exactly the retention hot roots."""

    limit: int
    count: int = 0

    def consume(self) -> None:
        self.count += 1
        if self.count > self.limit:
            raise FileOrchestrationJournalError(
                "file_journal_file_limit_exceeded",
                field="retention_discovery",
                evidence={"max_files": self.limit},
            )


@_install_public_read_attribution
class FileOrchestrationJournalRepository:
    # Capability marker used by the shared submit/reconcile paths.  Legacy and
    # PostgreSQL repositories deliberately keep their historical behaviour.
    supports_accepted_submit_reconcile = True
    """Read-side file implementation for scheduler orchestration state."""

    def __init__(
        self,
        journal_root: str | Path,
        *,
        max_bytes: int = MAX_FILE_JOURNAL_JSON_BYTES,
        max_files: int = MAX_FILE_JOURNAL_DISCOVERED_FILES,
        max_depth: int = MAX_FILE_JOURNAL_SCAN_DEPTH,
        max_json_nodes: int = MAX_FILE_JOURNAL_JSON_NODES,
        max_json_depth: int = MAX_FILE_JOURNAL_JSON_DEPTH,
        max_records: int = MAX_FILE_JOURNAL_RECORDS,
    ) -> None:
        self.root = Path(journal_root)
        self.max_bytes = int(max_bytes)
        self.max_files = int(max_files)
        self.max_depth = int(max_depth)
        self.max_json_nodes = int(max_json_nodes)
        self.max_json_depth = int(max_json_depth)
        self.max_records = int(max_records)
        self._write_lock = threading.Lock()
        # Identity of the thread inside a cycle write window, as
        # (thread_ident, normalized source_id, cycle_segment).  Only the
        # `_locked_cycle_write` context manager sets it; only the thread it
        # names may skip fingerprint revalidation for exactly that cycle.
        self._cycle_write_owner: tuple[int, str, str] | None = None
        # The production scheduler shares one repository instance across a
        # thread pool of per-cohort orchestrators, so every read-side cache
        # access below (lookup, store, eviction, whole-table iteration) is a
        # critical section guarded by `_cache_lock`. Lock order is one-way:
        # a thread holding `_write_lock` may take `_cache_lock`, never the
        # reverse — nothing under `_cache_lock` does IO, JSON work, or takes
        # another lock. Helpers suffixed `_cache_locked` require it held.
        self._cache_lock = threading.Lock()
        self._cycle_rows_cache: dict[
            tuple[str, str, str | None, tuple[str, ...]],
            tuple[tuple[Any, ...] | None, _CycleRows],
        ] = {}
        self._direct_jobs_cycle_cache: dict[
            tuple[str, str],
            tuple[tuple[Any, ...], list[dict[str, Any]]],
        ] = {}
        # #1734 D10: the narrowed cycle replay's memo. Its signature is scoped
        # to one cycle's own files (`_cycle_job_records_signature`), unlike
        # `_direct_jobs_cycle_cache` above, whose shared-directory stat makes
        # every write invalidate every entry.
        self._cycle_job_records_cache: dict[
            tuple[str, str, bool, tuple[str, ...]],
            tuple[tuple[Any, ...], list[dict[str, Any]]],
        ] = {}
        self._read_bytes_cache: dict[str, tuple[tuple[int, int, int], bytes, bool]] = {}
        self._read_bytes_cache_total = 0
        self._reconcile_inventory_lock_depth = 0
        self._reconcile_inventory_migration_checked = False

    def discover_retention_cycles(
        self,
        *,
        max_files: int | None = None,
        max_depth: int | None = None,
    ) -> FileJournalRetentionDiscovery:
        """Discover the complete union of the three retention-owned hot roots.

        The aggregate file budget is deliberately independent of the repository
        read budget: callers can tighten it for an operational invocation, but
        no root may consume a separate allowance and make a truncated union look
        complete.  Every path is shape-validated by the journal owner before its
        source/cycle identity is admitted.
        """

        limit_files = self.max_files if max_files is None else int(max_files)
        limit_depth = self.max_depth if max_depth is None else int(max_depth)
        if limit_files < 1 or limit_depth < 0:
            return FileJournalRetentionDiscovery(status="blocked", reason="discovery_limit_invalid")
        try:
            return self._discover_retention_cycles_unlocked(
                max_files=limit_files,
                max_depth=limit_depth,
            )
        except FileOrchestrationJournalError as error:
            return FileJournalRetentionDiscovery(status="blocked", reason=error.reason)

    @contextmanager
    def open_retention_cycle(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> Iterable[FileJournalRetentionCycle]:
        """Acquire the existing cycle flock without waiting, or return ``busy``.

        This owns the full retention transaction window.  It intentionally does
        not use the regular write context because retention must not wait behind
        a scheduler writer, while its lock order remains ``_write_lock`` then
        the existing cycle flock and never enters the inventory lock.
        """

        canonical_source = _normalize_file_source_id(source_id, field="source_id")
        normalized_cycle = _ensure_utc(cycle_time)
        if not self._write_lock.acquire(blocking=False):
            yield FileJournalRetentionCycle(
                None,
                source_id=canonical_source,
                cycle_time=normalized_cycle,
                status="busy",
                reason="in_flight",
            )
            return
        try:
            self._ensure_root_unlocked()
            with self._cycle_file_lock_unlocked(
                source_id=canonical_source,
                cycle_time=normalized_cycle,
                non_blocking=True,
            ) as acquired:
                if not acquired:
                    yield FileJournalRetentionCycle(
                        None,
                        source_id=canonical_source,
                        cycle_time=normalized_cycle,
                        status="busy",
                        reason="in_flight",
                    )
                    return
                with self._cache_lock:
                    self._cycle_rows_cache.clear()
                    self._cycle_job_records_cache.clear()
                    self._direct_jobs_cycle_cache.clear()
                window = FileJournalRetentionCycle(
                    self,
                    source_id=canonical_source,
                    cycle_time=normalized_cycle,
                    status="locked",
                )
                try:
                    yield window
                finally:
                    window.close()
                    with self._cache_lock:
                        self._cycle_rows_cache.clear()
                        self._cycle_job_records_cache.clear()
                        self._direct_jobs_cycle_cache.clear()
        finally:
            self._write_lock.release()

    def has_active_orchestration(self, *, source_id: str, cycle_time: datetime) -> bool:
        try:
            canonical_source_id = _normalize_file_source_id(source_id, field="source_id")
            rows = self._cycle_rows(source_id=canonical_source_id, cycle_time=cycle_time, model_id=None)
        except FileOrchestrationJournalError:
            return True
        return any(_job_is_active(job) for job in _current_terminal_jobs(rows.pipeline_jobs.values()))

    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        try:
            canonical_source_id = _normalize_file_source_id(source_id, field="source_id")
            rows = self._cycle_rows(source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id)
        except FileOrchestrationJournalError:
            return True
        candidate_jobs = [
            job
            for job in _current_terminal_jobs(rows.pipeline_jobs.values())
            if _job_matches_candidate(job, source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id)
        ]
        # Visibility stays WIDE (candidate_jobs still includes the foreign
        # named exact cycle-run row, keeping duplicate-submission scans broad),
        # but terminal-completion suppression authority is candidate-scoped:
        # only the candidate's own or model-less cohort completion may mark a
        # stale ACTIVE hydro placeholder as superseded.  A foreign named
        # completion is not this candidate's completion, so it cannot suppress
        # the hydro-active arm — the DB counterpart's source/cycle/model ACTIVE
        # hydro arm is a plain `UNION ALL` member with no terminal-suppression
        # clause (`chain_repository.py:57-96`) and answers True on the same
        # shape (#1472).
        has_terminal_completion = any(
            _job_is_terminal_success(job)
            and _job_is_current_terminal_completion(job)
            and not _is_foreign_model_cycle_scope_job(
                job, source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id
            )
            for job in candidate_jobs
        )
        hydro_run = rows.hydro_run
        if _row_matches_candidate(hydro_run, source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id):
            if str(hydro_run.get("status") or "") in ACTIVE_HYDRO_STATUSES and not has_terminal_completion:
                return True
        return any(_job_is_active(job) for job in candidate_jobs)

    def has_completed_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        try:
            canonical_source_id = _normalize_file_source_id(source_id, field="source_id")
            rows = self._cycle_rows(source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id)
        except FileOrchestrationJournalError:
            return False
        hydro_run = rows.hydro_run
        hydro_run_matches = _row_matches_candidate(
            hydro_run,
            source_id=canonical_source_id,
            cycle_time=cycle_time,
            model_id=model_id,
        )
        if hydro_run is not None and not hydro_run_matches:
            return False
        # This gate answers a CANDIDATE-scoped question ("has THIS candidate
        # completed"), so another model's named cycle-run row is not completion
        # evidence here even though the shared row predicate (which also feeds
        # the deliberately wider duplicate-submission gates) accepts it.  The DB
        # counterpart reads `hydro.hydro_run` under a source/cycle/model
        # three-key restriction (`chain_repository.py:98-111`) and never sees
        # another model's job rows; this conjunct aligns the journal verdict's
        # direction with it (#1302).  Model-less cohort rows stay cycle-wide.
        has_terminal_completion = any(
            _job_is_terminal_success(job)
            and _job_is_current_terminal_completion(job)
            and _job_matches_candidate(job, source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id)
            and not _is_foreign_model_cycle_scope_job(
                job, source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id
            )
            for job in _current_terminal_jobs(rows.pipeline_jobs.values())
        )
        if chain_repository_state._compute_state_save_qc_terminal_enabled():
            return has_terminal_completion
        if hydro_run_matches and str(hydro_run.get("status") or "") in COMPLETED_HYDRO_STATUSES:
            return True
        return has_terminal_completion

    def completed_pipeline_init_state_id(
        self, *, source_id: str, cycle_time: datetime, model_id: str
    ) -> str | None:
        """Return the recorded ``init_state_id`` / ``initial_state_id`` for a cycle.

        Legacy string wrapper over :meth:`completed_pipeline_init_state_identity`:
        it delegates to the full mapping and returns only the two historical
        aliases it has always recognized.  A bare ``state_id`` alias stays
        unavailable here, so §8.7 predecessor scoring and candidate quarantine
        keep their no-judgement shape for those rows.  Returns ``None`` —
        never raises — when the full accessor returns no mapping.
        """
        identity = self.completed_pipeline_init_state_identity(
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
        )
        if not isinstance(identity, Mapping):
            return None
        recorded = identity.get("init_state_id") or identity.get("initial_state_id")
        if recorded in (None, ""):
            return None
        return str(recorded).strip() or None

    def completed_pipeline_init_state_identity(
        self, *, source_id: str, cycle_time: datetime, model_id: str
    ) -> dict[str, Any] | None:
        """Return the journal's recorded per-model init-state identity, or ``None``.

        Single authority behind the completion verdict, discovery §8.7 scoring
        and candidate quarantine (design D1/D4).  Read-only companion to
        :meth:`has_completed_pipeline`, serving the same memoized ``_cycle_rows``
        latest-view rows so a completion probe plus an identity probe cost one
        cycle read, not two.  Returns a defensive copy and never writes the
        journal.  Scheduler wiring consumes it via ``getattr(repo, ..., None)``
        (repo convention), so it is intentionally absent from the
        ``ActiveCandidateRepository`` Protocol; repositories without it simply
        yield no identity judgement.

        Authority order (design D2):

        1. A matching completed ``hydro_run`` row carrying any init-state
           identity alias wins, preserving legacy per-basin semantics.
        2. Otherwise the current accepted-submit contract candidate rows of the
           internal (untruncated, public-redaction-free) pipeline-jobs view are
           scanned; the LATEST canonical-truth-order row is chosen FIRST and
           then accepted only if it is terminal-success and its normalized
           immutable evidence carries exactly one identity entry bound to this
           candidate.  An older succeeded row cannot hide a newer failed,
           empty or malformed row, because selection precedes qualification.

        Every other shape — no journal/unreadable rows, cohort master maps,
        marker-free historical/ordinary jobs, foreign-model rows, malformed
        versioned rows, malformed read-stage timestamps, latest rows that
        failed or recorded an empty/plural identity — returns ``None`` rather
        than raising.  The value is the writer-recorded identity mapping (id
        plus the optional checksum/URI/valid-time fields), never the bounded
        candidate-state projection or a public-redacted row.
        """
        try:
            canonical_source_id = _normalize_file_source_id(source_id, field="source_id")
            rows = self._cycle_rows(source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id)
        except (FileOrchestrationJournalError, TypeError, ValueError):
            # The canonical read path reports its own corruption as
            # FileOrchestrationJournalError, but the shared timestamp parser
            # may surface a bare ValueError (and the shared row predicates a
            # TypeError) for a malformed value inside an otherwise readable
            # latest view.  Every malformed input shape is a no-judgement here
            # (fails to absent): the authority never leaks read-stage
            # input-origin exceptions to the verdict or §8.7 consumers.
            return None
        hydro_run = rows.hydro_run
        if _row_matches_candidate(
            hydro_run,
            source_id=canonical_source_id,
            cycle_time=cycle_time,
            model_id=model_id,
        ) and str(hydro_run.get("status") or "") in COMPLETED_HYDRO_STATUSES:
            recorded = _init_state_identity_from_hydro(hydro_run)
            if recorded is not None:
                return recorded
        try:
            candidates = [
                job
                for job in rows.pipeline_jobs.values()
                if _job_matches_candidate(
                    job,
                    source_id=canonical_source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                )
                and accepted_submit_contract_is_current(job)
                and accepted_submit_row_kind(job) == "candidate"
            ]
            if not candidates:
                return None
            latest = max(candidates, key=_pipeline_job_truth_sort_key)
            if not _job_is_terminal_success(latest):
                return None
            identity = _candidate_row_self_bound_identity(latest, model_id=model_id)
        except (AcceptedSubmitEvidenceError, AttributeError, TypeError, ValueError):
            # One unreadable/malformed current row must not blank the journal;
            # every malformed shape is a no-judgement (fails to absent).
            return None
        return dict(identity) if identity is not None else None

    def completed_pipeline_init_state_id_occurrences(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        init_state_id: str,
    ) -> int:
        """Count FAILED quarantine-convergence attempts that re-recorded ``init_state_id``.

        The §8.7 quarantine breaker (#1157) asks "has a quarantine rerun of
        this cycle+model already completed and come back with the same stale
        token?".  The original defect run needs no counting — the caller's own
        positive mismatch witnesses it — so only reruns are counted, and only
        those the journal can PROVE were quarantine reruns:

        - the row must be a terminal cohort MASTER row
          (``accepted_submit_row_kind`` == ``"master"``); the per-model
          terminal rows that reconcile COPIES the identity onto carry distinct
          ``job_id`` values and would double-count one submission, so they are
          excluded — a submission's master row plus its per-model terminal row
          count as 1,
        - the master must be a completed convergence attempt for THIS model:
          aggregate terminal success (``succeeded``/``complete``/``published``)
          always qualifies; a ``partially_failed`` cohort master qualifies for
          the target model only when its bounded ``candidate_projections``
          (kept by the journal at at most 256 entries) contains that exact
          ``model_id`` with ``array_task_outcome == "succeeded"`` — a failed,
          missing, malformed, truncated, or duplicate projection does not
          count (fail toward liveness; if the target is not visible after the
          256-entry bound the row undercounts to zero),
        - its ``init_state_identities`` map must record the token under an
          entry naming THIS ``model_id`` (a cohort master carries one entry per
          member; another model's entry is not this candidate's lineage),
        - and its ``journal_predecessor_quarantine_rerun_model_ids`` provenance
          must list THIS ``model_id``.  Masters minted by unrelated
          whitelisted replacements — ``retry_terminal_run_manifest_missing``,
          ``retry_missing_forecast_output`` after a Slurm failure — re-record
          the same token but carry no such provenance, and must not pre-arm
          the breaker into fail-stopping the FIRST quarantine judgement.

        Read from the memoized ``_cycle_rows`` view (``pipeline_jobs`` is keyed
        by ``job_id`` and collapse-free), never from the bounded
        ``candidate_state`` payload, so ``candidate_state_job_limit``
        truncation cannot undercount.

        Returns ``0`` — never raises — when the count cannot be read (no
        journal rows, unreadable rows, empty/blank token) and for every journal
        written before #1157, whose rows carry no provenance field at all.
        Callers treat ``0`` as "breaker disengaged" (fail toward liveness: one
        more rerun beats a wrong fail-stop, and a pre-#1157 deployment simply
        keeps today's behavior until its first stamped rerun lands).

        No journal mutation and no run-manifest reads: this reports what the
        JOURNAL recorded.  Scheduler wiring consumes it via
        ``getattr(repo, ..., None)`` (repo convention, cf.
        ``scheduler_backfill_predecessor.py:226``), so it is intentionally
        absent from the ``ActiveCandidateRepository`` Protocol.
        """
        token = str(init_state_id or "").strip()
        if not token:
            return 0
        try:
            canonical_source_id = _normalize_file_source_id(source_id, field="source_id")
            rows = self._cycle_rows(source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id)
        except FileOrchestrationJournalError:
            return 0
        occurrences = 0
        for job in _current_terminal_jobs(rows.pipeline_jobs.values()):
            try:
                if not _job_is_breaker_terminal_success(job, model_id=model_id):
                    continue
                if not _job_matches_candidate(
                    job,
                    source_id=canonical_source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                ):
                    continue
                if accepted_submit_row_kind(job) != "master":
                    continue
                if model_id not in normalize_quarantine_rerun_model_ids(
                    job.get(QUARANTINE_RERUN_PROVENANCE_FIELD)
                ):
                    continue
                if _master_row_records_init_state_id(job, model_id=model_id, init_state_id=token):
                    occurrences += 1
            except (AttributeError, TypeError, ValueError):
                # One unreadable row must not blank the whole count; skipping it
                # can only undercount, which leaves the breaker disengaged.
                continue
        return occurrences

    def active_slurm_jobs(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        limit: int = DEFAULT_CANDIDATE_STATE_JOB_LIMIT,
    ) -> list[dict[str, Any]]:
        try:
            canonical_source_id = _normalize_file_source_id(source_id, field="source_id")
            rows = self._cycle_rows(source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id)
        except FileOrchestrationJournalError:
            return [
                _public_scheduler_row(
                    {
                        "job_id": "file_journal_read_blocked",
                        "cycle_id": _blocked_cycle_id(source_id, cycle_time),
                        "model_id": model_id,
                        "status": "running",
                        "stage": "file_journal_read",
                        "slurm_job_id": "unknown_after_attempt",
                    }
                )
            ]
        jobs = [
            _public_scheduler_row(job)
            for job in _current_terminal_jobs(rows.pipeline_jobs.values())
            if _file_journal_real_slurm_job_id(job.get("slurm_job_id"))
            and _job_is_active(job)
            and _job_matches_candidate(job, source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id)
        ]
        jobs.sort(key=_db_compatible_pipeline_job_order_key)
        return jobs[: max(int(limit), 1)]

    def candidate_state(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        run_id: str,
        forcing_version_id: str,
        candidate_id: str,
        retry_limit: int | None = None,
        job_limit: int = DEFAULT_CANDIDATE_STATE_JOB_LIMIT,
        event_limit: int = DEFAULT_CANDIDATE_STATE_EVENT_LIMIT,
    ) -> dict[str, Any] | None:
        try:
            canonical_source_id = _normalize_file_source_id(source_id, field="source_id")
            canonical_run_id = _canonical_candidate_run_id(
                run_id,
                source_id=canonical_source_id,
                cycle_time=cycle_time,
                model_id=model_id,
            )
            canonical_forcing_version_id = _canonical_forcing_version_id(
                forcing_version_id,
                source_id=canonical_source_id,
                cycle_time=cycle_time,
                model_id=model_id,
            )
            canonical_candidate_id = _canonical_candidate_id(
                candidate_id,
                source_id=canonical_source_id,
                cycle_time=cycle_time,
                model_id=model_id,
            )
            rows = self._cycle_rows(source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id)
        except FileOrchestrationJournalError as error:
            return _file_journal_blocked_candidate_state(
                error,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
                run_id=run_id,
                forcing_version_id=forcing_version_id,
                candidate_id=candidate_id,
                retry_limit=retry_limit,
                job_limit=job_limit,
                event_limit=event_limit,
            )
        # Evidence-size guard: cohort (model-less cycle-scope) jobs are
        # attributed to every candidate of the cycle; replicating their full
        # payloads (cohort_members, candidate_projections, completion-stage
        # copyback details) into all 18 candidate states multiplies pass
        # evidence past the 5MB limit. Compact them ONLY on this public
        # candidate-state surface — internal reads and latest-view
        # materialization must keep full fidelity.
        cycle_scope_completion_job_ids = {
            str(job.get("job_id") or "")
            for job in rows.pipeline_jobs.values()
            if _is_model_less_cycle_scope_job(job, source_id=canonical_source_id, cycle_time=cycle_time)
            and str(job.get("stage") or "") in _CYCLE_SCOPE_COMPLETION_STAGES
        }
        # Candidate-state membership mirrors the DB read path's candidate-state
        # predicate (`chain_repository_state.py:510-515`), whose cycle-run clause
        # carries `model_id IS NULL`: a row naming a FOREIGN model is not this
        # candidate's row even when its run id is the cycle run id.  Without this
        # the foreign row - and the manual retry marker riding on it - would pin
        # this candidate's derived attempt to another model's `retry_count`
        # (#1288).
        #
        # The exclusion is deliberately scoped to this projection instead of the
        # shared row predicate `_job_matches_candidate`, and the completion gate
        # carries a SECOND local application of it (`has_completed_pipeline`, #1302) for
        # the same reason: that predicate also feeds the cycle-level
        # duplicate-submission gates (`has_active_pipeline`,
        # `active_slurm_jobs`), whose DB counterparts match the cycle run id
        # UNCONDITIONALLY (`chain_repository.py:74-79` and `:177-181`), so those
        # two gates keep answering wide.
        #
        # Row visibility and suppression authority are two different axes.  The
        # duplicate-submission gates' wide ROW VISIBILITY is deliberate: an exact
        # cycle-run row of another model must keep entering `candidate_jobs` /
        # `active_slurm_jobs` so a duplicate submission is still detected.  But
        # terminal-completion SUPPRESSION AUTHORITY is candidate-scoped: a
        # foreign named exact cycle-run completion is not this candidate's
        # completion, so `has_active_pipeline`'s local terminal-completion
        # conjunction excludes it — the DB counterpart's source/cycle/model
        # ACTIVE hydro arm is a plain `UNION ALL` member with no terminal-
        # suppression clause (`chain_repository.py:57-96`), and answers True on
        # the composite shape (#1472).  `has_completed_pipeline` has no DB
        # job-row counterpart at all (`chain_repository.py:98-111` reads
        # `hydro.hydro_run` only) — but that DB gate's source/cycle/model
        # three-key restriction fixes the DIRECTION the journal side must answer
        # in, which is why the exclusion belongs there too rather than being
        # taken as licence for a wide answer.  Narrowing the shared predicate
        # itself would still loosen the duplicate-submission gates and diverge
        # from their DB counterparts.
        #
        # Rows and their `pipeline_job` events must leave in the SAME step: a
        # marker whose row is gone resolves to no job row and re-enters the attempt
        # decision through the cycle-scope entity grammar
        # (`scheduler_state_manual_retry._unresolvable_marker_entity_pins_attempt`).
        foreign_model_cycle_scope_job_ids = {
            str(job.get("job_id"))
            for job in rows.pipeline_jobs.values()
            if job.get("job_id") not in (None, "")
            and _is_foreign_model_cycle_scope_job(
                job, source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id
            )
        }
        state = chain_repository_state.candidate_state_from_rows(
            source_id=canonical_source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            run_id=canonical_run_id,
            forcing_version_id=canonical_forcing_version_id,
            candidate_id=canonical_candidate_id,
            hydro_run=rows.hydro_run,
            pipeline_jobs=[
                _public_scheduler_row(
                    _compact_cycle_scope_job(job)
                    if _is_model_less_cycle_scope_job(
                        job, source_id=canonical_source_id, cycle_time=cycle_time
                    )
                    else job
                )
                for job in rows.pipeline_jobs.values()
                if not _is_foreign_model_cycle_scope_job(
                    job, source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id
                )
            ],
            pipeline_events=[
                _public_scheduler_row(
                    _compact_cycle_scope_event(event)
                    if str(event.get("entity_id") or "") in cycle_scope_completion_job_ids
                    else event
                )
                for event in rows.pipeline_events
                if not (
                    str(event.get("entity_type") or "pipeline_job") == "pipeline_job"
                    and str(event.get("entity_id") or "") in foreign_model_cycle_scope_job_ids
                )
            ],
            forcing_version=self._candidate_state_forcing_version(
                rows.forcing_version,
                source_id=canonical_source_id,
                cycle_time=cycle_time,
                model_id=model_id,
            ),
            forecast_cycle=rows.forecast_cycle,
            retry_limit=retry_limit,
            job_limit=job_limit,
            event_limit=event_limit,
        )
        if state is None:
            return None
        run_manifest_identity = _run_manifest_model_package_identity(rows.hydro_run)
        if run_manifest_identity is not None:
            state["run_manifest_model_package"] = run_manifest_identity
        return _public_candidate_state(state)

    def _candidate_state_forcing_version(
        self,
        row: Mapping[str, Any] | None,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
    ) -> dict[str, Any] | None:
        """Resolve candidate-state forcing provenance across the journal read tiers.

        ``find_forcing_context`` already falls back to the journal direct file
        (``<root>/forcing/<source>/<cycle>/<model>.json``) when the row tier is
        empty; the candidate-state read must agree with it, otherwise the
        downstream artifact guard sees ``forcing_version: null`` for a cycle whose
        provenance IS on disk and demotes a recoverable candidate to a missing
        package (#1203).  The recovered mapping is a SHALLOW COPY carrying a
        ``forcing_version_source`` marker so an operator can tell the tiers apart;
        ``rows`` is never mutated.

        Error semantics deliberately diverge from ``find_forcing_context``: that
        read is an explicit query whose caller wants the truth, so it raises.  This
        one is a bulk per-candidate derivation, so a single corrupt direct file
        degrades to "no witness" instead of failing the whole scheduler pass.
        """

        if row is not None:
            return {**dict(row), "forcing_version_source": "journal"}
        try:
            direct = self._forcing_context(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        except FileOrchestrationJournalError:
            return None
        if direct is None:
            return None
        return {**dict(direct), "forcing_version_source": "direct"}

    def load_model_context(self, model_id: str) -> ModelContext:
        try:
            model_context = self._model_context(model_id)
            if model_context is None:
                raise OrchestratorError("MODEL_NOT_FOUND", f"model context not found in file journal: {model_id}")
            return _model_context_from_mapping(model_context, model_id=model_id)
        except FileOrchestrationJournalError as error:
            raise OrchestratorError(
                "FILE_JOURNAL_READ_BLOCKED",
                f"model context blocked by file journal state: {error.reason}",
            ) from error

    def find_forcing_context(self, *, source_id: str, cycle_time: datetime, model_id: str) -> ForcingContext:
        try:
            canonical_source_id = _normalize_file_source_id(source_id, field="source_id")
            rows = self._cycle_rows(source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id)
            if rows.forcing_version is None:
                forcing_context = self._forcing_context(
                    source_id=canonical_source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                )
            else:
                forcing_context = rows.forcing_version
            if forcing_context is None:
                return ForcingContext(None, None)
            return _forcing_context_from_mapping(forcing_context)
        except FileOrchestrationJournalError as error:
            raise OrchestratorError(
                "FILE_JOURNAL_READ_BLOCKED",
                f"forcing context blocked by file journal state: {error.reason}",
            ) from error

    def query_candidate_state(self, idempotency_key: str) -> dict[str, Any] | None:
        # Derivation runs OUTSIDE the blocked-row handler on purpose: an
        # underivable key must fall open to the whole-tree scan, not be
        # reported as a blocked read.
        cycle_scope = _cycle_scope_from_idempotency_key(idempotency_key)
        try:
            for job in self._iter_pipeline_job_records_scoped(cycle_scope):
                if str(job.get("idempotency_key") or "") == idempotency_key:
                    return _public_scheduler_row(job)
        except FileOrchestrationJournalError as error:
            return _blocked_query_job(error, idempotency_key=idempotency_key)
        return None

    def _candidate_job_for_idempotency_unlocked(self, idempotency_key: str) -> dict[str, Any] | None:
        cycle_scope = _cycle_scope_from_idempotency_key(idempotency_key)
        for job in self._iter_pipeline_job_records_scoped(cycle_scope):
            if str(job.get("idempotency_key") or "") == idempotency_key:
                return dict(job)
        return None

    def get_pipeline_job(self, job_id: str) -> dict[str, Any] | None:
        try:
            job = self._pipeline_job_for_id_unlocked(job_id)
            if job is not None:
                return _public_scheduler_row(job)
        except FileOrchestrationJournalError as error:
            return _blocked_query_job(error, job_id=job_id)
        return None

    def _pipeline_job_for_id_unlocked(self, job_id: str) -> dict[str, Any] | None:
        expected_job_id = _safe_segment(job_id)
        direct_job = self._direct_pipeline_job_record(expected_job_id)
        if direct_job is not None:
            source_id = _source_id_from_job(direct_job)
            cycle_time = _cycle_time_from_job(direct_job)
            model_id = _optional_safe_identity(direct_job, "model_id")
            if model_id is not None:
                rows = self._cycle_rows(
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                )
            else:
                rows = _CycleRows()
                for record in self._cycle_journal_records(source_id=source_id, cycle_time=cycle_time):
                    self._apply_journal_record(
                        rows,
                        record,
                        source_id=source_id,
                        cycle_time=cycle_time,
                    )
            replayed = rows.pipeline_jobs.get(expected_job_id)
            return dict(replayed or direct_job)
        # D1a: the job id carries (source, cycle) for both live shapes, so the
        # direct-miss fallback is narrowed too. include_direct stays False so
        # the replay never re-counts the direct record probed above.
        cycle_scope = _cycle_scope_from_job_id(expected_job_id)
        for job in self._iter_pipeline_job_records_scoped(cycle_scope, include_direct=False):
            if str(job.get("job_id") or "") == expected_job_id:
                return dict(job)
        return None

    def get_accepted_submit_pipeline_job(self, pipeline_job_id: str) -> dict[str, Any] | None:
        """Read one deterministic cohort master without global history discovery."""

        source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(pipeline_job_id)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            row = self._accepted_submit_job_for_id_unlocked(
                pipeline_job_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            return _public_scheduler_row(row) if row is not None else None

    def _accepted_submit_job_for_id_unlocked(
        self,
        pipeline_job_id: str,
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> dict[str, Any] | None:
        """Resolve exact direct plus the one cycle journal; never latest/global."""

        expected_job_id = _safe_segment(pipeline_job_id)
        canonical_source = _normalize_file_source_id(source_id, field="source_id")
        direct = self._direct_pipeline_job_record(expected_job_id)
        rows = _CycleRows()
        for record in self._cycle_journal_records(source_id=canonical_source, cycle_time=cycle_time):
            self._apply_journal_record(
                rows,
                record,
                source_id=canonical_source,
                cycle_time=cycle_time,
            )
        replayed = rows.pipeline_jobs.get(expected_job_id)
        row = dict(replayed or direct) if replayed is not None or direct is not None else None
        if row is None:
            return None
        if (
            _source_id_from_job(row) != canonical_source
            or _cycle_time_from_job(row) != cycle_time.astimezone(UTC)
        ):
            raise FileOrchestrationJournalError(
                "file_journal_identity_mismatch",
                field="job_id",
            )
        return row

    def query_pipeline_jobs_by_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
        cycle_scope = _cycle_scope_from_cycle_id(cycle_id)
        try:
            jobs = [
                _public_scheduler_row(job)
                for job in self._iter_pipeline_job_records_scoped(cycle_scope)
                if str(job.get("cycle_id") or "") == cycle_id
            ]
            jobs.sort(key=_db_compatible_pipeline_job_order_key)
            return jobs
        except FileOrchestrationJournalError as error:
            return [_blocked_query_job(error, cycle_id=cycle_id)]

    def query_pipeline_jobs_by_run(self, run_id: str) -> list[dict[str, Any]]:
        cycle_scope = _cycle_scope_from_file_run_id(run_id)
        try:
            jobs = [
                _public_scheduler_row(job)
                for job in self._iter_pipeline_job_records_scoped(cycle_scope)
                if str(job.get("run_id") or "") == run_id
            ]
            jobs.sort(key=_db_compatible_pipeline_job_order_key)
            return jobs
        except FileOrchestrationJournalError as error:
            return [_blocked_query_job(error, run_id=run_id)]

    def query_pipeline_job_by_slurm_id(self, slurm_job_id: str) -> dict[str, Any] | None:
        try:
            for job in self._iter_pipeline_job_records():
                if str(job.get("slurm_job_id") or "") == slurm_job_id:
                    return _public_scheduler_row(job)
        except FileOrchestrationJournalError as error:
            return _blocked_query_job(error, slurm_job_id=slurm_job_id)
        return None

    def query_reserved_unbound_jobs(self) -> list[SimpleNamespace]:
        jobs = [
            _file_reconcile_namespace(job)
            for job in self._iter_reconcile_pipeline_job_records()
            if str(job.get("status") or "") == "reserved"
            and job.get("slurm_job_id") in (None, "")
            and job.get("idempotency_key") not in (None, "")
        ]
        jobs.sort(key=lambda job: (_datetime_sort_key(job.created_at), str(job.job_id)))
        return jobs

    def query_released_identity_blocked_jobs(self) -> list[dict[str, Any]]:
        """Enumerate the released identity-blocked wedge for an OPERATOR (#1748).

        Read-only, and deliberately consumed by nothing automatic: reconcile
        iterates ``query_reserved_unbound_jobs``, which never yields this shape,
        and that is the whole reason the shape is a permanent terminal.  This
        exists so ``IDENTITY_RELEASED_RESERVATION_NEEDS_OPERATOR`` is
        ACTIONABLE -- the recovery API demands CAS values a human otherwise has
        no supported way to read.  The filter mirrors that API's own admission
        shape so a row listed here is one it will actually consider.

        The constraint that binds is NOT wall time (#1810).  An earlier version
        of this docstring defended a whole-tree replay as "acceptable because
        this is an operator command run by hand on a wedge"; what actually
        fired on node-22 was the fail-closed aggregate ``_RecordBudget`` the
        replay opens, because ``journal/`` is append-only history that had
        already crossed ``MAX_FILE_JOURNAL_RECORDS``.  No amount of operator
        patience gets past a budget, and raising it only moves the cliff.  So
        the read cost here is bounded by ONE cycle's records instead:

        1. Enumerate the flat ``pipeline-jobs/`` directory ONCE as a CANDIDATE
           filter.  That surface is guarded by ``max_files``, not by a record
           budget, and every row in this shape is on it by construction --
           ``_write_pipeline_job_direct_unlocked`` sends a current-contract
           *candidate* to ``by-cycle/`` and everything else, cohort masters
           included, to the flat file, and no pruning path unlinks it.
        2. Confirm through the cycle-scoped replay, which is what the returned
           row is built from.  The flat record is not treated as authoritative:
           a stale one can only produce a candidate that confirmation drops.
           Candidates are GROUPED by cycle first, so the cost is one replay per
           distinct cycle rather than one per candidate (#1810 design D14).

        The reconcile inventory is NOT usable as the enumeration surface: it
        prunes its anchor the moment a row stops needing restart reconcile,
        which is exactly what the release does, so a released row is by
        definition absent from it.

        The listing is point-in-time on a live journal, exactly as the
        whole-tree replay was.  That is safe because the recovery action
        re-reads under ``_locked_cycle_write`` and compares there, so a stale
        listing costs at most a refused invocation, never a bad write.
        """

        candidates: dict[str, dict[str, Any]] = {}
        # Eagerly consumed inside the lane, never across a ``yield`` of this
        # frame: #1734 D11's rule for tagging a generator's reads.
        with journal_read_lane("direct_flat_scan"):
            for job in self._iter_direct_pipeline_job_records():
                if _released_identity_blocked_row(job):
                    candidates[_required_safe_identity(job, "job_id")] = job

        confirmed: dict[str, dict[str, Any]] = {}
        unscoped: set[str] = set()
        scoped: dict[tuple[str, datetime], set[str]] = {}
        for job_id, candidate in candidates.items():
            cycle_scope = _released_candidate_cycle_scope(candidate)
            if cycle_scope is None:
                unscoped.add(job_id)
                continue
            scoped.setdefault(cycle_scope, set()).add(job_id)
        # Confirm ONCE PER CYCLE, not once per candidate, and list the flat
        # directory once for the whole call (#1810 design D14). Candidate count
        # is unbounded by construction -- the admitted shape is what a mass
        # release puts many rows into at once -- so a per-candidate replay of a
        # 4,557-file listing is D9's rejected growth law, re-entered.
        with _flat_direct_job_listing_memo_scope():
            for cycle_scope, scope_job_ids in scoped.items():
                for job in self._iter_pipeline_job_records_scoped(cycle_scope):
                    job_id = str(job.get("job_id") or "")
                    if job_id in scope_job_ids and _released_identity_blocked_row(job):
                        confirmed[job_id] = job
        if unscoped:
            # #1734 D4, kept verbatim: an underivable scope costs the old
            # expensive read, never a false "not found".  ONE pass serves every
            # fallen-open candidate -- each call re-replays the whole tree.
            for job in self._iter_pipeline_job_records():
                job_id = str(job.get("job_id") or "")
                if job_id in unscoped and _released_identity_blocked_row(job):
                    confirmed[job_id] = job

        jobs = [_public_scheduler_row(job) for job in confirmed.values()]
        jobs.sort(key=lambda job: str(job.get("job_id") or ""))
        return jobs

    def query_inflight_jobs(self) -> list[SimpleNamespace]:
        jobs = [
            _file_reconcile_namespace(job)
            for job in self._iter_reconcile_pipeline_job_records()
            if _job_needs_restart_reconcile(job)
            and _file_journal_real_slurm_job_id(job.get("slurm_job_id"))
        ]
        jobs.sort(
            key=lambda job: (
                _datetime_sort_key(job.submitted_at),
                _datetime_sort_key(job.created_at),
                str(job.job_id),
            )
        )
        return jobs

    def query_rollback_unsettled_jobs(self) -> list[SimpleNamespace]:
        """Read bounded current authority and allow only explicitly settled jobs."""

        authority_roots = {
            name: self.root / name
            for name in (
                _RECONCILE_INVENTORY_DIRECTORY,
                "journal",
                "latest",
                "pipeline-jobs",
                _LEGACY_ACTIVE_RECONCILE_DIRECTORY,
            )
        }
        authority_signatures = {
            name: _stat_signature(path) for name, path in authority_roots.items()
        }
        records: dict[str, dict[str, Any]] = {}
        try:
            for job in self._iter_reconcile_inventory_records(
                quiescence=True,
                strict_disappearance=True,
            ):
                _upsert_by_key(records, job, key="job_id")
            for job in self._iter_rollback_scope_pipeline_job_records(
                authority_signatures=authority_signatures,
            ):
                if _job_blocks_rollback_quiescence(job):
                    _upsert_by_key(records, job, key="job_id")
        except FileOrchestrationJournalError:
            self._require_rollback_authority_roots_unchanged(
                authority_roots,
                authority_signatures,
            )
            raise
        self._require_rollback_authority_roots_unchanged(
            authority_roots,
            authority_signatures,
        )
        jobs = [_file_reconcile_namespace(job) for job in records.values()]
        jobs.sort(
            key=lambda job: (
                _datetime_sort_key(getattr(job, "submitted_at", None)),
                _datetime_sort_key(getattr(job, "created_at", None)),
                str(job.job_id),
            )
        )
        return jobs

    def _require_rollback_authority_roots_unchanged(
        self,
        authority_roots: Mapping[str, Path],
        authority_signatures: Mapping[str, tuple[int, int, int] | None],
    ) -> None:
        for name, path in authority_roots.items():
            if _stat_signature(path) != authority_signatures[name]:
                raise FileOrchestrationJournalError(
                    "file_journal_quiescence_authority_changed",
                    field=name.replace("-", "_"),
                )

    def _iter_rollback_scope_pipeline_job_records(
        self,
        *,
        authority_signatures: Mapping[str, tuple[int, int, int] | None],
    ) -> Iterable[dict[str, Any]]:
        """Discover old-writer rows created after the rollback fence, not history."""

        fence = self._read_optional_json(
            self.root / _RECONCILE_INVENTORY_ROLLBACK_PREP_RECEIPT
        )
        if fence is None:
            raise FileOrchestrationJournalError(
                "file_journal_quiescence_authority_changed",
                field="reconcile_inventory_rollback_receipt",
            )
        prepared = self._validated_reconcile_inventory_rollback_receipt(fence)
        prepared_at = _coerce_datetime(prepared["prepared_at"], field="prepared_at").timestamp()
        budget = _RecordBudget(max(self.max_records, 1), "rollback_scope_records")
        records: dict[str, dict[str, Any]] = {}
        selected_signatures: dict[Path, Any] = {}

        def changed_since_prepare(path: Path) -> bool:
            before = _stat_signature(path)
            try:
                metadata = stat_no_follow(path, containment_root=self.root)
            except (FileNotFoundError, OSError, SafeFilesystemError) as error:
                raise FileOrchestrationJournalError(
                    "file_journal_quiescence_authority_changed",
                    field=str(_relative_evidence(path, self.root)),
                ) from error
            if before != _stat_signature(path):
                raise FileOrchestrationJournalError(
                    "file_journal_quiescence_authority_changed",
                    field=str(_relative_evidence(path, self.root)),
                )
            changed = metadata.st_mtime >= prepared_at
            if changed:
                selected_signatures[path] = before
            return changed

        for path in self._iter_reconcile_direct_pipeline_job_paths(
            strict_disappearance=True,
            expected_root_signature=authority_signatures["pipeline-jobs"],
        ):
            if not changed_since_prepare(path):
                continue
            payload = self._read_optional_json(path)
            if payload is None or _stat_signature(path) != selected_signatures[path]:
                raise FileOrchestrationJournalError(
                    "file_journal_quiescence_authority_changed",
                    field="pipeline_jobs",
                )
            budget.consume()
            job = self._validated_direct_pipeline_job_record(
                payload,
                expected_job_id=_safe_segment(path.stem),
            )
            _upsert_by_key(records, job, key="job_id")

        for path in self._iter_migration_journal_paths(
            expected_root_signature=authority_signatures["journal"],
        ):
            if not changed_since_prepare(path):
                continue
            source_id, cycle_time = _journal_identity_from_path(
                path, root=self.root, surface="journal"
            )
            rows = _CycleRows()
            for record in self._read_migration_jsonl(path):
                budget.consume()
                self._apply_journal_record(
                    rows,
                    record,
                    source_id=source_id,
                    cycle_time=cycle_time,
                )
            if _stat_signature(path) != selected_signatures[path]:
                raise FileOrchestrationJournalError(
                    "file_journal_quiescence_authority_changed",
                    field="journal",
                )
            for job in rows.pipeline_jobs.values():
                _upsert_by_key(records, job, key="job_id")

        latest_paths = sorted(
            _iter_discovered_files(
                self.root / "latest",
                root=self.root,
                suffix=".json",
                recursive=True,
                max_files=self.max_files,
                max_depth=self.max_depth,
                strict_disappearance=True,
                expected_root_signature=authority_signatures["latest"],
            )
        )
        for path in latest_paths:
            if not changed_since_prepare(path):
                continue
            payload = self._read_optional_json(path)
            if payload is None:
                raise FileOrchestrationJournalError(
                    "file_journal_quiescence_authority_changed",
                    field="latest",
                )
            source_id, cycle_time, model_id = _latest_identity_from_path(path, root=self.root)
            rows = _CycleRows()
            self._apply_latest_view(
                rows,
                payload,
                source_id=source_id,
                cycle_time=cycle_time,
                expected_model_id=model_id,
            )
            if _stat_signature(path) != selected_signatures[path]:
                raise FileOrchestrationJournalError(
                    "file_journal_quiescence_authority_changed",
                    field="latest",
                )
            for job in rows.pipeline_jobs.values():
                budget.consume()
                _upsert_by_key(records, job, key="job_id")

        for job in self._iter_legacy_active_reconcile_records(
            expected_root_signature=authority_signatures[_LEGACY_ACTIVE_RECONCILE_DIRECTORY],
        ):
            budget.consume()
            _upsert_by_key(records, job, key="job_id")
        yield from records.values()

    def bind_reservation(
        self,
        idempotency_key: str,
        *,
        slurm_job_id: str,
        status: str = "submitted",
        array_task_id: int | None = None,
    ) -> SimpleNamespace | None:
        bound = self.bind_pipeline_job_reservation(
            idempotency_key,
            slurm_job_id=slurm_job_id,
            status=status,
            array_task_id=array_task_id,
        )
        return _file_reconcile_namespace(bound) if bound is not None else None

    def update_job_status(
        self,
        job_id: str,
        status: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> SimpleNamespace:
        _previous_status, updated = self.update_pipeline_job_status(
            job_id,
            status,
            error_code=error_code,
            error_message=error_message,
        )
        return _file_reconcile_namespace(updated)

    @property
    def supports_writes(self) -> bool:
        return True

    def ensure_forecast_cycle(self, *, source_id: str, cycle_time: datetime) -> dict[str, Any]:
        source_id = _normalize_file_source_id(source_id, field="source_id")
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=None).forecast_cycle
            if existing is not None:
                row = dict(existing)
                changed = False
                for key, value in (
                    ("cycle_id", _cycle_id_for_file_source(source_id, cycle_time)),
                    ("source_id", source_id),
                    ("cycle_time", _format_utc(cycle_time)),
                    ("issue_time", _format_utc(cycle_time)),
                ):
                    if row.get(key) in (None, ""):
                        row[key] = value
                        changed = True
                if not changed:
                    return _public_scheduler_row(row)
                row["updated_at"] = _format_utc(_utcnow())
                self._append_validated_record_unlocked(
                    "forecast_cycle",
                    row,
                    source_id=source_id,
                    cycle_time=cycle_time,
                )
                return _public_scheduler_row(row)
            row = {
                "cycle_id": _cycle_id_for_file_source(source_id, cycle_time),
                "source_id": source_id,
                "cycle_time": _format_utc(cycle_time),
                "issue_time": _format_utc(cycle_time),
                "status": "discovered",
                "created_at": _format_utc(_utcnow()),
                "updated_at": _format_utc(_utcnow()),
            }
            self._append_validated_record_unlocked(
                "forecast_cycle",
                row,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            return _public_scheduler_row(row)

    def append_historical_forecast_cycle(self, record: Mapping[str, Any]) -> dict[str, Any] | None:
        source_id = _required_source_id(record, "source_id")
        cycle_time = _parse_cycle_time_field(record, "cycle_time")
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=None).forecast_cycle
            if existing is not None:
                return _public_scheduler_row(existing)
            self._append_validated_record_unlocked(
                "forecast_cycle",
                record,
                source_id=source_id,
                cycle_time=cycle_time,
            )
        return _public_scheduler_row(record)

    def create_hydro_run(self, context: Any, manifest: dict[str, Any]) -> dict[str, Any]:
        init_state = manifest.get("initial_state") if isinstance(manifest.get("initial_state"), Mapping) else {}
        model = manifest.get("model") if isinstance(manifest.get("model"), Mapping) else {}
        row = {
            "run_id": str(context.run_id),
            "candidate_id": manifest.get("candidate_id"),
            "run_type": manifest.get("run_type", "forecast"),
            "scenario_id": manifest["scenario_id"],
            "model_id": str(context.model_id),
            "basin_id": model.get("basin_id") or getattr(context, "basin_id", None),
            "array_task_id": manifest.get("array_task_id", getattr(context, "array_task_id", None)),
            "basin_version_id": str(context.basin_version_id),
            "forcing_version_id": str(context.forcing_version_id),
            "init_state_id": getattr(context, "init_state_id", None) or init_state.get("state_id"),
            # #1164: the initial-state decision must be auditable from the journal
            # row itself, not only from the run manifest object.  A packaged-IC
            # bootstrap has no ``init_state_id`` at all, so absence of an id is no
            # longer sufficient to conclude "cold start".
            "quality": init_state.get("quality"),
            "source_id": _normalize_file_source_id(context.source_id, field="source_id"),
            "cycle_time": _format_utc(context.cycle_time),
            "start_time": _format_utc(context.start_time),
            "end_time": _format_utc(context.end_time),
            "status": "created",
            "submission_attempt": max(int(manifest.get("submission_attempt") or 1), 1),
            "run_manifest_uri": context.run_manifest_uri,
            "output_uri": context.output_uri,
            "log_uri": context.log_uri,
            "created_at": _format_utc(_utcnow()),
            "updated_at": _format_utc(_utcnow()),
        }
        return self._write_hydro_run(row, retriable_only=True)

    def create_hydro_run_from_basin(self, basin: Mapping[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
        model = _mapping_value(manifest, "model")
        forcing = _optional_mapping_value(manifest, "forcing")
        outputs = _optional_mapping_value(manifest, "outputs")
        initial_state = _optional_mapping_value(manifest, "initial_state")
        cycle_time = parse_cycle_time(str(manifest["cycle_time"]))
        row = {
            "run_id": str(manifest["run_id"]),
            "candidate_id": manifest.get("candidate_id"),
            "run_type": manifest.get("run_type", "forecast"),
            "scenario_id": manifest["scenario_id"],
            "model_id": str(model["model_id"]),
            "basin_id": model.get("basin_id") or basin.get("basin_id"),
            "array_task_id": basin.get("task_id"),
            "basin_version_id": str(model["basin_version_id"]),
            "forcing_version_id": forcing.get("forcing_version_id"),
            "init_state_id": initial_state.get("state_id") or basin.get("init_state_id"),
            # #1164: see ``create_hydro_run`` — the quality face travels with the
            # row so a packaged-IC bootstrap is distinguishable from a cold start.
            "quality": initial_state.get("quality") or basin.get("init_state_quality"),
            "source_id": _normalize_file_source_id(
                manifest.get("source_id") or basin.get("source_id"),
                field="source_id",
            ),
            "cycle_time": _format_utc(cycle_time),
            "start_time": _format_utc(_coerce_datetime(manifest["start_time"], field="start_time")),
            "end_time": _format_utc(_coerce_datetime(manifest["end_time"], field="end_time")),
            "status": "created",
            "submission_attempt": max(int(manifest.get("submission_attempt") or 1), 1),
            "run_manifest_uri": outputs.get("run_manifest_uri"),
            "output_uri": outputs.get("output_uri"),
            "log_uri": outputs.get("log_uri"),
            "created_at": _format_utc(_utcnow()),
            "updated_at": _format_utc(_utcnow()),
        }
        try:
            return self._write_hydro_run(row, retriable_only=True)
        except OrchestratorError as error:
            if error.error_code != "HYDRO_RUN_NOT_RETRIABLE":
                raise
            existing = self._hydro_run_for(row["run_id"])
            if existing is None:
                raise OrchestratorError(
                    "HYDRO_RUN_NOT_FOUND",
                    f"hydro_run not found after conflict: {row['run_id']}",
                ) from error
            return _public_scheduler_row(existing)

    def append_historical_hydro_run(self, record: Mapping[str, Any]) -> dict[str, Any] | None:
        source_id = _required_source_id(record, "source_id")
        cycle_time = _parse_cycle_time_field(record, "cycle_time")
        model_id = _required_safe_identity(record, "model_id")
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._hydro_run_for(str(record["run_id"]))
            if existing is not None:
                return _public_scheduler_row(existing)
            self._append_validated_record_unlocked(
                "hydro_run",
                record,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
                materialize_model_id=model_id,
            )
        return _public_scheduler_row(record)

    def forecast_cohort_runtime_identity_matches(self, identity: Mapping[str, Any]) -> bool:
        """Validate accepted-submit members against independently written run manifests."""
        members = ordered_cohort_members(identity.get("cohort_members"))
        if not members:
            return False
        try:
            source_id = _normalize_file_source_id(identity.get("source_id"), field="source_id")
            cycle_id = _required_safe_identity(identity, "cycle_id")
            cycle_time = parse_cycle_time(cycle_id.split("_", maxsplit=1)[1])
            expected_cycle_time = _format_utc(cycle_time)
            model_ids = [str(member.get("model_id") or "") for member in members]
            if any(not model_id for model_id in model_ids):
                return False
            with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
                rows_by_model = self._cycle_rows_by_model_unlocked(
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_ids=model_ids,
                    include_direct_jobs=False,
                )
            for member in members:
                model_rows = rows_by_model.get(str(member.get("model_id") or ""))
                hydro_run = model_rows.hydro_run if model_rows is not None else None
                if hydro_run is None:
                    return False
                for field in ("run_id", "model_id", "scenario_id"):
                    if str(hydro_run.get(field) or "") != str(member.get(field) or ""):
                        return False
                # Correction (#1749): the original rationale here claimed the
                # file-journal per-model writers *never* persist
                # ``candidate_id``/``basin_id``/``array_task_id``. That claim
                # was false — ``create_hydro_run_from_basin`` (called from
                # ``chain_manifests.py:386``, the array-shaped cohort path)
                # persists all three, and every sampled production row carries
                # non-null values.
                #
                # The surviving true reason for tolerating ``None``: *some*
                # writer paths do not persist these fields (``create_hydro_run``
                # with a manifest/run context that carries none of them), so an
                # absent value still means "not stored", not "different job" —
                # same semantics as the sacct comment gate in ``reconcile``. A
                # present-but-different value stays fatal.
                #
                # ``array_task_id`` is no longer compared at all: it is the
                # index a member occupied in one array submission (a
                # per-submission layout artefact, frozen on the row that wrote
                # it), not an identity. A member-set change renumbers the array
                # and every surviving row goes stale, which failed the whole
                # cohort on layout churn alone.
                #
                # ``submission_attempt`` is likewise not compared (#1792): a
                # successful per-model ``hydro_run`` row is frozen at the
                # attempt that wrote it, while reclaim advances the
                # accepted-submit master to a new attempt. The attempt number
                # stays persisted on both rows as lineage evidence, but it is
                # not cross-submission equality identity.
                for field in ("candidate_id", "basin_id"):
                    observed = hydro_run.get(field)
                    if observed is not None and str(observed) != str(member.get(field) or ""):
                        return False
                if (
                    _normalize_file_source_id(hydro_run.get("source_id"), field="source_id") != source_id
                    or _format_utc(_parse_cycle_time_field(hydro_run, "cycle_time")) != expected_cycle_time
                ):
                    return False
        except (FileOrchestrationJournalError, IndexError, TypeError, ValueError):
            return False
        return True

    def update_hydro_run_status(
        self,
        run_id: str,
        status: str,
        *,
        slurm_job_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        try:
            source_id, cycle_time = _source_cycle_from_file_run_id(run_id)
        except FileOrchestrationJournalError as error:
            raise OrchestratorError("HYDRO_RUN_NOT_FOUND", f"hydro_run not found: {run_id}") from error
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._hydro_run_for(run_id)
            if existing is None:
                raise OrchestratorError("HYDRO_RUN_NOT_FOUND", f"hydro_run not found: {run_id}")
            # The base row is the exact durable read (``_hydro_run_for``), so a
            # non-clearing update that omits the error family keeps the durable
            # value; only the clearing set writes the provided values, including
            # ``None``.  Caller-supplied placeholders would be stripped by the
            # write boundary, so they are resolved against the durable base the
            # same way as the pipeline-job legs: a withheld value is not an
            # overwrite instruction.
            row = dict(existing)
            safe_error_message = _durable_error_message(
                _resolved_caller_evidence(error_message, durable=existing.get("error_message"))
            )
            row.update({"status": status, "updated_at": _format_utc(_utcnow())})
            for key, value in (("slurm_job_id", slurm_job_id),):
                if value is not None:
                    row[key] = value
            resolved_error_code = _resolved_caller_evidence(error_code, durable=existing.get("error_code"))
            if status in {"pending", "created", "succeeded", "complete", "parsed", "published"}:
                row["error_code"] = resolved_error_code
                row["error_message"] = safe_error_message
            else:
                if resolved_error_code is not None:
                    row["error_code"] = resolved_error_code
                if safe_error_message is not None:
                    row["error_message"] = safe_error_message
            model_id = _required_safe_identity(row, "model_id")
            self._append_validated_record_unlocked(
                "hydro_run",
                row,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
                materialize_model_id=model_id,
            )
            return _public_scheduler_row(row)

    def upsert_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any]:
        # #1748: the operator-recovery attestation is settable ONLY by the typed
        # recovery API.  The merge whitelist below already keeps this generic
        # path from changing it on an existing row; this guard closes the other
        # half -- forging it on a row this call creates.
        if record.get(OPERATOR_RECOVERY_ATTESTATION_FIELD) not in (None, ""):
            raise FileOrchestrationJournalError(
                "file_journal_authority_transition_requires_typed_api",
                field=OPERATOR_RECOVERY_ATTESTATION_FIELD,
            )
        # Writer-authority closed world (#1564 D8): the operator decision is
        # durable provenance granted only by the dedicated typed demotion,
        # never by an ordinary pipeline-job upsert -- including a marker-free
        # legacy master upgraded to the current contract in this one merge,
        # whose explicit raw token no other accepted-submit writer gate would
        # see.  Refuse before any row construction, lock, mutation, or event.
        if record.get("reconciliation_decision") == OPERATOR_VERIFIED_ABSENCE_DECISION:
            raise FileOrchestrationJournalError(
                "file_journal_authority_transition_requires_typed_api",
                field="reconciliation_decision",
            )
        # Upserts may be partial status transitions for an existing cohort.
        # Validate the accepted-submit contract only after merging with its
        # canonical durable identity; fresh rows still validate in full.
        row = self._pipeline_job_row(record, validate_accepted_submit=False)
        source_id = _source_id_from_job(row)
        cycle_time = _cycle_time_from_job(row)
        model_id = _optional_safe_identity(row, "model_id")
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._pipeline_job_for_id_unlocked(str(row["job_id"]))
            persisted_master_identity: dict[str, Any] | None = None
            persisted_master_state: dict[str, Any] | None = None
            persisted_candidate_evidence: dict[str, Any] | None = None
            if existing is None:
                _validate_accepted_submit_evidence(row)
                if accepted_submit_contract_is_current(row) and accepted_submit_row_kind(row) == "master":
                    raise FileOrchestrationJournalError(
                        "file_journal_authority_transition_requires_typed_api",
                        field="upsert_pipeline_job",
                    )
            if existing is not None:
                explicit_fields = set(record)
                incoming = row
                _validate_accepted_submit_evidence(existing)
                if accepted_submit_contract_is_current(
                    existing
                ) and accepted_submit_master_identity_is_structural(existing):
                    persisted_master_identity = _accepted_submit_master_identity(existing)
                    persisted_master_state = _accepted_submit_master_state(existing)
                    for field, persisted_value in persisted_master_identity.items():
                        if field not in explicit_fields:
                            continue
                        incoming_value = incoming.get(field)
                        if field == "source_id":
                            try:
                                incoming_value = normalize_source_id(str(record.get(field) or ""))
                            except ValueError as error:
                                raise FileOrchestrationJournalError(
                                    "file_journal_evidence_invariant_invalid",
                                    field=field,
                                ) from error
                        elif field == "cycle_time":
                            incoming_value = _optional_format_datetime(
                                record.get(field), field=field
                            )
                        elif type(persisted_value) is bool and type(record.get(field)) is not bool:
                            raise FileOrchestrationJournalError(
                                "file_journal_evidence_type_invalid",
                                field=field,
                            )
                        if incoming_value != persisted_value:
                            raise FileOrchestrationJournalError(
                                "file_journal_evidence_invariant_invalid",
                                field=field,
                            )
                elif accepted_submit_contract_is_current(
                    existing
                ) and accepted_submit_row_kind(existing) == "candidate":
                    # Derived per-model rows freeze their lineage evidence the
                    # same way the master freezes its map (#1187). The contract
                    # marker is required for the same reason it is on the master
                    # arm: normalization only owns this field on contract-current
                    # rows, so marker-free historical per-model rows must keep
                    # their pre-change behaviour.
                    persisted_candidate_evidence = _accepted_submit_candidate_evidence(existing)
                row = dict(existing)
                # #1589 (design D3/D8): the merge is an unconditional write of
                # every explicitly carried field, so a caller that round-trips a
                # public row hands in ``[object-uri]`` for anything the display
                # projection redacted.  Resolving against the persisted value
                # keeps the real URI instead of letting the write boundary erase
                # it.  Frozen master evidence is unaffected: a mapping/list
                # value never resolves (only a whole-value placeholder does), so
                # a divergent ``init_state_identities`` map still REACHES the
                # #1183/#1187 frozen check and is still loudly rejected.
                for key in _PIPELINE_JOB_UPSERT_MUTABLE_FIELDS:
                    if key in explicit_fields:
                        row[key] = _resolved_caller_evidence(
                            incoming[key], durable=existing.get(key)
                        )
                row["updated_at"] = _format_utc(_utcnow())
                model_id = _optional_safe_identity(row, "model_id")
            _validate_accepted_submit_evidence(row)
            if persisted_candidate_evidence is not None:
                # Compared AFTER the merge on purpose: the merge loop copies
                # only explicitly carried keys, so an upsert that omits the
                # field compares the persisted value against itself and keeps
                # it silently, exactly as before this gate existed. Comparing
                # the incoming row instead would pit the closed constructor's
                # unconditional default (an empty map) against a populated
                # persisted one and fail every ordinary per-model upsert.
                merged_candidate_evidence = _accepted_submit_candidate_evidence(row)
                for field, persisted_value in persisted_candidate_evidence.items():
                    if merged_candidate_evidence[field] != persisted_value:
                        raise FileOrchestrationJournalError(
                            "file_journal_evidence_invariant_invalid",
                            field=field,
                        )
            if persisted_master_state is not None:
                merged_master_state = _accepted_submit_master_state(row)
                for field, persisted_value in persisted_master_state.items():
                    if merged_master_state[field] != persisted_value:
                        raise FileOrchestrationJournalError(
                            "file_journal_evidence_invariant_invalid",
                            field=field,
                        )
                # An exact ordinary replay is a read, not an authority event:
                # do not append another journal record or advance updated_at.
                return _public_scheduler_row(existing)
            return self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=model_id)

    def append_historical_pipeline_job(self, record: Mapping[str, Any]) -> dict[str, Any] | None:
        row = self._pipeline_job_row(dict(record))
        if accepted_submit_contract_is_current(row) and accepted_submit_row_kind(row) == "master":
            raise FileOrchestrationJournalError(
                "file_journal_authority_transition_requires_typed_api",
                field="append_historical_pipeline_job",
            )
        source_id = _source_id_from_job(row)
        cycle_time = _cycle_time_from_job(row)
        model_id = _optional_safe_identity(row, "model_id")
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._pipeline_job_for_id_unlocked(str(row["job_id"]))
            if existing is not None:
                return _public_scheduler_row(existing)
            return self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=model_id)

    def reserve_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any] | None:
        if accepted_submit_contract_is_current(record) and accepted_submit_row_kind(record) == "master":
            _accepted_submit_attempt_anchor(record.get("submission_attempt_started_at"))
            dirty_fields = {
                field
                for field in (
                    "slurm_job_id",
                    "array_task_id",
                    "submitted_at",
                    "started_at",
                    "finished_at",
                    "exit_code",
                    "error_code",
                    "error_message",
                    "log_uri",
                    "submit_outcome",
                    "reconciliation_source",
                    "reconciliation_decision",
                    "reconciliation_reason_class",
                    "matched_slurm_job_id",
                    # #1850 Fix A: a clean reservation may never carry binding
                    # provenance in -- it is minted only by a typed bind and
                    # cleared only by a reclaim/new attempt.
                    SLURM_BINDING_SOURCE_FIELD,
                    SLURM_ACCOUNTING_SUBMITTED_AT_FIELD,
                    # #1748: a fresh reservation may never carry an operator
                    # attestation in -- the marker is settable only by the typed
                    # recovery API, on an already-released row.
                    OPERATOR_RECOVERY_ATTESTATION_FIELD,
                )
                if record.get(field) not in (None, "")
            }
            if record.get("retry_count") not in (None, 0):
                dirty_fields.add("retry_count")
            if record.get("manual_retry_marker") not in (None, False):
                dirty_fields.add("manual_retry_marker")
            if record.get("previous_job_id") not in (None, ""):
                dirty_fields.add("previous_job_id")
            if record.get("candidate_projections") not in (None, [], ()):
                dirty_fields.add("candidate_projections")
            if record.get("cancellation_receipt_recorded") not in (None, False):
                dirty_fields.add("cancellation_receipt_recorded")
            if str(record.get("status") or "reserved") != "reserved" or dirty_fields:
                raise FileOrchestrationJournalError(
                    "file_journal_clean_reservation_required",
                    field=sorted(dirty_fields)[0] if dirty_fields else "status",
                )
        row = self._pipeline_job_row(
            {
                **record,
                "status": record.get("status", "reserved"),
                "submitted_at": None,
                "started_at": None,
                "finished_at": None,
                "exit_code": None,
                # A null ``error_code`` here is the auto-retry isolation contract:
                # a released reservation inherits it, so ``classify_failure`` reads
                # UNKNOWN_FAILURE and never re-submits (spec:
                # candidate-projection-stage-attempt-retention).
                "error_code": None,
                "error_message": None,
                "log_uri": None,
                # #1850 Fix A: a fresh reservation opens without binding
                # provenance; a typed bind mints it and a reclaim clears it.
                SLURM_BINDING_SOURCE_FIELD: None,
                SLURM_ACCOUNTING_SUBMITTED_AT_FIELD: None,
            }
        )
        source_id = _source_id_from_job(row)
        cycle_time = _cycle_time_from_job(row)
        model_id = _optional_safe_identity(row, "model_id")
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            if self._pipeline_job_conflicts_unlocked(row):
                return None
            # #1796: the authority append inside ``_write_pipeline_job_unlocked``
            # is the commit point of this reservation.  A derived
            # direct/inventory/latest projection fault after it is contained to
            # a bounded committed-warning (see
            # ``_project_committed_pipeline_job_write``) instead of escaping as
            # ``FILE_JOURNAL_WRITE_FAILED`` and stranding a live pre-sbatch
            # ``reserved`` row.  Pre-append failure stays fail-closed.
            return self._write_pipeline_job_unlocked(
                row,
                exclusive_direct=True,
                model_id=model_id,
                _committed_projection_containment=True,
            )

    def reclaim_pipeline_job_reservation(self, record: dict[str, Any]) -> dict[str, Any] | None:
        expected_current_attempt = record.get("expected_submission_attempt")
        expected_current_anchor = record.get("expected_submission_attempt_started_at")
        request_row = self._pipeline_job_row(record)
        idempotency_key = str(request_row["idempotency_key"])
        source_id = _source_id_from_job(request_row)
        cycle_time = _cycle_time_from_job(request_row)
        versioned_master = bool(
            accepted_submit_contract_is_current(request_row)
            and accepted_submit_row_kind(request_row) == "master"
        )
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = (
                self._accepted_submit_job_for_id_unlocked(
                    str(request_row["job_id"]),
                    source_id=source_id,
                    cycle_time=cycle_time,
                )
                if versioned_master
                else self._candidate_job_for_idempotency_unlocked(idempotency_key)
            )
            matched_by_key = bool(
                existing is not None and str(existing.get("idempotency_key") or "") == idempotency_key
            )
            if existing is None and request_row.get("job_id") not in (None, ""):
                existing = (
                    None
                    if versioned_master
                    else self._pipeline_job_for_id_unlocked(str(request_row["job_id"]))
                )
            if existing is None:
                return None
            existing_is_current_master = bool(
                accepted_submit_contract_is_current(existing)
                and accepted_submit_row_kind(existing) == "master"
            )
            if existing_is_current_master:
                if not versioned_master:
                    return None
                if (
                    str(existing.get("status") or "") != "reservation_lost"
                    or existing.get("slurm_job_id") not in (None, "")
                    or existing.get("submit_outcome") != "submit_result_ambiguous"
                    or existing.get("reconciliation_source") != "slurm_exact_comment"
                    or existing.get("reconciliation_decision")
                    not in {"absence_retry_permitted", OPERATOR_VERIFIED_ABSENCE_DECISION}
                    or existing.get("reconciliation_reason_class") is not None
                    or existing.get("matched_slurm_job_id") is not None
                    or expected_current_attempt is None
                    or expected_current_anchor is None
                ):
                    return None
                if max(int(existing.get("submission_attempt") or 1), 1) != max(
                    int(expected_current_attempt), 1
                ):
                    return None
                try:
                    if _accepted_submit_attempt_anchor(
                        existing.get("submission_attempt_started_at")
                    ) != _accepted_submit_attempt_anchor(expected_current_anchor):
                        return None
                except FileOrchestrationJournalError:
                    return None
            if versioned_master and (
                not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
                or not matched_by_key
            ):
                return None
            if versioned_master:
                existing_identity = _accepted_submit_master_identity(existing)
                request_identity = _accepted_submit_master_identity(request_row)
                if any(
                    request_identity[field] != existing_identity[field]
                    for field in existing_identity
                    if field not in {"submission_attempt", "submission_attempt_started_at"}
                ):
                    return None
            existing_status = str(existing.get("status") or "")
            if matched_by_key:
                if existing.get("slurm_job_id") not in (None, "") or existing_status not in {
                    "submission_failed",
                    "reservation_lost",
                }:
                    return None
            else:
                if (
                    existing.get("idempotency_key") not in (None, "")
                    or existing.get("slurm_job_id") not in (None, "")
                    or existing_status != "pending"
                    or (
                        not versioned_master
                        and self._candidate_job_for_idempotency_unlocked(idempotency_key) is not None
                    )
                ):
                    return None
            # The new attempt's row is derived from the PERSISTED row, so the
            # init-state identity mapping it carries is the one captured at the
            # first reservation. That is the adjudicated semantics (#1188):
            # "stable from reservation onward" means from the FIRST reservation,
            # and a reclaim does not refresh the mapping even though it opens a
            # new submission attempt.
            row = (
                apply_accepted_submit_transition(
                    existing,
                    AcceptedSubmitTransition.begin_attempt(),
                )
                if accepted_submit_row_kind(existing) == "master"
                else dict(existing)
            )
            row.update(
                {
                    "status": "reserved",
                    "slurm_job_id": None,
                    "array_task_id": None,
                    "submitted_at": None,
                    "started_at": None,
                    "finished_at": None,
                    "exit_code": None,
                    # Same auto-retry isolation contract as the reservation write
                    # above: a reclaimed reservation that is later released must
                    # still classify as non-retriable (spec:
                    # candidate-projection-stage-attempt-retention).
                    "error_code": None,
                    "error_message": None,
                    "cancellation_receipt_recorded": False,
                    "idempotency_key": idempotency_key,
                    # #1850 Fix A: a reclaim opens a NEW attempt, so the
                    # previous attempt's binding provenance is cleared here,
                    # never inherited across attempts.
                    SLURM_BINDING_SOURCE_FIELD: None,
                    SLURM_ACCOUNTING_SUBMITTED_AT_FIELD: None,
                    "updated_at": _format_utc(_utcnow()),
                }
            )
            # The successful lock holder owns the new attempt: for a versioned
            # master it is always the durable existing attempt + 1, exactly
            # once, never taken from the lock-external reclaim request.  (The
            # anchor below follows the same discipline.)  Legacy/non-versioned
            # rows keep the historical max() so their existing behaviour is
            # unchanged.
            if versioned_master:
                row["submission_attempt"] = int(existing.get("submission_attempt") or 1) + 1
            else:
                row["submission_attempt"] = max(
                    int(existing.get("submission_attempt") or 1) + 1,
                    int(request_row.get("submission_attempt") or 1),
                )
            # The successful lock holder owns a new attempt. Its immutable
            # authority anchor is captured here, never copied from a stale
            # lock-external reclaim request.
            row["submission_attempt_started_at"] = _format_utc(_utcnow())
            if not versioned_master:
                # INIT_STATE_IDENTITY_FIELD is deliberately absent from this
                # backfill set (#1188): keeping it out is what makes the reclaim
                # keep-first, since a recomputed mapping on the lock-external
                # request row would otherwise overwrite the first attempt's
                # captured lineage. Same reason as the attempt anchor above —
                # durable authority state never comes from outside the lock.
                # Two halves, not one: for a versioned master the enclosing
                # ``if not versioned_master:`` never runs, so keep-first there
                # rests on the guard AND on this omission. Adding the field back
                # to the tuple alone would not change versioned-master behaviour,
                # and flipping the guard alone would not either; both must hold.
                for key in (
                    "run_id",
                    "cycle_id",
                    "model_id",
                    "stage",
                    "candidate_id",
                    "job_type",
                    "slurm_comment",
                    "cohort_members",
                    "cohort_digest",
                    "restart_stage",
                    "expected_slurm_user",
                    "expected_slurm_account",
                    "slurm_ownership_required",
                    "native_shud_resubmitted",
                ):
                    if key in request_row and request_row.get(key) not in (None, ""):
                        row[key] = request_row[key]
                for key in ("run_id", "cycle_id", "model_id", "stage", "candidate_id", "job_type"):
                    if row.get(key) in (None, "") and request_row.get(key) not in (None, ""):
                        row[key] = request_row[key]
            # #1564 D9 / #1796 committed reclaim completion: the authority append
            # inside ``_write_pipeline_job_unlocked`` is the commit point of the
            # new attempt.  A derived direct/inventory/latest projection failure
            # after that append must never be reported as an uncommitted failure
            # that strands a live pre-sbatch ``reserved`` row -- the identical
            # defect is reachable through every reclaim door (operator
            # ``operator_verified_absence`` and automatic
            # ``absence_retry_permitted``) plus plain reserve, so containment is
            # opt-in for every reservation/reclaim write at this boundary.
            model_id = _optional_safe_identity(row, "model_id")
            return self._write_pipeline_job_unlocked(
                row,
                exclusive_direct=False,
                model_id=model_id,
                _committed_projection_containment=True,
            )

    def bind_pipeline_job_reservation(
        self,
        idempotency_key: str,
        *,
        slurm_job_id: str,
        status: str = "submitted",
        array_task_id: int | None = None,
    ) -> dict[str, Any] | None:
        initial = self._candidate_job_for_idempotency_unlocked(idempotency_key)
        if initial is None:
            return None
        if accepted_submit_contract_is_current(initial) and accepted_submit_row_kind(initial) == "master":
            raise FileOrchestrationJournalError(
                "file_journal_authority_transition_requires_typed_api",
                field="bind_reservation",
            )
        source_id = _source_id_from_job(initial)
        cycle_time = _cycle_time_from_job(initial)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._candidate_job_for_idempotency_unlocked(idempotency_key)
            if existing is None or existing.get("slurm_job_id") not in (None, ""):
                return None
            if accepted_submit_contract_is_current(existing) and accepted_submit_row_kind(existing) == "master":
                raise FileOrchestrationJournalError(
                    "file_journal_authority_transition_requires_typed_api",
                    field="bind_reservation",
                )
            row = apply_accepted_submit_transition(
                existing,
                AcceptedSubmitTransition.accepted(status=status),
            )
            row.update(
                {
                    "slurm_job_id": str(slurm_job_id),
                    "submitted_at": row.get("submitted_at") or _format_utc(_utcnow()),
                    "updated_at": _format_utc(_utcnow()),
                }
            )
            if array_task_id is not None:
                row["array_task_id"] = array_task_id
            model_id = _optional_safe_identity(row, "model_id")
            return self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=model_id)

    def commit_pipeline_job_submit_attempt(
        self,
        idempotency_key: str,
        *,
        pipeline_job_id: str | None = None,
        expected_submission_attempt: int,
        slurm_job_id: str,
        transition: AcceptedSubmitTransition,
        array_task_id: int | None = None,
        submitted_at: datetime | None = None,
        slurm_accounting_submitted_at: datetime | str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        exit_code: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        log_uri: str | None = None,
    ) -> AcceptedSubmitCommitResult:
        """Bind and transition exactly one still-current reservation attempt.

        The attempt check, reserved-state check, unbound check, Slurm id bind,
        and complete accepted-submit evidence transition all happen under the
        same cycle lock and in one durable journal replacement.
        """

        if not isinstance(transition, AcceptedSubmitTransition):
            raise TypeError("transition must be AcceptedSubmitTransition")
        if transition.submit_outcome != "accepted":
            raise ValueError("submit-attempt commit requires an accepted transition")
        # Writer-authority closed world (#1564 D8): the operator decision is
        # durable provenance granted only by the dedicated typed demotion, never
        # by an accepted-submit writer, even when a caller forges an accepted
        # transition carrying the token.  Refuse before any mutation.
        if transition.reconciliation_decision == OPERATOR_VERIFIED_ABSENCE_DECISION:
            raise FileOrchestrationJournalError(
                "file_journal_authority_transition_requires_typed_api",
                field="reconciliation_decision",
            )
        if type(transition.status) is not str or transition.status not in {
            "submitted",
            "pending",
            "queued",
            "running",
        }:
            raise FileOrchestrationJournalError(
                "file_journal_evidence_enum_invalid", field="status"
            )
        requested_id = str(slurm_job_id)
        if not requested_id.isdigit():
            raise ValueError("submit-attempt commit requires a numeric Slurm master job id")
        if (
            transition.reconciliation_decision == "matched_bound"
            and transition.matched_slurm_job_id != requested_id
        ):
            raise ValueError("matched accounting id must equal the bound Slurm job id")
        if pipeline_job_id is not None:
            source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(pipeline_job_id)
        else:
            initial = self._candidate_job_for_idempotency_unlocked(idempotency_key)
            if initial is None:
                return AcceptedSubmitCommitResult("missing")
            source_id = _source_id_from_job(initial)
            cycle_time = _cycle_time_from_job(initial)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = (
                self._accepted_submit_job_for_id_unlocked(
                    pipeline_job_id,
                    source_id=source_id,
                    cycle_time=cycle_time,
                )
                if pipeline_job_id is not None
                else self._candidate_job_for_idempotency_unlocked(idempotency_key)
            )
            if existing is None:
                return AcceptedSubmitCommitResult("missing")
            if pipeline_job_id is not None and (
                not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
                or str(existing.get("idempotency_key") or "") != idempotency_key
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            current_id = str(existing.get("slurm_job_id") or "")
            current_attempt = max(int(existing.get("submission_attempt") or 1), 1)
            if current_attempt != max(int(expected_submission_attempt), 1):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            # #1850 round 3 (Fix A): canonical normalization and derived bind
            # provenance are computed BEFORE the idempotent early-return. A
            # replay is idempotent ONLY when the full bind shape it WOULD write
            # -- the transition bind lane, the attempt-scoped derived binding
            # source, and (for the fallback) the canonical accounting Submit --
            # matches the durable bind exactly. A different canonical Submit on
            # the same tuple is a conflicting replay: non-committed, zero-write,
            # never reported as ``idempotent``/``bound``.
            fallback_unique = (
                transition.reconciliation_source == "slurm_name_window_unique"
                and transition.reconciliation_decision == "matched_bound"
            )
            # Canonical sacct ``Submit`` is the ONE authority for the fallback.
            # The candidate instant that drives the claimant/occupancy scan
            # comes ONLY from the explicit canonical accounting input, never
            # from the legacy gateway/commit ``submitted_at`` (which remains
            # acceptance/commit time and is never incarnation or window proof).
            # The fallback bind REQUIRES a strict canonical instant; every
            # non-fallback lane may not carry one.
            canonical_accounting_submit = normalize_slurm_accounting_submitted_at(
                slurm_accounting_submitted_at
            )
            if fallback_unique and canonical_accounting_submit is None:
                raise FileOrchestrationJournalError(
                    "file_journal_submit_instant_required",
                    field=SLURM_ACCOUNTING_SUBMITTED_AT_FIELD,
                )
            if not fallback_unique and slurm_accounting_submitted_at not in (None, ""):
                raise FileOrchestrationJournalError(
                    "file_journal_evidence_invariant_invalid",
                    field=SLURM_ACCOUNTING_SUBMITTED_AT_FIELD,
                )
            candidate_submit = _strict_utc_datetime(canonical_accounting_submit)
            if fallback_unique and candidate_submit is None:
                raise FileOrchestrationJournalError(
                    "file_journal_submit_instant_required",
                    field=SLURM_ACCOUNTING_SUBMITTED_AT_FIELD,
                )
            # The bind provenance this commit WOULD persist, derived centrally
            # from the transition shape (never from caller-forgeable fields).
            derived_binding_source = binding_source_for_transition(
                submit_outcome=str(transition.submit_outcome or ""),
                reconciliation_decision=transition.reconciliation_decision,
                reconciliation_source=str(transition.reconciliation_source or ""),
            )
            if derived_binding_source is None:
                # #1850 round 4 (Fix A): the typed commit accepts ONLY the three
                # legal bind shapes (ordinary accepted -> gateway_submit,
                # exact-comment matched -> slurm_exact_comment, name-window
                # matched -> slurm_name_window_unique). A transition that mints
                # no binding provenance (accepted + a held/defer/blocked
                # accounting decision, rejected, timeout, pre-outcome) is not a
                # bind and is refused with a stable error BEFORE any mutation or
                # occupancy scan -- otherwise a closed-world bind could be
                # forged with ``binding_source=None``.
                raise FileOrchestrationJournalError(
                    "file_journal_authority_transition_requires_typed_api",
                    field="transition",
                )
            if current_id:
                if current_id != requested_id:
                    return AcceptedSubmitCommitResult("collision", dict(existing))
                same_lane = (
                    existing.get("submit_outcome") == transition.submit_outcome
                    and existing.get("reconciliation_source") == transition.reconciliation_source
                    and existing.get("reconciliation_decision") == transition.reconciliation_decision
                    and existing.get("matched_slurm_job_id") == transition.matched_slurm_job_id
                )
                # #1850 round 4 (Fix C): replay-equality is decided by ONE
                # centralized helper.  It keeps the pre-change v1 read
                # compatibility (a legacy row missing BOTH additive provenance
                # fields stays idempotent for an ordinary/exact-comment same-lane
                # replay, zero-write, never backfilled), while every new
                # provenance-carrying row still requires the exact derived
                # binding source and canonical accounting Submit.  Partial,
                # contradictory, or name-window-on-missing-fields shapes are
                # never idempotent and fall through to ``stale``.
                if bind_replay_is_idempotent(
                    existing,
                    derived_binding_source=derived_binding_source,
                    canonical_accounting_submit=canonical_accounting_submit,
                    same_lane=same_lane,
                ):
                    return AcceptedSubmitCommitResult("idempotent", dict(existing))
                return AcceptedSubmitCommitResult("stale", dict(existing))
            if str(existing.get("status") or "") != "reserved":
                return AcceptedSubmitCommitResult("stale", dict(existing))
            with self._reconcile_inventory_file_lock_unlocked():
                entry_names = self._reconcile_inventory_entry_names_unlocked()
                other_masters, ambiguous = self._reconcile_inventory_jobs_matching_unlocked(
                    entry_names,
                    expected_user=str(existing.get("expected_slurm_user") or ""),
                    expected_account=str(existing.get("expected_slurm_account") or ""),
                    candidate_submit=candidate_submit,
                    active_slurm_job_id=requested_id,
                    include_job_id=str(existing.get("job_id") or ""),
                    fallback_unique=fallback_unique,
                )
                if ambiguous:
                    # More than one current reserved-unbound forecast master
                    # claims this candidate window for the same owner: no
                    # claimant may bind, regardless of reconcile iteration
                    # order or concurrent source/cycle writers.
                    return AcceptedSubmitCommitResult(
                        "ambiguous_fallback_match", dict(existing)
                    )
                if any(
                    str(master.get("slurm_job_id") or "") == requested_id
                    for master in other_masters
                ):
                    # The requested id is already bound to another active
                    # current accepted-submit master in any source/cycle.
                    return AcceptedSubmitCommitResult(
                        "active_slurm_id_occupied", dict(existing)
                    )
                # The bind write happens INSIDE the journal-global inventory
                # lock, so no concurrent source/cycle writer (normal stage
                # submit, exact-comment commit, fallback commit) can enter the
                # scan-bind window or bind this id between the scan and the
                # commit point (cycle lock -> inventory lock order).
                row = apply_accepted_submit_transition(existing, transition)
                # #1850 Fix A (round 2): every successful typed bind records its
                # attempt-scoped binding provenance exactly once, derived
                # centrally from the transition SHAPE -- never from
                # caller-forgeable transition fields (removed) and never from
                # the legacy ``submitted_at``. The name-window fallback
                # persists the single canonical accounting Submit keyword; the
                # ordinary gateway submit and exact-comment matched recovery
                # persist no canonical evidence. ``binding_source_for_transition``
                # returns ``None`` for every non-bind shape, so a held/defer/
                # release/reject/timeout transition never mints provenance.
                derived_binding_source = binding_source_for_transition(
                    submit_outcome=str(transition.submit_outcome or ""),
                    reconciliation_decision=transition.reconciliation_decision,
                    reconciliation_source=str(transition.reconciliation_source or ""),
                )
                if derived_binding_source is not None:
                    row[SLURM_BINDING_SOURCE_FIELD] = derived_binding_source
                    row[SLURM_ACCOUNTING_SUBMITTED_AT_FIELD] = (
                        canonical_accounting_submit
                        if derived_binding_source == "slurm_name_window_unique"
                        else None
                    )
                # #1589 (design D3): unconditional writes of caller evidence,
                # so withheld resolves against the persisted row rather than
                # erasing it.  These ``durable=`` arguments are LOAD BEARING,
                # not uniformity: a ``reserved`` row is not necessarily
                # evidence-free.  ``reserve`` nulls the whole family, but
                # ``reclaim_pipeline_job_reservation`` nulls
                # ``exit_code``/``error_code``/``error_message`` and NOT
                # ``log_uri``, so a reclaimed reservation carries the
                # previous attempt's URI into this leg; and an unbound
                # submit-evidence transition writes evidence onto a row that
                # stays ``reserved``.  Neutralizing the ``log_uri``
                # resolution below turns exactly the
                # ``commit_pipeline_job_submit_attempt`` arm of the J20
                # replay table red -- do not "simplify" these away.
                row.update(
                    {
                        "slurm_job_id": requested_id,
                        "submitted_at": _format_utc(submitted_at or _utcnow()),
                        "started_at": (
                            _format_utc(started_at) if started_at is not None else None
                        ),
                        "finished_at": (
                            _format_utc(finished_at) if finished_at is not None else None
                        ),
                        "exit_code": _resolved_caller_evidence(
                            exit_code, durable=existing.get("exit_code")
                        ),
                        "error_code": _resolved_caller_evidence(
                            error_code, durable=existing.get("error_code")
                        ),
                        "error_message": _resolved_caller_evidence(
                            error_message, durable=existing.get("error_message")
                        ),
                        "log_uri": _resolved_caller_evidence(
                            log_uri, durable=existing.get("log_uri")
                        ),
                        "updated_at": _format_utc(_utcnow()),
                    }
                )
                if array_task_id is not None:
                    row["array_task_id"] = array_task_id
                model_id = _optional_safe_identity(row, "model_id")
                written = self._write_pipeline_job_unlocked(
                    row, exclusive_direct=False, model_id=model_id
                )
                return AcceptedSubmitCommitResult("applied", written)

    def transition_pipeline_job_submit_evidence(
        self,
        job_id: str,
        transition: AcceptedSubmitTransition,
        *,
        accepted_submit_contract_version: str | None = None,
        expected_submission_attempt: int | None = None,
        expected_statuses: Sequence[str] | None = None,
        require_unbound: bool = False,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        exit_code: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        log_uri: str | None = None,
    ) -> AcceptedSubmitCommitResult:
        """Atomically replace submit outcome and the complete accounting tuple."""

        if not isinstance(transition, AcceptedSubmitTransition):
            raise TypeError("transition must be AcceptedSubmitTransition")
        versioned = accepted_submit_contract_version == ACCEPTED_SUBMIT_CONTRACT_VERSION
        if accepted_submit_contract_version not in (None, ACCEPTED_SUBMIT_CONTRACT_VERSION):
            raise FileOrchestrationJournalError(
                "file_journal_evidence_enum_invalid",
                field=ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD,
            )
        if versioned:
            if (
                transition.submit_outcome != "submit_result_ambiguous"
                or transition.reconciliation_decision
                not in _GENERIC_VERSIONED_RECONCILIATION_DECISIONS
            ):
                raise FileOrchestrationJournalError(
                    "file_journal_authority_transition_requires_typed_api",
                    field="transition",
                )
            if (
                expected_submission_attempt is None
                or not expected_statuses
                or not require_unbound
            ):
                raise FileOrchestrationJournalError(
                    "file_journal_authority_transition_requires_cas",
                    field="expected_submission_attempt",
                )
            source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        else:
            # Writer-authority closed world (#1805): the legacy compatibility
            # path has no decision whitelist, so an ``operator_verified_absence``
            # transition would mint the typed operator authority without the
            # dedicated audited demotion.  Refuse before any read, lock
            # acquisition, row construction, durable mutation, or event.  The
            # versioned gate above keeps its own unchanged behavior.
            if transition.reconciliation_decision == OPERATOR_VERIFIED_ABSENCE_DECISION:
                raise FileOrchestrationJournalError(
                    "file_journal_authority_transition_requires_typed_api",
                    field="reconciliation_decision",
                )
            initial = self._pipeline_job_for_id_unlocked(job_id)
            if initial is None:
                return AcceptedSubmitCommitResult("missing")
            if accepted_submit_contract_is_current(initial) and accepted_submit_row_kind(initial) == "master":
                raise FileOrchestrationJournalError(
                    "file_journal_authority_transition_requires_typed_api",
                    field="accepted_submit_contract_version",
                )
            source_id = _source_id_from_job(initial)
            cycle_time = _cycle_time_from_job(initial)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = (
                self._accepted_submit_job_for_id_unlocked(
                    job_id,
                    source_id=source_id,
                    cycle_time=cycle_time,
                )
                if versioned
                else self._pipeline_job_for_id_unlocked(job_id)
            )
            if existing is None:
                return AcceptedSubmitCommitResult("missing")
            if not versioned and accepted_submit_contract_is_current(existing) and accepted_submit_row_kind(
                existing
            ) == "master":
                raise FileOrchestrationJournalError(
                    "file_journal_authority_transition_requires_typed_api",
                    field="accepted_submit_contract_version",
                )
            if versioned and (
                not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            if expected_submission_attempt is not None and max(
                int(existing.get("submission_attempt") or 1), 1
            ) != max(int(expected_submission_attempt), 1):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            if expected_statuses is not None and str(existing.get("status") or "") not in set(expected_statuses):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            if require_unbound and existing.get("slurm_job_id") not in (None, ""):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            row = apply_accepted_submit_transition(existing, transition)
            # #1589 (design D3): resolve BEFORE the guards, which puts it before
            # the ``changed_fields`` equality gate below too -- that ordering is
            # the whole fix.  The gate compares the row against durable state,
            # and durable state has been stripped by the write boundary, so a
            # raw placeholder here would make an identical replay differ from
            # the row forever: every pass "applied", every pass another record.
            started_at = _resolved_caller_evidence(started_at)
            finished_at = _resolved_caller_evidence(finished_at)
            exit_code = _resolved_caller_evidence(exit_code)
            error_code = _resolved_caller_evidence(error_code)
            error_message = _resolved_caller_evidence(error_message)
            log_uri = _resolved_caller_evidence(log_uri)
            if started_at is not None:
                row["started_at"] = _format_utc(started_at)
            if finished_at is not None:
                row["finished_at"] = _format_utc(finished_at)
            if exit_code is not None:
                row["exit_code"] = exit_code
            if error_code is not None:
                row["error_code"] = error_code
            if error_message is not None:
                row["error_message"] = _durable_error_message(error_message)
            if log_uri is not None:
                row["log_uri"] = log_uri
            changed_fields = (
                "submit_outcome",
                "reconciliation_source",
                "reconciliation_decision",
                "reconciliation_reason_class",
                "matched_slurm_job_id",
                # Without this the consecutive-blocked counter's increment would
                # be judged idempotent and silently never reach the journal.
                "identity_blocked_streak",
                "status",
                "started_at",
                "finished_at",
                "exit_code",
                "error_code",
                "error_message",
                "log_uri",
            )
            if all(row.get(field) == existing.get(field) for field in changed_fields):
                return AcceptedSubmitCommitResult("idempotent", dict(existing))
            row["updated_at"] = _format_utc(_utcnow())
            model_id = _optional_safe_identity(row, "model_id")
            written = self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=model_id)
            return AcceptedSubmitCommitResult("applied", written)

    def transition_pipeline_job_runtime_status(
        self,
        job_id: str,
        status: str,
        *,
        expected_statuses: Sequence[str] | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        exit_code: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        log_uri: str | None = None,
    ) -> AcceptedSubmitCommitResult:
        """Advance one current master through non-terminal runtime states."""

        if status in TERMINAL_PIPELINE_STATUSES:
            raise FileOrchestrationJournalError(
                "file_journal_terminal_projection_required",
                field="status",
            )
        source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._accepted_submit_job_for_id_unlocked(
                job_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            if (
                existing is None
                or not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing or {}))
            if (
                existing.get("submit_outcome") != "accepted"
                or not str(existing.get("slurm_job_id") or "").isdigit()
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            if expected_statuses is not None and str(existing.get("status") or "") not in set(
                expected_statuses
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            current_status = str(existing.get("status") or "")
            if status not in _ACCEPTED_RUNTIME_TRANSITIONS.get(current_status, frozenset()):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            row = dict(existing)
            row["status"] = status
            # #1589 (design D3): same ordering requirement as the submit-evidence
            # leg -- resolve before the guards and therefore before the equality
            # gate.  ``running -> running`` is a legal self transition, so a raw
            # placeholder here is an unbounded append loop: nothing else in this
            # leg ever stops an identical poll from "changing" the row.
            started_at = _resolved_caller_evidence(started_at)
            finished_at = _resolved_caller_evidence(finished_at)
            exit_code = _resolved_caller_evidence(exit_code)
            error_code = _resolved_caller_evidence(error_code)
            error_message = _resolved_caller_evidence(error_message)
            log_uri = _resolved_caller_evidence(log_uri)
            if started_at is not None:
                row["started_at"] = _format_utc(started_at)
            if finished_at is not None:
                row["finished_at"] = _format_utc(finished_at)
            if exit_code is not None:
                row["exit_code"] = exit_code
            if error_code is not None:
                row["error_code"] = error_code
            if error_message is not None:
                row["error_message"] = _durable_error_message(error_message)
            if log_uri is not None:
                row["log_uri"] = log_uri
            if all(
                row.get(field) == existing.get(field)
                for field in (
                    "status",
                    "started_at",
                    "finished_at",
                    "exit_code",
                    "error_code",
                    "error_message",
                    "log_uri",
                )
            ):
                return AcceptedSubmitCommitResult("idempotent", dict(existing))
            row["updated_at"] = _format_utc(_utcnow())
            written = self._write_pipeline_job_unlocked(
                row,
                exclusive_direct=False,
                model_id=None,
            )
            return AcceptedSubmitCommitResult("applied", written)

    def request_pipeline_job_cancellation(
        self,
        job_id: str,
        *,
        expected_statuses: Sequence[str],
        reason: str,
    ) -> AcceptedSubmitCommitResult:
        """Persist cancellation intent before the external Slurm side effect."""

        source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._accepted_submit_job_for_id_unlocked(
                job_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            if (
                existing is None
                or not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing or {}))
            if (
                existing.get("submit_outcome") != "accepted"
                or not str(existing.get("slurm_job_id") or "").isdigit()
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            if str(existing.get("status") or "") == "cancellation_pending":
                return AcceptedSubmitCommitResult("idempotent", dict(existing))
            if (
                str(existing.get("status") or "") == "reconcile_unverified"
                and bool(existing.get("cancellation_receipt_recorded", False))
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            if str(existing.get("status") or "") not in set(expected_statuses):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            row = dict(existing)
            row.update(
                {
                    "status": "cancellation_pending",
                    "error_code": "SLURM_CANCELLATION_REQUESTED",
                    "error_message": _durable_error_message(reason),
                    "updated_at": _format_utc(_utcnow()),
                }
            )
            written = self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=None)
            return AcceptedSubmitCommitResult("applied", written)

    def complete_pipeline_job_cancellation(
        self,
        job_id: str,
        *,
        finished_at: datetime,
        exit_code: int | None,
        error_code: str | None,
        error_message: str | None,
        log_uri: str | None,
    ) -> AcceptedSubmitCommitResult:
        """Record the Gateway receipt while deferring terminal truth to task accounting."""

        source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._accepted_submit_job_for_id_unlocked(
                job_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            if existing is None:
                return AcceptedSubmitCommitResult("stale", dict(existing or {}))
            if (
                not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
                or existing.get("submit_outcome") != "accepted"
                or not str(existing.get("slurm_job_id") or "").isdigit()
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            # #1589 (design D3): this leg writes its whole evidence family
            # UNCONDITIONALLY, so a stripped placeholder does not decline the
            # overwrite -- it destroys the value.  Withheld therefore resolves
            # against the persisted row.  Resolved ONCE, above the comparator,
            # so the value compared and the value persisted are literally the
            # same expression: otherwise the second Gateway receipt compares a
            # raw placeholder against erased durable state, answers ``stale``,
            # and ``chain_forecast_control.cancel_active_cycle_jobs`` drops the
            # cancellation event on an uncommitted result.
            resolved_exit_code = _resolved_caller_evidence(exit_code, durable=existing.get("exit_code"))
            resolved_error_message = _durable_error_message(
                _resolved_caller_evidence(error_message, durable=existing.get("error_message"))
            )
            resolved_log_uri = _resolved_caller_evidence(log_uri, durable=existing.get("log_uri"))
            desired_error_code = (
                _resolved_caller_evidence(error_code, durable=existing.get("error_code"))
                or "SLURM_JOB_CANCELLED"
            )
            if str(existing.get("status") or "") == "reconcile_unverified":
                desired = {
                    "finished_at": _format_utc(finished_at),
                    "exit_code": resolved_exit_code,
                    "error_code": desired_error_code,
                    "error_message": resolved_error_message,
                    "log_uri": resolved_log_uri,
                    "cancellation_receipt_recorded": True,
                }
                if all(existing.get(field) == value for field, value in desired.items()):
                    return AcceptedSubmitCommitResult("idempotent", dict(existing))
                return AcceptedSubmitCommitResult("stale", dict(existing))
            if str(existing.get("status") or "") != "cancellation_pending":
                return AcceptedSubmitCommitResult("stale", dict(existing))
            row = dict(existing)
            row.update(
                {
                    "status": "reconcile_unverified",
                    "finished_at": _format_utc(finished_at),
                    "exit_code": resolved_exit_code,
                    "error_code": desired_error_code,
                    "error_message": resolved_error_message,
                    "log_uri": resolved_log_uri,
                    "cancellation_receipt_recorded": True,
                    "updated_at": _format_utc(_utcnow()),
                }
            )
            written = self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=None)
            return AcceptedSubmitCommitResult("applied", written)

    def reject_pipeline_job_submit_attempt(
        self,
        idempotency_key: str,
        *,
        pipeline_job_id: str | None = None,
        expected_submission_attempt: int,
        finished_at: datetime,
        error_code: str,
        error_message: str,
        stage: str,
        job_type: str,
    ) -> AcceptedSubmitCommitResult:
        """Reject one current attempt and its staged hydro cohort atomically."""

        if pipeline_job_id is not None:
            source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(pipeline_job_id)
        else:
            initial = self._candidate_job_for_idempotency_unlocked(idempotency_key)
            if initial is None:
                return AcceptedSubmitCommitResult("missing")
            source_id = _source_id_from_job(initial)
            cycle_time = _cycle_time_from_job(initial)
        attempt = max(int(expected_submission_attempt), 1)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = (
                self._accepted_submit_job_for_id_unlocked(
                    pipeline_job_id,
                    source_id=source_id,
                    cycle_time=cycle_time,
                )
                if pipeline_job_id is not None
                else self._candidate_job_for_idempotency_unlocked(idempotency_key)
            )
            if existing is None:
                return AcceptedSubmitCommitResult("missing")
            if pipeline_job_id is not None and (
                not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
                or str(existing.get("idempotency_key") or "") != idempotency_key
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            if (
                max(int(existing.get("submission_attempt") or 1), 1) != attempt
                or str(existing.get("status") or "") != "reserved"
                or existing.get("slurm_job_id") not in (None, "")
            ):
                if (
                    max(int(existing.get("submission_attempt") or 1), 1) == attempt
                    and existing.get("submit_outcome") == "rejected"
                    and str(existing.get("status") or "") == "submission_failed"
                ):
                    return AcceptedSubmitCommitResult("idempotent", dict(existing))
                return AcceptedSubmitCommitResult("stale", dict(existing))

            safe_message = _durable_error_message(error_message)
            master = apply_accepted_submit_transition(existing, AcceptedSubmitTransition.rejected())
            master.update(
                {
                    "finished_at": _format_utc(finished_at),
                    "error_code": error_code,
                    "error_message": safe_message,
                    "updated_at": _format_utc(_utcnow()),
                }
            )
            payloads: list[tuple[str, dict[str, Any], str | None]] = []
            touched_models: set[str] = set()
            members = _bounded_cohort_members(existing.get("cohort_members"))
            model_rows = self._cycle_rows_by_model_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                model_ids=(str(member.get("model_id") or "") for member in members),
                include_direct_jobs=False,
            )
            for member in members:
                run_id = str(member.get("run_id") or "")
                model_id = str(member.get("model_id") or "")
                if not run_id or not model_id:
                    continue
                snapshot = model_rows.get(model_id)
                hydro = snapshot.hydro_run if snapshot is not None else None
                if isinstance(hydro, Mapping) and str(hydro.get("run_id") or "") != run_id:
                    hydro = None
                if (
                    hydro is None
                    or max(int(hydro.get("submission_attempt") or 1), 1) != attempt
                    or str(hydro.get("status") or "") not in ACTIVE_HYDRO_STATUSES
                ):
                    continue
                hydro_row = dict(hydro)
                hydro_row.update(
                    {
                        "status": "failed",
                        "error_code": error_code,
                        "error_message": safe_message,
                        "updated_at": _format_utc(_utcnow()),
                    }
                )
                payloads.append(("hydro_run", hydro_row, model_id))
                touched_models.add(model_id)
            payloads.append(("pipeline_job", master, None))
            event = {
                "event_id": self._next_accepted_submit_event_id_unlocked(
                    source_id=source_id,
                    cycle_time=cycle_time,
                ),
                "entity_type": "pipeline_job",
                "entity_id": str(master["job_id"]),
                "event_type": "submission",
                "status_from": "reserved",
                "status_to": "submission_failed",
                "message": f"{stage} submission failed: {safe_message}",
                "details": {
                    "stage": stage,
                    "job_type": job_type,
                    "error": safe_message,
                    "submit_outcome": "rejected",
                    "submission_attempt": attempt,
                },
                "created_at": _format_utc(_utcnow()),
            }
            payloads.append(("pipeline_event", event, None))

            next_sequence = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)
            records: list[dict[str, Any]] = []
            for offset, (record_type, payload, model_id) in enumerate(payloads):
                record = _journal_record_for_write(
                    record_type,
                    payload,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                    sequence=next_sequence + offset,
                )
                self._validate_outgoing_record(
                    record,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    record_type=record_type,
                    model_id=model_id,
                )
                records.append(record)
            self._append_journal_records_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                records=records,
            )
            self._write_pipeline_job_direct_unlocked(master, records[-2])
            for model_id in sorted(touched_models):
                self._materialize_latest_unlocked(
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                    include_direct_jobs=False,
                )
            return AcceptedSubmitCommitResult("applied", _public_scheduler_row(master))

    def mark_pipeline_job_permanently_failed(
        self,
        job_id: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        finished_at: datetime | None = None,
        event_details: Mapping[str, Any] | None = None,
    ) -> AcceptedSubmitCommitResult:
        """Mark one current-contract cohort master permanently failed (#1312).

        ``update_pipeline_job_status`` refuses every master row on purpose, so
        the permanent-failure mark required for declined retries needs its own
        typed authority transition.  Only the WRITE SEQUENCE is borrowed from
        :meth:`reject_pipeline_job_submit_attempt`; none of its preconditions
        apply here (the target is already bound and already terminal) and its
        accounting replacement must NOT happen: a permanent-failure mark is a
        labelling transition, so the accepted-submit accounting tuple
        (reconciliation decision / submit outcome / matched Slurm job id) is
        preserved field-for-field by constructing the row in-lock.

        Idempotency reads the PERSISTED row, never the caller's snapshot, and
        the ``permanently_failed`` event is appended only with a real flip.
        """

        source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._accepted_submit_job_for_id_unlocked(
                job_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            if existing is None:
                return AcceptedSubmitCommitResult("missing")
            if (
                not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing))
            status_from = str(existing.get("status") or "")
            if status_from == "permanently_failed":
                return AcceptedSubmitCommitResult("idempotent", dict(existing))
            if status_from not in PERMANENT_FAILURE_SOURCE_STATUSES:
                return AcceptedSubmitCommitResult("stale", dict(existing))
            master = dict(existing)
            master["status"] = "permanently_failed"
            if error_code not in (None, ""):
                master["error_code"] = error_code
            if error_message is not None:
                master["error_message"] = _durable_error_message(error_message)
            if finished_at is not None:
                master["finished_at"] = _format_utc(finished_at)
            master["updated_at"] = _format_utc(_utcnow())
            event = {
                "event_id": self._next_accepted_submit_event_id_unlocked(
                    source_id=source_id,
                    cycle_time=cycle_time,
                ),
                "entity_type": "pipeline_job",
                "entity_id": str(master["job_id"]),
                "event_type": "permanently_failed",
                "status_from": status_from or None,
                "status_to": "permanently_failed",
                "message": "automatic retry declined; job marked permanently failed",
                "details": dict(event_details or {}),
                "created_at": _format_utc(_utcnow()),
            }
            payloads: list[tuple[str, dict[str, Any], str | None]] = [
                ("pipeline_job", master, None),
                ("pipeline_event", event, None),
            ]
            next_sequence = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)
            records: list[dict[str, Any]] = []
            for offset, (record_type, payload, model_id) in enumerate(payloads):
                record = _journal_record_for_write(
                    record_type,
                    payload,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                    sequence=next_sequence + offset,
                )
                self._validate_outgoing_record(
                    record,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    record_type=record_type,
                    model_id=model_id,
                )
                records.append(record)
            self._append_journal_records_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                records=records,
            )
            self._write_pipeline_job_direct_unlocked(master, records[0])
            return AcceptedSubmitCommitResult("applied", _public_scheduler_row(master))

    def record_pipeline_job_reconciliation(
        self,
        job_id: str,
        *,
        submit_outcome: str | None = None,
        reconciliation_decision: str | None = None,
        matched_slurm_job_id: str | None = None,
        candidate_projections: Sequence[Mapping[str, Any]] | None = None,
        status: str | None = None,
    ) -> dict[str, Any] | None:
        """Compatibility/projection API backed by complete typed transitions."""
        # Writer-authority closed world (#1805): the legacy reconciliation
        # recorder accepts raw decision strings, so it would mint the typed
        # operator authority without the dedicated audited demotion.  Refuse
        # before any read, lock acquisition, row construction, durable
        # mutation, or event; legal legacy decisions keep this API.
        if reconciliation_decision == OPERATOR_VERIFIED_ABSENCE_DECISION:
            raise FileOrchestrationJournalError(
                "file_journal_authority_transition_requires_typed_api",
                field="reconciliation_decision",
            )
        initial = self._pipeline_job_for_id_unlocked(job_id)
        if initial is None:
            return None
        if accepted_submit_contract_is_current(initial) and accepted_submit_row_kind(initial) == "master":
            raise FileOrchestrationJournalError(
                "file_journal_authority_transition_requires_typed_api",
                field="pipeline_job_reconciliation",
            )
        source_id = _source_id_from_job(initial)
        cycle_time = _cycle_time_from_job(initial)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._pipeline_job_for_id_unlocked(job_id)
            if existing is None:
                return None
            if accepted_submit_contract_is_current(existing) and accepted_submit_row_kind(existing) == "master":
                raise FileOrchestrationJournalError(
                    "file_journal_authority_transition_requires_typed_api",
                    field="pipeline_job_reconciliation",
                )
            row = dict(existing)
            if reconciliation_decision is not None:
                outcome = submit_outcome or str(existing.get("submit_outcome") or "")
                row = apply_accepted_submit_transition(
                    row,
                    AcceptedSubmitTransition.accounting(
                        reconciliation_decision,
                        submit_outcome=outcome,
                        matched_slurm_job_id=matched_slurm_job_id,
                        status=status,
                    ),
                )
            elif submit_outcome is not None:
                row = apply_accepted_submit_transition(
                    row,
                    AcceptedSubmitTransition(submit_outcome=submit_outcome, status=status),
                )
            elif matched_slurm_job_id is not None:
                raise FileOrchestrationJournalError(
                    "file_journal_evidence_invariant_invalid", field="matched_slurm_job_id"
                )
            if candidate_projections is not None:
                try:
                    row["candidate_projections"] = normalize_candidate_projections(
                        candidate_projections,
                        cohort_members=existing.get("cohort_members"),
                    )
                except AcceptedSubmitEvidenceError as error:
                    raise FileOrchestrationJournalError(error.reason, field=error.field) from error
            if status is not None and submit_outcome is None and reconciliation_decision is None:
                row["status"] = status
            row["updated_at"] = _format_utc(_utcnow())
            model_id = _optional_safe_identity(row, "model_id")
            return self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=model_id)

    def permit_pipeline_job_retry(
        self,
        job_id: str,
        *,
        accepted_submit_contract_version: str | None = None,
        expected_submission_attempt: int | None = None,
        expected_submission_attempt_started_at: datetime | None = None,
        expected_status: str = "reserved",
    ) -> int:
        """Move one still-reserved cohort to retryable exactly once under its cycle lock."""
        versioned = accepted_submit_contract_version == ACCEPTED_SUBMIT_CONTRACT_VERSION
        if versioned and (
            type(expected_submission_attempt) is not int or expected_submission_attempt < 1
        ):
            raise FileOrchestrationJournalError(
                "file_journal_evidence_type_invalid", field="expected_submission_attempt"
            )
        if versioned:
            source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        else:
            initial = self._pipeline_job_for_id_unlocked(job_id)
            if initial is None:
                return 0
            if accepted_submit_contract_is_current(initial) and accepted_submit_row_kind(initial) == "master":
                raise FileOrchestrationJournalError(
                    "file_journal_authority_transition_requires_typed_api",
                    field="accepted_submit_contract_version",
                )
            source_id = _source_id_from_job(initial)
            cycle_time = _cycle_time_from_job(initial)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = (
                self._accepted_submit_job_for_id_unlocked(
                    job_id,
                    source_id=source_id,
                    cycle_time=cycle_time,
                )
                if versioned
                else self._pipeline_job_for_id_unlocked(job_id)
            )
            if existing is None or str(existing.get("status") or "") != expected_status:
                return 0
            if not versioned and accepted_submit_contract_is_current(existing) and accepted_submit_row_kind(
                existing
            ) == "master":
                raise FileOrchestrationJournalError(
                    "file_journal_authority_transition_requires_typed_api",
                    field="accepted_submit_contract_version",
                )
            if versioned and (
                not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
            ):
                return 0
            if versioned:
                if expected_submission_attempt_started_at is None:
                    return 0
                try:
                    anchor_matches = _accepted_submit_attempt_anchor(
                        existing.get("submission_attempt_started_at")
                    ) == _accepted_submit_attempt_anchor(expected_submission_attempt_started_at)
                except FileOrchestrationJournalError:
                    return 0
                if not anchor_matches:
                    return 0
            if versioned and existing.get("submission_attempt") != expected_submission_attempt:
                return 0
            if not versioned and expected_submission_attempt is not None and max(
                int(existing.get("submission_attempt") or 1), 1
            ) != max(int(expected_submission_attempt), 1):
                return 0
            if existing.get("slurm_job_id") not in (None, ""):
                return 0
            # This write produces ``absence_retry_permitted``, one of the reclaim doors
            # the auto-retry isolation contract explicitly EXCLUDES: the row is meant to
            # be retried (``_verified_accepted_submit_forecast_retry`` and
            # ``reclaim_pipeline_job_reservation``'s precondition both key off this
            # shape).  The null ``error_code`` here is only the fact that the transition
            # introduces no new code; the isolation contract's subject is the
            # ``identity_mismatch_released`` sub-shape below (spec:
            # candidate-projection-stage-attempt-retention).
            cohort_row = apply_accepted_submit_transition(
                existing,
                AcceptedSubmitTransition.accounting(
                    "absence_retry_permitted",
                    submit_outcome="submit_result_ambiguous",
                    status="reservation_lost",
                ),
            )
            cohort_row.update(
                {
                    "updated_at": _format_utc(_utcnow()),
                }
            )
            attempt = max(int(existing.get("submission_attempt") or 1), 1)
            source_id = _source_id_from_job(existing)
            cycle_time = _cycle_time_from_job(existing)
            payloads: list[tuple[str, dict[str, Any], str | None]] = []
            touched_models: set[str] = set()
            members = _bounded_cohort_members(existing.get("cohort_members"))
            model_rows = self._cycle_rows_by_model_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                model_ids=(str(member.get("model_id") or "") for member in members),
                include_direct_jobs=False,
            )
            for member in members:
                run_id = str(member.get("run_id") or "")
                model_id = str(member.get("model_id") or "")
                if not run_id or not model_id:
                    continue
                snapshot = model_rows.get(model_id)
                hydro = snapshot.hydro_run if snapshot is not None else None
                if isinstance(hydro, Mapping) and str(hydro.get("run_id") or "") != run_id:
                    hydro = None
                if (
                    hydro is None
                    or int(hydro.get("submission_attempt") or 1) != attempt
                    or str(hydro.get("status") or "") not in ACTIVE_HYDRO_STATUSES
                ):
                    continue
                hydro_row = dict(hydro)
                hydro_row.update(
                    {
                        "status": "failed",
                        "error_code": "SLURM_RESERVATION_LOST",
                        "error_message": "Forecast submission was authoritatively absent; this attempt is retryable.",
                        "updated_at": _format_utc(_utcnow()),
                    }
                )
                payloads.append(("hydro_run", hydro_row, model_id))
                touched_models.add(model_id)
            payloads.append(("pipeline_job", cohort_row, _optional_safe_identity(cohort_row, "model_id")))
            next_sequence = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)
            records: list[dict[str, Any]] = []
            for offset, (record_type, payload, model_id) in enumerate(payloads):
                record = _journal_record_for_write(
                    record_type,
                    payload,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                    sequence=next_sequence + offset,
                )
                self._validate_outgoing_record(
                    record,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    record_type=record_type,
                    model_id=model_id,
                )
                records.append(record)
            self._append_journal_records_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                records=records,
            )
            self._write_pipeline_job_direct_unlocked(cohort_row, records[-1])
            for model_id in sorted(touched_models):
                self._materialize_latest_unlocked(
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                    include_direct_jobs=False,
                )
            return len(records)

    def demote_operator_verified_reserved_job(
        self,
        job_id: str,
        *,
        accepted_submit_contract_version: str | None,
        expected_submission_attempt: int,
        expected_submission_attempt_started_at: datetime,
        checked_by: str,
        checked_at: datetime,
        verification_note: str,
    ) -> OperatorDemoteReceipt | None:
        """Atomically demote one operator-verified dead comment-unobservable reservation.

        File-journal-only (#1564).  The cluster does not store job comments
        (#1116), so no automatic reconcile pass can prove absence; an operator
        who independently verified the job dead with Slurm evidence uses this
        typed CAS to convert the narrowly-defined held shape
        (``reserved`` / ``submit_result_ambiguous`` /
        ``slurm_exact_comment`` / ``accounting_unavailable`` /
        ``comment_accounting_unproven``, unbound) into the reclaimable
        ``reservation_lost`` / ``operator_verified_absence`` terminal, with a
        durable audit event carrying the operator evidence.

        Everything is re-read under the cycle lock and every durable field is
        compared, so a concurrent bind / permit / release / demote / reclaim
        that advanced the row before this request obtained the lock loses the
        compare-and-swap and this method writes zero bytes.  Invalid input
        types/enums raise ``FileOrchestrationJournalError`` before any write;
        a stale or mismatched durable row returns ``None`` (never raises).

        On success the returned :class:`OperatorDemoteReceipt` carries the
        exact normalized, secret-redacted operator strings the audit event
        durably recorded, so a CLI can print the receipt without ever
        re-deriving (or leaking) the raw inputs.  ``written_record_count`` is
        the number of durable records appended (master + audit event + any
        active hydro projections).
        """

        if accepted_submit_contract_version != ACCEPTED_SUBMIT_CONTRACT_VERSION:
            raise FileOrchestrationJournalError(
                "file_journal_evidence_enum_invalid",
                field=ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD,
            )
        if type(expected_submission_attempt) is not int or expected_submission_attempt < 1:
            raise FileOrchestrationJournalError(
                "file_journal_evidence_type_invalid", field="expected_submission_attempt"
            )
        if expected_submission_attempt_started_at is None:
            raise FileOrchestrationJournalError(
                "file_journal_evidence_required", field="expected_submission_attempt_started_at"
            )
        normalized_checked_at = _accepted_submit_attempt_anchor(checked_at)
        checked_by_text = _operator_evidence_text(checked_by, field="checked_by")
        verification_note_text = _operator_evidence_text(verification_note, field="verification_note")
        expected_anchor = _accepted_submit_attempt_anchor(expected_submission_attempt_started_at)
        source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._accepted_submit_job_for_id_unlocked(
                job_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            if existing is None:
                return None
            if (
                not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
                or str(existing.get("status") or "") != "reserved"
                or existing.get("slurm_job_id") not in (None, "")
                or existing.get("matched_slurm_job_id") is not None
                or existing.get("submit_outcome") != "submit_result_ambiguous"
                or existing.get("reconciliation_source") != "slurm_exact_comment"
                or existing.get("reconciliation_decision") != "accounting_unavailable"
                or existing.get("reconciliation_reason_class") != "comment_accounting_unproven"
                or existing.get("submission_attempt") != expected_submission_attempt
                or _accepted_submit_attempt_anchor(existing.get("submission_attempt_started_at"))
                != expected_anchor
            ):
                return None
            # The operator's old anchor is only a CAS expectation, never a new
            # authority value: the post-state keeps the persisted attempt and
            # anchor untouched, so reclaim later mints attempt+1 under its own
            # lock-owned anchor.
            cohort_row = apply_accepted_submit_transition(
                existing,
                AcceptedSubmitTransition.accounting(
                    OPERATOR_VERIFIED_ABSENCE_DECISION,
                    submit_outcome="submit_result_ambiguous",
                    status="reservation_lost",
                ),
            )
            cohort_row["updated_at"] = _format_utc(_utcnow())
            attempt = max(int(existing.get("submission_attempt") or 1), 1)
            payloads: list[tuple[str, dict[str, Any], str | None]] = []
            touched_models: set[str] = set()
            members = _bounded_cohort_members(existing.get("cohort_members"))
            model_rows = self._cycle_rows_by_model_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                model_ids=(str(member.get("model_id") or "") for member in members),
                include_direct_jobs=False,
            )
            for member in members:
                run_id = str(member.get("run_id") or "")
                model_id = str(member.get("model_id") or "")
                if not run_id or not model_id:
                    continue
                snapshot = model_rows.get(model_id)
                hydro = snapshot.hydro_run if snapshot is not None else None
                if isinstance(hydro, Mapping) and str(hydro.get("run_id") or "") != run_id:
                    hydro = None
                if (
                    hydro is None
                    or int(hydro.get("submission_attempt") or 1) != attempt
                    or str(hydro.get("status") or "") not in ACTIVE_HYDRO_STATUSES
                ):
                    continue
                hydro_row = dict(hydro)
                hydro_row.update(
                    {
                        "status": "failed",
                        "error_code": "SLURM_RESERVATION_LOST",
                        "error_message": "Operator verified the forecast submission absent; this attempt is retryable.",
                        "updated_at": _format_utc(_utcnow()),
                    }
                )
                payloads.append(("hydro_run", hydro_row, model_id))
                touched_models.add(model_id)
            payloads.append(("pipeline_job", cohort_row, _optional_safe_identity(cohort_row, "model_id")))
            event_id = self._next_accepted_submit_event_id_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
            )
            event = {
                "event_id": event_id,
                "entity_type": "pipeline_job",
                "entity_id": str(cohort_row["job_id"]),
                "event_type": "operator_verified_absence",
                "status_from": "reserved",
                "status_to": "reservation_lost",
                "message": "Operator verified the comment-unobservable reservation absent.",
                "details": {
                    "checked_by": checked_by_text,
                    "checked_at": normalized_checked_at,
                    "verification_note": verification_note_text,
                    "expected_submission_attempt": expected_submission_attempt,
                    "expected_submission_attempt_started_at": expected_anchor,
                    "prior_reconciliation_decision": str(
                        existing.get("reconciliation_decision") or ""
                    ),
                    "prior_reconciliation_reason_class": str(
                        existing.get("reconciliation_reason_class") or ""
                    ),
                },
                "created_at": _format_utc(_utcnow()),
            }
            payloads.append(("pipeline_event", event, None))
            next_sequence = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)
            records: list[dict[str, Any]] = []
            for offset, (record_type, payload, model_id) in enumerate(payloads):
                record = _journal_record_for_write(
                    record_type,
                    payload,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                    sequence=next_sequence + offset,
                )
                self._validate_outgoing_record(
                    record,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    record_type=record_type,
                    model_id=model_id,
                )
                records.append(record)
            self._append_journal_records_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                records=records,
            )
            # The append above is the authority commit point.  Direct/latest
            # files are derived projections that cannot be rolled back with the
            # journal: a failure here is contained to a bounded warning (the
            # operation is already committed), every remaining independent
            # projection is still attempted, and journal replay stays the
            # source of truth for later reads/writes.  Never raise a false
            # operation failure after authority state and audit evidence are
            # durable -- a repeated operator request then loses CAS without
            # appending a duplicate decision.
            warnings: list[ProjectionWarning] = []
            try:
                self._write_pipeline_job_direct_unlocked(cohort_row, records[-2])
            except Exception as error:
                warnings.append(
                    ProjectionWarning(
                        projection="pipeline_job_direct",
                        model_id=None,
                        error_type=_projection_error_type(error),
                        reason=_projection_error_reason(error),
                    )
                )
            for model_id in sorted(touched_models):
                try:
                    self._materialize_latest_unlocked(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        model_id=model_id,
                        include_direct_jobs=False,
                    )
                except Exception as error:
                    warnings.append(
                        ProjectionWarning(
                            projection="latest",
                            model_id=model_id,
                            error_type=_projection_error_type(error),
                            reason=_projection_error_reason(error),
                        )
                    )
            return OperatorDemoteReceipt(
                job_id=str(cohort_row["job_id"]),
                journal_root=str(self.root),
                status_from=str(existing.get("status") or ""),
                status_to=str(cohort_row.get("status") or ""),
                reconciliation_decision=str(cohort_row.get("reconciliation_decision") or ""),
                submission_attempt=attempt,
                submission_attempt_started_at=str(cohort_row.get("submission_attempt_started_at") or ""),
                checked_by=checked_by_text,
                checked_at=normalized_checked_at,
                verification_note=verification_note_text,
                written_record_count=len(records),
                warnings=tuple(warnings),
            )

    def release_identity_blocked_reservation(
        self,
        job_id: str,
        *,
        accepted_submit_contract_version: str | None = None,
        expected_submission_attempt: int | None = None,
        expected_submission_attempt_started_at: datetime | None = None,
        expected_status: str = "reserved",
        identity_blocked_streak: int = 0,
    ) -> int:
        """Abandon one reservation whose identity stayed unverifiable for too long.

        Dedicated typed API on purpose: the generic evidence transition may not
        change master status and its decision whitelist stays closed. The CAS
        discipline mirrors :meth:`permit_pipeline_job_retry` (expected attempt,
        attempt anchor, expected status, unbound required), so a concurrently
        advanced attempt loses the race and the caller keeps its blocked outcome.

        The released row is a deliberate non-reclaimable terminal: reclaim
        accepts exactly two lower-level decisions -- ``absence_retry_permitted``
        and the #1564 ``operator_verified_absence`` -- and an
        ``identity_mismatch_released`` row matches neither, so this idempotency
        key is spent.  Liveness comes from the next attempt minting a
        retry-suffixed key.
        """

        if accepted_submit_contract_version != ACCEPTED_SUBMIT_CONTRACT_VERSION:
            raise FileOrchestrationJournalError(
                "file_journal_evidence_enum_invalid",
                field=ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD,
            )
        if type(expected_submission_attempt) is not int or expected_submission_attempt < 1:
            raise FileOrchestrationJournalError(
                "file_journal_evidence_type_invalid", field="expected_submission_attempt"
            )
        if type(identity_blocked_streak) is not int or identity_blocked_streak < 1:
            raise FileOrchestrationJournalError(
                "file_journal_evidence_type_invalid", field="identity_blocked_streak"
            )
        source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._accepted_submit_job_for_id_unlocked(
                job_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            if existing is None or str(existing.get("status") or "") != expected_status:
                return 0
            if (
                not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
            ):
                return 0
            if expected_submission_attempt_started_at is None:
                return 0
            try:
                anchor_matches = _accepted_submit_attempt_anchor(
                    existing.get("submission_attempt_started_at")
                ) == _accepted_submit_attempt_anchor(expected_submission_attempt_started_at)
            except FileOrchestrationJournalError:
                return 0
            if not anchor_matches:
                return 0
            if existing.get("submission_attempt") != expected_submission_attempt:
                return 0
            if existing.get("slurm_job_id") not in (None, ""):
                return 0
            # Released rows stay outside automatic retry BECAUSE this transition adds no
            # ``error_code``: stamping a transient one here (SLURM_RESERVATION_LOST is on
            # the transient list) would turn every release into an automatic duplicate
            # submission (spec: candidate-projection-stage-attempt-retention).
            row = apply_accepted_submit_transition(
                existing,
                AcceptedSubmitTransition.accounting(
                    IDENTITY_MISMATCH_RELEASED_DECISION,
                    submit_outcome="submit_result_ambiguous",
                    status="reservation_lost",
                    identity_blocked_streak=identity_blocked_streak,
                ),
            )
            row["updated_at"] = _format_utc(_utcnow())
            model_id = _optional_safe_identity(row, "model_id")
            written = self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=model_id)
            if written is None:
                return 0
            # #1748: the released terminal is otherwise SILENT -- the row is
            # indistinguishable from an ordinary in-flight reservation until a
            # human happens to read the journal, and nothing automatic can
            # revive it (that is deliberate, see the error_code comment above).
            # This is the single release write point, so emitting here is
            # exactly-once per release and covers BOTH prior states (a fresh
            # reservation and a reclaim re-seed).  Emitting at the
            # ``reconcile.py`` caller instead would double-emit, since it is the
            # sole caller.  Gated on the successful write so a CAS refusal above
            # stays write-free and signal-free.
            # The release bytes are already appended and ``_locked_cycle_write``
            # has NO rollback, so a raise here would leave the row durably
            # released with no operator record -- permanently, because the
            # reconcile loop iterates ``query_reserved_unbound_jobs()`` and
            # never re-enters this door for a ``reservation_lost`` row.  Same
            # best-effort discipline as ``_record_permanent_failure_mark_failure``:
            # the evidence write exists to keep the terminal from being silent,
            # never to turn a correct durable release into a raise.  The catch
            # spans the filesystem/OS class too: the private-recovery side write
            # raises ``SafeFilesystemError``/``OSError``, which ``reconcile.py``'s
            # enclosing ``except FileOrchestrationJournalError`` would NOT catch
            # -- it would abort the whole reconcile pass.
            try:
                self._append_pipeline_job_event_unlocked(
                    row,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    event_type=IDENTITY_RELEASED_OPERATOR_SIGNAL,
                    status_from=expected_status,
                    status_to="reservation_lost",
                    message=(
                        f"{IDENTITY_RELEASED_OPERATOR_SIGNAL} pipeline job {job_id} released an "
                        "identity-blocked reservation; the cohort is a permanent terminal until an "
                        "operator invokes recover_released_identity_blocked_reservation."
                    ),
                    details={
                        "pipeline_job_id": job_id,
                        "cohort_digest": row.get("cohort_digest"),
                        "identity_blocked_streak": identity_blocked_streak,
                        "recovery_api": "recover_released_identity_blocked_reservation",
                        # Both the API and the surface a human can actually
                        # reach: naming only the former is the round-3 defect.
                        "operator_command": _released_reservation_recovery_command(job_id),
                    },
                )
            except (OrchestratorError, FileOrchestrationJournalError, SafeFilesystemError, OSError) as error:
                self._record_identity_released_signal_failure(
                    job_id,
                    row,
                    error,
                    source_id=source_id,
                    cycle_time=cycle_time,
                )
            return 1

    def _record_identity_released_signal_failure(
        self,
        job_id: str,
        row: Mapping[str, Any],
        error: Exception,
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> None:
        """Leave a degraded trace when the #1748 operator signal could not be written.

        #1312 C-P2: the fallback must not be silent.  The payload is kept
        minimal on purpose -- ``file_journal_byte_limit_exceeded`` is one of the
        realistic reasons the primary emission fails, and a smaller record still
        fits -- but it keeps ``recovery_api`` so the degraded trace still tells
        an operator what to invoke.  If this write fails too (a filesystem fault
        that killed the primary very likely kills this one), it degrades to a
        log and still does not raise.

        Note: ``_materialize_latest_unlocked`` runs AFTER the append inside
        ``_append_validated_record_unlocked``, so a materialize fault means the
        primary event did land and this trace is redundant.  Noisy beats silent.
        """

        reason = str(
            getattr(error, "reason", None) or getattr(error, "error_code", None) or type(error).__name__
        )
        try:
            self._append_pipeline_job_event_unlocked(
                row,
                source_id=source_id,
                cycle_time=cycle_time,
                event_type=IDENTITY_RELEASED_SIGNAL_FAILED_EVENT,
                status_from="reservation_lost",
                status_to="reservation_lost",
                message=(
                    f"{IDENTITY_RELEASED_OPERATOR_SIGNAL} could not be written for pipeline job "
                    f"{job_id} (reason={reason}); the row IS released and needs "
                    "recover_released_identity_blocked_reservation."
                ),
                details={
                    "pipeline_job_id": job_id,
                    "reason": reason,
                    "error_type": type(error).__name__,
                    "recovery_api": "recover_released_identity_blocked_reservation",
                    # The degraded trace is exactly when a human is reading by
                    # hand, so it needs the runnable command more, not less.
                    "operator_command": _released_reservation_recovery_command(job_id),
                },
            )
        except (OrchestratorError, FileOrchestrationJournalError, SafeFilesystemError, OSError):
            LOGGER.warning(
                "%s could not be written for pipeline job %s (reason=%s) and neither could its "
                "failure trace; the row IS released and needs "
                "recover_released_identity_blocked_reservation.",
                IDENTITY_RELEASED_OPERATOR_SIGNAL,
                job_id,
                reason,
            )

    def _append_pipeline_job_event_unlocked(
        self,
        row: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
        event_type: str,
        status_from: str | None,
        status_to: str | None,
        message: str | None,
        details: Mapping[str, Any],
        materialize: bool = True,
    ) -> None:
        """Append one pipeline_job event from inside an open cycle write window.

        ``insert_pipeline_event`` cannot be reused here: it re-enters
        ``_locked_cycle_write``, whose ``finally`` clears ``_cycle_write_owner``
        for the still-open outer window and whose second ``flock`` on the same
        cycle lock file would block this thread against itself.
        """

        model_id = _optional_safe_identity(row, "model_id")
        event = {
            "event_id": self._next_event_id_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
            ),
            "entity_type": "pipeline_job",
            "entity_id": _required_safe_identity(row, "job_id"),
            "event_type": event_type,
            "status_from": status_from,
            "status_to": status_to,
            "message": message,
            "details": dict(details),
            "created_at": _format_utc(_utcnow()),
        }
        self._append_validated_record_unlocked(
            "pipeline_event",
            event,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            materialize_model_id=model_id if materialize else None,
        )

    def recover_released_identity_blocked_reservation(
        self,
        job_id: str,
        *,
        accepted_submit_contract_version: str | None = None,
        expected_submission_attempt: int | None = None,
        expected_submission_attempt_started_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Operator-gated liveness for one released identity-blocked cohort (#1748).

        The released row is a permanent terminal for every AUTOMATIC arm: the
        release deliberately withholds ``error_code`` so ``should_auto_retry`` is
        false by construction, and lower-level reclaim accepts exactly two
        absence decisions -- ``absence_retry_permitted`` and the
        typed-demotion-only ``operator_verified_absence``.  This released row
        carries ``identity_mismatch_released``, matches neither, and reaches
        liveness only through the separate operator-attestation disjunct.  This
        door records a durable operator attestation ON THE RELEASED ROW and
        writes **no** successor pipeline-job row.

        Why a marker and not a row: the successor identity the ordinary retry
        path mints is exactly ``_next_current_master_retry_identity``'s, so
        pre-materializing it occupies that ``job_id``/idempotency key,
        ``_pipeline_job_conflicts_unlocked`` then refuses the ordinary reserve,
        reclaim refuses too (the row is ``pending``, not ``reservation_lost``),
        and the pass takes ``_skip_duplicate_submission`` on every later pass --
        permanently inert.  The recovery's only durable output must be an INPUT
        the ordinary submission path already consumes; here that consumer is the
        additive disjunct in
        ``chain_forecast_orchestrator_cycle._terminal_stage_needs_manual_retry``.

        This performs **no** Slurm-side liveness or absence check and must not be
        described as one.  On a cluster whose accounting does not store job
        comments absence is not provable; invoking this is an operator
        attestation, and the judgement deliberately sits with the operator.  For
        the same reason there is no automatic caller, and no automatic path can
        set the marker (it is outside ``_PIPELINE_JOB_UPSERT_MUTABLE_FIELDS`` and
        rejected by the clean-reservation check).

        ``should_auto_retry`` is never consulted and no ``error_code`` is written.

        Returns the attested row, or ``None`` when the call is refused.  Refusal
        is write-free, and a repeat attestation is an idempotent no-op.
        """

        if accepted_submit_contract_version != ACCEPTED_SUBMIT_CONTRACT_VERSION:
            raise FileOrchestrationJournalError(
                "file_journal_evidence_enum_invalid",
                field=ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD,
            )
        if type(expected_submission_attempt) is not int or expected_submission_attempt < 1:
            raise FileOrchestrationJournalError(
                "file_journal_evidence_type_invalid", field="expected_submission_attempt"
            )
        source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._accepted_submit_job_for_id_unlocked(
                job_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            if existing is None:
                return None
            if (
                not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
            ):
                return None
            if str(existing.get("status") or "") != "reservation_lost":
                return None
            if existing.get("reconciliation_decision") != IDENTITY_MISMATCH_RELEASED_DECISION:
                return None
            if existing.get("slurm_job_id") not in (None, "") or existing.get(
                "matched_slurm_job_id"
            ) not in (None, ""):
                return None
            # CAS mirrors ``release_identity_blocked_reservation``: a
            # concurrently advanced attempt loses the race, write-free.
            if expected_submission_attempt_started_at is None:
                return None
            try:
                anchor_matches = _accepted_submit_attempt_anchor(
                    existing.get("submission_attempt_started_at")
                ) == _accepted_submit_attempt_anchor(expected_submission_attempt_started_at)
            except FileOrchestrationJournalError:
                return None
            if not anchor_matches:
                return None
            if existing.get("submission_attempt") != expected_submission_attempt:
                return None
            if existing.get(OPERATOR_RECOVERY_ATTESTATION_FIELD) not in (None, ""):
                # Idempotent: a second attestation leaves the row byte-identical
                # and emits nothing.  At most one recovered attempt still
                # follows, because the ordinary reservation path's own conflict
                # gate owns that exclusion, not this call.
                return _public_scheduler_row(existing)
            row = {
                **existing,
                OPERATOR_RECOVERY_ATTESTATION_FIELD: _format_utc(_utcnow()),
                "updated_at": _format_utc(_utcnow()),
            }
            return self._write_pipeline_job_unlocked(
                self._pipeline_job_row(row),
                exclusive_direct=False,
                model_id=_optional_safe_identity(row, "model_id"),
            )

    def project_forecast_cohort_tasks(
        self,
        job_id: str,
        *,
        master_slurm_job_id: str,
        projections: Sequence[Mapping[str, Any]],
        complete: bool,
        master_status: str,
        master_error_code: str | None,
        reconciliation_decision: str,
        finished_at: datetime | None = None,
        exit_code: int | None = None,
        master_error_message: str | None = None,
        log_uri: str | None = None,
    ) -> dict[str, int]:
        """Project one accounting pass under one cycle lock and one materialization/model."""
        if type(master_status) is not str or master_status not in {
            "succeeded",
            "partially_failed",
            "failed",
            "cancelled",
        }:
            raise FileOrchestrationJournalError(
                "file_journal_evidence_enum_invalid", field="master_status"
            )
        # Writer-authority closed world (#1564 D8): the operator decision is
        # never a raw caller-supplied cohort-projection decision, on either the
        # bound or the mismatched-ID branch.  Refuse before any source parsing,
        # lock acquisition, mutation, or event.
        if reconciliation_decision == OPERATOR_VERIFIED_ABSENCE_DECISION:
            raise FileOrchestrationJournalError(
                "file_journal_authority_transition_requires_typed_api",
                field="reconciliation_decision",
            )
        del master_status, master_error_code
        source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._accepted_submit_job_for_id_unlocked(
                job_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            if (
                existing is None
                or not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
            ):
                return {"total": 0, "pipeline_status": 0, "pipeline_event": 0}
            if str(existing.get("slurm_job_id") or "") != master_slurm_job_id:
                result = self._defer_forecast_cohort_projection_unlocked(
                    existing,
                    reconciliation_decision="identity_mismatch_blocked",
                    reconciliation_reason_class=None,
                    error_code="SLURM_MASTER_IDENTITY_MISMATCH",
                    error_message="terminal Slurm master id did not match the durable accepted submit",
                    finished_at=finished_at,
                    exit_code=exit_code,
                    log_uri=log_uri,
                )
                return {
                    "total": 1 if result.wrote else 0,
                    "pipeline_status": 1 if result.wrote else 0,
                    "pipeline_event": 0,
                }
            if reconciliation_decision != "matched_bound":
                raise FileOrchestrationJournalError(
                    "file_journal_evidence_enum_invalid", field="reconciliation_decision"
                )
            existing_projections = {
                int(item["array_task_id"]): dict(item)
                for item in _bounded_candidate_projections(existing.get("candidate_projections"))
                if str(item.get("array_task_id", "")).isdigit()
            }
            if len(projections) > MAX_FORECAST_COHORT_MEMBERS:
                raise FileOrchestrationJournalError(
                    "file_journal_evidence_limit_exceeded", field="candidate_projections"
                )
            members = {
                int(member["array_task_id"]): dict(member)
                for member in ordered_cohort_members(existing.get("cohort_members"))
            }
            normalized_projections: list[dict[str, Any]] = []
            seen_task_ids: set[int] = set()
            for projection in projections:
                raw_task_id = projection.get("array_task_id")
                if type(raw_task_id) is not int or raw_task_id in seen_task_ids:
                    raise FileOrchestrationJournalError(
                        "file_journal_task_identity_mismatch",
                        field="array_task_id",
                    )
                seen_task_ids.add(raw_task_id)
                bounded = _bounded_candidate_projections([projection])
                try:
                    item = normalize_candidate_projections(
                        bounded,
                        cohort_members=existing.get("cohort_members"),
                    )[0]
                except (AcceptedSubmitEvidenceError, IndexError) as error:
                    raise FileOrchestrationJournalError(
                        "file_journal_task_identity_mismatch",
                        field=getattr(error, "field", "candidate_projections"),
                    ) from error
                outcome = item.get("array_task_outcome")
                task_slurm_job_id = projection.get("task_slurm_job_id")
                expected_task_slurm_job_id = f"{master_slurm_job_id}_{raw_task_id}"
                if outcome in {"succeeded", "failed"} and str(task_slurm_job_id or "") != expected_task_slurm_job_id:
                    raise FileOrchestrationJournalError(
                        "file_journal_task_identity_mismatch",
                        field="task_slurm_job_id",
                    )
                if task_slurm_job_id not in (None, "") and str(task_slurm_job_id) != expected_task_slurm_job_id:
                    raise FileOrchestrationJournalError(
                        "file_journal_task_identity_mismatch",
                        field="task_slurm_job_id",
                    )
                normalized_projections.append(
                    {
                        **item,
                        "task_slurm_job_id": task_slurm_job_id,
                        "error_code": projection.get("error_code"),
                    }
                )
            if seen_task_ids != set(members) or (
                complete
                and any(
                    item.get("array_task_outcome") not in {"succeeded", "failed"}
                    for item in normalized_projections
                )
            ):
                raise FileOrchestrationJournalError(
                    "file_journal_task_identity_mismatch",
                    field="candidate_projections",
                )
            if not complete:
                result = self._defer_forecast_cohort_projection_unlocked(
                    existing,
                    reconciliation_decision="accounting_unavailable",
                    reconciliation_reason_class="coverage_incomplete",
                    error_code="SLURM_TASK_ACCOUNTING_INCOMPLETE",
                    error_message="terminal Slurm array task accounting was incomplete",
                    finished_at=finished_at,
                    exit_code=exit_code,
                    log_uri=log_uri,
                )
                return {
                    "total": 1 if result.wrote else 0,
                    "pipeline_status": 1 if result.wrote else 0,
                    "pipeline_event": 0,
                }
            succeeded_tasks = sum(
                item.get("array_task_outcome") == "succeeded"
                for item in normalized_projections
            )
            if succeeded_tasks == len(normalized_projections):
                projected_master_status = "succeeded"
                projected_master_error_code = None
            elif succeeded_tasks == 0:
                projected_master_status = "failed"
                projected_master_error_code = next(
                    (
                        str(item["error_code"])
                        for item in normalized_projections
                        if item.get("error_code") not in (None, "")
                    ),
                    "SLURM_ARRAY_TASK_FAILED",
                )
            else:
                projected_master_status = "partially_failed"
                projected_master_error_code = next(
                    (
                        str(item["error_code"])
                        for item in normalized_projections
                        if item.get("error_code") not in (None, "")
                    ),
                    "SLURM_ARRAY_TASK_FAILED",
                )
            verified: list[dict[str, Any]] = []
            for projection in sorted(normalized_projections, key=lambda item: int(item["array_task_id"])):
                if projection.get("array_task_outcome") not in {"succeeded", "failed"}:
                    continue
                task_id = int(projection["array_task_id"])
                previous = existing_projections.get(task_id)
                if previous is not None and previous.get("array_task_outcome") in {"succeeded", "failed"}:
                    continue
                existing_projections[task_id] = {
                    key: projection.get(key)
                    for key in ACCEPTED_PROJECTION_FIELDS
                }
                verified.append(projection)

            payloads: list[tuple[str, dict[str, Any], str | None]] = []
            direct_jobs: list[dict[str, Any]] = []
            touched_models: set[str] = set()
            event_id = self._next_accepted_submit_event_id_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
            )
            model_rows = self._cycle_rows_by_model_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                model_ids=(
                    str(projection["model_id"])
                    for projection in verified
                    if projection.get("model_id") not in (None, "")
                ),
                include_direct_jobs=False,
            )
            for projection in verified:
                run_id = str(projection.get("run_id") or "")
                model_id = str(projection.get("model_id") or "")
                candidate_id = str(projection.get("candidate_id") or "")
                task_id = int(projection["array_task_id"])
                if not run_id or not model_id or not candidate_id:
                    continue
                task_status = str(projection["array_task_outcome"])
                task_status = "succeeded" if task_status == "succeeded" else "failed"
                candidate_job_id = f"job_{run_id}_forecast_reconciled_{master_slurm_job_id}_{task_id}"
                # Forward accounting (#1183): the master row captured every
                # task's warm-start identity at reservation time, when the
                # planning context still existed.  This accounting pass owns no
                # planning context, so it copies this task's entry verbatim;
                # pre-change masters recorded none and yield an empty map.
                task_init_state_identity = init_state_identity_for_task(
                    existing.get(INIT_STATE_IDENTITY_FIELD),
                    task_id,
                )
                candidate_job = self._pipeline_job_row(
                    {
                        "job_id": candidate_job_id,
                        "run_id": run_id,
                        "cycle_id": existing["cycle_id"],
                        "job_type": "run_shud_forecast_array",
                        "slurm_job_id": str(projection.get("task_slurm_job_id") or f"{master_slurm_job_id}_{task_id}"),
                        "array_task_id": task_id,
                        "model_id": model_id,
                        "status": task_status,
                        "stage": "forecast",
                        "candidate_id": candidate_id,
                        "error_code": None if task_status == "succeeded" else projection.get("error_code"),
                        "submit_outcome": "accepted",
                        "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
                        INIT_STATE_IDENTITY_FIELD: (
                            [task_init_state_identity] if task_init_state_identity is not None else []
                        ),
                        "restart_stage": projection.get("restart_stage"),
                        "native_shud_resubmitted": False,
                    }
                )
                payloads.append(("pipeline_job", candidate_job, model_id))
                direct_jobs.append(candidate_job)
                event = {
                    "event_id": event_id,
                    "entity_type": "pipeline_job",
                    "entity_id": candidate_job_id,
                    "event_type": "array_task_reconciled",
                    "status_from": "reconciling",
                    "status_to": task_status,
                    "message": None,
                    "details": _bounded_candidate_projections([projection])[0],
                    "created_at": _format_utc(_utcnow()),
                }
                event_id += 1
                payloads.append(("pipeline_event", event, model_id))
                model_snapshot = model_rows.get(model_id)
                hydro = model_snapshot.hydro_run if model_snapshot is not None else None
                if isinstance(hydro, Mapping) and str(hydro.get("run_id") or "") != run_id:
                    hydro = None
                hydro_status = str(hydro.get("status") or "") if isinstance(hydro, Mapping) else ""
                hydro_error_code = hydro.get("error_code") if isinstance(hydro, Mapping) else None
                hydro_is_retryable = (
                    hydro_status in ACTIVE_HYDRO_STATUSES
                    or (
                        hydro_status == "failed"
                        and hydro_error_code in {"SLURM_GATEWAY_UNAVAILABLE", "SLURM_RESERVATION_LOST"}
                    )
                )
                if (
                    task_status == "succeeded"
                    and isinstance(hydro, Mapping)
                    and hydro_is_retryable
                    and hydro_error_code in {None, "SLURM_GATEWAY_UNAVAILABLE", "SLURM_RESERVATION_LOST"}
                ):
                    hydro_row = dict(hydro)
                    hydro_row.update(
                        {
                            "status": "succeeded",
                            "slurm_job_id": candidate_job["slurm_job_id"],
                            "error_code": None,
                            "error_message": None,
                            "updated_at": _format_utc(_utcnow()),
                        }
                    )
                    payloads.append(("hydro_run", hydro_row, model_id))
                elif (
                    task_status == "failed"
                    and isinstance(hydro, Mapping)
                    and hydro_is_retryable
                    and hydro_error_code in {None, "SLURM_GATEWAY_UNAVAILABLE", "SLURM_RESERVATION_LOST"}
                ):
                    hydro_row = dict(hydro)
                    hydro_row.update(
                        {
                            "status": "failed",
                            "slurm_job_id": candidate_job["slurm_job_id"],
                            "error_code": projection.get("error_code") or "SLURM_JOB_FAILED",
                            "error_message": "terminal Slurm array task failed",
                            "updated_at": _format_utc(_utcnow()),
                        }
                    )
                    payloads.append(("hydro_run", hydro_row, model_id))
                touched_models.add(model_id)

            # Terminal stickiness (#1312, widened by #1589/#1629): a master
            # whose persisted status the projection cannot derive keeps that
            # status, because the projection owns exactly
            # ``PROJECTION_DERIVED_MASTER_STATUSES`` and must not overwrite a
            # terminal truth it has no authority to produce.  Under the routed
            # domain that means ``permanently_failed`` and ``cancelled``.  The
            # attribution family sticks ONLY for ``permanently_failed``: a row
            # saying "permanently failed" while its error code is recomputed by
            # every pass is self-contradictory.  A ``cancelled`` row gets status
            # stickiness only -- it still takes the current task-derived error
            # family and refreshes ``candidate_projections`` plus the
            # observational family (``finished_at`` / ``exit_code`` /
            # ``log_uri``), because status preservation must not become a
            # whole-row freeze.  Refreshing observational evidence under the
            # mark is intended behaviour and contradicts nothing, so it is
            # deliberately NOT frozen (the defer path's whole-row short circuit
            # is the rejected alternative).
            #
            # The trigger is "terminal status outside the derived domain":
            # widening to the derived values would pin the first pass's
            # conclusion forever, i.e. disable the projection, and widening to
            # every non-derived status (including live ``reserved``/``submitted``
            # rows) would freeze a bound master before the projection could ever
            # derive its terminal outcome.  ``submission_failed`` and
            # ``reservation_lost`` are terminal yet still rejected before this
            # projection by their submit-outcome/inventory gates, so they are
            # never rewritten through a ``matched_bound`` accounting tuple; the
            # terminal-membership predicate never sees them as ``existing``.
            persisted_status = str(existing.get("status") or "")
            # The derived domain is the *only* set this projection can overwrite:
            # sticky means "the projection cannot derive this status", and the
            # projection owns no non-terminal status (a ``reserved``/``submitted``
            # bound master still derives its terminal outcome here).  Requiring
            # terminal membership keeps the predicate from pinning a live row.
            master_is_sticky = (
                persisted_status in TERMINAL_PIPELINE_STATUSES
                and persisted_status not in PROJECTION_DERIVED_MASTER_STATUSES
            )
            master_is_permanently_failed = persisted_status == "permanently_failed"
            sticky_master_status = persisted_status if master_is_sticky else projected_master_status
            cohort_row = apply_accepted_submit_transition(
                existing,
                AcceptedSubmitTransition.accounting(
                    reconciliation_decision,
                    submit_outcome="accepted",
                    matched_slurm_job_id=master_slurm_job_id,
                    status=sticky_master_status,
                    # #1850 Fix B: a complete matched-bound terminal projection
                    # derives the legal current source from immutable binding
                    # provenance instead of the exact-comment factory default,
                    # so a name-window fallback-bound row keeps/restores its
                    # name-window source. Binding provenance itself is carried
                    # by ``apply_accepted_submit_transition`` (the transition
                    # carries no new binding fields, so the prior values
                    # survive).
                    reconciliation_source=matched_bound_reconciliation_source(
                        existing.get(SLURM_BINDING_SOURCE_FIELD)
                    ),
                ),
            )
            cohort_row.update(
                {
                    "candidate_projections": [
                        existing_projections[task_id] for task_id in sorted(existing_projections)
                    ],
                    "error_code": (
                        existing.get("error_code")
                        if master_is_permanently_failed
                        else projected_master_error_code
                    ),
                    "updated_at": _format_utc(_utcnow()),
                }
            )
            # #1589 (design D3): a display placeholder is a withheld value, not
            # an instruction to overwrite.  Resolving the caller's evidence
            # restores what these ``is not None`` guards were meant to ask --
            # "did the caller supply a real value?" -- so a round-tripped public
            # row can no longer displace a real durable value that the write
            # boundary would then withhold, losing it outright.  Resolved here,
            # above the guards, which also puts it above the ``cohort_changed``
            # comparison below.
            finished_at = _resolved_caller_evidence(finished_at)
            exit_code = _resolved_caller_evidence(exit_code)
            master_error_message = _resolved_caller_evidence(master_error_message)
            log_uri = _resolved_caller_evidence(log_uri)
            if finished_at is not None:
                cohort_row["finished_at"] = _format_utc(finished_at)
            if exit_code is not None:
                cohort_row["exit_code"] = exit_code
            if master_error_message is not None and not master_is_permanently_failed:
                cohort_row["error_message"] = _durable_error_message(master_error_message)
            if log_uri is not None:
                cohort_row["log_uri"] = log_uri
            cohort_changed = any(
                cohort_row.get(key) != existing.get(key)
                for key in (
                    "candidate_projections",
                    "status",
                    "error_code",
                    "reconciliation_source",
                    "reconciliation_decision",
                    "matched_slurm_job_id",
                    "finished_at",
                    "exit_code",
                    "error_message",
                    "log_uri",
                )
            )
            if cohort_changed:
                payloads.append(("pipeline_job", cohort_row, _optional_safe_identity(cohort_row, "model_id")))
                payloads.append(
                    (
                        "pipeline_event",
                        {
                            "event_id": event_id,
                            "entity_type": "pipeline_job",
                            "entity_id": str(cohort_row["job_id"]),
                            "event_type": "status_change",
                            "status_from": str(existing.get("status") or "") or None,
                            "status_to": str(cohort_row.get("status") or "") or None,
                            "message": "forecast cohort terminal projection committed",
                            "details": {
                                "slurm_job_id": master_slurm_job_id,
                                "projection_complete": complete,
                                "projected_task_count": len(existing_projections),
                            },
                            "created_at": _format_utc(_utcnow()),
                        },
                        None,
                    )
                )
            if not payloads:
                return {"total": 0, "pipeline_status": 0, "pipeline_event": 0}

            next_sequence = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)
            records: list[dict[str, Any]] = []
            for offset, (record_type, payload, model_id) in enumerate(payloads):
                record = _journal_record_for_write(
                    record_type,
                    payload,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                    sequence=next_sequence + offset,
                )
                self._validate_outgoing_record(
                    record,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    record_type=record_type,
                    model_id=model_id,
                )
                records.append(record)
            self._append_journal_records_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                records=records,
            )
            materialization_next_sequence = next_sequence + len(records)
            self._apply_records_to_model_rows(
                model_rows,
                records,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            source_segments = _cycle_read_source_segments(
                source_id=source_id,
                source_segment_override=None,
                root=self.root,
            )
            cycle_segment = format_cycle_time(cycle_time)
            for model_id, rows in model_rows.items():
                rows.pipeline_events = _dedupe_events(rows.pipeline_events)
                self._cache_cycle_rows(
                    (source_id, cycle_segment, model_id, source_segments),
                    rows,
                    fingerprint=None,
                )
            pipeline_records = [record for record in records if str(record.get("record_type") or "") == "pipeline_job"]
            for record, direct_job in zip(pipeline_records[: len(direct_jobs)], direct_jobs, strict=True):
                self._write_pipeline_job_direct_unlocked(direct_job, record)
            if cohort_changed:
                self._write_pipeline_job_direct_unlocked(cohort_row, pipeline_records[-1])
            for model_id in sorted(touched_models):
                self._materialize_latest_unlocked(
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                    next_sequence=materialization_next_sequence,
                )
            event_writes = sum(record_type == "pipeline_event" for record_type, _payload, _model in payloads)
            return {
                "total": len(records),
                "pipeline_status": len(records) - event_writes,
                "pipeline_event": event_writes,
            }

    def defer_forecast_cohort_projection(
        self,
        job_id: str,
        *,
        reconciliation_decision: str,
        reconciliation_reason_class: str | None,
        error_code: str,
        error_message: str,
        finished_at: datetime | None = None,
        exit_code: int | None = None,
        log_uri: str | None = None,
    ) -> AcceptedSubmitCommitResult:
        """Fail one current forecast projection closed without touching cohort members."""

        # Writer-authority closed world (#1564 D8): the operator decision is
        # never a raw caller-supplied defer decision; only the dedicated typed
        # demotion may write it.  Refuse before any mutation.
        if reconciliation_decision == OPERATOR_VERIFIED_ABSENCE_DECISION:
            raise FileOrchestrationJournalError(
                "file_journal_authority_transition_requires_typed_api",
                field="reconciliation_decision",
            )
        source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._accepted_submit_job_for_id_unlocked(
                job_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            if (
                existing is None
                or not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing or {}))
            return self._defer_forecast_cohort_projection_unlocked(
                existing,
                reconciliation_decision=reconciliation_decision,
                reconciliation_reason_class=reconciliation_reason_class,
                error_code=error_code,
                error_message=error_message,
                finished_at=finished_at,
                exit_code=exit_code,
                log_uri=log_uri,
            )

    def _defer_forecast_cohort_projection_unlocked(
        self,
        existing: Mapping[str, Any],
        *,
        reconciliation_decision: str,
        reconciliation_reason_class: str | None,
        error_code: str,
        error_message: str,
        finished_at: datetime | None,
        exit_code: int | None,
        log_uri: str | None,
    ) -> AcceptedSubmitCommitResult:
        current_status = str(existing.get("status") or "")
        if current_status in TERMINAL_PIPELINE_STATUSES:
            return AcceptedSubmitCommitResult("idempotent", dict(existing))
        if (
            existing.get("submit_outcome") != "accepted"
            or not str(existing.get("slurm_job_id") or "").isdigit()
        ):
            return AcceptedSubmitCommitResult("stale", dict(existing))
        row = apply_accepted_submit_transition(
            existing,
            AcceptedSubmitTransition.accounting(
                reconciliation_decision,
                submit_outcome="accepted",
                reconciliation_reason_class=reconciliation_reason_class,
                status="reconcile_unverified",
                # #1850 Fix B: a defer/incomplete-projection transition reasserts
                # the held tuple with the EXACT-COMMENT current source and no
                # matched id, but must retain the attempt's immutable binding
                # provenance. ``apply_accepted_submit_transition`` carries the
                # prior binding fields through because this transition sets none.
                reconciliation_source="slurm_exact_comment",
            ),
        )
        # #1589 (design D3): same rule as the batched projection leg -- a
        # display placeholder is a withheld value, not an overwrite.  This leg
        # is separately reachable: pass 1 parks the row on
        # ``reconcile_unverified``, which is not terminal, so the whole-row
        # short circuit above does not stop a pass 2 carrying a placeholder.
        # Everything below is resolved above the ``changed_fields`` equality
        # gate, so the value compared is the value persisted.
        #
        # The leg has TWO regimes and they take different resolutions.  The
        # error family here is written UNCONDITIONALLY, so declining is not an
        # option and a withheld value resolving to ``None`` would destroy the
        # persisted one -- and the gate would then compare a raw placeholder
        # against erased durable state and never converge, appending a record
        # per replay.  Hence ``durable=``, resolved BEFORE the
        # ``_durable_error_message`` wrap, exactly as
        # ``complete_pipeline_job_cancellation`` does it.  The trio below is
        # guarded by ``is not None``, where ``None`` makes the guard decline and
        # declining IS keeping, so it takes no ``durable=``.
        resolved_error_code = _resolved_caller_evidence(error_code, durable=existing.get("error_code"))
        resolved_error_message = _durable_error_message(
            _resolved_caller_evidence(error_message, durable=existing.get("error_message"))
        )
        row.update(
            {
                "status": "reconcile_unverified",
                "error_code": resolved_error_code,
                "error_message": resolved_error_message,
                "updated_at": _format_utc(_utcnow()),
            }
        )
        finished_at = _resolved_caller_evidence(finished_at)
        exit_code = _resolved_caller_evidence(exit_code)
        log_uri = _resolved_caller_evidence(log_uri)
        if finished_at is not None:
            row["finished_at"] = _format_utc(finished_at)
        if exit_code is not None:
            row["exit_code"] = exit_code
        if log_uri is not None:
            row["log_uri"] = log_uri
        changed_fields = (
            "status",
            "reconciliation_source",
            "reconciliation_decision",
            "reconciliation_reason_class",
            "matched_slurm_job_id",
            "error_code",
            "error_message",
            "finished_at",
            "exit_code",
            "log_uri",
        )
        if all(row.get(field) == existing.get(field) for field in changed_fields):
            return AcceptedSubmitCommitResult("idempotent", dict(existing))
        written = self._write_pipeline_job_unlocked(
            row,
            exclusive_direct=False,
            model_id=None,
        )
        return AcceptedSubmitCommitResult("applied", written)

    def update_pipeline_job_status(
        self,
        job_id: str,
        status: str,
        *,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        exit_code: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        log_uri: str | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        initial = self._pipeline_job_for_id_unlocked(job_id)
        if initial is None:
            raise OrchestratorError("PIPELINE_JOB_NOT_FOUND", f"pipeline_job not found: {job_id}")
        if accepted_submit_contract_is_current(initial) and accepted_submit_row_kind(initial) == "master":
            raise FileOrchestrationJournalError(
                "file_journal_authority_transition_requires_typed_api",
                field="status",
            )
        source_id = _source_id_from_job(initial)
        cycle_time = _cycle_time_from_job(initial)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._pipeline_job_for_id_unlocked(job_id)
            if existing is None:
                raise OrchestratorError("PIPELINE_JOB_NOT_FOUND", f"pipeline_job not found: {job_id}")
            if accepted_submit_contract_is_current(existing) and accepted_submit_row_kind(existing) == "master":
                raise FileOrchestrationJournalError(
                    "file_journal_authority_transition_requires_typed_api",
                    field="status",
                )
            previous_status = str(existing.get("status") or "") or None
            terminal_guarded = previous_status in {"succeeded", "failed", "cancelled"} and status not in {
                "partially_failed",
                "permanently_failed",
            }
            if previous_status == "permanently_failed" or terminal_guarded:
                return previous_status, _public_scheduler_row(existing)
            row = dict(existing)
            row["status"] = status
            # #1589 (design D3): resolve before the guards below.  No ``durable=``
            # fallback and no equality gate is added here on purpose: ``row``
            # starts as a copy of ``existing``, so a guard that declines already
            # keeps the persisted value, and this leg appending on every call --
            # gate-free -- is pre-existing behaviour that is not this fix's to
            # change.  What the fix owes is value convergence, and the guards
            # give exactly that.
            started_at = _resolved_caller_evidence(started_at)
            finished_at = _resolved_caller_evidence(finished_at)
            exit_code = _resolved_caller_evidence(exit_code)
            error_code = _resolved_caller_evidence(error_code)
            error_message = _resolved_caller_evidence(error_message)
            log_uri = _resolved_caller_evidence(log_uri)
            safe_error_message = _durable_error_message(error_message)
            for key, value in (
                ("started_at", started_at),
                ("finished_at", finished_at),
                ("exit_code", exit_code),
                ("log_uri", log_uri),
            ):
                if value is not None:
                    row[key] = _format_utc(value) if isinstance(value, datetime) else value
            if status in {"succeeded", "complete", "published"} and error_code is None:
                row["error_code"] = None
            elif error_code is not None:
                row["error_code"] = error_code
            if status in {"succeeded", "complete", "published"} and error_message is None:
                row["error_message"] = None
            elif error_message is not None:
                row["error_message"] = safe_error_message
            row["updated_at"] = _format_utc(_utcnow())
            model_id = _optional_safe_identity(row, "model_id")
            return previous_status, self._write_pipeline_job_unlocked(row, exclusive_direct=False, model_id=model_id)

    def insert_pipeline_event(
        self,
        *,
        entity_type: str,
        entity_id: str,
        event_type: str,
        status_from: str | None,
        status_to: str | None,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_entity_type = _pipeline_event_entity_type(entity_type)
        source_id, cycle_time, model_id = self._pipeline_event_target(
            entity_type=normalized_entity_type,
            entity_id=entity_id,
        )
        row = {
            "entity_type": normalized_entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "status_from": status_from,
            "status_to": status_to,
            "message": message,
            "details": details or {},
            "created_at": _format_utc(_utcnow()),
        }
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            event_id = self._next_event_id_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
            )
            row["event_id"] = event_id
            self._append_validated_record_unlocked(
                "pipeline_event",
                row,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
                materialize_model_id=model_id,
                sequence=event_id,
            )
            if normalized_entity_type == "forecast_cycle":
                self._materialize_cycle_latest_unlocked(source_id=source_id, cycle_time=cycle_time)
        return _public_scheduler_row(row)

    def append_historical_pipeline_event(self, record: Mapping[str, Any]) -> dict[str, Any] | None:
        entity_type = _pipeline_event_entity_type(record.get("entity_type") or "pipeline_job")
        entity_id = _required_safe_identity(record, "entity_id")
        try:
            source_id, cycle_time, model_id = self._pipeline_event_target(
                entity_type=entity_type,
                entity_id=entity_id,
            )
        except OrchestratorError:
            return None
        row = {
            "event_id": record.get("event_id"),
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": str(record["event_type"]),
            "status_from": record.get("status_from"),
            "status_to": record.get("status_to"),
            "message": record.get("message"),
            "details": dict(record.get("details") or {}),
            "created_at": _optional_format_datetime(record.get("created_at"), field="created_at")
            or _format_utc(_utcnow()),
        }
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            rows = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
            event_id = row.get("event_id")
            if event_id in (None, ""):
                event_id = self._next_event_id_unlocked(
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                )
                row["event_id"] = event_id
            existing = next(
                (
                    event
                    for event in rows.pipeline_events
                    if str(event.get("event_id") or "") == str(event_id)
                    and str(event.get("entity_id") or "") == entity_id
                    and str(event.get("entity_type") or "pipeline_job") == entity_type
                ),
                None,
            )
            if existing is not None:
                return _public_scheduler_row(existing)
            self._append_validated_record_unlocked(
                "pipeline_event",
                row,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
                materialize_model_id=model_id,
            )
            if entity_type == "forecast_cycle":
                self._materialize_cycle_latest_unlocked(source_id=source_id, cycle_time=cycle_time)
        return _public_scheduler_row(row)

    def _pipeline_event_target(
        self,
        *,
        entity_type: str,
        entity_id: str,
    ) -> tuple[str, datetime, str | None]:
        if entity_type == "pipeline_job":
            job = self.get_pipeline_job(entity_id)
            if job is None:
                raise OrchestratorError("PIPELINE_JOB_NOT_FOUND", f"pipeline_job not found for event: {entity_id}")
            return _source_id_from_job(job), _cycle_time_from_job(job), _optional_safe_identity(job, "model_id")
        if entity_type == "forecast_cycle":
            cycle_id = _safe_identity_text(str(entity_id), field="entity_id")
            source_id, cycle_time = _source_cycle_from_cycle_id(cycle_id)
            return source_id, cycle_time, None
        raise OrchestratorError(
            "PIPELINE_EVENT_ENTITY_UNSUPPORTED",
            f"pipeline_event entity_type is not supported by file journal: {entity_type}",
        )

    def update_forecast_cycle_status(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        source_id = _normalize_file_source_id(source_id, field="source_id")
        row = {
            "cycle_id": _cycle_id_for_file_source(source_id, cycle_time),
            "source_id": source_id,
            "cycle_time": _format_utc(cycle_time),
            "issue_time": _format_utc(cycle_time),
            "status": status,
            "error_code": error_code,
            "error_message": _durable_error_message(error_message),
            "updated_at": _format_utc(_utcnow()),
        }
        self._append_validated_record("forecast_cycle", row, source_id=source_id, cycle_time=cycle_time)
        return _public_scheduler_row(row)

    def list_stage_statuses(
        self,
        *,
        source_id: str | None,
        cycle_time: datetime,
        model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        statuses: list[dict[str, Any]] = []
        if source_id is None:
            try:
                sources = self._cycle_source_discoveries(cycle_time=cycle_time)
            except FileOrchestrationJournalError as error:
                return [
                    _blocked_stage_status(
                        error,
                        source_id="unknown",
                        cycle_time=cycle_time,
                        model_id=model_id,
                    )
                ]
            for source in sources:
                statuses.extend(
                    self._list_stage_statuses_for_source(
                        source_id=source.source_id,
                        cycle_time=cycle_time,
                        model_id=model_id,
                        source_segment_overrides=source.source_segments,
                    )
                )
            statuses.sort(key=_db_compatible_stage_status_order_key)
            return statuses
        statuses = self._list_stage_statuses_for_source(
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
        )
        statuses.sort(key=_db_compatible_stage_status_order_key)
        return statuses

    def _list_stage_statuses_for_source(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str | None,
        source_segment_override: str | None = None,
        source_segment_overrides: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        source_id = _normalize_file_source_id(source_id, field="source_id")
        try:
            rows = self._cycle_rows(
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
                source_segment_override=source_segment_override,
                source_segment_overrides=source_segment_overrides,
            )
        except FileOrchestrationJournalError as error:
            return [_blocked_stage_status(error, source_id=source_id, cycle_time=cycle_time, model_id=model_id)]
        return [
            _public_scheduler_row(
                {
                    "stage": job.get("stage"),
                    "status": job.get("status"),
                    "job_id": job.get("job_id"),
                    "run_id": job.get("run_id"),
                    "cycle_id": job.get("cycle_id"),
                    "job_type": job.get("job_type"),
                    "slurm_job_id": job.get("slurm_job_id"),
                    "model_id": job.get("model_id"),
                    "source_id": source_id,
                    "submitted_at": job.get("submitted_at"),
                    "started_at": job.get("started_at"),
                    "finished_at": job.get("finished_at"),
                    "exit_code": job.get("exit_code"),
                    "error_code": job.get("error_code"),
                    "error_message": job.get("error_message"),
                    "log_uri": job.get("log_uri"),
                }
            )
            for job in rows.pipeline_jobs.values()
        ]

    def _cycle_source_ids(self, *, cycle_time: datetime) -> list[str]:
        return sorted({source.source_id for source in self._cycle_source_discoveries(cycle_time=cycle_time)})

    def _cycle_source_discoveries(self, *, cycle_time: datetime) -> list[_CycleSourceDiscovery]:
        cycle_segment = format_cycle_time(cycle_time)
        sources: dict[str, _CycleSourceDiscovery] = {}
        for path in sorted(
            _iter_regular_json_files(
                self.root / "latest",
                root=self.root,
                recursive=True,
                max_files=self.max_files,
                max_depth=self.max_depth,
            )
        ):
            parts = path.relative_to(self.root).parts
            if len(parts) == 4 and parts[0] == "latest" and parts[2] == cycle_segment:
                source = _cycle_source_discovery_from_segment(parts[1])
                _merge_cycle_source_discovery(sources, source, root=self.root)
        for surface in ("journal", "pipeline-events"):
            for path in sorted(
                _iter_jsonl_files(
                    self.root / surface,
                    root=self.root,
                    max_files=self.max_files,
                    max_depth=self.max_depth,
                )
            ):
                parts = path.relative_to(self.root).parts
                if len(parts) != 3 or parts[0] != surface:
                    continue
                # A continuation segment belongs to its base cycle; skipping it
                # would hide the newest content of an overflowed cycle.
                segment_cycle, segment_index = _split_journal_segment_stem(Path(parts[2]).stem)
                if segment_cycle != cycle_segment:
                    continue
                _require_journal_segment_lineage(
                    path,
                    root=self.root,
                    cycle_segment=segment_cycle,
                    segment_index=segment_index,
                )
                source = _cycle_source_discovery_from_segment(parts[1])
                _merge_cycle_source_discovery(sources, source, root=self.root)
        file_source_ids = set(sources)
        for job in self._iter_direct_pipeline_job_records():
            if _format_utc(_cycle_time_from_job(job)) == _format_utc(cycle_time):
                source_id = _source_id_from_job(job)
                if source_id not in file_source_ids:
                    sources.setdefault(
                        source_id,
                        _CycleSourceDiscovery(source_id=source_id, source_segments=(_safe_segment(source_id),)),
                    )
        return sorted(sources.values(), key=lambda source: (source.source_id, source.source_segments))

    def _cycle_rows(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str | None,
        source_segment_override: str | None = None,
        source_segment_overrides: tuple[str, ...] | None = None,
    ) -> _CycleRows:
        rows = _CycleRows()
        source_id = _normalize_file_source_id(source_id, field="source_id")
        source_segments = _cycle_read_source_segments(
            source_id=source_id,
            source_segment_override=source_segment_override,
            source_segment_overrides=source_segment_overrides,
            root=self.root,
        )
        cycle_segment = format_cycle_time(cycle_time)
        cache_key = (source_id, cycle_segment, model_id, source_segments)
        # A hit is trusted as-is only inside the write window, and only for
        # the thread and cycle the window covers: the cycle flock excludes
        # other writers for THAT cycle and the in-window append hook sweeps
        # every reachable source/cycle key, so the next read recomputes from
        # the newly committed journal bytes.  Any other thread — or the same
        # thread reading a different cycle from inside a window — must prove
        # its source files are stat-identical, otherwise writes from other
        # processes (or direct file fixtures) would be served stale forever.
        # The marker is the only producer of the fast path, and
        # `None == <tuple>` is always false, so a cold instance always
        # revalidates.
        in_write_window = self._cycle_write_owner == (
            threading.get_ident(),
            source_id,
            cycle_segment,
        )
        # A window entry carries `fingerprint=None` and can therefore only be
        # hit through the unvalidated branch above, so the wipe performed
        # when the window opens is the fast path's correctness precondition —
        # not a performance measure — because the owner's hits bypass the
        # fingerprint check.  Reads after the wipe may store their own
        # `fingerprint=None` entries for the rest of the window.  The owner
        # still computes no source-file fingerprint (#1567 D1b), but it runs
        # the containment probe over the DIRECTORIES that feed its cycle — and
        # nothing below them — so one of those directories swapped for a
        # symlink during the window turns the hit into a recompute that fails
        # loud with whatever token the cold reader reports for that directory
        # (design D1's table; it is a property of the (leg, lane) pair, not of
        # the leg alone).  Stated limit, design D1b: a LEAF swapped for a
        # symlink during the window is NOT seen — a leaf probe is exactly the
        # source-file fingerprint this fast path exists to skip — so the owner
        # keeps serving its pre-tamper cached rows there while a cold instance
        # raises.
        fingerprint = (
            None
            if in_write_window
            else self._cycle_rows_source_fingerprint(source_segments=source_segments, cycle_segment=cycle_segment)
        )
        # A fingerprint that observed a containment fault (#1567 D1) can
        # neither hit nor be stored.  Identity (`is`) on the marker, never
        # equality, so no exotic `__eq__` can resurrect the fast path.
        probe_faulted = (
            self._cycle_directories_probe_faulted(source_segments=source_segments, cycle_segment=cycle_segment)
            if in_write_window
            else fingerprint is _FINGERPRINT_CONTAINMENT_FAULT
        )
        with self._cache_lock:
            cached = self._cycle_rows_cache.get(cache_key)
        if (
            cached is not None
            and not probe_faulted
            and (in_write_window or (fingerprint is not None and cached[0] == fingerprint))
        ):
            # Cached rows are never mutated in place after being stored, so
            # cloning a hit outside the mutex cannot observe a torn value.
            return _clone_cycle_rows(cached[1])
        # Model-scoped reads build from the model's own latest view plus
        # model-filtered journal records. They must not derive hydro_run /
        # forcing_version / model_context from the merged model_id=None rows:
        # _CycleRows keeps single slots for those, so the cross-model merge
        # keeps one winner and the model filter then erases every other
        # model's rows. Only pipeline jobs (keyed by job_id, collapse-free)
        # are shared with the base rows so the pipeline-jobs directory is
        # scanned once per cycle instead of once per model.
        # #1734 D11: the baseline per-cycle read lane. It is not one of the
        # design's A/B/C candidates — it is the cost they must be separated
        # FROM — but the spec requires every counted byte to carry a lane, so
        # the miss path is tagged here. Eager only: no ``yield`` is crossed.
        with journal_read_lane("cycle_rows"):
            for source_segment in source_segments:
                latest_paths = self._latest_paths(source_segment, cycle_segment, model_id=model_id)
                for path in latest_paths:
                    payload = self._read_optional_json(path)
                    if payload is not None:
                        self._apply_latest_view(
                            rows,
                            payload,
                            source_id=source_id,
                            cycle_time=cycle_time,
                            expected_model_id=_safe_segment(path.stem),
                        )
                for record in self._read_cycle_segments(self.root / "journal" / source_segment, cycle_segment):
                    self._apply_journal_record(
                        rows,
                        record,
                        source_id=source_id,
                        cycle_time=cycle_time,
                        expected_model_id=model_id,
                    )
                for record in self._read_cycle_segments(
                    self.root / "pipeline-events" / source_segment, cycle_segment
                ):
                    self._apply_journal_record(
                        rows,
                        record,
                        source_id=source_id,
                        cycle_time=cycle_time,
                        expected_record_type="pipeline_event",
                        expected_model_id=model_id,
                    )
            for job in self._direct_pipeline_job_records_for_cycle_cached(
                source_id=source_id,
                cycle_time=cycle_time,
            ):
                _insert_missing_by_key(rows.pipeline_jobs, job, key="job_id")
        if model_id is not None:
            _filter_cycle_rows_for_model(rows, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        rows.pipeline_events = _dedupe_events(rows.pipeline_events)
        # #1567 D1: a read whose fingerprint (or, for the window owner, whose
        # directory probe) observed a containment fault stores nothing, so a
        # later read cannot recompute the same marker, compare equal and serve
        # rows that were computed under the tamper.
        if not probe_faulted:
            self._cache_cycle_rows(cache_key, rows, fingerprint=fingerprint)
        return _clone_cycle_rows(rows)

    def _cycle_rows_by_model_unlocked(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_ids: Iterable[str],
        include_direct_jobs: bool = True,
    ) -> dict[str, _CycleRows]:
        """Build exact model rows with one cycle-wide source scan.

        ``_CycleRows`` has single hydro/forcing/context slots, so this must
        reduce records into separate model containers rather than filtering
        the lossy ``model_id=None`` merge.  The caller holds the cycle write
        lock; the populated model caches therefore remain authoritative until
        an append sweeps them.
        """
        source_id = _normalize_file_source_id(source_id, field="source_id")
        normalized_model_ids = sorted({_safe_segment(model_id) for model_id in model_ids})
        rows_by_model = {model_id: _CycleRows() for model_id in normalized_model_ids}
        if not rows_by_model:
            return {}
        source_segments = _cycle_read_source_segments(
            source_id=source_id,
            source_segment_override=None,
            root=self.root,
        )
        cycle_segment = format_cycle_time(cycle_time)
        for source_segment in source_segments:
            for path in self._latest_paths(source_segment, cycle_segment, model_id=None):
                model_id = _safe_segment(path.stem)
                rows = rows_by_model.get(model_id)
                if rows is None:
                    continue
                payload = self._read_optional_json(path)
                if payload is not None:
                    self._apply_latest_view(
                        rows,
                        payload,
                        source_id=source_id,
                        cycle_time=cycle_time,
                        expected_model_id=model_id,
                    )
            self._apply_records_to_model_rows(
                rows_by_model,
                self._read_cycle_segments(self.root / "journal" / source_segment, cycle_segment),
                source_id=source_id,
                cycle_time=cycle_time,
            )
            self._apply_records_to_model_rows(
                rows_by_model,
                self._read_cycle_segments(self.root / "pipeline-events" / source_segment, cycle_segment),
                source_id=source_id,
                cycle_time=cycle_time,
                expected_record_type="pipeline_event",
            )
        direct_jobs = (
            self._direct_pipeline_job_records_for_cycle_cached(
                source_id=source_id,
                cycle_time=cycle_time,
            )
            if include_direct_jobs
            else ()
        )
        for model_id, rows in rows_by_model.items():
            for job in direct_jobs:
                _insert_missing_by_key(rows.pipeline_jobs, job, key="job_id")
            _filter_cycle_rows_for_model(rows, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
            rows.pipeline_events = _dedupe_events(rows.pipeline_events)
            if include_direct_jobs:
                self._cache_cycle_rows(
                    (source_id, cycle_segment, model_id, source_segments),
                    rows,
                    fingerprint=None,
                )
        return {model_id: _clone_cycle_rows(rows) for model_id, rows in rows_by_model.items()}

    def _apply_records_to_model_rows(
        self,
        rows_by_model: Mapping[str, _CycleRows],
        records: Sequence[Mapping[str, Any]],
        *,
        source_id: str,
        cycle_time: datetime,
        expected_record_type: str | None = None,
    ) -> None:
        """Route one decoded record batch into exact per-model reducers."""
        for record in records:
            payload = _payload_or_record_payload(record)
            record_type = _record_type(record, payload)
            _require_schema(record, FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION)
            _require_source_cycle(record, source_id=source_id, cycle_time=cycle_time)
            _require_record_payload_identity_match(record_type, record, payload)
            if expected_record_type is not None and record_type != expected_record_type:
                raise FileOrchestrationJournalError(
                    "file_journal_record_type_mismatch",
                    field="record_type",
                    evidence={"expected": expected_record_type, "actual": record_type[:80]},
                )
            record_model_id = _record_model_id(
                record,
                payload,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            targets = (
                ((record_model_id, rows_by_model[record_model_id]),)
                if record_model_id in rows_by_model
                else tuple(rows_by_model.items())
                if record_model_id is None
                else ()
            )
            for model_id, rows in targets:
                self._apply_journal_record(
                    rows,
                    record,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    expected_record_type=expected_record_type,
                    expected_model_id=model_id,
                )

    def _direct_pipeline_job_records_for_cycle_cached(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> list[dict[str, Any]]:
        """One pipeline-jobs directory scan per cycle, shared across models.

        Entries in that directory only change via atomic rename, which bumps
        the directory's (mtime_ns, size, inode); a matching signature proves
        the memoized listing still reflects the on-disk job set. Model-level
        scoping happens in _filter_cycle_rows_for_model, mirroring how the
        unfiltered scan feeds the model_id=None rows.
        """
        cache_key = (source_id, format_cycle_time(cycle_time))
        signature = (
            _stat_signature(self.root / "pipeline-jobs"),
            _stat_signature(
                self.root
                / "pipeline-jobs"
                / "by-cycle"
                / _safe_segment(source_id)
                / format_cycle_time(cycle_time)
            ),
        )
        with self._cache_lock:
            cached = self._direct_jobs_cycle_cache.get(cache_key)
        if cached is not None and cached[0] == signature:
            return [dict(job) for job in cached[1]]
        # #1734 D11 candidate B: this is the CACHE-MISS path, which is the cost
        # this cache's shared-directory signature keeps re-paying. The lane tag
        # itself sits per-path inside the reader below, because that reader
        # merges the unpartitioned flat directory with the already-partitioned
        # by-cycle tree and the two must not be graded as one.
        jobs = [
            dict(job)
            for job in self._iter_direct_pipeline_job_records_for_cycle(
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=None,
            )
        ]
        cache_limit = max(int(MAX_FILE_JOURNAL_CYCLE_ROWS_CACHE_ENTRIES), 1)
        entry = (signature, [dict(job) for job in jobs])
        with self._cache_lock:
            if cache_key not in self._direct_jobs_cycle_cache and len(self._direct_jobs_cycle_cache) >= cache_limit:
                self._direct_jobs_cycle_cache.pop(next(iter(self._direct_jobs_cycle_cache)), None)
            self._direct_jobs_cycle_cache[cache_key] = entry
        return jobs

    def _cycle_rows_source_fingerprint(
        self,
        *,
        source_segments: tuple[str, ...],
        cycle_segment: str,
    ) -> tuple[Any, ...] | _FingerprintContainmentFault:
        """Stat-level identity of every file that feeds `_cycle_rows`.

        Appends, atomic replaces, additions and removals all change the
        (mtime_ns, size, inode) of a source file — or the latest-directory
        listing, or the pipeline-jobs directory whose entries only change
        via rename — so a matching fingerprint proves a cached entry still
        reflects the on-disk state.

        #1567 D1: every stat in this family — the segment slots, the event-log
        slots, the ``latest/<source>/<cycle>`` listing's own directory, the
        by-cycle direct partition and the flat ``pipeline-jobs`` root — routes
        through ``_containment_stat_signature``.  If any of them observes a
        containment fault the whole fingerprint collapses to
        :data:`_FINGERPRINT_CONTAINMENT_FAULT`, which ``_cycle_rows`` neither
        hits nor stores; storing it would let the NEXT read compute the same
        marker, compare equal and serve the tampered rows — the same hole in a
        new shape.
        """
        latest_entries: list[tuple[str, str, tuple[int, int, int] | None]] = []
        journal_signatures: list[tuple[str, Any]] = []
        event_signatures: list[tuple[str, Any]] = []
        direct_partition_signatures: list[tuple[str, Any]] = []
        latest_directory_signatures: list[tuple[str, Any]] = []
        for source_segment in source_segments:
            latest_directory = self.root / "latest" / source_segment / cycle_segment
            # The scandir is the one leg the helper cannot be a drop-in for:
            # ``os.scandir`` FOLLOWS a symlinked parent and lists the decoy
            # without raising, so the directory itself is probed first and a
            # fault short-circuits the listing.  Entries inside a probed-real
            # directory keep their own ``entry.stat(follow_symlinks=False)``.
            latest_directory_signature = self._containment_stat_signature(latest_directory)
            latest_directory_signatures.append((source_segment, latest_directory_signature))
            if latest_directory_signature is not None and not _signature_has_containment_fault(
                latest_directory_signature
            ):
                try:
                    with os.scandir(latest_directory) as it:
                        for entry in it:
                            if entry.name.endswith(".json"):
                                try:
                                    entry_stat = entry.stat(follow_symlinks=False)
                                    latest_entries.append(
                                        (
                                            source_segment,
                                            entry.name,
                                            (entry_stat.st_mtime_ns, entry_stat.st_size, entry_stat.st_ino),
                                        )
                                    )
                                except OSError:
                                    latest_entries.append((source_segment, entry.name, None))
                except OSError:
                    pass
            journal_signatures.append(
                (
                    source_segment,
                    self._cycle_segment_signatures(self.root / "journal" / source_segment, cycle_segment),
                )
            )
            event_signatures.append(
                (
                    source_segment,
                    self._cycle_segment_signatures(
                        self.root / "pipeline-events" / source_segment, cycle_segment
                    ),
                )
            )
            direct_partition_signatures.append(
                (
                    source_segment,
                    self._containment_stat_signature(
                        self.root / "pipeline-jobs" / "by-cycle" / source_segment / cycle_segment
                    ),
                )
            )
        fingerprint = (
            tuple(journal_signatures),
            tuple(event_signatures),
            tuple(direct_partition_signatures),
            tuple(sorted(latest_entries)),
            tuple(latest_directory_signatures),
            self._containment_stat_signature(self.root / "pipeline-jobs"),
        )
        if _signature_has_containment_fault(fingerprint):
            return _FINGERPRINT_CONTAINMENT_FAULT
        return fingerprint

    def _cache_cycle_rows(
        self,
        cache_key: tuple[str, str, str | None, tuple[str, ...]],
        rows: _CycleRows,
        *,
        fingerprint: tuple[Any, ...] | None,
    ) -> None:
        cache_limit = max(int(MAX_FILE_JOURNAL_CYCLE_ROWS_CACHE_ENTRIES), 1)
        entry = (fingerprint, _clone_cycle_rows(rows))
        with self._cache_lock:
            if cache_key not in self._cycle_rows_cache and len(self._cycle_rows_cache) >= cache_limit:
                self._cycle_rows_cache.pop(next(iter(self._cycle_rows_cache)), None)
            self._cycle_rows_cache[cache_key] = entry

    def _latest_paths(self, source_segment: str, cycle_segment: str, *, model_id: str | None) -> list[Path]:
        directory = self.root / "latest" / source_segment / cycle_segment
        if model_id is not None:
            return [directory / f"{_safe_segment(model_id)}.json"]
        return sorted(
            _iter_regular_json_files(
                directory,
                root=self.root,
                max_files=self.max_files,
                max_depth=self.max_depth,
            )
        )

    def _apply_latest_view(
        self,
        rows: _CycleRows,
        payload: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
        expected_model_id: str,
    ) -> None:
        _require_schema(payload, FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION)
        _require_source_cycle(payload, source_id=source_id, cycle_time=cycle_time)
        _require_model_id(payload, expected_model_id, required=True)
        latest_replay_sequence = _latest_replay_sequence(payload)
        hydro_run = _first_mapping(payload, "hydro_run", "hydro")
        if hydro_run is not None:
            _validate_hydro_run_identity(
                hydro_run,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=expected_model_id,
            )
            hydro_run = _with_latest_replay_order(hydro_run, latest_replay_sequence)
        forecast_cycle = _first_mapping(payload, "forecast_cycle")
        if forecast_cycle is not None:
            _validate_forecast_cycle_identity(forecast_cycle, source_id=source_id, cycle_time=cycle_time)
            forecast_cycle = _with_latest_replay_order(forecast_cycle, latest_replay_sequence)
        forcing_version = _first_mapping(payload, "forcing_version", "forcing_context")
        if forcing_version is not None:
            _validate_forcing_version_identity(
                forcing_version,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=expected_model_id,
            )
            forcing_version = _with_latest_replay_order(forcing_version, latest_replay_sequence)
        model_context = _first_mapping(payload, "model_context")
        if model_context is not None:
            _validate_model_context_identity(model_context, model_id=expected_model_id)
            model_context = _with_latest_replay_order(model_context, latest_replay_sequence)
        rows.hydro_run = _latest_mapping(rows.hydro_run, hydro_run)
        rows.forecast_cycle = _latest_mapping(rows.forecast_cycle, forecast_cycle)
        rows.forcing_version = _latest_mapping(
            rows.forcing_version,
            forcing_version,
        )
        rows.model_context = _latest_mapping(rows.model_context, model_context)
        for job in _record_list(payload, "pipeline_jobs", "jobs", single_key="pipeline_job"):
            _validate_pipeline_job_identity(
                job,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=expected_model_id,
            )
            _validate_accepted_submit_evidence(job)
            job = _with_latest_replay_order(job, latest_replay_sequence)
            _upsert_by_key(rows.pipeline_jobs, job, key="job_id")
        for event in _record_list(payload, "pipeline_events", "events", single_key="pipeline_event"):
            _validate_event_identity(event, source_id=source_id, cycle_time=cycle_time)
            event = _with_latest_replay_order(event, latest_replay_sequence)
            rows.pipeline_events.append(event)
        replay = payload.get("replay")
        if isinstance(replay, Mapping):
            rows.replay.update(_evidence_safe(dict(replay)))

    def _apply_journal_record(
        self,
        rows: _CycleRows,
        record: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
        expected_record_type: str | None = None,
        expected_model_id: str | None = None,
    ) -> None:
        payload = _payload_or_record_payload(record)
        record_type = _record_type(record, payload)
        _require_schema(record, FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION)
        _require_source_cycle(record, source_id=source_id, cycle_time=cycle_time)
        _require_record_payload_identity_match(record_type, record, payload)
        record_model_id = _record_model_id(
            record,
            payload,
            source_id=source_id,
            cycle_time=cycle_time,
        )
        if expected_record_type is not None and record_type != expected_record_type:
            raise FileOrchestrationJournalError(
                "file_journal_record_type_mismatch",
                field="record_type",
                evidence={"expected": expected_record_type, "actual": record_type[:80]},
            )
        if expected_model_id is not None and record_model_id is not None and record_model_id != expected_model_id:
            return
        payload = _with_replay_order(payload, record)
        if record_type == "pipeline_job":
            _validate_pipeline_job_identity(
                payload,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=record_model_id if record_model_id is not None else expected_model_id,
            )
            _validate_accepted_submit_evidence(payload)
            _upsert_by_key(rows.pipeline_jobs, payload, key="job_id")
        elif record_type == "pipeline_event":
            self._apply_event_record(rows, record, source_id=source_id, cycle_time=cycle_time)
        elif record_type in {"hydro_run", "forecast_cycle", "forcing_version", "model_context"}:
            _validate_payload_identity(
                record_type,
                payload,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=record_model_id,
            )
            setattr(rows, record_type, _latest_mapping(getattr(rows, record_type), payload))
        else:
            raise FileOrchestrationJournalError("file_journal_unknown_record_type", field="record_type")

    def _apply_event_record(
        self,
        rows: _CycleRows,
        record: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> None:
        payload = _with_replay_order(_payload_or_record_payload(record), record)
        if "event_id" not in payload and record.get("sequence") not in (None, ""):
            payload["event_id"] = record.get("sequence")
        _validate_event_identity(payload, source_id=source_id, cycle_time=cycle_time)
        rows.pipeline_events.append(dict(payload))

    def _read_json(self, path: Path) -> dict[str, Any]:
        payload = self._read_optional_json(path)
        if payload is None:
            raise FileOrchestrationJournalError(
                "file_journal_view_missing",
                field=str(_relative_evidence(path, self.root)),
            )
        return payload

    def _read_bytes_limited_cached(self, path: Path) -> tuple[bytes, bool]:
        """Read file bytes through a stat-identity cache.

        The stat probe is only a cache key: any anomaly (missing file,
        symlink, non-regular target) falls through to the hardened
        no-follow reader, which stays the sole authority for errors and
        content. A hit requires an exact (mtime_ns, size, inode) match,
        so appends and atomic-rename replacements always miss. The
        returned flag is True when these exact bytes already passed
        `_decode_mapping` validation in this process.
        """
        key = str(path)
        signature: tuple[int, int, int] | None = None
        try:
            probe = os.stat(path, follow_symlinks=False)
        except OSError:
            probe = None
            self._read_bytes_cache_drop(key)
        if probe is not None and stat.S_ISREG(probe.st_mode):
            signature = (probe.st_mtime_ns, probe.st_size, probe.st_ino)
            with self._cache_lock:
                cached = self._read_bytes_cache.get(key)
            if cached is not None and cached[0] == signature:
                # #1734 D11: a byte-cache hit performs no filesystem read, so
                # it is accounted separately and never as `bytes`.
                _record_journal_read(byte_count=len(cached[1]), cached=True)
                return cached[1], cached[2]
        # The journal's durable writes replace files atomically, which changes
        # the target inode; the hardened reader reports that as
        # `kind="identity_changed"`.  That is the SAME event as a normal
        # concurrent write at this layer, so a mid-open replacement is
        # absorbed with a bounded retry, selected on the structured kind —
        # never on message text — and with no sleep.  Every other refusal
        # (symlink, non-regular, containment, a different kind) propagates on
        # the first attempt, and an exhausted retry fails closed with the
        # last exception unchanged.
        for attempt in range(MAX_FILE_JOURNAL_IDENTITY_RETRY_ATTEMPTS):
            try:
                content = read_bytes_limited_no_follow(
                    path, max_bytes=self.max_bytes, containment_root=self.root
                )
                break
            except SafeFilesystemError as error:
                if error.kind != "identity_changed":
                    raise
                if attempt + 1 == MAX_FILE_JOURNAL_IDENTITY_RETRY_ATTEMPTS:
                    raise
                # The stat probe and its signature must be re-derived together
                # on every round; the stale signature from the previous round
                # would crash on the new probe's fields.
                signature = None
                try:
                    probe = os.stat(path, follow_symlinks=False)
                except OSError:
                    probe = None
                    self._read_bytes_cache_drop(key)
                if probe is not None and stat.S_ISREG(probe.st_mode):
                    signature = (probe.st_mtime_ns, probe.st_size, probe.st_ino)
        if signature is not None and len(content) == probe.st_size:
            self._read_bytes_cache_store(key, signature, content)
        # #1734 D11: the ONE place journal bytes cross the syscall boundary, so
        # the one place `rchar` can be attributed to an entrypoint. Both
        # `_read_optional_json` and `_read_jsonl` route through here.
        _record_journal_read(byte_count=len(content), cached=False)
        return content, False

    def _read_bytes_cache_store(self, key: str, signature: tuple[int, int, int], content: bytes) -> None:
        if len(content) > MAX_FILE_JOURNAL_READ_CACHE_BYTES:
            return
        with self._cache_lock:
            self._read_bytes_cache_drop_cache_locked(key)
            while self._read_bytes_cache and (
                len(self._read_bytes_cache) >= MAX_FILE_JOURNAL_READ_CACHE_ENTRIES
                or self._read_bytes_cache_total + len(content) > MAX_FILE_JOURNAL_READ_CACHE_BYTES
            ):
                self._read_bytes_cache_drop_cache_locked(next(iter(self._read_bytes_cache)))
            self._read_bytes_cache[key] = (signature, content, False)
            self._read_bytes_cache_total += len(content)

    def _read_bytes_cache_drop(self, key: str) -> None:
        with self._cache_lock:
            self._read_bytes_cache_drop_cache_locked(key)

    def _read_bytes_cache_drop_cache_locked(self, key: str) -> None:
        entry = self._read_bytes_cache.pop(key, None)
        if entry is not None:
            self._read_bytes_cache_total -= len(entry[1])

    def _read_bytes_cache_mark_validated(self, key: str, content: bytes) -> None:
        with self._cache_lock:
            entry = self._read_bytes_cache.get(key)
            if entry is not None and entry[1] is content:
                self._read_bytes_cache[key] = (entry[0], entry[1], True)

    def _read_optional_json(self, path: Path) -> dict[str, Any] | None:
        try:
            content, prevalidated = self._read_bytes_limited_cached(path)
        except FileNotFoundError:
            return None
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_unreadable",
                field=str(_relative_evidence(path, self.root)),
                evidence={"error_type": type(error).__name__},
            ) from error
        self._require_within_byte_limit(content, path)
        if prevalidated:
            return _decode_mapping_prevalidated(content, field=str(_relative_evidence(path, self.root)))
        payload = _decode_mapping(
            content,
            field=str(_relative_evidence(path, self.root)),
            max_nodes=self.max_json_nodes,
            max_depth=self.max_json_depth,
        )
        self._read_bytes_cache_mark_validated(str(path), content)
        return payload

    def _read_jsonl(self, path: Path, *, segment_index: int = 0) -> list[dict[str, Any]]:
        """Decode one cycle event-log segment with segment-offset replay order.

        The offset is a FIXED stride (``segment_index * MAX_FILE_JOURNAL_
        RECORDS``), not a cumulative line count: it stays strictly monotonic
        across segments while remaining bounded by the segment cap, so the
        latest-view sentinel keeps winning same-``sequence`` ties.  Segment 0
        is byte-identical to the pre-rotation numbering.
        """
        try:
            content, prevalidated = self._read_bytes_limited_cached(path)
        except FileNotFoundError:
            return []
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_unreadable",
                field=str(_relative_evidence(path, self.root)),
                evidence={"error_type": type(error).__name__},
            ) from error
        self._require_within_byte_limit(content, path)
        records: list[dict[str, Any]] = []
        for line_number, raw_line in enumerate(content.splitlines(), start=1):
            if not raw_line.strip():
                continue
            if len(records) >= MAX_FILE_JOURNAL_RECORDS:
                raise FileOrchestrationJournalError("file_journal_record_limit_exceeded", field="journal")
            if prevalidated:
                record = _decode_mapping_prevalidated(
                    raw_line,
                    field=f"{_relative_evidence(path, self.root)}:{line_number}",
                )
            else:
                record = _decode_mapping(
                    raw_line,
                    field=f"{_relative_evidence(path, self.root)}:{line_number}",
                    max_nodes=self.max_json_nodes,
                    max_depth=self.max_json_depth,
                )
            record[_REPLAY_ORDER_FIELD] = segment_index * MAX_FILE_JOURNAL_RECORDS + line_number
            records.append(record)
        if not prevalidated:
            self._read_bytes_cache_mark_validated(str(path), content)
        return records

    def _require_within_byte_limit(self, content: bytes, path: Path) -> None:
        if len(content) > self.max_bytes:
            raise FileOrchestrationJournalError(
                "file_journal_byte_limit_exceeded",
                field=str(_relative_evidence(path, self.root)),
            )

    def _iter_direct_pipeline_job_records(self) -> Iterable[dict[str, Any]]:
        directory = self.root / "pipeline-jobs"
        for path in sorted(
            _iter_regular_json_files(
                directory,
                root=self.root,
                max_files=self.max_files,
                max_depth=self.max_depth,
            )
        ):
            payload = self._read_optional_json(path)
            if payload is not None:
                yield self._validated_direct_pipeline_job_record(payload, expected_job_id=_safe_segment(path.stem))

    def _flat_direct_pipeline_job_paths(self) -> list[Path]:
        """The whole flat ``pipeline-jobs/`` listing, memoized per read call.

        Unmemoized by default: a listing that outlived its call would make a
        live journal look frozen. ``_flat_direct_job_listing_memo_scope``
        activates it for the duration of ONE read-only query, which is what
        keeps the confirm half of ``query_released_identity_blocked_jobs`` from
        re-listing 4,557 files once per candidate (#1810 design D14).
        """

        memo = _flat_direct_job_listing_memo.get()
        cache_key = str(self.root)
        if memo is not None:
            memoized = memo.get(cache_key)
            if memoized is not None:
                return list(memoized)
        paths = sorted(
            _iter_regular_json_files(
                self.root / "pipeline-jobs",
                root=self.root,
                max_files=self.max_files,
                max_depth=self.max_depth,
            )
        )
        if memo is not None:
            memo[cache_key] = list(paths)
        return paths

    def _flat_direct_pipeline_job_paths_for_cycle(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> list[Path]:
        """The ONE definition of the flat ``pipeline-jobs/`` filename filter.

        The flat directory is not partitioned: it holds one file per cohort
        master row and per non-forecast-stage row for ALL retained history
        (4,303 files / 12.29 MiB on node-22), so reading it whole would dominate
        the cycle slice and keep the growth law intact — the very thing this
        change repairs (design D2a).

        The filter fails toward reading too much: a file is skipped ONLY when
        its name resolves to a different ``(source_id, cycle)``. A name that
        does not resolve at all is read, which is D4's fall-open pushed down to
        the filename level. The same fall-open covers a ``source_id`` this
        instance cannot normalise: an unusable scope filters nothing.

        Both flat readers route through here (design D9). It yields PATHS
        rather than records because ``_iter_direct_pipeline_job_records_for_
        cycle`` interleaves its flat and by-cycle legs in one merged sort, so
        record-level delegation would change that reader's yield order.
        ``_cycle_scope_from_job_id`` returns the canonical source spelling, so
        the caller's spelling is normalised once here — a raw comparison would
        skip every file of a source passed as ``ifs`` rather than ``IFS``,
        which is a silent empty result rather than a slow one.
        """

        try:
            canonical_source_id: str | None = _normalize_file_source_id(source_id, field="source_id")
        except FileOrchestrationJournalError:
            canonical_source_id = None
        cycle_segment = format_cycle_time(cycle_time)
        paths = self._flat_direct_pipeline_job_paths()
        if canonical_source_id is None:
            return paths
        selected: list[Path] = []
        for path in paths:
            name_scope = _cycle_scope_from_job_id(path.stem)
            if name_scope is not None and (
                name_scope[0] != canonical_source_id or format_cycle_time(name_scope[1]) != cycle_segment
            ):
                continue
            selected.append(path)
        return selected

    def _iter_flat_direct_pipeline_job_records_for_cycle(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> Iterable[dict[str, Any]]:
        """Flat ``pipeline-jobs/`` records for one cycle, filtered by file name.

        Filtering lives in ``_flat_direct_pipeline_job_paths_for_cycle``; this
        is the record-shaped view of it.
        """

        for path in self._flat_direct_pipeline_job_paths_for_cycle(
            source_id=source_id,
            cycle_time=cycle_time,
        ):
            payload = self._read_optional_json(path)
            if payload is not None:
                yield self._validated_direct_pipeline_job_record(payload, expected_job_id=_safe_segment(path.stem))

    def _direct_pipeline_job_record(self, expected_job_id: str) -> dict[str, Any] | None:
        # #1734 D11: a single-row probe, not a scan — its own lane so its two
        # reads are never mistaken for a flat-directory sweep.
        with journal_read_lane("direct_row_probe"):
            payload = self._read_optional_json(self.root / "pipeline-jobs" / f"{expected_job_id}.json")
            if payload is None:
                match = _CANDIDATE_JOB_ID_RE.fullmatch(expected_job_id)
                if match is not None:
                    source_id = _normalize_file_source_id(match.group(1), field="job_id")
                    payload = self._read_optional_json(
                        self.root
                        / "pipeline-jobs"
                        / "by-cycle"
                        / _safe_segment(source_id)
                        / _safe_segment(match.group(2))
                        / f"{expected_job_id}.json"
                    )
        if payload is None:
            return None
        return self._validated_direct_pipeline_job_record(payload, expected_job_id=expected_job_id)

    def _iter_direct_pipeline_job_records_for_cycle(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str | None,
    ) -> Iterable[dict[str, Any]]:
        """Direct records for one cycle from both surfaces, content-filtered.

        The flat leg is filename-prefiltered by delegating to
        ``_flat_direct_pipeline_job_paths_for_cycle`` — the SAME definition the
        other flat reader uses (design D9). Before that, this reader decoded
        every file in the unpartitioned flat directory (13.18 MB across 4,375
        files on node-22) on every cache miss, which is the parity defect D9
        names. Only the flat leg changes: the by-cycle partition is already
        cycle-scoped, and both legs still feed the one merged sort so the yield
        order is unchanged.

        The two legs carry DISTINCT lane tags (#1734 round-1 finding 3). One
        tag over the merged list graded partitioned bytes against the flat
        directory's size: measured, by-cycle was 33.5% of the lane in a scratch
        fixture and would be roughly two thirds at node-22's tree sizes (26 MB
        by-cycle / 13 MB flat). The tag is set per path around the eager read
        only — never across the ``yield`` — because a ``ContextVar`` token reset
        from a generator a consumer abandoned can land in a foreign context, and
        an instrument must not be able to fail a read path (design D11).
        """

        by_cycle_directory = (
            self.root
            / "pipeline-jobs"
            / "by-cycle"
            / _safe_segment(_normalize_file_source_id(source_id, field="source_id"))
            / format_cycle_time(cycle_time)
        )
        flat_paths = self._flat_direct_pipeline_job_paths_for_cycle(
            source_id=source_id,
            cycle_time=cycle_time,
        )
        flat_keys = {str(path) for path in flat_paths}
        for path in sorted(
            [
                *flat_paths,
                *_iter_regular_json_files(
                    by_cycle_directory,
                    root=self.root,
                    max_files=self.max_files,
                    max_depth=self.max_depth,
                ),
            ]
        ):
            expected_job_id = _safe_segment(path.stem)
            lane = "direct_flat_scan" if str(path) in flat_keys else "direct_by_cycle_scan"
            with journal_read_lane(lane):
                payload = self._read_optional_json(path)
            if payload is None:
                continue
            job = self._validated_direct_pipeline_job_record(payload, expected_job_id=expected_job_id)
            if model_id is None:
                if _job_matches_source_cycle(job, source_id=source_id, cycle_time=cycle_time):
                    yield job
                continue
            if _job_matches_candidate(job, source_id=source_id, cycle_time=cycle_time, model_id=model_id):
                yield job

    def _iter_pipeline_job_records(self, *, include_direct: bool = True) -> Iterable[dict[str, Any]]:
        """Whole-tree replay — design D11's candidate A, and everyone's fall-open.

        The lane tag wraps only the eager collection phase, never the ``yield``:
        a ``ContextVar`` token reset from a generator that a consumer abandoned
        could land in a different context, and an instrument must not be able
        to raise on a read path.
        """

        with journal_read_lane("full_tree_replay"):
            jobs = self._replay_all_pipeline_job_records(include_direct=include_direct)
        yield from jobs.values()

    def _replay_all_pipeline_job_records(self, *, include_direct: bool = True) -> dict[str, dict[str, Any]]:
        jobs: dict[str, dict[str, Any]] = {}
        budget = _RecordBudget(max(self.max_records, 1), "pipeline_job_records")
        for path in sorted(
            _iter_regular_json_files(
                self.root / "latest",
                root=self.root,
                recursive=True,
                max_files=self.max_files,
                max_depth=self.max_depth,
            )
        ):
            payload = self._read_optional_json(path)
            if payload is None:
                continue
            source_id, cycle_time, model_id = _latest_identity_from_path(path, root=self.root)
            rows = _CycleRows()
            self._apply_latest_view(
                rows,
                payload,
                source_id=source_id,
                cycle_time=cycle_time,
                expected_model_id=model_id,
            )
            for job in rows.pipeline_jobs.values():
                budget.consume()
                _upsert_by_key(jobs, job, key="job_id")
        for path in sorted(
            _iter_jsonl_files(
                self.root / "journal",
                root=self.root,
                max_files=self.max_files,
                max_depth=self.max_depth,
            ),
            key=lambda candidate: _journal_segment_sort_key(candidate, root=self.root, surface="journal"),
        ):
            source_id, cycle_time = _journal_identity_from_path(path, root=self.root, surface="journal")
            for record in self._read_jsonl(path):
                budget.consume()
                rows = _CycleRows()
                self._apply_journal_record(rows, record, source_id=source_id, cycle_time=cycle_time)
                for job in rows.pipeline_jobs.values():
                    _upsert_by_key(jobs, job, key="job_id")
        if include_direct:
            for job in self._iter_direct_pipeline_job_records():
                budget.consume()
                _insert_missing_by_key(jobs, job, key="job_id")
        return jobs

    def _cycle_job_records_signature(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        source_segments: tuple[str, ...],
        cycle_segment: str,
        include_direct: bool,
    ) -> tuple[Any, ...]:
        """Stat identity of exactly the files one cycle's replay would open.

        Design D10: every component is scoped to THIS cycle, never to a shared
        directory. ``_direct_jobs_cycle_cache`` is the cautionary example in
        this same file — its first component is
        ``_stat_signature(root / "pipeline-jobs")``, a globally shared
        directory, so any write to any cycle invalidates every entry. That is
        correct and it thrashes; a memo built the same way buys nothing.

        Each leg is enumerated by the SAME call the reader uses, then lstat'ed
        per file, so additions, removals, appends and atomic replaces all move
        the signature and no alignment drift is possible:

        * ``latest/<segment>/<cycle>/`` — already cycle-partitioned.
        * ``journal/<segment>/<cycle>*.jsonl`` — the directory is shared across
          the source's cycles, so the MATCHED FILE SET is stat'ed, including
          the fall-open stems ``_journal_segment_stem_in_cycle_scope`` admits.
        * flat ``pipeline-jobs/`` — the directory is shared globally, so the
          PREFILTERED file set (D2a/D9) is stat'ed. Skipped only when
          ``include_direct`` is false, which is the one case where the replay
          does not open the surface at all.

        Per-file lstat over an already-narrowed set is metadata only: it is the
        set the pass would open anyway and it contributes nothing to ``rchar``.

        Stated limitation: the signature does not cover the identity of the
        directories themselves (a cycle directory replaced by a fresh one whose
        children are byte-identical is a hit). Every production write to these
        surfaces is an append or an atomic file-level replace, so this is not
        reachable by a writer; a wholesale out-of-band directory swap with
        identical contents is indistinguishable from no change, by design.
        """

        latest_signatures: list[tuple[str, tuple[int, int, int] | None]] = []
        journal_signatures: list[tuple[str, tuple[int, int, int] | None]] = []
        for segment in source_segments:
            for path in sorted(
                _iter_regular_json_files(
                    self.root / "latest" / segment / cycle_segment,
                    root=self.root,
                    recursive=True,
                    max_files=self.max_files,
                    max_depth=self.max_depth,
                )
            ):
                latest_signatures.append((str(path), _stat_signature(path)))
            for path in sorted(
                path
                for path in _iter_jsonl_files(
                    self.root / "journal" / segment,
                    root=self.root,
                    max_files=self.max_files,
                    max_depth=self.max_depth,
                )
                if _journal_segment_stem_in_cycle_scope(path.stem, cycle_segment)
            ):
                journal_signatures.append((str(path), _stat_signature(path)))
        direct_signatures: tuple[tuple[str, tuple[int, int, int] | None], ...] = ()
        if include_direct:
            direct_signatures = tuple(
                (str(path), _stat_signature(path))
                for path in self._flat_direct_pipeline_job_paths_for_cycle(
                    source_id=source_id,
                    cycle_time=cycle_time,
                )
            )
        return (
            tuple(latest_signatures),
            tuple(journal_signatures),
            direct_signatures,
        )

    def _iter_pipeline_job_records_for_cycle(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        include_direct: bool = True,
    ) -> Iterable[dict[str, Any]]:
        """Memoized cycle-scoped replay, invalidated by THIS cycle's files only.

        Design D10, which is D7's pre-registered contingency now fired. The
        memo is keyed on ``(source_id, cycle, include_direct, source_segments)``
        — ``include_direct`` is part of the key because
        ``_pipeline_job_for_id_unlocked`` replays with it false while every
        other narrowed entrypoint replays with it true, and the two must not
        collide. Validation is by ``_cycle_job_records_signature``, whose
        scoping is the whole point of the memo.

        Lock discipline follows ``_direct_pipeline_job_records_for_cycle_cached``
        exactly (design D7 / spec ``pipeline-job-persistence`` L550): the
        signature is computed OUTSIDE ``_cache_lock`` because it does IO,
        lookup/store/eviction are the only critical sections, and nothing under
        the cache mutex takes another lock — the single lock order is
        unchanged.

        Rows are cloned on both store and serve, matching the sibling cache, so
        a caller mutating a returned row cannot poison the entry.
        """

        with journal_read_lane("cycle_replay"):
            return self._cycle_job_records_memoized(
                source_id=source_id,
                cycle_time=cycle_time,
                include_direct=include_direct,
            )

    def _cycle_job_records_memoized(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        include_direct: bool,
    ) -> list[dict[str, Any]]:
        source_segments = _cycle_read_source_segments(
            source_id=source_id,
            source_segment_override=None,
            root=self.root,
        )
        cycle_segment = format_cycle_time(cycle_time)
        cache_key = (source_id, cycle_segment, include_direct, source_segments)
        signature = self._cycle_job_records_signature(
            source_id=source_id,
            cycle_time=cycle_time,
            source_segments=source_segments,
            cycle_segment=cycle_segment,
            include_direct=include_direct,
        )
        with self._cache_lock:
            cached = self._cycle_job_records_cache.get(cache_key)
        if cached is not None and cached[0] == signature:
            return [dict(job) for job in cached[1]]
        jobs = list(
            self._replay_pipeline_job_records_for_cycle(
                source_id=source_id,
                cycle_time=cycle_time,
                source_segments=source_segments,
                cycle_segment=cycle_segment,
                include_direct=include_direct,
            )
        )
        cache_limit = max(int(MAX_FILE_JOURNAL_CYCLE_ROWS_CACHE_ENTRIES), 1)
        entry = (signature, [dict(job) for job in jobs])
        with self._cache_lock:
            if cache_key not in self._cycle_job_records_cache and len(self._cycle_job_records_cache) >= cache_limit:
                self._cycle_job_records_cache.pop(next(iter(self._cycle_job_records_cache)), None)
            self._cycle_job_records_cache[cache_key] = entry
        return jobs

    def _replay_pipeline_job_records_for_cycle(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        source_segments: tuple[str, ...],
        cycle_segment: str,
        include_direct: bool = True,
    ) -> Iterable[dict[str, Any]]:
        """Replay one cycle's pipeline jobs through the whole-tree merge path.

        This is a narrowing of the INPUT SET only (#1734 design D2): the same
        record sources as ``_iter_pipeline_job_records``, restricted to the
        cycle that owns the row — that cycle's ``latest/<segment>/<cycle>/``
        views, that cycle's ``journal/<segment>/<cycle>*.jsonl`` segments and
        the same flat ``pipeline-jobs/`` direct records — merged by the same
        ``_apply_journal_record`` last-write-wins ordering and yielding rows in
        the same order, so the result is the whole-tree answer filtered by the
        owning cycle.

        It deliberately does NOT route to
        ``_direct_pipeline_job_records_for_cycle_cached``: the
        ``pipeline-jobs/by-cycle/`` partition holds only current
        accepted-submit *candidate* rows (``_write_pipeline_job_direct_unlocked``),
        so every cohort master row and every non-forecast-stage row would be
        silently dropped.

        The flat ``pipeline-jobs/`` direct source is filtered by file name
        (design D2a) rather than read whole, with the filter falling open on
        any name it cannot parse — see
        ``_iter_flat_direct_pipeline_job_records_for_cycle``.

        Source segment aliases are resolved through
        ``_cycle_read_source_segments`` because run identifiers spell the
        source lower case while ``latest/``/``journal/`` carry the normalised
        casing (``gfs`` but ``IFS``/``ERA5``).
        """

        jobs: dict[str, dict[str, Any]] = {}
        budget = _RecordBudget(max(self.max_records, 1), "pipeline_job_records")
        for path in sorted(
            path
            for segment in source_segments
            for path in _iter_regular_json_files(
                self.root / "latest" / segment / cycle_segment,
                root=self.root,
                recursive=True,
                max_files=self.max_files,
                max_depth=self.max_depth,
            )
        ):
            payload = self._read_optional_json(path)
            if payload is None:
                continue
            path_source_id, path_cycle_time, model_id = _latest_identity_from_path(path, root=self.root)
            rows = _CycleRows()
            self._apply_latest_view(
                rows,
                payload,
                source_id=path_source_id,
                cycle_time=path_cycle_time,
                expected_model_id=model_id,
            )
            for job in rows.pipeline_jobs.values():
                budget.consume()
                _upsert_by_key(jobs, job, key="job_id")
        for path in sorted(
            (
                path
                for segment in source_segments
                for path in _iter_jsonl_files(
                    self.root / "journal" / segment,
                    root=self.root,
                    max_files=self.max_files,
                    max_depth=self.max_depth,
                )
                if _journal_segment_stem_in_cycle_scope(path.stem, cycle_segment)
            ),
            key=lambda candidate: _journal_segment_sort_key(candidate, root=self.root, surface="journal"),
        ):
            path_source_id, path_cycle_time = _journal_identity_from_path(path, root=self.root, surface="journal")
            for record in self._read_jsonl(path):
                budget.consume()
                rows = _CycleRows()
                self._apply_journal_record(
                    rows,
                    record,
                    source_id=path_source_id,
                    cycle_time=path_cycle_time,
                )
                for job in rows.pipeline_jobs.values():
                    _upsert_by_key(jobs, job, key="job_id")
        if include_direct:
            for job in self._iter_flat_direct_pipeline_job_records_for_cycle(
                source_id=source_id,
                cycle_time=cycle_time,
            ):
                budget.consume()
                _insert_missing_by_key(jobs, job, key="job_id")
        yield from jobs.values()

    def _iter_pipeline_job_records_scoped(
        self,
        cycle_scope: tuple[str, datetime] | None,
        *,
        include_direct: bool = True,
    ) -> Iterable[dict[str, Any]]:
        """Cycle-scoped replay when the key named a cycle, whole tree otherwise.

        ``cycle_scope is None`` is the fall-open path (#1734 design D4): an
        underivable key costs the old full scan, never a false "not found".
        """

        if cycle_scope is None:
            return self._iter_pipeline_job_records(include_direct=include_direct)
        source_id, cycle_time = cycle_scope
        return self._iter_pipeline_job_records_for_cycle(
            source_id=source_id,
            cycle_time=cycle_time,
            include_direct=include_direct,
        )

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
                        "completed_at": _format_utc(_utcnow()),
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
                        unlink_no_follow(marker_path, containment_root=self.root)
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
                        unlink_no_follow(rollforward_path, containment_root=self.root)
                    except (FileNotFoundError, OSError, SafeFilesystemError) as error:
                        raise FileOrchestrationJournalError(
                            "file_journal_rollback_preparation_unavailable",
                            field="reconcile_inventory_rollforward_receipt",
                        ) from error
                prepared_at = _format_utc(_utcnow())
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
                    unlink_no_follow(marker_path, containment_root=self.root)
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
                        "completed_at": _format_utc(_utcnow()),
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
                        "completed_at": _format_utc(_utcnow()),
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
                    unlink_no_follow(fence_path, containment_root=self.root)
                except (FileNotFoundError, OSError, SafeFilesystemError) as error:
                    raise FileOrchestrationJournalError(
                        "file_journal_rollforward_fence_consume_failed",
                        field="reconcile_inventory_rollback_receipt",
                    ) from error
                self._cleanup_reconcile_migration_temp_residues_unlocked()
                self._reconcile_inventory_migration_checked = True
                return dict(completed)

    def _require_scheduler_lease_guard(self, guard: Callable[[], bool]) -> None:
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
        _coerce_datetime(receipt.get("prepared_at"), field="prepared_at")
        _coerce_datetime(preflight.get("checked_at"), field="checked_at")
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
        _coerce_datetime(receipt.get("completed_at"), field="completed_at")
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
        paths = [
            *self._iter_reconcile_direct_pipeline_job_paths(),
            *self._iter_migration_legacy_active_paths(),
            *self._iter_migration_journal_paths(),
        ]
        entries: list[tuple[str, int, int]] = []
        for path in sorted(paths):
            try:
                metadata = stat_no_follow(path, containment_root=self.root)
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
        directory = self.root / _RECONCILE_INVENTORY_DIRECTORY
        try:
            entry_names = list_directory_no_follow_limited(
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
    ) -> tuple[list[dict[str, Any]], bool]:
        """Scan the reconcile inventory for occupancy and durable-claimant overlap.

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

        Reads only reconcile-inventory anchors and each anchor's exact
        canonical row; never enumerates the whole tree (#1850 D3). The caller
        must hold ``_write_lock`` AND ``_reconcile_inventory_file_lock_unlocked``
        through this scan and the subsequent bind write, so the scan+bind is
        atomic across processes (cycle lock -> inventory lock order).
        """

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
            if (
                fallback_unique
                and other_owner == active_slurm_job_id
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
                if _settled_incarnation_matches_candidate(canonical, candidate_submit):
                    # Same accounting incarnation (or unprovable recycle): the
                    # exact accounting row the fallback query returned, settled
                    # or not. The stale anchor must not let the fallback reuse
                    # it.
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
            if other_owner and other_owner == active_slurm_job_id:
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
                    if str(canonical.get("slurm_job_id") or "") != active_slurm_job_id:
                        # Stale projection (either direction): the canonical
                        # authority holds a different id — no same-id
                        # occupancy from this cycle.
                        continue
                    if str(canonical.get("status") or "") in TERMINAL_PIPELINE_STATUSES:
                        # #1850 Fix C: the SAME centralized incarnation
                        # adjudication as the anchor surface, so the two can
                        # never drift. Missing/malformed canonical Submit and
                        # every non-name-window provenance block fail-closed.
                        if _settled_incarnation_matches_candidate(canonical, candidate_submit):
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
        prefixes = (
            f".{_RECONCILE_INVENTORY_MIGRATION_MARKER}.",
            f".{_RECONCILE_INVENTORY_ROLLBACK_PREP_RECEIPT}.",
            f".{_RECONCILE_INVENTORY_ROLLFORWARD_RECEIPT}.",
        )
        try:
            entry_names = list_directory_no_follow_limited(
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
        try:
            mode = stat_no_follow(path, containment_root=self.root).st_mode
            if not stat.S_ISREG(mode):
                raise SafeFilesystemError(f"Atomic residue must be a regular file: {path}")
            unlink_no_follow(path, containment_root=self.root)
        except FileNotFoundError:
            return
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_reconcile_inventory_unavailable",
                field=field,
            ) from error

    def _backfill_reconcile_inventory_unlocked(self) -> None:
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
        records = self._read_jsonl(path, segment_index=segment_index)
        try:
            mode = stat_no_follow(path, containment_root=self.root).st_mode
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
        signatures = {path: _stat_signature(path) for path in watched_paths}
        latest_directory_signature = _stat_signature(latest_directory)
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
            any(signature != _stat_signature(path) for path, signature in signatures.items())
            or latest_directory_signature != _stat_signature(latest_directory)
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
            direct_missing = stat_no_follow(direct_path, containment_root=self.root) is None
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
        with self._reconcile_inventory_file_lock_unlocked():
            try:
                unlink_no_follow(
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
        directory = self.root / "pipeline-jobs"
        if self.max_files <= 0 or self.max_depth < 0:
            return
        if (
            expected_root_signature is not _UNSET
            and _stat_signature(directory) != expected_root_signature
        ):
            raise FileOrchestrationJournalError(
                "file_journal_quiescence_authority_changed",
                field="pipeline_jobs",
            )
        try:
            directory_mode = stat_no_follow(directory, containment_root=self.root).st_mode
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
            entry_names = list_directory_no_follow_limited(
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
            and _stat_signature(directory) != expected_root_signature
        ):
            raise FileOrchestrationJournalError(
                "file_journal_quiescence_authority_changed",
                field="pipeline_jobs",
            )
        for entry_name in sorted(entry_names):
            if entry_name == "by-cycle":
                try:
                    mode = stat_no_follow(directory / entry_name, containment_root=self.root).st_mode
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
                mode = stat_no_follow(path, containment_root=self.root).st_mode
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

    def _model_context(self, model_id: str) -> dict[str, Any] | None:
        payload = self._read_optional_json(self.root / "models" / f"{_safe_segment(model_id)}.json")
        if payload is not None:
            return self._validated_direct_model_context_record(payload, expected_model_id=model_id)
        for latest in _iter_regular_json_files(
            self.root / "latest",
            root=self.root,
            recursive=True,
            max_files=self.max_files,
            max_depth=self.max_depth,
        ):
            view = self._read_optional_json(latest)
            if view is None:
                continue
            source_id, cycle_time, latest_model_id = _latest_identity_from_path(latest, root=self.root)
            rows = _CycleRows()
            self._apply_latest_view(
                rows,
                view,
                source_id=source_id,
                cycle_time=cycle_time,
                expected_model_id=latest_model_id,
            )
            model_context = rows.model_context
            if model_context is not None and str(model_context.get("model_id") or "") == model_id:
                return model_context
        return None

    def _forcing_context(self, *, source_id: str, cycle_time: datetime, model_id: str) -> dict[str, Any] | None:
        path = (
            self.root
            / "forcing"
            / _safe_segment(source_id)
            / format_cycle_time(cycle_time)
            / f"{_safe_segment(model_id)}.json"
        )
        payload = self._read_optional_json(path)
        if payload is not None:
            return self._validated_direct_forcing_context_record(
                payload,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
            )
        return None

    def _validated_direct_model_context_record(
        self,
        record: Mapping[str, Any],
        *,
        expected_model_id: str,
    ) -> dict[str, Any]:
        _require_schema(record, FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION)
        payload = _payload_or_record_payload(record)
        record_type = _record_type(record, payload)
        if record_type != "model_context":
            raise FileOrchestrationJournalError(
                "file_journal_record_type_mismatch",
                field="record_type",
                evidence={"expected": "model_context", "actual": record_type[:80]},
            )
        _require_record_payload_identity_match(record_type, record, payload)
        record_model_id = _explicit_record_model_id(record, payload)
        if record_model_id in (None, ""):
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="model_id")
        if record_model_id != expected_model_id:
            raise FileOrchestrationJournalError(
                "file_journal_model_mismatch",
                field="model_id",
                evidence={"expected": expected_model_id, "actual": record_model_id[:80]},
            )
        _validate_model_context_identity(payload, model_id=expected_model_id)
        return payload

    def _validated_direct_forcing_context_record(
        self,
        record: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
    ) -> dict[str, Any]:
        _require_schema(record, FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION)
        payload = _payload_or_record_payload(record)
        record_type = _record_type(record, payload)
        if record_type != "forcing_version":
            raise FileOrchestrationJournalError(
                "file_journal_record_type_mismatch",
                field="record_type",
                evidence={"expected": "forcing_version", "actual": record_type[:80]},
            )
        _require_record_payload_identity_match(record_type, record, payload)
        _require_source_cycle(record, source_id=source_id, cycle_time=cycle_time)
        _require_model_id(record, model_id, required=True)
        _validate_forcing_version_identity(
            payload,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            require_source_cycle=True,
            require_model_id=True,
            require_forcing_version_id=True,
        )
        return payload

    def _validated_direct_pipeline_job_record(
        self,
        record: Mapping[str, Any],
        *,
        expected_job_id: str,
    ) -> dict[str, Any]:
        _require_schema(record, FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION)
        payload = _payload_or_record_payload(record)
        record_type = str(record.get("record_type") or payload.get("record_type") or "")
        if record_type != "pipeline_job":
            raise FileOrchestrationJournalError(
                "file_journal_record_type_mismatch",
                field="record_type",
                evidence={"expected": "pipeline_job", "actual": record_type[:80]},
            )
        _require_record_payload_identity_match(record_type, record, payload)
        source_id = _required_source_id(record, "source_id")
        cycle_time = _parse_cycle_time_field(record, "cycle_time")
        model_id = _record_model_id(record, payload, source_id=source_id, cycle_time=cycle_time)
        _validate_pipeline_job_identity(
            payload,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            expected_job_id=expected_job_id,
        )
        _validate_accepted_submit_evidence(payload)
        return payload

    def _write_hydro_run(self, row: Mapping[str, Any], *, retriable_only: bool) -> dict[str, Any]:
        row = _redact_durable_error_message_fields("hydro_run", row)
        source_id = _required_source_id(row, "source_id")
        cycle_time = _parse_cycle_time_field(row, "cycle_time")
        model_id = _required_safe_identity(row, "model_id")
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._hydro_run_for(str(row["run_id"]))
            if (
                retriable_only
                and existing is not None
                and str(existing.get("status") or "")
                not in {
                    "failed",
                    "cancelled",
                }
            ):
                raise OrchestratorError(
                    "HYDRO_RUN_NOT_RETRIABLE",
                    f"hydro_run already exists and is not retriable: {row['run_id']}",
                )
            self._append_validated_record_unlocked(
                "hydro_run",
                row,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
                materialize_model_id=model_id,
            )
            return _public_scheduler_row(row)

    def _hydro_run_for(self, run_id: str) -> dict[str, Any] | None:
        """Return the exact durable hydro row, never a public projection.

        Every production caller of this private lookup uses it as the base of
        a durable merge or read, so rendering ``_public_scheduler_row`` here
        would hand placeholders back into the write path and erase real
        attribution on a non-clearing update.  Public redaction belongs only
        at the explicit return/query boundaries (``update_hydro_run_status``,
        ``create_hydro_run_from_basin``, ``append_historical_hydro_run``), which
        render through ``_public_scheduler_row`` themselves.  The returned row
        is a copy; callers that mutate it must write it back under the same
        cycle lock this read is performed under.
        """
        safe_run_id = _safe_identity_text(str(run_id), field="run_id")
        match = _FORECAST_RUN_ID_RE.fullmatch(safe_run_id)
        model_id = _model_id_from_file_run_id(safe_run_id)
        if match is None:
            match = _CYCLE_COHORT_RUN_ID_RE.fullmatch(safe_run_id)
        if match is None:
            return None
        run_source, run_cycle = match.group(1), match.group(2)
        source_id = _normalize_file_source_id(run_source, field="run_id")
        cycle_time = parse_cycle_time(run_cycle)
        rows = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        if rows.hydro_run is not None and str(rows.hydro_run.get("run_id") or "") == safe_run_id:
            return dict(rows.hydro_run)
        return None

    def _pipeline_job_row(
        self,
        record: Mapping[str, Any],
        *,
        validate_accepted_submit: bool = True,
    ) -> dict[str, Any]:
        cycle_id = _required_safe_identity(record, "cycle_id")
        source_id, cycle_time = _source_cycle_from_cycle_id(cycle_id)
        if validate_accepted_submit:
            _validate_accepted_submit_evidence({**record, "source_id": source_id})
        now = _format_utc(_utcnow())
        row = {
            "job_id": _required_safe_identity(record, "job_id"),
            "run_id": _required_safe_identity(record, "run_id"),
            "cycle_id": cycle_id,
            "source_id": source_id,
            "cycle_time": _format_utc(cycle_time),
            "job_type": _required_text(record, "job_type"),
            "slurm_job_id": record.get("slurm_job_id"),
            "array_task_id": record.get("array_task_id"),
            "model_id": record.get("model_id"),
            "status": str(record.get("status") or "pending"),
            "stage": record.get("stage"),
            "idempotency_key": record.get("idempotency_key"),
            "candidate_id": record.get("candidate_id"),
            "submitted_at": _optional_format_datetime(record.get("submitted_at"), field="submitted_at"),
            "started_at": _optional_format_datetime(record.get("started_at"), field="started_at"),
            "finished_at": _optional_format_datetime(record.get("finished_at"), field="finished_at"),
            "exit_code": record.get("exit_code"),
            "retry_count": record.get("retry_count", 0),
            "manual_retry_marker": bool(record.get("manual_retry_marker", False)),
            "previous_job_id": _optional_safe_identity(record, "previous_job_id"),
            "error_code": record.get("error_code"),
            "error_message": _durable_error_message(record.get("error_message")),
            "log_uri": record.get("log_uri"),
            "submit_outcome": record.get("submit_outcome"),
            "slurm_comment": record.get("slurm_comment"),
            "cohort_members": _bounded_cohort_members(record.get("cohort_members")),
            "cohort_digest": record.get("cohort_digest"),
            # Explicit member of the closed constructor (#1183): a key absent
            # here is silently dropped from the reservation write, and the
            # later frozen-value check would then raise on the phantom change.
            INIT_STATE_IDENTITY_FIELD: _bounded_init_state_identities(
                record.get(INIT_STATE_IDENTITY_FIELD)
            ),
            # Explicit member of the closed constructor for the same reason
            # (#1157): absent here the reservation's provenance stamp would be
            # dropped on write and the later frozen-value check would raise on
            # the phantom change.
            QUARANTINE_RERUN_PROVENANCE_FIELD: normalize_quarantine_rerun_model_ids(
                record.get(QUARANTINE_RERUN_PROVENANCE_FIELD)
            ),
            "restart_stage": record.get("restart_stage"),
            # #1748: the operator-recovery attestation.  Deliberately absent from
            # ``_PIPELINE_JOB_UPSERT_MUTABLE_FIELDS`` so the generic upsert can
            # neither forge nor strip it -- only the typed recovery API writes it.
            OPERATOR_RECOVERY_ATTESTATION_FIELD: _optional_format_datetime(
                record.get(OPERATOR_RECOVERY_ATTESTATION_FIELD),
                field=OPERATOR_RECOVERY_ATTESTATION_FIELD,
            ),
            "submission_attempt": record.get("submission_attempt", 1),
            "submission_attempt_started_at": _optional_format_datetime(
                record.get("submission_attempt_started_at"), field="submission_attempt_started_at"
            ),
            "expected_slurm_user": record.get("expected_slurm_user"),
            "expected_slurm_account": record.get("expected_slurm_account"),
            "slurm_ownership_required": bool(record.get("slurm_ownership_required", False)),
            "reconciliation_source": record.get("reconciliation_source"),
            "reconciliation_decision": record.get("reconciliation_decision"),
            "reconciliation_reason_class": record.get("reconciliation_reason_class"),
            "matched_slurm_job_id": record.get("matched_slurm_job_id"),
            # #1850 Fix A: attempt-scoped binding provenance rides the closed
            # constructor like every other durable master field, so it survives
            # replay and is never silently dropped by a write boundary.
            SLURM_BINDING_SOURCE_FIELD: record.get(SLURM_BINDING_SOURCE_FIELD),
            SLURM_ACCOUNTING_SUBMITTED_AT_FIELD: _optional_format_datetime(
                record.get(SLURM_ACCOUNTING_SUBMITTED_AT_FIELD),
                field=SLURM_ACCOUNTING_SUBMITTED_AT_FIELD,
            ),
            "candidate_projections": _bounded_candidate_projections(record.get("candidate_projections")),
            "cancellation_receipt_recorded": record.get("cancellation_receipt_recorded", False),
            "identity_blocked_streak": record.get("identity_blocked_streak", 0),
            "native_shud_resubmitted": record.get("native_shud_resubmitted"),
            "created_at": _optional_format_datetime(record.get("created_at"), field="created_at") or now,
            "updated_at": _optional_format_datetime(record.get("updated_at"), field="updated_at") or now,
        }
        if ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD in record:
            row[ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD] = record.get(
                ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD
            )
        _validate_pipeline_job_identity(
            row,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=_optional_safe_identity(row, "model_id"),
        )
        if validate_accepted_submit:
            _validate_accepted_submit_evidence(row)
        return row

    def _write_pipeline_job(self, row: Mapping[str, Any], *, exclusive_direct: bool) -> dict[str, Any] | None:
        source_id = _source_id_from_job(row)
        cycle_time = _cycle_time_from_job(row)
        model_id = _optional_safe_identity(row, "model_id")
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            return self._write_pipeline_job_unlocked(row, exclusive_direct=exclusive_direct, model_id=model_id)

    def _project_committed_pipeline_job_write(
        self,
        row: Mapping[str, Any],
        record: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str | None,
    ) -> None:
        """Run every derived projection after a committed reservation/reclaim append.

        #1796 (generalizing #1564 D9): the authority append in
        ``_write_pipeline_job_unlocked`` / ``_write_current_master_unlocked`` is
        the commit point of the new reservation attempt.  A direct/inventory or
        latest projection fault after it is contained to ONE bounded non-secret
        warning per failed projection (fixed code/reason tokens + validated
        projection/model identity only -- never exception text, class, path,
        ``.error_code``, ``.reason``, or secret-shaped detail), plus a
        best-effort durable ``committed_projection_fault`` event whose append
        never re-invokes the failed projection.  Every remaining independent
        projection is still attempted and the committed result is always
        returned: a later pass must never see a falsely-failed write that
        strands a live pre-sbatch ``reserved`` row or wedges the cycle.
        """

        try:
            self._write_pipeline_job_direct_unlocked(row, record)
        except Exception as error:  # noqa: BLE001 - bounded committed-warning containment.
            _emit_reclaim_projection_warning("pipeline_job_direct", None, error)
            self._emit_committed_projection_fault_event(
                row,
                projection="pipeline_job_direct",
                projection_model_id=None,
                error=error,
                source_id=source_id,
                cycle_time=cycle_time,
            )
        if model_id is not None:
            try:
                self._materialize_latest_unlocked(
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                )
            except Exception as error:  # noqa: BLE001 - bounded committed-warning containment.
                _emit_reclaim_projection_warning("latest", model_id, error)
                self._emit_committed_projection_fault_event(
                    row,
                    projection="latest",
                    projection_model_id=model_id,
                    error=error,
                    source_id=source_id,
                    cycle_time=cycle_time,
                )

    def _emit_committed_projection_fault_event(
        self,
        row: Mapping[str, Any],
        *,
        projection: str,
        projection_model_id: str | None,
        error: Exception,
        source_id: str,
        cycle_time: datetime,
    ) -> None:
        """Best-effort durable bounded evidence for one committed projection fault.

        The authoritative append is already durable, so a failure here must
        never surface: the process warning and the committed reservation are the
        final fallback.  ``materialize=False`` keeps the append from
        re-invoking the projection that just failed (the event lands without
        re-entering the failed direct/latest write), and the payload carries
        only fixed tokens plus validated identity.
        """

        del error  # the exception is used only to decide that it must not surface.
        try:
            self._append_pipeline_job_event_unlocked(
                row,
                source_id=source_id,
                cycle_time=cycle_time,
                event_type=COMMITTED_PROJECTION_FAULT_EVENT,
                status_from=str(row.get("status") or ""),
                status_to=str(row.get("status") or ""),
                message=(
                    f"committed reservation/reclaim projection fault: "
                    f"projection={projection} code=FILE_JOURNAL_RECLAIM_PROJECTION_FAULT "
                    "the reservation is committed; journal replay is authoritative"
                ),
                details={
                    "projection": projection,
                    "model_id": projection_model_id,
                    "reason": _PROJECTION_WARNING_FALLBACK_TOKEN,
                    "error_type": _PROJECTION_WARNING_FALLBACK_TOKEN,
                    "submission_attempt": row.get("submission_attempt"),
                    "submission_attempt_started_at": row.get("submission_attempt_started_at"),
                    "committed": True,
                },
                materialize=False,
            )
        except Exception:  # noqa: BLE001 - observability must never fail the committed reservation.
            pass

    def _write_pipeline_job_unlocked(
        self,
        row: Mapping[str, Any],
        *,
        exclusive_direct: bool,
        model_id: str | None,
        _committed_projection_containment: bool = False,
    ) -> dict[str, Any] | None:
        row = _redact_durable_error_message_fields("pipeline_job", row)
        source_id = _source_id_from_job(row)
        cycle_time = _cycle_time_from_job(row)
        row = {**row, "source_id": source_id}
        if exclusive_direct and self._pipeline_job_conflicts_unlocked(row):
            return None
        sequence = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)
        record = _journal_record_for_write(
            "pipeline_job",
            row,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            sequence=sequence,
        )
        self._validate_outgoing_record(
            record,
            source_id=source_id,
            cycle_time=cycle_time,
            record_type="pipeline_job",
            model_id=model_id,
        )
        is_current_master = bool(
            accepted_submit_contract_is_current(row) and accepted_submit_row_kind(row) == "master"
        )
        # Fix C: the journal-global inventory lock (cycle -> inventory order,
        # reentrant via depth) serializes every write that CREATES or CHANGES
        # a bound owner, or CREATES/RECLAIMS a reserved claimant — the write
        # shapes the occupancy scan in ``commit_pipeline_job_submit_attempt``
        # must never observe mid-write. NOT every current accepted-submit
        # master write reaches this section: batch terminal/rejection/operator
        # transitions append their journal records directly (bypassing this
        # helper) and are safe precisely because they never introduce an
        # owner or claimant — they only settle an existing row, and a stale
        # reconcile-inventory anchor left behind is adjudicated against the
        # canonical journal (accounting-incarnation check) rather than being
        # read as vacancy. Non-master and legacy writes are unchanged.
        if is_current_master:
            return self._write_current_master_unlocked(
                row=row,
                record=record,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
                _committed_projection_containment=_committed_projection_containment,
            )
        anchor_path = self.root / _RECONCILE_INVENTORY_DIRECTORY / f"{_required_safe_identity(row, 'job_id')}.json"
        anchor_preexisting = self._reconcile_inventory_anchor_exists_unlocked(anchor_path)
        anchor_published = False
        if _reconcile_inventory_row_kind(row) is not None and _job_needs_restart_reconcile(row):
            anchor_published = self._sync_reconcile_inventory_for_row_unlocked(row)
        try:
            self._append_journal_record_unlocked(source_id=source_id, cycle_time=cycle_time, record=record)
        except Exception:
            if anchor_published and not anchor_preexisting:
                self._remove_reconcile_inventory_anchor_unlocked(str(row["job_id"]))
            raise
        if _committed_projection_containment:
            self._project_committed_pipeline_job_write(
                row,
                record,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
            )
            return _public_scheduler_row(row)
        self._write_pipeline_job_direct_unlocked(row, record)
        if model_id is not None:
            self._materialize_latest_unlocked(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        # Known boundary (#1592): this projects the UNSTRIPPED ``row``.  It is a
        # caller-facing return value, not durable state -- the durable record and
        # direct row file were both built from the stripped record above.  A
        # caller that round-trips this value back into a write is caught by the
        # strip at the write boundary, so it cannot become persistent pollution.
        return _public_scheduler_row(row)

    def _write_current_master_unlocked(
        self,
        *,
        row: Mapping[str, Any],
        record: Mapping[str, Any],
        source_id: str,
        cycle_time: datetime,
        model_id: str | None,
        _committed_projection_containment: bool,
    ) -> dict[str, Any] | None:
        """The current accepted-submit master commit section, under the global
        inventory lock. Caller already holds ``_write_lock`` and the cycle
        flock; the inventory lock is acquired in cycle -> inventory order and
        its reentrancy (depth counter) keeps the nested anchor sync safe."""

        with self._reconcile_inventory_file_lock_unlocked():
            anchor_path = self.root / _RECONCILE_INVENTORY_DIRECTORY / f"{_required_safe_identity(row, 'job_id')}.json"
            anchor_preexisting = self._reconcile_inventory_anchor_exists_unlocked(anchor_path)
            anchor_published = False
            if _reconcile_inventory_row_kind(row) is not None and _job_needs_restart_reconcile(row):
                anchor_published = self._sync_reconcile_inventory_for_row_unlocked(row)
            try:
                self._append_journal_record_unlocked(source_id=source_id, cycle_time=cycle_time, record=record)
            except Exception:
                if anchor_published and not anchor_preexisting:
                    self._remove_reconcile_inventory_anchor_unlocked(str(row["job_id"]))
                raise
            if _committed_projection_containment:
                self._project_committed_pipeline_job_write(
                    row,
                    record,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                )
                return _public_scheduler_row(row)
            self._write_pipeline_job_direct_unlocked(row, record)
            if model_id is not None:
                self._materialize_latest_unlocked(
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                )
            # Known boundary (#1592): this projects the UNSTRIPPED ``row``.  It
            # is a caller-facing return value, not durable state -- the durable
            # record and direct row file were both built from the stripped
            # record above.
            return _public_scheduler_row(row)

    def _write_pipeline_job_direct_unlocked(
        self,
        row: Mapping[str, Any],
        record: Mapping[str, Any],
    ) -> None:
        source_id = _source_id_from_job(row)
        cycle_time = _cycle_time_from_job(row)
        job_id = _required_safe_identity(row, "job_id")
        if accepted_submit_contract_is_current(row) and accepted_submit_row_kind(row) == "candidate":
            direct_path = (
                self.root
                / "pipeline-jobs"
                / "by-cycle"
                / _safe_segment(source_id)
                / format_cycle_time(cycle_time)
                / f"{job_id}.json"
            )
        else:
            direct_path = self.root / "pipeline-jobs" / f"{job_id}.json"
        self._atomic_write_json_unlocked(direct_path, record)
        self._sync_reconcile_inventory_for_row_unlocked(row)
        with self._cache_lock:
            self._direct_jobs_cycle_cache.pop((source_id, format_cycle_time(cycle_time)), None)

    def _restore_derived_master_direct_unlocked(
        self,
        canonical: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> bool:
        """Repair a missing derived flat master direct from canonical authority.

        Phase 6h handoff: a batch terminal projection can commit the canonical
        cycle journal and then fail the derived direct write, leaving the
        reconcile-inventory anchor stale (row settled) with NO flat master
        direct. The bounded fallback scan locates cycles ONLY through the flat
        master surface, so pruning the stale anchor before restoring the direct
        would open a window where the same accounting incarnation reads as
        vacant. This helper re-materializes the derived direct from the
        canonical row (journal remains the sole authority — no new journal
        record is appended) and then syncs the anchor (which removes it for a
        settled row). If the direct write fails, the anchor sync never runs and
        the anchor is KEPT — fail closed, never prune first.

        Caller must hold ``_write_lock``, the cycle flock for ``(source_id,
        cycle_time)``, and (via the nested anchor sync) the reentrant
        journal-global inventory lock, so no competing fallback scan+bind can
        observe both locator surfaces absent between the restore and the
        prune. Returns True when a direct was restored, False when the direct
        already exists (no repair needed).
        """

        job_id = _required_safe_identity(canonical, "job_id")
        direct_path = self.root / "pipeline-jobs" / f"{job_id}.json"
        try:
            if stat_no_follow(direct_path, containment_root=self.root) is not None:
                return False
        except FileNotFoundError:
            pass
        model_id = _optional_safe_identity(canonical, "model_id")
        sequence = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)
        record = _journal_record_for_write(
            "pipeline_job",
            canonical,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            sequence=sequence,
        )
        self._validate_outgoing_record(
            record,
            source_id=source_id,
            cycle_time=cycle_time,
            record_type="pipeline_job",
            model_id=model_id,
        )
        # Writes the direct file then syncs the anchor (removes it for a
        # settled row). If the direct write raises, the sync never runs and
        # the stale anchor is preserved.
        self._write_pipeline_job_direct_unlocked(canonical, record)
        return True

    def _reconcile_inventory_anchor_exists_unlocked(self, path: Path) -> bool:
        try:
            mode = stat_no_follow(path, containment_root=self.root).st_mode
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

    def _pipeline_job_conflicts_unlocked(self, row: Mapping[str, Any]) -> bool:
        job_id = str(row.get("job_id") or "")
        if accepted_submit_contract_is_current(row) and accepted_submit_row_kind(row) == "master":
            source_id = _source_id_from_job(row)
            cycle_time = _cycle_time_from_job(row)
            return bool(
                job_id
                and self._accepted_submit_job_for_id_unlocked(
                    job_id,
                    source_id=source_id,
                    cycle_time=cycle_time,
                )
                is not None
            )
        if job_id and self.get_pipeline_job(job_id) is not None:
            return True
        idempotency_key = row.get("idempotency_key")
        return idempotency_key not in (None, "") and self.query_candidate_state(str(idempotency_key)) is not None

    def _append_validated_record(
        self,
        record_type: str,
        payload: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str | None = None,
        materialize_model_id: str | None = None,
        sequence: int | None = None,
    ) -> None:
        source_id = _normalize_file_source_id(source_id, field="source_id")
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            self._append_validated_record_unlocked(
                record_type,
                payload,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
                materialize_model_id=materialize_model_id,
                sequence=sequence,
            )

    def _append_validated_record_unlocked(
        self,
        record_type: str,
        payload: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str | None = None,
        materialize_model_id: str | None = None,
        sequence: int | None = None,
    ) -> None:
        # Caller-boundary strip: this sees the RAW payload, before the public
        # event rendering below.  For pipeline_event this is the only
        # anti-laundering layer there is -- ``_journal_record_for_write``
        # deliberately skips events so it cannot erase that rendering's
        # intentional placeholders (#1592 design D2b).  Do not remove it.
        payload = _strip_redaction_placeholders(payload)
        payload = _redact_durable_error_message_fields(record_type, payload)
        private_recovery_payload = dict(payload) if record_type == "pipeline_event" else None
        if record_type == "pipeline_event":
            payload = _public_pipeline_event_payload(payload)
        record_sequence = sequence or self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)
        record = _journal_record_for_write(
            record_type,
            payload,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            sequence=record_sequence,
        )
        self._validate_outgoing_record(
            record,
            source_id=source_id,
            cycle_time=cycle_time,
            record_type=record_type,
            model_id=model_id,
        )
        if private_recovery_payload is not None:
            self._write_pipeline_event_private_recovery_unlocked(
                private_recovery_payload,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
            )
        self._append_journal_record_unlocked(source_id=source_id, cycle_time=cycle_time, record=record)
        if materialize_model_id is not None:
            self._materialize_latest_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=materialize_model_id,
            )

    def _write_pipeline_event_private_recovery_unlocked(
        self,
        payload: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str | None,
    ) -> None:
        record = _private_runtime_root_recovery_record(
            payload,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
        )
        if record is None:
            return
        path = _private_runtime_root_recovery_path(
            self.root,
            source_id=source_id,
            cycle_time=cycle_time,
            entity_id=str(record["entity_id"]),
            event_id=str(record["event_id"]),
        )
        content = _json_bytes(record)
        self._require_within_byte_limit(content, path)
        self._atomic_write_bytes_unlocked(path, content)

    def _pipeline_event_private_runtime_root_candidates(
        self,
        job: Mapping[str, Any],
        event: Mapping[str, Any],
        *,
        candidate_budget: int,
    ) -> _RuntimeRootCandidateBatch | None:
        event_id = event.get("event_id")
        if event_id in (None, ""):
            return None
        source_id = _source_id_from_job(job)
        cycle_time = _cycle_time_from_job(job)
        path = _private_runtime_root_recovery_path(
            self.root,
            source_id=source_id,
            cycle_time=cycle_time,
            entity_id=str(event.get("entity_id") or ""),
            event_id=str(event_id),
        )
        payload = self._read_optional_json(path)
        if payload is None:
            return None
        try:
            _validate_private_runtime_root_recovery_record(
                payload,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=_optional_safe_identity(job, "model_id"),
                event=event,
            )
        except FileOrchestrationJournalError:
            return None
        candidates_payload = payload.get("candidates")
        if not isinstance(candidates_payload, Sequence) or isinstance(candidates_payload, str | bytes | bytearray):
            return None
        candidates: list[_RuntimeRootCandidate] = []
        total_count = 0
        for item in candidates_payload:
            if not isinstance(item, Mapping):
                return None
            raw_path = item.get("path")
            value = item.get("value")
            if not isinstance(raw_path, Sequence) or isinstance(raw_path, str | bytes | bytearray):
                return None
            if not isinstance(value, Mapping) or not _has_runtime_root_field(value):
                continue
            candidate_path = tuple(str(part) for part in raw_path)
            if not candidate_path:
                return None
            total_count += 1
            if len(candidates) < candidate_budget:
                candidates.append(
                    _RuntimeRootCandidate(
                        f"file_journal_event:{event.get('entity_id')}:{event_id}:{'.'.join(candidate_path)}",
                        dict(value),
                    )
                )
        return _RuntimeRootCandidateBatch(
            candidates=candidates,
            event_candidate_returned_count=len(candidates),
            event_candidate_total_count=total_count,
            event_candidate_omitted_count=max(total_count - len(candidates), 0),
        )

    def _validate_outgoing_record(
        self,
        record: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
        record_type: str,
        model_id: str | None,
    ) -> None:
        """The one write-side validator every journal lane runs before its first byte.

        #1760 D4: for a ``pipeline_job`` record this is also the single
        definition of the job-id scope gate.  It sits BESIDE the
        ``_apply_journal_record`` call below — not inside it — so the read-side
        replay (``_validate_pipeline_job_identity``) keeps its current
        semantics and a historical row that pre-dates the gate is never turned
        into a replay fault.  It runs BEFORE that call so the reason token is
        deterministic when a row diverges in more than one field.
        """

        self._require_job_id_cycle_scope(record, record_type=record_type)
        rows = _CycleRows()
        self._apply_journal_record(
            rows,
            record,
            source_id=source_id,
            cycle_time=cycle_time,
            expected_record_type=record_type,
            expected_model_id=model_id,
        )

    def _require_job_id_cycle_scope(self, record: Mapping[str, Any], *, record_type: str) -> None:
        """Reject a pipeline-job row whose ``job_id`` contradicts its own scope.

        The flat direct file NAME is authoritative for cycle scoping (#1759
        D2a): a cycle-scoped reader of ``pipeline-jobs/`` skips a file whose
        name resolves to another ``(source_id, cycle)``.  This gate makes the
        agreement between that name and the row's content an enforced
        invariant rather than an emergent property, at the one place every
        pipeline-job write lane passes before any byte — journal record or
        direct file — reaches disk.

        The comparison is normalized-to-normalized on both sides and against
        the row's OWN ``source_id`` / ``cycle_time`` (the payload), never the
        lane's kwargs, so a lane that takes the pair as arguments is judged the
        same way as one that derives it from the row.  ``None`` from
        ``_cycle_scope_from_job_id`` — an id matching neither recognised shape
        — passes: the fall-open rule of #1734 D1a is unchanged at the write
        boundary.  A payload whose own pair cannot be derived at all also
        passes here, so the pre-existing identity error keeps its token and is
        raised by ``_apply_journal_record`` where it always was.
        """

        if record_type != "pipeline_job":
            return
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            return
        scope = _cycle_scope_from_job_id(payload.get("job_id"))
        if scope is None:
            return
        try:
            own_pair = (
                _source_id_from_job(payload),
                format_cycle_time(_cycle_time_from_job(payload)),
            )
        except FileOrchestrationJournalError:
            return
        claimed_pair = (scope[0], format_cycle_time(scope[1]))
        if claimed_pair != own_pair:
            raise FileOrchestrationJournalError(
                "file_journal_job_id_scope_mismatch",
                field="job_id",
                evidence={
                    "expected": f"{own_pair[0]}/{own_pair[1]}"[:80],
                    "actual": f"{claimed_pair[0]}/{claimed_pair[1]}"[:80],
                },
            )

    def _next_sequence(self, *, source_id: str, cycle_time: datetime) -> int:
        with self._write_lock:
            return self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)

    def _next_sequence_unlocked(self, *, source_id: str, cycle_time: datetime) -> int:
        source_id = _normalize_file_source_id(source_id, field="source_id")
        cycle_segment = format_cycle_time(cycle_time)
        source_segments = _cycle_read_source_segments(source_id=source_id, source_segment_override=None, root=self.root)
        # #1734 D11: baseline lane — the write paths' sequence floor replay.
        with journal_read_lane("sequence_replay"):
            sequences: list[int] = []
            # Public contract for the insert_pipeline_event lane, which computes the
            # sequence floor before any precondition read; for the other write lanes
            # this is defense in depth behind the read frame's conversion.
            try:
                for source_segment in source_segments:
                    for surface in ("journal", "pipeline-events"):
                        # The floor must span ALL segments: a sequence reused across a
                        # rollover boundary is silent state corruption.
                        segment_paths = self._cycle_segment_paths(self.root / surface / source_segment, cycle_segment)
                        for segment_index, path in enumerate(segment_paths):
                            if not self._sequence_regular_file_exists(path):
                                continue
                            records = self._read_jsonl(path, segment_index=segment_index)
                            sequences.extend((_optional_replay_sequence(record) or 0) for record in records)
                sequences.extend(
                    self._latest_replay_sequences_unlocked(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        source_segments=source_segments,
                    )
                )
            except _JournalProbeContainmentError as error:
                raise _probe_containment_failure(error) from error
        return max(sequences, default=0) + 1

    def _latest_replay_sequences_unlocked(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        source_segments: tuple[str, ...] | None = None,
    ) -> list[int]:
        source_id = _normalize_file_source_id(source_id, field="source_id")
        if source_segments is None:
            source_segments = _cycle_read_source_segments(
                source_id=source_id,
                source_segment_override=None,
                root=self.root,
            )
        # #1734 D11: baseline lane — same family as `_next_sequence_unlocked`.
        with journal_read_lane("sequence_replay"):
            sequences: list[int] = []
            cycle_segment = format_cycle_time(cycle_time)
            for source_segment in source_segments:
                if not self._sequence_directory_exists(self.root / "latest" / source_segment / cycle_segment):
                    continue
                for path in self._latest_paths(source_segment, cycle_segment, model_id=None):
                    payload = self._read_optional_json(path)
                    if payload is None:
                        continue
                    _require_schema(payload, FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION)
                    _require_source_cycle(payload, source_id=source_id, cycle_time=cycle_time)
                    sequences.append(_latest_replay_sequence(payload) or 0)
        return sequences

    def _probe_stat_mode(self, path: Path) -> int | None:
        """Stat one journal slot under the hardened readers' containment rules.

        ``None`` means genuine absence — the entry itself or any parent
        component missing under a chain of real directories, which includes a
        wholly uninitialized journal tree.  A symlinked parent component, a
        symlink occupying the slot, or any other stat fault leaves through the
        carrier: bare ``os.stat(follow_symlinks=False)`` does not follow the
        final component but does follow symlinked parents, which turned a
        tampered tree into "absent".
        """
        try:
            return stat_no_follow(path, containment_root=self.root).st_mode
        except FileNotFoundError:
            return None
        except (SafeFilesystemError, OSError) as error:
            raise _JournalProbeContainmentError(
                field=str(_relative_evidence(path, self.root)),
                error_type=type(error).__name__,
            ) from error

    def _containment_stat_signature(
        self, path: Path
    ) -> tuple[int, int, int] | _FingerprintContainmentFault | None:
        """Stat identity for the cycle-rows fingerprint family, under containment.

        The single helper every stat that feeds ``_cycle_rows_source_fingerprint``
        and ``_cycle_segment_signatures`` routes through (#1567 D1), so the cache
        judges its source files under the same discipline as the hardened
        readers:

        * a real entry -> ``(mtime_ns, size, inode)``;
        * genuine absence under a chain of real directories -> ``None``, so an
          untouched empty directory stays a cacheable legal empty read;
        * a containment fault -> :data:`_FINGERPRINT_CONTAINMENT_FAULT`, which
          makes the whole fingerprint non-cacheable.

        Bare ``os.stat(follow_symlinks=False)`` (``_stat_signature``) does not
        follow the final component but DOES follow symlinked parents, which is
        what let a real empty directory and a ``symlink -> empty decoy``
        fingerprint alike.
        """

        try:
            entry_stat = stat_no_follow(path, containment_root=self.root)
        except FileNotFoundError:
            return None
        except (SafeFilesystemError, OSError):
            return _FINGERPRINT_CONTAINMENT_FAULT
        return (entry_stat.st_mtime_ns, entry_stat.st_size, entry_stat.st_ino)

    def _cycle_directories_probe_faulted(
        self,
        *,
        source_segments: tuple[str, ...],
        cycle_segment: str,
    ) -> bool:
        """Containment probe of the directories that feed one cycle (#1567 D1b).

        The write-window owner's fast path computes no source-file fingerprint;
        it runs these directory-only containment stats instead, so one of the
        listed DIRECTORIES swapped for a symlink during the window turns the
        owner's hit into a recompute.  The recompute then fails loud with
        whichever token the cold reader produces for that directory — design
        D1's table: ``file_journal_unreadable`` for the journal, pipeline-events
        and (model-scoped) latest directories, ``file_journal_unsafe_scanned_entry``
        for the by-cycle partition, the flat ``pipeline-jobs`` root and the
        cross-model (``model_id=None``) latest read.

        Stated limit: nothing BELOW these directories is probed, so a leaf file
        swapped for a symlink during the window is not detected here.  That is
        deliberate — a leaf probe is the source-file fingerprint this fast path
        exists to skip — and it is why this is deliberately NOT routed through
        ``_cycle_rows_source_fingerprint``.
        """

        directories = [self.root / "pipeline-jobs"]
        for source_segment in source_segments:
            directories.append(self.root / "journal" / source_segment)
            directories.append(self.root / "pipeline-events" / source_segment)
            directories.append(self.root / "latest" / source_segment / cycle_segment)
            directories.append(
                self.root / "pipeline-jobs" / "by-cycle" / source_segment / cycle_segment
            )
        return any(
            self._containment_stat_signature(directory) is _FINGERPRINT_CONTAINMENT_FAULT
            for directory in directories
        )

    def _sequence_regular_file_exists(self, path: Path) -> bool:
        mode = self._probe_stat_mode(path)
        return mode is not None and stat.S_ISREG(mode)

    def _sequence_directory_exists(self, path: Path) -> bool:
        mode = self._probe_stat_mode(path)
        return mode is not None and stat.S_ISDIR(mode)

    def _next_event_id_unlocked(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str | None,
    ) -> int:
        sequence_floor = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time) - 1
        rows = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=None)
        event_ids = [_optional_positive_int(event.get("event_id")) or 0 for event in rows.pipeline_events]
        return max([sequence_floor, *event_ids], default=0) + 1

    def _next_accepted_submit_event_id_unlocked(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> int:
        """Compute a cohort event id from exact cycle surfaces, excluding global direct files."""

        sequence_floor = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time) - 1
        rows = _CycleRows()
        cycle_segment = format_cycle_time(cycle_time)
        for source_segment in _cycle_read_source_segments(
            source_id=source_id,
            source_segment_override=None,
            root=self.root,
        ):
            for surface in ("journal", "pipeline-events"):
                for record in self._read_cycle_segments(
                    self.root / surface / source_segment, cycle_segment
                ):
                    self._apply_journal_record(
                        rows,
                        record,
                        source_id=source_id,
                        cycle_time=cycle_time,
                    )
        event_ids = [_optional_positive_int(event.get("event_id")) or 0 for event in rows.pipeline_events]
        return max([sequence_floor, *event_ids], default=0) + 1

    def _append_journal_record_unlocked(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        record: Mapping[str, Any],
    ) -> None:
        self._append_journal_bytes_unlocked(
            source_id=source_id,
            cycle_time=cycle_time,
            pending=_json_bytes(record),
            read_failure="failed to read existing file journal before append",
        )
        self._apply_record_to_cycle_rows_cache(source_id=source_id, cycle_time=cycle_time, record=record)

    def _append_journal_records_unlocked(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        records: Sequence[Mapping[str, Any]],
    ) -> None:
        """Append a validated record batch with one bounded journal rewrite."""
        if not records:
            return
        self._append_journal_bytes_unlocked(
            source_id=source_id,
            cycle_time=cycle_time,
            pending=b"\n".join(_json_bytes(record).rstrip(b"\n") for record in records) + b"\n",
            read_failure="failed to read existing file journal before batch append",
        )
        for record in records:
            self._apply_record_to_cycle_rows_cache(source_id=source_id, cycle_time=cycle_time, record=record)

    def _append_journal_bytes_unlocked(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        pending: bytes,
        read_failure: str,
    ) -> None:
        """Append pending bytes to the cycle log, rolling over when they do not fit.

        Rotation only decides where NEW lines land — frozen segments are never
        rewritten.  A payload that cannot fit an empty segment by itself still
        fails closed with ``file_journal_byte_limit_exceeded`` and writes
        nothing, so no empty segment file is ever left behind.
        """
        directory = self._journal_directory(source_id=source_id)
        cycle_segment = format_cycle_time(cycle_time)
        # Defense in depth here, unlike _next_sequence_unlocked's public
        # insert_pipeline_event lane: no public lane reaches this frame today
        # (reaching the append means an earlier probe already succeeded).
        try:
            segments = self._cycle_segment_paths(directory, cycle_segment)
        except _JournalProbeContainmentError as error:
            raise _probe_containment_failure(error) from error
        path = segments[-1] if segments else directory / _journal_segment_name(cycle_segment, 0)
        try:
            existing = read_bytes_limited_no_follow(path, max_bytes=self.max_bytes, containment_root=self.root)
        except FileNotFoundError:
            existing = b""
        except (OSError, SafeFilesystemError) as error:
            raise OrchestratorError(
                "FILE_JOURNAL_WRITE_FAILED",
                read_failure,
                {"error_type": type(error).__name__},
            ) from error
        self._require_within_byte_limit(existing, path)
        content = existing
        if content and not content.endswith(b"\n"):
            content += b"\n"
        content += pending
        if existing and len(content) > self.max_bytes and len(pending) <= self.max_bytes:
            if len(segments) >= MAX_FILE_JOURNAL_CYCLE_SEGMENTS:
                raise FileOrchestrationJournalError(
                    "file_journal_segment_limit_exceeded",
                    field=str(
                        _relative_evidence(directory / _journal_segment_name(cycle_segment, 0), self.root)
                    ),
                    evidence={"segments": len(segments)},
                )
            path = directory / _journal_segment_name(cycle_segment, len(segments))
            content = pending
        self._require_within_byte_limit(content, path)
        self._atomic_write_bytes_unlocked(path, content)

    def _apply_record_to_cycle_rows_cache(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        record: Mapping[str, Any],
    ) -> None:
        """Invalidate in-window rows-cache entries for the appended record.

        Every reachable cache key for this source/cycle is removed, so the
        next read recomputes from the newly committed journal bytes.  The
        legacy `(source, cycle, None, None)` base-key update/store arm below
        is unreachable under all current key producers and is not a
        correctness premise.
        """
        source_id = _normalize_file_source_id(source_id, field="source_id")
        cycle_segment = format_cycle_time(cycle_time)
        base_key = (source_id, cycle_segment, None, None)
        # The whole update runs under `_cache_lock`: the stale-key sweep is the
        # only full-table iteration over `_cycle_rows_cache`, and the reducer
        # below is pure in-memory work, so read/modify/write stays atomic
        # against readers populating or evicting the same dict.
        with self._cache_lock:
            stale_keys = [
                key
                for key in self._cycle_rows_cache
                if key[0] == source_id and key[1] == cycle_segment and key != base_key
            ]
            for key in stale_keys:
                self._cycle_rows_cache.pop(key, None)
            cached = self._cycle_rows_cache.get(base_key)
            if cached is None:
                return
            updated = _clone_cycle_rows(cached[1])
            try:
                self._apply_journal_record(updated, record, source_id=source_id, cycle_time=cycle_time)
                updated.pipeline_events = _dedupe_events(updated.pipeline_events)
            except FileOrchestrationJournalError:
                self._cycle_rows_cache.pop(base_key, None)
                return
            # In-window entries carry no fingerprint: hits are trusted while the
            # cycle flock is held, and the window clears the cache on exit.
            self._cycle_rows_cache[base_key] = (None, updated)

    def _materialize_latest_unlocked(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        next_sequence: int | None = None,
        include_direct_jobs: bool = True,
    ) -> None:
        rows = (
            self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
            if include_direct_jobs
            else self._cycle_rows_by_model_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                model_ids=(model_id,),
                include_direct_jobs=False,
            )[model_id]
        )
        if next_sequence is None:
            next_sequence = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)
        latest = {
            "schema_version": FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION,
            "generated_at": _format_utc(_utcnow()),
            "source_id": source_id,
            "cycle_time": _format_utc(cycle_time),
            "model_id": model_id,
            "hydro_run": _strip_internal_fields(rows.hydro_run),
            "forecast_cycle": _strip_internal_fields(rows.forecast_cycle),
            "forcing_version": _strip_internal_fields(rows.forcing_version),
            "model_context": _strip_internal_fields(rows.model_context),
            "pipeline_jobs": [
                _strip_internal_fields(job)
                for job in rows.pipeline_jobs.values()
                if accepted_submit_row_kind(job) != "master"
            ],
            "pipeline_events": [_strip_internal_fields(event) for event in rows.pipeline_events],
            "replay": {
                "latest_sequence": next_sequence - 1,
                "job_count": sum(
                    accepted_submit_row_kind(job) != "master" for job in rows.pipeline_jobs.values()
                ),
                "event_count": len(rows.pipeline_events),
            },
        }
        self._atomic_write_json_unlocked(
            self.root
            / "latest"
            / _safe_segment(source_id)
            / format_cycle_time(cycle_time)
            / f"{_safe_segment(model_id)}.json",
            latest,
        )

    def _materialize_cycle_latest_unlocked(self, *, source_id: str, cycle_time: datetime) -> None:
        # The journal cannot change mid-sweep (cycle write lock is held), so
        # the next sequence is computed once instead of per model.
        next_sequence = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)
        for model_id in self._cycle_materialization_model_ids_unlocked(source_id=source_id, cycle_time=cycle_time):
            self._materialize_latest_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
                next_sequence=next_sequence,
            )

    def _cycle_materialization_model_ids_unlocked(self, *, source_id: str, cycle_time: datetime) -> list[str]:
        model_ids: set[str] = set()
        source_segment = _safe_segment(_normalize_file_source_id(source_id, field="source_id"))
        cycle_segment = format_cycle_time(cycle_time)
        for path in self._latest_paths(source_segment, cycle_segment, model_id=None):
            model_ids.add(_safe_segment(path.stem))
        try:
            rows = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=None)
        except FileOrchestrationJournalError:
            return sorted(model_ids)
        for job in rows.pipeline_jobs.values():
            model_id = _optional_safe_identity(job, "model_id")
            if model_id is not None:
                model_ids.add(model_id)
        for row in (rows.hydro_run, rows.forcing_version, rows.model_context):
            if isinstance(row, Mapping):
                model_id = _optional_safe_identity(row, "model_id")
                if model_id is not None:
                    model_ids.add(model_id)
        return sorted(model_ids)

    def _atomic_write_json_unlocked(self, path: Path, payload: Mapping[str, Any]) -> None:
        self._atomic_write_bytes_unlocked(path, _json_bytes(payload))

    def _atomic_write_bytes_unlocked(self, path: Path, content: bytes) -> None:
        try:
            atomic_write_bytes_no_follow(
                path,
                content,
                containment_root=self.root,
                require_durable_replace=True,
            )
        except (OSError, SafeFilesystemError) as error:
            raise OrchestratorError(
                "FILE_JOURNAL_WRITE_FAILED",
                "failed to atomically write file journal state",
                {"error_type": type(error).__name__},
            ) from error
        self._read_bytes_cache_drop(str(path))

    def _discover_retention_cycles_unlocked(
        self,
        *,
        max_files: int,
        max_depth: int,
    ) -> FileJournalRetentionDiscovery:
        cycles: set[tuple[str, datetime]] = set()
        budget = _RetentionDiscoveryBudget(limit=max_files)
        for surface in ("latest", "journal", "pipeline-events"):
            directory = self.root / surface
            try:
                directory_stat = stat_no_follow(directory, containment_root=self.root)
            except FileNotFoundError:
                continue
            except (OSError, SafeFilesystemError) as error:
                raise FileOrchestrationJournalError(
                    "file_journal_unreadable",
                    field=surface,
                    evidence={"error_type": type(error).__name__},
                ) from error
            if not stat.S_ISDIR(directory_stat.st_mode):
                raise FileOrchestrationJournalError(
                    "file_journal_unsafe_scanned_entry",
                    field=surface,
                    evidence={"entry_type": "not_directory"},
                )
            for path in _iter_retention_surface_files(
                directory,
                root=self.root,
                surface=surface,
                max_files=max_files,
                max_depth=max_depth,
                budget=budget,
            ):
                if surface == "latest":
                    source_id, cycle_time, _model_id = _latest_identity_from_path(path, root=self.root)
                else:
                    source_id, cycle_time, _segment_index = _journal_segment_identity_from_path(
                        path,
                        root=self.root,
                        surface=surface,
                    )
                cycles.add((source_id, cycle_time))
        return FileJournalRetentionDiscovery(
            status="ok",
            cycles=tuple(sorted(cycles, key=lambda item: (item[0], format_cycle_time(item[1])))),
        )

    def _inspect_retention_cycle_unlocked(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> FileJournalRetentionCycleInspection:
        """Classify one hot slice through the canonical cycle replay.

        Retention deliberately owns only the member inventory below.  Replay,
        direct-authority merge, source case handling, and live predicates stay
        in the normal cycle reader, including its filename-filtered flat direct
        leg and the by-cycle direct partition.  A flat direct can never become
        an archive member merely because it is canonical replay evidence.
        """

        members: tuple[FileJournalRetentionMember, ...] = ()
        try:
            source_id = _normalize_file_source_id(source_id, field="source_id")
            cycle_time = _ensure_utc(cycle_time)
            members = self._retention_cycle_members_unlocked(source_id=source_id, cycle_time=cycle_time)
            # The canonical reader includes both direct legs.  Flat direct
            # filenames intentionally fall open toward reading evidence, so an
            # unrelated malformed flat record is an integrity blocker for a
            # destructive retention pass rather than a reason to delete a
            # potentially recoverable hot slice.
            rows = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=None)
            # Direct rows are projection evidence whose merge precedence is
            # intentionally lower than journal/latest state for normal query
            # semantics.  Retention liveness is stricter: any canonical direct
            # row for this slice remains recovery authority even when a stale
            # hot projection carries the same job id.  Do not alter the public
            # replay merge; only widen this destructive-operation predicate.
            direct_rows = self._direct_pipeline_job_records_for_cycle_cached(
                source_id=source_id,
                cycle_time=cycle_time,
            )
            live_rows = [*rows.pipeline_jobs.values(), *direct_rows]
            try:
                blocking = any(_job_blocks_rollback_quiescence(job) for job in live_rows)
                released = any(_released_identity_blocked_row(job) for job in live_rows)
            except AcceptedSubmitEvidenceError:
                # A malformed current accepted master is recovery authority until
                # an owner transition proves otherwise; never archive it.
                blocking = True
                released = False
            return FileJournalRetentionCycleInspection(
                status="live" if blocking or released else "eligible",
                source_id=source_id,
                cycle_time=cycle_time,
                members=members,
                reason="live_row" if blocking or released else None,
            )
        except FileOrchestrationJournalError as error:
            return FileJournalRetentionCycleInspection(
                status="blocked",
                source_id=source_id,
                cycle_time=cycle_time,
                reason=error.reason,
            )
        except AcceptedSubmitEvidenceError:
            return FileJournalRetentionCycleInspection(
                status="live",
                source_id=source_id,
                cycle_time=cycle_time,
                members=members,
                reason="live_row",
            )
        except (OSError, SafeFilesystemError, OrchestratorError):
            return FileJournalRetentionCycleInspection(
                status="blocked",
                source_id=source_id,
                cycle_time=cycle_time,
                reason="file_journal_unreadable",
            )

    def _retention_cycle_members_unlocked(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> tuple[FileJournalRetentionMember, ...]:
        cycle_segment = format_cycle_time(cycle_time)
        source_segments = _cycle_read_source_segments(
            source_id=source_id,
            source_segment_override=None,
            root=self.root,
        )
        members: list[FileJournalRetentionMember] = []
        for source_segment in source_segments:
            for path in self._latest_paths(source_segment, cycle_segment, model_id=None):
                path_source, path_cycle, _model_id = _latest_identity_from_path(path, root=self.root)
                if path_source != source_id or path_cycle != cycle_time:
                    raise FileOrchestrationJournalError(
                        "file_journal_path_identity_mismatch",
                        field=str(_relative_evidence(path, self.root)),
                    )
                members.append(self._retention_member_for_path(path))
            for surface in ("journal", "pipeline-events"):
                directory = self.root / surface / source_segment
                for path in self._cycle_segment_paths(directory, cycle_segment):
                    path_source, path_cycle, _segment_index = _journal_segment_identity_from_path(
                        path,
                        root=self.root,
                        surface=surface,
                    )
                    if path_source != source_id or path_cycle != cycle_time:
                        raise FileOrchestrationJournalError(
                            "file_journal_path_identity_mismatch",
                            field=str(_relative_evidence(path, self.root)),
                        )
                    # Read each selected event-log member now, under the same
                    # owner parser used by replay, so malformed or disappearing
                    # authority cannot be archived as a merely opaque byte blob.
                    self._read_jsonl(path, segment_index=_segment_index)
                    members.append(self._retention_member_for_path(path))
        return tuple(sorted(members, key=lambda item: item.relative_path))

    def _retention_member_for_path(self, path: Path) -> FileJournalRetentionMember:
        try:
            entry = stat_no_follow(path, containment_root=self.root)
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_unreadable",
                field=str(_relative_evidence(path, self.root)),
                evidence={"error_type": type(error).__name__},
            ) from error
        if not stat.S_ISREG(entry.st_mode):
            raise FileOrchestrationJournalError(
                "file_journal_unsafe_scanned_entry",
                field=str(_relative_evidence(path, self.root)),
                evidence={"entry_type": "not_regular_file"},
            )
        return FileJournalRetentionMember(
            relative_path=_relative_evidence(path, self.root).as_posix(),
            size_bytes=int(entry.st_size),
        )

    def _remove_retention_members_unlocked(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        members: Sequence[FileJournalRetentionMember],
    ) -> FileJournalRetentionRemoval:
        """Durably unlink only the currently remaining manifest-bound members.

        A retry legitimately sees a strict subset after a prior interrupted
        cleanup.  It may never see a new member, and every remaining pathname
        must still carry the exact archive digest before the first unlink.  The
        preflight is deliberately complete before mutation so a same-size swap
        cannot turn a per-member failure into a partial cleanup.
        """

        try:
            expected_paths = {member.relative_path: member for member in members}
            if len(expected_paths) != len(members) or any(member.sha256 is None for member in members):
                return FileJournalRetentionRemoval(status="blocked", reason="manifest_member_invalid")
            if not expected_paths:
                return FileJournalRetentionRemoval(status="removed")
            current = self._retention_cycle_members_unlocked(source_id=source_id, cycle_time=cycle_time)
            current_paths = {member.relative_path: member for member in current}
            if any(path not in expected_paths for path in current_paths):
                return FileJournalRetentionRemoval(status="blocked", reason="member_set_changed")
            for relative_path, current_member in current_paths.items():
                expected = expected_paths[relative_path]
                if current_member.size_bytes != expected.size_bytes:
                    return FileJournalRetentionRemoval(status="blocked", reason="member_identity_changed")
                try:
                    content = read_bytes_durable_no_follow(
                        self.root / relative_path,
                        max_bytes=max(expected.size_bytes, 1),
                        containment_root=self.root,
                    )
                except (OSError, SafeFilesystemError):
                    return FileJournalRetentionRemoval(status="blocked", reason="member_identity_changed")
                if len(content) != expected.size_bytes or hashlib.sha256(content).hexdigest() != expected.sha256:
                    return FileJournalRetentionRemoval(status="blocked", reason="member_identity_changed")
            removed: list[str] = []
            for relative_path in sorted(current_paths):
                path = self.root / relative_path
                expected = expected_paths[relative_path]
                try:
                    # Repeat the durable byte proof immediately before every
                    # unlink.  The cycle flock excludes writers; this closes the
                    # remaining out-of-band same-size replacement window.
                    content = read_bytes_durable_no_follow(
                        path,
                        max_bytes=max(expected.size_bytes, 1),
                        containment_root=self.root,
                    )
                    if len(content) != expected.size_bytes or hashlib.sha256(content).hexdigest() != expected.sha256:
                        return FileJournalRetentionRemoval(
                            status="partial" if removed else "blocked",
                            removed_paths=tuple(removed),
                            reason="member_identity_changed",
                        )
                    unlink_no_follow_durable(path, containment_root=self.root, missing_ok=False)
                except (OSError, SafeFilesystemError):
                    return FileJournalRetentionRemoval(
                        status="partial" if removed else "blocked",
                        removed_paths=tuple(removed),
                        reason="member_unlink_failed",
                    )
                removed.append(relative_path)
            return FileJournalRetentionRemoval(status="removed", removed_paths=tuple(removed))
        except FileOrchestrationJournalError as error:
            return FileJournalRetentionRemoval(status="blocked", reason=error.reason)

    @contextmanager
    def _locked_cycle_write(self, *, source_id: str, cycle_time: datetime) -> Iterable[None]:
        with self._write_lock:
            with self._cache_lock:
                self._cycle_rows_cache.clear()
            self._ensure_root_unlocked()
            try:
                # First statement inside the try: a failure raised while the
                # window is opening must still clear the marker, so it is the
                # existing finally's job, never a new try/finally.  The
                # normalized source id keeps the marker's identity space in
                # step with the flock and with `_cycle_rows`'s comparison.
                self._cycle_write_owner = (
                    threading.get_ident(),
                    _normalize_file_source_id(source_id, field="source_id"),
                    format_cycle_time(cycle_time),
                )
                with self._cycle_file_lock_unlocked(source_id=source_id, cycle_time=cycle_time):
                    yield
            finally:
                # #1658 D2: the EXIT clear is a performance measure, so it is
                # scoped to the window's own (normalized source_id,
                # cycle_segment) prefix — every derived 4-tuple key for that
                # pair AND the legacy base key `(source, cycle, None, None)`.
                # The stale-key sweep in `_apply_record_to_cycle_rows_cache`
                # deliberately EXCLUDES the base key because it updates it in
                # place; this sweep must include it, because the window is over
                # and nothing will refresh it.  Entries other cycles populated
                # during the window survive.  The ENTRY clear above stays
                # global and untouched: it is the owner fast path's correctness
                # precondition, not a tunable.  A marker that was never set
                # (a failure while the window was opening) leaves no prefix to
                # scope by, so that degenerate path keeps the global wipe.
                owner = self._cycle_write_owner
                with self._cache_lock:
                    if owner is None:
                        self._cycle_rows_cache.clear()
                    else:
                        owner_source_id, owner_cycle_segment = owner[1], owner[2]
                        for key in [
                            key
                            for key in self._cycle_rows_cache
                            if key[0] == owner_source_id and key[1] == owner_cycle_segment
                        ]:
                            self._cycle_rows_cache.pop(key, None)
                self._cycle_write_owner = None

    @contextmanager
    def _cycle_file_lock_unlocked(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        non_blocking: bool = False,
    ) -> Iterable[bool]:
        import fcntl

        lock_path = (
            self.root
            / ".locks"
            / _safe_segment(_normalize_file_source_id(source_id, field="source_id"))
            / f"{format_cycle_time(cycle_time)}.lock"
        )
        parent_fd: int | None = None
        lock_fd: int | None = None
        lock_held = False
        try:
            lock_dir = ensure_directory_no_follow(lock_path.parent, containment_root=self.root)
            parent_fd = os.open(
                lock_dir,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            lock_fd = os.open(
                lock_path.name,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                0o666,
                dir_fd=parent_fd,
            )
            lock_stat = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_stat.st_mode):
                raise SafeFilesystemError(f"Cycle lock target must be a regular file: {lock_path}")
            if _file_lock_guard_mode() == "flock":
                flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if non_blocking else 0)
                try:
                    fcntl.flock(lock_fd, flags)
                except BlockingIOError:
                    if non_blocking:
                        yield False
                        return
                    raise
                lock_held = True
            yield True
        except (OSError, SafeFilesystemError) as error:
            raise OrchestratorError(
                "FILE_JOURNAL_WRITE_FAILED",
                "failed to acquire file orchestration journal cycle lock",
                {"error_type": type(error).__name__, "surface": "cycle_lock"},
            ) from error
        finally:
            if lock_fd is not None:
                if lock_held:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(lock_fd)
            if parent_fd is not None:
                os.close(parent_fd)

    @contextmanager
    def _reconcile_inventory_file_lock_unlocked(self) -> Iterable[None]:
        """Serialize inventory mutation and orphan-temp cleanup across processes."""

        if self._reconcile_inventory_lock_depth:
            self._reconcile_inventory_lock_depth += 1
            try:
                yield
            finally:
                self._reconcile_inventory_lock_depth -= 1
            return
        migration_cycle = datetime(1970, 1, 1, tzinfo=UTC)
        with self._cycle_file_lock_unlocked(source_id="gfs", cycle_time=migration_cycle):
            self._reconcile_inventory_lock_depth = 1
            try:
                yield
            finally:
                self._reconcile_inventory_lock_depth = 0

    def _journal_directory(self, *, source_id: str, surface: str = "journal") -> Path:
        return self.root / surface / _safe_segment(source_id)

    def _cycle_segment_paths(self, directory: Path, cycle_segment: str) -> list[Path]:
        """Ordered existing segment paths of one cycle event log.

        Exact-path probing over the bounded segment window (index 0 through
        ``MAX_FILE_JOURNAL_CYCLE_SEGMENTS``), never a directory scan: this
        runs per candidate read while a source directory holds one entry per
        cycle.  The returned prefix stops at the first gap; within the window
        a segment beyond that gap, or past the cap, is an integrity fault
        rather than a silently ignored file.  Past the window the exact-path
        reader cannot observe the file at all — a stray ``<cycle>.9.jsonl``
        stays invisible here and the recursive walkers and the
        reconcile-inventory backfill remain its only detecting readers.
        """
        paths: list[Path] = []
        gapped = False
        for index, name in enumerate(_journal_segment_names(cycle_segment)):
            path = directory / name
            if not self._journal_segment_exists(path):
                gapped = True
                continue
            if gapped:
                raise FileOrchestrationJournalError(
                    "file_journal_segment_gap",
                    field=str(_relative_evidence(path, self.root)),
                )
            if index >= MAX_FILE_JOURNAL_CYCLE_SEGMENTS:
                raise FileOrchestrationJournalError(
                    "file_journal_segment_limit_exceeded",
                    field=str(_relative_evidence(path, self.root)),
                )
            paths.append(path)
        return paths

    def _journal_segment_exists(self, path: Path) -> bool:
        """Probe a segment slot without hiding a non-regular occupant.

        A safe but non-regular occupant (a directory, a FIFO) counts as
        present so the hardened reader stays the sole authority for that
        failure, exactly as it is for an unsegmented cycle log today.  A
        symlink in the slot is a containment fault of the probe itself now,
        which surfaces the same reason token the reader would have raised.
        """
        return self._probe_stat_mode(path) is not None

    def _cycle_segment_signatures(self, directory: Path, cycle_segment: str) -> tuple[Any, ...]:
        """Stat identity of every segment slot a cycle log may occupy.

        Absent slots are included as ``None``: rollover freezes the previous
        segment forever, so a fingerprint that only watched the base file
        would serve stale rows for the rest of the cycle's life.

        #1567 D1: every slot resolves through ``_containment_stat_signature``,
        so a symlinked ``<surface>/<source>`` parent yields the fingerprint's
        containment-fault marker instead of an "absent" that is
        indistinguishable from a real empty directory.
        """
        return tuple(
            (name, self._containment_stat_signature(directory / name))
            for name in _journal_segment_names(cycle_segment)
        )

    def _read_cycle_segments(self, directory: Path, cycle_segment: str) -> list[dict[str, Any]]:
        """Replay every segment of one cycle event log in segment order."""

        records: list[dict[str, Any]] = []
        # Every public lane — read, and write via its precondition read —
        # routes through here, so this frame defines the public contract for
        # a probe containment fault.
        try:
            segment_paths = self._cycle_segment_paths(directory, cycle_segment)
        except _JournalProbeContainmentError as error:
            raise _probe_containment_failure(error) from error
        for segment_index, path in enumerate(segment_paths):
            records.extend(self._read_jsonl(path, segment_index=segment_index))
        return records

    def _cycle_journal_records(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        surface: str = "journal",
    ) -> list[dict[str, Any]]:
        # #1734 D11: baseline lane, eager — one cycle's own event log.
        with journal_read_lane("cycle_journal_replay"):
            return self._read_cycle_segments(
                self._journal_directory(source_id=source_id, surface=surface),
                format_cycle_time(cycle_time),
            )

    def _ensure_root_unlocked(self) -> None:
        try:
            ensure_directory_no_follow(self.root)
        except (OSError, SafeFilesystemError) as error:
            raise OrchestratorError(
                "FILE_JOURNAL_WRITE_FAILED",
                "failed to create file orchestration journal root",
                {"error_type": type(error).__name__, "surface": "journal_root"},
            ) from error


def _file_lock_guard_mode() -> str:
    configured = os.getenv(FILE_JOURNAL_LOCK_GUARD_MODE_ENV)
    if configured is None:
        legacy = os.getenv(LEGACY_FILE_LOCK_GUARD_MODE_ENV)
        if legacy is not None and legacy.strip().lower() in {"atomic", "none", "off", "disabled"}:
            raise SafeFilesystemError(
                f"{LEGACY_FILE_LOCK_GUARD_MODE_ENV}={legacy.strip()} does not prove a "
                "cross-process journal guard; configure "
                f"{FILE_JOURNAL_LOCK_GUARD_MODE_ENV}=flock explicitly"
            )
        configured = legacy if legacy is not None else "flock"
    value = configured.strip().lower()
    if value in {"", "flock", "fcntl"}:
        return "flock"
    raise SafeFilesystemError(f"Unsupported {FILE_JOURNAL_LOCK_GUARD_MODE_ENV}: {value}")


@_install_public_read_attribution
class FileJournalRetryService:
    """Retry adapter that records retry state in the file orchestration journal."""

    def __init__(
        self,
        repository: FileOrchestrationJournalRepository,
        config: RetryConfig | None = None,
    ) -> None:
        self.repository = repository
        self.config = config or RetryConfig()

    def should_auto_retry(self, job: Any) -> bool:
        return bool(self.retry_policy_for_job(job)["auto_retry"])

    def retry_policy_for_job(self, job: Any) -> dict[str, Any]:
        status = _file_retry_job_text(job, "status") or ""
        error_code = _file_retry_job_value(job, "error_code")
        retry_count = _file_retry_job_int(job, "retry_count")
        classification = classify_failure(error_code, attempt=retry_count, retry_limit=self.config.max_retries)
        return {
            **classification,
            "auto_retry": status != "permanently_failed"
            and classification["retryable"]
            and not classification["permanent"],
        }

    def handle_failed_job(self, job: Any) -> SimpleNamespace:
        current = self.repository.get_pipeline_job(str(_file_retry_job_value(job, "job_id") or ""))
        if (
            current is not None
            and accepted_submit_contract_is_current(current)
            and accepted_submit_row_kind(current) == "master"
        ):
            if not self.should_auto_retry(job):
                # Same decline judgement as the orchestrator-cycle arm
                # (``chain_forecast_orchestrator_cycle._schedule_cycle_stage_retry``)
                # must land the same permanent-failure mark (#1312).  The mark
                # is journal I/O on a formerly write-free decline, so a journal
                # failure falls back to the pre-#1312 return value instead of
                # raising through the caller (the mark is idempotent and the
                # next pass re-lands it); the caller arm is guarded the same way,
                # including the operator-visible signal the fallback leaves
                # behind (#1312 C-P2 — the fallback must not be silent).
                try:
                    return self.mark_permanently_failed(job)
                except (OrchestratorError, FileOrchestrationJournalError) as error:
                    self._record_permanent_failure_mark_failure(current, error)
                    return _file_retry_namespace(current)
            retry_job_id, retry_count = _next_current_master_retry_identity(current)
            return _file_retry_namespace(
                {
                    **current,
                    "job_id": retry_job_id,
                    "status": "pending",
                    "retry_count": retry_count,
                    "slurm_job_id": None,
                }
            )
        if self.should_auto_retry(job):
            return self.schedule_auto_retry(job)
        return self.mark_permanently_failed(job)

    def _record_permanent_failure_mark_failure(
        self,
        current: Mapping[str, Any],
        error: Exception,
    ) -> None:
        """Emit operator-visible evidence that a decline could not be marked.

        Same shape as the orchestrator-cycle arm's signal
        (``chain_forecast_orchestrator_cycle._record_permanent_failure_mark_failure``)
        so both decline exits are observable through one event type.  The
        emission is itself best effort: it exists to keep the fallback from
        being silent, never to turn a correct decline into a raise.
        """

        insert_pipeline_event = getattr(self.repository, "insert_pipeline_event", None)
        if not callable(insert_pipeline_event):
            return
        job_id = str(current.get("job_id") or "")
        status = str(current.get("status") or "") or None
        reason = str(
            getattr(error, "error_code", None) or getattr(error, "reason", None) or type(error).__name__
        )
        try:
            insert_pipeline_event(
                entity_type="pipeline_job",
                entity_id=job_id,
                event_type="permanent_failure_mark_failed",
                status_from=status,
                status_to=status,
                message=(
                    "automatic retry declined but the permanent-failure mark could not be "
                    f"written (reason={reason}); the row stays at status={status}."
                ),
                details={
                    "pipeline_job_id": job_id,
                    "reason": reason,
                    "error_type": type(error).__name__,
                    "field": getattr(error, "field", None),
                    "retry_mark_pending": True,
                },
            )
        except (OrchestratorError, FileOrchestrationJournalError):
            # Evidence emission must never abort a correct decline decision.
            pass

    def schedule_auto_retry(self, job: Any) -> SimpleNamespace:
        # The durable row is both the master routing check and the exact
        # lineage source, so it must be read privately; a durable read fault
        # must fail closed before any retry write, since falling back to the
        # caller snapshot would persist a false empty lineage.
        job_id = str(_file_retry_job_value(job, "job_id") or "")
        try:
            current = self.repository._pipeline_job_for_id_unlocked(job_id)
        except FileOrchestrationJournalError as error:
            raise RetryError(
                "AUTO_RETRY_EVIDENCE_UNAVAILABLE",
                "Auto retry durable predecessor could not be read safely.",
                {
                    "job_id": job_id,
                    "journal_reason": str(error.reason),
                    "journal_field": str(error.field),
                },
            ) from error
        if current is None:
            # No durable predecessor means lineage is unknowable; cloning the
            # caller snapshot would persist a false empty map.
            raise RetryError(
                "AUTO_RETRY_EVIDENCE_UNAVAILABLE",
                "Auto retry durable predecessor row is missing.",
                {
                    "job_id": job_id,
                    "journal_reason": "file_journal_predecessor_missing",
                    "journal_field": "job_id",
                },
            )
        if (
            current is not None
            and accepted_submit_contract_is_current(current)
            and accepted_submit_row_kind(current) == "master"
        ):
            # The master's virtual next-attempt row is a caller-facing
            # routing value only; it must render through the public
            # projection so the response never carries unredacted lineage.
            retry_job_id, retry_count = _next_current_master_retry_identity(current)
            return _file_retry_namespace(
                {
                    **_public_scheduler_row(current),
                    "job_id": retry_job_id,
                    "status": "pending",
                    "retry_count": retry_count,
                    "slurm_job_id": None,
                }
            )
        try:
            durable_lineage = _strict_retry_init_state_identities(current.get(INIT_STATE_IDENTITY_FIELD))
        except FileOrchestrationJournalError as error:
            raise RetryError(
                "AUTO_RETRY_EVIDENCE_UNAVAILABLE",
                "Auto retry durable predecessor lineage is invalid.",
                {
                    "job_id": job_id,
                    "journal_reason": str(error.reason),
                    "journal_field": str(error.field),
                },
            ) from error
        durable_previous_job_id = str(current["job_id"])
        source = _file_retry_job_record(job)
        previous_error = source.get("error_code")
        status_from = str(source.get("status") or "")
        next_retry_count = int(source.get("retry_count") or 0) + 1
        retry_job_id = f"{source['job_id']}_retry_{next_retry_count}"
        existing = self.repository.get_pipeline_job(retry_job_id)
        reused_existing_retry_job = False
        if existing is not None:
            if not _file_auto_retry_job_can_be_reused(existing):
                raise RetryError(
                    "AUTO_RETRY_JOB_CONFLICT",
                    "Existing file-journal auto retry job cannot be reset safely.",
                    {
                        "retry_job_id": retry_job_id,
                        "existing_status": existing.get("status"),
                        "existing_slurm_job_id": existing.get("slurm_job_id"),
                        "existing_array_task_id": existing.get("array_task_id"),
                        "previous_job_id": durable_previous_job_id,
                    },
                )
            reused_existing_retry_job = True
        retry_record = {
            **source,
            "job_id": retry_job_id,
            "slurm_job_id": None,
            "array_task_id": None,
            "status": "pending",
            "submitted_at": None,
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "retry_count": next_retry_count,
            "manual_retry_marker": False,
            "idempotency_key": None,
            "candidate_id": None,
            "error_code": None,
            "error_message": None,
            "log_uri": None,
            "updated_at": _format_utc(_utcnow()),
        }
        # An auto retry attempt has a fresh job/idempotency identity with
        # nulled candidate/array discriminators: it cannot satisfy the
        # predecessor's accepted-submit authority contract and must not
        # claim it.  Lineage and the immediate predecessor both come from
        # the durable row; the caller snapshot must control neither.
        retry_record.pop(ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD, None)
        retry_record[INIT_STATE_IDENTITY_FIELD] = durable_lineage
        retry_record["previous_job_id"] = durable_previous_job_id
        written = self.repository.upsert_pipeline_job(retry_record)
        backoff_seconds = compute_backoff_seconds(int(source.get("retry_count") or 0), self.config.backoff_schedule)
        self.repository.insert_pipeline_event(
            entity_type="pipeline_job",
            entity_id=retry_job_id,
            event_type="retry",
            status_from=status_from,
            status_to="pending",
            details={
                "trigger": "auto",
                "retry_count": next_retry_count,
                "previous_error": previous_error,
                "backoff_seconds": backoff_seconds,
                "previous_job_id": durable_previous_job_id,
                "slurm_job_id": written.get("slurm_job_id"),
                "failure": classify_failure(
                    previous_error,
                    attempt=int(source.get("retry_count") or 0),
                    retry_limit=self.config.max_retries,
                ),
                "reused_existing_retry_job": reused_existing_retry_job,
            },
        )
        return _file_retry_namespace(written)
    def mark_permanently_failed(self, job: Any) -> SimpleNamespace:
        source = _file_retry_job_record(job)
        current = self.repository.get_pipeline_job(str(source.get("job_id") or ""))
        if (
            current is not None
            and accepted_submit_contract_is_current(current)
            and accepted_submit_row_kind(current) == "master"
        ):
            # Master rows own a typed authority transition, and their
            # idempotency oracle is the PERSISTED row (#1312): the caller's
            # snapshot may be stale in either direction, so the snapshot gate
            # below is deliberately bypassed here.
            return self._mark_master_permanently_failed(current, source)
        if str(source.get("status") or "") == "permanently_failed":
            return _file_retry_namespace(source)
        status_from = str(source.get("status") or "")
        last_error = source.get("error_code")
        auto_retry_skipped = auto_retry_skipped_details(last_error)
        _previous_status, written = self.repository.update_pipeline_job_status(
            str(source["job_id"]),
            "permanently_failed",
            error_code=str(last_error) if last_error not in (None, "") else None,
            error_message=source.get("error_message"),
            finished_at=_utcnow(),
        )
        self.repository.insert_pipeline_event(
            entity_type="pipeline_job",
            entity_id=str(source["job_id"]),
            event_type="permanently_failed",
            status_from=status_from,
            status_to="permanently_failed",
            details={
                "final_retry_count": int(source.get("retry_count") or 0),
                "last_error": last_error,
                "failure": classify_failure(
                    last_error,
                    attempt=int(source.get("retry_count") or 0),
                    retry_limit=self.config.max_retries,
                ),
                "automatic_retry_stopped": True,
                **(auto_retry_skipped or {}),
            },
        )
        warn_unknown_error_code(auto_retry_skipped)
        return _file_retry_namespace(written)

    def _mark_master_permanently_failed(
        self,
        current: Mapping[str, Any],
        source: Mapping[str, Any],
    ) -> SimpleNamespace:
        """Route one master row through its typed permanent-failure transition.

        ``current`` is the freshly read PUBLIC row and ``source`` is the
        caller's public snapshot, so both messages are display projections:
        neither can carry authoritative text a durable value lacks.  A non-empty
        source message that DIFFERS from the current public message is
        caller-provided new text and SHALL be forwarded (the typed transition
        sanitizes it durably).  A source message identical to the current public
        message is round-tripped display evidence -- the same projection of the
        same durable value -- so passing it back would feed ``[local-path]`` /
        ``[object-uri]`` placeholders into durable state.  ``None`` is passed
        instead and the typed transition preserves the durable value.
        """

        last_error = source.get("error_code")
        if last_error in (None, ""):
            last_error = current.get("error_code")
        retry_count = int(source.get("retry_count") or current.get("retry_count") or 0)
        auto_retry_skipped = auto_retry_skipped_details(last_error)
        source_message = source.get("error_message")
        forwarded_message = (
            source_message
            if source_message not in (None, "") and source_message != current.get("error_message")
            else None
        )
        result = self.repository.mark_pipeline_job_permanently_failed(
            str(current["job_id"]),
            error_code=str(last_error) if last_error not in (None, "") else None,
            error_message=forwarded_message,
            finished_at=_utcnow(),
            event_details={
                "final_retry_count": retry_count,
                "last_error": last_error,
                "failure": classify_failure(
                    last_error,
                    attempt=retry_count,
                    retry_limit=self.config.max_retries,
                ),
                "automatic_retry_stopped": True,
                **(auto_retry_skipped or {}),
            },
        )
        # ``missing``/``stale``/``idempotent`` outcomes append no event, so the
        # unknown-code warning is gated on the write that actually landed.
        if result.wrote:
            warn_unknown_error_code(auto_retry_skipped)
        # ``stale``/``idempotent`` outcomes hand back the raw persisted row, so
        # the namespace is normalized to the same public shape callers already
        # get from ``get_pipeline_job``.
        return _file_retry_namespace(
            _public_scheduler_row(result.row) if result.row is not None else current
        )

    def record_manual_repair(
        self,
        run_id: str,
        *,
        requested_by: str | None = None,
        request_id: str | None = None,
        reason: str | None = None,
        policy_decision: PolicyDecision | None = None,
        trusted_internal: bool = False,
    ) -> SimpleNamespace:
        if trusted_internal:
            policy_decision = trusted_internal_policy_decision(
                "pipeline.retry_run",
                target_type="pipeline_run",
                target_id=run_id,
                actor_id="trusted-internal:file-journal-retry-service",
                roles=("sys_admin",),
            )
        decision = require_policy_evidence(
            policy_decision,
            action_id="pipeline.retry_run",
            target_type="pipeline_run",
            target_id=run_id,
        )
        if decision.decision != "allow":
            raise RetryError(
                decision.reason_code,
                decision.reason,
                {"run_id": run_id, "policy_decision": decision.to_dict(), "no_mutation_expected": True},
            )
        source_id, cycle_time = _source_cycle_from_file_run_id(run_id)
        with self.repository._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            failed_job, active_job = self._manual_retry_source_for_run(run_id)
            if active_job is not None:
                raise RetryConflictError(run_id, _file_retry_namespace(active_job))
            if failed_job is None:
                raise RetryNotFoundError(run_id)

            previous_error = failed_job.get("error_code") or (
                "cancelled" if failed_job.get("status") == "cancelled" else None
            )
            # The journal's clean-reservation invariant zeroes ``retry_count`` on master
            # rows, so the durable per-stage attempt lives in the ``_retry_<n>`` job-id
            # suffix.  A suffix-only target (persisted count 0, suffix N) must emit N+1 --
            # not a stale attempt-one claim that overrides the recovered floor
            # (#1577 round-1 cand-st-02).  ``effective_retry_attempt`` is the single owner
            # of that derivation, used by ``_next_current_master_retry_identity``.
            next_retry_count = effective_retry_attempt(failed_job["job_id"], failed_job.get("retry_count")) + 1
            details: dict[str, Any] = {
                "trigger": "manual",
                "retry_count": next_retry_count,
                "previous_error": previous_error,
                "previous_job_id": failed_job["job_id"],
                # The marker's OWN record of what it repairs, read by
                # ``scheduler_state_manual_retry._unresolvable_marker_entity_pins_attempt``
                # when the target row is gone from the candidate state (identity-filter
                # deletion or row-window truncation) and the id text is all that is left
                # otherwise.  Deliberately NOT named ``stage``:
                # ``chain_repository_state._normalized_record_stage`` reads ``details.stage``
                # off EVENT records too, so that name would make this very event vanish from
                # the candidate state for legacy-downstream-stage targets under the
                # production terminal-stage setting.
                "failed_stage": failed_job.get("stage"),
                "slurm_job_id": None,
                "manual_retry_marker": True,
                "prior_failure_reason": previous_error,
                "failure": classify_failure(
                    previous_error,
                    attempt=next_retry_count,
                    retry_limit=self.config.max_retries,
                    manual=True,
                ),
            }
            # The rest of the target row's write-time shape, on the same terms as
            # ``failed_stage`` above and for the same reader: with the row gone from the
            # candidate state the pin gate rebuilds the target from these keys and runs the
            # resolved-row routing over the reconstruction, instead of guessing from id text.
            # The key set and its closure invariant over the shared live-failure predicate live
            # on ``MARKER_TARGET_ROW_DETAIL_FIELDS`` (the sanitizer whitelist and the gate read
            # the same tuple, so the three surfaces cannot drift).  ``target_`` prefixes keep
            # them clear of the three consumed key names: ``stage``/``job_type`` (the
            # candidate-state record-stage reader) and ``model_id`` (the marker ATTRIBUTION
            # reader -- the target row's model is a different semantic axis).  Absent values are
            # not written, exactly as the sanitizer passes no empties; ``0`` and ``False`` are
            # recorded values and ARE written.
            #
            # ``failed_job`` is a PERSISTED row, so two of the eight keys are structurally
            # unreachable from here: ``repair_status``/``active_blocker`` are annotations the
            # candidate-state projection applies to a row copy and ``_pipeline_job_row`` has no
            # such fields, so ``target_repair_status``/``target_active_blocker`` are never
            # emitted.  The gate honours them if a record ever carries them; recording those
            # projection-only annotations at write time is an accepted permanent limitation
            # (#1482 option (c)), so a target already annotated repaired remains a disclosed
            # conservative pin rather than a second durable repair authority.  #1186 remains
            # the separate open operator-entry/exposure issue.
            for detail_key, row_field in MARKER_TARGET_ROW_DETAIL_FIELDS:
                target_value = failed_job.get(row_field)
                if target_value not in (None, ""):
                    details[detail_key] = target_value
            if requested_by not in (None, ""):
                details["requested_by"] = requested_by
            if request_id not in (None, ""):
                details["request_id"] = request_id
            if reason not in (None, ""):
                details["reason"] = reason
            details["policy_decision"] = decision.to_dict()
            self._append_pipeline_event_unlocked(
                failed_job,
                event_type="retry",
                status_from=str(failed_job.get("status") or ""),
                status_to="manual_repair_requested",
                details=details,
            )
            marker = {
                "job_id": failed_job["job_id"],
                "run_id": run_id,
                "cycle_id": failed_job.get("cycle_id"),
                "job_type": failed_job.get("job_type"),
                "model_id": failed_job.get("model_id"),
                "stage": failed_job.get("stage"),
                "status": "manual_repair_requested",
                "retry_count": next_retry_count,
                "manual_retry_marker": True,
                "previous_job_id": failed_job["job_id"],
                "prior_failure_reason": previous_error,
            }
            return _file_retry_namespace(marker)

    def _create_pending_manual_retry_job(self, run_id: str) -> _PendingManualRetryResult:
        source_id, cycle_time = _source_cycle_from_file_run_id(run_id)
        with self.repository._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            failed_job, active_job = self._manual_retry_source_for_run(run_id)
            if active_job is not None:
                raise RetryConflictError(run_id, _file_retry_namespace(active_job))
            if failed_job is None:
                raise RetryNotFoundError(run_id)
            # The public projection decides retryability and conflict only;
            # durable bytes must come from the private row under the same
            # lock, or redacted placeholders / false empty lineage would be
            # persisted.  A raising private read is invalid durable evidence,
            # translated the same way as the row constructor below.
            try:
                failed_job = self.repository._pipeline_job_for_id_unlocked(str(failed_job["job_id"]))
            except FileOrchestrationJournalError as error:
                raise RetryEvidenceInvalidError(
                    run_id, reason=str(error.reason), field=str(error.field)
                ) from error
            if failed_job is None:
                # Only an external writer can delete the private row between
                # selection and this lock-held rebind; never mint a retry row
                # off the projection.
                raise RetryNotFoundError(run_id)
            try:
                _strict_retry_init_state_identities(failed_job.get(INIT_STATE_IDENTITY_FIELD))
            except FileOrchestrationJournalError as error:
                raise RetryEvidenceInvalidError(
                    run_id, reason=str(error.reason), field=str(error.field)
                ) from error

            previous_error = failed_job.get("error_code") or (
                "cancelled" if failed_job.get("status") == "cancelled" else None
            )
            # Same attempt truth as ``record_manual_repair``: the durable attempt lives in
            # the job-id suffix when the clean-reservation invariant zeroed the persisted
            # count, so a suffix-only target must emit N+1 for the retry row, its marker
            # event, the idempotency key, and the failure payload
            # (#1577 round-1 cand-st-02).
            next_retry_count = effective_retry_attempt(failed_job["job_id"], failed_job.get("retry_count")) + 1
            retry_job_id = _next_file_manual_retry_job_id_for_run(self.repository, run_id)
            retry_record = {
                **failed_job,
                "job_id": retry_job_id,
                "status": "pending",
                "slurm_job_id": None,
                "array_task_id": None,
                "submitted_at": None,
                "started_at": None,
                "finished_at": None,
                "exit_code": None,
                "retry_count": next_retry_count,
                "manual_retry_marker": True,
                "previous_job_id": failed_job["job_id"],
                "idempotency_key": f"manual_retry:{run_id}:{next_retry_count}",
                "candidate_id": None,
                "error_code": None,
                "error_message": None,
                "log_uri": None,
                "updated_at": _format_utc(_utcnow()),
            }
            # A retry attempt has a fresh job/idempotency identity and points
            # back through ``previous_job_id``; it is not the accepted-submit
            # authority row, so the marker must not carry over.  The lineage
            # map below is attempt-level provenance and stays.
            retry_record.pop(ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD, None)
            # #1804: the operator-recovery attestation is bound to the exact
            # released source row, never to a distinct manual-retry attempt.
            # This clone boundary is the one place a row becomes a NEW retry
            # row; the successor is explicitly cleared here so the credential
            # cannot cross even when a future caller selects an attested
            # released predecessor.  The source row keeps its attestation --
            # only the successor field is cleared.
            retry_record[OPERATOR_RECOVERY_ATTESTATION_FIELD] = None
            try:
                retry_row = self.repository._pipeline_job_row(retry_record)
            except FileOrchestrationJournalError as error:
                # Only stable reason/field tokens travel: the raw journal
                # evidence may embed private paths or URIs.
                raise RetryEvidenceInvalidError(
                    run_id, reason=str(error.reason), field=str(error.field)
                ) from error
            if self.repository._pipeline_job_conflicts_unlocked(retry_row):
                conflict = active_job or self.repository._pipeline_job_for_id_unlocked(retry_job_id) or retry_record
                raise RetryConflictError(run_id, _file_retry_namespace(conflict))
            written = self.repository._write_pipeline_job_unlocked(
                retry_row,
                exclusive_direct=True,
                model_id=_optional_safe_identity(retry_row, "model_id"),
            )
            if written is None:
                conflict = active_job or self.repository._pipeline_job_for_id_unlocked(retry_job_id) or retry_record
                raise RetryConflictError(run_id, _file_retry_namespace(conflict))
            self._append_pipeline_event_unlocked(
                written,
                event_type="retry",
                status_from=str(failed_job.get("status") or ""),
                status_to="pending",
                details={
                    "trigger": "manual",
                    "retry_count": next_retry_count,
                    "previous_error": previous_error,
                    "previous_job_id": failed_job["job_id"],
                    "slurm_job_id": written.get("slurm_job_id"),
                    "manual_retry_marker": True,
                    "prior_failure_reason": previous_error,
                    "failure": classify_failure(
                        previous_error,
                        attempt=next_retry_count,
                        retry_limit=self.config.max_retries,
                        manual=True,
                    ),
                },
            )
            # The private row actually written under this lock, captured BEFORE
            # the public projection: exact lineage for the trusted pre-submit
            # snapshot, marker absent, previous_job_id already bound.
            return _PendingManualRetryResult(
                public_job=_file_retry_namespace(written),
                private_snapshot=dict(retry_row),
            )

    def _append_pipeline_event_unlocked(
        self,
        job: Mapping[str, Any],
        *,
        event_type: str,
        status_from: str | None,
        status_to: str | None,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        source_id = _source_id_from_job(job)
        cycle_time = _cycle_time_from_job(job)
        model_id = _optional_safe_identity(job, "model_id")
        row = {
            "event_id": self.repository._next_event_id_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
            ),
            "entity_type": "pipeline_job",
            "entity_id": str(job["job_id"]),
            "event_type": event_type,
            "status_from": status_from,
            "status_to": status_to,
            "message": None,
            "details": details,
            "created_at": _format_utc(_utcnow()),
        }
        self.repository._append_validated_record_unlocked(
            "pipeline_event",
            row,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            materialize_model_id=model_id,
        )
        return _public_scheduler_row(row)

    def attempt_manual_retry(
        self,
        run_id: str,
        gateway: Any | None = None,
        *,
        policy_decision: PolicyDecision | None = None,
        trusted_internal: bool = False,
    ) -> SimpleNamespace:
        if trusted_internal:
            policy_decision = trusted_internal_policy_decision(
                "pipeline.retry_run",
                target_type="pipeline_run",
                target_id=run_id,
                actor_id="trusted-internal:file-journal-retry-service",
                roles=("sys_admin",),
            )
        decision = require_policy_evidence(
            policy_decision,
            action_id="pipeline.retry_run",
            target_type="pipeline_run",
            target_id=run_id,
        )
        if decision.decision != "allow":
            raise RetryError(
                decision.reason_code,
                decision.reason,
                {"run_id": run_id, "policy_decision": decision.to_dict(), "no_mutation_expected": True},
            )
        if gateway is None:
            raise RetryError(
                "RETRY_EXECUTION_UNAVAILABLE",
                "No Slurm gateway available for retry submission.",
                {"run_id": run_id},
            )

        pending = self._create_pending_manual_retry_job(run_id)
        retry_job = pending.public_job
        # Trusted PRE-SUBMIT private snapshot: carried out of the lock-held
        # producer (no new lock-outside durable read), strictly validated
        # again defensively before any gateway side effect.  This defensive
        # failure converges onto the same 409 boundary as the producer's own
        # evidence rejection (D4) instead of a generic 500.
        trusted_pending_snapshot = pending.private_snapshot
        try:
            _strict_retry_init_state_identities(trusted_pending_snapshot.get(INIT_STATE_IDENTITY_FIELD))
        except FileOrchestrationJournalError as error:
            raise RetryEvidenceInvalidError(
                run_id, reason=str(error.reason), field=str(error.field)
            ) from error
        runtime_root_resolution: dict[str, Any] | None = None
        runtime_root_contract: dict[str, str] | None = None
        try:
            request, runtime_root_resolution, runtime_root_contract = self._manual_retry_submission_request(retry_job)
            submitted = _submit_file_manual_retry_job(gateway, request)
        except Exception as error:
            if runtime_root_resolution is not None:
                _attach_retry_runtime_root_resolution(error, runtime_root_resolution)
            if runtime_root_contract is not None:
                _attach_retry_runtime_root_contract(error, runtime_root_contract)
            updated = self._record_manual_retry_submission_failure(retry_job.job_id, error)
            return _file_retry_namespace(updated)
        updated = self._record_manual_retry_submission_success(
            retry_job.job_id,
            submitted,
            trusted_pending_snapshot,
            runtime_root_resolution=runtime_root_resolution,
            runtime_root_contract=runtime_root_contract,
        )
        return _file_retry_namespace(updated)

    def _manual_retry_submission_request(
        self,
        retry_job: SimpleNamespace,
    ) -> tuple[SubmitJobRequest, dict[str, Any] | None, dict[str, str] | None]:
        model_id = retry_job.model_id or _model_id_from_file_run_id(retry_job.run_id) or "unknown"
        submission_job = _RetrySubmissionJob(
            job_id=retry_job.job_id,
            run_id=retry_job.run_id,
            cycle_id=retry_job.cycle_id,
            job_type=retry_job.job_type,
            model_id=model_id,
            stage=retry_job.stage,
            retry_count=int(retry_job.retry_count or 0),
            previous_job_id=getattr(retry_job, "previous_job_id", None),
        )
        runtime_root = self._resolve_file_retry_runtime_roots(submission_job)
        runtime_root_contract = runtime_root.manifest_fields if runtime_root is not None else None
        runtime_root_resolution = runtime_root.evidence if runtime_root is not None else None
        manifest = _retry_submission_manifest(
            submission_job,
            model_id=model_id,
            runtime_root_fields=runtime_root_contract,
        )
        array_tasks = _file_manual_retry_array_tasks(submission_job, runtime_root_contract)
        if array_tasks is not None:
            manifest["tasks"] = array_tasks
        return (
            SubmitJobRequest(
                run_id=retry_job.run_id,
                model_id=model_id,
                job_type=retry_job.job_type,
                manifest=manifest,
            ),
            runtime_root_resolution,
            runtime_root_contract,
        )

    def _resolve_file_retry_runtime_roots(self, retry_job: _RetrySubmissionJob) -> SimpleNamespace | None:
        candidate_batch = self._file_retry_runtime_root_candidates(retry_job)
        db_free_required = _candidate_batch_db_free_required(candidate_batch)
        runtime_roots_required = retry_job.job_type == DOWNLOAD_SOURCE_CYCLE_JOB_TYPE or db_free_required
        rejected: list[dict[str, str]] = []
        rejected_total_count = 0
        best_resolved: dict[str, tuple[str, str]] = {}
        best_missing = list(_REQUIRED_RUNTIME_ROOT_FIELDS)
        secret_rejected = False
        unsafe_rejected = False
        best_db_free_resolved: dict[str, tuple[str, str]] = {}
        best_db_free_missing: list[str] = list(_DB_FREE_REQUIRED_SELECTOR_FIELDS) if db_free_required else []
        for candidate in candidate_batch.candidates:
            resolution = _resolve_runtime_root_candidate(candidate.source, candidate.value)
            db_free_resolution = (
                _resolve_db_free_runtime_candidate(candidate.source, candidate.value) if db_free_required else None
            )
            rejected_total_count += len(resolution.rejected)
            if db_free_resolution is not None:
                rejected_total_count += len(db_free_resolution.rejected)
            if len(rejected) < _RUNTIME_ROOT_REJECTION_EVIDENCE_LIMIT:
                remaining = _RUNTIME_ROOT_REJECTION_EVIDENCE_LIMIT - len(rejected)
                rejected.extend(resolution.rejected[:remaining])
            if db_free_resolution is not None and len(rejected) < _RUNTIME_ROOT_REJECTION_EVIDENCE_LIMIT:
                remaining = _RUNTIME_ROOT_REJECTION_EVIDENCE_LIMIT - len(rejected)
                rejected.extend(db_free_resolution.rejected[:remaining])
            candidate_secret_rejected = resolution.secret_rejected
            candidate_unsafe_rejected = resolution.unsafe_rejected
            secret_rejected = secret_rejected or candidate_secret_rejected
            unsafe_rejected = unsafe_rejected or candidate_unsafe_rejected
            if db_free_resolution is not None:
                candidate_secret_rejected = candidate_secret_rejected or db_free_resolution.secret_rejected
                candidate_unsafe_rejected = candidate_unsafe_rejected or db_free_resolution.unsafe_rejected
                secret_rejected = secret_rejected or db_free_resolution.secret_rejected
                unsafe_rejected = unsafe_rejected or db_free_resolution.unsafe_rejected
            if len(resolution.resolved) > len(best_resolved):
                best_resolved = resolution.resolved
                best_missing = resolution.missing
            if db_free_resolution is not None and len(db_free_resolution.resolved) > len(best_db_free_resolved):
                best_db_free_resolved = db_free_resolution.resolved
                best_db_free_missing = db_free_resolution.missing
            db_free_complete = db_free_resolution is None or db_free_resolution.complete
            if (
                not resolution.complete
                or not db_free_complete
                or candidate_secret_rejected
                or candidate_unsafe_rejected
            ):
                continue
            evidence = _runtime_root_resolution_evidence(
                retry_job,
                resolved=resolution.resolved,
                missing=[],
                rejected=rejected,
                rejected_total_count=rejected_total_count,
                candidate_batch=candidate_batch,
                db_free_resolved=db_free_resolution.resolved if db_free_resolution is not None else {},
                db_free_missing=[] if db_free_resolution is not None else [],
                db_free_required=db_free_required,
            )
            manifest_fields = {field: value for field, (value, _source) in resolution.resolved.items()}
            if db_free_resolution is not None:
                manifest_fields.update(
                    {field: value for field, (value, _source) in db_free_resolution.resolved.items()}
                )
            return SimpleNamespace(manifest_fields=manifest_fields, evidence=evidence)
        if not runtime_roots_required:
            return None
        evidence = _runtime_root_resolution_evidence(
            retry_job,
            resolved=best_resolved,
            missing=best_missing,
            rejected=rejected,
            rejected_total_count=rejected_total_count,
            candidate_batch=candidate_batch,
            db_free_resolved=best_db_free_resolved,
            db_free_missing=best_db_free_missing,
            db_free_required=db_free_required,
        )
        if secret_rejected:
            raise _RetryRuntimeRootResolutionError(
                RETRY_RUNTIME_ROOTS_SECRET_BEARING,
                "Manual retry runtime-root evidence contains secret-bearing values.",
                evidence,
            )
        if unsafe_rejected:
            raise _RetryRuntimeRootResolutionError(
                RETRY_RUNTIME_ROOTS_UNSAFE,
                "Manual retry runtime-root evidence contains unsafe local root values.",
                evidence,
            )
        if best_missing:
            raise _RetryRuntimeRootResolutionError(
                RETRY_RUNTIME_ROOTS_UNRESOLVED,
                "Manual retry cannot resolve required object-store runtime roots.",
                evidence,
            )
        if best_db_free_missing:
            raise _RetryRuntimeRootResolutionError(
                RETRY_RUNTIME_ROOTS_UNRESOLVED,
                "Manual retry cannot resolve required DB-free scheduler runtime selectors.",
                evidence,
            )
        raise _RetryRuntimeRootResolutionError(
            RETRY_RUNTIME_ROOTS_UNRESOLVED,
            "Manual retry cannot resolve required runtime roots.",
            evidence,
        )

    def _file_retry_runtime_root_candidates(self, retry_job: _RetrySubmissionJob) -> _RuntimeRootCandidateBatch:
        candidates: list[_RuntimeRootCandidate] = []
        provenance_job_ids: list[str] = []
        event_candidate_returned_count = 0
        event_candidate_total_count = 0
        event_candidate_omitted_count = 0
        event_rows_scanned_count = 0
        event_rows_total_count = 0
        event_rows_omitted_count = 0
        manual_retry_event_rows_ignored = 0
        if retry_job.previous_job_id:
            provenance_job_ids = self._file_retry_provenance_job_ids(str(retry_job.previous_job_id))
        for job_id in provenance_job_ids:
            if len(candidates) >= _RUNTIME_ROOT_EVENT_CANDIDATE_LIMIT:
                break
            event_batch = self._file_retry_event_runtime_root_candidates(
                job_id,
                candidate_budget=_RUNTIME_ROOT_EVENT_CANDIDATE_LIMIT - len(candidates),
            )
            candidates.extend(event_batch.candidates)
            event_candidate_returned_count += event_batch.event_candidate_returned_count
            event_candidate_total_count += event_batch.event_candidate_total_count
            event_candidate_omitted_count += event_batch.event_candidate_omitted_count
            event_rows_scanned_count += event_batch.event_rows_scanned_count
            event_rows_total_count += event_batch.event_rows_total_count
            event_rows_omitted_count += event_batch.event_rows_omitted_count
            manual_retry_event_rows_ignored += event_batch.manual_retry_event_rows_ignored
        excluded = set(provenance_job_ids)
        if retry_job.run_id:
            same_run_jobs = [
                job
                for job in sorted(
                    self.repository.query_pipeline_jobs_by_run(str(retry_job.run_id)),
                    key=_db_compatible_pipeline_job_order_key,
                )
                if str(job.get("job_id") or "")
                and str(job.get("job_id") or "") not in excluded
                and str(job.get("job_id") or "") != retry_job.job_id
                and str(job.get("job_type") or "") == DOWNLOAD_SOURCE_CYCLE_JOB_TYPE
                and not (
                    retry_job.cycle_id
                    and job.get("cycle_id") not in (None, "")
                    and job.get("cycle_id") != retry_job.cycle_id
                )
                and job.get("manual_retry_marker") is not True
            ]
            same_run_scan_jobs = same_run_jobs[:_RUNTIME_ROOT_SAME_RUN_JOB_SCAN_LIMIT]
            same_run_jobs_omitted = max(len(same_run_jobs) - len(same_run_scan_jobs), 0)
            event_rows_total_count += len(same_run_jobs)
            event_rows_omitted_count += same_run_jobs_omitted
            for job in same_run_scan_jobs:
                job_id = str(job.get("job_id") or "")
                if len(candidates) >= _RUNTIME_ROOT_EVENT_CANDIDATE_LIMIT:
                    break
                event_batch = self._file_retry_event_runtime_root_candidates(
                    job_id,
                    candidate_budget=_RUNTIME_ROOT_EVENT_CANDIDATE_LIMIT - len(candidates),
                )
                candidates.extend(event_batch.candidates)
                event_candidate_returned_count += event_batch.event_candidate_returned_count
                event_candidate_total_count += event_batch.event_candidate_total_count
                event_candidate_omitted_count += event_batch.event_candidate_omitted_count
                event_rows_scanned_count += event_batch.event_rows_scanned_count
                event_rows_total_count += event_batch.event_rows_total_count
                event_rows_omitted_count += event_batch.event_rows_omitted_count
                manual_retry_event_rows_ignored += event_batch.manual_retry_event_rows_ignored
        env_candidate = _runtime_root_env_candidate()
        if env_candidate:
            candidates.append(_RuntimeRootCandidate("runtime_config:environment", env_candidate))
        return _RuntimeRootCandidateBatch(
            candidates=candidates,
            event_candidate_returned_count=event_candidate_returned_count,
            event_candidate_total_count=event_candidate_total_count,
            event_candidate_omitted_count=event_candidate_omitted_count,
            event_rows_scanned_count=event_rows_scanned_count,
            event_rows_total_count=event_rows_total_count,
            event_rows_omitted_count=event_rows_omitted_count,
            manual_retry_event_rows_ignored=manual_retry_event_rows_ignored,
        )

    def _file_retry_provenance_job_ids(self, job_id: str) -> list[str]:
        job_ids: list[str] = []
        seen: set[str] = set()
        current: str | None = job_id
        for _ in range(16):
            if not current or current in seen:
                break
            seen.add(current)
            job_ids.append(current)
            current = self._file_retry_previous_job_id(current)
        return job_ids

    def _file_retry_previous_job_id(self, job_id: str) -> str | None:
        job = self.repository.get_pipeline_job(job_id)
        if job is None:
            return None
        source_id = _source_id_from_job(job)
        cycle_time = _cycle_time_from_job(job)
        model_id = _optional_safe_identity(job, "model_id")
        rows = self.repository._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        retry_events = sorted(
            (
                event
                for event in rows.pipeline_events
                if str(event.get("entity_id") or "") == job_id and str(event.get("event_type") or "") == "retry"
            ),
            key=lambda event: _optional_positive_int(event.get("event_id")) or 0,
            reverse=True,
        )
        for event in retry_events:
            details = event.get("details") if isinstance(event.get("details"), Mapping) else {}
            previous_job_id = details.get("previous_job_id")
            if isinstance(previous_job_id, str) and previous_job_id.strip():
                return previous_job_id.strip()
        return None

    def _file_retry_event_runtime_root_candidates(
        self,
        job_id: str,
        *,
        candidate_budget: int,
    ) -> _RuntimeRootCandidateBatch:
        job = self.repository.get_pipeline_job(job_id)
        if job is None:
            return _RuntimeRootCandidateBatch(candidates=[])
        source_id = _source_id_from_job(job)
        cycle_time = _cycle_time_from_job(job)
        model_id = _optional_safe_identity(job, "model_id")
        rows = self.repository._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=model_id)
        submission_events = [
            event
            for event in rows.pipeline_events
            if str(event.get("entity_id") or "") == job_id and str(event.get("event_type") or "") == "submission"
        ]
        event_rows_total_count = len(submission_events)
        if job.get("manual_retry_marker") is True:
            return _RuntimeRootCandidateBatch(
                candidates=[],
                event_rows_total_count=event_rows_total_count,
                manual_retry_event_rows_ignored=event_rows_total_count,
            )
        events = sorted(
            submission_events,
            key=lambda event: _optional_positive_int(event.get("event_id")) or 0,
            reverse=True,
        )[:_RUNTIME_ROOT_EVENT_ROW_SCAN_LIMIT]
        candidates: list[_RuntimeRootCandidate] = []
        event_candidate_total_count = 0
        manual_retry_event_rows_ignored = 0
        for event in events:
            details = event.get("details") if isinstance(event.get("details"), Mapping) else {}
            if _event_details_is_manual_retry_submission(details):
                manual_retry_event_rows_ignored += 1
                continue

            event_id = event.get("event_id")
            event_source = f"file_journal_event:{job_id}:{event_id}"
            private_batch = self.repository._pipeline_event_private_runtime_root_candidates(
                job,
                event,
                candidate_budget=candidate_budget - len(candidates),
            )
            if private_batch is not None:
                candidates.extend(private_batch.candidates)
                event_candidate_total_count += private_batch.event_candidate_total_count
                continue
            for path in _RUNTIME_ROOT_EVENT_CANDIDATE_PATHS:
                candidate = _mapping_at(details, path)
                if candidate and _has_runtime_root_field(candidate):
                    event_candidate_total_count += 1
                    if len(candidates) < candidate_budget:
                        candidates.append(
                            _RuntimeRootCandidate(
                                f"{event_source}:{'.'.join(path)}",
                                candidate,
                            )
                        )
            if _has_runtime_root_field(details):
                event_candidate_total_count += 1
                if len(candidates) < candidate_budget:
                    candidates.append(
                        _RuntimeRootCandidate(
                            f"{event_source}:details",
                            details,
                        )
                    )
        return _RuntimeRootCandidateBatch(
            candidates=candidates,
            event_candidate_returned_count=len(candidates),
            event_candidate_total_count=event_candidate_total_count,
            event_candidate_omitted_count=max(event_candidate_total_count - len(candidates), 0),
            event_rows_scanned_count=len(events),
            event_rows_total_count=event_rows_total_count,
            event_rows_omitted_count=max(event_rows_total_count - len(events), 0),
            manual_retry_event_rows_ignored=manual_retry_event_rows_ignored,
        )

    def _record_manual_retry_submission_success(
        self,
        job_id: str,
        submitted: Any,
        trusted_pending_snapshot: Mapping[str, Any],
        *,
        runtime_root_resolution: dict[str, Any] | None = None,
        runtime_root_contract: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload = _file_retry_gateway_payload(submitted)
        # The gateway side effect already happened, so a pending row whose
        # lineage was corrupted DURING the call must not lose its submission
        # binding: validate the trusted pre-submit snapshot and write its
        # exact normalized lineage, never the later (possibly malformed or
        # divergent) current row's copy and never a public projection.
        trusted_lineage = _strict_retry_init_state_identities(
            trusted_pending_snapshot.get(INIT_STATE_IDENTITY_FIELD)
        )
        # Update the PRIVATE durable row: the public projection's lineage
        # and URI fields are display placeholders, and writing that copy
        # back would launder them over the pending row's real values.
        row = self.repository._pipeline_job_for_id_unlocked(job_id)
        if row is None:
            raise RetryNotFoundError(job_id)
        slurm_job_id = payload.get("job_id") or payload.get("slurm_job_id")
        row.update(
            {
                "status": "submitted",
                "slurm_job_id": str(slurm_job_id) if slurm_job_id is not None else None,
                "submitted_at": _format_utc(_file_retry_gateway_time(payload.get("submitted_at")) or _utcnow()),
                "started_at": _optional_format_datetime(payload.get("started_at"), field="started_at"),
                "finished_at": _optional_format_datetime(payload.get("finished_at"), field="finished_at"),
                "error_code": None,
                "error_message": None,
                INIT_STATE_IDENTITY_FIELD: trusted_lineage,
                "updated_at": _format_utc(_utcnow()),
            }
        )
        written = self.repository.upsert_pipeline_job(row)
        self._reset_hydro_run_after_retry_submission(written)
        details: dict[str, Any] = {
            "trigger": "manual",
            "slurm_job_id": written.get("slurm_job_id"),
            "gateway_status": str(payload.get("status")) if payload.get("status") is not None else None,
        }
        if runtime_root_resolution is not None:
            details["runtime_root_resolution"] = _public_evidence(runtime_root_resolution)
        if runtime_root_contract is not None:
            details["runtime_root_contract"] = _public_evidence(runtime_root_contract)
        self.repository.insert_pipeline_event(
            entity_type="pipeline_job",
            entity_id=job_id,
            event_type="submission",
            status_from="pending",
            status_to="submitted",
            message=f"Manual retry submitted as Slurm job {written.get('slurm_job_id')}.",
            details=details,
        )
        return written

    def _reset_hydro_run_after_retry_submission(self, retry_job: Mapping[str, Any]) -> None:
        run_id = retry_job.get("run_id")
        if run_id in (None, ""):
            return
        existing = self.repository._hydro_run_for(str(run_id))
        if existing is None or str(existing.get("status") or "") not in {"failed", "cancelled"}:
            return
        self.repository.update_hydro_run_status(str(run_id), "pending", slurm_job_id=retry_job.get("slurm_job_id"))

    def _record_manual_retry_submission_failure(self, job_id: str, error: Exception) -> dict[str, Any]:
        error_code = _retry_submission_error_code(error)
        error_message = _safe_error_message(str(getattr(error, "message", None) or error))
        _previous_status, written = self.repository.update_pipeline_job_status(
            job_id,
            "submission_failed",
            error_code=error_code,
            error_message=error_message,
            finished_at=_utcnow(),
        )
        self.repository.insert_pipeline_event(
            entity_type="pipeline_job",
            entity_id=job_id,
            event_type="submission",
            status_from="pending",
            status_to="submission_failed",
            message=f"Manual retry submission failed: {error_message}",
            details=_manual_retry_submission_failure_details(error, error_code=error_code, error_message=error_message),
        )
        return written

    def _manual_retry_source_for_run(self, run_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        jobs = self.repository.query_pipeline_jobs_by_run(run_id)
        safe_jobs = sorted(
            (job for job in jobs if str(job.get("job_id") or "") != "file_journal_read_blocked"),
            key=_file_retry_job_truth_sort_key,
        )
        durable_run = self.repository._hydro_run_for(run_id)
        durable_status = str(durable_run.get("status") or "") if durable_run is not None else None
        if durable_status in MANUAL_RETRY_DURABLE_SUCCESS_STATUSES:
            return None, None
        active_job = next((job for job in safe_jobs if str(job.get("status") or "") in ACTIVE_RETRY_STATUSES), None)
        if active_job is not None:
            return None, active_job
        if not safe_jobs:
            return None, None
        latest_job = safe_jobs[-1]
        latest_status = str(latest_job.get("status") or "")
        if latest_status in MANUAL_RETRY_SOURCE_STATUSES:
            return latest_job, None
        if durable_status is not None and (
            durable_status in PARTIAL_OR_FAILED_HYDRO_STATUSES or durable_status.startswith("failed")
        ):
            failed_job = next(
                (job for job in reversed(safe_jobs) if str(job.get("status") or "") in MANUAL_RETRY_SOURCE_STATUSES),
                None,
            )
            return failed_job, None
        if latest_status in TERMINAL_SUCCESS_RETRY_STATUSES:
            return None, None
        return None, None


def _next_current_master_retry_identity(current: Mapping[str, Any]) -> tuple[str, int]:
    job_id = str(current.get("job_id") or "")
    base_job_id, _suffix_attempt = split_retry_job_identity(job_id)
    retry_count = effective_retry_attempt(job_id, current.get("retry_count")) + 1
    return f"{base_job_id}{RETRY_JOB_ID_MARKER}{retry_count}", retry_count


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default) + "\n").encode("utf-8")


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return _format_utc(value)
    return str(value)


def _journal_record_for_write(
    record_type: str,
    payload: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str | None,
    sequence: int,
) -> dict[str, Any]:
    # #1592: every journal record is constructed here, so the anti-laundering
    # strip belongs here rather than at each write call site.  Two durable write
    # paths -- the cohort projection payload loop and
    # ``_write_pipeline_job_unlocked`` -- never reach
    # ``_append_validated_record_unlocked``, so a caller round-tripping a public
    # row used to launder ``[object-uri]``/``[uri]`` into durable state through
    # them.  Placed before the sibling error-message sanitizer, matching the
    # existing order at that outer call site.
    #
    # THE PRINCIPLE, not an exception list: the anti-laundering strip removes
    # placeholders that came FROM A CALLER.  It must never run downstream of the
    # journal's own public rendering, because that rendering DELIBERATELY
    # produces ``[object-uri]``/``[local-path]`` as the durable public value.
    # ``_append_validated_record_unlocked`` sanitizes pipeline_event payloads
    # (``_public_pipeline_event_payload``) AFTER its own caller-boundary strip
    # and immediately before calling this function, so stripping events here
    # would erase deliberate evidence rather than laundering -- turning a
    # rendered "there was an object URI here" into "there was nothing here".
    # The layering that follows is therefore: an event that goes through
    # ``_append_validated_record_unlocked`` strips at that caller boundary,
    # where its raw un-sanitized payload still exists; the job lane strips here,
    # at the record constructor.  That is NOT a guarantee for every event -- five
    # inline payload loops build their payload dicts and call this function
    # directly, so whatever they emit gets neither the caller-boundary strip nor
    # ``_public_pipeline_event_payload``:
    #
    #   * ``reject_pipeline_job_submit_attempt`` (submission-failed rejection)
    #     -- one ``submission`` event;
    #   * ``mark_pipeline_job_permanently_failed`` (permanent-failure mark)
    #     -- one ``permanently_failed`` event;
    #   * ``permit_pipeline_job_retry`` (reservation-lost release) -- NO event
    #     payload at all, only ``hydro_run``/``pipeline_job`` rows;
    #   * ``project_forecast_cohort_tasks`` (cohort task projection) -- TWO
    #     events, ``array_task_reconciled`` and ``status_change``;
    #   * ``demote_operator_verified_reserved_job`` (operator-verified demotion)
    #     -- ONE ``operator_verified_absence`` event whose audited ``details``
    #     (``checked_by`` / ``verification_note``) are pre-sanitized at the
    #     single operator-evidence authority (``_operator_evidence_text``) so
    #     they are bounded, secret-redacted, and local/object-path sanitized
    #     before this loop; the remaining detail fields are fixed/typed values.
    #
    # The four that do emit events are safe only because their ``details`` carry
    # no URI-bearing field, audited field by field in design D1 and re-audited
    # for both projection payloads and the demotion audit details; that is the
    # declared cost of the carve-out, not a structural property.  Anyone adding
    # a sixth loop, or a second renderer, must apply the same rule to it rather
    # than add another record_type here.
    if record_type != "pipeline_event":
        payload = _strip_redaction_placeholders(payload)
    payload = _redact_durable_error_message_fields(record_type, payload)
    record: dict[str, Any] = {
        "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
        "sequence": int(sequence),
        "record_type": record_type,
        "source_id": _normalize_file_source_id(source_id, field="source_id"),
        "cycle_time": _format_utc(cycle_time),
        "created_at": _format_utc(_utcnow()),
        "payload": _strip_internal_fields(payload),
    }
    if model_id not in (None, ""):
        record["model_id"] = _safe_identity_text(str(model_id), field="model_id")
    for payload_field in ("job_id", "run_id", "cycle_id", "event_id", "entity_id", "forcing_version_id"):
        value = payload.get(payload_field)
        if value not in (None, ""):
            record[payload_field] = value
    return record


def _public_pipeline_event_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(payload)
    details = row.get("details")
    row["details"] = _public_evidence(details) if isinstance(details, Mapping) else _public_evidence(details or {})
    if "message" in row:
        row["message"] = _public_message(row.get("message"))
    return row


def _private_runtime_root_recovery_record(
    payload: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str | None,
) -> dict[str, Any] | None:
    details = payload.get("details") if isinstance(payload.get("details"), Mapping) else {}
    candidates = _runtime_root_recovery_candidate_records(details)
    if not candidates:
        return None
    event_id = payload.get("event_id")
    if event_id in (None, ""):
        return None
    entity_id = _required_safe_identity(payload, "entity_id")
    record: dict[str, Any] = {
        "schema_version": FILE_ORCHESTRATION_PRIVATE_RECOVERY_SCHEMA_VERSION,
        "record_type": _PRIVATE_RUNTIME_ROOT_RECOVERY_RECORD_TYPE,
        "source_id": _normalize_file_source_id(source_id, field="source_id"),
        "cycle_time": _format_utc(cycle_time),
        "entity_type": str(payload.get("entity_type") or "pipeline_job"),
        "entity_id": entity_id,
        "event_type": str(payload.get("event_type") or ""),
        "event_id": str(event_id),
        "status_from": payload.get("status_from"),
        "status_to": payload.get("status_to"),
        "event_created_at": payload.get("created_at"),
        "created_at": _format_utc(_utcnow()),
        "candidates": candidates,
    }
    if model_id not in (None, ""):
        record["model_id"] = _safe_identity_text(str(model_id), field="model_id")
    _validate_json_complexity(
        record,
        field="private_runtime_root_recovery",
        max_nodes=MAX_FILE_JOURNAL_JSON_NODES,
        max_depth=MAX_FILE_JOURNAL_JSON_DEPTH,
    )
    return record


def _runtime_root_recovery_candidate_records(details: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for path in _RUNTIME_ROOT_EVENT_CANDIDATE_PATHS:
        candidate = _mapping_at(details, path)
        if candidate and _has_runtime_root_field(candidate):
            value = _runtime_root_recovery_candidate_value(candidate)
            if value:
                candidates.append({"path": list(path), "value": value})
    if _has_runtime_root_field(details):
        value = _runtime_root_recovery_candidate_value(details)
        if value:
            candidates.append({"path": ["details"], "value": value})
    return candidates


def _runtime_root_recovery_candidate_value(candidate: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for root_field in (*_RUNTIME_ROOT_FIELDS, *_DB_FREE_RUNTIME_FIELDS):
        if root_field not in candidate:
            continue
        value = _strip_internal_fields(candidate[root_field])
        if isinstance(value, str) and secret_manifest_value_reason(value) is not None:
            continue
        values[root_field] = value
    return values


def _private_runtime_root_recovery_path(
    root: Path,
    *,
    source_id: str,
    cycle_time: datetime,
    entity_id: str,
    event_id: str,
) -> Path:
    return (
        root
        / "private"
        / "runtime-root-recovery"
        / _safe_segment(_normalize_file_source_id(source_id, field="source_id"))
        / format_cycle_time(cycle_time)
        / _safe_segment(entity_id)
        / f"{_safe_segment(event_id)}.json"
    )


def _validate_private_runtime_root_recovery_record(
    row: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str | None,
    event: Mapping[str, Any],
) -> None:
    _require_schema(row, FILE_ORCHESTRATION_PRIVATE_RECOVERY_SCHEMA_VERSION)
    if row.get("record_type") != _PRIVATE_RUNTIME_ROOT_RECOVERY_RECORD_TYPE:
        raise FileOrchestrationJournalError("file_journal_record_type_mismatch", field="record_type")
    _require_source_cycle(row, source_id=source_id, cycle_time=cycle_time)
    if model_id not in (None, ""):
        _require_model_id(row, str(model_id), required=False)
    for identity_field in ("entity_type", "entity_id", "event_type", "event_id", "status_from", "status_to"):
        if _private_event_identity_value(row.get(identity_field)) != _private_event_identity_value(
            event.get(identity_field)
        ):
            raise FileOrchestrationJournalError(
                "file_journal_event_mismatch",
                field=identity_field,
                evidence={
                    "expected": _private_event_identity_value(event.get(identity_field))[:80],
                    "actual": _private_event_identity_value(row.get(identity_field))[:80],
                },
            )
    event_created_at = _private_event_identity_value(event.get("created_at"))
    if event_created_at and _private_event_identity_value(row.get("event_created_at")) != event_created_at:
        raise FileOrchestrationJournalError("file_journal_event_mismatch", field="event_created_at")


#: Fixed non-secret token for EVERY caught projection-warning exception.  The
#: fixture only requires the warning to name the failed projection; projection
#: and model_id already do that.  A constant (never a hash or any other
#: deterministic derivation of the raw input) keeps the warning non-secret by
#: construction: the arbitrary exception text, class name, ``.reason`` or
#: ``.error_code`` is used only to decide *that* it must not be exposed, never
#: to produce an output that a correlation or low-entropy dictionary attack
#: could tie back to it.  Even exact ``OrchestratorError`` /
#: ``FileOrchestrationJournalError`` instances are NOT trusted, because their
#: constructors accept arbitrary code/reason strings that could be compact
#: secrets.
_PROJECTION_WARNING_FALLBACK_TOKEN = "projection_fault"


def _projection_error_reason(error: Exception) -> str:
    """A fixed, non-secret reason token for a post-commit projection fault.

    Every caught exception maps to the same constant: never the exception
    text, ``.error_code``, ``.reason``, class name, path, or any secret-shaped
    detail, compact or otherwise.
    """
    del error
    return _PROJECTION_WARNING_FALLBACK_TOKEN


def _emit_reclaim_projection_warning(
    projection: str, model_id: str | None, error: Exception
) -> None:
    """One bounded observable warning per failed committed-reclaim projection.

    The authority append has already committed, so the warning is the only
    surface that reports the fault; it must never turn committed success into
    a failure.  Every field is a fixed non-secret token: the stable projection
    name, the already-validated model id, and the fixed warning code.  Never
    the exception text, class name, path, ``.error_code``, ``.reason``, repr,
    or any secret-shaped detail.  A logger failure is absorbed so the reclaim
    result is unaffected.
    """
    try:
        LOGGER.warning(
            "committed reclaim projection fault: projection=%s model_id=%s code=FILE_JOURNAL_RECLAIM_PROJECTION_FAULT",
            projection,
            model_id,
        )
    except Exception:  # noqa: BLE001 - observability must never fail the committed reclaim.
        pass
    del error


def _projection_error_type(error: Exception) -> str:
    """A fixed, non-secret ``error_type`` token for a projection warning.

    Every caught exception maps to the same constant, so no class name -- even
    an exact trusted-type label or a safe-looking identifier -- can be echoed.
    """
    del error
    return _PROJECTION_WARNING_FALLBACK_TOKEN


def _private_event_identity_value(value: Any) -> str:
    return "" if value in (None, "") else str(value)


def _strip_internal_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _strip_internal_fields(item)
            for key, item in value.items()
            if not str(key).startswith("_file_journal_")
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_strip_internal_fields(item) for item in value]
    if isinstance(value, datetime):
        return _format_utc(value)
    return value


def _durable_error_message(value: Any) -> str | None:
    if value is None:
        return None
    return _safe_error_message(str(value))


#: Bounded operator attribution text for the #1564 demotion CAS.  The bounds
#: keep one operator record far under the journal record/byte ceilings and the
#: redaction keeps secrets out of the durable event and the CLI receipt.
MAX_OPERATOR_CHECKED_BY_LENGTH = 256
MAX_OPERATOR_VERIFICATION_NOTE_LENGTH = 2048


def _operator_evidence_text(value: Any, *, field: str) -> str:
    """Return one bounded, secret-redacted, path-sanitized operator attribution string.

    This is the single authority for operator evidence text on the #1564
    demotion: the returned value is exactly what the durable audit event stores
    and what the CLI receipt echoes, so it must be non-secret and path/URI
    sanitized by construction here -- the demotion inline batch bypasses
    ``_public_pipeline_event_payload``.  Required (non-blank) and bounded on
    every entry point, so no demotion can be recorded without a named checker
    and a non-empty note, and no caller can smuggle an unbounded,
    secret-bearing, or path-bearing value into the durable audit event.
    """

    if not isinstance(value, str) or not value.strip():
        raise FileOrchestrationJournalError("file_journal_evidence_required", field=field)
    limit = MAX_OPERATOR_CHECKED_BY_LENGTH if field == "checked_by" else MAX_OPERATOR_VERIFICATION_NOTE_LENGTH
    if len(value) > limit:
        raise FileOrchestrationJournalError("file_journal_evidence_limit_exceeded", field=field)
    sanitized = _safe_error_message(value)
    # Apply the same public evidence sanitizer the display/read path uses, so a
    # local path, object URI, or credential never survives into the durable
    # event or the CLI receipt.  The return stays a str and non-blank.
    sanitized = _public_message(sanitized)
    if not isinstance(sanitized, str) or not sanitized.strip():
        raise FileOrchestrationJournalError("file_journal_evidence_required", field=field)
    return sanitized


def _redact_durable_error_message_fields(record_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(payload)
    if record_type in {"pipeline_job", "hydro_run", "forecast_cycle"} and "error_message" in row:
        row["error_message"] = _durable_error_message(row.get("error_message"))
    return row


def _mapping_value(row: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    value = row.get(field)
    if not isinstance(value, Mapping):
        raise FileOrchestrationJournalError("file_journal_expected_object", field=field)
    return value


def _optional_mapping_value(row: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    value = row.get(field)
    if value in (None, ""):
        return {}
    if not isinstance(value, Mapping):
        raise FileOrchestrationJournalError("file_journal_expected_object", field=field)
    return value


def _coerce_datetime(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    try:
        return parse_cycle_time(str(value))
    except (TypeError, ValueError) as error:
        raise FileOrchestrationJournalError("file_journal_invalid_datetime", field=field) from error


def _strict_utc_datetime(value: Any) -> datetime | None:
    """Strict aware-UTC coercion at evidence-authority boundaries.

    Accepts an aware ``datetime`` or an ISO-8601 string (the durable ``...Z``
    shape) and returns its UTC instant, or ``None`` for anything else, so a
    caller can fail closed on a missing/invalid boundary instant rather than
    inventing a timezone.
    """

    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
        return _strict_utc_datetime(parsed)
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    except (OverflowError, TypeError, ValueError):
        return None


def _optional_format_datetime(value: Any, *, field: str) -> str | None:
    if value in (None, ""):
        return None
    return _format_utc(_coerce_datetime(value, field=field))


def _file_retry_job_value(job: Any, field: str) -> Any:
    if isinstance(job, Mapping):
        return job.get(field)
    return getattr(job, field, None)


def _file_retry_job_text(job: Any, field: str) -> str | None:
    value = _file_retry_job_value(job, field)
    return str(value) if value not in (None, "") else None


def _file_reconcile_namespace(row: Mapping[str, Any]) -> SimpleNamespace:
    payload = dict(row)
    for dt_field in (
        "created_at",
        "updated_at",
        "submitted_at",
        "started_at",
        "finished_at",
        "cycle_time",
        "submission_attempt_started_at",
    ):
        value = payload.get(dt_field)
        if value in (None, ""):
            continue
        try:
            payload[dt_field] = _coerce_datetime(value, field=dt_field)
        except FileOrchestrationJournalError:
            pass
    return SimpleNamespace(**payload)


def _bounded_cohort_members(value: Any) -> list[dict[str, Any]]:
    """Return the durable, credential-safe ordered member identity map."""
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    allowed = (
        "array_task_id",
        "candidate_id",
        "run_id",
        "model_id",
        "basin_id",
        "scenario_id",
        "restart_stage",
    )
    result: list[dict[str, Any]] = []
    for item in value[:256]:
        if not isinstance(item, Mapping):
            continue
        member = {key: item.get(key) for key in allowed}
        result.append(member)
    return result


def _bounded_init_state_identities(value: Any) -> list[dict[str, Any]]:
    """Return the durable per-model init-state identity map (#1183).

    Unlike the member map, identity fields are optional per entry: only the
    keys actually recorded are kept, so an unrecorded checksum stays absent
    instead of becoming a null-valued claim.
    """

    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    result: list[dict[str, Any]] = []
    for item in value[:MAX_FORECAST_COHORT_MEMBERS]:
        if not isinstance(item, Mapping):
            continue
        entry = {
            key: item[key]
            for key in (
                "array_task_id",
                "model_id",
                "init_state_id",
                "init_state_checksum",
                "init_state_uri",
                "init_state_valid_time",
            )
            if key in item and item[key] is not None
        }
        result.append(entry)
    return result


_RETRY_LINEAGE_PLACEHOLDER_TOKENS = frozenset(
    {"[object-uri]", "[uri]", "[local-path]", "[redacted]", "sha256:[redacted]"}
)


def _retry_lineage_contains_placeholder(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_retry_lineage_contains_placeholder(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return any(_retry_lineage_contains_placeholder(item) for item in value)
    if isinstance(value, str):
        return value in _RETRY_LINEAGE_PLACEHOLDER_TOKENS
    return False


def _strict_retry_init_state_identities(value: Any) -> list[dict[str, Any]]:
    """Strict retry-lineage boundary shared by both retry producers.

    Unlike ``_bounded_init_state_identities`` (which tolerantly normalizes
    malformed input to a partial map), a retry row's inherited lineage must be
    either genuinely empty (absent/None/``[]``) or fully valid: a malformed
    map silently normalized to ``[]`` would falsify the attempt's warm-start
    provenance, and a placeholder-bearing value would launder display
    redaction into durable state.  Raises ``FileOrchestrationJournalError``
    with stable reason/field only.
    """

    if value is None:
        return []
    try:
        normalized = normalize_init_state_identities(value)
    except AcceptedSubmitEvidenceError as error:
        raise FileOrchestrationJournalError(error.reason, field=error.field) from error
    if _retry_lineage_contains_placeholder(normalized):
        raise FileOrchestrationJournalError(
            "file_journal_evidence_placeholder_invalid",
            field=INIT_STATE_IDENTITY_FIELD,
        )
    return normalized


def _bounded_candidate_projections(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    allowed = (
        "candidate_id",
        "run_id",
        "model_id",
        "array_task_id",
        "array_task_outcome",
        "restart_stage",
        "native_shud_resubmitted",
    )
    return [{key: item.get(key) for key in allowed} for item in value[:256] if isinstance(item, Mapping)]


def _validate_accepted_submit_evidence(row: Mapping[str, Any]) -> None:
    """Delegate every file surface to the canonical accepted-submit boundary."""

    try:
        normalize_accepted_submit_evidence(row)
    except AcceptedSubmitEvidenceError as error:
        raise FileOrchestrationJournalError(error.reason, field=error.field) from error


def _accepted_submit_master_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return accepted_submit_master_immutable_identity(row)
    except AcceptedSubmitEvidenceError as error:
        raise FileOrchestrationJournalError(error.reason, field=error.field) from error


def _accepted_submit_master_state(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return accepted_submit_master_ordinary_upsert_state(row)
    except AcceptedSubmitEvidenceError as error:
        raise FileOrchestrationJournalError(error.reason, field=error.field) from error


def _accepted_submit_candidate_evidence(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return accepted_submit_candidate_immutable_evidence(row)
    except AcceptedSubmitEvidenceError as error:
        raise FileOrchestrationJournalError(error.reason, field=error.field) from error


def _accepted_submit_attempt_anchor(value: Any) -> str:
    try:
        return normalize_accepted_submit_attempt_anchor(value)
    except AcceptedSubmitEvidenceError as error:
        raise FileOrchestrationJournalError(error.reason, field=error.field) from error


def _file_journal_real_slurm_job_id(value: Any) -> bool:
    text = str(value or "")
    return bool(text and text.lower() != "local")


def _reconcile_inventory_row_kind(job: Mapping[str, Any]) -> str | None:
    if accepted_submit_contract_is_current(job):
        return "current_master" if accepted_submit_row_kind(job) == "master" else None
    return "legacy"


def _settled_incarnation_matches_candidate(
    canonical: Mapping[str, Any],
    candidate_submit: datetime,
) -> bool:
    """Return whether a settled same-id sibling owns the candidate's accounting incarnation.

    #1850 Fix C (centralized): ONLY strict canonical ``slurm_accounting_submitted_at``
    from a provenance-compatible accounting bind may prove recycle. The legacy
    ``submitted_at`` is gateway/commit time and is NEVER incarnation proof. A
    same-id settled row blocks (returns True, "same incarnation") when its
    canonical accounting Submit equals the candidate; a different canonical
    instant permits recycle (returns False); and every absent / malformed /
    gateway-only / exact-comment-without-Submit / legacy-missing provenance
    blocks fail-closed (returns True) even when the legacy ``submitted_at``
    differs. Used identically by the reconcile-inventory anchor surface and the
    flat-replay surface so the two can never drift.
    """

    canonical_submit = normalize_slurm_accounting_submitted_at(
        canonical.get(SLURM_ACCOUNTING_SUBMITTED_AT_FIELD)
    )
    if canonical_submit is None:
        # Missing/malformed canonical accounting Submit, or the row predates
        # the additive fields (or was bound gateway/exact-comment without
        # canonical evidence): uncertainty is fail-closed, never recycle.
        return True
    binding_source = canonical.get(SLURM_BINDING_SOURCE_FIELD)
    if binding_source != "slurm_name_window_unique":
        # Canonical Submit only ever rides the name-window lane (enforced by
        # typed validation), but a hand-forged/mixed row must still fail
        # closed rather than be read as a different incarnation.
        return True
    return _format_utc(_strict_utc_datetime(canonical_submit)) == _format_utc(candidate_submit)


def _job_needs_restart_reconcile(job: Mapping[str, Any]) -> bool:
    status = str(job.get("status") or "")
    if accepted_submit_contract_is_current(job) and accepted_submit_row_kind(job) == "master":
        if status in TERMINAL_PIPELINE_STATUSES:
            if (
                job.get("submit_outcome") != "accepted"
                or not _file_journal_real_slurm_job_id(job.get("slurm_job_id"))
            ):
                return False
            members = _bounded_cohort_members(job.get("cohort_members"))
            projections = _bounded_candidate_projections(job.get("candidate_projections"))
            member_ids = {
                int(member["array_task_id"])
                for member in members
                if type(member.get("array_task_id")) is int
            }
            projected_ids = {
                int(projection["array_task_id"])
                for projection in projections
                if type(projection.get("array_task_id")) is int
                and projection.get("array_task_outcome") in {"succeeded", "failed"}
            }
            return not member_ids or projected_ids != member_ids
    if status == "reserved" and job.get("slurm_job_id") in (None, "") and job.get("idempotency_key") not in (None, ""):
        return True
    return status in {
        "pending",
        "queued",
        "submitted",
        "running",
        "cancellation_pending",
        "reconcile_unverified",
    } and _file_journal_real_slurm_job_id(job.get("slurm_job_id"))


def _job_blocks_rollback_quiescence(job: Mapping[str, Any]) -> bool:
    """Fail closed: only the explicit terminal allowlist can be quiescent."""

    status = str(job.get("status") or "")
    if status not in TERMINAL_PIPELINE_STATUSES:
        return True
    return _job_needs_restart_reconcile(job)


def _file_retry_job_int(job: Any, field: str) -> int:
    value = _file_retry_job_value(job, field)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _file_retry_job_record(job: Any) -> dict[str, Any]:
    fields = (
        "job_id",
        "run_id",
        "cycle_id",
        "source_id",
        "cycle_time",
        "job_type",
        "slurm_job_id",
        "array_task_id",
        "model_id",
        "status",
        "stage",
        "idempotency_key",
        "candidate_id",
        "submitted_at",
        "started_at",
        "finished_at",
        "exit_code",
        "retry_count",
        "manual_retry_marker",
        "previous_job_id",
        "error_code",
        "error_message",
        "log_uri",
        "created_at",
        "updated_at",
    )
    record = {name: _file_retry_job_value(job, name) for name in fields if _file_retry_job_value(job, name) is not None}
    for identity_field in ("job_id", "run_id", "cycle_id"):
        record[identity_field] = _safe_identity_text(str(record.get(identity_field) or ""), field=identity_field)
    record["job_type"] = str(record.get("job_type") or "")
    if record["job_type"] == "":
        raise FileOrchestrationJournalError("file_journal_missing_field", field="job_type")
    record["status"] = str(record.get("status") or "failed")
    record["retry_count"] = _file_retry_job_int(job, "retry_count")
    record["manual_retry_marker"] = bool(record.get("manual_retry_marker", False))
    return record


def _file_retry_namespace(row: Mapping[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(**dict(row))


def _file_auto_retry_job_can_be_reused(job: Mapping[str, Any]) -> bool:
    if job.get("manual_retry_marker") is True:
        return False
    if job.get("slurm_job_id") not in (None, "") or job.get("array_task_id") not in (None, ""):
        return False
    return str(job.get("status") or "") in {"pending", "submission_failed"}


def _next_file_manual_retry_job_id_for_run(repository: FileOrchestrationJournalRepository, run_id: str) -> str:
    prefix = f"{_safe_identity_text(run_id, field='run_id')}_retry_"
    used_retry_job_ids = {
        str(job.get("job_id"))
        for job in repository.query_pipeline_jobs_by_run(run_id)
        if job.get("manual_retry_marker") is True or str(job.get("job_id") or "").startswith(prefix)
    }
    deterministic_job_id = f"{prefix}active"
    if deterministic_job_id not in used_retry_job_ids:
        return deterministic_job_id
    sequence = 2
    while f"{prefix}{sequence}" in used_retry_job_ids:
        sequence += 1
    return f"{prefix}{sequence}"


def _file_retry_gateway_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        payload = dict(value)
    else:
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            payload = dict(model_dump(mode="json"))
        elif hasattr(value, "__dict__"):
            payload = dict(value.__dict__)
        else:
            raise TypeError(f"Expected mapping-like Slurm submission payload, got {type(value).__name__}")
    status = payload.get("status")
    status_value = getattr(status, "value", status)
    if status_value is not None:
        payload["status"] = status_value
    return payload


def _file_retry_gateway_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return _coerce_datetime(value, field="gateway_time")


def _manual_retry_submission_failure_details(
    error: Exception,
    *,
    error_code: str,
    error_message: str,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "trigger": "manual",
        "error_code": error_code,
        "error_message": error_message,
    }
    runtime_root_resolution = _runtime_root_resolution_from_error(error)
    if runtime_root_resolution is not None:
        details["runtime_root_resolution"] = _public_evidence(runtime_root_resolution)
    runtime_root_contract = _runtime_root_contract_from_error(error)
    if runtime_root_contract is not None:
        details["runtime_root_contract"] = _public_evidence(runtime_root_contract)
    return details


def _mapping_has_runtime_root_fields(value: Mapping[str, Any]) -> bool:
    return any(field in value for field in (*_RUNTIME_ROOT_FIELDS, *_DB_FREE_RUNTIME_FIELDS))


def _file_retry_job_truth_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _datetime_sort_key(
            row.get("updated_at")
            or row.get("finished_at")
            or row.get("submitted_at")
            or row.get("started_at")
            or row.get("created_at")
        ),
        _datetime_sort_key(row.get("created_at")),
        str(row.get("job_id") or ""),
    )


def _model_id_from_file_run_id(run_id: str | None) -> str | None:
    if not run_id:
        return None
    text = str(run_id)
    match = _FORECAST_RUN_ID_RE.fullmatch(text)
    if match is not None:
        return match.group(3)
    suffix_match = re.search(r"(?:^|_)(model(?:_[A-Za-z0-9.-]+)+)$", text)
    return suffix_match.group(1) if suffix_match is not None else None


def _source_cycle_from_file_run_id(run_id: str) -> tuple[str, datetime]:
    safe_run_id = _safe_identity_text(str(run_id), field="run_id")
    match = _FORECAST_RUN_ID_RE.fullmatch(safe_run_id) or _CYCLE_COHORT_RUN_ID_RE.fullmatch(safe_run_id)
    if match is None:
        raise FileOrchestrationJournalError("file_journal_invalid_identity", field="run_id")
    source_id = _normalize_file_source_id(match.group(1), field="run_id")
    try:
        return source_id, parse_cycle_time(match.group(2))
    except (TypeError, ValueError) as error:
        raise FileOrchestrationJournalError("file_journal_invalid_cycle_time", field="run_id") from error


def _cycle_scope_from_file_run_id(run_id: Any) -> tuple[str, datetime] | None:
    """Derive ``(source_id, cycle_time)`` from a run id, or ``None``.

    ``None`` means "not derivable with certainty" and its only legal
    consequence is falling back to the whole-tree scan (#1734 design D4). It is
    never a "row not found": a narrowed lookup that misses a row double-submits
    a cohort or mints a wrong retry, whereas the fallback is merely as slow as
    the prior behaviour.

    The forecast and cohort shapes are adjudicated by the existing
    ``_source_cycle_from_file_run_id``; the analysis shape (whose cycle is its
    start timestamp) is added here from the same canonical regex module. No
    fresh parser, and the source segment always goes through
    ``_normalize_file_source_id`` because run ids spell the source lower case
    while the on-disk directory carries the normalised casing.
    """

    try:
        return _source_cycle_from_file_run_id(str(run_id))
    except FileOrchestrationJournalError:
        pass
    try:
        safe_run_id = _safe_identity_text(str(run_id), field="run_id")
    except FileOrchestrationJournalError:
        return None
    match = _ANALYSIS_RUN_ID_RE.fullmatch(safe_run_id)
    if match is None:
        return None
    try:
        return _normalize_file_source_id(match.group(1), field="run_id"), parse_cycle_time(match.group(2))
    except (TypeError, ValueError, FileOrchestrationJournalError):
        return None


def _cycle_scope_from_job_id(job_id: Any) -> tuple[str, datetime] | None:
    """Derive ``(source_id, cycle_time)`` from a job id, or ``None`` (D1a).

    Both live job id shapes carry the pair: ``_CANDIDATE_JOB_ID_RE``
    (``job_fcst_{source}_{cycle}_...``), which
    ``_direct_pipeline_job_record`` already uses to route a by-cycle partition
    read, and ``_ACCEPTED_SUBMIT_MASTER_JOB_ID_RE``
    (``job_cycle_{source}_{cycle}_{stage}...``) for cohort rows. An earlier
    ruling held that ``job_id`` carried no cycle; Task 1(b)'s measurement
    forced that ruling's reversal and this function is the correction.

    ``None`` is the D4 fall-open signal, never "row not found". It is also the
    filename-level filter for the flat direct surface (D2a): a name that does
    not parse is read rather than skipped.
    """

    try:
        safe_job_id = _safe_identity_text(str(job_id), field="job_id")
    except FileOrchestrationJournalError:
        return None
    match = _CANDIDATE_JOB_ID_RE.fullmatch(safe_job_id) or _ACCEPTED_SUBMIT_MASTER_JOB_ID_RE.fullmatch(safe_job_id)
    if match is None:
        return None
    try:
        return _normalize_file_source_id(match.group(1), field="job_id"), parse_cycle_time(match.group(2))
    except (TypeError, ValueError, FileOrchestrationJournalError):
        return None


def _released_identity_blocked_row(job: Mapping[str, Any]) -> bool:
    """The ONE admission predicate for the released identity-blocked wedge.

    Shared by ``query_released_identity_blocked_jobs``'s candidate filter and
    by its authoritative confirmation, so "the same six clauses" is true by
    construction rather than by two hand-kept copies (#1810).
    """

    return (
        accepted_submit_contract_is_current(job)
        and accepted_submit_row_kind(job) == "master"
        and str(job.get("status") or "") == "reservation_lost"
        and job.get("reconciliation_decision") == IDENTITY_MISMATCH_RELEASED_DECISION
        and job.get("slurm_job_id") in (None, "")
        and job.get("matched_slurm_job_id") in (None, "")
    )


def _released_candidate_cycle_scope(job: Mapping[str, Any]) -> tuple[str, datetime] | None:
    """Scope a flat candidate the way its WRITER scoped it, or fall open.

    ``_write_pipeline_job_direct_unlocked`` derives ``(source_id, cycle_time)``
    from row CONTENT with these exact two functions, and
    ``_write_pipeline_job_unlocked`` appends the journal record under the same
    pair, so reading with them is the byte-exact inverse of the write. Deriving
    from the job_id string instead would be a second, weaker predicate: it
    judges a name where the writer judged content (#1810 design D3).

    Both helpers RAISE rather than returning ``None``; the ``None`` here is the
    #1734 D4 fall-open signal, never "row not found". It is unreachable from
    the flat surface today -- ``_validated_direct_pipeline_job_record`` requires
    a round-tripping ``cycle_id`` on every row it yields -- and is kept because
    an underivable scope must cost the old expensive read, not a silent drop.
    """

    try:
        return _source_id_from_job(job), _cycle_time_from_job(job)
    except FileOrchestrationJournalError:
        return None


def _cycle_scope_from_idempotency_key(idempotency_key: Any) -> tuple[str, datetime] | None:
    """Derive ``(source_id, cycle_time)`` from ``run_id:stage[:suffix]``.

    ``chain_runtime_utils._cycle_stage_idempotency_key`` builds the key by
    prefixing the run id, and run ids never contain ``:``. Any other key shape
    falls open to the whole-tree scan.
    """

    run_id, separator, _rest = str(idempotency_key).partition(":")
    if not separator or not run_id:
        return None
    return _cycle_scope_from_file_run_id(run_id)


def _cycle_scope_from_cycle_id(cycle_id: Any) -> tuple[str, datetime] | None:
    """Derive ``(source_id, cycle_time)`` from ``{source_lower}_{cycle}``.

    Round-trip mismatch, unknown source and unparseable cycle token all fall
    open rather than raising: the caller's blocked-row handler must stay
    reserved for genuine read faults.
    """

    try:
        return _source_cycle_from_cycle_id(str(cycle_id))
    except FileOrchestrationJournalError:
        return None


def _source_cycle_from_cycle_id(cycle_id: str) -> tuple[str, datetime]:
    source, separator, cycle_stamp = str(cycle_id).rpartition("_")
    if not separator:
        raise FileOrchestrationJournalError("file_journal_invalid_identity", field="cycle_id")
    source_id = _normalize_file_source_id(source, field="cycle_id")
    try:
        cycle_time = parse_cycle_time(cycle_stamp)
    except (TypeError, ValueError) as error:
        raise FileOrchestrationJournalError("file_journal_invalid_cycle_time", field="cycle_id") from error
    expected = _cycle_id_for_file_source(source_id, cycle_time)
    if cycle_id != expected:
        raise FileOrchestrationJournalError(
            "file_journal_cycle_id_mismatch",
            field="cycle_id",
            evidence={"expected": expected, "actual": cycle_id[:80]},
        )
    return source_id, cycle_time


def _accepted_submit_source_cycle_from_job_id(pipeline_job_id: str) -> tuple[str, datetime]:
    safe_job_id = _safe_identity_text(str(pipeline_job_id), field="job_id")
    match = _ACCEPTED_SUBMIT_MASTER_JOB_ID_RE.fullmatch(safe_job_id)
    if match is None:
        raise FileOrchestrationJournalError("file_journal_invalid_identity", field="job_id")
    source_id = _normalize_file_source_id(match.group(1), field="job_id")
    try:
        return source_id, parse_cycle_time(match.group(2))
    except (TypeError, ValueError) as error:
        raise FileOrchestrationJournalError("file_journal_invalid_cycle_time", field="job_id") from error


def _source_id_from_job(job: Mapping[str, Any]) -> str:
    source = _optional_source_id(job, "source_id")
    if source is not None:
        return source
    source, _cycle_time = _source_cycle_from_cycle_id(_required_safe_identity(job, "cycle_id"))
    return source


def _cycle_time_from_job(job: Mapping[str, Any]) -> datetime:
    if job.get("cycle_time") not in (None, ""):
        return _parse_cycle_time_field(job, "cycle_time")
    _source, cycle_time = _source_cycle_from_cycle_id(_required_safe_identity(job, "cycle_id"))
    return cycle_time


def _file_journal_blocked_candidate_state(
    error: FileOrchestrationJournalError,
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
    run_id: str,
    forcing_version_id: str,
    candidate_id: str,
    retry_limit: int | None,
    job_limit: int,
    event_limit: int,
) -> dict[str, Any]:
    cycle_id = _blocked_cycle_id(source_id, cycle_time)
    return _public_candidate_state(
        {
            "candidate_id": candidate_id,
            "run_id": run_id,
            "forcing_version_id": forcing_version_id,
            "retry_limit": retry_limit,
            "job_limit": job_limit,
            "event_limit": event_limit,
            "pipeline_status": "running",
            "stage": "file_journal_read",
            "file_journal": {
                "status": "blocked",
                "reason": error.reason,
                "field": error.field,
                "evidence": _public_evidence(error.evidence),
            },
            "pipeline_jobs": [
                {
                    "job_id": "file_journal_read_blocked",
                    "run_id": run_id,
                    "cycle_id": cycle_id,
                    "model_id": model_id,
                    "status": "running",
                    "stage": "file_journal_read",
                    "error_code": error.reason,
                }
            ],
        }
    )


def _blocked_stage_status(
    error: FileOrchestrationJournalError,
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str | None,
) -> dict[str, Any]:
    return _public_evidence(
        {
            "stage": "file_journal_read",
            "status": "running",
            "job_id": "file_journal_read_blocked",
            "cycle_id": _blocked_cycle_id(source_id, cycle_time),
            "model_id": model_id,
            "slurm_job_id": "unknown_after_attempt",
            "error_code": error.reason,
            "file_journal": {
                "status": "blocked",
                "reason": error.reason,
                "field": error.field,
                "evidence": _public_evidence(error.evidence),
            },
        }
    )


def _run_manifest_model_package_identity(hydro_run: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(hydro_run, Mapping):
        return None
    manifest_path = _object_store_uri_local_path(str(hydro_run.get("run_manifest_uri") or ""))
    if manifest_path is None:
        return None
    object_root = _object_store_root()
    if object_root is None:
        return None
    try:
        payload = json.loads(
            read_bytes_limited_no_follow(
                manifest_path,
                max_bytes=MAX_FILE_JOURNAL_JSON_BYTES,
                containment_root=object_root,
            ).decode("utf-8")
        )
    except (FileNotFoundError, OSError, SafeFilesystemError, json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    identity: dict[str, Any] = {"source": "run_manifest", "status": "loaded"}
    package_uri = _first_nested_text(
        payload,
        ("model", "model_package_uri"),
        ("identity", "model_package_uri"),
        ("model_package_uri",),
    )
    if package_uri is not None:
        identity["model_package_uri_sha256"] = _stable_sha256(package_uri)
    package_manifest_uri = _first_nested_text(
        payload,
        ("model", "model_package_manifest_uri"),
        ("identity", "model_package_manifest_uri"),
        ("model_package_manifest_uri",),
    )
    if package_manifest_uri is not None:
        identity["model_package_manifest_uri_sha256"] = _stable_sha256(package_manifest_uri)
    package_checksum = _first_nested_text(
        payload,
        ("model", "model_package_checksum"),
        ("identity", "model_package_checksum"),
        ("model", "package_checksum"),
        ("package_checksum",),
    )
    if package_checksum is not None:
        identity["model_package_checksum"] = package_checksum
        identity["model_package_checksum_sha256"] = _stable_sha256(package_checksum)
    if len(identity) == 2:
        return None
    return identity


def _object_store_uri_local_path(uri: str) -> Path | None:
    text = uri.strip()
    if not text:
        return None
    root = _object_store_root()
    if root is None:
        return None
    prefix = os.getenv("OBJECT_STORE_PREFIX", "").strip().rstrip("/")
    key: str | None
    if prefix and text.startswith(f"{prefix}/"):
        key = text[len(prefix) + 1 :]
    elif "://" not in text and not text.startswith("/"):
        key = text
    else:
        return None
    if not key:
        return None
    return root / key


def _object_store_root() -> Path | None:
    root = os.getenv("OBJECT_STORE_ROOT", "").strip()
    if not root:
        return None
    return Path(root).expanduser().resolve()


def _first_nested_text(payload: Mapping[str, Any], *paths: tuple[str, ...]) -> str | None:
    for path in paths:
        current: Any = payload
        for part in path:
            if not isinstance(current, Mapping):
                current = None
                break
            current = current.get(part)
        if current not in (None, ""):
            return str(current)
    return None


def _stable_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _public_scheduler_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return _public_evidence(row)


_PERSISTED_REDACTION_PLACEHOLDERS = frozenset({"[object-uri]", "[uri]"})


def _strip_redaction_placeholders(value: Any) -> Any:
    """Drop object-URI display placeholders from durable journal payloads.

    Public query results replace object URIs with placeholders such as
    ``[object-uri]``. A caller that round-trips those rows into a write must
    not launder the placeholders into durable state: store ``None`` (value
    withheld) instead, so decision paths never compare against placeholders.
    ``[local-path]``/``[redacted]`` are intentionally persisted for
    runtime-root and secret evidence and stay untouched, as does the
    deliberate pipeline-event public sanitization applied after this step.
    """

    if isinstance(value, Mapping):
        return {key: _strip_redaction_placeholders(nested) for key, nested in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_strip_redaction_placeholders(item) for item in value]
    if isinstance(value, str) and value in _PERSISTED_REDACTION_PLACEHOLDERS:
        return None
    return value


def _resolved_caller_evidence(value: Any, *, durable: Any = None) -> Any:
    """Resolve one caller-supplied evidence field against durable state (#1589 D3).

    THE ONE PLACE the "a display placeholder is a WITHHELD value" rule is
    spelled out.  Every write leg that lets a caller hand in evidence must send
    it through here BEFORE both its overwrite guard and its equality gate,
    because the write boundary (``_journal_record_for_write``) strips
    placeholders out of durable state: a leg that compares raw caller input
    against already-stripped durable state can never converge, and its guard
    reads a placeholder as "the caller supplied a real value" when the caller
    supplied nothing of the sort.

    Withheld means KEEP.  ``durable`` is the value the row already carries:

    * placeholder in  -> ``durable`` out.  Pass ``durable=`` at legs whose write
      is UNCONDITIONAL, where declining is not an option and ``None`` would
      destroy the value.  Omit it at legs guarded by ``is not None``: there,
      ``None`` makes the guard decline, and declining IS keeping.  Omitting it
      is also what keeps ``datetime``-typed parameters safe -- resolving one to
      the row's already-formatted string would then hit ``_format_utc``.
    * genuine ``None`` in -> ``None`` out.  A caller that really passed ``None``
      said "no value", and legs that persist that as a clear must keep doing so;
      only a placeholder was ever withheld.
    * anything else -> unchanged (non-string evidence included).

    The "every write leg" above is the RULE, not a claim that every leg obeys it
    today.  Two legs deliberately still write a caller error family unresolved:
    ``update_forecast_cycle_status`` builds a fresh row and never reads the
    persisted one, so it has nothing to resolve against;
    ``reject_pipeline_job_submit_attempt`` writes the rejection's own required
    error as the attempt's authoritative outcome and its idempotent branch never
    compares the error fields, so neither displacement nor append growth bites.
    The hydro-run and retry-permanent-failure legs resolve against the durable
    base/current row (their placeholders are withheld values), so they are NOT
    exempt.
    """

    stripped = _strip_redaction_placeholders(value)
    if stripped is None and value is not None:
        return durable
    return stripped


def _public_candidate_state(state: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(state)
    raw_manifest = payload.get("nfs_raw_manifest")
    if isinstance(raw_manifest, Mapping):
        payload["nfs_raw_manifest"] = _public_raw_manifest_evidence(raw_manifest)
    return _public_evidence(payload)


def _public_evidence(value: Any) -> Any:
    return _sanitize_public_evidence(value)


def _sanitize_public_evidence(value: Any) -> Any:
    if isinstance(value, datetime):
        return _format_utc(value)
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_public_field(str(key), nested)
            for key, nested in value.items()
            if not str(key).startswith("_file_journal_")
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_sanitize_public_evidence(item) for item in value]
    return _sanitize_public_scalar(value)


def _sanitize_public_field(key: str, value: Any) -> Any:
    lowered = key.lower()
    if is_sensitive_key(key):
        return "[redacted]" if value not in (None, "") else value
    if lowered == "message" or lowered.endswith("_message"):
        return _public_message(value)
    if lowered.endswith("_path") or lowered.endswith("_root") or lowered in {"path", "root"}:
        return "[local-path]" if value not in (None, "") else value
    if lowered.endswith("_uri") or lowered in {"uri", "object_uri", "manifest_uri"}:
        return _sanitize_file_provider_evidence_scalar(key, value)
    return _sanitize_public_evidence(value)


def _sanitize_public_scalar(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    sanitized = _sanitize_public_path_or_uri_scalar(value)
    if sanitized != value:
        return sanitized
    return _sanitize_public_text(value)


def _sanitize_public_path_or_uri_scalar(value: str) -> str:
    text = value.strip()
    if not text or any(char.isspace() for char in text):
        return value
    if (
        text.startswith("/")
        or text.startswith("~")
        or "://" in text
        or text.startswith("s3:")
        or text.startswith("published:")
    ):
        return _sanitize_file_provider_evidence_scalar("uri", value)
    return value


def _public_message(value: Any) -> Any:
    if value in (None, ""):
        return value
    if not isinstance(value, str):
        return _sanitize_public_evidence(value)
    return _sanitize_public_text(value)


def _sanitize_public_text(value: str) -> str:
    redacted = _safe_error_message(value)
    return _sanitize_public_text_tokens(redacted)


def _sanitize_public_text_tokens(value: str) -> str:
    rendered: list[str] = []
    token = ""
    for char in value:
        if char.isspace():
            if token:
                rendered.append(_sanitize_public_text_token(token))
                token = ""
            rendered.append(char)
        else:
            token += char
    if token:
        rendered.append(_sanitize_public_text_token(token))
    return "".join(rendered)


def _sanitize_public_text_token(value: str) -> str:
    prefix_length = 0
    suffix_length = 0
    while prefix_length < len(value) and value[prefix_length] in "'\"([{<":
        prefix_length += 1
    while suffix_length < len(value) - prefix_length and value[len(value) - suffix_length - 1] in "'\".,;:!?)]}>":
        suffix_length += 1
    prefix = value[:prefix_length]
    suffix = value[len(value) - suffix_length :] if suffix_length else ""
    core = value[prefix_length : len(value) - suffix_length if suffix_length else len(value)]
    if not core:
        return value
    sanitized = _sanitize_public_path_or_uri_scalar(core)
    if sanitized == core:
        for separator in ("=", ":"):
            key, found, nested = core.partition(separator)
            if not found or not key or not nested:
                continue
            sanitized_nested = _sanitize_public_path_or_uri_scalar(nested)
            if sanitized_nested != nested:
                sanitized = f"{key}{found}{sanitized_nested}"
                break
    return f"{prefix}{sanitized}{suffix}" if sanitized != core else value


def _blocked_query_job(
    error: FileOrchestrationJournalError,
    *,
    job_id: str = "file_journal_read_blocked",
    idempotency_key: str | None = None,
    cycle_id: str | None = None,
    run_id: str | None = None,
    slurm_job_id: str | None = None,
) -> dict[str, Any]:
    return _public_evidence(
        {
            "job_id": job_id or "file_journal_read_blocked",
            "idempotency_key": idempotency_key,
            "cycle_id": cycle_id,
            "run_id": run_id,
            "slurm_job_id": slurm_job_id or "unknown_after_attempt",
            "status": "running",
            "stage": "file_journal_read",
            "error_code": error.reason,
            "file_journal": {
                "status": "blocked",
                "reason": error.reason,
                "field": error.field,
                "evidence": _evidence_safe(error.evidence),
            },
        }
    )


def _job_is_active(job: Mapping[str, Any]) -> bool:
    status = str(job.get("status") or "")
    if _job_is_unsubmitted_retry_placeholder(job, status=status):
        return False
    return status not in ("", *TERMINAL_PIPELINE_STATUSES)


def _candidate_scoped_forecast_cycle(forecast_cycle: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(forecast_cycle, Mapping):
        return None
    status = str(forecast_cycle.get("status") or "")
    if status in _TERMINAL_FORECAST_CYCLE_SUCCESS_STATUSES:
        return None
    return forecast_cycle


def _job_is_unsubmitted_retry_placeholder(job: Mapping[str, Any], *, status: str | None = None) -> bool:
    job_status = str(job.get("status") or "") if status is None else status
    if job_status not in {"pending", "queued", "submitted"}:
        return False
    if job.get("slurm_job_id") not in (None, "") or job.get("array_task_id") not in (None, ""):
        return False
    if job.get("submitted_at") not in (None, ""):
        return False
    try:
        retry_count = int(job.get("retry_count") or 0)
    except (TypeError, ValueError):
        return False
    return retry_count > 0 and job.get("candidate_id") in (None, "") and job.get("idempotency_key") in (None, "")


def _job_is_terminal_success(job: Mapping[str, Any]) -> bool:
    return str(job.get("status") or "") in {"succeeded", "complete", "published"}


def _job_is_breaker_terminal_success(job: Mapping[str, Any], *, model_id: str) -> bool:
    """§8.7 breaker success gate: aggregate success or a proven per-model success.

    Only the quarantine-breaker occurrence accessor uses this predicate; the
    shared ``_job_is_terminal_success`` and its other call sites stay unchanged.
    An aggregate terminal-success master always qualifies (legacy shapes carry
    no projections at all).  A ``partially_failed`` cohort master counts for
    the target ``model_id`` only when its bounded ``candidate_projections``
    (at most 256 entries) contains EXACTLY ONE projection naming that model
    and that projection carries ``array_task_outcome == "succeeded"``.  A
    failed, missing, malformed, truncated, or duplicate target projection
    never counts — a succeeded-then-failed or succeeded-then-succeeded
    duplicate is an ambiguous projection and undercounts to zero like any
    other non-proof: the breaker fails toward liveness, so a target that is
    not visible after the 256-entry bound undercounts to zero and the
    quarantine retry stays engaged.
    """

    if _job_is_terminal_success(job):
        return True
    if str(job.get("status") or "") != "partially_failed":
        return False
    projections = job.get("candidate_projections")
    if not isinstance(projections, Sequence) or isinstance(projections, str | bytes | bytearray):
        return False
    bounded_projections = projections[:MAX_FORECAST_COHORT_MEMBERS]
    target_outcomes: list[str] = []
    for projection in bounded_projections:
        if not isinstance(projection, Mapping):
            continue
        if str(projection.get("model_id") or "") != model_id:
            continue
        target_outcomes.append(str(projection.get("array_task_outcome") or ""))
    if len(target_outcomes) != 1:
        # Zero targets (no projection names this model) and multiple targets
        # (same/conflicting outcomes/task ids) are both ambiguous: never count.
        return False
    return target_outcomes[0] == "succeeded"


_INIT_STATE_ALIAS_KEYS = ("init_state_id", "initial_state_id", "state_id")

#: Canonical ``init_state_*`` key for each matcher identity field whose bare
#: aliases must not feed the old string wrapper.  ``state_id`` is deliberately
#: absent: its canonical key is ``init_state_id`` and fabricating it from a
#: bare ``state_id`` alias would leak a bare-``state_id`` row into the legacy
#: accessor, which the authority contract forbids.  The id is instead carried
#: verbatim under its recorded alias (``_INIT_STATE_ALIAS_KEYS``).
_INIT_STATE_CANONICAL_KEYS = {
    "checksum": "init_state_checksum",
    "uri": "init_state_uri",
    "valid_time": "init_state_valid_time",
}


def _init_state_identity_from_hydro(hydro_run: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project a completed hydro row's recorded identity onto one full mapping.

    Every optional identity field the shared matcher consumes
    (``INIT_STATE_IDENTITY_FIELDS`` through ``init_state_field``) is resolved
    against the row and emitted under its canonical ``init_state_*`` key, so
    checksum/URI/valid-time conflicts are not lost when the verdict compares a
    completed hydro identity.  The id aliases (``init_state_id`` /
    ``initial_state_id`` / bare ``state_id``) are kept verbatim as recorded;
    the canonical ``init_state_id`` is never fabricated from a bare
    ``state_id``, so the string wrapper keeps returning ``None`` for a
    bare-``state_id``-only row while the full accessor still exposes it.
    """

    identity: dict[str, Any] = {}
    for key in _INIT_STATE_ALIAS_KEYS:
        value = hydro_run.get(key)
        if value not in (None, ""):
            identity[key] = value
    for identity_field in INIT_STATE_IDENTITY_FIELDS:
        if identity_field == "state_id":
            continue
        value = init_state_field(hydro_run, identity_field)
        if value not in (None, ""):
            identity[_INIT_STATE_CANONICAL_KEYS[identity_field]] = value
    return identity or None


def _candidate_row_self_bound_identity(
    job: Mapping[str, Any], *, model_id: str
) -> dict[str, Any] | None:
    """Return the one normalized identity entry bound to this candidate row.

    ``accepted_submit_candidate_immutable_evidence`` already normalized and
    validated the row's ``init_state_identities`` for the current contract; the
    entry must name exactly THIS model.  A master-shaped or malformed row never
    reaches this helper (the caller restricts to contract-current candidate
    rows first), and an empty or multi-entry map yields ``None``.
    """

    try:
        evidence = accepted_submit_candidate_immutable_evidence(job)
    except AcceptedSubmitEvidenceError:
        return None
    identities = evidence.get(INIT_STATE_IDENTITY_FIELD)
    if not isinstance(identities, Sequence) or isinstance(identities, str | bytes):
        return None
    if len(identities) != 1:
        return None
    entry = identities[0]
    if not isinstance(entry, Mapping):
        return None
    if str(entry.get("model_id") or "") != model_id:
        return None
    return {key: value for key, value in entry.items() if value not in (None, "")}


def _master_row_records_init_state_id(
    job: Mapping[str, Any], *, model_id: str, init_state_id: str
) -> bool:
    """Whether a cohort master's identity map names this model with this token."""

    identities = job.get(INIT_STATE_IDENTITY_FIELD)
    if not isinstance(identities, Sequence) or isinstance(identities, str | bytes):
        return False
    for entry in identities:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("model_id") or "") != model_id:
            continue
        if str(entry.get("init_state_id") or "").strip() == init_state_id:
            return True
    return False


def _job_is_current_terminal_completion(job: Mapping[str, Any]) -> bool:
    stage = chain_repository_state._normalized_record_stage(job)
    if chain_repository_state._compute_state_save_qc_terminal_enabled():
        return stage == "state_save_qc"
    return stage in {"parse", "state_save_qc", "publish"}


def _current_terminal_jobs(jobs: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [job for job in jobs if chain_repository_state._record_allowed_for_compute_state_terminal(job)]


# Deliberately WITHOUT ``init_state_identities`` (#1183): the master row's map
# holds one entry per cohort member, and this projection is replicated into
# every candidate state of the cycle — carrying it here would multiply the
# whole cohort's identity map by 18 for no reader.  Each per-model terminal row
# carries its own single entry instead.
_CYCLE_SCOPE_JOB_PROJECTION_KEYS = (
    "job_id",
    "run_id",
    "cycle_id",
    "cycle_time",
    "source_id",
    "model_id",
    "job_type",
    "stage",
    "status",
    "slurm_job_id",
    "array_task_id",
    "error_code",
    "restart_stage",
    "restart_from_stage",
    "retry_count",
    "created_at",
    "updated_at",
    "submitted_at",
    "started_at",
    "finished_at",
)


def _is_model_less_cycle_scope_job(
    job: Mapping[str, Any], *, source_id: str, cycle_time: datetime
) -> bool:
    if job.get("model_id") not in (None, ""):
        return False
    source_id = _normalize_file_source_id(source_id, field="source_id")
    cycle_run_id = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
    run_id = str(job.get("run_id") or "")
    return run_id == cycle_run_id or run_id.startswith(f"{cycle_run_id}_")


def _is_foreign_model_cycle_scope_job(
    job: Mapping[str, Any], *, source_id: str, cycle_time: datetime, model_id: str
) -> bool:
    """A row of ANOTHER model that merely carries the cycle run id.

    Complement of ``_is_model_less_cycle_scope_job`` on the same run-id grammar and
    the same null semantics (``in (None, "")``): a job whose ``model_id`` is set and
    names a different model is that model's row, not a cohort row shared by the
    cycle.  Only the exact ``cycle_<source>_<stamp>`` run id is tested — a foreign
    named row carrying a suffixed cohort run id never reaches a candidate's rows in
    the first place (``_job_matches_candidate`` restricts the suffix arm to
    model-less rows), and a foreign row carrying the candidate's OWN ``fcst_...``
    run id stays the candidate's row on both read paths.
    """

    job_model_id = job.get("model_id")
    if job_model_id in (None, "") or str(job_model_id) == model_id:
        return False
    source_id = _normalize_file_source_id(source_id, field="source_id")
    cycle_run_id = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
    return str(job.get("run_id") or "") == cycle_run_id


_CYCLE_SCOPE_COMPLETION_STAGES = frozenset({"parse", "state_save_qc", "publish"})


def _compact_cycle_scope_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Drop bulky details from completion-stage cohort events.

    Cohort copyback/publish events carry multi-KB payloads; replicated into
    every candidate state they multiply pass evidence past the size guard.
    Submission-stage events (download/forcing/convert) keep their details -
    they hold per-candidate evidence such as runtime-root contracts.
    """

    return {key: value for key, value in event.items() if key != "details"}


def _compact_cycle_scope_job(job: Mapping[str, Any]) -> dict[str, Any]:
    """Project a cycle-scope cohort job onto its per-candidate identity fields.

    Cohort jobs are attributed to every candidate in the cycle; copying their
    full payload (cohort_members, candidate_projections, ...) into each of the
    18 candidate states multiplies pass evidence past the size guard. Decision
    logic only reads identity, stage, status, and timestamps.
    """

    return {key: job[key] for key in _CYCLE_SCOPE_JOB_PROJECTION_KEYS if key in job}


def _job_matches_candidate(job: Mapping[str, Any], *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
    source_id = _normalize_file_source_id(source_id, field="source_id")
    cycle_id = _cycle_id_for_file_source(source_id, cycle_time)
    cycle_stamp = format_cycle_time(cycle_time)
    cycle_run_id = f"cycle_{source_id.lower()}_{cycle_stamp}"
    candidate_run_id = f"fcst_{source_id.lower()}_{cycle_stamp}_{model_id}"
    if str(job.get("cycle_id") or "") != cycle_id:
        return False
    run_id = str(job.get("run_id") or "")
    return (
        run_id in {candidate_run_id, cycle_run_id}
        or str(job.get("model_id") or "") == model_id
        # Model-less cycle-scope cohort jobs (e.g. state_save_qc cohorts with
        # run_id "cycle_<source>_<stamp>_<suffix>") belong to every candidate
        # in the cycle; the DB query path already includes them via cycle_id.
        or (
            job.get("model_id") in (None, "")
            and (run_id == cycle_run_id or run_id.startswith(f"{cycle_run_id}_"))
        )
    )


def _job_matches_source_cycle(job: Mapping[str, Any], *, source_id: str, cycle_time: datetime) -> bool:
    source_id = _normalize_file_source_id(source_id, field="source_id")
    cycle_id = _cycle_id_for_file_source(source_id, cycle_time)
    if str(job.get("cycle_id") or "") != cycle_id:
        return False
    cycle_stamp = format_cycle_time(cycle_time)
    run_id = str(job.get("run_id") or "")
    return run_id == f"cycle_{source_id.lower()}_{cycle_stamp}" or run_id.startswith(
        f"fcst_{source_id.lower()}_{cycle_stamp}_"
    )


def _event_matches_candidate_rows(
    event: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    pipeline_jobs: Mapping[str, Mapping[str, Any]],
    forecast_cycle: Mapping[str, Any] | None,
    cycle_terminated: bool = False,
) -> bool:
    entity_type = str(event.get("entity_type") or "pipeline_job")
    entity_id = str(event.get("entity_id") or "")
    if entity_type == "pipeline_job":
        return entity_id in pipeline_jobs
    if entity_type == "forecast_cycle":
        expected_cycle_id = _cycle_id_for_file_source(source_id, cycle_time)
        if entity_id != expected_cycle_id:
            return False
        if forecast_cycle is not None:
            return str(forecast_cycle.get("cycle_id") or "") == expected_cycle_id
        # A terminally-succeeded cycle keeps its events suppressed so stale
        # cohort events cannot resurrect candidate work; a cycle with no row
        # at all still surfaces its own events (read contract).
        return not cycle_terminated
    return False


def _pipeline_event_entity_type(value: Any) -> str:
    entity_type = _scalar_text(
        "pipeline_job" if value in (None, "") else value,
        field="entity_type",
        invalid_reason="file_journal_invalid_identity",
    )
    if entity_type not in _SUPPORTED_PIPELINE_EVENT_ENTITY_TYPES:
        raise FileOrchestrationJournalError(
            "file_journal_event_entity_type_mismatch",
            field="entity_type",
            evidence={
                "expected": "|".join(sorted(_SUPPORTED_PIPELINE_EVENT_ENTITY_TYPES)),
                "actual": entity_type[:80],
            },
        )
    return entity_type


def _row_matches_candidate(
    row: Mapping[str, Any] | None,
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
) -> bool:
    if not isinstance(row, Mapping):
        return False
    source_id = _normalize_file_source_id(source_id, field="source_id")
    actual_source = _optional_source_id(row, "source_id")
    if actual_source is not None and actual_source != source_id:
        return False
    if row.get("cycle_time") not in (None, ""):
        try:
            parsed_cycle_time = parse_cycle_time(str(row["cycle_time"]))
        except (TypeError, ValueError) as error:
            raise FileOrchestrationJournalError("file_journal_invalid_cycle_time", field="cycle_time") from error
        if _format_utc(parsed_cycle_time) != _format_utc(cycle_time):
            return False
    return str(row.get("model_id") or "") in ("", model_id)


def _payload_or_record_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    if "payload" in record:
        payload = record.get("payload")
        if isinstance(payload, Mapping):
            return dict(payload)
        raise FileOrchestrationJournalError("file_journal_expected_object", field="payload")
    return dict(record)


def _record_type(record: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    value = record.get("record_type")
    if value in (None, ""):
        value = payload.get("record_type")
    if value in (None, ""):
        return ""
    return _scalar_text(value, field="record_type", invalid_reason="file_journal_invalid_field")


def _record_list(payload: Mapping[str, Any], *keys: str, single_key: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    single = payload.get(single_key)
    if isinstance(single, Mapping):
        records.append(dict(single))
    elif single not in (None, ""):
        raise FileOrchestrationJournalError("file_journal_expected_object", field=single_key)
    for key in keys:
        value = payload.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            for index, item in enumerate(value):
                if not isinstance(item, Mapping):
                    raise FileOrchestrationJournalError("file_journal_expected_object", field=f"{key}[{index}]")
                records.append(dict(item))
            continue
        raise FileOrchestrationJournalError("file_journal_expected_list", field=key)
    return records


def _first_mapping(payload: Mapping[str, Any], *keys: str) -> dict[str, Any] | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, Mapping):
            return dict(value)
        if value not in (None, ""):
            raise FileOrchestrationJournalError("file_journal_expected_object", field=key)
    return None


def _latest_mapping(current: dict[str, Any] | None, incoming: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if incoming is None:
        return current
    if current is None:
        return dict(incoming)
    current_replay_key = _replay_order_key(current)
    incoming_replay_key = _replay_order_key(incoming)
    if current_replay_key is not None or incoming_replay_key is not None:
        if current_replay_key is None:
            return dict(incoming)
        if incoming_replay_key is None:
            return current
        return dict(incoming) if incoming_replay_key >= current_replay_key else current
    current_time = _datetime_sort_key(current.get("updated_at") or current.get("created_at"))
    incoming_time = _datetime_sort_key(incoming.get("updated_at") or incoming.get("created_at"))
    return dict(incoming) if incoming_time >= current_time else current


def _upsert_by_key(target: dict[str, dict[str, Any]], row: Mapping[str, Any], *, key: str) -> None:
    row_key = _required_safe_identity(row, key)
    existing = target.get(row_key)
    target[row_key] = _latest_mapping(existing, row) or dict(row)


def _insert_missing_by_key(target: dict[str, dict[str, Any]], row: Mapping[str, Any], *, key: str) -> None:
    row_key = _required_safe_identity(row, key)
    target.setdefault(row_key, dict(row))


def _with_replay_order(payload: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(payload)
    sequence = _optional_replay_sequence(record)
    if sequence is not None:
        row[_REPLAY_SEQUENCE_FIELD] = sequence
    line_order = record.get(_REPLAY_ORDER_FIELD)
    if isinstance(line_order, int):
        row[_REPLAY_ORDER_FIELD] = line_order
    return row


def _with_latest_replay_order(row: Mapping[str, Any], latest_replay_sequence: int | None) -> dict[str, Any]:
    if latest_replay_sequence is None:
        return dict(row)
    payload = dict(row)
    payload[_REPLAY_SEQUENCE_FIELD] = latest_replay_sequence
    payload[_REPLAY_ORDER_FIELD] = _LATEST_REPLAY_ORDER_SENTINEL
    return payload


def _latest_replay_sequence(payload: Mapping[str, Any]) -> int | None:
    replay = payload.get("replay")
    if not isinstance(replay, Mapping):
        return None
    value = replay.get("latest_sequence")
    if value in (None, ""):
        return None
    text = _scalar_text(
        value,
        field="replay.latest_sequence",
        invalid_reason="file_journal_invalid_field",
    )
    try:
        return int(text)
    except ValueError as error:
        raise FileOrchestrationJournalError("file_journal_invalid_field", field="replay.latest_sequence") from error


def _optional_replay_sequence(record: Mapping[str, Any]) -> int | None:
    value = record.get("sequence")
    if value in (None, ""):
        return None
    text = _scalar_text(value, field="sequence", invalid_reason="file_journal_invalid_field")
    try:
        return int(text)
    except ValueError as error:
        raise FileOrchestrationJournalError("file_journal_invalid_field", field="sequence") from error


def _optional_positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _replay_order_key(row: Mapping[str, Any]) -> tuple[int, int] | None:
    sequence = row.get(_REPLAY_SEQUENCE_FIELD)
    line_order = row.get(_REPLAY_ORDER_FIELD)
    if not isinstance(sequence, int) and not isinstance(line_order, int):
        return None
    sequence_value = sequence if isinstance(sequence, int) else -1
    line_order_value = line_order if isinstance(line_order, int) else -1
    return sequence_value, line_order_value


def _dedupe_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    keyed: dict[str, dict[str, Any]] = {}
    unkeyed: list[dict[str, Any]] = []
    for event in events:
        key = event.get("event_id")
        if key in (None, ""):
            unkeyed.append(dict(event))
            continue
        keyed[str(key)] = _latest_mapping(keyed.get(str(key)), event) or dict(event)
    return [*keyed.values(), *unkeyed]


def _db_compatible_pipeline_job_order_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    submitted_at = row.get("submitted_at")
    submitted_missing = submitted_at in (None, "")
    submitted_key = datetime.max.replace(tzinfo=UTC) if submitted_missing else _datetime_sort_key(submitted_at)
    return (
        submitted_missing,
        submitted_key,
        _datetime_sort_key(row.get("created_at")),
        str(row.get("job_id") or ""),
        str(row.get("run_id") or ""),
        str(row.get("slurm_job_id") or ""),
    )


def _db_compatible_stage_status_order_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    stage = str(row.get("stage") or "")
    return (
        _STAGE_STATUS_ORDER.get(stage, _UNKNOWN_STAGE_STATUS_ORDER),
        stage,
        str(row.get("source_id") or ""),
        str(row.get("cycle_id") or ""),
        str(row.get("model_id") or ""),
        str(row.get("job_id") or ""),
        str(row.get("run_id") or ""),
    )


def _decode_mapping(content: bytes, *, field: str, max_nodes: int, max_depth: int) -> dict[str, Any]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise FileOrchestrationJournalError(
            "file_journal_malformed_json",
            field=field,
            evidence={"error_type": type(error).__name__},
        ) from error
    if not isinstance(payload, Mapping):
        raise FileOrchestrationJournalError("file_journal_expected_object", field=field)
    _validate_json_complexity(payload, field=field, max_nodes=max_nodes, max_depth=max_depth)
    return dict(payload)


def _stat_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        file_stat = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return (file_stat.st_mtime_ns, file_stat.st_size, file_stat.st_ino)


def _decode_mapping_prevalidated(content: bytes, *, field: str) -> dict[str, Any]:
    """Decode bytes whose complexity validation already passed once.

    The complexity limits are a pure function of the bytes, so the graph
    walk is skipped for byte-identical re-reads; decoding still returns
    fresh objects on every call.
    """
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise FileOrchestrationJournalError(
            "file_journal_malformed_json",
            field=field,
            evidence={"error_type": type(error).__name__},
        ) from error
    if not isinstance(payload, Mapping):
        raise FileOrchestrationJournalError("file_journal_expected_object", field=field)
    return dict(payload)


def _validate_json_complexity(value: Any, *, field: str, max_nodes: int, max_depth: int) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    visited = 0
    while stack:
        item, depth = stack.pop()
        visited += 1
        if visited > max_nodes:
            raise FileOrchestrationJournalError(
                "file_journal_json_node_limit_exceeded",
                field=field,
                evidence={"max_nodes": max_nodes},
            )
        if depth > max_depth:
            raise FileOrchestrationJournalError(
                "file_journal_json_depth_exceeded",
                field=field,
                evidence={"max_depth": max_depth},
            )
        if isinstance(item, Mapping):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray):
            stack.extend((child, depth + 1) for child in item)


def _require_schema(payload: Mapping[str, Any], expected: str) -> None:
    if payload.get("schema_version") != expected:
        raise FileOrchestrationJournalError(
            "file_journal_schema_mismatch",
            field="schema_version",
            evidence={"expected": expected, "actual": str(payload.get("schema_version") or "")[:80]},
        )


def _normalize_file_source_id(value: Any, *, field: str) -> str:
    if value in (None, ""):
        raise FileOrchestrationJournalError("file_journal_missing_identity", field=field)
    text = _scalar_text(value, field=field, invalid_reason="file_journal_invalid_identity")
    text = _safe_identity_text(text, field=field)
    try:
        return normalize_source_id(text)
    except ValueError as error:
        raise FileOrchestrationJournalError(
            "file_journal_invalid_identity",
            field=field,
            evidence={"actual": text[:80]},
        ) from error


def _cycle_source_discovery_from_segment(source_segment: str) -> _CycleSourceDiscovery:
    source_segment = _safe_segment(source_segment)
    return _CycleSourceDiscovery(
        source_id=_normalize_file_source_id(source_segment, field="source_id"),
        source_segments=(source_segment,),
    )


def _merge_cycle_source_discovery(
    sources: dict[str, _CycleSourceDiscovery],
    source: _CycleSourceDiscovery,
    *,
    root: Path | None = None,
) -> None:
    """Merge one surface's discovery into the per-source segment list.

    #1761 D3: when ``root`` is given, a candidate segment is dropped only when
    ``_names_same_directory`` proves by ``(st_dev, st_ino)`` that it names a
    directory already kept — the same identity discipline the primary alias
    branch of ``_cycle_read_source_segments`` uses.  On a case-insensitive
    volume the mixed pair this merge used to produce (``latest/IFS`` +
    ``journal/ifs``) collapses to one segment, so every record is read once; on
    a case-sensitive volume no surface can prove identity and both real
    directories stay.  A symlinked alias keeps its own inode under the no-follow
    stat and therefore stays too, where the read path fails it closed.  With
    ``root is None`` the historical string dedup is kept unchanged.
    """

    existing = sources.get(source.source_id)
    if existing is None:
        sources[source.source_id] = source
        return
    source_segments = list(existing.source_segments)
    # The identity probe costs one no-follow stat per surface per kept segment,
    # and this merge runs once per matching file under ``latest/`` and once per
    # matching segment under ``journal/``/``pipeline-events/``.  So it is
    # deferred until a candidate actually survives the cheap string check: the
    # common case, where every surface spells the source the same way, pays
    # nothing beyond the string compare it already paid.
    kept_identities: list[dict[str, tuple[int, int]]] | None = None
    for source_segment in source.source_segments:
        if source_segment in source_segments:
            continue
        if root is not None:
            if kept_identities is None:
                kept_identities = [
                    _source_segment_directory_identities(root, segment) for segment in source_segments
                ]
            if any(_names_same_directory(root, source_segment, identities) for identities in kept_identities):
                continue
            kept_identities.append(_source_segment_directory_identities(root, source_segment))
        source_segments.append(source_segment)
    sources[source.source_id] = _CycleSourceDiscovery(
        source_id=existing.source_id,
        source_segments=tuple(source_segments),
    )


def _cycle_read_source_segment(*, source_id: str, source_segment_override: str | None) -> str:
    if source_segment_override is None:
        return _safe_segment(source_id)
    source_segment = _safe_segment(source_segment_override)
    segment_source_id = _normalize_file_source_id(source_segment, field="source_id")
    if segment_source_id != source_id:
        raise FileOrchestrationJournalError(
            "file_journal_source_mismatch",
            field="source_id",
            evidence={"expected": source_id, "actual": segment_source_id[:80]},
        )
    return source_segment


_SOURCE_SEGMENT_SURFACES: tuple[str, ...] = (
    "latest",
    "journal",
    "pipeline-events",
    "pipeline-jobs/by-cycle",
)


def _source_segment_directory_identities(root: Path, segment: str) -> dict[str, tuple[int, int]]:
    """`(st_dev, st_ino)` of every per-source directory this segment names."""

    identities: dict[str, tuple[int, int]] = {}
    for surface in _SOURCE_SEGMENT_SURFACES:
        try:
            entry_stat = os.stat(root.joinpath(*surface.split("/"), segment), follow_symlinks=False)
        except (OSError, ValueError):
            continue
        if stat.S_ISDIR(entry_stat.st_mode):
            identities[surface] = (entry_stat.st_dev, entry_stat.st_ino)
    return identities


def _names_same_directory(
    root: Path,
    segment: str,
    other_identities: Mapping[str, tuple[int, int]],
) -> bool:
    """True only when a surface proves both segments name ONE directory.

    On a case-insensitive filesystem (macOS) ``gfs`` and ``GFS`` resolve to
    the same inode, so reading both would enumerate every record twice and
    silently inflate record/file budgets.  On a case-sensitive filesystem the
    two directories are genuinely distinct (or one is absent) and no surface
    can prove identity, so both segments are kept and both are read.
    Symlinked aliases keep a distinct inode under ``follow_symlinks=False``
    and therefore stay in the list, where the read path's containment
    discipline still fails them closed.
    """

    if not other_identities:
        return False
    identities = _source_segment_directory_identities(root, segment)
    return any(identities.get(surface) == identity for surface, identity in other_identities.items())


def _cycle_read_source_segments(
    *,
    source_id: str,
    source_segment_override: str | None,
    source_segment_overrides: tuple[str, ...] | None = None,
    root: Path | None = None,
) -> tuple[str, ...]:
    if source_segment_overrides is not None:
        segments: list[str] = []
        # #1761 D3: mirror the primary branch's identity dedup.  Every override
        # still goes through `_cycle_read_source_segment` first, so the
        # per-item source-mismatch validation (`file_journal_source_mismatch`)
        # is unchanged, and an override list that is empty after collapsing
        # still fails closed with `file_journal_missing_identity`.
        override_identities: list[dict[str, tuple[int, int]]] = []
        for source_segment_override_item in source_segment_overrides:
            segment = _cycle_read_source_segment(
                source_id=source_id,
                source_segment_override=source_segment_override_item,
            )
            if segment in segments:
                continue
            if root is not None:
                if any(_names_same_directory(root, segment, identities) for identities in override_identities):
                    continue
                override_identities.append(_source_segment_directory_identities(root, segment))
            segments.append(segment)
        if not segments:
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="source_id")
        return tuple(segments)
    primary = _cycle_read_source_segment(
        source_id=source_id,
        source_segment_override=source_segment_override,
    )
    if source_segment_override is not None:
        return (primary,)
    segments = [primary]
    kept_identities: list[dict[str, tuple[int, int]]] = (
        [_source_segment_directory_identities(root, primary)] if root is not None else []
    )
    for alias in (source_id.lower(), source_id.upper()):
        segment = _safe_segment(alias)
        if segment in segments:
            continue
        if root is not None:
            if any(_names_same_directory(root, segment, identities) for identities in kept_identities):
                continue
            kept_identities.append(_source_segment_directory_identities(root, segment))
        segments.append(segment)
    return tuple(segments)


def _required_source_id(row: Mapping[str, Any], field: str) -> str:
    return _normalize_file_source_id(row.get(field), field=field)


def _optional_source_id(row: Mapping[str, Any], field: str) -> str | None:
    value = row.get(field)
    if value in (None, ""):
        return None
    return _normalize_file_source_id(value, field=field)


def _cycle_id_for_file_source(source_id: str, cycle_time: datetime) -> str:
    return cycle_id_for(_normalize_file_source_id(source_id, field="source_id"), cycle_time)


def _blocked_cycle_id(source_id: str, cycle_time: datetime) -> str:
    try:
        return _cycle_id_for_file_source(source_id, cycle_time)
    except FileOrchestrationJournalError:
        return "file_journal_read_blocked"


def _canonical_candidate_run_id(value: str, *, source_id: str, cycle_time: datetime, model_id: str) -> str:
    cycle_stamp = format_cycle_time(cycle_time)
    match = _FORECAST_RUN_ID_RE.fullmatch(str(value))
    if match is None:
        return value
    run_source, run_cycle, run_model = match.groups()
    try:
        matches_source = _normalize_file_source_id(run_source, field="run_id") == source_id
    except FileOrchestrationJournalError:
        return value
    if matches_source and run_cycle == cycle_stamp and run_model == model_id:
        return f"fcst_{source_id.lower()}_{cycle_stamp}_{model_id}"
    return value


def _canonical_forcing_version_id(value: str, *, source_id: str, cycle_time: datetime, model_id: str) -> str:
    cycle_stamp = format_cycle_time(cycle_time)
    match = re.fullmatch(r"forc_([^_]+)_(\d{10})_(.+)", str(value))
    if match is None:
        return value
    forcing_source, forcing_cycle, forcing_model = match.groups()
    try:
        matches_source = _normalize_file_source_id(forcing_source, field="forcing_version_id") == source_id
    except FileOrchestrationJournalError:
        return value
    if matches_source and forcing_cycle == cycle_stamp and forcing_model == model_id:
        return f"forc_{source_id.lower()}_{cycle_stamp}_{model_id}"
    return value


def _canonical_candidate_id(value: str, *, source_id: str, cycle_time: datetime, model_id: str) -> str:
    text = str(value)
    candidate_source, separator, remainder = text.partition(":")
    if not separator:
        return value
    try:
        matches_source = _normalize_file_source_id(candidate_source, field="candidate_id") == source_id
    except FileOrchestrationJournalError:
        return value
    if not matches_source:
        return value
    remainder = remainder.replace(f"forecast_{candidate_source}_", f"forecast_{source_id}_", 1)
    return f"{source_id}:{remainder}"


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if value in (None, ""):
        raise FileOrchestrationJournalError("file_journal_missing_identity", field=field)
    return _scalar_text(value, field=field, invalid_reason="file_journal_invalid_identity")


def _required_safe_identity(row: Mapping[str, Any], field: str) -> str:
    return _safe_identity_text(_required_text(row, field), field=field)


def _optional_text(
    row: Mapping[str, Any],
    field: str,
    *,
    invalid_reason: str = "file_journal_invalid_field",
) -> str | None:
    value = row.get(field)
    if value in (None, ""):
        return None
    return _scalar_text(value, field=field, invalid_reason=invalid_reason)


def _optional_safe_identity(row: Mapping[str, Any], field: str) -> str | None:
    value = _optional_text(row, field, invalid_reason="file_journal_invalid_identity")
    if value is None:
        return None
    return _safe_identity_text(value, field=field)


def _scalar_text(value: Any, *, field: str, invalid_reason: str) -> str:
    if isinstance(value, Mapping) or (isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)):
        raise FileOrchestrationJournalError(invalid_reason, field=field)
    if isinstance(value, bytes | bytearray):
        raise FileOrchestrationJournalError(invalid_reason, field=field)
    return str(value)


def _safe_identity_text(value: str, *, field: str) -> str:
    if (
        not value
        or len(value) > MAX_FILE_JOURNAL_PATH_SEGMENT_CHARS
        or value in {".", ".."}
        or _SAFE_SEGMENT_RE.fullmatch(value) is None
    ):
        raise FileOrchestrationJournalError("file_journal_unsafe_identity", field=field)
    return value


def _validated_actual_writer_generation(value: str) -> str:
    return _validated_git_writer_generation(
        value,
        field="actual_writer_generation",
        invalid_reason="file_journal_rollback_writer_generation_unresolvable",
    )


def _validated_git_writer_generation(
    value: str,
    *,
    field: str,
    invalid_reason: str,
) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value) is None:
        raise FileOrchestrationJournalError(invalid_reason, field=field)
    return value.lower()


def _validate_scheduler_visible_fields(row: Mapping[str, Any]) -> None:
    for visible_field in (
        "status",
        "stage",
        "slurm_job_id",
        "idempotency_key",
        "error_code",
        "event_type",
        "status_from",
        "status_to",
    ):
        _optional_text(row, visible_field)


def _parse_cycle_time_field(row: Mapping[str, Any], field: str) -> datetime:
    value = row.get(field)
    if value in (None, ""):
        raise FileOrchestrationJournalError("file_journal_missing_identity", field=field)
    try:
        return parse_cycle_time(str(value))
    except (TypeError, ValueError) as error:
        raise FileOrchestrationJournalError("file_journal_invalid_cycle_time", field=field) from error


def _require_source_cycle(row: Mapping[str, Any], *, source_id: str, cycle_time: datetime) -> None:
    expected_source = _normalize_file_source_id(source_id, field="source_id")
    actual_source = _required_source_id(row, "source_id")
    if actual_source != expected_source:
        raise FileOrchestrationJournalError(
            "file_journal_source_mismatch",
            field="source_id",
            evidence={"expected": expected_source, "actual": actual_source[:80]},
        )
    parsed_cycle_time = _parse_cycle_time_field(row, "cycle_time")
    if _format_utc(parsed_cycle_time) != _format_utc(cycle_time):
        raise FileOrchestrationJournalError(
            "file_journal_cycle_mismatch",
            field="cycle_time",
            evidence={"expected": _format_utc(cycle_time), "actual": _format_utc(parsed_cycle_time)},
        )


def _require_cycle_id(row: Mapping[str, Any], expected_cycle_id: str) -> None:
    actual = _required_safe_identity(row, "cycle_id")
    if actual != expected_cycle_id:
        raise FileOrchestrationJournalError(
            "file_journal_cycle_id_mismatch",
            field="cycle_id",
            evidence={"expected": expected_cycle_id, "actual": actual[:80]},
        )


def _require_model_id(row: Mapping[str, Any], expected_model_id: str, *, required: bool) -> None:
    actual = _optional_safe_identity(row, "model_id")
    if actual is None:
        if required:
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="model_id")
        return
    if actual != expected_model_id:
        raise FileOrchestrationJournalError(
            "file_journal_model_mismatch",
            field="model_id",
            evidence={"expected": expected_model_id, "actual": actual[:80]},
        )


_RECORD_PAYLOAD_IDENTITY_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "hydro_run": (
        ("run_id", "file_journal_run_mismatch"),
        ("model_id", "file_journal_model_mismatch"),
    ),
    "forecast_cycle": (("cycle_id", "file_journal_cycle_id_mismatch"),),
    "forcing_version": (
        ("forcing_version_id", "file_journal_forcing_version_mismatch"),
        ("model_id", "file_journal_model_mismatch"),
    ),
    "model_context": (("model_id", "file_journal_model_mismatch"),),
    "pipeline_job": (
        ("job_id", "file_journal_job_mismatch"),
        ("run_id", "file_journal_run_mismatch"),
        ("model_id", "file_journal_model_mismatch"),
    ),
    "pipeline_event": (
        ("event_id", "file_journal_event_mismatch"),
        ("entity_id", "file_journal_job_mismatch"),
    ),
}


def _require_record_payload_identity_match(
    record_type: str,
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> None:
    for identity_field, reason in _RECORD_PAYLOAD_IDENTITY_FIELDS.get(record_type, ()):
        envelope_value = _optional_safe_identity(record, identity_field)
        payload_value = _optional_safe_identity(payload, identity_field)
        if envelope_value is not None and payload_value is not None and envelope_value != payload_value:
            raise FileOrchestrationJournalError(
                reason,
                field=identity_field,
                evidence={"expected": envelope_value, "actual": payload_value[:80]},
            )


def _record_model_id(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
) -> str | None:
    envelope_model_id = _optional_safe_identity(record, "model_id")
    payload_model_id = _optional_safe_identity(payload, "model_id")
    envelope_run_model_id = _model_id_from_run_identity(
        record.get("run_id"),
        source_id=source_id,
        cycle_time=cycle_time,
    )
    payload_run_model_id = _model_id_from_run_identity(
        payload.get("run_id"),
        source_id=source_id,
        cycle_time=cycle_time,
    )
    if (
        envelope_run_model_id is not None
        and payload_run_model_id is not None
        and envelope_run_model_id != payload_run_model_id
    ):
        raise FileOrchestrationJournalError(
            "file_journal_run_mismatch",
            field="run_id",
            evidence={"expected": envelope_run_model_id, "actual": payload_run_model_id[:80]},
        )
    if envelope_model_id is not None and payload_model_id is not None and envelope_model_id != payload_model_id:
        raise FileOrchestrationJournalError(
            "file_journal_model_mismatch",
            field="model_id",
            evidence={"expected": envelope_model_id, "actual": payload_model_id[:80]},
        )
    explicit_model_id = envelope_model_id if envelope_model_id is not None else payload_model_id
    inferred_run_model_id = envelope_run_model_id if envelope_run_model_id is not None else payload_run_model_id
    if (
        explicit_model_id is not None
        and inferred_run_model_id is not None
        and explicit_model_id != inferred_run_model_id
    ):
        raise FileOrchestrationJournalError(
            "file_journal_run_mismatch",
            field="run_id",
            evidence={"expected": explicit_model_id, "actual": inferred_run_model_id[:80]},
        )
    return explicit_model_id if explicit_model_id is not None else inferred_run_model_id


def _explicit_record_model_id(record: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    envelope_model_id = _optional_safe_identity(record, "model_id")
    payload_model_id = _optional_safe_identity(payload, "model_id")
    if envelope_model_id is not None and payload_model_id is not None and envelope_model_id != payload_model_id:
        raise FileOrchestrationJournalError(
            "file_journal_model_mismatch",
            field="model_id",
            evidence={"expected": envelope_model_id, "actual": payload_model_id[:80]},
        )
    return envelope_model_id if envelope_model_id is not None else payload_model_id


def _model_id_from_run_identity(value: Any, *, source_id: str, cycle_time: datetime) -> str | None:
    if value in (None, ""):
        return None
    run_id = _safe_identity_text(
        _scalar_text(value, field="run_id", invalid_reason="file_journal_invalid_identity"),
        field="run_id",
    )
    cycle_stamp = format_cycle_time(cycle_time)
    forecast_match = _FORECAST_RUN_ID_RE.fullmatch(run_id)
    if forecast_match is not None:
        run_source, run_cycle, run_model = forecast_match.groups()
        if _normalize_file_source_id(run_source, field="run_id") == source_id and run_cycle == cycle_stamp:
            return _safe_identity_text(run_model, field="run_id")
        return None
    cycle_match = _CYCLE_RUN_ID_RE.fullmatch(run_id)
    if cycle_match is not None:
        run_source, run_cycle = cycle_match.groups()
        if _normalize_file_source_id(run_source, field="run_id") == source_id and run_cycle == cycle_stamp:
            return None
    return None


def _validate_hydro_run_identity(
    row: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
) -> None:
    _require_source_cycle(row, source_id=source_id, cycle_time=cycle_time)
    _require_model_id(row, model_id, required=True)
    actual_run_id = _required_safe_identity(row, "run_id")
    cycle_stamp = format_cycle_time(cycle_time)
    expected_forecast_run_id = f"fcst_{source_id.lower()}_{cycle_stamp}_{model_id}"
    expected_cycle_run_prefix = f"cycle_{source_id.lower()}_{cycle_stamp}"
    if actual_run_id != expected_forecast_run_id and (
        actual_run_id != expected_cycle_run_prefix and not actual_run_id.startswith(f"{expected_cycle_run_prefix}_")
    ):
        raise FileOrchestrationJournalError(
            "file_journal_run_mismatch",
            field="run_id",
            evidence={
                "expected": f"{expected_forecast_run_id}|{expected_cycle_run_prefix}",
                "actual": actual_run_id[:80],
            },
        )
    _validate_scheduler_visible_fields(row)


def _validate_forecast_cycle_identity(row: Mapping[str, Any], *, source_id: str, cycle_time: datetime) -> None:
    _require_source_cycle(row, source_id=source_id, cycle_time=cycle_time)
    _require_cycle_id(row, _cycle_id_for_file_source(source_id, cycle_time))
    _validate_scheduler_visible_fields(row)


def _validate_forcing_version_identity(
    row: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
    require_forcing_version_id: bool = True,
    require_source_cycle: bool = False,
    require_model_id: bool = False,
) -> None:
    source_id = _normalize_file_source_id(source_id, field="source_id")
    if require_source_cycle:
        _require_source_cycle(row, source_id=source_id, cycle_time=cycle_time)
    else:
        actual_source = _optional_source_id(row, "source_id")
        if actual_source is not None and actual_source != source_id:
            raise FileOrchestrationJournalError(
                "file_journal_source_mismatch",
                field="source_id",
                evidence={"expected": source_id, "actual": actual_source[:80]},
            )
        if row.get("cycle_time") not in (None, ""):
            parsed_cycle_time = _parse_cycle_time_field(row, "cycle_time")
            if _format_utc(parsed_cycle_time) != _format_utc(cycle_time):
                raise FileOrchestrationJournalError(
                    "file_journal_cycle_mismatch",
                    field="cycle_time",
                    evidence={"expected": _format_utc(cycle_time), "actual": _format_utc(parsed_cycle_time)},
                )
    _require_model_id(row, model_id, required=require_model_id)
    forcing_version_id = row.get("forcing_version_id")
    if forcing_version_id in (None, ""):
        if require_forcing_version_id:
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="forcing_version_id")
        return
    expected_prefix = f"forc_{source_id.lower()}_{format_cycle_time(cycle_time)}_{model_id}"
    actual_forcing_version_id = _required_safe_identity(row, "forcing_version_id")
    if actual_forcing_version_id != expected_prefix:
        raise FileOrchestrationJournalError(
            "file_journal_forcing_version_mismatch",
            field="forcing_version_id",
            evidence={"expected": expected_prefix, "actual": actual_forcing_version_id[:80]},
        )


def _validate_model_context_identity(row: Mapping[str, Any], *, model_id: str) -> None:
    _require_model_id(row, model_id, required=True)


def _validate_pipeline_job_identity(
    row: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str | None,
    expected_job_id: str | None = None,
) -> None:
    source_id = _normalize_file_source_id(source_id, field="source_id")
    job_id = _required_safe_identity(row, "job_id")
    if expected_job_id is not None and job_id != expected_job_id:
        raise FileOrchestrationJournalError(
            "file_journal_job_mismatch",
            field="job_id",
            evidence={"expected": expected_job_id, "actual": job_id[:80]},
        )
    actual_source = _optional_source_id(row, "source_id")
    if actual_source is not None:
        if actual_source != source_id:
            raise FileOrchestrationJournalError(
                "file_journal_source_mismatch",
                field="source_id",
                evidence={"expected": source_id, "actual": actual_source[:80]},
            )
    if row.get("cycle_time") not in (None, ""):
        parsed_cycle_time = _parse_cycle_time_field(row, "cycle_time")
        if _format_utc(parsed_cycle_time) != _format_utc(cycle_time):
            raise FileOrchestrationJournalError(
                "file_journal_cycle_mismatch",
                field="cycle_time",
                evidence={"expected": _format_utc(cycle_time), "actual": _format_utc(parsed_cycle_time)},
            )
    _require_cycle_id(row, _cycle_id_for_file_source(source_id, cycle_time))
    _validate_scheduler_visible_fields(row)
    cycle_run_id = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
    if model_id not in (None, ""):
        _require_model_id(row, str(model_id), required=False)
        candidate_run_id = f"fcst_{source_id.lower()}_{format_cycle_time(cycle_time)}_{model_id}"
        cycle_run_prefix = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
        run_id = _required_safe_identity(row, "run_id")
        if run_id != candidate_run_id and run_id != cycle_run_id and not run_id.startswith(f"{cycle_run_prefix}_"):
            raise FileOrchestrationJournalError(
                "file_journal_run_mismatch",
                field="run_id",
                evidence={"expected": f"{candidate_run_id}|{cycle_run_prefix}", "actual": run_id[:80]},
            )
        return
    run_id = _required_safe_identity(row, "run_id")
    if (
        run_id != cycle_run_id
        and not run_id.startswith(f"{cycle_run_id}_")
        and not run_id.startswith(f"fcst_{source_id.lower()}_{format_cycle_time(cycle_time)}_")
    ):
        raise FileOrchestrationJournalError(
            "file_journal_run_mismatch",
            field="run_id",
            evidence={"expected": f"{cycle_run_id}|{cycle_run_id}_<cohort>", "actual": run_id[:80]},
        )


def _validate_event_identity(
    row: Mapping[str, Any],
    *,
    source_id: str | None = None,
    cycle_time: datetime | None = None,
) -> None:
    _optional_text(row, "event_id")
    entity_id = _required_safe_identity(row, "entity_id")
    entity_type = _pipeline_event_entity_type(row.get("entity_type") or "pipeline_job")
    if entity_type == "forecast_cycle" and source_id is not None and cycle_time is not None:
        expected_cycle_id = _cycle_id_for_file_source(source_id, cycle_time)
        if entity_id != expected_cycle_id:
            raise FileOrchestrationJournalError(
                "file_journal_event_entity_mismatch",
                field="entity_id",
                evidence={"expected": expected_cycle_id, "actual": entity_id[:80]},
            )
    _validate_scheduler_visible_fields(row)


def _validate_payload_identity(
    record_type: str,
    payload: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str | None,
) -> None:
    if record_type == "hydro_run":
        if model_id in (None, ""):
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="model_id")
        _validate_hydro_run_identity(payload, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
    elif record_type == "forecast_cycle":
        _validate_forecast_cycle_identity(payload, source_id=source_id, cycle_time=cycle_time)
    elif record_type == "forcing_version":
        if model_id in (None, ""):
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="model_id")
        _validate_forcing_version_identity(payload, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
    elif record_type == "model_context":
        if model_id in (None, ""):
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="model_id")
        _validate_model_context_identity(payload, model_id=model_id)


def _latest_identity_from_path(path: Path, *, root: Path) -> tuple[str, datetime, str]:
    parts = path.relative_to(root).parts
    if len(parts) != 4 or parts[0] != "latest":
        raise FileOrchestrationJournalError(
            "file_journal_path_identity_mismatch",
            field=str(_relative_evidence(path, root)),
        )
    source_id = _normalize_file_source_id(parts[1], field="source_id")
    cycle_segment = _safe_segment(parts[2])
    model_id = _safe_segment(Path(parts[3]).stem)
    return source_id, _parse_cycle_segment(cycle_segment, field=str(_relative_evidence(path, root))), model_id


def _journal_identity_from_path(path: Path, *, root: Path, surface: str) -> tuple[str, datetime]:
    source_id, cycle_time, _segment_index = _journal_segment_identity_from_path(
        path,
        root=root,
        surface=surface,
    )
    return source_id, cycle_time


def _journal_segment_identity_from_path(
    path: Path,
    *,
    root: Path,
    surface: str,
) -> tuple[str, datetime, int]:
    """Resolve (source, cycle, segment index) for one cycle event-log file.

    Continuation segments belong to their base cycle; genuinely unparseable
    names keep raising ``file_journal_invalid_cycle_time`` exactly as before.
    """
    parts = path.relative_to(root).parts
    if len(parts) != 3 or parts[0] != surface:
        raise FileOrchestrationJournalError(
            "file_journal_path_identity_mismatch",
            field=str(_relative_evidence(path, root)),
        )
    source_id = _normalize_file_source_id(parts[1], field="source_id")
    cycle_stem, segment_index = _split_journal_segment_stem(Path(parts[2]).stem)
    cycle_time = _parse_cycle_segment(
        _safe_segment(cycle_stem),
        field=str(_relative_evidence(path, root)),
    )
    _require_journal_segment_lineage(
        path,
        root=root,
        cycle_segment=cycle_stem,
        segment_index=segment_index,
    )
    return source_id, cycle_time, segment_index


def _journal_segment_name(cycle_segment: str, segment_index: int) -> str:
    """Canonical file name of one per-cycle event-log segment."""

    if segment_index <= 0:
        return f"{cycle_segment}.jsonl"
    return f"{cycle_segment}.{segment_index}.jsonl"


def _journal_segment_names(cycle_segment: str) -> tuple[str, ...]:
    """Every segment slot a cycle may own, plus the first illegal one.

    The extra probe narrows — it does not eliminate — the asymmetry between
    the recursive walkers and the cycle-level readers: it pulls the first
    over-cap segment into enumeration and the cache fingerprint, so the
    asymmetry is pushed out to the window boundary.  An index beyond
    ``MAX_FILE_JOURNAL_CYCLE_SEGMENTS`` is still walker-detected only,
    because widening the window further would mean globbing the directory.
    """
    return tuple(
        _journal_segment_name(cycle_segment, index)
        for index in range(MAX_FILE_JOURNAL_CYCLE_SEGMENTS + 1)
    )


def _journal_segment_stem_in_cycle_scope(stem: str, cycle_segment: str) -> bool:
    """Does this segment file name belong to (or fail to disclaim) one cycle?

    Same fall-open shape D2a fixes on the flat direct surface: skip a name ONLY
    when it resolves to a *different* cycle, exactly as the whole-tree scan
    would attribute it. A name that resolves to nothing — ``<cycle>.x.jsonl``,
    ``not-a-cycle.jsonl`` — is handed to ``_journal_identity_from_path`` so it
    fails closed with the same reason the whole-tree scan raises, instead of
    being quietly skipped.
    """

    base, _segment_index = _split_journal_segment_stem(stem)
    if base == cycle_segment:
        return True
    try:
        parse_cycle_time(_safe_segment(base))
    except (TypeError, ValueError, FileOrchestrationJournalError):
        return True
    return False


def _split_journal_segment_stem(stem: str) -> tuple[str, int]:
    """Split a journal file stem into (cycle segment, segment index).

    ``<cycle>`` is segment 0 and ``<cycle>.<n>`` (n >= 1) is segment n.
    Anything else — a non-numeric suffix, or an explicit ``.0`` — is returned
    unchanged as segment 0, so foreign names keep today's parsing behaviour
    byte-identically.
    """
    base, dot, suffix = stem.rpartition(".")
    if dot and base and _JOURNAL_SEGMENT_INDEX_RE.fullmatch(suffix):
        return base, int(suffix)
    return stem, 0


def _require_journal_segment_lineage(
    path: Path,
    *,
    root: Path,
    cycle_segment: str,
    segment_index: int,
) -> None:
    """Fail closed on orphan, gapped or over-cap continuation segments.

    One rule for the cycle-level enumeration and every recursive walker, so a
    corrupt segment gets the same answer from every reader rather than being
    ignored by one and read by another.
    """
    if segment_index <= 0:
        return
    directory = path.parent
    for index in range(min(segment_index, MAX_FILE_JOURNAL_CYCLE_SEGMENTS)):
        if _stat_signature(directory / _journal_segment_name(cycle_segment, index)) is None:
            raise FileOrchestrationJournalError(
                "file_journal_segment_gap",
                field=str(_relative_evidence(path, root)),
            )
    if segment_index >= MAX_FILE_JOURNAL_CYCLE_SEGMENTS:
        raise FileOrchestrationJournalError(
            "file_journal_segment_limit_exceeded",
            field=str(_relative_evidence(path, root)),
        )


def _journal_segment_sort_key(path: Path, *, root: Path, surface: str) -> tuple[str, str, int]:
    """Order journal paths by parsed identity, never by raw path name.

    Lexicographic order puts ``<cycle>.1.jsonl`` before ``<cycle>.jsonl``
    (``'1' < 'j'``), which would replay a continuation segment ahead of the
    base it continues.
    """
    parts = path.relative_to(root).parts if path.is_relative_to(root) else ()
    if len(parts) != 3 or parts[0] != surface:
        return (str(path), "", 0)
    cycle_segment, segment_index = _split_journal_segment_stem(Path(parts[2]).stem)
    return (parts[1], cycle_segment, segment_index)


def _parse_cycle_segment(value: str, *, field: str) -> datetime:
    try:
        return parse_cycle_time(value)
    except (TypeError, ValueError) as error:
        raise FileOrchestrationJournalError("file_journal_invalid_cycle_time", field=field) from error


def _iter_retention_surface_files(
    directory: Path,
    *,
    root: Path,
    surface: str,
    max_files: int,
    max_depth: int,
    budget: _RetentionDiscoveryBudget,
) -> Iterable[Path]:
    """Walk one exact retention root and reject every unrecognized occupant.

    ``latest`` admits only ``source/cycle/model.json``.  The two event surfaces
    admit only one source directory and canonical bounded cycle segments.  The
    walk counts every directory entry, not just accepted files, because a large
    foreign subtree must not hide beyond a convenient suffix filter.
    """

    def walk(current: Path, depth: int) -> Iterable[Path]:
        if depth > max_depth:
            raise FileOrchestrationJournalError(
                "file_journal_depth_limit_exceeded",
                field=str(_relative_evidence(current, root)),
                evidence={"max_depth": max_depth},
            )
        try:
            current_stat = stat_no_follow(current, containment_root=root)
        except FileNotFoundError:
            return
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_unreadable",
                field=str(_relative_evidence(current, root)),
                evidence={"error_type": type(error).__name__},
            ) from error
        if not stat.S_ISDIR(current_stat.st_mode):
            raise FileOrchestrationJournalError(
                "file_journal_unsafe_scanned_entry",
                field=str(_relative_evidence(current, root)),
                evidence={"entry_type": "not_directory"},
            )
        try:
            names = list_directory_no_follow_limited(
                current,
                containment_root=root,
                max_entries=max_files - budget.count,
            )
        except FileNotFoundError:
            return
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_unreadable",
                field=str(_relative_evidence(current, root)),
                evidence={"error_type": type(error).__name__},
            ) from error
        for name in sorted(names):
            budget.consume()
            if _SAFE_SEGMENT_RE.fullmatch(name) is None:
                raise FileOrchestrationJournalError(
                    "file_journal_unsafe_path_segment",
                    field=str(_relative_evidence(current / name, root)),
                )
            path = current / name
            try:
                entry_stat = stat_no_follow(path, containment_root=root)
            except FileNotFoundError:
                raise FileOrchestrationJournalError(
                    "file_journal_unreadable",
                    field=str(_relative_evidence(path, root)),
                ) from None
            except (OSError, SafeFilesystemError) as error:
                raise FileOrchestrationJournalError(
                    "file_journal_unsafe_scanned_entry",
                    field=str(_relative_evidence(path, root)),
                    evidence={"error_type": type(error).__name__},
                ) from error
            relative = path.relative_to(directory).parts
            if stat.S_ISDIR(entry_stat.st_mode):
                if (surface == "latest" and len(relative) < 3) or (surface != "latest" and len(relative) <= 1):
                    yield from walk(path, depth + 1)
                    continue
                raise FileOrchestrationJournalError(
                    "file_journal_unrecognised_retention_member",
                    field=str(_relative_evidence(path, root)),
                )
            if not stat.S_ISREG(entry_stat.st_mode):
                raise FileOrchestrationJournalError(
                    "file_journal_unsafe_scanned_entry",
                    field=str(_relative_evidence(path, root)),
                    evidence={"entry_type": "not_regular_file"},
                )
            if surface == "latest":
                if len(relative) != 3 or not name.endswith(".json"):
                    raise FileOrchestrationJournalError(
                        "file_journal_unrecognised_retention_member",
                        field=str(_relative_evidence(path, root)),
                    )
                _latest_identity_from_path(path, root=root)
            else:
                if len(relative) != 2 or not name.endswith(".jsonl"):
                    raise FileOrchestrationJournalError(
                        "file_journal_unrecognised_retention_member",
                        field=str(_relative_evidence(path, root)),
                    )
                _journal_segment_identity_from_path(path, root=root, surface=surface)
            yield path

    yield from walk(directory, 0)


def _iter_regular_json_files(
    directory: Path,
    *,
    root: Path,
    recursive: bool = False,
    max_files: int,
    max_depth: int,
) -> Iterable[Path]:
    yield from _iter_discovered_files(
        directory,
        root=root,
        suffix=".json",
        recursive=recursive,
        max_files=max_files,
        max_depth=max_depth,
    )


def _iter_jsonl_files(directory: Path, *, root: Path, max_files: int, max_depth: int) -> Iterable[Path]:
    yield from _iter_discovered_files(
        directory,
        root=root,
        suffix=".jsonl",
        recursive=True,
        max_files=max_files,
        max_depth=max_depth,
    )


def _iter_discovered_files(
    directory: Path,
    *,
    root: Path,
    suffix: str,
    recursive: bool,
    max_files: int,
    max_depth: int,
    strict_disappearance: bool = False,
    expected_root_signature: Any = _UNSET,
) -> Iterable[Path]:
    scanned_entries = 0

    def walk(current: Path, depth: int) -> Iterable[Path]:
        nonlocal scanned_entries
        if depth > max_depth:
            raise FileOrchestrationJournalError(
                "file_journal_depth_limit_exceeded",
                field=str(_relative_evidence(current, root)),
                evidence={"max_depth": max_depth},
            )
        strict_authority_walk = strict_disappearance and expected_root_signature is not _UNSET
        directory_signature = (
            _stat_signature(current) if strict_authority_walk else _UNSET
        )
        if (
            current == directory
            and expected_root_signature is not _UNSET
            and directory_signature != expected_root_signature
        ):
            raise FileOrchestrationJournalError(
                "file_journal_quiescence_authority_changed",
                field=str(_relative_evidence(current, root)),
            )
        if current != directory and strict_authority_walk and directory_signature is None:
            raise FileOrchestrationJournalError(
                "file_journal_quiescence_authority_changed",
                field=str(_relative_evidence(current, root)),
            )

        def require_directory_unchanged() -> None:
            if strict_authority_walk and _stat_signature(current) != directory_signature:
                raise FileOrchestrationJournalError(
                    "file_journal_quiescence_authority_changed",
                    field=str(_relative_evidence(current, root)),
                )

        try:
            current_mode = stat_no_follow(current, containment_root=root).st_mode
        except FileNotFoundError:
            if strict_disappearance and expected_root_signature is not _UNSET and (
                current != directory or expected_root_signature is not None
            ):
                raise FileOrchestrationJournalError(
                    "file_journal_quiescence_authority_changed",
                    field=str(_relative_evidence(current, root)),
                )
            if strict_disappearance and current != directory:
                raise FileOrchestrationJournalError(
                    "file_journal_reconcile_inventory_migration_invalid",
                    field=str(_relative_evidence(current, root)),
                )
            return
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_unsafe_scanned_entry",
                field=str(_relative_evidence(current, root)),
                evidence={"error_type": type(error).__name__},
            ) from error
        require_directory_unchanged()
        if not stat.S_ISDIR(current_mode):
            raise FileOrchestrationJournalError(
                "file_journal_unsafe_scanned_entry",
                field=str(_relative_evidence(current, root)),
                evidence={"entry_type": "not_directory"},
            )
        remaining_entries = max_files - scanned_entries
        if remaining_entries < 0:
            raise FileOrchestrationJournalError(
                "file_journal_file_limit_exceeded",
                field=str(_relative_evidence(directory, root)),
                evidence={"max_files": max_files},
            )
        try:
            entry_names = list_directory_no_follow_limited(
                current,
                containment_root=root,
                max_entries=remaining_entries,
            )
        except FileNotFoundError:
            if strict_disappearance:
                raise FileOrchestrationJournalError(
                    (
                        "file_journal_quiescence_authority_changed"
                        if expected_root_signature is not _UNSET
                        else "file_journal_reconcile_inventory_migration_invalid"
                    ),
                    field=str(_relative_evidence(current, root)),
                )
            return
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_unreadable",
                field=str(_relative_evidence(current, root)),
                evidence={"error_type": type(error).__name__},
            ) from error
        if len(entry_names) > remaining_entries:
            raise FileOrchestrationJournalError(
                "file_journal_file_limit_exceeded",
                field=str(_relative_evidence(directory, root)),
                evidence={"max_files": max_files},
            )
        require_directory_unchanged()
        scanned_entries += len(entry_names)
        for entry_name in sorted(entry_names):
            if _SAFE_SEGMENT_RE.fullmatch(entry_name) is None:
                raise FileOrchestrationJournalError(
                    "file_journal_unsafe_path_segment",
                    field=str(_relative_evidence(current / entry_name, root)),
                )
            entry = current / entry_name
            try:
                mode = stat_no_follow(entry, containment_root=root).st_mode
            except FileNotFoundError:
                if strict_disappearance:
                    raise FileOrchestrationJournalError(
                        (
                            "file_journal_quiescence_authority_changed"
                            if expected_root_signature is not _UNSET
                            else "file_journal_reconcile_inventory_migration_invalid"
                        ),
                        field=str(_relative_evidence(entry, root)),
                    )
                continue
            except (OSError, SafeFilesystemError) as error:
                raise FileOrchestrationJournalError(
                    "file_journal_unsafe_scanned_entry",
                    field=str(_relative_evidence(entry, root)),
                    evidence={"error_type": type(error).__name__},
                ) from error
            if stat.S_ISDIR(mode):
                if recursive:
                    yield from walk(entry, depth + 1)
                continue
            if entry_name.endswith(suffix):
                if not stat.S_ISREG(mode):
                    raise FileOrchestrationJournalError(
                        "file_journal_unsafe_scanned_entry",
                        field=str(_relative_evidence(entry, root)),
                        evidence={"entry_type": "not_regular_file"},
                    )
                yield entry
        require_directory_unchanged()

    yield from walk(directory, 0)


def _safe_segment(value: str) -> str:
    text = str(value)
    if (
        not text
        or len(text) > MAX_FILE_JOURNAL_PATH_SEGMENT_CHARS
        or text in {".", ".."}
        or _SAFE_SEGMENT_RE.fullmatch(text) is None
    ):
        raise FileOrchestrationJournalError("file_journal_unsafe_path_segment", field="path")
    return text


def _relative_evidence(path: Path, root: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError:
        return Path("[local-path]")


def _model_context_from_mapping(row: Mapping[str, Any], *, model_id: str) -> ModelContext:
    _validate_model_context_identity(row, model_id=model_id)
    return ModelContext(
        model_id=_required_safe_identity(row, "model_id"),
        basin_id=_optional_str(row.get("basin_id"), field="basin_id"),
        basin_version_id=_required_context_str(row, "basin_version_id"),
        river_network_version_id=_required_context_str(row, "river_network_version_id"),
        segment_count=_required_int(row, "segment_count"),
        model_package_uri=_required_context_str(row, "model_package_uri"),
        output_segment_count=_optional_int(row.get("output_segment_count"), field="output_segment_count"),
        model_package_checksum=_optional_str(
            row.get("model_package_checksum") or row.get("package_checksum"),
            field="model_package_checksum",
        ),
    )


def _forcing_context_from_mapping(row: Mapping[str, Any]) -> ForcingContext:
    lineage = _lineage_mapping(row.get("lineage_json"))
    return ForcingContext(
        _optional_str(row.get("forcing_version_id"), field="forcing_version_id"),
        _optional_str(row.get("forcing_package_uri"), field="forcing_package_uri"),
        _optional_datetime(row.get("start_time"), field="start_time"),
        _optional_datetime(row.get("end_time"), field="end_time"),
        _optional_str(row.get("source_id"), field="source_id"),
        _optional_int(
            _fallback_value(row.get("max_lead_hours"), lineage.get("max_lead_hours")),
            field="max_lead_hours",
        ),
        _optional_str(
            _fallback_value(
                row.get("forcing_package_manifest_uri"),
                lineage.get("forcing_package_manifest_uri"),
            ),
            field="forcing_package_manifest_uri",
        ),
        _optional_str(
            _fallback_value(
                row.get("forcing_package_manifest_checksum"),
                lineage.get("forcing_package_manifest_checksum"),
            ),
            field="forcing_package_manifest_checksum",
        ),
    )


def _fallback_value(primary: Any, fallback: Any) -> Any:
    return fallback if primary in (None, "") else primary


def _lineage_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if isinstance(value, Mapping):
        return value
    return {}


def _optional_str(value: Any, *, field: str = "value") -> str | None:
    if value in (None, ""):
        return None
    return _scalar_text(value, field=field, invalid_reason="file_journal_invalid_field")


def _required_context_str(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if value in (None, ""):
        raise FileOrchestrationJournalError("file_journal_missing_field", field=field)
    return _scalar_text(value, field=field, invalid_reason="file_journal_invalid_field")


def _required_int(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    if value in (None, ""):
        raise FileOrchestrationJournalError("file_journal_missing_field", field=field)
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise FileOrchestrationJournalError("file_journal_invalid_field", field=field) from error


def _optional_int(value: Any, *, field: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise FileOrchestrationJournalError("file_journal_invalid_field", field=field) from error


def _optional_datetime(value: Any, *, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _ensure_utc(value)
    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text).astimezone(UTC)
    except ValueError as error:
        raise FileOrchestrationJournalError("file_journal_invalid_field", field=field) from error
