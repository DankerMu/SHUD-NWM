from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.shud_forcing_contract import (
    CANONICAL_SHUD_FORCING_INDEX_BASENAME,
    CANONICAL_SHUD_FORCING_INDEX_MEMBER,
    LEGACY_SHUD_FORCING_INDEX_BASENAME,
    LEGACY_SHUD_FORCING_INDEX_MEMBER,
)
from scripts import create_qhh_shud_manifest as qhh_manifest
from services.orchestrator.chain import ForecastOrchestrator
from workers.model_registry import qhh_production_bootstrap

# M24 §5.1 diagnostic-retirement guardrail: QHH diagnostic scripts are RETAINED
# for manual debugging but MUST NOT be referenced/invoked by the production
# scheduler/chain cohort path (the generic daemon is the supported runner). This
# static scan enforces that boundary. The phase62 boundary also requires the
# backend-smoke shell to fail closed on the canonical active checkout: direct
# Python runs through the detached root's exact `.venv/bin/python`, and `uv run`
# remains only for `nhms-*`/dynamic forcing CLIs after the detached guard.
_QHH_DIAGNOSTIC_TOKENS = (
    "run_qhh_cycle",
    "run_qhh_continuous",
    "run_qhh_backend_smoke",
    "create_qhh_shud_manifest",
    "scripts/run_qhh_cycle.sh",
    "scripts/run_qhh_continuous.py",
    "scripts/run_qhh_backend_smoke.sh",
    "scripts/create_qhh_shud_manifest.py",
)


def _production_cohort_sources() -> list[Path]:
    """Recursively glob services/orchestrator production Python modules (the
    diagnostic scripts and this test file are NOT scanned — they contain the
    diagnostic basenames)."""
    return sorted(
        path
        for path in Path("services/orchestrator").rglob("*.py")
        if "__pycache__" not in path.parts
    )


def test_production_scheduler_does_not_invoke_qhh_diagnostic_scripts() -> None:
    sources = _production_cohort_sources()
    assert sources, "expected services/orchestrator production Python modules to scan"

    for source_path in sources:
        text = source_path.read_text(encoding="utf-8")
        for token in _QHH_DIAGNOSTIC_TOKENS:
            assert token not in text, (
                f"production cohort module {source_path} references diagnostic token "
                f"{token!r}; QHH scripts are diagnostic-only and must not be "
                "wired into the production scheduler/chain path (M24 §5.1)"
            )
        # The manifest builder must be the chain's own runtime-manifest assembly,
        # never an import of the diagnostic standalone builder.
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                assert "create_qhh_shud_manifest" not in stripped, (
                    f"production cohort module {source_path} imports the diagnostic "
                    "manifest builder (create_qhh_shud_manifest); production manifests "
                    "are assembled by services/orchestrator/chain.py (M24 §5.1)"
                )

    # Mirror the spec's "manifest builder is the chain" requirement: the production
    # runtime-manifest assembly stays in the chain cohort, not the diagnostic script.
    manifest_text = Path("services/orchestrator/chain_manifests.py").read_text(encoding="utf-8")
    assert "_build_forecast_runtime_manifest" in manifest_text
    assert ForecastOrchestrator._build_forecast_runtime_manifest.__name__ == "_build_forecast_runtime_manifest"


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


def _station_index_manifest(member: str, tsd_uri: str) -> dict:
    return {
        "station_count": 1,
        "files": [{"role": "shud_forcing", "relative_path": member, "uri": tsd_uri}],
        "lineage": {"station_signature": {"station_count": 1}},
    }


@pytest.mark.parametrize(
    ("member", "expected_source"),
    [
        (CANONICAL_SHUD_FORCING_INDEX_MEMBER, CANONICAL_SHUD_FORCING_INDEX_BASENAME),
        (LEGACY_SHUD_FORCING_INDEX_MEMBER, LEGACY_SHUD_FORCING_INDEX_BASENAME),
    ],
    ids=["canonical", "legacy"],
)
def test_qhh_manifest_accepts_either_station_index_member(
    tmp_path: Path,
    member: str,
    expected_source: str,
) -> None:
    """B10 (#1176): the builder mirrors the runtime contract (canonical or legacy),
    recording the identity it matched so ``station_source`` never contradicts it."""
    store = LocalObjectStore(tmp_path)
    tsd_uri = f"forcing/gfs/2026050700/basin_v1/demo_model/{member}"
    store.write_bytes_atomic(tsd_uri, b"1 20260507\n/data\nID Lon Lat X Y Z Filename\n1 100 30 1 1 1 f.csv\n")

    station_source = qhh_manifest._validate_shud_forcing_header(
        _station_index_manifest(member, tsd_uri), store, 1
    )

    assert station_source == expected_source


