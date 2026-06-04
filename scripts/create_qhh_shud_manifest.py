from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

from packages.common.object_store import LocalObjectStore
from packages.common.source_identity import normalize_source_id

ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = Path(os.getenv("QHH_RUN_ROOT", ROOT / ".nhms-runs" / "qhh-continuous")).resolve()
PACKAGE_MANIFEST = Path(os.getenv("PACKAGE_MANIFEST", RUN_ROOT / "qhh-package-manifest.json")).expanduser().resolve()
OBJECT_STORE_ROOT = Path(os.getenv("OBJECT_STORE_ROOT", str(RUN_ROOT))).expanduser().resolve()
OBJECT_STORE_PREFIX = os.getenv("OBJECT_STORE_PREFIX", "")
MODEL_ID = os.getenv("QHH_MODEL_ID", "basins_qhh_shud")
PACKAGE_VERSION = os.getenv("QHH_PACKAGE_VERSION", "v0.0.1-qhh-smoke-lake2")
SOURCE_ID = normalize_source_id(os.getenv("QHH_SOURCE_ID", "gfs"))
PROJECT_NAME = os.getenv("QHH_PROJECT_NAME", "qhh")
OUTPUT_INTERVAL_MINUTES = int(os.getenv("QHH_MODEL_OUTPUT_INTERVAL", "5"))
THREADS = int(os.getenv("QHH_SHUD_THREADS", "1"))


