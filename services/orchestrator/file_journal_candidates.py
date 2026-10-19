"""File-journal candidate-state and cycle-scope helpers.

Pipeline-job and event predicates, cycle-scope derivation from run, cycle and
idempotency identities, the read-blocked sentinel row, cohort-membership and
task-outcome projections, init-state identity extraction and the public row
views behind the file orchestration journal's candidate state.  The journal's
error type and the status constants it owns stay in the facade and are imported
inside the functions that use them; the two names tests patch on the facade
(the bounded no-follow reader and the accepted-submit candidate evidence) are
looked up on the facade module at call time.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from packages.common.contracts.time_normalization import format_cycle_time, parse_cycle_time
from packages.common.safe_fs import SafeFilesystemError
from packages.common.status_vocab import PIPELINE_JOB_SUCCESS
from services.orchestrator import chain_repository_state
from services.orchestrator.accepted_submit_identity import (
    IDENTITY_MISMATCH_RELEASED_DECISION,
    INIT_STATE_IDENTITY_FIELD,
    MAX_FORECAST_COHORT_MEMBERS,
    QUARANTINE_RERUN_PROVENANCE_FIELD,
    AcceptedSubmitEvidenceError,
    accepted_submit_contract_is_current,
    accepted_submit_row_kind,
    normalize_quarantine_rerun_model_ids,
    ordered_cohort_members,
)
from services.orchestrator.chain_source_cycle import _datetime_sort_key, _pipeline_job_truth_sort_key
from services.orchestrator.file_journal_primitives import (
    _blocked_cycle_id,
    _cycle_id_for_file_source,
    _normalize_file_source_id,
    _optional_source_id,
    _parse_cycle_time_field,
    _required_safe_identity,
    _safe_identity_text,
    _scalar_text,
)
from services.orchestrator.public_evidence import _public_evidence
from services.orchestrator.run_identity import CYCLE_COHORT_RUN_ID_RE as _CYCLE_COHORT_RUN_ID_RE
from services.orchestrator.run_identity import FORECAST_RUN_ID_RE as _FORECAST_RUN_ID_RE
from services.orchestrator.scheduler_file_providers import _public_raw_manifest_evidence
from services.orchestrator.scheduler_init_state_match import INIT_STATE_IDENTITY_FIELDS, init_state_field
from services.orchestrator.scheduler_state import _evidence_safe, _format_utc
from services.orchestrator.scheduler_state_types import COHORT_MEMBER_ATTRIBUTED_STAGES, DOWNSTREAM_STAGE_ALIASES

if TYPE_CHECKING:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

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
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
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

    Only the forecast shape ``fcst_{source}_{cycle}_{model}`` and the cohort
    shape ``cycle_{source}_{cycle}[_suffix]`` derive, both through
    ``_source_cycle_from_file_run_id`` (which normalises the source segment,
    because run ids spell the source lower case while the on-disk directory
    carries the normalised casing). The analysis shape
    ``analysis_{source}_{start}_{end}_{model}`` deliberately does NOT derive
    (#1762): the only consumers of this helper are the ``pipeline_job``
    lookups, and ``_validate_pipeline_job_identity`` rejects an analysis run id
    with ``file_journal_run_mismatch`` on BOTH of its branches -- with a model
    id and without -- on every write and read path, so no ``pipeline_job`` row
    carrying one can exist to be found. ``run_identity.ANALYSIS_RUN_ID_RE`` and
    ``parse_run_cycle`` are untouched; retention still consumes them.
    """

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
    try:
        return _source_cycle_from_file_run_id(str(run_id))
    except FileOrchestrationJournalError:
        pass
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

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
    try:
        return _source_cycle_from_cycle_id(str(cycle_id))
    except FileOrchestrationJournalError:
        return None


def _source_cycle_from_cycle_id(cycle_id: str) -> tuple[str, datetime]:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
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


def _forcing_submitline_array_spec(submit_line: str) -> str | None:
    """#2675: the one ``--array=`` value of a forcing ``SubmitLine``, or ``None``.

    Whitespace-delimited like ``reconcile._submitline_comment_key`` (the gateway
    passes sbatch an argv list).  Every array option spelling counts as an
    occurrence (``--array=<v>``, ``--array <v>``, ``-a<v>``, ``-a <v>``); anything
    but exactly one occurrence, which must be the ``--array=<v>`` form the
    gateway renders, yields ``None`` -- a missing, duplicated (even identical)
    or other-shaped value is never guessed at.
    """

    occurrences = [
        token
        for token in submit_line.split()
        if token == "--array" or token.startswith("--array=") or token.startswith("-a")
    ]
    if len(occurrences) != 1 or not occurrences[0].startswith("--array="):
        return None
    return occurrences[0][len("--array=") :] or None


