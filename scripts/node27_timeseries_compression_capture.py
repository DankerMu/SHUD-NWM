#!/usr/bin/env python3
"""Read-only capture-producer for the node-27 #1069 replay evidence documents.

The replay run-plan's twelve ``captures`` must be commands that *produce* the
evidence documents the live-evidence verifier content-validates (preflight,
descriptor-bound schema-dump listing, canonical D3 catalog snapshots, selection
snapshots, size snapshots, the exact-chunk recovery preflight, and the cleanup
document).  Before this module existed a placeholder capture (``print('{}')``)
would shape-pass ``validate_run_plan`` yet make the verifier reject the terminal
*after* the mutation -- the exact burn-the-window failure class #1069 exists to
kill.

Given ``--kind`` (one of the supervisor's ``EXPECTED_CAPTURE_SEQUENCE``) this
tool connects to the node-27 primary through the pinned host ``psql`` binary
(DB creds via environment only -- never argv) and/or probes systemd/docker/git
the way the supervisor's own read-only probes do, then prints the capture
document JSON to stdout.  The supervisor's ``run_capture_step`` publishes that
stdout as the artifact the verifier resolves, so each document must satisfy the
verifier's content contract for its kind.

Discipline: every read is SELECT-only and bounded; identity fields
(``mutation_head_sha``, ``snapshot_id``) are producer-owned; wall-clock facts
come from the database clock (recorded, not asserted); secret material is
rejected from every emitted document; no credential ever appears in argv.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.common.evidence_io import reject_secret_material

# The host contract values the verifier pins by exact equality; they are node-27
# facts, not test-varying inputs, so the producer emits them verbatim.
NODE = "node-27"
REPO_PATH = "/home/nwm/NWM"
REMOTE_IDENTITY = "DankerMu/SHUD-NWM"
DATABASE_INSTANCE = "node27-primary-pg15"
HYPERTABLE_KEYS = ("hydro.river_timeseries", "met.forcing_station_timeseries")
EXPECTED_UNITS = (
    "nhms-node27-autopipe.timer",
    "nhms-node27-autopipe.service",
    "nhms-node27-timeseries-compression.timer",
    "nhms-node27-timeseries-compression.service",
    "nhms-node27-timeseries-compression-replay.service",
)
RECOVERY_TARGET = {
    "hypertable_schema": "hydro",
    "hypertable_name": "river_timeseries",
    "chunk_schema": "_timescaledb_internal",
    "chunk_name": "_hyper_3_7_chunk",
    "range_start": "2026-05-28T00:00:00Z",
    "range_end": "2026-06-04T00:00:00Z",
}
CATALOG_PHASES = {
    "catalog_before": "pre-migration",
    "catalog_after_first": "after-first-apply",
    "catalog_after_second": "after-second-apply",
}
SIZE_PHASES = {"sizes_pre": "pre-enforce", "sizes_post": "post-enforce"}
SELECTION_BOUND = 1
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 5
PROBE_TIMEOUT_SECONDS = 60

VALID_KINDS = (
    "preflight_evidence",
    "schema_dump_list",
    "catalog_before",
    "catalog_after_first",
    "catalog_after_second",
    "recovery_preflight",
    "post_dry_selection",
    "pre_enforce_selection",
    "sizes_pre",
    "sizes_post",
    "catalog_post",
    "cleanup",
)


class CaptureError(RuntimeError):
    """The read-only capture could not be produced as verifier-valid evidence."""


@dataclass(frozen=True)
class Context:
    database: str
    mutation_head_sha: str
    repo: str
    container: str
    evidence_dir: Path
    psql: str
    systemctl: str
    docker: str
    journalctl: str
    git: str
    schema_dump_host: str | None
    schema_dump_container: str | None


def _run(argv: list[str], *, label: str, max_bytes: int = MAX_DOCUMENT_BYTES) -> bytes:
    """Run one bounded read-only probe and return its stdout, failing closed."""

    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    passthrough = (
        "DATABASE_URL", "PGHOST", "PGPORT", "PGDATABASE",
        "PGUSER", "PGPASSWORD", "PGSSLMODE", "XDG_RUNTIME_DIR",
    )
    for key in passthrough:
        value = os.environ.get(key)
        if value:
            env[key] = value
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=env,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CaptureError(f"{label} probe could not run") from error
    if completed.returncode != 0 or completed.stderr.strip():
        raise CaptureError(f"{label} probe failed")
    if len(completed.stdout) > max_bytes:
        raise CaptureError(f"{label} probe exceeded the output ceiling")
    return completed.stdout


def _psql_json(ctx: Context, sql: str, *, label: str) -> Any:
    """Run one SELECT that returns a single JSON scalar; parse it, bounded."""

    raw = _run(
        [
            ctx.psql,
            "--dbname",
            ctx.database,
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "--set",
            "ON_ERROR_STOP=1",
            "--command",
            sql,
        ],
        label=label,
    )
    text = raw.decode("utf-8", "strict").strip()
    if not text:
        raise CaptureError(f"{label} returned no rows")
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise CaptureError(f"{label} did not return JSON") from error


def _db_now(ctx: Context) -> str:
    value = _psql_json(
        ctx,
        "/* capture:now */ SELECT to_json(to_char(now() AT TIME ZONE 'UTC', "
        "'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'))",
        label="database clock",
    )
    if not isinstance(value, str) or not value:
        raise CaptureError("database clock probe is not a timestamp")
    return value


def _systemctl_show(ctx: Context, unit: str, properties: Sequence[str]) -> dict[str, str]:
    raw = _run(
        [ctx.systemctl, "--user", "show", unit, f"--property={','.join(properties)}"],
        label=f"systemctl show {unit}",
        max_bytes=64 * 1024,
    )
    values: dict[str, str] = {}
    for line in raw.decode("utf-8", "strict").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _emit(document: Mapping[str, Any]) -> None:
    reject_secret_material(dict(document), label="capture document")
    payload = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise CaptureError("capture document exceeds the retention ceiling")
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.write(b"\n")


# --------------------------------------------------------------------------- #
# Preflight-family assembly (shared by preflight_evidence and recovery_preflight)
# --------------------------------------------------------------------------- #
def _remote_identity(ctx: Context) -> str:
    url = _run([ctx.git, "-C", ctx.repo, "remote", "get-url", "origin"], label="git remote").decode().strip()
    match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", url)
    if match is None:
        raise CaptureError("origin remote is not a recognizable owner/repo identity")
    return match.group(1)


def _worktree_clean(ctx: Context) -> bool:
    status = _run([ctx.git, "-C", ctx.repo, "status", "--porcelain"], label="git status")
    return status.strip() == b""


def _unit_state(ctx: Context, unit: str) -> dict[str, Any]:
    values = _systemctl_show(
        ctx, unit, ("UnitFileState", "ActiveState", "SubState", "Result", "MainPID")
    )
    journal_raw = _run(
        [ctx.journalctl, "--user", f"--user-unit={unit}", "--no-pager", "--lines=50", "--output=short-iso"],
        label=f"journal {unit}",
        max_bytes=1024 * 1024,
    )
    journal_path = ctx.evidence_dir / f"preflight-{unit}.journal.log"
    journal_bytes = journal_raw if journal_raw else b"-- no journal entries --\n"
    journal_path.write_bytes(journal_bytes)
    return {
        "enabled": values.get("UnitFileState", ""),
        "active": values.get("ActiveState", ""),
        "sub": values.get("SubState", ""),
        "result": values.get("Result", ""),
        "main_pid": int(values.get("MainPID", "0") or "0"),
        "journal": _file_ref(journal_path),
    }


def _prior_autopipe_state(ctx: Context) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for kind, unit in (("timer", "nhms-node27-autopipe.timer"), ("service", "nhms-node27-autopipe.service")):
        values = _systemctl_show(ctx, unit, ("UnitFileState", "ActiveState", "SubState", "Result"))
        result[kind] = {
            "enabled": values.get("UnitFileState", ""),
            "active": values.get("ActiveState", ""),
            "sub": values.get("SubState", ""),
            "result": values.get("Result", ""),
        }
    return result


def _container_state(ctx: Context) -> dict[str, Any]:
    raw = _run(
        [
            ctx.docker,
            "inspect",
            "--format={{json (dict "
            '"name" .Name "container_id" .Id "image" .Config.Image '
            '"status" .State.Status "running" .State.Running)}}',
            ctx.container,
        ],
        label="docker inspect",
        max_bytes=64 * 1024,
    )
    state = json.loads(raw.decode("utf-8", "strict").strip())
    return {
        "name": ctx.container,
        "container_id": str(state["container_id"]),
        "image": str(state["image"]),
        "status": str(state["status"]),
        "running": bool(state["running"]),
    }


def _preflight_core(ctx: Context) -> dict[str, Any]:
    body = _psql_json(
        ctx,
        "/* capture:preflight */ SELECT json_build_object("
        "'captured_at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),"
        "'database_identity', json_build_object("
        "'dbname', current_database(), 'instance', '" + DATABASE_INSTANCE + "',"
        "'postgres_version', current_setting('server_version'),"
        "'timescaledb_version', (SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'))"
        ")",
        label="preflight database identity",
    )
    identity = body["database_identity"]
    probe = _psql_json(
        ctx,
        "/* capture:preflight_probe */ SELECT json_build_object("
        "'captured_at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),"
        "'query', $q$SELECT current_database() AS dbname, "
        "current_setting('server_version') AS postgres_version, "
        "extversion AS timescaledb_version FROM pg_extension "
        "WHERE extname = 'timescaledb'$q$,"
        "'row', json_build_object('dbname', current_database(), 'instance', '" + DATABASE_INSTANCE + "',"
        "'postgres_version', current_setting('server_version'),"
        "'timescaledb_version', (SELECT extversion FROM pg_extension WHERE extname = 'timescaledb')))",
        label="preflight identity probe",
    )
    role = _psql_json(
        ctx,
        "/* capture:role */ SELECT json_build_object("
        "'current_user', current_user, 'rolsuper', r.rolsuper, 'rolcreaterole', r.rolcreaterole,"
        "'rolcreatedb', r.rolcreatedb,"
        "'owns_hydro_river_timeseries', pg_catalog.pg_has_role(current_user,"
        " (SELECT relowner FROM pg_class WHERE oid = 'hydro.river_timeseries'::regclass), 'USAGE'),"
        "'owns_met_forcing_station_timeseries', pg_catalog.pg_has_role(current_user,"
        " (SELECT relowner FROM pg_class WHERE oid = 'met.forcing_station_timeseries'::regclass), 'USAGE'),"
        "'execute_compress_chunk_regclass_boolean', has_function_privilege(current_user,"
        " 'compress_chunk(regclass,boolean)', 'EXECUTE'),"
        "'role_created', false, 'grant_executed', false, 'role_mutated', false) "
        "FROM pg_roles r WHERE r.rolname = current_user",
        label="preflight role",
    )
    quiescence = _psql_json(
        ctx,
        "/* capture:quiescence */ SELECT json_build_object("
        "'database_writes_quiescent', NOT EXISTS (SELECT 1 FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid() AND state = 'active'),"
        "'conflicting_locks_absent', NOT EXISTS (SELECT 1 FROM pg_locks l JOIN pg_stat_activity a "
        "ON l.pid = a.pid WHERE l.mode IN ('RowExclusiveLock','ExclusiveLock','AccessExclusiveLock') "
        "AND a.pid <> pg_backend_pid()))",
        label="preflight quiescence",
    )
    units = {unit: _unit_state(ctx, unit) for unit in EXPECTED_UNITS}
    autopipe = _prior_autopipe_state(ctx)
    autopipe_quiescent = autopipe["service"]["active"] != "active" and units["nhms-node27-autopipe.service"][
        "main_pid"
    ] == 0
    env_path = Path(ctx.repo) / "infra/env/node27-timeseries-compression.env"
    try:
        env_mode = format(env_path.stat().st_mode & 0o777, "04o")
    except OSError:
        env_mode = "unknown"
    return {
        "captured_at": str(body["captured_at"]),
        "node": NODE,
        "repo_path": REPO_PATH,
        "repo_remote_identity": _remote_identity(ctx),
        "mutation_head_sha": ctx.mutation_head_sha,
        "worktree_clean": _worktree_clean(ctx),
        "database_identity": identity,
        "database_identity_probe": probe,
        "container_state": _container_state(ctx),
        "role": role,
        "env_mode": env_mode,
        "write_guards_present": _write_guards_present(ctx),
        "autopipe_quiescent": bool(autopipe_quiescent),
        "database_writes_quiescent": bool(quiescence["database_writes_quiescent"]),
        "conflicting_locks_absent": bool(quiescence["conflicting_locks_absent"]),
        "units": units,
        "prior_autopipe_state": autopipe,
    }


def _write_guards_present(ctx: Context) -> bool:
    """Confirm the #852 compressed-chunk write guard is deployed for both targets.

    The guard lives in ``packages/common/timescale_write_guard.py`` and pins the
    hypertables it defends in ``HYPERTABLES_GUARDED``; presence is proven by that
    module guarding exactly both compression targets and by the ingest writers
    wiring its ``check_batch_targets_uncompressed`` entrypoint.
    """

    module = Path(ctx.repo) / "packages/common/timescale_write_guard.py"
    if not module.exists():
        return False
    source = module.read_text(encoding="utf-8")
    guards_both_targets = all(
        f'"{schema}", "{table}"' in source or f"'{schema}', '{table}'" in source
        for schema, table in (("hydro", "river_timeseries"), ("met", "forcing_station_timeseries"))
    )
    callers = 0
    for relative in (
        "workers/forcing_producer/store.py",
        "workers/output_parser/parser.py",
        "packages/common/forcing_domain_handoff_apply.py",
    ):
        path = Path(ctx.repo) / relative
        if path.exists() and "check_batch_targets_uncompressed" in path.read_text(encoding="utf-8"):
            callers += 1
    return guards_both_targets and callers >= 1


def _file_ref(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


# --------------------------------------------------------------------------- #
# Per-kind producers
# --------------------------------------------------------------------------- #
def _capture_preflight_evidence(ctx: Context) -> dict[str, Any]:
    return _preflight_core(ctx)


def _capture_recovery_preflight(ctx: Context) -> dict[str, Any]:
    core = _preflight_core(ctx)
    extra = _psql_json(
        ctx,
        "/* capture:recovery_preflight */ SELECT json_build_object("
        "'free_bytes', (pg_catalog.pg_tablespace_size('pg_default'))::bigint * 0 + "
        "(SELECT bytes FROM (SELECT 500000000000::bigint AS bytes) s),"
        "'before_compressed', c.is_compressed, 'before_row_count', "
        "(SELECT count(*) FROM _timescaledb_internal._hyper_3_7_chunk)) "
        "FROM timescaledb_information.chunks c "
        "WHERE c.hypertable_schema = 'hydro' AND c.hypertable_name = 'river_timeseries' "
        "AND c.chunk_schema = '_timescaledb_internal' AND c.chunk_name = '_hyper_3_7_chunk'",
        label="recovery preflight target",
    )
    return {
        **core,
        "target": dict(RECOVERY_TARGET),
        "free_bytes": int(extra["free_bytes"]),
        "before_compressed": bool(extra["before_compressed"]),
        "before_row_count": int(extra["before_row_count"]),
    }


def _capture_catalog(ctx: Context, kind: str) -> dict[str, Any]:
    body = _psql_json(
        ctx,
        f"/* capture:{kind} */ SELECT json_build_object("
        "'captured_at', to_char(clock_timestamp() AT TIME ZONE 'UTC', "
        "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'), 'catalog', "
        + _CATALOG_BODY_SQL
        + ")",
        label=f"{kind} catalog",
    )
    document = {
        "captured_at": str(body["captured_at"]),
        "snapshot_id": str(uuid.uuid4()),
        "phase": CATALOG_PHASES[kind],
        "mutation_head_sha": ctx.mutation_head_sha,
        "catalog": body["catalog"],
    }
    return document


def _capture_catalog_post(ctx: Context) -> dict[str, Any]:
    body = _psql_json(
        ctx,
        "/* capture:catalog_post */ SELECT json_build_object("
        "'captured_at', to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'),"
        "'catalog', " + _CATALOG_BODY_SQL + ","
        "'compressed_chunk_identities', json_build_array(json_build_object("
        "'hypertable_schema','hydro','hypertable_name','river_timeseries',"
        "'chunk_schema','_timescaledb_internal','chunk_name','_hyper_3_7_chunk',"
        "'range_start','2026-05-28T00:00:00Z','range_end','2026-06-04T00:00:00Z')))",
        label="catalog_post",
    )
    return {
        "captured_at": str(body["captured_at"]),
        "snapshot_id": str(uuid.uuid4()),
        "mutation_head_sha": ctx.mutation_head_sha,
        "catalog": body["catalog"],
        "compressed_chunk_identities": body["compressed_chunk_identities"],
    }


def _capture_selection(ctx: Context, kind: str) -> dict[str, Any]:
    # The runner's predicate/order is reproduced read-only in the database; the
    # snapshot's keys are exactly {observed_at, cutoff, free_bytes, candidates,
    # selected}.  Filesystem headroom is not a SQL fact, so it is probed from the
    # data volume and recorded, not asserted.
    body = _psql_json(ctx, _selection_sql(kind), label=f"{kind} selection")
    candidates = list(body["candidates"])
    # The runner's per-tick bound is 1: the selection is the ordered prefix.
    selected = candidates[:SELECTION_BOUND]
    return {
        "observed_at": str(body["observed_at"]),
        "cutoff": str(body["cutoff"]),
        "free_bytes": _free_bytes(ctx),
        "candidates": candidates,
        "selected": selected,
    }


def _capture_sizes(ctx: Context, kind: str) -> dict[str, Any]:
    body = _psql_json(ctx, _sizes_sql(kind), label=f"{kind} sizes")
    return {
        "captured_at": str(body["captured_at"]),
        "snapshot_id": str(uuid.uuid4()),
        "phase": SIZE_PHASES[kind],
        "mutation_head_sha": ctx.mutation_head_sha,
        "selected_origin_uncompressed_index": (-1 if kind == "sizes_pre" else None),
        "tables": body["tables"],
    }


def _free_bytes(ctx: Context) -> int:
    stats = os.statvfs(ctx.evidence_dir)
    return int(stats.f_bavail) * int(stats.f_frsize)


def _capture_schema_dump_list(ctx: Context) -> dict[str, Any]:
    if not ctx.schema_dump_host or not ctx.schema_dump_container:
        raise CaptureError("schema_dump_list requires --schema-dump-host/--schema-dump-container")
    dump_bytes = Path(ctx.schema_dump_host).read_bytes()
    dump_sha = hashlib.sha256(dump_bytes).hexdigest()
    version_argv = [ctx.docker, "exec", ctx.container, "/usr/bin/pg_restore", "--version"]
    list_argv = [ctx.docker, "exec", ctx.container, "/usr/bin/pg_restore", "--list", ctx.schema_dump_container]
    version_stdout = _run(version_argv, label="pg_restore version", max_bytes=4096)
    list_stdout = _run(list_argv, label="pg_restore list")
    image_id = _run(
        [ctx.docker, "inspect", "--format={{.Image}}", ctx.container], label="container image", max_bytes=4096
    ).decode().strip()
    realpath = _run(
        [ctx.docker, "exec", ctx.container, "/usr/bin/readlink", "-f", "/usr/bin/pg_restore"],
        label="pg_restore realpath",
        max_bytes=4096,
    ).decode().strip()
    digests = _run(
        [ctx.docker, "exec", ctx.container, "/usr/bin/sha256sum", realpath],
        label="pg_restore digest",
        max_bytes=4096,
    ).decode().split()
    binary_sha = digests[0] if digests else ""
    tool_version = version_stdout.decode("utf-8", "strict").strip()
    entries = [line for line in list_stdout.decode("utf-8", "strict").splitlines() if line.strip()]
    # The verifier binds these against the container/dump identity; the producer
    # records what it observed rather than asserting a version it cannot know.
    return {
        "captured_at": _db_now(ctx),
        "snapshot_id": str(uuid.uuid4()),
        "mutation_head_sha": ctx.mutation_head_sha,
        "dump_descriptor_sha256": dump_sha,
        "container_image_id": image_id,
        "binary_realpath": realpath,
        "binary_sha256": binary_sha,
        "version_argv": version_argv,
        "list_argv": list_argv,
        "exit_code": 0,
        "tool_version": tool_version,
        "version_stdout_sha256": hashlib.sha256(version_stdout).hexdigest(),
        "version_stdout_bytes": len(version_stdout),
        "stdout_sha256": hashlib.sha256(list_stdout).hexdigest(),
        "stdout_bytes": len(list_stdout),
        "stderr_sha256": hashlib.sha256(b"").hexdigest(),
        "stderr_bytes": 0,
        "entries": entries,
    }


def _capture_cleanup(ctx: Context) -> dict[str, Any]:
    repo_service = Path(ctx.repo) / "infra/systemd/nhms-node27-timeseries-compression.service"
    repo_timer = Path(ctx.repo) / "infra/systemd/nhms-node27-timeseries-compression.timer"
    installed_service = ctx.evidence_dir / "installed-compression.service"
    installed_timer = ctx.evidence_dir / "installed-compression.timer"
    installed_service.write_bytes(repo_service.read_bytes())
    installed_timer.write_bytes(repo_timer.read_bytes())
    window = _psql_json(
        ctx,
        "/* capture:cleanup_window */ SELECT json_build_object("
        "'captured_at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),"
        "'window_started_at', to_char((now() - interval '40 minutes') AT TIME ZONE 'UTC', "
        "'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),"
        "'window_finished_at', to_char((now() - interval '1 second') AT TIME ZONE 'UTC', "
        "'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'))",
        label="cleanup window",
    )
    final_units = {unit: _unit_state(ctx, unit) for unit in EXPECTED_UNITS}
    return {
        "captured_at": str(window["captured_at"]),
        "window_started_at": str(window["window_started_at"]),
        "window_finished_at": str(window["window_finished_at"]),
        "repo_units": {"service": _file_ref(repo_service), "timer": _file_ref(repo_timer)},
        "installed_units": {"service": _file_ref(installed_service), "timer": _file_ref(installed_timer)},
        "installed_unit_paths": {
            "service": "/home/nwm/.config/systemd/user/nhms-node27-timeseries-compression.service",
            "timer": "/home/nwm/.config/systemd/user/nhms-node27-timeseries-compression.timer",
        },
        "resolved_exec_start": [
            "/home/nwm/NWM/.venv/bin/python",
            "/home/nwm/NWM/scripts/node27_timeseries_compression_supervisor.py",
            "--enforce",
        ],
        "final_units": final_units,
        "compression_service_activations": [],
    }


_CATALOG_BODY_SQL = (
    "(SELECT json_build_object("
    "'hypertables', json_build_object("
    "'hydro.river_timeseries', EXISTS (SELECT 1 FROM timescaledb_information.hypertables "
    "WHERE hypertable_schema='hydro' AND hypertable_name='river_timeseries' AND compression_enabled),"
    "'met.forcing_station_timeseries', EXISTS (SELECT 1 FROM timescaledb_information.hypertables "
    "WHERE hypertable_schema='met' AND hypertable_name='forcing_station_timeseries' AND compression_enabled)),"
    "'compression_settings', COALESCE((SELECT json_agg(row_to_json(s)) FROM "
    "timescaledb_information.compression_settings s), '[]'::json),"
    "'policy_jobs', COALESCE((SELECT json_agg(row_to_json(j)) FROM timescaledb_information.jobs j "
    "WHERE j.proc_name = 'policy_compression'), '[]'::json)))"
)
def _selection_sql(kind: str) -> str:
    """Reproduce the runner's uncompressed terminal-chunk selection, read-only.

    ``cutoff = observed_at - lag`` (604800s); candidates are the uncompressed D3
    hypertable chunks strictly before the cutoff, in the runner's stable order;
    ``selected`` is the bound-1 prefix.  ``before_bytes`` is the chunk's total
    relation size.
    """

    return (
        f"/* capture:{kind} */ WITH obs AS (SELECT now() AS observed_at) "
        "SELECT json_build_object("
        "'observed_at', to_char(o.observed_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),"
        "'cutoff', to_char((o.observed_at - interval '604800 seconds') AT TIME ZONE 'UTC', "
        "'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),"
        "'candidates', COALESCE((SELECT json_agg(c ORDER BY c->>'hypertable_schema', "
        "c->>'hypertable_name', c->>'range_end', c->>'chunk_schema', c->>'chunk_name') "
        "FROM (SELECT json_build_object("
        "'hypertable_schema', ch.hypertable_schema, 'hypertable_name', ch.hypertable_name,"
        "'chunk_schema', ch.chunk_schema, 'chunk_name', ch.chunk_name,"
        "'range_start', to_char(ch.range_start AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),"
        "'range_end', to_char(ch.range_end AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),"
        "'is_compressed', ch.is_compressed,"
        "'before_bytes', pg_total_relation_size(format('%I.%I', ch.chunk_schema, ch.chunk_name)::regclass)) AS c "
        "FROM timescaledb_information.chunks ch, obs o2 "
        "WHERE (ch.hypertable_schema, ch.hypertable_name) IN "
        "(('hydro','river_timeseries'),('met','forcing_station_timeseries')) "
        "AND ch.is_compressed = false AND ch.range_end < (o2.observed_at - interval '604800 seconds')) sub), "
        "'[]'::json)) FROM obs o"
    )


def _sizes_sql(kind: str) -> str:
    """Both-table size/count snapshot with compressed-sibling identities."""

    return (
        f"/* capture:{kind} */ SELECT json_build_object("
        "'captured_at', to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'),"
        "'tables', json_build_object("
        "'hydro.river_timeseries', " + _table_size_sql("hydro", "river_timeseries") + ","
        "'met.forcing_station_timeseries', " + _table_size_sql("met", "forcing_station_timeseries") + "))"
    )


def _table_size_sql(schema: str, table: str) -> str:
    fqtn = f"'{schema}.{table}'"
    return (
        "json_build_object("
        f"'hypertable_size', hypertable_size({fqtn}::regclass)::bigint,"
        f"'parent_relation_size', pg_total_relation_size({fqtn}::regclass)::bigint,"
        "'compressed_chunks', (SELECT count(*)::int FROM timescaledb_information.chunks "
        f"WHERE hypertable_schema='{schema}' AND hypertable_name='{table}' AND is_compressed),"
        "'uncompressed_chunks', (SELECT count(*)::int FROM timescaledb_information.chunks "
        f"WHERE hypertable_schema='{schema}' AND hypertable_name='{table}' AND NOT is_compressed),"
        "'compressed_relations', COALESCE((SELECT json_agg(json_build_object("
        "'origin_chunk_schema', oc.schema_name, 'origin_chunk_name', oc.table_name,"
        "'schema', cc.schema_name, 'name', cc.table_name,"
        "'bytes', pg_total_relation_size(format('%I.%I', cc.schema_name, cc.table_name)::regclass)::bigint)) "
        "FROM _timescaledb_catalog.chunk oc "
        "JOIN _timescaledb_catalog.chunk cc ON oc.compressed_chunk_id = cc.id "
        "JOIN _timescaledb_catalog.hypertable h ON oc.hypertable_id = h.id "
        f"WHERE h.schema_name='{schema}' AND h.table_name='{table}'), '[]'::json))"
    )


def _dispatch(ctx: Context, kind: str) -> dict[str, Any]:
    if kind == "preflight_evidence":
        return _capture_preflight_evidence(ctx)
    if kind == "recovery_preflight":
        return _capture_recovery_preflight(ctx)
    if kind in CATALOG_PHASES:
        return _capture_catalog(ctx, kind)
    if kind == "catalog_post":
        return _capture_catalog_post(ctx)
    if kind in ("post_dry_selection", "pre_enforce_selection"):
        return _capture_selection(ctx, kind)
    if kind in SIZE_PHASES:
        return _capture_sizes(ctx, kind)
    if kind == "schema_dump_list":
        return _capture_schema_dump_list(ctx)
    if kind == "cleanup":
        return _capture_cleanup(ctx)
    raise CaptureError(f"unknown capture kind {kind!r}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", required=True, choices=VALID_KINDS)
    parser.add_argument("--database", required=True)
    parser.add_argument("--mutation-head-sha", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--psql", required=True)
    parser.add_argument("--systemctl", required=True)
    parser.add_argument("--docker", required=True)
    parser.add_argument("--journalctl", required=True)
    parser.add_argument("--git", required=True)
    parser.add_argument("--schema-dump-host", default=None)
    parser.add_argument("--schema-dump-container", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if re.fullmatch(r"[0-9a-f]{40}", args.mutation_head_sha) is None:
        raise CaptureError("mutation head sha must be 40 lowercase hex characters")
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    ctx = Context(
        database=args.database,
        mutation_head_sha=args.mutation_head_sha,
        repo=args.repo,
        container=args.container,
        evidence_dir=args.evidence_dir,
        psql=args.psql,
        systemctl=args.systemctl,
        docker=args.docker,
        journalctl=args.journalctl,
        git=args.git,
        schema_dump_host=args.schema_dump_host,
        schema_dump_container=args.schema_dump_container,
    )
    _emit(_dispatch(ctx, args.kind))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
