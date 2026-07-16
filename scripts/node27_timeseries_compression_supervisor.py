#!/usr/bin/env python3
"""Controlled producer for the node-27 issue #1069 qualifying replay.

The supervisor is the sole process owner for a v3 replay.  It validates an
immutable concrete run plan, writes an append-only JSONL ledger directly from
the child execution path, enforces one capture-wide wall deadline, and leaves
semantic acceptance to the pure live-evidence verifier.

This module intentionally contains no default plan that can mutate a database.
The reviewed live run supplies a descriptor-bound plan and explicit
``--enforce``.  Unit tests exercise the process and finalizer contracts with
local harmless children.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import time
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from packages.common import compression_terminal_state as terminal_state
from packages.common.evidence_io import (
    BoundedEvidenceError,
    FileIdentity,
    inspect_bounded_file_no_follow,
    read_bounded_json_no_follow,
    read_bounded_json_with_identity_no_follow,
    reject_secret_material,
)
from packages.common.safe_fs import atomic_write_bytes_no_follow

SCHEMA_VERSION = "3.0"
RUN_PLAN_VERSION = "1.0"
# The finalizer state is a durable file an untrusted party could rewrite
# between ExecStart and ExecStopPost, and its run_id is interpolated into a
# sibling marker filename.  This allowlist is a superset of the uuid4 the
# producer emits and excludes every path separator, NUL and traversal
# character, so the interpolation can only ever name one bounded, ordinary
# entry inside the state directory.
RUN_ID_PATTERN = r"[0-9A-Za-z._-]{1,64}"
EXPECTED_REPO = "/home/nwm/NWM"
EXPECTED_DATABASE = "nhms"
EXPECTED_CONTAINER = "nhms-db"
# MEASURED (Round-5 gate §G2): inside timescaledb-ha:pg15, `/usr/bin/pg_restore`
# is a symlink whose realpath is the pg_wrapper dispatcher (the stable entrypoint
# the child actually invokes), NOT `/usr/bin/pg_restore`.  Bind the wrapper and
# fail closed on any drift.
CONTAINER_PG_RESTORE_REALPATH = "/usr/share/postgresql-common/pg_wrapper"
EXPECTED_REVIEWED_REMOTE_REF = "refs/remotes/origin/feat/issue-1069-live-compression"
EXPECTED_REMOTE_IDENTITY = "DankerMu/SHUD-NWM"
MAX_LEDGER_BYTES = 16 * 1024**2
MAX_STREAM_BYTES = 8 * 1024**2
MAX_CATALOG_ROWS = 50_000
MAX_CATALOG_BYTES = 16 * 1024**2
MAX_CANDIDATES = 10_000
DEFAULT_WALL_SECONDS = 900.0
FINALIZER_LOCK_TIMEOUT_SECONDS = 5.0
TERM_GRACE_SECONDS = 3.0
POST_KILL_DRAIN_SECONDS = 0.25
FAILURE_RESERVE_SECONDS = TERM_GRACE_SECONDS + POST_KILL_DRAIN_SECONDS + FINALIZER_LOCK_TIMEOUT_SECONDS + 1.0
DEFAULT_RUN_PLAN = Path("/home/nwm/node27-timeseries-compression/run-plan.json")
DEFAULT_LEDGER = Path("/home/nwm/node27-timeseries-compression/supervisor-ledger.jsonl")
DEFAULT_RECEIPT = Path("/home/nwm/node27-timeseries-compression/terminal-evidence.json")
DEFAULT_FINALIZER_STATE = Path("/home/nwm/node27-timeseries-compression/finalizer-state.json")

EXPECTED_COUNTS: Mapping[str, int] = {
    "migration_apply": 2,
    "decompress": 1,
    "compression_dry_run": 1,
    "compression_enforce": 1,
    "compression_service_activation": 0,
    "retention": 0,
    "drill": 0,
    "role": 0,
    "node22": 0,
    "pg_dump": 1,
    "pg_restore_version": 1,
    "pg_restore_list": 1,
    "benchmark_before": 1,
    "benchmark_after": 1,
}
EXPECTED_COMMAND_SEQUENCE = (
    "pg_dump",
    "pg_restore_version",
    "pg_restore_list",
    "migration_apply",
    "migration_apply",
    "decompress",
    "compression_dry_run",
    "benchmark_before",
    "compression_enforce",
    "benchmark_after",
)
EXPECTED_CAPTURE_SEQUENCE = (
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
MUTATION_KINDS = frozenset({"migration_apply", "decompress", "compression_enforce"})
READ_ONLY_KINDS = frozenset(
    {
        "compression_dry_run",
        "pg_dump",
        "pg_restore_version",
        "pg_restore_list",
        "benchmark_before",
        "benchmark_after",
    }
)
AUTHORIZED_KINDS = MUTATION_KINDS | READ_ONLY_KINDS
CHECKPOINT_KINDS = (
    "preflight",
    "before_mutation",
    "after_mutation",
    "postflight",
    "cleanup",
)
FORBIDDEN_INHERITED_ENV = frozenset(
    {
        "PYTHONPATH",
        "PYTHONHOME",
        "NODE27_TIMESERIES_COMPRESSION_PYTHON",
        "NODE27_TIMESERIES_COMPRESSION_SCRIPT",
        "NODE27_TIMESERIES_COMPRESSION_REPO_ROOT",
    }
)
CHILD_ENV_ALLOWLIST = (
    "DATABASE_URL",
    "PGHOST",
    "PGPORT",
    "PGDATABASE",
    "PGUSER",
    "PGPASSWORD",
    "PGSSLMODE",
)
# The supervisor pins an absolute argv[0] for every probe it owns, so no PATH
# entry can substitute one.  Tests repoint this seam at a directory of stub
# binaries to execute the real probe code paths offline; production must always
# read /usr/bin, which `test_supervisor_bin_dir_defaults_to_the_pinned_system_path`
# asserts.  Only host-side binaries resolve through it -- paths that name a
# binary inside the DB container stay literal, because they are not this host's.
SUPERVISOR_BIN_DIR = Path("/usr/bin")
# `systemctl --user` locates the user manager through $XDG_RUNTIME_DIR; with it
# unset, bus_set_address_user() returns -ENOMEDIUM and the probe exits non-zero
# with "Failed to connect to bus", killing the first preflight checkpoint.
# Measured on node-27: forwarding XDG_RUNTIME_DIR alone is sufficient, so
# DBUS_SESSION_BUS_ADDRESS stays out and the authorized DB/compression children
# keep the maximally scrubbed CHILD_ENV_ALLOWLIST.  This applies only to the
# read-only probes the supervisor itself owns.
PROBE_ENV_ALLOWLIST = ("XDG_RUNTIME_DIR",)


class SupervisorError(RuntimeError):
    """The producer cannot safely execute or retain the replay."""


class HardWallExpired(SupervisorError):
    """The single supervisor hard wall expired."""


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _host_bin(name: str) -> str:
    """Resolve one host-side pinned binary through the injectable bin directory."""

    return str(SUPERVISOR_BIN_DIR / name)


def _child_environment() -> dict[str, str]:
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    environment.update({key: os.environ[key] for key in CHILD_ENV_ALLOWLIST if os.environ.get(key)})
    return environment


def _probe_environment() -> dict[str, str]:
    """Environment for supervisor-owned read-only probes.

    Adds only the systemd user-bus locator (`$XDG_RUNTIME_DIR`) on top of the
    scrubbed child environment, so `systemctl --user` can reach the user manager.
    The authorized DB/compression children keep `_child_environment` and never
    receive it.
    """

    environment = _child_environment()
    environment.update({key: os.environ[key] for key in PROBE_ENV_ALLOWLIST if os.environ.get(key)})
    return environment


@dataclass(frozen=True)
class HardWall:
    """One monotonic deadline established before any acquisition."""

    started_monotonic: float
    deadline_monotonic: float

    @classmethod
    def start(cls, seconds: float) -> HardWall:
        if seconds <= 0:
            raise ValueError("wall seconds must be positive")
        now = time.monotonic()
        return cls(now, now + seconds)

    def remaining(self, label: str) -> float:
        value = self.deadline_monotonic - time.monotonic()
        if value <= 0:
            raise HardWallExpired(f"hard wall expired before {label}")
        return value

    def bounded_milliseconds(self, configured_ms: int, label: str) -> int:
        return max(1, min(configured_ms, int(self.remaining(label) * 1000)))

    def reserving(self, seconds: float, label: str) -> HardWall:
        """Return a child wall that leaves time for terminal publication."""

        if seconds <= 0 or self.remaining(label) <= seconds:
            raise HardWallExpired(f"hard wall cannot reserve finalizer budget before {label}")
        return HardWall(self.started_monotonic, self.deadline_monotonic - seconds)


class AppendOnlyLedger:
    """Durable append-only producer ledger with unique event identifiers."""

    def __init__(self, path: Path, *, run_id: str, run_plan_id: str, invocation_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.run_plan_id = run_plan_id
        self.invocation_id = invocation_id
        self._ids: set[str] = set()
        self._bytes = 0
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        self._fd = os.open(path, flags, 0o600)

    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        event_id = str(event.get("event_id", ""))
        if not event_id or event_id in self._ids:
            raise SupervisorError("ledger event IDs must be non-empty and unique")
        record = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "run_plan_id": self.run_plan_id,
            "invocation_id": self.invocation_id,
            "supervisor_pid": os.getpid(),
            **dict(event),
        }
        reject_secret_material(record, label="supervisor ledger event")
        raw = _canonical(record)
        if self._bytes + len(raw) > MAX_LEDGER_BYTES:
            raise SupervisorError("supervisor ledger exceeds the byte ceiling")
        written = os.write(self._fd, raw)
        if written != len(raw):
            raise SupervisorError("short append to supervisor ledger")
        os.fsync(self._fd)
        self._bytes += written
        self._ids.add(event_id)
        return record

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> AppendOnlyLedger:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def _option_value(argv: Sequence[str], option: str, *, kind: str) -> str:
    if argv.count(option) != 1:
        raise SupervisorError(f"{kind} must contain exactly one {option}")
    index = argv.index(option)
    if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
        raise SupervisorError(f"{kind} {option} value is missing")
    return argv[index + 1]


def _assert_exact_argv(argv: list[str], *, kind: str, associations: Mapping[str, Any]) -> None:
    wrapper = f"{EXPECTED_REPO}/scripts/node27_timeseries_compression_once.sh"
    migration = f"{EXPECTED_REPO}/db/migrations/000047_hypertable_compression_settings.sql"
    if kind == "pg_dump":
        if set(associations) != {"schema_dump"}:
            raise SupervisorError("pg_dump output ownership differs")
        expected = [
            "/usr/bin/pg_dump",
            "--dbname",
            EXPECTED_DATABASE,
            "--format=custom",
            "--schema-only",
            "--file",
            str(associations["schema_dump"]),
        ]
        if argv != expected:
            raise SupervisorError("pg_dump argv differs")
    elif kind == "pg_restore_version":
        if associations or argv != [
            "/usr/bin/docker",
            "exec",
            EXPECTED_CONTAINER,
            "/usr/bin/pg_restore",
            "--version",
        ]:
            raise SupervisorError("pg_restore version argv differs")
    elif kind == "pg_restore_list":
        if (
            associations
            or argv[:5]
            != [
                "/usr/bin/docker",
                "exec",
                EXPECTED_CONTAINER,
                "/usr/bin/pg_restore",
                "--list",
            ]
            or len(argv) != 6
            or not argv[-1].startswith("/var/lib/postgresql/")
        ):
            raise SupervisorError("pg_restore list argv/output ownership differs")
    elif kind == "migration_apply":
        if associations or argv != [
            "/usr/bin/psql",
            "--dbname",
            EXPECTED_DATABASE,
            "--no-psqlrc",
            "--set",
            "ON_ERROR_STOP=1",
            "--file",
            migration,
        ]:
            raise SupervisorError("migration argv/output ownership differs")
    elif kind == "decompress":
        if set(associations) != {"recovery_receipt"}:
            raise SupervisorError("decompress argv/output ownership differs")
        expected_prefix = [
            f"{EXPECTED_REPO}/.venv/bin/python",
            f"{EXPECTED_REPO}/scripts/node27_timeseries_decompression_replay.py",
            "--database",
            EXPECTED_DATABASE,
            "--mutation-head-sha",
        ]
        target_args = [
            "--receipt-path",
            str(associations["recovery_receipt"]),
            "--hypertable-schema",
            "hydro",
            "--hypertable-name",
            "river_timeseries",
            "--chunk-schema",
            "_timescaledb_internal",
            "--chunk-name",
            "_hyper_3_7_chunk",
            "--range-start",
            "2026-05-28T00:00:00Z",
            "--range-end",
            "2026-06-04T00:00:00Z",
        ]
        if (
            set(associations) != {"recovery_receipt"}
            or argv[:5] != expected_prefix
            or len(argv) != 6 + len(target_args)
            or re.fullmatch(r"[0-9a-f]{40}", argv[5]) is None
            or argv[6:] != target_args
        ):
            raise SupervisorError("decompress argv/output ownership differs")
    elif kind in {"compression_dry_run", "compression_enforce"}:
        required = (
            {"dry_run_receipt"}
            if kind == "compression_dry_run"
            else {"enforce_receipt"}
        )
        prefix = [wrapper, *(["--enforce"] if kind == "compression_enforce" else [])]
        if set(associations) != required or argv[: len(prefix)] != prefix:
            raise SupervisorError(f"{kind} argv/output ownership differs")
        allowed = {"--receipt-path", "--lock-path"}
        option_tokens = argv[len(prefix) :: 2]
        if len(argv) != len(prefix) + 4 or set(option_tokens) != allowed:
            raise SupervisorError(f"{kind} option set differs")
        receipt_label = "dry_run_receipt" if kind == "compression_dry_run" else "enforce_receipt"
        if _option_value(argv, "--receipt-path", kind=kind) != associations[receipt_label]:
            raise SupervisorError(f"{kind} receipt path differs")
        if (
            _option_value(argv, "--lock-path", kind=kind)
            != "/home/nwm/node27-timeseries-compression-replay/compression.lock"
        ):
            raise SupervisorError(f"{kind} lock path differs")
    elif kind in {"benchmark_before", "benchmark_after"}:
        required = {"benchmark_before"} if kind == "benchmark_before" else {"benchmarks"}
        if set(associations) != required or argv[:2] != [
            f"{EXPECTED_REPO}/.venv/bin/python",
            f"{EXPECTED_REPO}/scripts/node27_timeseries_compression_benchmark.py",
        ]:
            raise SupervisorError(f"{kind} argv/output ownership differs")
        flags = argv[2::2]
        expected_flags = [
            "--phase",
            *(["--before-path"] if kind == "benchmark_after" else []),
            "--output",
            "--curve-basin-version-id",
            "--curve-river-segment-id",
            "--curve-river-network-version-id",
            "--curve-issue-time",
            "--curve-end-time",
            "--curve-scenario",
            "--mvt-run-id",
            "--mvt-basin-version-id",
            "--mvt-river-network-version-id",
            "--mvt-valid-time",
            "--mvt-z",
            "--mvt-x",
            "--mvt-y",
        ]
        if flags != expected_flags or len(argv) != 2 + 2 * len(expected_flags):
            raise SupervisorError(f"{kind} benchmark option order differs")
        phase = "before" if kind == "benchmark_before" else "after"
        output_label = "benchmark_before" if kind == "benchmark_before" else "benchmarks"
        if (
            _option_value(argv, "--phase", kind=kind) != phase
            or _option_value(argv, "--output", kind=kind) != associations[output_label]
        ):
            raise SupervisorError(f"{kind} phase/output differs")
        if (
            kind == "benchmark_after"
            and _option_value(argv, "--before-path", kind=kind)
            != "/home/nwm/node27-timeseries-compression-replay/benchmark-before.json"
        ):
            raise SupervisorError("benchmark_after before path differs")
    else:
        raise SupervisorError(f"{kind} has no canonical argv contract")


def _assert_concrete_argv(
    argv: Any, *, kind: str, associations: Mapping[str, Any] | None = None, exact: bool = False
) -> list[str]:
    if not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv):
        raise SupervisorError(f"{kind} argv must be a non-empty string array")
    forbidden = ("<", ">", "${", "$(", "{{", "}}", "*", "?")
    if any(not part or any(token in part for token in forbidden) for part in argv):
        raise SupervisorError(f"{kind} argv contains a placeholder or shell template")
    executable = Path(argv[0])
    if not executable.is_absolute():
        raise SupervisorError(f"{kind} executable must be an absolute path")
    normalized = list(argv)
    if exact:
        _assert_exact_argv(normalized, kind=kind, associations=associations or {})
    return normalized


def validate_run_plan(plan: Any, *, inherited_env: Mapping[str, str]) -> dict[str, Any]:
    """Validate exact cardinality, provenance and concrete commands before spawn."""

    if not isinstance(plan, Mapping):
        raise SupervisorError("run plan must be an object")
    required = {
        "plan_version",
        "run_plan_id",
        "mutation_head_sha",
        "reviewed_remote_ref",
        "database",
        "repo_path",
        "operator_attestation",
        "commands",
        "captures",
        "checkpoints",
    }
    if set(plan) != required:
        raise SupervisorError("run plan keys differ")
    if (
        plan["plan_version"] != RUN_PLAN_VERSION
        or plan["repo_path"] != EXPECTED_REPO
        or plan["database"] != EXPECTED_DATABASE
        or not isinstance(plan["run_plan_id"], str)
        or not plan["run_plan_id"]
        or not isinstance(plan["mutation_head_sha"], str)
        or len(plan["mutation_head_sha"]) != 40
        or re.fullmatch(r"[0-9a-f]{40}", plan["mutation_head_sha"]) is None
        or plan["reviewed_remote_ref"] != EXPECTED_REVIEWED_REMOTE_REF
    ):
        raise SupervisorError("run plan provenance differs")
    attestation = plan["operator_attestation"]
    if not isinstance(attestation, Mapping) or attestation != {
        "sole_db_user_during_window": True,
        "database_audit_proof": False,
        "trust_limit": "discrete observations; no absolute direct-SQL bypass proof",
    }:
        raise SupervisorError("sole-DB-user attestation/trust limit differs")
    inherited = sorted(key for key in FORBIDDEN_INHERITED_ENV if inherited_env.get(key))
    if inherited:
        raise SupervisorError("inherited runtime/path override is forbidden")
    commands = plan["commands"]
    if not isinstance(commands, list):
        raise SupervisorError("run plan commands must be an array")
    counts: Counter[str] = Counter()
    command_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for command in commands:
        if not isinstance(command, Mapping) or set(command) != {
            "command_id",
            "kind",
            "argv",
            "artifact_associations",
        }:
            raise SupervisorError("run plan command shape differs")
        command_id = command["command_id"]
        kind = command["kind"]
        if not isinstance(command_id, str) or not command_id or command_id in command_ids:
            raise SupervisorError("run plan command IDs must be unique")
        if kind not in AUTHORIZED_KINDS:
            raise SupervisorError("run plan contains an unauthorized command kind")
        associations = command["artifact_associations"]
        if not isinstance(associations, Mapping):
            raise SupervisorError("artifact associations must be an object")
        reject_secret_material(associations, label="run plan artifact associations")
        if any(
            not isinstance(label, str) or not label or not isinstance(path, str) or not Path(path).is_absolute()
            for label, path in associations.items()
        ):
            raise SupervisorError("run plan artifact outputs must be named absolute paths")
        argv = _assert_concrete_argv(command["argv"], kind=str(kind), associations=associations, exact=True)
        command_ids.add(command_id)
        counts[str(kind)] += 1
        normalized.append({**dict(command), "argv": argv})
    if any(counts.get(kind, 0) != count for kind, count in EXPECTED_COUNTS.items()):
        raise SupervisorError("run plan command cardinality differs")
    if tuple(item["kind"] for item in normalized) != EXPECTED_COMMAND_SEQUENCE:
        raise SupervisorError("run plan command order differs")
    decompression = next(item for item in normalized if item["kind"] == "decompress")
    if decompression["argv"][5] != plan["mutation_head_sha"]:
        raise SupervisorError("decompression producer mutation SHA differs")
    captures = plan["captures"]
    if not isinstance(captures, list):
        raise SupervisorError("run plan captures must be an array")
    capture_ids: set[str] = set()
    normalized_captures: list[dict[str, Any]] = []
    for capture in captures:
        if not isinstance(capture, Mapping) or set(capture) != {
            "capture_id",
            "kind",
            "argv",
            "output_path",
        }:
            raise SupervisorError("run plan capture shape differs")
        capture_id = capture["capture_id"]
        kind = capture["kind"]
        output_path = capture["output_path"]
        if (
            not isinstance(capture_id, str)
            or not capture_id
            or capture_id in capture_ids
            or kind not in EXPECTED_CAPTURE_SEQUENCE
            or not isinstance(output_path, str)
            or not Path(output_path).is_absolute()
        ):
            raise SupervisorError("run plan capture identity/output differs")
        argv = _assert_concrete_argv(capture["argv"], kind=f"capture {kind}")
        capture_ids.add(capture_id)
        normalized_captures.append({**dict(capture), "argv": argv})
    if tuple(item["kind"] for item in normalized_captures) != EXPECTED_CAPTURE_SEQUENCE:
        raise SupervisorError("run plan capture order/cardinality differs")
    owned_outputs: dict[str, str] = {}
    for command in normalized:
        for label, path in command["artifact_associations"].items():
            if label in owned_outputs or path in owned_outputs.values():
                raise SupervisorError("run plan child output ownership is not bijective")
            owned_outputs[str(label)] = str(path)
    for capture in normalized_captures:
        label = str(capture["kind"])
        path = str(capture["output_path"])
        if label in owned_outputs or path in owned_outputs.values():
            raise SupervisorError("run plan capture output ownership is not bijective")
        owned_outputs[label] = path
    if set(owned_outputs) != {
        "schema_dump",
        "recovery_receipt",
        "dry_run_receipt",
        "benchmark_before",
        "enforce_receipt",
        "benchmarks",
        *EXPECTED_CAPTURE_SEQUENCE,
    }:
        raise SupervisorError("run plan semantic output ownership differs")
    checkpoints = plan["checkpoints"]
    if not isinstance(checkpoints, list) or not checkpoints:
        raise SupervisorError("run plan checkpoints are missing")
    checkpoint_ids: set[str] = set()
    required_phases = {"preflight", "postflight", "cleanup"}
    mutation_ids = {item["command_id"] for item in normalized if item["kind"] in MUTATION_KINDS}
    seen_before: set[str] = set()
    seen_after: set[str] = set()
    for checkpoint in checkpoints:
        if not isinstance(checkpoint, Mapping) or set(checkpoint) != {
            "checkpoint_id",
            "phase",
            "command_id",
        }:
            raise SupervisorError("checkpoint shape differs")
        checkpoint_id = checkpoint["checkpoint_id"]
        phase = checkpoint["phase"]
        command_id = checkpoint["command_id"]
        if checkpoint_id in checkpoint_ids or phase not in CHECKPOINT_KINDS:
            raise SupervisorError("checkpoint ID/phase differs")
        checkpoint_ids.add(str(checkpoint_id))
        if phase in required_phases:
            if command_id is not None:
                raise SupervisorError("global checkpoint must not bind a command")
            required_phases.remove(str(phase))
        elif command_id not in mutation_ids:
            raise SupervisorError("mutation checkpoint binds an unknown command")
        elif phase == "before_mutation":
            seen_before.add(str(command_id))
        elif phase == "after_mutation":
            seen_after.add(str(command_id))
    if required_phases or seen_before != mutation_ids or seen_after != mutation_ids:
        raise SupervisorError("checkpoint/run-plan bijection differs")
    return {**dict(plan), "commands": normalized, "captures": normalized_captures}


def run_plan_id(plan: Mapping[str, Any]) -> str:
    """Hash a plan with its identity field blanked to avoid a self-hash paradox."""

    return hashlib.sha256(_canonical({**dict(plan), "run_plan_id": ""})).hexdigest()


def bounded_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    max_rows: int = MAX_CATALOG_ROWS,
    max_bytes: int = MAX_CATALOG_BYTES,
    max_candidates: int = MAX_CANDIDATES,
) -> list[dict[str, Any]]:
    """Materialize a stable catalog page under shared row/byte/candidate gates."""

    if len(rows) > min(max_rows, max_candidates):
        raise SupervisorError("catalog/candidate ceiling exceeded")
    result: list[dict[str, Any]] = []
    size = 2
    for row in rows:
        item = dict(row)
        reject_secret_material(item, label="catalog row")
        size += len(_canonical(item))
        if size > max_bytes:
            raise SupervisorError("catalog byte ceiling exceeded")
        result.append(item)
    return result


def _drain_child(
    process: subprocess.Popen[bytes],
    *,
    wall: HardWall,
    stdout_limit: int,
    stderr_limit: int,
    term_grace: float,
) -> tuple[bytes, bytes, bool, dict[str, int]]:
    selector = selectors.DefaultSelector()
    buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    dropped: dict[str, int] = {"stdout": 0, "stderr": 0}
    streams: list[tuple[str, BinaryIO | None, int]] = [
        ("stdout", process.stdout, stdout_limit),
        ("stderr", process.stderr, stderr_limit),
    ]
    for name, stream, _ in streams:
        if stream is not None:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
    terminated = False
    kill_at: float | None = None
    stop_drain_at: float | None = None

    def signal_group(sig: signal.Signals) -> None:
        # os.killpg on a group whose only member is a zombie returns EPERM on
        # Darwin, so catch OSError, not just ProcessLookupError.
        try:
            os.killpg(process.pid, sig)
        except OSError:
            pass

    while selector.get_map() or process.poll() is None:
        remaining = wall.deadline_monotonic - time.monotonic()
        if remaining <= 0 and not terminated:
            signal_group(signal.SIGTERM)
            terminated = True
            kill_at = time.monotonic() + term_grace
        if terminated and kill_at is not None and time.monotonic() >= kill_at:
            signal_group(signal.SIGKILL)
            stop_drain_at = time.monotonic() + POST_KILL_DRAIN_SECONDS
            kill_at = None
        if stop_drain_at is not None and time.monotonic() >= stop_drain_at:
            break
        timeout = 0.05
        if remaining > 0:
            timeout = min(timeout, remaining)
        for key, _ in selector.select(timeout=max(timeout, 0)):
            chunk = os.read(key.fd, 64 * 1024)
            if not chunk:
                selector.unregister(key.fileobj)
                continue
            name = str(key.data)
            limit = stdout_limit if name == "stdout" else stderr_limit
            target = buffers[name]
            if len(target) + len(chunk) > limit:
                if not terminated and process.poll() is None:
                    signal_group(signal.SIGTERM)
                    terminated = True
                    kill_at = time.monotonic() + term_grace
                allowed = max(0, limit - len(target))
                # Bytes over the ceiling are dropped whether or not the child is
                # still alive to be terminated; the dropped count is the truthful
                # truncation signal, independent of `terminated`.
                dropped[name] += len(chunk) - allowed
                target.extend(chunk[:allowed])
            else:
                target.extend(chunk)
        if process.poll() is not None and not selector.get_map():
            break
    for key in list(selector.get_map().values()):
        try:
            selector.unregister(key.fileobj)
        except (KeyError, ValueError):
            pass
        try:
            key.fileobj.close()
        except OSError:
            pass
    selector.close()
    try:
        process.wait(timeout=max(POST_KILL_DRAIN_SECONDS, 0.1))
    except subprocess.TimeoutExpired:
        signal_group(signal.SIGKILL)
        process.wait(timeout=max(term_grace, 0.1))
    return bytes(buffers["stdout"]), bytes(buffers["stderr"]), terminated, dropped


def run_child(
    command: Mapping[str, Any],
    *,
    wall: HardWall,
    ledger: AppendOnlyLedger,
    mutation_head_sha: str,
    database: str,
    stdout_limit: int = MAX_STREAM_BYTES,
    stderr_limit: int = MAX_STREAM_BYTES,
    term_grace: float = TERM_GRACE_SECONDS,
    artifact_dir: Path | None = None,
    observed_associations: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Spawn, stream-bound, terminate/reap and ledger one authorized child."""

    kind = str(command["kind"])
    argv = _assert_concrete_argv(command["argv"], kind=kind)
    for label, raw_path in command["artifact_associations"].items():
        try:
            os.lstat(Path(str(raw_path)))
        except FileNotFoundError:
            continue
        raise SupervisorError(f"{kind} output {label} exists before spawn")
    wall.remaining(f"spawn {kind}")
    started_utc = _utc_now()
    started_mono = time.monotonic()
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=_child_environment(),
    )
    stdout, stderr, terminated, dropped = _drain_child(
        process,
        wall=wall,
        stdout_limit=stdout_limit,
        stderr_limit=stderr_limit,
        term_grace=term_grace,
    )
    finished_mono = time.monotonic()
    finished_utc = _utc_now()
    stdout_identity: dict[str, Any] = {
        "bytes": len(stdout),
        "sha256": hashlib.sha256(stdout).hexdigest(),
        "truncated": dropped["stdout"] > 0,
    }
    stderr_identity: dict[str, Any] = {
        "bytes": len(stderr),
        "sha256": hashlib.sha256(stderr).hexdigest(),
        "truncated": dropped["stderr"] > 0,
    }
    if artifact_dir is not None:
        safe_id = str(command["command_id"]).replace("/", "-")
        stdout_identity["artifact"] = _observed_ref(artifact_dir / f"{safe_id}-stdout.bin", stdout)
        stderr_identity["artifact"] = _observed_ref(artifact_dir / f"{safe_id}-stderr.bin", stderr)
    complete_associations = dict(observed_associations or {})
    for label, raw_path in command["artifact_associations"].items():
        path = Path(str(raw_path))
        try:
            identity = inspect_bounded_file_no_follow(
                path,
                max_bytes=MAX_CATALOG_BYTES,
                label=f"{kind} produced artifact {label}",
            )
        except BoundedEvidenceError:
            if not terminated and process.returncode == 0:
                raise
            complete_associations[str(label)] = {"path": str(path), "missing": True}
            continue
        complete_associations[str(label)] = {
            "artifact": {"path": str(path), "sha256": identity.sha256, "bytes": identity.size},
            "device": identity.device,
            "inode": identity.inode,
        }
    event = ledger.append(
        {
            "event_id": str(uuid.uuid4()),
            "event_type": "child_exit",
            "command_id": command["command_id"],
            "kind": kind,
            "argv": argv,
            "pid": process.pid,
            "started_at": started_utc,
            "finished_at": finished_utc,
            "started_monotonic": started_mono,
            "finished_monotonic": finished_mono,
            "exit_code": process.returncode,
            "terminated_by_supervisor": terminated,
            "possible_mutation": bool(kind in MUTATION_KINDS and (terminated or process.returncode)),
            "stdout": stdout_identity,
            "stderr": stderr_identity,
            "mutation_head_sha": mutation_head_sha,
            "database": database,
            "artifact_associations": complete_associations,
        }
    )
    if terminated or dropped["stdout"] or dropped["stderr"]:
        raise HardWallExpired(f"{kind} exceeded the hard wall or output ceiling")
    if process.returncode != 0:
        raise SupervisorError(f"{kind} exited non-zero")
    return event


