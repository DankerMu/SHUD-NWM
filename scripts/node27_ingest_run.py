#!/usr/bin/env python3
"""Register one object-store run into the local DB so display can serve it.

Reads ``<object_store_root>/runs/<run_id>/input/manifest.json`` and upserts the
minimal rows the output parser + display latest-product path require:

1. ``met.data_source`` for the run's source (e.g. ``gfs``); the registry
   bootstrap does not seed this, but ``hydro.hydro_run`` / ``met.forcing_version``
   both FK to it.
2. ``met.forcing_version`` from the manifest ``forcing`` / ``identity`` blocks.
3. ``hydro.hydro_run`` (status ``created``) with ``output_uri`` pointing at the
   object-store output prefix so ``workers.output_parser`` can resolve the CSVs.

The core registry rows (basin/model/river_segment) must already be seeded via
``nhms-model bootstrap-qhh-production`` (or the discover/publish/import path).

Idempotent: every write is an upsert. Safe to re-run.

Example::

    OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store \\
    OBJECT_STORE_PREFIX=s3://nhms \\
    DATABASE_URL=postgresql://nhms:nhms_dev@127.0.0.1:55432/nhms \\
    python scripts/node27_ingest_run.py --run-id fcst_gfs_2026061118_basins_qhh_shud
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import Json, RealDictCursor


def _manifest_path(object_store_root: Path, run_id: str) -> Path:
    return object_store_root / "runs" / run_id / "input" / "manifest.json"


def _load_manifest(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _require(value: Any, field: str) -> Any:
    if value in (None, ""):
        raise ValueError(f"manifest missing required field: {field}")
    return value


def _source_id(manifest: dict[str, Any]) -> str:
    identity = manifest.get("identity") or {}
    value = manifest.get("source_id") or identity.get("source_id") or identity.get("source")
    return str(_require(value, "source_id"))


def upsert_data_source(cursor: Any, source_id: str) -> dict[str, Any]:
    cursor.execute(
        """
        INSERT INTO met.data_source (
            source_id, source_name, source_type, status, native_format,
            license_status, adapter_name, config_json
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_id) DO UPDATE SET
            source_name = EXCLUDED.source_name,
            source_type = EXCLUDED.source_type,
            status = EXCLUDED.status,
            adapter_name = EXCLUDED.adapter_name
        RETURNING source_id, status::text AS status
        """,
        (
            source_id,
            f"{source_id.upper()} (node-27 ingest)",
            "global_forecast",
            "enabled",
            "GRIB2",
            None,
            f"{source_id}_adapter",
            Json({"seed": "node27_ingest_run"}),
        ),
    )
    return dict(cursor.fetchone())


def upsert_forcing_version(cursor: Any, manifest: dict[str, Any], source_id: str) -> dict[str, Any]:
    identity = manifest.get("identity") or {}
    forcing = manifest.get("forcing") or {}
    forcing_version_id = str(
        _require(forcing.get("forcing_version_id") or identity.get("forcing_version_id"), "forcing_version_id")
    )
    model_id = str(_require(identity.get("model_id") or (manifest.get("model") or {}).get("model_id"), "model_id"))
    cycle_time = _require(manifest.get("cycle_time") or identity.get("cycle_time"), "cycle_time")
    start_time = _require(manifest.get("start_time") or identity.get("start_time"), "start_time")
    end_time = _require(manifest.get("end_time") or identity.get("end_time"), "end_time")
    package_uri = str(
        _require(
            forcing.get("forcing_package_uri") or forcing.get("forcing_uri"),
            "forcing_package_uri",
        )
    )
    # Manifest mislabels station_count as 0 (station_forcing_unavailable); it is
    # only a first-insert placeholder. node27_mirror_forcing is authoritative and
    # writes the real count from node-22, so the ON CONFLICT below intentionally
    # does NOT update station_count (re-register must not clobber the mirrored value).
    station_count = int(forcing.get("station_count") or 0)
    lineage = {
        "seed": "node27_ingest_run",
        "run_id": str(identity.get("run_id") or manifest.get("run_id")),
        "quality_flag": forcing.get("quality_flag"),
    }
    cursor.execute(
        """
        INSERT INTO met.forcing_version (
            forcing_version_id, model_id, source_id, cycle_time, start_time,
            end_time, station_count, forcing_package_uri, checksum, lineage_json
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (forcing_version_id) DO UPDATE SET
            model_id = EXCLUDED.model_id,
            source_id = EXCLUDED.source_id,
            cycle_time = EXCLUDED.cycle_time,
            start_time = EXCLUDED.start_time,
            end_time = EXCLUDED.end_time,
            forcing_package_uri = EXCLUDED.forcing_package_uri,
            lineage_json = EXCLUDED.lineage_json
        RETURNING forcing_version_id, station_count
        """,
        (
            forcing_version_id,
            model_id,
            source_id,
            cycle_time,
            start_time,
            end_time,
            station_count,
            package_uri,
            None,
            Json(lineage),
        ),
    )
    return dict(cursor.fetchone())


def upsert_hydro_run(cursor: Any, manifest: dict[str, Any], source_id: str) -> dict[str, Any]:
    identity = manifest.get("identity") or {}
    model = manifest.get("model") or {}
    forcing = manifest.get("forcing") or {}
    outputs = manifest.get("outputs") or {}
    initial_state = manifest.get("initial_state") or {}

    run_id = str(_require(identity.get("run_id") or manifest.get("run_id"), "run_id"))
    run_type = str(manifest.get("run_type", "forecast"))
    scenario_id = str(_require(manifest.get("scenario_id") or identity.get("scenario_id"), "scenario_id"))
    model_id = str(_require(identity.get("model_id") or model.get("model_id"), "model_id"))
    basin_version_id = str(
        _require(identity.get("basin_version_id") or model.get("basin_version_id"), "basin_version_id")
    )
    forcing_version_id = str(
        _require(forcing.get("forcing_version_id") or identity.get("forcing_version_id"), "forcing_version_id")
    )
    init_state_id = initial_state.get("state_id")
    cycle_time = _require(manifest.get("cycle_time") or identity.get("cycle_time"), "cycle_time")
    start_time = _require(manifest.get("start_time") or identity.get("start_time"), "start_time")
    end_time = _require(manifest.get("end_time") or identity.get("end_time"), "end_time")
    run_manifest_uri = str(
        _require(outputs.get("run_manifest_uri") or f"s3://nhms/runs/{run_id}/input/manifest.json", "run_manifest_uri")
    )
    output_uri = str(_require(outputs.get("output_uri") or f"s3://nhms/runs/{run_id}/output/", "output_uri"))
    log_uri = outputs.get("log_uri") or f"s3://nhms/runs/{run_id}/logs/"

    # Seed at 'succeeded' (object-store already holds SHUD output) so the output
    # parser's mark_run_parsed (WHERE status IN succeeded/parsed/failed) can
    # advance it to 'parsed'. ON CONFLICT intentionally leaves status untouched
    # so re-running this registrar never regresses a run that is already parsed.
    cursor.execute(
        """
        INSERT INTO hydro.hydro_run (
            run_id, run_type, scenario_id, model_id, basin_version_id,
            forcing_version_id, init_state_id, source_id, cycle_time,
            start_time, end_time, status, run_manifest_uri, output_uri, log_uri
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'succeeded', %s, %s, %s)
        ON CONFLICT (run_id) DO UPDATE SET
            scenario_id = EXCLUDED.scenario_id,
            model_id = EXCLUDED.model_id,
            basin_version_id = EXCLUDED.basin_version_id,
            forcing_version_id = EXCLUDED.forcing_version_id,
            init_state_id = EXCLUDED.init_state_id,
            source_id = EXCLUDED.source_id,
            cycle_time = EXCLUDED.cycle_time,
            start_time = EXCLUDED.start_time,
            end_time = EXCLUDED.end_time,
            run_manifest_uri = EXCLUDED.run_manifest_uri,
            output_uri = EXCLUDED.output_uri,
            log_uri = EXCLUDED.log_uri,
            updated_at = now()
        RETURNING run_id, status::text AS status, output_uri
        """,
        (
            run_id,
            run_type,
            scenario_id,
            model_id,
            basin_version_id,
            forcing_version_id,
            init_state_id,
            source_id,
            cycle_time,
            start_time,
            end_time,
            run_manifest_uri,
            output_uri,
            log_uri,
        ),
    )
    return dict(cursor.fetchone())


def ingest_run(database_url: str, object_store_root: Path, run_id: str) -> dict[str, Any]:
    manifest_path = _manifest_path(object_store_root, run_id)
    if not manifest_path.is_file():
        raise FileNotFoundError(f"manifest not found: {manifest_path}")
    manifest = _load_manifest(manifest_path)
    source_id = _source_id(manifest)

    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    try:
        with connection:
            with connection.cursor() as cursor:
                data_source = upsert_data_source(cursor, source_id)
                forcing_version = upsert_forcing_version(cursor, manifest, source_id)
                hydro_run = upsert_hydro_run(cursor, manifest, source_id)
    finally:
        connection.close()

    return {
        "run_id": run_id,
        "manifest_path": str(manifest_path),
        "data_source": data_source,
        "forcing_version": forcing_version,
        "hydro_run": hydro_run,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register one object-store run into the local DB.")
    parser.add_argument("--run-id", required=True, help="Run id to register (object-store runs/<run_id>).")
    parser.add_argument(
        "--object-store-root",
        default=os.environ.get("OBJECT_STORE_ROOT"),
        help="Object-store filesystem root. Defaults to OBJECT_STORE_ROOT.",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="PostgreSQL URL. Defaults to DATABASE_URL.",
    )
    args = parser.parse_args(argv)

    if not args.database_url:
        parser.error("DATABASE_URL or --database-url is required.")
    if not args.object_store_root:
        parser.error("OBJECT_STORE_ROOT or --object-store-root is required.")

    report = ingest_run(args.database_url, Path(args.object_store_root), args.run_id)
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
