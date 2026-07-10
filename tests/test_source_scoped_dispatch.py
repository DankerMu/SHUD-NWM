"""Source-scope fail-closed dispatch precondition (Epic #961, §4.1).

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
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.orchestrator import scheduler as scheduler_module
from services.orchestrator.scheduler import ProductionScheduler as _RealProductionScheduler
from services.orchestrator.scheduler import ProductionSchedulerConfig
from workers.data_adapters.base import CycleDiscovery

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