def run_capture_step(
    capture: Mapping[str, Any],
    *,
    wall: HardWall,
    ledger: AppendOnlyLedger,
    artifact_dir: Path,
    stdout_limit: int = MAX_CATALOG_BYTES,
    stderr_limit: int = MAX_STREAM_BYTES,
    term_grace: float = TERM_GRACE_SECONDS,
) -> dict[str, Any]:
    """Run one supervisor-owned probe and atomically publish only its stdout."""

    capture_id = str(capture["capture_id"])
    kind = str(capture["kind"])
    output_path = Path(str(capture["output_path"]))
    argv = _assert_concrete_argv(capture["argv"], kind=f"capture {kind}")
    try:
        os.lstat(output_path)
    except FileNotFoundError:
        pass
    else:
        raise SupervisorError(f"capture {kind} output exists before its owner")
    wall.remaining(f"spawn capture {kind}")
    started_at = _utc_now()
    started_monotonic = time.monotonic()
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=_probe_environment(),
    )
    stdout, stderr, terminated, dropped = _drain_child(
        process,
        wall=wall,
        stdout_limit=stdout_limit,
        stderr_limit=stderr_limit,
        term_grace=term_grace,
    )
    finished_monotonic = time.monotonic()
    finished_at = _utc_now()
    if terminated or dropped["stdout"] or dropped["stderr"]:
        raise HardWallExpired(f"capture {kind} exceeded the hard wall or output ceiling")
    if process.returncode != 0 or stderr.strip() or not stdout:
        raise SupervisorError(f"capture {kind} probe failed")
    artifact = _observed_ref(output_path, stdout)
    safe_id = capture_id.replace("/", "-")
    event = ledger.append(
        {
            "event_id": str(uuid.uuid4()),
            "event_type": "capture",
            "capture_id": capture_id,
            "kind": kind,
            "argv": argv,
            "pid": process.pid,
            "started_at": started_at,
            "finished_at": finished_at,
            "started_monotonic": started_monotonic,
            "finished_monotonic": finished_monotonic,
            "exit_code": process.returncode,
            "terminated_by_supervisor": False,
            "stdout": {
                "bytes": len(stdout),
                "sha256": hashlib.sha256(stdout).hexdigest(),
                "truncated": dropped["stdout"] > 0,
                "artifact": _observed_ref(artifact_dir / f"{safe_id}-stdout.bin", stdout),
            },
            "stderr": {
                "bytes": len(stderr),
                "sha256": hashlib.sha256(stderr).hexdigest(),
                "truncated": dropped["stderr"] > 0,
                "artifact": _observed_ref(artifact_dir / f"{safe_id}-stderr.bin", stderr),
            },
            "artifact_association": artifact,
        }
    )
    return event


