from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.common.object_store import LocalObjectStore, sha256_bytes
from scripts import create_qhh_shud_manifest as qhh_manifest
from workers.model_registry import qhh_production_bootstrap


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

    store.write_bytes_atomic(tsd_uri, b"\xef\xbb\xbf")
    with pytest.raises(RuntimeError, match="station header is empty"):
        qhh_manifest._validate_shud_forcing_header(manifest, store, 2)


def test_run_qhh_cycle_validates_model_output_interval_before_shud_runtime() -> None:
    script = Path("scripts/run_qhh_cycle.sh").read_text(encoding="utf-8")

    assert "validate_model_output_interval()" in script
    assert "must evenly divide forecast window" in script
    assert script.index("\nvalidate_model_output_interval\n") < script.index("\nprepare_database_url\n")


def test_slurm_sbatch_cleans_filtered_env_file_after_sourcing() -> None:
    script = Path("scripts/run_qhh_cycle.sbatch").read_text(encoding="utf-8")

    assert "trap cleanup_slurm_env_file EXIT" in script
    assert 'rm -f -- "$QHH_SLURM_ENV_FILE"' in script
    assert script.index('source "$QHH_SLURM_ENV_FILE"') < script.index('exec "$ROOT_DIR/scripts/run_qhh_cycle.sh"')


def test_qhh_manifest_rejects_forcing_package_checksum_mismatch(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    manifest_uri = "forcing/gfs/2026050700/basin_v1/demo_model/forcing_package.json"
    store.write_bytes_atomic(manifest_uri, b'{"station_count":1}\n')

    with pytest.raises(RuntimeError, match="forcing_version checksum does not match"):
        qhh_manifest._validate_forcing_package_checksum_matches_db(
            {"checksum": "stale-db-checksum"},
            manifest_uri,
            store,
        )


def test_qhh_manifest_accepts_forcing_package_checksum_and_exports_file_evidence(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    manifest_uri = "forcing/gfs/2026050700/basin_v1/demo_model/forcing_package.json"
    tsd_uri = "forcing/gfs/2026050700/basin_v1/demo_model/shud/qhh.tsd.forc"
    tsd_content = b"1 20260507\n/data\nID Lon Lat X Y Z Filename\n1 100 30 1 1 1 forcing.csv\n"
    store.write_bytes_atomic(tsd_uri, tsd_content)
    package_manifest = {
        "station_count": 1,
        "files": [
            {
                "role": "shud_forcing",
                "relative_path": "shud/qhh.tsd.forc",
                "uri": tsd_uri,
                "checksum": sha256_bytes(tsd_content),
            }
        ],
        "lineage": {"station_signature": {"station_count": 1}},
    }
    package_content = json.dumps(package_manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    store.write_bytes_atomic(manifest_uri, package_content)

    checksum = qhh_manifest._validate_forcing_package_checksum_matches_db(
        {"checksum": sha256_bytes(package_content)},
        manifest_uri,
        store,
    )
    files = qhh_manifest._forcing_file_checksums(package_manifest)

    assert checksum == sha256_bytes(package_content)
    assert files == [
        {
            "role": "shud_forcing",
            "relative_path": "shud/qhh.tsd.forc",
            "uri": tsd_uri,
            "checksum": sha256_bytes(tsd_content),
        }
    ]


def test_seed_qhh_shud_output_segments_ignores_existing_output_rows_for_order_offset() -> None:
    sql = Path(qhh_production_bootstrap.__file__).read_text(encoding="utf-8")

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


def test_local_pg_start_logs_redacted_database_url_and_url_command_prints_full_url() -> None:
    script = Path("scripts/local_pg.sh").read_text(encoding="utf-8")
    start_body = script[script.index("start() {") : script.index("\nstop() {")]
    url_body = script[script.index("\nurl() {") : script.index('\ncase "${1:-start}"')]

    assert "redacted_database_url()" in script
    assert 'log "DATABASE_URL=$(redacted_database_url)"' in start_body
    assert 'log "DATABASE_URL=$(cat "$ROOT_DIR/.pgdata/qhh-smoke.database-url")"' not in script
    assert '-v app_password="$APP_PASSWORD"' not in script
    assert "database_url" in url_body
    assert "redacted_database_url" not in url_body


def test_local_pg_database_url_file_is_created_private_without_real_postgres() -> None:
    script = Path("scripts/local_pg.sh").read_text(encoding="utf-8")
    start_body = script[script.index("start() {") : script.index("\nstop() {")]
    init_body = script[script.index("init() {") : script.index("\nstart() {")]

    assert 'mkdir -p "$ROOT_DIR/.pgdata" "$PGDATA" "$PGSOCKET_DIR" "$PGLOG_DIR"' in init_body
    assert 'chmod 700 "$ROOT_DIR/.pgdata" "$PGDATA" "$PGSOCKET_DIR" "$PGLOG_DIR"' in init_body
    assert "umask 077" in start_body
    assert 'url_file="$ROOT_DIR/.pgdata/qhh-smoke.database-url"' in start_body
    assert 'tmp_url_file="$(mktemp "$url_file.XXXXXX")"' in start_body
    assert 'database_url > "$tmp_url_file"' in start_body
    assert 'chmod 600 "$tmp_url_file"' in start_body
    assert 'mv -f "$tmp_url_file" "$url_file"' in start_body
