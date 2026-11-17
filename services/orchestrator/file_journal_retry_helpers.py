"""File-journal retry and accepted-submit helpers.

The journal record constructor and its JSON encoding, the projection-warning
and durable-error redaction tokens, operator evidence text, the tolerant/strict
datetime and retry-lineage coercions, the accepted-submit evidence adapters,
restart-reconcile and rollback-quiescence predicates, and the retry job/gateway
value readers behind the file orchestration journal.  The journal's error type,
schema version and terminal status set stay in the facade and are imported
inside the functions that use them; the record clock ``_utcnow``, which tests
patch on the facade, is looked up on the facade module at call time by the
record constructor.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from packages.common.contracts.time_normalization import parse_cycle_time
from services.orchestrator.accepted_submit_identity import (
    INIT_STATE_IDENTITY_FIELD,
    MAX_FORECAST_COHORT_MEMBERS,
    SLURM_ACCOUNTING_SUBMITTED_AT_FIELD,
    SLURM_BINDING_SOURCE_FIELD,
    AcceptedSubmitEvidenceError,
    accepted_submit_contract_is_current,
    accepted_submit_master_immutable_identity,
    accepted_submit_master_ordinary_upsert_state,
    accepted_submit_row_kind,
    is_forecast_cohort_stage_name,
    normalize_accepted_submit_attempt_anchor,
    normalize_accepted_submit_evidence,
    normalize_init_state_identities,
    normalize_slurm_accounting_submitted_at,
)
from services.orchestrator.chain_source_cycle import _datetime_sort_key
from services.orchestrator.file_journal_candidates import _strip_redaction_placeholders
from services.orchestrator.file_journal_primitives import (
    _normalize_file_source_id,
    _safe_identity_text,
)
from services.orchestrator.public_evidence import _public_evidence, _public_message
from services.orchestrator.retry import _safe_error_message
from services.orchestrator.retry_identity import RETRY_JOB_ID_MARKER, effective_retry_attempt, split_retry_job_identity
from services.orchestrator.scheduler_state import _ensure_utc, _format_utc


def _next_current_master_retry_identity(current: Mapping[str, Any]) -> tuple[str, int]:
    job_id = str(current.get("job_id") or "")
    base_job_id, _suffix_attempt = split_retry_job_identity(job_id)
    retry_count = effective_retry_attempt(job_id, current.get("retry_count")) + 1
    return f"{base_job_id}{RETRY_JOB_ID_MARKER}{retry_count}", retry_count


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
    #     before this loop; the remaining detail fields are fixed/typed values;
    #   * its forcing branch ``_demote_operator_verified_forcing_locked``
    #     (#2682) -- ONE ``operator_verified_absence`` event with the same
    #     pre-sanitized ``checked_by`` / ``verification_note``; its other
    #     details are a literal lane, typed attempt/anchor values, the prior
    #     submit outcome and the row's own validated attempt comment.
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
    from services.orchestrator import file_orchestration_journal
    from services.orchestrator.file_orchestration_journal import FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION
    record: dict[str, Any] = {
        "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
        "sequence": int(sequence),
        "record_type": record_type,
        "source_id": _normalize_file_source_id(source_id, field="source_id"),
        "cycle_time": _format_utc(cycle_time),
        "created_at": _format_utc(file_orchestration_journal._utcnow()),
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


def _projection_error_type(error: Exception) -> str:
    """A fixed, non-secret ``error_type`` token for a projection warning.

    Every caught exception maps to the same constant, so no class name -- even
    an exact trusted-type label or a safe-looking identifier -- can be echoed.
    """
    del error
    return _PROJECTION_WARNING_FALLBACK_TOKEN


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
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError("file_journal_evidence_required", field=field)
    limit = MAX_OPERATOR_CHECKED_BY_LENGTH if field == "checked_by" else MAX_OPERATOR_VERIFICATION_NOTE_LENGTH
    if len(value) > limit:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError("file_journal_evidence_limit_exceeded", field=field)
    sanitized = _safe_error_message(value)
    # Apply the same public evidence sanitizer the display/read path uses, so a
    # local path, object URI, or credential never survives into the durable
    # event or the CLI receipt.  The return stays a str and non-blank.
    sanitized = _public_message(sanitized)
    if not isinstance(sanitized, str) or not sanitized.strip():
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError("file_journal_evidence_required", field=field)
    return sanitized


def _operator_owner_evidence_text(value: str | None, *, field: str) -> str:
    """#2675: the operator's sacct ``User`` / ``Account`` as bounded, sanitizer-clean evidence.

    The ``_operator_evidence_text`` bound (``checked_by``'s, raising
    ``file_journal_evidence_limit_exceeded``) and its sanitizer, except that
    absent or blank is allowed and yields ``""``, and a value the sanitizer
    would alter (a secret, path or URI) raises ``file_journal_unsafe_identity``
    instead of being redacted.  The value is the owner-rule comparison input as
    well as the audit text, and the journal may already hold a recorded owner
    in redacted form, so a redacted input could collide with it; refusing keeps
    the comparison exact.  The caller has already rejected non-string values.
    """

    if value is None or not value.strip():
        return ""
    if len(value) > MAX_OPERATOR_CHECKED_BY_LENGTH:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError("file_journal_evidence_limit_exceeded", field=field)
    if _public_message(_safe_error_message(value)) != value:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError("file_journal_unsafe_identity", field=field)
    return value


def _redact_durable_error_message_fields(record_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(payload)
    if record_type in {"pipeline_job", "hydro_run", "forecast_cycle"} and "error_message" in row:
        row["error_message"] = _durable_error_message(row.get("error_message"))
    return row


def _mapping_value(row: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    value = row.get(field)
    if not isinstance(value, Mapping):
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError("file_journal_expected_object", field=field)
    return value


def _optional_mapping_value(row: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    value = row.get(field)
    if value in (None, ""):
        return {}
    if not isinstance(value, Mapping):
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError("file_journal_expected_object", field=field)
    return value


def _coerce_cycle_or_iso_datetime(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    try:
        return parse_cycle_time(str(value))
    except (TypeError, ValueError) as error:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
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
    return _format_utc(_coerce_cycle_or_iso_datetime(value, field=field))


def _file_retry_job_value(job: Any, field: str) -> Any:
    if isinstance(job, Mapping):
        return job.get(field)
    return getattr(job, field, None)


def _file_retry_job_text(job: Any, field: str) -> str | None:
    value = _file_retry_job_value(job, field)
    return str(value) if value not in (None, "") else None


def _file_reconcile_namespace(row: Mapping[str, Any]) -> SimpleNamespace:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
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
            payload[dt_field] = _coerce_cycle_or_iso_datetime(value, field=dt_field)
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
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(error.reason, field=error.field) from error
    if _retry_lineage_contains_placeholder(normalized):
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
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
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(error.reason, field=error.field) from error


def _accepted_submit_master_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return accepted_submit_master_immutable_identity(row)
    except AcceptedSubmitEvidenceError as error:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(error.reason, field=error.field) from error


def _accepted_submit_master_state(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return accepted_submit_master_ordinary_upsert_state(row)
    except AcceptedSubmitEvidenceError as error:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(error.reason, field=error.field) from error


def _accepted_submit_attempt_anchor(value: Any) -> str:
    try:
        return normalize_accepted_submit_attempt_anchor(value)
    except AcceptedSubmitEvidenceError as error:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(error.reason, field=error.field) from error


def _file_journal_real_slurm_job_id(value: Any) -> bool:
    text = str(value or "")
    return bool(text and text.lower() != "local")


def _is_legacy_unversioned_forecast_cohort_master(row: Mapping[str, Any]) -> bool:
    """Return whether ``row`` is an unversioned forecast cohort master, in any status (#2674).

    A forecast cohort row (``stage``, or ``job_type`` when ``stage`` is empty,
    is a forecast alias) with non-empty ``cohort_members`` and no current
    accepted-submit contract marker.  The properties are tested cheap-first; a
    malformed marker still raises from ``accepted_submit_contract_is_current``
    (corruption fails closed).
    """

    if not is_forecast_cohort_stage_name(row.get("stage"), row.get("job_type")):
        return False
    members = row.get("cohort_members")
    if not isinstance(members, Sequence) or isinstance(members, str | bytes) or not members:
        return False
    return not accepted_submit_contract_is_current(row)


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
        from services.orchestrator.file_orchestration_journal import TERMINAL_PIPELINE_STATUSES
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
    from services.orchestrator.file_orchestration_journal import TERMINAL_PIPELINE_STATUSES
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
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
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
