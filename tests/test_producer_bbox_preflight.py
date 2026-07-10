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

Runs only under the ``-k "match or mismatch or missing or superseded"``
filter documented in ``openspec/changes/direct-grid-build-enablement/tasks.md``
§3.1 Evidence Floor; each name matches at least one of those keywords.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.grid_registry_bbox_guard import BboxMismatchError
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
from workers.forcing_producer.producer import (
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
    # Sanity-check that the NaN really was picked up (not spuriously matching
    # some other ValueError path in produce()).
    assert math.isnan(
        repository.find_registered_snapshot_bbox_by_identity(
            source_id="gfs",
            grid_id="grid_a",
            grid_signature="sha256:grid-signature-actual",
        )[0]
    )
