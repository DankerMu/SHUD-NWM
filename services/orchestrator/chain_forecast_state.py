from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping

from packages.common.source_identity import normalize_source_id
from packages.common.state_lineage import (
    STATE_QC_FAILED,
    STATE_TOO_STALE,
    WARM_START_LINEAGE_MISMATCH,
    WARM_START_SUCCESSOR_CHECKPOINT_MISSING,
    WARM_START_SUCCESSOR_CHECKPOINT_UNUSABLE,
)
from packages.common.state_manager import StateSnapshot, assess_freshness
from services.orchestrator import chain as _chain
from services.orchestrator import chain_manifests
from services.orchestrator.chain_types import (
    ForcingContext,
    ForecastRunContext,
    InitialStateSelection,
    ModelContext,
    OrchestratorError,
)
from workers.data_adapters.base import cycle_id_for, format_cycle_time

scenario_for_source = _chain.scenario_for_source
_MAX_STATE_FALLBACK_CANDIDATES = _chain._MAX_STATE_FALLBACK_CANDIDATES


def _directory_uri(*args, **kwargs):
    return getattr(_chain, "_directory_uri")(*args, **kwargs)


def _ensure_utc(*args, **kwargs):
    return getattr(_chain, "_ensure_utc")(*args, **kwargs)


def _forecast_state_checkpoint_hours(*args, **kwargs):
    return getattr(_chain, "_forecast_state_checkpoint_hours")(*args, **kwargs)


def _format_time(*args, **kwargs):
    return getattr(_chain, "_format_time")(*args, **kwargs)


def _initial_state_lineage(*args, **kwargs):
    return getattr(_chain, "_initial_state_lineage")(*args, **kwargs)


def _optional_str(*args, **kwargs):
    return getattr(_chain, "_optional_str")(*args, **kwargs)


def _parse_gateway_time(*args, **kwargs):
    return getattr(_chain, "_parse_gateway_time")(*args, **kwargs)


def _resolve_forecast_horizon_hours(*args, **kwargs):
    return getattr(_chain, "_resolve_forecast_horizon_hours")(*args, **kwargs)


def _validate_state_lineage(*args, **kwargs):
    return getattr(_chain, "_validate_state_lineage")(*args, **kwargs)


def _validate_strict_state_lineage(*args, **kwargs):
    return getattr(_chain, "_validate_strict_state_lineage")(*args, **kwargs)


def _package_checksum_matches(*args, **kwargs):
    return getattr(_chain, "_package_checksum_matches")(*args, **kwargs)


def _build_run_context(
    self,
    source_id: str,
    cycle_time: datetime,
    model: ModelContext,
    forcing: ForcingContext,
    initial_state: InitialStateSelection | None = None,
    max_lead_hours: int | None = None,
) -> ForecastRunContext:
    source_id = normalize_source_id(source_id)
    compact_cycle = format_cycle_time(cycle_time)
    run_id = f"fcst_{source_id.lower()}_{compact_cycle}_{model.model_id}"
    start_time = cycle_time
    forecast_horizon_hours = _resolve_forecast_horizon_hours(
        source_id=source_id,
        cycle_time=cycle_time,
        configured_horizon_hours=self.config.forecast_horizon_hours,
        forcing=forcing,
        max_lead_hours=max_lead_hours,
    )
    end_time = cycle_time + timedelta(hours=forecast_horizon_hours)
    fallback_forcing_uri = f"forcing/{source_id.lower()}/{compact_cycle}/{model.basin_version_id}/{model.model_id}/"
    selected_state = initial_state or InitialStateSelection(None, None, None, None, "cold_start_no_state")
    return ForecastRunContext(
        run_id=run_id,
        source_id=source_id,
        scenario_id=self._forecast_scenario_id(source_id),
        cycle_id=cycle_id_for(source_id, cycle_time),
        cycle_time=cycle_time,
        model_id=model.model_id,
        basin_id=model.basin_id,
        basin_version_id=model.basin_version_id,
        river_network_version_id=model.river_network_version_id,
        segment_count=model.segment_count,
        model_package_uri=model.model_package_uri,
        forcing_version_id=forcing.forcing_version_id,
        forcing_package_uri=forcing.forcing_package_uri or fallback_forcing_uri,
        start_time=start_time,
        end_time=end_time,
        forecast_horizon_hours=forecast_horizon_hours,
        run_manifest_uri=self.object_store.uri_for_key(f"runs/{run_id}/input/manifest.json"),
        output_uri=_directory_uri(self.object_store, f"runs/{run_id}/output/"),
        log_uri=_directory_uri(self.object_store, f"runs/{run_id}/logs/"),
        forcing_package_manifest_uri=getattr(forcing, "forcing_package_manifest_uri", None),
        forcing_package_manifest_checksum=getattr(forcing, "forcing_package_manifest_checksum", None),
        init_state_id=selected_state.state_id,
        init_state_uri=selected_state.state_uri,
        init_state_valid_time=selected_state.valid_time,
        init_state_checksum=selected_state.checksum,
        init_state_quality=selected_state.quality,
        init_state_lineage=_initial_state_lineage(selected_state),
        output_segment_count=model.output_segment_count,
    )