def test_qhh_manifest_rejects_both_station_index_members(tmp_path: Path) -> None:
    """B10 (#1176): canonical + legacy in one manifest is fail-closed."""
    store = LocalObjectStore(tmp_path)
    tsd_bytes = b"1 20260507\n/data\nID Lon Lat X Y Z Filename\n1 100 30 1 1 1 f.csv\n"
    canonical_uri = f"forcing/gfs/2026050700/basin_v1/demo_model/{CANONICAL_SHUD_FORCING_INDEX_MEMBER}"
    legacy_uri = f"forcing/gfs/2026050700/basin_v1/demo_model/{LEGACY_SHUD_FORCING_INDEX_MEMBER}"
    store.write_bytes_atomic(canonical_uri, tsd_bytes)
    store.write_bytes_atomic(legacy_uri, tsd_bytes)
    manifest = {
        "station_count": 1,
        "files": [
            {"role": "shud_forcing", "relative_path": CANONICAL_SHUD_FORCING_INDEX_MEMBER, "uri": canonical_uri},
            {"role": "shud_forcing", "relative_path": LEGACY_SHUD_FORCING_INDEX_MEMBER, "uri": legacy_uri},
        ],
        "lineage": {"station_signature": {"station_count": 1}},
    }

    with pytest.raises(RuntimeError, match="more than one SHUD station-index member") as exc_info:
        qhh_manifest._validate_shud_forcing_header(manifest, store, 1)

    assert CANONICAL_SHUD_FORCING_INDEX_MEMBER in str(exc_info.value)
    assert LEGACY_SHUD_FORCING_INDEX_MEMBER in str(exc_info.value)


def test_qhh_manifest_missing_station_index_member_names_both_identities(tmp_path: Path) -> None:
    """B10 (#1176): the absence error names both accepted identities."""
    store = LocalObjectStore(tmp_path)
    manifest = {
        "station_count": 1,
        "files": [{"role": "shud_forcing_csv", "relative_path": "shud/f.csv", "uri": "shud/f.csv"}],
        "lineage": {"station_signature": {"station_count": 1}},
    }

    with pytest.raises(RuntimeError, match="missing the SHUD station-index member") as exc_info:
        qhh_manifest._validate_shud_forcing_header(manifest, store, 1)

    assert CANONICAL_SHUD_FORCING_INDEX_MEMBER in str(exc_info.value)
    assert LEGACY_SHUD_FORCING_INDEX_MEMBER in str(exc_info.value)


def test_qhh_manifest_header_failure_names_the_resolved_member(tmp_path: Path) -> None:
    """B16 (#1176 round-1 V3-1): header failures name the member actually resolved
    (two failure wings carry no URI, so the member name is their only identity)."""
    store = LocalObjectStore(tmp_path)
    tsd_uri = f"forcing/gfs/2026050700/basin_v1/demo_model/{CANONICAL_SHUD_FORCING_INDEX_MEMBER}"
    manifest = _station_index_manifest(CANONICAL_SHUD_FORCING_INDEX_MEMBER, tsd_uri)

    store.write_bytes_atomic(tsd_uri, b"2 20260507\nshud\n")
    with pytest.raises(RuntimeError, match="station header") as count_mismatch:
        qhh_manifest._validate_shud_forcing_header(manifest, store, 1)
    assert str(count_mismatch.value).startswith(f"{CANONICAL_SHUD_FORCING_INDEX_BASENAME} station header")
    assert LEGACY_SHUD_FORCING_INDEX_BASENAME not in str(count_mismatch.value)

    store.write_bytes_atomic(tsd_uri, b"not-a-count 20260507\nshud\n")
    with pytest.raises(RuntimeError, match="station header count is invalid") as invalid_count:
        qhh_manifest._validate_shud_forcing_header(manifest, store, 1)
    assert str(invalid_count.value).startswith(f"{CANONICAL_SHUD_FORCING_INDEX_BASENAME} station header")

    store.write_bytes_atomic(tsd_uri, b"\xef\xbb\xbf")
    with pytest.raises(RuntimeError, match="station header is empty") as empty_header:
        qhh_manifest._validate_shud_forcing_header(manifest, store, 1)
    assert str(empty_header.value).startswith(f"{CANONICAL_SHUD_FORCING_INDEX_BASENAME} station header")


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


def test_run_qhh_cycle_preserves_ifs_probe_failed_json_before_set_e_exit() -> None:
    script = Path("scripts/run_qhh_cycle.sh").read_text(encoding="utf-8")

    assert 'uv run nhms-ifs download --cycle-time "$CYCLE_TIME" | tee "$CYCLE_ROOT/download.stdout.json"' in script
    assert 'DOWNLOAD_EXIT="${PIPESTATUS[0]}"' in script
    assert 'if [[ "$DOWNLOAD_STATUS" == "probe_failed" || "$DOWNLOAD_STATUS" == "rate_limited" ]]; then' in script
    assert 'json_status "$STATE_FILE" "$DOWNLOAD_STATUS"' in script
    assert '"classifier=${DOWNLOAD_CLASSIFIER:-$DOWNLOAD_STATUS}"' in script
    assert 'exit 0' in script[script.index('if [[ "$DOWNLOAD_STATUS" == "probe_failed"') :]


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


