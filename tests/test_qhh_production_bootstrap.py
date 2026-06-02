from __future__ import annotations

import json
from pathlib import Path

import pytest
from psycopg2.extras import Json

import workers.model_registry.qhh_production_bootstrap as qhh_bootstrap
from packages.common.model_registry import PsycopgModelRegistryStore
from services.orchestrator.scheduler import ProductionScheduler, ProductionSchedulerConfig
from tests.integration_helpers import apply_migrations_from_zero, psycopg_connection
from tests.test_basins_registry_import import _write_registry_fixture
from tests.test_production_scheduler import FakeAdapter
from workers.model_registry.cli import _argparse_main
from workers.model_registry.qhh_production_bootstrap import (
    MAX_QHH_BOOTSTRAP_DISCOVERY_ENTRIES,
    MAX_QHH_OUTPUT_SEGMENTS,
    MAX_QHH_TSD_FORC_BYTES,
    QhhProductionBootstrapError,
    bootstrap_qhh_production,
    read_qhh_output_segment_count,
    read_qhh_tsd_forc,
)


def test_read_qhh_tsd_forc_reports_created_station_identity(tmp_path: Path) -> None:
    input_dir = tmp_path / "qhh" / "input" / "qhh"
    input_dir.mkdir(parents=True)
    tsd_forc = input_dir / "qhh.tsd.forc"
    tsd_forc.write_text(
        "2 6\n"
        "/forcing\n"
        "ID Lon Lat X Y Z Filename\n"
        "1 100.1 30.1 1 2 -9999 X000001.csv\n"
        "2 100.2 30.2 3 4 12.5 X000002.csv\n",
        encoding="utf-8",
    )

    stations, checksum = read_qhh_tsd_forc(tsd_forc, input_dir)

    assert checksum
    assert [station.station_id for station in stations] == ["qhh_forc_001", "qhh_forc_002"]
    assert stations[0].elevation_m == 0.0
    assert stations[1].forcing_filename == "X000002.csv"


@pytest.mark.parametrize(
    ("content", "error_code"),
    [
        (
            "2 6\n/forcing\nID Lon Lat X Y Z Filename\n1 100 30 1 1 1 X000001.csv\n",
            "QHH_BOOTSTRAP_STATION_COUNT_MISMATCH",
        ),
        ("1 6\n/forcing\nID Lon Lat X Y Z Filename\nbad row\n", "QHH_BOOTSTRAP_TSD_FORC_MALFORMED"),
    ],
)
def test_read_qhh_tsd_forc_rejects_mismatch_and_malformed(
    tmp_path: Path,
    content: str,
    error_code: str,
) -> None:
    input_dir = tmp_path / "qhh"
    input_dir.mkdir()
    tsd_forc = input_dir / "qhh.tsd.forc"
    tsd_forc.write_text(content, encoding="utf-8")

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        read_qhh_tsd_forc(tsd_forc, input_dir)

    assert exc_info.value.error_code == error_code
    assert exc_info.value.details["no_mutation_expected"] is True


def test_read_qhh_tsd_forc_rejects_oversized_input(tmp_path: Path) -> None:
    input_dir = tmp_path / "qhh"
    input_dir.mkdir()
    tsd_forc = input_dir / "qhh.tsd.forc"
    tsd_forc.write_bytes(b"1 6\n" + (b"x" * (MAX_QHH_TSD_FORC_BYTES + 1)))

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        read_qhh_tsd_forc(tsd_forc, input_dir)

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_TSD_FORC_OVERSIZED"


def test_read_qhh_tsd_forc_rejects_symlink_leaf(tmp_path: Path) -> None:
    input_dir = tmp_path / "qhh"
    input_dir.mkdir()
    outside = tmp_path / "outside.tsd.forc"
    outside.write_text("1\nmeta\nheader\n1 100 30 1 1 1 X000001.csv\n", encoding="utf-8")
    link = input_dir / "qhh.tsd.forc"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink support unavailable: {error}")

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        read_qhh_tsd_forc(link, input_dir)

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE"


def test_read_qhh_tsd_forc_rejects_non_regular_file(tmp_path: Path) -> None:
    input_dir = tmp_path / "qhh"
    tsd_forc = input_dir / "qhh.tsd.forc"
    tsd_forc.mkdir(parents=True)

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        read_qhh_tsd_forc(tsd_forc, input_dir)

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_PROJECT_FILE_UNSAFE"


