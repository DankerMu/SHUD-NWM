#!/usr/bin/env python3
"""Basin-agnostic autopipeline: discover object-store runs, seed missing basin
registries, then register -> object-store forcing handoff -> parse ->
refresh-coverage every run.

Generalises the earlier qhh-hardcoded ingest into a basin-agnostic pipeline so
node-27 can self-serve every publishable basin under ``BASINS_ROOT`` plus any
run that appears under ``<OBJECT_STORE_ROOT>/runs/``:

  1. Scan ``runs/`` and parse ``fcst_{gfs,ifs}_<cycle10>_basins_<basin>_shud``
     into (basin, source, cycle). Non-matching dirs are ignored.
  2. Discover publishable basins directly from ``BASINS_ROOT`` and seed any
     registry rows that are missing (``core.basin`` has no ``basins_<basin>``
     row), using the generic model-registry CLI -- discover-basins ->
     publish-basins -> import-basins-registry -> activate model_instance. If a
     run manifest exists for the basin, its package identity overrides the
     inventory-derived default.
  3. For every run, run the per-run pipeline (each step a subprocess so one
     run's failure never aborts the batch):
       register -> scripts/node27_ingest_run.py
       forcing  -> object-store forcing-domain handoff DB apply
       parse    -> workers.output_parser.cli parse
       refresh  -> scripts/node27_refresh_coverage.py  (Mission-4; skipped if absent)

Idempotent and failure-isolated. Re-running only does outstanding work:
already-seeded basins and already-parsed runs are detected and skipped.
Prints a JSON summary; exit 0 unless a run hard-failed.

Object-store / DB env (same contract as the per-run scripts)::

    OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store
    OBJECT_STORE_PREFIX=s3://nhms
    DATABASE_URL=postgresql://nhms:nhms_dev@127.0.0.1:55432/nhms
    BASINS_ROOT=/home/ghdc/nwm/Basins        # geometry source for registry seed
"""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psycopg2

from packages.common.forcing_domain_handoff_apply import (
    APPLY_MODE as OBJECT_STORE_HANDOFF_MODE,
)
from packages.common.forcing_domain_handoff_apply import (
    REASON_APPLY_COMPRESSED_CHUNK_BLOCKED,
    apply_forcing_domain_handoff_path,
)
from packages.common.redaction import redact_payload, redact_text
from workers.model_registry.basins_discovery import discover_basins_inventory
from workers.model_registry.basins_radiation_template import repair_missing_tsd_rl_for_basin

PY = sys.executable
# fcst_<source>_<cycle10>_basins_<basin>_shud  (basin may contain underscores).
RUN_RE = re.compile(r"^fcst_(?P<source>gfs|ifs)_(?P<cycle>\d{10})_basins_(?P<basin>.+)_shud$")
DIRECT_GRID_RUN_RE = re.compile(r"^fcst_(?P<source>gfs|ifs)_(?P<cycle>\d{10})_dg_[0-9a-f]+$")

# Auth for import-basins-registry (models.switch_version => model_admin|sys_admin).
SEED_AUTH_ACTOR = os.environ.get("AUTOPIPE_AUTH_ACTOR", "node27-autopipe")
SEED_AUTH_ROLE = os.environ.get("AUTOPIPE_AUTH_ROLE", "model_admin")
# Scratch root for per-basin seed copies (the basin geometry subtree + the
# publish obj-store). Defaults to the system temp dir, but on a host whose / is
# small set AUTOPIPE_WORK_ROOT to a path on the big volume (node-27: / is 98G,
# /home is 1.7T) so a multi-GB basin copy never fills /. Scratch is removed
# after every seed regardless (see _seed_basin).
WORK_ROOT = os.environ.get("AUTOPIPE_WORK_ROOT") or tempfile.gettempdir()
DEFAULT_SEED_PACKAGE_VERSION_TEMPLATE = "vbasins-{slug_id}-production"
SAFE_PACKAGE_VERSION_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
DEFAULT_RUN_WORKERS = 1
MAX_RUN_WORKERS = 8

INGEST_ROLE = "node27_data_plane_ingest"
# Phase 3.5 statistics guard (issue #1378). Every cycle's run_id/run_key is a
# value the frontier chunk's planner statistics have never seen, so estimated
# rows collapse to ~0 and the valid-times query flips off the identity index —
# a plan regression no row-count threshold predicts. The floor exists only to
# skip chunks this tick never touched (one real run writes segments x timesteps
# rows, orders of magnitude above it), never to postpone the refresh.
# The guard's second leg (issue #1468) repairs a statistics WIPE instead: PG15
# discards all cumulative statistics on crash recovery without writing
# `stats_reset`, and a zero-churn table then sits at n_mod_since_analyze = 0
# forever, so no autovacuum threshold can ever re-trigger it.
# That leg is therefore ungated by ingest -- its trigger is statistics being
# absent, not the frontier having moved (see _analyze_unanalyzed_authority_tables).
STATS_GUARD_MIN_MODS = 10_000
STATS_GUARD_MAX_CHUNKS = 3
# 6x the measured worst case (~20 s for a 250M-row chunk; 64 s for three on
# 2026-08-19), and 3 x 120 s still fits inside the 10 min tick cadence.
STATS_GUARD_TIMEOUT_MS = 120_000
INGEST_SUMMARY_SCHEMA = "nhms.node27_ingest.autopipeline.v1"
INGEST_PREFLIGHT_SCHEMA = "nhms.node27_ingest.preflight.v1"
PREFLIGHT_BLOCKED_RC = 2
INGEST_STAGE_SHAPE = (
    "seed_registry",
    "register",
    "object_store_forcing_handoff",
    "parse",
    "refresh_coverage",
    "publish_status",
)
LIBPQ_AMBIENT_ENV_FORBIDDEN_REASON = "LIBPQ_AMBIENT_ENV_FORBIDDEN"
LIBPQ_CONNECTION_ENV_KEYS = frozenset(
    {
        "PGAPPNAME",
        "PGCHANNELBINDING",
        "PGCLIENTENCODING",
        "PGCONNECT_TIMEOUT",
        "PGDATABASE",
        "PGDATESTYLE",
        "PGGEQO",
        "PGGSSDELEGATION",
        "PGGSSENCMODE",
        "PGGSSLIB",
        "PGHOST",
        "PGHOSTADDR",
        "PGKRBSRVNAME",
        "PGLOCALEDIR",
        "PGLOADBALANCEHOSTS",
        "PGMAXPROTOCOLVERSION",
        "PGMINPROTOCOLVERSION",
        "PGOPTIONS",
        "PGPASSFILE",
        "PGPASSWORD",
        "PGPORT",
        "PGREQUIREAUTH",
        "PGREQUIREPEER",
        "PGREQUIRESSL",
        "PGSERVICE",
        "PGSERVICEFILE",
        "PGSSL_CERT_FILE",
        "PGSSL_KEY_FILE",
        "PGSSL_ROOT_CERT_FILE",
        "PGSSLCERTMODE",
        "PGSSLCOMPRESSION",
        "PGSSLCRL",
        "PGSSLCRLDIR",
        "PGSSLCERT",
        "PGSSLKEY",
        "PGSSLMAXPROTOCOLVERSION",
        "PGSSLMINPROTOCOLVERSION",
        "PGSSLMODE",
        "PGSSLNEGOTIATION",
        "PGSSLROOTCERT",
        "PGSSLSNI",
        "PGSYSCONFDIR",
        "PGTARGETSESSIONATTRS",
        "PGTZ",
        "PGUSER",
    }
)
DISPLAY_HEALTH_SEPARATION = "display_api_health_is_readonly_consumer_health_not_ingest_writer_readiness"
DATABASE_URL_QUERY_OVERRIDE_FORBIDDEN = "DATABASE_URL_QUERY_OVERRIDE_FORBIDDEN"
DATABASE_URL_NODE22_HISTORICAL_ENDPOINT = "DATABASE_URL_NODE22_HISTORICAL_ENDPOINT"
DATABASE_URL_ENDPOINT_NOT_NODE27 = "DATABASE_URL_ENDPOINT_NOT_NODE27"
DATABASE_URL_ARGV_FORBIDDEN = "DATABASE_URL_ARGV_FORBIDDEN"
DATABASE_URL_FILE_INVALID = "DATABASE_URL_FILE_INVALID"
DATABASE_URL_FILE_UNSAFE = "DATABASE_URL_FILE_UNSAFE"
NODE22_DSN_ARGV_FORBIDDEN = "NODE22_DSN_ARGV_FORBIDDEN"
NODE22_DB_RUNTIME_ENV_FORBIDDEN = "NODE22_DB_RUNTIME_ENV_FORBIDDEN"
DEFAULT_ALLOWED_DB_ENDPOINTS = "127.0.0.1:55432,localhost:55432"
NODE22_DB_RUNTIME_ENV_KEYS = frozenset(
    {
        "N22_DSN",
        "NHMS_NODE22_DSN_SOURCE",
        "NHMS_ALLOW_ARCHIVED_NODE22_DB_ROLLBACK_MIRROR",
    }
)
DATABASE_URL_ALLOWED_QUERY_KEYS = frozenset(
    {
        "application_name",
        "connect_timeout",
        "fallback_application_name",
        "sslmode",
    }
)
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

NO_FORCING_HANDOFF_MODE = "object_store_forcing_domain_handoff_missing"
NO_FORCING_HANDOFF_REASON = "OBJECT_STORE_FORCING_HANDOFF_REQUIRED"
FORCING_HANDOFF_UNAVAILABLE_REASON = "OBJECT_STORE_FORCING_HANDOFF_UNAVAILABLE"
FORCING_HANDOFF_FAILED_REASON = "OBJECT_STORE_FORCING_HANDOFF_FAILED"
FORCING_STAGE = "forcing_handoff"
# #1781: savepoint that scopes the decline read inside the caller's transaction.
DECLINE_READ_SAVEPOINT_NAME = "nhms_declined_runs_read"


@dataclass(frozen=True)
class DatabaseUrlConfig:
    url: str | None
    source: str | None
    error_code: str | None = None
    error_message: str | None = None


# --------------------------------------------------------------------------- #
# ingest preflight
# --------------------------------------------------------------------------- #
def _preflight_blocker(code: str, env_var: str, message: str) -> dict[str, str]:
    return {"code": code, "env_var": env_var, "message": message}


def _path_preflight(env_var: str, raw_value: str | None) -> tuple[dict[str, Any], list[dict[str, str]]]:
    raw = (raw_value or "").strip()
    if not raw:
        return {"env_var": env_var, "configured": False}, [
            _preflight_blocker(f"{env_var}_MISSING", env_var, f"{env_var} is required for node-27 ingest.")
        ]

    path = Path(raw)
    evidence = {"env_var": env_var, "configured": True, "path": str(path)}
    if not path.is_absolute():
        return evidence, [
            _preflight_blocker(
                f"{env_var}_UNSAFE",
                env_var,
                f"{env_var} must be an absolute non-root path.",
            )
        ]
    if not path.is_dir():
        return evidence, [
            _preflight_blocker(
                f"{env_var}_NOT_DIRECTORY",
                env_var,
                f"{env_var} must point to an existing directory.",
            )
        ]
    resolved = path.resolve()
    evidence["resolved_path"] = str(resolved)
    if resolved == Path("/"):
        return evidence, [
            _preflight_blocker(
                f"{env_var}_UNSAFE",
                env_var,
                f"{env_var} must not resolve to the filesystem root.",
            )
        ]
    return evidence, []


def _database_username_class(username: str | None) -> str:
    normalized = (username or "").strip().lower()
    if not normalized:
        return "missing"
    if "display" in normalized or "readonly" in normalized or normalized.endswith("_ro") or normalized.endswith("ro"):
        return "display_readonly_like"
    return "writer_candidate"


