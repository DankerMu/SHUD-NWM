"""Source-scope fail-closed dispatch precondition (Epic #961, §4.1 + §4.2).

Exercises the REAL dispatch path -- ``scheduler_candidates.build_candidates``
invoked through ``ProductionScheduler._build_candidates`` -- to prove three
things pinned by the parent OpenSpec change:

* ``in_scope_dispatches``: an in-scope source lands in the candidate list; a
  legacy IDW model (no direct-grid contract) is unaffected regardless of
  requested source.
* ``out_of_scope_fails_closed``: an out-of-scope source is blocked at the
  candidate-assembly boundary with the parser's fail-closed error surfaced
  and no fallback candidate.
* ``boundary_not_producer_only``: the block is decided at
  ``build_candidates`` -- ``forcing_producer.produce`` is never called for
  the out-of-scope pairing.

Plus a shape-defense regression:

* ``malformed_direct_contract_still_blocked``: a candidate whose
  ``resource_profile.direct_grid_forcing`` fails the parser for a reason
  OTHER than source-scope is blocked under a distinct reason code carrying
  the parser's field/error payload.

SUB-10 (#971) §4.2 (new dispatch-seam behavior + regression locks over
existing ``workers/forcing_producer`` single-source, no-splice, no-legacy-
compat guarantees):

* ``missing_source_no_run``: a ``ForcingProductionError`` with the two
  producer messages meaning "no usable canonical data for this cycle"
  becomes a blocked candidate with the NAMED reason
  ``forcing_source_missing_for_cycle`` and ``state_evidence["error_code"]
  == "MISSING_SOURCE_DATA_FOR_CYCLE"``; no substitution candidate is
  emitted and the producer is not re-invoked.
* ``single_source_run``: a direct-grid variant configured for one
  applicable source runs against that ONE ``source_id`` exactly once.
* ``midrun_splice_forbidden``: a mid-run missing-variable exception classifies
  under the same missing-source reason; dispatch does not retry with a
  different source or invoke any "splice"/"fallback" method.
* ``no_legacy_compat``: the scheduler execution seam does not import any
  legacy CMFD / IDW compatibility symbol, and after a missing-source failure
  the producer is not re-invoked with a different ``source_id``.
* ``availability_is_display_concern``: when source A fails missing-source and
  source B has data, dispatch emits exactly one blocked (A) and one ready
  (B) candidate -- no third "cross-source-merged" candidate is synthesized.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import scheduler as scheduler_module
from services.orchestrator import scheduler_execution as scheduler_execution_module
from services.orchestrator.scheduler import ProductionScheduler as _RealProductionScheduler
from services.orchestrator.scheduler import ProductionSchedulerConfig
from services.orchestrator.scheduler_execution import (
    _MISSING_SOURCE_DATA_ERROR_PREFIXES,
    FORCING_SOURCE_MISSING_FOR_CYCLE_REASON,
    MISSING_SOURCE_DATA_FOR_CYCLE_ERROR_CODE,
    _missing_source_data_evidence,
)
from workers.data_adapters.base import CycleDiscovery
from workers.forcing_producer.producer import ForcingProductionError

CYCLE_TIME = datetime(2026, 5, 21, 6, 0, tzinfo=UTC)
NOW = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
GRID_ID = "grid_demo"
GRID_SIGNATURE = "grid-signature-demo"


class _NoopRegistry:
    """Registry stub -- these tests bypass registry entirely and pass models
    directly into ``_build_candidates``. Prevents PsycopgModelRegistryStore
    from being constructed from env."""

    def list_models(self, **_kwargs: Any) -> dict[str, Any]:
        return {"items": [], "total": 0, "limit": 0, "offset": 0}

    def get_model(self, model_id: str) -> dict[str, Any]:
        raise KeyError(model_id)


class _NoopReconcileStore:
    def query_reserved_unbound_jobs(self) -> list[Any]:
        return []

    def query_inflight_jobs(self) -> list[Any]:
        return []

    def bind_reservation(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("noop reconcile store must not bind reservations")

    def update_job_status(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("noop reconcile store must not update job status")


class _FakeForcingProducer:
    """Records every ``produce`` call; boundary tests assert an empty list."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def produce(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        raise AssertionError(
            "forcing_producer.produce must not be called for an out-of-scope "
            "direct-grid candidate; source-scope must fail closed at "
            "scheduler_candidates.build_candidates (Epic #961, §4.1)."
        )


class _TestProductionScheduler(_RealProductionScheduler):
    """Minimal ProductionScheduler wrapper for candidate-assembly tests.

    Mirrors ``tests/test_production_scheduler.py``'s ProductionScheduler
    wrapper: canonical-readiness is bypassed so a candidate that passes the
    source-scope check flows through to the ``candidates`` list without
    hitting the ``_UnavailableCanonicalReadinessProvider`` fallback.
    """

    def __init__(
        self,
        config: ProductionSchedulerConfig,
        *,
        registry: Any | None = None,
        adapters: Mapping[str, Any] | None = None,
        forcing_producer: Any | None = None,
    ) -> None:
        super().__init__(
            config,
            registry=registry if registry is not None else _NoopRegistry(),
            adapters=adapters or {},
            active_repository=None,
            canonical_readiness_provider=None,
            forcing_producer=forcing_producer,
            orchestrator_factory=None,
            reconcile_store=_NoopReconcileStore(),
            reconcile_comment_query=lambda _key: None,
            reconcile_sacct_query=lambda _job_id: None,
        )

    def _canonical_readiness_for_candidate(
        self,
        candidate: Any,
        cycle: Any,
    ) -> dict[str, Any] | None:
        return None


