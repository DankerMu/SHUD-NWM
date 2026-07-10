"""§3.1 producer bbox preflight — fail-closed evidence for Epic #973 SUB-6.

Wires
:func:`packages.common.grid_registry_bbox_guard.verify_download_bbox_matches_registry`
into the top of the direct-grid branch of
:meth:`workers.forcing_producer.producer.ForcingProducer.produce` and proves
the pinned §3.1 contract:

1. Matching bbox lets production proceed (writes fire).
2. Mismatched bbox raises :class:`BboxMismatchError` with
   ``expected_bbox`` / ``actual_bbox`` / ``grid_snapshot_id`` populated, and
   the direct-grid production surface produces zero writes / zero output.
3. Missing registered snapshot fails closed (zero writes).
4. Superseded snapshot fails closed (zero writes).
5. :class:`ValueError` from ``env_reader`` (malformed
   ``NHMS_DOWNLOAD_BBOX_*``) or the guard's finiteness gate propagates
   un-swallowed (zero writes).
6. Cache-hit + mismatched bbox still fails closed BEFORE the "return
   existing ready" early-return, proving the ordering promised by the
   ``_preflight_direct_grid_bbox`` call-site docstring at
   ``workers/forcing_producer/producer.py`` §"preflight … runs before every
   direct-grid repository write AND before the cache-hit early-return".
7. Non-preflight :class:`ValueError` inside the outer try (e.g.
   :func:`_safe_path_component`'s ``ValueError("Invalid path component.")``)
   MUST still be wrapped as :class:`ForcingProductionError` so callers such
   as :mod:`services.production_closure.met_validation`, which only catch
   :class:`ForcingProductionError`, still see the wrapped shape. The
   un-swallowed ValueError contract is narrowed to EXACTLY the preflight
   env_reader / finiteness gate via the private
   ``_PreflightValueErrorPropagate`` sentinel.

Runs only under the ``-k "match or mismatch or missing or superseded or
safe_path"`` filter documented in
``openspec/changes/direct-grid-build-enablement/tasks.md``
§3.1 Evidence Floor; each name matches at least one of those keywords.

Tests 9-11 cover tasks.md §3.2 (SUB-7): delegation to the pinned
``verify_download_bbox_matches_registry`` guard + longitude-convention
(-180..180 canonical) fail-closed landing per Decision 7. Cross-convention
``east=200`` (0..360) vs registered -180..180 snapshot fails closed with
``BboxMismatchError`` rather than clipping a shifted region. They run under
the ``-k "delegates or convention"`` filter documented in the §3.2 Evidence
Floor.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common import grid_registry_bbox_guard as _guard_module
from packages.common.grid_registry_bbox_guard import (
    BboxMismatchError,
    RegisteredBboxSnapshotProtocol,
)
from tests.test_forcing_producer import (
    _build_direct_grid_repository,
    _direct_grid_manifest_for_default_grid,
)
from workers.data_adapters.region import GeoBBox
from workers.forcing_producer import (
    ForcingProducer,
    ForcingProducerConfig,
    parse_direct_grid_forcing_contract,
)
from workers.forcing_producer import producer as _producer_module
from workers.forcing_producer.producer import (
    ForcingProductionError,
    MissingRegisteredGridSnapshotError,
    SupersededGridSnapshotError,
)

# ---------------------------------------------------------------------------
# Test fixtures — env, snapshot ids, canonical grid signature monkeypatch.
# ---------------------------------------------------------------------------

# China-buffered defaults from ``workers/data_adapters/region.py``. Tests do
# NOT set ``NHMS_DOWNLOAD_BBOX_*`` so the pinned guard's default env_reader
# resolves to exactly these values, matching what the fake repository returns
# for the happy-path snapshot row.
_DEFAULT_ENV_BBOX = GeoBBox(south=8.0, north=64.0, west=63.0, east=145.0)

_REGISTERED_SNAPSHOT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _pin_direct_grid_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    """Match the manifest's declared grid_signature so the direct-grid contract
    validation passes and the preflight receives the expected signature.
    """
    monkeypatch.setattr(
        "workers.forcing_producer.producer._grid_signature_hash",
        lambda _grid_points: "sha256:grid-signature-actual",
    )


def _make_direct_grid_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    snapshot_row_factory: Any = None,
) -> tuple[Any, Any, Any]:
    """Assemble a direct-grid producer + repository fake pinned to the
    ``_direct_grid_manifest_for_default_grid`` fixture. Optionally overrides
    the fake's snapshot lookup so tests can drive the fail-closed paths.
    """
    _pin_direct_grid_signature(monkeypatch)
    contract = parse_direct_grid_forcing_contract(
        _direct_grid_manifest_for_default_grid(), source_id="GFS"
    )
    store, repository = _build_direct_grid_repository(tmp_path, contract=contract)
    if snapshot_row_factory is not None:
        repository.find_registered_snapshot_bbox_by_identity = snapshot_row_factory  # type: ignore[method-assign]
    return contract, store, repository


def _assert_zero_direct_grid_production_writes(
    repository: Any,
    tmp_path: Path,
) -> None:
    """§3.1 "zero direct-grid production side effects" invariant — every
    direct-grid write surface is untouched and no forcing output landed.
    Matches the shape used by :func:`_assert_direct_grid_failure_without_idw_or_ready_outputs`
    in ``test_forcing_producer.py``.
    """
    assert repository.direct_grid_station_ensure_count == 0
    assert repository.interp_weight_upsert_count == 0
    assert repository.interp_weights == []
    assert repository.forcing_versions == {}
    assert repository.components == []
    assert repository.timeseries == []
    assert repository.upsert_count == 0
    assert repository.load_station_count == 0
    assert repository.load_weight_count == 0
    assert not any(
        event[0] == "finalize_forcing_version" for event in repository.events
    )
    assert not (tmp_path / "forcing").exists()


def _build_producer_with_env_reader(
    tmp_path: Path,
    repository: Any,
    store: Any,
    *,
    env_reader: Any = None,
) -> ForcingProducer:
    config = ForcingProducerConfig(workspace_root=tmp_path, idw_neighbors=3)
    return ForcingProducer(
        config=config,
        repository=repository,
        object_store=store,
        env_reader=env_reader,
    )


# ---------------------------------------------------------------------------
# 1. Matching bbox → production proceeds.
# ---------------------------------------------------------------------------


def test_matching_bbox_lets_direct_grid_production_proceed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matching env bbox + registered snapshot bbox → preflight passes,
    production runs to completion, direct-grid writes fire.
    """
    contract, store, repository = _make_direct_grid_setup(tmp_path, monkeypatch)

    producer = _build_producer_with_env_reader(tmp_path, repository, store)
    result = producer.produce(
        source_id="gfs", cycle_time="2026050700", model_id="demo_model"
    )

    assert result.status == "forcing_ready"
    # Writes DID land — proves the preflight did not falsely block a matching bbox.
    assert repository.direct_grid_station_ensure_count == 1
    assert repository.interp_weight_upsert_count == 1
    assert repository.upsert_count == 1
    assert repository.timeseries  # station timeseries written
    assert repository.components  # forcing components written
    # And the last cycle update is the READY status, not a fail-closed status.
    assert repository.cycle_updates[-1]["status"] == "forcing_ready"


