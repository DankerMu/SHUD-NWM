#!/usr/bin/env python3
"""Mirror one run's forcing domain from an explicit-DSN, allow-flagged,
sunset-bound, compatibility-only archived rollback node-22 source.

This is a compatibility-only rollback bridge for historical deployments where
an operator intentionally restarts the archived node-22 PostgreSQL rollback
container and provides its forcing-domain DSN. Current NHMS production state
does not use node-22 as an active database source: the node-22 local
PostgreSQL instance on ``:55433`` is historical, do-not-connect for current
production, archived, and stopped. New production readiness work should prefer
contracted object-store forcing-domain handoff packages.

Per ``--run-id`` (reads the object-store manifest for identity), idempotently:

  (a) UPSERT ``met.forcing_version`` checksum + station_count from node-22
      (the manifest top-level mislabels station_count as 0 /
      ``station_forcing_unavailable``; node-22 holds the real 386).
  (b) Replace ``met.forcing_station_timeseries`` for this forcing_version with
      node-22's rows (per-cycle; row count varies by horizon).
  (c) Ensure ``met.interp_weight`` exists for this run's (model_id, source);
      it is static per model+source, so it is mirrored once and reused.

node-22 DSN resolution source: env ``N22_DSN`` only. Parent tools may resolve
the source DSN from ``N22_DSN`` or an owner-only file and pass it through the
child environment instead of argv. The DSN is ignored unless
``--allow-archived-node22-db-rollback-mirror`` or
``NHMS_ALLOW_ARCHIVED_NODE22_DB_ROLLBACK_MIRROR=true`` is also set. This archived
rollback mirror never reads display runtime configuration
(``infra/env/display.env`` or display ``DATABASE_URL``) as the node-22 source.
Local DSN: ``--database-url`` -> env ``DATABASE_URL``, and it must not point at
node-22 historical PostgreSQL.

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
from urllib.parse import parse_qsl, urlsplit

import psycopg2
from psycopg2.extras import Json, RealDictCursor, execute_values

from packages.common.redaction import redact_payload

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_DEFAULT = "postgresql://nhms:nhms_dev@127.0.0.1:55432/nhms"
NODE22_DSN_MISSING_REASON = "NODE22_TRANSITIONAL_MIRROR_DSN_MISSING"
NODE22_DSN_INVALID_REASON = "NODE22_TRANSITIONAL_MIRROR_DSN_INVALID"
NODE22_DSN_QUERY_OVERRIDE_FORBIDDEN_REASON = "NODE22_TRANSITIONAL_MIRROR_DSN_QUERY_OVERRIDE_FORBIDDEN"
NODE22_DSN_ENDPOINT_NOT_ARCHIVED_NODE22_REASON = "NODE22_TRANSITIONAL_MIRROR_DSN_ENDPOINT_NOT_ARCHIVED_NODE22"
NODE22_DSN_USERNAME_MISSING_REASON = "NODE22_TRANSITIONAL_MIRROR_DSN_USERNAME_MISSING"
NODE22_DSN_PASSWORD_MISSING_REASON = "NODE22_TRANSITIONAL_MIRROR_DSN_PASSWORD_MISSING"
NODE22_ROLLBACK_MIRROR_NOT_ALLOWED_REASON = "ARCHIVED_NODE22_DB_ROLLBACK_MIRROR_NOT_ALLOWED"
NODE22_MIRROR_FAILED_REASON = "NODE22_TRANSITIONAL_MIRROR_FAILED"
DATABASE_URL_NODE22_HISTORICAL_ENDPOINT_REASON = "DATABASE_URL_NODE22_HISTORICAL_ENDPOINT"
DATABASE_URL_QUERY_OVERRIDE_FORBIDDEN_REASON = "DATABASE_URL_QUERY_OVERRIDE_FORBIDDEN"
DATABASE_URL_ENDPOINT_NOT_NODE27_REASON = "DATABASE_URL_ENDPOINT_NOT_NODE27"
DATABASE_URL_INVALID_REASON = "DATABASE_URL_INVALID"
DATABASE_URL_USERNAME_MISSING_REASON = "DATABASE_URL_USERNAME_MISSING"
DATABASE_URL_READONLY_IDENTITY_REASON = "DATABASE_URL_READONLY_IDENTITY"
DATABASE_URL_PASSWORD_MISSING_REASON = "DATABASE_URL_PASSWORD_MISSING"
DEFAULT_ALLOWED_DB_ENDPOINTS = "127.0.0.1:55432,localhost:55432"
DATABASE_URL_ALLOWED_QUERY_KEYS = frozenset(
    {
        "application_name",
        "connect_timeout",
        "fallback_application_name",
        "sslmode",
    }
)
TRANSITIONAL_MIRROR_MODE = "archived_node22_rollback_forcing_mirror"
TRANSITIONAL_MIRROR_PURPOSE = "compatibility_only"
ARCHIVED_NODE22_DB_ROLLBACK_MIRROR_ENV = "NHMS_ALLOW_ARCHIVED_NODE22_DB_ROLLBACK_MIRROR"
TRANSITIONAL_MIRROR_SUNSET = (
    "Use only for an explicit archived rollback drill after intentionally "
    "restarting nhms-22-e2e-db; remove this mirror after object-store "
    "forcing-domain handoff packages and the node-27 DB apply path satisfy "
    "display readiness without node-22 DB access."
)
HISTORICAL_NODE22_PG_STATUS = "historical_do_not_connect_archived_stopped_rollback"
NODE22_HISTORICAL_DB_HOSTS = frozenset(
    {
        "210.77.77.22",
        "10.0.2.100",
        "node-22",
        "node22",
        "compute-control",
        "compute_control",
    }
)
NODE22_HISTORICAL_DB_PORT = 55433

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
    """Compatibility-only archived rollback mirror lacks explicit N22_DSN, allow flag, sunset/removal.

    The mirror is allow-flagged and sunset-bound after object-store
    forcing-domain handoff replaces old pre-contract run recovery.
    """


@dataclass(frozen=True)
class Node22MirrorSource:
    url: str
    source: str


def _non_empty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _node22_dsn_source_from_env() -> str:
    # Compatibility-only archived rollback mirror: explicit N22_DSN source,
    # allow-flagged activation, and sunset-bound after object-store handoff.
    source_hint = _non_empty(os.environ.get("NHMS_NODE22_DSN_SOURCE"))
    if source_hint and (source_hint == "env:N22_DSN" or source_hint.startswith("file:")):
        return source_hint
    return "env:N22_DSN"


def _resolve_node22_source() -> Node22MirrorSource:
    env_dsn = _non_empty(os.environ.get("N22_DSN"))
    if env_dsn:
        return Node22MirrorSource(url=env_dsn, source=_node22_dsn_source_from_env())
    # Compatibility-only archived rollback mirror: explicit DSN, allow flag, sunset/removal.
    raise Node22MirrorDsnMissing("Explicit archived node-22 rollback mirror DSN is required.")


def _database_port(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _database_username_class(username: str | None) -> str:
    normalized = (username or "").strip().lower()
    if not normalized:
        return "missing"
    if "display" in normalized or "readonly" in normalized or normalized.endswith("_ro") or normalized.endswith("ro"):
        return "display_readonly_like"
    return "writer_candidate"


def _dsn_query_blockers(
    query: str,
    *,
    env_var: str,
    code: str,
    message: str,
) -> list[dict[str, str]]:
    if not query:
        return []
    query_keys = {key.strip().lower() for key, _value in parse_qsl(query, keep_blank_values=True)}
    if query_keys and any(key not in DATABASE_URL_ALLOWED_QUERY_KEYS for key in query_keys):
        return [
            {
                "code": code,
                "env_var": env_var,
                "message": message,
            }
        ]
    return []


def _database_query_blockers(query: str) -> list[dict[str, str]]:
    return _dsn_query_blockers(
        query,
        env_var="DATABASE_URL",
        code=DATABASE_URL_QUERY_OVERRIDE_FORBIDDEN_REASON,
        message="DATABASE_URL query parameters must not override mirror destination or credential source.",
    )


def _node22_source_query_blockers(query: str) -> list[dict[str, str]]:
    return _dsn_query_blockers(
        query,
        env_var="N22_DSN",
        code=NODE22_DSN_QUERY_OVERRIDE_FORBIDDEN_REASON,
        message="N22_DSN query parameters must not override mirror source endpoint or credential source.",
    )


def _parse_allowed_database_endpoints(value: str | None) -> set[tuple[str, int]]:
    raw = (value or DEFAULT_ALLOWED_DB_ENDPOINTS).strip()
    endpoints: set[tuple[str, int]] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        host, port = item.rsplit(":", 1)
        try:
            endpoints.add((host.strip().lower(), int(port)))
        except ValueError:
            continue
    return endpoints


def _source_preflight(node22_url: str | None) -> tuple[dict[str, Any], list[dict[str, str]]]:
    raw = (node22_url or "").strip()
    if not raw:
        return {"configured": False}, [
            {
                "code": NODE22_DSN_MISSING_REASON,
                "env_var": "N22_DSN",
                "message": "N22_DSN is required for the archived node-22 rollback mirror source.",
            }
        ]
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return {"configured": True}, [
            {
                "code": NODE22_DSN_INVALID_REASON,
                "env_var": "N22_DSN",
                "message": "N22_DSN must be a valid PostgreSQL URL.",
            }
        ]
    query_blockers = _node22_source_query_blockers(parsed.query)
    try:
        dsn_parameters = psycopg2.extensions.parse_dsn(raw)
    except psycopg2.Error:
        if query_blockers:
            return {"configured": True, "scheme": parsed.scheme or None}, query_blockers
        return {"configured": True, "scheme": parsed.scheme or None}, [
            {
                "code": NODE22_DSN_INVALID_REASON,
                "env_var": "N22_DSN",
                "message": "N22_DSN must be a valid PostgreSQL URL.",
            }
        ]

    host = dsn_parameters.get("host")
    port = _database_port(dsn_parameters.get("port"))
    database = dsn_parameters.get("dbname")
    username = dsn_parameters.get("user")
    password_present = bool(dsn_parameters.get("password"))
    normalized_host = str(host or "").strip().lower()
    identity: dict[str, Any] = {
        "configured": True,
        "scheme": parsed.scheme,
        "host": host,
        "port": port,
        "database": database,
        "username_present": bool((username or "").strip()),
        "password_present": password_present,
    }
    blockers = list(query_blockers)
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or not host
        or not database
        or (dsn_parameters.get("port") and port is None)
    ):
        blockers.append(
            {
                "code": NODE22_DSN_INVALID_REASON,
                "env_var": "N22_DSN",
                "message": "N22_DSN must include PostgreSQL scheme, host, port, and database name.",
            }
        )
        return identity, blockers
    if database != "nhms" or port != NODE22_HISTORICAL_DB_PORT or normalized_host not in NODE22_HISTORICAL_DB_HOSTS:
        blockers.append(
            {
                "code": NODE22_DSN_ENDPOINT_NOT_ARCHIVED_NODE22_REASON,
                "env_var": "N22_DSN",
                "message": "N22_DSN must target the archived node-22 rollback PostgreSQL endpoint.",
            }
        )
    if not identity["username_present"]:
        blockers.append(
            {
                "code": NODE22_DSN_USERNAME_MISSING_REASON,
                "env_var": "N22_DSN",
                "message": "N22_DSN must include an explicit read-only source username.",
            }
        )
    if not password_present:
        blockers.append(
            {
                "code": NODE22_DSN_PASSWORD_MISSING_REASON,
                "env_var": "N22_DSN",
                "message": "N22_DSN must include explicit password material for the read-only source username.",
            }
        )
    return identity, blockers


def _destination_preflight(database_url: str | None) -> tuple[dict[str, Any], list[dict[str, str]]]:
    raw = (database_url or "").strip()
    if not raw:
        return {"configured": False}, [
            {
                "code": DATABASE_URL_INVALID_REASON,
                "env_var": "DATABASE_URL",
                "message": "DATABASE_URL is required for the node-27 mirror destination.",
            }
        ]
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return {"configured": True}, [
            {
                "code": DATABASE_URL_INVALID_REASON,
                "env_var": "DATABASE_URL",
                "message": "DATABASE_URL must be a valid PostgreSQL URL.",
            }
        ]
    query_blockers = _database_query_blockers(parsed.query)
    try:
        dsn_parameters = psycopg2.extensions.parse_dsn(raw)
    except psycopg2.Error:
        if query_blockers:
            return {"configured": True, "scheme": parsed.scheme or None}, query_blockers
        return {"configured": True, "scheme": parsed.scheme or None}, [
            {
                "code": DATABASE_URL_INVALID_REASON,
                "env_var": "DATABASE_URL",
                "message": "DATABASE_URL must be a valid PostgreSQL URL.",
            }
        ]

    host = dsn_parameters.get("host")
    port = _database_port(dsn_parameters.get("port"))
    database = dsn_parameters.get("dbname")
    username = dsn_parameters.get("user")
    username_class = _database_username_class(username)
    password_present = bool(dsn_parameters.get("password"))
    identity: dict[str, Any] = {
        "configured": True,
        "scheme": parsed.scheme,
        "host": host,
        "port": port,
        "database": database,
        "username_present": username_class != "missing",
        "username_class": username_class,
        "password_present": password_present,
    }
    blockers = list(query_blockers)
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or not host
        or not database
        or (dsn_parameters.get("port") and port is None)
    ):
        blockers.append(
            {
                "code": DATABASE_URL_INVALID_REASON,
                "env_var": "DATABASE_URL",
                "message": "DATABASE_URL must include PostgreSQL scheme, host, port, and database name.",
            }
        )
        return identity, blockers
    normalized_host = str(host).strip().lower()
    if normalized_host in NODE22_HISTORICAL_DB_HOSTS or port == NODE22_HISTORICAL_DB_PORT:
        blockers.append(
            {
                "code": DATABASE_URL_NODE22_HISTORICAL_ENDPOINT_REASON,
                "env_var": "DATABASE_URL",
                "message": "DATABASE_URL must point to the node-27 writer, not node-22 historical PostgreSQL.",
            }
        )
    allowed = _parse_allowed_database_endpoints(os.environ.get("NODE27_INGEST_ALLOWED_DATABASE_ENDPOINTS"))
    if database != "nhms" or port is None or (normalized_host, port) not in allowed:
        blockers.append(
            {
                "code": DATABASE_URL_ENDPOINT_NOT_NODE27_REASON,
                "env_var": "DATABASE_URL",
                "message": "DATABASE_URL must target an allowed node-27 PostgreSQL endpoint.",
            }
        )
    if identity["username_class"] == "missing":
        blockers.append(
            {
                "code": DATABASE_URL_USERNAME_MISSING_REASON,
                "env_var": "DATABASE_URL",
                "message": "DATABASE_URL must include an explicit node-27 ingest writer username.",
            }
        )
        return identity, blockers
    if identity["username_class"] == "display_readonly_like":
        blockers.append(
            {
                "code": DATABASE_URL_READONLY_IDENTITY_REASON,
                "env_var": "DATABASE_URL",
                "message": "DATABASE_URL appears to use a display/readonly identity, not a node-27 ingest writer.",
            }
        )
    if not password_present:
        blockers.append(
            {
                "code": DATABASE_URL_PASSWORD_MISSING_REASON,
                "env_var": "DATABASE_URL",
                "message": "DATABASE_URL must include explicit password material for the node-27 ingest writer.",
            }
        )
    return identity, blockers


def _truthy_env(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _archived_rollback_mirror_allowed(cli_allowed: bool) -> bool:
    return bool(cli_allowed or _truthy_env(os.environ.get(ARCHIVED_NODE22_DB_ROLLBACK_MIRROR_ENV)))


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
            "detail": (
                "Set N22_DSN for the compatibility-only, sunset/removal-bound, "
                "archived/stopped node-22 rollback forcing mirror, plus the explicit rollback "
                "allow flag. Parent tools must pass any resolved DSN through "
                "child environment or owner-only file indirection, not argv."
            ),
        },
        dsn_source=None,
    )


def _rollback_mirror_not_allowed_report(run_id: str, *, dsn_source: str) -> dict[str, Any]:
    return _with_mirror_boundary(
        {
            "run_id": run_id,
            "skipped": True,
            "reason": NODE22_ROLLBACK_MIRROR_NOT_ALLOWED_REASON,
            "detail": (
                "N22_DSN is configured, but compatibility-only, sunset-bound, "
                "archived node-22 DB rollback mirror use requires "
                f"--allow-archived-node22-db-rollback-mirror or "
                f"{ARCHIVED_NODE22_DB_ROLLBACK_MIRROR_ENV}=true."
            ),
        },
        dsn_source=dsn_source,
    )


def _destination_forbidden_report(
    run_id: str,
    *,
    dsn_source: str,
    destination: dict[str, Any],
    blockers: list[dict[str, str]],
) -> dict[str, Any]:
    reason = blockers[0]["code"] if blockers else DATABASE_URL_ENDPOINT_NOT_NODE27_REASON
    return _with_mirror_boundary(
        {
            "run_id": run_id,
            "failed": True,
            "reason": reason,
            "detail": "DATABASE_URL failed node-27 mirror destination preflight.",
            "destination": destination,
            "blockers": blockers,
        },
        dsn_source=dsn_source,
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


def _source_forbidden_report(
    run_id: str,
    *,
    dsn_source: str,
    source: dict[str, Any],
    blockers: list[dict[str, str]],
) -> dict[str, Any]:
    reason = blockers[0]["code"] if blockers else NODE22_DSN_ENDPOINT_NOT_ARCHIVED_NODE22_REASON
    return _with_mirror_boundary(
        {
            "run_id": run_id,
            "failed": True,
            "reason": reason,
            "detail": "N22_DSN failed archived node-22 rollback mirror source preflight.",
            "source": source,
            "blockers": blockers,
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
    """Mirror station rows in the explicit-DSN, allow-flagged rollback drill.

    ``met.forcing_station_timeseries.station_id`` FK-references ``met.met_station``.
    The generic basins registry import seeds geometry/model rows but NOT the
    forcing-grid stations (that was a qhh-bootstrap-specific extra), so without
    this the timeseries insert FK-fails. Stations are static per basin_version,
    so this is mirrored once and upserted idempotently from the archived
    compatibility-only node-22 rollback source; geom is moved as EWKB to preserve
    SRID. Short-circuits when the local row count already matches node-22
    (stations do not change cycle-to-cycle, so re-running across a basin's 100+
    runs skips the per-row upsert after run 1)."""
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
    parser = argparse.ArgumentParser(description="Archived rollback forcing-domain mirror to node-27 local DB.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--object-store-root",
        default=os.environ.get("OBJECT_STORE_ROOT"),
        help="Object-store filesystem root. Defaults to OBJECT_STORE_ROOT.",
    )
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL") or LOCAL_DEFAULT)
    parser.add_argument(
        "--allow-archived-node22-db-rollback-mirror",
        action="store_true",
        help=(
            "Deliberately allow the explicit-DSN, allow-flagged, compatibility-only, "
            "sunset-bound archived node-22 DB rollback mirror. "
            "This is not normal node-27 ingest operation."
        ),
    )
    args = parser.parse_args(argv)

    try:
        node22_source = _resolve_node22_source()
    except Node22MirrorDsnMissing:
        _dump_json(_missing_node22_dsn_report(args.run_id))
        return 2
    if not _archived_rollback_mirror_allowed(args.allow_archived_node22_db_rollback_mirror):
        _dump_json(_rollback_mirror_not_allowed_report(args.run_id, dsn_source=node22_source.source))
        return 2

    source, source_blockers = _source_preflight(node22_source.url)
    if source_blockers:
        _dump_json(
            _source_forbidden_report(
                args.run_id,
                dsn_source=node22_source.source,
                source=source,
                blockers=source_blockers,
            )
        )
        return 2

    if not args.object_store_root:
        parser.error("OBJECT_STORE_ROOT or --object-store-root is required.")
    destination, destination_blockers = _destination_preflight(args.database_url)
    if destination_blockers:
        _dump_json(
            _destination_forbidden_report(
                args.run_id,
                dsn_source=node22_source.source,
                destination=destination,
                blockers=destination_blockers,
            )
        )
        return 2

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