def _run_capture_argv(argv: list[str], *, wall: HardWall, label: str, max_bytes: int = 1024**2) -> bytes:
    """Run one fixed read-only checkpoint probe under the supervisor hard wall."""

    wall.remaining(f"{label} spawn")
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=_probe_environment(),
    )
    stdout, stderr, terminated, dropped = _drain_child(
        process,
        wall=wall,
        stdout_limit=max_bytes,
        stderr_limit=max_bytes,
        term_grace=TERM_GRACE_SECONDS,
    )
    if dropped["stdout"] or dropped["stderr"] or terminated or process.returncode != 0:
        raise SupervisorError(f"{label} checkpoint probe failed")
    if stderr.strip():
        raise SupervisorError(f"{label} checkpoint probe wrote stderr")
    return stdout


def resolve_container_pg_restore_identity(*, wall: HardWall, dump_path: str) -> dict[str, str]:
    """Resolve the actual container image, binary and mounted dump identities."""

    if not dump_path.startswith("/var/lib/postgresql/"):
        raise SupervisorError("pg_restore dump path is outside the DB container data mount")
    image_id = (
        _run_capture_argv(
            [_host_bin("docker"), "inspect", "--format={{.Image}}", EXPECTED_CONTAINER],
            wall=wall,
            label="DB container image identity",
            max_bytes=4096,
        )
        .decode()
        .strip()
    )
    realpath = (
        _run_capture_argv(
            [
                _host_bin("docker"),
                "exec",
                EXPECTED_CONTAINER,
                "/usr/bin/readlink",
                "-f",
                "/usr/bin/pg_restore",
            ],
            wall=wall,
            label="container pg_restore realpath",
            max_bytes=4096,
        )
        .decode()
        .strip()
    )
    hashes = (
        _run_capture_argv(
            [
                _host_bin("docker"),
                "exec",
                EXPECTED_CONTAINER,
                "/usr/bin/sha256sum",
                realpath,
                dump_path,
            ],
            wall=wall,
            label="container pg_restore/dump digests",
            max_bytes=4096,
        )
        .decode()
        .splitlines()
    )
    if (
        not image_id.startswith("sha256:")
        or realpath != CONTAINER_PG_RESTORE_REALPATH
        or len(hashes) != 2
    ):
        raise SupervisorError("container pg_restore identity differs")
    binary_sha256 = hashes[0].split()[0]
    dump_sha256 = hashes[1].split()[0]
    if any(len(value) != 64 for value in (binary_sha256, dump_sha256)):
        raise SupervisorError("container pg_restore/dump digest is invalid")
    return {
        "dump_sha256": dump_sha256,
        "container_image_id": image_id,
        "binary_realpath": realpath,
        "binary_sha256": binary_sha256,
    }


