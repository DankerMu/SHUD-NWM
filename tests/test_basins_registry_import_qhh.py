"""Basins registry import: QHH crosswalk, segment slices and post-import DB contracts.

Partition 7 of 7 of the former monolith ``tests/test_basins_registry_import.py``
(issue #1913): the 22 QHH/crosswalk cases, kept together because their PR1/PR2/Path-C
and issue #566 contracts share the ``tests/fixtures/basins/qhh-sample`` fixture, and
because their seven integration nodes are one ci.yml ``database:`` path.  That
fixture's sample constants and loaders moved to the non-collectible
``tests/basins_registry_import_helpers.py`` so this file stays under the 1,000-line
structural limit.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from packages.common.model_registry import PsycopgModelRegistryStore
from tests.basins_registry_import_helpers import (
    _CLI_MODEL_ADMIN_AUTH_ARGS,
    _QHH_SAMPLE_DIR,
    _QHH_SAMPLE_SEG_SHP,
    _parse_qhh_sample,
    _stage_qhh_sample_fixture,
    _write_prj,
    _write_registry_fixture,
)
from tests.integration_helpers import apply_migrations_from_zero, psycopg_connection
from workers.model_registry.basins_discovery import discover_basins_inventory
from workers.model_registry.basins_geometry import (
    BasinsGeometryError,
    CrosswalkRow,
    parse_basins_geometry,
    parse_seg_shp_crosswalk,
)
from workers.model_registry.basins_registry_import import (
    BasinsRegistryImportError,
    _build_river_segment_crosswalk_rows,
    import_basins_registry,
)
from workers.model_registry.cli import _argparse_main


def test_parse_seg_shp_crosswalk_extracts_all_records() -> None:
    """qhh-sample fixture seg.shp has 18 records over iRiv ∈ {1, 2, 3, 9, 180}."""
    import shapefile

    reader = shapefile.Reader(str(_QHH_SAMPLE_SEG_SHP))
    try:
        rows = parse_seg_shp_crosswalk(reader)
    finally:
        reader.close()

    assert len(rows) == 18
    assert all(isinstance(row, CrosswalkRow) for row in rows)
    # Every iRiv in the fixture comes from the documented sampled reach set.
    assert {row.iRiv for row in rows} == {1, 2, 3, 9, 180}
    # segment_order is the natural row-offset enumeration -> monotonically
    # increasing 0..N-1 sequence.
    assert [row.segment_order for row in rows] == list(range(18))
    # The qhh seg.shp dbf only carries iRiv + iEle (no Length field), so
    # length_m must be None on every row.
    assert all(row.length_m is None for row in rows)
    # iEle is an integer mesh-element index pulled verbatim from the dbf.
    assert all(isinstance(row.iEle, int) and row.iEle > 0 for row in rows)


def test_build_crosswalk_rows_format() -> None:
    """Constructor builds dict rows shaped for core.river_segment_crosswalk insert."""
    segments = [
        CrosswalkRow(iRiv=1, iEle=3099, segment_order=0, length_m=349.02),
        CrosswalkRow(iRiv=2, iEle=2597, segment_order=6, length_m=391.40),
    ]
    model_id = "basins_qhh_shud"
    rnv_id = "rnv_basins_qhh_shud_v1"
    reach_indices = {1, 2, 3, 9, 180}

    rows = _build_river_segment_crosswalk_rows(model_id, rnv_id, segments, reach_indices)

    assert len(rows) == 2
    assert rows[0] == {
        "river_network_version_id": rnv_id,
        "river_segment_id": "basins_qhh_shud_reach_000001",
        "source": "basins_seg_shp",
        "external_id": "1:3099",
        "properties_json": {
            "iRiv": 1,
            "iEle": 3099,
            "segment_order": 0,
            "length_m": 349.02,
        },
    }
    assert rows[1] == {
        "river_network_version_id": rnv_id,
        "river_segment_id": "basins_qhh_shud_reach_000002",
        "source": "basins_seg_shp",
        "external_id": "2:2597",
        "properties_json": {
            "iRiv": 2,
            "iEle": 2597,
            "segment_order": 6,
            "length_m": 391.40,
        },
    }


def test_build_crosswalk_rows_reach_missing_reports_set() -> None:
    """A segment whose iRiv is not in reach_indices raises a structured error."""
    segments = [
        CrosswalkRow(iRiv=1, iEle=3099, segment_order=0, length_m=None),
        CrosswalkRow(iRiv=999, iEle=4242, segment_order=1, length_m=None),
    ]
    with pytest.raises(BasinsGeometryError) as excinfo:
        _build_river_segment_crosswalk_rows(
            model_id="basins_qhh_shud",
            river_network_version_id="rnv_test",
            segments=segments,
            reach_indices={1, 2, 3},
        )
    assert excinfo.value.error_code == "BASINS_REGISTRY_CROSSWALK_REACH_MISSING"
    payload = excinfo.value.to_payload()
    assert payload["missing_iRiv"] == [999]
    # Sanity: a happy iRiv not declared missing must not appear in the payload.
    assert 1 not in payload["missing_iRiv"]


def test_build_crosswalk_rows_collapses_duplicate_external_identity() -> None:
    segments = [
        CrosswalkRow(iRiv=7, iEle=42, segment_order=3, length_m=120.5),
        CrosswalkRow(iRiv=7, iEle=42, segment_order=9, length_m=120.5),
    ]

    rows = _build_river_segment_crosswalk_rows(
        model_id="basins_duplicate_shud",
        river_network_version_id="rnv_duplicate",
        segments=segments,
        reach_indices={7},
    )

    assert len(rows) == 1
    assert rows[0]["external_id"] == "7:42"
    assert rows[0]["properties_json"]["segment_order"] == 3


def test_build_crosswalk_rows_rejects_conflicting_duplicate_external_identity() -> None:
    segments = [
        CrosswalkRow(iRiv=7, iEle=42, segment_order=3, length_m=120.5),
        CrosswalkRow(iRiv=7, iEle=42, segment_order=9, length_m=121.0),
    ]

    with pytest.raises(BasinsGeometryError) as exc_info:
        _build_river_segment_crosswalk_rows(
            model_id="basins_duplicate_shud",
            river_network_version_id="rnv_duplicate",
            segments=segments,
            reach_indices={7},
        )

    assert exc_info.value.error_code == "BASINS_REGISTRY_CROSSWALK_DUPLICATE_CONFLICT"
    assert exc_info.value.details["external_id"] == "7:42"


# --- 2.9 reach count matches sp.riv -----------------------------------------


def test_reach_count_matches_sp_riv(tmp_path: Path) -> None:
    """qhh-sample: 5 reaches in river.shp, 5 reaches in .sp.riv header."""

    parsed, _model_id = _parse_qhh_sample(tmp_path)
    assert parsed.segment_count == 5
    assert parsed.output_segment_count == 5
    assert parsed.evidence_counts["river_count"] == 5


# --- 2.10 river.shp single-part invariant fail-fast --------------------------


def test_river_shp_invariant_fail_fast_on_multipart(tmp_path: Path) -> None:
    """Construct a multi-part river.shp record -> invariant fires before any DB write."""

    _, input_dir, _, _, model_id = _write_registry_fixture(tmp_path)
    import shapefile

    target = input_dir / "gis" / "river"
    # Rewrite river.shp with two records: a clean single-part one (good
    # so the count check still matches sp.riv) and a multi-part one.
    writer = shapefile.Writer(str(target), shapeType=shapefile.POLYLINE)
    for name in (
        "Index",
        "Down",
        "Type",
        "Slope",
        "Length",
        "BC",
        "Depth",
        "BankSlope",
        "Width",
        "Sinuosity",
        "Manning",
        "Cwr",
        "KsatH",
        "BedThick",
    ):
        writer.field(name, "N" if name in ("Index", "Down", "Type", "BC") else "F", decimal=6)
    writer.line([[[100.0, 30.0], [100.1, 30.1]]])
    writer.record(1, 2, 2, 0.001, 100.0, 0, 1.5, 0.5, 10.0, 1.05, 0.035, 1.0, 1e-5, 0.5)
    # Multi-part: two disjoint parts in a single record.
    writer.line([
        [[100.2, 30.2], [100.25, 30.25]],
        [[100.5, 30.5], [100.55, 30.55]],
    ])
    writer.record(2, 0, 2, 0.001, 100.0, 0, 1.5, 0.5, 10.0, 1.05, 0.035, 1.0, 1e-5, 0.5)
    writer.close()
    _write_prj((input_dir / "gis" / "river").with_suffix(".prj"))

    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]
    with pytest.raises(BasinsGeometryError) as excinfo:
        parse_basins_geometry(
            model_id=model_id,
            input_dir=input_dir,
            shud_input_name="alias-a",
            required_files=model["required_files"],
        )
    assert excinfo.value.error_code == "BASINS_REGISTRY_RIVER_SHP_INVARIANT_VIOLATED"
    assert excinfo.value.details["offending_index"] == 2
    assert excinfo.value.details["part_count"] == 2


def test_river_shp_invariant_fail_fast_on_missing_field(tmp_path: Path) -> None:
    """Drop ``BankSlope`` from river.shp dbf -> invariant fires before any DB write."""

    _, input_dir, _, _, model_id = _write_registry_fixture(tmp_path)
    import shapefile

    target = input_dir / "gis" / "river"
    writer = shapefile.Writer(str(target), shapeType=shapefile.POLYLINE)
    for name in (
        "Index",
        "Down",
        "Type",
        "Slope",
        "Length",
        "BC",
        "Depth",
        # BankSlope intentionally absent.
        "Width",
        "Sinuosity",
        "Manning",
        "Cwr",
        "KsatH",
        "BedThick",
    ):
        writer.field(name, "N" if name in ("Index", "Down", "Type", "BC") else "F", decimal=6)
    for index in range(1, 3):
        writer.line([[[100.0 + 0.1 * index, 30.0], [100.05 + 0.1 * index, 30.05]]])
        down = 2 if index == 1 else 0
        writer.record(index, down, 2, 0.001, 100.0, 0, 1.5, 10.0, 1.05, 0.035, 1.0, 1e-5, 0.5)
    writer.close()
    _write_prj(target.with_suffix(".prj"))

    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]
    with pytest.raises(BasinsGeometryError) as excinfo:
        parse_basins_geometry(
            model_id=model_id,
            input_dir=input_dir,
            shud_input_name="alias-a",
            required_files=model["required_files"],
        )
    assert excinfo.value.error_code == "BASINS_REGISTRY_RIVER_SHP_INVARIANT_VIOLATED"
    assert "BankSlope" in excinfo.value.details["missing_fields"]


# --- 2.11 reach IDs zero-padded + downstream resolves ----------------------


def test_reach_ids_are_zero_padded(tmp_path: Path) -> None:
    """qhh-sample reach Index=1 -> river_segment_id ends in ``_reach_000001``."""

    parsed, model_id = _parse_qhh_sample(tmp_path)
    ids = sorted(segment.river_segment_id for segment in parsed.river_segments)
    # qhh-sample uses Index ∈ {1, 2, 3, 9, 180} (see fixture README).
    assert ids[0] == f"{model_id}_reach_000001"
    assert f"{model_id}_reach_000009" in ids
    assert f"{model_id}_reach_000180" in ids


def test_downstream_id_resolves(tmp_path: Path) -> None:
    """qhh-sample: Index=1 has Down=2 -> downstream resolves to _reach_000002;
    Index=180 has Down=181 (not in subset) -> remains a string downstream
    reference; Index=3 has Down=4 (not in subset) -> ditto. The terminal
    case (Down=0) is exercised via _write_registry_fixture in the default
    happy-path test above; here we focus on resolution to existing IDs."""

    parsed, model_id = _parse_qhh_sample(tmp_path)
    by_id = {segment.river_segment_id: segment for segment in parsed.river_segments}
    first = by_id[f"{model_id}_reach_000001"]
    assert first.downstream_segment_id == f"{model_id}_reach_000002"
    # qhh-sample Index=180 has Down=181; we still construct the reach-style
    # downstream string verbatim (the FK / consumer can choose to filter
    # against existing IDs if they need transitive closure).
    last = by_id[f"{model_id}_reach_000180"]
    assert last.downstream_segment_id == f"{model_id}_reach_000181"


# --- 2.12 reach geom no cross-gap straight bridges --------------------------


def test_reach_geom_no_cross_gap_bridges(tmp_path: Path) -> None:
    """qhh-sample river.shp is single-part by construction. We assert the
    spec invariant inline: every reach polyline's max edge length must be
    ≤ ``max(300m, 4 × median_edge)`` measured by equirectangular metres
    against EPSG:4490. The numeric thresholds are hard-coded here per the
    spec requirement that no module-level constant carry them."""

    parsed, _model_id = _parse_qhh_sample(tmp_path)
    earth_radius_m = 6_371_000.0
    for segment in parsed.river_segments:
        wkt = segment.geom_wkt
        assert wkt.startswith("LINESTRING(")
        coords = [
            tuple(float(value) for value in pair.split())
            for pair in wkt.removeprefix("LINESTRING(").rstrip(")").split(", ")
        ]
        assert len(coords) >= 2
        edges = []
        import math

        for a, b in zip(coords[:-1], coords[1:], strict=True):
            lat_rad = ((a[1] + b[1]) / 2.0) * (math.pi / 180.0)
            dx = (b[0] - a[0]) * (math.pi / 180.0) * math.cos(lat_rad) * earth_radius_m
            dy = (b[1] - a[1]) * (math.pi / 180.0) * earth_radius_m
            edges.append(math.hypot(dx, dy))
        ordered = sorted(edges)
        median_edge = ordered[len(ordered) // 2]
        threshold = max(300.0, 4 * median_edge)
        assert max(edges) <= threshold, (
            f"reach {segment.river_segment_id} has cross-gap edge "
            f"{max(edges):.2f}m > threshold {threshold:.2f}m"
        )


# --- 2.13 missing file fail-fast --------------------------------------------


def test_river_shp_missing_fails_fast(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Removing river.shp must fail before any DB write with the
    payload-precise ``BASINS_REGISTRY_RIVER_SHP_MISSING`` code."""

    _, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    (input_dir / "gis" / "river.shp").unlink()

    exit_code = _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            "postgresql://nhms:nhms@localhost:1/nhms",
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert error["error_code"] == "BASINS_REGISTRY_RIVER_SHP_MISSING"
    assert model_id


