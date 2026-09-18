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

The same fail-closed runs on the bootstrap main path (``_bootstrap_database``);
that leg reaches the degenerate-source state through a re-bootstrap whose
``river.shp`` itself degenerated. A source reach whose ``Type`` is JSON null (a
blank numeric dbf cell) is "no Type" to both the backfill and the check, so it
never blocks the seed.

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
from tests.qhh_production_bootstrap_helpers import _qhh_registry_fixture, _refresh_inventory_and_manifest
from workers.model_registry.qhh_production_bootstrap import (
    QhhProductionBootstrapError,
    _prepare_sources_from_bounded_json,
    bootstrap_qhh_production,
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


def _seed_model_and_reaches(database_url: str, reach_types: dict[int, int | None] = REACH_TYPES) -> None:
    """A model on one network whose source reaches carry geometry and ``Type``; no output rows yet.

    A ``None`` entry in ``reach_types`` stores ``"Type": null`` -- what a blank
    numeric dbf cell becomes after pyshp reads it as ``None``.
    """
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
            (NETWORK_ID, BASIN_VERSION_ID, len(reach_types)),
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
        for index, stream_type in reach_types.items():
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


def _generation(database_url: str, network_id: str = NETWORK_ID) -> int:
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT geometry_generation FROM core.river_network_version WHERE river_network_version_id = %s",
            (network_id,),
        )
        return int(cursor.fetchone()["geometry_generation"])


def _output_types(
    database_url: str, network_id: str = NETWORK_ID, output_ids: tuple[str, ...] = OUTPUT_IDS
) -> list[tuple[str, object, object]]:
    """(id, stored stream_type, properties Type) per output row; a JSON-null or absent Type reads as None."""
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT river_segment_id, stream_type, properties_json->'Type' AS type_json
            FROM core.river_segment
            WHERE river_network_version_id = %s AND river_segment_id = ANY(%s)
            ORDER BY river_segment_id
            """,
            (network_id, list(output_ids)),
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


def test_seed_over_a_source_reach_whose_type_is_json_null_commits(throwaway_database_url: str, tmp_path: Path) -> None:
    """R-1 / D3-1: a source ``"Type": null`` is "no Type", so the seed commits.

    The backfill copies ``Type`` only when the source value is not null, so the
    matching output row legitimately stays without one. A key-existence source
    predicate (``? 'Type'``) counted that row as erased-but-restorable and rolled
    every seed of such a network back, permanently.
    """
    url = throwaway_database_url
    reach_types: dict[int, int | None] = {1: 3, 2: None}
    _seed_model_and_reaches(url, reach_types)
    sp_riv = _write_sp_riv(tmp_path)
    expected_types = [(OUTPUT_IDS[0], 3.0, 3), (OUTPUT_IDS[1], None, None)]

    for expected_generation in (1, 2):
        # First seed creates the rows; a re-seed erases Type in its upsert and
        # the backfill restores it. Both backfills copy geometry for both rows.
        try:
            report = seed_qhh_output_segments(
                database_url=url, model_id=MODEL_ID, sp_riv_path=sp_riv, containment_root=tmp_path
            )
        except QhhProductionBootstrapError as error:
            pytest.fail(f"the seed rolled back over a JSON-null source Type: {error.to_payload()!r}")
        assert report["status"] == "seeded", report
        assert report["geometry_backfilled_count"] == len(reach_types), report
        assert report["geometry_missing_count"] == 0, report
        assert _output_types(url) == expected_types
        assert _generation(url) == expected_generation

    with psycopg_connection(url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT properties_json ? 'Type' AS has_type
            FROM core.river_segment
            WHERE river_network_version_id = %s AND river_segment_id = %s
            """,
            (NETWORK_ID, OUTPUT_IDS[1]),
        )
        assert cursor.fetchone()["has_type"] is False


def _bootstrap_output_ids(model_id: str, count: int) -> tuple[str, ...]:
    return tuple(f"{model_id}_shud_riv_{index:06d}" for index in range(1, count + 1))


def _model_active(database_url: str, model_id: str) -> tuple[bool, str | None]:
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT active_flag, lifecycle_state FROM core.model_instance WHERE model_id = %s",
            (model_id,),
        )
        row = cursor.fetchone()
        return bool(row["active_flag"]), row["lifecycle_state"]


