from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore, sha256_bytes
from tests.test_forcing_producer import (
    FakeForcingRepository,
    _build_producer,
    _direct_grid_manifest_for_default_grid,
    _direct_grid_validation_assets,
    _write_canonical_products,
)
from workers.forcing_producer import GridPoint, parse_cycle_time, parse_direct_grid_forcing_contract
from workers.forcing_producer.producer import FORCING_VARIABLES
from workers.shud_runtime.runtime import SHUDRuntime, SHUDRuntimeConfig, SHUDRuntimeError


class FakeHydroRunRepository:
    def __init__(self) -> None:
        self.statuses: list[str] = []
        self.failures: list[tuple[str, str]] = []

    def create_run(self, manifest: dict[str, Any], run_manifest_uri: str) -> dict[str, Any]:
        self.statuses.append("created")
        return {"run_id": manifest["run_id"], "run_manifest_uri": run_manifest_uri}

    def update_status(self, _run_id: str, status: str, **_fields: Any) -> dict[str, Any]:
        self.statuses.append(status)
        return {}

    def mark_failed(self, _run_id: str, error_code: str, error_message: str, **_fields: Any) -> dict[str, Any]:
        self.statuses.append("failed")
        self.failures.append((error_code, error_message))
        return {}


def test_issue_548_direct_grid_compact_e2e_producer_to_runtime_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_root = tmp_path / "object-store"
    store = LocalObjectStore(object_root, object_store_prefix="s3://nhms")
    monkeypatch.setattr(
        "workers.forcing_producer.producer.compute_idw_weights",
        lambda **_kwargs: pytest.fail("direct-grid E2E fixture must not call IDW neighbor search"),
    )
    monkeypatch.setattr(
        FakeForcingRepository,
        "load_met_stations",
        lambda *_args, **_kwargs: pytest.fail("direct-grid E2E fixture must not load legacy IDW stations"),
    )
    ordered_grid_points = (
        GridPoint("0", -75.0, 40.0),
        GridPoint("1", -74.5, 40.2),
        GridPoint("2", -74.0, 40.4),
    )
    manifest = _direct_grid_manifest_for_default_grid()
    manifest["grid_signature"] = _grid_signature_hash(ordered_grid_points)
    contract = parse_direct_grid_forcing_contract(manifest, source_id="GFS")
    products = _write_canonical_products(
        store,
        forecast_hours=(0, 3),
        values_by_variable={
            "prcp_rate_or_amount": (301.0, 302.0, 999.0),
            "air_temperature_2m": (110.0, 120.0, 999.0),
            "relative_humidity_2m": (0.10, 0.20, 999.0),
            "shortwave_down": (401.0, 402.0, 999.0),
            "wind_u_10m": (30.0, 60.0, 999.0),
            "wind_v_10m": (40.0, 80.0, 999.0),
            "pressure_surface": (201000.0, 202000.0, 999.0),
        },
    )
    products = tuple(_with_e2e_distinct_f000_point_values(product, store) for product in products)
    repository = FakeForcingRepository(
        stations=(),
        products=products,
        forcing_mapping_contract=contract,
        direct_grid_validation_assets=_direct_grid_validation_assets(
            binding_checksum=contract.binding_checksum.removeprefix("sha256:"),
            model_input_package_id=contract.model_input_package_id,
            sp_att_checksum=contract.sp_att_checksum.removeprefix("sha256:"),
            sp_att_content=(
                "2\n"
                "ID\tA\tB\tC\tFORC\n"
                "1\t0\t0\t0\t1\n"
                "2\t0\t0\t0\t2\n"
            ),
        ),
    )
    producer = _build_producer(object_root, repository, store)

    first = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert first.status == "forcing_ready"
    first_manifest_bytes = store.read_bytes(first.file_uris["package_manifest"])
    first_manifest_checksum = sha256_bytes(first_manifest_bytes)
    assert first_manifest_checksum == first.checksum
    assert repository.mapping_contract_calls == [
        {"model_id": "demo_model", "basin_version_id": "basin_v1", "source_id": "gfs"}
    ]
    assert repository.load_station_count == 0
    assert repository.load_weight_count == 0
    assert repository.direct_grid_station_ensure_count == 1
    assert repository.interp_weight_upsert_count == 1
    assert {weight.method for weight in repository.interp_weights} == {"direct_grid"}
    assert {weight.grid_cell_id for weight in repository.interp_weights} == {"0", "1"}

    valid_time = parse_cycle_time("2026050700")
    values = {(row.station_id, row.variable, row.valid_time): row.value for row in repository.timeseries}
    assert values[("qhh_forc_001", "PRCP", valid_time)] == pytest.approx(301.0)
    assert values[("qhh_forc_001", "TEMP", valid_time)] == pytest.approx(10.0)
    assert values[("qhh_forc_001", "RH", valid_time)] == pytest.approx(0.50)
    assert values[("qhh_forc_001", "Rn", valid_time)] == pytest.approx(401.0)
    assert values[("qhh_forc_001", "wind", valid_time)] == pytest.approx(5.0)
    assert values[("qhh_forc_002", "PRCP", valid_time)] == pytest.approx(302.0)
    assert values[("qhh_forc_002", "TEMP", valid_time)] == pytest.approx(20.0)
    assert values[("qhh_forc_002", "RH", valid_time)] == pytest.approx(0.75)
    assert values[("qhh_forc_002", "Rn", valid_time)] == pytest.approx(402.0)
    assert values[("qhh_forc_002", "wind", valid_time)] == pytest.approx(10.0)
    assert values[("qhh_forc_001", "Press", valid_time)] == pytest.approx(101000.0)
    assert values[("qhh_forc_002", "Press", valid_time)] == pytest.approx(102000.0)
    f003_time = parse_cycle_time("2026050703")
    point_rows = [
        row
        for row in repository.timeseries
        if row.valid_time == valid_time and row.variable in {"TEMP", "RH", "wind", "Press"}
    ]
    assert point_rows
    assert {row.source_id for row in point_rows} == {"gfs"}
    assert all(row.valid_time != f003_time for row in point_rows)

    package_root = object_root / store.normalize_key(first.forcing_package_uri)
    tsd_forc = (package_root / "shud" / "qhh.tsd.forc").read_text(encoding="utf-8")
    assert tsd_forc.splitlines()[:5] == [
        "2 20260507",
        "shud",
        "ID\tLon\tLat\tX\tY\tZ\tFilename",
        "1\t-75\t40\t1\t2\t3657\tX100.95Y36.25.csv",
        "2\t-74.5\t40.2\t2\t3\t-9999\tX101.05Y36.25.csv",
    ]
    first_csv_lines = (package_root / "shud" / "X100.95Y36.25.csv").read_text(encoding="utf-8").splitlines()
    second_csv_lines = (package_root / "shud" / "X101.05Y36.25.csv").read_text(encoding="utf-8").splitlines()
    assert first_csv_lines[1] == "Time_Day\tPrecip\tTemp\tRH\tWind\tRN"
    assert second_csv_lines[1] == "Time_Day\tPrecip\tTemp\tRH\tWind\tRN"
    assert "Press" not in "\n".join(first_csv_lines + second_csv_lines)
    assert first_csv_lines[2].split("\t") == ["0", "301", "10", "0.5", "5", "401"]
    assert second_csv_lines[2].split("\t") == ["0", "302", "20", "0.75", "10", "402"]

    package_manifest = json.loads((package_root / "forcing_package.json").read_text(encoding="utf-8"))
    lineage = repository.forcing_versions[first.forcing_version_id]["lineage_json"]
    for payload in (package_manifest["lineage"], lineage):
        assert payload["forcing_mapping_mode"] == "direct_grid"
        assert payload["spatial_mapping_method"] == "direct_grid"
        assert payload["binding_uri"] == contract.binding_uri
        assert payload["binding_checksum"] == contract.binding_checksum
        assert payload["model_input_package_id"] == contract.model_input_package_id
        assert payload["sp_att_path"] == contract.sp_att_path
        assert payload["sp_att_checksum"] == contract.sp_att_checksum
        assert payload["applicable_source_ids"] == ["gfs", "IFS"]
        assert payload["grid_id"] == contract.grid_id
        assert payload["grid_signature"] == contract.grid_signature
        assert payload["direct_grid_station_identity"]["station_ids"] == ["qhh_forc_001", "qhh_forc_002"]
    assert lineage["forcing_package_manifest_uri"] == first.file_uris["package_manifest"]
    assert lineage["forcing_package_manifest_checksum"] == first.checksum
    assert package_manifest["lineage"]["canonical_input_signature"]["checksum"]
    assert package_manifest["lineage"]["canonical_input_signature"] == lineage["canonical_input_signature"]
    assert package_manifest["units"]["Press"] == "Pa"
    assert package_manifest["variable_set"] == list(FORCING_VARIABLES)

    row_count = len(repository.timeseries)
    row_identity = {(row.station_id, row.variable, row.valid_time) for row in repository.timeseries}
    repository.fail_next_direct_grid_station_ensure = True
    repository.fail_next_interp_weight_upsert = True
    monkeypatch.setattr(
        producer,
        "_read_canonical_field",
        lambda *_args, **_kwargs: pytest.fail("unchanged direct-grid rerun must reuse ready identity"),
    )
    second = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert second.status == "already_done"
    assert second.forcing_version_id == first.forcing_version_id
    assert second.forcing_package_uri == first.forcing_package_uri
    assert second.checksum == first.checksum
    assert second.file_uris == first.file_uris
    assert store.read_bytes(second.file_uris["package_manifest"]) == first_manifest_bytes
    assert sha256_bytes(store.read_bytes(second.file_uris["package_manifest"])) == first_manifest_checksum
    assert len(repository.forcing_versions) == 1
    assert len(repository.timeseries) == row_count
    assert {(row.station_id, row.variable, row.valid_time) for row in repository.timeseries} == row_identity
    assert [event[0] for event in repository.events].count("finalize_forcing_version") == 1

    _write_compact_shud_model_package(object_root)
    runtime_repository = FakeHydroRunRepository()
    runtime = SHUDRuntime(
        config=SHUDRuntimeConfig(
            workspace_root=tmp_path / "workspace",
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            command_style="shud_project",
            shud_executable=str(Path("tests/mock_shud_omp.py").resolve()),
        ),
        repository=runtime_repository,
        object_store=LocalObjectStore(object_root, object_store_prefix="s3://nhms"),
    )
    manifest = _runtime_manifest(first, lineage=lineage)
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)

    model_input_dir = input_dir / "alias-a"
    staged_sp_att = (model_input_dir / "alias-a.sp.att").read_text(encoding="utf-8")
    staged_tsd = (model_input_dir / "alias-a.tsd.forc").read_text(encoding="utf-8")
    assert "1\t0\t0\t0\t1" in staged_sp_att
    assert "2\t0\t0\t0\t2" in staged_sp_att
    assert "2\t0\t0\t0\t1" not in staged_sp_att
    assert "1\t-75\t40\t1\t2\t3657\tX100.95Y36.25.csv" in staged_tsd
    assert "2\t-74.5\t40.2\t2\t3\t-9999\tX101.05Y36.25.csv" in staged_tsd
    assert (model_input_dir / "X100.95Y36.25.csv").read_text(encoding="utf-8").splitlines()[1] == (
        "Time_Day\tPrecip\tTemp\tRH\tWind\tRN"
    )
    assert (model_input_dir / "X101.05Y36.25.csv").exists()

    _write_compact_shud_model_package(object_root, forc_values=(3, 3))
    negative_repository = FakeHydroRunRepository()
    negative_runtime = SHUDRuntime(
        config=SHUDRuntimeConfig(
            workspace_root=tmp_path / "negative-workspace",
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            command_style="shud_project",
            shud_executable=str(Path("tests/mock_shud_omp.py").resolve()),
        ),
        repository=negative_repository,
        object_store=LocalObjectStore(object_root, object_store_prefix="s3://nhms"),
    )
    negative_manifest = _runtime_manifest(first, lineage=lineage)
    negative_manifest["run_id"] = "direct_grid_issue_548_negative_ownership"
    negative_manifest["outputs"] = {
        "output_uri": "s3://nhms/runs/direct_grid_issue_548_negative_ownership/output/",
        "log_uri": "s3://nhms/runs/direct_grid_issue_548_negative_ownership/logs/",
    }

    with pytest.raises(SHUDRuntimeError) as exc_info:
        negative_runtime.execute(negative_manifest)

    assert exc_info.value.error_code == "DIRECT_GRID_FORCING_OWNERSHIP_RANGE"
    assert "missing=[3], allowed=[1, 2]" in exc_info.value.message
    assert negative_repository.statuses == ["created", "failed"]
    assert negative_repository.failures[0][0] == "DIRECT_GRID_FORCING_OWNERSHIP_RANGE"


