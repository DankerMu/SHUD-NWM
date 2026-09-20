"""Receipt schema tokens, outcome/reason vocabularies and bound constants.

Split out of ``scripts/scheduler_file_provider_refresh.py`` by #1099; the
historical module remains the executable entrypoint and attribute facade.
"""

from __future__ import annotations

from datetime import timedelta

from scripts.publish_scheduler_file_registry import CALIBRATION_OVERRIDE_NOT_SELECTED_REASON

SCHEMA_VERSION = "nhms.scheduler.file_provider_refresh_receipt.v1"

OUTCOMES = frozenset(
    {
        "dry_run",
        "published",
        "already_running",
        "failed",
        "replace_uncertain",
        "restored_previous",
        "published_receipt_failed",
    }
)

REASONS = frozenset(
    {
        "success",
        "dry_run_complete",
        "refresh_already_running",
        "configuration_invalid",
        "provider_invalid",
        "provider_preimage_changed",
        "provider_replace_failed",
        "provider_replace_uncertain",
        "provider_postread_failed",
        "workspace_limit_exceeded",
        "orphan_limit_exceeded",
        "primary_receipt_failed",
        "receipt_channels_failed",
        "emergency_record_invalid",
        # #1080 registry cutover gate refusal tokens.  Emitted only before any
        # canonical provider replacement; previous canonical bytes stay intact.
        "registry_cutover_undeclared",
        "registry_cutover_removal_refused",
        "registry_cutover_declaration_invalid",
        # #1832 round-2 C1: a declared calibration override that cannot be
        # loaded or applied.  `CalibrationOverrideError` is a bare
        # `RuntimeError` subclass, so before this token it fell through to the
        # generic handler and was published as `provider_invalid` with the
        # error code, the message and the offending entry all discarded -- the
        # same reason a dozen unrelated causes already emit, on a lane that
        # retries every tick.
        "calibration_override_invalid",
    }
)

CALIBRATION_OVERRIDE_REFUSAL_REASON = "calibration_override_invalid"

# Parity with the publisher's own token (`_declared_entries_not_applied`); the
# receipt admits exactly the reasons the publisher can emit.
CALIBRATION_OVERRIDE_NOT_APPLIED_REASONS = frozenset({CALIBRATION_OVERRIDE_NOT_SELECTED_REASON})

REGISTRY_CUTOVER_REFUSAL_REASONS = frozenset(
    {
        "registry_cutover_undeclared",
        "registry_cutover_removal_refused",
        "registry_cutover_declaration_invalid",
    }
)

CUTOVER_SCHEMA_VERSION = "nhms.scheduler.registry_package_cutover.v1"

REGISTRY_MANIFEST_SCHEMA_VERSION = "nhms.scheduler.file_model_registry.v1"

CUTOVER_TRANSITION_MODES = frozenset({"replace", "retire"})

# Per-bucket transition modes (#1433).  The declaration-level constant above is
# the union the loader accepts; each classification bucket admits exactly one
# mode, so a forged `"retire"` row inside `declared_cutovers` (or the reverse)
# is rejected by `_validate_object_group` instead of riding the union.
CUTOVER_REPLACE_TRANSITION_MODES = frozenset({"replace"})

CUTOVER_RETIRE_TRANSITION_MODES = frozenset({"retire"})

CUTOVER_DECLARATION_ENV = "NHMS_REGISTRY_CUTOVER_DECLARATION_PATH"

MAX_CUTOVER_DECLARATION_BYTES = 256 * 1024

CUTOVER_PAST_TOLERANCE = timedelta(hours=24)

CUTOVER_FUTURE_TOLERANCE = timedelta(hours=168)

# Aligned to the 00:00/12:00 UTC compute cycle cadence.
CUTOVER_CYCLE_HOURS = frozenset({0, 12})

MAX_RECEIPT_BYTES = 1024 * 1024

MAX_COLLECTION_ITEMS = 256

MAX_STRING_LENGTH = 512

MAX_RESIDUES = 64

MAX_HISTORY = 32

MAX_WORKSPACE_BYTES = 64 * 1024**3

MAX_WORKSPACE_ENTRIES = 250_000

MAX_WORKSPACE_DEPTH = 32

MAX_ORPHANS = 4096

MAX_ORPHAN_EVIDENCE = 256

RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "started_at",
        "finished_at",
        "outcome",
        "reason",
        "operation_outcome",
        "operation_reason",
        "phase",
        "database_free",
        "providers",
        "orphans",
        "residues",
    }
)

# Optional top-level keys.  `registry_classification` is emitted whenever the
# registry-cutover gate ran (the schema's allOf conditional requires it on
# dry_run/published/refusal outcomes); `cutover_gate` (#1132) is emitted
# whenever the runner constructed the audit block.  The allowed-set below is
# exact-match, so a key missing here makes every receipt carrying it fail as
# `receipt_shape_invalid`.
# `calibration_overrides` (#1832) is emitted whenever the run got far enough to
# know what the declared overrides did -- a completed publish, or a refusal
# raised while loading/applying them.
RECEIPT_OPTIONAL_KEYS = frozenset({"registry_classification", "cutover_gate", "calibration_overrides"})

class RefreshError(RuntimeError):
    def __init__(self, reason: str, *, outcome: str = "failed", phase: str = "precommit") -> None:
        super().__init__(reason)
        self.reason = reason if reason in REASONS else "provider_invalid"
        self.outcome = outcome if outcome in OUTCOMES else "failed"
        self.phase = phase