def _forecast_scenario_id(self, source_id: str) -> str:
    if self.config.scenario_id_explicit and self.config.scenario_id:
        return self.config.scenario_id
    return scenario_for_source(source_id)


def _build_run_manifest(self, context: ForecastRunContext) -> dict[str, Any]:
    return chain_manifests.build_forecast_run_manifest(
        context,
        forecast_state_checkpoint_hours=_chain._forecast_state_checkpoint_hours,
    )


def _state_passes_qc(self, state: StateSnapshot) -> bool:
    """Selection-time QC gate for a warm-start candidate.

    Defers to the state manager's optional ``state_variable_qc_passed`` hook when
    present; absent the hook, a usable snapshot is trusted (run-time/save-time QC
    already gated ``usable_flag``). Returns False to skip a candidate that fails QC.
    """

    hook = getattr(self.state_manager, "state_variable_qc_passed", None)
    if hook is None:
        return True
    try:
        return bool(hook(state))
    except Exception:  # noqa: BLE001 - a QC hook failure must not crash selection
        return False


def _select_forecast_initial_state(
    self,
    *,
    model_id: str,
    cycle_time: datetime,
    source_id: str | None = None,
    model_package_version: str | None = None,
    model_package_checksum: str | None = None,
    max_lead_hours: int | None = None,
) -> InitialStateSelection:
    if self.config.require_forecast_warm_start:
        return self._select_strict_forecast_initial_state(
            model_id=model_id,
            cycle_time=cycle_time,
            source_id=source_id,
            model_package_version=model_package_version,
            model_package_checksum=model_package_checksum,
        )
    if self.state_manager is None:
        return InitialStateSelection(None, None, None, None, "cold_start_no_state")

    cursor = cycle_time
    last_rejection_code: str | None = None
    # Fallback loop: reject incompatible-lineage / failed-QC candidates and try
    # the next older usable state, never failing the cycle for a missing successor.
    for _ in range(_MAX_STATE_FALLBACK_CANDIDATES):
        state = self._exact_or_latest_usable_state(
            model_id=model_id,
            cycle_time=cycle_time,
            before_time=cursor,
            source_id=source_id,
        )
        if state is None:
            return InitialStateSelection(
                None, None, None, None, "cold_start_no_state", rejection_code=last_rejection_code
            )

        quality = assess_freshness(
            state.valid_time,
            cycle_time,
            soft_threshold_days=self.config.state_soft_stale_threshold_days,
            hard_threshold_days=self.config.state_hard_stale_threshold_days,
        )
        if quality == "cold_start_stale_state":
            # Older states are even staler; stop and record stale cold start. The
            # primary cause here is staleness, so the rejection_code is the explicit
            # STATE_TOO_STALE marker -- never a carried-forward LINEAGE_* code from a
            # younger candidate, which would falsely conflate quality=stale with a
            # lineage rejection.
            return InitialStateSelection(None, None, None, None, quality, rejection_code=STATE_TOO_STALE)

        rejection_code = _validate_state_lineage(
            state,
            source_id=source_id,
            model_package_version=model_package_version,
            model_package_checksum=model_package_checksum,
            max_lead_hours=max_lead_hours,
        )
        if rejection_code is None and not self._state_passes_qc(state):
            rejection_code = STATE_QC_FAILED
        if rejection_code is not None:
            # Record the rejection on the candidate and advance to an older one.
            last_rejection_code = rejection_code
            cursor = state.valid_time - timedelta(microseconds=1)
            continue

        return InitialStateSelection(
            state_id=state.state_id,
            state_uri=state.state_uri,
            valid_time=state.valid_time,
            checksum=state.checksum,
            quality=quality,
            source_id=state.source_id,
            cycle_id=state.cycle_id,
            lead_hours=state.lead_hours,
            model_package_version=state.model_package_version,
            model_package_checksum=state.model_package_checksum,
            rejection_code=None,
        )

    return InitialStateSelection(None, None, None, None, "cold_start_no_state", rejection_code=last_rejection_code)