def _runtime_manifest(result: Any, *, lineage: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": "direct_grid_issue_548_compact",
        "run_type": "forecast",
        "scenario_id": "forecast_gfs_deterministic",
        "source_id": "gfs",
        "cycle_time": "2026-05-07T00:00:00Z",
        "start_time": "2026-05-07T00:00:00Z",
        "end_time": "2026-05-07T03:00:00Z",
        "model": {
            "model_id": "demo_model",
            "basin_version_id": "basin_v1",
            "model_package_uri": "s3://nhms/models/demo_model/direct-grid-fixture/package/",
            "project_name": "alias-a",
            "segment_count": 2,
        },
        "initial_state": {"state_id": None, "ic_file_uri": None},
        "forcing": {
            "forcing_version_id": result.forcing_version_id,
            "forcing_uri": result.forcing_package_uri,
            "package_manifest_uri": result.file_uris["package_manifest"],
            "package_manifest_checksum": result.checksum,
            "forcing_mapping_mode": "direct_grid",
            "spatial_mapping_method": "direct_grid",
            "lineage": lineage,
        },
        "runtime": {
            "command_style": "shud_project",
            "output_interval_minutes": 1440,
        },
        "outputs": {
            "output_uri": "s3://nhms/runs/direct_grid_issue_548_compact/output/",
            "log_uri": "s3://nhms/runs/direct_grid_issue_548_compact/logs/",
        },
    }