def _degenerate_bootstrap_river_source(
    database_url: str,
    *,
    tmp_path: Path,
    root: Path,
    input_dir: Path,
    inventory_path: Path,
    manifest_path: Path,
    model_id: str,
) -> int:
    """Collapse every ``river.shp`` reach to a zero-length line, on disk AND in the registry.

    The bootstrap re-imports the registry core before seeding, and that import
    fails with ``BASINS_REGISTRY_CHECKSUM_CONFLICT`` unless the stored network
    checksum and reach rows equal what it derives from the files. So the
    shapefile degenerates first, the inventory/manifest are refreshed, and the
    stored rows are set to exactly the values the re-import compares against
    (derived through the bootstrap's own source preparation).
    """
    import shapefile

    # Rewrite river.shp in place: same fields and records (so Type and Length stay),
    # each reach collapsed onto its own first vertex. The .prj is left untouched.
    river_base = str(input_dir / "gis" / "river")
    with shapefile.Reader(river_base) as reader:
        fields = reader.fields[1:]
        first_vertices = [shape.points[0] for shape in reader.shapes()]
        records = [list(record) for record in reader.records()]
    with shapefile.Writer(river_base, shapeType=shapefile.POLYLINE) as writer:
        for field in fields:
            writer.field(*field)
        for (x, y), record in zip(first_vertices, records, strict=True):
            writer.line([[[x, y], [x, y]]])
            writer.record(*record)
    _refresh_inventory_and_manifest(tmp_path, root, inventory_path, manifest_path)
    sources = _prepare_sources_from_bounded_json(inventory_path, manifest_path, model_id=model_id)
    network_id = sources.ids["river_network_version_id"]
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core.river_network_version SET checksum = %s WHERE river_network_version_id = %s",
            (sources.geometry.river_network_checksum, network_id),
        )
        assert cursor.rowcount == 1
        for segment in sources.geometry.river_segments:
            cursor.execute(
                """
                UPDATE core.river_segment
                SET geom = ST_Multi(ST_GeomFromText(%s, 4490)), length_m = %s, properties_json = %s
                WHERE river_segment_id = %s AND river_network_version_id = %s
                """,
                (
                    segment.geom_wkt,
                    segment.length_m,
                    Json(
                        {
                            **segment.properties,
                            "basin_slug": sources.model.get("basin_slug"),
                            "shud_input_name": sources.model.get("shud_input_name"),
                        }
                    ),
                    segment.river_segment_id,
                    network_id,
                ),
            )
            assert cursor.rowcount == 1
        cursor.execute(
            """
            SELECT COUNT(*) AS degenerate
            FROM core.river_segment
            WHERE river_network_version_id = %s
              AND COALESCE(properties_json->>'shud_output_river', 'false') <> 'true'
              AND geom IS NOT NULL AND ST_Length(geom) = 0
              AND properties_json->>'Type' IS NOT NULL
            """,
            (network_id,),
        )
        return int(cursor.fetchone()["degenerate"])


def test_bootstrap_whose_trailing_backfill_restores_nothing_fails_closed(
    throwaway_database_url: str, tmp_path: Path
) -> None:
    """R-2 / D3-1: the bootstrap main path fails closed after its trailing backfill.

    Same state as the seed leg above, reached through ``bootstrap_qhh_production``
    -> ``_bootstrap_database``: output rows carry geometry and ``Type`` from
    earlier runs, then every source reach degenerates, so the re-bootstrap's
    upsert erases ``Type``, its trailing backfill restores nothing, and the
    geometry guard (``geom IS NULL`` only) passes. Only the stream-type check
    stands between that and an erased-``Type`` commit with an unrotated tile
    cache identity.
    """
    url = throwaway_database_url
    apply_migrations_from_zero(url)
    basin_slug = "qhh-stream-type"
    root, input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path, basin_slug=basin_slug)

    def bootstrap() -> dict[str, object]:
        return bootstrap_qhh_production(
            database_url=url,
            basins_root=root,
            qhh_basin_slug=basin_slug,
            model_id=model_id,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
        )

    first = bootstrap()
    assert first["status"] == "bootstrapped" and first["active"] is True, first
    network_id = str(first["river_network_version_id"])
    output_ids = _bootstrap_output_ids(model_id, int(first["output_segment_count"]))
    # The fixture's river.shp gives every reach Type 2.
    healthy_types = [(output_id, 2.0, 2) for output_id in output_ids]
    assert _output_types(url, network_id, output_ids) == healthy_types
    assert _generation(url, network_id) == 1
    # A healthy re-bootstrap erases Type in its upsert and its backfill restores it (+1).
    second = bootstrap()
    assert second["status"] == "bootstrapped", second
    assert _output_types(url, network_id, output_ids) == healthy_types
    generation_before = _generation(url, network_id)
    assert generation_before == 2

    degenerate = _degenerate_bootstrap_river_source(
        url,
        tmp_path=tmp_path,
        root=root,
        input_dir=input_dir,
        inventory_path=inventory_path,
        manifest_path=manifest_path,
        model_id=model_id,
    )
    assert degenerate == len(output_ids)

    raised: QhhProductionBootstrapError | None = None
    try:
        report = bootstrap()
    except QhhProductionBootstrapError as error:
        raised = error
        report = None
    after_types = _output_types(url, network_id, output_ids)
    after_generation = _generation(url, network_id)

    assert raised is not None, (
        f"the bootstrap committed ({report!r}) with the output stream types erased: {after_types!r}, "
        f"geometry_generation {generation_before} -> {after_generation}"
    )
    assert raised.error_code == "QHH_OUTPUT_SEGMENT_STREAM_TYPE_INCOMPLETE", raised.to_payload()
    assert raised.details["river_network_version_id"] == network_id
    assert raised.details["stream_type_missing_count"] == len(output_ids)
    # Rolled back: the upsert's erasure never committed, nothing rotated, and the
    # code is not a persistent-inactive blocker, so the model stays as committed.
    assert after_types == healthy_types
    assert after_generation == generation_before
    assert _model_active(url, model_id) == (True, "active")
