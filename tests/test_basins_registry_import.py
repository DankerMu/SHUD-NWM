"""Basins registry import: core digest, orchestration and BUG-008 contracts.

Retained core (partition 1 of 7) of the former monolith
``tests/test_basins_registry_import.py`` (issue #1913): the 18 core-preparation,
output-segment/resource-profile, digest/canonical-geometry, import-core and
pre-migration schema contracts, including both ``output_segment_count`` cases named
by BUG-20260527-008.  Shared test support lives in the non-collectible
``tests/basins_registry_import_helpers.py``; the parser, CLI, security, auth, DB and
QHH families moved to their own partitions, which
``scripts/select_ci_tests.py``'s ``BASINS_REGISTRY_IMPORT_TESTS`` routes as one
corpus.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tests.basins_registry_import_helpers import (
    _CLI_MODEL_ADMIN_AUTH_ARGS,
    _FakeRiverSegmentCursor,
    _patch_execute_values,
    _spy_import_basin_helpers,
    _write_registry_fixture,
)
from workers.model_registry.basins_discovery import BASINS_DISCOVERY_SCHEMA_VERSION_V1
from workers.model_registry.basins_package import (
    BASINS_PACKAGE_SCHEMA_VERSION,
    BASINS_PACKAGE_SCHEMA_VERSION_V1,
    publish_basins_package,
)
from workers.model_registry.basins_registry_import import (
    _canonical_singlepart_line_coordinates,
    _ensure_output_river_segments,
    _normalize_properties_for_digest,
    _output_river_segment_rows,
    _resource_profile,
    _river_segment_digest_row,
    prepare_basins_import_sources,
    prepare_relocated_basins_import_sources_after_package_verification,
)
from workers.model_registry.cli import _argparse_main


def test_prepare_import_sources_does_not_need_data_basins_default(tmp_path: Path) -> None:
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)

    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)

    assert sources.ids["model_id"] == model_id
    assert sources.source_root == (tmp_path / "basins" / "basin-a").resolve()


def test_resource_profile_records_output_segment_count(tmp_path: Path) -> None:
    """PR 2: segment_count now == reach count == output_segment_count."""

    _, _, inventory_path, manifest_path, _ = _write_registry_fixture(
        tmp_path,
        sp_river_count=1,
        sp_segment_count=2,
    )

    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)
    profile = _resource_profile(sources)

    assert profile["output_segment_count"] == 1
    assert profile["segment_count"] == 1


def test_output_river_segment_rows_use_canonical_ids_and_output_flag(tmp_path: Path) -> None:
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        sp_river_count=3,
        sp_segment_count=2,
    )
    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)

    rows = _output_river_segment_rows(sources)

    assert [row["river_segment_id"] for row in rows] == [
        f"{model_id}_shud_riv_000001",
        f"{model_id}_shud_riv_000002",
        f"{model_id}_shud_riv_000003",
    ]
    assert all(row["properties"]["shud_output_river"] is True for row in rows)
    assert [row["properties"]["shud_riv_index"] for row in rows] == [1, 2, 3]
    # PR 2: segment_order offsets past the reach layer (segment_count = 3 reach
    # rows in this fixture; output rows start at segment_count + 1).
    assert [row["segment_order"] for row in rows] == [4, 5, 6]


def test_ensure_output_river_segments_seeds_exactly_output_segment_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        sp_river_count=3,
        sp_segment_count=2,
    )
    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)
    cursor = _FakeRiverSegmentCursor(sources.ids["river_network_version_id"])
    _patch_execute_values(monkeypatch)

    inserted = _ensure_output_river_segments(cursor, sources)

    output_rows = cursor.output_river_segments()
    assert inserted == 3
    assert [row["river_segment_id"] for row in output_rows] == [
        f"{model_id}_shud_riv_000001",
        f"{model_id}_shud_riv_000002",
        f"{model_id}_shud_riv_000003",
    ]
    assert all(row["properties_json"]["shud_output_river"] is True for row in output_rows)
    # Geometry-layer rows are untouched by output seeding.
    assert cursor.geometry_segment_count() == 0


def test_ensure_output_river_segments_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, inventory_path, manifest_path, _ = _write_registry_fixture(
        tmp_path,
        sp_river_count=3,
        sp_segment_count=2,
    )
    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)
    cursor = _FakeRiverSegmentCursor(sources.ids["river_network_version_id"])
    _patch_execute_values(monkeypatch)

    first = _ensure_output_river_segments(cursor, sources)
    second = _ensure_output_river_segments(cursor, sources)

    assert first == 3
    assert second == 0
    assert len(cursor.output_river_segments()) == 3


def test_normalize_properties_for_digest_collapses_pg_numeric_roundtrip() -> None:
    """PG JSONB stores numbers as ``numeric`` and emits canonical text; a
    Python ``float(5550.0)`` written into JSONB may come back as
    ``int(5550)`` once psycopg2 ``json.loads`` decodes the text. The digest
    normaliser must produce equal output for both forms so the SHA-256 of
    incoming vs re-read ``properties_json`` stays stable across re-ingest."""

    incoming = {
        "Index": 1,
        "Down": 2,
        "Length": 5550.0,
        "KsatH": 1e-05,
        "Slope": 0.01,
        "BedThick": 1.0,
        "terminal_reach": False,
        "source_layer": "river",
    }
    # Simulated post-PG-JSONB read: psycopg2 -> json.loads -> ints where the
    # original Python had floats and an integer where JSON text dropped the
    # trailing zero (e.g. ``5550`` instead of ``5550.0``).
    after_pg = {
        "Index": 1,
        "Down": 2,
        "Length": 5550,
        "KsatH": 1e-05,
        "Slope": 0.01,
        "BedThick": 1,
        "terminal_reach": False,
        "source_layer": "river",
    }
    incoming_norm = _normalize_properties_for_digest(incoming)
    after_pg_norm = _normalize_properties_for_digest(after_pg)
    assert incoming_norm == after_pg_norm
    # Booleans must survive as ``bool`` rather than be collapsed to 0.0/1.0.
    assert isinstance(incoming_norm["terminal_reach"], bool)
    # Strings round-trip untouched.
    assert incoming_norm["source_layer"] == "river"
    # Stable JSON serialisation -> same SHA-256.
    incoming_json = json.dumps(incoming_norm, sort_keys=True, separators=(",", ":"))
    after_pg_json = json.dumps(after_pg_norm, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(incoming_json.encode("utf-8")).hexdigest() == hashlib.sha256(
        after_pg_json.encode("utf-8")
    ).hexdigest()


def test_river_segment_digest_row_stable_across_pg_numeric_roundtrip() -> None:
    """End-to-end digest row stability: building the digest row from the
    incoming Python form and from the simulated PG-roundtrip form must
    produce identical dicts (and therefore identical hashes)."""

    incoming_props = {"Length": 5550.0, "Slope": 0.01, "iRiv": 1, "terminal_reach": False}
    pg_props = {"Length": 5550, "Slope": 0.01, "iRiv": 1, "terminal_reach": False}
    row_a = _river_segment_digest_row(
        river_segment_id="m_reach_000001",
        segment_order=1,
        downstream_segment_id="m_reach_000002",
        length_m=5550.0,
        geom_wkt="LINESTRING(0 0, 1 1)",
        properties=incoming_props,
    )
    row_b = _river_segment_digest_row(
        river_segment_id="m_reach_000001",
        segment_order=1,
        downstream_segment_id="m_reach_000002",
        length_m=5550.0,
        geom_wkt="LINESTRING(0 0, 1 1)",
        properties=pg_props,
    )
    assert row_a == row_b


def test_river_segment_digest_collapses_postgis_storage_shape() -> None:
    """``LINESTRING(...)`` (parser emit) and single-part ``MULTILINESTRING((...))``
    (PostGIS round-trip after ``ST_Multi(ST_GeomFromText(...))``) must produce
    identical digest rows. This is the direct fix for the
    ``BASINS_REGISTRY_CHECKSUM_CONFLICT`` storm that PR 2 hit on re-ingest."""

    common: dict[str, Any] = dict(
        river_segment_id="m_reach_000001",
        segment_order=1,
        downstream_segment_id=None,
        length_m=100.0,
        properties={"Index": 1, "Length": 100.0},
    )
    incoming = _river_segment_digest_row(
        **common,
        geom_wkt="LINESTRING(100 30,100.05 30.05)",
    )
    stored = _river_segment_digest_row(
        **common,
        geom_wkt="MULTILINESTRING((100 30,100.05 30.05))",
    )
    assert incoming == stored


def test_canonical_geometry_collapses_numeric_text_variants() -> None:
    """``100`` / ``100.0`` / ``1e2`` all canonicalize to the same coordinate
    string so a serialiser quirk on either side cannot drift the digest."""

    assert _canonical_singlepart_line_coordinates(
        "LINESTRING(100 30,200 40)"
    ) == _canonical_singlepart_line_coordinates("LINESTRING(100.0 30.0,2e2 40.0)")


def test_canonical_geometry_rejects_real_multipart() -> None:
    """Genuine multi-part ``MULTILINESTRING`` MUST NOT be silently folded.
    PR 2's ``gis/river.shp`` parser guarantees a single-part reach geometry;
    a multi-part input is an invariant violation that needs to surface."""

    with pytest.raises(ValueError, match="single-part"):
        _canonical_singlepart_line_coordinates("MULTILINESTRING((0 0,1 1),(2 2,3 3))")


def test_canonical_geometry_collapses_negative_zero() -> None:
    """``-0`` collapses to ``+0`` so an IEEE-754 sign quirk cannot drift the
    digest between Python and PostGIS round-trips."""

    assert _canonical_singlepart_line_coordinates(
        "LINESTRING(-0 0,1 1)"
    ) == _canonical_singlepart_line_coordinates("LINESTRING(0 0,1 1)")


def test_import_basin_into_registry_core_calls_output_seed_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    import workers.model_registry.basins_registry_import as bri

    spies = _spy_import_basin_helpers(monkeypatch)
    row_counts = bri.import_basin_into_registry_core(MagicMock(), MagicMock())

    assert spies["_ensure_output_river_segments"].call_count == 1
    assert spies["_backfill_output_segment_geometry"].call_count == 1
    assert "output_river_segment" in row_counts


def test_import_basin_into_registry_core_skips_output_seed_and_backfill_when_flags_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #575: re-ingesting a bootstrapped basin (e.g. qhh after
    qhh_production_bootstrap) needs to skip the generic output-row seed +
    backfill because the existing output rows carry custom properties_json
    that would trip BASINS_REGISTRY_CHECKSUM_CONFLICT.
    """
    from unittest.mock import MagicMock

    import workers.model_registry.basins_registry_import as bri

    spies = _spy_import_basin_helpers(monkeypatch)
    row_counts = bri.import_basin_into_registry_core(
        MagicMock(),
        MagicMock(),
        seed_output_river_segments=False,
        backfill_output_segment_geometry=False,
    )

    assert spies["_ensure_output_river_segments"].call_count == 0
    assert spies["_backfill_output_segment_geometry"].call_count == 0
    assert "output_river_segment" not in row_counts
    # Reach + crosswalk + mesh + model_instance still land — toggles only
    # affect the output-row contract.
    assert spies["_ensure_river_segments"].call_count == 1
    assert spies["_ensure_river_segment_crosswalk"].call_count == 1
    assert spies["_ensure_mesh"].call_count == 1
    assert spies["_ensure_model_instance"].call_count == 1


