from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from packages.common.redaction import redact_payload
from services.orchestrator import scheduler as _scheduler
from services.orchestrator import scheduler_discovery as _scheduler_discovery
from services.orchestrator import scheduler_evidence as _scheduler_evidence_module
from services.orchestrator.retention import RetentionConfig, run_retention
from workers.data_adapters.base import format_cycle_time

RECONCILE_DB_CONNECT_TIMEOUT_SECONDS = _scheduler.RECONCILE_DB_CONNECT_TIMEOUT_SECONDS
RECONCILE_DB_STATEMENT_TIMEOUT_MS = _scheduler.RECONCILE_DB_STATEMENT_TIMEOUT_MS
MAX_EVIDENCE_BYTES = _scheduler_evidence_module.MAX_EVIDENCE_BYTES
SchedulerEvidenceWriteError = _scheduler_evidence_module.SchedulerEvidenceWriteError
SchedulerPassResult = _scheduler.SchedulerPassResult
SchedulerResourceLimitError = _scheduler_discovery.SchedulerResourceLimitError
UNKNOWN_AFTER_ATTEMPT = _scheduler_evidence_module.UNKNOWN_AFTER_ATTEMPT


def _LeaseHeartbeat(*args, **kwargs):
    return getattr(_scheduler, "_LeaseHeartbeat")(*args, **kwargs)


def _blocked_pass_status(*args, **kwargs):
    return getattr(_scheduler, "_blocked_pass_status")(*args, **kwargs)


def _bounded_active_slurm_jobs(*args, **kwargs):
    return getattr(_scheduler, "_bounded_active_slurm_jobs")(*args, **kwargs)


def _bounded_evidence_payload(*args, **kwargs):
    return getattr(_scheduler, "_bounded_evidence_payload")(*args, **kwargs)


def _cancel_candidate_evidence_write_blocked_evidence(*args, **kwargs):
    return getattr(_scheduler, "_cancel_candidate_evidence_write_blocked_evidence")(*args, **kwargs)


def _cancelled_job_pipeline_event_write(*args, **kwargs):
    return getattr(_scheduler, "_cancelled_job_pipeline_event_write")(*args, **kwargs)


def _cancelled_job_pipeline_status_write(*args, **kwargs):
    return getattr(_scheduler, "_cancelled_job_pipeline_status_write")(*args, **kwargs)


def _candidate_evidence_write_blocked_evidence(*args, **kwargs):
    return getattr(_scheduler, "_candidate_evidence_write_blocked_evidence")(*args, **kwargs)


def _candidate_preflight_blocked_evidence(*args, **kwargs):
    return getattr(_scheduler, "_candidate_preflight_blocked_evidence")(*args, **kwargs)


def _candidate_slurm_preflight_blocked_evidence(*args, **kwargs):
    return getattr(_scheduler, "_candidate_slurm_preflight_blocked_evidence")(*args, **kwargs)


def _empty_counts(*args, **kwargs):
    return getattr(_scheduler, "_empty_counts")(*args, **kwargs)


def _empty_model_discovery(*args, **kwargs):
    return getattr(_scheduler, "_empty_model_discovery")(*args, **kwargs)


def _ensure_utc(*args, **kwargs):
    return getattr(_scheduler, "_ensure_utc")(*args, **kwargs)


def _evidence_reservation_blocked_payload(*args, **kwargs):
    return getattr(_scheduler, "_evidence_reservation_blocked_payload")(*args, **kwargs)


def _evidence_safe(*args, **kwargs):
    return getattr(_scheduler, "_evidence_safe")(*args, **kwargs)


def _evidence_status(*args, **kwargs):
    return getattr(_scheduler, "_evidence_status")(*args, **kwargs)


def _evidence_write_error_payload(*args, **kwargs):
    return getattr(_scheduler, "_evidence_write_error_payload")(*args, **kwargs)


def _execution_write_proof(*args, **kwargs):
    return getattr(_scheduler, "_execution_write_proof")(*args, **kwargs)


def _execution_write_proof_from_evidence(*args, **kwargs):
    return getattr(_scheduler, "_execution_write_proof_from_evidence")(*args, **kwargs)


def _format_utc(*args, **kwargs):
    return getattr(_scheduler, "_format_utc")(*args, **kwargs)


def _no_mutation_proof(*args, **kwargs):
    return getattr(_scheduler, "_no_mutation_proof")(*args, **kwargs)


def _now(*args, **kwargs):
    return getattr(_scheduler, "_now")(*args, **kwargs)


def _open_evidence_directory(*args, **kwargs):
    return getattr(_scheduler, "_open_evidence_directory")(*args, **kwargs)


def _require_evidence_artifact_available(*args, **kwargs):
    return getattr(_scheduler, "_require_evidence_artifact_available")(*args, **kwargs)


def _restart_reconcile_proof(*args, **kwargs):
    return getattr(_scheduler, "_restart_reconcile_proof")(*args, **kwargs)


def _require_safe_directory_final_component(*args, **kwargs):
    return getattr(_scheduler, "_require_safe_directory_final_component")(*args, **kwargs)


def _require_under_workspace(*args, **kwargs):
    return getattr(_scheduler, "_require_under_workspace")(*args, **kwargs)


def _scheduler_cancellation_status(*args, **kwargs):
    return getattr(_scheduler, "_scheduler_cancellation_status")(*args, **kwargs)


def _scheduler_evidence(*args, **kwargs):
    return getattr(_scheduler, "_scheduler_evidence")(*args, **kwargs)


def _scheduler_execution_boundary_from_cancellation(*args, **kwargs):
    return getattr(_scheduler, "_scheduler_execution_boundary_from_cancellation")(*args, **kwargs)