def test_read_qhh_output_segment_count_rejects_out_of_range_positive_count(tmp_path: Path) -> None:
    input_dir = tmp_path / "qhh"
    input_dir.mkdir()
    sp_riv = input_dir / "qhh.sp.riv"
    sp_riv.write_text(f"{MAX_QHH_OUTPUT_SEGMENTS + 1} 6\n1 0 0 0.01 100 0\n", encoding="utf-8")

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        read_qhh_output_segment_count(sp_riv, input_dir)

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_OUTPUT_SEGMENT_COUNT_INVALID"
    assert exc_info.value.details["output_segment_count"] == MAX_QHH_OUTPUT_SEGMENTS + 1
    assert exc_info.value.details["max_output_segment_count"] == MAX_QHH_OUTPUT_SEGMENTS
    assert exc_info.value.details["no_mutation_expected"] is True


def test_bootstrap_rejects_relative_traversal_project_path(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    root.mkdir()

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            basins_root=root,
            qhh_basin_slug="../qhh",
            work_dir=tmp_path / "work",
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_PROJECT_PATH_UNSAFE"
    assert exc_info.value.details["no_mutation_expected"] is True


def test_bootstrap_rejects_discovery_entry_overflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _input_dir, _inventory_path, _manifest_path, _model_id = _qhh_registry_fixture(tmp_path)
    for index in range(MAX_QHH_BOOTSTRAP_DISCOVERY_ENTRIES + 1):
        (root / f"unrelated-{index:04d}").mkdir()
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(tmp_path / "object-store"))

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            basins_root=root,
            work_dir=tmp_path / "work",
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_DISCOVERY_ENTRY_LIMIT_EXCEEDED"
    assert exc_info.value.details["max_entries"] == MAX_QHH_BOOTSTRAP_DISCOVERY_ENTRIES


def test_bootstrap_rejects_evidence_no_clobber_before_database(tmp_path: Path) -> None:
    root, _input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(tmp_path)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    evidence_path = evidence_dir / "bootstrap.json"
    evidence_path.write_text("existing\n", encoding="utf-8")

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            basins_root=root,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            evidence_dir=evidence_dir,
            evidence_path=evidence_path,
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_EVIDENCE_NO_CLOBBER"


def test_bootstrap_rejects_evidence_path_outside_root(tmp_path: Path) -> None:
    root, _input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(tmp_path)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            basins_root=root,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            evidence_dir=evidence_dir,
            evidence_path=tmp_path / "outside.json",
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_EVIDENCE_PATH_UNSAFE"


def test_bootstrap_removes_reserved_evidence_when_database_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    evidence_path = evidence_dir / "bootstrap.json"

    def fail_database(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        assert evidence_path.exists()
        assert evidence_path.read_bytes() == b""
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_DATABASE_ERROR",
            "Injected database failure after evidence reservation.",
            model_id=model_id,
        )

    monkeypatch.setattr(qhh_bootstrap, "_bootstrap_database", fail_database)

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            basins_root=root,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            evidence_dir=evidence_dir,
            evidence_path=evidence_path,
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_DATABASE_ERROR"
    assert not evidence_path.exists()


def test_bootstrap_rejects_malformed_manifest_json(tmp_path: Path) -> None:
    root, _input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(tmp_path)
    manifest_path.write_text("{not-json\n", encoding="utf-8")

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            basins_root=root,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_PACKAGE_MANIFEST_INVALID"


def test_bootstrap_rejects_precomputed_sources_from_different_physical_qhh_root(tmp_path: Path) -> None:
    source_root, source_input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(
        tmp_path / "source"
    )
    (
        current_root,
        _current_input_dir,
        _current_inventory_path,
        _current_manifest_path,
        _model_id,
    ) = _qhh_registry_fixture(
        tmp_path / "current"
    )
    source_input_dir.joinpath("qhh.sp.riv").write_text("not-a-valid-river-count\n", encoding="utf-8")

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            basins_root=current_root,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
        )

    assert source_root != current_root
    assert exc_info.value.error_code == "QHH_BOOTSTRAP_SOURCE_ROOT_MISMATCH"
    assert set(exc_info.value.details["fields"]) == {"source_root", "input_dir"}
    assert exc_info.value.details["actual_source_root"] == str(source_root / "qhh")
    assert exc_info.value.details["expected_source_root"] == str(current_root / "qhh")
    assert exc_info.value.details["no_mutation_expected"] is True