def _qhh_run_guard_markers() -> dict[str, str]:
    """Anchor strings that must all precede the first chain/uv action per file
    (located by ``script.index`` so mutation-proof checks share one definition)."""
    return {
        "continuous_main": 'def main(argv: list[str] | None = None) -> int:',
        "continuous_guard": "def _require_detached_diagnostic_checkout",
        "continuous_guard_call": "_require_detached_diagnostic_checkout()",
        "continuous_db": "_require_slurm_reachable_database()",
        "continuous_run_root_mkdir": 'run_root.mkdir(parents=True, exist_ok=True)',
        "continuous_lock": "_exclusive_lock(",
        "continuous_subprocess": "subprocess.run(",
        "cycle_guard": 'BLOCKED: the QHH diagnostic chain cannot run from the canonical active checkout',
        "cycle_mkdir": 'mkdir -p "$RUN_ROOT" "$OBJECT_ROOT" "$CYCLE_ROOT"',
        "cycle_uv_first": "uv run ",
        "sbatch_guard": 'BLOCKED: the QHH diagnostic chain cannot run from the canonical active checkout',
        "sbatch_mkdir": "slurm-logs",
        "sbatch_exec": 'exec "$ROOT_DIR/scripts/run_qhh_cycle.sh"',
        "backend_guard": "BLOCKED: the QHH diagnostic chain cannot run from the canonical active checkout",
        "backend_mkdir": 'mkdir -p "$RUN_ROOT" "$OBJECT_ROOT"',
        "backend_uv_first": "uv run ",
    }


def test_qhh_python_main_guard_precedes_db_run_root_lock_and_subprocess() -> None:
    script = Path("scripts/run_qhh_continuous.py").read_text(encoding="utf-8")
    markers = _qhh_run_guard_markers()

    assert markers["continuous_guard"] in script
    assert markers["continuous_db"] in script
    assert markers["continuous_run_root_mkdir"] in script
    assert markers["continuous_lock"] in script
    assert markers["continuous_subprocess"] in script
    assert 'CANONICAL_ACTIVE_ROOT = Path("/scratch/frd_muziyao/NWM").resolve()' in script
    # The exact call must exist inside main() and be strictly ordered
    # def main < guard call < DB/run-root mkdir/lock/subprocess. The helper
    # definition alone ("def _require_detached_diagnostic_checkout") is not a
    # call and must not satisfy the seam.
    assert markers["continuous_guard_call"] in script
    main_index = script.index(markers["continuous_main"])
    guard_call_index = script.index(markers["continuous_guard_call"])
    assert main_index < guard_call_index
    assert guard_call_index < script.index(markers["continuous_db"])
    assert guard_call_index < script.index(markers["continuous_run_root_mkdir"])
    assert guard_call_index < script.index(markers["continuous_lock"])
    assert guard_call_index < script.index(markers["continuous_subprocess"])


def test_qhh_cycle_shell_guard_precedes_first_uv_run_and_mkdir() -> None:
    script = Path("scripts/run_qhh_cycle.sh").read_text(encoding="utf-8")
    markers = _qhh_run_guard_markers()

    # ROOT_DIR must be the canonical physical path (pwd -P); a logical symlink
    # alias of the active checkout must not slip past the guard.
    assert 'ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"' in script
    assert 'ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"' not in script
    assert 'if [[ "$ROOT_DIR" == "/scratch/frd_muziyao/NWM" ]]; then' in script
    assert markers["cycle_guard"] in script
    assert markers["cycle_mkdir"] in script
    assert markers["cycle_uv_first"] in script
    assert script.index('ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"') < script.index(
        markers["cycle_guard"]
    )
    assert script.index(markers["cycle_guard"]) < script.index(markers["cycle_mkdir"])
    assert script.index(markers["cycle_guard"]) < script.index(markers["cycle_uv_first"])


