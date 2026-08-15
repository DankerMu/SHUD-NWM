from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from packages.common.object_store import LocalObjectStore, ObjectStoreError
from packages.common.source_identity import normalize_source_id
from packages.common.storage import validate_object_path
from services.orchestrator.retry import classify_failure
from services.orchestrator.scheduler_init_state_match import EVIDENCE_REDACTION_PLACEHOLDERS
from services.orchestrator.scheduler_state_common import (
    _evidence_safe,
    _first_state_datetime,
    _forecast_cycle_manifest_uri,
    _is_raw_manifest_object_uri,
    _object_manifest_is_missing,
)
from services.orchestrator.scheduler_state_manual_retry import (
    _event_is_manual_retry_marker,
    _manual_retry_new_attempt,
    _manual_retry_payload,
)
from services.orchestrator.scheduler_state_rows import (
    _bounded_task_result_rows,
    _canonical_downstream_stage,
    _event_has_failure_signal,
    _is_source_cycle_download_stage,
    _pipeline_job_is_repaired_stage_evidence,
    _state_events,
    _state_has_only_unsubmitted_auto_retry_placeholders,
    _state_jobs,
    _state_output_uri,
    _state_retry_attempt,
    _state_retry_limit,
    _state_status,
)
from services.orchestrator.scheduler_state_types import (
    ACTIVE_PIPELINE_STATUSES,
    FAILED_PIPELINE_STATUSES,
    NATIVE_SHUD_STAGE_ALIASES,
    TERMINAL_PIPELINE_SUCCESS_STATUSES,
    TRANSIENT_RETRY_REASON_CODES,
    SchedulerCandidateLike,
)
from workers.data_adapters.base import format_cycle_time

_COPYBACK_REQUIRED_RESTART_STAGES = {"copyback"}
#: ``unsafe_reason`` for a probe the object store refused to run (#1365 round-1):
#: a symlinked/non-regular probe target, a stale or unreadable filesystem handle,
#: an unsafe store root.  Compared against by the sidecar tier, so it is a
#: cross-site contract rather than a local literal.
_ARTIFACT_PROBE_ERROR_REASON = "artifact_probe_error"


def _failed_stage(state: Mapping[str, Any]) -> str | None:
    for key in ("failed_stage", "stage", "restart_stage"):
        value = state.get(key)
        if value not in (None, ""):
            return str(value)
    for job in reversed(_state_jobs(state)):
        if _pipeline_job_is_repaired_stage_evidence(job):
            continue
        status = str(job.get("status") or "")
        if status in FAILED_PIPELINE_STATUSES and job.get("stage") not in (None, ""):
            return str(job["stage"])
    return None

def _durable_shud_output_exists(state: Mapping[str, Any]) -> bool:
    if state.get("durable_shud_output_exists") is not None:
        return bool(state.get("durable_shud_output_exists"))
    hydro_status = _state_status(state, "hydro_status", "hydro_run_status")
    if hydro_status in {"succeeded", "parsed", "published", "complete"}:
        return True
    if _state_output_uri(state):
        for job in _state_jobs(state):
            stage = str(job.get("stage") or job.get("job_type") or "")
            status = str(job.get("status") or "")
            if stage in NATIVE_SHUD_STAGE_ALIASES and status in TERMINAL_PIPELINE_SUCCESS_STATUSES:
                return True
    return False

def _force_native_shud_rerun(state: Mapping[str, Any]) -> bool:
    return bool(state.get("force_native_shud_rerun") or state.get("force_rerun") or state.get("force_shud_rerun"))

def _cold_start_quarantined_failure(failure: Mapping[str, Any], prior_failure: str | None = None) -> bool:
    reason = prior_failure or failure.get("prior_failure_reason") or failure.get("reason_code")
    return str(reason or "").upper() == "COLD_START_QUARANTINED"

#: #1313 D2 -- THE single permanence judgement source for the db-free decision
#: ladder, shaped as a ``remedy category x classification`` table.  Every
#: pre-guard evidence channel that is about to overwrite a permanent failure
#: classification names the remedy it would apply; the judge refuses ONLY the
#: classifications that PROVE that remedy cannot address the cause.
#:
#: Everything else stays open on purpose.  The geometry-gated channels have
#: already established causality structurally (a raw manifest probed missing
#: after a previously successful download; a repair download newer than the
#: failed job), so unknown-default codes such as ``SLURM_JOB_FAILED`` and
#: input-defect codes such as ``INVALID_MANIFEST`` keep their existing repair
#: path -- the production repair remedy is NOT retired.
_REMEDY_NON_CAUSAL_CLASSIFIERS = frozenset(
    {
        # OUT_OF_MEMORY: re-running the same ``memory_gb`` reproduces it.
        "resource_configuration",
        # POLICY_BLOCKED / PERMISSION_DENIED / TEMPLATE_NOT_ALLOWED (retry.py:183).
        "policy_blocked",
    }
)
#: The code arm is NOT foldable into the classifier arm.  ``classifier`` is a
#: state-overridable transit key: ``_failure_policy_payload`` honours an explicit
#: ``classifier`` / ``failure_classifier`` on the state, and the identity filter
#: whitelists it, so a state carrying ``classifier: "unknown_failure"`` together
#: with ``error_code: OUT_OF_MEMORY`` would walk straight past a classifier-only
#: judge.  ``_model_package_refresh_retry_evidence`` has carried both arms since
#: #1161 for exactly this reason; neither arm may be dropped.
_REMEDY_NON_CAUSAL_CODES = frozenset({"OUT_OF_MEMORY"})
#: ``changed_model_package`` keeps the #1161 refusal list verbatim (zero semantic
#: change is the acceptance line, #1313 D2): a refreshed package genuinely can
#: clear a policy/template rejection -- the template ships inside the package --
#: so that remedy is not proven non-causal for the ``policy_blocked`` class the
#: way an input re-ingestion is.
_CHANGED_MODEL_PACKAGE_NON_CAUSAL_CLASSIFIERS = frozenset({"resource_configuration"})
_REMEDY_NON_CAUSAL_CLASSIFIER_TABLE: dict[str, frozenset[str]] = {
    "raw_input_reingestion": _REMEDY_NON_CAUSAL_CLASSIFIERS,
    "changed_model_package": _CHANGED_MODEL_PACKAGE_NON_CAUSAL_CLASSIFIERS,
}


def _remedy_permits_permanent_failure(failure: Mapping[str, Any], *, remedy: str) -> bool:
    """Whether ``remedy`` may still be applied to an already-permanent failure.

    ``True`` means "this classification does not prove the remedy irrelevant";
    the calling channel keeps its existing behaviour.  ``False`` means the
    channel must not emit a retry decision, and the ladder continues to the
    remaining channels (a genuinely changed model package may still claim the
    candidate) and, absent another legitimate claim, to the permanent-failure
    guard.
    """

    classifiers = _REMEDY_NON_CAUSAL_CLASSIFIER_TABLE.get(remedy, _REMEDY_NON_CAUSAL_CLASSIFIERS)
    if str(failure.get("classifier") or "") in classifiers:
        return False
    if str(failure.get("reason_code") or "").upper() in _REMEDY_NON_CAUSAL_CODES:
        return False
    return True


def _failure_policy_payload(
    state: Mapping[str, Any],
    *,
    default_error_code: str | None = None,
    manual: bool = False,
) -> dict[str, Any]:
    error_code = _state_error_code(state) or default_error_code or "UNKNOWN_FAILURE"
    stage = _failed_stage(state)
    attempt = _state_retry_attempt(state, stage=stage)
    retry_limit = _state_retry_limit(state)
    classification = classify_failure(error_code, attempt=attempt, retry_limit=retry_limit, manual=manual)
    explicit_classifier = state.get("failure_classifier") or state.get("classifier")
    if explicit_classifier not in (None, ""):
        classification["classifier"] = str(explicit_classifier)
    # #1313 D5: the top-level ``retryable`` key is a state-overridable transit key
    # (identity-filter whitelisted, no production writer today).  It may only
    # REASSERT a retryability the classification already grants -- it can never
    # whiten a permanent code back into an automatic retry.
    if state.get("retryable") is True and classification["retryable"]:
        classification["retryable"] = True
        classification["permanent"] = False
    if state.get("permanent") is True:
        classification["retryable"] = False
        classification["permanent"] = True
    return {
        **classification,
        "error_message": _state_error_message(state),
        "stage": stage,
        "task_identity": _state_task_identity(state),
    }