def test_bootstrap_rejects_manifest_digest_mismatch(tmp_path: Path) -> None:
    root, _input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(tmp_path)
    manifest_path.with_suffix(manifest_path.suffix + ".sha256").write_text("0" * 64 + "\n", encoding="utf-8")

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            basins_root=root,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_MANIFEST_DIGEST_MISMATCH"


def test_bootstrap_cli_outputs_typed_blocker_for_missing_database_url(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    exit_code = _argparse_main(
        [
            "bootstrap-qhh-production",
            "--basins-root",
            str(root),
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
        ]
    )

    error = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert error["error_code"] == "QHH_BOOTSTRAP_DATABASE_URL_MISSING"


@pytest.mark.integration
def test_bootstrap_qhh_production_success_idempotent_and_scheduler_ready(
    tmp_path: Path,
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    root, _input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    first = bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
        evidence_dir=evidence_dir,
        evidence_path=evidence_dir / "first.json",
    )
    second = bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
    )

    assert first["active"] is True
    assert first["scheduler_readiness"]["ready"] is True
    assert first["station_row_counts"] == {"created": 2, "updated": 0, "unchanged": 0}
    assert first["output_segment_row_counts"] == {"created": 2, "updated": 0, "unchanged": 0}
    assert first["non_goal_proof"]["forcing_version_rows_created"] == 0
    assert first["non_goal_proof"]["forcing_station_timeseries_rows_created"] == 0
    assert second["station_row_counts"] == {"created": 0, "updated": 0, "unchanged": 2}
    assert second["output_segment_row_counts"] == {"created": 0, "updated": 0, "unchanged": 2}
    assert json.loads((evidence_dir / "first.json").read_text(encoding="utf-8"))["model_id"] == model_id

    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT mi.active_flag,
                       mi.lifecycle_state,
                       mi.shud_code_version,
                       mi.model_package_uri,
                       mi.resource_profile,
                       bv.basin_id
                FROM core.model_instance mi
                JOIN core.basin_version bv
                  ON bv.basin_version_id = mi.basin_version_id
                WHERE mi.model_id = %s
                """,
                (model_id,),
            )
            model = cursor.fetchone()
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM met.met_station
                WHERE properties_json->>'model_id' = %s
                  AND station_role = 'forcing_grid'
                """,
                (model_id,),
            )
            station_count = int(cursor.fetchone()["count"])
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM core.river_segment
                WHERE river_network_version_id = %s
                  AND COALESCE(properties_json->>'shud_output_river', 'false') = 'true'
                """,
                (first["river_network_version_id"],),
            )
            output_count = int(cursor.fetchone()["count"])
            cursor.execute("SELECT COUNT(*) AS count FROM met.forcing_version WHERE model_id = %s", (model_id,))
            forcing_count = int(cursor.fetchone()["count"])

    assert model["active_flag"] is True
    assert model["lifecycle_state"] == "active"
    assert model["shud_code_version"] == "basins-shud"
    assert model["resource_profile"]["runnable"] is True
    assert model["resource_profile"]["station_count"] == 2
    assert station_count == 2
    assert output_count == 2
    assert forcing_count == 0

    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(workspace_root=tmp_path / "workspace", model_ids=(model_id,)),
        registry=PsycopgModelRegistryStore(integration_database_url),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )
    result = scheduler.run_once()
    reasons = {item["reason"] for item in result.evidence["model_discovery"]["exclusions"]}
    assert model_id in {item["model_id"] for item in result.evidence["candidates"]}
    assert not {"not_shud_model", "not_runnable", "incomplete_model_metadata"} & reasons


@pytest.mark.integration
def test_bootstrap_station_failure_rolls_back_model_readiness(
    tmp_path: Path,
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    root, _input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path)

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url=integration_database_url,
            basins_root=root,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            fail_after_model_metadata=True,
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_PARTIAL_BOOTSTRAP_ROLLBACK"
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM core.model_instance WHERE model_id = %s AND active_flag = true",
                (model_id,),
            )
            active_count = int(cursor.fetchone()["count"])
            cursor.execute(
                "SELECT COUNT(*) AS count FROM met.met_station WHERE properties_json->>'model_id' = %s",
                (model_id,),
            )
            station_count = int(cursor.fetchone()["count"])
    assert active_count == 0
    assert station_count == 0


@pytest.mark.integration
def test_bootstrap_duplicate_active_model_blocks_before_station_writes(
    tmp_path: Path,
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    root, _input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path)
    bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
    )
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO core.basin (basin_id, basin_name, basin_group)
                VALUES ('basins_qhh_duplicate', 'Duplicate QHH', 'integration')
                """
            )
            cursor.execute(
                """
                INSERT INTO core.basin_version (
                    basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum
                )
                VALUES (
                    'basins_qhh_duplicate_vbasins',
                    'basins_qhh_duplicate',
                    'vbasins',
                    ST_Multi(ST_MakeEnvelope(100, 30, 101, 31, 4490)),
                    true,
                    'integration://qhh-duplicate',
                    'dup-basin-sha'
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO core.river_network_version (
                    river_network_version_id, basin_version_id, version_label, segment_count, source_uri, checksum
                )
                VALUES (
                    'basins_qhh_duplicate_rivnet_vbasins',
                    'basins_qhh_duplicate_vbasins',
                    'vbasins',
                    1,
                    'integration://qhh-duplicate-rivnet',
                    'dup-rivnet-sha'
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO core.mesh_version (
                    mesh_version_id, basin_version_id, version_label, mesh_uri, checksum, properties_json
                )
                VALUES (
                    'basins_qhh_duplicate_mesh_vbasins',
                    'basins_qhh_duplicate_vbasins',
                    'vbasins',
                    'integration://qhh-duplicate-mesh',
                    'dup-mesh-sha',
                    %s
                )
                """,
                (Json({}),),
            )
            cursor.execute(
                """
                INSERT INTO core.model_instance (
                    model_id,
                    basin_version_id,
                    river_network_version_id,
                    mesh_version_id,
                    calibration_version_id,
                    shud_code_version,
                    model_package_uri,
                    active_flag,
                    lifecycle_state,
                    resource_profile
                )
                SELECT
                    'basins_qhh_shud_duplicate',
                    'basins_qhh_duplicate_vbasins',
                    'basins_qhh_duplicate_rivnet_vbasins',
                    'basins_qhh_duplicate_mesh_vbasins',
                    'duplicate-calib',
                    shud_code_version,
                    model_package_uri,
                    true,
                    'active',
                    resource_profile || %s
                FROM core.model_instance
                WHERE model_id = %s
                """,
                (Json({"project_name": "qhh", "basin_slug": "qhh"}), model_id),
            )

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url=integration_database_url,
            basins_root=root,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_DUPLICATE_ACTIVE_MODEL"