def _assert_backend_smoke_guard(text: str) -> None:
    """phase62: backend-smoke is the fourth detached-only boundary. Pure-text seam
    for in-memory mutations: the active-root rejection, the exact
    `.venv/bin/python` existence guard, and ``$PYTHON`` all precede any
    RUN_ROOT-derived state, mkdir, direct Python, or `uv run` action."""
    script = text
    markers = _qhh_run_guard_markers()

    assert 'ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"' in script
    assert 'ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"' not in script
    assert 'if [[ "$ROOT_DIR" == "/scratch/frd_muziyao/NWM" ]]; then' in script
    assert markers["backend_guard"] in script
    assert 'PYTHON="$ROOT_DIR/.venv/bin/python"' in script
    assert 'if [[ ! -x "$PYTHON" ]]; then' in script
    assert markers["backend_mkdir"] in script
    assert markers["backend_uv_first"] in script

    root_index = script.index('ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"')
    guard_index = script.index(markers["backend_guard"])
    python_def_index = script.index('PYTHON="$ROOT_DIR/.venv/bin/python"')
    exact_guard_index = script.index('if [[ ! -x "$PYTHON" ]]; then')
    mkdir_index = script.index(markers["backend_mkdir"])
    # First executable `uv run` line (prose/header mentions must not anchor).
    first_uv_index = min(
        script.index(line)
        for line in script.splitlines()
        if "uv run" in line and not line.lstrip().startswith("#") and not line.lstrip().startswith("printf ")
    )
    # Guard order before any side effect: active-root rejection, then the
    # exact-interpreter existence guard, then state/mkdir/Python/uv. The guarded
    # direct-Python DB check must also precede the migration scripts.
    assert root_index < guard_index
    assert guard_index < python_def_index
    assert python_def_index < exact_guard_index
    assert exact_guard_index < mkdir_index
    assert exact_guard_index < script.index('"$PYTHON" - "$path" "$status" "$reason"')
    assert exact_guard_index < first_uv_index
    assert script.index('"$PYTHON" - <<\'PY\'') < script.index("apply_smoke_migrations.py")


def test_qhh_backend_smoke_shell_guard_precedes_state_mkdir_direct_python_and_uv() -> None:
    script = Path("scripts/run_qhh_backend_smoke.sh").read_text(encoding="utf-8")
    _assert_backend_smoke_guard(script)