def _artifact_ref(path: Path, raw: bytes) -> dict[str, Any]:
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    else:
        raise SupervisorError("checkpoint artifact path already exists")
    atomic_write_bytes_no_follow(path, raw, mode=0o600)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _observed_ref(path: Path, raw: bytes) -> dict[str, Any]:
    ref = _artifact_ref(path, raw)
    identity = inspect_bounded_file_no_follow(path, max_bytes=max(len(raw), 1), label="checkpoint observation")
    return {"artifact": ref, "device": identity.device, "inode": identity.inode}


def _governed_user_unit(row: Mapping[str, Any]) -> str | None:
    governed = {
        "nhms-node27-timeseries-compression.service",
        "nhms-node27-timeseries-compression-replay.service",
    }
    user_fields_present = "_SYSTEMD_USER_UNIT" in row or "USER_UNIT" in row
    user_units = {
        str(row[key])
        for key in ("_SYSTEMD_USER_UNIT", "USER_UNIT")
        if key in row and str(row[key]) in governed
    }
    if len(user_units) > 1:
        raise SupervisorError("checkpoint journal user-unit fields conflict")
    if user_units:
        return next(iter(user_units))
    if user_fields_present:
        return None
    fallback_units = {
        str(row[key])
        for key in ("_SYSTEMD_UNIT", "UNIT")
        if key in row and str(row[key]) in governed
    }
    if len(fallback_units) > 1:
        raise SupervisorError("checkpoint journal fallback unit fields conflict")
    return next(iter(fallback_units)) if fallback_units else None


