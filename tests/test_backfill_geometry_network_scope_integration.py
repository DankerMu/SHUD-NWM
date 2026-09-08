"""Output-geometry backfill: the write side must stay inside one river network version.

Issue #2158.  This contract lives in its own file rather than in
``tests/test_basins_registry_import_db.py`` because #1913 froze the entire test
inventory of the seven ``tests/test_basins_registry_import*.py`` partitions in
``tests/fixtures/basins_registry_partition_oracle.json`` -- node counts,
per-definition AST digests and execution-semantics counts are equality
assertions (``tests/test_select_ci_tests.py:14486/14497/14512/14588``), so
adding one test there reddens four guards, and those guards forbid
regenerating the baseline from the object under test.  The file name stays
clear of the ``test_basins_registry_import`` and ``qhh_production_bootstrap``
corpora and carries ``integration`` so ``.github/workflows/ci.yml``'s
``database`` filter (``tests/*integration*.py``) opens the lane; the
``real-db-integration`` job runs the whole tree under
``-m "integration and not timescaledb_210"``, so no selector row is needed.
"""

from __future__ import annotations

from typing import Any

import pytest
from psycopg2.extras import Json

from tests.integration_helpers import apply_migrations_from_zero, psycopg_connection
from workers.model_registry.basins_registry_import import _backfill_output_segment_geometry

pytestmark = pytest.mark.integration