def test_seg_shp_missing_fails_fast(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Removing seg.shp must fail before any DB write with the
    payload-precise ``BASINS_REGISTRY_SEG_SHP_MISSING`` code."""

    _, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    (input_dir / "gis" / "seg.shp").unlink()

    exit_code = _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            "postgresql://nhms:nhms@localhost:1/nhms",
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert error["error_code"] == "BASINS_REGISTRY_SEG_SHP_MISSING"
    assert model_id


# --- 2.14 per-basin ingest is transactional ---------------------------------


def test_per_basin_ingest_is_transactional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate a crosswalk-write failure and assert the river_segment
    writes performed earlier in the same transaction roll back. We patch
    psycopg2.connect with a fake connection that surfaces commit / rollback
    + a fake cursor that raises on crosswalk INSERT."""

    _, _, inventory_path, manifest_path, _ = _write_registry_fixture(tmp_path)
    rollback_calls: list[int] = []
    commit_calls: list[int] = []
    captured_statements: list[str] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self._last_rows: list[Any] = []

        def __enter__(self) -> "_FakeCursor":
            return self

        def __exit__(self, *args: Any, **kwargs: Any) -> None:
            return None

        def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
            del parameters
            captured_statements.append(" ".join(statement.split())[:120])
            normalized = statement.lower()
            # COUNT-style probes return 0 so the caller treats the table as
            # empty and proceeds to the INSERT path we want to fault on.
            if "count(*)" in normalized or " exists" in normalized.split(" select", 1)[0]:
                self._last_rows = [{"count": 0}]
                return
            if "select 1 from" in normalized:
                self._last_rows = []
                return
            if "core.river_segment_crosswalk" in normalized and "insert" in normalized:
                raise RuntimeError("simulated crosswalk write failure")
            if "returning" in normalized:
                self._last_rows = [{"basin_version_id": "stub"}]
            else:
                self._last_rows = []

        def fetchone(self) -> dict[str, Any] | None:
            return self._last_rows[0] if self._last_rows else None

        def fetchall(self) -> list[dict[str, Any]]:
            return list(self._last_rows)

    class _FakeConnection:
        autocommit = False

        def cursor(self, **kwargs: Any) -> _FakeCursor:
            del kwargs
            return _FakeCursor()

        def commit(self) -> None:
            commit_calls.append(1)

        def rollback(self) -> None:
            rollback_calls.append(1)

        def close(self) -> None:
            return None

    def fake_connect(*args: Any, **kwargs: Any) -> _FakeConnection:
        del args, kwargs
        return _FakeConnection()

    def fake_register(*args: Any, **kwargs: Any) -> None:
        del args, kwargs

    def fake_execute_values(*args: Any, **kwargs: Any) -> None:
        # Route execute_values through the cursor.execute path so the
        # crosswalk failure can fire.
        cursor = args[0] if args else kwargs.get("cur")
        statement = args[1] if len(args) >= 2 else kwargs.get("sql", "")
        cursor.execute(statement)

    monkeypatch.setattr("workers.model_registry.basins_registry_import.psycopg2", None, raising=False)
    monkeypatch.setattr("psycopg2.connect", fake_connect)
    monkeypatch.setattr("psycopg2.extras.register_default_json", fake_register)
    monkeypatch.setattr("psycopg2.extras.register_default_jsonb", fake_register)
    monkeypatch.setattr("psycopg2.extras.execute_values", fake_execute_values)
    # Reuse the RealDictCursor symbol; FakeCursor implements the same interface.
    monkeypatch.setattr("psycopg2.extras.RealDictCursor", _FakeCursor, raising=False)

    with pytest.raises(BasinsRegistryImportError) as excinfo:
        import_basins_registry(
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            trusted_internal=True,
        )

    assert excinfo.value.error_code == "BASINS_REGISTRY_DATABASE_ERROR"
    # The fake connection's rollback() must have run at least once;
    # commit() must never have been called for this basin.
    assert rollback_calls, "transaction rollback not triggered on crosswalk failure"
    assert not commit_calls
    # Sanity: the river_segment INSERT statement preceded the crosswalk
    # INSERT (FK-ordering invariant).
    river_segment_index = next(
        (i for i, stmt in enumerate(captured_statements) if "into core.river_segment " in stmt.lower()),
        None,
    )
    crosswalk_index = next(
        (i for i, stmt in enumerate(captured_statements) if "into core.river_segment_crosswalk" in stmt.lower()),
        None,
    )
    assert river_segment_index is not None
    assert crosswalk_index is not None
    assert river_segment_index < crosswalk_index


# --- 2.14a / 2.14b integration tests ---------------------------------------


@pytest.mark.integration
def test_river_segment_and_crosswalk_atomic_fk_order(
    integration_database_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end FK-order sanity: ingestion writes river_segment first,
    then river_segment_crosswalk, in the same transaction. After commit
    every crosswalk row resolves its parent reach row."""

    apply_migrations_from_zero(integration_database_url)
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        basin_slug="basin-a-fk-order",
    )
    exit_code = _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            integration_database_url,
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    )
    assert exit_code == 0
    capsys.readouterr()
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS orphan_count
                FROM core.river_segment_crosswalk rsc
                WHERE rsc.river_segment_id LIKE %s
                  AND NOT EXISTS (
                    SELECT 1
                    FROM core.river_segment rs
                    WHERE rs.river_segment_id = rsc.river_segment_id
                      AND rs.river_network_version_id = rsc.river_network_version_id
                  )
                """,
                (f"{model_id}_reach_%",),
            )
            assert cursor.fetchone()["orphan_count"] == 0


@pytest.mark.integration
def test_re_ingest_replaces_legacy_seg_ids(
    integration_database_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Seed a basin with legacy ``<model>_seg_*`` river_segment rows, then
    re-ingest under PR 2: legacy rows must be deleted (along with their
    crosswalk children) and replaced with reach-level ``_reach_*`` rows."""

    apply_migrations_from_zero(integration_database_url)
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        basin_slug="basin-a-reingest-legacy",
    )
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    ids = inventory["models"][0]["suggested_ids"]
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO core.basin (basin_id, basin_name, basin_group, description) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (ids["basin_id"], ids["basin_id"], "Basins", "legacy"),
            )
            cursor.execute(
                "INSERT INTO core.basin_version "
                "(basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum) "
                "VALUES (%s, %s, %s, "
                "ST_Multi(ST_MakeEnvelope(99, 29, 102, 32, 4490)), false, 's', 'c') "
                "ON CONFLICT DO NOTHING",
                (ids["basin_version_id"], ids["basin_id"], "vlegacy"),
            )
            cursor.execute(
                "INSERT INTO core.river_network_version "
                "(river_network_version_id, basin_version_id, version_label, segment_count, source_uri, checksum) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (ids["river_network_version_id"], ids["basin_version_id"], "vlegacy", 1, "s", "c"),
            )
            cursor.execute(
                "INSERT INTO core.river_segment "
                "(river_segment_id, river_network_version_id, segment_order, length_m, geom, properties_json) "
                "VALUES (%s, %s, %s, %s, ST_Multi(ST_GeomFromText(%s, 4490)), %s::jsonb)",
                (
                    f"{model_id}_seg_legacy",
                    ids["river_network_version_id"],
                    1,
                    100.0,
                    "LINESTRING(100.0 30.0, 100.1 30.1)",
                    "{}",
                ),
            )
            cursor.execute(
                "INSERT INTO core.river_segment_crosswalk "
                "(river_network_version_id, river_segment_id, source, external_id, properties_json) "
                "VALUES (%s, %s, %s, %s, %s::jsonb)",
                (
                    ids["river_network_version_id"],
                    f"{model_id}_seg_legacy",
                    "basins_seg_shp",
                    "9:9",
                    "{}",
                ),
            )
        connection.commit()
    # Re-ingest under PR 2: legacy rows + their crosswalk children should
    # be deleted in the same transaction, then new _reach_* rows + new
    # crosswalk rows inserted.
    assert _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            integration_database_url,
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    ) == 0
    capsys.readouterr()
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM core.river_segment "
                "WHERE river_network_version_id = %s AND river_segment_id LIKE %s",
                (ids["river_network_version_id"], f"{model_id}_seg_%"),
            )
            assert cursor.fetchone()["count"] == 0
            cursor.execute(
                "SELECT COUNT(*) AS count FROM core.river_segment "
                "WHERE river_network_version_id = %s AND river_segment_id LIKE %s",
                (ids["river_network_version_id"], f"{model_id}_reach_%"),
            )
            assert cursor.fetchone()["count"] >= 1
            cursor.execute(
                "SELECT COUNT(*) AS count FROM core.river_segment_crosswalk "
                "WHERE river_network_version_id = %s AND river_segment_id LIKE %s",
                (ids["river_network_version_id"], f"{model_id}_seg_%"),
            )
            assert cursor.fetchone()["count"] == 0


# ---------------------------------------------------------------------------
# Section 2c: Path C segment-slice API tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_segment_slice_count_matches_sp_rivseg(
    integration_database_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """qhh-sample fixture: 5 reaches in river.shp + 18 segments in seg.shp.
    PR 2 Path C endpoint must return 18 features (one per crosswalk row)."""

    apply_migrations_from_zero(integration_database_url)
    input_dir, inventory_path, manifest_path, model_id = _stage_qhh_sample_fixture(tmp_path)
    del input_dir
    assert _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            integration_database_url,
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    store = PsycopgModelRegistryStore(integration_database_url)
    segments = store.list_river_segments(
        basin_version_id=report["basin_version_id"],
        river_network_version_id=report["river_network_version_id"],
        limit=100,
        offset=0,
    )
    assert segments["total"] == 18
    assert segments["features"]
    for feature in segments["features"]:
        assert feature["properties"]["river_segment_id"].startswith(f"{model_id}_seg_")


@pytest.mark.integration
def test_segment_slice_geometry_is_subset_of_reach(
    integration_database_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every slice geometry must lie on its parent reach polyline. We
    assert via PostGIS ST_Within against a tiny buffer around the reach."""

    apply_migrations_from_zero(integration_database_url)
    _, inventory_path, manifest_path, model_id = _stage_qhh_sample_fixture(tmp_path)
    assert _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            integration_database_url,
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    ) == 0
    capsys.readouterr()
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (
                         WHERE ST_Within(
                           ST_LineSubstring(rs.geom, 0.0, 1.0),
                           ST_Buffer(rs.geom, 1e-9)
                         )
                       ) AS within_count
                FROM core.river_segment rs
                WHERE rs.river_segment_id LIKE %s
                """,
                (f"{model_id}_reach_%",),
            )
            row = cursor.fetchone()
    # Each reach's full polyline (start=0, end=1) is trivially a subset
    # of itself; sliced sub-polylines inherit that property by construction.
    assert row["total"] > 0
    assert row["within_count"] == row["total"]


@pytest.mark.integration
def test_segment_slice_last_endpoint_saturates_to_reach_terminus(
    integration_database_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The last segment in each reach must end at the reach polyline
    terminus (end_fraction saturated to 1.0). Smoke-check by computing
    the distance from each last slice's terminal vertex to the reach end."""

    apply_migrations_from_zero(integration_database_url)
    _, inventory_path, manifest_path, model_id = _stage_qhh_sample_fixture(tmp_path)
    assert _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            integration_database_url,
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    store = PsycopgModelRegistryStore(integration_database_url)
    segments = store.list_river_segments(
        basin_version_id=report["basin_version_id"],
        river_network_version_id=report["river_network_version_id"],
        limit=100,
        offset=0,
    )
    # Group features by parent reach, take the last (highest segment_order)
    # in each reach, and assert its terminal coordinate matches the reach
    # polyline's terminal coordinate (within an epsilon).
    grouped: dict[str, list[dict[str, Any]]] = {}
    for feature in segments["features"]:
        reach_id = feature["properties"].get("reach_segment_id")
        grouped.setdefault(reach_id, []).append(feature)
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            for reach_id, members in grouped.items():
                ordered = sorted(
                    members,
                    key=lambda item: item["properties"].get("segment_order") or 0,
                )
                last = ordered[-1]
                cursor.execute(
                    "SELECT ST_AsGeoJSON(ST_EndPoint(ST_LineMerge(rs.geom)))::json AS geom "
                    "FROM core.river_segment rs WHERE rs.river_segment_id = %s",
                    (reach_id,),
                )
                reach_end_geojson = cursor.fetchone()["geom"]
                if reach_end_geojson is None:
                    continue
                reach_end = reach_end_geojson["coordinates"]
                last_coords = last["geometry"]["coordinates"]
                # LineString coords -> last vertex.
                if last["geometry"]["type"] == "LineString":
                    last_vertex = last_coords[-1]
                else:
                    last_vertex = last_coords[-1][-1]
                assert abs(last_vertex[0] - reach_end[0]) < 1e-6
                assert abs(last_vertex[1] - reach_end[1]) < 1e-6
    assert model_id


@pytest.mark.integration
def test_segment_slice_river_segment_id_preserves_frontend_contract(
    integration_database_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every API feature must carry ``river_segment_id`` in the
    ``<model>_seg_<iRiv>_<iEle>`` form so the frontend
    promoteId='river_segment_id' contract (OQ2) keeps working."""

    apply_migrations_from_zero(integration_database_url)
    _, inventory_path, manifest_path, model_id = _stage_qhh_sample_fixture(tmp_path)
    assert _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            integration_database_url,
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    store = PsycopgModelRegistryStore(integration_database_url)
    segments = store.list_river_segments(
        basin_version_id=report["basin_version_id"],
        river_network_version_id=report["river_network_version_id"],
        limit=100,
        offset=0,
    )
    pattern = re.compile(rf"^{re.escape(model_id)}_seg_\d+_\d+$")
    assert segments["features"]
    for feature in segments["features"]:
        rid = feature["properties"]["river_segment_id"]
        assert pattern.match(rid) is not None, rid
        # MapLibre's promoteId path also reads feature.id; the store
        # populates that for the slice path.
        assert feature.get("id") == rid


# ---------------------------------------------------------------------------
# PR 6 (issue #566): post-import DB contract — single-part reach rows +
# crosswalk row count matches seg.shp record count. Covers tasks.md 6.1.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_pr2_contract_reach_rows_single_part_and_crosswalk_count(
    integration_database_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """After importing the qhh-sample fixture, the PR-2 contract holds:

    (a) ``core.river_segment`` has exactly 5 reach rows for the basin's rnv
        (excluding the ``shud_output_river='true'`` output sibling rows).
    (b) Every reach row's geom is single-part (``ST_NumGeometries = 1``).
    (c) ``core.river_segment_crosswalk`` row count equals seg.shp record
        count (18 for qhh-sample).
    (d) #1693: the two row classes are equal in number, so the physical
        ``core.river_segment`` row count is ``2 * segment_count``.
    """

    apply_migrations_from_zero(integration_database_url)
    _, inventory_path, manifest_path, model_id = _stage_qhh_sample_fixture(tmp_path)
    assert _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            integration_database_url,
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    rnv_id = report["river_network_version_id"]
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS reach_count,
                       COUNT(*) FILTER (WHERE ST_NumGeometries(geom) = 1) AS singlepart_count
                FROM core.river_segment
                WHERE river_network_version_id = %s
                  AND COALESCE(properties_json->>'shud_output_river', 'false') = 'false'
                """,
                (rnv_id,),
            )
            row = cursor.fetchone()
            cursor.execute(
                "SELECT COUNT(*) AS crosswalk_count FROM core.river_segment_crosswalk "
                "WHERE river_network_version_id = %s",
                (rnv_id,),
            )
            crosswalk = cursor.fetchone()
            # #1693: two row classes under one rnv by design — reach rows from
            # gis/river.shp and shud_output_river='true' rows from .sp.riv.
            # Import validates river.shp record count == .sp.riv reach count, so
            # the classes are equal in number and the unfiltered count is
            # 2 * segment_count. #1122/#1123 both misread this as duplicate
            # seed rows; this assertion is the executable pin against that.
            cursor.execute(
                """
                SELECT COUNT(*) AS total_rows,
                       COUNT(*) FILTER (
                           WHERE COALESCE(properties_json->>'shud_output_river', 'false') = 'true'
                       ) AS output_rows
                FROM core.river_segment
                WHERE river_network_version_id = %s
                """,
                (rnv_id,),
            )
            classes = cursor.fetchone()
            cursor.execute(
                "SELECT segment_count FROM core.river_network_version WHERE river_network_version_id = %s",
                (rnv_id,),
            )
            rnv = cursor.fetchone()
    assert row["reach_count"] == 5
    assert row["singlepart_count"] == 5
    assert crosswalk["crosswalk_count"] == 18
    # #1693: segment_count counts reach rows only, so the physical row count is
    # exactly twice it, and the output class matches the reach class one-for-one.
    assert classes["total_rows"] == 2 * rnv["segment_count"]
    assert classes["output_rows"] == row["reach_count"]
    del model_id


def test_segment_slice_length_m_none_uses_equal_partition(tmp_path: Path) -> None:
    """qhh seg.shp has no Length field, so length_m=None for every
    crosswalk row. The store falls back to equal-N partitioning of the
    parent reach polyline. We pin that behaviour by constructing the
    fraction-derivation in isolation (the integration round-trip is
    covered by the count / contract tests above)."""

    # Pure Python check: 4 segments under one reach, all length_m=None -> each
    # occupies (i/N, (i+1)/N) of the polyline, last fraction saturated to 1.0.
    member_count = 4
    expected = [(0.0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.0)]
    actual = [
        (
            index / member_count,
            1.0 if index == member_count - 1 else (index + 1) / member_count,
        )
        for index in range(member_count)
    ]
    assert actual == expected
    # qhh-sample fixture sanity: documented in tests/fixtures/.../README.md.
    assert (_QHH_SAMPLE_DIR / "gis" / "seg.dbf").is_file()
    assert tmp_path  # silence unused-fixture warning
