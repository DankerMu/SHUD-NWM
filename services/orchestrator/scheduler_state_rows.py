from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from packages.common.source_identity import normalize_source_id
from services.orchestrator.production_contract import (
    PRODUCTION_EVIDENCE_CORRELATION_FIELDS,
    PRODUCTION_IDENTITY_FIELDS,
)
from services.orchestrator.retry_identity import effective_retry_attempt
from services.orchestrator.scheduler_state_common import (
    _coerce_int,
    _coerce_optional_nonnegative_int,
    _evidence_safe,
    _first_nested_state_value,
    _format_utc,
    _looks_like_production_job_id,
    _stage_cycle_run_matches_candidate,
)
from services.orchestrator.scheduler_state_types import (
    ACTIVE_PIPELINE_STATUSES,
    CANDIDATE_STATE_TASK_RESULT_LIMIT,
    DEFAULT_CANDIDATE_STATE_EVENT_LIMIT,
    DEFAULT_CANDIDATE_STATE_JOB_LIMIT,
    DOWNSTREAM_RESTART_STAGES,
    DOWNSTREAM_STAGE_ALIASES,
    FAILED_PIPELINE_STATUSES,
    STATE_CANDIDATE_SCOPED_PROOF_FIELDS,
    STATE_M23_COMPARISON_FIELDS,
)
from workers.data_adapters.base import format_cycle_time

# State keys carrying the per-stage attempt maxima across the job-limit
# truncation (#1179), and the candidate-identity metadata of the rows those
# maxima came from.  Both are present, possibly empty, on a state produced by
# ``candidate_state_from_rows``; the journal's read-blocked stub
# (``file_orchestration_journal._file_journal_blocked_candidate_state``) does
# not go through that projection and carries neither.  Every reader goes
# through ``.get``.  See ``stage_retry_attempt_floors``.
STAGE_RETRY_ATTEMPT_FLOORS_KEY = "stage_retry_attempt_floors"
STAGE_RETRY_ATTEMPT_FLOOR_SOURCES_KEY = "stage_retry_attempt_floor_sources"

#: Fields copied out of a contributing row into its floor-source record: the
#: ROW-LEVEL fields the identity/scope predicates read (``_legacy_identity_values``
#: aliases, ``_state_row_references_job_ids`` job id keys, and the source-cycle
#: download-blocker predicate's status/stage fields), plus the nested ``identity``
#: payload ``_legacy_identity_values`` treats as aliases of them.  Deeper nested
#: proofs (``task_identity`` / ``failed_task`` / ``details.task_results``, which
#: ``_nested_state_identity_payloads`` also reads) do NOT ride along, so a record
#: can answer non-authoritative where its row answered authoritative.  The
#: direction is conservative -- the floor is dropped, i.e. pre-#1179 behaviour --
#: and no production job row carrying such a payload has been found.
STAGE_RETRY_ATTEMPT_FLOOR_SOURCE_FIELDS = (
    "job_id",
    "pipeline_job_id",
    "pipeline_event_id",
    "entity_id",
    "previous_job_id",
    "failed_job_id",
    "run_id",
    "model_id",
    "basin_id",
    "source",
    "source_id",
    "cycle_time",
    "cycle_time_utc",
    "cycle_id",
    "basin_version_id",
    "river_network_version_id",
    "canonical_product_id",
    "forcing_version_id",
    "hydro_run_id",
    "published_manifest_id",
    "stage",
    "job_type",
    "status",
    "pipeline_status",
    "job_status",
    "error_code",
    "retry_count",
    "repair_status",
    "active_blocker",
)


def _bounded_candidate_state(state: Mapping[str, Any]) -> dict[str, Any]:
    bounded = dict(state)
    events = _state_events(bounded)
    if events:
        bounded["pipeline_events"] = [_bounded_candidate_event(event) for event in events]
        bounded.pop("events", None)
    return bounded