def _select_strict_forecast_initial_state(
    self,
    *,
    model_id: str,
    cycle_time: datetime,
    source_id: str | None,
    model_package_version: str | None,
    model_package_checksum: str | None,
) -> InitialStateSelection:
    if self.state_manager is None:
        raise OrchestratorError(
            WARM_START_SUCCESSOR_CHECKPOINT_MISSING,
            "Strict forecast warm-start requires a state manager.",
            {"model_id": model_id, "source_id": source_id, "cycle_time": _format_time(cycle_time)},
        )
    state = self._get_exact_forecast_state(
        model_id=model_id,
        cycle_time=cycle_time,
        source_id=source_id,
    )
    if state is None:
        raise OrchestratorError(
            WARM_START_SUCCESSOR_CHECKPOINT_MISSING,
            "Exact successor checkpoint is required for strict forecast warm-start.",
            {"model_id": model_id, "source_id": source_id, "cycle_time": _format_time(cycle_time)},
        )
    return self._validate_strict_forecast_state(
        state,
        model_id=model_id,
        cycle_time=cycle_time,
        source_id=source_id,
        model_package_version=model_package_version,
        model_package_checksum=model_package_checksum,
    )


def _validate_prefilled_forecast_initial_state(
    self,
    basin: Mapping[str, Any],
    *,
    source_id: str | None,
    cycle_time: datetime,
    model_package_version: str | None,
    model_package_checksum: str | None,
) -> InitialStateSelection:
    model_id = str(basin.get("model_id") or "")
    if not model_id:
        raise OrchestratorError("BASIN_MODEL_ID_MISSING", "Each basin entry requires model_id.")
    state = self._resolve_prefilled_forecast_state(
        basin,
        model_id=model_id,
        cycle_time=cycle_time,
        source_id=source_id,
    )
    if state is None:
        raise OrchestratorError(
            WARM_START_SUCCESSOR_CHECKPOINT_MISSING,
            "Scheduler-prefilled warm-start state was not found.",
            {
                "model_id": model_id,
                "source_id": source_id,
                "cycle_time": _format_time(cycle_time),
                "init_state_id": basin.get("init_state_id"),
                "init_state_uri": basin.get("init_state_uri"),
            },
        )
    selection = self._validate_strict_forecast_state(
        state,
        model_id=model_id,
        cycle_time=cycle_time,
        source_id=source_id,
        model_package_version=model_package_version,
        model_package_checksum=model_package_checksum,
    )
    self._validate_prefilled_state_identity(basin, selection)
    return selection


def _resolve_prefilled_forecast_state(
    self,
    basin: Mapping[str, Any],
    *,
    model_id: str,
    cycle_time: datetime,
    source_id: str | None,
) -> StateSnapshot | None:
    if self.state_manager is None:
        return None
    state_id = _optional_str(basin.get("init_state_id"))
    if state_id is not None:
        provider = getattr(self.state_manager, "get_state_snapshot", None)
        if callable(provider):
            state = provider(state_id)
            if state is not None:
                return state
        repository_provider = getattr(getattr(self.state_manager, "repository", None), "get_state_snapshot", None)
        if callable(repository_provider):
            state = repository_provider(state_id)
            if state is not None:
                return state
    return self._get_exact_forecast_state(model_id=model_id, cycle_time=cycle_time, source_id=source_id)