def _config(tmp_path: Path, **overrides: Any) -> ProductionSchedulerConfig:
    values: dict[str, Any] = {
        "workspace_root": tmp_path,
        "sources": ("gfs",),
        "lookback_hours": 24,
        "cycle_lag_hours": 0,
        "max_cycles_per_source": 1,
        "allowed_cycle_hours_utc": (0, 6, 12, 18),
        "dry_run": True,
        "now": NOW,
    }
    values.update(overrides)
    return ProductionSchedulerConfig(**values)


def _valid_direct_grid_forcing(
    *,
    applicable_source_ids: list[str],
    include_binding_uri: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "forcing_mapping_mode": "direct_grid",
        "binding_uri": "s3://nhms/models/demo/direct-grid/binding.json",
        "binding_checksum": "sha256:binding-a",
        "model_input_package_id": "model-input-a-v1",
        "sp_att_path": "input/demo.sp.att",
        "sp_att_checksum": "sha256:sp-att",
        "applicable_source_ids": list(applicable_source_ids),
        "grid_id": GRID_ID,
        "grid_signature": GRID_SIGNATURE,
        "station_bindings": [
            {
                "station_id": "demo_forc_001",
                "shud_forcing_index": 1,
                "forcing_filename": "X100.95Y36.25.csv",
                "longitude": 100.95,
                "latitude": 36.25,
                "x": 1,
                "y": 2,
                "z": 3657,
                "grid_id": GRID_ID,
                "grid_cell_id": "cell-001",
            },
            {
                "station_id": "demo_forc_002",
                "shud_forcing_index": 2,
                "forcing_filename": "X101.05Y36.25.csv",
                "longitude": 101.05,
                "latitude": 36.25,
                "x": 2,
                "y": 3,
                "z": 3600,
                "grid_id": GRID_ID,
                "grid_cell_id": "cell-002",
            },
        ],
    }
    if not include_binding_uri:
        payload.pop("binding_uri")
    return payload


def _direct_grid_model(
    *,
    model_id: str,
    applicable_source_ids: list[str],
    include_binding_uri: bool = True,
) -> scheduler_module.RegisteredSchedulerModel:
    resource_profile = {
        "canonical_grid_key": "canonical-grid-demo",
        "direct_grid_forcing": _valid_direct_grid_forcing(
            applicable_source_ids=applicable_source_ids,
            include_binding_uri=include_binding_uri,
        ),
    }
    return scheduler_module.RegisteredSchedulerModel(
        model_id=model_id,
        basin_id="basin_a",
        basin_version_id="basin_a_v1",
        river_network_version_id="basin_a_rivnet_v1",
        segment_count=3,
        output_segment_count=3,
        model_package_uri=f"s3://nhms/models/{model_id}/package/",
        shud_code_version="2.0",
        resource_profile=resource_profile,
        resource_profile_summary={},
        display_capabilities={},
    )


def _legacy_idw_model(model_id: str) -> scheduler_module.RegisteredSchedulerModel:
    return scheduler_module.RegisteredSchedulerModel(
        model_id=model_id,
        basin_id="basin_a",
        basin_version_id="basin_a_v1",
        river_network_version_id="basin_a_rivnet_v1",
        segment_count=3,
        output_segment_count=3,
        model_package_uri=f"s3://nhms/models/{model_id}/package/",
        shud_code_version="2.0",
        resource_profile={},
        resource_profile_summary={},
        display_capabilities={},
    )


def _cycle_for(source_id: str) -> scheduler_module.SchedulerSourceCycle:
    return scheduler_module.SchedulerSourceCycle(
        discovery=CycleDiscovery(
            cycle_id=f"{source_id}_2026052106",
            source_id=source_id,
            cycle_time=CYCLE_TIME,
            cycle_hour=6,
            available=True,
            status="discovered",
        ),
        horizon={},
    )


# --- in_scope_dispatches ---------------------------------------------------


def test_in_scope_dispatches_direct_grid_variant_lands_in_candidates(
    tmp_path: Path,
) -> None:
    """A direct-grid variant whose ``applicable_source_ids`` contains the
    requested source normalizes and lands in the ``candidates`` list.

    Locks §4.1 evidence key ``in_scope_dispatches``: the source-scope check
    does not block an in-scope pairing.
    """

    scheduler = _TestProductionScheduler(_config(tmp_path))

    candidates, blocked, skipped, duplicate_exclusions, slurm_sync = scheduler._build_candidates(
        models=[
            _direct_grid_model(model_id="model_direct_a", applicable_source_ids=["GFS", "IFS"])
        ],
        cycles=[_cycle_for("gfs")],
    )

    assert len(candidates) == 1
    assert candidates[0].model_id == "model_direct_a"
    assert candidates[0].source_id == "gfs"
    assert blocked == []
    assert skipped == []
    assert duplicate_exclusions == []
    assert slurm_sync == []


def test_in_scope_dispatches_legacy_idw_model_unaffected_by_source_scope_check(
    tmp_path: Path,
) -> None:
    """A legacy IDW model (no ``direct_grid_forcing``) is dispatched exactly
    as before, regardless of source.

    Locks §4.1 evidence key ``in_scope_dispatches``: legacy models are
    untouched by the source-scope precondition.
    """

    scheduler = _TestProductionScheduler(_config(tmp_path))

    candidates, blocked, _skipped, _duplicates, _slurm = scheduler._build_candidates(
        models=[_legacy_idw_model("model_legacy_a")],
        cycles=[_cycle_for("gfs")],
    )

    assert len(candidates) == 1
    assert candidates[0].model_id == "model_legacy_a"
    assert candidates[0].source_id == "gfs"
    assert blocked == []