def _bounded_candidate_event(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(event)
    details = payload.get("details")
    if not isinstance(details, Mapping):
        return payload
    details_payload = dict(details)
    task_sample = _bounded_task_result_sample(details_payload)
    if task_sample is not None:
        task_rows, task_metadata = task_sample
        details_payload["task_results"] = task_rows
        details_payload.update(task_metadata)
    payload["details"] = details_payload
    return payload

def _event_identity_containers(index: int, event: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    containers: list[tuple[str, Mapping[str, Any]]] = [(f"pipeline_events[{index}]", event)]
    details = event.get("details")
    if not isinstance(details, Mapping):
        return containers
    identity = details.get("identity")
    if isinstance(identity, Mapping):
        containers.append((f"pipeline_events[{index}].details.identity", identity))
    containers.append((f"pipeline_events[{index}].details", details))
    for task_index, task in enumerate(_bounded_task_result_rows(details)):
        containers.append((f"pipeline_events[{index}].details.task_results[{task_index}]", task))
        task_identity = task.get("identity")
        if isinstance(task_identity, Mapping):
            containers.append(
                (f"pipeline_events[{index}].details.task_results[{task_index}].identity", task_identity)
            )
    for key in ("task_identity", "failed_task", "failed_task_identity"):
        value = details.get(key)
        if isinstance(value, Mapping):
            containers.append((f"pipeline_events[{index}].details.{key}", value))
    return containers

def _legacy_non_authoritative_state_row(expected: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    return bool(row) and not _state_row_has_authoritative_candidate_proof(expected, row)

def _state_row_has_authoritative_candidate_proof(
    expected: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    include_nested: bool = True,
) -> bool:
    row_values = _legacy_identity_values(row)
    expected_values = _legacy_identity_values(expected)
    if _state_values_have_authoritative_candidate_proof(row_values, expected_values):
        return True
    if not include_nested:
        return False
    for nested in _nested_state_identity_payloads(row):
        nested_values = _legacy_identity_values(nested)
        if _state_values_have_authoritative_candidate_proof(nested_values, expected_values):
            return True
    return False

def _state_values_have_authoritative_candidate_proof(
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    if not row_values:
        return False
    if _state_values_are_scoped_to_other_candidate(row_values, expected_values):
        return False
    if _state_values_have_complete_m23_identity(row_values, expected_values):
        return True
    if _state_values_have_candidate_scoped_m23_proof(row_values, expected_values):
        return True
    return _legacy_values_prove_same_candidate(row_values, expected_values)

def _state_row_is_scoped_to_other_candidate(expected: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    expected_values = _legacy_identity_values(expected)
    if _state_values_are_scoped_to_other_candidate(_legacy_identity_values(row), expected_values):
        return True
    return any(
        _state_values_are_scoped_to_other_candidate(_legacy_identity_values(nested), expected_values)
        for nested in _nested_state_identity_payloads(row)
    )

def _state_values_are_scoped_to_other_candidate(
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    for identity_field in STATE_CANDIDATE_SCOPED_PROOF_FIELDS:
        value = row_values.get(identity_field)
        expected = expected_values.get(identity_field)
        if identity_field == "run_id" and _stage_cycle_run_matches_candidate(value, expected_values):
            continue
        if identity_field == "run_id" and _shared_stage_cycle_run_matches_candidate(
            value,
            row_values,
            expected_values,
        ):
            continue
        if value not in (None, "") and expected not in (None, "") and value != expected:
            return True
    return False

def _state_values_have_complete_m23_identity(
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    for identity_field in PRODUCTION_IDENTITY_FIELDS:
        value = row_values.get(identity_field)
        expected = expected_values.get(identity_field)
        if value in (None, "") or expected in (None, "") or value != expected:
            return False
    return True

def _state_values_have_candidate_scoped_m23_proof(
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    return any(
        row_values.get(field) not in (None, "") and row_values.get(field) == expected_values.get(field)
        for field in STATE_CANDIDATE_SCOPED_PROOF_FIELDS
    )

def _legacy_values_prove_same_candidate(
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    if not row_values:
        return False
    for identity_field in ("model_id", "source", "cycle_time"):
        value = row_values.get(identity_field)
        expected_value = expected_values.get(identity_field)
        if value not in (None, "") and expected_value not in (None, "") and value != expected_value:
            return False
    run_id = row_values.get("run_id")
    expected_run_id = expected_values.get("run_id")
    if run_id not in (None, ""):
        if run_id == expected_run_id:
            return True
        if _shared_stage_cycle_run_matches_candidate(run_id, row_values, expected_values):
            return True
        if not _stage_cycle_run_matches_candidate(run_id, expected_values):
            return False
        return True
    source = row_values.get("source")
    cycle_time = row_values.get("cycle_time")
    model_id = row_values.get("model_id")
    if source in (None, "") or cycle_time in (None, ""):
        return False
    if source != expected_values.get("source") or cycle_time != expected_values.get("cycle_time"):
        return False
    return model_id in (None, "", expected_values.get("model_id"))


def _shared_stage_cycle_run_matches_candidate(
    run_id: str | None,
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    """Accept a cohort run only when the row independently binds the model.

    Multi-basin Slurm arrays intentionally share one source/cycle/stage run id.
    A bare shared run id is not candidate proof; the row must also carry the
    exact candidate model id so sibling-model state cannot bleed across rows.
    """

    if run_id in (None, "") or row_values.get("model_id") != expected_values.get("model_id"):
        return False
    source = str(expected_values.get("source") or "").lower()
    cycle_time = str(expected_values.get("cycle_time") or "")
    if not source or not cycle_time:
        return False
    try:
        compact_cycle = format_cycle_time(cycle_time)
    except (TypeError, ValueError):
        return False
    prefix = f"cycle_{source}_{compact_cycle}_"
    stage = str(run_id).removeprefix(prefix) if str(run_id).startswith(prefix) else ""
    if "_cohort_" in stage:
        stage, separator, member_digest = stage.partition("_cohort_")
        if not separator or re.fullmatch(r"[0-9a-f]{12}", member_digest) is None:
            return False
    return stage == "full" or stage in DOWNSTREAM_RESTART_STAGES

def _state_row_has_m23_comparison_fields(values: Mapping[str, str]) -> bool:
    return any(
        field in values
        for field in (
            *STATE_M23_COMPARISON_FIELDS,
            *PRODUCTION_EVIDENCE_CORRELATION_FIELDS,
        )
    )

def _state_row_has_m23_comparison_evidence(row: Mapping[str, Any]) -> bool:
    if _state_row_has_m23_comparison_fields(_legacy_identity_values(row)):
        return True
    return any(
        _state_row_has_m23_comparison_fields(_legacy_identity_values(nested))
        for nested in _nested_state_identity_payloads(row)
    )

def _nested_state_identity_payloads(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    payloads: list[Mapping[str, Any]] = []
    for key in ("identity", "task_identity", "failed_task", "failed_task_identity"):
        value = row.get(key)
        if isinstance(value, Mapping):
            payloads.append(value)
    details = row.get("details")
    if isinstance(details, Mapping):
        payloads.append(details)
        for key in ("identity", "task_identity", "failed_task", "failed_task_identity"):
            value = details.get(key)
            if isinstance(value, Mapping):
                payloads.append(value)
        for task in _bounded_task_result_rows(details):
            payloads.append(task)
            identity = task.get("identity")
            if isinstance(identity, Mapping):
                payloads.append(identity)
    return payloads

def _bounded_task_result_rows(details: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    task_sample = _bounded_task_result_sample(details)
    if task_sample is None:
        return []
    return task_sample[0]

def _bounded_task_result_sample(
    details: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], dict[str, Any]] | None:
    task_results = details.get("task_results")
    if not isinstance(task_results, Sequence) or isinstance(task_results, str | bytes | bytearray):
        return None
    task_rows: list[Mapping[str, Any]] = []
    observed_count = 0
    overflow = False
    for index, task in enumerate(task_results):
        observed_count = index + 1
        if index >= CANDIDATE_STATE_TASK_RESULT_LIMIT:
            overflow = True
            break
        if isinstance(task, Mapping):
            task_rows.append(dict(task))
    reported_total = _coerce_optional_nonnegative_int(details.get("task_results_total"))
    total = max(reported_total, observed_count) if reported_total is not None else observed_count
    included = len(task_rows)
    overflow = overflow or total > included
    metadata: dict[str, Any] = {
        "task_results_total": total,
        "task_results_included": included,
        "task_results_limit": CANDIDATE_STATE_TASK_RESULT_LIMIT,
        "task_results_overflow": overflow,
    }
    if overflow:
        metadata["task_results_omitted"] = max(total - included, 0)
    return task_rows, metadata

def _legacy_compatible_state_row(expected: Mapping[str, Any], row: Mapping[str, Any]) -> Mapping[str, Any]:
    row_values = _legacy_identity_values(row)
    expected_values = _legacy_identity_values(expected)
    if not _stage_cycle_run_matches_candidate(
        row_values.get("run_id"), expected_values
    ) and not _shared_stage_cycle_run_matches_candidate(
        row_values.get("run_id"),
        row_values,
        expected_values,
    ):
        return row
    payload = dict(row)
    payload.pop("run_id", None)
    identity = payload.get("identity")
    if isinstance(identity, Mapping):
        identity_payload = dict(identity)
        identity_payload.pop("run_id", None)
        payload["identity"] = identity_payload
    return payload

def _legacy_identity_values(payload: Mapping[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    aliases: dict[str, tuple[tuple[str, ...], ...]] = {
        "run_id": (("run_id",), ("identity", "run_id")),
        "model_id": (("model_id",), ("identity", "model_id")),
        "basin_id": (("basin_id",), ("identity", "basin_id")),
        "source": (("source",), ("source_id",), ("identity", "source"), ("identity", "source_id")),
        "cycle_time": (
            ("cycle_time",),
            ("cycle_time_utc",),
            ("identity", "cycle_time"),
            ("identity", "cycle_time_utc"),
        ),
        "basin_version_id": (("basin_version_id",), ("identity", "basin_version_id")),
        "river_network_version_id": (("river_network_version_id",), ("identity", "river_network_version_id")),
        "canonical_product_id": (("canonical_product_id",), ("identity", "canonical_product_id")),
        "forcing_version_id": (("forcing_version_id",), ("identity", "forcing_version_id")),
        "hydro_run_id": (("hydro_run_id",), ("identity", "hydro_run_id")),
        "published_manifest_id": (("published_manifest_id",), ("identity", "published_manifest_id")),
        "pipeline_job_id": (("pipeline_job_id",), ("identity", "pipeline_job_id")),
        "pipeline_event_id": (("pipeline_event_id",), ("identity", "pipeline_event_id")),
    }
    for identity_field, field_aliases in aliases.items():
        value = _first_nested_state_value(payload, field_aliases)
        if value in (None, ""):
            continue
        if identity_field == "source":
            try:
                value = normalize_source_id(str(value))
            except ValueError:
                value = str(value).strip()
        elif identity_field == "cycle_time":
            try:
                value = _format_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
            except ValueError:
                try:
                    value = _format_utc(datetime.strptime(str(value), "%Y%m%d%H").replace(tzinfo=UTC))
                except ValueError:
                    value = str(value).strip()
        else:
            value = str(value).strip()
        if value:
            values[identity_field] = value
    job_id = payload.get("job_id") or payload.get("entity_id")
    if "pipeline_job_id" not in values and job_id not in (None, "") and _looks_like_production_job_id(job_id):
        values["stage_job_id"] = str(job_id).strip()
    event_id = payload.get("event_id")
    if "pipeline_event_id" not in values and event_id not in (None, ""):
        values["stage_event_id"] = str(event_id).strip()
    return values

def _candidate_state_has_identity_mismatch(evidence: Mapping[str, Any]) -> bool:
    validation = evidence.get("production_identity_validation")
    return isinstance(validation, Mapping) and validation.get("status") == "mismatch"

def _state_jobs(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = state.get("pipeline_jobs") or state.get("jobs")
    max_jobs = _state_job_limit(state)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [dict(item) for item in value if isinstance(item, Mapping)][:max_jobs]
    single = state.get("pipeline_job") or state.get("job")
    if isinstance(single, Mapping):
        return [dict(single)]
    fields = {
        "job_id",
        "pipeline_job_id",
        "run_id",
        "cycle_id",
        "job_type",
        "slurm_job_id",
        "array_task_id",
        "model_id",
        "status",
        "pipeline_status",
        "job_status",
        "stage",
        "exit_code",
        "retry_count",
        "error_code",
        "error_message",
        "log_uri",
    }
    if any(key in state for key in fields):
        return [dict(state)]
    return []

def _job_is_cycle_scope_row(job: Mapping[str, Any]) -> bool:
    """Row-level cycle-scope test, shared by every derivation that scans the row list.

    Mirrors the journal's strict ``_is_model_less_cycle_scope_job`` semantics: cycle
    scope requires an empty ``model_id`` AND the ``cycle_<source>_<stamp>[_suffix]``
    run-id grammar.  ``model_id`` alone is not sufficient — model-less rows can also
    carry the candidate's OWN ``fcst_...`` run id, and treating those as cycle scope
    would silently discard the operator-pinned attempt number.  Scheduler state
    carries no source/cycle context, so the grammar is checked by prefix.

    It lives here rather than in ``scheduler_state_manual_retry`` (which re-exports it
    under the same name) because this module is the import floor: the stage-scoped
    attempt derivation below and ``scheduler_state_failure``'s candidate-scoped
    failed-stage resolver both need the same judgement, and one predicate with three
    consumers cannot drift the way three copies would (#1179 D5).
    """

    if job.get("model_id") not in (None, ""):
        return False
    return str(job.get("run_id") or "").startswith("cycle_")

def _state_events(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = state.get("pipeline_events") or state.get("events")
    max_events = _state_event_limit(state)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [dict(item) for item in value if isinstance(item, Mapping)][:max_events]
    return []

def _state_job_limit(state: Mapping[str, Any]) -> int:
    return max(_coerce_int(state.get("job_limit"), default=DEFAULT_CANDIDATE_STATE_JOB_LIMIT), 1)

def _state_event_limit(state: Mapping[str, Any]) -> int:
    return max(_coerce_int(state.get("event_limit"), default=DEFAULT_CANDIDATE_STATE_EVENT_LIMIT), 1)

def _canonical_downstream_stage(stage: str | None) -> str | None:
    if stage is None:
        return None
    normalized = DOWNSTREAM_STAGE_ALIASES.get(stage)
    if normalized in DOWNSTREAM_RESTART_STAGES:
        return normalized
    return None

def _state_retry_attempt(state: Mapping[str, Any], *, stage: str | None = None) -> int:
    """Return the retry attempt recorded for ``state``, scoped to ``stage`` when given.

    With ``stage`` supplied the answer is ``max(flat, stage-matching jobs)``:
    the state's own candidate-scoped ``retry_count`` is the floor, and ONLY jobs
    whose canonical stage matches contribute anything beyond it — via their
    durable ``_retry_<n>`` suffix attempt.  Non-matching jobs contribute nothing,
    because ``pipeline_jobs`` is the unfiltered cycle-wide list while the flat
    count is candidate-scoped; charging a cycle-scope download's persisted
    ``retry_count`` to the forecast budget would block candidates whose own
    forecast has not been retried once.  Stage identity comes from the job
    projection's authoritative ``stage`` field — never from job-id substrings,
    because production ids embed several stage tokens
    (``..._convert_model_0_forecast_retry_1_retry_2``).

    A stage-scoped read also honours the projection's
    ``stage_retry_attempt_floors`` (#1179).  What that makes
    truncation-invariant is the STAGE-MATCHING ROW SCAN component: for every
    canonical stage it returns what the untruncated job list would have derived.
    The candidate-level flat ``retry_count`` this maxes against is aggregated
    AFTER truncation and stays window-sensitive exactly as it was before #1179 —
    a cross-stage row's persisted count reaching this answer through the flat
    channel is pre-existing behaviour, tracked in #1579, not something the floors
    fix or promise.

    A stage that names no canonical downstream restart stage — ``download``, the
    one production cycle stage outside ``DOWNSTREAM_RESTART_STAGES`` — is derived
    the same way against the row's RAW authoritative stage name (#1298).  Before
    that arm existed such a stage short-circuited to the flat count, which the
    journal's clean-reservation invariant has reset to 0, so a candidate whose only
    live failure sat at a non-canonical stage lost the durable record of its
    consumed ``_retry_<n>`` identity and re-minted it.  Two boundaries of that arm:

    * it subtracts cycle-scope rows itself.  The canonical arm's exclusion is a
      side effect of stage matching, but a raw-name match would pair the
      candidate's own single-basin ``download`` row with the cohort's model-less
      one — the identity filter keeps source-cycle download rows on purpose — and
      charge the cohort's persisted counter to this candidate.
    * it is WINDOW-SENSITIVE: ``stage_retry_attempt_floors`` covers canonical
      stages only (#1179), so a non-canonical stage's maximum-attempt row
      truncated out of the ``job_limit`` window is not restored.  The floors key
      domain is deliberately not widened for it — the shapes that reach this arm
      (single-basin cycles, a handful of rows) sit far from the truncation.

    The canonical arm's derivation is unchanged byte for byte, including its
    existing count of model-less cohort rows at the same canonical stage.

    Without ``stage`` the flat-first order and the cross-job recorded-count max
    are preserved byte-for-byte for the evidence-owner / manual-retry consumers,
    and the floors never leak in: they are per-stage maxima over the UNFILTERED
    cycle-wide rows, exactly the cross-scope charge the stage argument exists to
    prevent.  The flat value never short-circuits the stage-scoped derivation: a
    real projected state ALWAYS carries a top-level ``retry_count`` (0 whenever
    the journal's clean-reservation invariant reset the forecast master row).
    """

    flat = _state_flat_retry_attempt(state)
    canonical_stage = _canonical_downstream_stage(stage)
    if canonical_stage is None:
        if stage not in (None, ""):
            return max(flat or 0, _state_non_canonical_stage_retry_attempt(state, str(stage)))
        if flat is not None:
            return flat
        return _state_job_retry_attempt(state, None)
    return max(flat or 0, _state_job_retry_attempt(state, canonical_stage))

def _state_non_canonical_stage_retry_attempt(state: Mapping[str, Any], stage: str) -> int:
    """Maximum attempt the CANDIDATE-scope rows at raw stage name ``stage`` carry (#1298).

    Same counting rule as ``_job_retry_attempt``'s matching branch — the higher of a
    row's recorded ``retry_count`` and its ``_retry_<n>`` id suffix — over the rows
    whose authoritative stage field equals ``stage`` literally.  No canonical
    normalization: the argument is by definition a stage the alias table does not
    name, so equality is the only identity available.  Cycle-scope rows are
    subtracted here; see ``_state_retry_attempt`` for why.
    """

    return max(
        (
            effective_retry_attempt(job.get("job_id"), _coerce_int(job.get("retry_count"), default=0))
            for job in _state_jobs(state)
            if _job_stage_name(job) == stage and not _job_is_cycle_scope_row(job)
        ),
        default=0,
    )

def _state_flat_retry_attempt(state: Mapping[str, Any]) -> int | None:
    for key in ("retry_attempt", "attempt", "retry_count"):
        value = state.get(key)
        if value not in (None, ""):
            return _coerce_int(value, default=0)
    return None

def _state_job_retry_attempt(state: Mapping[str, Any], canonical_stage: str | None) -> int:
    """Maximum attempt the state's rows carry, floored by the projection's carry-over.

    The row scan alone is only as complete as the ``job_limit`` window: the row
    holding a stage's maximum attempt may have been truncated away.  For a
    stage-scoped read the projection's ``stage_retry_attempt_floors`` restores
    that number (#1179) — the floors are built over the untruncated rows, so THIS
    component is truncation-invariant (the caller's flat aggregate is not; see
    ``_state_retry_attempt``).  A state that no projection produced carries no
    floors key and reads exactly as before, and a state whose identity filtering
    dropped a stage's every contributing row carries no floor for it either.
    """

    jobs = _state_jobs(state)
    scanned = max((_job_retry_attempt(job, canonical_stage) for job in jobs), default=0)
    if canonical_stage is None:
        return scanned
    return max(scanned, _state_stage_retry_attempt_floor(state, canonical_stage))

def _state_stage_retry_attempt_floor(state: Mapping[str, Any], canonical_stage: str) -> int:
    floors = state.get(STAGE_RETRY_ATTEMPT_FLOORS_KEY) or {}
    if not isinstance(floors, Mapping):
        return 0
    return _coerce_int(floors.get(canonical_stage), default=0)

def _job_retry_attempt(job: Mapping[str, Any], canonical_stage: str | None) -> int:
    recorded = _coerce_int(job.get("retry_count"), default=0)
    if canonical_stage is None:
        return recorded
    if _canonical_downstream_stage(_job_stage_name(job)) != canonical_stage:
        # ``pipeline_jobs`` is the UNFILTERED cycle-wide list: it carries
        # model-less cycle-scope rows (download / convert / forcing / parse /
        # state_save_qc / publish) whose ``retry_count`` the auto-retry service
        # durably persists.  Counting them would charge another scope's retries
        # to this candidate's stage budget.
        return 0
    return effective_retry_attempt(job.get("job_id"), recorded)

def _job_stage_name(job: Mapping[str, Any]) -> str | None:
    for key in ("stage", "job_type"):
        value = job.get(key)
        if value not in (None, ""):
            return str(value)
    return None

def stage_retry_attempt_floors(
    jobs: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, int], dict[str, list[dict[str, Any]]]]:
    """Per-canonical-stage maximum effective attempt over ``jobs``, and its sources (#1179).

    The candidate-state projection truncates ``pipeline_jobs`` to ``job_limit``
    by pure freshness, so in the reverse geometry — the ``*_forecast_retry_N``
    row OLDER than ``job_limit`` fresher rows of other stages — the only durable
    record of attempt N was dropped and the stage-scoped derivation silently read
    0, leaving #1173's L2 budget unbound.  The projection carries these floors
    across the truncation rather than retaining rows, so the row population, and
    every state key derived from it, stays exactly what pure freshness selects.

    Derivation must stay identical to the consumer side above
    (``_canonical_downstream_stage`` + ``_job_stage_name`` +
    ``effective_retry_attempt``), which is why this lives here and not in the
    projection module: that module's ``_STAGE_ALIASES`` is a different domain
    (it admits ``download`` and omits ``copyback``), and an attempt lives in a
    persisted ``retry_count`` — ``retry.py`` mints ``*_retry_active`` ids with
    ``retry_count=N`` — just as durably as in a ``_retry_<n>`` suffix.  A stage
    whose maximum is 0 is omitted: every row of it derives 0 anyway, so a zero
    floor would say nothing.

    The second return value records, per stage, the identity of EVERY row that
    reached that stage's maximum.  ``pipeline_jobs`` is the unfiltered cycle-wide
    list, so a floor may well come from a row that is not this candidate's; the
    identity filter re-runs its own row predicates over these records to narrow
    the floors with the row population (``scheduler_state_identity_filter``).
    Recording all the tied rows rather than the first is what makes that
    narrowing conservative: the floor survives while ANY contributor does.
    """

    floors: dict[str, int] = {}
    sources: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        stage = _canonical_downstream_stage(_job_stage_name(job))
        if stage is None:
            continue
        attempt = effective_retry_attempt(job.get("job_id"), job.get("retry_count"))
        current = floors.get(stage, 0)
        # An equal attempt is a TIE and joins the contributor list; a zero one
        # never enters, so an all-zero stage stays out of the mapping.
        if attempt < current or attempt <= 0:
            continue
        if attempt > current:
            floors[stage] = attempt
            sources[stage] = []
        sources[stage].append(_stage_retry_attempt_floor_source(job))
    return floors, sources

def _stage_retry_attempt_floor_source(job: Mapping[str, Any]) -> dict[str, Any]:
    """Project a contributing row onto the fields the row filters read.

    A whitelist rather than the whole row: these records ride along in every
    candidate state, and the cohort rows that most often carry a floor are the
    same ones the journal already compacts for the evidence-size guard
    (``file_orchestration_journal._compact_cycle_scope_job``).  The nested
    ``identity`` payload is kept because ``_legacy_identity_values`` reads its
    fields as aliases of the row-level ones.
    """

    record = {key: job[key] for key in STAGE_RETRY_ATTEMPT_FLOOR_SOURCE_FIELDS if key in job}
    identity = job.get("identity")
    if isinstance(identity, Mapping):
        nested = {key: identity[key] for key in STAGE_RETRY_ATTEMPT_FLOOR_SOURCE_FIELDS if key in identity}
        if nested:
            record["identity"] = nested
    return record

def _state_retry_limit(state: Mapping[str, Any]) -> int | None:
    for key in ("retry_limit", "max_retries"):
        value = state.get(key)
        if value not in (None, ""):
            return _coerce_int(value, default=0)
    from services.orchestrator.scheduler_state_types import DEFAULT_RETRY_LIMIT

    return DEFAULT_RETRY_LIMIT

def _state_overflow_evidence(state: Mapping[str, Any]) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "job_limit": _state_job_limit(state),
        "event_limit": _state_event_limit(state),
    }
    overflow = False
    for count_key, limit_key, output_key in (
        ("pipeline_jobs_total", "job_limit", "pipeline_jobs"),
        ("pipeline_events_total", "event_limit", "pipeline_events"),
    ):
        count = state.get(count_key)
        if count in (None, ""):
            continue
        count_value = _coerce_int(count, default=0)
        limit_value = int(evidence[limit_key])
        evidence[f"{output_key}_total"] = count_value
        evidence[f"{output_key}_returned"] = min(count_value, limit_value)
        if count_value > limit_value:
            evidence[f"{output_key}_overflow"] = True
            overflow = True
    if state.get("state_truncated") is True:
        overflow = True
        evidence["state_truncated"] = True
    if not overflow:
        return {}
    evidence["bounded"] = True
    evidence["overflow"] = True
    evidence["reason"] = "candidate_state_row_limit_applied"
    return evidence

def _job_state_evidence(job: Mapping[str, Any]) -> dict[str, Any]:
    kept = {
        key: job.get(key)
        for key in (
            "job_id",
            "pipeline_job_id",
            "pipeline_event_id",
            "run_id",
            "cycle_id",
            "job_type",
            "slurm_job_id",
            "array_task_id",
            "model_id",
            "basin_id",
            "source",
            "source_id",
            "cycle_time",
            "basin_version_id",
            "river_network_version_id",
            "canonical_product_id",
            "forcing_version_id",
            "hydro_run_id",
            "published_manifest_id",
            "status",
            "stage",
            "exit_code",
            "retry_count",
            "error_code",
            "error_message",
            "log_uri",
            "repair_status",
            "superseded_by_job_id",
            "repaired_by_job_id",
            "repairs_job_id",
            "repairs_job_ids",
            "active_blocker",
        )
        if key in job and job.get(key) is not None
    }
    return _evidence_safe(kept)

def _optional_mapping_state(value: Any, *, defaults: Mapping[str, Any]) -> dict[str, Any] | None:
    payload = dict(value) if isinstance(value, Mapping) else {}
    for key, fallback in defaults.items():
        if fallback not in (None, ""):
            payload.setdefault(key, fallback)
    payload = {key: val for key, val in payload.items() if val not in (None, "")}
    return _evidence_safe(payload) if payload else None

def _state_status(state: Mapping[str, Any], *keys: str) -> str | None:
    explicit_key_seen = False
    for key in keys:
        explicit_key_seen = explicit_key_seen or key in state
        value = state.get(key)
        if value not in (None, ""):
            return str(value)
    if explicit_key_seen:
        return None
    for job in reversed(_state_jobs(state)):
        for key in keys:
            value = job.get(key)
            if value not in (None, ""):
                return str(value)
    return None

def _state_output_uri(state: Mapping[str, Any]) -> str | None:
    for container_key in ("hydro_run", "outputs", "runtime_outputs"):
        value = state.get(container_key)
        if isinstance(value, Mapping) and value.get("output_uri") not in (None, ""):
            return str(value["output_uri"])
    value = state.get("output_uri") or state.get("durable_output_uri")
    return str(value) if value not in (None, "") else None

def _state_active_jobs(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    explicit = state.get("active_slurm_jobs")
    if isinstance(explicit, Sequence) and not isinstance(explicit, str | bytes | bytearray):
        return [
            _evidence_safe(dict(job))
            for job in explicit
            if isinstance(job, Mapping) and _job_has_real_slurm_binding(job)
        ]
    active: list[dict[str, Any]] = []
    for job in _state_jobs(state):
        status = str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
        if _job_has_real_slurm_binding(job) and status in ACTIVE_PIPELINE_STATUSES:
            active.append(_job_state_evidence(job))
    return active

def _job_has_real_slurm_binding(job: Mapping[str, Any]) -> bool:
    slurm_job_id = str(job.get("slurm_job_id") or "")
    return bool(slurm_job_id and slurm_job_id.lower() != "local") or job.get("array_task_id") not in (None, "")

def _job_is_unsubmitted_auto_retry_placeholder(job: Mapping[str, Any]) -> bool:
    status = str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
    if status not in {"pending", "submission_failed"}:
        return False
    if job.get("manual_retry_marker") is True:
        return False
    if job.get("slurm_job_id") not in (None, "") or job.get("array_task_id") not in (None, ""):
        return False
    retry_count = _coerce_int(job.get("retry_count"), default=0)
    if retry_count <= 0:
        return False
    job_id = str(job.get("job_id") or "")
    return "_retry_" in job_id

def _state_has_only_unsubmitted_auto_retry_placeholders(state: Mapping[str, Any]) -> bool:
    jobs = _state_jobs(state)
    active_jobs = [
        job
        for job in jobs
        if str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
        in ACTIVE_PIPELINE_STATUSES
    ]
    return bool(active_jobs) and all(_job_is_unsubmitted_auto_retry_placeholder(job) for job in active_jobs)

def _pipeline_job_is_repaired_stage_evidence(job: Mapping[str, Any]) -> bool:
    return job.get("repair_status") == "repaired" or job.get("active_blocker") is False

def _event_has_failure_signal(event: Mapping[str, Any]) -> bool:
    details = event.get("details")
    details_mapping = details if isinstance(details, Mapping) else {}
    status = str(
        event.get("status_to")
        or details_mapping.get("status_to")
        or details_mapping.get("status")
        or details_mapping.get("state")
        or ""
    )
    return status in FAILED_PIPELINE_STATUSES or details_mapping.get("error_code") not in (None, "")

def _is_source_cycle_download_stage(stage: str | None) -> bool:
    return stage in {"download", "download_source_cycle", "download_gfs"}

def _state_task_payload_failed(payload: Mapping[str, Any]) -> bool:
    status = str(payload.get("status") or payload.get("state") or "")
    return status not in {"", "succeeded", "complete", "published"}