def _backend_shell_logical_lines(text: str) -> list[str]:
    """Bounded logical-shell view: join `\\` continuations, drop `#` comments and
    heredoc Python bodies (a `<<'PY'` opener swallows to the closing `PY`)."""
    physical = text.splitlines()
    logical: list[str] = []
    i = 0
    while i < len(physical):
        line = physical[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if "<<" in line and line.rstrip().endswith("'PY'"):
            logical.append(line)
            i += 1
            while i < len(physical) and physical[i].rstrip() != "PY":
                i += 1
            i += 1  # the closing PY marker
            continue
        joined = line
        while joined.rstrip().endswith("\\") and i + 1 < len(physical):
            i += 1
            joined = joined[: joined.rstrip().rfind("\\")] + " " + physical[i].lstrip()
        logical.append(joined)
        i += 1
    return logical


def _backend_direct_python_calls(logical: list[str]) -> list[str]:
    """Direct-Python launcher lines (`if !`, `RUN_ID="$(...)`, or a leading
    `"$PYTHON"`/`python`/`python3`/`uv run python`)."""
    direct: list[str] = []
    for line in logical:
        stripped = line.lstrip()
        if stripped.startswith(("if ! ", 'RUN_ID="$(')) and "PYTHON" in line:
            direct.append(line)
            continue
        if stripped.startswith(("python ", "python3 ", "uv run python", '"$PYTHON" ')) and "PYTHON" in line:
            direct.append(line)
    return direct


def _assert_backend_inventory(text: str) -> None:
    """Lock direct-Python (10) and uv CLI (9) inventories: exact `"$PYTHON"`
    launchers/targets and exact `nhms-*`/dynamic forcing uv commands."""
    logical = _backend_shell_logical_lines(text)
    direct = _backend_direct_python_calls(logical)
    expected = [
        '"$PYTHON" - "$path" "$status" "$reason" <<',  # stdin: write_json_status
        'if ! "$PYTHON" - <<',  # stdin: DB gate
        '"$PYTHON" scripts/apply_smoke_migrations.py',
        '"$PYTHON" -m packages.common.migrate',
        '"$PYTHON" scripts/reset_qhh_smoke_db.py',
        '"$PYTHON" scripts/seed_qhh_forcing_stations.py',
        '"$PYTHON" scripts/seed_qhh_shud_output_segments.py',
        '"$PYTHON" scripts/create_qhh_shud_manifest.py',
        'RUN_ID="$("$PYTHON" - "$RUN_ROOT/create-qhh-shud-manifest.stdout.json"',  # stdin: RUN_ID parse
        '"$PYTHON" scripts/summarize_qhh_smoke_results.py',
    ]
    assert len(direct) == len(expected), f"expected {len(expected)} direct-Python calls, got {len(direct)}"
    for line in expected:
        # Each expected launcher must match exactly one collected call.
        matches = [ln for ln in direct if line.split(" ", 1)[1] in ln]
        assert len(matches) == 1, f"expected exactly one direct-Python call {line!r}"
        assert '"$PYTHON"' in matches[0] and "uv run" not in matches[0]

    stdin_calls = [ln for ln in direct if "<<'PY'" in ln]
    assert len(stdin_calls) == 3, f"expected 3 stdin direct-Python calls, got {len(stdin_calls)}"
    assert any('"$PYTHON" - "$path" "$status" "$reason" <<' in ln for ln in stdin_calls)
    assert any('if ! "$PYTHON" - <<' in ln for ln in stdin_calls)
    assert any('RUN_ID="$("$PYTHON" - "$RUN_ROOT/create-qhh-shud-manifest.stdout.json"' in ln for ln in stdin_calls)

    uv = [line for line in logical if line.lstrip().startswith("uv run ")]
    expected_uv = [
        "uv run nhms-model discover-basins",
        "uv run nhms-model publish-basins",
        "uv run nhms-model import-basins-registry",
        "uv run nhms-gfs download",
        "uv run nhms-canonical convert",
        'uv run "${FORCING_ARGS[@]}"',
        "uv run nhms-shud-runtime execute",
        "uv run nhms-parse shud-output",
        "uv run nhms-orchestrator publish-qdown",
    ]
    assert len(uv) == len(expected_uv), f"expected {len(expected_uv)} uv run CLIs, got {len(uv)}"
    for line in expected_uv:
        assert any(line in ln for ln in uv), f"missing uv CLI {line!r}"
    for line in uv:
        assert "uv run python" not in line, f"uv run python must not survive: {line!r}"
    assert (
        'FORCING_ARGS=(nhms-forcing produce --source-id gfs --cycle-time "$QHH_CYCLE_TIME" --model-id "$MODEL_ID")'
        in text
    )
    # The exact-interpreter existence guard must precede the first direct-Python call.
    assert 'if [[ ! -x "$PYTHON" ]]; then' in text
    assert text.index('if [[ ! -x "$PYTHON" ]]; then') < text.index(direct[0])


def test_qhh_backend_smoke_direct_python_uses_exact_detached_interpreter() -> None:
    script = Path("scripts/run_qhh_backend_smoke.sh").read_text(encoding="utf-8")
    _assert_backend_inventory(script)


def test_qhh_sbatch_canonicalization_and_guard_precede_mkdir_and_exec() -> None:
    script = Path("scripts/run_qhh_cycle.sbatch").read_text(encoding="utf-8")
    markers = _qhh_run_guard_markers()

    assert 'ROOT_DIR="$(pwd -P)"' in script
    assert 'ROOT_DIR="${QHH_REPO_ROOT:-/scratch/frd_muziyao/NWM}"' in script
    assert 'if [[ "$ROOT_DIR" == "/scratch/frd_muziyao/NWM" ]]; then' in script
    assert markers["sbatch_guard"] in script
    assert markers["sbatch_mkdir"] in script
    assert markers["sbatch_exec"] in script
    assert script.index('ROOT_DIR="$(pwd -P)"') < script.index(markers["sbatch_guard"])
    assert script.index(markers["sbatch_guard"]) < script.index(markers["sbatch_mkdir"])
    assert script.index(markers["sbatch_guard"]) < script.index(markers["sbatch_exec"])


def test_qhh_manifest_smoke_requires_detached_root_exact_interpreter_and_no_bare_uv() -> None:
    readme = Path("scripts/diagnostic/qhh/README.md").read_text(encoding="utf-8")

    assert "Run Boundary (authoritative)" in readme
    assert "/scratch/frd_muziyao/NWM" in readme
    assert "must **fail closed**" in readme
    assert "QHH_DIAGNOSTIC_CHECKOUT" in readme
    assert (
        '"$QHH_DIAGNOSTIC_CHECKOUT/.venv/bin/python" "$QHH_DIAGNOSTIC_CHECKOUT/scripts/'
        'run_qhh_continuous.py" --once --executor slurm'
    ) in readme
    assert "/scratch/frd_muziyao/NWM/.venv/bin/python -m pytest" in readme
    # The manifest must never show a bare uv launch of the QHH chain.
    assert "uv run python scripts/run_qhh_continuous.py" not in readme
    assert "uv run pytest" not in readme


def _assert_manifest_boundary(text: str) -> None:
    """The manifest's Run Boundary must enumerate all four entrypoints (scoped to
    that section so the tables below cannot mask a removal)."""
    readme = text

    boundary = readme[
        readme.index("Run Boundary (authoritative)") : readme.index("## Diagnostic Entrypoints")
    ]
    for token in (
        "scripts/run_qhh_continuous.py",
        "scripts/run_qhh_cycle.sh",
        "scripts/run_qhh_cycle.sbatch",
        "scripts/run_qhh_backend_smoke.sh",
    ):
        assert token in boundary
    assert "All four" in boundary


def test_qhh_manifest_authoritative_boundary_lists_four_entrypoints_including_backend_smoke() -> None:
    readme = Path("scripts/diagnostic/qhh/README.md").read_text(encoding="utf-8")
    _assert_manifest_boundary(readme)


def test_qhh_manifest_backend_smoke_entrypoint_semantics() -> None:
    """phase62: the manifest states backend-smoke's exact-interpreter and guard rule."""
    readme = Path("scripts/diagnostic/qhh/README.md").read_text(encoding="utf-8")

    assert "run_qhh_backend_smoke.sh" in readme
    assert "exact `.venv/bin/python`" in readme
    assert "detached + exact-interpreter guard" in readme


def _assert_backend_smoke_runbook(text: str) -> None:
    """The runbook recipe must be explicitly detached (no active-root $PWD recipe)."""
    runbook = text

    assert "QHH_DIAGNOSTIC_CHECKOUT" in runbook
    assert 'pwd -P' in runbook
    assert "QHH_DIAGNOSTIC_CHECKOUT must be a detached worktree, not the active root" in runbook
    assert "$QHH_DIAGNOSTIC_CHECKOUT/.venv/bin/python" in runbook
    assert 'cd "$QHH_DIAGNOSTIC_CHECKOUT"' in runbook

    cd_index = runbook.index('cd "$QHH_DIAGNOSTIC_CHECKOUT"')
    assert runbook.index("scripts/local_pg.sh start") > cd_index
    assert runbook.index("scripts/run_qhh_backend_smoke.sh") > cd_index
    assert "$PWD/SHUD/shud" not in runbook
    assert "SHUD_EXECUTABLE=$QHH_DIAGNOSTIC_CHECKOUT/SHUD/shud" in runbook


def test_qhh_backend_smoke_runbook_recipe_is_detached_explicit_no_implicit_active_root() -> None:
    runbook = Path("docs/runbooks/qhh-backend-smoke.md").read_text(encoding="utf-8")
    _assert_backend_smoke_runbook(runbook)


def test_qhh_continuous_module_smoke_requires_detached_root_exact_interpreter() -> None:
    module = Path("scripts/run_qhh_continuous.py").read_text(encoding="utf-8")

    assert "QHH_DIAGNOSTIC_CHECKOUT" in module
    assert '"$QHH_DIAGNOSTIC_CHECKOUT/.venv/bin/python"' in module
    assert "uv run python scripts/run_qhh_continuous.py" not in module


def test_qhh_cycle_header_requires_detached_root_and_forbids_bare_uv_smoke() -> None:
    script = Path("scripts/run_qhh_cycle.sh").read_text(encoding="utf-8")

    assert "QHH_DIAGNOSTIC_CHECKOUT" in script
    assert 'uv run python scripts/run_qhh_continuous.py --once --executor slurm' not in script


def test_qhh_cycle_shell_root_is_physical_canonical_through_symlink_alias(tmp_path: Path) -> None:
    """The cycle shell's ROOT_DIR must be the physical path, not a symlink alias."""
    real_root = tmp_path / "real-checkout"
    scripts_dir = real_root / "scripts"
    scripts_dir.mkdir(parents=True)
    probe = scripts_dir / "probe-root.sh"
    probe.write_text(
        'ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"; printf "%s" "$ROOT_DIR"\n',
        encoding="utf-8",
    )
    alias = tmp_path / "alias-checkout"
    alias.symlink_to(real_root)

    completed = subprocess.run(
        ["bash", str(alias / "scripts" / "probe-root.sh")],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert os.path.realpath(completed.stdout.strip()) == str(real_root.resolve())
    assert str(alias) not in completed.stdout


def _backend_smoke_mutants() -> dict[str, str]:
    """One representative regression per seam, each must turn the guard tests red.
    In-memory only — the parent tree is never modified."""
    script = Path("scripts/run_qhh_backend_smoke.sh").read_text(encoding="utf-8")
    guard_line = 'if [[ "$ROOT_DIR" == "/scratch/frd_muziyao/NWM" ]]; then'
    exact_line = 'if [[ ! -x "$PYTHON" ]]; then'
    direct_line = '"$PYTHON" - "$path" "$status" "$reason" <<\'PY\''
    migrate_line = '"$PYTHON" scripts/apply_smoke_migrations.py | tee "$RUN_ROOT/migrate.log"'
    gate_line = '"$PYTHON" - <<\'PY\''
    root_p = 'ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"'
    root = 'ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"'
    discover_needle = "uv run nhms-model discover-basins \\\n  --basins-root"
    guard_head, _guard_sep, guard_tail = script.partition(discover_needle)
    return {
        "guard_moved_after_uv": guard_head.replace(guard_line, "# guard removed").replace(
            'cannot run from the canonical active checkout (%s).\\n\' "$ROOT_DIR" >&2',
            "(no active-root rejection message)\\n' >&2",
        )
        + f"\n{guard_line}\n"
        + discover_needle
        + guard_tail,
        "root_not_physical_and_guard_removed": script.replace(root_p, root).replace(guard_line, "# guard removed"),
        "wrong_active_literal": script.replace(
            guard_line, 'if [[ "$ROOT_DIR" == "/wrong/active/root" ]]; then'
        ).replace(
            'no exact .venv/bin/python interpreter (%s).\\n\' "$PYTHON" >&2',
            "(no exact interpreter message)\\n' >&2",
        ).replace(
            'if [[ "$ROOT_DIR" == "/wrong/active/root" ]]; then',
            'if [[ "$ROOT_DIR" == "/wrong/active/root" || "$ROOT_DIR" == "/scratch/frd_muziyao/NWM" ]]; then',
        ).replace(root_p, root),
        "exact_interpreter_guard_removed": script.replace(exact_line, "# exact-interpreter guard removed").replace(
            'no exact .venv/bin/python interpreter (%s).\\n\' "$PYTHON" >&2',
            "(no exact interpreter message)\\n' >&2",
        ).replace(
            "# exact-interpreter guard removed",
            'if [[ ! -e "$ROOT_DIR/.venv/bin/python" ]]; then\n  :  # non-fatal reachability check\nfi',
        ),
        "reverted_one_direct_python": script.replace(
            direct_line, 'uv run python - "$path" "$status" "$reason" <<\'PY\''
        ),
        "reverted_migrate_script": script.replace(
            migrate_line, 'uv run python scripts/apply_smoke_migrations.py | tee "$RUN_ROOT/migrate.log"'
        ).replace(gate_line, 'uv run python - <<\'PY\''),
        # Remove backend-smoke from the Run Boundary region only (the entrypoint
        # table, helper-dependency rows, and rg guard below still name it).
        "removed_backend_from_manifest_boundary": (
            lambda readme: readme[: readme.index("Run Boundary (authoritative)")]
            + readme[readme.index("Run Boundary (authoritative)") : readme.index("## Diagnostic Entrypoints")]
            .replace(
                "`scripts/run_qhh_cycle.sbatch`, `scripts/run_qhh_backend_smoke.sh`) refuse to start",
                "`scripts/run_qhh_cycle.sbatch`) refuse to start",
            )
            .replace("(`run_qhh_backend_smoke.sh`) runs", "runs")
            + readme[readme.index("## Diagnostic Entrypoints") :]
        )(Path("scripts/diagnostic/qhh/README.md").read_text(encoding="utf-8")),
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "guard_moved_after_uv",
        "root_not_physical_and_guard_removed",
        "wrong_active_literal",
        "exact_interpreter_guard_removed",
        "reverted_one_direct_python",
        "reverted_migrate_script",
        "removed_backend_from_manifest_boundary",
    ],
)
def test_qhh_backend_smoke_guard_mutations_are_red(mutation: str) -> None:
    """In-memory regressions must fail the phase62 static seam (red proof)."""
    source = _backend_smoke_mutants()[mutation]

    if mutation == "removed_backend_from_manifest_boundary":
        with pytest.raises(AssertionError):
            _assert_manifest_boundary(source)
        return

    raised = False
    try:
        _assert_backend_smoke_guard(source)
    except (AssertionError, ValueError):
        raised = True
    assert raised, "guard seam must reject the mutated script"
    # The active-root guard mutations must also fail the dedicated active-root
    # fail-closed seam (the exact-interpreter seam is covered by its own test).
    if mutation in {"guard_moved_after_uv", "root_not_physical_and_guard_removed", "wrong_active_literal"}:
        with pytest.raises(AssertionError):
            _assert_backend_smoke_active_root_rejection(source)
    else:
        with pytest.raises(AssertionError):
            _assert_backend_inventory(source)


def _assert_backend_smoke_active_root_rejection(text: str) -> None:
    """Active-root literal, physical ROOT_DIR, and rejection message must stay
    intact (the seam the guard/root/wrong-literal mutations regress)."""
    script = text
    assert 'ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"' in script
    assert 'if [[ "$ROOT_DIR" == "/scratch/frd_muziyao/NWM" ]]; then' in script
    assert "BLOCKED: the QHH diagnostic chain cannot run from the canonical active checkout" in script
    assert "scripts/diagnostic/qhh/README.md" in script
    assert script.index('ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"') < script.index(
        'if [[ "$ROOT_DIR" == "/scratch/frd_muziyao/NWM" ]]; then'
    )


def _backend_inventory_mutants() -> dict[str, str]:
    """In-memory regressions against the locked inventory (parent untouched);
    each targets a non-guard call (the summarize seed), unmaskable by the
    guard-ordering seam."""
    script = Path("scripts/run_qhh_backend_smoke.sh").read_text(encoding="utf-8")
    summarize = '"$PYTHON" scripts/summarize_qhh_smoke_results.py'
    publish_uv = "uv run nhms-orchestrator publish-qdown"
    return {
        "summarize_bare_python": script.replace(
            summarize, 'python scripts/summarize_qhh_smoke_results.py'
        ),
        "summarize_python3": script.replace(
            summarize, 'python3 scripts/summarize_qhh_smoke_results.py'
        ),
        "summarize_wrong_root": script.replace(
            summarize, '"$ROOT_DIR/venv/bin/python" scripts/summarize_qhh_smoke_results.py'
        ),
        "summarize_deleted": script.replace(summarize, ""),
        "publish_uv_bash": script.replace(publish_uv, "uv run bash publish-qdown"),
        "extra_unknown_uv": script.replace(publish_uv, "uv run nhms-orchestrator publish-qdown\nuv run nhms-mystery x"),
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "summarize_bare_python",
        "summarize_python3",
        "summarize_wrong_root",
        "summarize_deleted",
        "publish_uv_bash",
        "extra_unknown_uv",
    ],
)
def test_qhh_backend_smoke_inventory_mutations_are_red(mutation: str) -> None:
    """In-memory inventory regressions must fail the locked-inventory seam."""
    source = _backend_inventory_mutants()[mutation]
    with pytest.raises(AssertionError):
        _assert_backend_inventory(source)