def _validate_prefilled_state_identity(
    self,
    basin: Mapping[str, Any],
    selection: InitialStateSelection,
) -> None:
    raw_valid_time = basin.get("init_state_valid_time")
    try:
        valid_time = _parse_gateway_time(raw_valid_time)
    except (TypeError, ValueError) as exc:
        raise OrchestratorError(
            WARM_START_LINEAGE_MISMATCH,
            "Scheduler-prefilled warm-start valid_time is malformed.",
            {"field": "init_state_valid_time", "observed": raw_valid_time},
        ) from exc
    if raw_valid_time not in (None, "") and valid_time is None:
        raise OrchestratorError(
            WARM_START_LINEAGE_MISMATCH,
            "Scheduler-prefilled warm-start valid_time is malformed.",
            {"field": "init_state_valid_time", "observed": raw_valid_time},
        )
    raw_lineage = basin.get("init_state_lineage")
    if raw_lineage in (None, ""):
        lineage: dict[str, Any] = {}
    elif isinstance(raw_lineage, Mapping):
        lineage = dict(raw_lineage)
    else:
        raise OrchestratorError(
            WARM_START_LINEAGE_MISMATCH,
            "Scheduler-prefilled warm-start lineage is malformed.",
            {"field": "init_state_lineage", "observed_type": type(raw_lineage).__name__},
        )
    checks = (
        ("init_state_id", selection.state_id),
        ("init_state_uri", selection.state_uri),
        ("init_state_checksum", selection.checksum),
    )
    for field_name, expected in checks:
        observed = basin.get(field_name)
        if observed not in (None, "") and expected is not None and str(observed) != str(expected):
            raise OrchestratorError(
                WARM_START_LINEAGE_MISMATCH,
                "Scheduler-prefilled warm-start identity does not match the strict successor checkpoint.",
                {"field": field_name, "observed": observed, "expected": expected},
            )
    if valid_time is not None and selection.valid_time is not None:
        if _ensure_utc(valid_time) != _ensure_utc(selection.valid_time):
            raise OrchestratorError(
                WARM_START_LINEAGE_MISMATCH,
                "Scheduler-prefilled warm-start valid_time does not match the strict successor checkpoint.",
                {"observed": _format_time(valid_time), "expected": _format_time(selection.valid_time)},
            )
    lineage_checks = (
        ("cycle_id", selection.cycle_id),
        ("model_package_version", selection.model_package_version),
        ("model_package_checksum", selection.model_package_checksum),
    )
    observed_source_id = lineage.get("source_id")
    if observed_source_id not in (None, "") and selection.source_id not in (None, ""):
        try:
            observed_normalized_source = normalize_source_id(str(observed_source_id))
            expected_normalized_source = normalize_source_id(selection.source_id)
        except ValueError as exc:
            raise OrchestratorError(
                WARM_START_LINEAGE_MISMATCH,
                "Scheduler-prefilled warm-start lineage source_id is malformed.",
                {"field": "source_id", "observed": observed_source_id, "expected": selection.source_id},
            ) from exc
        if observed_normalized_source != expected_normalized_source:
            raise OrchestratorError(
                WARM_START_LINEAGE_MISMATCH,
                "Scheduler-prefilled warm-start lineage does not match the strict successor checkpoint.",
                {"field": "source_id", "observed": observed_source_id, "expected": selection.source_id},
            )
    for key, expected in lineage_checks:
        observed = lineage.get(key)
        if observed in (None, "") or expected in (None, ""):
            continue
        matches = (
            _package_checksum_matches(observed, expected)
            if key == "model_package_checksum"
            else str(observed) == str(expected)
        )
        if not matches:
            raise OrchestratorError(
                WARM_START_LINEAGE_MISMATCH,
                "Scheduler-prefilled warm-start lineage does not match the strict successor checkpoint.",
                {"field": key, "observed": observed, "expected": expected},
            )
    raw_lead_hours = lineage.get("lead_hours")
    if raw_lead_hours in (None, ""):
        lead_hours = None
    elif isinstance(raw_lead_hours, bool):
        raise OrchestratorError(
            WARM_START_LINEAGE_MISMATCH,
            "Scheduler-prefilled warm-start lead_hours is malformed.",
            {"field": "lead_hours", "observed": raw_lead_hours},
        )
    elif isinstance(raw_lead_hours, int):
        lead_hours = raw_lead_hours
    elif isinstance(raw_lead_hours, str) and raw_lead_hours.strip().lstrip("+-").isdigit():
        lead_hours = int(raw_lead_hours)
    else:
        raise OrchestratorError(
            WARM_START_LINEAGE_MISMATCH,
            "Scheduler-prefilled warm-start lead_hours is malformed.",
            {"field": "lead_hours", "observed": raw_lead_hours},
        )
    if lead_hours is not None and selection.lead_hours is not None and lead_hours != selection.lead_hours:
        raise OrchestratorError(
            WARM_START_LINEAGE_MISMATCH,
            "Scheduler-prefilled warm-start lead_hours does not match the strict successor checkpoint.",
            {"observed": lead_hours, "expected": selection.lead_hours},
        )


