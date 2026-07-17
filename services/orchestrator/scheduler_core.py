# ruff: noqa: E501,F401,F821,I001
from __future__ import annotations
from services.orchestrator import scheduler as _scheduler
from services.orchestrator import scheduler_generation as _generation


# Issue #1081 §8.1: sentinel that separates "declaration not yet loaded" from
# "loaded and returned ``None``" (env unset — no declaration configured).
_CUTOVER_DECLARATION_UNLOADED: object = object()


def _db_free_file_registry_from_config(config: _scheduler.ProductionSchedulerConfig) -> _scheduler.ModelRegistryReader:
    from services.orchestrator.scheduler_file_providers import FileSchedulerModelRegistry

    return FileSchedulerModelRegistry(
        str(config.scheduler_registry_manifest),
        object_store_root=config.object_store_root,
        object_store_prefix=_scheduler.os.getenv("OBJECT_STORE_PREFIX"),
        published_artifact_root=config.published_artifact_root,
        now=config.now,
    )


def _db_free_canonical_readiness_provider_from_config(
    config: _scheduler.ProductionSchedulerConfig,
) -> _scheduler.CanonicalReadinessProvider:
    from services.orchestrator.scheduler_file_providers import FileCanonicalReadinessProvider

    return FileCanonicalReadinessProvider(
        str(config.scheduler_canonical_readiness_index),
        object_store_root=config.object_store_root,
        object_store_prefix=_scheduler.os.getenv("OBJECT_STORE_PREFIX"),
        published_artifact_root=config.published_artifact_root,
        now=config.now,
    )


def _db_free_orchestration_repository_from_config(
    config: _scheduler.ProductionSchedulerConfig,
) -> _scheduler.ActiveCandidateRepository:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    return FileOrchestrationJournalRepository(str(config.scheduler_journal_root))


def _db_free_state_manager_from_config(config: _scheduler.ProductionSchedulerConfig) -> _scheduler.StateManager:
    from packages.common.object_store import LocalObjectStore

    repository = _scheduler.FileStateSnapshotIndexRepository(
        str(config.scheduler_state_index),
        object_store_root=config.object_store_root,
        object_store_prefix=_scheduler.os.getenv("OBJECT_STORE_PREFIX"),
        published_artifact_root=config.published_artifact_root,
        now=config.now,
    )
    return _scheduler.StateManager(
        repository=repository,
        object_store=LocalObjectStore(
            config.object_store_root or config.workspace_root,
            object_store_prefix=_scheduler.os.getenv("OBJECT_STORE_PREFIX", ""),
        ),
    )


def _db_free_file_retry_service_from_env(repository: _scheduler.FileOrchestrationJournalRepository) -> _scheduler.Any:
    from services.orchestrator.retry import RetryConfig
    from services.slurm_gateway.config import SlurmGatewaySettings

    return _scheduler.FileJournalRetryService(repository, RetryConfig.from_settings(SlurmGatewaySettings()))