# ---------------------------------------------------------------------------
# 2. Mismatched bbox → BboxMismatchError with populated fields + zero writes.
# ---------------------------------------------------------------------------


def test_mismatched_bbox_raises_BboxMismatchError_with_populated_fields_and_zero_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registered snapshot bbox differs from env bbox → the guard raises
    :class:`BboxMismatchError` carrying ``expected_bbox`` (env side),
    ``actual_bbox`` (snapshot side), and ``grid_snapshot_id``; and zero
    direct-grid production writes fire.
    """
    # Mismatch: shift the snapshot south corner by +1.0 degrees so bit-exact
    # float equality fails on that field, matching the SUB-4 comparator.
    mismatch_south = _DEFAULT_ENV_BBOX.south + 1.0

    def snapshot_row_factory(
        *, source_id: str, grid_id: str, grid_signature: str
    ) -> tuple[float, float, float, float, uuid.UUID, Any]:
        del source_id, grid_id, grid_signature
        return (
            mismatch_south,
            _DEFAULT_ENV_BBOX.north,
            _DEFAULT_ENV_BBOX.west,
            _DEFAULT_ENV_BBOX.east,
            _REGISTERED_SNAPSHOT_ID,
            None,
        )

    _contract, store, repository = _make_direct_grid_setup(
        tmp_path, monkeypatch, snapshot_row_factory=snapshot_row_factory
    )
    producer = _build_producer_with_env_reader(tmp_path, repository, store)

    with pytest.raises(BboxMismatchError) as excinfo:
        producer.produce(
            source_id="gfs", cycle_time="2026050700", model_id="demo_model"
        )

    # All three failure-payload attributes are populated per SUB-4 contract.
    assert excinfo.value.grid_snapshot_id == _REGISTERED_SNAPSHOT_ID
    assert excinfo.value.expected_bbox == {
        "south": _DEFAULT_ENV_BBOX.south,
        "north": _DEFAULT_ENV_BBOX.north,
        "west": _DEFAULT_ENV_BBOX.west,
        "east": _DEFAULT_ENV_BBOX.east,
    }
    assert excinfo.value.actual_bbox == {
        "south": mismatch_south,
        "north": _DEFAULT_ENV_BBOX.north,
        "west": _DEFAULT_ENV_BBOX.west,
        "east": _DEFAULT_ENV_BBOX.east,
    }
    _assert_zero_direct_grid_production_writes(repository, tmp_path)
    # Fail-closed status is written; that is NOT a direct-grid production write.
    assert repository.cycle_updates[-1]["status"] == "failed_forcing"
    assert repository.cycle_updates[-1]["error_code"] == "FORCING_FAILED"


# ---------------------------------------------------------------------------
# 3. Missing registered snapshot → fail closed, zero writes.
# ---------------------------------------------------------------------------


def test_missing_snapshot_fails_closed_and_writes_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No registered ``canonical_grid_snapshot`` row for
    ``(source_id, grid_id, grid_signature)`` → producer raises
    :class:`MissingRegisteredGridSnapshotError` before any direct-grid write.
    """

    def snapshot_row_factory(
        *, source_id: str, grid_id: str, grid_signature: str
    ) -> None:
        del source_id, grid_id, grid_signature
        return None

    _contract, store, repository = _make_direct_grid_setup(
        tmp_path, monkeypatch, snapshot_row_factory=snapshot_row_factory
    )
    producer = _build_producer_with_env_reader(tmp_path, repository, store)

    with pytest.raises(MissingRegisteredGridSnapshotError) as excinfo:
        producer.produce(
            source_id="gfs", cycle_time="2026050700", model_id="demo_model"
        )

    # Preserves the identity triple on the exception for downstream logging.
    assert excinfo.value.source_id == "gfs"
    assert excinfo.value.grid_id == "grid_a"
    assert excinfo.value.grid_signature == "sha256:grid-signature-actual"
    _assert_zero_direct_grid_production_writes(repository, tmp_path)
    assert repository.cycle_updates[-1]["status"] == "failed_forcing"


