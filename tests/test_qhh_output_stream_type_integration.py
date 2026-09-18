"""The QHH output-segment seed cannot commit an erased stream type (#2154 D3-1).

``seed_qhh_output_segments`` upserts the output rows with ``ON CONFLICT DO
UPDATE SET properties_json = EXCLUDED.properties_json``, which drops the
``Type`` an earlier backfill copied in and so NULLs the STORED ``stream_type``.
The trailing ``_backfill_output_segment_geometry(..., only_missing=False)`` on
the same cursor is what restores it (and bumps ``geometry_generation``). When
that backfill updates nothing -- here because every source reach's geometry is
degenerate and its ``ST_Length(source.geom) > 0`` filter drops the batch -- the
erasure used to commit silently with the tile cache identity unrotated. The seed
now fails closed with ``QHH_OUTPUT_SEGMENT_STREAM_TYPE_INCOMPLETE`` and the
transaction rolls back.

Real DB only: ``stream_type`` is a STORED generated column (000048). Runs on a
per-test throwaway database. File name stays clear of the frozen
``qhh_production_bootstrap`` test corpus (its partition oracle pins that name
set) and carries ``integration`` for ci.yml's ``database`` filter; no selector
row is needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from psycopg2.extras import Json

from tests.integration_helpers import apply_migrations_from_zero, psycopg_connection
from workers.model_registry.qhh_production_bootstrap import (
    QhhProductionBootstrapError,
    seed_qhh_output_segments,
)

pytestmark = pytest.mark.integration

_PREFIX = "it2154"
MODEL_ID = f"{_PREFIX}_model"
BASIN_ID = f"{_PREFIX}_basin"
BASIN_VERSION_ID = f"{_PREFIX}_basin_v1"
NETWORK_ID = f"{_PREFIX}_rnv"
MESH_ID = f"{_PREFIX}_mesh"
REACH_TYPES = {1: 3, 2: 5}
OUTPUT_IDS = tuple(f"{MODEL_ID}_shud_riv_{index:06d}" for index in REACH_TYPES)


def _seed_model_and_reaches(database_url: str) -> None:
    """A model on one network whose source reaches carry geometry and ``Type``; no output rows yet."""
    apply_migrations_from_zero(database_url)
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO core.basin (basin_id, basin_name, basin_group, description) VALUES (%s, %s, %s, %s)",
            (BASIN_ID, "Issue 2154 Basin", "integration", "Stream-type fail-closed basin."),
        )
        cursor.execute(
            """
            INSERT INTO core.basin_version (
                basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum
            )
            VALUES (%s, %s, 'v1', ST_Multi(ST_MakeEnvelope(109.0, 29.0, 112.0, 32.0, 4490)),
                    true, 'integration://basin', 'basin-sha')
            """,
            (BASIN_VERSION_ID, BASIN_ID),
        )
        cursor.execute(
            """
            INSERT INTO core.river_network_version (
                river_network_version_id, basin_version_id, version_label, segment_count, source_uri, checksum
            )
            VALUES (%s, %s, 'v1', %s, 'integration://river-network', 'rnv-sha')
            """,
            (NETWORK_ID, BASIN_VERSION_ID, len(REACH_TYPES)),
        )
        cursor.execute(
            """
            INSERT INTO core.mesh_version (mesh_version_id, basin_version_id, version_label, mesh_uri)
            VALUES (%s, %s, 'v1', 'integration://mesh')
            """,
            (MESH_ID, BASIN_VERSION_ID),
        )
        cursor.execute(
            """
            INSERT INTO core.model_instance (
                model_id, basin_version_id, river_network_version_id, mesh_version_id,
                calibration_version_id, shud_code_version, model_package_uri
            )
            VALUES (%s, %s, %s, %s, 'cal-v1', 'shud-test', 'integration://package/')
            """,
            (MODEL_ID, BASIN_VERSION_ID, NETWORK_ID, MESH_ID),
        )
        for index, stream_type in REACH_TYPES.items():
            cursor.execute(
                """
                INSERT INTO core.river_segment (
                    river_segment_id, river_network_version_id, segment_order, length_m, geom, properties_json
                )
                VALUES (%s, %s, %s, %s, ST_Multi(ST_GeomFromText(%s, 4490)), %s)
                """,
                (
                    f"{MODEL_ID}_reach_{index:06d}",
                    NETWORK_ID,
                    index,
                    1000.0 * index,
                    f"LINESTRING(110.{index} 30.0, 110.{index} 30.5)",
                    Json({"iRiv": str(index), "Type": stream_type}),
                ),
            )


def _write_sp_riv(root: Path) -> Path:
    path = root / f"{_PREFIX}.sp.riv"
    rows = "\n".join(f"{index} 0 1 0.001 100 0" for index in REACH_TYPES)
    path.write_text(f"{len(REACH_TYPES)} 6\nIndex Down Type Slope Length BC\n{rows}\n", encoding="utf-8")
    return path


def _generation(database_url: str) -> int:
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT geometry_generation FROM core.river_network_version WHERE river_network_version_id = %s",
            (NETWORK_ID,),
        )
        return int(cursor.fetchone()["geometry_generation"])


def _output_types(database_url: str) -> list[tuple[str, object, object]]:
    """(id, stored stream_type, properties Type) per output row."""
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT river_segment_id, stream_type, properties_json->'Type' AS type_json
            FROM core.river_segment
            WHERE river_network_version_id = %s AND river_segment_id = ANY(%s)
            ORDER BY river_segment_id
            """,
            (NETWORK_ID, list(OUTPUT_IDS)),
        )
        return [(row["river_segment_id"], row["stream_type"], row["type_json"]) for row in cursor.fetchall()]


