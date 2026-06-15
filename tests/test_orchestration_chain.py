from __future__ import annotations

import dataclasses
import json
import shlex
import sys
import types
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from packages.common.object_store import LocalObjectStore
from services.artifacts import ArtifactReader, ArtifactReaderConfig
from services.orchestrator.chain import (
    M3_STAGES,
    CycleOrchestrationContext,
    ForcingContext,
    ForecastOrchestrator,
    ModelContext,
    OrchestratorConfig,
    OrchestratorError,
    PsycopgOrchestratorRepository,
    build_model_run_assembly,
)
from services.orchestrator.persistence import Base, PipelineJob, PipelineStore
from services.orchestrator.retry import RetryConfig, RetryService
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES
from workers.canonical_converter.converter import (
    GFS_REQUIRED_STANDARD_VARIABLES,
    expected_converter_version,
)
from workers.data_adapters.base import cycle_id_for, format_cycle_time


class FakeCycleSlurmClient:
    def __init__(
        self,
        *,
        fail_stage: str | None = None,
        never_terminal_stage: str | None = None,
        array_results_by_stage: dict[str, list[str] | list[list[str]]] | None = None,
        failures_before_success_by_stage: dict[str, int] | None = None,
        error_code_by_stage: dict[str, str] | None = None,
        accounting_by_stage: dict[str, dict[str, str]] | None = None,
        malformed_array_accounting_stages: set[str] | None = None,
    ) -> None:
        self.fail_stage = fail_stage
        self.never_terminal_stage = never_terminal_stage
        self.array_results_by_stage = array_results_by_stage or {}
        self.failures_before_success_by_stage = failures_before_success_by_stage or {}
        self.error_code_by_stage = error_code_by_stage or {}
        self.accounting_by_stage = accounting_by_stage or {}
        self.malformed_array_accounting_stages = malformed_array_accounting_stages or set()
        self.submissions: list[dict[str, Any]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.poll_counts: dict[str, int] = {}
        self.fetch_log_calls: list[str] = []
        self.cancelled_jobs: list[str] = []
        self.next_job = 2000
        self.fail_next_array_submission_stage: str | None = None
        self.task_log_uri_by_stage: dict[str, list[str]] = {}
        self.task_error_message_by_stage: dict[str, list[str]] = {}

    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._submit(payload["manifest"]["stage"], payload["run_id"], payload["model_id"], payload["manifest"])

    def submit_job_array(
        self,
        job_type: str,
        *,
        cycle_id: str,
        stage_name: str,
        tasks: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        del job_type, cycle_id
        if self.fail_next_array_submission_stage == stage_name:
            raise RuntimeError(f"{stage_name} submission failed")
        payload = {"stage": stage_name, "tasks": tasks, "manifest": manifest}
        return self._submit(stage_name, manifest["run_id"], manifest["model_id"], payload)

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        job = self.jobs[job_id]
        self.poll_counts[job_id] += 1
        count = self.poll_counts[job_id]
        submitted_at = datetime.fromisoformat(job["submitted_at"].replace("Z", "+00:00"))
        if count == 1:
            job["status"] = "running"
            job["started_at"] = _fmt(submitted_at + timedelta(minutes=1))
        elif self.never_terminal_stage == job["stage"]:
            job["status"] = "running"
        else:
            remaining_failures = self.failures_before_success_by_stage.get(job["stage"], 0)
            failed = self.fail_stage == job["stage"] or job["stage_attempt"] < remaining_failures
            job["status"] = "failed" if failed else "succeeded"
            job["finished_at"] = _fmt(submitted_at + timedelta(minutes=2))
            job["exit_code"] = 1 if failed else 0
            job.update(self.accounting_by_stage.get(job["stage"], {}))
            if failed:
                job["error_code"] = self.error_code_by_stage.get(job["stage"], "FORCED_FAILURE")
                job["error_message"] = "forced failure"
        return dict(job)

    def get_array_task_results(self, job_id: str) -> list[dict[str, Any]]:
        job = self.jobs[job_id]
        if job["stage"] in self.malformed_array_accounting_stages:
            return [{"task_id": "not-an-int", "status": "succeeded", "exit_code": 0}]
        statuses = self.array_results_by_stage.get(job["stage"])
        task_count = len(job["payload"].get("tasks") or [])
        if statuses is None:
            statuses = ["succeeded"] * task_count
        elif statuses and isinstance(statuses[0], list):
            statuses = statuses[min(job["stage_attempt"], len(statuses) - 1)]
        return [
            {
                "task_id": index,
                "job_id": f"{job_id}_{index}",
                "status": status,
                "exit_code": 0 if status == "succeeded" else 1,
                "log_uri": self._task_field(
                    self.task_log_uri_by_stage,
                    job["stage"],
                    index,
                    f"s3://nhms/runs/{job['run_id']}/logs/{job_id}_{index}.out",
                ),
                "error_message": self._task_field(
                    self.task_error_message_by_stage,
                    job["stage"],
                    index,
                    None,
                ),
                "accounting": {
                    "elapsed": f"00:0{index + 1}:00",
                    "max_rss": f"{index + 1}024K",
                    "alloc_tres": "cpu=1,mem=2G",
                },
            }
            for index, status in enumerate(statuses)
        ]

    def fetch_logs(self, job_id: str) -> dict[str, Any]:
        self.fetch_log_calls.append(job_id)
        return {"job_id": job_id, "run_id": self.jobs[job_id]["run_id"], "complete": True, "logs": "ok"}

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        job = self.jobs[job_id]
        self.cancelled_jobs.append(job_id)
        job["status"] = "cancelled"
        job["exit_code"] = -1
        job["finished_at"] = _fmt(_dt("2026-05-01T00:30:00Z"))
        return dict(job)

    def _submit(self, stage: str, run_id: str, model_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        active = [job for job in self.jobs.values() if job["status"] not in {"succeeded", "failed", "cancelled"}]
        if active:
            raise AssertionError("orchestrator submitted a stage before the previous stage reached terminal status")
        self.next_job += 1
        job_id = str(self.next_job)
        stage_attempt = sum(1 for job in self.jobs.values() if job["stage"] == stage)
        submitted_at = _dt("2026-05-01T00:00:00Z") + timedelta(minutes=len(self.submissions) * 5)
        job = {
            "job_id": job_id,
            "run_id": run_id,
            "model_id": model_id,
            "stage": stage,
            "status": "submitted",
            "submitted_at": _fmt(submitted_at),
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "error_code": None,
            "error_message": None,
            "payload": payload,
            "stage_attempt": stage_attempt,
        }
        self.submissions.append(payload)
        self.jobs[job_id] = job
        self.poll_counts[job_id] = 0
        return dict(job)

    @staticmethod
    def _task_field(fields_by_stage: dict[str, list[str]], stage: str, index: int, default: str | None) -> str | None:
        fields = fields_by_stage.get(stage) or []
        return fields[index] if index < len(fields) else default


class ImmediateTerminalSlurmClient(FakeCycleSlurmClient):
    def get_job_status(self, job_id: str) -> dict[str, Any]:
        raise AssertionError(f"terminal job should not be polled: {job_id}")

    def _submit(self, stage: str, run_id: str, model_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.next_job += 1
        job_id = str(self.next_job)
        submitted_at = _dt("2026-05-01T00:00:00Z") + timedelta(minutes=len(self.submissions) * 5)
        job = {
            "job_id": job_id,
            "run_id": run_id,
            "model_id": model_id,
            "stage": stage,
            "status": "succeeded",
            "submitted_at": _fmt(submitted_at),
            "started_at": _fmt(submitted_at + timedelta(minutes=1)),
            "finished_at": _fmt(submitted_at + timedelta(minutes=2)),
            "exit_code": 0,
            "error_code": None,
            "error_message": None,
            "payload": payload,
            "stage_attempt": 0,
        }
        self.submissions.append(payload)
        self.jobs[job_id] = job
        self.poll_counts[job_id] = 0
        return dict(job)


class SubmitJobOnlyCycleSlurmClient:
    def __init__(self) -> None:
        self._delegate = FakeCycleSlurmClient()
        self.submissions = self._delegate.submissions
        self.submit_job_payloads: list[dict[str, Any]] = []

    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.submit_job_payloads.append(payload)
        return self._delegate.submit_job(payload)

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        return self._delegate.get_job_status(job_id)

    def fetch_logs(self, job_id: str) -> dict[str, Any]:
        return self._delegate.fetch_logs(job_id)


class PublishFailureSlurmClient(FakeCycleSlurmClient):
    def get_job_status(self, job_id: str) -> dict[str, Any]:
        job = self.jobs[job_id]
        if job["stage"] == "publish":
            self.poll_counts[job_id] += 1
            submitted_at = datetime.fromisoformat(job["submitted_at"].replace("Z", "+00:00"))
            job["status"] = "failed"
            job["finished_at"] = _fmt(submitted_at + timedelta(minutes=1))
            job["exit_code"] = 1
            job["error_code"] = "NO_PUBLISHABLE_PRODUCTS"
            job["error_message"] = "No publishable flood return-period products found for cycle_id=gfs_2026050100."
            return dict(job)
        return super().get_job_status(job_id)


def _successful_control_node_publisher(calls: list[dict[str, Any]] | None = None) -> type:
    class _PublishedResult:
        def to_dict(self) -> dict[str, Any]:
            return {
                "cycle_id": "gfs_2026050100",
                "status": "published",
                "layers": [{"layer_id": "q-down:gfs_2026050100"}],
                "artifacts": [],
                "lineage": {"cycle_id": "gfs_2026050100"},
            }

    class _ControlNodePublisher:
        def __init__(self, **kwargs: Any) -> None:
            if calls is not None:
                calls.append({"init": dict(kwargs)})

        def publish_cycle(self, cycle_id: str) -> _PublishedResult:
            if calls is not None:
                calls.append({"cycle_id": cycle_id})
            return _PublishedResult()

    return _ControlNodePublisher


class FakeCycleRepository:
    def __init__(self, *, active: bool = False) -> None:
        self.active = active
        self.jobs: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.cycle_statuses: list[str] = []
        self.hydro_runs: dict[str, dict[str, Any]] = {}

    def has_active_orchestration(self, *, source_id: str, cycle_time: datetime) -> bool:
        del source_id, cycle_time
        return self.active

    def ensure_forecast_cycle(self, *, source_id: str, cycle_time: datetime) -> dict[str, Any]:
        return {"cycle_id": f"{source_id}_{cycle_time:%Y%m%d%H}", "status": "discovered"}

    def update_forecast_cycle_status(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        del source_id, cycle_time
        self.cycle_statuses.append(status)
        return {"status": status, "error_code": error_code, "error_message": error_message}

    def upsert_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any]:
        existing = self.jobs.get(record["job_id"])
        merged = dict(existing) if existing else {}
        merged.update(record)
        self.jobs[record["job_id"]] = merged
        return dict(merged)

    def reserve_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any] | None:
        key = record["idempotency_key"]
        for job in self.jobs.values():
            if job.get("idempotency_key") == key:
                return None
        if record["job_id"] in self.jobs:
            return None
        stored = dict(record)
        stored.setdefault("slurm_job_id", None)
        self.jobs[record["job_id"]] = stored
        return dict(stored)

    def reclaim_pipeline_job_reservation(self, record: dict[str, Any]) -> dict[str, Any] | None:
        key = record["idempotency_key"]
        for job in self.jobs.values():
            if (
                job.get("idempotency_key") == key
                and job.get("slurm_job_id") is None
                and job.get("status") in {"submission_failed", "reservation_lost"}
            ):
                job.update(
                    {
                        "status": "reserved",
                        "slurm_job_id": None,
                        "array_task_id": None,
                        "submitted_at": None,
                        "started_at": None,
                        "finished_at": None,
                        "exit_code": None,
                        "error_code": None,
                        "error_message": None,
                        "run_id": job.get("run_id") or record.get("run_id"),
                        "cycle_id": job.get("cycle_id") or record.get("cycle_id"),
                        "model_id": job.get("model_id") or record.get("model_id"),
                        "stage": job.get("stage") or record.get("stage"),
                        "candidate_id": job.get("candidate_id") or record.get("candidate_id"),
                    }
                )
                return dict(job)
        pending = self.jobs.get(record["job_id"])
        if (
            pending is not None
            and pending.get("idempotency_key") in (None, "")
            and pending.get("slurm_job_id") is None
            and pending.get("status") == "pending"
            and not any(job.get("idempotency_key") == key for job in self.jobs.values())
        ):
            pending["status"] = "reserved"
            pending["idempotency_key"] = key
            pending["candidate_id"] = pending.get("candidate_id") or record.get("candidate_id")
            pending["submitted_at"] = None
            pending["started_at"] = None
            pending["finished_at"] = None
            pending["exit_code"] = None
            pending["error_code"] = None
            pending["error_message"] = None
            return dict(pending)
        return None

    def bind_pipeline_job_reservation(
        self,
        idempotency_key: str,
        *,
        slurm_job_id: str,
        status: str = "submitted",
        array_task_id: int | None = None,
    ) -> dict[str, Any] | None:
        for job in self.jobs.values():
            if job.get("idempotency_key") == idempotency_key and job.get("slurm_job_id") is None:
                job["slurm_job_id"] = slurm_job_id
                job["status"] = status
                if array_task_id is not None:
                    job["array_task_id"] = array_task_id
                return dict(job)
        return None

    def query_candidate_state(self, idempotency_key: str) -> dict[str, Any] | None:
        for job in self.jobs.values():
            if job.get("idempotency_key") == idempotency_key:
                return dict(job)
        return None

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
        previous = self.jobs[job_id]["status"]
        self.jobs[job_id].update(
            {
                "status": status,
                "started_at": started_at or self.jobs[job_id].get("started_at"),
                "finished_at": finished_at or self.jobs[job_id].get("finished_at"),
                "exit_code": exit_code,
                "error_code": error_code,
                "error_message": error_message,
                "log_uri": log_uri or self.jobs[job_id].get("log_uri"),
            }
        )
        return previous, dict(self.jobs[job_id])

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
        event = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "status_from": status_from,
            "status_to": status_to,
            "message": message,
            "details": details or {},
        }
        self.events.append(event)
        return event

    def get_pipeline_job(self, job_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        return dict(job) if job is not None else None

    def query_pipeline_jobs_by_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
        return sorted(
            [dict(job) for job in self.jobs.values() if job["cycle_id"] == cycle_id],
            key=lambda job: str(job["submitted_at"]),
        )

    def query_pipeline_jobs_by_run(self, run_id: str) -> list[dict[str, Any]]:
        return sorted(
            [dict(job) for job in self.jobs.values() if job["run_id"] == run_id],
            key=lambda job: str(job["submitted_at"]),
        )

    def create_hydro_run_from_basin(self, basin: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
        run_id = str(manifest["run_id"])
        existing = self.hydro_runs.get(run_id)
        if existing is not None and existing["status"] not in {"failed", "cancelled"}:
            return dict(existing)
        record = {
            "run_id": run_id,
            "status": "created",
            "model_id": manifest["model"]["model_id"],
            "basin_version_id": manifest["model"]["basin_version_id"],
            "source_id": manifest["source_id"],
            "cycle_time": manifest["cycle_time"],
            "run_manifest": dict(manifest),
            "basin": dict(basin),
        }
        self.hydro_runs[run_id] = record
        return dict(record)

    def update_hydro_run_status(
        self,
        run_id: str,
        status: str,
        *,
        slurm_job_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        record = self.hydro_runs[run_id]
        record["status"] = status
        if slurm_job_id is not None:
            record["slurm_job_id"] = slurm_job_id
        if error_code is not None:
            record["error_code"] = error_code
        if error_message is not None:
            record["error_message"] = error_message
        return dict(record)


class StoreBackedCycleRepository(FakeCycleRepository):
    def __init__(self, store: PipelineStore, *, active: bool = False) -> None:
        super().__init__(active=active)
        self.store = store

    def upsert_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any]:
        existing = self.store.get_job(record["job_id"])
        if existing is None:
            job = self.store.create_job(
                job_id=record["job_id"],
                run_id=record.get("run_id"),
                cycle_id=record.get("cycle_id"),
                job_type=record["job_type"],
                slurm_job_id=record.get("slurm_job_id"),
                model_id=record.get("model_id"),
                stage=record.get("stage"),
                status=record.get("status", "pending"),
                commit=False,
            )
        else:
            job = existing
            job.run_id = record.get("run_id")
            job.cycle_id = record.get("cycle_id")
            job.job_type = record["job_type"]
            job.slurm_job_id = record.get("slurm_job_id")
            job.model_id = record.get("model_id")
            job.stage = record.get("stage")
            job.status = record.get("status", job.status)
        job.submitted_at = record.get("submitted_at")
        job.started_at = record.get("started_at")
        job.finished_at = record.get("finished_at")
        job.exit_code = record.get("exit_code")
        job.retry_count = int(record.get("retry_count") or getattr(job, "retry_count", 0) or 0)
        job.error_code = record.get("error_code")
        job.error_message = record.get("error_message")
        job.log_uri = record.get("log_uri")
        self.store.session.add(job)
        self.store.session.commit()
        self.store.session.refresh(job)
        self.jobs[record["job_id"]] = self._job_to_dict(job)
        return dict(self.jobs[record["job_id"]])

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
        job = self.store.get_job(job_id)
        if job is None:
            previous, record = super().update_pipeline_job_status(
                job_id,
                status,
                started_at=started_at,
                finished_at=finished_at,
                exit_code=exit_code,
                error_code=error_code,
                error_message=error_message,
                log_uri=log_uri,
            )
            return previous, record
        previous = job.status
        job.status = status
        if started_at is not None:
            job.started_at = started_at
        if finished_at is not None:
            job.finished_at = finished_at
        if exit_code is not None:
            job.exit_code = exit_code
        if error_code is not None:
            job.error_code = error_code
        if error_message is not None:
            job.error_message = error_message
        if log_uri is not None:
            job.log_uri = log_uri
        self.store.session.add(job)
        self.store.session.commit()
        self.store.session.refresh(job)
        record = self._job_to_dict(job)
        self.jobs[job_id] = record
        return previous, dict(record)

    def reserve_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any] | None:
        job = self.store.reserve_job(
            job_id=record["job_id"],
            run_id=record.get("run_id"),
            cycle_id=record.get("cycle_id"),
            job_type=record["job_type"],
            model_id=record.get("model_id"),
            stage=record.get("stage"),
            status=record.get("status", "reserved"),
            idempotency_key=record["idempotency_key"],
            candidate_id=record.get("candidate_id"),
        )
        if job is None:
            return None
        result = self._job_to_dict(job)
        self.jobs[record["job_id"]] = result
        return dict(result)

    def bind_pipeline_job_reservation(
        self,
        idempotency_key: str,
        *,
        slurm_job_id: str,
        status: str = "submitted",
        array_task_id: int | None = None,
    ) -> dict[str, Any] | None:
        job = self.store.bind_reservation(
            idempotency_key,
            slurm_job_id=slurm_job_id,
            status=status,
            array_task_id=array_task_id,
        )
        if job is None:
            return None
        result = self._job_to_dict(job)
        self.jobs[result["job_id"]] = result
        return dict(result)

    def reclaim_pipeline_job_reservation(self, record: dict[str, Any]) -> dict[str, Any] | None:
        # Mirror production: atomically take over a DEAD reservation
        # (slurm_job_id IS NULL AND status IN submission_failed/reservation_lost)
        # back to 'reserved'; a live row never matches.
        job = self.store.reclaim_reservation(
            record["idempotency_key"],
            job_id=record.get("job_id"),
            run_id=record.get("run_id"),
            cycle_id=record.get("cycle_id"),
            model_id=record.get("model_id"),
            stage=record.get("stage"),
            candidate_id=record.get("candidate_id"),
        )
        if job is None:
            return None
        result = self._job_to_dict(job)
        self.jobs[result["job_id"]] = result
        return dict(result)

    def query_candidate_state(self, idempotency_key: str) -> dict[str, Any] | None:
        job = self.store.query_candidate_state(idempotency_key)
        return self._job_to_dict(job) if job is not None else None

    def get_pipeline_job(self, job_id: str) -> dict[str, Any] | None:
        job = self.store.get_job(job_id)
        return self._job_to_dict(job) if job is not None else None

    def query_pipeline_jobs_by_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
        jobs = [
            self._job_to_dict(job)
            for job in self.store.query_jobs_by_cycle(cycle_id)
        ]
        return jobs or super().query_pipeline_jobs_by_cycle(cycle_id)

    def query_pipeline_jobs_by_run(self, run_id: str) -> list[dict[str, Any]]:
        return [
            self._job_to_dict(job)
            for job in self.store.query_jobs_by_run(run_id)
        ] or super().query_pipeline_jobs_by_run(run_id)

    @staticmethod
    def _job_to_dict(job: PipelineJob) -> dict[str, Any]:
        return {
            "job_id": job.job_id,
            "run_id": job.run_id,
            "cycle_id": job.cycle_id,
            "job_type": job.job_type,
            "slurm_job_id": job.slurm_job_id,
            "array_task_id": job.array_task_id,
            "model_id": job.model_id,
            "status": job.status,
            "stage": job.stage,
            "idempotency_key": job.idempotency_key,
            "candidate_id": job.candidate_id,
            "submitted_at": job.submitted_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "exit_code": job.exit_code,
            "retry_count": job.retry_count,
            "error_code": job.error_code,
            "error_message": job.error_message,
            "log_uri": job.log_uri,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        }


class WriteFailingObjectStore(LocalObjectStore):
    def write_bytes_atomic(self, key_or_uri: str, content: bytes) -> str:
        if key_or_uri.endswith("/input/manifest.json"):
            raise OSError("permission denied")
        return super().write_bytes_atomic(key_or_uri, content)


class LogWriteFailingObjectStore(LocalObjectStore):
    def write_bytes_atomic(self, key_or_uri: str, content: bytes) -> str:
        if "/logs/" in key_or_uri:
            raise OSError("log write failed")
        return super().write_bytes_atomic(key_or_uri, content)


class FirstLogWriteFailingObjectStore(LocalObjectStore):
    def __init__(self, root: Path | str, object_store_prefix: str = "") -> None:
        super().__init__(root, object_store_prefix)
        self.log_write_calls = 0

    def write_bytes_atomic(self, key_or_uri: str, content: bytes) -> str:
        if "/logs/" in key_or_uri:
            self.log_write_calls += 1
            if self.log_write_calls == 1:
                raise OSError("first log write failed")
        return super().write_bytes_atomic(key_or_uri, content)


class InvalidJsonForecastOrchestrator(ForecastOrchestrator):
    def _validate_forecast_runtime_manifest(
        self,
        manifest_path: Path,
        manifest: dict[str, Any],
        *,
        task_index: int,
    ) -> None:
        manifest_path.write_text("{bad json", encoding="utf-8")
        super()._validate_forecast_runtime_manifest(manifest_path, manifest, task_index=task_index)


class MissingManifestForecastOrchestrator(ForecastOrchestrator):
    def _validate_forecast_runtime_manifest(
        self,
        manifest_path: Path,
        manifest: dict[str, Any],
        *,
        task_index: int,
    ) -> None:
        manifest_path.unlink(missing_ok=True)
        super()._validate_forecast_runtime_manifest(manifest_path, manifest, task_index=task_index)


class UnreadableManifestForecastOrchestrator(ForecastOrchestrator):
    def _validate_forecast_runtime_manifest(
        self,
        manifest_path: Path,
        manifest: dict[str, Any],
        *,
        task_index: int,
    ) -> None:
        raise OrchestratorError(
            "RUNTIME_MANIFEST_READ_FAILED",
            f"Forecast runtime manifest cannot be read for task {task_index}: permission denied",
            {"manifest_path": str(manifest_path), "task_id": task_index},
        )


class FakeRetryService:
    def __init__(self, *, max_retries: int = 1) -> None:
        self.config = RetryConfig(max_retries=max_retries, backoff_schedule=[0])
        self.retry_counts: dict[str, int] = {}
        self.handled_job_ids: list[str] = []

    def handle_failed_job(self, job: Any) -> Any:
        self.handled_job_ids.append(job.job_id)
        retry_count = self.retry_counts.get(job.job_id, int(getattr(job, "retry_count", 0) or 0))
        if retry_count >= self.config.max_retries:
            job.status = "permanently_failed"
            return job
        retry_count += 1
        self.retry_counts[job.job_id] = retry_count
        job.job_id = f"{job.job_id}_retry_{retry_count}"
        job.retry_count = retry_count
        job.status = "pending"
        return job


def _dataclass_field_defaults(cls: type) -> list[tuple[str, Any]]:
    snapshot: list[tuple[str, Any]] = []
    for dataclass_field in dataclasses.fields(cls):
        if dataclass_field.default is not dataclasses.MISSING:
            default: Any = dataclass_field.default
        elif dataclass_field.default_factory is not dataclasses.MISSING:
            default = ("factory", dataclass_field.default_factory.__name__)
        else:
            default = "required"
        snapshot.append((dataclass_field.name, default))
    return snapshot


def _stage_catalog_snapshot(stages: Sequence[Any]) -> list[tuple[str, str, str, str, str, bool]]:
    return [
        (
            stage.stage,
            stage.job_type,
            stage.template_name,
            stage.success_cycle_status,
            stage.failure_cycle_status,
            stage.is_array,
        )
        for stage in stages
    ]


def test_chain_type_exports_preserve_legacy_identity_and_dataclass_contracts() -> None:
    import services.orchestrator as orchestrator_package
    import services.orchestrator.chain as legacy_chain
    from services.orchestrator import chain_types

    type_names = [
        "StageDefinition",
        "ModelContext",
        "ForcingContext",
        "InitialStateSelection",
        "ForecastRunContext",
        "AnalysisRunContext",
        "StageRunResult",
        "PipelineResult",
        "ArrayTaskResult",
        "ArrayAggregation",
        "DisplayLogPublication",
        "DisplayLogPublicationAttempt",
        "TerminalJobObservation",
        "CycleOrchestrationContext",
        "ModelRunAssembly",
    ]

    for name in type_names:
        assert getattr(legacy_chain, name) is getattr(chain_types, name)
    assert orchestrator_package.PipelineResult is chain_types.PipelineResult
    assert orchestrator_package.StageRunResult is chain_types.StageRunResult

    assert {
        name: (
            dataclasses.is_dataclass(getattr(chain_types, name)),
            getattr(chain_types, name).__dataclass_params__.frozen,
            _dataclass_field_defaults(getattr(chain_types, name)),
        )
        for name in type_names
    } == {
        "StageDefinition": (
            True,
            True,
            [
                ("stage", "required"),
                ("job_type", "required"),
                ("template_name", "required"),
                ("success_cycle_status", "required"),
                ("failure_cycle_status", "required"),
                ("is_array", False),
            ],
        ),
        "ModelContext": (
            True,
            True,
            [
                ("model_id", "required"),
                ("basin_id", "required"),
                ("basin_version_id", "required"),
                ("river_network_version_id", "required"),
                ("segment_count", "required"),
                ("model_package_uri", "required"),
                ("output_segment_count", None),
            ],
        ),
        "ForcingContext": (
            True,
            True,
            [
                ("forcing_version_id", "required"),
                ("forcing_package_uri", "required"),
                ("start_time", None),
                ("end_time", None),
                ("source_id", None),
                ("max_lead_hours", None),
            ],
        ),
        "InitialStateSelection": (
            True,
            True,
            [
                ("state_id", "required"),
                ("state_uri", "required"),
                ("valid_time", "required"),
                ("checksum", "required"),
                ("quality", "required"),
                ("source_id", None),
                ("cycle_id", None),
                ("lead_hours", None),
                ("model_package_version", None),
                ("model_package_checksum", None),
                ("rejection_code", None),
            ],
        ),
        "ForecastRunContext": (
            True,
            True,
            [
                ("run_id", "required"),
                ("source_id", "required"),
                ("scenario_id", "required"),
                ("cycle_id", "required"),
                ("cycle_time", "required"),
                ("model_id", "required"),
                ("basin_id", "required"),
                ("basin_version_id", "required"),
                ("river_network_version_id", "required"),
                ("segment_count", "required"),
                ("model_package_uri", "required"),
                ("forcing_version_id", "required"),
                ("forcing_package_uri", "required"),
                ("start_time", "required"),
                ("end_time", "required"),
                ("forecast_horizon_hours", "required"),
                ("run_manifest_uri", "required"),
                ("output_uri", "required"),
                ("log_uri", "required"),
                ("init_state_id", None),
                ("init_state_uri", None),
                ("init_state_valid_time", None),
                ("init_state_checksum", None),
                ("init_state_quality", "cold_start_no_state"),
                ("output_segment_count", None),
            ],
        ),
        "AnalysisRunContext": (
            True,
            True,
            [
                ("run_id", "required"),
                ("source_id", "required"),
                ("cycle_id", "required"),
                ("cycle_time", "required"),
                ("model_id", "required"),
                ("basin_id", "required"),
                ("basin_version_id", "required"),
                ("river_network_version_id", "required"),
                ("segment_count", "required"),
                ("model_package_uri", "required"),
                ("forcing_version_id", "required"),
                ("forcing_package_uri", "required"),
                ("start_time", "required"),
                ("end_time", "required"),
                ("run_manifest_uri", "required"),
                ("output_uri", "required"),
                ("log_uri", "required"),
                ("init_state_id", None),
                ("init_state_uri", None),
                ("init_state_valid_time", None),
                ("output_segment_count", None),
                ("update_ic_step_minutes", None),
                ("forcing_causality", None),
            ],
        ),
        "StageRunResult": (
            True,
            True,
            [
                ("stage", "required"),
                ("job_type", "required"),
                ("pipeline_job_id", "required"),
                ("slurm_job_id", "required"),
                ("status", "required"),
                ("exit_code", None),
                ("error_code", None),
                ("error_message", None),
                ("log_uri", None),
                ("accounting", ("factory", "dict")),
                ("task_results", ()),
                ("finished_at", None),
            ],
        ),
        "PipelineResult": (
            True,
            True,
            [
                ("run_id", "required"),
                ("cycle_id", "required"),
                ("status", "required"),
                ("stages", "required"),
                ("candidate_outcomes", ()),
            ],
        ),
        "ArrayTaskResult": (
            True,
            True,
            [
                ("task_id", "required"),
                ("slurm_job_id", "required"),
                ("status", "required"),
                ("exit_code", None),
                ("error_code", None),
                ("error_message", None),
                ("log_uri", None),
                ("accounting", ("factory", "dict")),
            ],
        ),
        "ArrayAggregation": (
            True,
            True,
            [
                ("total", "required"),
                ("succeeded", "required"),
                ("failed", "required"),
                ("cancelled", "required"),
                ("task_results", "required"),
            ],
        ),
        "DisplayLogPublication": (
            True,
            True,
            [
                ("candidate_uri", "required"),
                ("advertised_uri", "required"),
                ("should_persist_logs", "required"),
            ],
        ),
        "DisplayLogPublicationAttempt": (
            True,
            True,
            [
                ("advertised_uri", "required"),
                ("error", None),
            ],
        ),
        "TerminalJobObservation": (
            True,
            True,
            [
                ("job", "required"),
                ("publication_attempt", None),
            ],
        ),
        "CycleOrchestrationContext": (
            True,
            False,
            [
                ("source_id", "required"),
                ("cycle_time", "required"),
                ("cycle_id", "required"),
                ("run_id", "required"),
                ("all_basins", "required"),
                ("active_basins", "required"),
                ("restart_stage", None),
                ("had_partial", False),
                ("last_partial_status", None),
                ("task_outcomes", ("factory", "dict")),
                ("retry_attempt", None),
            ],
        ),
        "ModelRunAssembly": (
            True,
            True,
            [
                ("identity", "required"),
                ("forcing", "required"),
                ("runtime", "required"),
                ("outputs", "required"),
                ("frequency", "required"),
                ("display", "required"),
                ("quality_states", "required"),
                ("residual_blockers", "required"),
            ],
        ),
    }

    aggregation = chain_types.ArrayAggregation(
        total=3,
        succeeded=1,
        failed=1,
        cancelled=1,
        task_results=(
            chain_types.ArrayTaskResult(0, "3000_0", "succeeded"),
            chain_types.ArrayTaskResult(1, "3000_1", "failed"),
            chain_types.ArrayTaskResult(2, "3000_2", "cancelled"),
        ),
    )
    assert aggregation.status == "partially_failed"
    assert aggregation.succeeded_task_ids == (0,)
    assert aggregation.failed_task_ids == (1,)
    assert aggregation.cancelled_task_ids == (2,)
    assert chain_types.ArrayAggregation(0, 0, 0, 0, ()).status == "failed"
    assert chain_types.ArrayAggregation(2, 2, 0, 0, ()).status == "succeeded"

    publication = chain_types.DisplayLogPublication("s3://candidate/log.txt", None, should_persist_logs=True)
    assert publication.requires_publish_before_advertise is True

    identity = {"model_id": "model-a"}
    assembly = chain_types.ModelRunAssembly(
        identity=identity,
        forcing={"forcing_version_id": "forcing-v1"},
        runtime={"dt": 60},
        outputs={"output_uri": "s3://outputs/model-a"},
        frequency={"enabled": True},
        display={"layer_id": "q-down"},
        quality_states={"state": "ready"},
        residual_blockers=({"code": "none"},),
    )
    entry = assembly.to_manifest_entry()
    assert set(entry) == {
        "identity",
        "forcing_metadata",
        "shud_runtime",
        "outputs",
        "frequency_contract",
        "display_contract",
        "quality_states",
        "residual_blockers",
    }
    assert list(entry) == [
        "identity",
        "forcing_metadata",
        "shud_runtime",
        "outputs",
        "frequency_contract",
        "display_contract",
        "quality_states",
        "residual_blockers",
    ]
    entry["identity"]["model_id"] = "mutated"
    entry["residual_blockers"][0]["code"] = "mutated"
    assert assembly.identity == {"model_id": "model-a"}
    assert assembly.residual_blockers == ({"code": "none"},)


def test_chain_stage_catalog_preserves_static_snapshots_and_legacy_identity() -> None:
    import services.orchestrator.chain as legacy_chain
    from services.orchestrator import chain_stages

    assert legacy_chain.LEGACY_FORECAST_STAGES is chain_stages.LEGACY_FORECAST_STAGES
    assert legacy_chain.M3_STAGES is chain_stages.M3_STAGES
    assert legacy_chain.STAGES is chain_stages.STAGES
    assert legacy_chain.ANALYSIS_STAGES is chain_stages.ANALYSIS_STAGES
    assert chain_stages.STAGES is chain_stages.M3_STAGES

    m3_stage_snapshot = [
        (
            "download",
            "download_source_cycle",
            "download_source_cycle.sbatch",
            "raw_complete",
            "failed_download",
            False,
        ),
        ("convert", "convert_canonical", "convert_canonical.sbatch", "canonical_ready", "failed_convert", False),
        (
            "forcing",
            "produce_forcing_array",
            "produce_forcing_array.sbatch",
            "forcing_ready",
            "failed_forcing",
            True,
        ),
        (
            "forecast",
            "run_shud_forecast_array",
            "run_shud_forecast_array.sbatch",
            "forecast_running",
            "failed_run",
            True,
        ),
        ("parse", "parse_output_array", "parse_output_array.sbatch", "complete", "failed_parse", True),
        (
            "state_save_qc",
            "save_state_snapshot_array",
            "save_state_snapshot_array.sbatch",
            "complete",
            "failed_publish",
            True,
        ),
        ("frequency", "compute_frequency_array", "compute_frequency_array.sbatch", "complete", "failed_parse", True),
        ("publish", "publish_tiles", "publish_tiles.sbatch", "complete", "failed_publish", False),
    ]

    assert {
        "LEGACY_FORECAST_STAGES": _stage_catalog_snapshot(chain_stages.LEGACY_FORECAST_STAGES),
        "M3_STAGES": _stage_catalog_snapshot(chain_stages.M3_STAGES),
        "STAGES": _stage_catalog_snapshot(chain_stages.STAGES),
        "ANALYSIS_STAGES": _stage_catalog_snapshot(chain_stages.ANALYSIS_STAGES),
    } == {
        "LEGACY_FORECAST_STAGES": [
            ("download_gfs", "download", "download_source_cycle.sbatch", "raw_complete", "failed_download", False),
            ("convert_canonical", "canonical", "convert_canonical.sbatch", "canonical_ready", "failed_convert", False),
            ("produce_forcing", "forcing", "produce_forcing.sbatch", "forcing_ready", "failed_forcing", False),
            ("run_shud_forecast", "forecast", "run_shud_forecast.sbatch", "forecast_running", "failed_run", False),
            ("parse_output", "parse", "parse_output.sbatch", "complete", "failed_parse", False),
        ],
        "M3_STAGES": m3_stage_snapshot,
        "STAGES": m3_stage_snapshot,
        "ANALYSIS_STAGES": [
            (
                "era5_download",
                "analysis_download_source_cycle",
                "analysis_download_source_cycle.sbatch",
                "raw_complete",
                "failed_download",
                False,
            ),
            (
                "canonical_convert",
                "analysis_convert_canonical",
                "analysis_convert_canonical.sbatch",
                "canonical_ready",
                "failed_convert",
                False,
            ),
            (
                "forcing_produce",
                "analysis_produce_forcing",
                "analysis_produce_forcing.sbatch",
                "forcing_ready",
                "failed_forcing",
                False,
            ),
            ("analysis_run", "run_shud_analysis", "run_shud_analysis.sbatch", "forecast_running", "failed_run", False),
            (
                "parse_output",
                "parse_analysis_output",
                "parse_analysis_output.sbatch",
                "complete",
                "failed_parse",
                False,
            ),
            ("state_save_qc", "save_state_snapshot", "save_state_snapshot.sbatch", "complete", "failed_publish", False),
        ],
    }


def test_m3_cycle_orchestration_submits_all_stages_lazily(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(3))

    assert result.status == "complete"
    assert [submission["stage"] for submission in client.submissions] == [stage.stage for stage in M3_STAGES]
    assert [stage.status for stage in result.stages] == ["succeeded"] * len(M3_STAGES)
    assert repository.cycle_statuses[-1] == "complete"
    assert {job["status"] for job in repository.jobs.values()} == {"succeeded"}


def test_m3_forecast_saves_state_before_frequency() -> None:
    stages = [stage.stage for stage in M3_STAGES]

    assert stages == [
        "download",
        "convert",
        "forcing",
        "forecast",
        "parse",
        "state_save_qc",
        "frequency",
        "publish",
    ]


def test_non_array_stage_submissions_carry_slurm_template_and_env_contract(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(
        tmp_path,
        repository,
        client,
        slurm_job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
        slurm_env={"NHMS_PROFILE": "prod/gfs_00", "NHMS_RUN_LABEL": "prod_gfs_00"},
    )

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    assert result.status == "complete"
    non_array_submissions = {
        submission["stage"]: submission
        for submission in client.submissions
        if submission["stage"] in {"download", "convert", "publish"}
    }
    assert set(non_array_submissions) == {"download", "convert", "publish"}
    for submission in non_array_submissions.values():
        assert submission["slurm_job_type_templates"] == dict(DEFAULT_JOB_TYPE_TEMPLATES)
        assert submission["slurm_env"] == {
            "NHMS_PROFILE": "prod/gfs_00",
            "NHMS_RUN_LABEL": "prod_gfs_00",
        }


def test_cycle_stage_submission_events_record_runtime_root_contract(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    assert result.status == "complete"
    submission_events = [event for event in repository.events if event["event_type"] == "submission"]
    download_event = next(event for event in submission_events if event["details"]["stage"] == "download")
    forecast_event = next(event for event in submission_events if event["details"]["stage"] == "forecast")
    expected_contract = {
        "workspace_dir": str(tmp_path / "workspace"),
        "object_store_root": str(tmp_path / "object-store"),
        "object_store_prefix": "s3://nhms",
        "published_artifact_uri_prefix": "published://",
    }
    assert download_event["details"]["runtime_root_contract"] == expected_contract
    assert forecast_event["details"]["runtime_root_contract"] == expected_contract


def test_cycle_download_success_without_raw_manifest_is_resubmitted(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    repository.jobs["job_cycle_ifs_2026050100_download"] = {
        "job_id": "job_cycle_ifs_2026050100_download",
        "run_id": "cycle_ifs_2026050100",
        "cycle_id": "ifs_2026050100",
        "job_type": "download_source_cycle",
        "slurm_job_id": "6084",
        "model_id": None,
        "status": "succeeded",
        "stage": "download",
        "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:01:00Z")),
        "finished_at": _fmt(_dt("2026-05-01T00:02:00Z")),
        "exit_code": 0,
        "error_code": None,
        "error_message": None,
        "log_uri": "s3://nhms/runs/cycle_ifs_2026050100/logs/download.log",
    }
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("IFS", "2026050100", _basins(1))

    assert result.status == "complete"
    assert client.submissions[0]["stage"] == "download"
    assert client.submissions[0]["source_id"] == "IFS"
    assert result.stages[0].pipeline_job_id == "job_cycle_ifs_2026050100_download_retry_1"
    assert repository.jobs["job_cycle_ifs_2026050100_download"]["slurm_job_id"] == "6084"
    assert repository.jobs["job_cycle_ifs_2026050100_download_retry_1"]["slurm_job_id"] == "2001"
    assert repository.jobs["job_cycle_ifs_2026050100_download_retry_1"]["status"] == "succeeded"


def test_download_repair_retries_stale_failed_downstream_stage(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    repository.jobs["job_cycle_ifs_2026050100_download_retry_1"] = {
        "job_id": "job_cycle_ifs_2026050100_download_retry_1",
        "run_id": "cycle_ifs_2026050100",
        "cycle_id": "ifs_2026050100",
        "job_type": "download_source_cycle",
        "slurm_job_id": "6097",
        "model_id": None,
        "status": "succeeded",
        "stage": "download",
        "submitted_at": _fmt(_dt("2026-05-01T00:10:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:11:00Z")),
        "finished_at": _fmt(_dt("2026-05-01T00:20:00Z")),
        "exit_code": 0,
        "error_code": None,
        "error_message": None,
        "log_uri": "s3://nhms/runs/cycle_ifs_2026050100/logs/download_retry_1.log",
    }
    repository.jobs["job_cycle_ifs_2026050100_convert"] = {
        "job_id": "job_cycle_ifs_2026050100_convert",
        "run_id": "cycle_ifs_2026050100",
        "cycle_id": "ifs_2026050100",
        "job_type": "convert_canonical",
        "slurm_job_id": "6085",
        "model_id": None,
        "status": "failed",
        "stage": "convert",
        "submitted_at": _fmt(_dt("2026-05-01T00:03:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:04:00Z")),
        "finished_at": _fmt(_dt("2026-05-01T00:05:00Z")),
        "exit_code": 1,
        "error_code": "SLURM_JOB_FAILED",
        "error_message": "old convert failed against missing raw manifest",
        "log_uri": "s3://nhms/runs/cycle_ifs_2026050100/logs/convert.log",
    }
    repository.jobs["job_cycle_ifs_2026050100_download"] = {
        "job_id": "job_cycle_ifs_2026050100_download",
        "run_id": "cycle_ifs_2026050100",
        "cycle_id": "ifs_2026050100",
        "job_type": "download_source_cycle",
        "slurm_job_id": "6084",
        "model_id": None,
        "status": "succeeded",
        "stage": "download",
        "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:01:00Z")),
        "finished_at": _fmt(_dt("2026-05-01T00:02:00Z")),
        "exit_code": 0,
        "error_code": None,
        "error_message": None,
        "log_uri": "s3://nhms/runs/cycle_ifs_2026050100/logs/download.log",
    }
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    orchestrator.object_store.write_bytes_atomic(
        "raw/IFS/2026050100/manifest.json",
        b'{"source_id":"IFS"}',
    )

    result = orchestrator.orchestrate_cycle("IFS", "2026050100", _basins(1))

    assert result.status == "complete"
    assert [submission["stage"] for submission in client.submissions[:2]] == ["convert", "forcing"]
    assert result.stages[0].pipeline_job_id == "job_cycle_ifs_2026050100_download_retry_1"
    assert result.stages[1].pipeline_job_id == "job_cycle_ifs_2026050100_convert_retry_1"
    assert repository.jobs["job_cycle_ifs_2026050100_convert"]["status"] == "failed"
    assert repository.jobs["job_cycle_ifs_2026050100_convert_retry_1"]["status"] == "succeeded"


def test_array_pipeline_jobs_are_persisted_as_cycle_level_rows(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "complete"
    for stage in ("forcing", "forecast", "parse", "state_save_qc", "frequency"):
        job = repository.jobs[f"job_cycle_gfs_2026050100_{stage}"]
        assert job["run_id"] == "cycle_gfs_2026050100"
        assert job["model_id"] is None


def test_single_candidate_downstream_restart_jobs_are_candidate_scoped(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "output_uri": "s3://nhms/runs/fcst_gfs_2026050100_model_0/output/",
        "orchestration_run_id": "cycle_gfs_2026050100_parse",
        "restart_stage": "parse",
        "durable_shud_output_reused": True,
        "native_shud_resubmitted": False,
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    assert result.status == "complete"
    for stage in ("parse", "state_save_qc", "frequency", "publish"):
        job = repository.jobs[f"job_cycle_gfs_2026050100_parse_{stage}"]
        assert job["run_id"] == "cycle_gfs_2026050100_parse"
        assert job["model_id"] == "model_0"


def test_candidate_scoped_parse_restarts_do_not_reuse_sibling_stage_jobs(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    def restart_basin(model_index: int) -> dict[str, Any]:
        model_id = f"model_{model_index}"
        return {
            **_basins(model_index + 1)[model_index],
            "run_id": f"fcst_gfs_2026050100_{model_id}",
            "candidate_id": f"gfs:2026-05-01T00:00:00Z:{model_id}:forecast_gfs_deterministic",
            "model_package_uri": f"s3://nhms/models/{model_id}/v1/package/",
            "output_uri": f"s3://nhms/runs/fcst_gfs_2026050100_{model_id}/output/",
            "station_count": 2,
            "frequency_capabilities": {"return_periods": True},
            "display_capabilities": {"tiles": True},
            "orchestration_run_id": f"cycle_gfs_2026050100_parse_{model_id}",
            "restart_stage": "parse",
            "durable_shud_output_reused": True,
            "native_shud_resubmitted": False,
        }

    first_result = orchestrator.orchestrate_cycle("gfs", "2026050100", [restart_basin(0)])

    assert first_result.status == "complete"
    assert [submission["stage"] for submission in client.submissions] == [
        "parse",
        "state_save_qc",
        "frequency",
        "publish",
    ]
    for stage in ("parse", "state_save_qc", "frequency", "publish"):
        job = repository.jobs[f"job_cycle_gfs_2026050100_parse_model_0_{stage}"]
        assert job["run_id"] == "cycle_gfs_2026050100_parse_model_0"
        assert job["model_id"] == "model_0"

    repository.jobs["job_cycle_gfs_2026050100_parse_model_0_parse_active_sibling"] = {
        "job_id": "job_cycle_gfs_2026050100_parse_model_0_parse_active_sibling",
        "run_id": "cycle_gfs_2026050100_parse_model_0",
        "cycle_id": "gfs_2026050100",
        "job_type": "parse_output_array",
        "slurm_job_id": "unrelated-active-sibling",
        "model_id": "model_0",
        "status": "running",
        "stage": "parse",
        "submitted_at": _fmt(_dt("2026-05-01T01:00:00Z")),
        "started_at": _fmt(_dt("2026-05-01T01:01:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "log_uri": "s3://nhms/runs/cycle_gfs_2026050100_parse_model_0/logs/parse-active.log",
    }
    first_submission_count = len(client.submissions)

    second_result = orchestrator.orchestrate_cycle("gfs", "2026050100", [restart_basin(1)])

    assert second_result.status == "complete"
    assert [submission["stage"] for submission in client.submissions[first_submission_count:]] == [
        "parse",
        "state_save_qc",
        "frequency",
        "publish",
    ]
    assert [stage.pipeline_job_id for stage in second_result.stages] == [
        "job_cycle_gfs_2026050100_parse_model_1_parse",
        "job_cycle_gfs_2026050100_parse_model_1_state_save_qc",
        "job_cycle_gfs_2026050100_parse_model_1_frequency",
        "job_cycle_gfs_2026050100_parse_model_1_publish",
    ]
    for stage in ("parse", "state_save_qc", "frequency", "publish"):
        job = repository.jobs[f"job_cycle_gfs_2026050100_parse_model_1_{stage}"]
        assert job["run_id"] == "cycle_gfs_2026050100_parse_model_1"
        assert job["model_id"] == "model_1"
    assert repository.jobs["job_cycle_gfs_2026050100_parse_model_0_parse_active_sibling"]["status"] == "running"


def test_forecast_stage_writes_runtime_manifests_and_manifest_index_paths(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    assert result.status == "complete"
    assert set(repository.hydro_runs) == {"run_0", "run_1"}
    for task in forecast_submission["tasks"]:
        manifest_path = Path(task["manifest_path"])
        assert manifest_path == tmp_path / "workspace" / "runs" / task["run_id"] / "input" / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["run_id"] == task["run_id"]
        assert manifest["run_type"] == "forecast"
        assert manifest["scenario_id"] == "forecast_gfs_deterministic"
        assert manifest["source_id"] == "gfs"
        assert manifest["start_time"] == "2026-05-01T00:00:00Z"
        assert manifest["end_time"] == "2026-05-08T00:00:00Z"
        assert manifest["model"]["model_id"] == task["model_id"]
        assert manifest["model"]["basin_version_id"] == task["basin_version_id"]
        assert manifest["model"]["river_network_version_id"] == task["river_network_version_id"]
        assert manifest["forcing"]["forcing_uri"]
        assert manifest["outputs"]["run_manifest_uri"].endswith(f"runs/{task['run_id']}/input/manifest.json")
        assert repository.hydro_runs[task["run_id"]]["status"] == "created"

    index_path = Path(forecast_submission["manifest"]["manifest_index_path"])
    index_entries = json.loads(index_path.read_text(encoding="utf-8"))
    assert [entry["manifest_path"] for entry in index_entries] == [
        str(tmp_path / "workspace" / "runs" / "run_0" / "input" / "manifest.json"),
        str(tmp_path / "workspace" / "runs" / "run_1" / "input" / "manifest.json"),
    ]


def test_forecast_runtime_manifest_write_rejects_symlink_target_without_submission(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    workspace = tmp_path / "workspace"
    target = workspace / "runs" / "run_1" / "input" / "manifest.json"
    target.parent.mkdir(parents=True)
    target.write_text("untouched", encoding="utf-8")
    link = workspace / "runs" / "run_0" / "input" / "manifest.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert", "forcing"]
    assert target.read_text(encoding="utf-8") == "untouched"
    forecast_job = repository.jobs["job_cycle_gfs_2026050100_forecast"]
    assert forecast_job["status"] == "submission_failed"
    assert forecast_job["error_code"] == "RUNTIME_MANIFEST_WRITE_FAILED"


def test_cycle_manifest_index_write_rejects_symlink_target_without_stage_submission(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    workspace = tmp_path / "workspace"
    target = workspace / "runs" / "sibling" / "input" / "forcing_manifest_index.json"
    target.parent.mkdir(parents=True)
    target.write_text("untouched", encoding="utf-8")
    link = workspace / "runs" / "cycle_gfs_2026050100" / "input" / "forcing_manifest_index.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert"]
    assert target.read_text(encoding="utf-8") == "untouched"
    forcing_job = repository.jobs["job_cycle_gfs_2026050100_forcing"]
    assert forcing_job["status"] == "submission_failed"
    assert forcing_job["error_code"] == "CYCLE_MANIFEST_INDEX_WRITE_FAILED"


def test_cycle_manifest_index_rejects_task_count_over_limit_before_stage_submission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from packages.common import manifest_index as manifest_index_module

    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    monkeypatch.setattr(manifest_index_module, "MAX_MANIFEST_INDEX_ENTRIES", 1)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert"]
    index_path = tmp_path / "workspace" / "runs" / "cycle_gfs_2026050100" / "input" / "forcing_manifest_index.json"
    assert not index_path.exists()
    forcing_job = repository.jobs["job_cycle_gfs_2026050100_forcing"]
    assert forcing_job["status"] == "submission_failed"
    assert forcing_job["error_code"] == "CYCLE_MANIFEST_INDEX_INVALID"


def test_cycle_manifest_index_rejects_serialized_size_over_limit_before_stage_submission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from packages.common import manifest_index as manifest_index_module

    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    monkeypatch.setattr(manifest_index_module, "MAX_MANIFEST_INDEX_BYTES", 32)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert"]
    index_path = tmp_path / "workspace" / "runs" / "cycle_gfs_2026050100" / "input" / "forcing_manifest_index.json"
    assert not index_path.exists()
    forcing_job = repository.jobs["job_cycle_gfs_2026050100_forcing"]
    assert forcing_job["status"] == "submission_failed"
    assert forcing_job["error_code"] == "CYCLE_MANIFEST_INDEX_INVALID"


def test_model_run_identity_and_quality_contracts_propagate_to_worker_manifests(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "candidate_id": "gfs:2026-05-01T00:00:00Z:model_0:forecast_gfs_deterministic",
        "run_id": "fcst_gfs_2026050100_model_0",
        "forcing_version_id": "forc_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "station_count": 2,
        "segment_count": 3,
        "station_ids": ["sta_001", "sta_002"],
        "frequency_capabilities": {
            "return_periods": True,
            "curves_available": False,
            "warning_thresholds_available": False,
        },
        "display_capabilities": {"tiles": True, "optional_weather_available": False},
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    parse_submission = next(submission for submission in client.submissions if submission["stage"] == "parse")
    frequency_submission = next(submission for submission in client.submissions if submission["stage"] == "frequency")
    publish_submission = next(submission for submission in client.submissions if submission["stage"] == "publish")
    task = forecast_submission["tasks"][0]
    runtime_manifest = json.loads(Path(task["manifest_path"]).read_text(encoding="utf-8"))

    assert result.status == "complete"
    assert runtime_manifest["identity"]["candidate_id"] == basin["candidate_id"]
    assert runtime_manifest["identity"]["run_id"] == basin["run_id"]
    assert runtime_manifest["identity"]["schema_version"] == "nhms.production.identity_status_uri_contract.v1"
    assert runtime_manifest["identity"]["contract_id"] == "m23-qhh-22-production-identity-status-uri.v1"
    assert runtime_manifest["identity"]["canonical_product_id"] == "canon_gfs_2026050100"
    assert runtime_manifest["identity"]["forcing_version_id"] == basin["forcing_version_id"]
    assert runtime_manifest["identity"]["hydro_run_id"] == basin["run_id"]
    assert runtime_manifest["identity"]["published_manifest_id"] == f"manifest_{basin['run_id']}"
    assert runtime_manifest["identity"]["basin_id"] == basin["basin_id"]
    assert "pipeline_job_id" not in runtime_manifest["identity"]
    assert runtime_manifest["model"]["model_package_uri"] == basin["model_package_uri"]
    assert runtime_manifest["model"]["model_package_manifest_uri"] == "s3://nhms/models/model_0/v1/manifest.json"
    assert runtime_manifest["forcing"]["station_count"] == 2
    assert runtime_manifest["runtime"]["mode"] == "native_shud_project"
    assert runtime_manifest["runtime"]["output_river"]["river_network_version_id"] == "river_v0"
    assert runtime_manifest["outputs"]["output_uri"] == "s3://nhms/runs/fcst_gfs_2026050100_model_0/output/"
    assert runtime_manifest["quality_states"]["frequency"]["unavailable_products"] == [
        "frequency_curves",
        "warning_thresholds",
    ]
    assert runtime_manifest["quality_states"]["display"]["unavailable_products"] == ["optional_weather_products"]
    assert {blocker["code"] for blocker in runtime_manifest["residual_blockers"]} == {
        "FREQUENCY_CURVES_UNAVAILABLE",
        "WARNING_THRESHOLDS_UNAVAILABLE",
        "OPTIONAL_WEATHER_PRODUCTS_UNAVAILABLE",
    }
    parse_entry = parse_submission["tasks"][0]
    frequency_entry = frequency_submission["tasks"][0]
    model_run_evidence = parse_submission["manifest"]["model_runs"][0]
    assert model_run_evidence["production_stage"] == "parse"
    assert model_run_evidence["canonical_product_id"] == "canon_gfs_2026050100"
    assert model_run_evidence["published_manifest_id"] == f"manifest_{basin['run_id']}"
    assert model_run_evidence["basin_id"] == basin["basin_id"]
    assert "pipeline_job_id" not in model_run_evidence
    assert parse_entry["model_run_assembly"]["identity"]["candidate_id"] == basin["candidate_id"]
    assert frequency_entry["model_run_assembly"]["identity"]["run_id"] == basin["run_id"]
    assert frequency_submission["manifest"]["quality_states"][0]["quality_flag"] == "frequency_inputs_unavailable"
    assert publish_submission["metadata"]["residual_blockers"]
    assert publish_submission["metadata"]["quality_states"][0]["run_id"] == basin["run_id"]


def test_nested_forcing_station_metadata_reaches_runtime_manifest(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "run_id": "fcst_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "resource_profile": {
            "forcing_station_metadata": {
                "station_count": 386,
                "station_ids": ["sta_001", "sta_002"],
                "source": "qhh_package_manifest",
                "quality_flag": "ok",
                "shud_station": "qhh.tsd.forc",
            }
        },
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    runtime_manifest = json.loads(Path(forecast_submission["tasks"][0]["manifest_path"]).read_text(encoding="utf-8"))
    assert result.status == "complete"
    assert runtime_manifest["forcing"]["station_metadata"] == {
        "schema_version": "nhms.forcing_station_metadata.v1",
        "state": "ready",
        "station_count": 386,
        "station_ids": ["sta_001", "sta_002"],
        "source": "qhh_package_manifest",
        "shud_station": "qhh.tsd.forc",
        "quality_flag": "ok",
    }
    assert runtime_manifest["forcing"]["shud_station"] == "qhh.tsd.forc"


def test_missing_station_forcing_is_quality_state_without_discarding_output_uri(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "run_id": "fcst_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True, "optional_weather_available": False},
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    publish_submission = next(submission for submission in client.submissions if submission["stage"] == "publish")
    quality = publish_submission["metadata"]["quality_states"][0]
    blockers = publish_submission["metadata"]["residual_blockers"]
    assert result.status == "complete"
    assert quality["quality_states"]["station_forcing"] == {
        "state": "unavailable",
        "quality_flag": "station_forcing_unavailable",
    }
    assert quality["output_uri"].endswith("runs/fcst_gfs_2026050100_model_0/output/")
    assert any(blocker["code"] == "STATION_FORCING_UNAVAILABLE" for blocker in blockers)
    assert any(blocker["code"] == "OPTIONAL_WEATHER_PRODUCTS_UNAVAILABLE" for blocker in blockers)


def test_missing_output_river_metadata_is_unavailable_not_fabricated_ready_segment(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "run_id": "fcst_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "station_count": 2,
        "segment_count": None,
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    runtime_manifest = json.loads(Path(forecast_submission["tasks"][0]["manifest_path"]).read_text(encoding="utf-8"))
    assert result.status == "complete"
    assert runtime_manifest["runtime"]["output_river"]["state"] == "unavailable"
    assert runtime_manifest["runtime"]["output_river"]["segment_count"] == 0
    assert runtime_manifest["identity"]["segment_count"] == 0
    assert runtime_manifest["quality_states"]["output_river"] == {
        "state": "unavailable",
        "quality_flag": "output_river_unavailable",
        "segment_count": 0,
    }
    assert any(blocker["code"] == "OUTPUT_RIVER_UNAVAILABLE" for blocker in runtime_manifest["residual_blockers"])


def test_valid_output_river_metadata_remains_ready(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "run_id": "fcst_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "station_count": 2,
        "output_river": {
            "segment_count": 2,
            "river_segment_ids": ["seg_001", "seg_002"],
            "identity_source": "package_manifest",
        },
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    runtime_manifest = json.loads(Path(forecast_submission["tasks"][0]["manifest_path"]).read_text(encoding="utf-8"))
    assert result.status == "complete"
    assert runtime_manifest["runtime"]["output_river"]["state"] == "ready"
    assert runtime_manifest["runtime"]["output_river"]["segment_count"] == 2
    assert runtime_manifest["runtime"]["output_river"]["river_segment_ids"] == ["seg_001", "seg_002"]


def test_scheduler_style_relative_output_key_does_not_downgrade_runtime_output_uri(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "run_id": "fcst_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "output_key": "runs/fcst_gfs_2026050100_model_0/output/",
        "station_count": 2,
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    task = forecast_submission["tasks"][0]
    runtime_manifest = json.loads(Path(task["manifest_path"]).read_text(encoding="utf-8"))
    assert result.status == "complete"
    assert runtime_manifest["outputs"]["output_uri"] == "s3://nhms/runs/fcst_gfs_2026050100_model_0/output/"
    assert task["output_uri"] == "s3://nhms/runs/fcst_gfs_2026050100_model_0/output/"


def test_relative_output_uri_falls_back_to_object_store_output_uri(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "run_id": "fcst_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "output_uri": "runs/fcst_gfs_2026050100_model_0/output/",
        "log_uri": "runs/fcst_gfs_2026050100_model_0/logs/",
        "station_count": 2,
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    task = forecast_submission["tasks"][0]
    runtime_manifest = json.loads(Path(task["manifest_path"]).read_text(encoding="utf-8"))
    assert result.status == "complete"
    assert runtime_manifest["outputs"]["output_uri"] == "s3://nhms/runs/fcst_gfs_2026050100_model_0/output/"
    assert runtime_manifest["outputs"]["log_uri"] == "s3://nhms/runs/fcst_gfs_2026050100_model_0/logs/"


def test_legacy_explicit_absolute_output_uri_is_preserved_outside_production_candidate_scope(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "output_uri": "s3://nhms/custom/fcst_gfs_2026050100_model_0/output",
        "log_uri": "s3://nhms/custom/fcst_gfs_2026050100_model_0/logs",
        "station_count": 2,
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    task = forecast_submission["tasks"][0]
    runtime_manifest = json.loads(Path(task["manifest_path"]).read_text(encoding="utf-8"))
    assert result.status == "complete"
    assert runtime_manifest["outputs"]["output_uri"] == "s3://nhms/custom/fcst_gfs_2026050100_model_0/output/"
    assert runtime_manifest["outputs"]["log_uri"] == "s3://nhms/custom/fcst_gfs_2026050100_model_0/logs/"


def test_production_candidate_wrong_absolute_output_uri_rejected_before_manifest_writes(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "candidate_id": "gfs:2026-05-01T00:00:00Z:model_0:forecast_gfs_deterministic",
        "run_id": "fcst_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "output_uri": "s3://wrong-bucket/prod/runs/fcst_gfs_2026050100_model_0/output/",
        "station_count": 2,
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
    }

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    assert exc_info.value.error_code == "CANDIDATE_IDENTITY_MISMATCH"
    assert exc_info.value.details["field"] == "output_uri"
    assert client.submissions == []
    assert repository.hydro_runs == {}
    assert not (tmp_path / "workspace" / "runs").exists()


def test_production_candidate_relative_output_uri_normalizes_to_canonical_uri(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "candidate_id": "gfs:2026-05-01T00:00:00Z:model_0:forecast_gfs_deterministic",
        "run_id": "fcst_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "output_uri": "runs/fcst_gfs_2026050100_model_0/output",
        "station_count": 2,
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    runtime_manifest = json.loads(Path(forecast_submission["tasks"][0]["manifest_path"]).read_text(encoding="utf-8"))
    assert result.status == "complete"
    assert runtime_manifest["outputs"]["output_uri"] == "s3://nhms/runs/fcst_gfs_2026050100_model_0/output/"


@pytest.mark.parametrize("missing_field", ["basin_version_id", "river_network_version_id", "model_package_uri"])
def test_production_candidate_missing_package_or_version_metadata_rejected_before_side_effects(
    tmp_path: Path,
    missing_field: str,
) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "candidate_id": "gfs:2026-05-01T00:00:00Z:model_0:forecast_gfs_deterministic",
        "run_id": "fcst_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "station_count": 2,
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
    }
    basin.pop(missing_field, None)

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    assert exc_info.value.error_code == "PRODUCTION_CANDIDATE_METADATA_UNAVAILABLE"
    assert exc_info.value.details["missing_fields"] == [missing_field]
    assert client.submissions == []
    assert repository.hydro_runs == {}
    assert not (tmp_path / "workspace" / "runs").exists()


def test_legacy_manual_basin_without_production_candidate_identity_keeps_default_metadata_compatibility(
    tmp_path: Path,
) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {"model_id": "manual_model", "station_count": 1, "segment_count": 1}

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    runtime_manifest = json.loads(Path(forecast_submission["tasks"][0]["manifest_path"]).read_text(encoding="utf-8"))
    assert result.status == "complete"
    assert runtime_manifest["model"]["basin_version_id"] == "manual_model_basin"
    assert runtime_manifest["model"]["river_network_version_id"] == "manual_model_river"
    assert runtime_manifest["model"]["model_package_uri"] == "models/manual_model/"


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("candidate_id", "gfs:2026-05-01T00:00:00Z:model_0:wrong_scenario"),
        ("run_id", "fcst_gfs_2026050100_other_model"),
        ("forcing_version_id", "forc_gfs_2026050100_other_model"),
        ("cycle_id", "gfs_2026050106"),
        ("cycle_time", "2026050106"),
        ("scenario_id", "wrong_scenario"),
        ("output_uri", "s3://nhms/runs/fcst_gfs_2026050100_other_model/output/"),
    ],
)
def test_prefilled_identity_mismatch_rejected_before_manifest_writes(
    tmp_path: Path,
    field_name: str,
    field_value: str,
) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "run_id": "fcst_gfs_2026050100_model_0",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "station_count": 2,
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
    }
    basin[field_name] = field_value

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    assert exc_info.value.error_code == "CANDIDATE_IDENTITY_MISMATCH"
    assert client.submissions == []
    assert repository.hydro_runs == {}
    assert not (tmp_path / "workspace" / "runs").exists()


@pytest.mark.parametrize("field_name", ["candidate_id", "run_id", "run_manifest_uri", "output_uri", "model_id"])
def test_duplicate_sibling_identity_rejected_before_manifest_writes(tmp_path: Path, field_name: str) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basins = _basins(2)
    basins[0].update(
        {
            "run_id": "fcst_gfs_2026050100_model_0",
            "candidate_id": "gfs:2026-05-01T00:00:00Z:model_0:forecast_gfs_deterministic",
            "model_package_uri": "s3://nhms/models/model_0/v1/package/",
            "run_manifest_uri": "s3://nhms/runs/fcst_gfs_2026050100_model_0/input/manifest.json",
            "output_uri": "s3://nhms/runs/fcst_gfs_2026050100_model_0/output/",
        }
    )
    basins[1].update(
        {
            "run_id": "fcst_gfs_2026050100_model_1",
            "candidate_id": "gfs:2026-05-01T00:00:00Z:model_1:forecast_gfs_deterministic",
            "model_package_uri": "s3://nhms/models/model_1/v1/package/",
            "run_manifest_uri": "s3://nhms/runs/fcst_gfs_2026050100_model_1/input/manifest.json",
            "output_uri": "s3://nhms/runs/fcst_gfs_2026050100_model_1/output/",
        }
    )
    basins[1][field_name] = basins[0][field_name]

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", "2026050100", basins)

    assert exc_info.value.error_code == "DUPLICATE_CANDIDATE_IDENTITY"
    assert exc_info.value.details["field"] == field_name
    assert client.submissions == []
    assert repository.hydro_runs == {}
    assert not (tmp_path / "workspace" / "runs").exists()


def test_hydro_run_creation_is_idempotent_for_existing_created_status() -> None:
    repository = FakeCycleRepository()
    basin = _basins(1)[0]
    manifest = {
        "run_id": "run_0",
        "source_id": "gfs",
        "cycle_time": "2026-05-01T00:00:00Z",
        "model": {
            "model_id": "model_0",
            "basin_version_id": "basin_v0",
            "river_network_version_id": "model_0_river",
        },
    }

    first = repository.create_hydro_run_from_basin(basin, manifest)
    second = repository.create_hydro_run_from_basin(basin, manifest)

    assert first["run_id"] == second["run_id"] == "run_0"
    assert len(repository.hydro_runs) == 1
    assert repository.hydro_runs["run_0"]["status"] == "created"


def test_cycle_orchestration_rejects_db_active_duplicate(tmp_path: Path) -> None:
    repository = FakeCycleRepository(active=True)
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    assert exc_info.value.error_code == "PIPELINE_ALREADY_ACTIVE"
    assert client.submissions == []


def test_repeated_scan_with_active_cycle_does_not_resubmit(tmp_path: Path) -> None:
    # M23-7 (#258): re-scans while a cycle is already active must be rejected by
    # the active guard and never produce a duplicate Slurm submission.
    repository = FakeCycleRepository(active=True)
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    for _ in range(3):
        with pytest.raises(OrchestratorError) as exc_info:
            orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))
        assert exc_info.value.error_code == "PIPELINE_ALREADY_ACTIVE"

    assert client.submissions == []


def test_trigger_forecast_rejects_candidate_scoped_active_restart(tmp_path: Path) -> None:
    class ActiveRestartRepository(FakeCycleRepository):
        def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
            del source_id, cycle_time, model_id
            return True

        def load_model_context(self, model_id: str) -> ModelContext:
            raise AssertionError(f"active guard must run before loading {model_id}")

        def find_forcing_context(self, *, source_id: str, cycle_time: datetime, model_id: str) -> ForcingContext:
            raise AssertionError("active guard must run before forcing lookup")

    repository = ActiveRestartRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.trigger_forecast(source_id="gfs", cycle_time="2026050100", model_id="model_a")

    assert exc_info.value.error_code == "PIPELINE_ALREADY_ACTIVE"
    assert client.submissions == []


def test_cycle_orchestration_rejects_unsafe_source_and_basin_ids(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    with pytest.raises(OrchestratorError) as source_exc:
        orchestrator.orchestrate_cycle("../gfs", "2026050100", _basins(1))

    unsafe_basins = _basins(1)
    unsafe_basins[0]["basin_id"] = "../basin"
    with pytest.raises(OrchestratorError) as basin_exc:
        orchestrator.orchestrate_cycle("gfs", "2026050100", unsafe_basins)

    assert source_exc.value.error_code == "UNSAFE_IDENTIFIER"
    assert basin_exc.value.error_code == "UNSAFE_IDENTIFIER"
    assert client.submissions == []


def test_stage_three_failure_blocks_downstream_stages(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(fail_stage="forcing", array_results_by_stage={"forcing": ["failed", "failed"]})
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert", "forcing"]
    assert repository.cycle_statuses[-1] == "failed_forcing"


def test_array_stage_requires_array_submit_contract_before_submission(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = SubmitJobOnlyCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    forcing_result = result.stages[-1]
    forcing_job = repository.jobs["job_cycle_gfs_2026050100_forcing"]
    assert result.status == "failed"
    assert forcing_result.stage == "forcing"
    assert forcing_result.status == "submission_failed"
    assert forcing_result.error_code == "SLURM_ARRAY_SUBMIT_UNSUPPORTED"
    assert forcing_job["status"] == "submission_failed"
    assert forcing_job["slurm_job_id"] is None
    assert forcing_job["error_code"] == "SLURM_ARRAY_SUBMIT_UNSUPPORTED"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert"]
    assert all(payload["job_type"] != "produce_forcing_array" for payload in client.submit_job_payloads)
    assert "forecast" not in [submission["stage"] for submission in client.submissions]
    assert repository.cycle_statuses[-1] == "failed_forcing"


def test_forecast_manifest_write_failure_marks_pipeline_failed(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(
        tmp_path,
        repository,
        client,
        object_store=WriteFailingObjectStore(tmp_path / "object-store", "s3://nhms"),
    )

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert", "forcing"]
    job = repository.jobs["job_cycle_gfs_2026050100_forecast"]
    assert job["status"] == "submission_failed"
    assert job["error_code"] == "RUNTIME_MANIFEST_WRITE_FAILED"
    assert repository.cycle_statuses[-1] == "failed_run"


def test_forecast_submission_failure_marks_staged_hydro_runs_failed(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    client.fail_next_array_submission_stage = "forecast"
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert", "forcing"]
    assert {run["status"] for run in repository.hydro_runs.values()} == {"failed"}
    assert {
        run["error_code"]
        for run in repository.hydro_runs.values()
    } == {"SBATCH_SUBMISSION_FAILED"}
    assert repository.jobs["job_cycle_gfs_2026050100_forecast"]["status"] == "submission_failed"


def test_invalid_forecast_runtime_manifest_blocks_publish(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client, orchestrator_cls=InvalidJsonForecastOrchestrator)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert", "forcing"]
    assert repository.jobs["job_cycle_gfs_2026050100_forecast"]["status"] == "submission_failed"
    assert "publish" not in [submission["stage"] for submission in client.submissions]
    assert repository.cycle_statuses[-1] == "failed_run"


def test_missing_forecast_runtime_manifest_blocks_publish(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client, orchestrator_cls=MissingManifestForecastOrchestrator)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert", "forcing"]
    assert repository.jobs["job_cycle_gfs_2026050100_forecast"]["status"] == "submission_failed"
    assert "publish" not in [submission["stage"] for submission in client.submissions]
    assert repository.cycle_statuses[-1] == "failed_run"


def test_unreadable_forecast_runtime_manifest_blocks_publish(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client, orchestrator_cls=UnreadableManifestForecastOrchestrator)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert", "forcing"]
    job = repository.jobs["job_cycle_gfs_2026050100_forecast"]
    assert job["status"] == "submission_failed"
    assert job["error_code"] == "RUNTIME_MANIFEST_READ_FAILED"
    assert "publish" not in [submission["stage"] for submission in client.submissions]
    assert repository.cycle_statuses[-1] == "failed_run"


def test_permanently_failed_stage_blocks_downstream_stages(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    cycle_id = "gfs_2026050100"
    run_id = "cycle_gfs_2026050100"
    repository.jobs[f"job_{run_id}_download"] = {
        "job_id": f"job_{run_id}_download",
        "run_id": run_id,
        "cycle_id": cycle_id,
        "job_type": "download_source_cycle",
        "slurm_job_id": "3001",
        "model_id": None,
        "status": "permanently_failed",
        "stage": "download",
        "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
        "started_at": None,
        "finished_at": _fmt(_dt("2026-05-01T00:02:00Z")),
        "exit_code": 1,
        "error_code": "SLURM_TIMEOUT",
        "error_message": "retry budget exhausted",
        "log_uri": None,
    }
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "failed"
    assert [stage.status for stage in result.stages] == ["permanently_failed"]
    assert client.submissions == []
    assert repository.cycle_statuses[-1] == "failed_download"


def test_failed_stage_auto_retries_before_downstream_stages(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(
        failures_before_success_by_stage={"download": 1},
        error_code_by_stage={"download": "SLURM_TIMEOUT"},
    )
    retry_service = FakeRetryService(max_retries=1)
    orchestrator = _orchestrator(tmp_path, repository, client, retry_service=retry_service)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "complete"
    assert [submission["stage"] for submission in client.submissions[:3]] == ["download", "download", "convert"]
    assert [stage.status for stage in result.stages] == ["succeeded"] * len(M3_STAGES)
    assert retry_service.handled_job_ids == ["job_cycle_gfs_2026050100_download"]


def test_poll_timeout_persists_failed_job_and_is_retry_eligible(monkeypatch, tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(never_terminal_stage="download")
    retry_service = FakeRetryService(max_retries=0)
    orchestrator = _orchestrator(tmp_path, repository, client, retry_service=retry_service)
    orchestrator.config = OrchestratorConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=tmp_path / "object-store",
        object_store_prefix="s3://nhms",
        poll_interval_seconds=1,
        job_timeout_seconds=1,
    )
    monotonic_values = iter([0.0, 0.0, 2.0, 2.0])
    monkeypatch.setattr("services.orchestrator.chain.time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr("services.orchestrator.chain.time.sleep", lambda _seconds: None)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    timed_out_job = repository.jobs["job_cycle_gfs_2026050100_download"]
    assert result.status == "failed"
    assert timed_out_job["status"] == "failed"
    assert timed_out_job["error_code"] == "SLURM_JOB_TIMEOUT"
    assert retry_service.handled_job_ids == ["job_cycle_gfs_2026050100_download"]
    assert any(
        event["event_type"] == "timeout"
        and event["entity_id"] == "job_cycle_gfs_2026050100_download"
        and event["status_to"] == "failed"
        and event["details"]["error_code"] == "SLURM_JOB_TIMEOUT"
        for event in repository.events
    )


def test_poll_timeout_publish_failure_persists_failed_job_without_advertising_uri(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    publish_root_file = tmp_path / "published-as-file"
    publish_root_file.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(publish_root_file))
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(never_terminal_stage="download")
    orchestrator = _orchestrator(tmp_path, repository, client)
    orchestrator.config = OrchestratorConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=tmp_path / "object-store",
        object_store_prefix="s3://nhms",
        poll_interval_seconds=1,
        job_timeout_seconds=1,
    )
    monotonic_values = iter([0.0, 0.0, 2.0, 2.0])
    monkeypatch.setattr("services.orchestrator.chain.time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr("services.orchestrator.chain.time.sleep", lambda _seconds: None)

    expected = (
        "published://logs/gfs/2026050100/"
        "cycle_gfs_2026050100/"
        "job_cycle_gfs_2026050100_download.out"
    )
    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    timed_out_job = repository.jobs["job_cycle_gfs_2026050100_download"]
    assert exc_info.value.error_code == "PUBLISHED_LOG_WRITE_FAILED"
    assert exc_info.value.details == {"log_uri": expected}
    assert timed_out_job["status"] == "failed"
    assert timed_out_job["error_code"] == "SLURM_JOB_TIMEOUT"
    assert timed_out_job["log_uri"] is None
    timeout_event = next(event for event in repository.events if event["event_type"] == "timeout")
    assert timeout_event["status_to"] == "failed"
    assert timeout_event["details"].get("slurm", {}).get("log_uri") is None
    assert repository.cycle_statuses[-1] == "failed_download"


def test_poll_timeout_legacy_log_write_failure_persists_failed_job_without_advertising_uri(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("NHMS_PUBLISHED_ARTIFACT_ROOT", raising=False)
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(never_terminal_stage="download")
    object_store = LogWriteFailingObjectStore(tmp_path / "object-store", "s3://nhms")
    orchestrator = _orchestrator(tmp_path, repository, client, object_store=object_store)
    orchestrator.config = OrchestratorConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=tmp_path / "object-store",
        object_store_prefix="s3://nhms",
        poll_interval_seconds=1,
        job_timeout_seconds=1,
    )
    monotonic_values = iter([0.0, 0.0, 2.0, 2.0])
    monkeypatch.setattr("services.orchestrator.chain.time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr("services.orchestrator.chain.time.sleep", lambda _seconds: None)

    expected = "s3://nhms/runs/cycle_gfs_2026050100/logs/download.log"
    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    timed_out_job = repository.jobs["job_cycle_gfs_2026050100_download"]
    assert exc_info.value.error_code == "PUBLISHED_LOG_WRITE_FAILED"
    assert exc_info.value.details == {"log_uri": expected}
    assert timed_out_job["status"] == "failed"
    assert timed_out_job["error_code"] == "SLURM_JOB_TIMEOUT"
    assert timed_out_job["log_uri"] is None
    timeout_event = next(event for event in repository.events if event["event_type"] == "timeout")
    assert timeout_event["status_to"] == "failed"
    assert timeout_event["details"].get("slurm", {}).get("log_uri") is None
    assert all(
        event["details"].get("slurm", {}).get("log_uri") != expected
        for event in repository.events
    )
    assert repository.cycle_statuses[-1] == "failed_download"


def test_partial_array_retry_only_resubmits_failed_basin_tasks(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(
        array_results_by_stage={"forcing": [["succeeded", "failed", "succeeded"], ["succeeded"]]}
    )
    retry_service = FakeRetryService(max_retries=1)
    orchestrator = _orchestrator(tmp_path, repository, client, retry_service=retry_service)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(3))

    forcing_submissions = [submission for submission in client.submissions if submission["stage"] == "forcing"]
    assert result.status == "complete"
    assert [task["model_id"] for task in forcing_submissions[0]["tasks"]] == ["model_0", "model_1", "model_2"]
    assert [task["model_id"] for task in forcing_submissions[1]["tasks"]] == ["model_1"]
    assert [submission["stage"] for submission in client.submissions].count("forcing") == 2
    assert client.submissions[4]["stage"] == "forecast"
    assert [task["model_id"] for task in client.submissions[4]["tasks"]] == ["model_0", "model_1", "model_2"]


def test_partial_array_retry_persists_submission_under_retry_job_id_with_real_retry_service(
    tmp_path: Path,
) -> None:
    store = _pipeline_store()
    repository = StoreBackedCycleRepository(store)
    client = FakeCycleSlurmClient(
        array_results_by_stage={"forcing": [["succeeded", "failed", "succeeded"], ["succeeded"]]}
    )
    retry_service = RetryService(store, RetryConfig(max_retries=1, backoff_schedule=[0]))
    orchestrator = _orchestrator(tmp_path, repository, client, retry_service=retry_service)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(3))

    retry_job = store.get_job("job_cycle_gfs_2026050100_forcing_retry_1")
    forcing_submissions = [submission for submission in client.submissions if submission["stage"] == "forcing"]
    pending_retry_jobs = [
        job.job_id
        for job in store.session.scalars(select(PipelineJob).where(PipelineJob.status == "pending"))
    ]
    assert result.status == "complete"
    assert retry_job is not None
    assert retry_job.status == "succeeded"
    assert retry_job.slurm_job_id == "2004"
    assert retry_job.retry_count == 1
    assert pending_retry_jobs == []
    assert repository.jobs["job_cycle_gfs_2026050100_forcing_retry_1"]["slurm_job_id"] == "2004"
    assert repository.jobs["job_cycle_gfs_2026050100_forcing_retry_1"]["status"] == "succeeded"
    assert repository.jobs["job_cycle_gfs_2026050100_forcing"]["status"] == "partially_failed"
    assert [task["model_id"] for task in forcing_submissions[1]["tasks"]] == ["model_1"]
    retry_event = next(
        event
        for event in repository.events
        if event["entity_id"] == "job_cycle_gfs_2026050100_forcing_retry_1"
        and event["status_to"] == "succeeded"
    )
    assert retry_event["details"]["task_results"][0]["array_task_id"] == 0
    assert retry_event["details"]["task_results"][0]["original_task_id"] == 1
    assert retry_event["details"]["task_results"][0]["model_id"] == "model_1"
    assert retry_event["details"]["task_results"][0]["run_id"] == "run_1"
    assert result.stages[2].pipeline_job_id == "job_cycle_gfs_2026050100_forcing_retry_1"
    assert result.stages[2].task_results[1]["array_task_id"] == 1
    assert result.stages[2].task_results[1]["original_task_id"] == 1
    assert result.stages[2].task_results[1]["model_id"] == "model_1"
    assert result.stages[2].task_results[1]["run_id"] == "run_1"
    assert result.stages[2].task_results[1]["status"] == "succeeded"


class _ArrayTaskIdMasterSlurmClient(FakeCycleSlurmClient):
    """Array client whose master submission reports a representative task id.

    Real array masters usually have no single task id, but when a gateway does
    surface one on the submission payload the orchestrator must persist it on the
    pipeline_job receipt and submission event (M23-7 / #258).
    """

    def _submit(self, stage: str, run_id: str, model_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        job = super()._submit(stage, run_id, model_id, payload)
        if "tasks" in payload:
            job["array_task_id"] = 0
        self.jobs[job["job_id"]]["array_task_id"] = job.get("array_task_id")
        return job


def test_array_submission_persists_slurm_receipt_with_array_task_id(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = _ArrayTaskIdMasterSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "complete"

    forcing_job = repository.jobs["job_cycle_gfs_2026050100_forcing"]
    # Receipt fields persisted on pipeline_job.
    assert forcing_job["array_task_id"] == 0
    assert forcing_job["slurm_job_id"]
    assert forcing_job["status"] in {"submitted", "running", "succeeded"}

    # Submission event records the representative array task id (not hardcoded None).
    submission_events = [
        event
        for event in repository.events
        if event["entity_id"] == "job_cycle_gfs_2026050100_forcing"
        and event["event_type"] == "submission"
    ]
    assert submission_events
    assert submission_events[0]["details"]["slurm"]["array_task_id"] == 0
    assert submission_events[0]["details"]["slurm"]["job_id"]

    # Per-task receipt coverage: each array task id is captured in stage results.
    forcing_stage = next(stage for stage in result.stages if stage.stage == "forcing")
    task_results = forcing_stage.task_results
    assert {task["array_task_id"] for task in task_results} == {0, 1}
    assert all(task["log_uri"] for task in task_results)


def test_array_master_without_task_id_persists_null_array_task_id(tmp_path: Path) -> None:
    # never-break: a master that reports no task id must still persist cleanly.
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "complete"
    forcing_job = repository.jobs["job_cycle_gfs_2026050100_forcing"]
    assert forcing_job["array_task_id"] is None


def test_restart_stage_parse_skips_durable_upstream_stages_without_existing_upstream_rows(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "run_id": "fcst_gfs_2026050100_model_0",
        "candidate_id": "gfs:2026-05-01T00:00:00Z:model_0:forecast_gfs_deterministic",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "output_uri": "s3://nhms/runs/fcst_gfs_2026050100_model_0/output/",
        "station_count": 2,
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
        "restart_stage": "parse",
        "durable_shud_output_reused": True,
        "native_shud_resubmitted": False,
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    assert result.status == "complete"
    assert [submission["stage"] for submission in client.submissions] == [
        "parse",
        "state_save_qc",
        "frequency",
        "publish",
    ]
    assert "job_cycle_gfs_2026050100_download" not in repository.jobs
    assert "job_cycle_gfs_2026050100_forecast" not in repository.jobs


@pytest.mark.parametrize(
    ("restart_stage", "expected_stages"),
    [
        ("frequency", ["frequency", "publish"]),
        ("publish", ["publish"]),
    ],
)
def test_restart_stage_frequency_and_publish_skip_durable_upstream_stages(
    tmp_path: Path,
    restart_stage: str,
    expected_stages: list[str],
) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basin = {
        **_basins(1)[0],
        "run_id": "fcst_gfs_2026050100_model_0",
        "candidate_id": "gfs:2026-05-01T00:00:00Z:model_0:forecast_gfs_deterministic",
        "model_package_uri": "s3://nhms/models/model_0/v1/package/",
        "output_uri": "s3://nhms/runs/fcst_gfs_2026050100_model_0/output/",
        "station_count": 2,
        "frequency_capabilities": {"return_periods": True},
        "display_capabilities": {"tiles": True},
        "restart_stage": restart_stage,
        "durable_shud_output_reused": True,
        "native_shud_resubmitted": False,
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    assert result.status == "complete"
    assert [submission["stage"] for submission in client.submissions] == expected_stages
    assert "job_cycle_gfs_2026050100_forecast" not in repository.jobs
    assert "job_cycle_gfs_2026050100_parse" not in repository.jobs


def test_crash_recovery_resumes_after_last_completed_stage(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    cycle_id = "gfs_2026050100"
    run_id = "cycle_gfs_2026050100"
    for index, stage in enumerate(M3_STAGES[:4]):
        repository.jobs[f"job_{run_id}_{stage.stage}"] = {
            "job_id": f"job_{run_id}_{stage.stage}",
            "run_id": run_id,
            "cycle_id": cycle_id,
            "job_type": stage.job_type,
            "slurm_job_id": str(3000 + index),
            "model_id": None,
            "status": "succeeded",
            "stage": stage.stage,
            "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z") + timedelta(minutes=index)),
            "started_at": None,
            "finished_at": None,
            "exit_code": 0,
            "error_code": None,
            "error_message": None,
            "log_uri": None,
        }
    client = FakeCycleSlurmClient(
        array_results_by_stage={
            "forcing": ["succeeded", "succeeded"],
            "forecast": ["succeeded", "succeeded"],
        }
    )
    client.jobs["3002"] = {
        "job_id": "3002",
        "run_id": run_id,
        "model_id": "model_0",
        "stage": "forcing",
        "status": "succeeded",
        "submitted_at": _fmt(_dt("2026-05-01T00:02:00Z")),
        "payload": {"tasks": [{}, {}]},
    }
    client.jobs["3003"] = {
        "job_id": "3003",
        "run_id": run_id,
        "model_id": "model_0",
        "stage": "forecast",
        "status": "succeeded",
        "submitted_at": _fmt(_dt("2026-05-01T00:03:00Z")),
        "payload": {"tasks": [{}, {}]},
    }
    orchestrator = _orchestrator(tmp_path, repository, client)
    orchestrator.object_store.write_bytes_atomic(
        "raw/gfs/2026050100/manifest.json",
        b'{"source_id":"GFS"}',
    )

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "complete"
    assert [submission["stage"] for submission in client.submissions] == [
        "parse",
        "state_save_qc",
        "frequency",
        "publish",
    ]


def test_resume_array_status_override_publishes_log_before_advertising_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published_root = tmp_path / "published"
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(published_root))
    repository = FakeCycleRepository()
    cycle_id = "gfs_2026050100"
    run_id = "cycle_gfs_2026050100"
    job_id = "job_cycle_gfs_2026050100_forcing"
    repository.jobs[job_id] = {
        "job_id": job_id,
        "run_id": run_id,
        "cycle_id": cycle_id,
        "job_type": "produce_forcing_array",
        "slurm_job_id": "3002",
        "model_id": None,
        "status": "running",
        "stage": "forcing",
        "submitted_at": _fmt(_dt("2026-05-01T00:02:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:03:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "log_uri": None,
    }
    client = FakeCycleSlurmClient(array_results_by_stage={"forcing": ["succeeded", "succeeded"]})
    client.jobs["3002"] = {
        "job_id": "3002",
        "run_id": run_id,
        "model_id": "model_0",
        "stage": "forcing",
        "status": "running",
        "submitted_at": _fmt(_dt("2026-05-01T00:02:00Z")),
        "finished_at": _fmt(_dt("2026-05-01T00:04:00Z")),
        "exit_code": 0,
        "error_code": None,
        "error_message": None,
        "payload": {"tasks": [{}, {}]},
        "stage_attempt": 0,
    }
    client.poll_counts["3002"] = 1
    orchestrator = _orchestrator(tmp_path, repository, client)

    context = CycleOrchestrationContext(
        source_id="gfs",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        cycle_id=cycle_id,
        run_id=run_id,
        all_basins=_basins(2),
        active_basins=_basins(2),
        restart_stage="forcing",
    )

    result, aggregation = orchestrator._resume_cycle_stage(M3_STAGES[2], context, repository.jobs[job_id])

    expected = (
        "published://logs/gfs/2026050100/"
        "cycle_gfs_2026050100/"
        "job_cycle_gfs_2026050100_forcing.out"
    )
    assert result.status == "succeeded"
    assert aggregation is not None
    assert repository.jobs[job_id]["log_uri"] == expected
    assert (
        published_root
        / "logs"
        / "gfs"
        / "2026050100"
        / "cycle_gfs_2026050100"
        / "job_cycle_gfs_2026050100_forcing.out"
    ).read_text(encoding="utf-8") == "ok"
    reader_result = ArtifactReader(
        ArtifactReaderConfig(published_root=published_root)
    ).read_text_tail(expected)
    assert reader_result.content == "ok"
    override_event = next(
        event
        for event in repository.events
        if event["entity_id"] == job_id and event["status_to"] == "succeeded"
    )
    assert override_event["details"]["slurm"]["log_uri"] == expected


def test_resume_array_status_override_publish_failure_does_not_advertise_missing_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_root_file = tmp_path / "published-as-file"
    publish_root_file.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(publish_root_file))
    repository = FakeCycleRepository()
    cycle_id = "gfs_2026050100"
    run_id = "cycle_gfs_2026050100"
    job_id = "job_cycle_gfs_2026050100_forcing"
    repository.jobs[job_id] = {
        "job_id": job_id,
        "run_id": run_id,
        "cycle_id": cycle_id,
        "job_type": "produce_forcing_array",
        "slurm_job_id": "3002",
        "model_id": None,
        "status": "running",
        "stage": "forcing",
        "submitted_at": _fmt(_dt("2026-05-01T00:02:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:03:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "log_uri": None,
    }
    client = FakeCycleSlurmClient(array_results_by_stage={"forcing": ["succeeded", "succeeded"]})
    client.jobs["3002"] = {
        "job_id": "3002",
        "run_id": run_id,
        "model_id": "model_0",
        "stage": "forcing",
        "status": "running",
        "submitted_at": _fmt(_dt("2026-05-01T00:02:00Z")),
        "finished_at": _fmt(_dt("2026-05-01T00:04:00Z")),
        "exit_code": 0,
        "error_code": None,
        "error_message": None,
        "payload": {"tasks": [{}, {}]},
        "stage_attempt": 0,
    }
    client.poll_counts["3002"] = 1
    orchestrator = _orchestrator(tmp_path, repository, client)

    expected = (
        "published://logs/gfs/2026050100/"
        "cycle_gfs_2026050100/"
        "job_cycle_gfs_2026050100_forcing.out"
    )
    context = CycleOrchestrationContext(
        source_id="gfs",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        cycle_id=cycle_id,
        run_id=run_id,
        all_basins=_basins(2),
        active_basins=_basins(2),
        restart_stage="forcing",
    )
    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator._resume_cycle_stage(M3_STAGES[2], context, repository.jobs[job_id])

    assert exc_info.value.error_code == "PUBLISHED_LOG_WRITE_FAILED"
    assert exc_info.value.details == {"log_uri": expected}
    assert repository.jobs[job_id]["status"] == "succeeded"
    assert repository.jobs[job_id]["exit_code"] == 0
    assert repository.jobs[job_id]["error_code"] is None
    assert repository.jobs[job_id]["log_uri"] is None
    override_event = next(
        event
        for event in repository.events
        if event["entity_id"] == job_id and event["status_to"] == "succeeded"
    )
    assert override_event["details"]["slurm"].get("log_uri") is None
    assert all(
        event["details"].get("slurm", {}).get("log_uri") != expected
        for event in repository.events
    )


def test_resume_array_status_override_legacy_log_write_failure_does_not_advertise_missing_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NHMS_PUBLISHED_ARTIFACT_ROOT", raising=False)
    repository = FakeCycleRepository()
    cycle_id = "gfs_2026050100"
    run_id = "cycle_gfs_2026050100"
    job_id = "job_cycle_gfs_2026050100_forcing"
    repository.jobs[job_id] = {
        "job_id": job_id,
        "run_id": run_id,
        "cycle_id": cycle_id,
        "job_type": "produce_forcing_array",
        "slurm_job_id": "3002",
        "model_id": None,
        "status": "running",
        "stage": "forcing",
        "submitted_at": _fmt(_dt("2026-05-01T00:02:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:03:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "log_uri": None,
    }
    client = FakeCycleSlurmClient(array_results_by_stage={"forcing": ["succeeded", "succeeded"]})
    client.jobs["3002"] = {
        "job_id": "3002",
        "run_id": run_id,
        "model_id": "model_0",
        "stage": "forcing",
        "status": "running",
        "submitted_at": _fmt(_dt("2026-05-01T00:02:00Z")),
        "finished_at": _fmt(_dt("2026-05-01T00:04:00Z")),
        "exit_code": 0,
        "error_code": None,
        "error_message": None,
        "payload": {"tasks": [{}, {}]},
        "stage_attempt": 0,
    }
    client.poll_counts["3002"] = 1
    object_store = LogWriteFailingObjectStore(tmp_path / "object-store", "s3://nhms")
    orchestrator = _orchestrator(tmp_path, repository, client, object_store=object_store)

    expected = "s3://nhms/runs/cycle_gfs_2026050100/logs/forcing.log"
    context = CycleOrchestrationContext(
        source_id="gfs",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        cycle_id=cycle_id,
        run_id=run_id,
        all_basins=_basins(2),
        active_basins=_basins(2),
        restart_stage="forcing",
    )
    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator._resume_cycle_stage(M3_STAGES[2], context, repository.jobs[job_id])

    assert exc_info.value.error_code == "PUBLISHED_LOG_WRITE_FAILED"
    assert exc_info.value.details == {"log_uri": expected}
    assert repository.jobs[job_id]["status"] == "succeeded"
    assert repository.jobs[job_id]["exit_code"] == 0
    assert repository.jobs[job_id]["error_code"] is None
    assert repository.jobs[job_id]["log_uri"] is None
    override_event = next(
        event
        for event in repository.events
        if event["entity_id"] == job_id and event["status_to"] == "succeeded"
    )
    assert override_event["details"]["slurm"].get("log_uri") is None
    assert all(
        event["details"].get("slurm", {}).get("log_uri") != expected
        for event in repository.events
    )
    assert not (tmp_path / "object-store" / "runs" / "cycle_gfs_2026050100" / "logs" / "forcing.log").exists()


def test_resume_array_status_override_preserves_existing_log_uri_without_fetching_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(tmp_path / "published"))
    repository = FakeCycleRepository()
    cycle_id = "gfs_2026050100"
    run_id = "cycle_gfs_2026050100"
    job_id = "job_cycle_gfs_2026050100_forcing"
    existing_log_uri = "published://logs/gfs/2026050100/existing/forcing.out"
    repository.jobs[job_id] = {
        "job_id": job_id,
        "run_id": run_id,
        "cycle_id": cycle_id,
        "job_type": "produce_forcing_array",
        "slurm_job_id": "3002",
        "model_id": None,
        "status": "running",
        "stage": "forcing",
        "submitted_at": _fmt(_dt("2026-05-01T00:02:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:03:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "log_uri": existing_log_uri,
    }
    client = FakeCycleSlurmClient(array_results_by_stage={"forcing": ["succeeded", "succeeded"]})
    client.jobs["3002"] = {
        "job_id": "3002",
        "run_id": run_id,
        "model_id": "model_0",
        "stage": "forcing",
        "status": "running",
        "submitted_at": _fmt(_dt("2026-05-01T00:02:00Z")),
        "finished_at": _fmt(_dt("2026-05-01T00:04:00Z")),
        "exit_code": 0,
        "error_code": None,
        "error_message": None,
        "payload": {"tasks": [{}, {}]},
        "stage_attempt": 0,
    }
    client.poll_counts["3002"] = 1

    def fail_fetch_logs(job_id: str) -> dict[str, Any]:
        raise AssertionError(f"existing log_uri should not be republished: {job_id}")

    client.fetch_logs = fail_fetch_logs  # type: ignore[method-assign]
    orchestrator = _orchestrator(tmp_path, repository, client)
    context = CycleOrchestrationContext(
        source_id="gfs",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        cycle_id=cycle_id,
        run_id=run_id,
        all_basins=_basins(2),
        active_basins=_basins(2),
        restart_stage="forcing",
    )

    result, aggregation = orchestrator._resume_cycle_stage(M3_STAGES[2], context, repository.jobs[job_id])

    assert result.status == "succeeded"
    assert result.log_uri == existing_log_uri
    assert aggregation is not None
    assert repository.jobs[job_id]["log_uri"] == existing_log_uri
    override_event = next(
        event
        for event in repository.events
        if event["entity_id"] == job_id and event["status_to"] == "succeeded"
    )
    assert override_event["details"]["slurm"]["log_uri"] == existing_log_uri


def test_partial_success_reindexes_downstream_and_keeps_partial_status(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(array_results_by_stage={"forcing": ["succeeded", "failed", "succeeded"]})
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(3))

    forecast_submission = client.submissions[3]
    publish_submission = client.submissions[-1]
    assert result.status == "parsed_partial"
    assert [(task["task_id"], task["original_task_id"], task["model_id"]) for task in forecast_submission["tasks"]] == [
        (0, 0, "model_0"),
        (1, 2, "model_2"),
    ]
    assert publish_submission["metadata"] == {
        "total_basins": 3,
        "published_basins": 2,
        "excluded_basins": ["basin_1"],
        "quality_states": publish_submission["metadata"]["quality_states"],
        "residual_blockers": publish_submission["metadata"]["residual_blockers"],
    }
    assert [item["model_id"] for item in publish_submission["basins"]] == ["model_0", "model_2"]
    assert [item["model_id"] for item in publish_submission["metadata"]["quality_states"]] == ["model_0", "model_2"]
    assert repository.cycle_statuses[-1] == "parsed_partial"


def test_array_partial_success_records_task_accounting_and_reduces_downstream_manifests(
    tmp_path: Path,
) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(array_results_by_stage={"forcing": ["succeeded", "failed", "succeeded"]})
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(3))

    forcing_result = next(stage for stage in result.stages if stage.stage == "forcing")
    forcing_event = next(
        event
        for event in repository.events
        if event["entity_id"] == "job_cycle_gfs_2026050100_forcing"
        and event["status_to"] == "partially_failed"
    )
    task_results = forcing_event["details"]["task_results"]
    forecast_submission = next(submission for submission in client.submissions if submission["stage"] == "forecast")
    assert result.status == "parsed_partial"
    assert forcing_result.status == "partially_failed"
    assert forcing_result.task_results == tuple(task_results)
    assert task_results[0]["array_task_id"] == 0
    assert task_results[0]["model_id"] == "model_0"
    assert task_results[0]["candidate_id"] == (
        "gfs:2026-05-01T00:00:00Z:model_0:forecast_gfs_deterministic"
    )
    assert task_results[0]["run_id"] == "run_0"
    assert task_results[0]["original_task_id"] == 0
    assert task_results[0]["slurm_job_id"].endswith("_0")
    assert task_results[0]["exit_code"] == 0
    assert task_results[0]["log_uri"].endswith("_0.out")
    assert task_results[0]["accounting"]["elapsed"] == "00:01:00"
    assert task_results[0]["resource_metrics"]["max_rss"] == "1024K"
    assert task_results[1]["status"] == "failed"
    assert task_results[1]["model_id"] == "model_1"
    assert task_results[1]["candidate_id"] == (
        "gfs:2026-05-01T00:00:00Z:model_1:forecast_gfs_deterministic"
    )
    assert task_results[1]["run_id"] == "run_1"
    assert task_results[1]["original_task_id"] == 1
    assert task_results[1]["exit_code"] == 1
    assert [task["model_id"] for task in forecast_submission["tasks"]] == ["model_0", "model_2"]
    assert repository.cycle_statuses[-1] == "parsed_partial"


def _publish_submission(client: "FakeCycleSlurmClient") -> dict[str, Any]:
    return next(submission for submission in client.submissions if submission["stage"] == "publish")


def _downstream_submissions(client: "FakeCycleSlurmClient", stages: tuple[str, ...]) -> list[dict[str, Any]]:
    return [submission for submission in client.submissions if submission["stage"] in stages]


def _array_partial_typed_failure(repository: "FakeCycleRepository", stage: str) -> dict[str, Any]:
    event = next(
        event
        for event in repository.events
        if event["entity_id"] == f"job_cycle_gfs_2026050100_{stage}"
        and event["status_to"] == "partially_failed"
    )
    return next(task for task in event["details"]["task_results"] if task["status"] == "failed")


def test_forecast_stage_partial_isolates_failed_basin_and_keeps_b_publishing(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(array_results_by_stage={"forecast": ["succeeded", "failed", "succeeded"]})
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(3))

    publish = _publish_submission(client)
    assert result.status == "parsed_partial"
    assert repository.cycle_statuses[-1] == "parsed_partial"
    # B basins (0, 2) proceed to publish; A (basin_1/model_1/run_1) is excluded everywhere downstream.
    for submission in _downstream_submissions(client, ("parse", "frequency")):
        assert [(task["task_id"], task["original_task_id"], task["model_id"]) for task in submission["tasks"]] == [
            (0, 0, "model_0"),
            (1, 2, "model_2"),
        ]
    assert publish["metadata"]["excluded_basins"] == ["basin_1"]
    assert [basin["model_id"] for basin in publish["basins"]] == ["model_0", "model_2"]
    assert publish["identity_contract"]["run_ids"] == ["run_0", "run_2"]
    # A records a typed failure and its identity is never attributed to B.
    failure = _array_partial_typed_failure(repository, "forecast")
    assert failure["model_id"] == "model_1"
    assert failure["run_id"] == "run_1"
    assert failure["status"] == "failed"
    assert failure["exit_code"] == 1
    assert failure["error_code"]
    assert "model_1" not in publish["identity_contract"]["model_ids"]
    assert "run_1" not in publish["identity_contract"]["run_ids"]
    assert "basin_v1" not in [state["basin_version_id"] for state in publish["metadata"]["quality_states"]]


def test_parse_stage_partial_isolates_failed_basin_and_keeps_b_publishing(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(array_results_by_stage={"parse": ["succeeded", "failed", "succeeded"]})
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(3))

    publish = _publish_submission(client)
    assert result.status == "parsed_partial"
    assert repository.cycle_statuses[-1] == "parsed_partial"
    # Failed basin_1 is dropped from the downstream frequency array; survivors reindex with original_task_id.
    assert [
        (task["task_id"], task["original_task_id"], task["model_id"])
        for submission in _downstream_submissions(client, ("frequency",))
        for task in submission["tasks"]
    ] == [(0, 0, "model_0"), (1, 2, "model_2")]
    assert publish["metadata"]["excluded_basins"] == ["basin_1"]
    assert [basin["model_id"] for basin in publish["basins"]] == ["model_0", "model_2"]
    failure = _array_partial_typed_failure(repository, "parse")
    assert failure["status"] == "failed"
    assert failure["model_id"] == "model_1"
    assert failure["run_id"] == "run_1"
    assert failure["error_code"]
    assert "model_1" not in publish["identity_contract"]["model_ids"]
    assert "run_1" not in publish["identity_contract"]["run_ids"]


def test_frequency_stage_partial_isolates_failed_basin_and_keeps_b_publishing(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(array_results_by_stage={"frequency": ["succeeded", "failed", "succeeded"]})
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(3))

    publish = _publish_submission(client)
    assert result.status == "parsed_partial"
    assert repository.cycle_statuses[-1] == "parsed_partial"
    assert publish["metadata"]["excluded_basins"] == ["basin_1"]
    assert [basin["model_id"] for basin in publish["basins"]] == ["model_0", "model_2"]
    assert publish["identity_contract"]["run_ids"] == ["run_0", "run_2"]
    failure = _array_partial_typed_failure(repository, "frequency")
    assert failure["status"] == "failed"
    assert failure["model_id"] == "model_1"
    assert failure["run_id"] == "run_1"
    assert failure["error_code"]
    assert "model_1" not in publish["identity_contract"]["model_ids"]
    assert "run_1" not in publish["identity_contract"]["run_ids"]


def test_publish_manifest_excludes_basin_failed_at_last_array_stage(tmp_path: Path) -> None:
    # §3B.4 case 5: the publish-MANIFEST isolation surface. A fails at the last array stage
    # before publish (frequency); the single non-array publish job must then publish only the
    # survivors. Publish is all-or-nothing per JOB, so per-basin isolation is expressed purely
    # through the manifest the publish job receives. No production change is required.
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(array_results_by_stage={"frequency": ["succeeded", "failed", "succeeded"]})
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(3))

    publish = _publish_submission(client)
    assert result.status == "parsed_partial"
    assert repository.cycle_statuses[-1] == "parsed_partial"
    # The publish job's manifest excludes the failed basin and publishes only survivors.
    assert publish["metadata"]["excluded_basins"] == ["basin_1"]
    assert publish["metadata"]["published_basins"] == 2
    assert [basin["model_id"] for basin in publish["basins"]] == ["model_0", "model_2"]
    # The failed basin's identity (model_1/run_1/basin_v1) appears in NO published state.
    published_states = publish["metadata"]["quality_states"]
    assert [state["model_id"] for state in published_states] == ["model_0", "model_2"]
    assert "run_1" not in [state["run_id"] for state in published_states]
    assert "basin_v1" not in [state["basin_version_id"] for state in published_states]
    assert "model_1" not in publish["identity_contract"]["model_ids"]
    assert "run_1" not in publish["identity_contract"]["run_ids"]


def test_two_basin_happy_path_reports_per_basin_identity_in_published_evidence(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    publish = _publish_submission(client)
    assert result.status == "complete"
    assert publish["metadata"]["published_basins"] == 2
    assert publish["metadata"]["excluded_basins"] == []
    # Per-basin run identity is individually addressable in the published evidence.
    assert publish["identity_contract"]["run_ids"] == ["run_0", "run_1"]
    assert publish["identity_contract"]["model_ids"] == ["model_0", "model_1"]
    identity_triples = [
        (state["model_id"], state["run_id"], state["basin_version_id"], state["river_network_version_id"])
        for state in publish["metadata"]["quality_states"]
    ]
    assert identity_triples == [
        ("model_0", "run_0", "basin_v0", "river_v0"),
        ("model_1", "run_1", "basin_v1", "river_v1"),
    ]
    assert [basin["model_id"] for basin in publish["basins"]] == ["model_0", "model_1"]


def test_same_named_segment_in_different_networks_keeps_distinct_production_identity(tmp_path: Path) -> None:
    # §3B.3: two basins whose river networks each contain a segment named "seg_main" but live in
    # DIFFERENT river_network_version_ids. The PRODUCTION pipeline must key the segment by the
    # composite (river_network_version_id, river_segment_id), NOT by name alone, so no segment/row
    # from network-0 is attributed to network-1 (and vice versa).
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basins = _basins(2)
    for basin in basins:
        # Same segment NAME under each basin's own distinct network (river_v0 / river_v1).
        basin["output_river"] = {
            "state": "ready",
            "river_segment_ids": ["seg_main"],
            "output_segment_count": 1,
        }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", basins)

    # --- Load-bearing proof against the published manifest (production end-to-end) ---
    publish = _publish_submission(client)
    assert result.status == "complete"
    # Each basin's per-basin published state is attributed to its OWN network; same-named segment
    # does not collapse the two basins onto one network.
    published_networks = [state["river_network_version_id"] for state in publish["metadata"]["quality_states"]]
    assert published_networks == ["river_v0", "river_v1"]
    assert len(set(published_networks)) == 2  # would be 1 if name-only keying merged them
    published_pairs = [
        (state["model_id"], state["river_network_version_id"])
        for state in publish["metadata"]["quality_states"]
    ]
    assert published_pairs == [("model_0", "river_v0"), ("model_1", "river_v1")]
    assert [run["river_network_version_id"] for run in publish["model_runs"]] == ["river_v0", "river_v1"]

    # --- Load-bearing proof against the production segment-identity assembler ---
    # build_model_run_assembly is the function _reindexed_manifest_entries runs per basin; its
    # output_river contract is where the same segment name is stamped with the basin's own network.
    assemblies = [
        build_model_run_assembly(
            {**basin, "cycle_time": "2026-05-01T00:00:00Z"},
            source_id="gfs",
            cycle_id="gfs_2026050100",
            cycle_time=_dt("2026-05-01T00:00:00Z"),
            scenario_id="forecast_gfs_deterministic",
            workspace_root=Path(orchestrator.config.workspace_root),
            object_store=orchestrator.object_store,
            default_forecast_horizon_hours=168,
        )
        for basin in basins
    ]
    composite_keys = [
        (assembly.runtime["output_river"]["river_network_version_id"], segment_id)
        for assembly in assemblies
        for segment_id in assembly.runtime["output_river"]["river_segment_ids"]
    ]
    # Same segment NAME, but the production composite keys stay distinct -> NO cross-network merge.
    # This assertion FAILS if production keying degrades to name-only (both would be ("seg_main",)).
    assert composite_keys == [("river_v0", "seg_main"), ("river_v1", "seg_main")]
    assert len(set(composite_keys)) == 2
    assert len({segment for _, segment in composite_keys}) == 1  # the segment NAME alone is shared
    # No network-0 segment is attributed to network-1's basin, and vice versa.
    assert assemblies[0].runtime["output_river"]["river_network_version_id"] == "river_v0"
    assert assemblies[1].runtime["output_river"]["river_network_version_id"] == "river_v1"


def test_array_task_result_events_redact_signed_log_uri_and_secret_error_text(tmp_path: Path) -> None:
    secret_log_uri = "s3://nhms/runs/cycle/logs/2003_0.out?X-Amz-Signature=supersecret"
    secret_error = "failed callback https://user:pass@example.test/log?token=rawsecret"
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(array_results_by_stage={"forcing": ["failed"]})
    client.task_log_uri_by_stage["forcing"] = [secret_log_uri]
    client.task_error_message_by_stage["forcing"] = [secret_error]
    orchestrator = _orchestrator(tmp_path, repository, client)

    orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    event = next(
        event
        for event in repository.events
        if event["entity_id"] == "job_cycle_gfs_2026050100_forcing"
        and event["event_type"] == "status_change"
        and event["status_to"] == "failed"
    )
    event_text = json.dumps(event["details"])
    event_record_text = json.dumps(event)
    task_result = event["details"]["task_results"][0]
    assert task_result["model_id"] == "model_0"
    assert task_result["candidate_id"] == "gfs:2026-05-01T00:00:00Z:model_0:forecast_gfs_deterministic"
    assert task_result["run_id"] == "run_0"
    assert task_result["status"] == "failed"
    assert task_result["error_code"] == "NODE_FAILURE"
    assert task_result["log_uri"] == "s3://nhms/runs/cycle/logs/2003_0.out"
    assert "supersecret" not in event_record_text
    assert "rawsecret" not in event_record_text
    assert "user:pass" not in event_record_text
    assert "X-Amz-Signature" not in event_record_text
    assert "token=" not in event_record_text
    assert "No error message provided" not in event_text


def test_pipeline_result_candidate_outcomes_redact_signed_task_log_uri(tmp_path: Path) -> None:
    secret_log_uri = "s3://nhms/runs/cycle/logs/2003_0.out?X-Amz-Signature=supersecret"
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(array_results_by_stage={"forcing": ["failed"]})
    client.task_log_uri_by_stage["forcing"] = [secret_log_uri]
    client.task_error_message_by_stage["forcing"] = [
        "failed token=rawsecret url=https://user:pass@example.test/log?signature=abc"
    ]
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    outcome = result.candidate_outcomes[0]
    result_text = json.dumps(result.candidate_outcomes)
    assert outcome["log_uri"] == "s3://nhms/runs/cycle/logs/2003_0.out"
    assert "supersecret" not in result_text
    assert "rawsecret" not in result_text
    assert "user:pass" not in result_text


def test_slurm_accounting_available_is_recorded_in_pipeline_event_details(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(
        accounting_by_stage={"download": {"elapsed": "00:02:00", "max_rss": "2048K", "alloc_tres": "cpu=2,mem=4G"}}
    )
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    accounting_event = next(
        event
        for event in repository.events
        if event["entity_id"] == "job_cycle_gfs_2026050100_download"
        and event["event_type"] == "slurm_accounting"
    )
    slurm = accounting_event["details"]["slurm"]
    assert result.status == "complete"
    assert slurm["job_id"] == "2001"
    assert slurm["state"] == "succeeded"
    assert slurm["exit_code"] == 0
    assert slurm["log_uri"].endswith("/download.log")
    assert slurm["accounting"]["elapsed"] == "00:02:00"
    assert slurm["accounting"]["max_rss"] == "2048K"
    assert slurm["resource_metrics"]["alloc_tres"] == "cpu=2,mem=4G"


def test_sync_cycle_statuses_emits_published_log_uri_when_publish_root_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(tmp_path / "published"))
    repository = FakeCycleRepository()
    cycle_id = "gfs_2026050100"
    repository.jobs["job_cycle_gfs_2026050100_forcing"] = {
        "job_id": "job_cycle_gfs_2026050100_forcing",
        "run_id": "cycle_gfs_2026050100",
        "cycle_id": cycle_id,
        "job_type": "produce_forcing_array",
        "slurm_job_id": "3001",
        "model_id": None,
        "status": "running",
        "stage": "forcing",
        "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:01:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "log_uri": None,
    }
    client = FakeCycleSlurmClient()
    client.jobs["3001"] = {
        "job_id": "3001",
        "run_id": "cycle_gfs_2026050100",
        "model_id": "model_0",
        "stage": "forcing",
        "status": "running",
        "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:01:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "payload": {},
        "stage_attempt": 0,
    }
    client.poll_counts["3001"] = 1
    orchestrator = _orchestrator(tmp_path, repository, client)

    updates = orchestrator.sync_cycle_statuses(cycle_id)

    expected = (
        "published://logs/gfs/2026050100/"
        "cycle_gfs_2026050100/"
        "job_cycle_gfs_2026050100_forcing.out"
    )
    assert updates[0]["log_uri"] == expected
    assert repository.jobs["job_cycle_gfs_2026050100_forcing"]["status"] == "succeeded"
    assert repository.jobs["job_cycle_gfs_2026050100_forcing"]["log_uri"] == expected
    published_file = (
        tmp_path
        / "published"
        / "logs"
        / "gfs"
        / "2026050100"
        / "cycle_gfs_2026050100"
        / "job_cycle_gfs_2026050100_forcing.out"
    )
    assert published_file.read_text(encoding="utf-8") == "ok"
    reader_result = ArtifactReader(
        ArtifactReaderConfig(published_root=tmp_path / "published")
    ).read_text_tail(expected)
    assert reader_result.content == "ok"
    assert reader_result.log_uri == expected
    status_event = next(event for event in repository.events if event["event_type"] == "status_change")
    assert status_event["details"]["slurm"]["log_uri"] == expected


def test_sync_cycle_statuses_publish_failure_does_not_advertise_missing_published_log_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_root_file = tmp_path / "published-as-file"
    publish_root_file.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(publish_root_file))
    repository = FakeCycleRepository()
    cycle_id = "gfs_2026050100"
    job_id = "job_cycle_gfs_2026050100_forcing"
    repository.jobs[job_id] = {
        "job_id": job_id,
        "run_id": "cycle_gfs_2026050100",
        "cycle_id": cycle_id,
        "job_type": "produce_forcing_array",
        "slurm_job_id": "3001",
        "model_id": None,
        "status": "running",
        "stage": "forcing",
        "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:01:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "log_uri": None,
    }
    client = FakeCycleSlurmClient()
    client.jobs["3001"] = {
        "job_id": "3001",
        "run_id": "cycle_gfs_2026050100",
        "model_id": "model_0",
        "stage": "forcing",
        "status": "running",
        "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:01:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "payload": {},
        "stage_attempt": 0,
    }
    client.poll_counts["3001"] = 1
    orchestrator = _orchestrator(tmp_path, repository, client)

    expected = (
        "published://logs/gfs/2026050100/"
        "cycle_gfs_2026050100/"
        "job_cycle_gfs_2026050100_forcing.out"
    )
    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.sync_cycle_statuses(cycle_id)

    assert exc_info.value.error_code == "PUBLISHED_LOG_WRITE_FAILED"
    assert exc_info.value.details == {"log_uri": expected}
    assert repository.jobs[job_id]["status"] == "succeeded"
    assert repository.jobs[job_id]["exit_code"] == 0
    assert repository.jobs[job_id]["error_code"] is None
    assert repository.jobs[job_id]["log_uri"] is None
    status_event = next(event for event in repository.events if event["event_type"] == "status_change")
    assert status_event["status_to"] == "succeeded"
    assert status_event["details"]["slurm"].get("log_uri") is None
    assert all(
        event["details"].get("slurm", {}).get("log_uri") != expected
        for event in repository.events
    )


def test_sync_cycle_statuses_legacy_log_write_failure_persists_terminal_state_without_advertising_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NHMS_PUBLISHED_ARTIFACT_ROOT", raising=False)
    repository = FakeCycleRepository()
    cycle_id = "gfs_2026050100"
    job_id = "job_cycle_gfs_2026050100_forcing"
    repository.jobs[job_id] = {
        "job_id": job_id,
        "run_id": "cycle_gfs_2026050100",
        "cycle_id": cycle_id,
        "job_type": "produce_forcing_array",
        "slurm_job_id": "3001",
        "model_id": None,
        "status": "running",
        "stage": "forcing",
        "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:01:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "log_uri": None,
    }
    client = FakeCycleSlurmClient()
    client.jobs["3001"] = {
        "job_id": "3001",
        "run_id": "cycle_gfs_2026050100",
        "model_id": "model_0",
        "stage": "forcing",
        "status": "running",
        "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:01:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "payload": {},
        "stage_attempt": 0,
    }
    client.poll_counts["3001"] = 1
    object_store = LogWriteFailingObjectStore(tmp_path / "object-store", "s3://nhms")
    orchestrator = _orchestrator(tmp_path, repository, client, object_store=object_store)

    expected = "s3://nhms/runs/cycle_gfs_2026050100/logs/forcing.log"
    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.sync_cycle_statuses(cycle_id)

    assert exc_info.value.error_code == "PUBLISHED_LOG_WRITE_FAILED"
    assert exc_info.value.details == {"log_uri": expected}
    assert repository.jobs[job_id]["status"] == "succeeded"
    assert repository.jobs[job_id]["exit_code"] == 0
    assert repository.jobs[job_id]["error_code"] is None
    assert repository.jobs[job_id]["log_uri"] is None
    status_event = next(event for event in repository.events if event["event_type"] == "status_change")
    assert status_event["status_to"] == "succeeded"
    assert status_event["details"]["slurm"].get("log_uri") is None
    assert all(
        event["details"].get("slurm", {}).get("log_uri") != expected
        for event in repository.events
    )
    assert not (tmp_path / "object-store" / "runs" / "cycle_gfs_2026050100" / "logs" / "forcing.log").exists()


def test_sync_cycle_statuses_log_write_failure_does_not_block_terminal_siblings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NHMS_PUBLISHED_ARTIFACT_ROOT", raising=False)
    repository = FakeCycleRepository()
    cycle_id = "gfs_2026050100"
    first_job_id = "job_cycle_gfs_2026050100_forcing"
    second_job_id = "job_cycle_gfs_2026050100_run"
    base_job = {
        "run_id": "cycle_gfs_2026050100",
        "cycle_id": cycle_id,
        "model_id": None,
        "status": "running",
        "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:01:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "log_uri": None,
    }
    repository.jobs[first_job_id] = {
        **base_job,
        "job_id": first_job_id,
        "job_type": "produce_forcing_array",
        "slurm_job_id": "3001",
        "stage": "forcing",
    }
    repository.jobs[second_job_id] = {
        **base_job,
        "job_id": second_job_id,
        "job_type": "run_forecast_array",
        "slurm_job_id": "3002",
        "stage": "forecast",
        "submitted_at": _fmt(_dt("2026-05-01T00:05:00Z")),
    }
    client = FakeCycleSlurmClient()
    for slurm_job_id, stage in [("3001", "forcing"), ("3002", "forecast")]:
        client.jobs[slurm_job_id] = {
            "job_id": slurm_job_id,
            "run_id": "cycle_gfs_2026050100",
            "model_id": "model_0",
            "stage": stage,
            "status": "running",
            "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
            "started_at": _fmt(_dt("2026-05-01T00:01:00Z")),
            "finished_at": None,
            "exit_code": None,
            "error_code": None,
            "error_message": None,
            "payload": {},
            "stage_attempt": 0,
        }
        client.poll_counts[slurm_job_id] = 1
    object_store = FirstLogWriteFailingObjectStore(tmp_path / "object-store", "s3://nhms")
    orchestrator = _orchestrator(tmp_path, repository, client, object_store=object_store)

    failed_uri = "s3://nhms/runs/cycle_gfs_2026050100/logs/forcing.log"
    successful_uri = "s3://nhms/runs/cycle_gfs_2026050100/logs/forecast.log"
    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.sync_cycle_statuses(cycle_id)

    assert exc_info.value.error_code == "PUBLISHED_LOG_WRITE_FAILED"
    assert exc_info.value.details == {"log_uri": failed_uri}
    assert repository.jobs[first_job_id]["status"] == "succeeded"
    assert repository.jobs[first_job_id]["exit_code"] == 0
    assert repository.jobs[first_job_id]["error_code"] is None
    assert repository.jobs[first_job_id]["log_uri"] is None
    assert repository.jobs[second_job_id]["status"] == "succeeded"
    assert repository.jobs[second_job_id]["exit_code"] == 0
    assert repository.jobs[second_job_id]["error_code"] is None
    assert repository.jobs[second_job_id]["log_uri"] == successful_uri
    events_by_job = {event["entity_id"]: event for event in repository.events if event["event_type"] == "status_change"}
    assert events_by_job[first_job_id]["status_to"] == "succeeded"
    assert events_by_job[first_job_id]["details"]["slurm"].get("log_uri") is None
    assert events_by_job[second_job_id]["status_to"] == "succeeded"
    assert events_by_job[second_job_id]["details"]["slurm"]["log_uri"] == successful_uri
    assert all(
        event["details"].get("slurm", {}).get("log_uri") != failed_uri
        for event in repository.events
    )
    assert not (tmp_path / "object-store" / "runs" / "cycle_gfs_2026050100" / "logs" / "forcing.log").exists()
    assert (tmp_path / "object-store" / "runs" / "cycle_gfs_2026050100" / "logs" / "forecast.log").read_text(
        encoding="utf-8"
    ) == "ok"


def test_sync_cycle_statuses_preserves_existing_log_uri_without_fetching_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(tmp_path / "published"))
    repository = FakeCycleRepository()
    cycle_id = "gfs_2026050100"
    job_id = "job_cycle_gfs_2026050100_forcing"
    existing_log_uri = "published://logs/gfs/2026050100/existing/job.out"
    repository.jobs[job_id] = {
        "job_id": job_id,
        "run_id": "cycle_gfs_2026050100",
        "cycle_id": cycle_id,
        "job_type": "produce_forcing_array",
        "slurm_job_id": "3001",
        "model_id": None,
        "status": "running",
        "stage": "forcing",
        "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:01:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "log_uri": existing_log_uri,
    }
    client = FakeCycleSlurmClient()
    client.jobs["3001"] = {
        "job_id": "3001",
        "run_id": "cycle_gfs_2026050100",
        "model_id": "model_0",
        "stage": "forcing",
        "status": "running",
        "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:01:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "payload": {},
        "stage_attempt": 0,
    }
    client.poll_counts["3001"] = 1

    def fail_fetch_logs(job_id: str) -> dict[str, Any]:
        raise AssertionError(f"existing log_uri should not be republished: {job_id}")

    client.fetch_logs = fail_fetch_logs  # type: ignore[method-assign]
    orchestrator = _orchestrator(tmp_path, repository, client)

    updates = orchestrator.sync_cycle_statuses(cycle_id)

    assert updates[0]["status"] == "succeeded"
    assert updates[0]["log_uri"] == existing_log_uri
    assert repository.jobs[job_id]["log_uri"] == existing_log_uri
    status_event = next(event for event in repository.events if event["event_type"] == "status_change")
    assert status_event["details"]["slurm"]["log_uri"] == existing_log_uri


def test_direct_cycle_publish_failure_does_not_advertise_missing_published_log_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_root_file = tmp_path / "published-as-file"
    publish_root_file.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(publish_root_file))
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    object_store = LocalObjectStore(tmp_path / "object-store", "s3://nhms")
    orchestrator = _orchestrator(tmp_path, repository, client, object_store=object_store)

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    expected = (
        "published://logs/gfs/2026050100/"
        "cycle_gfs_2026050100/"
        "job_cycle_gfs_2026050100_download.out"
    )
    job = repository.jobs["job_cycle_gfs_2026050100_download"]
    assert exc_info.value.error_code == "PUBLISHED_LOG_WRITE_FAILED"
    assert exc_info.value.details == {"log_uri": expected}
    assert job["status"] == "succeeded"
    assert job["exit_code"] == 0
    assert job["error_code"] is None
    assert job["log_uri"] is None
    terminal_event = next(
        event
        for event in repository.events
        if event["entity_id"] == "job_cycle_gfs_2026050100_download"
        and event["status_to"] == "succeeded"
    )
    assert terminal_event["details"]["slurm"].get("log_uri") is None
    assert all(
        event["details"].get("slurm", {}).get("log_uri") != expected
        for event in repository.events
    )
    assert not (tmp_path / "object-store" / "runs").exists()
    exception_text = str(exc_info.value)
    assert "published-as-file" not in exception_text
    assert "workspace" not in exception_text


def test_direct_cycle_legacy_log_write_failure_persists_terminal_state_before_raise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NHMS_PUBLISHED_ARTIFACT_ROOT", raising=False)
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    object_store = LogWriteFailingObjectStore(tmp_path / "object-store", "s3://nhms")
    orchestrator = _orchestrator(tmp_path, repository, client, object_store=object_store)

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    expected = "s3://nhms/runs/cycle_gfs_2026050100/logs/download.log"
    job = repository.jobs["job_cycle_gfs_2026050100_download"]
    assert exc_info.value.error_code == "PUBLISHED_LOG_WRITE_FAILED"
    assert exc_info.value.details == {"log_uri": expected}
    assert job["status"] == "succeeded"
    assert job["exit_code"] == 0
    assert job["error_code"] is None
    assert job["log_uri"] is None
    assert repository.cycle_statuses[-1] == "raw_complete"
    terminal_event = next(
        event
        for event in repository.events
        if event["entity_id"] == "job_cycle_gfs_2026050100_download"
        and event["status_to"] == "succeeded"
    )
    assert terminal_event["details"]["slurm"].get("log_uri") is None
    assert all(
        event["details"].get("slurm", {}).get("log_uri") != expected
        for event in repository.events
    )
    assert not (tmp_path / "object-store" / "runs" / "cycle_gfs_2026050100" / "logs" / "download.log").exists()


def test_direct_non_cycle_publish_failure_does_not_advertise_missing_published_log_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_root_file = tmp_path / "published-as-file"
    publish_root_file.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(publish_root_file))
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    object_store = LocalObjectStore(tmp_path / "object-store", "s3://nhms")
    orchestrator = _orchestrator(tmp_path, repository, client, object_store=object_store)
    context = orchestrator._build_run_context(
        "gfs",
        _dt("2026-05-01T00:00:00Z"),
        ModelContext(
            model_id="model_0",
            basin_id="basin_0",
            basin_version_id="basin_v0",
            river_network_version_id="river_v0",
            segment_count=1,
            model_package_uri="s3://nhms/models/model_0/v1/package/",
        ),
        ForcingContext("forc_gfs_2026050100_model_0", "s3://nhms/forcing/gfs/2026050100/model_0/"),
    )
    repository.hydro_runs[context.run_id] = {"run_id": context.run_id, "status": "staged"}

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator._submit_and_wait(M3_STAGES[0], context, first_stage=True)

    expected = (
        "published://logs/gfs/2026050100/"
        "fcst_gfs_2026050100_model_0/"
        "job_fcst_gfs_2026050100_model_0_download.out"
    )
    job = repository.jobs["job_fcst_gfs_2026050100_model_0_download"]
    assert exc_info.value.error_code == "PUBLISHED_LOG_WRITE_FAILED"
    assert exc_info.value.details == {"log_uri": expected}
    assert job["status"] == "succeeded"
    assert job["exit_code"] == 0
    assert job["error_code"] is None
    assert job["log_uri"] is None
    terminal_event = next(
        event
        for event in repository.events
        if event["entity_id"] == "job_fcst_gfs_2026050100_model_0_download"
        and event["status_to"] == "succeeded"
    )
    assert terminal_event["details"]["slurm"].get("log_uri") is None
    assert all(
        event["details"].get("slurm", {}).get("log_uri") != expected
        for event in repository.events
    )
    assert not (tmp_path / "object-store" / "runs").exists()


def test_direct_non_cycle_legacy_log_write_failure_persists_adjacent_state_before_raise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NHMS_PUBLISHED_ARTIFACT_ROOT", raising=False)
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    object_store = LogWriteFailingObjectStore(tmp_path / "object-store", "s3://nhms")
    orchestrator = _orchestrator(tmp_path, repository, client, object_store=object_store)
    context = orchestrator._build_run_context(
        "gfs",
        _dt("2026-05-01T00:00:00Z"),
        ModelContext(
            model_id="model_0",
            basin_id="basin_0",
            basin_version_id="basin_v0",
            river_network_version_id="river_v0",
            segment_count=1,
            model_package_uri="s3://nhms/models/model_0/v1/package/",
        ),
        ForcingContext("forc_gfs_2026050100_model_0", "s3://nhms/forcing/gfs/2026050100/model_0/"),
    )
    repository.hydro_runs[context.run_id] = {"run_id": context.run_id, "status": "staged"}

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator._submit_and_wait(M3_STAGES[0], context, first_stage=True)

    expected = "s3://nhms/runs/fcst_gfs_2026050100_model_0/logs/download.log"
    job = repository.jobs["job_fcst_gfs_2026050100_model_0_download"]
    assert exc_info.value.error_code == "PUBLISHED_LOG_WRITE_FAILED"
    assert exc_info.value.details == {"log_uri": expected}
    assert job["status"] == "succeeded"
    assert job["exit_code"] == 0
    assert job["error_code"] is None
    assert job["log_uri"] is None
    assert repository.cycle_statuses[-1] == "raw_complete"
    assert repository.hydro_runs[context.run_id]["status"] == "submitted"
    assert repository.hydro_runs[context.run_id]["slurm_job_id"] == "2001"
    terminal_event = next(
        event
        for event in repository.events
        if event["entity_id"] == "job_fcst_gfs_2026050100_model_0_download"
        and event["status_to"] == "succeeded"
    )
    assert terminal_event["details"]["slurm"].get("log_uri") is None
    assert all(
        event["details"].get("slurm", {}).get("log_uri") != expected
        for event in repository.events
    )
    assert not (tmp_path / "object-store" / "runs" / "fcst_gfs_2026050100_model_0" / "logs" / "download.log").exists()


def test_direct_non_cycle_polled_success_event_includes_readable_published_log_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published_root = tmp_path / "published"
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(published_root))
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    context = orchestrator._build_run_context(
        "gfs",
        _dt("2026-05-01T00:00:00Z"),
        ModelContext(
            model_id="model_0",
            basin_id="basin_0",
            basin_version_id="basin_v0",
            river_network_version_id="river_v0",
            segment_count=1,
            model_package_uri="s3://nhms/models/model_0/v1/package/",
        ),
        ForcingContext("forc_gfs_2026050100_model_0", "s3://nhms/forcing/gfs/2026050100/model_0/"),
    )
    repository.hydro_runs[context.run_id] = {"run_id": context.run_id, "status": "staged"}

    result = orchestrator._submit_and_wait(M3_STAGES[0], context, first_stage=True)

    expected = (
        "published://logs/gfs/2026050100/"
        "fcst_gfs_2026050100_model_0/"
        "job_fcst_gfs_2026050100_model_0_download.out"
    )
    job = repository.jobs["job_fcst_gfs_2026050100_model_0_download"]
    terminal_event = next(
        event
        for event in repository.events
        if event["entity_id"] == "job_fcst_gfs_2026050100_model_0_download"
        and event["status_to"] == "succeeded"
    )
    reader_result = ArtifactReader(
        ArtifactReaderConfig(published_root=published_root)
    ).read_text_tail(expected)
    assert job["log_uri"] == expected
    assert result.log_uri == expected
    assert terminal_event["details"]["slurm"]["log_uri"] == expected
    assert reader_result.log_uri == expected
    assert reader_result.content == "ok"


def test_direct_cycle_immediate_terminal_publish_failure_persists_terminal_state_before_raise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_root_file = tmp_path / "published-as-file"
    publish_root_file.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(publish_root_file))
    repository = FakeCycleRepository()
    client = ImmediateTerminalSlurmClient()
    object_store = LocalObjectStore(tmp_path / "object-store", "s3://nhms")
    orchestrator = _orchestrator(tmp_path, repository, client, object_store=object_store)

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    expected = (
        "published://logs/gfs/2026050100/"
        "cycle_gfs_2026050100/"
        "job_cycle_gfs_2026050100_download.out"
    )
    job = repository.jobs["job_cycle_gfs_2026050100_download"]
    assert exc_info.value.error_code == "PUBLISHED_LOG_WRITE_FAILED"
    assert exc_info.value.details == {"log_uri": expected}
    assert job["status"] == "succeeded"
    assert job["exit_code"] == 0
    assert job["error_code"] is None
    assert job["log_uri"] is None
    assert repository.cycle_statuses[-1] == "raw_complete"
    submission_event = next(
        event
        for event in repository.events
        if event["entity_id"] == "job_cycle_gfs_2026050100_download"
        and event["event_type"] == "submission"
    )
    assert submission_event["status_to"] == "succeeded"
    assert submission_event["details"]["slurm"].get("log_uri") is None
    assert all(
        event["details"].get("slurm", {}).get("log_uri") != expected
        for event in repository.events
    )
    assert client.fetch_log_calls == ["2001"]
    assert client.poll_counts["2001"] == 0
    assert not (tmp_path / "object-store" / "runs").exists()


def test_direct_cycle_immediate_terminal_symlink_publish_root_is_not_followed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_target = tmp_path / "workspace" / ".nhms-runs" / "published-target"
    private_target.mkdir(parents=True)
    published_link = tmp_path / "published-link"
    published_link.symlink_to(private_target, target_is_directory=True)
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(published_link))
    repository = FakeCycleRepository()
    client = ImmediateTerminalSlurmClient()
    object_store = LocalObjectStore(tmp_path / "object-store", "s3://nhms")
    orchestrator = _orchestrator(tmp_path, repository, client, object_store=object_store)

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    expected = (
        "published://logs/gfs/2026050100/"
        "cycle_gfs_2026050100/"
        "job_cycle_gfs_2026050100_download.out"
    )
    job = repository.jobs["job_cycle_gfs_2026050100_download"]
    assert exc_info.value.error_code == "PUBLISHED_LOG_WRITE_FAILED"
    assert exc_info.value.details == {"log_uri": expected}
    assert job["status"] == "succeeded"
    assert job["exit_code"] == 0
    assert job["error_code"] is None
    assert job["log_uri"] is None
    assert repository.cycle_statuses[-1] == "raw_complete"
    assert all(event["details"].get("slurm", {}).get("log_uri") != expected for event in repository.events)
    assert expected not in str({"jobs": repository.jobs, "events": repository.events})
    assert client.fetch_log_calls == ["2001"]
    assert client.poll_counts["2001"] == 0
    assert not (tmp_path / "object-store" / "runs").exists()
    assert not (private_target / "logs").exists()


def test_direct_cycle_immediate_terminal_legacy_log_write_failure_persists_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NHMS_PUBLISHED_ARTIFACT_ROOT", raising=False)
    repository = FakeCycleRepository()
    client = ImmediateTerminalSlurmClient()
    object_store = LogWriteFailingObjectStore(tmp_path / "object-store", "s3://nhms")
    orchestrator = _orchestrator(tmp_path, repository, client, object_store=object_store)

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    expected = "s3://nhms/runs/cycle_gfs_2026050100/logs/download.log"
    job = repository.jobs["job_cycle_gfs_2026050100_download"]
    assert exc_info.value.error_code == "PUBLISHED_LOG_WRITE_FAILED"
    assert exc_info.value.details == {"log_uri": expected}
    assert job["status"] == "succeeded"
    assert job["exit_code"] == 0
    assert job["error_code"] is None
    assert job["log_uri"] is None
    assert repository.cycle_statuses[-1] == "raw_complete"
    submission_event = next(
        event
        for event in repository.events
        if event["entity_id"] == "job_cycle_gfs_2026050100_download"
        and event["event_type"] == "submission"
    )
    assert submission_event["status_to"] == "succeeded"
    assert submission_event["details"]["slurm"].get("log_uri") is None
    assert all(
        event["details"].get("slurm", {}).get("log_uri") != expected
        for event in repository.events
    )
    assert client.fetch_log_calls == ["2001"]
    assert client.poll_counts["2001"] == 0
    assert not (tmp_path / "object-store" / "runs" / "cycle_gfs_2026050100" / "logs" / "download.log").exists()


def test_direct_non_cycle_immediate_terminal_publish_failure_persists_adjacent_state_before_raise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_root_file = tmp_path / "published-as-file"
    publish_root_file.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(publish_root_file))
    repository = FakeCycleRepository()
    client = ImmediateTerminalSlurmClient()
    object_store = LocalObjectStore(tmp_path / "object-store", "s3://nhms")
    orchestrator = _orchestrator(tmp_path, repository, client, object_store=object_store)
    context = orchestrator._build_run_context(
        "gfs",
        _dt("2026-05-01T00:00:00Z"),
        ModelContext(
            model_id="model_0",
            basin_id="basin_0",
            basin_version_id="basin_v0",
            river_network_version_id="river_v0",
            segment_count=1,
            model_package_uri="s3://nhms/models/model_0/v1/package/",
        ),
        ForcingContext("forc_gfs_2026050100_model_0", "s3://nhms/forcing/gfs/2026050100/model_0/"),
    )
    repository.hydro_runs[context.run_id] = {"run_id": context.run_id, "status": "staged"}

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator._submit_and_wait(M3_STAGES[0], context, first_stage=True)

    expected = (
        "published://logs/gfs/2026050100/"
        "fcst_gfs_2026050100_model_0/"
        "job_fcst_gfs_2026050100_model_0_download.out"
    )
    job = repository.jobs["job_fcst_gfs_2026050100_model_0_download"]
    assert exc_info.value.error_code == "PUBLISHED_LOG_WRITE_FAILED"
    assert exc_info.value.details == {"log_uri": expected}
    assert job["status"] == "succeeded"
    assert job["exit_code"] == 0
    assert job["error_code"] is None
    assert job["log_uri"] is None
    assert repository.cycle_statuses[-1] == "raw_complete"
    assert repository.hydro_runs[context.run_id]["status"] == "submitted"
    assert repository.hydro_runs[context.run_id]["slurm_job_id"] == "2001"
    status_event = next(
        event
        for event in repository.events
        if event["entity_id"] == "job_fcst_gfs_2026050100_model_0_download"
        and event["event_type"] == "status_change"
    )
    assert status_event["status_to"] == "succeeded"
    assert status_event["details"]["slurm"].get("log_uri") is None
    assert all(
        event["details"].get("slurm", {}).get("log_uri") != expected
        for event in repository.events
    )
    assert client.fetch_log_calls == ["2001"]
    assert client.poll_counts["2001"] == 0
    assert not (tmp_path / "object-store" / "runs").exists()


def test_direct_non_cycle_immediate_terminal_legacy_log_write_failure_persists_adjacent_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NHMS_PUBLISHED_ARTIFACT_ROOT", raising=False)
    repository = FakeCycleRepository()
    client = ImmediateTerminalSlurmClient()
    object_store = LogWriteFailingObjectStore(tmp_path / "object-store", "s3://nhms")
    orchestrator = _orchestrator(tmp_path, repository, client, object_store=object_store)
    context = orchestrator._build_run_context(
        "gfs",
        _dt("2026-05-01T00:00:00Z"),
        ModelContext(
            model_id="model_0",
            basin_id="basin_0",
            basin_version_id="basin_v0",
            river_network_version_id="river_v0",
            segment_count=1,
            model_package_uri="s3://nhms/models/model_0/v1/package/",
        ),
        ForcingContext("forc_gfs_2026050100_model_0", "s3://nhms/forcing/gfs/2026050100/model_0/"),
    )
    repository.hydro_runs[context.run_id] = {"run_id": context.run_id, "status": "staged"}

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator._submit_and_wait(M3_STAGES[0], context, first_stage=True)

    expected = "s3://nhms/runs/fcst_gfs_2026050100_model_0/logs/download.log"
    job = repository.jobs["job_fcst_gfs_2026050100_model_0_download"]
    assert exc_info.value.error_code == "PUBLISHED_LOG_WRITE_FAILED"
    assert exc_info.value.details == {"log_uri": expected}
    assert job["status"] == "succeeded"
    assert job["exit_code"] == 0
    assert job["error_code"] is None
    assert job["log_uri"] is None
    assert repository.cycle_statuses[-1] == "raw_complete"
    assert repository.hydro_runs[context.run_id]["status"] == "submitted"
    assert repository.hydro_runs[context.run_id]["slurm_job_id"] == "2001"
    status_event = next(
        event
        for event in repository.events
        if event["entity_id"] == "job_fcst_gfs_2026050100_model_0_download"
        and event["event_type"] == "status_change"
    )
    assert status_event["status_to"] == "succeeded"
    assert status_event["details"]["slurm"].get("log_uri") is None
    assert all(
        event["details"].get("slurm", {}).get("log_uri") != expected
        for event in repository.events
    )
    assert client.fetch_log_calls == ["2001"]
    assert client.poll_counts["2001"] == 0
    assert not (tmp_path / "object-store" / "runs" / "fcst_gfs_2026050100_model_0" / "logs" / "download.log").exists()


def test_direct_submit_success_with_immediate_terminal_publish_root_advertises_readable_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published_root = tmp_path / "published"
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(published_root))
    monkeypatch.setattr("services.orchestrator.chain.TilePublisher", _successful_control_node_publisher())
    repository = FakeCycleRepository()
    client = ImmediateTerminalSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    expected = (
        "published://logs/gfs/2026050100/"
        "cycle_gfs_2026050100/"
        "job_cycle_gfs_2026050100_download.out"
    )
    job = repository.jobs["job_cycle_gfs_2026050100_download"]
    submission_event = next(
        event
        for event in repository.events
        if event["entity_id"] == "job_cycle_gfs_2026050100_download"
        and event["event_type"] == "submission"
    )
    assert result.status == "complete"
    assert job["status"] == "succeeded"
    assert job["log_uri"] == expected
    assert result.stages[0].log_uri == expected
    assert submission_event["details"]["slurm"]["log_uri"] == expected
    assert (
        published_root
        / "logs"
        / "gfs"
        / "2026050100"
        / "cycle_gfs_2026050100"
        / "job_cycle_gfs_2026050100_download.out"
    ).read_text(encoding="utf-8") == "ok"


def test_malformed_array_accounting_records_gap_without_fabricating_metrics(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient(malformed_array_accounting_stages={"forcing"})
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    gap_event = next(
        event
        for event in repository.events
        if event["entity_id"] == "job_cycle_gfs_2026050100_forcing"
        and event["event_type"] == "slurm_accounting_gap"
    )
    forcing_event = next(
        event
        for event in repository.events
        if event["entity_id"] == "job_cycle_gfs_2026050100_forcing"
        and event["status_to"] == "failed"
    )
    assert result.status == "failed"
    assert [submission["stage"] for submission in client.submissions] == ["download", "convert", "forcing"]
    assert gap_event["details"]["fabricated_metrics"] is False
    assert gap_event["details"]["gap"]["error"]
    assert forcing_event["details"]["task_results"] == ()


def test_cancel_active_cycle_jobs_calls_gateway_and_records_no_replacement(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    cycle_id = "gfs_2026050100"
    repository.jobs["job_cycle_gfs_2026050100_forcing"] = {
        "job_id": "job_cycle_gfs_2026050100_forcing",
        "run_id": "cycle_gfs_2026050100",
        "cycle_id": cycle_id,
        "job_type": "produce_forcing_array",
        "slurm_job_id": "3001",
        "model_id": "model_0",
        "status": "running",
        "stage": "forcing",
        "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:01:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "log_uri": "s3://nhms/runs/cycle_gfs_2026050100/logs/forcing.log",
    }
    client = FakeCycleSlurmClient()
    client.jobs["3001"] = {
        "job_id": "3001",
        "run_id": "cycle_gfs_2026050100",
        "model_id": "model_0",
        "stage": "forcing",
        "status": "running",
        "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:01:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "payload": {},
        "stage_attempt": 0,
    }
    orchestrator = _orchestrator(tmp_path, repository, client)

    cancelled = orchestrator.cancel_active_cycle_jobs(cycle_id, reason="scheduler_cancel_requested")

    cancel_event = repository.events[-1]
    assert client.cancelled_jobs == ["3001"]
    assert cancelled[0]["status"] == "cancelled"
    assert cancel_event["event_type"] == "cancel"
    assert cancel_event["details"]["replacement_submitted"] is False
    assert cancel_event["details"]["slurm"]["job_id"] == "3001"
    assert cancel_event["details"]["reason"] == "scheduler_cancel_requested"
    assert client.submissions == []


def test_cancel_active_cycle_jobs_redacts_credential_bearing_log_uri(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    cycle_id = "gfs_2026050100"
    secret_log_uri = "https://user:pass@example.test/logs/forcing.log?token=supersecret"
    repository.jobs["job_cycle_gfs_2026050100_forcing"] = {
        "job_id": "job_cycle_gfs_2026050100_forcing",
        "run_id": "cycle_gfs_2026050100",
        "cycle_id": cycle_id,
        "job_type": "produce_forcing_array",
        "slurm_job_id": "3001",
        "model_id": "model_0",
        "status": "running",
        "stage": "forcing",
        "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:01:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "log_uri": secret_log_uri,
    }
    client = FakeCycleSlurmClient()
    client.jobs["3001"] = {
        "job_id": "3001",
        "run_id": "cycle_gfs_2026050100",
        "model_id": "model_0",
        "stage": "forcing",
        "status": "running",
        "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:01:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "payload": {},
        "stage_attempt": 0,
    }
    orchestrator = _orchestrator(tmp_path, repository, client)

    orchestrator.cancel_active_cycle_jobs(cycle_id, reason="scheduler_cancel_requested")

    cancel_event = repository.events[-1]
    event_text = json.dumps(cancel_event["details"])
    assert cancel_event["details"]["slurm"]["log_uri"] == "https://example.test/logs/forcing.log"
    assert "supersecret" not in event_text
    assert "user:pass" not in event_text
    assert "token=" not in event_text


def test_cancel_active_cycle_jobs_conflict_records_gap_without_rewriting_cancelled(tmp_path: Path) -> None:
    class ConflictCancelClient(FakeCycleSlurmClient):
        def cancel_job(self, job_id: str) -> dict[str, Any]:
            from services.orchestrator.chain import SlurmClientError

            raise SlurmClientError(
                "JOB_ALREADY_TERMINAL",
                "Slurm Gateway returned HTTP 409.",
                {
                    "response": {
                        "error": {
                            "code": "JOB_ALREADY_TERMINAL",
                            "details": {"job_id": job_id, "status": "succeeded"},
                        }
                    }
                },
            )

    repository = FakeCycleRepository()
    cycle_id = "gfs_2026050100"
    repository.jobs["job_cycle_gfs_2026050100_forcing"] = {
        "job_id": "job_cycle_gfs_2026050100_forcing",
        "run_id": "cycle_gfs_2026050100",
        "cycle_id": cycle_id,
        "job_type": "produce_forcing_array",
        "slurm_job_id": "3001",
        "model_id": "model_0",
        "status": "running",
        "stage": "forcing",
        "submitted_at": _fmt(_dt("2026-05-01T00:00:00Z")),
        "started_at": _fmt(_dt("2026-05-01T00:01:00Z")),
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "log_uri": "s3://nhms/runs/cycle_gfs_2026050100/logs/forcing.log",
    }
    client = ConflictCancelClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    cancelled = orchestrator.cancel_active_cycle_jobs(cycle_id, reason="scheduler_cancel_requested")

    assert repository.jobs["job_cycle_gfs_2026050100_forcing"]["status"] == "running"
    assert cancelled[0]["status"] == "running"
    assert cancelled[0]["error_code"] == "JOB_ALREADY_TERMINAL"
    gap_event = repository.events[-1]
    assert gap_event["event_type"] == "slurm_cancellation_gap"
    assert gap_event["status_to"] == "blocked"
    assert gap_event["details"]["replacement_submitted"] is False
    assert gap_event["details"]["slurm"]["cancellation_proven"] is False


def test_accounting_gap_event_redacts_secret_gateway_error_text(tmp_path: Path) -> None:
    class SecretAccountingClient(FakeCycleSlurmClient):
        def get_array_task_results(self, job_id: str) -> list[dict[str, Any]]:
            raise ValueError(
                "gateway failed https://user:pass@example.test/accounting?token=rawsecret password=hunter2"
            )

    repository = FakeCycleRepository()
    client = SecretAccountingClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    gap_event = next(
        event
        for event in repository.events
        if event["entity_id"] == "job_cycle_gfs_2026050100_forcing"
        and event["event_type"] == "slurm_accounting_gap"
    )
    event_text = json.dumps(gap_event["details"])
    assert gap_event["details"]["gap"]["error_code"] is None
    assert "rawsecret" not in event_text
    assert "hunter2" not in event_text
    assert "user:pass" not in event_text
    assert "token=" not in event_text
    assert "password=hunter2" not in event_text
    assert "password=[redacted]" in event_text


def test_psycopg_active_slurm_jobs_includes_cycle_run_array_job_for_filtered_model() -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []

    class CapturingRepository(PsycopgOrchestratorRepository):
        def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
            calls.append((statement, parameters))
            return [
                {
                    "job_id": "job_cycle_gfs_2026050100_forecast",
                    "run_id": "cycle_gfs_2026050100",
                    "cycle_id": "gfs_2026050100",
                    "job_type": "run_shud_forecast_array",
                    "slurm_job_id": "3001",
                    "model_id": "model_a",
                    "status": "running",
                    "stage": "forecast",
                }
            ]

    repository = CapturingRepository("postgresql://example")

    jobs = repository.active_slurm_jobs(
        source_id="gfs",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        model_id="model_b",
    )

    statement, parameters = calls[0]
    assert "OR pj.run_id = %s" in statement
    assert parameters == (
        "gfs_2026050100",
        "model_b",
        "model_b",
        "fcst_gfs_2026050100_model_b",
        "cycle_gfs_2026050100",
        "cycle_gfs_2026050100",
        100,
    )
    assert jobs[0]["run_id"] == "cycle_gfs_2026050100"
    assert jobs[0]["model_id"] == "model_a"


def test_psycopg_has_active_pipeline_includes_queued_pipeline_rows() -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []

    class CapturingRepository(PsycopgOrchestratorRepository):
        def _fetch_optional(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
            calls.append((statement, parameters))
            return {"active": 1}

    repository = CapturingRepository("postgresql://example")

    assert (
        repository.has_active_pipeline(
            source_id="gfs",
            cycle_time=_dt("2026-05-01T00:00:00Z"),
            model_id="model_a",
        )
        is True
    )

    statement, parameters = calls[0]
    terminal_statuses = _pipeline_status_not_in_clause(statement)
    assert "queued" not in terminal_statuses
    assert "submitted" not in terminal_statuses
    assert "running" not in terminal_statuses
    assert parameters[:3] == (
        "gfs",
        _dt("2026-05-01T00:00:00Z"),
        "model_a",
    )
    assert set(parameters[3]) == {"created", "staged", "submitted", "running"}
    assert parameters[4:] == (
        "gfs_2026050100",
        "fcst_gfs_2026050100_model_a",
        "cycle_gfs_2026050100",
        "model_a",
        "cycle_gfs_2026050100",
    )


def test_psycopg_active_slurm_jobs_includes_queued_pipeline_rows() -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []

    class CapturingRepository(PsycopgOrchestratorRepository):
        def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
            calls.append((statement, parameters))
            return [
                {
                    "job_id": "job_cycle_gfs_2026050100_forcing",
                    "run_id": "cycle_gfs_2026050100",
                    "cycle_id": "gfs_2026050100",
                    "job_type": "run_shud_forecast_array",
                    "slurm_job_id": "3001",
                    "model_id": "model_a",
                    "status": "queued",
                    "stage": "forcing",
                }
            ]

    repository = CapturingRepository("postgresql://example")

    jobs = repository.active_slurm_jobs(
        source_id="gfs",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        model_id="model_a",
    )

    statement, _parameters = calls[0]
    terminal_statuses = _pipeline_status_not_in_clause(statement)
    assert "queued" not in terminal_statuses
    assert "submitted" not in terminal_statuses
    assert "running" not in terminal_statuses
    assert jobs[0]["status"] == "queued"
    assert jobs[0]["slurm_job_id"] == "3001"


def test_psycopg_candidate_state_limits_jobs_and_reads_events_for_candidate_scope() -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []

    class CapturingRepository(PsycopgOrchestratorRepository):
        def _fetch_optional(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
            calls.append((statement, parameters))
            if "FROM hydro.hydro_run" in statement:
                return {
                    "run_id": "fcst_gfs_2026050100_model_b",
                    "status": "failed",
                    "output_uri": "s3://nhms/runs/fcst_gfs_2026050100_model_b/output/",
                }
            return None

        def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
            calls.append((statement, parameters))
            if "FROM ops.pipeline_event" in statement:
                return [
                    {
                        "event_id": 1,
                        "entity_type": "pipeline_job",
                        "entity_id": "job_cycle_gfs_2026050100_forcing",
                        "event_type": "status_change",
                        "status_from": "running",
                        "status_to": "partially_failed",
                        "details": {
                            "stage": "forcing",
                            "task_results": [
                                {"task_id": 0, "model_id": "model_a", "status": "succeeded"},
                                {
                                    "task_id": 1,
                                    "model_id": "model_b",
                                    "status": "failed",
                                    "error_code": "NODE_FAILURE",
                                },
                            ],
                        },
                    },
                    {
                        "event_id": 2,
                        "entity_type": "pipeline_job",
                        "entity_id": "job_cycle_gfs_2026050100_forcing",
                        "event_type": "retry",
                        "details": {"trigger": "manual", "manual_retry_marker": True, "retry_count": 2},
                    },
                ]
            if "FROM ops.pipeline_job" in statement:
                return [
                    {
                        "job_id": "job_cycle_gfs_2026050100_forcing",
                        "run_id": "cycle_gfs_2026050100",
                        "cycle_id": "gfs_2026050100",
                        "model_id": None,
                        "status": "partially_failed",
                        "stage": "forcing",
                        "error_code": "NODE_FAILURE",
                        "retry_count": 1,
                    },
                    {
                        "job_id": "job_cycle_gfs_2026050100_publish",
                        "run_id": "cycle_gfs_2026050100",
                        "cycle_id": "gfs_2026050100",
                        "model_id": None,
                        "status": "succeeded",
                        "stage": "publish",
                        "retry_count": 0,
                    },
                    {
                        "job_id": "overflow",
                        "run_id": "cycle_gfs_2026050100",
                        "cycle_id": "gfs_2026050100",
                        "model_id": None,
                        "status": "succeeded",
                        "stage": "publish",
                    },
                ]
            return []

    repository = CapturingRepository("postgresql://example")

    state = repository.candidate_state(
        source_id="gfs",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        model_id="model_b",
        run_id="fcst_gfs_2026050100_model_b",
        forcing_version_id="forc_gfs_2026050100_model_b",
        candidate_id="gfs:2026-05-01T00:00:00Z:model_b:forecast_gfs_deterministic",
        retry_limit=3,
        job_limit=2,
        event_limit=1,
    )

    job_call = next(call for call in calls if "FROM ops.pipeline_job" in call[0])
    event_call = next(call for call in calls if "FROM ops.pipeline_event" in call[0])
    assert "LIMIT %s" in job_call[0]
    assert "COALESCE(updated_at, finished_at, submitted_at, started_at, created_at) DESC" in job_call[0]
    assert job_call[1][-1] == 3
    assert "SELECT pj.job_id" in event_call[0]
    assert "ORDER BY pe.created_at DESC, pe.event_id DESC" in event_call[0]
    assert event_call[1] == (
        "fcst_gfs_2026050100_model_b",
        "gfs_2026050100",
        "model_b",
        "gfs_2026050100",
        "fcst_gfs_2026050100_model_b",
        "gfs_2026050100",
        "cycle_gfs_2026050100",
        2,
    )
    assert state is not None
    assert state["pipeline_status"] == "partially_failed"
    assert state["failed_stage"] == "forcing"
    assert state["array_task_id"] == 1
    assert state["successful_sibling_outputs_reused"] is True
    assert state["pipeline_jobs_total"] == 3
    assert state["state_truncated"] is True


def test_psycopg_candidate_state_latest_truth_timestamp_selects_terminal_success() -> None:
    class CapturingRepository(PsycopgOrchestratorRepository):
        def _fetch_optional(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
            del statement, parameters
            return None

        def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
            del parameters
            if "FROM ops.pipeline_job" in statement:
                return [
                    {
                        "job_id": "job_success",
                        "run_id": "fcst_gfs_2026050100_model_b",
                        "cycle_id": "gfs_2026050100",
                        "model_id": "model_b",
                        "status": "succeeded",
                        "stage": "publish",
                        "submitted_at": "2026-05-01T06:00:00Z",
                        "finished_at": "2026-05-01T06:40:00Z",
                        "updated_at": "2026-05-01T06:40:00Z",
                    },
                    {
                        "job_id": "job_failed",
                        "run_id": "fcst_gfs_2026050100_model_b",
                        "cycle_id": "gfs_2026050100",
                        "model_id": "model_b",
                        "status": "failed",
                        "stage": "parse",
                        "error_code": "FAILED_PARSE",
                        "submitted_at": "2026-05-01T06:10:00Z",
                        "finished_at": "2026-05-01T06:20:00Z",
                        "updated_at": "2026-05-01T06:20:00Z",
                    },
                    {
                        "job_id": "old_overflow",
                        "run_id": "fcst_gfs_2026050100_model_b",
                        "cycle_id": "gfs_2026050100",
                        "model_id": "model_b",
                        "status": "failed",
                        "stage": "parse",
                        "error_code": "FAILED_PARSE",
                        "updated_at": "2026-05-01T05:00:00Z",
                    },
                ]
            return []

    repository = CapturingRepository("postgresql://example")

    state = repository.candidate_state(
        source_id="gfs",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        model_id="model_b",
        run_id="fcst_gfs_2026050100_model_b",
        forcing_version_id="forc_gfs_2026050100_model_b",
        candidate_id="gfs:2026-05-01T00:00:00Z:model_b:forecast_gfs_deterministic",
        retry_limit=3,
        job_limit=2,
        event_limit=10,
    )

    assert state is not None
    assert state["pipeline_status"] == "succeeded"
    assert state["failed_stage"] is None
    assert state["pipeline_truth_timestamp"] == "2026-05-01T06:40:00Z"
    assert state["pipeline_jobs_total"] == 3
    assert state["state_truncated"] is True


def test_psycopg_candidate_state_rejects_ambiguous_array_task_identity() -> None:
    class CapturingRepository(PsycopgOrchestratorRepository):
        def _fetch_optional(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
            del statement, parameters
            return None

        def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
            del parameters
            if "FROM ops.pipeline_job" in statement:
                return [
                    {
                        "job_id": "job_cycle_gfs_2026050100_forcing",
                        "run_id": "cycle_gfs_2026050100",
                        "cycle_id": "gfs_2026050100",
                        "model_id": None,
                        "status": "partially_failed",
                        "stage": "forcing",
                        "error_code": "NODE_FAILURE",
                        "retry_count": 1,
                    },
                    {
                        "job_id": "job_cycle_gfs_2026050100_publish",
                        "run_id": "cycle_gfs_2026050100",
                        "cycle_id": "gfs_2026050100",
                        "model_id": None,
                        "status": "succeeded",
                        "stage": "publish",
                        "retry_count": 0,
                    },
                ]
            if "FROM ops.pipeline_event" in statement:
                return [
                    {
                        "event_id": 1,
                        "entity_type": "pipeline_job",
                        "entity_id": "job_cycle_gfs_2026050100_forcing",
                        "event_type": "status_change",
                        "status_from": "running",
                        "status_to": "partially_failed",
                        "details": {
                            "stage": "forcing",
                            "task_results": [
                                {"task_id": 0, "status": "succeeded"},
                                {"task_id": 1, "status": "failed", "error_code": "NODE_FAILURE"},
                            ],
                        },
                    }
                ]
            return []

    repository = CapturingRepository("postgresql://example")

    state = repository.candidate_state(
        source_id="gfs",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        model_id="model_b",
        run_id="fcst_gfs_2026050100_model_b",
        forcing_version_id="forc_gfs_2026050100_model_b",
        candidate_id="gfs:2026-05-01T00:00:00Z:model_b:forecast_gfs_deterministic",
        retry_limit=3,
        job_limit=10,
        event_limit=10,
    )

    assert state is not None
    assert state["shared_cycle_aggregate"] is True
    assert state["pipeline_status"] is None
    assert state["array_task_id"] is None
    assert state["original_task_id"] is None
    assert state["successful_sibling_outputs_reused"] is False
    assert state["successful_sibling_task_count"] == 0


def test_psycopg_candidate_state_ignores_sibling_candidate_only_task_failure() -> None:
    class CapturingRepository(PsycopgOrchestratorRepository):
        def _fetch_optional(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
            del statement, parameters
            return None

        def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
            del parameters
            if "FROM ops.pipeline_event" in statement:
                return [
                    {
                        "event_id": 1,
                        "entity_type": "pipeline_job",
                        "entity_id": "job_cycle_gfs_2026050100_forecast",
                        "event_type": "status_change",
                        "status_to": "partially_failed",
                        "created_at": "2026-05-01T00:05:00Z",
                        "details": {
                            "stage": "forecast",
                            "task_results": [
                                {
                                    "task_id": 1,
                                    "original_task_id": 1,
                                    "candidate_id": "gfs:2026-05-01T00:00:00Z:model_b:forecast_gfs_deterministic",
                                    "status": "failed",
                                    "error_code": "NODE_FAILURE",
                                }
                            ],
                        },
                    }
                ]
            if "FROM ops.pipeline_job" in statement:
                return [
                    {
                        "job_id": "job_cycle_gfs_2026050100_forecast",
                        "run_id": "cycle_gfs_2026050100",
                        "cycle_id": "gfs_2026050100",
                        "model_id": None,
                        "status": "partially_failed",
                        "stage": "forecast",
                    }
                ]
            return []

    repository = CapturingRepository("postgresql://example")
    common = {
        "source_id": "gfs",
        "cycle_time": _dt("2026-05-01T00:00:00Z"),
        "forcing_version_id": "forc_gfs_2026050100_model_a",
        "retry_limit": 3,
        "job_limit": 10,
        "event_limit": 10,
    }

    model_a = repository.candidate_state(
        **common,
        model_id="model_a",
        run_id="fcst_gfs_2026050100_model_a",
        candidate_id="gfs:2026-05-01T00:00:00Z:model_a:forecast_gfs_deterministic",
    )
    model_b = repository.candidate_state(
        **{**common, "forcing_version_id": "forc_gfs_2026050100_model_b"},
        model_id="model_b",
        run_id="fcst_gfs_2026050100_model_b",
        candidate_id="gfs:2026-05-01T00:00:00Z:model_b:forecast_gfs_deterministic",
    )

    assert model_a is not None
    assert model_a["pipeline_status"] is None
    assert model_a["array_task_id"] is None
    assert model_b is not None
    assert model_b["pipeline_status"] == "partially_failed"
    assert model_b["failed_stage"] == "forecast"
    assert model_b["original_task_id"] == 1


def test_psycopg_candidate_state_ignores_conflicting_candidate_and_model_task_payload() -> None:
    class CapturingRepository(PsycopgOrchestratorRepository):
        def _fetch_optional(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
            del statement, parameters
            return None

        def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
            del parameters
            if "FROM ops.pipeline_event" in statement:
                return [
                    {
                        "event_id": 1,
                        "entity_type": "pipeline_job",
                        "entity_id": "job_cycle_gfs_2026050100_forecast",
                        "event_type": "status_change",
                        "status_to": "partially_failed",
                        "created_at": "2026-05-01T00:05:00Z",
                        "details": {
                            "stage": "forecast",
                            "task_results": [
                                {
                                    "task_id": 1,
                                    "original_task_id": 1,
                                    "candidate_id": "gfs:2026-05-01T00:00:00Z:model_b:forecast_gfs_deterministic",
                                    "model_id": "model_a",
                                    "status": "failed",
                                    "error_code": "NODE_FAILURE",
                                },
                                {
                                    "task_id": 2,
                                    "original_task_id": 2,
                                    "candidate_id": "gfs:2026-05-01T00:00:00Z:model_a:forecast_gfs_deterministic",
                                    "model_id": "model_a",
                                    "status": "failed",
                                    "error_code": "OUT_OF_MEMORY",
                                },
                            ],
                        },
                    }
                ]
            if "FROM ops.pipeline_job" in statement:
                return [
                    {
                        "job_id": "job_cycle_gfs_2026050100_forecast",
                        "run_id": "cycle_gfs_2026050100",
                        "cycle_id": "gfs_2026050100",
                        "model_id": None,
                        "status": "partially_failed",
                        "stage": "forecast",
                    }
                ]
            return []

    repository = CapturingRepository("postgresql://example")

    state = repository.candidate_state(
        source_id="gfs",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        model_id="model_a",
        run_id="fcst_gfs_2026050100_model_a",
        forcing_version_id="forc_gfs_2026050100_model_a",
        candidate_id="gfs:2026-05-01T00:00:00Z:model_a:forecast_gfs_deterministic",
        retry_limit=3,
        job_limit=10,
        event_limit=10,
    )

    assert state is not None
    assert state["pipeline_status"] == "partially_failed"
    assert state["original_task_id"] == 2
    assert state["error_code"] == "OUT_OF_MEMORY"


def test_psycopg_candidate_state_later_retry_success_supersedes_older_failed_task() -> None:
    class CapturingRepository(PsycopgOrchestratorRepository):
        def _fetch_optional(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
            del statement, parameters
            return {
                "run_id": "fcst_gfs_2026050100_model_b",
                "status": "failed",
                "output_uri": "s3://nhms/runs/fcst_gfs_2026050100_model_b/output/",
            }

        def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
            del parameters
            if "FROM ops.pipeline_job" in statement:
                return [
                    {
                        "job_id": "job_cycle_gfs_2026050100_forcing",
                        "run_id": "cycle_gfs_2026050100",
                        "cycle_id": "gfs_2026050100",
                        "model_id": None,
                        "status": "partially_failed",
                        "stage": "forcing",
                        "error_code": "NODE_FAILURE",
                        "retry_count": 1,
                    }
                ]
            if "FROM ops.pipeline_event" in statement:
                return [
                    {
                        "event_id": 1,
                        "entity_type": "pipeline_job",
                        "entity_id": "job_cycle_gfs_2026050100_forcing",
                        "event_type": "status_change",
                        "status_to": "partially_failed",
                        "created_at": "2026-05-01T00:01:00Z",
                        "details": {
                            "stage": "forcing",
                            "task_results": [
                                {
                                    "task_id": 1,
                                    "original_task_id": 1,
                                    "model_id": "model_b",
                                    "status": "failed",
                                    "error_code": "NODE_FAILURE",
                                }
                            ],
                        },
                    },
                    {
                        "event_id": 2,
                        "entity_type": "pipeline_job",
                        "entity_id": "job_cycle_gfs_2026050100_forcing_retry_1",
                        "event_type": "status_change",
                        "status_to": "succeeded",
                        "created_at": "2026-05-01T00:05:00Z",
                        "details": {
                            "stage": "forcing",
                            "task_results": [
                                {
                                    "task_id": 0,
                                    "original_task_id": 1,
                                    "model_id": "model_b",
                                    "status": "succeeded",
                                }
                            ],
                        },
                    },
                ]
            return []

    repository = CapturingRepository("postgresql://example")

    state = repository.candidate_state(
        source_id="gfs",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        model_id="model_b",
        run_id="fcst_gfs_2026050100_model_b",
        forcing_version_id="forc_gfs_2026050100_model_b",
        candidate_id="gfs:2026-05-01T00:00:00Z:model_b:forecast_gfs_deterministic",
        retry_limit=3,
        job_limit=10,
        event_limit=10,
    )

    assert state is not None
    assert state["pipeline_status"] is None
    assert state["failed_stage"] is None
    assert state["array_task_id"] is None
    assert state["original_task_id"] is None


class _SourceCycleRetrySupersessionRepository(PsycopgOrchestratorRepository):
    def __init__(
        self,
        *,
        jobs: Sequence[Mapping[str, Any]],
        events: Sequence[Mapping[str, Any]],
        forecast_cycle: Mapping[str, Any] | None,
    ) -> None:
        super().__init__("postgresql://example")
        self._jobs = [dict(job) for job in jobs]
        self._events = [dict(event) for event in events]
        self._forecast_cycle = dict(forecast_cycle) if forecast_cycle is not None else None

    def _fetch_optional(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
        del parameters
        if "FROM met.forecast_cycle" in statement:
            return dict(self._forecast_cycle) if self._forecast_cycle is not None else None
        return None

    def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
        del parameters
        if "FROM ops.pipeline_event" in statement:
            return [dict(event) for event in self._events]
        if "FROM ops.pipeline_job" in statement:
            return [dict(job) for job in self._jobs]
        return []


def _source_cycle_retry_state(
    *,
    jobs: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    manifest_uri: str | None = "raw/gfs/2026050100/manifest.json",
    forecast_status: str = "raw_complete",
    job_limit: int = 10,
    event_limit: int = 10,
) -> dict[str, Any]:
    repository = _SourceCycleRetrySupersessionRepository(
        jobs=jobs,
        events=events,
        forecast_cycle={
            "cycle_id": "gfs_2026050100",
            "source_id": "gfs",
            "cycle_time": _dt("2026-05-01T00:00:00Z"),
            "status": forecast_status,
            "manifest_uri": manifest_uri,
        },
    )

    state = repository.candidate_state(
        source_id="gfs",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        model_id="model_b",
        run_id="fcst_gfs_2026050100_model_b",
        forcing_version_id="forc_gfs_2026050100_model_b",
        candidate_id="gfs:2026-05-01T00:00:00Z:model_b:forecast_gfs_deterministic",
        retry_limit=3,
        job_limit=job_limit,
        event_limit=event_limit,
    )
    assert state is not None
    return state


def _failed_source_cycle_download_job(**overrides: Any) -> dict[str, Any]:
    payload = {
        "job_id": "job_cycle_gfs_2026050100_download",
        "run_id": "cycle_gfs_2026050100",
        "cycle_id": "gfs_2026050100",
        "job_type": "download_source_cycle",
        "slurm_job_id": "6101",
        "model_id": None,
        "status": "permanently_failed",
        "stage": "download",
        "retry_count": 1,
        "error_code": "SLURM_JOB_FAILED",
        "error_message": "download source cycle failed",
        "submitted_at": "2026-05-01T00:00:00Z",
        "finished_at": "2026-05-01T00:10:00Z",
        # The stale failed row was touched after the repair; it must still not
        # become the active blocker once a linked retry and manifest prove repair.
        "updated_at": "2026-05-01T01:00:00Z",
    }
    payload.update(overrides)
    return payload


def _successful_source_cycle_retry_job(**overrides: Any) -> dict[str, Any]:
    payload = {
        "job_id": "job_cycle_gfs_2026050100_retry_active",
        "run_id": "cycle_gfs_2026050100",
        "cycle_id": "gfs_2026050100",
        "job_type": "download_source_cycle",
        "slurm_job_id": "6102",
        "model_id": None,
        "status": "succeeded",
        "stage": "download",
        "retry_count": 2,
        "manual_retry_marker": True,
        "submitted_at": "2026-05-01T00:20:00Z",
        "finished_at": "2026-05-01T00:35:00Z",
        "updated_at": "2026-05-01T00:35:00Z",
    }
    payload.update(overrides)
    return payload


def _manual_retry_event(**overrides: Any) -> dict[str, Any]:
    details = {
        "trigger": "manual",
        "manual_retry_marker": True,
        "retry_count": 2,
        "previous_job_id": "job_cycle_gfs_2026050100_download",
        "stage": "download",
        "job_type": "download_source_cycle",
    }
    details.update(overrides.pop("details", {}))
    payload = {
        "event_id": 20,
        "entity_type": "pipeline_job",
        "entity_id": "job_cycle_gfs_2026050100_retry_active",
        "event_type": "retry",
        "status_from": "permanently_failed",
        "status_to": "pending",
        "created_at": "2026-05-01T00:20:00Z",
        "details": details,
    }
    payload.update(overrides)
    return payload


def test_psycopg_candidate_state_source_cycle_retry_success_repairs_stale_failed_download() -> None:
    state = _source_cycle_retry_state(
        jobs=[
            _failed_source_cycle_download_job(),
            _successful_source_cycle_retry_job(),
        ],
        events=[_manual_retry_event()],
    )

    assert state["pipeline_status"] is None
    assert state["failed_stage"] is None
    assert state["error_code"] is None
    assert state["stage"] is None
    assert state["repaired_stage_evidence"] == {
        "status": "repaired",
        "repair_status": "repaired",
        "stage": "download",
        "job_type": "download_source_cycle",
        "original_failed_job_id": "job_cycle_gfs_2026050100_download",
        "repairing_retry_job_id": "job_cycle_gfs_2026050100_retry_active",
        "manual_retry_event_id": 20,
        "manual_retry_marker": True,
        "manifest_uri": "raw/gfs/2026050100/manifest.json",
        "forecast_cycle_status": "raw_complete",
        "source_id": "gfs",
        "cycle_id": "gfs_2026050100",
        "cycle_time": "2026-05-01T00:00:00Z",
    }
    failed_job = next(job for job in state["pipeline_jobs"] if job["job_id"] == "job_cycle_gfs_2026050100_download")
    retry_job = next(job for job in state["pipeline_jobs"] if job["job_id"] == "job_cycle_gfs_2026050100_retry_active")
    assert failed_job["status"] == "permanently_failed"
    assert retry_job["status"] == "succeeded"


def test_psycopg_candidate_state_source_cycle_multihop_retry_repairs_failed_ancestors() -> None:
    original_job_id = "job_cycle_gfs_2026050100_download"
    failed_retry_job_id = "job_cycle_gfs_2026050100_download_retry_1"
    successful_retry_job_id = "job_cycle_gfs_2026050100_download_retry_2"

    state = _source_cycle_retry_state(
        jobs=[
            _failed_source_cycle_download_job(
                job_id=original_job_id,
                slurm_job_id="6101",
                retry_count=0,
                error_code="ORIGINAL_FAILED",
                submitted_at="2026-05-01T00:00:00Z",
                finished_at="2026-05-01T00:10:00Z",
                updated_at="2026-05-01T00:10:00Z",
            ),
            _failed_source_cycle_download_job(
                job_id=failed_retry_job_id,
                slurm_job_id="6102",
                retry_count=1,
                error_code="FAILED_RETRY",
                submitted_at="2026-05-01T00:20:00Z",
                finished_at="2026-05-01T00:30:00Z",
                created_at="2026-05-01T00:20:00Z",
                updated_at="2026-05-01T00:30:00Z",
            ),
            _successful_source_cycle_retry_job(
                job_id=successful_retry_job_id,
                slurm_job_id="6103",
                retry_count=2,
                submitted_at="2026-05-01T00:40:00Z",
                finished_at="2026-05-01T00:50:00Z",
                created_at="2026-05-01T00:40:00Z",
                updated_at="2026-05-01T00:50:00Z",
            ),
        ],
        events=[
            _manual_retry_event(
                event_id=21,
                entity_id=failed_retry_job_id,
                created_at="2026-05-01T00:20:00Z",
                details={
                    "previous_job_id": original_job_id,
                    "retry_count": 1,
                },
            ),
            _manual_retry_event(
                event_id=22,
                entity_id=successful_retry_job_id,
                created_at="2026-05-01T00:40:00Z",
                details={
                    "previous_job_id": failed_retry_job_id,
                    "retry_count": 2,
                },
            ),
        ],
    )

    assert state["pipeline_status"] is None
    assert state["failed_stage"] is None
    assert state["error_code"] is None
    assert state["repaired_stage_evidence"]["original_failed_job_id"] == original_job_id
    assert state["repaired_stage_evidence"]["repairing_retry_job_id"] == successful_retry_job_id
    assert state["repaired_stage_evidence"]["manual_retry_event_id"] == 22

    original_job = next(job for job in state["pipeline_jobs"] if job["job_id"] == original_job_id)
    failed_retry_job = next(job for job in state["pipeline_jobs"] if job["job_id"] == failed_retry_job_id)
    successful_retry_job = next(job for job in state["pipeline_jobs"] if job["job_id"] == successful_retry_job_id)
    for job in (original_job, failed_retry_job):
        assert job["repair_status"] == "repaired"
        assert job["repaired_by_job_id"] == successful_retry_job_id
        assert job["active_blocker"] is False
    assert successful_retry_job["repair_status"] == "repair_succeeded"
    assert successful_retry_job["repairs_job_id"] == original_job_id
    assert successful_retry_job["repairs_job_ids"] == [original_job_id, failed_retry_job_id]


def test_psycopg_candidate_state_prefixed_s3_manifest_repairs_stale_failed_source_cycle() -> None:
    state = _source_cycle_retry_state(
        jobs=[
            _failed_source_cycle_download_job(),
            _successful_source_cycle_retry_job(),
        ],
        events=[_manual_retry_event()],
        manifest_uri="s3://nhms-prod/qhh/raw/gfs/2026050100/manifest.json",
    )

    assert state["pipeline_status"] is None
    assert state["failed_stage"] is None
    assert state["error_code"] is None
    assert state["repaired_stage_evidence"]["manifest_uri"] == (
        "s3://nhms-prod/qhh/raw/gfs/2026050100/manifest.json"
    )
    failed_job = next(job for job in state["pipeline_jobs"] if job["job_id"] == "job_cycle_gfs_2026050100_download")
    assert failed_job["repair_status"] == "repaired"
    assert failed_job["repaired_by_job_id"] == "job_cycle_gfs_2026050100_retry_active"
    assert failed_job["active_blocker"] is False


def test_psycopg_candidate_state_failed_download_manifest_allows_linked_retry_repair() -> None:
    state = _source_cycle_retry_state(
        jobs=[
            _failed_source_cycle_download_job(),
            _successful_source_cycle_retry_job(),
        ],
        events=[_manual_retry_event()],
        manifest_uri="s3://nhms/raw/gfs/2026050100/manifest.json",
        forecast_status="failed_download",
    )

    assert state["pipeline_status"] is None
    assert state["failed_stage"] is None
    assert state["error_code"] is None
    assert state["repaired_stage_evidence"]["forecast_cycle_status"] == "failed_download"
    assert state["repaired_stage_evidence"]["repairing_retry_job_id"] == (
        "job_cycle_gfs_2026050100_retry_active"
    )
    failed_job = next(job for job in state["pipeline_jobs"] if job["job_id"] == "job_cycle_gfs_2026050100_download")
    assert failed_job["repair_status"] == "repaired"
    assert failed_job["active_blocker"] is False


def test_find_existing_source_cycle_stage_prefers_successful_manual_retry_identity() -> None:
    context = CycleOrchestrationContext(
        source_id="IFS",
        cycle_time=_dt("2026-06-09T12:00:00Z"),
        cycle_id="ifs_2026060912",
        run_id="cycle_ifs_2026060912",
        all_basins=[],
        active_basins=[],
    )

    selected = ForecastOrchestrator._find_existing_stage_job(
        ForecastOrchestrator,
        [
            {
                "job_id": "job_cycle_ifs_2026060912_download",
                "run_id": "cycle_ifs_2026060912",
                "cycle_id": "ifs_2026060912",
                "job_type": "download_source_cycle",
                "stage": "download",
                "status": "permanently_failed",
                "retry_count": 0,
                "finished_at": "2026-06-10T12:08:32Z",
                "created_at": "2026-06-10T04:03:46Z",
            },
            {
                "job_id": "cycle_ifs_2026060912_retry_2",
                "run_id": "cycle_ifs_2026060912",
                "cycle_id": "ifs_2026060912",
                "job_type": "download_source_cycle",
                "stage": "download",
                "status": "succeeded",
                "retry_count": 2,
                "updated_at": "2026-06-12T11:10:41Z",
                "created_at": "2026-06-12T10:51:55Z",
            },
        ],
        M3_STAGES[0],
        context=context,
    )

    assert selected is not None
    assert selected["job_id"] == "cycle_ifs_2026060912_retry_2"


def test_find_existing_source_cycle_stage_prefers_retry_active_by_persisted_retry_count() -> None:
    context = CycleOrchestrationContext(
        source_id="IFS",
        cycle_time=_dt("2026-06-09T12:00:00Z"),
        cycle_id="ifs_2026060912",
        run_id="cycle_ifs_2026060912",
        all_basins=[],
        active_basins=[],
    )

    selected = ForecastOrchestrator._find_existing_stage_job(
        ForecastOrchestrator,
        [
            {
                "job_id": "job_cycle_ifs_2026060912_download_retry_3",
                "run_id": "cycle_ifs_2026060912",
                "cycle_id": "ifs_2026060912",
                "job_type": "download_source_cycle",
                "stage": "download",
                "status": "permanently_failed",
                "retry_count": 3,
                "finished_at": "2026-06-12T10:40:00Z",
                "created_at": "2026-06-12T10:00:00Z",
            },
            {
                "job_id": "cycle_ifs_2026060912_retry_active",
                "run_id": "cycle_ifs_2026060912",
                "cycle_id": "ifs_2026060912",
                "job_type": "download_source_cycle",
                "stage": "download",
                "status": "succeeded",
                "retry_count": 4,
                "updated_at": "2026-06-12T11:10:41Z",
                "created_at": "2026-06-12T10:51:55Z",
            },
        ],
        M3_STAGES[0],
        context=context,
    )

    assert selected is not None
    assert selected["job_id"] == "cycle_ifs_2026060912_retry_active"


def test_psycopg_candidate_state_unrelated_success_does_not_repair_source_cycle_failure() -> None:
    state = _source_cycle_retry_state(
        jobs=[
            _failed_source_cycle_download_job(),
            _successful_source_cycle_retry_job(
                job_id="job_cycle_gfs_2026050100_publish",
                job_type="publish_results",
                stage="publish",
                retry_count=0,
                manual_retry_marker=False,
            ),
        ],
        events=[],
    )

    assert state["pipeline_status"] == "permanently_failed"
    assert state["failed_stage"] == "download"
    assert state["error_code"] == "SLURM_JOB_FAILED"
    assert "repaired_stage_evidence" not in state


def test_psycopg_candidate_state_unlinked_source_cycle_success_does_not_repair_failure() -> None:
    state = _source_cycle_retry_state(
        jobs=[
            _failed_source_cycle_download_job(),
            _successful_source_cycle_retry_job(),
        ],
        events=[
            _manual_retry_event(
                details={"previous_job_id": "job_cycle_gfs_2026050100_other_failed"},
            )
        ],
    )

    assert state["pipeline_status"] == "permanently_failed"
    assert state["failed_stage"] == "download"
    assert state["error_code"] == "SLURM_JOB_FAILED"
    assert "repaired_stage_evidence" not in state


@pytest.mark.parametrize("retry_status", ["pending", "failed"])
def test_psycopg_candidate_state_stale_or_non_succeeded_retry_does_not_repair_source_cycle_failure(
    retry_status: str,
) -> None:
    state = _source_cycle_retry_state(
        jobs=[
            _failed_source_cycle_download_job(),
            _successful_source_cycle_retry_job(status=retry_status),
        ],
        events=[
            _manual_retry_event(created_at="2026-05-01T00:01:00Z")
            if retry_status == "pending"
            else _manual_retry_event()
        ],
    )

    assert state["pipeline_status"] == "permanently_failed"
    assert state["failed_stage"] == "download"
    assert state["error_code"] == "SLURM_JOB_FAILED"
    assert "repaired_stage_evidence" not in state


@pytest.mark.parametrize(
    "manifest_uri",
    [
        None,
        "raw/gfs/2026050106/manifest.json",
        "raw/ifs/2026050100/manifest.json",
        "raw/gfs/2026050100/index.json",
        "s3://nhms-prod/qhh/raw/gfs/2026050106/manifest.json",
        "s3://nhms-prod/qhh/raw/ifs/2026050100/manifest.json",
        "s3://nhms-prod/qhh/raw/gfs/2026050100/index.json",
        "https://host/raw/gfs/2026050100/manifest.json",
        "file:///raw/gfs/2026050100/manifest.json",
        "s3://nhms-prod/qhh/raw/gfs/../2026050100/manifest.json",
    ],
)
def test_psycopg_candidate_state_unsupported_scheme_or_mismatched_manifest_keeps_source_cycle_failure_active(
    manifest_uri: str | None,
) -> None:
    state = _source_cycle_retry_state(
        jobs=[
            _failed_source_cycle_download_job(),
            _successful_source_cycle_retry_job(),
        ],
        events=[_manual_retry_event()],
        manifest_uri=manifest_uri,
    )

    assert state["pipeline_status"] == "permanently_failed"
    assert state["failed_stage"] == "download"
    assert state["error_code"] == "SLURM_JOB_FAILED"
    assert "repaired_stage_evidence" not in state


def test_psycopg_candidate_state_repaired_source_cycle_then_later_unrepaired_failure_stays_active() -> None:
    later_failed_job_id = "job_cycle_gfs_2026050100_download_retry_3"
    state = _source_cycle_retry_state(
        jobs=[
            _failed_source_cycle_download_job(),
            _successful_source_cycle_retry_job(),
            _failed_source_cycle_download_job(
                job_id=later_failed_job_id,
                slurm_job_id="6103",
                status="permanently_failed",
                retry_count=3,
                error_code="LATER_UNREPAIRED",
                error_message="later source cycle retry failed",
                submitted_at="2026-05-01T01:50:00Z",
                finished_at="2026-05-01T02:00:00Z",
                updated_at="2026-05-01T02:00:00Z",
            ),
        ],
        events=[_manual_retry_event()],
    )

    assert state["pipeline_status"] == "permanently_failed"
    assert state["failed_stage"] == "download"
    assert state["error_code"] == "LATER_UNREPAIRED"
    assert state["pipeline_truth_timestamp"] == "2026-05-01T02:00:00Z"
    assert "repaired_stage_evidence" not in state
    older_failed_job = next(
        job for job in state["pipeline_jobs"] if job["job_id"] == "job_cycle_gfs_2026050100_download"
    )
    later_failed_job = next(job for job in state["pipeline_jobs"] if job["job_id"] == later_failed_job_id)
    assert older_failed_job["repair_status"] == "repaired"
    assert older_failed_job["repaired_by_job_id"] == "job_cycle_gfs_2026050100_retry_active"
    assert older_failed_job["active_blocker"] is False
    assert "repair_status" not in later_failed_job


def test_psycopg_candidate_state_truncated_repair_window_keeps_latest_unrepaired_failure_active() -> None:
    later_failed_job_id = "job_cycle_gfs_2026050100_download_retry_3"
    state = _source_cycle_retry_state(
        jobs=[
            _failed_source_cycle_download_job(
                job_id=later_failed_job_id,
                slurm_job_id="6103",
                status="permanently_failed",
                retry_count=3,
                error_code="LATER_UNREPAIRED",
                error_message="later source cycle retry failed",
                submitted_at="2026-05-01T01:50:00Z",
                finished_at="2026-05-01T02:00:00Z",
                updated_at="2026-05-01T02:00:00Z",
            ),
            _successful_source_cycle_retry_job(),
            _failed_source_cycle_download_job(),
            _failed_source_cycle_download_job(
                job_id="job_cycle_gfs_2026050100_omitted_old_failure",
                slurm_job_id="6099",
                retry_count=0,
                error_code="OMITTED_OLD_FAILURE",
                submitted_at="2026-04-30T23:30:00Z",
                finished_at="2026-04-30T23:40:00Z",
                updated_at="2026-04-30T23:40:00Z",
            ),
        ],
        events=[_manual_retry_event()],
        job_limit=3,
    )

    assert state["state_truncated"] is True
    assert state["pipeline_status"] == "permanently_failed"
    assert state["failed_stage"] == "download"
    assert state["error_code"] == "LATER_UNREPAIRED"
    assert state["pipeline_truth_timestamp"] == "2026-05-01T02:00:00Z"
    assert "source_cycle_repair_evidence" not in state
    assert "repaired_stage_evidence" not in state
    older_failed_job = next(
        job for job in state["pipeline_jobs"] if job["job_id"] == "job_cycle_gfs_2026050100_download"
    )
    successful_retry_job = next(
        job for job in state["pipeline_jobs"] if job["job_id"] == "job_cycle_gfs_2026050100_retry_active"
    )
    later_failed_job = next(job for job in state["pipeline_jobs"] if job["job_id"] == later_failed_job_id)
    assert older_failed_job["repair_status"] == "repaired"
    assert older_failed_job["repaired_by_job_id"] == "job_cycle_gfs_2026050100_retry_active"
    assert older_failed_job["active_blocker"] is False
    assert successful_retry_job["repair_status"] == "repair_succeeded"
    assert successful_retry_job["repairs_job_id"] == "job_cycle_gfs_2026050100_download"
    assert "repair_status" not in later_failed_job


def test_psycopg_candidate_state_equal_truth_timestamp_selects_later_source_cycle_failure() -> None:
    older_failed_job_id = "job_cycle_gfs_2026050100_download_retry_2"
    later_failed_job_id = "job_cycle_gfs_2026050100_download_retry_3"

    state = _source_cycle_retry_state(
        jobs=[
            _failed_source_cycle_download_job(
                job_id=older_failed_job_id,
                retry_count=2,
                error_code="OLDER_UNREPAIRED",
                submitted_at="2026-05-01T01:20:00Z",
                finished_at="2026-05-01T01:30:00Z",
                created_at="2026-05-01T01:20:00Z",
                updated_at="2026-05-01T02:00:00Z",
            ),
            _failed_source_cycle_download_job(
                job_id=later_failed_job_id,
                retry_count=3,
                error_code="LATER_UNREPAIRED",
                submitted_at="2026-05-01T01:50:00Z",
                finished_at="2026-05-01T02:00:00Z",
                created_at="2026-05-01T01:50:00Z",
                updated_at="2026-05-01T02:00:00Z",
            ),
        ],
        events=[],
    )

    assert state["pipeline_status"] == "permanently_failed"
    assert state["failed_stage"] == "download"
    assert state["error_code"] == "LATER_UNREPAIRED"
    assert state["pipeline_truth_timestamp"] == "2026-05-01T02:00:00Z"
    active_job = next(job for job in state["pipeline_jobs"] if job["job_id"] == later_failed_job_id)
    assert active_job["retry_count"] == 3


def test_psycopg_candidate_state_truncated_source_cycle_repair_window_is_inconclusive() -> None:
    state = _source_cycle_retry_state(
        jobs=[
            _successful_source_cycle_retry_job(),
            _failed_source_cycle_download_job(),
            _failed_source_cycle_download_job(
                job_id="job_cycle_gfs_2026050100_omitted_old_failure",
                slurm_job_id="6099",
                retry_count=0,
                error_code="OMITTED_OLD_FAILURE",
                submitted_at="2026-04-30T23:30:00Z",
                finished_at="2026-04-30T23:40:00Z",
                updated_at="2026-04-30T23:40:00Z",
            ),
        ],
        events=[],
        job_limit=2,
        event_limit=10,
    )

    assert state["state_truncated"] is True
    assert state["pipeline_status"] is None
    assert state["failed_stage"] is None
    assert state["error_code"] is None
    assert "repaired_stage_evidence" not in state
    assert state["source_cycle_repair_evidence"] == {
        "status": "inconclusive_truncated",
        "truncated": True,
        "reason": "source_cycle_repair_window_truncated",
        "unresolved_failed_job_ids": ["job_cycle_gfs_2026050100_download"],
    }


def test_psycopg_candidate_state_bounds_nested_task_results_before_state_decision() -> None:
    class CapturingRepository(PsycopgOrchestratorRepository):
        def _fetch_optional(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
            del statement, parameters
            return None

        def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
            del parameters
            if "FROM ops.pipeline_event" in statement:
                task_results = [
                    {
                        "task_id": index,
                        "original_task_id": index,
                        "model_id": "model_a",
                        "status": "succeeded",
                    }
                    for index in range(17)
                ]
                task_results[-1] = {
                    **task_results[-1],
                    "status": "failed",
                    "error_code": "NODE_FAILURE",
                }
                return [
                    {
                        "event_id": 1,
                        "entity_type": "pipeline_job",
                        "entity_id": "job_cycle_gfs_2026050100_forecast",
                        "event_type": "status_change",
                        "status_to": "partially_failed",
                        "created_at": "2026-05-01T00:05:00Z",
                        "details": {
                            "stage": "forecast",
                            "task_results": task_results,
                        },
                    }
                ]
            if "FROM ops.pipeline_job" in statement:
                return [
                    {
                        "job_id": "job_cycle_gfs_2026050100_forecast",
                        "run_id": "cycle_gfs_2026050100",
                        "cycle_id": "gfs_2026050100",
                        "model_id": None,
                        "status": "partially_failed",
                        "stage": "forecast",
                        "error_code": "NODE_FAILURE",
                    }
                ]
            return []

    repository = CapturingRepository("postgresql://example")

    state = repository.candidate_state(
        source_id="gfs",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        model_id="model_a",
        run_id="fcst_gfs_2026050100_model_a",
        forcing_version_id="forc_gfs_2026050100_model_a",
        candidate_id="gfs:2026-05-01T00:00:00Z:model_a:forecast_gfs_deterministic",
        retry_limit=3,
        job_limit=10,
        event_limit=10,
    )

    assert state is not None
    details = state["pipeline_events"][0]["details"]
    assert state["pipeline_status"] is None
    assert state["array_task_id"] is None
    assert len(details["task_results"]) == 16
    assert details["task_results_total"] == 17
    assert details["task_results_included"] == 16
    assert details["task_results_overflow"] is True
    assert details["task_results_omitted"] == 1


def test_psycopg_candidate_state_does_not_scan_task_results_past_overflow_sentinel() -> None:
    task_results = BoundedReadSequence(
        [
            {
                "task_id": index,
                "original_task_id": index,
                "model_id": "model_b",
                "status": "succeeded",
            }
            for index in range(24)
        ],
        allowed_reads=17,
    )

    class CapturingRepository(PsycopgOrchestratorRepository):
        def _fetch_optional(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
            del statement, parameters
            return None

        def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
            del parameters
            if "FROM ops.pipeline_event" in statement:
                return [
                    {
                        "event_id": 1,
                        "entity_type": "pipeline_job",
                        "entity_id": "job_cycle_gfs_2026050100_forecast",
                        "event_type": "status_change",
                        "status_to": "partially_failed",
                        "created_at": "2026-05-01T00:05:00Z",
                        "details": {
                            "stage": "forecast",
                            "task_results": task_results,
                            "task_results_total": 120,
                        },
                    }
                ]
            if "FROM ops.pipeline_job" in statement:
                return [
                    {
                        "job_id": "job_cycle_gfs_2026050100_forecast",
                        "run_id": "cycle_gfs_2026050100",
                        "cycle_id": "gfs_2026050100",
                        "model_id": None,
                        "status": "partially_failed",
                        "stage": "forecast",
                        "error_code": "NODE_FAILURE",
                    }
                ]
            return []

    repository = CapturingRepository("postgresql://example")

    state = repository.candidate_state(
        source_id="gfs",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        model_id="model_b",
        run_id="fcst_gfs_2026050100_model_b",
        forcing_version_id="forc_gfs_2026050100_model_b",
        candidate_id="gfs:2026-05-01T00:00:00Z:model_b:forecast_gfs_deterministic",
        retry_limit=3,
        job_limit=10,
        event_limit=10,
    )

    assert state is not None
    details = state["pipeline_events"][0]["details"]
    assert state["pipeline_status"] is None
    assert state["array_task_id"] is None
    assert len(details["task_results"]) == 16
    assert details["task_results_total"] == 120
    assert details["task_results_included"] == 16
    assert details["task_results_limit"] == 16
    assert details["task_results_overflow"] is True
    assert details["task_results_omitted"] == 104
    assert task_results.read_count == 17


def test_publish_stage_failure_maps_to_failed_publish_cycle_status(tmp_path: Path) -> None:
    repository = FakeCycleRepository()
    client = PublishFailureSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "failed"
    assert result.stages[-1].stage == "publish"
    assert result.stages[-1].status == "failed"
    assert result.stages[-1].error_code == "NO_PUBLISHABLE_PRODUCTS"
    assert repository.cycle_statuses[-1] == "failed_publish"


def test_publish_stage_runs_on_control_node_when_published_root_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published_root = tmp_path / "published"
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(published_root))
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_URI_PREFIX", "published://")

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("services.orchestrator.chain.TilePublisher", _successful_control_node_publisher(calls))

    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "complete"
    assert "publish" not in [submission["stage"] for submission in client.submissions]
    assert calls[-1] == {"cycle_id": "gfs_2026050100"}
    publish_job = repository.jobs["job_cycle_gfs_2026050100_publish"]
    assert publish_job["slurm_job_id"] == "local"
    assert publish_job["status"] == "succeeded"
    assert publish_job["log_uri"].startswith("published://logs/gfs/2026050100/")
    published_log = published_root / publish_job["log_uri"].removeprefix("published://")
    payload = json.loads(published_log.read_text(encoding="utf-8"))
    assert payload["status"] == "published"
    assert payload["layers"] == [{"layer_id": "q-down:gfs_2026050100"}]


def test_trigger_ready_forecasts_rejects_stale_canonical_lineage_before_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-05-01T00:00:00Z")
    current_policy = {"source": "gfs", "forecast_hours": [0, 3]}
    current_object = {"source": "gfs", "object": "current"}
    stale_object = {"source": "gfs", "object": "stale"}
    repository = ReadyForecastRepository(
        cycle_time=cycle_time,
        canonical_products=_canonical_rows(
            source_id="gfs",
            cycle_time=cycle_time,
            forecast_hours=(0, 3),
            policy_identity=current_policy,
            source_object_identity=stale_object,
        ),
    )
    client = ImmediateTerminalSlurmClient()
    _patch_auto_trigger_identity(monkeypatch, policy=current_policy, source_object=current_object)
    orchestrator = _orchestrator(tmp_path, repository, client)

    results = orchestrator.trigger_ready_forecasts(source_id="gfs")

    assert len(results) == 1
    assert results[0].status == "skipped"
    assert client.submissions == []
    outcome = results[0].candidate_outcomes[0]
    assert outcome["reason"] == "canonical_identity_mismatch"
    evidence = outcome["state_evidence"]["canonical_readiness"]
    assert evidence["ready"] is False
    assert evidence["source_object_identity_matched"] is False


def test_trigger_ready_forecasts_rejects_missing_required_lineage_before_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-05-01T00:00:00Z")
    current_policy = {"source": "gfs", "forecast_hours": [0, 3]}
    current_object = {"source": "gfs", "object": "current"}
    repository = ReadyForecastRepository(
        cycle_time=cycle_time,
        canonical_products=_canonical_rows(
            source_id="gfs",
            cycle_time=cycle_time,
            forecast_hours=(0, 3),
            policy_identity=current_policy,
            source_object_identity=current_object,
            omit_source_object_identity=True,
        ),
    )
    client = ImmediateTerminalSlurmClient()
    _patch_auto_trigger_identity(monkeypatch, policy=current_policy, source_object=current_object)
    orchestrator = _orchestrator(tmp_path, repository, client)

    results = orchestrator.trigger_ready_forecasts(source_id="gfs")

    assert len(results) == 1
    assert results[0].status == "skipped"
    assert client.submissions == []
    outcome = results[0].candidate_outcomes[0]
    assert outcome["reason"] == "canonical_lineage_missing"
    evidence = outcome["state_evidence"]["canonical_readiness"]
    assert evidence["missing_source_object_identity_row_count"] > 0
    assert evidence["ready"] is False


def test_trigger_ready_forecasts_matching_lineage_preserves_submission_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-05-01T00:00:00Z")
    current_policy = {"source": "gfs", "forecast_hours": [0, 3]}
    current_object = {"source": "gfs", "object": "current"}
    repository = ReadyForecastRepository(
        cycle_time=cycle_time,
        canonical_products=_canonical_rows(
            source_id="gfs",
            cycle_time=cycle_time,
            forecast_hours=(0, 3),
            policy_identity=current_policy,
            source_object_identity=current_object,
        ),
    )
    client = ImmediateTerminalSlurmClient()
    _patch_auto_trigger_identity(monkeypatch, policy=current_policy, source_object=current_object)
    orchestrator = _orchestrator(tmp_path, repository, client)

    results = orchestrator.trigger_ready_forecasts(source_id="gfs")

    assert len(results) == 1
    assert results[0].status == "complete"
    assert [submission["stage"] for submission in client.submissions] == [
        "produce_forcing",
        "run_shud_forecast",
        "parse_output",
    ]
    assert repository.hydro_runs["fcst_gfs_2026050100_model_0"]["status"] == "parsed"


def test_trigger_ready_forecasts_demotes_stale_converter_version_before_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-05-01T00:00:00Z")
    current_policy = {"source": "gfs", "forecast_hours": [0, 3]}
    current_object = {"source": "gfs", "object": "current"}
    repository = ReadyForecastRepository(
        cycle_time=cycle_time,
        canonical_products=_canonical_rows(
            source_id="gfs",
            cycle_time=cycle_time,
            forecast_hours=(0, 3),
            policy_identity=current_policy,
            source_object_identity=current_object,
            converter_version="m1.0",
        ),
    )
    client = ImmediateTerminalSlurmClient()
    _patch_auto_trigger_identity(monkeypatch, policy=current_policy, source_object=current_object)
    orchestrator = _orchestrator(tmp_path, repository, client)

    results = orchestrator.trigger_ready_forecasts(source_id="gfs")

    assert len(results) == 1
    assert results[0].status == "skipped"
    assert client.submissions == []
    assert repository.cycle_statuses == ["raw_complete"]
    outcome = results[0].candidate_outcomes[0]
    assert outcome["reason"] == "canonical_converter_version_stale"
    evidence = outcome["state_evidence"]["canonical_readiness"]
    assert evidence["expected_converter_version"] == expected_converter_version("gfs")
    assert evidence["observed_converter_versions"] == ["m1.0"]
    stale_events = [
        event for event in repository.events if event["event_type"] == "canonical_converter_version_stale"
    ]
    assert len(stale_events) == 1
    assert stale_events[0]["status_from"] == "canonical_ready"
    assert stale_events[0]["status_to"] == "raw_complete"


def test_trigger_ready_forecasts_demotes_old_mm_precip_unit_before_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Migration self-heal regression lock: a pre-#269 cycle whose precip rows were
    # written with unit="mm" and *no* converter_version must still be demoted via
    # the orthogonal unit criterion, instead of dying terminally at the producer
    # mm/day gate.
    cycle_time = _dt("2026-05-01T00:00:00Z")
    current_policy = {"source": "gfs", "forecast_hours": [0, 3]}
    current_object = {"source": "gfs", "object": "current"}
    repository = ReadyForecastRepository(
        cycle_time=cycle_time,
        canonical_products=_canonical_rows(
            source_id="gfs",
            cycle_time=cycle_time,
            forecast_hours=(0, 3),
            policy_identity=current_policy,
            source_object_identity=current_object,
            omit_converter_version=True,
            unit="mm",
        ),
    )
    client = ImmediateTerminalSlurmClient()
    _patch_auto_trigger_identity(monkeypatch, policy=current_policy, source_object=current_object)
    orchestrator = _orchestrator(tmp_path, repository, client)

    results = orchestrator.trigger_ready_forecasts(source_id="gfs")

    assert len(results) == 1
    assert results[0].status == "skipped"
    assert client.submissions == []
    assert repository.cycle_statuses == ["raw_complete"]
    outcome = results[0].candidate_outcomes[0]
    assert outcome["reason"] == "canonical_converter_version_stale"
    evidence = outcome["state_evidence"]["canonical_readiness"]
    assert evidence["observed_converter_versions"] == ["unit:mm"]
    stale_events = [
        event for event in repository.events if event["event_type"] == "canonical_converter_version_stale"
    ]
    assert len(stale_events) == 1
    assert stale_events[0]["status_to"] == "raw_complete"


def test_trigger_ready_forecasts_mm_per_day_precip_unit_is_not_demoted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Post-#269 contract products (unit="mm/day") are consumable even when the
    # converter_version is absent, so they must not be demoted by the unit check.
    cycle_time = _dt("2026-05-01T00:00:00Z")
    current_policy = {"source": "gfs", "forecast_hours": [0, 3]}
    current_object = {"source": "gfs", "object": "current"}
    repository = ReadyForecastRepository(
        cycle_time=cycle_time,
        canonical_products=_canonical_rows(
            source_id="gfs",
            cycle_time=cycle_time,
            forecast_hours=(0, 3),
            policy_identity=current_policy,
            source_object_identity=current_object,
            omit_converter_version=True,
            unit="mm/day",
        ),
    )
    client = ImmediateTerminalSlurmClient()
    _patch_auto_trigger_identity(monkeypatch, policy=current_policy, source_object=current_object)
    orchestrator = _orchestrator(tmp_path, repository, client)

    results = orchestrator.trigger_ready_forecasts(source_id="gfs")

    assert "raw_complete" not in repository.cycle_statuses
    assert not [
        event for event in repository.events if event["event_type"] == "canonical_converter_version_stale"
    ]
    # Positive lock: not just "undemoted" but actually advanced to submission, so a
    # future bug that silently drops the cycle elsewhere cannot pass this test.
    assert results[0].status == "complete"
    assert client.submissions != []


def test_trigger_ready_forecasts_uppercase_mm_per_day_precip_unit_is_not_demoted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Normalization lock: the unit criterion lower-cases/strips before comparison,
    # so a "MM/DAY" (or padded) unit is the canonical contract and must NOT demote.
    cycle_time = _dt("2026-05-01T00:00:00Z")
    current_policy = {"source": "gfs", "forecast_hours": [0, 3]}
    current_object = {"source": "gfs", "object": "current"}
    repository = ReadyForecastRepository(
        cycle_time=cycle_time,
        canonical_products=_canonical_rows(
            source_id="gfs",
            cycle_time=cycle_time,
            forecast_hours=(0, 3),
            policy_identity=current_policy,
            source_object_identity=current_object,
            converter_version=expected_converter_version("gfs"),
            unit="  MM/DAY ",
        ),
    )
    client = ImmediateTerminalSlurmClient()
    _patch_auto_trigger_identity(monkeypatch, policy=current_policy, source_object=current_object)
    orchestrator = _orchestrator(tmp_path, repository, client)

    results = orchestrator.trigger_ready_forecasts(source_id="gfs")

    assert "raw_complete" not in repository.cycle_statuses
    assert not [
        event for event in repository.events if event["event_type"] == "canonical_converter_version_stale"
    ]
    assert results[0].status == "complete"


def test_trigger_ready_forecasts_missing_converter_version_is_not_demoted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Explicit lock on the 8fd0b6e missing-version backstop: a cycle without a
    # converter_version and without any unit must NOT be demoted (fixture/seed
    # safety), so the version criterion alone never fires on a missing value.
    cycle_time = _dt("2026-05-01T00:00:00Z")
    current_policy = {"source": "gfs", "forecast_hours": [0, 3]}
    current_object = {"source": "gfs", "object": "current"}
    repository = ReadyForecastRepository(
        cycle_time=cycle_time,
        canonical_products=_canonical_rows(
            source_id="gfs",
            cycle_time=cycle_time,
            forecast_hours=(0, 3),
            policy_identity=current_policy,
            source_object_identity=current_object,
            omit_converter_version=True,
        ),
    )
    client = ImmediateTerminalSlurmClient()
    _patch_auto_trigger_identity(monkeypatch, policy=current_policy, source_object=current_object)
    orchestrator = _orchestrator(tmp_path, repository, client)

    results = orchestrator.trigger_ready_forecasts(source_id="gfs")

    assert "raw_complete" not in repository.cycle_statuses
    assert not [
        event for event in repository.events if event["event_type"] == "canonical_converter_version_stale"
    ]
    assert results[0].status == "complete"
    assert client.submissions != []


def test_trigger_ready_forecasts_non_precip_bad_unit_is_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Variable-guard lock: the unit criterion only fires for prcp_rate_or_amount.
    # A non-precip product carrying an off-contract unit must be ignored, not demoted.
    cycle_time = _dt("2026-05-01T00:00:00Z")
    current_policy = {"source": "gfs", "forecast_hours": [0, 3]}
    current_object = {"source": "gfs", "object": "current"}
    canonical_products = _canonical_rows(
        source_id="gfs",
        cycle_time=cycle_time,
        forecast_hours=(0, 3),
        policy_identity=current_policy,
        source_object_identity=current_object,
        converter_version=expected_converter_version("gfs"),
    )
    for row in canonical_products:
        if row["variable"] != "prcp_rate_or_amount":
            row["unit"] = "bogus-unit"
    repository = ReadyForecastRepository(cycle_time=cycle_time, canonical_products=canonical_products)
    client = ImmediateTerminalSlurmClient()
    _patch_auto_trigger_identity(monkeypatch, policy=current_policy, source_object=current_object)
    orchestrator = _orchestrator(tmp_path, repository, client)

    results = orchestrator.trigger_ready_forecasts(source_id="gfs")

    assert "raw_complete" not in repository.cycle_statuses
    assert not [
        event for event in repository.events if event["event_type"] == "canonical_converter_version_stale"
    ]
    assert results[0].status == "complete"


def test_trigger_ready_forecasts_era5_version_is_not_demoted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cross-source version isolation: ERA5 (which never changed precip units)
    # must not be demoted, whether its products carry the current ERA5 version or
    # omit it entirely. The unit criterion stays dormant (no unit recorded).
    cycle_time = _dt("2026-05-01T00:00:00Z")
    current_policy = {"source": "ERA5", "forecast_hours": [0, 3]}
    current_object = {"source": "ERA5", "object": "current"}
    for version_kwargs in (
        {"converter_version": expected_converter_version("ERA5")},
        {"omit_converter_version": True},
    ):
        repository = ReadyForecastRepository(
            cycle_time=cycle_time,
            canonical_products=_canonical_rows(
                source_id="ERA5",
                cycle_time=cycle_time,
                forecast_hours=(0, 3),
                policy_identity=current_policy,
                source_object_identity=current_object,
                **version_kwargs,
            ),
            source_id="ERA5",
        )
        client = ImmediateTerminalSlurmClient()
        _patch_auto_trigger_identity(monkeypatch, policy=current_policy, source_object=current_object)
        orchestrator = _orchestrator(tmp_path, repository, client)

        results = orchestrator.trigger_ready_forecasts(source_id="ERA5")

        assert "raw_complete" not in repository.cycle_statuses
        assert not [
            event for event in repository.events if event["event_type"] == "canonical_converter_version_stale"
        ]
        # Positive lock: ERA5 reaches the pipeline and is never dropped *for staleness*
        # (any other skip reason is out of scope for this cross-source isolation test).
        assert results
        assert all(
            outcome["reason"] != "canonical_converter_version_stale"
            for result in results
            for outcome in result.candidate_outcomes
        )


def test_trigger_ready_forecasts_current_converter_version_is_not_demoted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-05-01T00:00:00Z")
    current_policy = {"source": "gfs", "forecast_hours": [0, 3]}
    current_object = {"source": "gfs", "object": "current"}
    repository = ReadyForecastRepository(
        cycle_time=cycle_time,
        canonical_products=_canonical_rows(
            source_id="gfs",
            cycle_time=cycle_time,
            forecast_hours=(0, 3),
            policy_identity=current_policy,
            source_object_identity=current_object,
            converter_version=expected_converter_version("gfs"),
        ),
    )
    client = ImmediateTerminalSlurmClient()
    _patch_auto_trigger_identity(monkeypatch, policy=current_policy, source_object=current_object)
    orchestrator = _orchestrator(tmp_path, repository, client)

    results = orchestrator.trigger_ready_forecasts(source_id="gfs")

    assert len(results) == 1
    assert results[0].status == "complete"
    assert "raw_complete" not in repository.cycle_statuses
    assert [submission["stage"] for submission in client.submissions] == [
        "produce_forcing",
        "run_shud_forecast",
        "parse_output",
    ]


def test_trigger_ready_forecasts_rejects_non_ok_canonical_row_before_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-05-01T00:00:00Z")
    current_policy = {"source": "gfs", "forecast_hours": [0, 3]}
    current_object = {"source": "gfs", "object": "current"}
    canonical_products = _canonical_rows(
        source_id="gfs",
        cycle_time=cycle_time,
        forecast_hours=(0, 3),
        policy_identity=current_policy,
        source_object_identity=current_object,
    )
    rejected = next(
        row
        for row in canonical_products
        if row["variable"] == "shortwave_down" and row["lead_time_hours"] == 3
    )
    rejected["quality_flag"] = "fail"
    repository = ReadyForecastRepository(cycle_time=cycle_time, canonical_products=canonical_products)
    client = ImmediateTerminalSlurmClient()
    _patch_auto_trigger_identity(monkeypatch, policy=current_policy, source_object=current_object)
    orchestrator = _orchestrator(tmp_path, repository, client)

    results = orchestrator.trigger_ready_forecasts(source_id="gfs")

    assert len(results) == 1
    assert results[0].status == "skipped"
    assert client.submissions == []
    outcome = results[0].candidate_outcomes[0]
    assert outcome["reason"] == "missing_canonical_leads"
    evidence = outcome["state_evidence"]["canonical_readiness"]
    assert evidence["ready"] is False
    assert evidence["rejected_quality_flags"] == {"fail": 1}
    assert evidence["missing_leads"][0]["missing_variables"] == ["shortwave_down"]


def test_trigger_ready_forecasts_rejects_checksum_missing_canonical_row_before_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-05-01T00:00:00Z")
    current_policy = {"source": "gfs", "forecast_hours": [0, 3]}
    current_object = {"source": "gfs", "object": "current"}
    canonical_products = _canonical_rows(
        source_id="gfs",
        cycle_time=cycle_time,
        forecast_hours=(0, 3),
        policy_identity=current_policy,
        source_object_identity=current_object,
    )
    rejected = next(
        row
        for row in canonical_products
        if row["variable"] == "shortwave_down" and row["lead_time_hours"] == 3
    )
    rejected["checksum"] = ""
    repository = ReadyForecastRepository(cycle_time=cycle_time, canonical_products=canonical_products)
    client = ImmediateTerminalSlurmClient()
    _patch_auto_trigger_identity(monkeypatch, policy=current_policy, source_object=current_object)
    orchestrator = _orchestrator(tmp_path, repository, client)

    results = orchestrator.trigger_ready_forecasts(source_id="gfs")

    assert len(results) == 1
    assert results[0].status == "skipped"
    assert client.submissions == []
    outcome = results[0].candidate_outcomes[0]
    assert outcome["reason"] == "missing_canonical_leads"
    evidence = outcome["state_evidence"]["canonical_readiness"]
    assert evidence["ready"] is False
    assert evidence["checksum_missing_row_count"] == 1
    assert evidence["checksum_missing_samples"][0]["reason"] == "checksum_missing"
    assert evidence["checksum_missing_samples"][0]["variable"] == "shortwave_down"
    assert evidence["missing_leads"][0]["missing_variables"] == ["shortwave_down"]


def test_ready_cycle_query_uses_stripped_nonblank_checksum_predicate() -> None:
    class CapturingRepository(PsycopgOrchestratorRepository):
        def __init__(self) -> None:
            super().__init__("postgresql://example")
            self.statement = ""

        def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
            self.statement = statement
            assert parameters == ("gfs", 5)
            return []

    repository = CapturingRepository()

    assert repository.list_canonical_ready_cycles(source_id="gfs", limit=5) == []
    assert "NULLIF(BTRIM(cmp.checksum), '') IS NOT NULL" in repository.statement
    assert "cmp.checksum <> ''" not in repository.statement


def test_trigger_ready_forecasts_provider_error_fails_closed_before_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-05-01T00:00:00Z")
    repository = ReadyForecastRepository(
        cycle_time=cycle_time,
        canonical_products=_canonical_rows(
            source_id="gfs",
            cycle_time=cycle_time,
            forecast_hours=(0, 3),
            policy_identity={"source": "gfs", "forecast_hours": [0, 3]},
            source_object_identity={"source": "gfs", "object": "current"},
        ),
    )
    client = ImmediateTerminalSlurmClient()

    def fail_policy(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("identity provider failed")

    monkeypatch.setattr("services.orchestrator.chain._auto_trigger_source_policy_identity", fail_policy)
    orchestrator = _orchestrator(tmp_path, repository, client)

    results = orchestrator.trigger_ready_forecasts(source_id="gfs")

    assert len(results) == 1
    assert results[0].status == "skipped"
    assert client.submissions == []
    evidence = results[0].candidate_outcomes[0]["state_evidence"]["canonical_readiness"]
    assert evidence["reason"] == "canonical_readiness_query_failed"
    assert evidence["dependency"]["status"] == "unavailable"


def _orchestrator(
    tmp_path: Path,
    repository: FakeCycleRepository,
    client: Any,
    *,
    retry_service: Any | None = None,
    object_store: LocalObjectStore | None = None,
    orchestrator_cls: type[ForecastOrchestrator] = ForecastOrchestrator,
    slurm_job_type_templates: dict[str, str] | None = None,
    slurm_env: dict[str, str] | None = None,
) -> ForecastOrchestrator:
    workspace = tmp_path / "workspace"
    object_root = tmp_path / "object-store"
    config = OrchestratorConfig(
        workspace_root=workspace,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        poll_interval_seconds=0,
        job_timeout_seconds=5,
        slurm_job_type_templates=slurm_job_type_templates or {},
        slurm_env=slurm_env or {},
    )
    return orchestrator_cls(
        config=config,
        repository=repository,
        slurm_client=client,
        object_store=object_store or LocalObjectStore(object_root, "s3://nhms"),
        retry_service=retry_service,
    )


class ReadyForecastRepository(FakeCycleRepository):
    def __init__(
        self,
        *,
        cycle_time: datetime,
        canonical_products: Sequence[Mapping[str, Any]],
        source_id: str = "gfs",
    ) -> None:
        super().__init__()
        self.source_id = source_id
        self.cycle_time = cycle_time
        self.canonical_products = [dict(product) for product in canonical_products]

    def list_canonical_ready_cycles(self, *, source_id: str | None, limit: int) -> list[dict[str, Any]]:
        del limit
        if source_id not in (None, self.source_id):
            return []
        return [
            {
                "source_id": self.source_id,
                "cycle_time": self.cycle_time,
                "cycle_id": cycle_id_for(self.source_id, self.cycle_time),
                "max_lead_hours": 3,
                "canonical_products": [dict(product) for product in self.canonical_products],
            }
        ]

    def list_forecast_model_ids(self) -> list[str]:
        return ["model_0"]

    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        del source_id, cycle_time, model_id
        return False

    def load_model_context(self, model_id: str) -> ModelContext:
        assert model_id == "model_0"
        return ModelContext(
            model_id=model_id,
            basin_id="basin_0",
            basin_version_id="basin_v0",
            river_network_version_id="river_v0",
            segment_count=1,
            model_package_uri="s3://nhms/models/model_0/v1/package/",
        )

    def find_forcing_context(self, *, source_id: str, cycle_time: datetime, model_id: str) -> ForcingContext:
        assert (source_id, cycle_time, model_id) == (self.source_id, self.cycle_time, "model_0")
        return ForcingContext(
            f"forc_{source_id}_{format_cycle_time(cycle_time)}_{model_id}",
            f"s3://nhms/forcing/{source_id}/{format_cycle_time(cycle_time)}/{model_id}/",
            max_lead_hours=3,
        )

    def create_hydro_run(self, context: Any, manifest: dict[str, Any]) -> dict[str, Any]:
        self.hydro_runs[context.run_id] = {
            "run_id": context.run_id,
            "status": "created",
            "run_manifest": dict(manifest),
        }
        return dict(self.hydro_runs[context.run_id])


def _canonical_rows(
    *,
    source_id: str,
    cycle_time: datetime,
    forecast_hours: Sequence[int],
    policy_identity: Mapping[str, Any],
    source_object_identity: Mapping[str, Any],
    omit_source_object_identity: bool = False,
    converter_version: str | None = None,
    omit_converter_version: bool = False,
    unit: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    compact_cycle = format_cycle_time(cycle_time)
    resolved_converter_version = (
        converter_version if converter_version is not None else expected_converter_version(source_id)
    )
    for forecast_hour in forecast_hours:
        for variable in GFS_REQUIRED_STANDARD_VARIABLES:
            lineage = {
                "policy_identity": dict(policy_identity),
                "source_object_identity": dict(source_object_identity),
                "source_cycle_id": f"{source_id}_{compact_cycle}",
                "converter_version": resolved_converter_version,
            }
            if omit_source_object_identity:
                lineage.pop("source_object_identity")
            if omit_converter_version:
                lineage.pop("converter_version", None)
            row: dict[str, Any] = {
                "canonical_product_id": f"{source_id}_{compact_cycle}_{variable}_f{forecast_hour:03d}",
                "source_id": source_id,
                "cycle_time": cycle_time,
                "valid_time": cycle_time + timedelta(hours=forecast_hour),
                "lead_time_hours": forecast_hour,
                "variable": variable,
                "checksum": f"sha256:{variable}:{forecast_hour}",
                "quality_flag": "ok",
                "lineage_json": lineage,
            }
            if unit is not None:
                row["unit"] = unit
            rows.append(row)
    return rows


def _patch_auto_trigger_identity(
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy: Mapping[str, Any],
    source_object: Mapping[str, Any],
) -> None:
    monkeypatch.setattr(
        "services.orchestrator.chain._auto_trigger_source_policy_identity",
        lambda **_kwargs: dict(policy),
    )
    monkeypatch.setattr(
        "services.orchestrator.chain._auto_trigger_source_object_identity",
        lambda **_kwargs: dict(source_object),
    )


def _pipeline_store() -> PipelineStore:
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_schemas(dbapi_connection: Any, _connection_record: Any) -> None:
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS ops")

    Base.metadata.create_all(engine)
    return PipelineStore(Session(engine))


def _basins(count: int) -> list[dict[str, Any]]:
    return [
        {
            "model_id": f"model_{index}",
            "basin_id": f"basin_{index}",
            "basin_version_id": f"basin_v{index}",
            "river_network_version_id": f"river_v{index}",
            "run_id": f"run_{index}",
        }
        for index in range(count)
    ]


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _pipeline_status_not_in_clause(statement: str) -> set[str]:
    marker = "pj.status NOT IN ("
    start = statement.index(marker) + len(marker)
    end = statement.index(")", start)
    return {status.strip().strip("'") for status in statement[start:end].split(",") if status.strip()}


class BoundedReadSequence(Sequence[Any]):
    def __init__(self, items: list[Any], *, allowed_reads: int) -> None:
        self.items = items
        self.allowed_reads = allowed_reads
        self.read_count = 0

    def __iter__(self) -> Any:
        for index, item in enumerate(self.items):
            if index >= self.allowed_reads:
                raise AssertionError("task_results scanned past overflow sentinel")
            self.read_count = index + 1
            yield item

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            raise AssertionError("task_results must not be sliced")
        if index >= self.allowed_reads:
            raise AssertionError("task_results scanned past overflow sentinel")
        self.read_count = max(self.read_count, index + 1)
        return self.items[index]

    def __len__(self) -> int:
        raise AssertionError("task_results length must not be required")


def _fmt(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class _CaptureCursor:
    """Minimal psycopg2-style cursor that records the executed SQL/params."""

    def __init__(self, captured: list[tuple[str, tuple[Any, ...]]]) -> None:
        self._captured = captured
        self.description = [type("Col", (), {"name": "job_id"})()]

    def __enter__(self) -> _CaptureCursor:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
        self._captured.append((statement, parameters))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [("captured-job",)]


class _CaptureConnection:
    def __init__(self, captured: list[tuple[str, tuple[Any, ...]]]) -> None:
        self._captured = captured
        self.autocommit = False

    def cursor(self) -> _CaptureCursor:
        return _CaptureCursor(self._captured)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def test_psycopg_upsert_pipeline_job_sql_matches_params_for_array_task_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the real psycopg SQL: column/placeholder/param positions must line up.

    All other tests exercise the fake dict repository, so the hand-edited
    ``array_task_id`` column (15 -> 16) and its ON CONFLICT clause are never
    validated. A miscounted ``%s`` or a shifted param would still pass those
    tests but crash on the real Postgres at node-22. This captures the actual
    SQL/params and asserts they are mutually consistent.
    """

    captured: list[tuple[str, tuple[Any, ...]]] = []

    fake_psycopg2 = types.ModuleType("psycopg2")
    fake_psycopg2.Error = Exception  # type: ignore[attr-defined]
    fake_psycopg2.connect = lambda _url: _CaptureConnection(captured)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)

    repository = PsycopgOrchestratorRepository("postgresql://capture/test")
    record = {
        "job_id": "job-1",
        "run_id": "run-1",
        "cycle_id": "cycle-1",
        "job_type": "forecast",
        "slurm_job_id": "12345",
        "array_task_id": 7,
        "model_id": "model-1",
        "status": "submitted",
        "stage": "forecast",
        "submitted_at": None,
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "log_uri": None,
    }

    repository.upsert_pipeline_job(record)

    assert len(captured) == 1
    statement, parameters = captured[0]

    # (a) array_task_id is in the INSERT column list and the ON CONFLICT update.
    assert "array_task_id" in statement
    assert "array_task_id = EXCLUDED.array_task_id" in statement

    # (b) placeholder count equals param count -> no off-by-one in the VALUES tuple.
    insert_values = statement.split("VALUES", 1)[1].split("ON CONFLICT", 1)[0]
    assert insert_values.count("%s") == len(parameters)

    # The INSERT column list length must match the param/placeholder count too.
    column_block = statement.split("INSERT INTO", 1)[1].split("VALUES", 1)[0]
    column_list = column_block[column_block.index("(") + 1 : column_block.rindex(")")]
    columns = [name.strip() for name in column_list.split(",") if name.strip()]
    assert len(columns) == len(parameters)

    # (c) array_task_id's value lands at the param slot matching its column index.
    array_index = columns.index("array_task_id")
    assert parameters[array_index] == 7


def test_template_export_lines_injects_grib_env_when_set(monkeypatch):
    from services.orchestrator.chain import _template_export_lines

    root = "/scratch/frd_muziyao/nhms-grib"
    monkeypatch.setenv("NHMS_GRIB_ENV_ROOT", root)
    lines = _template_export_lines({"workspace_dir": "/work"})

    # $PATH / ${LD_LIBRARY_PATH:-} must survive literally (not swallowed by quotes).
    # shlex.quote leaves a no-special-char path unquoted, so build the expected
    # lines the same way the implementation does.
    quoted = shlex.quote(root)
    assert f"export PATH={quoted}/bin:$PATH" in lines
    assert f"export LD_LIBRARY_PATH={quoted}/lib:${{LD_LIBRARY_PATH:-}}" in lines
    # Injection lines come after the existing export_fields lines.
    assert lines.index(f"export PATH={quoted}/bin:$PATH") > 0


def test_template_export_lines_omits_grib_env_when_unset(monkeypatch, tmp_path: Path):
    from services.orchestrator.chain import _template_export_lines

    monkeypatch.delenv("NHMS_GRIB_ENV_ROOT", raising=False)
    # Environment-independent: point the runtime venv-bin at an existing tmp dir
    # via the explicit override, so the assertion holds with or without cwd/.venv.
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    monkeypatch.setenv("NHMS_PYTHON_VENV_BIN", str(venv_bin))
    lines = _template_export_lines({"workspace_dir": "/work"})

    expected_venv = shlex.quote(str(venv_bin.resolve()))
    assert f"export PATH={expected_venv}:$PATH" in lines
    assert not any(line.startswith("export LD_LIBRARY_PATH=") for line in lines)


def test_template_export_lines_includes_published_artifact_root(monkeypatch):
    from services.orchestrator.chain import _template_export_lines

    monkeypatch.delenv("NHMS_GRIB_ENV_ROOT", raising=False)
    lines = _template_export_lines(
        {
            "workspace_dir": "/work",
            "published_artifact_root": "/ghdc/data/nwm/published",
            "published_artifact_uri_prefix": "published://",
        }
    )

    assert "export NHMS_PUBLISHED_ARTIFACT_ROOT=/ghdc/data/nwm/published" in lines
    assert "export NHMS_PUBLISHED_ARTIFACT_URI_PREFIX=published://" in lines


def test_template_export_lines_quotes_grib_env_with_special_chars(monkeypatch):
    from services.orchestrator.chain import _template_export_lines

    root = "/scratch/with space/$(touch pwn)/grib"
    monkeypatch.setenv("NHMS_GRIB_ENV_ROOT", root)
    lines = _template_export_lines({"workspace_dir": "/work"})

    quoted = shlex.quote(root)
    assert f"export PATH={quoted}/bin:$PATH" in lines
    assert f"export LD_LIBRARY_PATH={quoted}/lib:${{LD_LIBRARY_PATH:-}}" in lines
    # The dangerous substitution must be inside single quotes (neutralized).
    path_line = next(line for line in lines if line.startswith("export PATH="))
    assert "$(touch pwn)" not in path_line.replace(quoted, "")


# --- M24 §3A: two-phase reserve -> bind through the real chain submit path ----


def test_chain_stage_reserves_before_submit_and_binds_after(tmp_path: Path) -> None:
    """Each cycle-level stage is reserved (durably, reserved status, no slurm
    bind) BEFORE sbatch and atomically bound to slurm_job_id after, with the
    idempotency comment threaded to the slurm client."""

    from services.orchestrator.reservation import (
        SLURM_COMMENT_PREFIX,
        idempotency_key_from_comment,
    )

    store = _pipeline_store()
    repository = StoreBackedCycleRepository(store)

    observed: list[dict[str, Any]] = []

    class _ReservationAssertingClient(FakeCycleSlurmClient):
        def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
            comment = payload.get("comment")
            key = idempotency_key_from_comment(comment)
            # The reservation MUST already be durable and unbound at submit time.
            state = repository.query_candidate_state(key)
            observed.append(
                {
                    "stage": payload["manifest"]["stage"],
                    "comment": comment,
                    "reserved_before_submit": state is not None,
                    "slurm_unbound_at_submit": state is not None and state["slurm_job_id"] is None,
                    "status_at_submit": state["status"] if state else None,
                }
            )
            return super().submit_job(payload)

        def submit_job_array(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return super().submit_job_array(*args, **kwargs)

    client = _ReservationAssertingClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))

    assert result.status == "complete"

    # Non-array stages went through the reservation-asserting submit_job.
    assert observed, "expected at least one non-array stage submission"
    for entry in observed:
        assert entry["comment"].startswith(SLURM_COMMENT_PREFIX)
        assert entry["reserved_before_submit"] is True
        assert entry["slurm_unbound_at_submit"] is True
        assert entry["status_at_submit"] == "reserved"

    # After the run, those reservations are bound to a slurm_job_id (phase 2).
    for stage in ("download", "convert"):
        job = repository.jobs[f"job_cycle_gfs_2026050100_{stage}"]
        assert job["idempotency_key"] == f"cycle_gfs_2026050100:{stage}"
        assert job["slurm_job_id"] is not None


def test_array_stage_submission_threads_idempotency_comment(tmp_path: Path) -> None:
    """Every ARRAY-stage submission carries the idempotency ``--comment`` through
    ``_submit_array_stage`` → ``submit_job_array(manifest=...)`` so array-master
    crash recovery can reconcile-by-comment (item 2 BLOCKER).

    Counterfactual: drop the array comment injection (chain.py ``stage_manifest[
    "comment"] = slurm_comment_for(...)`` at submit, or the manifest pass-through
    in ``_submit_array_stage``) and the array manifests carry no comment, so the
    per-array assertion below goes red — while the non-array path is unaffected.
    """

    from services.orchestrator.reservation import (
        SLURM_COMMENT_PREFIX,
        idempotency_key_from_comment,
    )

    array_comments: dict[str, str | None] = {}

    class _ArrayCommentCapturingClient(FakeCycleSlurmClient):
        def submit_job_array(
            self,
            job_type: str,
            *,
            cycle_id: str,
            stage_name: str,
            tasks: list[dict[str, Any]],
            manifest: dict[str, Any],
        ) -> dict[str, Any]:
            array_comments[stage_name] = manifest.get("comment")
            return super().submit_job_array(
                job_type,
                cycle_id=cycle_id,
                stage_name=stage_name,
                tasks=tasks,
                manifest=manifest,
            )

    store = _pipeline_store()
    repository = StoreBackedCycleRepository(store)
    client = _ArrayCommentCapturingClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))
    assert result.status == "complete"

    array_stages = [stage.stage for stage in M3_STAGES if stage.is_array]
    assert array_stages, "expected array stages in M3 pipeline"
    for stage_name in array_stages:
        comment = array_comments.get(stage_name)
        assert comment is not None, f"array stage {stage_name} submitted without --comment"
        assert comment.startswith(SLURM_COMMENT_PREFIX)
        # The threaded comment must recover the candidate's idempotency_key so the
        # array master can be reconciled-by-comment after a crash before bind.
        assert (
            idempotency_key_from_comment(comment)
            == f"cycle_gfs_2026050100:{stage_name}"
        )


def test_chain_stage_reservation_is_idempotent_across_resubmit(tmp_path: Path) -> None:
    """Re-running the same cycle reuses the reservation (same idempotency_key,
    no duplicate durable row)."""

    store = _pipeline_store()
    repository = StoreBackedCycleRepository(store)
    orchestrator = _orchestrator(tmp_path, repository, FakeCycleSlurmClient())

    orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(2))
    key = "cycle_gfs_2026050100:download"
    first = repository.query_candidate_state(key)
    assert first is not None

    # A second pass over the same cycle must not create a second row for the key.
    rows = [j for j in store.session.query(PipelineJob).all() if j.idempotency_key == key]
    assert len(rows) == 1


def test_manual_retry_terminal_stage_submits_new_attempt_identity(tmp_path: Path) -> None:
    """A manual retry must not reuse the old terminal stage job/idempotency key.

    Production hit this with ``job_cycle_gfs_..._download`` already failed and
    bound to an old Slurm job: reusing the same idempotency key made the reserve
    gate skip sbatch and evidence kept pointing at the stale failure.
    """

    from services.orchestrator.reservation import idempotency_key_from_comment

    store = _pipeline_store()
    repository = StoreBackedCycleRepository(store)
    old_job = store.create_job(
        job_id="job_cycle_gfs_2026050100_download",
        run_id="cycle_gfs_2026050100",
        cycle_id="gfs_2026050100",
        job_type="download_source_cycle",
        slurm_job_id="6051",
        model_id=None,
        stage="download",
        status="failed",
        idempotency_key="cycle_gfs_2026050100:download",
    )
    old_job.exit_code = 127
    old_job.error_code = "SLURM_JOB_FAILED"
    store.session.add(old_job)
    store.session.commit()

    comments: list[str | None] = []

    class _CommentCapturingClient(FakeCycleSlurmClient):
        def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
            comments.append(payload.get("comment"))
            return super().submit_job(payload)

    client = _CommentCapturingClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basins = _basins(1)
    basins[0]["manual_retry_attempt"] = 4
    basins[0]["state_evidence"] = {
        "decision": "manual_retry",
        "manual_retry": {"allowed": True, "new_attempt": 4},
        "fresh_ingestion": {"required": True, "mode": "full_chain"},
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", basins)

    retry_job_id = "job_cycle_gfs_2026050100_download_retry_4"
    assert result.status == "complete"
    assert result.stages[0].pipeline_job_id == retry_job_id
    persisted_old_job = store.get_job("job_cycle_gfs_2026050100_download")
    assert persisted_old_job is not None
    assert persisted_old_job.slurm_job_id == "6051"
    assert persisted_old_job.status == "failed"
    retry_job = repository.jobs[retry_job_id]
    assert retry_job["status"] == "succeeded"
    assert retry_job["slurm_job_id"] == "2001"
    assert retry_job["idempotency_key"] == "cycle_gfs_2026050100:download:retry_4"
    assert idempotency_key_from_comment(comments[0]) == "cycle_gfs_2026050100:download:retry_4"
    assert client.submissions[0]["stage"] == "download"


class _RaceSemanticsCycleRepository(StoreBackedCycleRepository):
    """Reserve with production INSERT...ON CONFLICT DO NOTHING RETURNING.

    Returns the inserted row on a win, ``None`` on a conflict (a row already
    exists) — the DB RETURNING win/lose signal the reserve gate depends on.
    """

    def reserve_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any] | None:
        job = self.store.reserve_job(
            job_id=record["job_id"],
            run_id=record.get("run_id"),
            cycle_id=record.get("cycle_id"),
            job_type=record["job_type"],
            model_id=record.get("model_id"),
            stage=record.get("stage"),
            status=record.get("status", "reserved"),
            idempotency_key=record["idempotency_key"],
            candidate_id=record.get("candidate_id"),
        )
        if job is None:
            return None
        result = self._job_to_dict(job)
        self.jobs[record["job_id"]] = result
        return dict(result)


def test_overlapping_pass_does_not_double_submit_real_submit_path(tmp_path: Path) -> None:
    """An overlapping pass drives the REAL submit path for a candidate stage that
    a prior pass already has IN FLIGHT; the reserve gate makes sbatch fire zero
    times — the skip happens BEFORE manifest build / sbatch.

    The prior pass is modelled by a durable, active (running, slurm-bound)
    reservation row under the candidate's idempotency_key — exactly what a
    concurrent pass would have written via reserve+bind.

    Counterfactual: revert the gate (drop ``_reservation_already_inflight`` /
    ``_skip_duplicate_submission``) and this overlapping pass proceeds to build
    the manifest and call submit_job → submit_calls becomes non-empty → red.
    """

    from services.orchestrator.chain import _cycle_stage_idempotency_key

    store = _pipeline_store()
    repository = _RaceSemanticsCycleRepository(store)

    submit_calls: list[str] = []

    class _CountingSubmitClient(FakeCycleSlurmClient):
        def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
            submit_calls.append(payload["manifest"]["stage"])
            return super().submit_job(payload)

        def submit_job_array(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            submit_calls.append(kwargs.get("stage_name", "array"))
            return super().submit_job_array(*args, **kwargs)

    client = _CountingSubmitClient()
    orchestrator = _orchestrator(tmp_path, repository, client)

    stage = M3_STAGES[0]  # download: non-array, single submit_job.
    context = CycleOrchestrationContext(
        source_id="gfs",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        cycle_id="gfs_2026050100",
        run_id="cycle_gfs_2026050100",
        all_basins=_basins(2),
        active_basins=_basins(2),
    )

    # Prior overlapping pass already reserved AND bound this candidate (active,
    # in flight). reserve_job wins; bind stamps a slurm_job_id + 'running'.
    key = _cycle_stage_idempotency_key(context, stage)
    store.reserve_job(
        job_id="prior_pass_job",
        run_id=context.run_id,
        cycle_id=context.cycle_id,
        job_type=stage.job_type,
        model_id=None,
        stage=stage.stage,
        idempotency_key=key,
    )
    store.bind_reservation(key, slurm_job_id="90099", status="running")

    # This (overlapping) pass goes through the real submit entry point; the
    # reserve gate must short-circuit to a typed skip with NO sbatch.
    result, aggregation = orchestrator._submit_and_wait_cycle_stage(stage, context)

    assert result.status == "skipped_duplicate_submission"
    assert aggregation is None
    assert submit_calls == []  # gate fired before any sbatch.
    assert any(skip["idempotency_key"] == key for skip in orchestrator.duplicate_submission_skips)
    # No second durable row was created for the key.
    rows = [j for j in store.session.query(PipelineJob).all() if j.idempotency_key == key]
    assert len(rows) == 1