def _accepted_submit_source_cycle_from_job_id(pipeline_job_id: str) -> tuple[str, datetime]:
    from services.orchestrator.file_orchestration_journal import (
        _ACCEPTED_SUBMIT_MASTER_JOB_ID_RE,
        FileOrchestrationJournalError,
    )
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
    from services.orchestrator import file_orchestration_journal
    from services.orchestrator.file_orchestration_journal import MAX_FILE_JOURNAL_JSON_BYTES
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
            file_orchestration_journal.read_bytes_limited_no_follow(
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

_PUBLICATION_JOB_FIELDS = (
    "job_id",
    "run_id",
    "cycle_id",
    "job_type",
    "slurm_job_id",
    "array_task_id",
    "model_id",
    "status",
    "stage",
    "submitted_at",
    "started_at",
    "finished_at",
    "exit_code",
    "retry_count",
    "error_code",
    "error_message",
    "log_uri",
    "created_at",
    "updated_at",
)


def _publication_scheduler_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Closed allowlist of durable job fields for display provenance publication."""

    return {field: row.get(field) for field in _PUBLICATION_JOB_FIELDS}



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


def _blocked_query_job(
    error: FileOrchestrationJournalError,
    *,
    job_id: str = "file_journal_read_blocked",
    idempotency_key: str | None = None,
    cycle_id: str | None = None,
    run_id: str | None = None,
    slurm_job_id: str | None = None,
) -> dict[str, Any]:
    """The synthetic row the five query entrypoints return for a blocked read.

    #1953: the status names the blocked read instead of borrowing the vocabulary
    of a job that is actually running.  Everything else is unchanged -- the
    ``job_id`` defaults, the ``file_journal`` marker, the reason token, the field
    and the identifiers -- because every reader that decides on this row keys on
    the marker through :func:`_is_blocked_query_job` and reports the fault
    through :func:`_blocked_query_job_fault` (#2385/#2387: the retry-lane
    consumers that used to compare ``job_id`` no longer do; no identity field
    discriminates all five lanes).  The row stays PRESENT and non-terminal: the
    duplicate-submission and active-cycle guards read it as an in-flight job,
    which is what keeps them refusing to schedule against a journal nobody could
    read.
    """

    from services.orchestrator.file_orchestration_journal import FILE_JOURNAL_READ_BLOCKED_STATUS
    return _public_evidence(
        {
            "job_id": job_id or "file_journal_read_blocked",
            "idempotency_key": idempotency_key,
            "cycle_id": cycle_id,
            "run_id": run_id,
            "slurm_job_id": slurm_job_id or "unknown_after_attempt",
            "status": FILE_JOURNAL_READ_BLOCKED_STATUS,
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


def _is_blocked_query_job(job: Any) -> bool:
    """True only for the synthetic row :func:`_blocked_query_job` mints.

    Shape-keyed on the marker, never on ``job_id`` and never on the status
    literal (#2385/#2387 design D1).  No identity field discriminates all five
    query lanes: by-run and by-cycle keep the real ``run_id``/``cycle_id`` and
    the DEFAULT ``job_id``, while by-id keeps the real ``job_id``.  Only the
    marker covers every lane.

    Accepts ``Any`` and answers ``False`` for anything that is not a Mapping
    carrying the marker, because ``chain_forecast_execution``'s classifier
    duck-types its repository: when there is no ``get_pipeline_job`` it reads a
    plain ``repository.jobs`` mapping, and such a row must read as a normal one.

    ``pipeline_job_provenance`` deliberately keys on the status literal instead:
    its input is the publication projection, whose closed field allowlist drops
    ``file_journal`` entirely, so the marker is not available there at all.
    """

    if not isinstance(job, Mapping):
        return False
    marker = job.get("file_journal")
    return isinstance(marker, Mapping) and marker.get("status") == "blocked"


def _blocked_query_job_fault(job: Mapping[str, Any]) -> tuple[str, str]:
    """The journal's own ``(reason, field)`` off a row :func:`_is_blocked_query_job` accepted.

    Every consumer surfaces exactly these two tokens -- in a classified error or
    in a warning -- so the operator sees the journal's refusal rather than a
    fault the consumer derived from the unreadable row.
    """

    marker = job.get("file_journal")
    if not isinstance(marker, Mapping):
        return "", ""
    return str(marker.get("reason") or ""), str(marker.get("field") or "")


def _job_is_active(job: Mapping[str, Any]) -> bool:
    from services.orchestrator.file_orchestration_journal import TERMINAL_PIPELINE_STATUSES
    status = str(job.get("status") or "")
    if _job_is_unsubmitted_retry_placeholder(job, status=status):
        return False
    return status not in ("", *TERMINAL_PIPELINE_STATUSES)


def _candidate_scoped_forecast_cycle(forecast_cycle: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    from services.orchestrator.file_orchestration_journal import _TERMINAL_FORECAST_CYCLE_SUCCESS_STATUSES
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
    return str(job.get("status") or "") in PIPELINE_JOB_SUCCESS


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

    from services.orchestrator import file_orchestration_journal
    try:
        evidence = file_orchestration_journal.accepted_submit_candidate_immutable_evidence(job)
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


def _newer_master_identity_than_hydro(
    rows: Any,
    hydro_run: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
) -> dict[str, Any] | None:
    """Identity of a master accepted strictly after the completed hydro row, else ``None`` (#2397).

    Keys: the hydro row's ``created_at`` against the master's ``created_at``
    (its accepted-submit time).  Neither is refreshed by a later write, and a
    same-run_id rerun cannot rewrite a succeeded hydro row, so a newer master
    is the only journal truth of that rerun.  Among masters whose identity map
    names this model (model-less cohort masters otherwise match every model of
    the cycle), only the QUALIFIED ones compete -- terminal-success for this
    model and exactly one identity entry naming it and carrying an id -- and the
    newest of those is taken.  A later failed or still-running rerun carries no
    converged identity, so it neither wins nor hides the newest successful one
    (IS-01).  Any unreadable shape -> ``None``, which keeps the hydro identity
    (fails toward legacy).
    """

    unknown = datetime.min.replace(tzinfo=UTC)
    hydro_created = _datetime_sort_key(hydro_run.get("created_at"))
    if hydro_created == unknown:
        return None
    def _self_bound_identity(job: Mapping[str, Any]) -> dict[str, Any] | None:
        identities = job.get(INIT_STATE_IDENTITY_FIELD)
        if not isinstance(identities, Sequence) or isinstance(identities, str | bytes):
            return None
        entries = [
            entry
            for entry in identities
            if isinstance(entry, Mapping) and str(entry.get("model_id") or "") == model_id
        ]
        if len(entries) != 1:
            return None
        identity = {key: value for key, value in entries[0].items() if value not in (None, "")}
        if not any(identity.get(key) not in (None, "") for key in _INIT_STATE_ALIAS_KEYS):
            return None
        return identity

    try:
        # Model-less cohort masters match every candidate of the cycle, so only
        # submissions that carried THIS model compete: a sibling model's later
        # submission must not hide this model's converged rerun.  Qualification
        # (success + self-bound identity) comes BEFORE recency (IS-01).
        qualified = [
            (job, identity)
            for job in rows.pipeline_jobs.values()
            if _job_matches_candidate(job, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
            and accepted_submit_contract_is_current(job)
            and accepted_submit_row_kind(job) == "master"
            and _job_is_breaker_terminal_success(job, model_id=model_id)
            and (identity := _self_bound_identity(job)) is not None
        ]
        if not qualified:
            return None
        latest, identity = max(
            qualified,
            key=lambda item: (_datetime_sort_key(item[0].get("created_at")), _pipeline_job_truth_sort_key(item[0])),
        )
        if _datetime_sort_key(latest.get("created_at")) <= hydro_created:
            return None
    except (AcceptedSubmitEvidenceError, AttributeError, TypeError, ValueError):
        return None
    return identity


def _quarantine_rerun_masters(
    rows: Any,
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
    require_completed: bool,
    provenance_field: str = QUARANTINE_RERUN_PROVENANCE_FIELD,
) -> Iterator[Mapping[str, Any]]:
    """Cohort masters whose quarantine provenance names ``model_id`` (#1157/#1562/#1555).

    ``provenance_field`` selects the model-id-list stamp read: the §8.7
    quarantine provenance by default, or the budget re-entry provenance (r3-02).

    ``require_completed`` keeps only completed convergence attempts: aggregate
    terminal success, or ``partially_failed`` with this model's own projection
    succeeded.  Without it every stamped master counts whatever its status --
    the provenance is written when the rerun is accepted for submission.
    Per-model terminal copies are never masters.  One unreadable row is
    skipped (it can only undercount).
    """

    for job in _current_terminal_jobs(rows.pipeline_jobs.values()):
        try:
            if require_completed and not _job_is_breaker_terminal_success(job, model_id=model_id):
                continue
            if not _job_matches_candidate(job, source_id=source_id, cycle_time=cycle_time, model_id=model_id):
                continue
            if accepted_submit_row_kind(job) != "master":
                continue
            if model_id not in normalize_quarantine_rerun_model_ids(job.get(provenance_field)):
                continue
        except (AttributeError, TypeError, ValueError):
            continue
        yield job


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
    # #2603: the recorded-membership class, computed before compaction strips
    # ``cohort_members``; downstream consumers read it, never recompute it.
    "cohort_membership",
    # #2603 B2: on a ``member`` row that ended partially, the candidate's own
    # task outcome, and (when it succeeded) the row's durable status.
    "cohort_task_outcome",
    "cohort_row_status",
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


def _is_manual_retry_marker_event(event: Mapping[str, Any]) -> bool:
    """The manual-retry marker shape (``scheduler_state_manual_retry._manual_retry_marker_shape``)."""

    details = event.get("details")
    if event.get("event_type") not in {"retry", "manual_retry"} or not isinstance(details, Mapping):
        return False
    return details.get("trigger") == "manual" or details.get("manual_retry_marker") is True


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


def _cohort_run_ids_excluding_model(
    jobs: Iterable[Mapping[str, Any]], *, source_id: str, cycle_time: datetime, model_id: str
) -> frozenset[str]:
    """Suffixed cohort run ids whose recorded membership provably excludes ``model_id`` (#2543).

    Since #2543 one restart-compatible cohort can be split into several execution
    cohorts of the same cycle (a default group plus one per resource-profile
    override), each with its own ``cycle_<source>_<stamp>_<stage>_...`` run id.  The
    model-less arm of ``_job_matches_candidate`` makes every such row visible to
    every candidate of the cycle, so a running sibling cohort would block a
    candidate that is not one of its members.  Only forcing and forecast rows
    record ``cohort_members``; the union over all rows of a run id is that run's
    membership (a cohort run id is a digest of its membership, so it never
    changes).  A run id is returned only when at least one row records a complete,
    untruncated member list and the model is in none of them; runs without any
    recorded membership (e.g. before their forcing/forecast row exists) and the
    bare ``cycle_<source>_<stamp>`` run id stay cycle-wide, as before.
    """

    return frozenset(
        run_id
        for run_id, member_model_ids in _complete_cohort_members_by_run(
            jobs, source_id=source_id, cycle_time=cycle_time
        ).items()
        if model_id not in member_model_ids
    )


def _complete_cohort_members_by_run(
    jobs: Iterable[Mapping[str, Any]], *, source_id: str, cycle_time: datetime
) -> dict[str, frozenset[str]]:
    """Recorded membership of each suffixed cohort run id, where it is provable.

    The single membership rule behind :func:`_cohort_run_ids_excluding_model`
    (#2543), also read by the manual-retry cohort hint (#2584).  Only model-less
    rows of a ``cycle_<source>_<stamp>_...`` run id count, and only their
    ``cohort_members`` (forcing and forecast rows).  A run id is present only when
    at least one row records a member list and no row of that run records a
    truncated, length-mismatched, cap-sized or blank-``model_id`` list; its value
    is the union of the recorded ``model_id`` values.
    """

    cycle_run_id = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
    members_by_run: dict[str, set[str]] = {}
    incomplete_runs: set[str] = set()
    for job in jobs:
        run_id = str(job.get("run_id") or "")
        if job.get("model_id") not in (None, "") or not run_id.startswith(f"{cycle_run_id}_"):
            continue
        if job.get("cohort_members") in (None, "", [], ()):
            continue
        member_model_ids = _recorded_cohort_member_model_ids(job)
        if member_model_ids is None:
            incomplete_runs.add(run_id)
            continue
        members_by_run.setdefault(run_id, set()).update(member_model_ids)
    return {
        run_id: frozenset(member_model_ids)
        for run_id, member_model_ids in members_by_run.items()
        if run_id not in incomplete_runs
    }


def _recorded_cohort_member_model_ids(job: Mapping[str, Any]) -> frozenset[str] | None:
    """A row's OWN complete ``cohort_members`` model ids; ``None`` when absent or incomplete.

    The completeness predicate of :func:`_complete_cohort_members_by_run`, applied to
    one row.  Callers distinguish absent from incomplete by the raw field.
    """

    raw_members = job.get("cohort_members")
    members = ordered_cohort_members(raw_members)
    member_model_ids = frozenset(str(member.get("model_id") or "") for member in members)
    if (
        not members
        or not isinstance(raw_members, Sequence)
        or len(members) != len(raw_members)
        or len(members) >= MAX_FORECAST_COHORT_MEMBERS
        or "" in member_model_ids
    ):
        return None
    return member_model_ids


#: Values of the ``cohort_membership`` annotation a model-less cycle-scope row
#: carries on the candidate-state surface (#2603).
COHORT_MEMBERSHIP_MEMBER = "member"
COHORT_MEMBERSHIP_NON_MEMBER = "non_member"
COHORT_MEMBERSHIP_INCOMPLETE = "incomplete"
COHORT_MEMBERSHIP_UNWITNESSED = "unwitnessed"


def _cycle_scope_cohort_membership(
    jobs: Iterable[Mapping[str, Any]], *, source_id: str, cycle_time: datetime, model_id: str
) -> dict[str, str]:
    """Classify each model-less cycle-scope row for one candidate (#2603), keyed by ``job_id``.

    A row is judged ONLY by the ``cohort_members`` it records itself (forcing,
    forecast, and downstream master rows since #2603 B4), with the completeness
    predicate of :func:`_complete_cohort_members_by_run` (``has_active_pipeline``).
    So a narrowed forecast/state_save_qc row of a partially failed forcing cohort
    drops the members forcing lost.  A row recording no list -- including every
    row of the bare ``cycle_<source>_<stamp>`` run id and a pre-B4 downstream row
    of a run whose forcing/forecast rows do record members -- is ``unwitnessed``:
    another row's list (or the run's union) does not say which members THIS row
    ran for.
    """

    cycle_run_id = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
    classes: dict[str, str] = {}
    for job in jobs:
        if not _is_model_less_cycle_scope_job(job, source_id=source_id, cycle_time=cycle_time):
            continue
        job_id = str(job.get("job_id") or "")
        if not job_id:
            continue
        if not str(job.get("run_id") or "").startswith(f"{cycle_run_id}_") or job.get("cohort_members") in (
            None,
            "",
            [],
            (),
        ):
            classes[job_id] = COHORT_MEMBERSHIP_UNWITNESSED
            continue
        own_members = _recorded_cohort_member_model_ids(job)
        if own_members is None:
            classes[job_id] = COHORT_MEMBERSHIP_INCOMPLETE
        else:
            classes[job_id] = COHORT_MEMBERSHIP_MEMBER if model_id in own_members else COHORT_MEMBERSHIP_NON_MEMBER
    return classes


#: Values of the ``cohort_task_outcome`` projection key (#2603 B2, partial rows):
#: the candidate's OWN task outcome in a ``member`` array row that ended partially.
COHORT_TASK_OUTCOME_SUCCEEDED = "succeeded"
COHORT_TASK_OUTCOME_FAILED = "failed"
COHORT_TASK_OUTCOME_UNKNOWN = "unknown"


def _cohort_row_ended_partially(job: Mapping[str, Any], row_events: Sequence[Mapping[str, Any]]) -> bool:
    """The row ended with only some tasks failed: ``partially_failed``, or marked permanent from it."""

    status = str(job.get("status") or "")
    if status == "partially_failed":
        return True
    if status != "permanently_failed":
        return False
    for event in row_events:
        if event.get("status_to") == "partially_failed":
            return True
        # The retry adjudicator's permanent mark of THIS row (not the ``retry``
        # event a replacement row carries with its source row's status).
        if event.get("event_type") == "permanently_failed" and event.get("status_from") == "partially_failed":
            return True
    return False


def _cohort_member_task_outcome(
    job: Mapping[str, Any], row_events: Sequence[Mapping[str, Any]], *, model_id: str
) -> str:
    """The candidate's own task outcome in a partially ended ``member`` row.

    Read from the row's latest array aggregation event (``status_change`` to
    ``partially_failed``, written by
    ``chain_array_accounting.record_cycle_stage_status_override`` with per-task
    ``task_results`` naming each task's ``model_id``).  A member absent from a
    well-formed list is unverified, i.e. ``failed``.  ``unknown`` when no such
    event exists, or its tasks lack/duplicate a model id or name a model outside
    the row's own recorded members.
    """

    members = _recorded_cohort_member_model_ids(job) or frozenset()
    for event in reversed(row_events):
        details = event.get("details")
        if event.get("status_to") != "partially_failed" or not isinstance(details, Mapping):
            continue
        tasks = details.get("task_results")
        if not isinstance(tasks, Sequence) or isinstance(tasks, str | bytes) or not tasks:
            continue
        statuses: dict[str, str] = {}
        for task in tasks:
            task_model_id = str(task.get("model_id") or "") if isinstance(task, Mapping) else ""
            if not task_model_id or task_model_id in statuses or task_model_id not in members:
                return COHORT_TASK_OUTCOME_UNKNOWN
            statuses[task_model_id] = str(task.get("status") or "")
        if statuses.get(model_id) == "succeeded":
            return COHORT_TASK_OUTCOME_SUCCEEDED
        return COHORT_TASK_OUTCOME_FAILED
    return COHORT_TASK_OUTCOME_UNKNOWN


def _cohort_member_task_outcomes(
    jobs: Iterable[Mapping[str, Any]],
    events: Iterable[Mapping[str, Any]],
    cohort_membership: Mapping[str, str],
    *,
    model_id: str,
) -> dict[str, str]:
    """Per ``member`` row that ended partially: the candidate's own task outcome (#2603 B2).

    Computed before event compaction strips ``task_results``.  Rows of
    ``forecast`` are skipped: a forecast master is never attributed by membership
    (its per-member truth is the task projection rows).
    """

    member_jobs = {
        str(job.get("job_id") or ""): job
        for job in jobs
        if cohort_membership.get(str(job.get("job_id") or "")) == COHORT_MEMBERSHIP_MEMBER
        and DOWNSTREAM_STAGE_ALIASES.get(str(job.get("stage") or job.get("job_type") or ""))
        in COHORT_MEMBER_ATTRIBUTED_STAGES
    }
    if not member_jobs:
        return {}
    events_by_job: dict[str, list[Mapping[str, Any]]] = {}
    for event in events:
        entity_id = str(event.get("entity_id") or "")
        if entity_id in member_jobs and str(event.get("entity_type") or "pipeline_job") == "pipeline_job":
            events_by_job.setdefault(entity_id, []).append(event)
    outcomes: dict[str, str] = {}
    for job_id, job in member_jobs.items():
        row_events = events_by_job.get(job_id, [])
        if _cohort_row_ended_partially(job, row_events):
            outcomes[job_id] = _cohort_member_task_outcome(job, row_events, model_id=model_id)
    return outcomes


def _cohort_member_row_view(job: Mapping[str, Any], membership: str | None, task_outcome: str | None) -> dict[str, Any]:
    """The candidate's view of a model-less cohort row, before compaction.

    A member whose own task succeeded in a partially ended row sees the row as its
    success (``status`` ``succeeded``; the durable status stays readable as
    ``cohort_row_status``), exactly as a per-model row would read.
    """

    view = dict(job)
    if membership is not None:
        view["cohort_membership"] = membership
    if task_outcome is not None:
        view["cohort_task_outcome"] = task_outcome
        if task_outcome == COHORT_TASK_OUTCOME_SUCCEEDED:
            view["cohort_row_status"] = job.get("status")
            view["status"] = "succeeded"
            view["error_code"] = None
            view["error_message"] = None
    return view


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
    from services.orchestrator.file_orchestration_journal import (
        _SUPPORTED_PIPELINE_EVENT_ENTITY_TYPES,
        FileOrchestrationJournalError,
    )
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
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
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