# --- out_of_scope_fails_closed --------------------------------------------


def test_out_of_scope_fails_closed_no_fallback_candidate(tmp_path: Path) -> None:
    """An out-of-scope source is blocked with the parser's source-scope
    error surfaced; no fallback IDW/other-source candidate is produced.

    Locks §4.1 evidence key ``out_of_scope_fails_closed``.
    """

    scheduler = _TestProductionScheduler(_config(tmp_path))
    model = _direct_grid_model(
        model_id="model_direct_ifs_only",
        applicable_source_ids=["IFS"],
    )

    candidates, blocked, skipped, duplicate_exclusions, slurm_sync = scheduler._build_candidates(
        models=[model],
        cycles=[_cycle_for("gfs")],
    )

    assert candidates == []
    assert skipped == []
    assert duplicate_exclusions == []
    assert slurm_sync == []
    assert len(blocked) == 1

    blocked_candidate = blocked[0]
    assert blocked_candidate.status == "blocked"
    assert blocked_candidate.reason == "direct_grid_source_out_of_scope"
    assert blocked_candidate.model_id == "model_direct_ifs_only"
    assert blocked_candidate.source_id == "gfs"

    scope_evidence = blocked_candidate.state_evidence["direct_grid_source_scope"]
    assert scope_evidence["code"] == "direct_grid_source_out_of_scope"
    assert scope_evidence["requested_source_id"] == "gfs"
    # Parser normalizes 'IFS' -> 'IFS' (uppercase); 'gfs' -> 'gfs' (lowercase).
    assert scope_evidence["normalized_source_id"] == "gfs"
    assert list(scope_evidence["applicable_source_ids"]) == ["IFS"]
    assert scope_evidence["message"] == (
        "Direct-grid contract does not apply to the current source."
    )


def test_out_of_scope_fails_closed_no_second_candidate_for_other_source(
    tmp_path: Path,
) -> None:
    """The scheduler never fabricates a fallback candidate for a different
    source when the requested source is out of scope for this basin's
    direct-grid variant. Locks §4.1 evidence key ``out_of_scope_fails_closed``.
    """

    scheduler = _TestProductionScheduler(_config(tmp_path))

    candidates, blocked, _skipped, _duplicates, _slurm = scheduler._build_candidates(
        models=[
            _direct_grid_model(
                model_id="model_direct_ifs_only",
                applicable_source_ids=["IFS"],
            )
        ],
        cycles=[_cycle_for("gfs")],
    )

    # No candidate for any source: the block is fail-closed, not "try IFS
    # instead" or "try legacy IDW".
    assert candidates == []
    assert all(item.source_id == "gfs" for item in blocked)
    assert all(item.reason == "direct_grid_source_out_of_scope" for item in blocked)


# --- boundary_not_producer_only -------------------------------------------


def test_boundary_not_producer_only_producer_never_called_for_out_of_scope(
    tmp_path: Path,
) -> None:
    """Prove the block lands at ``build_candidates`` -- ``forcing_producer.
    produce`` is never called for the out-of-scope pairing.

    The check is at the dispatch/staging boundary, not deferred to the
    producer's own parser check (INV-5). Locks §4.1 evidence key
    ``boundary_not_producer_only``.
    """

    producer = _FakeForcingProducer()
    scheduler = _TestProductionScheduler(_config(tmp_path), forcing_producer=producer)
    model = _direct_grid_model(
        model_id="model_direct_ifs_only",
        applicable_source_ids=["IFS"],
    )

    candidates, blocked, _skipped, _duplicates, _slurm = scheduler._build_candidates(
        models=[model],
        cycles=[_cycle_for("gfs")],
    )

    # The candidate never enters the ``candidates`` list, so
    # ``produce_forcing_for_candidates`` (which only iterates candidates)
    # has nothing to hand to ``produce``. Assert the producer stayed idle
    # even after we pipe the (empty) ``candidates`` through the real
    # execution seam.
    assert candidates == []
    assert len(blocked) == 1
    assert blocked[0].reason == "direct_grid_source_out_of_scope"

    ready, forcing_blocked, evidence = scheduler._produce_forcing_for_candidates(candidates)
    assert ready == []
    assert forcing_blocked == []
    assert evidence == []
    assert producer.calls == []


# --- malformed_direct_contract_still_blocked ------------------------------


def test_malformed_direct_contract_still_blocked_under_distinct_reason(
    tmp_path: Path,
) -> None:
    """A malformed direct-grid contract (missing a required field OTHER than
    source scope) blocks the candidate under ``direct_grid_contract_invalid``
    -- distinct from the source-scope block -- and carries the parser's
    field/error payload for triage.

    Defense in depth: the registration surface (#962) validates shape, but
    candidate rows can carry stale contracts. The dispatch boundary refuses
    them fail-closed rather than silently falling through to a producer
    that would then fail deeper.
    """

    scheduler = _TestProductionScheduler(_config(tmp_path))
    # ``gfs`` is in scope, so the block cannot be the source-scope one --
    # any block MUST come from the shape check.
    model = _direct_grid_model(
        model_id="model_direct_malformed",
        applicable_source_ids=["GFS"],
        include_binding_uri=False,
    )

    candidates, blocked, _skipped, _duplicates, _slurm = scheduler._build_candidates(
        models=[model],
        cycles=[_cycle_for("gfs")],
    )

    assert candidates == []
    assert len(blocked) == 1
    blocked_candidate = blocked[0]
    assert blocked_candidate.reason == "direct_grid_contract_invalid"

    contract_evidence = blocked_candidate.state_evidence["direct_grid_contract"]
    assert contract_evidence["code"] == "direct_grid_contract_invalid"
    assert contract_evidence["requested_source_id"] == "gfs"
    assert contract_evidence["field"] == "binding_uri"
    error_payload = contract_evidence["error"]
    assert error_payload["error_code"] == "DIRECT_GRID_CONTRACT_INVALID"
    assert error_payload["field"] == "binding_uri"
    # Ensure this is NOT the source-scope evidence key.
    assert "direct_grid_source_scope" not in blocked_candidate.state_evidence