def test_refresh_parent_version_materialization_updates_mesh_and_model_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #575: a basin originally bootstrapped under a different
    package_version still carries the old mesh_uri / model_package_uri at
    re-ingest time. _refresh_parent_version_materialization must in-place
    rewrite them along with basin_version / river_network_version, so the
    subsequent _ensure_* idempotency checks take the no-op path instead of
    raising CHECKSUM_CONFLICT.
    """
    from unittest.mock import MagicMock

    import workers.model_registry.basins_registry_import as bri

    captured: list[str] = []

    class _RecordingCursor:
        def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
            captured.append(" ".join(str(statement).split()))

    # All 4 parent rows already exist in DB → every UPDATE branch fires.
    monkeypatch.setattr(bri, "_fetch_optional", lambda c, sql, params: {"present": 1})
    monkeypatch.setattr(bri, "_mesh_uri", lambda src: "s3://pkg-new/test_input.sp.mesh")
    monkeypatch.setattr(bri, "_source_checksum", lambda src, name: "ck-mesh-new")
    monkeypatch.setattr(bri, "_resource_profile", lambda src: {"scheduler": "slurm", "version": "new"})
    monkeypatch.setattr(bri, "_json", lambda value: json.dumps(value, sort_keys=True))

    sources = MagicMock()
    sources.ids = {
        "river_network_version_id": "rnv-id",
        "basin_version_id": "bv-id",
        "mesh_version_id": "mesh-id",
        "model_id": "model-id",
    }
    sources.geometry = MagicMock(
        segment_count=42,
        river_network_source_uri="s3://pkg-new/river.shp",
        river_network_checksum="ck-rn-new",
        domain_source_uri="s3://pkg-new/domain.shp",
        domain_checksum="ck-dom-new",
    )
    sources.manifest = {
        "model_package_uri": "s3://pkg-new/package/",
        "manifest_uri": "s3://pkg-new/manifest.json",
        "package_checksum": "ck-pkg-new",
        "source_inventory_checksum": "ck-inv-new",
    }
    sources.model = {
        "basin_slug": "test-basin",
        "shud_input_name": "test_input",
        "source_path": "/src",
        "resolved_source_path": "/abs/src",
    }

    bri._refresh_parent_version_materialization(_RecordingCursor(), sources)

    update_statements = [s for s in captured if s.startswith("UPDATE")]
    assert any("UPDATE core.river_network_version" in s for s in update_statements)
    assert any("UPDATE core.basin_version" in s for s in update_statements)
    assert any("UPDATE core.mesh_version" in s for s in update_statements)
    assert any("UPDATE core.model_instance" in s for s in update_statements)
    assert len(update_statements) == 4


def test_freshly_published_package_imports_after_package_schema_bump(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1813 task 4.5: the real packager's manifest must survive the import schema pin.

    A hand-built fixture cannot cover this -- updating its literal makes it green
    while real publishes are still rejected with
    BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID.
    """

    _, _, inventory_path, _, model_id = _write_registry_fixture(tmp_path)
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(tmp_path / "object-store"))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    manifest_path = tmp_path / "published.manifest.json"
    publish_basins_package(
        inventory_path=inventory_path,
        model_id=model_id,
        version="vbasins-test",
        output_path=manifest_path,
    )
    published_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert published_manifest["schema_version"] == BASINS_PACKAGE_SCHEMA_VERSION

    sources = prepare_basins_import_sources(
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
    )
    assert sources.manifest["schema_version"] == BASINS_PACKAGE_SCHEMA_VERSION
    assert sources.model["model_id"] == model_id

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
    error = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    # Manifest validation is fully past; only the unreachable database remains.
    assert error["error_code"] == "BASINS_REGISTRY_DATABASE_ERROR"