def test_qhh_backend_smoke_inventory_mutants_cover_each_mutation() -> None:
    """The inventory battery is non-degenerate: every mutant differs from the source."""
    script = Path("scripts/run_qhh_backend_smoke.sh").read_text(encoding="utf-8")
    mutants = _backend_inventory_mutants()
    for name, mutated in mutants.items():
        assert mutated != script, f"inventory mutant {name} must differ from the source"


def test_qhh_backend_smoke_active_root_rejection_is_fail_closed() -> None:
    script = Path("scripts/run_qhh_backend_smoke.sh").read_text(encoding="utf-8")
    _assert_backend_smoke_active_root_rejection(script)


def _assert_backend_smoke_exact_interpreter_guard(text: str) -> None:
    """Exact-interpreter existence guard stays fail-closed before the first
    `$PYTHON` invocation."""
    script = text
    assert 'if [[ ! -x "$PYTHON" ]]; then' in script
    assert 'exit 2' in script[script.index('if [[ ! -x "$PYTHON" ]]; then') :]
    assert "no exact .venv/bin/python interpreter" in script
    assert script.index('if [[ ! -x "$PYTHON" ]]; then') < script.index('"$PYTHON" - "$path" "$status" "$reason"')


def test_qhh_backend_smoke_exact_interpreter_guard_is_fail_closed() -> None:
    script = Path("scripts/run_qhh_backend_smoke.sh").read_text(encoding="utf-8")
    _assert_backend_smoke_exact_interpreter_guard(script)


