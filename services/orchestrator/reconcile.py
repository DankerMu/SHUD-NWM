"""Restart reconcile for in-flight Slurm jobs by durable candidate identity.

On scheduler/gateway restart, in-flight job ids MUST be recovered from the
durable ``pipeline_job`` table (NOT the gateway's in-memory ``_jobs``), then each
is reconciled against authoritative Slurm accounting via ``sacct``. Reconcile is
read-only with respect to submission: it queries ``sacct`` and updates the
durable status; it NEVER resubmits a still-running or already-terminal
candidate.

Identity verification: ``pipeline_job`` durably binds ``slurm_job_id`` to
``run_id``/``model_id``/``cycle_id``/``stage``. ``sacct`` is queried for that
``slurm_job_id`` and the returned record must (a) exist in accounting and (b)
carry the expected ``JobName`` stage token (``nhms_<stage>``), confirming the
accounting row is the job we recorded and guarding against job-id reuse or
mismatch. If accounting has no matching/verifiable row, the candidate is marked
``reconcile_unverified`` rather than blindly resubmitted.

This module is the compatibility facade: the implementation lives in the
``services.orchestrator.reconcile_<topic>`` leaves (records, identity,
sacct_probe, comment_sacct, comment_proof, inflight, reserved_support,
reserved_phases, reserved), and every name re-exported here is the leaf object. Leaves never
import this facade. Patch a name on the leaf that reads it, except names
production resolves through this facade at call time.
"""

from __future__ import annotations

import logging
import subprocess as subprocess

