from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from services.orchestrator import chain_array_accounting, chain_manifests, chain_repository_state, chain_runtime_utils
from services.orchestrator.chain_types import (
    AnalysisRunContext,
    ForcingContext,
    ForecastRunContext,
    ModelContext,
    OrchestratorError,
)
from workers.data_adapters.base import cycle_id_for, format_cycle_time, parse_cycle_time

ACTIVE_HYDRO_STATUSES = {"created", "staged", "submitted", "running"}
COMPLETED_HYDRO_STATUSES = {"succeeded", "parsed", "frequency_done", "published", "complete"}
DEFAULT_CANDIDATE_STATE_EVENT_LIMIT = 100
DEFAULT_CANDIDATE_STATE_JOB_LIMIT = 100
_first_optional_int = chain_runtime_utils._first_optional_int
_max_lead_hours_from_lineage = chain_runtime_utils._max_lead_hours_from_lineage
_nested_mapping = chain_manifests._nested_mapping
_optional_str = chain_runtime_utils._optional_str
_coerce_mapping = chain_array_accounting._coerce_mapping
_parse_gateway_time = chain_runtime_utils._parse_gateway_time


@dataclass(frozen=True)
class PsycopgOrchestratorRepository:
    database_url: str

    @classmethod
    def from_env(cls) -> PsycopgOrchestratorRepository:
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise OrchestratorError("DATABASE_URL_MISSING", "DATABASE_URL is required for orchestration.")
        return cls(database_url)

    def has_active_orchestration(self, *, source_id: str, cycle_time: datetime) -> bool:
        cycle_id = cycle_id_for(source_id, cycle_time)
        row = self._fetch_optional(
            """
            SELECT 1 AS active
            FROM ops.pipeline_job
            WHERE cycle_id = %s
              AND status NOT IN (
                'succeeded', 'partially_failed', 'failed', 'cancelled', 'submission_failed', 'permanently_failed'
              )
            LIMIT 1
            """,
            (cycle_id,),
        )
        return row is not None

    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        cycle_id = cycle_id_for(source_id, cycle_time)
        cycle_run_id = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
        candidate_run_id = f"fcst_{source_id.lower()}_{format_cycle_time(cycle_time)}_{model_id}"
        row = self._fetch_optional(
            """
            SELECT 1 AS active
            FROM hydro.hydro_run h
            WHERE h.source_id = %s
              AND h.cycle_time = %s
              AND h.model_id = %s
              AND h.status::text = ANY(%s)
            UNION ALL
            SELECT 1 AS active
            FROM ops.pipeline_job pj
            WHERE pj.cycle_id = %s
              AND pj.status NOT IN (
                'succeeded', 'partially_failed', 'failed', 'cancelled', 'submission_failed', 'permanently_failed'
              )
              AND (
                    pj.run_id = %s
                 OR pj.run_id = %s
                 OR pj.model_id = %s
                 OR (pj.run_id = %s AND pj.model_id IS NULL)
              )
            LIMIT 1
            """,
            (
                source_id,
                cycle_time,
                model_id,
                list(ACTIVE_HYDRO_STATUSES),
                cycle_id,
                candidate_run_id,
                cycle_run_id,
                model_id,
                cycle_run_id,
            ),
        )
        return row is not None

    def has_completed_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        row = self._fetch_optional(
            """
            SELECT 1 AS completed
            FROM hydro.hydro_run
            WHERE source_id = %s
              AND cycle_time = %s
              AND model_id = %s
              AND status::text = ANY(%s)
            LIMIT 1
            """,
            (source_id, cycle_time, model_id, list(COMPLETED_HYDRO_STATUSES)),
        )
        return row is not None

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
        return chain_repository_state.candidate_state(
            self,
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

    def active_slurm_jobs(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        limit: int = DEFAULT_CANDIDATE_STATE_JOB_LIMIT,
    ) -> list[dict[str, Any]]:
        cycle_id = cycle_id_for(source_id, cycle_time)
        cycle_run_id = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
        limit = max(int(limit), 1)
        return self._fetch_all(
            """
            SELECT
                pj.job_id,
                pj.run_id,
                pj.cycle_id,
                pj.job_type,
                pj.slurm_job_id,
                pj.model_id,
                pj.status,
                pj.stage,
                pj.submitted_at,
                pj.started_at,
                pj.finished_at,
                pj.exit_code,
                pj.error_code,
                pj.error_message,
                pj.log_uri
            FROM ops.pipeline_job pj
            LEFT JOIN hydro.hydro_run h ON h.run_id = pj.run_id
            WHERE pj.cycle_id = %s
              AND pj.slurm_job_id IS NOT NULL
              AND pj.status NOT IN (
                'succeeded', 'partially_failed', 'failed', 'cancelled', 'submission_failed', 'permanently_failed'
              )
              AND (
                    h.model_id = %s
                 OR pj.model_id = %s
                 OR pj.run_id = %s
                 OR pj.run_id = %s
                 OR (pj.model_id IS NULL AND pj.run_id = %s)
              )
            ORDER BY pj.submitted_at ASC NULLS LAST, pj.created_at ASC
            LIMIT %s
            """,
            (
                cycle_id,
                model_id,
                model_id,
                f"fcst_{source_id.lower()}_{format_cycle_time(cycle_time)}_{model_id}",
                cycle_run_id,
                cycle_run_id,
                limit,
            ),
        )

    def has_active_analysis_run(self, *, model_id: str, start_time: datetime, end_time: datetime) -> bool:
        row = self._fetch_optional(
            """
            SELECT 1 AS active
            FROM hydro.hydro_run h
            WHERE h.run_type = 'analysis'
              AND h.model_id = %s
              AND h.status NOT IN ('failed', 'cancelled', 'superseded')
              AND h.start_time < %s
              AND h.end_time > %s
            LIMIT 1
            """,
            (model_id, end_time, start_time),
        )
        return row is not None

    def list_canonical_ready_cycles(self, *, source_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        parameters: list[Any] = []
        source_filter = ""
        if source_id is not None:
            source_filter = "AND fc.source_id = %s"
            parameters.append(source_id)
        parameters.append(max(int(limit), 1))
        return self._fetch_all(
            f"""
            SELECT
                fc.source_id,
                fc.cycle_time,
                fc.cycle_id,
                MAX(cmp.lead_time_hours) AS max_lead_hours,
                COALESCE(
                    jsonb_agg(
                        jsonb_build_object(
                            'canonical_product_id', cmp.canonical_product_id,
                            'source_id', cmp.source_id,
                            'cycle_time', cmp.cycle_time,
                            'valid_time', cmp.valid_time,
                            'lead_time_hours', cmp.lead_time_hours,
                            'variable', cmp.variable,
                            'unit', cmp.unit,
                            'quality_flag', cmp.quality_flag,
                            'checksum', cmp.checksum,
                            'lineage_json', cmp.lineage_json
                        )
                        ORDER BY cmp.lead_time_hours, cmp.variable, cmp.canonical_product_id
                    ) FILTER (WHERE cmp.canonical_product_id IS NOT NULL),
                    '[]'::jsonb
                ) AS canonical_products
            FROM met.forecast_cycle fc
            LEFT JOIN met.canonical_met_product cmp
              ON cmp.source_id = fc.source_id
             AND cmp.cycle_time = fc.cycle_time
             AND cmp.quality_flag = 'ok'
             AND NULLIF(BTRIM(cmp.checksum), '') IS NOT NULL
            WHERE fc.status = 'canonical_ready'
              {source_filter}
            GROUP BY fc.source_id, fc.cycle_time, fc.cycle_id
            ORDER BY fc.cycle_time ASC, fc.source_id ASC
            LIMIT %s
            """,
            tuple(parameters),
        )

    def list_forecast_model_ids(self) -> list[str]:
        rows = self._fetch_all(
            """
            SELECT model_id
            FROM core.model_instance
            WHERE active_flag = true
              AND lifecycle_state = 'active'
            ORDER BY model_id
            """,
            (),
        )
        return [str(row["model_id"]) for row in rows]

    def load_model_context(self, model_id: str) -> ModelContext:
        row = self._fetch_one(
            """
            SELECT
                mi.model_id,
                bv.basin_id,
                mi.basin_version_id,
                mi.river_network_version_id,
                rn.segment_count,
                mi.resource_profile,
                mi.model_package_uri
            FROM core.model_instance mi
            JOIN core.basin_version bv ON bv.basin_version_id = mi.basin_version_id
            JOIN core.river_network_version rn ON rn.river_network_version_id = mi.river_network_version_id
            WHERE mi.model_id = %s
            """,
            (model_id,),
            missing_code="MODEL_NOT_FOUND",
            missing_message=f"model_instance not found: {model_id}",
        )
        return ModelContext(
            model_id=str(row["model_id"]),
            basin_id=row.get("basin_id"),
            basin_version_id=str(row["basin_version_id"]),
            river_network_version_id=str(row["river_network_version_id"]),
            segment_count=int(row["segment_count"]),
            model_package_uri=str(row["model_package_uri"]),
            output_segment_count=_first_optional_int(
                _nested_mapping(row.get("resource_profile")).get("output_segment_count"),
                row["segment_count"],
            ),
            model_package_checksum=_optional_str(_nested_mapping(row.get("resource_profile")).get("package_checksum")),
        )

    def find_forcing_context(self, *, source_id: str, cycle_time: datetime, model_id: str) -> ForcingContext:
        row = self._fetch_optional(
            """
            SELECT forcing_version_id, forcing_package_uri, start_time, end_time, source_id, lineage_json
            FROM met.forcing_version
            WHERE source_id = %s
              AND cycle_time = %s
              AND model_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (source_id, cycle_time, model_id),
        )
        if row is None:
            return ForcingContext(None, None)
        return ForcingContext(
            row.get("forcing_version_id"),
            row.get("forcing_package_uri"),
            row.get("start_time"),
            row.get("end_time"),
            row.get("source_id"),
            _max_lead_hours_from_lineage(row.get("lineage_json")),
            _optional_str(_nested_mapping(row.get("lineage_json")).get("forcing_package_manifest_uri")),
            _optional_str(_nested_mapping(row.get("lineage_json")).get("forcing_package_manifest_checksum")),
        )

    def ensure_forecast_cycle(self, *, source_id: str, cycle_time: datetime) -> dict[str, Any]:
        return self._fetch_one(
            """
            INSERT INTO met.forecast_cycle (cycle_id, source_id, cycle_time, issue_time, status)
            VALUES (%s, %s, %s, %s, 'discovered')
            ON CONFLICT (source_id, cycle_time) DO UPDATE SET
                issue_time = COALESCE(met.forecast_cycle.issue_time, EXCLUDED.issue_time)
            RETURNING *
            """,
            (cycle_id_for(source_id, cycle_time), source_id, cycle_time, cycle_time),
        )

    def create_hydro_run(
        self,
        context: ForecastRunContext | AnalysisRunContext,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        init_state_id = getattr(context, "init_state_id", None) or manifest.get("initial_state", {}).get("state_id")
        return self._fetch_one(
            """
            INSERT INTO hydro.hydro_run (
                run_id,
                run_type,
                scenario_id,
                model_id,
                basin_version_id,
                forcing_version_id,
                init_state_id,
                source_id,
                cycle_time,
                start_time,
                end_time,
                status,
                run_manifest_uri,
                output_uri,
                log_uri
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'created', %s, %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET
                status = 'created',
                forcing_version_id = EXCLUDED.forcing_version_id,
                init_state_id = EXCLUDED.init_state_id,
                run_manifest_uri = EXCLUDED.run_manifest_uri,
                output_uri = EXCLUDED.output_uri,
                log_uri = EXCLUDED.log_uri,
                error_code = NULL,
                error_message = NULL,
                updated_at = now()
            WHERE hydro.hydro_run.status IN ('failed', 'cancelled')
            RETURNING *
            """,
            (
                context.run_id,
                manifest.get("run_type", "forecast"),
                manifest["scenario_id"],
                context.model_id,
                context.basin_version_id,
                context.forcing_version_id,
                init_state_id,
                context.source_id,
                context.cycle_time,
                context.start_time,
                context.end_time,
                context.run_manifest_uri,
                context.output_uri,
                context.log_uri,
            ),
            missing_code="HYDRO_RUN_NOT_RETRIABLE",
            missing_message=f"hydro_run already exists and is not retriable: {context.run_id}",
        )

    def create_hydro_run_from_basin(
        self,
        basin: Mapping[str, Any],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        run_id = str(manifest["run_id"])
        model = _coerce_mapping(manifest["model"])
        forcing = _coerce_mapping(manifest.get("forcing") or {})
        outputs = _coerce_mapping(manifest.get("outputs") or {})
        initial_state = _coerce_mapping(manifest.get("initial_state") or {})
        statement = """
            INSERT INTO hydro.hydro_run (
                run_id,
                run_type,
                scenario_id,
                model_id,
                basin_version_id,
                forcing_version_id,
                init_state_id,
                source_id,
                cycle_time,
                start_time,
                end_time,
                status,
                run_manifest_uri,
                output_uri,
                log_uri
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'created', %s, %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET
                status = 'created',
                forcing_version_id = EXCLUDED.forcing_version_id,
                init_state_id = EXCLUDED.init_state_id,
                run_manifest_uri = EXCLUDED.run_manifest_uri,
                output_uri = EXCLUDED.output_uri,
                log_uri = EXCLUDED.log_uri,
                error_code = NULL,
                error_message = NULL,
                updated_at = now()
            WHERE hydro.hydro_run.status IN ('failed', 'cancelled')
            RETURNING *
            """
        parameters = (
            run_id,
            manifest.get("run_type", "forecast"),
            manifest["scenario_id"],
            model["model_id"],
            model["basin_version_id"],
            forcing.get("forcing_version_id"),
            initial_state.get("state_id") or basin.get("init_state_id"),
            manifest.get("source_id") or basin.get("source_id"),
            parse_cycle_time(manifest["cycle_time"]),
            _parse_gateway_time(manifest["start_time"]),
            _parse_gateway_time(manifest["end_time"]),
            outputs.get("run_manifest_uri"),
            outputs.get("output_uri"),
            outputs.get("log_uri"),
        )
        try:
            return self._fetch_one(
                statement,
                parameters,
                missing_code="HYDRO_RUN_NOT_RETRIABLE",
                missing_message=f"hydro_run already exists and is not retriable: {run_id}",
            )
        except OrchestratorError as exc:
            if exc.error_code != "HYDRO_RUN_NOT_RETRIABLE":
                raise
            return self._fetch_one(
                "SELECT * FROM hydro.hydro_run WHERE run_id = %s",
                (run_id,),
                missing_code="HYDRO_RUN_NOT_FOUND",
                missing_message=f"hydro_run not found after conflict: {run_id}",
            )

    def update_hydro_run_status(
        self,
        run_id: str,
        status: str,
        *,
        slurm_job_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        assignments = ["status = %s", "updated_at = now()"]
        parameters: list[Any] = [status]
        for column, value in (
            ("slurm_job_id", slurm_job_id),
            ("error_code", error_code),
            ("error_message", error_message),
        ):
            if value is not None:
                assignments.append(f"{column} = %s")
                parameters.append(value)
        parameters.append(run_id)
        return self._fetch_one(
            f"""
            UPDATE hydro.hydro_run
            SET {", ".join(assignments)}
            WHERE run_id = %s
            RETURNING *
            """,
            tuple(parameters),
        )

    def upsert_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._fetch_one(
            """
            INSERT INTO ops.pipeline_job (
                job_id,
                run_id,
                cycle_id,
                job_type,
                slurm_job_id,
                array_task_id,
                model_id,
                status,
                stage,
                submitted_at,
                started_at,
                finished_at,
                exit_code,
                error_code,
                error_message,
                log_uri
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (job_id) DO UPDATE SET
                slurm_job_id = EXCLUDED.slurm_job_id,
                array_task_id = EXCLUDED.array_task_id,
                model_id = EXCLUDED.model_id,
                status = EXCLUDED.status,
                submitted_at = EXCLUDED.submitted_at,
                started_at = EXCLUDED.started_at,
                finished_at = EXCLUDED.finished_at,
                exit_code = EXCLUDED.exit_code,
                error_code = EXCLUDED.error_code,
                error_message = EXCLUDED.error_message,
                log_uri = EXCLUDED.log_uri,
                updated_at = now()
            RETURNING *
            """,
            (
                record["job_id"],
                record["run_id"],
                record["cycle_id"],
                record["job_type"],
                record["slurm_job_id"],
                record.get("array_task_id"),
                record.get("model_id"),
                record["status"],
                record["stage"],
                record["submitted_at"],
                record["started_at"],
                record["finished_at"],
                record["exit_code"],
                record["error_code"],
                record["error_message"],
                record["log_uri"],
            ),
        )

    def reserve_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """Phase 1: durable reservation row keyed by idempotency_key.

        ``ON CONFLICT DO NOTHING RETURNING`` (absorbing any unique conflict) is
        the authoritative win/lose signal: a returned row means THIS call
        inserted the reservation (won the race); ``None`` means a row already
        existed (lost / already in-flight). The caller never decides the winner
        by comparing a deterministic job_id — only the presence of the RETURNING
        row counts.

        The conflict target is deliberately omitted. A narrow
        ``ON CONFLICT (idempotency_key)`` only absorbs idempotency_key clashes;
        a pre-existing row carrying the SAME job_id but a NULL idempotency_key
        (a legacy / non-reserve row) would slip past the partial index and hit
        the job_id primary key instead, raising and aborting the whole pass. The
        protocol contract is "reserve never raises; RETURNING decides" — so we
        absorb ANY unique conflict (idempotency_key unique index OR job_id PK):
        any clash → DO NOTHING → zero rows → ``None`` → judged a loss, never an
        exception.

        A plain reserve can only INSERT, so it loses forever to a DEAD reservation
        row that still occupies the idempotency_key unique index (one that was
        reserved but never bound — sbatch rejected it into ``submission_failed``,
        or a crashed pass had it demoted to ``reservation_lost`` by reconcile).
        Reclaiming such a dead row is NOT this method's job; it is handled by
        ``reclaim_pipeline_job_reservation`` (C1 + m3), which atomically takes the
        dead row back to ``reserved`` so this pass can re-submit.

        ``submitted_at`` is deliberately left NULL at reserve time; it is stamped
        only when the reservation is bound to a real slurm_job_id (phase 2).
        """

        return self._fetch_optional(
            """
            INSERT INTO ops.pipeline_job (
                job_id, run_id, cycle_id, job_type, model_id, stage,
                status, idempotency_key, candidate_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING *
            """,
            (
                record["job_id"],
                record.get("run_id"),
                record.get("cycle_id"),
                record["job_type"],
                record.get("model_id"),
                record.get("stage"),
                record.get("status", "reserved"),
                record["idempotency_key"],
                record.get("candidate_id"),
            ),
        )

    def reclaim_pipeline_job_reservation(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """Atomically take over a DEAD reservation (reserved-but-never-bound).

        A row is dead when ``slurm_job_id IS NULL AND status IN
        ('submission_failed','reservation_lost')`` — it was reserved but never got
        a live slurm job (sbatch rejected, or the pass crashed and reconcile
        demoted it). Such a row keeps the idempotency_key and therefore occupies
        the partial unique index, so a plain ``reserve`` would lose to it forever.
        This UPDATE re-claims it back to ``reserved`` so THIS pass can re-submit.

        Race-safe: the predicate matches ONLY the dead statuses, so two concurrent
        take-overs cannot both win — the loser sees ``status='reserved'`` (set by
        the winner) and the WHERE no longer matches, returning zero rows. A
        genuinely in-flight ``reserved``/``submitted``/``running`` row is never
        matched, so no double-submit. ``job_id`` (the PK) is deliberately NOT
        overwritten — the dead row already carries the deterministic job_id for
        this identity, so there is no PK-collision risk. Lifecycle fields are
        reset; identity columns are filled only when previously NULL via COALESCE.
        """

        return self._fetch_optional(
            """
            WITH reclaimed_by_key AS (
                UPDATE ops.pipeline_job
                SET status = 'reserved',
                    slurm_job_id = NULL,
                    array_task_id = NULL,
                    submitted_at = NULL,
                    started_at = NULL,
                    finished_at = NULL,
                    exit_code = NULL,
                    error_code = NULL,
                    error_message = NULL,
                    run_id = COALESCE(run_id, %s),
                    cycle_id = COALESCE(cycle_id, %s),
                    model_id = COALESCE(model_id, %s),
                    stage = COALESCE(stage, %s),
                    candidate_id = COALESCE(candidate_id, %s),
                    updated_at = now()
                WHERE idempotency_key = %s
                  AND slurm_job_id IS NULL
                  AND status IN ('submission_failed', 'reservation_lost')
                RETURNING *
            ), reclaimed_pending AS (
                UPDATE ops.pipeline_job
                SET status = 'reserved',
                    idempotency_key = %s,
                    candidate_id = COALESCE(candidate_id, %s),
                    submitted_at = NULL,
                    started_at = NULL,
                    finished_at = NULL,
                    exit_code = NULL,
                    error_code = NULL,
                    error_message = NULL,
                    updated_at = now()
                WHERE job_id = %s
                  AND idempotency_key IS NULL
                  AND slurm_job_id IS NULL
                  AND status = 'pending'
                  AND NOT EXISTS (SELECT 1 FROM reclaimed_by_key)
                  AND NOT EXISTS (
                      SELECT 1 FROM ops.pipeline_job
                      WHERE idempotency_key = %s
                  )
                RETURNING *
            )
            SELECT * FROM reclaimed_by_key
            UNION ALL
            SELECT * FROM reclaimed_pending
            LIMIT 1
            """,
            (
                record.get("run_id"),
                record.get("cycle_id"),
                record.get("model_id"),
                record.get("stage"),
                record.get("candidate_id"),
                record["idempotency_key"],
                record["idempotency_key"],
                record.get("candidate_id"),
                record["job_id"],
                record["idempotency_key"],
            ),
        )

    def bind_pipeline_job_reservation(
        self,
        idempotency_key: str,
        *,
        slurm_job_id: str,
        status: str = "submitted",
        array_task_id: int | None = None,
    ) -> dict[str, Any] | None:
        """Phase 2: atomically bind slurm_job_id; no-op if already bound."""

        return self._fetch_optional(
            """
            UPDATE ops.pipeline_job
            SET slurm_job_id = %s,
                array_task_id = COALESCE(%s, array_task_id),
                status = %s,
                submitted_at = COALESCE(submitted_at, now()),
                updated_at = now()
            WHERE idempotency_key = %s
              AND slurm_job_id IS NULL
            RETURNING *
            """,
            (slurm_job_id, array_task_id, status, idempotency_key),
        )

    def query_candidate_state(self, idempotency_key: str) -> dict[str, Any] | None:
        return self._fetch_optional(
            "SELECT * FROM ops.pipeline_job WHERE idempotency_key = %s",
            (idempotency_key,),
        )

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
        current = self._fetch_optional("SELECT status FROM ops.pipeline_job WHERE job_id = %s", (job_id,))
        previous_status = current.get("status") if current is not None else None
        record = self._fetch_optional(
            """
            UPDATE ops.pipeline_job
            SET status = %s,
                started_at = COALESCE(%s, started_at),
                finished_at = COALESCE(%s, finished_at),
                exit_code = COALESCE(%s, exit_code),
                error_code = CASE
                    WHEN %s IN ('succeeded', 'complete', 'published') AND %s IS NULL THEN NULL
                    ELSE COALESCE(%s, error_code)
                END,
                error_message = CASE
                    WHEN %s IN ('succeeded', 'complete', 'published') AND %s IS NULL THEN NULL
                    ELSE COALESCE(%s, error_message)
                END,
                log_uri = COALESCE(%s, log_uri),
                updated_at = now()
            WHERE job_id = %s
              AND status <> 'permanently_failed'
              AND (
                    status NOT IN ('succeeded', 'failed', 'cancelled')
                 OR %s IN ('partially_failed', 'permanently_failed')
              )
            RETURNING *
            """,
            (
                status,
                started_at,
                finished_at,
                exit_code,
                status,
                error_code,
                error_code,
                status,
                error_message,
                error_message,
                log_uri,
                job_id,
                status,
            ),
        )
        if record is None:
            record = self._fetch_one(
                "SELECT * FROM ops.pipeline_job WHERE job_id = %s",
                (job_id,),
                missing_code="PIPELINE_JOB_NOT_FOUND",
                missing_message=f"pipeline_job not found: {job_id}",
            )
        return previous_status, record

    def get_pipeline_job(self, job_id: str) -> dict[str, Any] | None:
        return self._fetch_optional("SELECT * FROM ops.pipeline_job WHERE job_id = %s", (job_id,))

    def query_pipeline_jobs_by_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT *
            FROM ops.pipeline_job
            WHERE cycle_id = %s
            ORDER BY submitted_at ASC NULLS LAST, created_at ASC
            """,
            (cycle_id,),
        )

    def query_pipeline_jobs_by_run(self, run_id: str) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT *
            FROM ops.pipeline_job
            WHERE run_id = %s
            ORDER BY submitted_at ASC NULLS LAST, created_at ASC
            """,
            (run_id,),
        )

    def query_pipeline_job_by_slurm_id(self, slurm_job_id: str) -> dict[str, Any] | None:
        return self._fetch_optional(
            "SELECT * FROM ops.pipeline_job WHERE slurm_job_id = %s LIMIT 1",
            (slurm_job_id,),
        )

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
        try:
            from psycopg2.extras import Json
        except ImportError as error:
            raise OrchestratorError("PSYCOPG2_MISSING", "psycopg2 is required for orchestration.") from error

        return self._fetch_one(
            """
            INSERT INTO ops.pipeline_event (
                entity_type,
                entity_id,
                event_type,
                status_from,
                status_to,
                message,
                details
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (entity_type, entity_id, event_type, status_from, status_to, message, Json(details or {})),
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
        return self._fetch_optional(
            """
            UPDATE met.forecast_cycle
            SET status = %s,
                error_code = %s,
                error_message = %s
            WHERE source_id = %s
              AND cycle_time = %s
            RETURNING *
            """,
            (status, error_code, error_message, source_id, cycle_time),
        )

    def list_stage_statuses(
        self,
        *,
        source_id: str | None,
        cycle_time: datetime,
        model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        parameters: list[Any] = [cycle_time]
        filters = ["fc.cycle_time = %s"]
        if source_id is not None:
            filters.append("fc.source_id = %s")
            parameters.append(source_id)
        if model_id is not None:
            filters.append("h.model_id = %s")
            parameters.append(model_id)
        return self._fetch_all(
            f"""
            SELECT
                pj.job_id,
                pj.run_id,
                pj.cycle_id,
                pj.job_type,
                pj.slurm_job_id,
                pj.model_id,
                pj.status,
                pj.stage,
                pj.submitted_at,
                pj.started_at,
                pj.finished_at,
                pj.exit_code,
                pj.error_code,
                pj.error_message,
                pj.log_uri
            FROM ops.pipeline_job pj
            JOIN met.forecast_cycle fc ON fc.cycle_id = pj.cycle_id
            LEFT JOIN hydro.hydro_run h ON h.run_id = pj.run_id
            WHERE {" AND ".join(filters)}
            ORDER BY CASE pj.stage
                WHEN 'download' THEN 1
                WHEN 'convert' THEN 2
                WHEN 'forcing' THEN 3
                WHEN 'forecast' THEN 4
                WHEN 'parse' THEN 5
                WHEN 'state_save_qc' THEN 6
                WHEN 'frequency' THEN 7
                WHEN 'publish' THEN 8
                WHEN 'download_gfs' THEN 1
                WHEN 'convert_canonical' THEN 2
                WHEN 'produce_forcing' THEN 3
                WHEN 'run_shud_forecast' THEN 4
                WHEN 'parse_output' THEN 15
                WHEN 'era5_download' THEN 11
                WHEN 'canonical_convert' THEN 12
                WHEN 'forcing_produce' THEN 13
                WHEN 'analysis_run' THEN 14
                WHEN 'state_save_qc' THEN 16
                ELSE 99
            END
            """,
            tuple(parameters),
        )

    def _fetch_one(
        self,
        statement: str,
        parameters: tuple[Any, ...],
        *,
        missing_code: str = "DATABASE_ROW_MISSING",
        missing_message: str = "Database operation did not return a row.",
    ) -> dict[str, Any]:
        row = self._fetch_optional(statement, parameters)
        if row is None:
            raise OrchestratorError(missing_code, missing_message)
        return row

    def _fetch_optional(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
        rows = self._fetch_all(statement, parameters)
        return rows[0] if rows else None

    def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
        try:
            import psycopg2
        except ImportError as error:
            raise OrchestratorError("PSYCOPG2_MISSING", "psycopg2 is required for orchestration.") from error

        connection = None
        try:
            connection = psycopg2.connect(self.database_url)
            connection.autocommit = False
            with connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                if cursor.description is None:
                    connection.commit()
                    return []
                rows = cursor.fetchall()
                columns = [description.name for description in cursor.description]
                connection.commit()
                return [dict(zip(columns, row, strict=True)) for row in rows]
        except psycopg2.Error as error:
            if connection is not None:
                connection.rollback()
            raise OrchestratorError(
                "ORCHESTRATOR_DB_ERROR",
                f"Orchestrator database operation failed: {error}",
            ) from error
        finally:
            if connection is not None:
                connection.close()