def _write_compact_shud_model_package(object_root: Path, *, forc_values: tuple[int, int] = (1, 2)) -> None:
    package = object_root / "models" / "demo_model" / "direct-grid-fixture" / "package"
    package.mkdir(parents=True, exist_ok=True)
    (package / "alias-a.sp.mesh").write_text("mesh\n", encoding="utf-8")
    (package / "alias-a.cfg.para").write_text(
        "START_TIME = {{START_TIME}}\n"
        "END_TIME = {{END_TIME}}\n"
        "OUTPUT_DIR = {{OUTPUT_DIR}}\n"
        "MODEL_OUTPUT_INTERVAL = {{MODEL_OUTPUT_INTERVAL}}\n"
        "SEGMENT_COUNT = {{SEGMENT_COUNT}}\n"
        "old_ic_file = alias-a.cfg.ic\n",
        encoding="utf-8",
    )
    (package / "alias-a.cfg.calib").write_text("calib\n", encoding="utf-8")
    (package / "alias-a.sp.riv").write_text("2 1\n", encoding="utf-8")
    (package / "alias-a.sp.rivseg").write_text("2 4\n", encoding="utf-8")
    sp_att = (
        "2\n"
        "ID\tA\tB\tC\tFORC\n"
        f"1\t0\t0\t0\t{forc_values[0]}\n"
        f"2\t0\t0\t0\t{forc_values[1]}\n"
    )
    (package / "alias-a.sp.att").write_text(sp_att, encoding="utf-8")
    assert sha256_bytes(sp_att.encode("utf-8")) == sha256_bytes((package / "alias-a.sp.att").read_bytes())