from services.orchestrator.reconcile_comment_proof import _bounded_comment_records as _bounded_comment_records
from services.orchestrator.reconcile_comment_proof import (
    _classify_global_comment_records as _classify_global_comment_records,
)
from services.orchestrator.reconcile_comment_proof import (
    _comment_query_accepts_attempt_anchor as _comment_query_accepts_attempt_anchor,
)
from services.orchestrator.reconcile_comment_proof import (
    _comment_query_accepts_contract_version as _comment_query_accepts_contract_version,
)
from services.orchestrator.reconcile_comment_proof import (
    _comment_query_accepts_forcing_controller_identity as _comment_query_accepts_forcing_controller_identity,
)
from services.orchestrator.reconcile_comment_proof import _comment_query_accepts_scope as _comment_query_accepts_scope
from services.orchestrator.reconcile_comment_proof import _comment_query_capability as _comment_query_capability
from services.orchestrator.reconcile_comment_proof import _comment_query_records as _comment_query_records
from services.orchestrator.reconcile_comment_proof import _CommentAccountingProof as _CommentAccountingProof
from services.orchestrator.reconcile_comment_proof import (
    _effective_versioned_absence_coverage as _effective_versioned_absence_coverage,
)
from services.orchestrator.reconcile_comment_proof import (
    _query_comment_accounting_proof as _query_comment_accounting_proof,
)
from services.orchestrator.reconcile_comment_sacct import _SUBMITLINE_COMMENT_FLAG as _SUBMITLINE_COMMENT_FLAG
from services.orchestrator.reconcile_comment_sacct import COMMENT_SACCT_LOOKBACK_DAYS as COMMENT_SACCT_LOOKBACK_DAYS
from services.orchestrator.reconcile_comment_sacct import COMMENT_SACCT_PAGE_HOURS as COMMENT_SACCT_PAGE_HOURS
from services.orchestrator.reconcile_comment_sacct import COMMENT_SACCT_TIMEOUT_SECONDS as COMMENT_SACCT_TIMEOUT_SECONDS
from services.orchestrator.reconcile_comment_sacct import FALLBACK_JOB_NAME as FALLBACK_JOB_NAME
from services.orchestrator.reconcile_comment_sacct import FALLBACK_SACCT_FORMAT as FALLBACK_SACCT_FORMAT
from services.orchestrator.reconcile_comment_sacct import MAX_COMMENT_SACCT_BYTES as MAX_COMMENT_SACCT_BYTES
from services.orchestrator.reconcile_comment_sacct import MAX_COMMENT_SACCT_ROWS as MAX_COMMENT_SACCT_ROWS
from services.orchestrator.reconcile_comment_sacct import _bounded_sacct_page as _bounded_sacct_page
from services.orchestrator.reconcile_comment_sacct import _bounded_sacct_stdout as _bounded_sacct_stdout
from services.orchestrator.reconcile_comment_sacct import _fallback_candidate_in_window as _fallback_candidate_in_window
from services.orchestrator.reconcile_comment_sacct import _fallback_eligibility as _fallback_eligibility
from services.orchestrator.reconcile_comment_sacct import _FallbackMaster as _FallbackMaster
from services.orchestrator.reconcile_comment_sacct import (
    _forcing_controller_identity_request as _forcing_controller_identity_request,
)
from services.orchestrator.reconcile_comment_sacct import _index_comment_sacct_matches as _index_comment_sacct_matches
from services.orchestrator.reconcile_comment_sacct import _parse_fallback_sacct_rows as _parse_fallback_sacct_rows
from services.orchestrator.reconcile_comment_sacct import _SacctScanBudget as _SacctScanBudget
from services.orchestrator.reconcile_comment_sacct import _submitline_comment_key as _submitline_comment_key
from services.orchestrator.reconcile_comment_sacct import default_comment_sacct_querier as default_comment_sacct_querier
from services.orchestrator.reconcile_comment_sacct import default_sacct_querier as default_sacct_querier
from services.orchestrator.reconcile_identity import _FORCING_STAGE_ALIASES as _FORCING_STAGE_ALIASES
from services.orchestrator.reconcile_identity import _FORECAST_STAGE_ALIASES as _FORECAST_STAGE_ALIASES
from services.orchestrator.reconcile_identity import _GENERIC_ARRAY_JOB_NAMES as _GENERIC_ARRAY_JOB_NAMES
from services.orchestrator.reconcile_identity import _accepted_submit_versioned_job as _accepted_submit_versioned_job
from services.orchestrator.reconcile_identity import (
    _array_master_record_matches_job as _array_master_record_matches_job,
)
from services.orchestrator.reconcile_identity import _expected_job_name_token as _expected_job_name_token
from services.orchestrator.reconcile_identity import _fallback_comment_allowed as _fallback_comment_allowed
from services.orchestrator.reconcile_identity import (
    _file_cohort_runtime_identity_matches as _file_cohort_runtime_identity_matches,
)
from services.orchestrator.reconcile_identity import (
    _forcing_accounting_identity_matches as _forcing_accounting_identity_matches,
)
from services.orchestrator.reconcile_identity import _forcing_submit_reconcile_job as _forcing_submit_reconcile_job
from services.orchestrator.reconcile_identity import _identity_mapping_matches_job as _identity_mapping_matches_job
from services.orchestrator.reconcile_identity import _identity_matches as _identity_matches
from services.orchestrator.reconcile_identity import _identity_text as _identity_text
from services.orchestrator.reconcile_identity import _is_forecast_cohort_job as _is_forecast_cohort_job
from services.orchestrator.reconcile_identity import (
    _record_has_durable_identity_proof as _record_has_durable_identity_proof,
)
from services.orchestrator.reconcile_identity import _record_identity_mappings as _record_identity_mappings
from services.orchestrator.reconcile_identity import _stage_family as _stage_family
from services.orchestrator.reconcile_identity import _stages_match as _stages_match
from services.orchestrator.reconcile_inflight import ReconcileOutcome as ReconcileOutcome
from services.orchestrator.reconcile_inflight import (
    _bound_forcing_submit_reconcile_job as _bound_forcing_submit_reconcile_job,
)
from services.orchestrator.reconcile_inflight import _file_cohort_task_projections as _file_cohort_task_projections
from services.orchestrator.reconcile_inflight import _sacct_query_job_id as _sacct_query_job_id
from services.orchestrator.reconcile_inflight import (
    _terminal_file_cohort_identity_matches as _terminal_file_cohort_identity_matches,
)
from services.orchestrator.reconcile_inflight import _transition_file_runtime_status as _transition_file_runtime_status
from services.orchestrator.reconcile_inflight import reconcile_inflight_jobs as reconcile_inflight_jobs
from services.orchestrator.reconcile_records import _FALLBACK_SUBMIT_FORMATS as _FALLBACK_SUBMIT_FORMATS
from services.orchestrator.reconcile_records import ABSENCE_UNCONFIRMED_ACTION as ABSENCE_UNCONFIRMED_ACTION
from services.orchestrator.reconcile_records import FALLBACK_BASIS_NAME_WINDOW_COUNT as FALLBACK_BASIS_NAME_WINDOW_COUNT
from services.orchestrator.reconcile_records import FALLBACK_BASIS_SUBMITLINE_EXACT as FALLBACK_BASIS_SUBMITLINE_EXACT
from services.orchestrator.reconcile_records import (
    IDENTITY_MISMATCH_RELEASED_ACTION as IDENTITY_MISMATCH_RELEASED_ACTION,
)
from services.orchestrator.reconcile_records import JOURNAL_QUARANTINED_ACTION as JOURNAL_QUARANTINED_ACTION
from services.orchestrator.reconcile_records import MAX_EXACT_COMMENT_MATCHES as MAX_EXACT_COMMENT_MATCHES
from services.orchestrator.reconcile_records import MAX_FALLBACK_MASTERS as MAX_FALLBACK_MASTERS
from services.orchestrator.reconcile_records import RECONCILE_UNVERIFIED_STATUS as RECONCILE_UNVERIFIED_STATUS
from services.orchestrator.reconcile_records import RESERVATION_ABSENCE_GRACE as RESERVATION_ABSENCE_GRACE
from services.orchestrator.reconcile_records import RESERVATION_LOST_STATUS as RESERVATION_LOST_STATUS
from services.orchestrator.reconcile_records import CommentAccountingResult as CommentAccountingResult
from services.orchestrator.reconcile_records import CommentSacctQuerier as CommentSacctQuerier
from services.orchestrator.reconcile_records import ReconcileQuerySaturated as ReconcileQuerySaturated
from services.orchestrator.reconcile_records import ReconcileQueryUnavailable as ReconcileQueryUnavailable
from services.orchestrator.reconcile_records import SacctQuerier as SacctQuerier
from services.orchestrator.reconcile_records import SacctRecord as SacctRecord
from services.orchestrator.reconcile_records import _aggregate_array_member_rows as _aggregate_array_member_rows
from services.orchestrator.reconcile_records import _as_utc_datetime as _as_utc_datetime
from services.orchestrator.reconcile_records import _callable_accepts_keyword as _callable_accepts_keyword
from services.orchestrator.reconcile_records import _fallback_master_id as _fallback_master_id
from services.orchestrator.reconcile_records import _parse_fallback_submit_instant as _parse_fallback_submit_instant
from services.orchestrator.reconcile_records import _parse_master_sacct_row as _parse_master_sacct_row
from services.orchestrator.reconcile_records import _ReconcileFallbackQuerySaturated as _ReconcileFallbackQuerySaturated
from services.orchestrator.reconcile_records import (
    _ReconcileFallbackQueryUnavailable as _ReconcileFallbackQueryUnavailable,
)
from services.orchestrator.reconcile_records import _sacct_record_from_fields as _sacct_record_from_fields
from services.orchestrator.reconcile_records import _strict_utc_datetime as _strict_utc_datetime
from services.orchestrator.reconcile_reserved import (
    _reserved_record_identity_matches as _reserved_record_identity_matches,
)
from services.orchestrator.reconcile_reserved import reconcile_reserved_unbound_jobs as reconcile_reserved_unbound_jobs
from services.orchestrator.reconcile_reserved_support import ReservationReconcileOutcome as ReservationReconcileOutcome
from services.orchestrator.reconcile_reserved_support import (
    _accepted_submit_reconcile_job as _accepted_submit_reconcile_job,
)
from services.orchestrator.reconcile_reserved_support import (
    _accepted_submit_runtime_lane_eligible as _accepted_submit_runtime_lane_eligible,
)
from services.orchestrator.reconcile_reserved_support import _identity_blocked_outcome as _identity_blocked_outcome
from services.orchestrator.reconcile_reserved_support import (
    _identity_blocked_release_anchor as _identity_blocked_release_anchor,
)
from services.orchestrator.reconcile_reserved_support import _job_attempt_anchor as _job_attempt_anchor
from services.orchestrator.reconcile_reserved_support import (
    _job_identity_blocked_streak as _job_identity_blocked_streak,
)
from services.orchestrator.reconcile_reserved_support import _job_submission_attempt as _job_submission_attempt
from services.orchestrator.reconcile_reserved_support import _record_file_reconciliation as _record_file_reconciliation
from services.orchestrator.reconcile_reserved_support import (
    _release_identity_blocked_reservation as _release_identity_blocked_reservation,
)
from services.orchestrator.reconcile_reserved_support import (
    _targeted_reserved_unbound_jobs as _targeted_reserved_unbound_jobs,
)
from services.orchestrator.reconcile_reserved_support import (
    _transition_file_reconciliation as _transition_file_reconciliation,
)
from services.orchestrator.reconcile_reserved_support import (
    _transition_forcing_submit_ambiguity as _transition_forcing_submit_ambiguity,
)
from services.orchestrator.reconcile_sacct_probe import _CONTROLLER_IDENTITY_FIELDS as _CONTROLLER_IDENTITY_FIELDS
from services.orchestrator.reconcile_sacct_probe import (
    COMMENT_SACCT_VISIBILITY_TIMEOUT_SECONDS as COMMENT_SACCT_VISIBILITY_TIMEOUT_SECONDS,
)
from services.orchestrator.reconcile_sacct_probe import MAX_VISIBILITY_PROBE_BYTES as MAX_VISIBILITY_PROBE_BYTES
from services.orchestrator.reconcile_sacct_probe import MAX_VISIBILITY_PROBE_ROWS as MAX_VISIBILITY_PROBE_ROWS
from services.orchestrator.reconcile_sacct_probe import (
    _accounting_store_flags_classify as _accounting_store_flags_classify,
)
from services.orchestrator.reconcile_sacct_probe import _bounded_visibility_stdout as _bounded_visibility_stdout
from services.orchestrator.reconcile_sacct_probe import (
    _controller_array_task_identity_is_compatible as _controller_array_task_identity_is_compatible,
)
from services.orchestrator.reconcile_sacct_probe import (
    _controller_forcing_job_name_matches as _controller_forcing_job_name_matches,
)
from services.orchestrator.reconcile_sacct_probe import _controller_job_fields as _controller_job_fields
from services.orchestrator.reconcile_sacct_probe import _controller_job_records as _controller_job_records
from services.orchestrator.reconcile_sacct_probe import (
    _private_data_allows_global_jobs as _private_data_allows_global_jobs,
)
from services.orchestrator.reconcile_sacct_probe import (
    _query_forcing_controller_identity as _query_forcing_controller_identity,
)
from services.orchestrator.reconcile_sacct_probe import _terminate_and_reap as _terminate_and_reap
from services.orchestrator.reconcile_sacct_probe import default_comment_storage_probe as default_comment_storage_probe
from services.orchestrator.reconcile_sacct_probe import (
    default_global_accounting_visibility_probe as default_global_accounting_visibility_probe,
)
from services.slurm_gateway.real_backend import SLURM_JOB_ID_RE as SLURM_JOB_ID_RE

LOGGER = logging.getLogger(__name__)