@pytest.mark.integration
def test_registry_import_ignores_bootstrap_output_identity_rows_on_rerun(
    tmp_path: Path,
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    root, _input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(tmp_path)

    first = bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
    )
    second = bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
    )

    assert first["registry_import"]["row_counts"]["river_segment"] == 2
    assert second["registry_import"]["row_counts"]["river_segment"] == 0
    assert second["status"] == "bootstrapped"


def _qhh_registry_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, str]:
    root, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        basin_slug="qhh",
        sp_segment_count=2,
    )
    input_dir = _rename_fixture_input_to_qhh(input_dir)
    _write_valid_qhh_tsd_forc(input_dir)
    _refresh_inventory_and_manifest(tmp_path, root, inventory_path, manifest_path)
    return root, input_dir, inventory_path, manifest_path, model_id


def _rename_fixture_input_to_qhh(input_dir: Path) -> Path:
    for path in list(input_dir.glob("alias-a.*")):
        target = input_dir / path.name.replace("alias-a", "qhh", 1)
        path.rename(target)
    qhh_input_dir = input_dir.parent / "qhh"
    input_dir.rename(qhh_input_dir)
    return qhh_input_dir


def _write_valid_qhh_tsd_forc(input_dir: Path) -> None:
    input_dir.joinpath("qhh.tsd.forc").write_text(
        "2 6\n"
        "/forcing\n"
        "ID Lon Lat X Y Z Filename\n"
        "1 100.1 30.1 1 2 -9999 X000001.csv\n"
        "2 100.2 30.2 3 4 12.5 X000002.csv\n",
        encoding="utf-8",
    )


def _refresh_inventory_and_manifest(
    tmp_path: Path,
    root: Path,
    inventory_path: Path,
    manifest_path: Path,
) -> None:
    from tests.test_basins_registry_import import _package_manifest_for_model
    from workers.model_registry.basins_discovery import discover_basins_inventory, write_inventory

    inventory = discover_basins_inventory(root)
    write_inventory(inventory, inventory_path)
    model = inventory["models"][0]
    manifest = _package_manifest_for_model(model, model["model_id"], inventory=inventory)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    del tmp_path