def _grid_signature_hash(grid_points: tuple[GridPoint, ...]) -> str:
    payload = {
        "grid_points": [
            (point.grid_cell_id, round(float(point.longitude), 12), round(float(point.latitude), 12))
            for point in grid_points
        ]
    }
    return sha256_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _with_e2e_distinct_f000_point_values(product: Any, store: LocalObjectStore) -> Any:
    if product.lead_time_hours != 0:
        return product
    values_by_variable = {
        "air_temperature_2m": (10.0, 20.0, 999.0),
        "relative_humidity_2m": (0.50, 0.75, 999.0),
        "wind_u_10m": (3.0, 6.0, 999.0),
        "wind_v_10m": (4.0, 8.0, 999.0),
        "pressure_surface": (101000.0, 102000.0, 999.0),
    }
    values = values_by_variable.get(product.variable)
    if values is None:
        return product
    from tests.test_forcing_producer import _netcdf_bytes

    content = _netcdf_bytes(product.variable, values=values)
    store.write_bytes_atomic(product.object_uri, content)
    return type(product)(
        canonical_product_id=product.canonical_product_id,
        source_id=product.source_id,
        cycle_time=product.cycle_time,
        valid_time=product.valid_time,
        variable=product.variable,
        unit=product.unit,
        grid_id=product.grid_id,
        object_uri=product.object_uri,
        checksum=sha256_bytes(content),
        grid_definition_uri=product.grid_definition_uri,
        native_time_resolution=product.native_time_resolution,
        native_spatial_resolution=product.native_spatial_resolution,
        quality_flag=product.quality_flag,
        lead_time_hours=product.lead_time_hours,
        lineage_json=product.lineage_json,
    )
