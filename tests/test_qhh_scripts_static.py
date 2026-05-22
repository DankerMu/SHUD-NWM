from __future__ import annotations

from pathlib import Path

import pytest

from packages.common.object_store import LocalObjectStore
from scripts import create_qhh_shud_manifest as qhh_manifest


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


def test_qhh_manifest_helper_does_not_hard_code_default_nhms_object_prefix(tmp_path: Path) -> None:
    manifest_script = Path("scripts/create_qhh_shud_manifest.py").read_text(encoding="utf-8")
    store = LocalObjectStore(tmp_path, "")

    assert 'os.getenv("OBJECT_STORE_PREFIX", "")' in manifest_script
    assert '"s3://nhms"' not in manifest_script
    assert qhh_manifest._directory_uri(store, "runs/run_1/output") == "runs/run_1/output/"
