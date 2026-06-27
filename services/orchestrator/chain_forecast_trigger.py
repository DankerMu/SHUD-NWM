from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from packages.common.redaction import redact_payload
from packages.common.source_identity import normalize_source_id
from services.orchestrator import chain as _chain
from services.orchestrator.chain_stages import LEGACY_FORECAST_STAGES
from services.orchestrator.chain_types import OrchestratorError, PipelineResult
from workers.canonical_converter.converter import evaluate_canonical_readiness, expected_converter_version
from workers.data_adapters.base import cycle_id_for, format_cycle_time, parse_cycle_time

CANONICAL_DEMOTE_CYCLE_STATUS = "raw_complete"

__all__ = (
    "demote_stale_canonical_cycle",
    "has_completed_forecast",
    "list_canonical_ready_cycles",
    "list_forecast_model_ids",
    "stage_statuses",
    "trigger_forecast",
    "trigger_forecast_from_canonical",
    "trigger_ready_forecasts",
    "trigger_forecast_impl",
    "validate_auto_trigger_canonical_readiness",
)


def _completed_hydro_statuses() -> set[str]:
    return getattr(_chain, "COMPLETED_HYDRO_STATUSES")


def _accepted_horizon_from_hours(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_accepted_horizon_from_hours")(*args, **kwargs)


def _auto_trigger_canonical_readiness_unavailable_evidence(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_auto_trigger_canonical_readiness_unavailable_evidence")(*args, **kwargs)


def _auto_trigger_forecast_hours(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_auto_trigger_forecast_hours")(*args, **kwargs)


def _auto_trigger_source_object_identity(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_auto_trigger_source_object_identity")(*args, **kwargs)


def _auto_trigger_source_policy_identity(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_auto_trigger_source_policy_identity")(*args, **kwargs)


def _canonical_products_from_ready_cycle(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_canonical_products_from_ready_cycle")(*args, **kwargs)


def _format_time(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_format_time")(*args, **kwargs)


def _json_safe_pipeline_event_value(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_json_safe_pipeline_event_value")(*args, **kwargs)


def _pipeline_already_active_error_cls() -> type[Exception]:
    return getattr(_chain, "PipelineAlreadyActiveError")


def _optional_int(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_optional_int")(*args, **kwargs)


def _safe_pipeline_event_details(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_safe_pipeline_event_details")(*args, **kwargs)


def _skipped_ready_forecast_result(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_skipped_ready_forecast_result")(*args, **kwargs)


def _stale_converter_versions_in_cycle(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_stale_converter_versions_in_cycle")(*args, **kwargs)


def trigger_forecast(
    self: Any,
    *,
    source_id: str | None = None,
    cycle_time: str | datetime,
    model_id: str,
    basin_id: str | None = None,
    max_lead_hours: int | None = None,
) -> PipelineResult:
    return self._trigger_forecast(
        source_id=source_id or self.config.source_id,
        cycle_time=cycle_time,
        model_id=model_id,
        basin_id=basin_id,
        max_lead_hours=max_lead_hours,
        stages=LEGACY_FORECAST_STAGES,
    )


def trigger_forecast_from_canonical(
    self: Any,
    *,
    source_id: str | None = None,
    cycle_time: str | datetime,
    model_id: str,
    basin_id: str | None = None,
    max_lead_hours: int | None = None,
) -> PipelineResult:
    return self._trigger_forecast(
        source_id=source_id or self.config.source_id,
        cycle_time=cycle_time,
        model_id=model_id,
        basin_id=basin_id,
        max_lead_hours=max_lead_hours,
        stages=LEGACY_FORECAST_STAGES[2:],
    )


def trigger_forecast_impl(
    self: Any,
    *,
    source_id: str,
    cycle_time: str | datetime,
    model_id: str,
    basin_id: str | None,
    max_lead_hours: int | None,
    stages: Sequence[Any],
) -> PipelineResult:
    source_id = normalize_source_id(source_id)
    parsed_cycle_time = parse_cycle_time(cycle_time)
    if self.repository.has_active_pipeline(source_id=source_id, cycle_time=parsed_cycle_time, model_id=model_id):
        raise _pipeline_already_active_error_cls()(source_id, parsed_cycle_time, model_id)

    model = self.repository.load_model_context(model_id)
    if basin_id is not None and model.basin_id is not None and model.basin_id != basin_id:
        raise OrchestratorError(
            "MODEL_BASIN_MISMATCH",
            f"Model {model_id} belongs to basin {model.basin_id}, not {basin_id}.",
        )
    forcing = self.repository.find_forcing_context(
        source_id=source_id,
        cycle_time=parsed_cycle_time,
        model_id=model_id,
    )
    initial_state = self._select_forecast_initial_state(
        model_id=model_id,
        cycle_time=parsed_cycle_time,
        source_id=source_id,
        model_package_version=model.model_package_uri,
        model_package_checksum=model.model_package_checksum,
        max_lead_hours=max_lead_hours,
    )
    self.repository.ensure_forecast_cycle(source_id=source_id, cycle_time=parsed_cycle_time)
    context = self._build_run_context(
        source_id,
        parsed_cycle_time,
        model,
        forcing,
        initial_state,
        max_lead_hours=max_lead_hours,
    )
    manifest = self._build_run_manifest(context)
    self._write_run_manifest(context, manifest)
    self.repository.create_hydro_run(context, manifest)
    self.repository.update_hydro_run_status(context.run_id, "staged")
    return self.run_chain(context, stages=stages)


def stage_statuses(
    self: Any,
    *,
    cycle_time: str | datetime,
    source_id: str | None = None,
    model_id: str | None = None,
) -> list[dict[str, Any]]:
    return self.repository.list_stage_statuses(
        source_id=normalize_source_id(source_id) if source_id is not None else None,
        cycle_time=parse_cycle_time(cycle_time),
        model_id=model_id,
    )


def trigger_ready_forecasts(
    self: Any,
    *,
    source_id: str | None = None,
    model_ids: Sequence[str] | None = None,
    limit: int = 100,
) -> tuple[PipelineResult, ...]:
    resolved_source_id = normalize_source_id(source_id or self.config.source_id)
    ready_cycles = self._list_canonical_ready_cycles(source_id=resolved_source_id, limit=limit)
    selected_model_ids = tuple(model_ids) if model_ids is not None else self._list_forecast_model_ids()
    results: list[PipelineResult] = []
    for cycle in ready_cycles:
        cycle_source_id = normalize_source_id(str(cycle.get("source_id") or resolved_source_id))
        cycle_time_value = cycle.get("cycle_time")
        if cycle_time_value is None:
            continue
        parsed_cycle_time = parse_cycle_time(cycle_time_value)
        max_lead_hours = _optional_int(cycle.get("max_lead_hours"))
        stale_versions = _stale_converter_versions_in_cycle(cycle, source_id=cycle_source_id)
        if stale_versions:
            self._demote_stale_canonical_cycle(
                source_id=cycle_source_id,
                cycle_time=parsed_cycle_time,
                stale_versions=stale_versions,
            )
            for model_id in selected_model_ids:
                results.append(
                    _skipped_ready_forecast_result(
                        source_id=cycle_source_id,
                        cycle_time=parsed_cycle_time,
                        model_id=model_id,
                        reason="canonical_converter_version_stale",
                        canonical_readiness={
                            "ready": False,
                            "reason": "canonical_converter_version_stale",
                            "expected_converter_version": expected_converter_version(cycle_source_id),
                            "observed_converter_versions": sorted(stale_versions),
                        },
                    )
                )
            continue
        readiness = self._validate_auto_trigger_canonical_readiness(
            cycle,
            source_id=cycle_source_id,
            cycle_time=parsed_cycle_time,
            max_lead_hours=max_lead_hours,
        )
        for model_id in selected_model_ids:
            if not bool(readiness.get("ready")):
                results.append(
                    _skipped_ready_forecast_result(
                        source_id=cycle_source_id,
                        cycle_time=parsed_cycle_time,
                        model_id=model_id,
                        reason=str(readiness.get("reason") or "canonical_readiness_not_trusted"),
                        canonical_readiness=readiness,
                    )
                )
                continue
            if self._has_completed_forecast(
                source_id=cycle_source_id,
                cycle_time=parsed_cycle_time,
                model_id=model_id,
            ):
                continue
            try:
                results.append(
                    self.trigger_forecast_from_canonical(
                        source_id=cycle_source_id,
                        cycle_time=parsed_cycle_time,
                        model_id=model_id,
                        max_lead_hours=max_lead_hours,
                    )
                )
            except _pipeline_already_active_error_cls():
                continue
    return tuple(results)


def demote_stale_canonical_cycle(
    self: Any,
    *,
    source_id: str,
    cycle_time: datetime,
    stale_versions: set[str | None],
) -> None:
    expected = expected_converter_version(source_id)
    self.repository.update_forecast_cycle_status(
        source_id=source_id,
        cycle_time=cycle_time,
        status=CANONICAL_DEMOTE_CYCLE_STATUS,
    )
    self.repository.insert_pipeline_event(
        entity_type="forecast_cycle",
        entity_id=cycle_id_for(source_id, cycle_time),
        event_type="canonical_converter_version_stale",
        status_from="canonical_ready",
        status_to=CANONICAL_DEMOTE_CYCLE_STATUS,
        message="Canonical products written by a stale converter_version; demoting for re-conversion.",
        details=_safe_pipeline_event_details(
            {
                "source_id": source_id,
                "cycle_time": _format_time(cycle_time),
                "expected_converter_version": expected,
                "observed_converter_versions": sorted(
                    "<missing>" if version is None else str(version) for version in stale_versions
                ),
            }
        ),
    )


def validate_auto_trigger_canonical_readiness(
    self: Any,
    cycle: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    max_lead_hours: int | None,
) -> dict[str, Any]:
    forecast_hours = _auto_trigger_forecast_hours(
        source_id=source_id,
        cycle_time=cycle_time,
        configured_horizon_hours=self.config.forecast_horizon_hours,
        max_lead_hours=max_lead_hours,
    )
    try:
        policy_identity = _auto_trigger_source_policy_identity(
            source_id=source_id,
            cycle_time=cycle_time,
            forecast_hours=forecast_hours,
            workspace_root=self.config.workspace_root,
            object_store_root=self.config.object_store_root,
            object_store_prefix=self.config.object_store_prefix,
        )
        source_object_identity = _auto_trigger_source_object_identity(
            source_id=source_id,
            cycle_time=cycle_time,
            forecast_hours=forecast_hours,
            workspace_root=self.config.workspace_root,
            object_store_root=self.config.object_store_root,
            object_store_prefix=self.config.object_store_prefix,
        )
        products = _canonical_products_from_ready_cycle(cycle, source_id=source_id, cycle_time=cycle_time)
        readiness = evaluate_canonical_readiness(
            source_id=source_id,
            cycle_time=cycle_time,
            products=products,
            forecast_hours=forecast_hours,
            policy_identity=policy_identity,
            source_object_identity=source_object_identity,
            canonical_product_id=f"canon_{source_id.lower()}_{format_cycle_time(cycle_time)}",
        )
        evidence = dict(readiness.evidence)
    except Exception as error:
        evidence = _auto_trigger_canonical_readiness_unavailable_evidence(
            source_id=source_id,
            cycle_time=cycle_time,
            forecast_hours=forecast_hours,
            reason="canonical_readiness_query_failed",
            error=error,
        )
    evidence.setdefault("entrypoint", "trigger_ready_forecasts")
    evidence.setdefault("source_id", source_id)
    evidence.setdefault("source", source_id)
    evidence.setdefault("cycle_time", _format_time(cycle_time))
    evidence.setdefault("accepted_horizon", _accepted_horizon_from_hours(forecast_hours))
    return dict(redact_payload(_json_safe_pipeline_event_value(evidence)))


def list_canonical_ready_cycles(self: Any, *, source_id: str | None, limit: int) -> tuple[dict[str, Any], ...]:
    provider = getattr(self.repository, "list_canonical_ready_cycles", None)
    if not callable(provider):
        raise OrchestratorError(
            "READY_CYCLE_LIST_UNSUPPORTED",
            "The orchestrator repository does not support canonical-ready cycle listing.",
        )
    return tuple(dict(cycle) for cycle in provider(source_id=source_id, limit=limit))


def list_forecast_model_ids(self: Any) -> tuple[str, ...]:
    provider = getattr(self.repository, "list_forecast_model_ids", None)
    if not callable(provider):
        raise OrchestratorError(
            "FORECAST_MODEL_LIST_UNSUPPORTED",
            "The orchestrator repository does not support forecast model listing.",
        )
    return tuple(str(model_id) for model_id in provider())


def has_completed_forecast(self: Any, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
    provider = getattr(self.repository, "has_completed_pipeline", None)
    if callable(provider):
        return bool(provider(source_id=source_id, cycle_time=cycle_time, model_id=model_id))
    run_id = f"fcst_{source_id.lower()}_{format_cycle_time(cycle_time)}_{model_id}"
    hydro_runs = getattr(self.repository, "hydro_runs", None)
    if isinstance(hydro_runs, Mapping):
        run = hydro_runs.get(run_id)
        if isinstance(run, Mapping):
            return str(run.get("status")) in _completed_hydro_statuses()
    return False