def _scheduler_failed_count_from_execution(*args, **kwargs):
    return getattr(_scheduler, "_scheduler_failed_count_from_execution")(*args, **kwargs)


def _scheduler_lock_evidence_root_preflight(*args, **kwargs):
    return getattr(_scheduler, "_scheduler_lock_evidence_root_preflight")(*args, **kwargs)


def _scheduler_mutation_proof(*args, **kwargs):
    return getattr(_scheduler, "_scheduler_mutation_proof")(*args, **kwargs)


def _scheduler_partial_count_from_execution(*args, **kwargs):
    return getattr(_scheduler, "_scheduler_partial_count_from_execution")(*args, **kwargs)


def _scheduler_pass_status_from_cancellation(*args, **kwargs):
    return getattr(_scheduler, "_scheduler_pass_status_from_cancellation")(*args, **kwargs)


def _scheduler_pass_status_from_execution(*args, **kwargs):
    return getattr(_scheduler, "_scheduler_pass_status_from_execution")(*args, **kwargs)


def _scheduler_runtime_root_preflight(*args, **kwargs):
    return getattr(_scheduler, "_scheduler_runtime_root_preflight")(*args, **kwargs)


def _slurm_cancellation_blocked_count(*args, **kwargs):
    return getattr(_scheduler, "_slurm_cancellation_blocked_count")(*args, **kwargs)


def _slurm_cancellation_proof(*args, **kwargs):
    return getattr(_scheduler, "_slurm_cancellation_proof")(*args, **kwargs)


def _slurm_cancellation_proof_from_evidence(*args, **kwargs):
    return getattr(_scheduler, "_slurm_cancellation_proof_from_evidence")(*args, **kwargs)


def _slurm_cancellation_unknown_count(*args, **kwargs):
    return getattr(_scheduler, "_slurm_cancellation_unknown_count")(*args, **kwargs)


def _slurm_cancelled_count(*args, **kwargs):
    return getattr(_scheduler, "_slurm_cancelled_count")(*args, **kwargs)


def _slurm_preflight(*args, **kwargs):
    return getattr(_scheduler, "_slurm_preflight")(*args, **kwargs)


def _slurm_status_sync_count(*args, **kwargs):
    return getattr(_scheduler, "_slurm_status_sync_count")(*args, **kwargs)


def _slurm_status_sync_failed(*args, **kwargs):
    return getattr(_scheduler, "_slurm_status_sync_failed")(*args, **kwargs)


def _slurm_status_sync_mutated(*args, **kwargs):
    return getattr(_scheduler, "_slurm_status_sync_mutated")(*args, **kwargs)


def _slurm_status_sync_proof(*args, **kwargs):
    return getattr(_scheduler, "_slurm_status_sync_proof")(*args, **kwargs)


def _slurm_status_sync_proof_from_candidates(*args, **kwargs):
    return getattr(_scheduler, "_slurm_status_sync_proof_from_candidates")(*args, **kwargs)


def _slurm_status_sync_unknown_count(*args, **kwargs):
    return getattr(_scheduler, "_slurm_status_sync_unknown_count")(*args, **kwargs)


def _sync_candidate_evidence_write_blocked_evidence(*args, **kwargs):
    return getattr(_scheduler, "_sync_candidate_evidence_write_blocked_evidence")(*args, **kwargs)


def _write_new_regular_file(*args, **kwargs):
    return getattr(_scheduler, "_write_new_regular_file")(*args, **kwargs)


def _db_free_lock_evidence(config: Any, value: Mapping[str, Any]) -> dict[str, Any]:
    if not getattr(config, "db_free_required", False):
        return redact_payload(dict(value))
    return _db_free_safe_lock_result(value)


def _db_free_safe_lock_result(value: Mapping[str, Any]) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for key in ("acquired", "contention"):
        if key in value:
            evidence[key] = bool(value.get(key))
    if "lock_path" in value:
        evidence["lock_path"] = "[local-path]"
    if "lock_type" in value:
        evidence["lock_type"] = _db_free_safe_lock_code(value.get("lock_type"))
    if "reason" in value:
        evidence["reason"] = _db_free_safe_lock_code(value.get("reason"))
    if "error_type" in value:
        evidence["error_type"] = _db_free_safe_lock_code(value.get("error_type"))
    if "existing_lock" in value:
        evidence["existing_lock"] = _db_free_safe_lock_payload(value.get("existing_lock"))
    if "lease" in value:
        evidence["lease"] = _db_free_safe_lock_payload(value.get("lease"))
    return evidence


def _db_free_safe_lock_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"raw": None if value is None else "[lock-payload]"}
    if value.get("raw") is None:
        payload: dict[str, Any] = {"raw": None}
        for key in ("size_bytes", "max_bytes"):
            item = value.get(key)
            if isinstance(item, int) and not isinstance(item, bool):
                payload[key] = item
        if set(value).issubset(payload.keys()):
            return payload
    scheduler_owned = (
        value.get("owner") == _scheduler.LOCK_OWNER
        and value.get("schema_version") == _scheduler.LOCK_SCHEMA_VERSION
    )
    if not scheduler_owned:
        return {"raw": "[lock-payload]"}
    payload = {
        "owner": _scheduler.LOCK_OWNER,
        "schema_version": _scheduler.LOCK_SCHEMA_VERSION,
    }
    for key in ("pass_id", "heartbeat_at", "started_at"):
        if key in value:
            payload[key] = _db_free_safe_lock_code(value.get(key))
    for key in ("pid", "heartbeat_seq"):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool):
            payload[key] = item
    if "lock_path" in value:
        payload["lock_path"] = "[local-path]"
    return payload