def test_pre_migration_package_manifest_still_imports(tmp_path: Path) -> None:
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = BASINS_PACKAGE_SCHEMA_VERSION_V1
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    sources = prepare_basins_import_sources(
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
    )

    assert sources.manifest["schema_version"] == BASINS_PACKAGE_SCHEMA_VERSION_V1
    assert sources.model["model_id"] == model_id


def test_pre_migration_manifest_relocates_against_current_generation_inventory(tmp_path: Path) -> None:
    """A pre-bump manifest records the inventory generation it was published under.

    The relocation path regenerates the inventory with current discovery code, so
    the recorded and the presented inventory schema versions legitimately differ.
    """

    _, _, inventory_path, manifest_path, _ = _write_registry_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = BASINS_PACKAGE_SCHEMA_VERSION_V1
    manifest["source_inventory_schema_version"] = BASINS_DISCOVERY_SCHEMA_VERSION_V1
    manifest["source_inventory_checksum"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    sources = prepare_relocated_basins_import_sources_after_package_verification(
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
        verified_package_checksum=str(manifest["package_checksum"]),
    )

    assert sources.manifest["source_inventory_schema_version"] == BASINS_DISCOVERY_SCHEMA_VERSION_V1
    assert sources.inventory["schema_version"] != BASINS_DISCOVERY_SCHEMA_VERSION_V1


def test_import_refuses_unknown_package_manifest_schema_version(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "basins.package.v99"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

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

    error = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert error["error_code"] == "BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID"
    assert error["model_id"] == model_id
