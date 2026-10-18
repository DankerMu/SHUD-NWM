"""Status vocabulary: the single importable source of every status, verdict and raw-state set.

One named set per (domain, purpose) pair. Each set is copied verbatim from the
canonical site it replaces and is never a union across sites: sets that look
alike answer different questions (scheduler candidate construction, the
persistence terminal guard, the met-cycle upsert, the output parser), and
merging them would change runtime behaviour.

Every object here is immutable: a ``frozenset``, or a ``tuple`` where the
canonical site is ordered (a tuple is passed as a DB parameter or concatenated
there, or it reaches a schema/OpenAPI enum and must be deterministic under hash
randomisation). A canonical site whose object is a mutable ``set`` today binds
``set(<NAME>)`` in the module that owns the shared object, so each site keeps
its type and identity.

Rebinding schedule (OpenSpec change ``entropy-burndown-2026-10``): task 2.1a
rebinds the four ``scheduler_state_types`` names (durable hydro success,
hydro run code clearing, terminal pipeline success, failed pipeline); task 2.2
rebinds the API route's status tuple and local sets; tasks 2.3 and 2.4 rebind
the remaining orchestrator, worker, script and common sites.
``tests/test_status_vocab_parity.py`` pins every set against its canonical site,
the JSON schemas and the migration-folded database enums.

Standard library imports only: the orchestrator's light type modules import
this module and are themselves import-isolation probed.
"""

from __future__ import annotations

from typing import Literal

# --- Pipeline jobs -----------------------------------------------------------------

# The public, closed pipeline-job enum, in API/schema order. `reservation_lost` is
# written by the runtime and joins the public schema in task 2.2.
PipelineJobStatus = Literal[
    "pending",
    "queued",
    "submitted",
    "running",
    "succeeded",
    "partially_failed",
    "failed",
    "submission_failed",
    "reservation_lost",
    "permanently_failed",
    "cancelled",
    "skipped",
]
PIPELINE_JOB_STATUS_ORDER: tuple[str, ...] = (
    "pending",
    "queued",
    "submitted",
    "running",
    "succeeded",
    "partially_failed",
    "failed",
    "submission_failed",
    "reservation_lost",
    "permanently_failed",
    "cancelled",
    "skipped",
)
PIPELINE_JOB_STATUSES = frozenset(PIPELINE_JOB_STATUS_ORDER)
# Orchestrator-internal transients: never in a schema, an API tuple or a generated type.
PIPELINE_JOB_INTERNAL_STATUSES = frozenset({"reserved", "cancellation_pending", "reconcile_unverified"})
# Terminal pipeline-job statuses (chain types).
PIPELINE_JOB_TERMINAL = frozenset(
    {
        "succeeded",
        "partially_failed",
        "failed",
        "cancelled",
        "submission_failed",
        "reservation_lost",
        "permanently_failed",
    }
)
# Scheduler candidate construction: terminal without `reservation_lost`, kept apart
# because widening it changes which candidates are built.
PIPELINE_JOB_TERMINAL_WITHOUT_RESERVATION = PIPELINE_JOB_TERMINAL - {"reservation_lost"}
# Persistence terminal-status write guard and the two statuses allowed to override it.
PIPELINE_JOB_TERMINAL_GUARD = frozenset({"succeeded", "failed", "cancelled", "permanently_failed"})
PIPELINE_JOB_TERMINAL_OVERRIDES = frozenset({"partially_failed", "permanently_failed"})
# Persistence: statuses that block a manual retry (ordered, a DB parameter).
PIPELINE_JOB_ACTIVE_MANUAL_RETRY: tuple[str, ...] = ("pending", "queued", "submitted", "running")
# Read-side success tolerance. No pipeline-job writer emits `complete` or `published`;
# they are tolerated by comparison sites and never join the public enum.
PIPELINE_JOB_SUCCESS = frozenset({"succeeded", "complete", "published"})
# API route buckets.
PIPELINE_JOB_ACTIVE = frozenset({"pending", "queued", "submitted", "running"})
PIPELINE_JOB_PENDING = frozenset({"pending", "queued"})
PIPELINE_JOB_FAILED_UI = frozenset({"failed", "submission_failed", "permanently_failed", "cancelled"})
# Scheduler state: failed pipeline jobs, and pipeline jobs in flight.
FAILED_PIPELINE_STATUSES = frozenset({"failed", "submission_failed", "partially_failed", "permanently_failed"})
ACTIVE_PIPELINE_STATUSES = frozenset({"pending", "queued", "submitted", "running"})
# Persistence: an idempotency key already mapped to one of these is in flight
# (ordered, a DB parameter).
RESERVATION_ACTIVE_STATUSES: tuple[str, ...] = ("reserved", "submitted", "running", "queued", "pending")