# ---------------------------------------------------------------------------
# 4. Superseded snapshot → fail closed, zero writes.
# ---------------------------------------------------------------------------


def test_superseded_snapshot_fails_closed_and_writes_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registered snapshot with ``superseded_at`` non-NULL → producer
    raises :class:`SupersededGridSnapshotError`; upholds the pinned
    ``grid-drift-lifecycle`` cross-change contract §"Consumers of a
    superseded snapshot fail closed" on the producer-preflight surface.
    """
    superseded_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    def snapshot_row_factory(
        *, source_id: str, grid_id: str, grid_signature: str
    ) -> tuple[float, float, float, float, uuid.UUID, datetime]:
        del source_id, grid_id, grid_signature
        return (
            _DEFAULT_ENV_BBOX.south,
            _DEFAULT_ENV_BBOX.north,
            _DEFAULT_ENV_BBOX.west,
            _DEFAULT_ENV_BBOX.east,
            _REGISTERED_SNAPSHOT_ID,
            superseded_at,
        )

    _contract, store, repository = _make_direct_grid_setup(
        tmp_path, monkeypatch, snapshot_row_factory=snapshot_row_factory
    )
    producer = _build_producer_with_env_reader(tmp_path, repository, store)

    with pytest.raises(SupersededGridSnapshotError) as excinfo:
        producer.produce(
            source_id="gfs", cycle_time="2026050700", model_id="demo_model"
        )

    assert excinfo.value.source_id == "gfs"
    assert excinfo.value.grid_id == "grid_a"
    assert excinfo.value.grid_signature == "sha256:grid-signature-actual"
    assert excinfo.value.grid_snapshot_id == _REGISTERED_SNAPSHOT_ID
    assert excinfo.value.superseded_at == superseded_at
    _assert_zero_direct_grid_production_writes(repository, tmp_path)
    assert repository.cycle_updates[-1]["status"] == "failed_forcing"


# ---------------------------------------------------------------------------
# 5. ValueError from env_reader / finiteness gate → un-swallowed.
# ---------------------------------------------------------------------------


def test_env_reader_ValueError_before_bbox_match_check_propagates_un_swallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the injected ``env_reader`` raises :class:`ValueError` (mirrors
    the ``NHMS_DOWNLOAD_BBOX_*`` malformed-env path), the producer MUST NOT
    catch-and-continue nor wrap it in :class:`ForcingProductionError`. The
    raw ValueError propagates so the operator sees the actual shape-integrity
    failure at the download-env source.
    """

    def failing_env_reader() -> GeoBBox:
        raise ValueError("NHMS_DOWNLOAD_BBOX_SOUTH must be a float, got 'not-a-number'.")

    _contract, store, repository = _make_direct_grid_setup(tmp_path, monkeypatch)
    producer = _build_producer_with_env_reader(
        tmp_path, repository, store, env_reader=failing_env_reader
    )

    with pytest.raises(ValueError, match="NHMS_DOWNLOAD_BBOX_SOUTH"):
        producer.produce(
            source_id="gfs", cycle_time="2026050700", model_id="demo_model"
        )

    # No direct-grid writes and no ForcingProductionError wrapper.
    _assert_zero_direct_grid_production_writes(repository, tmp_path)
    # Symmetric with tests 2/3/4: the cycle status must be marked failed.
    assert repository.cycle_updates[-1]["status"] == "failed_forcing"