def test_output_geometry_backfill_only_touches_the_target_network_version(
    integration_database_url: str,
) -> None:
    """#2158: the backfill's UPDATE must match the full composite primary key.

    ``core.river_segment``'s PK is ``(river_segment_id, river_network_version_id)``
    (``db/migrations/000004_core.sql:42``) and the output id prefix is the
    ``model_id``, not the network version, so the same id text may legitimately
    exist under two networks. Both candidate SELECTs already scope by network;
    before this fix the write side matched on ``river_segment_id`` alone and a
    backfill for A also rewrote B's same-id row (``geom``, ``length_m``,
    ``properties_json`` and the STORED ``stream_type`` derived from it), counted
    it in the ``RETURNING`` total that reaches receipts, and bumped
    ``geometry_generation`` only on A -- leaving B's tile cache key serving
    A's geometry forever.

    A and B carry deliberately different reach geometry, ``length_m`` and
    ``Type`` so assertion (b) can tell "not written" apart from "written with
    A's values".
    """
    apply_migrations_from_zero(integration_database_url)
    prefix = "it2158"
    basin_id = f"{prefix}_basin"
    basin_version_id = f"{prefix}_basin_v1"
    network_a = f"{prefix}_rnv_a"
    network_b = f"{prefix}_rnv_b"
    reach_id = f"{prefix}_reach_000001"
    # The whole point: identical id text under both networks.
    output_id = f"{prefix}_shud_riv_000001"
    reach_wkt_a = "LINESTRING(110.0 30.0, 110.6 30.6)"
    reach_wkt_b = "LINESTRING(116.0 36.0, 116.6 36.6)"

    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            # Re-runnable against the session database: FK-safe delete order.
            cursor.execute("DELETE FROM core.river_segment WHERE river_segment_id LIKE %s", (f"{prefix}_%",))
            cursor.execute(
                "DELETE FROM core.river_network_version WHERE river_network_version_id LIKE %s",
                (f"{prefix}_%",),
            )
            cursor.execute("DELETE FROM core.basin_version WHERE basin_version_id LIKE %s", (f"{prefix}_%",))
            cursor.execute("DELETE FROM core.basin WHERE basin_id LIKE %s", (f"{prefix}_%",))

            cursor.execute(
                """
                INSERT INTO core.basin (basin_id, basin_name, basin_group, description)
                VALUES (%s, %s, %s, %s)
                """,
                (basin_id, "Issue 2158 Basin", "integration", "Backfill network-scope basin."),
            )
            cursor.execute(
                """
                INSERT INTO core.basin_version (
                    basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum
                )
                VALUES (
                    %s, %s, 'v1', ST_Multi(ST_MakeEnvelope(109.0, 29.0, 112.0, 32.0, 4490)),
                    true, 'integration://basin', 'basin-sha'
                )
                """,
                (basin_version_id, basin_id),
            )
            for network_id, version_label in ((network_a, "v-a"), (network_b, "v-b")):
                cursor.execute(
                    """
                    INSERT INTO core.river_network_version (
                        river_network_version_id, basin_version_id, version_label,
                        segment_count, source_uri, checksum
                    )
                    VALUES (%s, %s, %s, 2, 'integration://river-network', %s)
                    """,
                    (network_id, basin_version_id, version_label, f"rnv-sha-{version_label}"),
                )

            for network_id, reach_wkt, reach_length, stream_type in (
                (network_a, reach_wkt_a, 1200.0, 3),
                (network_b, reach_wkt_b, 1800.0, 4),
            ):
                # Source reach row: geom is geometry(MultiLineString, 4490) (000036),
                # so ST_Multi wraps the LineString fixture. `Type` is seeded as a JSON
                # number because the STORED `stream_type` (000048) casts
                # `properties_json->>'Type'` and the backfill copies the jsonb value
                # through provenance.
                cursor.execute(
                    """
                    INSERT INTO core.river_segment (
                        river_segment_id, river_network_version_id, segment_order,
                        length_m, geom, properties_json
                    )
                    VALUES (%s, %s, 1, %s, ST_Multi(ST_GeomFromText(%s, 4490)), %s)
                    """,
                    (reach_id, network_id, reach_length, reach_wkt, Json({"iRiv": "1", "Type": stream_type})),
                )
                cursor.execute(
                    """
                    INSERT INTO core.river_segment (
                        river_segment_id, river_network_version_id, segment_order,
                        length_m, geom, properties_json
                    )
                    VALUES (%s, %s, 2, NULL, NULL, %s)
                    """,
                    (
                        output_id,
                        network_id,
                        Json({"shud_output_river": "true", "shud_riv_index": "1"}),
                    ),
                )

            def _output_row(network_id: str) -> tuple[Any, Any, Any, Any]:
                cursor.execute(
                    """
                    SELECT ST_AsText(geom) AS geom_wkt, length_m, properties_json, stream_type
                    FROM core.river_segment
                    WHERE river_segment_id = %s AND river_network_version_id = %s
                    """,
                    (output_id, network_id),
                )
                row = cursor.fetchone()
                return (row["geom_wkt"], row["length_m"], row["properties_json"], row["stream_type"])

            def _generation(network_id: str) -> int:
                cursor.execute(
                    """
                    SELECT geometry_generation
                    FROM core.river_network_version
                    WHERE river_network_version_id = %s
                    """,
                    (network_id,),
                )
                return int(cursor.fetchone()["geometry_generation"])

            cursor.execute(
                """
                SELECT ST_AsText(geom) AS geom_wkt
                FROM core.river_segment
                WHERE river_segment_id = %s AND river_network_version_id = %s
                """,
                (reach_id, network_a),
            )
            source_geom_a = cursor.fetchone()["geom_wkt"]
            b_before = _output_row(network_b)
            generation_a_before = _generation(network_a)
            generation_b_before = _generation(network_b)
            assert b_before[0] is None and b_before[3] is None, b_before

            updated = _backfill_output_segment_geometry(cursor, network_a)

            a_after = _output_row(network_a)
            b_after = _output_row(network_b)
            generation_a_after = _generation(network_a)
            generation_b_after = _generation(network_b)

    # (a) A's output row carries A's reach geometry, length and stream class.
    assert a_after[0] == source_geom_a, f"A output geom {a_after[0]!r} != A reach geom {source_geom_a!r}"
    assert a_after[1] == 1200.0, f"A output length_m is {a_after[1]!r}, expected A's reach length 1200.0"
    assert a_after[2]["Type"] == 3, f"A output provenance Type is {a_after[2]!r}, expected Type 3"
    assert a_after[3] == 3.0, f"A output stream_type is {a_after[3]!r}, expected 3.0"
    # (b) B's same-id output row is byte-identical to its pre-call snapshot: not
    # rewritten at all, and in particular not rewritten with A's values.
    assert b_after == b_before, (
        f"backfill for {network_a} rewrote the same-id row in {network_b}: {b_before!r} -> {b_after!r} "
        f"(A's reach geometry is {source_geom_a!r})"
    )
    assert b_after[0] is None, f"B output geom must stay NULL, got {b_after[0]!r}"
    assert b_after[3] is None, f"B output stream_type must stay NULL, got {b_after[3]!r}"
    # (c) only the target network's tile cache key rotates.
    assert generation_a_after == generation_a_before + 1, (
        f"A geometry_generation {generation_a_before} -> {generation_a_after}, expected +1"
    )
    assert generation_b_after == generation_b_before, (
        f"B geometry_generation {generation_b_before} -> {generation_b_after}, expected unchanged"
    )
    # (d) the count that reaches receipts counts only the target network.
    assert updated == 1, f"backfill returned {updated}, expected 1 (only {network_a}'s output row)"
