"""QHH production bootstrap: seeded state, profiles and inventory preflight (issue #1948 partition B)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import workers.model_registry.qhh_production_bootstrap as qhh_bootstrap
from tests.qhh_production_bootstrap_helpers import _qhh_registry_fixture
from workers.model_registry.cli import _argparse_main
from workers.model_registry.qhh_production_bootstrap import (
    MAX_QHH_BOOTSTRAP_DISCOVERY_ENTRIES,
    QhhProductionBootstrapError,
    read_qhh_output_segment_count,
    read_qhh_tsd_forc,
)


@pytest.mark.parametrize(
    ("project_name", "expected_source"),
    [("qhh", "qhh.tsd.forc"), ("heihe", "heihe.tsd.forc")],
)
def test_seed_station_rows_provenance_follows_project_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    project_name: str,
    expected_source: str,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_execute_values(
        cursor: Any,
        sql: str,
        argslist: list[tuple[Any, ...]],
        *,
        template: str,
        page_size: int,
    ) -> None:
        del cursor, sql, template, page_size
        calls.append({"argslist": argslist})

    class FakeCursor:
        def execute(self, sql: str, params: tuple[Any, ...]) -> None:
            del sql, params

        def fetchall(self) -> list[dict[str, Any]]:
            return []

    station = qhh_bootstrap.QhhForcingStation(
        station_id="qhh_forc_001",
        station_name="QHH forcing station 001",
        forcing_index=1,
        longitude=100.1,
        latitude=30.1,
        x=1.0,
        y=2.0,
        z=-9999.0,
        elevation_m=0.0,
        forcing_filename="X000001.csv",
        original_id="1",
    )
    monkeypatch.setattr(qhh_bootstrap, "execute_values", fake_execute_values, raising=False)
    monkeypatch.setattr("psycopg2.extras.execute_values", fake_execute_values)

    qhh_bootstrap._seed_station_rows(
        FakeCursor(),
        model={
            "model_id": "basins_qhh_shud",
            "basin_id": "qhh",
            "basin_version_id": "qhh_v1",
        },
        stations=[station],
        project_name=project_name,
        # Filename deliberately decoupled from project_name: the seed lane takes
        # the two as independent inputs, so a filename-derived source must fail.
        tsd_forc_path=tmp_path / "input.tsd.forc",
        tsd_forc_checksum="sha",
    )

    assert calls
    properties = calls[0]["argslist"][0][8].adapted

    assert properties["source"] == expected_source
    assert properties["elevation_metadata"]["source"] == expected_source
    assert properties["forcing_source_identity"].startswith(f"{expected_source}:")
    assert properties["project_name"] == project_name

    if project_name != "qhh":
        # Scoped to these four provenance values only: the harness model/basin/
        # station ids legitimately contain "qhh".
        assert "qhh" not in properties["source"]
        assert "qhh" not in properties["elevation_metadata"]["source"]
        assert "qhh" not in properties["forcing_source_identity"]
        assert "qhh" not in properties["project_name"]


def test_existing_output_segment_digest_ignores_deterministic_geometry_backfill_properties() -> None:
    expected = {
        "seed": "qhh_production_bootstrap",
        "model_id": "basins_qhh_shud",
        "basin_id": "qhh",
        "basin_version_id": "qhh_v1",
        "shud_output_river": True,
        "shud_riv_index": 1,
        "source": "qhh.sp.riv",
        "source_file": "/input/qhh.sp.riv",
        "source_sha256": "sha",
        "geometry_source": "gis_rivseg_iRiv",
        "output_identity": "qhh.sp.riv:1",
    }
    enriched = {
        **expected,
        "geometry_source_segment_count": 2,
        "geometry_source_length_m": 123.4,
    }

    normalized = qhh_bootstrap._output_segment_idempotency_properties(enriched, expected)

    assert normalized == expected


def test_seed_output_segment_rows_reports_geometry_backfilled_rows_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sp_riv_path = Path("/input/qhh.sp.riv")
    model = {
        "model_id": "basins_qhh_shud",
        "basin_id": "qhh",
        "basin_version_id": "qhh_v1",
        "river_network_version_id": "qhh_rivnet_v1",
    }
    expected = qhh_bootstrap._output_segment_expected_properties(
        model=model,
        project_name="qhh",
        index=1,
        sp_riv_path=sp_riv_path,
        sp_riv_checksum="sha",
    )
    stored = {
        **expected,
        "geometry_source_segment_count": 2,
        "geometry_source_length_m": 123.4,
    }

    class FakeCursor:
        def __init__(self) -> None:
            self.sql = ""

        def execute(self, sql: str, params: tuple[Any, ...]) -> None:
            del params
            self.sql = sql

        def fetchone(self) -> dict[str, Any]:
            assert "order_offset" in self.sql
            return {"order_offset": 0}

        def fetchall(self) -> list[dict[str, Any]]:
            assert "FROM core.river_segment" in self.sql
            return [
                {
                    "river_segment_id": "basins_qhh_shud_shud_riv_000001",
                    "river_network_version_id": "qhh_rivnet_v1",
                    "segment_order": 1,
                    "properties_json": stored,
                }
            ]

    monkeypatch.setattr("psycopg2.extras.execute_values", lambda *args, **kwargs: None)

    counts = qhh_bootstrap._seed_output_segment_rows(
        FakeCursor(),
        model=model,
        project_name="qhh",
        output_segment_count=1,
        sp_riv_path=sp_riv_path,
        sp_riv_checksum="sha",
    )

    assert counts == {"created": 0, "updated": 0, "unchanged": 1}


def test_scheduler_ready_profile_overrides_cannot_replace_canonical_identity(tmp_path: Path) -> None:
    root, input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(tmp_path)
    paths = qhh_bootstrap._prepare_bootstrap_paths(
        basins_root=root,
        qhh_basin_slug="qhh",
        qhh_project_name="qhh",
        model_id="basins_qhh_shud",
        package_version="vbasins-qhh-production",
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
        work_dir=tmp_path / "work",
    )
    preflight_sources = qhh_bootstrap._prepare_preflight_sources_from_bounded_json(
        paths.inventory_path,
        paths.package_manifest_path,
        model_id="basins_qhh_shud",
    )
    sources = qhh_bootstrap._prepare_sources_from_preflight(preflight_sources, model_id="basins_qhh_shud")
    stations, tsd_checksum = read_qhh_tsd_forc(input_dir / "qhh.tsd.forc", input_dir)
    output_count, sp_checksum = read_qhh_output_segment_count(input_dir / "qhh.sp.riv", input_dir)
    profile = qhh_bootstrap._scheduler_ready_resource_profile(
        qhh_bootstrap.QhhBootstrapContext(
            sources=sources,
            paths=paths,
            stations=stations,
            output_segment_count=output_count,
            tsd_forc_checksum=tsd_checksum,
            sp_riv_checksum=sp_checksum,
            shud_code_version="basins-shud",
        ),
        resource_profile_overrides={
            "model_id": "evil",
            "project_name": "evil",
            "station_count": 999,
            "output_segment_count": 999,
            "runnable": False,
            "package_checksum": "evil",
            "memory_gb": 16,
            "partition": "debug",
        },
    )

    assert profile["model_id"] == "basins_qhh_shud"
    assert profile["project_name"] == "qhh"
    assert profile["station_count"] == 2
    assert profile["output_segment_count"] == 2
    assert profile["runnable"] is True
    assert profile["package_checksum"] == sources.manifest["package_checksum"]
    assert profile["memory_gb"] == 16
    assert profile["partition"] == "debug"


def test_scheduler_ready_profile_rebuild_strips_existing_run_scoped_identity(tmp_path: Path) -> None:
    root, input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(tmp_path)
    paths = qhh_bootstrap._prepare_bootstrap_paths(
        basins_root=root,
        qhh_basin_slug="qhh",
        qhh_project_name="qhh",
        model_id="basins_qhh_shud",
        package_version="vbasins-qhh-production",
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
        work_dir=tmp_path / "work",
    )
    preflight_sources = qhh_bootstrap._prepare_preflight_sources_from_bounded_json(
        paths.inventory_path,
        paths.package_manifest_path,
        model_id="basins_qhh_shud",
    )
    sources = qhh_bootstrap._prepare_sources_from_preflight(preflight_sources, model_id="basins_qhh_shud")
    stations, tsd_checksum = read_qhh_tsd_forc(input_dir / "qhh.tsd.forc", input_dir)
    output_count, sp_checksum = read_qhh_output_segment_count(input_dir / "qhh.sp.riv", input_dir)
    expected = qhh_bootstrap._scheduler_ready_resource_profile(
        qhh_bootstrap.QhhBootstrapContext(
            sources=sources,
            paths=paths,
            stations=stations,
            output_segment_count=output_count,
            tsd_forc_checksum=tsd_checksum,
            sp_riv_checksum=sp_checksum,
            shud_code_version="basins-shud",
        ),
        resource_profile_overrides={},
    )

    merged = qhh_bootstrap._canonical_scheduler_ready_resource_profile(
        {
            "partition": "debug",
            "canonical_product_id": "stale-canon",
            "published_manifest_id": "stale-manifest",
            "pipeline_job_id": "stale-job",
            "output_uri": "s3://nhms/runs/stale/output/",
            "custom_unowned": "preserve-me-not",
        },
        expected,
        resource_profile_overrides={"memory_gb": 12, "output_uri": "s3://evil/stale/"},
    )

    assert merged["partition"] == "standard"
    assert merged["memory_gb"] == 12
    assert merged["package_checksum"] == sources.manifest["package_checksum"]
    assert merged["model_package_uri"] == sources.manifest["model_package_uri"]
    assert not {
        "canonical_product_id",
        "published_manifest_id",
        "pipeline_job_id",
        "output_uri",
        "custom_unowned",
    } & set(merged)


def test_generated_qhh_inventory_enforces_budget_during_discovery_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _input_dir, _inventory_path, _manifest_path, model_id = _qhh_registry_fixture(tmp_path)
    qhh_source_root = root / "qhh"
    calls: list[Any] = []

    def fake_preflight(root_arg: Path, *, model_id: str, qhh_source_root: Path) -> None:
        calls.append(("preflight", root_arg, model_id, qhh_source_root))

    def fake_discover(root_arg: Path, *, budget: Any = None) -> dict[str, Any]:
        calls.append(("discover", root_arg, budget.max_entries if budget is not None else None))
        raise qhh_bootstrap.BasinsDiscoveryError(
            "QHH_BOOTSTRAP_DISCOVERY_ENTRY_LIMIT_EXCEEDED",
            "budget exhausted",
            path=str(root_arg),
        )

    monkeypatch.setattr(qhh_bootstrap, "_bounded_discovery_preflight", fake_preflight)
    monkeypatch.setattr(qhh_bootstrap, "discover_basins_inventory", fake_discover)

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        qhh_bootstrap._discover_qhh_inventory(root, model_id=model_id, qhh_source_root=qhh_source_root)

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_DISCOVERY_ENTRY_LIMIT_EXCEEDED"
    assert calls[0][0] == "preflight"
    assert calls[1] == ("discover", root, MAX_QHH_BOOTSTRAP_DISCOVERY_ENTRIES)


def test_generated_qhh_inventory_blocks_deep_calib_descendants_after_preflight(
    tmp_path: Path,
) -> None:
    root, _input_dir, _inventory_path, _manifest_path, model_id = _qhh_registry_fixture(tmp_path)
    qhh_source_root = root / "qhh"
    deep = qhh_source_root / "CALIB" / "d1" / "d2" / "d3" / "d4" / "d5" / "d6"
    deep.mkdir(parents=True)
    (deep / "calib.txt").write_text("calibration\n", encoding="utf-8")

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        qhh_bootstrap._discover_qhh_inventory(root, model_id=model_id, qhh_source_root=qhh_source_root)

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_DISCOVERY_DEPTH_EXCEEDED"
    assert exc_info.value.details["no_mutation_expected"] is True


def test_duplicate_active_preflight_matches_same_package_identity_without_qhh_flags() -> None:
    executed: dict[str, Any] = {}

    class FakeCursor:
        def execute(self, sql: str, params: tuple[Any, ...]) -> None:
            executed["sql"] = sql
            executed["params"] = params

        def fetchall(self) -> list[dict[str, Any]]:
            return [
                {
                    "model_id": "basins_qhh_shud",
                    "basin_id": "qhh",
                    "basin_version_id": "qhh_vbasins",
                    "river_network_version_id": "qhh_rivnet_vbasins",
                    "model_package_uri": "s3://pkg/package/",
                    "resource_profile": {},
                    "duplicate_reason": "model_id",
                },
                {
                    "model_id": "other_model",
                    "basin_id": "other_basin",
                    "basin_version_id": "other_basin_v1",
                    "river_network_version_id": "other_rivnet_v1",
                    "model_package_uri": "s3://pkg/package/",
                    "resource_profile": {},
                    "duplicate_reason": "model_package_uri",
                },
            ]

    sources = SimpleNamespace(
        manifest={
            "model_package_uri": "s3://pkg/package/",
            "package_checksum": "package-sha",
            "source_inventory_checksum": "inventory-sha",
        },
        model={"basin_slug": "qhh", "shud_input_name": "qhh"},
        ids={
            "model_id": "basins_qhh_shud",
            "basin_id": "qhh",
            "basin_version_id": "qhh_vbasins",
            "river_network_version_id": "qhh_rivnet_vbasins",
        },
    )

    rows = qhh_bootstrap._active_qhh_identity_rows(FakeCursor(), sources, model_id="basins_qhh_shud")

    assert rows == [
        {
            "model_id": "basins_qhh_shud",
            "basin_id": "qhh",
            "basin_version_id": "qhh_vbasins",
            "river_network_version_id": "qhh_rivnet_vbasins",
            "duplicate_reason": "model_id",
        },
        {
            "model_id": "other_model",
            "basin_id": "other_basin",
            "basin_version_id": "other_basin_v1",
            "river_network_version_id": "other_rivnet_v1",
            "duplicate_reason": "model_package_uri",
        },
    ]
    assert "mi.model_package_uri = %s" in executed["sql"]
    assert "mi.resource_profile->>'package_checksum' = %s" in executed["sql"]
    assert "source_inventory_checksum" not in executed["sql"]
    assert "s3://pkg/package/" in executed["params"]
    assert "package-sha" in executed["params"]


def test_duplicate_active_preflight_does_not_match_source_inventory_checksum_only() -> None:
    executed: dict[str, Any] = {}

    class FakeCursor:
        def execute(self, sql: str, params: tuple[Any, ...]) -> None:
            executed["sql"] = sql
            executed["params"] = params

        def fetchall(self) -> list[dict[str, Any]]:
            return []

    sources = SimpleNamespace(
        manifest={
            "model_package_uri": "s3://pkg/qhh/package/",
            "package_checksum": "qhh-package-sha",
            "source_inventory_checksum": "shared-inventory-sha",
        },
        model={"basin_slug": "qhh", "shud_input_name": "qhh"},
        ids={
            "model_id": "basins_qhh_shud",
            "basin_id": "qhh",
            "basin_version_id": "qhh_vbasins",
            "river_network_version_id": "qhh_rivnet_vbasins",
        },
    )

    rows = qhh_bootstrap._active_qhh_identity_rows(FakeCursor(), sources, model_id="basins_qhh_shud")

    assert rows == []
    assert "source_inventory_checksum" not in executed["sql"]
    assert "shared-inventory-sha" not in executed["params"]


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