def test_malformed_contract_block_distinguished_from_source_scope_block(
    tmp_path: Path,
) -> None:
    """The two direct-grid block reasons are structurally distinct: the
    source-scope block uses ``direct_grid_source_scope`` state-evidence key
    and reason ``direct_grid_source_out_of_scope``; the shape-defense block
    uses ``direct_grid_contract`` and ``direct_grid_contract_invalid``.
    """

    scheduler = _TestProductionScheduler(_config(tmp_path))

    _cands_scope, blocked_scope, *_ = scheduler._build_candidates(
        models=[
            _direct_grid_model(
                model_id="model_direct_ifs_only",
                applicable_source_ids=["IFS"],
            )
        ],
        cycles=[_cycle_for("gfs")],
    )
    _cands_shape, blocked_shape, *_ = scheduler._build_candidates(
        models=[
            _direct_grid_model(
                model_id="model_direct_malformed",
                applicable_source_ids=["GFS"],
                include_binding_uri=False,
            )
        ],
        cycles=[_cycle_for("gfs")],
    )

    assert blocked_scope[0].reason == "direct_grid_source_out_of_scope"
    assert "direct_grid_source_scope" in blocked_scope[0].state_evidence
    assert "direct_grid_contract" not in blocked_scope[0].state_evidence

    assert blocked_shape[0].reason == "direct_grid_contract_invalid"
    assert "direct_grid_contract" in blocked_shape[0].state_evidence
    assert "direct_grid_source_scope" not in blocked_shape[0].state_evidence


# --- §4.2 missing-source no-run + zero cross-source substitution ----------
#
# SUB-10 (#971) fake producers below simulate the compute-layer's own fail-
# closed exception surfaces; the point is to exercise the SCHEDULER SEAM
# (``produce_forcing_for_candidates``) around them. Producer bytes stay
# unchanged (HARD BOUNDARY, INV-5).


