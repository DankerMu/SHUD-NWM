# ruff: noqa: E501,F401,F821,I001
from __future__ import annotations
from services.orchestrator import scheduler as _scheduler


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
        self._reconcile_store = reconcile_store
        self._reconcile_store_build_error: str | None = None
        self._reconcile_comment_query = reconcile_comment_query
        self._reconcile_sacct_query = reconcile_sacct_query
        self.registry = registry if registry is not None else _scheduler.PsycopgModelRegistryStore.from_env()
        self.adapters = dict(adapters if adapters is not None else _scheduler._default_adapters())
        self.active_repository = active_repository
        if (
            canonical_readiness_provider is _scheduler._CANONICAL_READINESS_PROVIDER_UNSET
            or canonical_readiness_provider is None
        ):
            self.canonical_readiness_provider = _scheduler._UnavailableCanonicalReadinessProvider(
                reason="canonical_readiness_provider_absent", dependency="canonical_readiness_provider", retryable=True
            )
        else:
            self.canonical_readiness_provider = canonical_readiness_provider
        self.forcing_producer = forcing_producer
        self.orchestrator_factory = orchestrator_factory
        self.sleep = sleep or _scheduler._sleep
        self._source_readiness_context_cache: dict[tuple[str, str, str], dict[str, _scheduler.Any]] = {}

    @classmethod
    def from_env(cls, config: _scheduler.ProductionSchedulerConfig | None = None) -> _scheduler.ProductionScheduler:
        config = config or _scheduler.ProductionSchedulerConfig()
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

    def _run_restart_reconcile(self) -> dict[str, _scheduler.Any] | None:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime._run_restart_reconcile(self)

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

    def _run_retention(self, started_at: _scheduler.datetime) -> dict[str, _scheduler.Any]:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime._run_retention(self, started_at)

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
        return self._default_orchestrator_for(source_id, state_manager=_scheduler.StateManager.from_env())

    def _cancel_orchestrator_for(self, source_id: str) -> _scheduler.ForecastOrchestrator:
        if self.orchestrator_factory is not None:
            return self.orchestrator_factory(source_id)
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
                slurm_job_type_templates=config.slurm_job_type_templates,
                slurm_env=config.slurm_env,
            )
        return _scheduler.ForecastOrchestrator(
            config=config,
            repository=_scheduler._orchestrator_repository_from_env(),
            state_manager=state_manager,
            retry_service=_scheduler._retry_service_from_env(),
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
        return _scheduler._scheduler_discovery.discover_source_window(
            adapter, source_id=source_id, start_time=start_time, end_time=end_time
        )

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

    def _source_readiness_context(self, cycle: _scheduler.SchedulerSourceCycle) -> dict[str, _scheduler.Any]:
        cache_key = (
            cycle.discovery.source_id,
            _scheduler._ensure_utc(cycle.discovery.cycle_time).isoformat(),
            _scheduler.json.dumps(_scheduler._evidence_safe(cycle.horizon), sort_keys=True, default=str),
        )
        cached = self._source_readiness_context_cache.get(cache_key)
        if cached is not None:
            return cached
        adapter = self.adapters.get(cycle.discovery.source_id)
        forecast_hours = _scheduler._source_forecast_hours(cycle.discovery, adapter, cycle.horizon)
        context = {
            "forecast_hours": forecast_hours,
            "policy_identity": _scheduler._source_policy_identity(cycle.discovery, adapter, forecast_hours),
            "source_object_identity": _scheduler._source_object_identity(cycle.discovery, adapter, forecast_hours),
        }
        self._source_readiness_context_cache[cache_key] = context
        return context

    def _candidate_construction_context(self) -> _scheduler._scheduler_candidates.SchedulerCandidateConstructionContext:
        return _scheduler._scheduler_candidates.SchedulerCandidateConstructionContext(
            config=self.config,
            active_repository=self.active_repository,
            canonical_readiness_for_candidate=self._canonical_readiness_for_candidate,
            orchestrator_for=self._orchestrator_for,
            candidate_factory=_scheduler._candidate_for,
            candidate_state_provider_caller=_scheduler._call_candidate_state_provider,
            active_slurm_jobs_provider_caller=_scheduler._call_active_slurm_jobs_provider,
            active_slurm_jobs_bounder=_scheduler._bounded_active_slurm_jobs,
            candidate_state_decider=_scheduler._candidate_state_decision,
            candidate_state_identity_mismatch_detector=_scheduler._candidate_state_has_identity_mismatch,
            candidate_state_scoped_retry_detector=_scheduler._candidate_state_is_candidate_scoped_retry,
            repaired_state_audit_evidence_builder=_scheduler._candidate_repaired_state_audit_evidence,
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