def test_qhh_backend_smoke_mutants_cover_each_mutation() -> None:
    """The mutation battery is non-degenerate: every mutant differs from the source."""
    script = Path("scripts/run_qhh_backend_smoke.sh").read_text(encoding="utf-8")
    mutants = _backend_smoke_mutants()

    assert mutants["guard_moved_after_uv"] != script
    assert mutants["root_not_physical_and_guard_removed"] != script
    assert mutants["wrong_active_literal"] != script
    assert mutants["exact_interpreter_guard_removed"] != script
    assert mutants["reverted_one_direct_python"] != script
    assert mutants["reverted_migrate_script"] != script
    assert mutants["removed_backend_from_manifest_boundary"] != Path(
        "scripts/diagnostic/qhh/README.md"
    ).read_text(encoding="utf-8")


def test_qhh_backend_smoke_tests_never_write_tracked_sources() -> None:
    """The static tests must never write tracked repo sources (mutation proof is
    in-memory; write_text is only for tmp_path fixtures)."""
    test_src = Path(__file__).read_text(encoding="utf-8")
    for tracked in ("scripts/run_qhh_backend_smoke.sh", "scripts/diagnostic/qhh/README.md"):
        assert f'write_text({tracked!r}' not in test_src
    # A write back would name the tracked basename or restore a mutated copy;
    # check call-shaped occurrences excluding this self-check's own needles.
    needle = "original_scr" + "ipt"
    assert f"write_text({needle}" not in test_src
    needle_manifest = "original_man" + "ifest"
    assert f"write_text({needle_manifest}" not in test_src