# --- Hydro runs ----------------------------------------------------------------------

# The `schemas/run_status.schema.json` enum, in schema order.
HydroRunStatus = Literal[
    "created",
    "staged",
    "pending",
    "submitted",
    "running",
    "succeeded",
    "blocked",
    "unavailable",
    "partial",
    "parsed",
    "published",
    "failed",
    "cancelled",
    "superseded",
]
HYDRO_RUN_STATUS_ORDER: tuple[str, ...] = (
    "created",
    "staged",
    "pending",
    "submitted",
    "running",
    "succeeded",
    "blocked",
    "unavailable",
    "partial",
    "parsed",
    "published",
    "failed",
    "cancelled",
    "superseded",
)
HYDRO_RUN_STATUSES = frozenset(HYDRO_RUN_STATUS_ORDER)
# Scheduler state: hydro runs in flight.
ACTIVE_HYDRO_STATUSES = frozenset({"created", "staged", "pending", "submitted", "running"})
# Scheduler durable-success predicate. `complete` is the one member outside the
# hydro.run_status enum, tolerated for the file-journal lane.
DURABLE_HYDRO_SUCCESS_STATUSES = frozenset({"succeeded", "parsed", "published", "complete"})
# Hydro-run statuses whose journal write clears the row's error code.
HYDRO_RUN_CODE_CLEARING_STATUSES = frozenset({"pending", "created", "succeeded", "complete", "parsed", "published"})
# API route: terminal hydro runs.
HYDRO_RUN_TERMINAL_UI = frozenset({"succeeded", "parsed", "published", "failed", "cancelled", "superseded"})
# Display: a hydro run whose product is ready to show.
DISPLAY_PRODUCT_READY_STATUSES = frozenset({"succeeded", "parsed", "published"})
# Forecast store: latest ready QHH run (ordered, a DB parameter).
QHH_LATEST_READY_RUN_STATUSES: tuple[str, ...] = ("succeeded", "parsed", "published")
# Output parser: runs ready to parse, and runs the parser may mark failed (ordered, DB parameters).
PARSE_READY_RUN_STATUSES: tuple[str, ...] = ("succeeded", "parsed", "failed")
FAILABLE_RUN_STATUSES: tuple[str, ...] = ("created", "staged", "submitted", "running", "succeeded", "parsed")
# File journal: forecast-cycle success.
FORECAST_CYCLE_SUCCESS_STATUSES = frozenset({"complete", "succeeded", "parsed", "published"})

# --- Met cycles ----------------------------------------------------------------------