def _validate_strict_forecast_state(
    self,
    state: StateSnapshot,
    *,
    model_id: str,
    cycle_time: datetime,
    source_id: str | None,
    model_package_version: str | None,
    model_package_checksum: str | None,
) -> InitialStateSelection:
    if state.model_id != model_id or _ensure_utc(state.valid_time) != _ensure_utc(cycle_time):
        raise OrchestratorError(
            WARM_START_LINEAGE_MISMATCH,
            "Strict forecast warm-start state must match model_id and cycle_time.",
            {
                "model_id": model_id,
                "state_model_id": state.model_id,
                "cycle_time": _format_time(cycle_time),
                "state_valid_time": _format_time(state.valid_time),
            },
        )
    if not state.usable_flag or not self._state_passes_qc(state):
        raise OrchestratorError(
            WARM_START_SUCCESSOR_CHECKPOINT_UNUSABLE,
            "Exact successor checkpoint is unusable or failed state-variable QC.",
            {"state_id": state.state_id, "cycle_time": _format_time(cycle_time)},
        )
    rejection_code = _validate_strict_state_lineage(
        state,
        source_id=source_id,
        model_package_version=model_package_version,
        model_package_checksum=model_package_checksum,
    )
    if rejection_code is not None or state.lead_hours != 12:
        raise OrchestratorError(
            WARM_START_LINEAGE_MISMATCH,
            "Exact successor checkpoint lineage is incompatible with strict forecast warm-start.",
            {
                "state_id": state.state_id,
                "lineage_rejection_code": rejection_code,
                "lead_hours": state.lead_hours,
                "required_lead_hours": 12,
            },
        )
    return InitialStateSelection(
        state_id=state.state_id,
        state_uri=state.state_uri,
        valid_time=state.valid_time,
        checksum=state.checksum,
        quality="fresh",
        source_id=state.source_id,
        cycle_id=state.cycle_id,
        lead_hours=state.lead_hours,
        model_package_version=state.model_package_version,
        model_package_checksum=state.model_package_checksum,
        rejection_code=None,
    )


def _get_exact_forecast_state(
    self,
    *,
    model_id: str,
    cycle_time: datetime,
    source_id: str | None,
) -> StateSnapshot | None:
    if self.state_manager is None:
        return None
    repository = getattr(self.state_manager, "repository", None)
    exact_provider = getattr(repository, "get_state_snapshot_by_model_time", None)
    if not callable(exact_provider):
        exact_provider = getattr(self.state_manager, "get_state_snapshot_by_model_time", None)
    if not callable(exact_provider):
        return None
    if source_id is not None:
        exact = exact_provider(model_id=model_id, valid_time=_ensure_utc(cycle_time), source_id=source_id)
        if exact is not None:
            return exact
    return exact_provider(model_id=model_id, valid_time=_ensure_utc(cycle_time), source_id=None)


def _exact_or_latest_usable_state(
    self,
    *,
    model_id: str,
    cycle_time: datetime,
    before_time: datetime,
    source_id: str | None,
) -> StateSnapshot | None:
    if self.state_manager is None:
        return None
    repository = getattr(self.state_manager, "repository", None)
    exact_provider = getattr(repository, "get_state_snapshot_by_model_time", None)
    if callable(exact_provider) and _ensure_utc(before_time) == _ensure_utc(cycle_time):
        exact = exact_provider(model_id=model_id, valid_time=_ensure_utc(cycle_time), source_id=source_id)
        if exact is not None and exact.usable_flag:
            return exact
    return self.state_manager.get_latest_usable_state(model_id=model_id, before_time=before_time)