def test_finiteness_gate_ValueError_on_snapshot_bbox_matches_env_bit_exactly_propagates_un_swallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the registered snapshot bbox carries a non-finite value (NaN),
    the pinned guard's finiteness gate raises raw :class:`ValueError`. The
    producer MUST NOT wrap it — a corrupted snapshot NaN is a shape-integrity
    failure per SUB-4 policy, not a "mismatch" outcome.
    """

    def snapshot_row_factory(
        *, source_id: str, grid_id: str, grid_signature: str
    ) -> tuple[float, float, float, float, uuid.UUID, Any]:
        del source_id, grid_id, grid_signature
        return (
            float("nan"),  # snapshot-side non-finite → guard's finiteness gate
            _DEFAULT_ENV_BBOX.north,
            _DEFAULT_ENV_BBOX.west,
            _DEFAULT_ENV_BBOX.east,
            _REGISTERED_SNAPSHOT_ID,
            None,
        )

    _contract, store, repository = _make_direct_grid_setup(
        tmp_path, monkeypatch, snapshot_row_factory=snapshot_row_factory
    )
    producer = _build_producer_with_env_reader(tmp_path, repository, store)

    with pytest.raises(ValueError, match="finite"):
        producer.produce(
            source_id="gfs", cycle_time="2026050700", model_id="demo_model"
        )

    _assert_zero_direct_grid_production_writes(repository, tmp_path)
    # Symmetric with tests 2/3/4: the cycle status must be marked failed.
    assert repository.cycle_updates[-1]["status"] == "failed_forcing"
    # Sanity-check that the NaN really was picked up (not spuriously matching
    # some other ValueError path in produce()).
    assert math.isnan(
        repository.find_registered_snapshot_bbox_by_identity(
            source_id="gfs",
            grid_id="grid_a",
            grid_signature="sha256:grid-signature-actual",
        )[0]
    )


# ---------------------------------------------------------------------------
# 6. Cache-hit + mismatched bbox → preflight still fails closed BEFORE the
#    "return existing ready" early-return.
# ---------------------------------------------------------------------------


def test_cache_hit_with_mismatched_bbox_still_fails_closed_before_returning_existing_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``_preflight_direct_grid_bbox`` call-site docstring at
    ``workers/forcing_producer/producer.py`` promises the preflight fires
    BEFORE every direct-grid repository write AND before the cache-hit
    early-return. This test locks that ordering: even when the cache-hit
    branch would otherwise return an existing ready result, a mismatched
    snapshot bbox MUST still raise :class:`BboxMismatchError` and NOT return
    the cached record.
    """
    # Mismatch: shift the snapshot south corner so the guard's bit-exact
    # comparator fails on that field.
    mismatch_south = _DEFAULT_ENV_BBOX.south + 1.0

    def snapshot_row_factory(
        *, source_id: str, grid_id: str, grid_signature: str
    ) -> tuple[float, float, float, float, uuid.UUID, Any]:
        del source_id, grid_id, grid_signature
        return (
            mismatch_south,
            _DEFAULT_ENV_BBOX.north,
            _DEFAULT_ENV_BBOX.west,
            _DEFAULT_ENV_BBOX.east,
            _REGISTERED_SNAPSHOT_ID,
            None,
        )

    _contract, store, repository = _make_direct_grid_setup(
        tmp_path, monkeypatch, snapshot_row_factory=snapshot_row_factory
    )
    producer = _build_producer_with_env_reader(tmp_path, repository, store)

    # Force the cache-hit branch to be "currently valid" so, absent the
    # preflight, ``produce`` WOULD take the early-return path. If the
    # preflight ran AFTER the cache-hit check, this monkeypatch would mask
    # the mismatch and the test would fail because ``produce`` would return
    # an existing ready result instead of raising ``BboxMismatchError``.
    monkeypatch.setattr(
        "workers.forcing_producer.producer.ForcingProducer._existing_forcing_version_is_current",
        lambda *_args, **_kwargs: True,
    )

    with pytest.raises(BboxMismatchError) as excinfo:
        producer.produce(
            source_id="gfs", cycle_time="2026050700", model_id="demo_model"
        )

    assert excinfo.value.grid_snapshot_id == _REGISTERED_SNAPSHOT_ID
    # No cache-hit return happened: no direct-grid writes, no forcing
    # versions, and the last cycle status is the fail-closed marker.
    _assert_zero_direct_grid_production_writes(repository, tmp_path)
    assert repository.cycle_updates[-1]["status"] == "failed_forcing"