# The `met.cycle_status` enum folded over every migration, in DB ordinal order
# (`canonical_incomplete` is added AFTER `canonical_ready`, `cancelled` AFTER
# `failed_publish`).
CycleStatus = Literal[
    "discovered",
    "downloading",
    "raw_complete",
    "canonical_ready",
    "canonical_incomplete",
    "forcing_ready_partial",
    "forcing_ready",
    "forecast_running",
    "parsed_partial",
    "complete",
    "published",
    "failed_download",
    "failed_convert",
    "failed_forcing",
    "failed_run",
    "failed_parse",
    "failed_publish",
    "cancelled",
]
CYCLE_STATUS_ORDER: tuple[str, ...] = (
    "discovered",
    "downloading",
    "raw_complete",
    "canonical_ready",
    "canonical_incomplete",
    "forcing_ready_partial",
    "forcing_ready",
    "forecast_running",
    "parsed_partial",
    "complete",
    "published",
    "failed_download",
    "failed_convert",
    "failed_forcing",
    "failed_run",
    "failed_parse",
    "failed_publish",
    "cancelled",
)
CYCLE_STATUSES = frozenset(CYCLE_STATUS_ORDER)
# API route: terminal met cycles.
CYCLE_TERMINAL_UI = frozenset(
    {
        "complete",
        "published",
        "parsed_partial",
        "failed_download",
        "failed_convert",
        "failed_forcing",
        "failed_run",
        "failed_parse",
        "failed_publish",
        "cancelled",
    }
)
# Met store upsert: terminal cycles the upsert never overwrites (failed_* stay
# overwritable for retries), and the full preserved tuple it concatenates.
CYCLE_TERMINAL_UPSERT: tuple[str, ...] = ("published", "cancelled")
CYCLE_UPSERT_PRESERVED: tuple[str, ...] = (
    "raw_complete",
    "canonical_ready",
    "canonical_incomplete",
    "forcing_ready_partial",
    "forcing_ready",
    "forecast_running",
    "parsed_partial",
    "complete",
) + CYCLE_TERMINAL_UPSERT

# --- Slurm ---------------------------------------------------------------------------

# Every raw Slurm state name the gateway maps (the slurm gateway's state map keys).
SLURM_RAW_STATES = frozenset(
    {
        "BOOT_FAIL",
        "CANCELLED",
        "COMPLETED",
        "COMPLETING",
        "CONFIGURING",
        "DEADLINE",
        "FAILED",
        "NODE_FAIL",
        "OUT_OF_MEMORY",
        "PENDING",
        "PREEMPTED",
        "REQUEUED",
        "REVOKED",
        "RUNNING",
        "SPECIAL_EXIT",
        "SUSPENDED",
        "TIMEOUT",
    }
)
# Production-closure Slurm validation: terminal raw states.
SLURM_TERMINAL_STATES = frozenset(
    {
        "BOOT_FAIL",
        "CANCELLED",
        "COMPLETED",
        "DEADLINE",
        "FAILED",
        "NODE_FAIL",
        "OUT_OF_MEMORY",
        "PREEMPTED",
        "REVOKED",
        "SPECIAL_EXIT",
        "TIMEOUT",
    }
)
# QHH continuous runner: terminal success and terminal failure raw states.
SLURM_TERMINAL_SUCCESS = frozenset({"COMPLETED"})
SLURM_TERMINAL_FAILURE = frozenset(
    {"FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE"}
)

# --- QC verdicts and readiness items ------------------------------------------------

QC_VERDICTS = frozenset({"passed", "failed", "blocked"})
# Production-closure readiness item statuses: a distinct vocabulary, not QC verdicts.
READINESS_ITEM_STATUSES = frozenset({"passed", "failed", "blocked", "not_executed", "release_blocked"})

_QC_VERDICT_SPELLINGS: dict[str, str] = {
    "pass": "passed",
    "PASS": "passed",
    "passed": "passed",
    "PASSED": "passed",
    "fail": "failed",
    "FAIL": "failed",
    "failed": "failed",
    "FAILED": "failed",
    "blocked": "blocked",
    "BLOCKED": "blocked",
}


def normalize_qc_verdict(text: object) -> str:
    """Map a known QC verdict spelling to its `QC_VERDICTS` member.

    Read/compare paths only: receipt writers and validators keep their own
    spelling. An empty string, ``None``, a non-``str`` value or an unknown
    spelling raises ``ValueError``.
    """

    if not isinstance(text, str):
        raise ValueError(f"QC verdict must be a str, got {type(text).__name__}")
    try:
        return _QC_VERDICT_SPELLINGS[text]
    except KeyError:
        raise ValueError(f"unknown QC verdict spelling: {text!r}") from None
