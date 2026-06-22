#!/usr/bin/env python3
"""Mirror one run's forcing domain from an explicit transitional node-22 source.

This is a compatibility-only bridge for historical deployments where an
operator intentionally provides a node-22 forcing-domain DSN. Current NHMS
production state does not use node-22 as an active database source: the
node-22 local PostgreSQL instance on ``:55433`` is historical, do-not-connect
for current production, and pending removal. New production readiness work
should prefer contracted object-store forcing-domain handoff packages.

Per ``--run-id`` (reads the object-store manifest for identity), idempotently:

  (a) UPSERT ``met.forcing_version`` checksum + station_count from node-22
      (the manifest top-level mislabels station_count as 0 /
      ``station_forcing_unavailable``; node-22 holds the real 386).
  (b) Replace ``met.forcing_station_timeseries`` for this forcing_version with
      node-22's rows (per-cycle; row count varies by horizon).
  (c) Ensure ``met.interp_weight`` exists for this run's (model_id, source);
      it is static per model+source, so it is mirrored once and reused.

node-22 DSN resolution order: ``--node22-url`` -> env ``N22_DSN``. This
transitional compatibility mirror never reads display runtime configuration
(``infra/env/display.env`` or display ``DATABASE_URL``) as the node-22 source.
Local DSN: ``--database-url`` -> env ``DATABASE_URL``.

Exit / return contract: returns a report dict. If node-22 has no forcing_version
for this run (object-store has the run but node-22 never registered it), raises
``ForcingNotOnNode22`` so a batch driver can record a skip and continue.

Idempotent: every write is delete+insert or upsert. Safe to re-run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import Json, RealDictCursor, execute_values

from packages.common.redaction import redact_payload

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_DEFAULT = "postgresql://nhms:nhms_dev@127.0.0.1:55432/nhms"
NODE22_DSN_MISSING_REASON = "NODE22_TRANSITIONAL_MIRROR_DSN_MISSING"
NODE22_MIRROR_FAILED_REASON = "NODE22_TRANSITIONAL_MIRROR_FAILED"
TRANSITIONAL_MIRROR_MODE = "transitional_node22_forcing_mirror"
TRANSITIONAL_MIRROR_PURPOSE = "compatibility_only"
TRANSITIONAL_MIRROR_SUNSET = (
    "Remove this mirror after object-store forcing-domain handoff packages and "
    "the node-27 DB apply path satisfy display readiness without node-22 DB access."
)
HISTORICAL_NODE22_PG_STATUS = "historical_do_not_connect_pending_removal"

FST_COLUMNS = (
    "forcing_version_id",
    "basin_version_id",
    "station_id",
    "valid_time",
    "source_id",
    "variable",
    "value",
    "unit",
    "native_resolution",
    "quality_flag",
)
IW_COLUMNS = (
    "source_id",
    "grid_id",
    "model_id",
    "station_id",
    "variable",
    "grid_cell_id",
    "weight",
    "method",
    "grid_signature",
)


class ForcingNotOnNode22(RuntimeError):
    """node-22 has no forcing_version for this run; caller should skip + record."""


class Node22MirrorDsnMissing(RuntimeError):
    """The transitional mirror was requested without an explicit node-22 DSN."""


@dataclass(frozen=True)
class Node22MirrorSource:
    url: str
    source: str


def _non_empty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _resolve_node22_source(cli_value: str | None) -> Node22MirrorSource:
    cli_dsn = _non_empty(cli_value)
    if cli_dsn:
        return Node22MirrorSource(url=cli_dsn, source="cli:--node22-url")
    env_dsn = _non_empty(os.environ.get("N22_DSN"))
    if env_dsn:
        return Node22MirrorSource(url=env_dsn, source="env:N22_DSN")
    raise Node22MirrorDsnMissing("Explicit transitional node-22 mirror DSN is required.")


def _resolve_node22_url(cli_value: str | None) -> str:
    return _resolve_node22_source(cli_value).url


def _mirror_boundary_evidence(dsn_source: str | None) -> dict[str, Any]:
    return {
        "mode": TRANSITIONAL_MIRROR_MODE,
        "purpose": TRANSITIONAL_MIRROR_PURPOSE,
        "compatibility_only": True,
        "dsn": {
            "source": dsn_source,
            "printed": False,
            "dsn_redacted": True,
        },
        "forbidden_sources": ["infra/env/display.env", "display runtime DATABASE_URL"],
        "current_topology": {
            "node22_role": "compute_and_artifact_producer_only",
            "node22_local_postgres": {
                "port": ":55433",
                "status": HISTORICAL_NODE22_PG_STATUS,
                "implicit_source_allowed": False,
            },
            "node27_role": "active_db_ingest_display_host",
        },
        "source_boundary": {
            "role": "node-22 forcing-domain source",
            "access": "read_only",
            "tables": [
                "met.forcing_version",
                "met.met_station",
                "met.forcing_station_timeseries",
                "met.interp_weight",
            ],
        },
        "destination_boundary": {
            "role": "node-27 local data-plane",
            "writes": [
                "met.forcing_version",
                "met.met_station",
                "met.forcing_station_timeseries",
                "met.interp_weight",
            ],
        },
        "sunset_condition": TRANSITIONAL_MIRROR_SUNSET,
    }


def _with_mirror_boundary(report: dict[str, Any], *, dsn_source: str | None) -> dict[str, Any]:
    return {**report, "mirror_boundary": _mirror_boundary_evidence(dsn_source)}


def _missing_node22_dsn_report(run_id: str) -> dict[str, Any]:
    return _with_mirror_boundary(
        {
            "run_id": run_id,
            "skipped": True,
            "reason": NODE22_DSN_MISSING_REASON,
            "detail": "Pass --node22-url or set N22_DSN for the transitional node-22 forcing mirror.",
        },
        dsn_source=None,
    )


def _failed_node22_mirror_report(run_id: str, error: Exception, *, dsn_source: str) -> dict[str, Any]:
    return _with_mirror_boundary(
        {
            "run_id": run_id,
            "failed": True,
            "reason": NODE22_MIRROR_FAILED_REASON,
            "detail": str(error),
            "error_type": type(error).__name__,
        },
        dsn_source=dsn_source,
    )


def _dump_json(payload: dict[str, Any]) -> None:
    json.dump(redact_payload(payload), sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _manifest_identity(object_store_root: Path, run_id: str) -> dict[str, Any]:
    path = object_store_root / "runs" / run_id / "input" / "manifest.json"
    if not path.is_file():
        raise FileNotFoundError(f"manifest not found: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    identity = manifest.get("identity") or {}
    forcing = manifest.get("forcing") or {}
    forcing_version_id = forcing.get("forcing_version_id") or identity.get("forcing_version_id")
    model_id = identity.get("model_id") or (manifest.get("model") or {}).get("model_id")
    source_id = manifest.get("source_id") or identity.get("source_id") or identity.get("source")
    basin_version_id = identity.get("basin_version_id") or (manifest.get("model") or {}).get("basin_version_id")
    if not (forcing_version_id and model_id and source_id and basin_version_id):
        raise ValueError(f"manifest missing forcing identity for {run_id}")
    return {
        "forcing_version_id": str(forcing_version_id),
        "model_id": str(model_id),
        "source_id": str(source_id),
        "basin_version_id": str(basin_version_id),
    }


def _mirror_forcing_version(n22: Any, local: Any, forcing_version_id: str) -> dict[str, Any]:
    with n22.cursor(cursor_factory=RealDictCursor) as ncur:
        ncur.execute(
            "SELECT checksum, station_count FROM met.forcing_version WHERE forcing_version_id = %s",
            (forcing_version_id,),
        )
        src = ncur.fetchone()
    if src is None:
        raise ForcingNotOnNode22(forcing_version_id)
    with local.cursor() as lcur:
        lcur.execute(
            """
            UPDATE met.forcing_version
            SET checksum = %s, station_count = %s
            WHERE forcing_version_id = %s
            """,
            (src["checksum"], src["station_count"], forcing_version_id),
        )
        updated = lcur.rowcount
    return {
        "checksum_set": src["checksum"] is not None,
        "station_count": src["station_count"],
        "forcing_version_rows_updated": updated,
    }


def _mirror_met_stations(n22: Any, local: Any, basin_version_id: str) -> dict[str, Any]:
    """Mirror this basin's ``met.met_station`` rows from node-22 into the local DB.

    ``met.forcing_station_timeseries.station_id`` FK-references ``met.met_station``.
    The generic basins registry import seeds geometry/model rows but NOT the
    forcing-grid stations (that was a qhh-bootstrap-specific extra), so without
    this the timeseries insert FK-fails. Stations are static per basin_version,
    so this is mirrored once and upserted idempotently. node-22 is the source of
    truth; geom is moved as EWKB to preserve SRID. Short-circuits when the local
    row count already matches node-22 (stations don't change cycle-to-cycle, so
    re-running across a basin's 100+ runs skips the per-row upsert after run 1)."""
    with n22.cursor() as ncur:
        ncur.execute(
            "SELECT count(*) FROM met.met_station WHERE basin_version_id = %s",
            (basin_version_id,),
        )
        n22_count = ncur.fetchone()[0]
    with local.cursor() as lcur:
        lcur.execute(
            "SELECT count(*) FROM met.met_station WHERE basin_version_id = %s",
            (basin_version_id,),
        )
        local_count = lcur.fetchone()[0]
    if n22_count > 0 and local_count >= n22_count:
        return {"action": "present", "pulled_rows": 0, "local_rows": local_count}

    cols = (
        "station_id",
        "basin_version_id",
        "station_name",
        "elevation_m",
        "station_role",
        "active_flag",
        "properties_json",
    )
    with n22.cursor(cursor_factory=RealDictCursor) as ncur:
        ncur.execute(
            f"SELECT {', '.join(cols)}, ST_AsEWKB(geom) AS geom_ewkb "
            "FROM met.met_station WHERE basin_version_id = %s",
            (basin_version_id,),
        )
        rows = ncur.fetchall()
    insert_cols = (*cols, "geom")
    template = "(" + ", ".join(["%s"] * len(cols)) + ", ST_GeomFromEWKB(%s))"
    tuples = [
        (
            r["station_id"],
            r["basin_version_id"],
            r["station_name"],
            r["elevation_m"],
            r["station_role"],
            r["active_flag"],
            Json(r["properties_json"]) if r["properties_json"] is not None else None,
            bytes(r["geom_ewkb"]) if r["geom_ewkb"] is not None else None,
        )
        for r in rows
    ]
    with local.cursor() as lcur:
        if tuples:
            execute_values(
                lcur,
                f"""
                INSERT INTO met.met_station ({", ".join(insert_cols)})
                VALUES %s
                ON CONFLICT (station_id) DO UPDATE SET
                    basin_version_id = EXCLUDED.basin_version_id,
                    station_name = EXCLUDED.station_name,
                    elevation_m = EXCLUDED.elevation_m,
                    station_role = EXCLUDED.station_role,
                    active_flag = EXCLUDED.active_flag,
                    properties_json = EXCLUDED.properties_json,
                    geom = EXCLUDED.geom
                """,
                tuples,
                template=template,
                page_size=5000,
            )
        lcur.execute(
            "SELECT count(*) FROM met.met_station WHERE basin_version_id = %s",
            (basin_version_id,),
        )
        local_count = lcur.fetchone()[0]
    return {"action": "mirrored", "pulled_rows": len(tuples), "local_rows": local_count}


def _mirror_station_timeseries(n22: Any, local: Any, forcing_version_id: str) -> dict[str, Any]:
    cols = ", ".join(FST_COLUMNS)
    with n22.cursor(cursor_factory=RealDictCursor) as ncur:
        ncur.execute(
            f"SELECT {cols} FROM met.forcing_station_timeseries WHERE forcing_version_id = %s",
            (forcing_version_id,),
        )
        rows = ncur.fetchall()
    tuples = [tuple(r[c] for c in FST_COLUMNS) for r in rows]
    with local.cursor() as lcur:
        lcur.execute(
            "DELETE FROM met.forcing_station_timeseries WHERE forcing_version_id = %s",
            (forcing_version_id,),
        )
        if tuples:
            execute_values(
                lcur,
                f"INSERT INTO met.forcing_station_timeseries ({cols}) VALUES %s",
                tuples,
                page_size=5000,
            )
        lcur.execute(
            """
            SELECT count(*) AS rows, count(DISTINCT station_id) AS stations,
                   count(DISTINCT variable) AS variables
            FROM met.forcing_station_timeseries WHERE forcing_version_id = %s
            """,
            (forcing_version_id,),
        )
        verify = lcur.fetchone()
    return {
        "pulled_rows": len(tuples),
        "local_rows": verify[0],
        "local_stations": verify[1],
        "local_variables": verify[2],
    }


def _ensure_interp_weight(n22: Any, local: Any, model_id: str, source_id: str) -> dict[str, Any]:
    with local.cursor() as lcur:
        lcur.execute(
            """
            SELECT count(*) FROM met.interp_weight
            WHERE model_id = %s AND LOWER(source_id) = LOWER(%s)
            """,
            (model_id, source_id),
        )
        local_count = lcur.fetchone()[0]
    if local_count > 0:
        return {"action": "present", "local_rows": local_count}

    cols = ", ".join(IW_COLUMNS)
    with n22.cursor(cursor_factory=RealDictCursor) as ncur:
        ncur.execute(
            f"""
            SELECT {cols} FROM met.interp_weight
            WHERE model_id = %s AND LOWER(source_id) = LOWER(%s)
            """,
            (model_id, source_id),
        )
        rows = ncur.fetchall()
    tuples = [tuple(r[c] for c in IW_COLUMNS) for r in rows]
    with local.cursor() as lcur:
        if tuples:
            execute_values(
                lcur,
                f"""
                INSERT INTO met.interp_weight ({cols}) VALUES %s
                ON CONFLICT (source_id, grid_id, model_id, station_id, variable, grid_cell_id)
                DO NOTHING
                """,
                tuples,
                page_size=5000,
            )
        lcur.execute(
            """
            SELECT count(*) FROM met.interp_weight
            WHERE model_id = %s AND LOWER(source_id) = LOWER(%s)
            """,
            (model_id, source_id),
        )
        local_count = lcur.fetchone()[0]
    return {"action": "mirrored", "pulled_rows": len(tuples), "local_rows": local_count}


def mirror_forcing(
    *,
    run_id: str,
    object_store_root: Path,
    local_url: str,
    node22_url: str,
    node22_dsn_source: str = "explicit",
) -> dict[str, Any]:
    identity = _manifest_identity(object_store_root, run_id)
    n22 = psycopg2.connect(node22_url, connect_timeout=15)
    local = psycopg2.connect(local_url)
    try:
        # node-22 is read-only; keep its txn read-only and never commit writes there.
        n22.set_session(readonly=True, autocommit=True)
        forcing_version = _mirror_forcing_version(n22, local, identity["forcing_version_id"])
        met_stations = _mirror_met_stations(n22, local, identity["basin_version_id"])
        station_ts = _mirror_station_timeseries(n22, local, identity["forcing_version_id"])
        interp_weight = _ensure_interp_weight(n22, local, identity["model_id"], identity["source_id"])
        local.commit()
    except Exception:
        local.rollback()
        raise
    finally:
        n22.close()
        local.close()
    return _with_mirror_boundary(
        {
            "run_id": run_id,
            "forcing_version_id": identity["forcing_version_id"],
            "model_id": identity["model_id"],
            "source_id": identity["source_id"],
            "basin_version_id": identity["basin_version_id"],
            "forcing_version": forcing_version,
            "met_stations": met_stations,
            "station_timeseries": station_ts,
            "interp_weight": interp_weight,
        },
        dsn_source=node22_dsn_source,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mirror one run's forcing domain from node-22 to local DB.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--object-store-root",
        default=os.environ.get("OBJECT_STORE_ROOT"),
        help="Object-store filesystem root. Defaults to OBJECT_STORE_ROOT.",
    )
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL") or LOCAL_DEFAULT)
    parser.add_argument(
        "--node22-url",
        default=None,
        help="Explicit node-22 read-only DSN for the transitional mirror; else N22_DSN.",
    )
    args = parser.parse_args(argv)

    try:
        node22_source = _resolve_node22_source(args.node22_url)
    except Node22MirrorDsnMissing:
        _dump_json(_missing_node22_dsn_report(args.run_id))
        return 2

    if not args.object_store_root:
        parser.error("OBJECT_STORE_ROOT or --object-store-root is required.")

    try:
        report = mirror_forcing(
            run_id=args.run_id,
            object_store_root=Path(args.object_store_root),
            local_url=args.database_url,
            node22_url=node22_source.url,
            node22_dsn_source=node22_source.source,
        )
    except ForcingNotOnNode22 as exc:
        _dump_json(
            _with_mirror_boundary(
                {"run_id": args.run_id, "skipped": True, "reason": "FORCING_NOT_ON_NODE22", "detail": str(exc)},
                dsn_source=node22_source.source,
            )
        )
        return 2
    except Exception as exc:
        _dump_json(_failed_node22_mirror_report(args.run_id, exc, dsn_source=node22_source.source))
        return 1

    _dump_json(_with_mirror_boundary(report, dsn_source=node22_source.source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