class _MissingSourceProducer:
    """Fake ForcingProducer that raises the producer's real "no usable data
    for this cycle" ``ForcingProductionError`` messages. Records every call
    so we can assert the producer is invoked ONCE per candidate and never
    re-invoked with a different ``source_id`` after failure.
    """

    def __init__(self, *, message: str) -> None:
        self.message = message
        self.calls: list[dict[str, Any]] = []
        # Record any attribute lookup so a hypothetical "splice"/"fallback"
        # method would show up here — dispatch must never touch such a hook.
        self.attribute_lookups: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name in {"produce", "message", "calls", "attribute_lookups"}:
            raise AttributeError(name)
        self.attribute_lookups.append(name)
        raise AttributeError(name)

    def produce(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        raise ForcingProductionError(self.message)


class _RecordingSuccessProducer:
    """Fake ForcingProducer that succeeds; records every ``produce`` call for
    single-source / no-cross-source assertions.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def produce(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        source_id = str(kwargs["source_id"])
        cycle_time = kwargs["cycle_time"]
        model_id = str(kwargs["model_id"])
        return _StubForcingResult(source_id=source_id, cycle_time=cycle_time, model_id=model_id)


class _MixedResultProducer:
    """Raises for one source, succeeds for another. Used by
    ``availability_is_display_concern`` to prove the compute layer does NOT
    fabricate a third "cross-source-merged" candidate.
    """

    def __init__(self, *, missing_source_id: str, missing_message: str) -> None:
        self.missing_source_id = missing_source_id
        self.missing_message = missing_message
        self.calls: list[dict[str, Any]] = []

    def produce(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        if str(kwargs["source_id"]).lower() == self.missing_source_id.lower():
            raise ForcingProductionError(self.missing_message)
        source_id = str(kwargs["source_id"])
        cycle_time = kwargs["cycle_time"]
        model_id = str(kwargs["model_id"])
        return _StubForcingResult(source_id=source_id, cycle_time=cycle_time, model_id=model_id)


class _StubForcingResult:
    """Minimal duck-typed shape ``_candidate_forcing_ready_evidence`` and
    ``_candidate_with_forcing_result`` read from. Only the attributes actually
    accessed are populated; missing ones fall through the ``getattr(...,
    default)`` calls in the evidence builder.
    """

    def __init__(self, *, source_id: str, cycle_time: datetime, model_id: str) -> None:
        self.status = "forcing_ready"
        self.forcing_version_id = f"forcing-{source_id}-{model_id}"
        self.forcing_package_uri = f"s3://nhms/forcing/{source_id}/{model_id}/package.tar"
        self.checksum = f"sha256:forcing-{source_id}-{model_id}"
        self.station_count = 2
        self.timestep_count = 1
        self.variable_count = 1
        self.time_range = {"start": cycle_time.isoformat(), "end": cycle_time.isoformat()}
        self.units = {}
        self.file_uris = {}


# ---- missing_source_no_run ----------------------------------------------


@pytest.mark.parametrize(
    "producer_message",
    [
        "No canonical products are available.",
        "Missing required canonical products: T2:2025-01-01T00:00:00Z",
    ],
)
def test_missing_source_no_run_blocks_with_named_reason(
    tmp_path: Path,
    producer_message: str,
) -> None:
    """A ``ForcingProductionError`` matching one of the producer's two
    "no usable data for this cycle" prefixes lands as a blocked candidate
    with the NAMED reason ``forcing_source_missing_for_cycle`` and error_code
    ``MISSING_SOURCE_DATA_FOR_CYCLE`` -- not the generic
    ``forcing_production_blocked``.

    Also asserts no substitution candidate is emitted (dispatch does not
    fabricate a ``ready`` candidate for a different source) and the producer
    is invoked exactly once for the single candidate (no retry with a
    different source). Locks §4.2 evidence key ``missing_source_no_run``.
    """

    producer = _MissingSourceProducer(message=producer_message)
    scheduler = _TestProductionScheduler(_config(tmp_path), forcing_producer=producer)
    model = _direct_grid_model(
        model_id="model_direct_gfs_only",
        applicable_source_ids=["GFS"],
    )

    candidates, build_blocked, _skipped, _dups, _slurm = scheduler._build_candidates(
        models=[model],
        cycles=[_cycle_for("gfs")],
    )
    assert len(candidates) == 1
    assert build_blocked == []
    ready, blocked, evidence = scheduler._produce_forcing_for_candidates(candidates)

    # Producer invoked exactly once, on the requested source, no retry:
    assert len(producer.calls) == 1
    assert producer.calls[0]["source_id"] == "gfs"

    # Fail-closed at this seam: no substitution ready candidate.
    assert ready == []
    assert len(blocked) == 1

    blocked_candidate = blocked[0]
    assert blocked_candidate.status == "blocked"
    assert blocked_candidate.reason == FORCING_SOURCE_MISSING_FOR_CYCLE_REASON
    assert blocked_candidate.source_id == "gfs"

    # state_evidence carries the missing-source classification and requested
    # (source, cycle) identity. ``_blocked_candidate`` merges the passed
    # ``state_evidence`` mapping into the candidate's existing state_evidence
    # at the TOP LEVEL (see ``_merge_state_evidence`` -- shallow-key merge),
    # so the fields land at the top of ``blocked_candidate.state_evidence``.
    state_evidence = blocked_candidate.state_evidence
    assert state_evidence["error_code"] == MISSING_SOURCE_DATA_FOR_CYCLE_ERROR_CODE
    assert state_evidence["source_id"] == "gfs"
    # The identity evidence formats cycle_time_utc as an ISO string.
    assert state_evidence["cycle_time_utc"].startswith("2026-05-21T06:00:00")
    # Residual blocker rows are remapped to the missing-source classification.
    residual = state_evidence["residual_blockers"]
    assert isinstance(residual, list) and residual
    assert residual[0]["code"] == MISSING_SOURCE_DATA_FOR_CYCLE_ERROR_CODE
    assert residual[0]["quality_flag"] == FORCING_SOURCE_MISSING_FOR_CYCLE_REASON

    # Also inspect the returned evidence list carries the same classification.
    assert len(evidence) == 1
    assert evidence[0]["error_code"] == MISSING_SOURCE_DATA_FOR_CYCLE_ERROR_CODE
    assert evidence[0]["source_id"] == "gfs"


def test_missing_source_no_run_other_forcing_error_stays_generic(
    tmp_path: Path,
) -> None:
    """Regression: a ``ForcingProductionError`` message that is NOT one of
    the two producer "missing canonical data" prefixes must continue to land
    under the pre-existing generic ``forcing_production_blocked`` reason and
    ``FORCING_PRODUCTION_BLOCKED`` error_code.

    Locks §4.2's "preserve current behavior for other blocked-candidate
    paths" constraint: the classifier is tight, not a catch-all.
    """

    producer = _MissingSourceProducer(
        message="Forcing station_count 0 exceeds configured limit 1000.",
    )
    scheduler = _TestProductionScheduler(_config(tmp_path), forcing_producer=producer)
    model = _direct_grid_model(
        model_id="model_direct_gfs_only",
        applicable_source_ids=["GFS"],
    )

    candidates, _bblocked, _skipped, _dups, _slurm = scheduler._build_candidates(
        models=[model],
        cycles=[_cycle_for("gfs")],
    )
    _ready, blocked, evidence = scheduler._produce_forcing_for_candidates(candidates)

    assert len(blocked) == 1
    assert blocked[0].reason == "forcing_production_blocked"
    # Generic error_code is preserved; not remapped to the missing-source code.
    assert blocked[0].state_evidence["error_code"] == "FORCING_PRODUCTION_BLOCKED"
    assert evidence[0]["error_code"] == "FORCING_PRODUCTION_BLOCKED"


# ---- single_source_run --------------------------------------------------


def test_single_source_run_dispatches_requested_source_exactly_once(
    tmp_path: Path,
) -> None:
    """A direct-grid variant configured for a single applicable source runs
    against that ONE ``source_id`` exactly once. Regression-locks the "one
    run stays single-source end to end" guarantee (§4.2).
    """

    producer = _RecordingSuccessProducer()
    scheduler = _TestProductionScheduler(_config(tmp_path), forcing_producer=producer)
    model = _direct_grid_model(
        model_id="model_direct_gfs_only",
        applicable_source_ids=["GFS"],
    )

    candidates, _bblocked, _skipped, _dups, _slurm = scheduler._build_candidates(
        models=[model],
        cycles=[_cycle_for("gfs")],
    )
    assert len(candidates) == 1
    ready, blocked, _evidence = scheduler._produce_forcing_for_candidates(candidates)

    assert blocked == []
    assert len(ready) == 1
    assert ready[0].source_id == "gfs"

    # Producer called exactly once, only for the requested source.
    assert len(producer.calls) == 1
    assert producer.calls[0]["source_id"] == "gfs"
    assert all(call["source_id"] == "gfs" for call in producer.calls)
    # No second call for a different source in the same run.
    called_sources = {call["source_id"] for call in producer.calls}
    assert called_sources == {"gfs"}


# ---- midrun_splice_forbidden -------------------------------------------


def test_midrun_splice_forbidden_no_retry_no_splice_hook(
    tmp_path: Path,
) -> None:
    """A mid-run missing-variable ``ForcingProductionError`` (the producer's
    ``"Missing required canonical products: ..."`` message from
    ``producer.py:1118``) classifies at the SCHEDULER SEAM as
    ``forcing_source_missing_for_cycle`` -- dispatch does NOT retry with a
    different source, does NOT invoke any splice/fallback method (any
    non-``produce`` attribute access on the producer raises AttributeError
    and is recorded).

    Regression-locks §4.2 "Mid-run splicing of another source is forbidden":
    from the seam's vantage point, pre-run and mid-run missing-variable
    failures are the same "no usable data for this (source, cycle)" verdict.
    """

    producer = _MissingSourceProducer(
        message="Missing required canonical products: T2:2025-01-01T00:00:00Z",
    )
    scheduler = _TestProductionScheduler(_config(tmp_path), forcing_producer=producer)
    model = _direct_grid_model(
        model_id="model_direct_gfs_only",
        applicable_source_ids=["GFS"],
    )

    candidates, _bblocked, _skipped, _dups, _slurm = scheduler._build_candidates(
        models=[model],
        cycles=[_cycle_for("gfs")],
    )
    ready, blocked, _evidence = scheduler._produce_forcing_for_candidates(candidates)

    # One blocked candidate, no ready candidate, no fabricated retry.
    assert ready == []
    assert len(blocked) == 1
    assert blocked[0].reason == FORCING_SOURCE_MISSING_FOR_CYCLE_REASON
    assert (
        blocked[0].state_evidence["error_code"]
        == MISSING_SOURCE_DATA_FOR_CYCLE_ERROR_CODE
    )

    # Producer.produce called exactly once for the requested source; no
    # second call for a different source, no attribute lookup for a
    # ``splice``/``fallback``/``retry``-style method.
    assert len(producer.calls) == 1
    assert producer.calls[0]["source_id"] == "gfs"
    assert producer.attribute_lookups == []


# ---- no_legacy_compat ---------------------------------------------------

_LEGACY_COMPAT_SYMBOL_TOKENS: tuple[str, ...] = (
    "LegacyForcingCompat",
    "IDWFallback",
    "legacy_package_selector",
    "legacy_compat",
    "idw_fallback",
    # Snake-case module vectors: a future refactor that adds e.g.
    # ``from workers.legacy_forcing_compat import LegacyIDW`` would collapse
    # (after ``.replace("_", "")``) to ``workers.legacyforcingcompat.legacyidw``
    # -- the tokens below catch that normalized form. See
    # ``test_no_legacy_compat_detector_catches_snake_case_module`` for the
    # detector's own regression lock.
    "legacyforcingcompat",
    "legacyforcing",
    "legacycompat",
)


def _iter_module_imports(module_path: Path) -> list[str]:
    """Return every dotted name imported by ``module_path`` (both
    ``import x`` and ``from y import z`` forms). Used to prove the dispatch
    execution seam does not consult a legacy CMFD / IDW compatibility layer.
    """

    tree = ast.parse(module_path.read_text())
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                names.append(f"{module}.{alias.name}" if module else alias.name)
    return names


def test_no_legacy_compat_scheduler_execution_import_graph(
    tmp_path: Path,
) -> None:
    """The scheduler execution seam does not import any legacy CMFD / IDW
    compatibility symbol. Regression-locks §4.2 "no legacy/IDW compatibility
    layer is consulted".

    The check walks ``services.orchestrator.scheduler_execution``'s imports
    and asserts none contain the forbidden tokens in either raw form (catches
    ``LegacyForcingCompat`` / ``legacy_compat``) or normalized form with all
    ``_`` stripped (catches ``legacy_forcing_compat`` -> ``legacyforcingcompat``,
    which no single raw substring token would match because the mid-segment
    ``_forcing_`` breaks up ``legacy_compat``).
    """

    module_path = Path(scheduler_execution_module.__file__)
    imports = _iter_module_imports(module_path)

    for dotted_raw in imports:
        dotted_lower = dotted_raw.lower()
        dotted_normalized = dotted_lower.replace("_", "")
        for token in _LEGACY_COMPAT_SYMBOL_TOKENS:
            token_lower = token.lower()
            assert token_lower not in dotted_lower, (
                f"scheduler_execution imports {dotted_raw!r} -- contains forbidden "
                f"legacy-compat token {token!r} in raw form (§4.2 no_legacy_compat "
                f"regression)."
            )
            assert token_lower not in dotted_normalized, (
                f"scheduler_execution imports {dotted_raw!r} -- contains forbidden "
                f"legacy-compat token {token!r} in underscore-normalized form "
                f"(§4.2 no_legacy_compat regression; a snake_case module vector "
                f"like ``legacy_forcing_compat`` collapses to "
                f"``legacyforcingcompat`` here)."
            )


def test_no_legacy_compat_detector_catches_snake_case_module() -> None:
    """Negative-sanity: the underscore-normalized substring detector actually
    catches snake_case module vectors that a raw substring check misses.

    Locks the detector's own semantics — if a future refactor weakens the
    normalization back to raw substring, this test fails, protecting the
    guarantee that
    ``test_no_legacy_compat_scheduler_execution_import_graph`` gives.
    """

    forbidden_dotted_names = [
        "workers.legacy_forcing_compat.LegacyIDW",
        "workers.legacy_compat.IDW",
        "packages.legacy_forcing_compat",
    ]

    for dotted_raw in forbidden_dotted_names:
        dotted_lower = dotted_raw.lower()
        dotted_normalized = dotted_lower.replace("_", "")
        matched = False
        for token in _LEGACY_COMPAT_SYMBOL_TOKENS:
            token_lower = token.lower()
            if token_lower in dotted_lower or token_lower in dotted_normalized:
                matched = True
                break
        assert matched, (
            f"snake_case-module vector {dotted_raw!r} slipped past the "
            "no_legacy_compat detector -- expected at least one token from "
            "_LEGACY_COMPAT_SYMBOL_TOKENS to match either the raw or "
            "underscore-stripped form."
        )


def test_no_legacy_compat_producer_not_reinvoked_with_other_source(
    tmp_path: Path,
) -> None:
    """After a missing-source ``ForcingProductionError``, the producer is
    not re-invoked with a different ``source_id`` -- there is no legacy
    ``LegacyForcingCompat.substitute(...)`` fallback path in dispatch.

    Regression-locks the runtime side of §4.2 ``no_legacy_compat``.
    """

    producer = _MissingSourceProducer(
        message="No canonical products are available.",
    )
    scheduler = _TestProductionScheduler(_config(tmp_path), forcing_producer=producer)
    model = _direct_grid_model(
        model_id="model_direct_gfs_only",
        applicable_source_ids=["GFS"],
    )

    candidates, _bblocked, _skipped, _dups, _slurm = scheduler._build_candidates(
        models=[model],
        cycles=[_cycle_for("gfs")],
    )
    _ready, _blocked, _evidence = scheduler._produce_forcing_for_candidates(candidates)

    # Exactly one produce() call, only for the requested source. No second
    # invocation for any other source. No fallback attribute accessed.
    assert len(producer.calls) == 1
    called_sources = {call["source_id"] for call in producer.calls}
    assert called_sources == {"gfs"}
    assert producer.attribute_lookups == []


# ---- availability_is_display_concern ----------------------------------


def test_availability_is_display_concern_no_cross_source_merged_candidate(
    tmp_path: Path,
) -> None:
    """When source A fails missing-source and source B has data, dispatch
    emits exactly:

    * one BLOCKED candidate for source A (named missing-source reason), no
      output;
    * one READY candidate for source B, produced from source B ONLY;
    * NO third "cross-source-merged" candidate.

    Regression-locks §4.2 "cross-source availability is resolved by display
    best-available selection over already-produced per-source products, not
    by compute-layer source merging".
    """

    producer = _MixedResultProducer(
        missing_source_id="gfs",
        missing_message="No canonical products are available.",
    )
    scheduler = _TestProductionScheduler(
        _config(tmp_path, sources=("gfs", "ifs")),
        forcing_producer=producer,
    )
    model_a = _direct_grid_model(
        model_id="model_direct_gfs",
        applicable_source_ids=["GFS"],
    )
    model_b = _direct_grid_model(
        model_id="model_direct_ifs",
        applicable_source_ids=["IFS"],
    )

    candidates, _bblocked, _skipped, _dups, _slurm = scheduler._build_candidates(
        models=[model_a, model_b],
        cycles=[_cycle_for("gfs"), _cycle_for("ifs")],
    )
    # Two candidates -- A(GFS) and B(IFS) -- no merged third candidate at
    # dispatch time either.
    assert len(candidates) == 2
    ready, blocked, evidence = scheduler._produce_forcing_for_candidates(candidates)

    # Exactly one ready (source B) and one blocked (source A). Note the
    # storage-canonical casing differs by source: GFS -> ``"gfs"`` (lowercase),
    # IFS -> ``"IFS"`` (uppercase), per
    # ``packages/common/source_identity.normalize_source_id``.
    assert len(ready) == 1
    assert len(blocked) == 1
    assert ready[0].source_id == "IFS"
    assert ready[0].model_id == "model_direct_ifs"
    assert blocked[0].source_id == "gfs"
    assert blocked[0].model_id == "model_direct_gfs"
    assert blocked[0].reason == FORCING_SOURCE_MISSING_FOR_CYCLE_REASON

    # Producer called once per candidate; the two calls used the candidate's
    # own applicable source, never a cross-source substitute.
    calls_by_source = {call["source_id"] for call in producer.calls}
    assert calls_by_source == {"gfs", "IFS"}
    # No third produce() call for a merged/spliced source.
    assert len(producer.calls) == 2
    # Evidence contains one entry per candidate (2 items) -- no synthetic
    # third "cross-source-merged" evidence row.
    assert len(evidence) == 2


# ---- base blocked-candidate evidence shape preservation ----------------


def test_missing_source_no_run_preserves_base_forcing_blocked_evidence_shape(
    tmp_path: Path,
) -> None:
    """The missing-source remap preserves the FULL base
    ``_candidate_forcing_blocked_evidence`` shape -- it only rewrites
    ``error_code`` at the top level and ``code`` + ``quality_flag`` on each
    residual-blocker entry.

    Regression-locks §4.2 evidence-shape stability: if a future refactor of
    ``services/orchestrator/scheduler_candidate_execution_evidence.py::
    _candidate_forcing_blocked_evidence`` drops or renames one of the ~14
    base-shape fields (``stage``, ``production_stage``, ``status``,
    ``submitted``, ``slurm_submit_called``, ``execution_attempted``,
    ``forcing_producer_called``, ``mutation_outcome``, ``mutation_occurred``,
    ``met_result_table_write``, ``hydro_result_table_write``,
    ``pipeline_status_writes_proven_absent``,
    ``pipeline_event_writes_proven_absent``, ``qhh_script_invoked``,
    ``rshud_runtime_called``), missing-source evidence would silently
    propagate the change. Asserting key-set equality here forces the two to
    stay locked.
    """

    scheduler = _TestProductionScheduler(_config(tmp_path))
    model = _direct_grid_model(
        model_id="model_direct_gfs_only",
        applicable_source_ids=["GFS"],
    )
    candidates, _bblocked, _skipped, _dups, _slurm = scheduler._build_candidates(
        models=[model],
        cycles=[_cycle_for("gfs")],
    )
    assert len(candidates) == 1
    candidate = candidates[0]

    error = ForcingProductionError("No canonical products are available.")
    context = scheduler._scheduler_execution_context()

    base = dict(context.candidate_forcing_blocked_evidence(candidate, error))
    remapped = _missing_source_data_evidence(context, candidate, error)

    # Full key set matches -- a field-add/rename regression on the base
    # blocked-evidence surface would fail this immediately.
    assert set(remapped.keys()) == set(base.keys())

    # Every top-level field EXCEPT the two remap targets is unchanged.
    remap_targets = {"error_code", "residual_blockers"}
    assert {k: v for k, v in remapped.items() if k not in remap_targets} == {
        k: v for k, v in base.items() if k not in remap_targets
    }

    # Per-blocker preservation: everything except ``code``/``quality_flag``
    # is byte-identical to the base residual-blocker rows.
    base_residuals = base["residual_blockers"]
    remapped_residuals = remapped["residual_blockers"]
    assert isinstance(base_residuals, list) and base_residuals
    assert isinstance(remapped_residuals, list) and remapped_residuals
    assert len(base_residuals) == len(remapped_residuals)
    blocker_remap_targets = {"code", "quality_flag"}
    for i, base_entry in enumerate(base_residuals):
        remapped_entry = remapped_residuals[i]
        assert isinstance(base_entry, Mapping)
        assert isinstance(remapped_entry, Mapping)
        assert set(base_entry.keys()) == set(remapped_entry.keys())
        assert {
            k: v for k, v in remapped_entry.items() if k not in blocker_remap_targets
        } == {k: v for k, v in base_entry.items() if k not in blocker_remap_targets}

    # The remap targets carry the missing-source classification, not the
    # generic forcing-blocked default.
    assert remapped["error_code"] == MISSING_SOURCE_DATA_FOR_CYCLE_ERROR_CODE
    for entry in remapped_residuals:
        assert entry["code"] == MISSING_SOURCE_DATA_FOR_CYCLE_ERROR_CODE
        assert entry["quality_flag"] == FORCING_SOURCE_MISSING_FOR_CYCLE_REASON


# ---- producer message drift lock ---------------------------------------


def _producer_forcing_production_error_messages() -> list[str]:
    """AST-walk ``workers/forcing_producer/producer.py`` and return every
    string literal (or leading string-constant part of an f-string) passed
    as the FIRST positional argument to a ``ForcingProductionError(...)``
    call. Used by the drift-lock below to prove the detector prefixes still
    match at least one real producer raise site.
    """

    producer_path = Path("workers/forcing_producer/producer.py")
    if not producer_path.is_absolute():
        # Resolve relative to this test file's repo root so the test is
        # invocation-directory-independent.
        producer_path = (
            Path(__file__).resolve().parent.parent / "workers" / "forcing_producer" / "producer.py"
        )
    tree = ast.parse(producer_path.read_text())
    messages: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            continue
        if name != "ForcingProductionError":
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            messages.append(first.value)
            continue
        if isinstance(first, ast.JoinedStr):
            # Take the leading run of constant string parts -- that's the
            # prefix a ``str.startswith`` check would test against. Stop at
            # the first ``FormattedValue`` (interpolated ``{...}`` slot).
            leading_parts: list[str] = []
            for part in first.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    leading_parts.append(part.value)
                else:
                    break
            if leading_parts:
                messages.append("".join(leading_parts))
    return messages


def test_missing_source_prefixes_still_match_producer_raise_sites() -> None:
    """AST-walk producer.py, collect every ``ForcingProductionError(...)``
    first-positional message (including the leading-constant portion of any
    f-string form), and assert every detector prefix in
    ``_MISSING_SOURCE_DATA_ERROR_PREFIXES`` matches at least ONE real
    producer message.

    Regression-locks §4.2 producer message drift: if a future producer refactor
    renames ``"Missing required canonical products: ..."`` to
    ``"Missing canonical products required: ..."``, the parametrize + detector
    would both keep mirroring the OLD wording and this test would catch the
    drift instead of letting production silently regress to the generic reason.
    """

    producer_messages = _producer_forcing_production_error_messages()
    assert producer_messages, (
        "AST walk found no ForcingProductionError(...) calls in producer.py; "
        "the walker regressed or producer.py moved."
    )

    for prefix in _MISSING_SOURCE_DATA_ERROR_PREFIXES:
        assert any(
            msg.startswith(prefix) for msg in producer_messages
        ), (
            f"Detector prefix {prefix!r} does not match any "
            f"ForcingProductionError message in producer.py. Either the "
            f"producer wording drifted or the detector prefix drifted -- "
            f"both must move together."
        )