def _state_error_code(state: Mapping[str, Any]) -> str | None:
    for key in ("error_code", "reason_code", "failure_reason", "last_error", "previous_error"):
        value = state.get(key)
        if value not in (None, ""):
            return str(value)
    hydro_run = state.get("hydro_run")
    if isinstance(hydro_run, Mapping):
        for key in ("error_code", "reason_code", "failure_reason", "last_error", "previous_error"):
            value = hydro_run.get(key)
            if value not in (None, ""):
                return str(value)
    for job in reversed(_state_jobs(state)):
        if _pipeline_job_is_repaired_stage_evidence(job):
            continue
        value = job.get("error_code") or job.get("reason_code")
        if value not in (None, ""):
            return str(value)
    for event in reversed(_state_events(state)):
        details = event.get("details")
        if isinstance(details, Mapping):
            value = details.get("error_code") or details.get("last_error") or details.get("previous_error")
            if value not in (None, ""):
                return str(value)
    return None

def _state_error_message(state: Mapping[str, Any]) -> str | None:
    for key in ("error_message", "message"):
        value = state.get(key)
        if value not in (None, ""):
            return str(_evidence_safe(str(value)))
    hydro_run = state.get("hydro_run")
    if isinstance(hydro_run, Mapping):
        for key in ("error_message", "message"):
            value = hydro_run.get(key)
            if value not in (None, ""):
                return str(_evidence_safe(str(value)))
    for job in reversed(_state_jobs(state)):
        if _pipeline_job_is_repaired_stage_evidence(job):
            continue
        value = job.get("error_message")
        if value not in (None, ""):
            return str(_evidence_safe(str(value)))
    return None

def _state_task_identity(state: Mapping[str, Any]) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    for key in ("task_id", "array_task_id", "original_task_id", "stage", "job_id", "slurm_job_id"):
        value = state.get(key)
        if value not in (None, ""):
            identity[key] = value
    if identity:
        return _evidence_safe(identity)
    for event in reversed(_state_events(state)):
        details = event.get("details")
        if not isinstance(details, Mapping):
            continue
        for key in ("task_identity", "failed_task", "failed_task_identity"):
            value = details.get(key)
            if isinstance(value, Mapping):
                for nested_key in ("task_id", "array_task_id", "original_task_id", "stage", "job_id", "slurm_job_id"):
                    nested_value = value.get(nested_key)
                    if nested_value not in (None, ""):
                        identity[nested_key] = nested_value
                if identity:
                    return _evidence_safe(identity)
        for task in _bounded_task_result_rows(details):
            status = str(task.get("status") or task.get("state") or "")
            if status in {"succeeded", ""}:
                continue
            identity["array_task_id"] = task.get("array_task_id", task.get("task_id"))
            identity["task_id"] = task.get("task_id", task.get("array_task_id"))
            if details.get("stage") not in (None, ""):
                identity["stage"] = details.get("stage")
            if task.get("slurm_job_id") not in (None, ""):
                identity["slurm_job_id"] = task.get("slurm_job_id")
            return _evidence_safe(identity)
    for job in reversed(_state_jobs(state)):
        if _pipeline_job_is_repaired_stage_evidence(job):
            continue
        for key in ("array_task_id", "stage", "job_id", "slurm_job_id"):
            value = job.get(key)
            if value not in (None, ""):
                identity[key] = value
        if identity:
            return _evidence_safe(identity)
    return {}

def _permanent_reason(state: Mapping[str, Any], failure: Mapping[str, Any]) -> str:
    pipeline_status = _state_status(state, "pipeline_status", "job_status", "status")
    if pipeline_status == "permanently_failed":
        return "permanent_failure_guard"
    if failure.get("classifier") == "policy_blocked":
        return "policy_blocked"
    if failure.get("limit_exhausted") and failure.get("retryable") is False:
        if str(failure.get("reason_code") or "") in TRANSIENT_RETRY_REASON_CODES:
            return "retry_limit_exhausted"
    return "permanent_failure_guard"

def _prior_failure_reason(state: Mapping[str, Any]) -> str | None:
    for key in ("prior_failure_reason", "previous_error", "last_error", "error_code"):
        value = state.get(key)
        if value not in (None, ""):
            return str(value)
    for event in reversed(_state_events(state)):
        details = event.get("details")
        if isinstance(details, Mapping):
            value = details.get("prior_failure_reason") or details.get("previous_error") or details.get("last_error")
            if value not in (None, ""):
                return str(value)
    return None