def _db_free_safe_lock_code(value: Any) -> str:
    text = "" if value is None else str(value)
    lower = text.lower()
    if (
        "/" in text
        or "\\" in text
        or "postgres" in lower
        or "psycopg" in lower
        or any(
            word in lower
            for word in (
                "token",
                "password",
                "passwd",
                "pwd",
                "secret",
                "credential",
                "api_key",
                "apikey",
                "access_key",
                "accesskey",
                "session_key",
                "signature",
            )
        )
    ):
        return "[lock-payload]"
    if 0 < len(text) <= 256 and all(character.isalnum() or character in "._:-" for character in text):
        return text
    return "[lock-payload]"


def run_once(self) -> SchedulerPassResult:
    self._source_readiness_context_cache.clear()
    started_at = _now(self.config)
    pass_id = f"scheduler_{format_cycle_time(started_at)}_{_scheduler.uuid4().hex[:12]}"
    db_free_required = bool(getattr(self.config, "db_free_required", False))
    root_preflight = (
        _scheduler_runtime_root_preflight(self.config)
        if db_free_required
        else _scheduler_lock_evidence_root_preflight(self.config)
    )
    if root_preflight["status"] == "blocked":
        lock_payload = {
            "acquired": False,
            "contention": False,
            "lock_path": str(self.config.lock_path),
            "reason": "scheduler_root_preflight_blocked",
        }
        if db_free_required:
            lock_payload["lock_type"] = "file"
        evidence = self._base_evidence(pass_id, started_at)
        evidence.update(
            {
                "status": "preflight_blocked",
                "finished_at": _format_utc(_now(self.config)),
                "lock": _db_free_lock_evidence(self.config, lock_payload),
                "root_preflight": root_preflight,
                "counts": _empty_counts(),
                "candidates": [],
                "blocked_candidates": [],
                "skipped_candidates": [],
                "duplicate_exclusions": list(self.config.source_exclusions),
                "model_discovery": _empty_model_discovery(),
                "source_cycles": [],
                "model_run_evidence": [],
                "slurm_cancellation_evidence": [],
                "no_mutation_proof": _no_mutation_proof(),
                "execution_boundary": "scheduler_root_preflight_blocked",
            }
        )
        artifact_path = self._write_prelock_blocked_evidence(pass_id, evidence, root_preflight)
        return SchedulerPassResult(
            pass_id=pass_id,
            status="preflight_blocked",
            evidence=evidence,
            artifact_path=artifact_path,
        )
    db_free_preflight = self.config.db_free_runtime_preflight()
    if db_free_preflight["status"] == "blocked":
        evidence = self._base_evidence(pass_id, started_at)
        evidence.update(
            {
                "status": "preflight_blocked",
                "finished_at": _format_utc(_now(self.config)),
                "lock": _db_free_lock_evidence(
                    self.config,
                    {
                        "acquired": False,
                        "contention": False,
                        "lock_path": str(self.config.lock_path),
                        "lock_type": "file" if self.config.db_free_required else None,
                        "reason": "db_free_runtime_preflight_blocked",
                    },
                ),
                "root_preflight": root_preflight,
                "db_free_runtime": db_free_preflight,
                "counts": _empty_counts(),
                "candidates": [],
                "blocked_candidates": [],
                "skipped_candidates": [],
                "duplicate_exclusions": list(self.config.source_exclusions),
                "model_discovery": _empty_model_discovery(),
                "source_cycles": [],
                "model_run_evidence": [],
                "slurm_cancellation_evidence": [],
                "no_mutation_proof": _no_mutation_proof(),
                "execution_boundary": "db_free_runtime_preflight_blocked",
            }
        )
        artifact_path = self._write_prelock_blocked_evidence(pass_id, evidence, root_preflight)
        return SchedulerPassResult(
            pass_id=pass_id,
            status="preflight_blocked",
            evidence=evidence,
            artifact_path=artifact_path,
        )
    lock = self._build_scheduler_lease()
    lock_result = lock.acquire(pass_id=pass_id, started_at=started_at)
    lock_evidence = _db_free_lock_evidence(self.config, lock_result)
    if not lock_result["acquired"]:
        evidence = self._base_evidence(pass_id, started_at)
        evidence.update(
            {
                "status": "lock_contended",
                "finished_at": _format_utc(_now(self.config)),
                "lock": lock_evidence,
                "counts": _empty_counts(),
                "candidates": [],
                "blocked_candidates": [],
                "model_discovery": _empty_model_discovery(),
                "source_cycles": [],
                "no_mutation_proof": _no_mutation_proof(),
                "execution_boundary": "scheduler_lock_contended",
            }
        )
        if self.config.db_free_required:
            evidence["db_free_runtime"] = db_free_preflight
        artifact_path = self._write_evidence(pass_id, evidence)
        status = _evidence_status(evidence, "lock_contended")
        return SchedulerPassResult(
            pass_id=pass_id,
            status=status,
            evidence=evidence,
            artifact_path=artifact_path,
        )

    heartbeat = _LeaseHeartbeat(lock, pass_id, max(1, self.config.lock_ttl_seconds // 3))
    heartbeat.start()
    try:
        if not db_free_required:
            root_preflight = _scheduler_runtime_root_preflight(self.config)
        if not db_free_required and root_preflight["status"] == "blocked":
            finished_at = _now(self.config)
            evidence = self._base_evidence(pass_id, started_at)
            evidence.update(
                {
                    "status": "preflight_blocked",
                    "finished_at": _format_utc(finished_at),
                    "lock": lock_evidence,
                    "root_preflight": root_preflight,
                    "counts": _empty_counts(),
                    "candidates": [],
                    "blocked_candidates": [],
                    "skipped_candidates": [],
                    "duplicate_exclusions": list(self.config.source_exclusions),
                    "model_discovery": _empty_model_discovery(),
                    "source_cycles": [],
                    "model_run_evidence": [],
                    "slurm_cancellation_evidence": [],
                    "no_mutation_proof": _no_mutation_proof(),
                    "execution_boundary": "scheduler_root_preflight_blocked",
                }
            )
            artifact_path = self._write_evidence(pass_id, evidence)
            status = _evidence_status(evidence, "preflight_blocked")
            return SchedulerPassResult(
                pass_id=pass_id,
                status=status,
                evidence=evidence,
                artifact_path=artifact_path,
            )
        # M24 §3A: before planning/submitting this pass, recover any jobs
        # stuck in the submit-crash window (reserved-unbound) and refresh
        # in-flight statuses from accounting. Comment-reconcile finds back a
        # crashed cohort's slurm_job_id so we never re-submit an already
        # in-flight cohort.
        db_free_file_provider_blocker = getattr(self, "_db_free_file_provider_blocker", None)
        if self.config.db_free_required and db_free_file_provider_blocker is not None:
            finished_at = _now(self.config)
            evidence = self._base_evidence(pass_id, started_at)
            evidence.update(
                {
                    "status": "preflight_blocked",
                    "finished_at": _format_utc(finished_at),
                    "lock": lock_evidence,
                    "root_preflight": root_preflight,
                    "db_free_runtime": {
                        **db_free_preflight,
                        "provider_blocker": redact_payload(dict(db_free_file_provider_blocker)),
                    },
                    "counts": _empty_counts(),
                    "candidates": [],
                    "blocked_candidates": [],
                    "skipped_candidates": [],
                    "duplicate_exclusions": list(self.config.source_exclusions),
                    "model_discovery": _empty_model_discovery(),
                    "source_cycles": [],
                    "model_run_evidence": [],
                    "slurm_cancellation_evidence": [],
                    "no_mutation_proof": _no_mutation_proof(),
                    "execution_boundary": "db_free_file_provider_blocked",
                }
            )
            artifact_path = self._write_evidence(pass_id, evidence)
            status = _evidence_status(evidence, "preflight_blocked")
            return SchedulerPassResult(
                pass_id=pass_id,
                status=status,
                evidence=evidence,
                artifact_path=artifact_path,
            )
        restart_reconcile_evidence = self._run_restart_reconcile()
        restart_reconcile_proof = _restart_reconcile_proof(restart_reconcile_evidence)
        models, model_evidence = self._discover_models()
        cycles, source_cycle_evidence = self._discover_cycles(started_at, models=models)
        (
            candidates,
            blocked_candidates,
            skipped_candidates,
            candidate_duplicate_exclusions,
            slurm_status_sync_evidence,
        ) = self._build_candidates(models=models, cycles=cycles)
        cancellation_evidence: list[dict[str, Any]] = []
        pending_cancel_candidates = [
            candidate for candidate in skipped_candidates if candidate.get("reason") == "cancel_requested_active_slurm"
        ]
        cancel_active_slurm_requested = (
            self.config.cancel_active_slurm and not self.config.dry_run and bool(pending_cancel_candidates)
        )
        execution_evidence: list[dict[str, Any]] = []
        submitted_count = 0
        failed_count = 0
        partial_count = 0
        execution_boundary = "planning_only"
        pass_status = "planned"
        no_mutation_proof = _no_mutation_proof()
        execution_write_proof = _execution_write_proof()
        slurm_preflight_evidence: dict[str, Any] | None = None
        evidence_reservation: dict[str, Any] = {"status": "not_required"}
        pending_status_sync_candidates = [
            candidate
            for candidate in skipped_candidates
            if candidate.get("reason") == "active_slurm_status_sync_deferred"
        ]
        slurm_status_sync_proof = _slurm_status_sync_proof(sync_required=bool(pending_status_sync_candidates))
        slurm_cancellation_proof = _slurm_cancellation_proof()
        mutation_candidate_count = (
            len(candidates) + len(pending_cancel_candidates) + len(pending_status_sync_candidates)
        )
        # §4.2 lease: if the heartbeat reports our lease was taken over
        # mid-pass, short-circuit BEFORE any submission/cancellation so we
        # never race the new holder at the DB layer. The #290 DB reservation
        # would still prevent a real double-submit, but executing a doomed
        # pass wastes work and muddies evidence. Fall through to finally,
        # which stops the heartbeat and token-CAS releases the lock (a no-op
        # if it was already reclaimed).
        if heartbeat.lost:
            finished_at = _now(self.config)
            evidence = self._base_evidence(pass_id, started_at)
            evidence.update(
                {
                    "status": "lease_lost",
                    "finished_at": _format_utc(finished_at),
                    "lock": lock_evidence,
                    "counts": _empty_counts(),
                    "candidates": [],
                    "blocked_candidates": [],
                    "skipped_candidates": [],
                    "duplicate_exclusions": list(self.config.source_exclusions),
                    "model_discovery": _empty_model_discovery(),
                    "source_cycles": [],
                    "no_mutation_proof": _no_mutation_proof(),
                    "execution_boundary": "lease_lost",
                }
            )
            if root_preflight["status"] != "not_required":
                evidence["root_preflight"] = root_preflight
            artifact_path = self._write_evidence(pass_id, evidence)
            status = _evidence_status(evidence, "lease_lost")
            return SchedulerPassResult(
                pass_id=pass_id,
                status=status,
                evidence=evidence,
                artifact_path=artifact_path,
            )
        if not self.config.dry_run and mutation_candidate_count:
            if evidence_reservation["status"] == "not_required":
                evidence_reservation = self._reserve_pre_execution_evidence(
                    pass_id,
                    started_at,
                    mutation_candidate_count,
                )
            if evidence_reservation["status"] == "blocked":
                execution_evidence = [
                    _candidate_evidence_write_blocked_evidence(candidate, evidence_reservation)
                    for candidate in candidates
                ]
                execution_write_proof = _execution_write_proof_from_evidence(
                    execution_evidence,
                    reservation=evidence_reservation,
                )
                execution_evidence.extend(
                    _sync_candidate_evidence_write_blocked_evidence(candidate, evidence_reservation)
                    for candidate in pending_status_sync_candidates
                )
                cancellation_evidence = [
                    _cancel_candidate_evidence_write_blocked_evidence(candidate, evidence_reservation)
                    for candidate in pending_cancel_candidates
                ]
                execution_boundary = "evidence_preflight_blocked"
                pass_status = "preflight_blocked"
                slurm_status_sync_proof = _slurm_status_sync_proof(
                    sync_required=bool(pending_status_sync_candidates),
                    reservation=evidence_reservation,
                    blocked=True,
                )
                slurm_cancellation_proof = _slurm_cancellation_proof(
                    cancellation_required=bool(pending_cancel_candidates),
                    reservation=evidence_reservation,
                    blocked=True,
                )
            else:
                if pending_status_sync_candidates:
                    (
                        candidates,
                        blocked_candidates,
                        skipped_candidates,
                        candidate_duplicate_exclusions,
                        slurm_status_sync_evidence,
                    ) = self._build_candidates(
                        models=models,
                        cycles=cycles,
                        allow_slurm_status_sync=True,
                    )
                    pending_cancel_candidates = [
                        candidate
                        for candidate in skipped_candidates
                        if candidate.get("reason") == "cancel_requested_active_slurm"
                    ]
                    cancel_active_slurm_requested = (
                        self.config.cancel_active_slurm and not self.config.dry_run and bool(pending_cancel_candidates)
                    )
                slurm_status_sync_proof = _slurm_status_sync_proof_from_candidates(
                    slurm_status_sync_evidence,
                    reservation=evidence_reservation,
                )
                if _slurm_status_sync_failed(slurm_status_sync_proof):
                    pass_status = "slurm_status_sync_failed"
                    execution_boundary = "slurm_status_sync"
                else:
                    if cancel_active_slurm_requested:
                        cancellation_evidence = self._cancel_requested_active_slurm(skipped_candidates)
                        slurm_cancellation_proof = _slurm_cancellation_proof_from_evidence(
                            cancellation_evidence,
                            reservation=evidence_reservation,
                        )
                    if candidates and self.forcing_producer is not None:
                        (
                            candidates,
                            forcing_blocked_candidates,
                            forcing_evidence,
                        ) = self._produce_forcing_for_candidates(candidates)
                        blocked_candidates.extend(forcing_blocked_candidates)
                        execution_evidence.extend(forcing_evidence)
                    if candidates:
                        slurm_preflight = _slurm_preflight(self.config)
                        if slurm_preflight["status"] != "not_required":
                            slurm_preflight_evidence = redact_payload(slurm_preflight)
                        if slurm_preflight["status"] == "blocked":
                            execution_evidence.extend(
                                [
                                    _candidate_slurm_preflight_blocked_evidence(candidate, slurm_preflight)
                                    for candidate in candidates
                                ]
                            )
                            execution_write_proof = _execution_write_proof_from_evidence(
                                execution_evidence,
                                reservation=evidence_reservation,
                            )
                            execution_boundary = "slurm_preflight_blocked"
                            pass_status = "preflight_blocked"
                        elif self.orchestrator_factory is None and not self.config.slurm_execution_enabled:
                            execution_evidence.extend(
                                [
                                    _candidate_preflight_blocked_evidence(candidate, config=self.config)
                                    for candidate in candidates
                                ]
                            )
                            execution_write_proof = _execution_write_proof_from_evidence(
                                execution_evidence,
                                reservation=evidence_reservation,
                            )
                            execution_boundary = "preflight_blocked"
                            pass_status = "preflight_blocked"
                            no_mutation_proof = _no_mutation_proof()
                        else:
                            execution_evidence.extend(self._execute_candidates(candidates))
                            execution_write_proof = _execution_write_proof_from_evidence(
                                execution_evidence,
                                reservation=evidence_reservation,
                            )
                            submitted_count = sum(1 for item in execution_evidence if item.get("submitted") is True)
                            execution_boundary = (
                                "slurm_gateway_orchestration"
                                if self.config.slurm_execution_enabled
                                else "production_orchestration"
                            )
            if execution_evidence:
                pass_status = _scheduler_pass_status_from_execution(execution_evidence)
            if cancellation_evidence and not execution_evidence:
                pass_status = _scheduler_pass_status_from_cancellation(cancellation_evidence)
                execution_boundary = _scheduler_execution_boundary_from_cancellation(cancellation_evidence)
            elif cancellation_evidence and pass_status == "planned":
                pass_status = _scheduler_pass_status_from_cancellation(cancellation_evidence)
                execution_boundary = _scheduler_execution_boundary_from_cancellation(cancellation_evidence)
            if (
                pass_status == "planned"
                and execution_boundary == "planning_only"
                and _slurm_status_sync_mutated(slurm_status_sync_proof)
            ):
                pass_status = "slurm_status_synced"
                execution_boundary = "slurm_status_sync"
            if pass_status == "planned" and not candidates and blocked_candidates:
                pass_status = _blocked_pass_status(blocked_candidates)
            scheduler_mutation_proof = _scheduler_mutation_proof(
                execution_write_proof=execution_write_proof,
                slurm_status_sync_proof=slurm_status_sync_proof,
                slurm_cancellation_proof=slurm_cancellation_proof,
                restart_reconcile_proof=restart_reconcile_proof,
            )
            no_mutation_proof = {
                "adapter_download_called": False,
                "slurm_submit_called": scheduler_mutation_proof["slurm_submit_called"],
                "slurm_status_sync_called": slurm_status_sync_proof.get("sync_called") is True,
                "slurm_cancellation_called": slurm_cancellation_proof.get("cancel_called") is True,
                "shud_runtime_called": False,
                "hydro_result_table_writes": scheduler_mutation_proof["hydro_result_table_writes"],
                "met_result_table_writes": scheduler_mutation_proof["met_result_table_writes"],
                "pipeline_status_writes": scheduler_mutation_proof["pipeline_status_writes"],
                "pipeline_event_writes": scheduler_mutation_proof["pipeline_event_writes"],
            }
            if scheduler_mutation_proof.get("restart_reconcile_writes") is not False:
                no_mutation_proof["restart_reconcile_writes"] = scheduler_mutation_proof["restart_reconcile_writes"]
            failed_count = _scheduler_failed_count_from_execution(execution_evidence)
            partial_count = _scheduler_partial_count_from_execution(execution_evidence)
        elif restart_reconcile_proof.get("mutation_occurred") is True:
            no_mutation_proof = {
                **_no_mutation_proof(),
                "pipeline_status_writes": True,
                "pipeline_event_writes": restart_reconcile_proof.get("pipeline_event_writes") is True,
                "restart_reconcile_writes": True,
            }
            if execution_boundary == "planning_only":
                execution_boundary = "restart_reconcile"
            if pass_status == "planned":
                pass_status = "restart_reconciled"
        elif restart_reconcile_proof.get("mutation_occurred") == UNKNOWN_AFTER_ATTEMPT:
            no_mutation_proof = {
                **_no_mutation_proof(),
                "pipeline_status_writes": restart_reconcile_proof.get(
                    "pipeline_status_writes",
                    UNKNOWN_AFTER_ATTEMPT,
                ),
                "pipeline_event_writes": restart_reconcile_proof.get(
                    "pipeline_event_writes",
                    UNKNOWN_AFTER_ATTEMPT,
                ),
                "restart_reconcile_writes": UNKNOWN_AFTER_ATTEMPT,
            }
            if execution_boundary == "planning_only":
                execution_boundary = "restart_reconcile"
            if pass_status == "planned":
                pass_status = "restart_reconcile_unknown"
        finished_at = _now(self.config)
        evidence = self._base_evidence(pass_id, started_at)
        evidence["operator_filters"].update(model_evidence["operator_filters"])
        evidence["filters"] = dict(evidence["operator_filters"])
        duplicate_exclusions = [
            *self.config.source_exclusions,
            *[item for item in source_cycle_evidence if item.get("status") == "excluded"],
            *candidate_duplicate_exclusions,
        ]
        total_candidate_count = len(candidates) + len(blocked_candidates) + len(skipped_candidates)
        evidence.update(
            {
                "status": pass_status,
                "finished_at": _format_utc(finished_at),
                "lock": lock_evidence,
                "model_discovery": model_evidence,
                "source_cycles": source_cycle_evidence,
                "candidates": [candidate.to_dict() for candidate in candidates],
                "blocked_candidates": [candidate.to_dict() for candidate in blocked_candidates],
                "skipped_candidates": skipped_candidates,
                "duplicate_exclusions": duplicate_exclusions,
                "counts": {
                    "candidate_count": total_candidate_count,
                    "blocked_candidate_count": len(blocked_candidates),
                    "skipped_candidate_count": len(skipped_candidates),
                    "selected_model_count": len(models),
                    "source_cycle_count": len(cycles),
                    "submitted_count": submitted_count,
                    "failed_count": failed_count,
                    "partial_count": partial_count,
                    "slurm_status_sync_count": _slurm_status_sync_count(slurm_status_sync_proof),
                    "slurm_status_sync_unknown_count": _slurm_status_sync_unknown_count(
                        slurm_status_sync_proof,
                    ),
                    "slurm_cancelled_count": _slurm_cancelled_count(cancellation_evidence),
                    "slurm_cancellation_blocked_count": _slurm_cancellation_blocked_count(
                        cancellation_evidence,
                    ),
                    "slurm_cancellation_unknown_count": _slurm_cancellation_unknown_count(
                        slurm_cancellation_proof,
                    ),
                },
                "model_run_evidence": execution_evidence,
                "execution_write_proof": execution_write_proof,
                "slurm_cancellation_evidence": cancellation_evidence,
                "slurm_status_sync_proof": slurm_status_sync_proof,
                "slurm_cancellation_proof": slurm_cancellation_proof,
                "no_mutation_proof": no_mutation_proof,
                "execution_boundary": execution_boundary,
            }
        )
        if restart_reconcile_evidence is not None:
            evidence["restart_reconcile"] = restart_reconcile_evidence
            evidence["restart_reconcile_proof"] = restart_reconcile_proof
        overlap_receipt = getattr(self, "_last_submit_overlap_receipt", None)
        if overlap_receipt is not None:
            # M24 §3A Evidence Floor: archive the overlapping-submit receipt
            # into the durable pass artifact (not just memory) so
            # "receipt shows overlapping submits" has on-disk proof.
            evidence["submit_overlap_receipt"] = overlap_receipt.to_dict()
        if slurm_preflight_evidence is not None:
            evidence["slurm_preflight"] = slurm_preflight_evidence
        if evidence_reservation["status"] != "not_required":
            evidence["evidence_pre_execution"] = evidence_reservation
        if root_preflight["status"] != "not_required":
            evidence["root_preflight"] = root_preflight
        if self.config.backfill_enabled:
            evidence["backfill"] = {
                "enabled": True,
                "lookback_hours": self.config.lookback_hours,
                "audit": [item for item in source_cycle_evidence if item.get("type") == "backfill_audit"],
            }
        else:
            evidence["backfill"] = {"enabled": False}
        retention_force_reason = (
            "evidence_preflight_blocked" if evidence_reservation.get("status") == "blocked" else None
        )
        evidence["retention"] = self._run_retention(
            started_at,
            force_dry_run_reason=retention_force_reason,
        )
        try:
            artifact_path = self._write_evidence(pass_id, evidence)
        except (OSError, RuntimeError, ValueError, SchedulerEvidenceWriteError) as error:
            if evidence_reservation.get("status") != "blocked":
                raise
            evidence["evidence_write_error"] = _evidence_write_error_payload(error, self.config)
            artifact_path = None
        status = _evidence_status(evidence, pass_status)
        return SchedulerPassResult(
            pass_id=pass_id,
            status=status,
            evidence=evidence,
            artifact_path=artifact_path,
        )
    except SchedulerResourceLimitError as error:
        finished_at = _now(self.config)
        evidence = self._base_evidence(pass_id, started_at)
        evidence.update(
            {
                "status": "resource_limit_blocked",
                "finished_at": _format_utc(finished_at),
                "lock": lock_evidence,
                "limit": {"reason": error.reason, **error.details},
                "counts": _empty_counts(),
                "candidates": [],
                "blocked_candidates": [],
                "skipped_candidates": [],
                "duplicate_exclusions": list(self.config.source_exclusions),
                "model_discovery": _empty_model_discovery(),
                "source_cycles": [],
                "no_mutation_proof": _no_mutation_proof(),
                "execution_boundary": "planning_only",
            }
        )
        if root_preflight["status"] != "not_required":
            evidence["root_preflight"] = root_preflight
        artifact_path = self._write_evidence(pass_id, evidence)
        status = _evidence_status(evidence, "resource_limit_blocked")
        return SchedulerPassResult(
            pass_id=pass_id,
            status=status,
            evidence=evidence,
            artifact_path=artifact_path,
        )
    finally:
        try:
            heartbeat.stop()
        except Exception:
            pass
        lock.release(pass_id=pass_id)


def _run_restart_reconcile(self) -> dict[str, Any] | None:
    """Recover submit-crash and in-flight jobs at the start of an exec pass.

    Reconcile is read-only w.r.t. submission: it binds reserved-unbound rows
    back to their real slurm_job_id via the idempotency ``--comment`` and
    refreshes in-flight statuses from accounting. It NEVER re-submits, so an
    already in-flight cohort is recovered, not duplicated. Best-effort:
    failures are recorded but never abort the pass.
    """

    if self.config.dry_run or not self.config.restart_reconcile_enabled:
        return None
    store = self._restart_reconcile_store()
    if store is None:
        build_error = getattr(self, "_reconcile_store_build_error", None)
        if build_error is not None:
            return {
                "status": "skipped",
                "reason": "reconcile_store_build_failed",
                "error_type": build_error,
            }
        return {"status": "skipped", "reason": "reconcile_store_unavailable"}

    from services.orchestrator.reconcile import (
        reconcile_inflight_jobs,
        reconcile_reserved_unbound_jobs,
    )

    evidence: dict[str, Any] = {"status": "completed"}
    try:
        comment_query = self._restart_reconcile_comment_query()
        reserved = reconcile_reserved_unbound_jobs(store, comment_query=comment_query)
        evidence["reserved_unbound"] = {
            "count": len(reserved),
            "outcomes": [
                {
                    "job_id": o.job_id,
                    "idempotency_key": o.idempotency_key,
                    "action": o.action,
                    "status": o.status,
                    "slurm_job_id": o.slurm_job_id,
                }
                for o in reserved
            ],
        }
    except Exception as error:  # noqa: BLE001 - recovery must never abort the pass.
        evidence["status"] = "error"
        evidence["reserved_unbound_error"] = str(error)
        self._reset_reconcile_store_after_error()

    try:
        sacct_query = self._restart_reconcile_sacct_query()
        inflight = reconcile_inflight_jobs(store, sacct_query=sacct_query)
        evidence["inflight"] = {
            "count": len(inflight),
            "outcomes": [
                {
                    "job_id": o.job_id,
                    "slurm_job_id": o.slurm_job_id,
                    "action": o.action,
                    "status": o.status,
                }
                for o in inflight
            ],
        }
    except Exception as error:  # noqa: BLE001 - recovery must never abort the pass.
        evidence["status"] = "error"
        evidence["inflight_error"] = str(error)
        self._reset_reconcile_store_after_error()
    return evidence


def _reset_reconcile_store_after_error(self) -> None:
    """Recover the cached reconcile session after a write/commit failure.

    persistence commits with no rollback, so a failed commit leaves the
    cached session in pending-rollback state; reusing it next pass (or in
    the same pass's inflight segment) raises PendingRollbackError and
    silently kills crash recovery for the daemon's lifetime. Roll the
    session back to keep its connection reusable; only if rollback itself
    fails (the connection is truly dead) dispose the engine pool and drop
    the cache so the next pass rebuilds a clean store via
    _restart_reconcile_store.
    """

    store = self._reconcile_store
    if store is None:
        return
    try:
        store.session.rollback()
    except Exception:  # noqa: BLE001 - poisoned/dead session: dispose + drop so
        # the next pass rebuilds a clean one via _restart_reconcile_store.
        try:
            bind = store.session.get_bind()
            store.session.close()
            if hasattr(bind, "dispose"):
                bind.dispose()
        except Exception:  # noqa: BLE001 - cleanup is best-effort; never abort the pass.
            pass
        self._reconcile_store = None


def _restart_reconcile_store(self) -> Any | None:
    if self._reconcile_store is not None:
        return self._reconcile_store
    database_url = (self.config.database_url or "").strip()
    if not database_url:
        return None
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from services.orchestrator.persistence import PipelineStore

    # Best-effort: a malformed/unbuildable database_url must never abort the
    # pass. make_url() raises synchronously inside create_engine for a bad
    # DSN, so wrap the whole build. ZERO-LEAK: record only the exception
    # class name (provably secret-free); the raw message embeds the DSN
    # incl. password. The submit-path DB-host preflight still runs.
    try:
        engine = create_engine(
            database_url,
            future=True,
            connect_args={
                "connect_timeout": RECONCILE_DB_CONNECT_TIMEOUT_SECONDS,
                "options": f"-c statement_timeout={RECONCILE_DB_STATEMENT_TIMEOUT_MS}",
            },
        )
        self._reconcile_store = PipelineStore(Session(engine))
    except Exception as error:  # noqa: BLE001 - build must never abort the pass.
        self._reconcile_store_build_error = type(error).__name__
        return None
    self._reconcile_store_build_error = None
    return self._reconcile_store


def _restart_reconcile_comment_query(self) -> Callable[[str], Any]:
    if self._reconcile_comment_query is not None:
        return self._reconcile_comment_query
    from services.orchestrator.reconcile import default_comment_sacct_querier

    return default_comment_sacct_querier()


def _restart_reconcile_sacct_query(self) -> Callable[[str], Any]:
    if self._reconcile_sacct_query is not None:
        return self._reconcile_sacct_query
    from services.orchestrator.reconcile import default_sacct_querier

    return default_sacct_querier()


def _run_retention(
    self,
    started_at: datetime,
    *,
    force_dry_run_reason: str | None = None,
) -> dict[str, Any]:
    """Run forecast-data retention cleanup; never break the scheduling pass.

    Scheduler ``dry_run`` is the master switch: when the pass runs in
    dry-run (planning-only, no side effects), retention is forced into
    dry-run too, regardless of NHMS_RETENTION_DRY_RUN. This preserves the
    "dry_run => no side effects" contract so a planning pass never deletes
    aged artifacts even when the env enables real deletion. Evidence
    preflight failures use the same boundary because they claim no production
    mutation has happened yet.
    """
    retention_config = RetentionConfig.from_env()
    if not retention_config.enabled:
        return {"status": "disabled", "enabled": False}
    forced_dry_run = False
    force_dry_run = self.config.dry_run or force_dry_run_reason is not None
    if force_dry_run and not retention_config.dry_run:
        retention_config = replace(retention_config, dry_run=True)
        forced_dry_run = True
    try:
        result = run_retention(
            object_store_root=self.config.object_store_root,
            now=started_at,
            config=retention_config,
            published_artifact_root=self.config.published_artifact_root,
        )
    except Exception as error:  # noqa: BLE001 - cleanup must never abort scheduling
        return {"status": "error", "enabled": True, "error": str(error)}
    payload = result.to_dict()
    payload["status"] = "completed"
    if forced_dry_run:
        payload["forced_dry_run_by_scheduler"] = True
        payload["forced_dry_run_reason"] = force_dry_run_reason or "scheduler_dry_run"
    return payload


def _write_prelock_blocked_evidence(
    self,
    pass_id: str,
    evidence: dict[str, Any],
    root_preflight: Mapping[str, Any],
) -> Path | None:
    return _scheduler_evidence_module.write_prelock_blocked_evidence(
        self._scheduler_evidence_write_context(),
        pass_id,
        evidence,
        root_preflight,
        write_evidence_callback=self._write_evidence,
    )


def _reserve_pre_execution_evidence(
    self,
    pass_id: str,
    started_at: datetime,
    candidate_count: int,
) -> dict[str, Any]:
    return _scheduler_evidence_module.reserve_pre_execution_evidence(
        self._scheduler_evidence_write_context(),
        pass_id,
        started_at,
        candidate_count,
        now=_now(self.config),
    )


def _scheduler_evidence_write_context(self) -> _scheduler_evidence_module.SchedulerEvidenceWriteContext:
    return _scheduler_evidence_module.SchedulerEvidenceWriteContext(
        config=self.config,
        require_safe_directory_final_component=_scheduler._require_safe_directory_final_component,
        require_under_workspace=_scheduler._require_under_workspace,
        evidence_safe=_scheduler._evidence_safe,
        max_evidence_bytes=_scheduler.MAX_EVIDENCE_BYTES,
        bounded_evidence_payload=_scheduler._bounded_evidence_payload,
        open_evidence_directory=_scheduler._open_evidence_directory,
        write_new_regular_file=lambda artifact_name, serialized, dir_fd, artifact_path: _write_new_regular_file(
            artifact_name,
            serialized,
            dir_fd=dir_fd,
            artifact_path=artifact_path,
        ),
        require_evidence_artifact_available=lambda artifact_name, dir_fd, artifact_path: (
            _require_evidence_artifact_available(
                artifact_name,
                dir_fd=dir_fd,
                artifact_path=artifact_path,
            )
        ),
        reservation_blocked_payload=lambda config, pass_id, artifact_path, reason, details, evidence_safe: (
            _evidence_reservation_blocked_payload(
                config=config,
                pass_id=pass_id,
                artifact_path=artifact_path,
                reason=reason,
                details=details,
            )
        ),
        evidence_write_error_payload=_scheduler._evidence_write_error_payload,
    )