# ---------------------------------------------------------------------------
# 7. Regression lock for Fix A (SUB-6 Phase 6): non-preflight ValueError
#    inside the outer try MUST still be wrapped as ForcingProductionError.
# ---------------------------------------------------------------------------


def test_safe_path_component_ValueError_inside_outer_try_is_still_wrapped_as_ForcingProductionError(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix A regression: the un-swallowed ValueError contract is narrowed to
    EXACTLY the preflight env_reader / finiteness gate via the private
    ``_PreflightValueErrorPropagate`` sentinel. A ``ValueError`` from
    :func:`_safe_path_component` on ``resolved_basin_version_id`` inside the
    outer ``produce`` try block MUST still be wrapped as
    :class:`ForcingProductionError` so downstream callers such as
    :func:`services.production_closure.met_validation._run_forcing_production`,
    which only catch :class:`ForcingProductionError`, keep observing the
    wrapped shape. Regression against the previous broad
    ``isinstance(error, ValueError) and not isinstance(error,
    DirectGridContractError)`` catch, which incidentally leaked the raw
    ``ValueError("Invalid path component.")``.
    """
    _contract, store, repository = _make_direct_grid_setup(tmp_path, monkeypatch)
    # Poison the resolved basin_version_id so ``_safe_path_component`` at
    # the top of the outer try raises the raw ValueError("Invalid path
    # component.") that Fix A must re-wrap as ``ForcingProductionError``.
    repository.model_identity_by_model["demo_model"] = {
        "basin_id": "basin_a",
        "basin_version_id": "invalid/basin",
        "river_network_version_id": "rivnet_v1",
    }
    producer = _build_producer_with_env_reader(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match="Invalid path component"):
        producer.produce(
            source_id="gfs", cycle_time="2026050700", model_id="demo_model"
        )

    # Zero direct-grid writes AND the cycle is marked failed by the outer
    # ``except Exception`` -> ``_mark_failed`` path.
    _assert_zero_direct_grid_production_writes(repository, tmp_path)
    assert repository.cycle_updates[-1]["status"] == "failed_forcing"


# ---------------------------------------------------------------------------
# 9. §3.2 SUB-7: delegation to the pinned shared guard (no producer-local
#    re-implementation) — Requirement 1 in
#    ``openspec/changes/direct-grid-build-enablement/specs/producer-bbox-preflight/spec.md``
#    §"The preflight reuses the pinned guard, not a re-implementation".
# ---------------------------------------------------------------------------


def test_preflight_delegates_to_shared_guard_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§3.2 Requirement 1: the producer preflight delegates to the pinned
    ``packages.common.grid_registry_bbox_guard.verify_download_bbox_matches_registry``
    and does not re-implement bbox comparison, finiteness checks, or the
    mismatch error. Locks two invariants:

    1. Identity — the module attribute
       ``workers.forcing_producer.producer.verify_download_bbox_matches_registry``
       IS the same object as
       ``packages.common.grid_registry_bbox_guard.verify_download_bbox_matches_registry``.
       Any producer-local re-implementation would rebind the symbol and
       break this identity assertion.
    2. Delegation call-site — when ``produce`` runs the preflight, the
       shared guard is called EXACTLY once, with the resolved snapshot as
       the first positional arg (satisfying ``RegisteredBboxSnapshotProtocol``)
       and no ``env_reader`` kwarg (so the guard's pinned default
       ``china_buffered_bbox_from_env`` remains the env resolver when the
       producer is built without a test-injected ``env_reader``).
    """
    # Identity invariant — assert BEFORE any monkey-patch so the check
    # reflects the shipped import wiring, not a spy substitute.
    assert (
        _producer_module.verify_download_bbox_matches_registry
        is _guard_module.verify_download_bbox_matches_registry
    ), (
        "producer must import the pinned guard symbol; a producer-local "
        "re-implementation would break bbox-match ⟺ same canonical_grid_key."
    )

    _contract, store, repository = _make_direct_grid_setup(tmp_path, monkeypatch)

    # Spy records the call, then delegates to the real guard so the happy
    # path still lands ``forcing_ready``.
    calls: list[dict[str, Any]] = []
    real_guard = _guard_module.verify_download_bbox_matches_registry

    def spy(*args: Any, **kwargs: Any) -> None:
        calls.append({"args": args, "kwargs": kwargs})
        return real_guard(*args, **kwargs)

    monkeypatch.setattr(
        "workers.forcing_producer.producer.verify_download_bbox_matches_registry",
        spy,
    )

    producer = _build_producer_with_env_reader(tmp_path, repository, store)
    result = producer.produce(
        source_id="gfs", cycle_time="2026050700", model_id="demo_model"
    )

    assert result.status == "forcing_ready"
    assert len(calls) == 1, f"expected exactly one guard call, got {len(calls)}"
    call = calls[0]
    assert len(call["args"]) == 1
    snapshot_arg = call["args"][0]
    assert isinstance(snapshot_arg, RegisteredBboxSnapshotProtocol), (
        "first positional arg must satisfy RegisteredBboxSnapshotProtocol"
    )
    # Producer built with env_reader=None → producer.py:1091-1092 takes the
    # default branch and calls the guard without an ``env_reader`` kwarg.
    assert "env_reader" not in call["kwargs"]


# ---------------------------------------------------------------------------
# 10. §3.2 SUB-7: in-convention (-180..180) env bbox matches the registered
#     snapshot → preflight passes — Requirement 2 Scenario 1 in
#     ``openspec/changes/direct-grid-build-enablement/specs/producer-bbox-preflight/spec.md``
#     §"The producer owns the deployment-bbox longitude convention".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "west_value,east_value,case_id",
    [
        (70.0, 140.0, "positive_only_both_conventions_identical"),
        (-10.0, 140.0, "negative_west_locks_minus_180_180_canonicality"),
    ],
)
def test_in_convention_env_bbox_minus_180_180_matches_snapshot_and_preflight_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    west_value: float,
    east_value: float,
    case_id: str,
) -> None:
    """§3.2 Requirement 2 Scenario 1 (spec.md:40-42) — in-convention env bbox
    matches snapshot passes.

    Parametrized to include a negative-west case (``west=-10.0``) that:

    * Is a valid -180..180 value (``GeoBBox`` tolerates ``-180 <= west <= 360``
      per ``workers/data_adapters/region.py:44``).
    * Would move to ``350.0`` if a producer-local ``% 360`` normalization were
      introduced. Under a normalized-then-compared regression the guard's
      bit-exact comparator would see ``350.0 != -10.0`` and fire — this test
      would then fail, locking the -180..180 canonicality anchor named at
      ``spec.md:41`` (§"The producer owns the deployment-bbox longitude
      convention").
    * The positive-only case (``west=70.0`` / ``east=140.0``) is
      bit-identical in -180..180 and 0..360 conventions, so on its own it
      only proves "matching positive-longitude values pass" — not the
      canonicality invariant. The two cases together lock both.
    """
    del case_id  # documented in the parametrize id; no behavior branch on it.
    monkeypatch.setenv("NHMS_DOWNLOAD_BBOX_WEST", str(west_value))
    monkeypatch.setenv("NHMS_DOWNLOAD_BBOX_EAST", str(east_value))

    def snapshot_row_factory(
        *, source_id: str, grid_id: str, grid_signature: str
    ) -> tuple[float, float, float, float, uuid.UUID, Any]:
        del source_id, grid_id, grid_signature
        return (
            _DEFAULT_ENV_BBOX.south,
            _DEFAULT_ENV_BBOX.north,
            west_value,
            east_value,
            _REGISTERED_SNAPSHOT_ID,
            None,
        )

    _contract, store, repository = _make_direct_grid_setup(
        tmp_path, monkeypatch, snapshot_row_factory=snapshot_row_factory
    )
    producer = _build_producer_with_env_reader(tmp_path, repository, store)
    result = producer.produce(
        source_id="gfs", cycle_time="2026050700", model_id="demo_model"
    )

    assert result.status == "forcing_ready"
    # Writes DID land — proves the in-convention env bbox passed the guard.
    assert repository.direct_grid_station_ensure_count == 1
    assert repository.interp_weight_upsert_count == 1
    assert repository.upsert_count == 1
    assert repository.cycle_updates[-1]["status"] == "forcing_ready"