def test_seed_whose_trailing_backfill_restores_nothing_fails_closed(
    throwaway_database_url: str, tmp_path: Path
) -> None:
    """B-5 / D3-1: a healthy seed passes; a seed over degenerate source reaches rolls back.

    Degenerate (zero-length) source geometry keeps ``geom IS NOT NULL`` and
    ``Type``, so the fail-closed predicate -- which deliberately omits the
    backfill's ``ST_Length > 0`` -- counts every output row the upsert erased,
    while the backfill itself updates nothing and bumps nothing.
    """
    url = throwaway_database_url
    _seed_model_and_reaches(url)
    sp_riv = _write_sp_riv(tmp_path)

    def seed() -> dict[str, object]:
        return seed_qhh_output_segments(
            database_url=url, model_id=MODEL_ID, sp_riv_path=sp_riv, containment_root=tmp_path
        )

    healthy_types = [
        (OUTPUT_IDS[0], float(REACH_TYPES[1]), REACH_TYPES[1]),
        (OUTPUT_IDS[1], float(REACH_TYPES[2]), REACH_TYPES[2]),
    ]
    # First seed creates the rows; its backfill copies geometry and Type (+1).
    first = seed()
    assert first["status"] == "seeded" and first["geometry_backfilled_count"] == len(REACH_TYPES), first
    assert _output_types(url) == healthy_types
    assert _generation(url) == 1
    # A healthy re-seed erases Type in its upsert and its backfill restores it (+1).
    second = seed()
    assert second["status"] == "seeded", second
    assert _output_types(url) == healthy_types
    generation_before = _generation(url)
    assert generation_before == 2

    # Every source reach degenerates to a zero-length line.
    with psycopg_connection(url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE core.river_segment
            SET geom = ST_Multi(ST_GeomFromText('LINESTRING(110.0 30.0, 110.0 30.0)', 4490))
            WHERE river_network_version_id = %s AND properties_json ? 'iRiv'
            """,
            (NETWORK_ID,),
        )
        assert cursor.rowcount == len(REACH_TYPES)

    raised: QhhProductionBootstrapError | None = None
    try:
        report = seed()
    except QhhProductionBootstrapError as error:
        raised = error
        report = None
    after_types = _output_types(url)
    after_generation = _generation(url)

    assert raised is not None, (
        f"the seed committed ({report!r}) with the output stream types erased: {after_types!r}, "
        f"geometry_generation {generation_before} -> {after_generation}"
    )
    assert raised.error_code == "QHH_OUTPUT_SEGMENT_STREAM_TYPE_INCOMPLETE", raised.to_payload()
    assert raised.details["river_network_version_id"] == NETWORK_ID
    assert raised.details["stream_type_missing_count"] == len(REACH_TYPES)
    # Rolled back: the upsert's erasure never committed, and nothing rotated.
    assert after_types == healthy_types
    assert after_generation == generation_before