def main() -> int:
    cycle_token = os.environ["QHH_CYCLE_TIME"]
    cycle_time = datetime.strptime(cycle_token, "%Y%m%d%H").replace(tzinfo=UTC)
    source_segment = SOURCE_ID.lower()
    forcing_version_id = f"forc_{source_segment}_{cycle_token}_{MODEL_ID}"
    run_id = os.getenv("QHH_RUN_ID", f"fcst_{source_segment}_{cycle_token}_{MODEL_ID}")
    database_url = os.environ["DATABASE_URL"]
    object_store = LocalObjectStore(OBJECT_STORE_ROOT, OBJECT_STORE_PREFIX)

    with psycopg2.connect(database_url) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT model_id,
                   basin_version_id,
                   river_network_version_id,
                   mesh_version_id,
                   model_package_uri,
                   resource_profile
            FROM core.model_instance
            WHERE model_id = %s
            """,
            (MODEL_ID,),
        )
        model = _one(cur.fetchone(), f"model_instance not found: {MODEL_ID}")
        cur.execute(
            """
            SELECT forcing_version_id,
                   model_id,
                   source_id,
                   cycle_time,
                   start_time,
                   end_time,
                   forcing_package_uri,
                   checksum
            FROM met.forcing_version
            WHERE forcing_version_id = %s
            """,
            (forcing_version_id,),
        )
        forcing = _one(cur.fetchone(), f"forcing_version not found: {forcing_version_id}")

    segment_count = _first_int(RUN_ROOT / "models" / MODEL_ID / PACKAGE_VERSION / "package" / f"{PROJECT_NAME}.sp.riv")
    expected_package_uri = _directory_uri(object_store, f"models/{MODEL_ID}/{PACKAGE_VERSION}/package")
    model_package_uri = _model_package_uri(model, object_store)
    _validate_model_package_uri_matches_published(model_package_uri, expected_package_uri)
    published_package_manifest = _published_package_manifest(PACKAGE_MANIFEST)
    _validate_model_package_checksum_matches_published(model, published_package_manifest)
    forcing_manifest_uri = _forcing_package_manifest_uri(forcing)
    forcing_manifest_checksum = _validate_forcing_package_checksum_matches_db(
        forcing,
        forcing_manifest_uri,
        object_store,
    )
    forcing_manifest = _forcing_package_manifest(forcing_manifest_uri, object_store)
    station_count = _forcing_manifest_station_count(forcing_manifest)
    _validate_shud_forcing_header(forcing_manifest, object_store, station_count)
    forcing_files = _forcing_file_checksums(forcing_manifest)
    start_time = _format_time(forcing["start_time"])
    end_time = _format_time(forcing["end_time"])
    manifest = {
        "run_id": run_id,
        "run_type": "forecast",
        "scenario_id": _scenario_for_source(SOURCE_ID),
        "source_id": SOURCE_ID,
        "cycle_time": _format_time(cycle_time),
        "start_time": start_time,
        "end_time": end_time,
        "model": {
            "model_id": MODEL_ID,
            "basin_version_id": model["basin_version_id"],
            "river_network_version_id": model["river_network_version_id"],
            "mesh_version_id": model["mesh_version_id"],
            "model_package_uri": model_package_uri,
            "project_name": PROJECT_NAME,
            "segment_count": segment_count,
            "segment_source": "shud_sp_riv",
        },
        "initial_state": {
            "state_id": "qhh_packaged_calibrated_state",
            "ic_file_uri": None,
            "valid_time": start_time,
            "checksum": None,
            "quality": "packaged_calibrated_state",
        },
        "forcing": {
            "forcing_version_id": forcing["forcing_version_id"],
            "forcing_uri": forcing["forcing_package_uri"],
            "package_manifest_uri": forcing_manifest_uri,
            "package_manifest_checksum": forcing_manifest_checksum,
            "files": forcing_files,
            "station_count": station_count,
            "station_source": "qhh.tsd.forc",
            "shud_forcing_layout": "standard_multi_station",
        },
        "runtime": {
            "command_style": "shud_project",
            "output_interval_minutes": OUTPUT_INTERVAL_MINUTES,
            "init_mode": 3,
            "threads": THREADS,
        },
        "outputs": {
            "output_uri": _directory_uri(object_store, f"runs/{run_id}/output"),
            "log_uri": _directory_uri(object_store, f"runs/{run_id}/logs"),
            "run_manifest_uri": object_store.uri_for_key(f"runs/{run_id}/input/manifest.json"),
        },
    }

    manifest_path = RUN_ROOT / "runs" / run_id / "input" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
    manifest_path.write_text(manifest_json + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"status": "manifest_ready", "run_id": run_id, "manifest_path": str(manifest_path)},
            sort_keys=True,
        )
    )
    return 0


def _one(row: Any, message: str) -> dict[str, Any]:
    if row is None:
        raise RuntimeError(message)
    return dict(row)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _first_int(path: Path) -> int:
    return int(path.read_text(encoding="utf-8").split()[0])


def _model_package_uri(model: dict[str, Any], object_store: LocalObjectStore) -> str:
    configured_uri = str(model.get("model_package_uri") or "").strip()
    if configured_uri:
        object_store.normalize_key(configured_uri)
        return _ensure_directory_uri(configured_uri)
    return _directory_uri(object_store, f"models/{MODEL_ID}/{PACKAGE_VERSION}/package")


def _validate_model_package_uri_matches_published(model_package_uri: str, expected_package_uri: str) -> None:
    if _ensure_directory_uri(model_package_uri) != _ensure_directory_uri(expected_package_uri):
        raise RuntimeError(
            "model_instance model_package_uri does not match the published QHH package version: "
            f"{model_package_uri} != {expected_package_uri}. Import the just-published registry package before "
            "creating the runtime manifest."
        )


def _published_package_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        raise RuntimeError(f"published QHH package manifest is not readable: {path}") from error
    if not isinstance(manifest, dict):
        raise RuntimeError(f"published QHH package manifest must be a JSON object: {path}")
    return manifest


def _validate_model_package_checksum_matches_published(
    model: dict[str, Any],
    published_manifest: dict[str, Any],
) -> None:
    published_checksum = str(published_manifest.get("package_checksum") or "")
    if not published_checksum:
        raise RuntimeError("published QHH package manifest is missing package_checksum.")

    resource_profile = _model_resource_profile(model)
    model_checksum = str(resource_profile.get("package_checksum") or "")
    if not model_checksum:
        raise RuntimeError(
            "model_instance resource_profile is missing package_checksum. Import the just-published registry package "
            "before creating the runtime manifest."
        )
    if model_checksum != published_checksum:
        raise RuntimeError(
            "model_instance resource_profile package_checksum does not match the published QHH package manifest: "
            f"{model_checksum} != {published_checksum}. Import the just-published registry package before creating "
            "the runtime manifest."
        )


def _model_resource_profile(model: dict[str, Any]) -> dict[str, Any]:
    profile = model.get("resource_profile") or {}
    if isinstance(profile, str):
        try:
            profile = json.loads(profile)
        except json.JSONDecodeError:
            return {}
    if isinstance(profile, dict):
        return dict(profile)
    return {}


def _forcing_package_manifest_uri(forcing: dict[str, Any]) -> str:
    package_uri = str(forcing.get("forcing_package_uri") or "")
    return _ensure_directory_uri(package_uri) + "forcing_package.json"


def _validate_forcing_package_checksum_matches_db(
    forcing: dict[str, Any],
    manifest_uri: str,
    object_store: LocalObjectStore,
) -> str:
    db_checksum = str(forcing.get("checksum") or "").strip()
    if not db_checksum or db_checksum.lower() == "pending":
        raise RuntimeError(
            "forcing_version checksum is missing or pending. Finalize forcing before creating the runtime manifest."
        )
    try:
        actual_checksum = object_store.checksum(manifest_uri)
    except Exception as error:
        raise RuntimeError(f"forcing package manifest checksum is not readable: {manifest_uri}") from error
    if actual_checksum != db_checksum:
        raise RuntimeError(
            "forcing_version checksum does not match forcing package manifest checksum: "
            f"{db_checksum} != {actual_checksum}."
        )
    return actual_checksum


def _forcing_package_manifest(manifest_uri: str, object_store: LocalObjectStore) -> dict[str, Any]:
    try:
        return json.loads(object_store.read_bytes(manifest_uri).decode("utf-8"))
    except Exception as error:
        raise RuntimeError(f"forcing package manifest is not readable: {manifest_uri}") from error


def _forcing_file_checksums(manifest: dict[str, Any]) -> list[dict[str, str]]:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise RuntimeError("forcing package manifest is missing files.")
    selected: list[dict[str, str]] = []
    for file_entry in files:
        if not isinstance(file_entry, dict):
            continue
        uri = str(file_entry.get("uri") or "")
        checksum = str(file_entry.get("checksum") or "")
        if not uri or not checksum:
            raise RuntimeError("forcing package manifest file entry is missing uri or checksum.")
        selected.append(
            {
                "role": str(file_entry.get("role") or ""),
                "relative_path": str(file_entry.get("relative_path") or ""),
                "uri": uri,
                "checksum": checksum,
            }
        )
    if not selected:
        raise RuntimeError("forcing package manifest has no checksum-bearing files.")
    return selected


def _forcing_manifest_station_count(manifest: dict[str, Any]) -> int:
    station_count = int(manifest.get("station_count") or 0)
    lineage = manifest.get("lineage")
    if not isinstance(lineage, dict):
        raise RuntimeError("forcing package manifest is missing lineage.")
    station_signature = lineage.get("station_signature")
    if not isinstance(station_signature, dict):
        raise RuntimeError("forcing package manifest is missing station_signature.")
    signature_count = int(station_signature.get("station_count") or 0)
    if station_count <= 0 or station_count != signature_count:
        raise RuntimeError("forcing package station_count does not match station_signature.")
    return station_count


def _validate_shud_forcing_header(
    manifest: dict[str, Any],
    object_store: LocalObjectStore,
    station_count: int,
) -> None:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise RuntimeError("forcing package manifest is missing files.")
    tsd_uri = ""
    for file_entry in files:
        if isinstance(file_entry, dict) and file_entry.get("relative_path") == "shud/qhh.tsd.forc":
            tsd_uri = str(file_entry.get("uri") or "")
            break
    if not tsd_uri:
        raise RuntimeError("forcing package manifest is missing shud/qhh.tsd.forc.")
    first_line = object_store.read_bytes(tsd_uri).decode("utf-8").splitlines()[0]
    header_count = int(first_line.split()[0])
    if header_count != station_count:
        raise RuntimeError(
            f"qhh.tsd.forc station header {header_count} does not match forcing manifest {station_count}."
        )


def _directory_uri(object_store: LocalObjectStore, key: str) -> str:
    return _ensure_directory_uri(object_store.uri_for_key(key))


def _ensure_directory_uri(uri: str) -> str:
    return uri.rstrip("/") + "/"


def _scenario_for_source(source_id: str) -> str:
    if source_id == "gfs":
        return "forecast_gfs_deterministic"
    if source_id == "IFS":
        return "forecast_ifs_deterministic"
    return f"forecast_{source_id.lower()}_deterministic"


if __name__ == "__main__":
    raise SystemExit(main())