# ---------------------------------------------------------------------------
# 11. §3.2 SUB-7: cross-convention 0..360 env longitude vs registered
#     -180..180 snapshot → fail closed rather than silently clip — Requirement 2
#     Scenario 2 in
#     ``openspec/changes/direct-grid-build-enablement/specs/producer-bbox-preflight/spec.md``
#     §"The producer owns the deployment-bbox longitude convention".
# ---------------------------------------------------------------------------


def test_cross_convention_east_200_versus_registered_minus_180_180_snapshot_fails_closed_with_BboxMismatchError(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§3.2 Requirement 2 Scenario 2: a 0..360-style deployment ``east=200``
    that does not equal the registered -180..180 snapshot ``east=140`` MUST
    fail closed via :class:`BboxMismatchError` rather than silently clipping
    a shifted region.

    ``GeoBBox.__post_init__`` at ``workers/data_adapters/region.py:41-49``
    tolerates ``east`` up to 360, so ``east=200`` constructs a valid GeoBBox
    (0..360 convention) — the mismatch is caught by the guard's bit-exact
    equality check against the snapshot's -180..180 ``east=140``, not by env
    parsing.

    Non-goal per tasks.md §3.2: no change to the guard's signed-zero /
    finiteness compare semantics; this test only exercises the
    longitude-convention landing.
    """
    monkeypatch.setenv("NHMS_DOWNLOAD_BBOX_EAST", "200.0")

    def snapshot_row_factory(
        *, source_id: str, grid_id: str, grid_signature: str
    ) -> tuple[float, float, float, float, uuid.UUID, Any]:
        del source_id, grid_id, grid_signature
        return (
            _DEFAULT_ENV_BBOX.south,
            _DEFAULT_ENV_BBOX.north,
            _DEFAULT_ENV_BBOX.west,
            140.0,  # registered snapshot east in the canonical -180..180 form
            _REGISTERED_SNAPSHOT_ID,
            None,
        )

    _contract, store, repository = _make_direct_grid_setup(
        tmp_path, monkeypatch, snapshot_row_factory=snapshot_row_factory
    )
    producer = _build_producer_with_env_reader(tmp_path, repository, store)

    with pytest.raises(BboxMismatchError) as excinfo:
        producer.produce(
            source_id="gfs", cycle_time="2026050700", model_id="demo_model"
        )

    # Env side carries the 0..360 east=200; snapshot side carries -180..180
    # east=140 — the guard surfaces both in the mismatch payload so the
    # operator can see the convention mix-up directly.
    assert excinfo.value.expected_bbox["east"] == 200.0
    assert excinfo.value.actual_bbox["east"] == 140.0
    # Symmetric with test 2 mismatch-identity lock: the mismatch payload must
    # also carry the snapshot's grid_snapshot_id so downstream logging can pin
    # the exact registered row that failed the compare.
    assert excinfo.value.grid_snapshot_id == _REGISTERED_SNAPSHOT_ID
    _assert_zero_direct_grid_production_writes(repository, tmp_path)
    # Symmetry with tests 2/3/4/5/6/7 — fail-closed status marker.
    assert repository.cycle_updates[-1]["status"] == "failed_forcing"