def _database_query_blockers(query: str) -> list[dict[str, str]]:
    if not query:
        return []
    query_keys = {key.strip().lower() for key, _value in parse_qsl(query, keep_blank_values=True)}
    if query_keys and any(key not in DATABASE_URL_ALLOWED_QUERY_KEYS for key in query_keys):
        return [
            _preflight_blocker(
                DATABASE_URL_QUERY_OVERRIDE_FORBIDDEN,
                "DATABASE_URL",
                "DATABASE_URL query parameters must not override the ingest target or credential source.",
            )
        ]
    return []


def _database_port(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _database_url_points_to_historical_node22(host: str | None, port: int | None) -> bool:
    normalized_host = (host or "").strip().lower()
    return normalized_host in NODE22_HISTORICAL_DB_HOSTS or port == NODE22_HISTORICAL_DB_PORT


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


def _database_url_points_to_allowed_node27(
    *,
    host: str | None,
    port: int | None,
    database: str | None,
    env: dict[str, str],
) -> bool:
    normalized_host = (host or "").strip().lower()
    normalized_database = (database or "").strip()
    if normalized_database != "nhms" or port is None:
        return False
    allowed = _parse_allowed_database_endpoints(env.get("NODE27_INGEST_ALLOWED_DATABASE_ENDPOINTS"))
    return (normalized_host, port) in allowed


def _database_preflight(database_url: str | None, env: dict[str, str]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    raw = (database_url or "").strip()
    if not raw:
        return {"configured": False}, [
            _preflight_blocker("DATABASE_URL_MISSING", "DATABASE_URL", "DATABASE_URL is required for node-27 ingest.")
        ]

    try:
        parsed = urlsplit(raw)
    except ValueError:
        return {"configured": True}, [
            _preflight_blocker(
                "DATABASE_URL_INVALID",
                "DATABASE_URL",
                "DATABASE_URL must be a valid PostgreSQL URL.",
            )
        ]

    query_blockers = _database_query_blockers(parsed.query)

    try:
        dsn_parameters = psycopg2.extensions.parse_dsn(raw)
    except psycopg2.Error:
        if query_blockers:
            return {"configured": True, "scheme": parsed.scheme or None}, query_blockers
        return {"configured": True, "scheme": parsed.scheme or None}, [
            _preflight_blocker(
                "DATABASE_URL_INVALID",
                "DATABASE_URL",
                "DATABASE_URL must be a valid PostgreSQL URL.",
            )
        ]

    database = dsn_parameters.get("dbname")
    username = dsn_parameters.get("user")
    host = dsn_parameters.get("host")
    port = _database_port(dsn_parameters.get("port"))
    username_class = _database_username_class(username)
    password_present = bool(dsn_parameters.get("password"))
    identity = {
        "configured": True,
        "scheme": parsed.scheme,
        "host": host,
        "port": port,
        "database": database,
        "username_present": username_class != "missing",
        "username_class": username_class,
        "password_present": password_present,
    }
    blockers: list[dict[str, str]] = list(query_blockers)
    invalid_identity = (
        parsed.scheme not in {"postgres", "postgresql"}
        or not host
        or not database
        or (dsn_parameters.get("port") and port is None)
    )
    if invalid_identity:
        return identity, [
            _preflight_blocker(
                "DATABASE_URL_INVALID",
                "DATABASE_URL",
                "DATABASE_URL must include PostgreSQL scheme, host, and database name.",
            )
        ]
    if _database_url_points_to_historical_node22(host, port):
        blockers.append(
            _preflight_blocker(
                DATABASE_URL_NODE22_HISTORICAL_ENDPOINT,
                "DATABASE_URL",
                "DATABASE_URL must target the node-27 ingest writer, not node-22 historical PostgreSQL.",
            )
        )
    if not _database_url_points_to_allowed_node27(host=host, port=port, database=database, env=env):
        blockers.append(
            _preflight_blocker(
                DATABASE_URL_ENDPOINT_NOT_NODE27,
                "DATABASE_URL",
                "DATABASE_URL must target an allowed node-27 PostgreSQL endpoint.",
            )
        )
    if identity["username_class"] == "missing":
        blockers.append(
            _preflight_blocker(
                "DATABASE_URL_USERNAME_MISSING",
                "DATABASE_URL",
                "DATABASE_URL must include an explicit ingest writer username.",
            )
        )
        return identity, blockers
    if identity["username_class"] == "display_readonly_like":
        blockers.append(
            _preflight_blocker(
                "DATABASE_URL_READONLY_IDENTITY",
                "DATABASE_URL",
                "DATABASE_URL appears to use a display/readonly identity, not an ingest writer.",
            )
        )
    if not password_present:
        blockers.append(
            _preflight_blocker(
                "DATABASE_URL_PASSWORD_MISSING",
                "DATABASE_URL",
                "DATABASE_URL must include explicit password material for the ingest writer username.",
            )
        )
    if blockers:
        return identity, blockers
    return identity, []


def _raw_database_url_arg_present(argv: list[str]) -> bool:
    return any(arg == "--database-url" or arg.startswith("--database-url=") for arg in argv)


def _raw_node22_url_arg_present(argv: list[str]) -> bool:
    return any(arg == "--node22-url" or arg.startswith("--node22-url=") for arg in argv)


def _argv_option_value(argv: list[str], option: str) -> str | None:
    prefix = f"{option}="
    for index, arg in enumerate(argv):
        if arg.startswith(prefix):
            return arg[len(prefix) :]
        if arg == option and index + 1 < len(argv):
            return argv[index + 1]
    return None


def _ambient_libpq_env_blockers(env: dict[str, str]) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    for key in sorted(LIBPQ_CONNECTION_ENV_KEYS):
        if (env.get(key) or "").strip():
            blockers.append(
                _preflight_blocker(
                    LIBPQ_AMBIENT_ENV_FORBIDDEN_REASON,
                    key,
                    f"{key} must be unset so explicit node-27 ingest DSNs cannot be overridden by libpq state.",
                )
            )
    return blockers


def _node22_runtime_env_blockers(env: dict[str, str]) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    for key in sorted(NODE22_DB_RUNTIME_ENV_KEYS):
        if (env.get(key) or "").strip():
            blockers.append(
                _preflight_blocker(
                    NODE22_DB_RUNTIME_ENV_FORBIDDEN,
                    key,
                    f"{key} is forbidden in node-27 ingest runtime; use object-store forcing-domain handoff.",
                )
            )
    return blockers


def _database_url_file_config(path_value: str | None) -> DatabaseUrlConfig:
    raw = _non_empty(path_value)
    if raw is None:
        return DatabaseUrlConfig(url=None, source=None)
    path = Path(raw)
    source = f"file:{path}"
    if not path.is_absolute():
        return DatabaseUrlConfig(
            url=None,
            source=source,
            error_code=DATABASE_URL_FILE_UNSAFE,
            error_message="Node-27 writer DSN file path must be absolute.",
        )
    try:
        link_info = path.lstat()
    except OSError as exc:
        return DatabaseUrlConfig(
            url=None,
            source=source,
            error_code=DATABASE_URL_FILE_INVALID,
            error_message=f"Node-27 writer DSN file cannot be inspected: {exc.strerror or type(exc).__name__}.",
        )
    if stat.S_ISLNK(link_info.st_mode):
        return DatabaseUrlConfig(
            url=None,
            source=source,
            error_code=DATABASE_URL_FILE_UNSAFE,
            error_message="Node-27 writer DSN file must not be a symlink.",
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    try:
        fd = os.open(path, flags)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return DatabaseUrlConfig(
                url=None,
                source=source,
                error_code=DATABASE_URL_FILE_UNSAFE,
                error_message="Node-27 writer DSN file must be a regular file.",
            )
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            return DatabaseUrlConfig(
                url=None,
                source=source,
                error_code=DATABASE_URL_FILE_UNSAFE,
                error_message="Node-27 writer DSN file must be owner-only readable/writable.",
            )
        if not (info.st_mode & stat.S_IRUSR):
            return DatabaseUrlConfig(
                url=None,
                source=source,
                error_code=DATABASE_URL_FILE_UNSAFE,
                error_message="Node-27 writer DSN file must be readable by its owner.",
            )
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = None
            contents = handle.read()
    except OSError as exc:
        return DatabaseUrlConfig(
            url=None,
            source=source,
            error_code=DATABASE_URL_FILE_INVALID,
            error_message=(
                "Node-27 writer DSN file cannot be opened/read safely: "
                f"{exc.strerror or type(exc).__name__}."
            ),
        )
    finally:
        if fd is not None:
            os.close(fd)
    lines = [line.strip() for line in contents.splitlines() if line.strip()]
    if len(lines) != 1:
        return DatabaseUrlConfig(
            url=None,
            source=source,
            error_code=DATABASE_URL_FILE_INVALID,
            error_message="Node-27 writer DSN file must contain exactly one non-empty line.",
        )
    return DatabaseUrlConfig(url=lines[0], source=source)


def _database_url_config(database_url_file: str | None, env: dict[str, str]) -> DatabaseUrlConfig:
    file_config = _database_url_file_config(database_url_file)
    if file_config.url or file_config.error_code:
        return file_config
    env_url = _non_empty(env.get("DATABASE_URL"))
    if env_url:
        return DatabaseUrlConfig(url=env_url, source="env:DATABASE_URL")
    return DatabaseUrlConfig(url=None, source=None)


def _role_preflight(env: dict[str, str]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    service_role = (env.get("NHMS_SERVICE_ROLE") or "").strip().lower()
    ingest_role = (env.get("NHMS_NODE27_INGEST_ROLE") or "").strip().lower()
    evidence = {
        "role": INGEST_ROLE,
        "ingest_role_env": ingest_role or None,
        "service_role_env": service_role or None,
    }
    blockers: list[dict[str, str]] = []
    if not ingest_role:
        blockers.append(
            _preflight_blocker(
                "INGEST_ROLE_REQUIRED",
                "NHMS_NODE27_INGEST_ROLE",
                "NHMS_NODE27_INGEST_ROLE must be node27_data_plane_ingest for node-27 ingest.",
            )
        )
    if service_role == "display_readonly" or ingest_role == "display_readonly":
        blockers.append(
            _preflight_blocker(
                "INGEST_DISPLAY_READONLY_ROLE_FORBIDDEN",
                "NHMS_SERVICE_ROLE",
                "display_readonly runtime evidence cannot satisfy node-27 ingest writer readiness.",
            )
        )
    if ingest_role and ingest_role != INGEST_ROLE:
        blockers.append(
            _preflight_blocker(
                "INGEST_ROLE_UNSUPPORTED",
                "NHMS_NODE27_INGEST_ROLE",
                "NHMS_NODE27_INGEST_ROLE must be node27_data_plane_ingest when set.",
            )
        )
    return evidence, blockers


def _ingest_config_source(env: dict[str, str]) -> str:
    return (
        (env.get("NHMS_NODE27_INGEST_CONFIG_SOURCE") or "").strip()
        or (env.get("NODE27_AUTOPIPE_CONFIG_SOURCE") or "").strip()
        or "cli_or_environment"
    )


def _preflight_ingest_config(
    *,
    database_url: str | None,
    database_url_source: str | None = None,
    database_url_error_code: str | None = None,
    database_url_error_message: str | None = None,
    object_store_root: str | None,
    basins_root: str | None,
    env: dict[str, str],
) -> dict[str, Any]:
    blockers: list[dict[str, str]] = []
    role, role_blockers = _role_preflight(env)
    blockers.extend(role_blockers)
    blockers.extend(_ambient_libpq_env_blockers(env))
    blockers.extend(_node22_runtime_env_blockers(env))

    if database_url_error_code:
        database = {"configured": True, "source": database_url_source}
        blockers.append(
            _preflight_blocker(
                database_url_error_code,
                "DATABASE_URL",
                database_url_error_message or database_url_error_code,
            )
        )
    else:
        database, database_blockers = _database_preflight(database_url, env)
        if database_url_source:
            database["source"] = database_url_source
        blockers.extend(database_blockers)

    object_store, object_store_blockers = _path_preflight("OBJECT_STORE_ROOT", object_store_root)
    blockers.extend(object_store_blockers)
    basins, basins_blockers = _path_preflight("BASINS_ROOT", basins_root)
    blockers.extend(basins_blockers)
    work_root, work_root_blockers = _path_preflight("AUTOPIPE_WORK_ROOT", env.get("AUTOPIPE_WORK_ROOT"))
    blockers.extend(work_root_blockers)
    log_root, log_root_blockers = _path_preflight("AUTOPIPE_LOG_ROOT", env.get("AUTOPIPE_LOG_ROOT"))
    blockers.extend(log_root_blockers)

    return redact_payload(
        {
            "schema": INGEST_PREFLIGHT_SCHEMA,
            "status": "blocked" if blockers else "ready",
            "role": role,
            "stage_shape": list(INGEST_STAGE_SHAPE),
            "config_source": _ingest_config_source(env),
            "display_api_health_separate": True,
            "display_api_health_note": DISPLAY_HEALTH_SEPARATION,
            "database": database,
            "paths": {
                "object_store_root": object_store,
                "basins_root": basins,
                "work_root": work_root,
                "log_root": log_root,
            },
            "blockers": blockers,
        }
    )


def _ingest_evidence(preflight: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": INGEST_ROLE,
        "stage_shape": list(INGEST_STAGE_SHAPE),
        "config_source": preflight.get("config_source"),
        "display_api_health_separate": True,
        "display_api_health_note": DISPLAY_HEALTH_SEPARATION,
        "preflight": preflight,
    }


def _empty_seed_summary() -> dict[str, Any]:
    return {"seeded": [], "already_seeded": [], "failed": [], "details": []}


def _empty_runs_summary() -> dict[str, Any]:
    return {
        "already_ingested": 0,
        "published": 0,
        "processed": 0,
        "ingested": 0,
        "skipped": 0,
        "failed": 0,
        "declined": 0,
        "ingested_by_source": {},
        "details": [],
        "skipped_runs": [],
        "failed_runs": [],
        "declined_runs": [],
    }


def _emit_json_summary(summary: dict[str, Any]) -> None:
    json.dump(redact_payload(summary), sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


# --------------------------------------------------------------------------- #
# run discovery
# --------------------------------------------------------------------------- #
def _discover_runs(object_store_root: Path, sources: tuple[str, ...]) -> list[dict[str, str]]:
    runs_dir = object_store_root / "runs"
    out: list[dict[str, str]] = []
    if not runs_dir.is_dir():
        return out
    for entry in sorted(runs_dir.iterdir()):
        if not entry.is_dir():
            continue
        legacy_match = RUN_RE.match(entry.name)
        direct_grid_match = DIRECT_GRID_RUN_RE.match(entry.name)
        match = legacy_match or direct_grid_match
        if not match or match.group("source") not in sources:
            continue
        if legacy_match is not None:
            basin = _slug_id(legacy_match.group("basin"))
        else:
            try:
                basin = _basin_identity(object_store_root, entry.name)["basin_key"]
            except (OSError, ValueError, json.JSONDecodeError, TypeError, KeyError):
                # A direct-grid run has no basin identity in its run_id.  It is
                # unsafe to guess; leave malformed/incomplete copyback entries
                # for a later idempotent scan after the manifest is complete.
                continue
        out.append(
            {
                "run_id": entry.name,
                "source": match.group("source"),
                "cycle": match.group("cycle"),
                "basin": basin,
                "run_family": "legacy" if legacy_match is not None else "direct_grid",
            }
        )
    return out


def _read_manifest(object_store_root: Path, run_id: str) -> dict[str, Any]:
    path = object_store_root / "runs" / run_id / "input" / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _basin_identity(object_store_root: Path, run_id: str) -> dict[str, str]:
    """Derive (model_id, basin_id, package_version) from a run manifest.

    package_version is the second-to-last path segment of model_package_uri,
    e.g. ``s3://nhms/models/basins_heihe_shud/vbasins-heihe-production/package/``
    -> ``vbasins-heihe-production`` (matches the qhh DB row exactly).
    """
    manifest = _read_manifest(object_store_root, run_id)
    identity = manifest.get("identity") or {}
    model = manifest.get("model") or {}
    model_id = identity.get("model_id") or model.get("model_id")
    basin_id = identity.get("basin_id") or model.get("basin_id")
    basin_slug = identity.get("basin_slug") or model.get("basin_slug")
    package_uri = identity.get("model_package_uri") or model.get("model_package_uri")
    if not (model_id and basin_id and package_uri):
        raise ValueError(f"manifest for {run_id} missing model_id/basin_id/model_package_uri")
    segments = [seg for seg in str(package_uri).rstrip("/").split("/") if seg]
    # .../models/<model_id>/<version>/package  -> version is segment before 'package'
    version = segments[-2] if segments[-1] == "package" else segments[-1]
    return {
        "model_id": str(model_id),
        "basin_id": str(basin_id),
        "basin_key": _slug_id(str(basin_slug or basin_id).removeprefix("basins_")),
        "basin_slug": str(basin_slug or str(basin_id).removeprefix("basins_")),
        "package_version": version,
        "package_uri": str(package_uri),
    }


def _discover_seed_basin_identities(
    basins_root: Path,
    *,
    only_basin: str | None = None,
    object_store_prefix: str = "",
) -> dict[str, dict[str, str]]:
    inventory = discover_basins_inventory(basins_root)
    models = inventory.get("models")
    if not isinstance(models, Sequence) or isinstance(models, str | bytes | bytearray):
        return {}
    identities: dict[str, dict[str, str]] = {}
    for item in models:
        if not isinstance(item, Mapping) or not _seed_model_publishable_or_repairable(item):
            continue
        basin_slug = str(item.get("basin_slug") or "")
        basin_key = _slug_id(basin_slug)
        if only_basin and only_basin not in {basin_key, basin_slug}:
            continue
        model_id = str(item.get("model_id") or "")
        suggested_ids = item.get("suggested_ids") if isinstance(item.get("suggested_ids"), Mapping) else {}
        basin_id = str(suggested_ids.get("basin_id") or f"basins_{basin_key}")
        if not model_id or not basin_slug:
            continue
        package_version = _seed_package_version_for_model(item)
        prefix = object_store_prefix.rstrip("/")
        package_uri = f"{prefix}/models/{model_id}/{package_version}/package/" if prefix else ""
        identities[basin_key] = {
            "model_id": model_id,
            "basin_id": basin_id,
            "basin_key": basin_key,
            "basin_slug": basin_slug,
            "package_version": package_version,
            "package_uri": package_uri,
            "identity_source": "basins_inventory",
        }
    return dict(sorted(identities.items()))


def _seed_model_publishable_or_repairable(model: Mapping[str, Any]) -> bool:
    if model.get("status") == "valid" and model.get("default_publish_eligible") is True:
        return True
    return (
        model.get("status") == "partial"
        and model.get("default_publish_eligible") is not True
        and set(model.get("missing_required_files") or []) == {"*.tsd.rl"}
    )


def _seed_package_version_for_model(model: Mapping[str, Any]) -> str:
    template = os.environ.get("AUTOPIPE_PACKAGE_VERSION_TEMPLATE", DEFAULT_SEED_PACKAGE_VERSION_TEMPLATE)
    basin_slug = str(model.get("basin_slug") or "")
    slug_id = _slug_id(basin_slug)
    model_id = str(model.get("model_id") or f"basins_{slug_id}_shud")
    version = template.format(
        slug=basin_slug.replace("/", "_"),
        slug_id=slug_id,
        model_id=model_id,
    )
    if not SAFE_PACKAGE_VERSION_RE.fullmatch(version) or version in {".", ".."}:
        raise ValueError(f"unsafe AUTOPIPE_PACKAGE_VERSION_TEMPLATE rendered version: {version!r}")
    return version


def _slug_id(value: str) -> str:
    normalized = re.sub(r"[^0-9a-zA-Z]+", "_", value).strip("_").lower()
    return normalized or "unknown"


def _basin_key_set(value: str | None) -> set[str]:
    return {
        _slug_id(item).removeprefix("basins_")
        for item in str(value or "").split(",")
        if item.strip()
    }


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #
# #1714: every connection this script opens must be attributable in
# pg_stat_activity. libpq treats fallback_application_name as a DEFAULT only --
# an operator who writes ?application_name=... into DATABASE_URL still wins --
# so the code never takes that override away.
_APPLICATION_NAME = "nhms-autopipe"


def _connect(database_url: str, **kwargs: Any) -> Any:
    """Single connect surface for this script, tagged with _APPLICATION_NAME.

    The only thing this adds over ``psycopg2.connect`` is the attribution
    kwarg; every caller's other connect parameters pass through untouched.
    """
    return psycopg2.connect(
        database_url, fallback_application_name=_APPLICATION_NAME, **kwargs
    )


def _basin_seeded(database_url: str, basin_id: str) -> bool:
    conn = _connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM core.basin WHERE basin_id = %s", (basin_id,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def _record_recompute_decline(
    database_url: str,
    *,
    run_id: str,
    init_state_id: str,
    product_mtime: float,
    reason_code: str,
    detail: str | None,
) -> None:
    """Write one terminal decline record (#1781).

    Deliberately raises on any failure: the caller keeps ``outcome="failed"``
    when this does not commit, so a run is never treated as accounted for on a
    row that does not exist. ``ON CONFLICT DO NOTHING`` makes the write
    idempotent across concurrent workers within a tick and across tick replays.
    """
    conn = _connect(database_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ops.ingest_recompute_decline
                        (run_id, init_state_id, product_mtime, reason_code, detail)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (run_id, init_state_id, product_mtime, reason_code, detail),
                )
    finally:
        conn.close()


def _active_decline_count(database_url: str) -> int | None:
    """Row count of ``ops.ingest_recompute_decline`` for the tick summary (#1781).

    A long-lived terminal record is otherwise invisible after the tick that
    wrote it: the tick goes rc=0 and stays quiet forever. Putting the count in
    every summary makes "silently declining" greppable.

    Errors degrade to ``None`` rather than propagating, on the same rule as the
    stats guard: an observability read must never turn a successful ingest tick
    red. A null in the summary is itself the signal that the count is unknown.
    """
    try:
        conn = _connect(database_url)
    except psycopg2.Error:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM ops.ingest_recompute_decline")
            row = cur.fetchone()
            return int(row[0]) if row else None
    except psycopg2.Error:
        return None
    finally:
        conn.close()


def _declined_runs(
    cur: Any,
    run_ids: list[str],
    object_store_root: Path | None,
) -> set[str]:
    """Runs whose CURRENT product evidence exactly matches a decline record (#1781).

    One batched query, then object-store reads for the runs that came back only
    -- the stat/read cost scales with the number of decline records, not with
    the pending population.

    The match is exact on all three key components, and a run may carry SEVERAL
    records (the table accumulates: each newly-blocked regeneration adds one).
    Matching against ANY of a run's records is what makes the float-equality
    failure mode self-healing -- a mismatched read costs one more blocked tick,
    which writes another row, and never degrades into the permanent loop.

    ``object_store_root=None`` means there is no evidence side at all, so
    nothing is suppressed: the fail-closed rule of design D3 on the read side.
    Within that rule, the key itself comes from `_decline_key` -- the SAME
    helper the write side uses, so `''` (known to have no manifest, design D11)
    matches here instead of being skipped as missing evidence.
    """
    if object_store_root is None or not run_ids:
        return set()
    # Savepoint, not a bare try/except (#1781): this cursor shares the caller's
    # non-autocommit transaction with the completeness query that follows, so a
    # failed statement here poisons it and that query would then raise
    # InFailedSqlTransaction -- same dead tick, different exception. Scoping the
    # read lets it degrade to "suppress nothing" on any DB error (missing table
    # during a deploy window before migration 000055, most concretely), which is
    # the safe direction: the run is retried, blocked again, and the decline
    # write fails against the same missing table, so the outcome stays "failed"
    # and rc stays 1 -- pre-#1781 behaviour, with a valid summary.
    # The SAVEPOINT statement itself is inside the guard: "degrades to suppress
    # nothing on ANY DB error" has to include the statement that establishes the
    # scope, or the one path that cannot degrade is the one that opens it. The
    # flag is the ordering hazard -- ROLLBACK TO a savepoint that was never
    # established is itself an error.
    established = False
    try:
        cur.execute(f"SAVEPOINT {DECLINE_READ_SAVEPOINT_NAME}")
        established = True
        cur.execute(
            """
            SELECT run_id, init_state_id, product_mtime
            FROM ops.ingest_recompute_decline
            WHERE run_id = ANY(%s)
            """,
            (run_ids,),
        )
        rows = cur.fetchall()
    except psycopg2.Error:
        if established:
            cur.execute(f"ROLLBACK TO SAVEPOINT {DECLINE_READ_SAVEPOINT_NAME}")
            cur.execute(f"RELEASE SAVEPOINT {DECLINE_READ_SAVEPOINT_NAME}")
        return set()
    cur.execute(f"RELEASE SAVEPOINT {DECLINE_READ_SAVEPOINT_NAME}")

    recorded: dict[str, set[tuple[str, float]]] = {}
    for row in rows:
        recorded.setdefault(str(row[0]), set()).add((str(row[1]), float(row[2])))

    declined: set[str] = set()
    for run_id, keys in recorded.items():
        key = _decline_key(object_store_root, run_id)
        if key is None:
            continue
        if key in keys:
            declined.add(run_id)
    return declined


def _decline_key(object_store_root: Path, run_id: str) -> tuple[str, float] | None:
    """The (init_state_id, product_mtime) decline key for a run, or None when it
    is genuinely unobtainable (#1781 design D11).

    BOTH the write side (`_decline_blocked_recompute`) and the read side
    (`_declined_runs`) go through here, and that is the point: the two used to
    source the key separately and disagreed about `''` (`if not init_state_id`
    vs `if init_state_id is None`). Harmless only while the write side never
    recorded `''` -- and the moment it does, the disagreement becomes the
    "writes fine, reads never" half-fix, where every tick re-declines, the
    ON CONFLICT DO NOTHING swallows it, rc goes green and the handoff retries
    forever. One helper, one rule.

    A missing manifest (or a manifest with no `initial_state`) yields `''`, a
    legitimate key value meaning "known to have no manifest". It is not missing
    evidence: 14 of the 158 runs on the first node-27 tick had exactly this
    shape, and fail-closing on it re-created the very loop this change kills.
    `''` still works as a reopen condition -- if a manifest with a real
    `initial_state_id` ever appears, the key changes and stops matching, so the
    run is re-evaluated. A transiently unreadable manifest self-heals the same
    way.

    Only `product_mtime` is genuinely unknowable, so it alone returns None.
    """
    manifest = _load_run_manifest_or_none(object_store_root, run_id)
    init_state_id = _manifest_initial_state_id(manifest) if manifest is not None else None
    product_mtime = _run_product_mtime(object_store_root, run_id)
    if product_mtime is None:
        return None
    return (init_state_id or "", product_mtime)


def _already_ingested_runs(
    database_url: str,
    run_ids: list[str],
    *,
    object_store_root: Path | None = None,
) -> set[str]:
    """Return the subset of run_ids already fully ingested. Lets the cron
    re-scan cheaply -- finished runs are skipped instead of re-applying their
    per-cycle forcing handoff every tick.

    Completeness is decided by AUTHORITY STATE first (#1674). A run at status
    'published' is complete whether or not its river_timeseries rows are
    key-visible right now: publish only ever happens once rows exist, so a
    later invisibility has exactly two sources -- NULL-key legacy rows the
    backfill could not reach inside compressed chunks (a recorded, converging
    exclusion contract), or a retention-dropped chunk (an intentional
    deletion). Neither should re-trigger the per-cycle handoff. A run at status
    'parsed' still requires at least one key-visible row: after dual-write a
    parsed run always has one, so its absence means the parser chain did not
    finish and retrying is correct.

    If the object-store run was rewritten after DB parse, do not skip it. That
    is the normal recovery path after a cold-start run is replaced by a
    warm-start recompute with the same run_id. Recorded residual: on a legacy
    NULL-key run the aggregate yields parsed_at NULL, so recompute detection
    degrades to the init_state comparison alone -- a rewrite carrying the SAME
    initial state is not detected on that cohort. hydro_run.updated_at is NOT
    used as a fallback: publish deliberately leaves it alone while every tick's
    register upsert bumps it, so it is not a parse timestamp.

    Runs at status 'superseded' are retired: skipped unconditionally (no
    timeseries-row or manifest-currency check), even though their object-store
    products still exist. Reviving one requires the explicit --force path,
    whose register step flips 'superseded' back to an active status.

    Runs carrying a decline record whose key matches their current product
    evidence are likewise skipped unconditionally (#1781): their recompute was
    correctly detected but is physically unappliable, so it was terminated and
    recorded rather than retried every tick. Both --force (which bypasses this
    whole function) and any new evidence reopen the decision.
    """
    if not run_ids:
        return set()
    conn = _connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT run_id FROM hydro.hydro_run WHERE run_id = ANY(%s) AND status = 'superseded'",
                (run_ids,),
            )
            retired = {str(row[0]) for row in cur.fetchall()}
            # #1781: the second status-independent exclusion, same shape as
            # `retired` above. It must sit here and not inside
            # `_ingested_run_is_current`: the completeness statement below only
            # returns runs at 'parsed'/'published', and at deploy time 56 of the
            # 116 runs the compressed-chunk guard blocks on node-27 are at
            # 'succeeded' (the other 60 are 'published') -- never parsed, so
            # never in that result at all. The set keeps GROWING: every new
            # old-cycle run whose forcing window lands in the compressed chunk
            # joins it, so treat those figures as a snapshot, not a bound.
            declined = _declined_runs(cur, run_ids, object_store_root)
            cur.execute(
                """
                SELECT h.run_id,
                       h.init_state_id,
                       MAX(rt.created_at) AS parsed_at
                FROM hydro.hydro_run h
                -- #1674: completeness is authority-state first. A published run
                -- is complete whether or not its fact rows are key-visible
                -- (NULL-key legacy rows in compressed chunks, or
                -- retention-dropped chunks, must not re-trigger the per-cycle
                -- handoff); a parsed run still needs at least one key-visible
                -- row.
                -- #1442: key-only join, no transitional text aid
                -- (rt.run_id = h.run_id is the forbidden text fact join).
                LEFT JOIN hydro.river_timeseries rt
                  ON rt.run_key = h.run_key
                WHERE h.run_id = ANY(%s)
                  AND h.status IN ('parsed', 'published')
                GROUP BY h.run_id, h.init_state_id, h.status
                HAVING h.status = 'published' OR COUNT(rt.run_key) > 0
                """,
                (run_ids,),
            )
            return retired | declined | {
                str(row[0])
                for row in cur.fetchall()
                if _ingested_run_is_current(
                    run_id=str(row[0]),
                    db_init_state_id=row[1],
                    parsed_at=row[2],
                    object_store_root=object_store_root,
                )
            }
    finally:
        conn.close()


def _ingested_run_is_current(
    *,
    run_id: str,
    db_init_state_id: str | None,
    parsed_at: Any,
    object_store_root: Path | None,
) -> bool:
    if object_store_root is None:
        return True
    manifest = _load_run_manifest_or_none(object_store_root, run_id)
    if manifest is None:
        return True
    manifest_init_state_id = _manifest_initial_state_id(manifest)
    if manifest_init_state_id and str(db_init_state_id or "") != manifest_init_state_id:
        return False
    product_mtime = _run_product_mtime(object_store_root, run_id)
    if product_mtime is None or parsed_at is None or not hasattr(parsed_at, "timestamp"):
        return True
    return product_mtime <= parsed_at.timestamp() + 1.0


def _load_run_manifest_or_none(object_store_root: Path, run_id: str) -> dict[str, Any] | None:
    path = object_store_root / "runs" / run_id / "input" / "manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _manifest_initial_state_id(manifest: Mapping[str, Any]) -> str | None:
    initial_state = manifest.get("initial_state")
    if not isinstance(initial_state, Mapping):
        return None
    state_id = initial_state.get("state_id")
    return str(state_id) if state_id else None


def _run_product_mtime(object_store_root: Path, run_id: str) -> float | None:
    root = object_store_root / "runs" / run_id
    paths = [root / "input" / "manifest.json"]
    output_dir = root / "output"
    if output_dir.is_dir():
        paths.extend(
            path
            for path in output_dir.iterdir()
            if path.is_file()
            and (
                path.name in {"rivqdown.csv", "rivqdown.dat"}
                or path.name.endswith((".rivqdown", ".rivqdown.csv", ".rivqdown.dat"))
            )
        )
    mtimes: list[float] = []
    for path in paths:
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(mtimes) if mtimes else None


def _activate_model(database_url: str, model_id: str) -> int:
    """Activate a model only when its basin version has no active sibling.

    Generic registry import leaves a newly seeded basin without an active
    model, so the first model still needs activation.  Existing basins may
    already have an active legacy model while direct-grid run manifests point
    at immutable source-specific variants.  Re-activating every run variant
    would violate the one-active-model-per-basin-version invariant and must not
    prevent those runs from reaching the display ingest phase.
    """
    conn = _connect(database_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE core.model_instance
                    SET active_flag = true, lifecycle_state = 'active'
                    WHERE model_id = %s
                      AND (
                          active_flag = true
                          OR NOT EXISTS (
                              SELECT 1
                              FROM core.model_instance active_sibling
                              WHERE active_sibling.basin_version_id = core.model_instance.basin_version_id
                                AND active_sibling.active_flag = true
                          )
                      )
                    """,
                    (model_id,),
                )
                return cur.rowcount
    finally:
        conn.close()


def _model_river_network_version_id(database_url: str, model_id: str) -> str | None:
    conn = _connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT river_network_version_id
                FROM core.model_instance
                WHERE model_id = %s
                """,
                (model_id,),
            )
            row = cur.fetchone()
            return str(row[0]) if row and row[0] else None
    finally:
        conn.close()


def _ensure_seeded_basin_display_ready(database_url: str, model_id: str) -> dict[str, Any]:
    """Make an existing imported basin visible to display consumers.

    ``core.basin`` being present only proves the generic registry import ran at
    least once. Display still needs the model active and output reaches carrying
    geometry; older seed passes can leave both incomplete.
    """
    rnv_id = _model_river_network_version_id(database_url, model_id)
    if not rnv_id:
        raise ValueError(f"model_instance missing or incomplete for {model_id}")
    geom_rows = _backfill_output_geometry(database_url, rnv_id)
    activated = _activate_model(database_url, model_id)
    return {
        "model_id": model_id,
        "river_network_version_id": rnv_id,
        "output_geometry_backfilled": geom_rows,
        "model_activated_rows": activated,
    }


def _backfill_output_geometry(database_url: str, river_network_version_id: str) -> int:
    """Copy reach geometry onto the NULL-geom ``.sp.riv`` output reaches the
    generic import seeds. The import deliberately leaves those reaches NULL
    (display geometry is a separate concern), so without this the national /
    per-run MVT JOINs the reach rows but renders nothing -- the basin's river
    segments are invisible and unclickable on the live map (the heihe
    regression). ``only_missing`` keeps it idempotent."""
    from workers.model_registry.basins_registry_import import (
        _backfill_output_segment_geometry,
    )

    conn = _connect(database_url)
    try:
        with conn:
            with conn.cursor() as cur:
                return _backfill_output_segment_geometry(
                    cur,
                    river_network_version_id,
                    only_missing=True,
                )
    finally:
        conn.close()


def _publish_display_runs(database_url: str) -> int:
    """Advance fully-ingested display runs from 'parsed' to 'published'.

    ``/api/v1/layers`` surfaces display-ready hydro runs. A display node
    publishes q_down products after parsed river_timeseries rows appear so the
    overlay registers without waiting for compute-side jobs. Idempotent
    (published runs and runs without timeseries are left untouched). The
    parsed -> published transition keys on key-visible rows, which is right for
    the population it acts on: only runs written after the dual-write cutover
    can still be 'parsed'. Legacy NULL-key runs are already 'published' by
    contract -- they finished publishing before the cutover -- so this
    statement never has to reason about them (#1674 design D2).

    Status-only on purpose: ``updated_at`` means "run data changed" (register,
    mark_run_parsed), and display coverage staleness is
    ``coverage.refreshed_at < run.updated_at``. Bumping it here would re-stale
    every run whose coverage phase 2 just refreshed, making the cron backstop
    recompute each freshly published run for nothing. The MVT tile revision
    still rotates on publish because its digest basis includes ``status``
    (apps/api/routes/hydro_display.py ``_run_source_version``)."""
    conn = _connect(database_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE hydro.hydro_run h
                    SET status = 'published'
                    WHERE h.status = 'parsed'
                      -- #1442: key-only correlation, same reasoning as
                      -- _already_ingested_runs — the run arrives by join, so no
                      -- transitional text aid applies.
                      AND EXISTS (
                          SELECT 1 FROM hydro.river_timeseries rt WHERE rt.run_key = h.run_key
                      )
                    """
                )
                return cur.rowcount
    finally:
        conn.close()


_STATS_GUARD_CANDIDATES_SQL = """
SELECT c.chunk_schema, c.chunk_name, s.n_mod_since_analyze, s.last_analyze
FROM timescaledb_information.chunks c
JOIN pg_stat_user_tables s
  ON s.schemaname = c.chunk_schema
 AND s.relname = c.chunk_name
WHERE (c.hypertable_schema, c.hypertable_name) IN (
    ('hydro', 'river_timeseries'),
    ('met', 'forcing_station_timeseries')
)
  AND c.is_compressed = false
  AND s.n_mod_since_analyze >= %s
ORDER BY s.n_mod_since_analyze DESC, c.chunk_schema, c.chunk_name
"""

_STATS_GUARD_LAST_ANALYZE_SQL = """
SELECT last_analyze
FROM pg_stat_user_tables
WHERE schemaname = %s AND relname = %s
"""

# Repair leg (issue #1468): ordinary tables whose cumulative statistics were
# wiped. Double NULL is the signature -- an ANALYZE that ever ran, by hand or by
# autovacuum, leaves one of the two timestamps behind.
#
# ``relpages > 0`` survives the wipe because it lives in ``pg_class``, not in
# the cumulative statistics -- which is what makes it usable here at all, and
# also what limits it: ``relpages`` is maintained only by VACUUM / ANALYZE /
# CREATE INDEX, and is 0 on a relation none of the three has ever visited (PG14+
# pairs that with the ``reltuples = -1`` never-analyzed sentinel). So the clause
# does not only exclude empty tables: a POPULATED table that was never analyzed
# is invisible to this leg too. That is a deliberate residual risk, recorded in
# the change design's 残余风险 ledger (#1468), not an oversight -- such a table
# has churn by definition (the rows were written), so the flat 50-row
# autoanalyze threshold reaches it, and
# below 50 rows the planner's default estimates are harmless. Do not widen the
# predicate: dropping it would drag every empty table into the candidate set and
# spend the per-tick cap on relations with nothing to analyze.
#
# Hypertables and their chunks are excluded on purpose (#1378 D3):
# ANALYZE on a hypertable root recurses into the chunks, and ANALYZE on a bare
# compressed-chunk name zeroes the relstats TimescaleDB preserved at
# compression time. Chunks live in ``_timescaledb_internal``, which the schema
# tuple already excludes; the NOT EXISTS excludes the roots.
_STATS_GUARD_AUTHORITY_CANDIDATES_SQL = """
SELECT s.schemaname, s.relname, c.relpages, s.last_analyze
FROM pg_stat_user_tables s
JOIN pg_class c ON c.oid = s.relid
WHERE s.schemaname IN ('core', 'met', 'hydro')
  AND c.relkind = 'r'
  AND c.relpages > 0
  AND s.last_analyze IS NULL
  AND s.last_autoanalyze IS NULL
  AND NOT EXISTS (
      SELECT 1
      FROM timescaledb_information.hypertables h
      WHERE h.hypertable_schema = s.schemaname
        AND h.hypertable_name = s.relname
  )
ORDER BY c.relpages DESC, 1, 2
"""

# Chunk identifiers come from the TimescaleDB catalog, but ANALYZE takes no
# bind parameters -- refuse to interpolate anything that is not a bare
# identifier rather than quote-escaping by hand.
_STATS_GUARD_IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _stats_guard_error(exc: Exception) -> str:
    """Always a non-empty, credential-free string -- some DB errors carry no message."""

    return f"{type(exc).__name__}: {redact_text(str(exc))}".rstrip(": ")


def _analyze_one_relation(
    cur: Any,
    schema: str,
    name: str,
    entry: dict[str, Any],
    last_analyze_before: Any,
) -> dict[str, Any]:
    """ANALYZE one relation and read its statistics back -- failures stay local.

    Shared by both guard legs (frontier chunks and authority tables): the
    ANALYZE, the read-back self-check and the per-relation isolation are the
    same mechanism, only the candidate query and the entry's identity keys
    differ. ``entry`` arrives carrying those identity keys and leaves carrying
    the outcome ones.

    Per-relation isolation (design D1): a relation held by the compression lock
    or dropped between the candidate query and the ANALYZE must not swallow the
    rest of the batch. A failed ANALYZE leaves ``n_mod_since_analyze``
    untouched, so that same chunk stays on top of the next tick's descending
    candidate list and would starve every other frontier chunk forever.

    Safe because the caller's connection is autocommit: an error leaves no
    aborted transaction for the remaining relations to trip over.

    The returned entry always carries the same keys; a failure is
    ``status: "failed"`` plus a non-empty ``error`` string, so the summary's
    ``analyzed`` list is one flat, uniformly shaped record of every attempt.
    """
    try:
        if not (_STATS_GUARD_IDENT_RE.match(schema) and _STATS_GUARD_IDENT_RE.match(name)):
            raise ValueError(f"refusing to ANALYZE non-identifier relation name: {schema}.{name}")
        cur.execute(f"SET statement_timeout = {STATS_GUARD_TIMEOUT_MS}")
        started = time.monotonic()
        cur.execute(f'ANALYZE "{schema}"."{name}"')
        seconds = round(time.monotonic() - started, 3)
        cur.execute(_STATS_GUARD_LAST_ANALYZE_SQL, (schema, name))
        row = cur.fetchone()
        last_analyze = row[0] if row else None
        refreshed = last_analyze is not None and (last_analyze_before is None or last_analyze > last_analyze_before)
        entry["seconds"] = seconds
        entry["last_analyze"] = last_analyze.isoformat() if last_analyze is not None else None
        entry["status"] = "ok" if refreshed else "warning"
    except Exception as exc:  # noqa: BLE001 - deliberate per-relation isolation
        entry["seconds"] = None
        entry["last_analyze"] = None
        entry["status"] = "failed"
        entry["error"] = _stats_guard_error(exc)
    return entry


def _analyze_one_frontier_chunk(
    cur: Any,
    chunk_schema: str,
    chunk_name: str,
    n_mod: int,
    last_analyze_before: Any,
) -> dict[str, Any]:
    """Frontier-leg entry shape: ``chunk`` + the drift measure that selected it."""

    return _analyze_one_relation(
        cur,
        chunk_schema,
        chunk_name,
        {"chunk": f"{chunk_schema}.{chunk_name}", "n_mod_since_analyze": int(n_mod)},
        last_analyze_before,
    )


def _analyze_one_authority_table(
    cur: Any,
    schema: str,
    relname: str,
    relpages: int,
    last_analyze_before: Any,
) -> dict[str, Any]:
    """Repair-leg entry shape: ``table`` + the size that ordered it.

    ``relpages`` rather than ``n_mod_since_analyze``: the repair leg's
    candidates are tables whose counters were wiped, so their modification
    counter is exactly 0 and says nothing -- ``pg_class.relpages`` survives the
    wipe and is the only honest ordering key.
    """

    return _analyze_one_relation(
        cur,
        schema,
        relname,
        {"table": f"{schema}.{relname}", "relpages": int(relpages)},
        last_analyze_before,
    )


def _analyze_frontier_chunks(database_url: str) -> dict[str, Any]:
    """ANALYZE the uncompressed frontier chunks this tick's ingest touched.

    Runs on an autocommit connection because the cumulative statistics an
    ANALYZE reports are only visible to a *later* transaction: reading
    ``last_analyze`` back inside the ANALYZE's own transaction would report the
    previous value and fake a PG15 non-owner silent skip. The read-back is the
    only evidence that the ANALYZE did anything at all -- PG15 has no MAINTAIN
    privilege bit, so a non-owner gets a WARNING and a successful return.

    Two failure levels: a single chunk's ANALYZE or read-back is isolated to
    its own ``analyzed`` entry (``status: "failed"`` + ``error``) and the
    remaining selected chunks are still attempted; only a guard-level failure
    (connect, candidate query) sets ``status: "failed"`` on the summary itself.
    Neither changes the tick's return code.
    """
    summary: dict[str, Any] = {
        "status": "completed",
        "min_mods": STATS_GUARD_MIN_MODS,
        "max_chunks": STATS_GUARD_MAX_CHUNKS,
        "analyzed": [],
        "deferred": [],
    }
    conn = None
    try:
        conn = _connect(database_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(_STATS_GUARD_CANDIDATES_SQL, (STATS_GUARD_MIN_MODS,))
            candidates = cur.fetchall()
            selected = candidates[:STATS_GUARD_MAX_CHUNKS]
            # Truncation is never silent: the leftovers are a named backlog the
            # next tick picks up (statistics drift is progressive, not acute).
            summary["deferred"] = [f"{row[0]}.{row[1]}" for row in candidates[STATS_GUARD_MAX_CHUNKS:]]
            for chunk_schema, chunk_name, n_mod, last_analyze_before in selected:
                summary["analyzed"].append(
                    _analyze_one_frontier_chunk(cur, chunk_schema, chunk_name, n_mod, last_analyze_before)
                )
    # Guard-level failure only (connect / candidate query); per-chunk failures
    # never reach here. Statistics drift is a progressive illness, not an acute
    # one: report the failure honestly, keep whatever was refreshed, and let the
    # next tick retry rather than failing an otherwise successful ingest tick.
    except Exception as exc:  # noqa: BLE001 - deliberate blanket isolation
        summary["status"] = "failed"
        summary["error"] = _stats_guard_error(exc)
    finally:
        if conn is not None:
            conn.close()
    return summary


def _analyze_unanalyzed_authority_tables(database_url: str) -> dict[str, Any]:
    """ANALYZE the ordinary tables whose cumulative statistics were wiped.

    The repair leg (issue #1468). Same connection discipline, cap, timeout,
    identifier refusal, read-back self-check and two failure levels as
    ``_analyze_frontier_chunks`` -- see ``_analyze_one_relation``, which both
    legs share; only the candidate query and the entry's identity keys differ.

    Deliberately NOT gated on this tick's ingest: its trigger is statistics
    being absent, not the frontier having moved. A crash recovery zeroes the
    counters of every table at once, and the tables that need repair most are
    exactly the zero-churn authority tables an ingest tick never writes to.
    """
    summary: dict[str, Any] = {
        "status": "completed",
        "analyzed": [],
        "deferred": [],
    }
    conn = None
    try:
        conn = _connect(database_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(_STATS_GUARD_AUTHORITY_CANDIDATES_SQL)
            candidates = cur.fetchall()
            selected = candidates[:STATS_GUARD_MAX_CHUNKS]
            # Same named-backlog rule as the frontier leg: the leftovers are the
            # next tick's work, never a silent truncation.
            summary["deferred"] = [f"{row[0]}.{row[1]}" for row in candidates[STATS_GUARD_MAX_CHUNKS:]]
            for schema, relname, relpages, last_analyze_before in selected:
                summary["analyzed"].append(
                    _analyze_one_authority_table(cur, schema, relname, relpages, last_analyze_before)
                )
    # Guard-level failure only (connect / candidate query); per-table failures
    # never reach here and never change the tick's return code.
    except Exception as exc:  # noqa: BLE001 - deliberate blanket isolation
        summary["status"] = "failed"
        summary["error"] = _stats_guard_error(exc)
    finally:
        if conn is not None:
            conn.close()
    return summary


def _stats_guard(database_url: str, *, ingested_runs: int, env: Mapping[str, str]) -> dict[str, Any]:
    """Phase 3.5 gate: two legs, one switch.

    The frontier leg is gated on this tick having ingested rows (only such a
    tick moved a frontier). The repair leg is not gated at all -- see
    ``_analyze_unanalyzed_authority_tables`` -- so its result is attached to
    every non-skipped summary, ``not_triggered`` included.
    """
    skeleton: dict[str, Any] = {
        "min_mods": STATS_GUARD_MIN_MODS,
        "max_chunks": STATS_GUARD_MAX_CHUNKS,
        "analyzed": [],
        "deferred": [],
    }
    if (env.get("NODE27_AUTOPIPE_STATS_GUARD") or "").strip().lower() == "off":
        return {
            "status": "skipped",
            "reason": "NODE27_AUTOPIPE_STATS_GUARD=off",
            **skeleton,
            "authority": {
                "status": "skipped",
                "reason": "NODE27_AUTOPIPE_STATS_GUARD=off",
                "analyzed": [],
                "deferred": [],
            },
        }
    try:
        authority = _analyze_unanalyzed_authority_tables(database_url)
    except Exception as exc:  # noqa: BLE001 - same isolation as the frontier leg
        authority = {"status": "failed", "error": _stats_guard_error(exc), "analyzed": [], "deferred": []}
    if ingested_runs < 1:
        return {"status": "not_triggered", "reason": "no_run_ingested", **skeleton, "authority": authority}
    try:
        summary = _analyze_frontier_chunks(database_url)
    except Exception as exc:  # noqa: BLE001 - same isolation as coverage refresh
        summary = {"status": "failed", "error": _stats_guard_error(exc), **skeleton}
    summary["authority"] = authority
    return summary


# --------------------------------------------------------------------------- #
# subprocess plumbing
# --------------------------------------------------------------------------- #
def _run(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, cwd=str(REPO_ROOT))
    return proc.returncode, proc.stdout, proc.stderr


def _last_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    for candidate in (text, text.splitlines()[-1]):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _reason_codes(reasons: Any) -> list[str]:
    if not isinstance(reasons, list):
        return []
    codes: list[str] = []
    for reason in reasons:
        if isinstance(reason, dict) and reason.get("code"):
            codes.append(str(reason["code"]))
    return codes


def _reason_details(reasons: Any) -> dict[str, str]:
    """First non-empty ``detail`` per reason code (#1781).

    The decline record's ``detail`` column is the only post-hoc handle on a
    permanently suppressed run, and a bare reason code cannot tell an operator
    which chunk blocked the write. Values are carried through verbatim: the
    apply layer already ran them through ``redact_text``, and this must not
    widen what that narrowed.
    """
    details: dict[str, str] = {}
    if not isinstance(reasons, list):
        return details
    for reason in reasons:
        if not isinstance(reason, dict) or not reason.get("code"):
            continue
        code = str(reason["code"])
        detail = reason.get("detail")
        if detail and code not in details:
            details[code] = str(detail)
    return details


def _stable_reason_codes(
    *values: Any,
    default: str = FORCING_HANDOFF_UNAVAILABLE_REASON,
) -> list[str]:
    codes = [str(value) for value in values if value]
    return codes or [default]


def _handoff_manifest_path(object_store_root: Path, run_id: str) -> Path:
    return object_store_root / "runs" / run_id / "input" / "forcing_domain_handoff.json"


def _truthy_env(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _non_empty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _forcing_stage_from_handoff(report: dict[str, Any]) -> dict[str, Any]:
    return redact_payload(
        {
            "mode": report.get("mode") or OBJECT_STORE_HANDOFF_MODE,
            "status": report.get("status"),
            "ready": bool(report.get("ready")),
            "row_counts": dict(report.get("row_counts") or {}),
            "reason_codes": _reason_codes(report.get("unavailable_reasons")),
            "reason_details": _reason_details(report.get("unavailable_reasons")),
        }
    )


def _forcing_stage_missing_handoff(reason: str = NO_FORCING_HANDOFF_REASON) -> dict[str, Any]:
    return {
        "mode": NO_FORCING_HANDOFF_MODE,
        "status": "skipped",
        "ready": False,
        "row_counts": {},
        "reason_codes": [reason],
    }


def _apply_object_store_forcing_handoff(
    handoff_manifest: Path,
    *,
    object_store_root: Path,
    object_store_prefix: str,
    database_url: str,
) -> dict[str, Any]:
    connection = _connect(database_url)
    try:
        return apply_forcing_domain_handoff_path(
            handoff_manifest,
            object_store_root=object_store_root,
            object_store_prefix=object_store_prefix,
            connection=connection,
        )
    finally:
        connection.close()


def _process_forcing_stage(
    *,
    run_id: str,
    object_store_root: Path,
    database_url: str,
    object_store_prefix: str,
) -> dict[str, Any]:
    handoff_manifest = _handoff_manifest_path(object_store_root, run_id)
    if handoff_manifest.is_file():
        try:
            report = _apply_object_store_forcing_handoff(
                handoff_manifest,
                object_store_root=object_store_root,
                object_store_prefix=object_store_prefix,
                database_url=database_url,
            )
        except Exception as exc:  # noqa: BLE001 - isolate one bad run and continue the batch
            forcing_stage = {
                "mode": OBJECT_STORE_HANDOFF_MODE,
                "status": "failed",
                "ready": False,
                "row_counts": {},
                "reason_codes": [FORCING_HANDOFF_FAILED_REASON],
            }
            return {
                "outcome": "failed",
                "stage": FORCING_STAGE,
                "forcing_stage": forcing_stage,
                "error": f"{FORCING_HANDOFF_FAILED_REASON}: {redact_text(str(exc))}",
            }
        forcing_stage = _forcing_stage_from_handoff(report)
        if report.get("available") is True and report.get("ready") is True:
            return {"outcome": "ready", "forcing_stage": forcing_stage}
        status = str(report.get("status") or "unavailable")
        fallback_code = FORCING_HANDOFF_FAILED_REASON if status == "failed" else FORCING_HANDOFF_UNAVAILABLE_REASON
        forcing_stage["reason_codes"] = _stable_reason_codes(
            *forcing_stage.get("reason_codes", []),
            default=fallback_code,
        )
        return {
            "outcome": "failed",
            "stage": FORCING_STAGE,
            "forcing_stage": forcing_stage,
            "error": ",".join(forcing_stage["reason_codes"]),
        }

    reason = NO_FORCING_HANDOFF_REASON
    return {
        "outcome": "degraded",
        "stage": FORCING_STAGE,
        "forcing_stage": _forcing_stage_missing_handoff(reason=reason),
        "reason": reason,
    }


# --------------------------------------------------------------------------- #
# generic registry seed
# --------------------------------------------------------------------------- #
def _isolate_basin_root(basins_root: Path, basin: str) -> Path:
    """Copy a single basin subtree into a private root and strip Synology
    ``@eaDir`` sidecars, so discover-basins stays under its 2048-entry budget
    (scanning the whole multi-basin Basins root blows the limit)."""
    basin_key = _slug_id(basin)
    only_root = Path(WORK_ROOT) / f"{basin_key}-only-root"
    dst = only_root / basin
    if dst.exists():
        shutil.rmtree(only_root, ignore_errors=True)
    only_root.mkdir(parents=True, exist_ok=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(basins_root / basin, dst, symlinks=False)
    for ea in dst.rglob("@eaDir"):
        shutil.rmtree(ea, ignore_errors=True)
    repair_missing_tsd_rl_for_basin(
        isolated_root=only_root,
        basin_slug=basin,
        template_search_root=basins_root,
    )
    return only_root


def _seed_basin(
    *,
    basin: str,
    identity: dict[str, str],
    database_url: str,
    basins_root: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    model_id = identity["model_id"]
    basin_key = _slug_id(basin)
    work = Path(WORK_ROOT) / f"{basin_key}-seed"
    work.mkdir(parents=True, exist_ok=True)
    inv = work / "inventory.json"
    pkg = work / "package-manifest.json"
    obj_store = Path(WORK_ROOT) / f"{basin_key}-obj-store"  # writable; publish writes models/ here

    only_root = _isolate_basin_root(basins_root, basin)

    cli = [PY, "-m", "workers.model_registry.cli"]
    try:
        rc, out, err = _run(
            cli + ["discover-basins", "--basins-root", str(only_root), "--output", str(inv)], env
        )
        if rc != 0:
            return {
                "basin": basin,
                "outcome": "seed_failed",
                "stage": "discover",
                "error": redact_text((err or out)[-600:]),
            }

        pub_env = dict(env)
        pub_env["OBJECT_STORE_ROOT"] = str(obj_store)
        rc, out, err = _run(
            cli
            + [
                "publish-basins",
                "--inventory", str(inv),
                "--model-id", model_id,
                "--version", identity["package_version"],
                "--output", str(pkg),
            ],
            pub_env,
        )
        if rc != 0:
            return {
                "basin": basin,
                "outcome": "seed_failed",
                "stage": "publish",
                "error": redact_text((err or out)[-600:]),
            }

        rc, out, err = _run(
            cli
            + [
                "import-basins-registry",
                "--inventory", str(inv),
                "--package-manifest", str(pkg),
                "--auth-actor-id", SEED_AUTH_ACTOR,
                "--auth-role", SEED_AUTH_ROLE,
            ],
            env,
        )
        if rc != 0:
            return {
                "basin": basin,
                "outcome": "seed_failed",
                "stage": "import",
                "error": redact_text((err or out)[-600:]),
            }
        import_report = _last_json(out) or {}

        rnv_id = import_report.get("river_network_version_id")
        geom_rows = _backfill_output_geometry(database_url, rnv_id) if rnv_id else 0
        activated = _activate_model(database_url, model_id)
        return {
            "basin": basin,
            "outcome": "seeded",
            "model_id": model_id,
            "package_version": identity["package_version"],
            "import_status": import_report.get("status"),
            "segment_count": import_report.get("segment_count"),
            "output_segment_count": import_report.get("output_segment_count"),
            "output_geometry_backfilled": geom_rows,
            "model_activated_rows": activated,
        }
    finally:
        # Seed scratch (multi-GB basin copy + publish obj-store) is only needed
        # during the CLI calls above; always remove it so it never accumulates.
        for scratch in (only_root, work, obj_store):
            shutil.rmtree(scratch, ignore_errors=True)


# --------------------------------------------------------------------------- #
# per-run pipeline
# --------------------------------------------------------------------------- #
def _refresh_coverage_script() -> Path | None:
    path = REPO_ROOT / "scripts" / "node27_refresh_coverage.py"
    return path if path.is_file() else None


def _decline_blocked_recompute(
    run_id: str,
    *,
    object_store_root: Path,
    database_url: str,
    detail: str | None,
) -> str:
    """Terminate a compressed-chunk-blocked recompute, or keep failing (#1781).

    Returns the outcome the caller should report: ``"declined"`` only when the
    decline key was obtainable AND the record committed. Fail-closed on
    everything else -- an unobtainable key or a failed write leaves the run at
    ``"failed"``, so it retries. The one thing that must never happen is a run
    treated as accounted for on a record that was never written.

    "Unobtainable" is narrower than it once was (design D11): a missing
    manifest is not missing evidence, it is the `''` init component, so only an
    unreadable ``product_mtime`` and a failing write remain fail-closed.
    """
    key = _decline_key(object_store_root, run_id)
    if key is None:
        return "failed"
    init_state_id, product_mtime = key
    try:
        _record_recompute_decline(
            database_url,
            run_id=run_id,
            init_state_id=init_state_id,
            product_mtime=product_mtime,
            reason_code=REASON_APPLY_COMPRESSED_CHUNK_BLOCKED,
            detail=redact_text(detail) if detail else None,
        )
    except Exception:  # noqa: BLE001 - any write failure must fall back to retrying
        return "failed"
    return "declined"


def _process_run(
    run_id: str,
    env: dict[str, str],
    *,
    object_store_root: Path,
    database_url: str,
    object_store_prefix: str,
) -> dict[str, Any]:
    register = [PY, str(REPO_ROOT / "scripts" / "node27_ingest_run.py"), "--run-id", run_id]
    rc, out, err = _run(register, env)
    if rc != 0:
        return {
            "run_id": run_id,
            "outcome": "failed",
            "stage": "register",
            "rc": rc,
            "error": redact_text((err or out)[-500:]),
        }

    forcing = _process_forcing_stage(
        run_id=run_id,
        object_store_root=object_store_root,
        database_url=database_url,
        object_store_prefix=object_store_prefix,
    )
    forcing_stage = forcing.get("forcing_stage")
    if forcing["outcome"] == "failed":
        result = {
            "run_id": run_id,
            "outcome": "failed",
            "stage": forcing.get("stage", FORCING_STAGE),
            "error": forcing.get("error"),
            "forcing_stage": forcing_stage,
        }
        # #1781: a recompute the compressed-chunk guard rejects has no remedy
        # this tick, next tick, or any tick until an operator decompresses --
        # retrying it just reprints the same rejection forever. Record the
        # decision and terminate it. Every other forcing failure (including
        # HANDOFF_APPLY_SQL_FAILURE and the generic exception path) is
        # potentially transient and MUST keep failing so it keeps retrying.
        #
        # What makes this code mean ONLY "a compressed chunk was detected" is
        # the apply layer's `except CompressedChunkWriteError` / `except
        # CompressedChunkGuardError` split: the base class -- which also covers
        # the guard's own catalog SELECT timing out, a transient -- now carries
        # HANDOFF_APPLY_COMPRESSED_CHUNK_GUARD_FAILED and never reaches here.
        forcing_reasons = forcing_stage or {}
        if REASON_APPLY_COMPRESSED_CHUNK_BLOCKED in (forcing_reasons.get("reason_codes") or []):
            result["outcome"] = _decline_blocked_recompute(
                run_id,
                object_store_root=object_store_root,
                database_url=database_url,
                # The guard's own (redacted) message names the offending chunk;
                # the joined reason codes are only the fallback, and duplicate
                # the record's own `reason_code` column.
                detail=(forcing_reasons.get("reason_details") or {}).get(
                    REASON_APPLY_COMPRESSED_CHUNK_BLOCKED
                )
                or result.get("error"),
            )
        return result

    parse = [PY, "-m", "workers.output_parser.cli", "parse", "--run-id", run_id]
    rc, out, err = _run(parse, env)
    if rc != 0:
        return {
            "run_id": run_id,
            "outcome": "failed",
            "stage": "parse",
            "rc": rc,
            "error": redact_text((err or out)[-500:]),
            "forcing_stage": forcing_stage,
        }
    parse_payload = _last_json(out) or {}

    refresh_status = "skipped_no_script"
    refresh_script = _refresh_coverage_script()
    if refresh_script is not None:
        rc, out, err = _run([PY, str(refresh_script), "--run-id", run_id], env)
        if rc != 0:
            # Coverage refresh is Mission-4 territory; a failure here does not
            # invalidate the ingest -- record it but keep the run as ingested.
            refresh_status = f"refresh_failed_rc{rc}"
        else:
            # rc=0 with refreshed=false means the run yielded no coverage row
            # (no displayable forcing/river data yet); latest-product still
            # resolves via the CTE fallback. Record honestly as "no_coverage_row".
            payload = _last_json(out) or {}
            refresh_status = "refreshed" if payload.get("refreshed") else "no_coverage_row"

    return {
        "run_id": run_id,
        "outcome": "ingested",
        "stage": "coverage",
        "forcing_stage": forcing_stage,
        "station_rows": (forcing_stage or {}).get("row_counts", {}).get("met.forcing_station_timeseries"),
        "river_rows": parse_payload.get("rows_written"),
        "parse_status": parse_payload.get("status"),
        "coverage_refresh": refresh_status,
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    global WORK_ROOT
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    env = dict(os.environ)

    if _raw_node22_url_arg_present(raw_argv):
        preflight = _preflight_ingest_config(
            database_url=env.get("DATABASE_URL"),
            database_url_source="env:DATABASE_URL" if env.get("DATABASE_URL") else None,
            object_store_root=_argv_option_value(raw_argv, "--object-store-root") or env.get("OBJECT_STORE_ROOT"),
            basins_root=_argv_option_value(raw_argv, "--basins-root") or env.get("BASINS_ROOT"),
            env=env,
        )
        preflight["status"] = "blocked"
        preflight.setdefault("blockers", []).append(
            _preflight_blocker(
                NODE22_DSN_ARGV_FORBIDDEN,
                "N22_DSN",
                "Legacy node-22 DB DSNs are forbidden; use object-store forcing-domain handoff.",
            )
        )
        _emit_json_summary(
            {
                "schema": INGEST_SUMMARY_SCHEMA,
                "status": "preflight_blocked",
                "return_code": PREFLIGHT_BLOCKED_RC,
                "ingest": _ingest_evidence(preflight),
                "object_store_root": _argv_option_value(raw_argv, "--object-store-root"),
                "basins_root": _argv_option_value(raw_argv, "--basins-root"),
                "sources": [],
                "discovered_runs": 0,
                "basins": [],
                "seed": _empty_seed_summary(),
                "runs": _empty_runs_summary(),
            }
        )
        return PREFLIGHT_BLOCKED_RC

    if _raw_database_url_arg_present(raw_argv):
        preflight = _preflight_ingest_config(
            database_url=None,
            database_url_source="argv:--database-url",
            database_url_error_code=DATABASE_URL_ARGV_FORBIDDEN,
            database_url_error_message=(
                "Pass the node-27 writer DSN through env DATABASE_URL or owner-only "
                "--database-url-file; raw writer DSNs are forbidden in argv."
            ),
            object_store_root=_argv_option_value(raw_argv, "--object-store-root") or env.get("OBJECT_STORE_ROOT"),
            basins_root=_argv_option_value(raw_argv, "--basins-root") or env.get("BASINS_ROOT"),
            env=env,
        )
        _emit_json_summary(
            {
                "schema": INGEST_SUMMARY_SCHEMA,
                "status": "preflight_blocked",
                "return_code": PREFLIGHT_BLOCKED_RC,
                "ingest": _ingest_evidence(preflight),
                "object_store_root": _argv_option_value(raw_argv, "--object-store-root"),
                "basins_root": _argv_option_value(raw_argv, "--basins-root"),
                "sources": [],
                "discovered_runs": 0,
                "basins": [],
                "seed": _empty_seed_summary(),
                "runs": _empty_runs_summary(),
            }
        )
        return PREFLIGHT_BLOCKED_RC

    parser = argparse.ArgumentParser(description="Basin-agnostic node-27 autopipeline.")
    parser.add_argument("--object-store-root", default=os.environ.get("OBJECT_STORE_ROOT"))
    parser.add_argument(
        "--database-url-file",
        default=None,
        help="Owner-only file containing the node-27 writer DSN. Defaults to env DATABASE_URL.",
    )
    parser.add_argument("--basins-root", default=os.environ.get("BASINS_ROOT"))
    parser.add_argument("--sources", default="gfs,ifs", help="Comma list of sources (default gfs,ifs).")
    parser.add_argument("--only-basin", default=None, help="Restrict to a single basin slug (e.g. heihe).")
    parser.add_argument(
        "--only-cycle",
        default=None,
        metavar="YYYYMMDDHH",
        help="Restrict ingest to one exact UTC forecast cycle.",
    )
    parser.add_argument(
        "--direct-grid-only",
        action="store_true",
        help="Restrict ingest to direct-grid runs; excludes legacy basin-named runs.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Process at most N runs (smoke).")
    parser.add_argument("--seed-only", action="store_true", help="Only seed basin registries; skip run ingest.")
    parser.add_argument("--force", action="store_true", help="Re-ingest even already-parsed runs.")
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("AUTOPIPE_RUN_WORKERS", str(DEFAULT_RUN_WORKERS))),
        help=f"Independent per-run ingest workers (1-{MAX_RUN_WORKERS}; default env AUTOPIPE_RUN_WORKERS or 1).",
    )
    parser.add_argument(
        "--exclude-basins",
        default=os.environ.get("AUTOPIPE_EXCLUDE_BASINS", ""),
        help="Comma-separated retired basin slugs/ids excluded from seeding and ingest.",
    )
    parser.add_argument("--progress", action="store_true", help="Per-step progress to stderr.")
    args = parser.parse_args(raw_argv)

    if not 1 <= args.workers <= MAX_RUN_WORKERS:
        parser.error(f"--workers must be between 1 and {MAX_RUN_WORKERS}")

    sources = tuple(s.strip().lower() for s in args.sources.split(",") if s.strip())
    excluded_basins = _basin_key_set(args.exclude_basins)

    database_config = _database_url_config(args.database_url_file, env)
    preflight = _preflight_ingest_config(
        database_url=database_config.url,
        database_url_source=database_config.source,
        database_url_error_code=database_config.error_code,
        database_url_error_message=database_config.error_message,
        object_store_root=args.object_store_root,
        basins_root=args.basins_root,
        env=env,
    )
    if preflight["status"] != "ready":
        _emit_json_summary(
            {
                "schema": INGEST_SUMMARY_SCHEMA,
                "status": "preflight_blocked",
                "return_code": PREFLIGHT_BLOCKED_RC,
                "ingest": _ingest_evidence(preflight),
                "object_store_root": args.object_store_root,
                "basins_root": args.basins_root,
                "sources": list(sources),
                "discovered_runs": 0,
                "basins": [],
                "seed": _empty_seed_summary(),
                "runs": _empty_runs_summary(),
            }
        )
        return PREFLIGHT_BLOCKED_RC

    object_store_root = Path(args.object_store_root)
    basins_root = Path(args.basins_root)
    database_url = database_config.url or ""
    WORK_ROOT = str(Path(env["AUTOPIPE_WORK_ROOT"]))

    env["OBJECT_STORE_ROOT"] = str(object_store_root)
    env.setdefault("OBJECT_STORE_PREFIX", os.environ.get("OBJECT_STORE_PREFIX", ""))
    env["DATABASE_URL"] = database_url
    object_store_prefix = env.get("OBJECT_STORE_PREFIX", "")

    runs = _discover_runs(object_store_root, sources)
    if args.only_cycle:
        if re.fullmatch(r"\d{10}", args.only_cycle) is None:
            parser.error("--only-cycle must use the exact UTC format YYYYMMDDHH")
        runs = [r for r in runs if r["cycle"] == args.only_cycle]
    if args.direct_grid_only:
        runs = [r for r in runs if r["run_family"] == "direct_grid"]
    if args.only_basin:
        only_basin_key = _slug_id(args.only_basin)
        runs = [r for r in runs if r["basin"] == only_basin_key]
    runs = [r for r in runs if r["basin"] not in excluded_basins]

    # ---- phase 1: seed any unseeded basin -------------------------------
    # Seed candidates come from BASINS_ROOT first so node-27 display metadata is
    # ready for every publishable basin before the first node-22 run lands. If a
    # run manifest already exists, it overrides the inventory-derived package
    # identity for that basin.
    seed_results: list[dict[str, Any]] = []
    seed_identities = _discover_seed_basin_identities(
        basins_root,
        only_basin=args.only_basin,
        object_store_prefix=object_store_prefix,
    )
    seed_identities = {
        basin_key: identity
        for basin_key, identity in seed_identities.items()
        if basin_key not in excluded_basins
    }
    for basin_key in sorted({r["basin"] for r in runs}):
        first_run = next(r["run_id"] for r in runs if r["basin"] == basin_key)
        try:
            run_identity = _basin_identity(object_store_root, first_run)
        except Exception as exc:  # noqa: BLE001 - record + continue, isolate failure
            if basin_key not in seed_identities:
                seed_results.append(
                    {
                        "basin": basin_key,
                        "outcome": "seed_failed",
                        "stage": "identity",
                        "error": redact_text(str(exc)),
                    }
                )
            continue
        inventory_identity = seed_identities.get(basin_key, {})
        seed_identities[basin_key] = {
            **inventory_identity,
            **run_identity,
            "basin_key": basin_key,
            "basin_slug": inventory_identity.get("basin_slug") or run_identity.get("basin_slug") or basin_key,
            "identity_source": "run_manifest",
        }
    basins = sorted(seed_identities)
    for basin in basins:
        identity = seed_identities[basin]
        basin_slug = identity.get("basin_slug") or basin
        if _basin_seeded(database_url, identity["basin_id"]):
            try:
                display_ready = _ensure_seeded_basin_display_ready(database_url, identity["model_id"])
            except Exception as exc:  # noqa: BLE001 - record + continue, isolate failure
                seed_results.append(
                    {
                        "basin": basin,
                        "basin_slug": basin_slug,
                        "outcome": "seed_failed",
                        "stage": "display_ready",
                        "basin_id": identity["basin_id"],
                        "model_id": identity["model_id"],
                        "identity_source": identity.get("identity_source"),
                        "error": redact_text(str(exc)),
                    }
                )
                continue
            seed_results.append(
                {
                    "basin": basin,
                    "basin_slug": basin_slug,
                    "outcome": "already_seeded",
                    "basin_id": identity["basin_id"],
                    **display_ready,
                    "identity_source": identity.get("identity_source"),
                }
            )
            continue
        if args.progress:
            print(
                f"[seed] {basin_slug} ({identity['model_id']} @ {identity['package_version']})",
                file=sys.stderr,
                flush=True,
            )
        result = _seed_basin(
            basin=basin_slug,
            identity=identity,
            database_url=database_url,
            basins_root=basins_root,
            env=env,
        )
        result["basin"] = basin
        result["basin_slug"] = basin_slug
        result["identity_source"] = identity.get("identity_source")
        seed_results.append(result)
        if args.progress:
            print(
                f"[seed] {basin_slug}: {result['outcome']}"
                + (f" ({result.get('stage')})" if result["outcome"] == "seed_failed" else ""),
                file=sys.stderr,
                flush=True,
            )

    seed_failed = [s for s in seed_results if s["outcome"] == "seed_failed"]
    seeded_basins = {s["basin"] for s in seed_results if s["outcome"] in ("seeded", "already_seeded")}

    # ---- phase 2: per-run ingest (skip runs whose basin failed to seed) -------
    run_results: list[dict[str, Any]] = []
    already_count = 0
    if not args.seed_only:
        runnable = [r for r in runs if r["basin"] in seeded_basins]
        done = (
            set()
            if args.force
            else _already_ingested_runs(
                database_url,
                [r["run_id"] for r in runnable],
                object_store_root=object_store_root,
            )
        )
        already_count = len([r for r in runnable if r["run_id"] in done])
        pending = [r for r in runnable if r["run_id"] not in done]
        if args.limit is not None:
            pending = pending[: args.limit]
        def process_pending(run: dict[str, str]) -> dict[str, Any]:
            try:
                return _process_run(
                    run["run_id"],
                    env,
                    object_store_root=object_store_root,
                    database_url=database_url,
                    object_store_prefix=object_store_prefix,
                )
            except Exception as exc:  # noqa: BLE001 - preserve batch failure isolation across workers
                return {
                    "run_id": run["run_id"],
                    "outcome": "failed",
                    "stage": "worker",
                    "error": redact_text(str(exc)),
                }

        if args.workers == 1:
            run_results = [process_pending(run) for run in pending]
        else:
            ordered_results: list[dict[str, Any] | None] = [None] * len(pending)
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=args.workers,
                thread_name_prefix="node27-ingest",
            ) as executor:
                futures = {
                    executor.submit(process_pending, run): (index, run)
                    for index, run in enumerate(pending)
                }
                completed = 0
                for future in concurrent.futures.as_completed(futures):
                    index, run = futures[future]
                    result = future.result()
                    ordered_results[index] = result
                    completed += 1
                    if args.progress:
                        tail = f" ({result.get('stage')})" if result["outcome"] != "ingested" else ""
                        print(
                            f"[{completed}/{len(pending)}] {run['run_id']}: {result['outcome']}{tail}",
                            file=sys.stderr,
                            flush=True,
                        )
            run_results = [result for result in ordered_results if result is not None]

        if args.progress and args.workers == 1:
            for idx, result in enumerate(run_results, start=1):
                tail = f" ({result.get('stage')})" if result["outcome"] != "ingested" else ""
                print(
                    f"[{idx}/{len(pending)}] {result['run_id']}: {result['outcome']}{tail}",
                    file=sys.stderr,
                    flush=True,
                )

    # ---- phase 3: advance fully-ingested runs to 'published' so the layer ----
    # catalog (discharge / q_down overlay) actually surfaces them. Idempotent;
    # also back-fills runs parsed by earlier ticks before this step existed.
    published_count = 0
    publish_eligible = already_count > 0 or any(result.get("outcome") == "ingested" for result in run_results)
    if not args.seed_only and publish_eligible:
        published_count = _publish_display_runs(database_url)
        if args.progress:
            print(f"[publish] advanced {published_count} run(s) parsed -> published",
                  file=sys.stderr, flush=True)

    def by(outcome: str) -> list[dict[str, Any]]:
        return [r for r in run_results if r["outcome"] == outcome]

    # ---- phase 3.5: refresh planner statistics on the chunks this tick wrote -
    # Deliberately keyed on runs ingested *by this tick* (not the publish
    # predicate, which also fires for runs ingested earlier): a tick that wrote
    # no rows moved no frontier and has nothing to refresh.
    stats_guard = _stats_guard(
        database_url,
        ingested_runs=0 if args.seed_only else len(by("ingested")),
        env=env,
    )
    authority_guard = stats_guard.get("authority") or {"status": "skipped", "analyzed": [], "deferred": []}
    # Printed whenever EITHER leg did or attempted something. Keying the line on
    # the frontier leg's status alone suppressed it on exactly the no-ingest
    # ticks where the repair leg does its work (issue #1468).
    legs_worked = any(
        leg["analyzed"] or leg["deferred"] or leg["status"] == "failed" for leg in (stats_guard, authority_guard)
    )
    if args.progress and legs_worked:
        # Per-status, never a bare count: ``analyzed`` records every attempt, so
        # an all-failed tick would otherwise read like a success.
        statuses = [entry["status"] for entry in stats_guard["analyzed"]]
        authority_statuses = [entry["status"] for entry in authority_guard["analyzed"]]
        # Both segments carry the SAME four counts plus their own leg status.
        # `warning` is not decoration: it is the PG15 non-owner silent skip
        # (see `_analyze_one_relation`), i.e. an ANALYZE that returned success
        # and refreshed nothing -- folding it into neither ok nor failed would
        # report that as work done. The leg status is printed because a
        # guard-level failure (connect / candidate query) leaves `analyzed`
        # empty, so all four counts read 0 on a leg that did nothing at all.
        print(
            f"[stats-guard] {stats_guard['status']}: ok {statuses.count('ok')}"
            f", warning {statuses.count('warning')}, failed {statuses.count('failed')}"
            f", deferred {len(stats_guard['deferred'])}"
            f", authority {authority_guard['status']}: ok {authority_statuses.count('ok')}"
            f", warning {authority_statuses.count('warning')}"
            f", failed {authority_statuses.count('failed')}"
            f", deferred {len(authority_guard['deferred'])}",
            file=sys.stderr,
            flush=True,
        )

    # ---- phase 3.6: terminal-state accounting (#1781) ------------------------
    declined_results = by("declined")
    declines_active = _active_decline_count(database_url)
    # `declines_active is None` (count read failed) must PRINT, not silence the
    # line (#1781): a long-standing backlog with no new decline this tick is
    # exactly the steady state this line exists to surface, and `None` is falsy
    # like `0`. Only "nothing declined AND zero standing records" stays quiet.
    if args.progress and (declined_results or declines_active is None or declines_active > 0):
        # Named run_ids, not a bare count: the whole point of a terminal state
        # is that nothing else in the tick will ever mention these runs again.
        print(
            f"[declines] this tick {len(declined_results)}"
            f" ({', '.join(r['run_id'] for r in declined_results) or 'none'})"
            f", active records {declines_active if declines_active is not None else 'unknown'}",
            file=sys.stderr,
            flush=True,
        )

    summary = {
        "schema": INGEST_SUMMARY_SCHEMA,
        "status": "completed",
        "return_code": 0,
        "ingest": _ingest_evidence(preflight),
        "object_store_root": str(object_store_root),
        "basins_root": str(basins_root),
        "sources": list(sources),
        "excluded_basins": sorted(excluded_basins),
        "discovered_runs": len(runs),
        "basins": basins,
        "basin_slugs": [seed_identities[basin].get("basin_slug") for basin in basins],
        "seed": {
            "seeded": [s["basin"] for s in seed_results if s["outcome"] == "seeded"],
            "already_seeded": [s["basin"] for s in seed_results if s["outcome"] == "already_seeded"],
            "failed": [{"basin": s["basin"], "stage": s.get("stage"), "error": s.get("error")} for s in seed_failed],
            "details": seed_results,
        },
        "runs": {
            "already_ingested": already_count,
            "published": published_count,
            "processed": len(run_results),
            "workers": args.workers,
            "ingested": len(by("ingested")),
            "skipped": len(by("skipped")),
            "failed": len(by("failed")),
            "declined": len(by("declined")),
            "ingested_by_source": {
                src: len([r for r in by("ingested") if r["run_id"].startswith(f"fcst_{src}_")]) for src in sources
            },
            "details": run_results,
            "skipped_runs": [{"run_id": r["run_id"], "reason": r.get("reason")} for r in by("skipped")],
            "failed_runs": [
                {"run_id": r["run_id"], "stage": r.get("stage"), "error": r.get("error")} for r in by("failed")
            ],
            "declined_runs": [
                {
                    "run_id": r["run_id"],
                    "reason_code": REASON_APPLY_COMPRESSED_CHUNK_BLOCKED,
                }
                for r in by("declined")
            ],
        },
        "stats_guard": stats_guard,
        # #1781: a terminal decline is silent by construction -- it makes the
        # tick green and then never speaks again. Carrying the live row count on
        # EVERY tick is what keeps a long-standing decline greppable.
        "declines_active": declines_active,
    }
    # stats_guard is deliberately absent from this expression: a statistics
    # refresh failure never turns a successful ingest tick red.
    rc = 0 if (not seed_failed and not by("failed")) else 1
    summary["status"] = "completed" if rc == 0 else "completed_with_failures"
    summary["return_code"] = rc
    _emit_json_summary(summary)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