def _verify_checkout_lineage(plan: Mapping[str, Any], *, wall: HardWall) -> None:
    def git(*args: str) -> str:
        raw = _run_capture_argv(
            [_host_bin("git"), "-C", EXPECTED_REPO, *args],
            wall=wall,
            label=f"Git lineage {' '.join(args)}",
            max_bytes=MAX_STREAM_BYTES,
        )
        return raw.decode("utf-8").strip()

    try:
        status = git("status", "--porcelain=v1", "--untracked-files=normal")
        head = git("rev-parse", "HEAD")
        reviewed = git("rev-parse", str(plan["reviewed_remote_ref"]))
        remote = git("remote", "get-url", "origin")
    except (OSError, UnicodeDecodeError, SupervisorError) as error:
        raise SupervisorError("Git lineage probe failed") from error
    normalized_remote = re.sub(r"(?:\.git)?$", "", remote).removeprefix("https://github.com/")
    normalized_remote = normalized_remote.removeprefix("git@github.com:")
    if (
        status
        or head != plan["mutation_head_sha"]
        or reviewed != plan["mutation_head_sha"]
        or normalized_remote != EXPECTED_REMOTE_IDENTITY
    ):
        raise SupervisorError("clean checkout/origin/reviewed SHA lineage differs")


def capture_checkpoint(
    checkpoint: Mapping[str, Any],
    *,
    wall: HardWall,
    ledger: AppendOnlyLedger,
    artifact_dir: Path,
    journal_cursor: str,
    invocation_id: str,
) -> str:
    """Capture DB activity/locks plus canonical user-unit show/journal truth."""

    checkpoint_id = str(checkpoint["checkpoint_id"])
    safe_id = checkpoint_id.replace("/", "-")
    activity_sql = (
        "SELECT json_build_object('sessions',COALESCE(json_agg(s ORDER BY pid),'[]'::json)) "
        "FROM (SELECT pid,state,wait_event_type FROM pg_stat_activity "
        "WHERE datname=current_database() AND pid<>pg_backend_pid() "
        "AND state<>'idle') s"
    )
    lock_sql = (
        "SELECT json_build_object('conflicts',COALESCE(json_agg(l ORDER BY pid),'[]'::json)) "
        "FROM (SELECT pid,locktype,mode,granted FROM pg_locks WHERE NOT granted) l"
    )
    catalog_sql = (
        "SELECT json_build_object("
        "'hypertables',(SELECT json_object_agg(format('%s.%s',hypertable_schema,hypertable_name),compression_enabled) "
        "FROM timescaledb_information.hypertables WHERE (hypertable_schema,hypertable_name) IN "
        "(('hydro','river_timeseries'),('met','forcing_station_timeseries'))),"
        "'compression_settings',(SELECT COALESCE(json_agg(row_to_json(s) ORDER BY hypertable_schema,hypertable_name,"
        "segmentby_column_index NULLS LAST,orderby_column_index NULLS LAST),'[]'::json) FROM "
        "timescaledb_information.compression_settings s WHERE (hypertable_schema,hypertable_name) IN "
        "(('hydro','river_timeseries'),('met','forcing_station_timeseries'))),"
        "'policy_jobs',(SELECT COALESCE(json_agg(row_to_json(j)),'[]'::json) FROM "
        "timescaledb_information.jobs j WHERE proc_name='policy_compression' AND "
        "(hypertable_schema,hypertable_name) IN "
        "(('hydro','river_timeseries'),('met','forcing_station_timeseries'))))"
    )
    psql_prefix = [
        _host_bin("psql"),
        "--dbname",
        EXPECTED_DATABASE,
        "--no-psqlrc",
        "--tuples-only",
        "--no-align",
        "--set",
        "ON_ERROR_STOP=1",
        "--command",
    ]
    activity_raw = _run_capture_argv([*psql_prefix, activity_sql], wall=wall, label="database activity").strip() + b"\n"
    locks_raw = _run_capture_argv([*psql_prefix, lock_sql], wall=wall, label="relation locks").strip() + b"\n"
    catalog_raw = _run_capture_argv([*psql_prefix, catalog_sql], wall=wall, label="D3 catalog").strip() + b"\n"
    try:
        if json.loads(activity_raw) != {"sessions": []}:
            raise SupervisorError("checkpoint observed a conflicting database session")
        if json.loads(locks_raw) != {"conflicts": []}:
            raise SupervisorError("checkpoint observed a conflicting relation lock")
        validate_current_d3(json.loads(catalog_raw))
    except json.JSONDecodeError as error:
        raise SupervisorError("checkpoint database probe did not return JSON") from error

    def unit_show(unit: str) -> dict[str, Any]:
        raw = _run_capture_argv(
            [
                _host_bin("systemctl"),
                "--user",
                "show",
                unit,
                "--property=FragmentPath,ActiveState,SubState,MainPID,InvocationID,ExecMainStartTimestamp,ExecMainStartTimestampMonotonic",
            ],
            wall=wall,
            label=f"systemd show {unit}",
        )
        values = dict(line.decode().split("=", 1) for line in raw.splitlines() if b"=" in line)
        return {
            "FragmentPath": values.get("FragmentPath"),
            "ActiveState": values.get("ActiveState"),
            "SubState": values.get("SubState"),
            "MainPID": int(values.get("MainPID", "-1")),
            "InvocationID": values.get("InvocationID", ""),
            "ExecMainStartTimestamp": values.get("ExecMainStartTimestamp", ""),
            "ExecMainStartTimestampMonotonic": int(values.get("ExecMainStartTimestampMonotonic", "0")),
        }

    recurring_show = unit_show("nhms-node27-timeseries-compression.service")
    replay_show = unit_show("nhms-node27-timeseries-compression-replay.service")
    if recurring_show != {
        "FragmentPath": ("/home/nwm/.config/systemd/user/nhms-node27-timeseries-compression.service"),
        "ActiveState": "inactive",
        "SubState": "dead",
        "MainPID": 0,
        "InvocationID": "",
        "ExecMainStartTimestamp": "",
        "ExecMainStartTimestampMonotonic": 0,
    }:
        raise SupervisorError("checkpoint recurring compression unit is not inactive")
    if (
        replay_show["FragmentPath"]
        != "/home/nwm/.config/systemd/user/nhms-node27-timeseries-compression-replay.service"
        or replay_show["ActiveState"] != "activating"
        or replay_show["SubState"] != "start"
        or replay_show["MainPID"] != os.getpid()
        or replay_show["InvocationID"] != invocation_id
        or not replay_show["ExecMainStartTimestamp"]
        or replay_show["ExecMainStartTimestampMonotonic"] <= 0
    ):
        raise SupervisorError("checkpoint replay supervisor unit is not the active owner")
    show_document = {"recurring": recurring_show, "replay": replay_show}
    # The governed-unit window is purely the "no extra activation" assertion.
    # With the governed units silent during replay, `--after-cursor` positions
    # past the last matching entry, so journalctl yields zero rows and exits 0.
    # An empty window is therefore the expected steady state, not a lost cursor.
    # MEASURED (Round-5 gate §G1): this SAME argv WITH `--show-cursor` exits 1 on
    # an empty match, so `--show-cursor` is omitted here -- the end cursor comes
    # from the positioned `-n 0` boundary probe below, and the parse loop never
    # relied on a window cursor line.  `--user` matches the boundary/start-cursor
    # sibling probes (measured harmless on the silent unit).
    window_raw = _run_capture_argv(
        [
            _host_bin("journalctl"),
            "--user",
            "--user-unit=nhms-node27-timeseries-compression.service",
            "--user-unit=nhms-node27-timeseries-compression-replay.service",
            "--after-cursor",
            journal_cursor,
            "--no-pager",
            "--output=json",
        ],
        wall=wall,
        label="systemd journal",
        max_bytes=4 * 1024**2,
    )
    for raw_line in window_raw.splitlines():
        if not raw_line or raw_line.startswith(b"-- cursor: "):
            continue
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError as error:
            raise SupervisorError("checkpoint journal row is not structured JSON") from error
        if not isinstance(row, Mapping):
            raise SupervisorError("checkpoint journal row is not an object")
        unit = _governed_user_unit(row)
        observed_id = row.get("_SYSTEMD_INVOCATION_ID") or row.get("INVOCATION_ID")
        if unit == "nhms-node27-timeseries-compression.service" and observed_id:
            raise SupervisorError("checkpoint journal observed recurring compression activation")
        if (
            unit == "nhms-node27-timeseries-compression-replay.service"
            and observed_id
            and str(observed_id) != invocation_id
        ):
            raise SupervisorError("checkpoint journal observed another replay activation")
    # Advance the boundary from a probe guaranteed to be positioned: `-n 0`
    # positions at the journal tail, so a cursor is always emitted even over an
    # empty tail.  A missing cursor here is a genuine "cursor lost", distinct
    # from the empty governed-unit window above.
    boundary_raw = _run_capture_argv(
        [_host_bin("journalctl"), "--user", "-n", "0", "--show-cursor", "--no-pager"],
        wall=wall,
        label="systemd journal boundary cursor",
        max_bytes=16 * 1024,
    )
    boundary_cursors = [
        line.removeprefix(b"-- cursor: ").decode()
        for line in boundary_raw.splitlines()
        if line.startswith(b"-- cursor: ")
    ]
    if len(boundary_cursors) != 1:
        raise SupervisorError("checkpoint journal did not retain its ending cursor")
    end_cursor = boundary_cursors[0]
    # The persisted artifact carries the assertion window plus the positioned end
    # cursor, so the verifier can re-derive both the no-activation evidence and
    # the chronology boundary (journal_end_cursor) from one file.
    journal_artifact = window_raw + b"-- cursor: " + end_cursor.encode() + b"\n"
    refs = {
        "database_activity": _observed_ref(artifact_dir / f"{safe_id}-activity.json", activity_raw),
        "relation_locks": _observed_ref(artifact_dir / f"{safe_id}-locks.json", locks_raw),
        "catalog": _observed_ref(artifact_dir / f"{safe_id}-catalog.json", catalog_raw),
        "systemd_show": _observed_ref(artifact_dir / f"{safe_id}-systemd-show.json", _canonical(show_document)),
        "journal": _observed_ref(artifact_dir / f"{safe_id}-journal.log", journal_artifact),
    }
    ledger.append(
        {
            "event_id": str(uuid.uuid4()),
            "event_type": "checkpoint",
            "checkpoint_id": checkpoint_id,
            "phase": checkpoint["phase"],
            "command_id": checkpoint["command_id"],
            "captured_at": _utc_now(),
            "monotonic": time.monotonic(),
            "journal_start_cursor": journal_cursor,
            "journal_end_cursor": end_cursor,
            **refs,
        }
    )
    return end_cursor