class ProductionScheduler:
    def __init__(
        self,
        config: _scheduler.ProductionSchedulerConfig | None = None,
        *,
        registry: _scheduler.ModelRegistryReader | None = None,
        adapters: _scheduler.Mapping[str, _scheduler.CycleDiscoveryAdapter] | None = None,
        active_repository: _scheduler.ActiveCandidateRepository | None = None,
        canonical_readiness_provider: _scheduler.CanonicalReadinessProvider
        | None
        | object = _scheduler._CANONICAL_READINESS_PROVIDER_UNSET,
        forcing_producer: _scheduler.ForcingProducerRunner | None = None,
        orchestrator_factory: _scheduler.ProductionOrchestratorFactory | None = None,
        sleep: _scheduler.Callable[[float], None] | None = None,
        reconcile_store: _scheduler.Any | None = None,
        reconcile_comment_query: _scheduler.Callable[[str], _scheduler.Any] | None = None,
        reconcile_sacct_query: _scheduler.Callable[[str], _scheduler.Any] | None = None,
    ) -> None:
        self.config = config or _scheduler.ProductionSchedulerConfig()
        db_free_required = bool(getattr(self.config, "db_free_required", False))
        self._reconcile_store = reconcile_store
        self._reconcile_store_build_error: str | None = None
        self._reconcile_comment_query = reconcile_comment_query
        self._reconcile_sacct_query = reconcile_sacct_query
        if db_free_required:
            self.registry = registry if registry is not None else _db_free_file_registry_from_config(self.config)
            self.adapters = dict(adapters if adapters is not None else _scheduler._db_free_default_adapters(self.config))
        else:
            self.registry = registry if registry is not None else _scheduler.PsycopgModelRegistryStore.from_env()
            self.adapters = dict(adapters if adapters is not None else _scheduler._default_adapters())
        if active_repository is not None:
            self.active_repository = active_repository
        elif db_free_required:
            self.active_repository = _db_free_orchestration_repository_from_config(self.config)
        else:
            self.active_repository = None
        if (
            canonical_readiness_provider is _scheduler._CANONICAL_READINESS_PROVIDER_UNSET
            or canonical_readiness_provider is None
        ):
            if db_free_required:
                self.canonical_readiness_provider = _db_free_canonical_readiness_provider_from_config(self.config)
            else:
                self.canonical_readiness_provider = _scheduler._UnavailableCanonicalReadinessProvider(
                    reason="canonical_readiness_provider_absent",
                    dependency="canonical_readiness_provider",
                    retryable=True,
                )
        else:
            self.canonical_readiness_provider = canonical_readiness_provider
        self.forcing_producer = forcing_producer
        self.orchestrator_factory = orchestrator_factory
        self.sleep = sleep or _scheduler._sleep
        self._source_readiness_context_cache: dict[tuple[str, str, str], dict[str, _scheduler.Any]] = {}
        self._db_free_state_index_repository: _scheduler.FileStateSnapshotIndexRepository | None = None
        # SUB-2 (#860): populated by ``run_once`` with the per-pass
        # ``SchedulerPassTiming`` so SUB-3 / SUB-4 can call ``stage_span`` /
        # ``candidate_span`` via ``SchedulerExecutionContext.timing``.
        self._scheduler_pass_timing: _scheduler.Any | None = None
        # Issue #1081 §8.1: registry cutover declaration is loaded once per
        # scheduler pass and cached — D8.1 requires planning-time binding so a
        # mid-plan declaration change cannot corrupt in-flight candidates.
        # Sentinel ``_UNLOADED`` distinguishes "not yet loaded" from "loaded
        # and returned None" (env unset — no declaration configured).
        self._cutover_declaration_cache: _scheduler.Any = _CUTOVER_DECLARATION_UNLOADED

    @classmethod
    def from_env(cls, config: _scheduler.ProductionSchedulerConfig | None = None) -> _scheduler.ProductionScheduler:
        config = config or _scheduler.ProductionSchedulerConfig()
        if config.db_free_required:
            if (
                config.require_runtime_roots
                and _scheduler._scheduler_runtime_root_preflight(config)["status"] == "blocked"
            ):
                return cls(
                    config=config,
                    registry=_scheduler._BlockedModelRegistry(),
                    adapters={},
                    active_repository=None,
                    canonical_readiness_provider=_scheduler._UnavailableCanonicalReadinessProvider(
                        reason="db_free_runtime_root_preflight_blocked",
                        dependency="canonical_readiness_provider",
                        retryable=True,
                    ),
                )
            if config.db_free_runtime_preflight()["status"] == "blocked":
                return cls(
                    config=config,
                    registry=_scheduler._BlockedModelRegistry(),
                    adapters={},
                    active_repository=None,
                    canonical_readiness_provider=_scheduler._UnavailableCanonicalReadinessProvider(
                        reason="db_free_runtime_preflight_blocked",
                        dependency="canonical_readiness_provider",
                        retryable=True,
                    ),
                )
            return cls(config=config)
        if (
            config.require_runtime_roots
            and _scheduler._scheduler_lock_evidence_root_preflight(config)["status"] == "blocked"
        ):
            return cls(config=config, registry=_scheduler._BlockedModelRegistry(), adapters={}, active_repository=None)
        if config.require_runtime_roots and _scheduler._scheduler_runtime_root_preflight(config)["status"] == "blocked":
            return cls(config=config, registry=_scheduler._BlockedModelRegistry(), adapters={}, active_repository=None)
        return cls(
            config=config,
            active_repository=_scheduler._active_repository_from_env(),
            canonical_readiness_provider=_scheduler._canonical_readiness_provider_from_env(),
            forcing_producer=_scheduler._forcing_producer_from_env() if config.forcing_production_enabled else None,
        )

    def run_once(self) -> _scheduler.SchedulerPassResult:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime.run_once(self)

    def _build_scheduler_lease(self) -> _scheduler.Any:
        database_url = (self.config.database_url or "").strip()
        if self.config.db_free_required:
            return _scheduler.FileSchedulerLease(
                _scheduler.Path(self.config.lock_path),
                ttl_seconds=self.config.lock_ttl_seconds,
                workspace_root=_scheduler.Path(self.config.workspace_root),
            )
        if self.config.scheduler_lock_backend == "postgres" and database_url:
            return _scheduler.PostgresSchedulerLease(
                database_url,
                lock_name=f"nhms:production-scheduler:{_scheduler.Path(self.config.lock_path)}",
                display_lock_path=str(self.config.lock_path),
            )
        return _scheduler.FileSchedulerLease(
            _scheduler.Path(self.config.lock_path),
            ttl_seconds=self.config.lock_ttl_seconds,
            workspace_root=_scheduler.Path(self.config.workspace_root),
        )

    def _run_restart_reconcile(
        self,
        *,
        sacct_wait_sink: _scheduler.Callable[[float], None] | None = None,
    ) -> dict[str, _scheduler.Any] | None:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime._run_restart_reconcile(self, sacct_wait_sink=sacct_wait_sink)

    def _reset_reconcile_store_after_error(self) -> None:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime._reset_reconcile_store_after_error(self)

    def _restart_reconcile_store(self) -> _scheduler.Any | None:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime._restart_reconcile_store(self)

    def _restart_reconcile_comment_query(self) -> _scheduler.Callable[[str], _scheduler.Any]:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime._restart_reconcile_comment_query(self)

    def _restart_reconcile_sacct_query(self) -> _scheduler.Callable[[str], _scheduler.Any]:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime._restart_reconcile_sacct_query(self)

    def _run_retention(
        self,
        started_at: _scheduler.datetime,
        *,
        force_dry_run_reason: str | None = None,
    ) -> dict[str, _scheduler.Any]:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime._run_retention(
            self,
            started_at,
            force_dry_run_reason=force_dry_run_reason,
        )

    def _write_prelock_blocked_evidence(
        self, pass_id: str, evidence: dict[str, _scheduler.Any], root_preflight: _scheduler.Mapping[str, _scheduler.Any]
    ) -> _scheduler.Path | None:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime._write_prelock_blocked_evidence(self, pass_id, evidence, root_preflight)

    def _reserve_pre_execution_evidence(
        self, pass_id: str, started_at: _scheduler.datetime, candidate_count: int
    ) -> dict[str, _scheduler.Any]:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime._reserve_pre_execution_evidence(self, pass_id, started_at, candidate_count)

    def _scheduler_evidence_write_context(self) -> _scheduler._scheduler_evidence.SchedulerEvidenceWriteContext:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime._scheduler_evidence_write_context(self)

    def run_continuous(self, *, max_passes: int | None = None) -> list[_scheduler.SchedulerPassResult]:
        if max_passes is not None:
            max_passes = int(max_passes)
            if max_passes < 1:
                raise ValueError("production scheduler max_passes must be at least 1")
            if max_passes > _scheduler.MAX_CONTINUOUS_JSON_PASSES:
                raise ValueError(
                    f"production scheduler max_passes exceeds finite JSON output limit {_scheduler.MAX_CONTINUOUS_JSON_PASSES}"
                )
        results: list[_scheduler.SchedulerPassResult] = []
        completed = 0
        while max_passes is None or completed < max_passes:
            result = self.run_once()
            if max_passes is None:
                results[:] = [result]
            else:
                results.append(result)
            completed += 1
            if max_passes is not None and completed >= max_passes:
                break
            self.sleep(self.config.interval_seconds)
        return results

    def _refresh_db_free_file_providers(self) -> None:
        for provider in (self.registry, self.canonical_readiness_provider, self._db_free_state_index_repository):
            refresh = getattr(provider, "refresh", None)
            if callable(refresh):
                refresh()
        # Issue #1081 §8.1 / D8.1: the declaration reload happens at
        # planning-time.  Clearing the cache here keeps multi-pass runs
        # aligned with a single-pass reload while ``run_once`` remains the
        # planning boundary.
        self._cutover_declaration_cache = _CUTOVER_DECLARATION_UNLOADED

    def _produce_forcing_for_candidates(
        self, candidates: _scheduler.Sequence[_scheduler.SchedulerCandidate]
    ) -> tuple[
        list[_scheduler.SchedulerCandidate], list[_scheduler.SchedulerCandidate], list[dict[str, _scheduler.Any]]
    ]:
        return _scheduler._scheduler_execution.produce_forcing_for_candidates(
            self._scheduler_execution_context(), candidates
        )

    def _execute_candidates(
        self, candidates: _scheduler.Sequence[_scheduler.SchedulerCandidate]
    ) -> list[dict[str, _scheduler.Any]]:
        return _scheduler._scheduler_execution.execute_candidates(self._scheduler_execution_context(), candidates)

    def _execute_candidate_cohort(
        self,
        source_id: str,
        cycle_time: _scheduler.datetime,
        cycle_id: str,
        cycle_candidates: _scheduler.Sequence[_scheduler.SchedulerCandidate],
        *,
        orchestration_run_id: str | None,
    ) -> list[dict[str, _scheduler.Any]]:
        return _scheduler._scheduler_execution.execute_candidate_cohort(
            self._scheduler_execution_context(),
            source_id,
            cycle_time,
            cycle_id,
            cycle_candidates,
            orchestration_run_id=orchestration_run_id,
        )

    def _scheduler_execution_context(self) -> _scheduler._scheduler_execution.SchedulerExecutionContext:
        return _scheduler._scheduler_execution.SchedulerExecutionContext(
            config=self.config,
            forcing_producer=self.forcing_producer,
            orchestrator_for=self._orchestrator_for,
            execute_candidate_cohort=self._execute_candidate_cohort,
            set_last_submit_overlap_receipt=self._set_last_submit_overlap_receipt,
            submit_overlap_receipt_factory=_scheduler.SubmitOverlapReceipt,
            timed_submission=_scheduler.timed_submission,
            run_concurrent_submissions=_scheduler.run_concurrent_submissions,
            cycle_id_for=_scheduler.cycle_id_for,
            restart_compatible_candidate_cohorts=_scheduler._restart_compatible_candidate_cohorts,
            candidate_execution_cohorts=_scheduler._candidate_execution_cohorts,
            candidate_is_fresh_full_chain=_scheduler._candidate_is_fresh_full_chain,
            candidate_max_lead_hours=_scheduler._candidate_max_lead_hours,
            candidate_canonical_product_id=_scheduler._candidate_canonical_product_id,
            candidate_scheduler_canonical_identity=_scheduler._candidate_scheduler_canonical_identity,
            candidate_forcing_blocked_evidence=_scheduler._candidate_forcing_blocked_evidence,
            blocked_candidate=_scheduler._blocked_candidate,
            candidate_with_forcing_result=_scheduler._candidate_with_forcing_result,
            candidate_forcing_ready_evidence=_scheduler._candidate_forcing_ready_evidence,
            candidate_with_state_evidence=_scheduler._candidate_with_state_evidence,
            candidate_output_uri=_scheduler._candidate_output_uri,
            candidate_identity_evidence=_scheduler._candidate_identity_evidence,
            candidate_model_run_review_evidence=_scheduler._candidate_model_run_review_evidence,
            standard_chain_shape=lambda: [stage.stage for stage in _scheduler.ForecastOrchestrator.stages],
            candidate_basin_manifest=_scheduler._candidate_basin_manifest,
            slurm_env_check=_scheduler._slurm_env_check,
            candidate_slurm_preflight_blocked_evidence=_scheduler._candidate_slurm_preflight_blocked_evidence,
            secret_manifest_findings=_scheduler.iter_secret_manifest_findings,
            candidate_secret_manifest_blocked_evidence=_scheduler._candidate_secret_manifest_blocked_evidence,
            slurm_resource_profile_blockers=_scheduler._slurm_resource_profile_blockers,
            evidence_safe=_scheduler._evidence_safe,
            candidate_execution_evidence=_scheduler._candidate_execution_evidence,
            unknown_after_attempt=_scheduler.UNKNOWN_AFTER_ATTEMPT,
            # SUB-2 wiring for scheduler-pass-timing-instrumentation (#860):
            # ``run_once`` stashes the per-pass collector on ``self`` before
            # dispatching execution; SUB-3 / SUB-4 read it from the context.
            timing=getattr(self, "_scheduler_pass_timing", None),
        )

    def _set_last_submit_overlap_receipt(self, receipt: _scheduler.SubmitOverlapReceipt) -> None:
        self._last_submit_overlap_receipt = receipt

    def _cancel_requested_active_slurm(
        self, skipped_candidates: _scheduler.Sequence[dict[str, _scheduler.Any]]
    ) -> list[dict[str, _scheduler.Any]]:
        from services.orchestrator import scheduler_cancellation

        return scheduler_cancellation._cancel_requested_active_slurm(self, skipped_candidates)

    def _orchestrator_for(self, source_id: str) -> _scheduler.ForecastOrchestrator:
        if self.orchestrator_factory is not None:
            return self.orchestrator_factory(source_id)
        if self.config.db_free_required:
            state_manager = _db_free_state_manager_from_config(self.config)
            return self._default_orchestrator_for(source_id, state_manager=state_manager)
        return self._default_orchestrator_for(source_id, state_manager=_scheduler.StateManager.from_env())

    def _cancel_orchestrator_for(self, source_id: str) -> _scheduler.ForecastOrchestrator:
        if self.orchestrator_factory is not None:
            return self.orchestrator_factory(source_id)
        if self.config.db_free_required:
            return self._default_orchestrator_for(source_id, state_manager=None)
        return self._default_orchestrator_for(source_id, state_manager=None)

    def _default_orchestrator_for(
        self, source_id: str, *, state_manager: _scheduler.Any | None
    ) -> _scheduler.ForecastOrchestrator:
        config = _scheduler.OrchestratorConfig.from_env()
        if self.config.slurm_execution_enabled:
            config = _scheduler.OrchestratorConfig(
                workspace_root=self.config.workspace_root,
                object_store_root=self.config.object_store_root or config.object_store_root,
                object_store_prefix=_scheduler.os.getenv("OBJECT_STORE_PREFIX", config.object_store_prefix),
                slurm_gateway_url=config.slurm_gateway_url,
                templates_dir=config.templates_dir,
                poll_interval_seconds=config.poll_interval_seconds,
                job_timeout_seconds=config.job_timeout_seconds,
                source_id=config.source_id,
                forecast_horizon_hours=config.forecast_horizon_hours,
                scenario_id=config.scenario_id if config.scenario_id_explicit else None,
                era5_area=config.era5_area,
                state_soft_stale_threshold_days=config.state_soft_stale_threshold_days,
                state_hard_stale_threshold_days=config.state_hard_stale_threshold_days,
                require_forecast_warm_start=config.require_forecast_warm_start,
                forecast_warm_start_required_from=config.forecast_warm_start_required_from,
                terminal_stage=config.terminal_stage,
                slurm_job_type_templates=dict(self.config.slurm_job_type_templates or {}),
                slurm_env=dict(self.config.slurm_env),
            )
        if config.source_id != source_id:
            config = _scheduler.OrchestratorConfig(
                workspace_root=config.workspace_root,
                object_store_root=config.object_store_root,
                object_store_prefix=config.object_store_prefix,
                slurm_gateway_url=config.slurm_gateway_url,
                templates_dir=config.templates_dir,
                poll_interval_seconds=config.poll_interval_seconds,
                job_timeout_seconds=config.job_timeout_seconds,
                source_id=source_id,
                forecast_horizon_hours=config.forecast_horizon_hours,
                scenario_id=_scheduler.scenario_for_source(source_id),
                era5_area=config.era5_area,
                state_soft_stale_threshold_days=config.state_soft_stale_threshold_days,
                state_hard_stale_threshold_days=config.state_hard_stale_threshold_days,
                require_forecast_warm_start=config.require_forecast_warm_start,
                forecast_warm_start_required_from=config.forecast_warm_start_required_from,
                terminal_stage=config.terminal_stage,
                slurm_job_type_templates=config.slurm_job_type_templates,
                slurm_env=config.slurm_env,
            )
        return _scheduler.ForecastOrchestrator(
            config=config,
            repository=(
                self.active_repository
                if self.config.db_free_required and self.active_repository is not None
                else _scheduler._orchestrator_repository_from_env()
            ),
            state_manager=state_manager,
            retry_service=(
                _db_free_file_retry_service_from_env(self.active_repository)
                if self.config.db_free_required
                and isinstance(self.active_repository, _scheduler.FileOrchestrationJournalRepository)
                else None
                if self.config.db_free_required
                else _scheduler._retry_service_from_env()
            ),
        )

    def _base_evidence(self, pass_id: str, started_at: _scheduler.datetime) -> dict[str, _scheduler.Any]:
        return _scheduler._scheduler_evidence.base_evidence(
            self.config,
            pass_id,
            started_at,
            resolved_runtime_roots=_scheduler._scheduler_resolved_runtime_roots,
            runtime_config_evidence=_scheduler._scheduler_runtime_config_evidence,
        )

    def _discover_models(self) -> tuple[list[_scheduler.RegisteredSchedulerModel], dict[str, _scheduler.Any]]:
        return _scheduler._scheduler_models.discover_models(self)

    def _discovery_context(self) -> _scheduler._scheduler_discovery.SchedulerDiscoveryContext:
        return _scheduler._scheduler_discovery.SchedulerDiscoveryContext(
            config=self.config,
            adapters=self.adapters,
            active_repository=self.active_repository,
            floor_to_source_cycle_boundary=lambda value, _sources: _scheduler._floor_to_source_cycle_boundary(
                value, _sources, allowed_cycle_hours_utc=self.config.allowed_cycle_hours_utc
            ),
            source_horizon_metadata=_scheduler._source_horizon_metadata,
            candidate_factory=_scheduler._candidate_for,
            candidate_state_provider_caller=_scheduler._call_candidate_state_provider,
            candidate_state_decider=_scheduler._candidate_state_decision,
            strict_warm_start_for_candidate=self._strict_warm_start_for_candidate,
            successor_state_for_candidate=self._successor_warm_start_state_for_candidate,
            discover_source_window_provider=self._discover_source_window,
            cycle_completion_status_provider=self._cycle_completion_status,
        )

    def _cycle_completion_status(
        self,
        discovery: _scheduler.CycleDiscovery,
        models: _scheduler.Sequence[_scheduler.RegisteredSchedulerModel],
        *,
        horizon: _scheduler.Mapping[str, _scheduler.Any] | None = None,
    ) -> str:
        return _scheduler._scheduler_discovery.cycle_completion_status(
            self._discovery_context(), discovery, models, horizon=horizon
        )

    def _discover_cycles(
        self, started_at: _scheduler.datetime, models: _scheduler.Sequence[_scheduler.RegisteredSchedulerModel] = ()
    ) -> tuple[list[_scheduler.SchedulerSourceCycle], list[dict[str, _scheduler.Any]]]:
        return _scheduler._scheduler_discovery.discover_cycles(self._discovery_context(), started_at, models=models)

    def _discover_source_window(
        self,
        adapter: _scheduler.CycleDiscoveryAdapter,
        *,
        source_id: str,
        start_time: _scheduler.datetime,
        end_time: _scheduler.datetime,
    ) -> list[_scheduler.CycleDiscovery]:
        nfs_raw_discoveries = self._discover_source_window_from_nfs_raw_manifest(
            source_id=source_id,
            start_time=start_time,
            end_time=end_time,
        )
        if nfs_raw_discoveries is not None:
            return nfs_raw_discoveries
        return _scheduler._scheduler_discovery.discover_source_window(
            adapter, source_id=source_id, start_time=start_time, end_time=end_time
        )

    def _discover_source_window_from_nfs_raw_manifest(
        self,
        *,
        source_id: str,
        start_time: _scheduler.datetime,
        end_time: _scheduler.datetime,
    ) -> list[_scheduler.CycleDiscovery] | None:
        from services.orchestrator import source_cycle_raw_manifest
        from services.orchestrator.scheduler_file_providers import _public_raw_manifest_evidence

        enabled = _scheduler._env_flag(source_cycle_raw_manifest.NFS_RAW_MANIFEST_ENABLED_ENV)
        required = _scheduler._env_flag(source_cycle_raw_manifest.NFS_RAW_MANIFEST_REQUIRED_ENV)
        if not enabled and not required:
            return None

        start = _scheduler._ensure_utc(start_time)
        end = _scheduler._ensure_utc(end_time)
        if start > end:
            return []

        allowed_hours = sorted({int(hour) for hour in self.config.allowed_cycle_hours_utc if int(hour) in {0, 12}})
        discoveries: list[_scheduler.CycleDiscovery] = []
        current_date = start.date()
        while current_date <= end.date():
            for cycle_hour in allowed_hours:
                cycle_time = _scheduler.datetime(
                    current_date.year,
                    current_date.month,
                    current_date.day,
                    cycle_hour,
                    tzinfo=_scheduler.UTC,
                )
                if cycle_time < start or cycle_time > end:
                    continue
                readiness = source_cycle_raw_manifest.nfs_raw_manifest_readiness_from_env(source_id, cycle_time)
                if readiness is None:
                    return None
                status = str(readiness.get("status") or "missing")
                ready = status == "ready"
                raw_reason = readiness.get("reason")
                reason = None if ready else f"nfs_raw_manifest_{raw_reason or 'not_ready'}"
                discoveries.append(
                    _scheduler.CycleDiscovery(
                        cycle_id=_scheduler.cycle_id_for(source_id, cycle_time),
                        source_id=source_id,
                        cycle_time=cycle_time,
                        cycle_hour=cycle_hour,
                        available=ready,
                        status="discovered" if ready else status,
                        reason=reason,
                        classifier=source_cycle_raw_manifest.NFS_RAW_MANIFEST_READY_SOURCE,
                        retryable=False if ready else True,
                        probe_uri=None,
                        evidence=_public_raw_manifest_evidence(readiness),
                    )
                )
            current_date += _scheduler.timedelta(days=1)
        return discoveries

    def _canonical_readiness_for_candidate(
        self, candidate: _scheduler.SchedulerCandidate, cycle: _scheduler.SchedulerSourceCycle
    ) -> dict[str, _scheduler.Any] | None:
        provider = self.canonical_readiness_provider
        if provider is None:
            return None
        context = self._source_readiness_context(cycle)
        forecast_hours = list(context["forecast_hours"])
        policy_identity = dict(context["policy_identity"])
        source_object_identity = dict(context["source_object_identity"])
        try:
            readiness = provider.canonical_readiness(
                source_id=cycle.discovery.source_id,
                cycle_time=cycle.discovery.cycle_time,
                forecast_hours=forecast_hours,
                policy_identity=policy_identity,
                source_object_identity=source_object_identity,
                canonical_product_id=_scheduler._candidate_canonical_product_id(candidate),
                model_id=candidate.model_id,
                basin_id=candidate.basin_id,
            )
        except Exception as error:
            readiness = _scheduler._canonical_readiness_unavailable_evidence(
                cycle.discovery,
                candidate,
                forecast_hours=forecast_hours,
                policy_identity=policy_identity,
                source_object_identity=source_object_identity,
                reason="canonical_readiness_query_failed",
                dependency="canonical_readiness_provider",
                error=error,
                retryable=True,
            )
        evidence = dict(readiness)
        evidence.setdefault("source", cycle.discovery.source_id)
        evidence.setdefault("cycle_time", _scheduler._format_utc(cycle.discovery.cycle_time))
        evidence.setdefault("canonical_product_id", _scheduler._candidate_canonical_product_id(candidate))
        evidence.setdefault("model_id", candidate.model_id)
        evidence.setdefault("basin_id", candidate.basin_id)
        evidence.setdefault("policy_identity", policy_identity)
        evidence.setdefault("source_object_identity", source_object_identity)
        evidence.setdefault("accepted_horizon", _scheduler._accepted_horizon_from_hours(forecast_hours))
        return _scheduler._evidence_safe(evidence)

    def _db_free_strict_warm_start_required(self) -> bool:
        return bool(
            self.config.db_free_required
            and _scheduler.OrchestratorConfig.from_env().require_forecast_warm_start
        )

    def _db_free_strict_warm_start_required_for(
        self,
        candidate: _scheduler.SchedulerCandidate,
    ) -> bool:
        if not self.config.db_free_required:
            return False
        return _scheduler.OrchestratorConfig.from_env().strict_forecast_warm_start_required_for(
            candidate.cycle_time_utc
        )

    def _db_free_state_index_provider(self) -> _scheduler.FileStateSnapshotIndexRepository:
        if self._db_free_state_index_repository is None:
            self._db_free_state_index_repository = _scheduler.FileStateSnapshotIndexRepository(
                str(self.config.scheduler_state_index),
                object_store_root=self.config.object_store_root,
                object_store_prefix=_scheduler.os.getenv("OBJECT_STORE_PREFIX"),
                published_artifact_root=self.config.published_artifact_root,
                now=self.config.now,
            )
        return self._db_free_state_index_repository

    def _load_cutover_declaration(self) -> _scheduler.Any:
        """Return the parsed cutover declaration cached for this scheduler.

        Issue #1081 §8.1 / D8.1: declaration loading happens once at
        planning time.  A subsequent env change during the same
        ``ProductionScheduler`` lifetime is deliberately NOT observed — the
        scheduler must operate against a single stable declaration snapshot.
        """
        if self._cutover_declaration_cache is _CUTOVER_DECLARATION_UNLOADED:
            env_path = _scheduler.os.getenv(_generation.CUTOVER_DECLARATION_ENV) or None
            self._cutover_declaration_cache = _generation.load_cutover_declaration(
                env_path,
                now=self.config.now,
            )
        return self._cutover_declaration_cache

    def _evaluate_transition_decision(
        self,
        candidate: _scheduler.SchedulerCandidate,
        cycle: _scheduler.SchedulerSourceCycle,
        *,
        required_lead_hours: int,
        package_checksum: str | None,
    ) -> _generation.TransitionEvaluation | None:
        """Run the §8 generation-aware transition decision matrix for one candidate.

        Returns ``None`` when the state-index history signal is not ready
        (e.g. corrupt / unreadable index) so the caller can defer to the
        existing ``strict_warm_start_evidence`` path — that path surfaces the
        precise malformed / unavailable index reason and preserves the
        pre-§8 blocker semantics.  §8's new admit decisions cannot fire
        without a trustworthy history read.
        """
        declaration = self._load_cutover_declaration()
        candidate_time = _scheduler._ensure_utc(candidate.cycle_time_utc)
        expected_predecessor_cycle_id = _scheduler.cycle_id_for(
            candidate.source_id,
            candidate_time - _scheduler.timedelta(hours=required_lead_hours),
        )
        history_signal_evidence = self._db_free_state_index_provider().generation_scoped_history_signal(
            model_id=candidate.model_id,
            source_id=candidate.source_id,
            before_time=candidate_time,
            current_package_checksum=package_checksum,
            expected_predecessor_cycle_id=expected_predecessor_cycle_id,
            expected_predecessor_lead_hours=required_lead_hours,
        )
        if not bool(history_signal_evidence.get("ready")):
            return None
        signal = _generation._HistorySignal(
            exists_current_generation=bool(
                history_signal_evidence.get("history_exists_current_generation")
            ),
            exists_any_generation=bool(
                history_signal_evidence.get("history_exists_any_generation")
            ),
            latest_current_generation_checkpoint=history_signal_evidence.get(
                "latest_current_generation_checkpoint"
            ),
            latest_any_generation_checkpoint=history_signal_evidence.get(
                "latest_any_generation_checkpoint"
            ),
        )
        return _generation.evaluate_transition_decision(
            model_id=candidate.model_id,
            package_checksum=package_checksum,
            source_id=candidate.source_id,
            candidate_cycle_time_utc=candidate_time,
            required_lead_hours=required_lead_hours,
            history=signal,
            declaration=declaration,
        )

    def _legacy_strict_warm_start_evidence(
        self,
        candidate: _scheduler.SchedulerCandidate,
        *,
        required_lead_hours: int,
        package_checksum: str | None,
    ) -> dict[str, _scheduler.Any] | None:
        """Pre-§8 strict-warm-start evidence path.

        Used when the state-index history signal cannot be trusted (corrupt,
        unreadable, or missing index).  The output is byte-identical to the
        original flow so the existing corrupt-index / stale-index / missing
        exact-checkpoint regression tests continue to pass.
        """
        evidence = self._db_free_state_index_provider().strict_warm_start_evidence(
            model_id=candidate.model_id,
            source_id=candidate.source_id,
            valid_time=candidate.cycle_time_utc,
            model_package_version=candidate.model_package_uri,
            model_package_checksum=package_checksum,
            required_lead_hours=required_lead_hours,
        )
        if self._db_free_strict_warm_start_required_for(candidate):
            return evidence
        if bool(evidence.get("ready")):
            evidence["mode"] = "db_free_exact_warm_start"
            return evidence
        if str(evidence.get("reason") or "") != "state_snapshot_index_exact_checkpoint_missing":
            evidence["mode"] = "db_free_state_continuity"
            return evidence
        history = self._db_free_state_index_provider().usable_state_history_evidence(
            model_id=candidate.model_id,
            source_id=candidate.source_id,
            before_time=candidate.cycle_time_utc,
        )
        if not bool(history.get("ready")):
            history["mode"] = "db_free_state_continuity"
            return history
        if not bool(history.get("history_exists")):
            return None
        producer_cycle_time = _scheduler._ensure_utc(candidate.cycle_time_utc) - _scheduler.timedelta(
            hours=required_lead_hours
        )
        return _scheduler._evidence_safe(
            {
                **dict(evidence),
                "status": "blocked",
                "ready": False,
                "reason": "state_snapshot_index_prior_checkpoint_missing_after_history",
                "mode": "db_free_state_continuity",
                "required_lead_hours": required_lead_hours,
                "required_prior_cycle_time": _scheduler._format_utc(producer_cycle_time),
                "required_prior_cycle_id": _scheduler.cycle_id_for(candidate.source_id, producer_cycle_time),
                "continuity_policy": {
                    "decision": "block_or_backfill_prior_cycle",
                    "first_cold_seed_allowed": False,
                    "history_required_exact_successor": True,
                },
                "state_history": history,
                "failure": {
                    "classifier": "file_state_snapshot_index_unavailable",
                    "reason_code": "STATE_SNAPSHOT_INDEX_PRIOR_CHECKPOINT_MISSING_AFTER_HISTORY",
                    "dependency": "file_state_snapshot_index",
                    "retryable": True,
                    "permanent": False,
                },
            }
        )

    def _forecast_warm_start_env_enabled(self) -> bool:
        """Return True when ``NHMS_REQUIRE_FORECAST_WARM_START`` is set truthy.

        Unlike ``_db_free_strict_warm_start_required_for`` this is a plain
        env-level flag check — it does not consider
        ``NHMS_FORECAST_WARM_START_REQUIRED_FROM``.  This is intentional: the
        Issue #1081 §8 preflight for completed cycles is a compat-mode toggle
        (env=false → preserve pre-§8 terminal-skip flow; env=true → emit §8
        evidence for auditability even for cycles rolled out before
        ``required_from``).  See D8.9 alignment in
        ``_strict_warm_start_for_candidate``.
        """
        try:
            return bool(_scheduler.OrchestratorConfig.from_env().require_forecast_warm_start)
        except Exception:
            return False

    def _candidate_pipeline_already_complete(
        self, candidate: _scheduler.SchedulerCandidate
    ) -> bool:
        """Check whether the active repository already has a completed pipeline.

        Returns False if the active repository is missing or does not expose
        ``has_completed_pipeline``; any exception is swallowed and treated as
        "not complete" so this preflight never fails-closed on a probe error.
        """
        active_repo = getattr(self, "active_repository", None)
        provider = getattr(active_repo, "has_completed_pipeline", None) if active_repo is not None else None
        if not callable(provider):
            return False
        try:
            return bool(
                provider(
                    source_id=candidate.source_id,
                    cycle_time=candidate.cycle_time_utc,
                    model_id=candidate.model_id,
                )
            )
        except Exception:
            return False

    def _strict_warm_start_for_candidate(
        self, candidate: _scheduler.SchedulerCandidate, cycle: _scheduler.SchedulerSourceCycle
    ) -> dict[str, _scheduler.Any] | None:
        if not self.config.db_free_required:
            return None
        # Issue #1081 §8 preflight: if the active journal / pipeline repository
        # already recognises a completed pipeline for this exact
        # (source_id, cycle_time, model_id) AND the deployment is running
        # in warm-start-not-required (compat) mode, defer to the pre-§8
        # legacy path (return ``None``) so the existing terminal-skip flow in
        # ``scheduler_candidates.py`` resolves the completed cycle to
        # terminal_hydro_success / terminal_pipeline_success as before.
        # Admitting or blocking a cutover for a cycle that is already durably
        # complete is a no-op in terms of new work — the candidate is not
        # going to be submitted — so this preflight preserves the pre-§8
        # completed-cycle evidence shape without weakening §8's gating for
        # admittable-new work.
        #
        # D8.9 alignment: this preflight is gated by
        # ``NHMS_REQUIRE_FORECAST_WARM_START`` being FALSE.  When the operator
        # has opted into strict-warm-start (env=true), §8 evidence still
        # fires — see ``test_db_free_strict_warm_start_resubmits_completed_cold_start_terminal``
        # and ``test_db_free_strict_warm_start_reopens_completed_producer_missing_successor_checkpoint``
        # which assert cold_new_model + terminal-retry evidence for the
        # strict path.  The env cannot ADMIT a declaration-less cutover /
        # missing predecessor / wrong-generation checkpoint — it only decides
        # whether the completed-cycle evidence shape follows pre-§8 or §8.
        if (
            not self._forecast_warm_start_env_enabled()
            and self._candidate_pipeline_already_complete(candidate)
        ):
            return None
        required_lead_hours = self._required_warm_start_lead_hours(candidate, cycle)
        model_package_checksum = (
            candidate.resource_profile.get("package_checksum")
            or candidate.resource_profile.get("model_package_checksum")
        )
        checksum_str = (
            str(model_package_checksum) if model_package_checksum not in (None, "") else None
        )

        # Issue #1081 §8: run the generation-aware transition decision BEFORE
        # the existing exact-warm-start check.  D8.9 requires this to gate
        # regardless of ``NHMS_REQUIRE_FORECAST_WARM_START`` — the env can
        # only weaken *warm-start hints*, never admit a declaration-less
        # cutover / missing predecessor / wrong-generation checkpoint.
        #
        # If the candidate does not carry a registry ``package_checksum`` we
        # cannot compute a generation identity for §8 gating; fall through
        # to the legacy strict-warm-start path when no declaration is
        # configured either, preserving pre-§8 behavior for callers whose
        # model rows omit the checksum from ``resource_profile``.  When a
        # declaration IS configured, the transition matrix still runs and
        # will surface ``block_declaration_stale`` — we cannot admit a
        # declared cutover without a verifiable candidate identity.
        if checksum_str is None and self._load_cutover_declaration() is None:
            return self._legacy_strict_warm_start_evidence(
                candidate,
                required_lead_hours=required_lead_hours,
                package_checksum=checksum_str,
            )
        transition = self._evaluate_transition_decision(
            candidate,
            cycle,
            required_lead_hours=required_lead_hours,
            package_checksum=checksum_str,
        )
        if transition is None:
            # State-index unavailable / corrupt — the existing
            # strict_warm_start_evidence path (below) will emit the precise
            # index-level typed reason.  Skip §8 evidence attachment because
            # we cannot trust the history signal.
            return self._legacy_strict_warm_start_evidence(
                candidate,
                required_lead_hours=required_lead_hours,
                package_checksum=checksum_str,
            )
        transition_evidence = _generation.generation_evidence(transition)

        if transition.decision == _generation.TransitionDecision.COLD_NEW_MODEL:
            return _scheduler._evidence_safe(
                {
                    "status": "ready",
                    "ready": True,
                    "reason": None,
                    "mode": "db_free_cold_new_model",
                    "model_id": candidate.model_id,
                    "source_id": candidate.source_id,
                    "generation": transition.generation,
                    "cold_start_reason": transition.cold_start_reason,
                    "registry_cutover_transition": transition_evidence,
                }
            )
        if transition.decision == _generation.TransitionDecision.COLD_DECLARED_CUTOVER:
            return _scheduler._evidence_safe(
                {
                    "status": "ready",
                    "ready": True,
                    "reason": None,
                    "mode": "db_free_cold_declared_cutover",
                    "model_id": candidate.model_id,
                    "source_id": candidate.source_id,
                    "generation": transition.generation,
                    "cold_start_reason": transition.cold_start_reason,
                    "registry_cutover_transition": transition_evidence,
                }
            )
        # Declaration-level block decisions have no additional information
        # beyond the transition matrix — emit them directly.  Predecessor
        # pending falls through to the existing strict_warm_start_evidence
        # path so the precise field-level reason (lead-hours mismatch, object
        # missing, checksum mismatch, etc.) is preserved for operators.
        _DECLARATION_LEVEL_BLOCKS = frozenset(
            {
                _generation.TransitionDecision.BLOCK_DECLARATION_MISSING,
                _generation.TransitionDecision.BLOCK_DECLARATION_STALE,
                _generation.TransitionDecision.BLOCK_COLD_START_OUT_OF_WINDOW,
                _generation.TransitionDecision.BLOCK_WRONG_GENERATION,
            }
        )
        if transition.decision in _DECLARATION_LEVEL_BLOCKS:
            producer_cycle_time = _scheduler._ensure_utc(candidate.cycle_time_utc) - _scheduler.timedelta(
                hours=required_lead_hours
            )
            return _scheduler._evidence_safe(
                {
                    "status": "blocked",
                    "ready": False,
                    "reason": transition.typed_reason,
                    "mode": "db_free_registry_cutover_transition",
                    "model_id": candidate.model_id,
                    "source_id": candidate.source_id,
                    "generation": transition.generation,
                    "registry_cutover_transition": transition_evidence,
                    "required_lead_hours": required_lead_hours,
                    "required_prior_cycle_time": _scheduler._format_utc(producer_cycle_time),
                    "required_prior_cycle_id": _scheduler.cycle_id_for(
                        candidate.source_id, producer_cycle_time
                    ),
                    "selected_predecessor": transition.selected_predecessor,
                    "failure": {
                        "classifier": "registry_cutover_transition_blocked",
                        "reason_code": (transition.typed_reason or "").upper(),
                        "dependency": "registry_cutover_transition",
                        "retryable": False,
                        "permanent": False,
                    },
                }
            )

        # warm_continue AND block_predecessor_pending: fall through to
        # the existing exact-warm-start check
        # so we still validate the object exists, checksum matches, lineage
        # ties, etc.  Attach the transition summary to whichever evidence the
        # existing check returns so audit can trace the decision.
        evidence = self._db_free_state_index_provider().strict_warm_start_evidence(
            model_id=candidate.model_id,
            source_id=candidate.source_id,
            valid_time=candidate.cycle_time_utc,
            model_package_version=candidate.model_package_uri,
            model_package_checksum=checksum_str,
            required_lead_hours=required_lead_hours,
        )
        evidence["generation"] = transition.generation
        evidence["registry_cutover_transition"] = transition_evidence
        if self._db_free_strict_warm_start_required_for(candidate):
            return evidence
        if bool(evidence.get("ready")):
            evidence["mode"] = "db_free_exact_warm_start"
            return evidence
        if str(evidence.get("reason") or "") != "state_snapshot_index_exact_checkpoint_missing":
            evidence["mode"] = "db_free_state_continuity"
            return evidence

        history = self._db_free_state_index_provider().usable_state_history_evidence(
            model_id=candidate.model_id,
            source_id=candidate.source_id,
            before_time=candidate.cycle_time_utc,
        )
        if not bool(history.get("ready")):
            history["mode"] = "db_free_state_continuity"
            history["registry_cutover_transition"] = transition_evidence
            return history
        # NOTE: In warm_continue, current-generation history exists by
        # definition — the exact predecessor was just observed by the
        # generation-scoped history signal.  If ``strict_warm_start_evidence``
        # then says the exact match is missing, it means the object failed
        # verification (checksum / usable_flag / lineage) — we fall through
        # to the same block-with-prior-checkpoint reason as before so the
        # public reason string stays stable.
        if not bool(history.get("history_exists")):
            # Should not happen for warm_continue; keep the existing
            # cold-seed passthrough as a defensive fallback for other paths.
            return None
        producer_cycle_time = _scheduler._ensure_utc(candidate.cycle_time_utc) - _scheduler.timedelta(
            hours=required_lead_hours
        )
        return _scheduler._evidence_safe(
            {
                **dict(evidence),
                "status": "blocked",
                "ready": False,
                "reason": "state_snapshot_index_prior_checkpoint_missing_after_history",
                "mode": "db_free_state_continuity",
                "generation": transition.generation,
                "registry_cutover_transition": transition_evidence,
                "required_lead_hours": required_lead_hours,
                "required_prior_cycle_time": _scheduler._format_utc(producer_cycle_time),
                "required_prior_cycle_id": _scheduler.cycle_id_for(candidate.source_id, producer_cycle_time),
                "continuity_policy": {
                    "decision": "block_or_backfill_prior_cycle",
                    "first_cold_seed_allowed": False,
                    "history_required_exact_successor": True,
                },
                "state_history": history,
                "failure": {
                    "classifier": "file_state_snapshot_index_unavailable",
                    "reason_code": "STATE_SNAPSHOT_INDEX_PRIOR_CHECKPOINT_MISSING_AFTER_HISTORY",
                    "dependency": "file_state_snapshot_index",
                    "retryable": True,
                    "permanent": False,
                },
            }
        )

    def _required_warm_start_lead_hours(
        self,
        candidate: _scheduler.SchedulerCandidate,
        cycle: _scheduler.SchedulerSourceCycle,
    ) -> int:
        candidate_time = _scheduler._ensure_utc(candidate.cycle_time_utc)
        producer_cycle_time = _scheduler._floor_to_source_cycle_boundary(
            candidate_time - _scheduler.timedelta(microseconds=1),
            (cycle.discovery.source_id,),
            allowed_cycle_hours_utc=self.config.allowed_cycle_hours_utc,
        )
        elapsed_seconds = int((candidate_time - producer_cycle_time).total_seconds())
        if elapsed_seconds <= 0:
            return 12
        return max(1, int(round(elapsed_seconds / 3600.0)))

    def _successor_warm_start_state_for_candidate(
        self,
        candidate: _scheduler.SchedulerCandidate,
        cycle: _scheduler.SchedulerSourceCycle,
    ) -> dict[str, _scheduler.Any] | None:
        del cycle
        if not self.config.db_free_required:
            return None
        orchestrator_config = _scheduler.OrchestratorConfig.from_env()
        candidate_time = _scheduler._ensure_utc(candidate.cycle_time_utc)
        successor_time = self._next_allowed_cycle_time(candidate_time)
        if successor_time is None:
            return None
        lead_hours = max(1, int(round((successor_time - candidate_time).total_seconds() / 3600.0)))
        model_package_checksum = (
            candidate.resource_profile.get("package_checksum")
            or candidate.resource_profile.get("model_package_checksum")
        )
        state_index = self._db_free_state_index_provider()
        strict_required = (
            orchestrator_config.require_forecast_warm_start
            and orchestrator_config.strict_forecast_warm_start_required_for(successor_time)
        )
        if not strict_required:
            history = state_index.usable_state_history_evidence(
                model_id=candidate.model_id,
                source_id=candidate.source_id,
                before_time=successor_time,
            )
            if not bool(history.get("ready")):
                history["mode"] = "db_free_successor_state_continuity"
                history["producer_cycle_time"] = _scheduler._format_utc(candidate_time)
                history["successor_cycle_time"] = _scheduler._format_utc(successor_time)
                history["required_lead_hours"] = lead_hours
                return _scheduler._evidence_safe(history)
            if not bool(history.get("history_exists")):
                return None
        evidence = self._db_free_state_index_provider().strict_warm_start_evidence(
            model_id=candidate.model_id,
            source_id=candidate.source_id,
            valid_time=successor_time,
            model_package_version=candidate.model_package_uri,
            model_package_checksum=str(model_package_checksum) if model_package_checksum not in (None, "") else None,
            required_lead_hours=lead_hours,
        )
        evidence["mode"] = "strict_warm_start_successor_checkpoint"
        evidence["producer_cycle_time"] = _scheduler._format_utc(candidate_time)
        evidence["successor_cycle_time"] = _scheduler._format_utc(successor_time)
        evidence["required_lead_hours"] = lead_hours
        return evidence

    def _next_allowed_cycle_time(self, cycle_time: _scheduler.datetime) -> _scheduler.datetime | None:
        base = _scheduler._ensure_utc(cycle_time).replace(minute=0, second=0, microsecond=0)
        allowed_hours = sorted({int(hour) % 24 for hour in self.config.allowed_cycle_hours_utc})
        if not allowed_hours:
            return None
        for day_offset in range(0, 3):
            day_base = (base + _scheduler.timedelta(days=day_offset)).replace(hour=0)
            for hour in allowed_hours:
                candidate = day_base + _scheduler.timedelta(hours=hour)
                if candidate > cycle_time:
                    return candidate
        return None

    def _source_readiness_context(self, cycle: _scheduler.SchedulerSourceCycle) -> dict[str, _scheduler.Any]:
        from services.orchestrator import source_cycle_raw_manifest

        nfs_raw_readiness = source_cycle_raw_manifest.nfs_raw_manifest_readiness_from_env(
            cycle.discovery.source_id,
            cycle.discovery.cycle_time,
        )
        nfs_source_object_identity = None
        nfs_policy_identity = None
        if isinstance(nfs_raw_readiness, _scheduler.Mapping):
            nfs_source_object_identity = source_cycle_raw_manifest.source_object_identity_from_raw_manifest_readiness(
                nfs_raw_readiness
            )
            nfs_policy_identity = source_cycle_raw_manifest.source_policy_from_raw_manifest_readiness(nfs_raw_readiness)
        cache_key = (
            cycle.discovery.source_id,
            _scheduler._ensure_utc(cycle.discovery.cycle_time).isoformat(),
            _scheduler.json.dumps(_scheduler._evidence_safe(cycle.horizon), sort_keys=True, default=str),
            _scheduler.json.dumps(nfs_source_object_identity or {}, sort_keys=True, default=str),
            _scheduler.json.dumps(nfs_policy_identity or {}, sort_keys=True, default=str),
        )
        cached = self._source_readiness_context_cache.get(cache_key)
        if cached is not None:
            return cached
        adapter = self.adapters.get(cycle.discovery.source_id)
        forecast_hours = _scheduler._source_forecast_hours(cycle.discovery, adapter, cycle.horizon)
        context = {
            "forecast_hours": forecast_hours,
            "policy_identity": nfs_policy_identity
            or _scheduler._source_policy_identity(cycle.discovery, adapter, forecast_hours),
            "source_object_identity": nfs_source_object_identity
            or _scheduler._source_object_identity(cycle.discovery, adapter, forecast_hours),
        }
        self._source_readiness_context_cache[cache_key] = context
        return context

    def _candidate_construction_context(self) -> _scheduler._scheduler_candidates.SchedulerCandidateConstructionContext:
        return _scheduler._scheduler_candidates.SchedulerCandidateConstructionContext(
            config=self.config,
            active_repository=self.active_repository,
            canonical_readiness_for_candidate=self._canonical_readiness_for_candidate,
            strict_warm_start_for_candidate=self._strict_warm_start_for_candidate,
            orchestrator_for=self._orchestrator_for,
            candidate_factory=_scheduler._candidate_for,
            candidate_state_provider_caller=_scheduler._call_candidate_state_provider,
            active_slurm_jobs_provider_caller=_scheduler._call_active_slurm_jobs_provider,
            active_slurm_jobs_bounder=_scheduler._bounded_active_slurm_jobs,
            candidate_state_decider=_scheduler._candidate_state_decision,
            candidate_state_identity_mismatch_detector=_scheduler._candidate_state_has_identity_mismatch,
            candidate_state_scoped_retry_detector=_scheduler._candidate_state_is_candidate_scoped_retry,
            repaired_state_audit_evidence_builder=_scheduler._candidate_repaired_state_audit_evidence,
            successor_state_for_candidate=self._successor_warm_start_state_for_candidate,
            max_candidates=_scheduler.MAX_CANDIDATES,
        )

    def _build_candidates(
        self,
        *,
        models: _scheduler.Sequence[_scheduler.RegisteredSchedulerModel],
        cycles: _scheduler.Sequence[_scheduler.SchedulerSourceCycle],
        allow_slurm_status_sync: bool = False,
    ) -> tuple[
        list[_scheduler.SchedulerCandidate],
        list[_scheduler.SchedulerCandidate],
        list[dict[str, _scheduler.Any]],
        list[dict[str, _scheduler.Any]],
        list[dict[str, _scheduler.Any]],
    ]:
        return _scheduler._scheduler_candidates.build_candidates(
            self._candidate_construction_context(),
            models=models,
            cycles=cycles,
            allow_slurm_status_sync=allow_slurm_status_sync,
        )

    def _write_evidence(
        self, pass_id: str, evidence: _scheduler.Mapping[str, _scheduler.Any]
    ) -> _scheduler.Path | None:
        return _scheduler._scheduler_evidence.write_evidence(
            self._scheduler_evidence_write_context(), pass_id, evidence
        )
