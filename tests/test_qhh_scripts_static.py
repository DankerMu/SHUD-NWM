from __future__ import annotations

from pathlib import Path

import pytest

from packages.common.object_store import LocalObjectStore
from scripts import create_qhh_shud_manifest as qhh_manifest
from scripts import seed_qhh_shud_output_segments as seed_segments


def test_backend_smoke_exports_package_version_before_seed_helpers() -> None:
    script = Path("scripts/run_qhh_backend_smoke.sh").read_text(encoding="utf-8")

    export_index = script.index('export QHH_PACKAGE_VERSION="$PACKAGE_VERSION"')
    seed_index = script.index("scripts/seed_qhh_shud_output_segments.py")

    assert export_index < seed_index


def test_run_qhh_cycle_keeps_ifs_horizon_cycle_specific_by_default() -> None:
    script = Path("scripts/run_qhh_cycle.sh").read_text(encoding="utf-8")

    assert 'export IFS_FORECAST_END_HOUR="${QHH_IFS_FORECAST_END_HOUR:-${IFS_FORECAST_END_HOUR:-168}}"' not in script
    assert "unset IFS_FORECAST_END_HOUR" in script


def test_slurm_sbatch_sources_filtered_env_file_before_cycle_script() -> None:
    script = Path("scripts/run_qhh_cycle.sbatch").read_text(encoding="utf-8")

    assert 'source "$QHH_SLURM_ENV_FILE"' in script
    assert script.index('source "$QHH_SLURM_ENV_FILE"') < script.index('exec "$ROOT_DIR/scripts/run_qhh_cycle.sh"')


def test_qhh_manifest_uri_helpers_use_configured_non_default_object_prefix(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path, "s3://nhms-prod/qhh")

    assert qhh_manifest._directory_uri(store, "runs/run_1/output") == "s3://nhms-prod/qhh/runs/run_1/output/"
    assert qhh_manifest._model_package_uri(
        {"model_package_uri": "s3://nhms-prod/qhh/models/basins_qhh_shud/v1/package/"},
        store,
    ) == "s3://nhms-prod/qhh/models/basins_qhh_shud/v1/package/"
    with pytest.raises(ValueError, match="outside configured object store prefix|bucket does not match"):
        qhh_manifest._model_package_uri({"model_package_uri": "s3://nhms/models/bad/package/"}, store)


def test_qhh_manifest_rejects_db_model_package_uri_that_differs_from_published_version(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path, "s3://nhms-prod/qhh")
    expected = qhh_manifest._directory_uri(store, "models/basins_qhh_shud/v0.0.1-qhh-smoke-lake2/package")

    with pytest.raises(RuntimeError, match="model_package_uri does not match"):
        qhh_manifest._validate_model_package_uri_matches_published(
            "s3://nhms-prod/qhh/models/basins_qhh_shud/v0.0.1-qhh-smoke/package/",
            expected,
        )


def test_qhh_manifest_accepts_db_package_checksum_that_matches_published_manifest() -> None:
    qhh_manifest._validate_model_package_checksum_matches_published(
        {"resource_profile": {"package_checksum": "package-sha-1"}},
        {"package_checksum": "package-sha-1"},
    )
    qhh_manifest._validate_model_package_checksum_matches_published(
        {"resource_profile": '{"package_checksum": "package-sha-1"}'},
        {"package_checksum": "package-sha-1"},
    )


def test_qhh_manifest_rejects_db_package_checksum_that_differs_from_published_manifest() -> None:
    with pytest.raises(RuntimeError, match="package_checksum does not match"):
        qhh_manifest._validate_model_package_checksum_matches_published(
            {"resource_profile": {"package_checksum": "stale-package-sha"}},
            {"package_checksum": "package-sha-1"},
        )


def test_qhh_manifest_validates_forcing_manifest_station_count_and_header(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    tsd_uri = "forcing/gfs/2026050700/basin_v1/demo_model/shud/qhh.tsd.forc"
    store.write_bytes_atomic(tsd_uri, b"2 20260507\nshud\n")
    manifest = {
        "station_count": 2,
        "files": [
            {
                "relative_path": "shud/qhh.tsd.forc",
                "uri": tsd_uri,
            }
        ],
        "lineage": {
            "station_signature": {
                "station_count": 2,
                "station_ids": ["qhh_forc_001", "qhh_forc_002"],
                "checksum": "station-checksum",
            }
        },
    }

    assert qhh_manifest._forcing_manifest_station_count(manifest) == 2
    qhh_manifest._validate_shud_forcing_header(manifest, store, 2)

    store.write_bytes_atomic(tsd_uri, b"1 20260507\nshud\n")
    with pytest.raises(RuntimeError, match="station header"):
        qhh_manifest._validate_shud_forcing_header(manifest, store, 2)


def test_seed_qhh_shud_output_segments_ignores_existing_output_rows_for_order_offset() -> None:
    sql = Path(seed_segments.__file__).read_text(encoding="utf-8")

    assert "COALESCE(properties_json->>'shud_output_river', 'false') <> 'true'" in sql


def test_qhh_manifest_helper_does_not_hard_code_default_nhms_object_prefix(tmp_path: Path) -> None:
    manifest_script = Path("scripts/create_qhh_shud_manifest.py").read_text(encoding="utf-8")
    store = LocalObjectStore(tmp_path, "")

    assert 'os.getenv("OBJECT_STORE_PREFIX", "")' in manifest_script
    assert '"s3://nhms"' not in manifest_script
    assert qhh_manifest._directory_uri(store, "runs/run_1/output") == "runs/run_1/output/"


def test_run_qhh_cycle_registry_ready_requires_published_package_manifest_match() -> None:
    script = Path("scripts/run_qhh_cycle.sh").read_text(encoding="utf-8")

    assert 'uv run python - "$MODEL_ID" "$PACKAGE_MANIFEST"' in script
    assert "existing_uri == incoming_uri and existing_checksum == incoming_checksum" in script