def _safe_regular_identity(path: Path) -> FileIdentity | None:
    """Return one pinned regular-file identity, or ``None`` when unreadable.

    The identity is returned as a named structure on purpose: callers compare
    digests and inode metadata that are trivially confused by position.
    """

    try:
        return terminal_state.terminal_identity(path)
    except terminal_state.TerminalStateError:
        return None


def _stale_identity_document(identity: FileIdentity) -> dict[str, Any]:
    return {
        "device": identity.device,
        "inode": identity.inode,
        "bytes": identity.size,
        "sha256": identity.sha256,
    }


def finalize_receipt(
    receipt_path: Path,
    *,
    expected_stale_sha256: str,
    run_id: str,
    stage: str,
    possible_mutation: bool,
    mutation_head_sha: str,
    expected_stale_device: int | None = None,
    expected_stale_inode: int | None = None,
    expected_stale_bytes: int | None = None,
    deadline_monotonic: float | None = None,
) -> bool:
    """Publish a bound tombstone through the shared durable terminal state machine."""

    if (
        expected_stale_device is not None
        and expected_stale_inode is not None
        and expected_stale_bytes is not None
    ):
        expected_document = {
            "device": expected_stale_device,
            "inode": expected_stale_inode,
            "bytes": expected_stale_bytes,
            "sha256": expected_stale_sha256,
        }
    else:
        current = _safe_regular_identity(receipt_path)
        if current is None or current.sha256 != expected_stale_sha256:
            return False
        expected_document = _stale_identity_document(current)
    try:
        expected = terminal_state.identity_from_document(receipt_path, expected_document)
    except terminal_state.TerminalStateError:
        return False
    return terminal_state.publish_bound_failure(
        receipt_path,
        stage=stage,
        expected=expected,
        run_id=run_id,
        mutation_head_sha=mutation_head_sha,
        possible_mutation=possible_mutation,
        deadline_monotonic=(
            deadline_monotonic
            if deadline_monotonic is not None
            else time.monotonic() + FINALIZER_LOCK_TIMEOUT_SECONDS
        ),
    )


