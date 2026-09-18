"""#2156: per-basin and run-scoped tile identity sees in-place geometry rewrites.

`_backfill_output_segment_geometry` rewrites `core.river_segment.geom` and the
STORED `stream_type` under an unchanged network id and bumps
`core.river_network_version.geometry_generation` in the same transaction. The two
national digests already carry that counter; these tests pin the other two
digests that read `core.river_segment`:

* `_river_network_source_version` (per-basin `river-network` tile), whose basis is
  now `id|segment_count|checksum|geometry_generation` per network;
* `_run_source_version` (run-scoped `hydro` tile), fed by BOTH run readers --
  `_run_row` (tile routes, `/api/v1/layers?run_id=`) and `display_ready_run`
  (the `/api/v1/layers` default) -- which must project the same counter, or the
  catalog advertises a `source_version` the tile route never computes.

The readers run their real SQL against an in-memory sqlite catalog carrying the
columns the production tables have, so a projection typo fails here. The real
PostGIS backfill + tile bytes oracle is
`tests/test_mvt_national_identity_probe_integration.py` (node-27).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.errors import ApiError
from apps.api.routes import hydro_display
from services.tiles.mvt import TileInput, cache_key, display_ready_run

_BASIN_VERSION_ID = "bv_2156"
_NETWORK_ID = "rnv_2156"
_SIBLING_NETWORK_ID = "rnv_2156_b"
_MODEL_ID = "model_2156"
_RUN_ID = "run_2156"


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection: Any, _record: Any) -> None:  # pragma: no cover - sqlite hook
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS core")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS hydro")

    with engine.begin() as connection:
        # Column names mirror db/migrations (000002 core, 000057 geometry_generation,
        # 000006 hydro_run, 000059 timeseries_store); sqlite-compatible types.
        connection.execute(
            text(
                """
                CREATE TABLE core.river_network_version (
                    river_network_version_id TEXT PRIMARY KEY,
                    basin_version_id TEXT NOT NULL,
                    segment_count INTEGER,
                    checksum TEXT,
                    geometry_generation INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE core.model_instance (
                    model_id TEXT PRIMARY KEY,
                    basin_version_id TEXT,
                    river_network_version_id TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE hydro.hydro_run (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    model_id TEXT,
                    basin_version_id TEXT,
                    source_id TEXT,
                    cycle_time DATETIME,
                    updated_at DATETIME,
                    timeseries_store TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                "INSERT INTO core.river_network_version VALUES "
                "(:id, :bv, 2, 'sha-a', 0)"
            ),
            {"id": _NETWORK_ID, "bv": _BASIN_VERSION_ID},
        )
        connection.execute(
            text("INSERT INTO core.model_instance VALUES (:model, :bv, :rnv)"),
            {"model": _MODEL_ID, "bv": _BASIN_VERSION_ID, "rnv": _NETWORK_ID},
        )
        connection.execute(
            text(
                "INSERT INTO hydro.hydro_run VALUES "
                "(:run, 'parsed', :model, :bv, 'gfs', :cycle, :updated, 'narrow')"
            ),
            {
                "run": _RUN_ID,
                "model": _MODEL_ID,
                "bv": _BASIN_VERSION_ID,
                "cycle": datetime(2026, 7, 1, tzinfo=UTC),
                "updated": datetime(2026, 7, 1, 3, tzinfo=UTC),
            },
        )
    with Session(engine) as db_session:
        yield db_session
    engine.dispose()


def _set_network(session: Session, network_id: str = _NETWORK_ID, **columns: Any) -> None:
    assignments = ", ".join(f"{name} = :{name}" for name in columns)
    result = session.execute(
        text(f"UPDATE core.river_network_version SET {assignments} WHERE river_network_version_id = :id"),
        {**columns, "id": network_id},
    )
    assert result.rowcount == 1
    session.commit()


def _river_network_version(session: Session) -> str:
    return hydro_display._river_network_source_version(session, _BASIN_VERSION_ID)


def test_river_network_digest_moves_with_each_inventory_member_and_only_then(session: Session) -> None:
    """A-3: generation 0->1, `segment_count` and `checksum` each rotate the digest."""
    baseline = _river_network_version(session)
    assert baseline.startswith("river-network-set:")
    # The trailing id list stays readable and unchanged by the new basis.
    assert baseline.endswith(f":{_NETWORK_ID}")
    # Unchanged state -> byte-identical value.
    assert _river_network_version(session) == baseline

    _set_network(session, geometry_generation=1)
    after_backfill = _river_network_version(session)
    assert after_backfill != baseline, "an in-place geometry rewrite must rotate the per-basin digest"
    assert after_backfill.endswith(f":{_NETWORK_ID}")

    _set_network(session, geometry_generation=0)
    assert _river_network_version(session) == baseline, "the digest is a pure function of the basis"

    _set_network(session, segment_count=3)
    after_count = _river_network_version(session)
    assert after_count not in {baseline, after_backfill}

    _set_network(session, segment_count=2, checksum="sha-b")
    after_checksum = _river_network_version(session)
    assert after_checksum not in {baseline, after_backfill, after_count}


def test_river_network_digest_covers_every_network_of_the_basin_version(session: Session) -> None:
    """The basis is per network: a rewrite on either network rotates the set's digest."""
    session.execute(
        text("INSERT INTO core.river_network_version VALUES (:id, :bv, 5, 'sha-b', 0)"),
        {"id": _SIBLING_NETWORK_ID, "bv": _BASIN_VERSION_ID},
    )
    session.commit()
    baseline = _river_network_version(session)
    assert baseline.endswith(f":{_NETWORK_ID},{_SIBLING_NETWORK_ID}")

    _set_network(session, _SIBLING_NETWORK_ID, geometry_generation=1)

    assert _river_network_version(session) != baseline


def test_river_network_digest_still_404s_for_an_unknown_basin_version(session: Session) -> None:
    with pytest.raises(ApiError) as excinfo:
        hydro_display._river_network_source_version(session, "bv_unknown")
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "MVT_SOURCE_IDENTITY_NOT_FOUND"


def test_both_run_readers_project_the_network_generation_and_agree(session: Session) -> None:
    """A-4: `_run_row` and `display_ready_run` carry the counter; one run, one identity."""
    tile_row = hydro_display._run_row(session, _RUN_ID)
    catalog_row = display_ready_run(session)
    assert catalog_row is not None
    assert tile_row["geometry_generation"] == 0
    assert catalog_row["geometry_generation"] == 0
    baseline = hydro_display._run_source_version(tile_row)
    assert hydro_display._run_source_version(catalog_row) == baseline, (
        "the catalog must advertise the source_version the tile route computes"
    )

    _set_network(session, geometry_generation=1)

    tile_row = hydro_display._run_row(session, _RUN_ID)
    catalog_row = display_ready_run(session)
    assert catalog_row is not None
    assert tile_row["geometry_generation"] == 1
    assert catalog_row["geometry_generation"] == 1
    rotated = hydro_display._run_source_version(tile_row)
    assert rotated != baseline, "an in-place geometry rewrite must rotate the run-scoped digest"
    assert hydro_display._run_source_version(catalog_row) == rotated
    # Only the revision digest moves; the base (network id) does not.
    assert rotated.split(";")[0] == baseline.split(";")[0] == _NETWORK_ID


def test_run_source_version_includes_the_generation_in_its_revision_basis() -> None:
    """Value level, no SQL: the same row with a different counter is a different identity."""
    row = {
        "run_id": _RUN_ID,
        "basin_version_id": _BASIN_VERSION_ID,
        "river_network_version_id": _NETWORK_ID,
        "source_id": "gfs",
        "cycle_time": datetime(2026, 7, 1, tzinfo=UTC),
        "status": "parsed",
        "updated_at": datetime(2026, 7, 1, 3, tzinfo=UTC),
    }

    at_zero = hydro_display._run_source_version({**row, "geometry_generation": 0})

    assert hydro_display._run_source_version({**row, "geometry_generation": 0}) == at_zero
    assert hydro_display._run_source_version({**row, "geometry_generation": 1}) != at_zero
    # A run without a network reads NULL through the LEFT JOIN: still a value.
    assert hydro_display._run_source_version({**row, "geometry_generation": None}) != at_zero


def test_both_route_cache_keys_rotate_with_the_generation(session: Session) -> None:
    """A-5: the tile cache keys the two routes build move with the digests."""

    def keys() -> tuple[str, str]:
        river_network = cache_key(
            TileInput(
                layer_id="river-network",
                source_id=_BASIN_VERSION_ID,
                source_version=hydro_display._river_network_source_version(session, _BASIN_VERSION_ID),
                valid_time=None,
                z=9,
                x=398,
                y=197,
            )
        )
        hydro = cache_key(
            TileInput(
                layer_id="discharge",
                source_id=_RUN_ID,
                source_version=hydro_display._run_source_version(hydro_display._run_row(session, _RUN_ID)),
                valid_time="2026-07-01T02:00:00Z",
                z=9,
                x=398,
                y=197,
                variant_id="variable:q_down",
            )
        )
        return river_network, hydro

    before = keys()
    assert keys() == before

    _set_network(session, geometry_generation=1)
    after = keys()

    assert after[0] != before[0], "the per-basin river-network tile key must rotate"
    assert after[1] != before[1], "the run-scoped hydro tile key must rotate"
