#!/usr/bin/env python3
"""Node-27 bounded GFS/IFS source download runner.

This is the data-plane download sibling of ``node27_autopipeline.py``. It is
intentionally separate from display runtime config: display health does not
prove writer/download readiness, and display_readonly credentials must not be
used for source acquisition.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import psycopg2

from packages.common.redaction import redact_payload, redact_text
from workers.data_adapters.base import parse_cycle_time

DOWNLOAD_ROLE = "node27_data_plane_download"
DOWNLOAD_SUMMARY_SCHEMA = "nhms.node27_download.summary.v1"
DOWNLOAD_PREFLIGHT_SCHEMA = "nhms.node27_download.preflight.v1"
PREFLIGHT_BLOCKED_RC = 2
LOCK_BLOCKED_RC = 2
DEFAULT_ALLOWED_DB_ENDPOINTS = "127.0.0.1:55432,localhost:55432"
DATABASE_URL_ALLOWED_QUERY_KEYS = frozenset(
    {
        "application_name",
        "connect_timeout",
        "fallback_application_name",
        "sslmode",
    }
)
DATABASE_URL_QUERY_OVERRIDE_FORBIDDEN = "DATABASE_URL_QUERY_OVERRIDE_FORBIDDEN"
BBOX_ENV_KEYS = (
    "NHMS_DOWNLOAD_BBOX_SOUTH",
    "NHMS_DOWNLOAD_BBOX_NORTH",
    "NHMS_DOWNLOAD_BBOX_WEST",
    "NHMS_DOWNLOAD_BBOX_EAST",
)


def _preflight_blocker(code: str, env_var: str, message: str) -> dict[str, str]:
    return {"code": code, "env_var": env_var, "message": message}


def _database_username_class(username: str | None) -> str:
    normalized = (username or "").strip().lower()
    if not normalized:
        return "missing"
    if "display" in normalized or "readonly" in normalized or normalized.endswith("_ro") or normalized.endswith("ro"):
        return "display_readonly_like"
    return "writer_candidate"


def _database_port(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _database_query_blockers(query: str) -> list[dict[str, str]]:
    if not query:
        return []
    query_keys = {key.strip().lower() for key, _value in parse_qsl(query, keep_blank_values=True)}
    if query_keys and any(key not in DATABASE_URL_ALLOWED_QUERY_KEYS for key in query_keys):
        return [
            _preflight_blocker(
                DATABASE_URL_QUERY_OVERRIDE_FORBIDDEN,
                "DATABASE_URL",
                "DATABASE_URL query parameters must not override download target or credential source.",
            )
        ]
    return []


def _parse_allowed_endpoints(value: str | None) -> set[tuple[str, int]]:
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


def _database_preflight(database_url: str | None, env: dict[str, str]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    raw = (database_url or "").strip()
    if not raw:
        return {"configured": False}, [
            _preflight_blocker(
                "DATABASE_URL_MISSING",
                "DATABASE_URL",
                "DATABASE_URL is required for node-27 source downloads.",
            )
        ]

    try:
        parsed = urlsplit(raw)
    except ValueError:
        return {"configured": True}, [
            _preflight_blocker("DATABASE_URL_INVALID", "DATABASE_URL", "DATABASE_URL must be a PostgreSQL URL.")
        ]

    query_blockers = _database_query_blockers(parsed.query)
    try:
        dsn_parameters = psycopg2.extensions.parse_dsn(raw)
    except psycopg2.Error:
        if query_blockers:
            return {"configured": True, "scheme": parsed.scheme or None}, query_blockers
        return {"configured": True, "scheme": parsed.scheme or None}, [
            _preflight_blocker("DATABASE_URL_INVALID", "DATABASE_URL", "DATABASE_URL must be a PostgreSQL URL.")
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
    blockers = list(query_blockers)
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
                "DATABASE_URL must include PostgreSQL scheme, host, port, and database name.",
            )
        ]
    if username_class == "missing":
        blockers.append(
            _preflight_blocker(
                "DATABASE_URL_USERNAME_MISSING",
                "DATABASE_URL",
                "DATABASE_URL must include an explicit node-27 download writer username.",
            )
        )
    if username_class == "display_readonly_like":
        blockers.append(
            _preflight_blocker(
                "DATABASE_URL_READONLY_IDENTITY",
                "DATABASE_URL",
                "DATABASE_URL appears to use display/readonly identity, not a node-27 download writer.",
            )
        )
    if not password_present:
        blockers.append(
            _preflight_blocker(
                "DATABASE_URL_PASSWORD_MISSING",
                "DATABASE_URL",
                "DATABASE_URL must include explicit password material for the download writer username.",
            )
        )

    normalized_host = str(host or "").lower()
    if port == 55433 or normalized_host in {"10.0.2.100", "210.77.77.22"}:
        blockers.append(
            _preflight_blocker(
                "DATABASE_URL_NODE22_HISTORICAL_ENDPOINT",
                "DATABASE_URL",
                "node-27 source download must not use node-22 historical PostgreSQL.",
            )
        )
    allowed = _parse_allowed_endpoints(env.get("NODE27_DOWNLOAD_ALLOWED_DATABASE_ENDPOINTS"))
    if (normalized_host, int(port or -1)) not in allowed:
        blockers.append(
            _preflight_blocker(
                "DATABASE_URL_ENDPOINT_NOT_NODE27",
                "DATABASE_URL",
                "DATABASE_URL must target an allowed node-27 PostgreSQL endpoint.",
            )
        )
    return identity, blockers


def _path_preflight(env_var: str, raw_value: str | None) -> tuple[dict[str, Any], list[dict[str, str]]]:
    raw = (raw_value or "").strip()
    if not raw:
        return {"env_var": env_var, "configured": False}, [
            _preflight_blocker(f"{env_var}_MISSING", env_var, f"{env_var} is required for node-27 download.")
        ]
    path = Path(raw)
    evidence: dict[str, Any] = {"env_var": env_var, "configured": True, "path": str(path)}
    if not path.is_absolute():
        return evidence, [
            _preflight_blocker(f"{env_var}_UNSAFE", env_var, f"{env_var} must be an absolute non-root path.")
        ]
    if not path.is_dir():
        return evidence, [
            _preflight_blocker(f"{env_var}_NOT_DIRECTORY", env_var, f"{env_var} must point to an existing directory.")
        ]
    resolved = path.resolve()
    evidence["resolved_path"] = str(resolved)
    if resolved == Path("/"):
        return evidence, [
            _preflight_blocker(f"{env_var}_UNSAFE", env_var, f"{env_var} must not resolve to filesystem root.")
        ]
    return evidence, []


def _role_preflight(env: dict[str, str]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    download_role = (env.get("NHMS_NODE27_DOWNLOAD_ROLE") or "").strip().lower()
    service_role = (env.get("NHMS_SERVICE_ROLE") or "").strip().lower()
    evidence = {
        "role": DOWNLOAD_ROLE,
        "download_role_env": download_role or None,
        "service_role_env": service_role or None,
    }
    blockers: list[dict[str, str]] = []
    if not download_role:
        blockers.append(
            _preflight_blocker(
                "DOWNLOAD_ROLE_REQUIRED",
                "NHMS_NODE27_DOWNLOAD_ROLE",
                "NHMS_NODE27_DOWNLOAD_ROLE must be node27_data_plane_download.",
            )
        )
    if download_role and download_role != DOWNLOAD_ROLE:
        blockers.append(
            _preflight_blocker(
                "DOWNLOAD_ROLE_UNSUPPORTED",
                "NHMS_NODE27_DOWNLOAD_ROLE",
                "NHMS_NODE27_DOWNLOAD_ROLE must be node27_data_plane_download.",
            )
        )
    if service_role == "display_readonly" or download_role == "display_readonly":
        blockers.append(
            _preflight_blocker(
                "DOWNLOAD_DISPLAY_READONLY_ROLE_FORBIDDEN",
                "NHMS_SERVICE_ROLE",
                "display_readonly runtime config cannot satisfy node-27 download readiness.",
            )
        )
    return evidence, blockers


def _tool_path(tool: str, env: dict[str, str]) -> str | None:
    grib_root = (env.get("NHMS_GRIB_ENV_ROOT") or "").strip()
    if grib_root:
        candidate = Path(grib_root) / "bin" / tool
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(tool)


def _toolchain_preflight(env: dict[str, str]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    cdo_path = _tool_path("cdo", env)
    evidence = {
        "cdo": {"available": bool(cdo_path), "path": cdo_path},
        "nhms_grib_env_root": (env.get("NHMS_GRIB_ENV_ROOT") or "").strip() or None,
    }
    if cdo_path:
        return evidence, []
    return evidence, [
        _preflight_blocker(
            "GRIB_TOOL_CDO_MISSING",
            "PATH",
            "cdo must be available on PATH or under NHMS_GRIB_ENV_ROOT/bin.",
        )
    ]


def _bbox_preflight(env: dict[str, str]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    raw = {key: (env.get(key) or "").strip() for key in BBOX_ENV_KEYS}
    evidence: dict[str, Any] = {"configured": all(bool(value) for value in raw.values()), "values": raw}
    blockers: list[dict[str, str]] = []
    missing = [key for key, value in raw.items() if not value]
    if missing:
        for key in missing:
            blockers.append(_preflight_blocker(f"{key}_MISSING", key, f"{key} is required for node-27 download."))
        return evidence, blockers
    try:
        south = float(raw["NHMS_DOWNLOAD_BBOX_SOUTH"])
        north = float(raw["NHMS_DOWNLOAD_BBOX_NORTH"])
        west = float(raw["NHMS_DOWNLOAD_BBOX_WEST"])
        east = float(raw["NHMS_DOWNLOAD_BBOX_EAST"])
    except ValueError:
        return evidence, [
            _preflight_blocker("DOWNLOAD_BBOX_INVALID", "NHMS_DOWNLOAD_BBOX", "Download bbox values must be numeric.")
        ]
    evidence["numeric"] = {"south": south, "north": north, "west": west, "east": east}
    if south >= north or west >= east:
        blockers.append(
            _preflight_blocker(
                "DOWNLOAD_BBOX_INVALID",
                "NHMS_DOWNLOAD_BBOX",
                "Download bbox must satisfy south < north and west < east.",
            )
        )
    return evidence, blockers


def _cycle_hours_preflight(env: dict[str, str]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    raw = (env.get("NHMS_NODE27_DOWNLOAD_ALLOWED_CYCLE_HOURS_UTC") or "").strip()
    evidence: dict[str, Any] = {"configured": bool(raw), "raw": raw or None}
    if not raw:
        return evidence, [
            _preflight_blocker(
                "DOWNLOAD_CYCLE_HOURS_MISSING",
                "NHMS_NODE27_DOWNLOAD_ALLOWED_CYCLE_HOURS_UTC",
                "Allowed node-27 download cycle hours are required.",
            )
        ]
    try:
        hours = sorted({int(item.strip()) for item in raw.split(",") if item.strip()})
    except ValueError:
        return evidence, [
            _preflight_blocker(
                "DOWNLOAD_CYCLE_HOURS_INVALID",
                "NHMS_NODE27_DOWNLOAD_ALLOWED_CYCLE_HOURS_UTC",
                "Allowed node-27 download cycle hours must be comma-separated UTC hours.",
            )
        ]
    evidence["hours"] = hours
    if not hours or any(hour < 0 or hour > 23 for hour in hours):
        return evidence, [
            _preflight_blocker(
                "DOWNLOAD_CYCLE_HOURS_INVALID",
                "NHMS_NODE27_DOWNLOAD_ALLOWED_CYCLE_HOURS_UTC",
                "Allowed node-27 download cycle hours must be UTC hours in 0..23.",
            )
        ]
    return evidence, []


def _lock_preflight(raw_value: str | None) -> tuple[dict[str, Any], list[dict[str, str]]]:
    raw = (raw_value or "").strip()
    if not raw:
        return {"configured": False}, [
            _preflight_blocker(
                "NODE27_DOWNLOAD_LOCK_PATH_MISSING",
                "NODE27_DOWNLOAD_LOCK_PATH",
                "Lock path is required.",
            )
        ]
    path = Path(raw)
    evidence: dict[str, Any] = {"configured": True, "path": str(path)}
    if not path.is_absolute() or path == Path("/"):
        return evidence, [
            _preflight_blocker(
                "NODE27_DOWNLOAD_LOCK_PATH_UNSAFE",
                "NODE27_DOWNLOAD_LOCK_PATH",
                "Lock path must be absolute and non-root.",
            )
        ]
    parent = path.parent
    if not parent.is_dir():
        return evidence, [
            _preflight_blocker(
                "NODE27_DOWNLOAD_LOCK_PARENT_MISSING",
                "NODE27_DOWNLOAD_LOCK_PATH",
                "Lock path parent directory must exist.",
            )
        ]
    evidence["parent"] = str(parent.resolve())
    return evidence, []


def preflight_download_config(
    *,
    database_url: str | None,
    object_store_root: str | None,
    workspace_root: str | None,
    log_root: str | None,
    lock_path: str | None,
    env: dict[str, str],
) -> dict[str, Any]:
    blockers: list[dict[str, str]] = []
    role, role_blockers = _role_preflight(env)
    blockers.extend(role_blockers)

    database, database_blockers = _database_preflight(database_url, env)
    blockers.extend(database_blockers)

    object_store, object_store_blockers = _path_preflight("OBJECT_STORE_ROOT", object_store_root)
    blockers.extend(object_store_blockers)
    workspace, workspace_blockers = _path_preflight("WORKSPACE_ROOT", workspace_root)
    blockers.extend(workspace_blockers)
    logs, log_blockers = _path_preflight("NODE27_DOWNLOAD_LOG_ROOT", log_root)
    blockers.extend(log_blockers)

    lock, lock_blockers = _lock_preflight(lock_path)
    blockers.extend(lock_blockers)
    toolchain, toolchain_blockers = _toolchain_preflight(env)
    blockers.extend(toolchain_blockers)
    bbox, bbox_blockers = _bbox_preflight(env)
    blockers.extend(bbox_blockers)
    cycle_hours, cycle_hour_blockers = _cycle_hours_preflight(env)
    blockers.extend(cycle_hour_blockers)

    return redact_payload(
        {
            "schema": DOWNLOAD_PREFLIGHT_SCHEMA,
            "status": "blocked" if blockers else "ready",
            "role": role,
            "config_source": (env.get("NHMS_NODE27_DOWNLOAD_CONFIG_SOURCE") or "cli_or_environment"),
            "display_api_health_separate": True,
            "database": database,
            "paths": {
                "object_store_root": object_store,
                "workspace_root": workspace,
                "log_root": logs,
                "lock": lock,
            },
            "toolchain": toolchain,
            "bbox": bbox,
            "cycle_hours": cycle_hours,
            "blockers": blockers,
        }
    )


@contextmanager
def download_lock(lock_path: str) -> Iterator[bool]:
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _normalize_sources(values: Sequence[str] | None, env: dict[str, str]) -> tuple[str, ...]:
    raw_values: list[str] = []
    if values:
        raw_values.extend(values)
    else:
        raw_values.extend((env.get("NHMS_NODE27_DOWNLOAD_SOURCES") or "GFS,IFS").split(","))
    normalized: list[str] = []
    for raw in raw_values:
        source = raw.strip().upper()
        if not source:
            continue
        if source not in {"GFS", "IFS"}:
            raise ValueError(f"Unsupported source: {raw}")
        if source not in normalized:
            normalized.append(source)
    if not normalized:
        raise ValueError("At least one source is required.")
    return tuple(normalized)


def _download_command(source: str, cycle_time: str) -> list[str]:
    if source == "IFS":
        script = Path(sys.executable).with_name("nhms-ifs")
        executable = str(script) if script.exists() else "nhms-ifs"
        return [executable, "download", "--cycle-time", cycle_time]
    if source == "GFS":
        script = Path(sys.executable).with_name("nhms-gfs")
        executable = str(script) if script.exists() else "nhms-gfs"
        return [executable, "download", "--cycle-time", cycle_time]
    raise ValueError(f"Unsupported source: {source}")


def _bounded_text(value: str, *, limit: int = 4096) -> str:
    safe = redact_text(value)
    if len(safe) <= limit:
        return safe
    omitted = len(safe) - limit
    suffix = f"...[truncated {omitted} chars]"
    return f"{safe[: max(limit - len(suffix), 0)]}{suffix}"


@dataclass(frozen=True)
class SourceDownloadResult:
    source: str
    cycle_time: str
    status: str
    return_code: int
    command: list[str]
    result: dict[str, Any] | None
    stdout_tail: str
    stderr_tail: str

    def as_dict(self) -> dict[str, Any]:
        return redact_payload(
            {
                "source": self.source,
                "cycle_time": self.cycle_time,
                "status": self.status,
                "return_code": self.return_code,
                "command": self.command,
                "result": self.result,
                "stdout_tail": self.stdout_tail,
                "stderr_tail": self.stderr_tail,
            }
        )


def run_source_download(source: str, cycle_time: str, env: dict[str, str]) -> SourceDownloadResult:
    command = _download_command(source, cycle_time)
    timeout_seconds = int(env.get("NODE27_DOWNLOAD_COMMAND_TIMEOUT_SECONDS") or "21600")
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            env=env,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        return SourceDownloadResult(
            source=source,
            cycle_time=cycle_time,
            status="failed",
            return_code=124,
            command=command,
            result={"error_code": "DOWNLOAD_TIMEOUT"},
            stdout_tail=_bounded_text(error.stdout or ""),
            stderr_tail=_bounded_text(error.stderr or ""),
        )

    parsed_result: dict[str, Any] | None = None
    for line in reversed(completed.stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            parsed_result = candidate
            break
    status = "downloaded" if completed.returncode == 0 else "failed"
    return SourceDownloadResult(
        source=source,
        cycle_time=cycle_time,
        status=status,
        return_code=completed.returncode,
        command=command,
        result=parsed_result,
        stdout_tail=_bounded_text(completed.stdout),
        stderr_tail=_bounded_text(completed.stderr),
    )


def _emit_json_summary(summary: dict[str, Any]) -> None:
    json.dump(redact_payload(summary), sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _summary_path(env: dict[str, str], cli_value: str | None) -> Path | None:
    raw = (cli_value or env.get("NODE27_DOWNLOAD_SUMMARY_PATH") or "").strip()
    if not raw:
        return None
    return Path(raw)


def _write_summary_if_requested(summary: dict[str, Any], path: Path | None) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redact_payload(summary), ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _download_counts(details: list[dict[str, Any]]) -> dict[str, int]:
    failed = sum(1 for item in details if item.get("status") == "failed")
    downloaded = sum(1 for item in details if item.get("status") == "downloaded")
    return {"downloaded": downloaded, "failed": failed, "processed": len(details)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run bounded node-27 GFS/IFS source downloads.")
    parser.add_argument("--cycle-time", default=os.environ.get("NODE27_DOWNLOAD_CYCLE_TIME"))
    parser.add_argument("--source", action="append", dest="sources", help="Source to download: GFS or IFS.")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--object-store-root", default=os.environ.get("OBJECT_STORE_ROOT"))
    parser.add_argument("--workspace-root", default=os.environ.get("WORKSPACE_ROOT"))
    parser.add_argument("--log-root", default=os.environ.get("NODE27_DOWNLOAD_LOG_ROOT"))
    parser.add_argument("--lock-path", default=os.environ.get("NODE27_DOWNLOAD_LOCK_PATH"))
    parser.add_argument("--summary-path", default=None)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    preflight = preflight_download_config(
        database_url=args.database_url,
        object_store_root=args.object_store_root,
        workspace_root=args.workspace_root,
        log_root=args.log_root,
        lock_path=args.lock_path,
        env=env,
    )
    summary_file = _summary_path(env, args.summary_path)

    if preflight["status"] != "ready":
        summary = {
            "schema": DOWNLOAD_SUMMARY_SCHEMA,
            "status": "preflight_blocked",
            "return_code": PREFLIGHT_BLOCKED_RC,
            "role": DOWNLOAD_ROLE,
            "cycle_time": args.cycle_time,
            "sources": [],
            "preflight": preflight,
            "downloads": {"downloaded": 0, "failed": 0, "processed": 0, "details": []},
        }
        _write_summary_if_requested(summary, summary_file)
        _emit_json_summary(summary)
        return PREFLIGHT_BLOCKED_RC

    if not args.cycle_time:
        summary = {
            "schema": DOWNLOAD_SUMMARY_SCHEMA,
            "status": "preflight_blocked",
            "return_code": PREFLIGHT_BLOCKED_RC,
            "role": DOWNLOAD_ROLE,
            "cycle_time": None,
            "sources": [],
            "preflight": preflight,
            "downloads": {"downloaded": 0, "failed": 0, "processed": 0, "details": []},
            "blockers": [
                _preflight_blocker(
                    "NODE27_DOWNLOAD_CYCLE_TIME_MISSING",
                    "NODE27_DOWNLOAD_CYCLE_TIME",
                    "A cycle time is required for this bounded download runner.",
                )
            ],
        }
        _write_summary_if_requested(summary, summary_file)
        _emit_json_summary(summary)
        return PREFLIGHT_BLOCKED_RC

    try:
        parsed_cycle = parse_cycle_time(args.cycle_time)
        # Keep output canonical even if the caller used a space or offset.
        cycle_time = parsed_cycle.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        sources = _normalize_sources(args.sources, env)
    except ValueError as error:
        summary = {
            "schema": DOWNLOAD_SUMMARY_SCHEMA,
            "status": "preflight_blocked",
            "return_code": PREFLIGHT_BLOCKED_RC,
            "role": DOWNLOAD_ROLE,
            "cycle_time": args.cycle_time,
            "sources": [],
            "preflight": preflight,
            "downloads": {"downloaded": 0, "failed": 0, "processed": 0, "details": []},
            "blockers": [
                _preflight_blocker("NODE27_DOWNLOAD_ARGUMENT_INVALID", "argv", str(error)),
            ],
        }
        _write_summary_if_requested(summary, summary_file)
        _emit_json_summary(summary)
        return PREFLIGHT_BLOCKED_RC

    if args.preflight_only:
        summary = {
            "schema": DOWNLOAD_SUMMARY_SCHEMA,
            "status": "preflight_ready",
            "return_code": 0,
            "role": DOWNLOAD_ROLE,
            "cycle_time": cycle_time,
            "sources": list(sources),
            "preflight": preflight,
            "downloads": {"downloaded": 0, "failed": 0, "processed": 0, "details": []},
        }
        _write_summary_if_requested(summary, summary_file)
        _emit_json_summary(summary)
        return 0

    with download_lock(args.lock_path) as acquired:
        if not acquired:
            summary = {
                "schema": DOWNLOAD_SUMMARY_SCHEMA,
                "status": "lock_blocked",
                "return_code": LOCK_BLOCKED_RC,
                "role": DOWNLOAD_ROLE,
                "cycle_time": cycle_time,
                "sources": list(sources),
                "preflight": preflight,
                "downloads": {"downloaded": 0, "failed": 0, "processed": 0, "details": []},
                "blockers": [
                    _preflight_blocker(
                        "NODE27_DOWNLOAD_LOCK_HELD",
                        "NODE27_DOWNLOAD_LOCK_PATH",
                        "A previous node-27 download pass is still active.",
                    )
                ],
            }
            _write_summary_if_requested(summary, summary_file)
            _emit_json_summary(summary)
            return LOCK_BLOCKED_RC

        details = [run_source_download(source, cycle_time, env).as_dict() for source in sources]

    counts = _download_counts(details)
    return_code = 1 if counts["failed"] else 0
    status = "completed_with_failures" if counts["failed"] else "completed"
    summary = {
        "schema": DOWNLOAD_SUMMARY_SCHEMA,
        "status": status,
        "return_code": return_code,
        "role": DOWNLOAD_ROLE,
        "cycle_time": cycle_time,
        "sources": list(sources),
        "preflight": preflight,
        "downloads": {**counts, "details": details},
    }
    _write_summary_if_requested(summary, summary_file)
    _emit_json_summary(summary)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