def _write_finalizer_state(
    state_path: Path,
    *,
    receipt_path: Path,
    expected_stale_sha256: str,
    run_id: str,
    mutation_head_sha: str,
) -> None:
    try:
        os.lstat(state_path)
    except FileNotFoundError:
        pass
    else:
        raise SupervisorError("finalizer state path already exists")
    stale_identity = _safe_regular_identity(receipt_path)
    if stale_identity is None or stale_identity.sha256 != expected_stale_sha256:
        raise SupervisorError("finalizer stale receipt identity differs")
    state = {
        "schema_version": SCHEMA_VERSION,
        "receipt_path": str(receipt_path),
        "expected_stale_sha256": expected_stale_sha256,
        "expected_stale_device": stale_identity.device,
        "expected_stale_inode": stale_identity.inode,
        "expected_stale_bytes": stale_identity.size,
        "run_id": run_id,
        "mutation_head_sha": mutation_head_sha,
    }
    atomic_write_bytes_no_follow(state_path, _canonical(state), mode=0o600)


def finalize_from_state(
    state_path: Path, *, stage: str, deadline_monotonic: float | None = None
) -> bool:
    """Run the shared finalizer from one descriptor-bound supervisor state file."""

    try:
        _, value = read_bounded_json_no_follow(
            state_path,
            max_bytes=16 * 1024,
            label="finalizer state",
            max_depth=8,
            max_nodes=64,
            max_array_items=8,
        )
    except BoundedEvidenceError:
        return False
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "receipt_path",
        "expected_stale_sha256",
        "expected_stale_device",
        "expected_stale_inode",
        "expected_stale_bytes",
        "run_id",
        "mutation_head_sha",
    }:
        return False
    receipt = Path(str(value["receipt_path"]))
    if (
        value["schema_version"] != SCHEMA_VERSION
        or not receipt.is_absolute()
        or re.fullmatch(r"[0-9a-f]{40}", str(value["mutation_head_sha"])) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(value["expected_stale_sha256"])) is None
        # The run_id reaches a sibling marker filename below, so it must be a
        # bounded, separator-free name before it touches any path.
        or re.fullmatch(RUN_ID_PATTERN, str(value["run_id"])) is None
        or any(
            not isinstance(value[key], int)
            or isinstance(value[key], bool)
            or value[key] < 0
            for key in ("expected_stale_device", "expected_stale_inode", "expected_stale_bytes")
        )
        or value["expected_stale_inode"] < 1
    ):
        return False
    run_id = str(value["run_id"])
    deadline = (
        deadline_monotonic
        if deadline_monotonic is not None
        else time.monotonic() + FINALIZER_LOCK_TIMEOUT_SECONDS
    )
    replaced = finalize_receipt(
        receipt,
        expected_stale_sha256=str(value["expected_stale_sha256"]),
        expected_stale_device=int(value["expected_stale_device"]),
        expected_stale_inode=int(value["expected_stale_inode"]),
        expected_stale_bytes=int(value["expected_stale_bytes"]),
        run_id=run_id,
        stage=stage,
        possible_mutation=True,
        mutation_head_sha=str(value["mutation_head_sha"]),
        deadline_monotonic=deadline,
    )
    current = _safe_regular_identity(receipt)
    expected_identity = {
        "device": int(value["expected_stale_device"]),
        "inode": int(value["expected_stale_inode"]),
        "bytes": int(value["expected_stale_bytes"]),
        "sha256": str(value["expected_stale_sha256"]),
    }
    if not replaced:
        if current is None or _stale_identity_document(current) == expected_identity:
            return False
        if not terminal_state.terminal_is_authoritative(
            receipt, deadline_monotonic=deadline
        ):
            return False
    consume_path = state_path.with_name(f".{state_path.name}.{run_id}.consumed")
    try:
        consume_fd = os.open(
            consume_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        try:
            state_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return replaced
    except OSError:
        return False
    os.close(consume_fd)
    try:
        state_path.unlink()
    except OSError:
        return False
    return replaced

def disarm_finalizer_state(state_path: Path, *, run_id: str) -> None:
    """Remove only the state belonging to this successful supervisor run."""

    try:
        _, value = read_bounded_json_no_follow(
            state_path,
            max_bytes=16 * 1024,
            label="finalizer state",
            max_depth=8,
            max_nodes=64,
            max_array_items=8,
        )
    except BoundedEvidenceError:
        return
    if isinstance(value, Mapping) and value.get("run_id") == run_id:
        try:
            state_path.unlink()
        except OSError:
            pass


def validate_current_d3(catalog: Mapping[str, Any]) -> None:
    """Accept exact current D3 state; reject disabled, missing or drifted state."""

    expected_hypertables = {
        "hydro.river_timeseries": True,
        "met.forcing_station_timeseries": True,
    }
    if catalog.get("hypertables") != expected_hypertables or catalog.get("policy_jobs") != []:
        raise SupervisorError("catalog is not exact current D3 state")
    rows = catalog.get("compression_settings")
    expected = [
        ("hydro", "river_timeseries", "run_id", 1, None, None, None),
        ("hydro", "river_timeseries", "river_network_version_id", 2, None, None, None),
        ("hydro", "river_timeseries", "river_segment_id", 3, None, None, None),
        ("hydro", "river_timeseries", "variable", None, 1, True, False),
        ("hydro", "river_timeseries", "valid_time", None, 2, True, False),
        ("met", "forcing_station_timeseries", "forcing_version_id", 1, None, None, None),
        ("met", "forcing_station_timeseries", "station_id", 2, None, None, None),
        ("met", "forcing_station_timeseries", "variable", None, 1, True, False),
        ("met", "forcing_station_timeseries", "valid_time", None, 2, True, False),
    ]
    fields = (
        "hypertable_schema",
        "hypertable_name",
        "attname",
        "segmentby_column_index",
        "orderby_column_index",
        "orderby_asc",
        "orderby_nullsfirst",
    )
    if (
        not isinstance(rows, list)
        or [tuple(row.get(field) for field in fields) if isinstance(row, Mapping) else () for row in rows] != expected
    ):
        raise SupervisorError("catalog D3 compression settings differ")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-plan-path", type=Path, default=DEFAULT_RUN_PLAN)
    parser.add_argument("--ledger-path", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--receipt-path", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--finalizer-state-path", type=Path, default=DEFAULT_FINALIZER_STATE)
    parser.add_argument("--expected-stale-sha256")
    parser.add_argument("--wall-seconds", type=float, default=DEFAULT_WALL_SECONDS)
    parser.add_argument("--finalize-only", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    return parser


def execute_producer_state_machine(
    plan: Mapping[str, Any],
    *,
    wall: HardWall,
    ledger: AppendOnlyLedger,
    artifact_dir: Path,
    checkpoint_runner: Callable[[str, str | None], None],
    restore_identity_resolver: Callable[[HardWall, str], Mapping[str, Any]],
) -> None:
    """Execute the one canonical producer order used by live and harmless tests."""

    captures_by_kind = {str(item["kind"]): item for item in plan["captures"]}

    def capture(kind: str) -> None:
        run_capture_step(
            captures_by_kind[kind],
            wall=wall,
            ledger=ledger,
            artifact_dir=artifact_dir,
        )

    checkpoint_runner("preflight", None)
    capture("preflight_evidence")
    pg_restore_identity: Mapping[str, Any] | None = None
    migration_ordinal = 0
    for command in plan["commands"]:
        command_id = str(command["command_id"])
        kind = str(command["kind"])
        if kind == "pg_restore_version":
            list_command = next(item for item in plan["commands"] if item["kind"] == "pg_restore_list")
            pg_restore_identity = restore_identity_resolver(wall, str(list_command["argv"][-1]))
        if kind == "decompress":
            capture("recovery_preflight")
        if kind == "compression_enforce":
            capture("pre_enforce_selection")
            capture("sizes_pre")
        if kind in MUTATION_KINDS:
            checkpoint_runner("before_mutation", command_id)
        run_child(
            command,
            wall=wall,
            ledger=ledger,
            mutation_head_sha=str(plan["mutation_head_sha"]),
            database=str(plan["database"]),
            artifact_dir=artifact_dir,
            observed_associations=(
                pg_restore_identity if kind in {"pg_restore_version", "pg_restore_list"} else None
            ),
        )
        if kind == "pg_restore_list":
            capture("schema_dump_list")
            capture("catalog_before")
        if kind in MUTATION_KINDS:
            checkpoint_runner("after_mutation", command_id)
        if kind == "migration_apply":
            capture("catalog_after_first" if migration_ordinal == 0 else "catalog_after_second")
            migration_ordinal += 1
        elif kind == "compression_dry_run":
            capture("post_dry_selection")
        elif kind == "compression_enforce":
            capture("sizes_post")
            capture("catalog_post")
    checkpoint_runner("postflight", None)
    capture("cleanup")
    checkpoint_runner("cleanup", None)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.finalize_only:
        finalize_from_state(args.finalizer_state_path, stage="systemd-stop-post")
        return 0
    if not args.enforce:
        raise SupervisorError("qualifying supervisor execution requires literal --enforce")
    wall = HardWall.start(args.wall_seconds)
    operation_wall = wall.reserving(FAILURE_RESERVE_SECONDS, "operation budget")
    invocation_id = os.environ.get("INVOCATION_ID", "")
    expected_plan_sha256 = os.environ.get("NODE27_COMPRESSION_RUN_PLAN_SHA256", "")
    if re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None:
        raise SupervisorError("systemd INVOCATION_ID is required")
    if re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256) is None:
        raise SupervisorError("external run-plan SHA256 pin is required")
    # One read owns the plan: the bytes that are digested are the bytes that
    # are parsed and executed.  Digesting a separate pathname read would let an
    # inode ABA swap an unpinned plan body between the pin and the parse.
    _, plan_identity, plan_value = read_bounded_json_with_identity_no_follow(
        args.run_plan_path,
        max_bytes=1024**2,
        label="supervisor run plan",
        max_depth=24,
        max_nodes=20_000,
        max_array_items=1000,
    )
    if plan_identity.sha256 != expected_plan_sha256:
        raise SupervisorError("external run-plan SHA256 pin differs")
    plan = validate_run_plan(plan_value, inherited_env=os.environ)
    if run_plan_id(plan) != plan["run_plan_id"]:
        raise SupervisorError("run_plan_id differs from the immutable plan content")
    expected_stale_sha256 = args.expected_stale_sha256 or os.environ.get("NODE27_COMPRESSION_EXPECTED_STALE_SHA256", "")
    stale_identity = _safe_regular_identity(args.receipt_path)
    if (
        re.fullmatch(r"[0-9a-f]{64}", expected_stale_sha256) is None
        or stale_identity is None
        or stale_identity.sha256 != expected_stale_sha256
    ):
        raise SupervisorError("expected stale receipt identity/digest is required")
    run_id = str(uuid.uuid4())
    _write_finalizer_state(
        args.finalizer_state_path,
        receipt_path=args.receipt_path,
        expected_stale_sha256=expected_stale_sha256,
        run_id=run_id,
        mutation_head_sha=str(plan["mutation_head_sha"]),
    )
    try:
        _verify_checkout_lineage(
            plan,
            wall=operation_wall,
        )
    except SupervisorError:
        finalize_from_state(
            args.finalizer_state_path,
            stage="checkout-lineage",
            deadline_monotonic=wall.deadline_monotonic,
        )
        raise
    cursor_raw = _run_capture_argv(
        [_host_bin("journalctl"), "--user", "-n", "0", "--show-cursor", "--no-pager"],
        wall=operation_wall,
        label="journal start cursor",
        max_bytes=16 * 1024,
    )
    cursor_lines = [
        line.removeprefix(b"-- cursor: ").decode()
        for line in cursor_raw.splitlines()
        if line.startswith(b"-- cursor: ")
    ]
    if len(cursor_lines) != 1:
        raise SupervisorError("cannot bind the journal start cursor")
    journal_cursor = cursor_lines[0]
    checkpoints_by_phase: dict[tuple[str, str | None], Mapping[str, Any]] = {
        (str(item["phase"]), item["command_id"]): item for item in plan["checkpoints"]
    }
    with AppendOnlyLedger(
        args.ledger_path,
        run_id=run_id,
        run_plan_id=str(plan["run_plan_id"]),
        invocation_id=invocation_id,
    ) as ledger:
        try:
            def live_checkpoint(phase: str, command_id: str | None) -> None:
                nonlocal journal_cursor
                journal_cursor = capture_checkpoint(
                    checkpoints_by_phase[(phase, command_id)],
                    wall=operation_wall,
                    ledger=ledger,
                    artifact_dir=args.ledger_path.parent,
                    journal_cursor=journal_cursor,
                    invocation_id=invocation_id,
                )

            execute_producer_state_machine(
                plan,
                wall=operation_wall,
                ledger=ledger,
                artifact_dir=args.ledger_path.parent,
                checkpoint_runner=live_checkpoint,
                restore_identity_resolver=lambda probe_wall, dump_path: resolve_container_pg_restore_identity(
                    wall=probe_wall, dump_path=dump_path
                ),
            )
        except (HardWallExpired, SupervisorError):
            finalize_from_state(
                args.finalizer_state_path,
                stage="supervisor-child",
                deadline_monotonic=wall.deadline_monotonic,
            )
            raise
    disarm_finalizer_state(args.finalizer_state_path, run_id=run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