def _downstream_retry_evidence(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not _durable_shud_output_exists(state):
        return None
    failed_stage = _canonical_downstream_stage(_failed_stage(state))
    if failed_stage is None:
        return None
    if _force_native_shud_rerun(state):
        return None
    # #1313 D4: the ``{STAGE}_FAILED`` default below is READER-SYNTHESIZED -- it is
    # fabricated here when the state records no error code at all, and is therefore
    # not evidence under the spec's unknown-code clause.  Whether the code the
    # classification rests on was genuinely recorded decides which domain the
    # downstream-resume judgement applies.
    recorded_error_code = _state_error_code(state)
    failure = _failure_policy_payload(state, default_error_code=f"{failed_stage.upper()}_FAILED")
    if _cold_start_quarantined_failure(failure):
        return None
    if _downstream_failure_restartable(failure, code_recorded=recorded_error_code is not None):
        failure = {
            **failure,
            "retryable": True,
            "permanent": False,
            "limit_exhausted": False,
        }
    if failure["permanent"]:
        return None
    return {
        **base_evidence,
        "decision": "retry_downstream",
        "reason": "resume_downstream_after_durable_shud",
        "restart_stage": failed_stage,
        "restart_from_stage": failed_stage,
        "native_shud_resubmitted": False,
        "durable_shud_output_reused": True,
        "durable_output_uri": _state_output_uri(state),
        "force_native_shud_rerun": False,
        "failure": failure,
        "retry_policy": {
            "automatic_retry_allowed": failure["retryable"],
            "manual_retry_required": failure["permanent"],
            "attempt": failure["attempt"],
            "retry_limit": failure["retry_limit"],
        },
    }

_DOWNSTREAM_FORECAST_OUTPUT_DEPENDENT_STAGES = {"parse", "state_save_qc", "publish", "copyback"}
_MISSING_FORECAST_OUTPUT_RECOMPUTE_CODES = {
    "NODE_FAILURE",
    "OUT_OF_MEMORY",
    "PREEMPTED",
    "SLURM_TIMEOUT",
    "STATE_SAVE_QC_TASK_FAILED",
    "PARSE_TASK_FAILED",
    "PUBLISH_TASK_FAILED",
}


def _missing_forecast_output_recompute_evidence(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not _state_has_failure_signal(state):
        return None
    if _durable_shud_output_exists(state):
        return None
    failed_stage = _canonical_downstream_stage(_failed_stage(state))
    if failed_stage not in _DOWNSTREAM_FORECAST_OUTPUT_DEPENDENT_STAGES:
        return None
    failure = _failure_policy_payload(state, default_error_code=f"{failed_stage.upper()}_FAILED")
    # #1313 D2: this channel declares ``remedy="exempt"`` and deliberately does NOT
    # consult the shared permanence judgement.  Recomputing an ABSENT forecast
    # output is not a same-configuration rerun of the failed stage, so gating it on
    # its own approved code set (which includes OUT_OF_MEMORY) is the standing
    # ruling from #1161, carved out explicitly in the spec delta.
    reason_code = str(failure.get("reason_code") or "").upper()
    if reason_code not in _MISSING_FORECAST_OUTPUT_RECOMPUTE_CODES and reason_code not in TRANSIENT_RETRY_REASON_CODES:
        return None
    return {
        **base_evidence,
        "decision": "retry_missing_forecast_output",
        "reason": "recompute_forecast_after_missing_output",
        "stage": failed_stage,
        "restart_stage": "forecast",
        "restart_from_stage": "forecast",
        "native_shud_resubmitted": True,
        "durable_shud_output_reused": False,
        "force_native_shud_rerun": True,
        "missing_upstream": {
            "artifact_type": "forecast_output",
            "durable_shud_output_exists": False,
            "failed_downstream_stage": failed_stage,
        },
        "failure": {
            **failure,
            "classifier": "missing_forecast_output_recompute",
            "retryable": True,
            "permanent": False,
            "limit_exhausted": False,
        },
        "retry_policy": {
            "automatic_retry_allowed": True,
            "manual_retry_required": False,
            "attempt": failure["attempt"],
            "retry_limit": failure["retry_limit"],
            "override_reason": "missing_forecast_output_recompute",
        },
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
        },
    }


def _missing_upstream_forecast_artifact_evidence(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
    planned_retry: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return ``(blocker_evidence_or_None, forcing_provenance_annotation_or_None)``.

    The annotation is an out-channel: the forcing provenance tier is observed even
    when the guard does NOT block, so the decision the scheduler ultimately emits
    can name the tier that witnessed (or failed to witness) the package (#1203).
    """

    if not isinstance(planned_retry, Mapping):
        return None, None
    restart_stage = _canonical_downstream_stage(
        str(planned_retry.get("restart_stage") or planned_retry.get("restart_from_stage") or "")
    )
    if restart_stage is None:
        return None, None

    forcing_provenance: dict[str, Any] | None = None
    if restart_stage == "forecast":
        forcing_uri = _first_artifact_uri(
            state,
            (
                "forcing_package_uri",
                "forcing_uri",
                "package_uri",
                "forcing_package_path",
            ),
        )
        if forcing_uri in (None, "") or _is_withheld_uri_placeholder(forcing_uri):
            # A redaction placeholder is a WITHHELD reference, not a package
            # reference: the public-read boundary rewrites every s3/published-shaped
            # ``*_uri`` the journal read materializes into ``[object-uri]``
            # (``file_orchestration_journal._sanitize_public_field``).  Probing it
            # would report "missing" for a package nobody looked for, and would
            # also shadow the sidecar tier, so it takes the recovery path exactly
            # like an absent reference.  The probe itself is never taught about
            # placeholders and the redaction boundary is never bypassed (#1203
            # round-1 C1).
            sidecar = _forcing_sidecar_provenance(candidate)
            if not sidecar.witness:
                # Tier 1 (journal row), tier 2 (journal direct file) and tier 3
                # (object-store sidecar) all failed to witness provenance: this is
                # "cannot determine", NOT "package determined absent".  Still
                # fail-closed, but with a reason an operator can act on.
                forcing_provenance = {"source": "absent", "tier_status": sidecar.status}
                return (
                    _artifact_blocker_evidence(
                        candidate,
                        base_evidence,
                        planned_retry,
                        reason="forcing_version_row_absent",
                        error_code="FORCING_VERSION_ROW_ABSENT",
                        artifact_type="forcing_package_uri",
                        artifact_uri=None,
                        artifact_exists=False,
                        forcing_provenance=forcing_provenance,
                    ),
                    forcing_provenance,
                )
            # The producer's ``forcing_package_uri`` is a DIRECTORY uri, which the
            # object-path validator rejects (5 segments) and the probe would then
            # report as missing.  The probe object is therefore the package
            # MANIFEST FILE key derived from THIS candidate's own sidecar key
            # directory -- never the directory-shaped package uri, and never the
            # record's recorded manifest uri taken verbatim (that one is evidence
            # only: a producer/scheduler prefix drift would make it unresolvable,
            # and a copied sidecar could point at a foreign manifest and fail open,
            # #1203 round-1 V2-C2).
            forcing_provenance = {
                "source": "object_store_sidecar",
                "probe": "manifest",
                "package_uri": _evidence_safe(sidecar.package_uri),
                "manifest_uri": _evidence_safe(sidecar.manifest_uri),
                "probe_key": _evidence_safe(sidecar.manifest_probe_key),
                "forcing_version_id": _evidence_safe(sidecar.forcing_version_id),
            }
            sidecar_missing, sidecar_unsafe_reason = _artifact_uri_missing_status(
                candidate,
                str(sidecar.manifest_probe_key),
            )
            if sidecar_unsafe_reason == _ARTIFACT_PROBE_ERROR_REASON:
                # The probe could not read its object (a symlinked manifest leaf, an
                # NFS ESTALE/EIO stat).  Since #1365 round-1 the probe CONTAINS that
                # fault itself (``_ARTIFACT_PROBE_ERROR_REASON``) instead of letting
                # an ``ObjectStoreError`` escape and abort the whole scheduler pass,
                # so this leg reads the contained reason rather than catching an
                # exception -- and keeps this tier's richer evidence.
                #
                # An unreadable probe object is "cannot determine", NOT "package
                # determined absent" (#1203 round-2 V5-C2): routing it to
                # ``missing_forcing_package_uri`` would send the operator to the
                # exact-cycle forcing rebuild, which cannot clear a symlinked leaf,
                # an ESTALE handle, or a permission fault.  It therefore lands on
                # the same row-absent blocker as every other no-witness leg --
                # still fail-closed, still repair-eligible, with the tier detail
                # carried so the runbook can route it to "rebuild is ineffective".
                forcing_provenance = {
                    "source": "absent",
                    "tier_status": "sidecar_manifest_probe_error",
                }
                return (
                    _artifact_blocker_evidence(
                        candidate,
                        base_evidence,
                        planned_retry,
                        reason="forcing_version_row_absent",
                        error_code="FORCING_VERSION_ROW_ABSENT",
                        artifact_type="forcing_package_uri",
                        artifact_uri=None,
                        artifact_exists=False,
                        forcing_provenance=forcing_provenance,
                    ),
                    forcing_provenance,
                )
            if sidecar_missing:
                forcing_provenance = {**forcing_provenance, "artifact_exists": False}
                return (
                    _artifact_blocker_evidence(
                        candidate,
                        base_evidence,
                        planned_retry,
                        reason="missing_forcing_package_uri",
                        error_code="FORCING_PACKAGE_URI_MISSING",
                        artifact_type="forcing_package_uri",
                        artifact_uri=sidecar.package_uri,
                        artifact_exists=False,
                        unsafe_reason=sidecar_unsafe_reason,
                        forcing_provenance=forcing_provenance,
                    ),
                    forcing_provenance,
                )
            forcing_provenance = {**forcing_provenance, "artifact_exists": True}
        else:
            forcing_provenance = _journal_forcing_provenance(state, forcing_uri)
            # Same ruling as the sidecar tier, now on the journal/direct tiers
            # (#1365): the producer's ``forcing_package_uri`` names a package
            # PREFIX, which the object-path validator does not admit as a file key,
            # so probing it verbatim reported "missing" for every physically present
            # production package.  The probe object becomes the derived manifest
            # FILE key; the blocker keeps the recorded package uri as
            # ``artifact_uri`` (the derived key is evidence, never the reference).
            forcing_probe_uri = (
                _package_manifest_probe_uri(forcing_uri)
                if _needs_package_manifest_witness(forcing_uri)
                else forcing_uri
            )
            if forcing_provenance is not None:
                # Named unconditionally, mirroring the sidecar tier's evidence
                # shape: an operator reading a blocker must be able to tell WHAT
                # was probed without re-deriving it.  ``package_uri`` means the
                # recorded reference was already a file key and was probed as-is.
                forcing_provenance = {
                    **forcing_provenance,
                    "probe": "manifest" if forcing_probe_uri != forcing_uri else "package_uri",
                    "probe_key": _evidence_safe(forcing_probe_uri),
                }
            forcing_missing, forcing_unsafe_reason = _artifact_uri_missing_status(candidate, forcing_probe_uri)
            if forcing_missing:
                return (
                    _artifact_blocker_evidence(
                        candidate,
                        base_evidence,
                        planned_retry,
                        reason="missing_forcing_package_uri",
                        error_code="FORCING_PACKAGE_URI_MISSING",
                        artifact_type="forcing_package_uri",
                        artifact_uri=forcing_uri,
                        artifact_exists=False,
                        unsafe_reason=forcing_unsafe_reason,
                        forcing_provenance=forcing_provenance,
                    ),
                    forcing_provenance,
                )

    copyback_uri = _first_artifact_uri(
        state,
        (
            "copyback_source_uri",
            "copyback_source",
            "copyback_source_path",
            "copyback_uri",
        ),
    )
    copyback_required = restart_stage in _COPYBACK_REQUIRED_RESTART_STAGES or _copyback_source_required(state)
    if copyback_uri not in (None, ""):
        # #1365 D3: the copyback leg shares the probe, so the object branch's
        # root-unconfigured fail-closed ruling
        # (``unsafe_reason="object_store_root_unconfigured"``) applies to it
        # identically.  The forcing tiers' package-prefix witness derivation does
        # NOT: a copyback source directory has no canonical witness filename
        # (``forcing_package.json`` is forcing-producer domain), and the repo has
        # no production writer of ``copyback_source_uri`` to define one, so a
        # fabricated witness key is not an option here.
        #
        # Exact consequence (round-1 cand-03): a copyback uri at the pattern's own
        # depth (``runs/<run_id>/output/``) is refused by the validator and stays
        # fail-closed via the probe's ``ValueError`` leg, but a DEEPER directory
        # uri (``runs/<run_id>/output/<basin>/``) is admitted as a key and stats the
        # directory itself as present.  That deeper-directory fail-open predates
        # #1365 and is unchanged here -- it is routed as a separate follow-up.
        copyback_missing, copyback_unsafe_reason = _artifact_uri_missing_status(candidate, copyback_uri)
    else:
        copyback_missing, copyback_unsafe_reason = True, None
    if copyback_uri not in (None, "") and copyback_missing:
        return (
            _artifact_blocker_evidence(
                candidate,
                base_evidence,
                planned_retry,
                reason="missing_copyback_source",
                error_code="COPYBACK_SOURCE_MISSING",
                artifact_type="copyback_source",
                artifact_uri=copyback_uri,
                artifact_exists=False,
                unsafe_reason=copyback_unsafe_reason,
                forcing_provenance=forcing_provenance,
            ),
            forcing_provenance,
        )
    if copyback_uri in (None, "") and copyback_required:
        return (
            _artifact_blocker_evidence(
                candidate,
                base_evidence,
                planned_retry,
                reason="missing_copyback_source",
                error_code="COPYBACK_SOURCE_MISSING",
                artifact_type="copyback_source",
                artifact_uri=None,
                artifact_exists=False,
                forcing_provenance=forcing_provenance,
            ),
            forcing_provenance,
        )
    return None, forcing_provenance


def _artifact_blocker_evidence(
    candidate: SchedulerCandidateLike,
    base_evidence: Mapping[str, Any],
    planned_retry: Mapping[str, Any],
    *,
    reason: str,
    error_code: str,
    artifact_type: str,
    artifact_uri: str | None,
    artifact_exists: bool,
    unsafe_reason: str | None = None,
    forcing_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    provenance_evidence: dict[str, Any] = (
        {"forcing_provenance": dict(forcing_provenance)} if forcing_provenance else {}
    )
    return {
        **base_evidence,
        **provenance_evidence,
        "decision": "blocked_missing_upstream_artifact",
        "reason": reason,
        "error_code": error_code,
        "classifier": "missing_upstream_artifact",
        "restart_stage": planned_retry.get("restart_stage"),
        "restart_from_stage": planned_retry.get("restart_from_stage"),
        "native_shud_resubmitted": False,
        "replacement_submitted": False,
        "artifact_guard": {
            "artifact_type": artifact_type,
            "artifact_uri": _evidence_safe(artifact_uri),
            "artifact_exists": artifact_exists,
            "unsafe_reason": unsafe_reason,
            "stable_classifier": error_code,
            "planned_retry_decision": planned_retry.get("decision"),
            "planned_retry_reason": planned_retry.get("reason"),
        },
        "retry_policy": {
            "automatic_retry_allowed": False,
            "manual_retry_required": False,
            "attempt": _planned_retry_policy_value(planned_retry, "attempt", default=0),
            "retry_limit": _planned_retry_policy_value(planned_retry, "retry_limit", default=None),
        },
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
        },
    }


# Sidecar tier (#1203).  Mirrors the forcing producer's DB-free write side:
# ``forcing/<source_segment>/<compact_cycle>/<basin_version_id>/<model_id>/``
# holds ``forcing_version_record.json`` next to the package manifest
# (``workers/forcing_producer/file_store.py:_write_forcing_version_sidecar`` and
# ``workers/forcing_producer/producer.py`` prefix/``package_manifest_filename``).
_FORCING_SIDECAR_FILENAME = "forcing_version_record.json"
_FORCING_PACKAGE_MANIFEST_FILENAME = "forcing_package.json"
#: Sized from the records the producer actually writes: a production
#: ``forcing_version_record.json`` embeds per-station
#: ``lineage_json.output_files`` and measures 1.6-2.0 MB on node-22 (cycle
#: 2026080100/IFS, largest basin 2,014,038 bytes).  The original 64 KiB cap made
#: this tier fail ``sidecar_unreadable`` for EVERY production basin (#1203
#: round-2 V5-C1).  16 MiB matches the existing
#: ``object_store.MAX_OBJECT_MANIFEST_BYTES`` precedent; the read stays limited.
_FORCING_SIDECAR_MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class _ForcingSidecarProvenance:
    """Outcome of the object-store forcing-version sidecar read.

    ``witness`` is True only when the record names a package we can probe.  Every
    other outcome is "tier unavailable" (``status`` carries the detail) and is
    deliberately NOT treated as "package determined absent": the tier must never
    fail open into a retry, and never raise out of the decision path.
    """

    witness: bool
    status: str
    package_uri: str | None = None
    #: The manifest uri the record itself recorded -- EVIDENCE ONLY, never probed.
    manifest_uri: str | None = None
    #: The manifest object key derived from this candidate's own sidecar key
    #: directory: the only thing the existence probe is ever given.
    manifest_probe_key: str | None = None
    forcing_version_id: str | None = None


def _forcing_sidecar_provenance(candidate: SchedulerCandidateLike) -> _ForcingSidecarProvenance:
    basin_version_id = str(getattr(candidate, "basin_version_id", None) or "").strip()
    model_id = str(getattr(candidate, "model_id", None) or "").strip()
    if not basin_version_id or not model_id:
        return _ForcingSidecarProvenance(False, "identity_incomplete")
    resource_profile = candidate.resource_profile if isinstance(candidate.resource_profile, Mapping) else {}
    object_root = resource_profile.get("object_store_root") or os.getenv("OBJECT_STORE_ROOT")
    if object_root in (None, ""):
        # NOT the ``_object_manifest_is_missing`` fail-open quirk: an unconfigured
        # store means this tier cannot witness anything, not that the package exists.
        return _ForcingSidecarProvenance(False, "store_unconfigured")
    prefix = str(resource_profile.get("object_store_prefix") or os.getenv("OBJECT_STORE_PREFIX", ""))
    try:
        source_segment = normalize_source_id(candidate.source_id).lower()
        key_dir = (
            f"forcing/{source_segment}/{format_cycle_time(candidate.cycle_time_utc)}"
            f"/{basin_version_id}/{model_id}"
        )
    except (TypeError, ValueError):
        return _ForcingSidecarProvenance(False, "identity_incomplete")
    key = f"{key_dir}/{_FORCING_SIDECAR_FILENAME}"
    try:
        store = LocalObjectStore(str(object_root), object_store_prefix=prefix)
        if not store.exists(key):
            return _ForcingSidecarProvenance(False, "sidecar_absent")
        if store.size(key) > _FORCING_SIDECAR_MAX_BYTES:
            # Size pre-check so an anomalously large record is distinguishable
            # from a permission/IO read denial (#1203 round-2 V5-C1): the two
            # have completely different operator handling, and the runbook
            # routing table routes them differently.  ``store.size`` raising
            # ``ObjectStoreError`` on a stat fault is caught below and degrades
            # to ``sidecar_unreadable`` -- never escapes.
            return _ForcingSidecarProvenance(False, "sidecar_oversized")
        content = store.read_bytes_limited(key, max_bytes=_FORCING_SIDECAR_MAX_BYTES)
    except (ObjectStoreError, OSError, ValueError):
        return _ForcingSidecarProvenance(False, "sidecar_unreadable")
    try:
        record = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        # ``RecursionError`` is listed explicitly because it is NOT a
        # ``ValueError`` subclass: ``json`` raises it (not ``JSONDecodeError``)
        # on a deeply nested document, which is small enough that every
        # read-side guard above passes.  Uncaught it would escape this tier and
        # abort the whole scheduler pass; a record we cannot parse is malformed,
        # exactly like invalid JSON.  Same containment as
        # ``file_orchestration_journal._decode_mapping``.
        return _ForcingSidecarProvenance(False, "sidecar_malformed")
    if not isinstance(record, Mapping):
        return _ForcingSidecarProvenance(False, "sidecar_malformed")
    package_uri = str(record.get("forcing_package_uri") or "").strip()
    recorded_manifest_uri = _sidecar_recorded_manifest_uri(record)
    if not package_uri and not recorded_manifest_uri:
        # The record names no package at all, so it witnesses nothing.
        return _ForcingSidecarProvenance(False, "sidecar_malformed")
    return _ForcingSidecarProvenance(
        True,
        "sidecar_witness",
        package_uri=package_uri or None,
        manifest_uri=recorded_manifest_uri,
        manifest_probe_key=_sidecar_manifest_probe_key(key_dir),
        forcing_version_id=str(record.get("forcing_version_id") or "") or None,
    )


def _sidecar_manifest_probe_key(sidecar_key_dir: str) -> str:
    """Derive the package MANIFEST FILE key the existence probe is given.

    Derived from THIS candidate's own sidecar key directory plus the producer's
    default ``package_manifest_filename``, i.e. the producer's own
    ``_package_manifest_uri`` construction applied to the key the sidecar itself
    was read from.  Two failure legs make the record's recorded manifest uri
    unusable as a probe object (#1203 round-1 V2-C2):

    * a producer/scheduler ``OBJECT_STORE_PREFIX`` drift (``s3://nhms-prod`` vs
      ``s3://nhms``) makes ``_normalize_s3_uri`` raise, which the probe swallows
      into a false "missing" for a physically present package;
    * a sidecar copied or restored from elsewhere can name a FOREIGN manifest,
      which would then stand in as this candidate's witness and fail open.

    A key derived from the candidate identity has neither leg, and is a manifest
    FILE key (6 segments) rather than the directory-shaped package uri the
    object-path validator rejects.
    """

    return _package_manifest_probe_uri(sidecar_key_dir)


def _package_manifest_probe_uri(package_uri: str) -> str:
    """Join a forcing package prefix with the producer's manifest filename (#1365).

    THE single derivation of a forcing package's witness object, shared by the
    object-store sidecar tier (``_sidecar_manifest_probe_key``) and the
    journal/direct tier so the two cannot drift.  Literally the producer's own
    ``workers/forcing_producer/producer._package_manifest_uri`` construction with
    the producer's default ``package_manifest_filename``
    (``_FORCING_PACKAGE_MANIFEST_FILENAME``); no caller may hand-join the
    manifest name.

    Surrounding whitespace is stripped for the same reason the probe strips it:
    a recorded uri with a trailing space would otherwise fabricate a key with an
    empty path segment.
    """

    return f"{package_uri.strip().rstrip('/')}/{_FORCING_PACKAGE_MANIFEST_FILENAME}"


def _needs_package_manifest_witness(value: str) -> bool:
    """True when a recorded package reference must be probed via its witness (#1365).

    The trigger is VALIDATOR ADMISSIBILITY, not a decorative trailing ``/``
    (round-1 cand-05).  The forcing producer records ``forcing_package_uri`` as a
    directory uri (``producer._directory_uri``: 5 canonical segments, trailing
    ``/``), but the handoff lane stores a normalized copy of the SAME reference
    with the slash stripped (``forcing_producer/file_store.py`` ``normalize_key``
    ``.strip("/")``, and ``forcing_domain_handoff_apply``'s ``rtrim`` comparison
    proves both shapes coexist in ``met.forcing_version``).  Both shapes name a
    package prefix, and the closed-world object-path validator admits neither as a
    FILE key (it requires ``len(parts) > len(pattern.segments)``), so handing
    either to the existence probe swallows a ``ValueError`` into a false "missing"
    for a physically present package.

    A reference the validator DOES admit as a file key is probed as-is and never
    double-derived.  Local paths are excluded: the local leg stats the path itself
    and needs no witness object.  (The validator is consulted without the
    deployment's ``OBJECT_STORE_PREFIX``, which is an ``s3://`` uri in every
    tracked config and is therefore already stripped by the validator's own
    ``urlparse``.)

    The validator is itself a RAISING surface: it calls ``urlparse``, which
    rejects a recorded uri whose authority holds a ``[`` with ``ValueError:
    Invalid IPv6 URL``.  This classification runs OUTSIDE the probe's own
    containment (it decides what to probe), so an escaping ``ValueError`` here
    would abort the whole scheduler pass for every remaining candidate -- the
    exact fault master contained inside the probe's ``(OSError, ValueError)``
    leg.  An unparseable reference is not a package prefix we can derive a
    witness for, so it is answered ``False`` and probed as recorded; the probe's
    own ``ValueError`` leg then yields the fail-closed "missing" verdict with a
    null unsafe reason, i.e. the D4 repair-eligible unresolvable-reference
    residual (a rebuild re-records the reference and IS the remedy).
    """

    stripped = value.strip()
    if not stripped or _looks_like_local_uri_or_path(stripped):
        return False
    try:
        return not validate_object_path(stripped).valid
    except ValueError:
        return False


def _sidecar_recorded_manifest_uri(record: Mapping[str, Any]) -> str | None:
    """Read the record's own manifest uri (``lineage_json``) for evidence only.

    Same field the producer read side uses
    (``workers/forcing_producer/file_store.py:_forcing_package_manifest_uri``),
    surfaced so an operator can compare what the record claims against the key
    that was actually probed.  It is never handed to the probe.
    """

    lineage = record.get("lineage_json")
    if isinstance(lineage, Mapping):
        recorded = str(lineage.get("forcing_package_manifest_uri") or "").strip()
        if recorded:
            return recorded
    return None


def _is_withheld_uri_placeholder(value: str | None) -> bool:
    """True when the value is a redaction placeholder, i.e. a withheld reference.

    Reuses the shared placeholder set (``scheduler_init_state_match``) that the
    init-state identity comparison already treats as "value withheld, not value
    disagreeing"; the decision layer must not fork a second literal.
    """

    return str(value or "").strip() in EVIDENCE_REDACTION_PLACEHOLDERS


def _journal_forcing_provenance(state: Mapping[str, Any], forcing_uri: str) -> dict[str, Any] | None:
    """Name the journal tier that materialized ``state['forcing_version']`` (D1)."""

    if _is_withheld_uri_placeholder(forcing_uri):
        # A withheld uri is never probed, so no blocker may be stamped with a
        # journal/direct tier for it (#1203 round-1 C2).  Defence in depth: the
        # caller already routes placeholders to the sidecar tier.
        return None
    forcing_version = state.get("forcing_version")
    if not isinstance(forcing_version, Mapping):
        return None
    source = str(forcing_version.get("forcing_version_source") or "")
    if source not in {"journal", "direct"}:
        return None
    if forcing_uri not in {
        str(forcing_version.get(key))
        for key in ("forcing_package_uri", "forcing_uri", "package_uri", "forcing_package_path")
        if forcing_version.get(key) not in (None, "")
    }:
        # The uri the guard probed came from another state container, so the
        # journal tier marker does not describe it.
        return None
    return {
        "source": source,
        "package_uri": _evidence_safe(forcing_uri),
        "forcing_version_id": _evidence_safe(forcing_version.get("forcing_version_id")),
    }


def _first_artifact_uri(state: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for container in _artifact_state_containers(state):
        for key in keys:
            value = container.get(key)
            if value not in (None, ""):
                return str(value)
    return None


def _artifact_state_containers(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    containers: list[Mapping[str, Any]] = [state]
    for key in (
        "forecast_cycle",
        "forcing_version",
        "completed_stage_evidence",
        "copyback_evidence",
        "terminal_stage_evidence",
        "artifact_guard",
    ):
        value = state.get(key)
        if isinstance(value, Mapping):
            containers.append(value)
    for job in reversed(_state_jobs(state)):
        containers.append(job)
        details = job.get("details")
        if isinstance(details, Mapping):
            containers.append(details)
    for event in reversed(_state_events(state)):
        containers.append(event)
        details = event.get("details")
        if isinstance(details, Mapping):
            containers.append(details)
    return containers


def _artifact_uri_is_missing(candidate: SchedulerCandidateLike, artifact_uri: str) -> bool:
    return _artifact_uri_missing_status(candidate, artifact_uri)[0]


def _artifact_uri_missing_status(candidate: SchedulerCandidateLike, artifact_uri: str) -> tuple[bool, str | None]:
    value = artifact_uri.strip()
    if not value:
        return True, None
    if value.startswith("s3://") or not _looks_like_local_uri_or_path(value):
        if not _object_store_root_configured(candidate):
            # #1365 D2: ``_object_manifest_is_missing`` fail-OPENS (``return
            # False``) for ANY uri when no root is configured -- so a guard that
            # cannot probe would vouch for the existence of even a bogus key.
            # Fail closed instead, with a reason that keeps "no probe ran"
            # distinguishable from "probed, determined absent"
            # (``unsafe_reason=None``); same doctrine as the sidecar tier's
            # ``store_unconfigured``.  ``_object_manifest_is_missing`` itself is
            # left untouched: its raw-manifest repair-lane callers have different
            # fail-open consequences and are out of scope.
            return True, "object_store_root_unconfigured"
        try:
            return _object_manifest_is_missing(candidate, value), None
        except ObjectStoreError:
            # #1365 round-1 (cand-01): ``LocalObjectStore.exists`` turns a
            # ``SafeFilesystemError`` (a symlinked probe target or ancestor, an NFS
            # ESTALE/EIO stat, an unsafe store root) into an ``ObjectStoreError`` --
            # a ``RuntimeError`` subclass the ``(OSError, ValueError)`` leg below
            # does NOT cover, so it used to escape the whole decision path and abort
            # the scheduler pass for every remaining candidate.  Contain it HERE, so
            # every caller of this probe (forcing tier-1/2, copyback, sidecar) is
            # covered by one containment instead of per-call-site guards.
            #
            # The reason is distinguishable from both "probed, determined absent"
            # (``None``) and "no store configured"
            # (``object_store_root_unconfigured``): being non-null it is refused by
            # the authorized repair channel, which is the correct routing -- an
            # exact-cycle forcing rebuild cannot clear a filesystem fault.
            return True, _ARTIFACT_PROBE_ERROR_REASON
        except (OSError, ValueError):
            # Recorded reference the closed-world validator cannot resolve even
            # after witness derivation (#1365 D4 recorded residual): "determined
            # absent" with a null reason is honest at the repair boundary, because a
            # rebuild re-records the reference and IS an effective remedy.
            return True, None
    try:
        path = _local_artifact_path(value)
        if path is None:
            return True, "invalid_local_artifact_path"
        if not _local_artifact_path_is_allowed(candidate, path):
            return True, "local_artifact_path_outside_allowed_roots"
        return not path.exists(), None
    except (OSError, ValueError):
        return True, "local_artifact_path_unresolvable"


def _object_store_root_configured(candidate: SchedulerCandidateLike) -> bool:
    """Whether an object-store root exists for the probe to resolve keys against.

    Reads exactly the two sources ``_object_manifest_is_missing`` itself reads,
    in the same order, so the fail-closed pre-check and the probe can never
    disagree about which store (if any) is in play.
    """

    resource_profile = getattr(candidate, "resource_profile", None)
    profile_root = resource_profile.get("object_store_root") if isinstance(resource_profile, Mapping) else None
    return (profile_root or os.getenv("OBJECT_STORE_ROOT")) not in (None, "")


def _looks_like_local_uri_or_path(value: str) -> bool:
    return value.startswith("file://") or value.startswith("/") or value.startswith("~")


def _local_artifact_path(value: str) -> Path | None:
    if value.startswith("file://"):
        parsed = urlparse(value)
        if parsed.scheme != "file":
            return None
        return Path(unquote(parsed.path)).expanduser()
    return Path(value).expanduser()


def _local_artifact_path_is_allowed(candidate: SchedulerCandidateLike, path: Path) -> bool:
    resolved = path.resolve(strict=False)
    roots = _local_artifact_allowed_roots(candidate)
    return bool(roots) and any(_path_is_relative_to(resolved, root) for root in roots)


def _local_artifact_allowed_roots(candidate: SchedulerCandidateLike) -> tuple[Path, ...]:
    values: list[str] = []
    resource_profile = getattr(candidate, "resource_profile", {}) or {}
    if isinstance(resource_profile, Mapping):
        for key in (
            "object_store_root",
            "object_store_copyback_root",
            "copyback_root",
            "published_artifact_root",
        ):
            value = resource_profile.get(key)
            if value not in (None, ""):
                values.append(str(value))
    for env_name in ("OBJECT_STORE_ROOT", "NHMS_OBJECT_STORE_COPYBACK_ROOT", "NHMS_PUBLISHED_ARTIFACT_ROOT"):
        value = os.getenv(env_name)
        if value:
            values.append(value)
    roots: list[Path] = []
    seen: set[str] = set()
    for value in values:
        text = value.strip()
        if not text or "://" in text:
            continue
        try:
            root = Path(text).expanduser().resolve(strict=False)
        except (OSError, ValueError):
            continue
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)
    return tuple(roots)


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _copyback_source_required(state: Mapping[str, Any]) -> bool:
    if state.get("copyback_source_required") is not None:
        return bool(state.get("copyback_source_required"))
    copyback_evidence = state.get("copyback_evidence")
    if isinstance(copyback_evidence, Mapping) and copyback_evidence.get("source_required") is not None:
        return bool(copyback_evidence.get("source_required"))
    return False


def _planned_retry_policy_value(
    planned_retry: Mapping[str, Any],
    key: str,
    *,
    default: Any,
) -> Any:
    retry_policy = planned_retry.get("retry_policy")
    if isinstance(retry_policy, Mapping) and retry_policy.get(key) is not None:
        return retry_policy.get(key)
    return default

#: The placeholder domain's classifier arm, preserved VERBATIM from the blacklist
#: this judgement replaced (#1313 D4).  The seven codes the reader can synthesize
#: (``{CONVERT,FORCING,FORECAST,PARSE,STATE_SAVE_QC,PUBLISH,COPYBACK}_FAILED``)
#: never hit the old code arm, so this classifier arm was the blacklist's only
#: effective refusal surface there; dropping it would LOOSEN a latent corner (a
#: state that explicitly overrides ``classifier`` to one of these three), which
#: runs against this change's strictly-tightening direction.
_DOWNSTREAM_PLACEHOLDER_REFUSAL_CLASSIFIERS = frozenset(
    {"malformed_input", "policy_blocked", "resource_configuration"}
)


def _downstream_failure_restartable(failure: Mapping[str, Any], *, code_recorded: bool) -> bool:
    """Whether a durable-SHUD downstream failure may resume its failed stage.

    ``code_recorded`` splits the judgement by EVIDENCE SOURCE (#1313 D4).  A
    genuinely recorded code is governed by the spec's permanence clause -- a
    permanent or unknown-default-non-transient code refuses the resume, a
    transient code keeps it.  A reader-synthesized ``{STAGE}_FAILED`` placeholder
    is not evidence under that clause and keeps its pre-#1313 behaviour.
    """

    if failure.get("limit_exhausted") is True:
        return False
    if not code_recorded:
        return str(failure.get("classifier") or "") not in _DOWNSTREAM_PLACEHOLDER_REFUSAL_CLASSIFIERS
    return not failure.get("permanent")


def _completed_upstream_stage_retry_evidence(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    completed_stage = state.get("completed_stage_evidence")
    if not isinstance(completed_stage, Mapping):
        return None
    restart_stage = _canonical_downstream_stage(
        str(
            state.get("restart_stage")
            or state.get("restart_from_stage")
            or completed_stage.get("restart_stage")
            or completed_stage.get("restart_from_stage")
            or ""
        )
    )
    if restart_stage is None:
        return None
    failure_state = dict(state)
    failure_state.pop("restart_stage", None)
    failure_state.pop("restart_from_stage", None)
    if _state_has_failure_signal(failure_state):
        return None
    return {
        **base_evidence,
        "decision": "retry_after_completed_stage",
        "reason": "resume_after_completed_stage",
        "restart_stage": restart_stage,
        "restart_from_stage": restart_stage,
        "native_shud_resubmitted": restart_stage == "forecast",
        "completed_stage_evidence": _evidence_safe(dict(completed_stage)),
        "retry_policy": {
            "automatic_retry_allowed": True,
            "manual_retry_required": False,
            "attempt": _state_retry_attempt(state, stage=restart_stage),
            "retry_limit": _state_retry_limit(state),
        },
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
        },
    }


def _missing_raw_manifest_repair_evidence(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not _state_has_failure_signal(state):
        return None
    failed_stage = str(_failed_stage(state) or "")
    if failed_stage == "" or _is_source_cycle_download_stage(failed_stage):
        return None
    manifest_uri = _forecast_cycle_manifest_uri(candidate, state)
    if manifest_uri in (None, ""):
        return None
    if not _is_raw_manifest_object_uri(str(manifest_uri)):
        return None
    if not _has_successful_download_stage(state):
        return None
    if not _object_manifest_is_missing(candidate, str(manifest_uri)):
        return None
    failure = _failure_policy_payload(state)
    # #1313 D3: consulted AFTER the structural gates above (so healthy/running
    # candidates compute nothing new) and BEFORE the overwrite below.  Refusing
    # here is a fall-through, not a jump to the guard: the ladder still offers the
    # candidate to the model-package refresh channel, which may legitimately claim
    # it when the package genuinely changed.
    if failure["permanent"] and not _remedy_permits_permanent_failure(failure, remedy="raw_input_reingestion"):
        return None
    failure = {
        **failure,
        "retryable": True,
        "permanent": False,
        "limit_exhausted": False,
        "classifier": "recoverable_missing_raw_manifest",
    }
    return {
        **base_evidence,
        "decision": "retry_failed",
        "reason": "repair_missing_raw_manifest",
        "restart_stage": None,
        "restart_from_stage": "download",
        "fresh_ingestion": {"required": True, "mode": "full_chain"},
        "stage": failed_stage,
        "task_identity": _state_task_identity(state),
        "failure": failure,
        "raw_manifest_repair": {
            "manifest_uri": str(manifest_uri),
            "manifest_exists": False,
            "successful_download_stage": True,
            "downstream_failed_stage": failed_stage,
        },
        "retry_policy": {
            "automatic_retry_allowed": True,
            "manual_retry_required": False,
            "attempt": failure["attempt"],
            "retry_limit": failure["retry_limit"],
        },
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
        },
    }

def _repaired_raw_manifest_downstream_retry_evidence(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not _state_has_failure_signal(state):
        return None
    failed_stage = str(_failed_stage(state) or "")
    if failed_stage == "" or _is_source_cycle_download_stage(failed_stage):
        return None
    manifest_uri = _forecast_cycle_manifest_uri(candidate, state)
    if manifest_uri in (None, ""):
        return None
    if not _is_raw_manifest_object_uri(str(manifest_uri)):
        return None
    if _object_manifest_is_missing(candidate, str(manifest_uri)):
        return None
    repair_download = _latest_successful_download_stage(state)
    if repair_download is None:
        return None
    failed_job = _latest_failed_job_for_stage(state, failed_stage)
    if failed_job is None:
        return None
    repair_time = _job_terminal_time(repair_download)
    failed_time = _job_terminal_time(failed_job)
    if repair_time is not None and failed_time is not None and repair_time <= failed_time:
        return None
    failure = _failure_policy_payload(state)
    # #1313 D3: same shared judgement, same remedy category as the repair channel
    # above -- consulted after the structural gates, before the overwrite.
    if failure["permanent"] and not _remedy_permits_permanent_failure(failure, remedy="raw_input_reingestion"):
        return None
    failure = {
        **failure,
        "retryable": True,
        "permanent": False,
        "limit_exhausted": False,
        "classifier": "recoverable_downstream_after_raw_repair",
    }
    return {
        **base_evidence,
        "decision": "retry_failed",
        "reason": "retry_downstream_after_raw_repair",
        "restart_stage": None,
        "restart_from_stage": "download",
        "fresh_ingestion": {"required": False, "mode": "reuse_repaired_raw_then_full_chain"},
        "stage": failed_stage,
        "task_identity": _state_task_identity(state),
        "failure": failure,
        "raw_manifest_repair": {
            "manifest_uri": str(manifest_uri),
            "manifest_exists": True,
            "successful_download_stage": True,
            "successful_download_job_id": repair_download.get("job_id") or repair_download.get("pipeline_job_id"),
            "downstream_failed_stage": failed_stage,
            "downstream_failed_job_id": failed_job.get("job_id") or failed_job.get("pipeline_job_id"),
        },
        "retry_policy": {
            "automatic_retry_allowed": True,
            "manual_retry_required": False,
            "attempt": failure["attempt"],
            "retry_limit": failure["retry_limit"],
        },
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
        },
    }

def _has_successful_download_stage(state: Mapping[str, Any]) -> bool:
    return _latest_successful_download_stage(state) is not None

def _latest_successful_download_stage(state: Mapping[str, Any]) -> Mapping[str, Any] | None:
    matches: list[Mapping[str, Any]] = []
    for job in _state_jobs(state):
        stage = str(job.get("stage") or job.get("job_type") or "")
        status = str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
        if _is_source_cycle_download_stage(stage) and status in TERMINAL_PIPELINE_SUCCESS_STATUSES:
            matches.append(job)
    if not matches:
        return None
    return max(matches, key=_job_terminal_sort_key)

def _latest_failed_job_for_stage(state: Mapping[str, Any], stage_name: str) -> Mapping[str, Any] | None:
    normalized_stage = _canonical_downstream_stage(stage_name) or stage_name
    matches: list[Mapping[str, Any]] = []
    for job in _state_jobs(state):
        if _pipeline_job_is_repaired_stage_evidence(job):
            continue
        stage = str(job.get("stage") or job.get("job_type") or "")
        status = str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
        if stage in {normalized_stage, stage_name} and status in FAILED_PIPELINE_STATUSES:
            matches.append(job)
    if not matches:
        return None
    return max(matches, key=_job_terminal_sort_key)

def _job_terminal_sort_key(job: Mapping[str, Any]) -> tuple[int, datetime]:
    value = _job_terminal_time(job)
    if value is None:
        return (0, datetime.min.replace(tzinfo=UTC))
    return (1, value)

def _job_terminal_time(job: Mapping[str, Any]) -> datetime | None:
    return _first_state_datetime(job, "finished_at", "updated_at", "submitted_at", "started_at", "created_at")

def _retry_failure_evidence(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    failure = _failure_policy_payload(state)
    cold_start_quarantined = _cold_start_quarantined_failure(failure)
    if cold_start_quarantined:
        failure = {
            **failure,
            "classifier": "cold_start_quarantine_recompute",
            "retryable": True,
            "permanent": False,
            "limit_exhausted": False,
        }
    failed_stage = _failed_stage(state)
    restart_stage = (
        "forecast"
        if failed_stage in NATIVE_SHUD_STAGE_ALIASES or cold_start_quarantined
        else _canonical_downstream_stage(failed_stage)
    )
    return {
        **base_evidence,
        "decision": "retry_failed",
        "reason": "retry_failed_candidate",
        "stage": failed_stage,
        "restart_stage": restart_stage,
        "restart_from_stage": restart_stage,
        "task_identity": _state_task_identity(state),
        "failure": failure,
        "retry_policy": {
            "automatic_retry_allowed": failure["retryable"],
            "manual_retry_required": failure["permanent"],
            "attempt": failure["attempt"],
            "retry_limit": failure["retry_limit"],
        },
        "reuse": {
            "successful_sibling_outputs_reused": bool(state.get("successful_sibling_outputs_reused")),
            "durable_output_reused": _durable_shud_output_exists(state),
        },
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
        },
    }

def _permanent_failure_evidence(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not _state_has_failure_signal(state):
        return None
    failure = _failure_policy_payload(state)
    if not failure["permanent"]:
        return None
    if _cold_start_quarantined_failure(failure):
        return None
    return {
        **base_evidence,
        "decision": "permanent_failure",
        "reason": _permanent_reason(state, failure),
        "stage": _failed_stage(state),
        "task_identity": _state_task_identity(state),
        "failure": failure,
        "retry_policy": {
            "automatic_retry_allowed": False,
            "manual_retry_required": True,
            "attempt": failure["attempt"],
            "retry_limit": failure["retry_limit"],
        },
        "manual_retry_required": True,
        "prior_failure_reason": failure["reason_code"],
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
        },
    }


def _model_package_refresh_retry_evidence(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not _state_has_failure_signal(state):
        return None
    failure = _failure_policy_payload(state)
    if not failure["permanent"]:
        return None
    # A model-package refresh compares only the package shas and restarts the same
    # failed stage — it is not a memory-sizing remedy, so an out-of-memory failure
    # must not borrow this override to regain automatic retry (#1161).  Both arms
    # (classifier and code) now live in the shared judgement source; the refusal
    # set for this remedy is unchanged (#1313 D2).
    if not _remedy_permits_permanent_failure(failure, remedy="changed_model_package"):
        return None
    prior = state.get("run_manifest_model_package")
    if not isinstance(prior, Mapping):
        return None
    changed_fields = _model_package_changed_fields(candidate, prior)
    if not changed_fields:
        return None
    restart_stage = "forecast" if _failed_stage(state) in NATIVE_SHUD_STAGE_ALIASES else _failed_stage(state)
    return {
        **base_evidence,
        "decision": "retry_after_model_package_refresh",
        "reason": "retry_after_model_package_refresh",
        "stage": restart_stage,
        "restart_stage": restart_stage,
        "failure": {
            **failure,
            "manual_retry_required": False,
            "model_package_refresh": True,
        },
        "model_package_refresh": {
            "source": prior.get("source") or "run_manifest",
            "changed_fields": changed_fields,
            "previous_status": prior.get("status"),
        },
        "retry_policy": {
            "automatic_retry_allowed": True,
            "manual_retry_required": False,
            "attempt": failure["attempt"],
            "retry_limit": failure["retry_limit"],
            "override_reason": "model_package_refresh",
        },
        "manual_retry_required": False,
        "prior_failure_reason": failure["reason_code"],
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
        },
    }


def _model_package_changed_fields(candidate: SchedulerCandidateLike, prior: Mapping[str, Any]) -> list[str]:
    changed: list[str] = []
    previous_package_uri_sha = str(prior.get("model_package_uri_sha256") or "")
    if previous_package_uri_sha:
        current_package_uri = str(getattr(candidate, "model_package_uri", "") or "")
        if current_package_uri and _stable_sha256(current_package_uri) != previous_package_uri_sha:
            changed.append("model_package_uri")
    previous_manifest_uri_sha = str(prior.get("model_package_manifest_uri_sha256") or "")
    if previous_manifest_uri_sha:
        current_manifest_uri = _candidate_model_package_manifest_uri(candidate)
        if current_manifest_uri and _stable_sha256(current_manifest_uri) != previous_manifest_uri_sha:
            changed.append("model_package_manifest_uri")
    previous_checksum = str(prior.get("model_package_checksum") or "")
    current_checksum = _candidate_model_package_checksum(candidate)
    if previous_checksum and current_checksum and current_checksum != previous_checksum:
        changed.append("model_package_checksum")
    previous_checksum_sha = str(prior.get("model_package_checksum_sha256") or "")
    if previous_checksum_sha and current_checksum and _stable_sha256(current_checksum) != previous_checksum_sha:
        changed.append("model_package_checksum")
    return sorted(set(changed))


def _candidate_model_package_manifest_uri(candidate: SchedulerCandidateLike) -> str | None:
    resource_profile = getattr(candidate, "resource_profile", None)
    if isinstance(resource_profile, Mapping):
        explicit = resource_profile.get("manifest_uri") or resource_profile.get("model_package_manifest_uri")
        if explicit not in (None, ""):
            return str(explicit)
    package_uri = str(getattr(candidate, "model_package_uri", "") or "").rstrip("/")
    if not package_uri:
        return None
    if package_uri.endswith("/package"):
        return f"{package_uri.removesuffix('/package')}/manifest.json"
    return f"{package_uri}/manifest.json"


def _candidate_model_package_checksum(candidate: SchedulerCandidateLike) -> str | None:
    resource_profile = getattr(candidate, "resource_profile", None)
    if isinstance(resource_profile, Mapping):
        checksum = resource_profile.get("package_checksum") or resource_profile.get("model_package_checksum")
        if checksum not in (None, ""):
            return str(checksum)
    return None


def _stable_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _state_has_failure_signal(state: Mapping[str, Any]) -> bool:
    if _state_has_only_repaired_pipeline_failure_signal(state):
        return False
    pipeline_status = _state_status(state, "pipeline_status", "job_status", "status")
    hydro_status = _state_status(state, "hydro_status", "hydro_run_status")
    if pipeline_status in FAILED_PIPELINE_STATUSES or hydro_status in {"failed", "permanently_failed"}:
        return True
    if (
        pipeline_status in ACTIVE_PIPELINE_STATUSES
        and _state_has_only_unsubmitted_auto_retry_placeholders(state)
        and _failed_stage(state) is not None
        and _state_error_code(state) not in (None, "")
    ):
        return True
    if pipeline_status is not None:
        return False
    if _failed_stage(state) is not None and _state_error_code(state) not in (None, ""):
        return True
    return False

def _state_has_only_repaired_pipeline_failure_signal(state: Mapping[str, Any]) -> bool:
    jobs = _state_jobs(state)
    if not jobs:
        return False
    active_failure_jobs = [
        job
        for job in jobs
        if not _pipeline_job_is_repaired_stage_evidence(job)
        and (
            str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
            in FAILED_PIPELINE_STATUSES
            or job.get("error_code") not in (None, "")
            or job.get("reason_code") not in (None, "")
        )
    ]
    if active_failure_jobs:
        return False
    active_failure_events = [
        event
        for event in _state_events(state)
        if not _event_is_manual_retry_marker(event) and _event_has_failure_signal(event)
    ]
    if active_failure_events:
        return False
    repaired_failure_jobs = [
        job
        for job in jobs
        if _pipeline_job_is_repaired_stage_evidence(job)
        and (
            str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
            in FAILED_PIPELINE_STATUSES
            or job.get("error_code") not in (None, "")
            or job.get("reason_code") not in (None, "")
        )
    ]
    return bool(repaired_failure_jobs)

def _cancelled_state_evidence(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    pipeline_status = _state_status(state, "pipeline_status", "job_status", "status")
    hydro_status = _state_status(state, "hydro_status", "hydro_run_status")
    if pipeline_status != "cancelled" and hydro_status != "cancelled":
        return None
    return {
        **base_evidence,
        "decision": "cancelled_manual_retry_required",
        "reason": "manual_retry_required_after_cancelled",
        "terminal_status": "cancelled",
        "cancelled": True,
        "replacement_submitted": False,
        "manual_retry_required": True,
        "retry_policy": {
            "automatic_retry_allowed": False,
            "manual_retry_required": True,
            "attempt": _state_retry_attempt(state, stage=_failed_stage(state)),
            "retry_limit": _state_retry_limit(state),
        },
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
        },
    }

def _manual_retry_state_evidence(
    candidate: SchedulerCandidateLike,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    failure = _failure_policy_payload(state, manual=True)
    manual = _manual_retry_payload(state)
    prior_failure = _prior_failure_reason(state) or failure["reason_code"]
    previous_attempt = _state_retry_attempt(state, stage=_failed_stage(state))
    new_attempt = _manual_retry_new_attempt(state, previous_attempt=previous_attempt)
    evidence = {
        **base_evidence,
        "decision": "manual_retry",
        "reason": "manual_retry_requested",
        "manual_retry": {
            **manual,
            "marker": True,
            "allowed": True,
            "previous_attempt": previous_attempt,
            "new_attempt": new_attempt,
        },
        "failure": {
            **failure,
            "prior_failure_reason": prior_failure,
            "previous_attempt": previous_attempt,
            "new_attempt": new_attempt,
        },
        "retry_policy": {
            "automatic_retry_allowed": False,
            "manual_retry_required": False,
            "manual_retry_marker": True,
            "attempt": new_attempt,
            "previous_attempt": previous_attempt,
            "new_attempt": new_attempt,
            "retry_limit": failure["retry_limit"],
        },
        "prior_failure_reason": prior_failure,
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
        },
    }
    if _cold_start_quarantined_failure(failure, prior_failure):
        evidence["restart_stage"] = "forecast"
        evidence["restart_from_stage"] = "forecast"
        evidence["native_shud_resubmitted"] = True
        evidence["force_native_shud_rerun"] = True
    return evidence
