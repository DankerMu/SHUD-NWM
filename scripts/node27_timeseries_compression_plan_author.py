#!/usr/bin/env python3
"""Author the committed, executable node-27 #1069 replay run-plan.

The replay ``ExecStart`` pins ``/home/nwm/node27-timeseries-compression-replay/
run-plan.json``.  Before tonight nothing in the repository produced that file, so
the "reviewed run-plan" the runbook (§4.0) says to verify had no committed,
executable author -- a placeholder that shape-validates but whose captures never
produce the documents the verifier content-checks would burn the one-shot replay
window *after* the mutation.  This module emits the exact production plan whose
ten command argvs match the supervisor's ``_assert_exact_argv`` contract and
whose twelve captures are real invocations of the committed capture-producer
(``node27_timeseries_compression_capture.py``), then self-checks it against the
real ``validate_run_plan`` and prints its file sha256 + ``run_plan_id``.

The command argvs are pinned to the host contract; only the *capture* argvs carry
overridable ``--root``/bin-dir seams (captures are shape-validated but not
argv-pinned), so the pipeline dress-rehearsal can drive the real producer against
stub binaries.  Production defaults reproduce the host paths verbatim.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from scripts import node27_timeseries_compression_supervisor as supervisor

# The container sees the host dump through the bind mount added tonight; the
# pg_restore_list argv must name the CONTAINER path so the supervisor's
# ``/var/lib/postgresql/`` mount guard is satisfied.
DEFAULT_REPO = "/home/nwm/NWM"
DEFAULT_ROOT = "/home/nwm/node27-timeseries-compression-replay"
DEFAULT_SCHEMA_DUMP_HOST = "/home/nwm/nhms-evidence/schema-before.dump"
DEFAULT_SCHEMA_DUMP_CONTAINER = "/var/lib/postgresql/evidence/schema-before.dump"
DEFAULT_CAPTURE_SCRIPT = f"{DEFAULT_REPO}/scripts/node27_timeseries_compression_capture.py"
# The supervisor's exact-argv contract pins the compression lock and the
# benchmark-before hand-off path to the production replay directory literally, so
# they are host constants rather than ``--root``-relative (with the default root
# they coincide with the relocatable outputs).
PINNED_LOCK_PATH = f"{DEFAULT_ROOT}/compression.lock"
PINNED_BENCHMARK_BEFORE_PATH = f"{DEFAULT_ROOT}/benchmark-before.json"
DEFAULT_CONTAINER = supervisor.EXPECTED_CONTAINER
DEFAULT_DATABASE = supervisor.EXPECTED_DATABASE

# The exact curve/MVT parameters the runbook §4.0 representative-performance proof
# pins; the benchmark producer and verifier both bind them.
_BENCHMARK_PARAMS = [
    ("--curve-basin-version-id", "basins_heihe_vbasins"),
    ("--curve-river-segment-id", "basins_heihe_shud_reach_000001"),
    ("--curve-river-network-version-id", "basins_heihe_rivnet_vbasins"),
    ("--curve-issue-time", "2026-05-31T06:00:00Z"),
    ("--curve-end-time", "2026-06-07T06:00:00Z"),
    ("--curve-scenario", "forecast_gfs_deterministic"),
    ("--mvt-run-id", "fcst_gfs_2026053106_basins_heihe_shud"),
    ("--mvt-basin-version-id", "basins_heihe_vbasins"),
    ("--mvt-river-network-version-id", "basins_heihe_rivnet_vbasins"),
    ("--mvt-valid-time", "2026-05-31T06:00:00Z"),
    ("--mvt-z", "9"),
    ("--mvt-x", "399"),
    ("--mvt-y", "189"),
]
# The exact-chunk recovery target (matches the decompress command and verifier
# RECOVERY_TARGET).
_RECOVERY_TARGET_ARGS = [
    "--hypertable-schema", "hydro",
    "--hypertable-name", "river_timeseries",
    "--chunk-schema", "_timescaledb_internal",
    "--chunk-name", "_hyper_3_7_chunk",
    "--range-start", "2026-05-28T00:00:00Z",
    "--range-end", "2026-06-04T00:00:00Z",
]


class PlanAuthorError(RuntimeError):
    """The requested run-plan could not be authored as a valid production plan."""


def _flatten(pairs: list[tuple[str, str]]) -> list[str]:
    return [token for pair in pairs for token in pair]


def build_run_plan(
    *,
    mutation_head_sha: str,
    repo: str = DEFAULT_REPO,
    root: str = DEFAULT_ROOT,
    database: str = DEFAULT_DATABASE,
    container: str = DEFAULT_CONTAINER,
    schema_dump_host: str = DEFAULT_SCHEMA_DUMP_HOST,
    schema_dump_container: str = DEFAULT_SCHEMA_DUMP_CONTAINER,
    capture_repo: str | None = None,
    capture_python: str = sys.executable,
    capture_script: str = DEFAULT_CAPTURE_SCRIPT,
    capture_psql: str = "/usr/bin/psql",
    capture_systemctl: str = "/usr/bin/systemctl",
    capture_docker: str = "/usr/bin/docker",
    capture_journalctl: str = "/usr/bin/journalctl",
    capture_git: str = "/usr/bin/git",
) -> dict[str, Any]:
    """Return the complete concrete replay run-plan (unsigned run_plan_id)."""

    if re.fullmatch(r"[0-9a-f]{40}", mutation_head_sha) is None:
        raise PlanAuthorError("mutation head sha must be 40 lowercase hex characters")
    for label, value in (("repo", repo), ("root", root)):
        if not Path(value).is_absolute():
            raise PlanAuthorError(f"{label} must be an absolute path")

    python = f"{repo}/.venv/bin/python"
    wrapper = f"{repo}/scripts/node27_timeseries_compression_once.sh"
    migration = f"{repo}/db/migrations/000047_hypertable_compression_settings.sql"
    decompress_script = f"{repo}/scripts/node27_timeseries_decompression_replay.py"
    benchmark_script = f"{repo}/scripts/node27_timeseries_compression_benchmark.py"
    lock_path = PINNED_LOCK_PATH
    recovery_receipt = f"{root}/recovery-receipt.json"
    dry_run_receipt = f"{root}/dry-run-receipt.json"
    enforce_receipt = f"{root}/enforce-receipt.json"
    benchmark_before = f"{root}/benchmark-before.json"
    benchmarks = f"{root}/benchmarks.json"

    commands: list[dict[str, Any]] = [
        {
            "command_id": "pg-dump",
            "kind": "pg_dump",
            "argv": [
                "/usr/bin/pg_dump", "--dbname", database, "--format=custom",
                "--schema-only", "--file", schema_dump_host,
            ],
            "artifact_associations": {"schema_dump": schema_dump_host},
        },
        {
            "command_id": "pg-restore-version",
            "kind": "pg_restore_version",
            "argv": ["/usr/bin/docker", "exec", container, "/usr/bin/pg_restore", "--version"],
            "artifact_associations": {},
        },
        {
            "command_id": "pg-restore-list",
            "kind": "pg_restore_list",
            "argv": [
                "/usr/bin/docker", "exec", container, "/usr/bin/pg_restore",
                "--list", schema_dump_container,
            ],
            "artifact_associations": {},
        },
        {
            "command_id": "migration-1",
            "kind": "migration_apply",
            "argv": [
                "/usr/bin/psql", "--dbname", database, "--no-psqlrc",
                "--set", "ON_ERROR_STOP=1", "--file", migration,
            ],
            "artifact_associations": {},
        },
        {
            "command_id": "migration-2",
            "kind": "migration_apply",
            "argv": [
                "/usr/bin/psql", "--dbname", database, "--no-psqlrc",
                "--set", "ON_ERROR_STOP=1", "--file", migration,
            ],
            "artifact_associations": {},
        },
        {
            "command_id": "decompress",
            "kind": "decompress",
            "argv": [
                python, decompress_script, "--database", database,
                "--mutation-head-sha", mutation_head_sha,
                "--receipt-path", recovery_receipt, *_RECOVERY_TARGET_ARGS,
            ],
            "artifact_associations": {"recovery_receipt": recovery_receipt},
        },
        {
            "command_id": "dry-run",
            "kind": "compression_dry_run",
            "argv": [wrapper, "--receipt-path", dry_run_receipt, "--lock-path", lock_path],
            "artifact_associations": {"dry_run_receipt": dry_run_receipt},
        },
        {
            "command_id": "benchmark-before",
            "kind": "benchmark_before",
            "argv": [
                python, benchmark_script, "--phase", "before",
                "--output", benchmark_before, *_flatten(_BENCHMARK_PARAMS),
            ],
            "artifact_associations": {"benchmark_before": benchmark_before},
        },
        {
            "command_id": "enforce",
            "kind": "compression_enforce",
            "argv": [wrapper, "--enforce", "--receipt-path", enforce_receipt, "--lock-path", lock_path],
            "artifact_associations": {"enforce_receipt": enforce_receipt},
        },
        {
            "command_id": "benchmark-after",
            "kind": "benchmark_after",
            "argv": [
                python, benchmark_script, "--phase", "after",
                "--before-path", PINNED_BENCHMARK_BEFORE_PATH, "--output", benchmarks,
                *_flatten(_BENCHMARK_PARAMS),
            ],
            "artifact_associations": {"benchmarks": benchmarks},
        },
    ]

    # ``repo`` pins the plan's authorization boundary (the reviewed checkout); the
    # capture producer's local file reads can be redirected for the dress
    # rehearsal without weakening the pinned plan repo_path.
    capture_common = [
        "--database", database,
        "--mutation-head-sha", mutation_head_sha,
        "--repo", capture_repo or repo,
        "--container", container,
        "--evidence-dir", f"{root}/capture-artifacts",
        "--psql", capture_psql,
        "--systemctl", capture_systemctl,
        "--docker", capture_docker,
        "--journalctl", capture_journalctl,
        "--git", capture_git,
    ]
    captures: list[dict[str, Any]] = []
    for kind in supervisor.EXPECTED_CAPTURE_SEQUENCE:
        extra: list[str] = []
        if kind == "schema_dump_list":
            extra = [
                "--schema-dump-host", schema_dump_host,
                "--schema-dump-container", schema_dump_container,
            ]
        captures.append(
            {
                "capture_id": f"capture-{kind}",
                "kind": kind,
                "argv": [capture_python, capture_script, "--kind", kind, *capture_common, *extra],
                "output_path": f"{root}/capture-{kind}.json",
            }
        )

    mutation_ids = [c["command_id"] for c in commands if c["kind"] in supervisor.MUTATION_KINDS]
    checkpoints: list[dict[str, Any]] = [
        {"checkpoint_id": "preflight", "phase": "preflight", "command_id": None},
        {"checkpoint_id": "postflight", "phase": "postflight", "command_id": None},
        {"checkpoint_id": "cleanup", "phase": "cleanup", "command_id": None},
    ]
    for command_id in mutation_ids:
        checkpoints.append(
            {"checkpoint_id": f"before-{command_id}", "phase": "before_mutation", "command_id": command_id}
        )
        checkpoints.append(
            {"checkpoint_id": f"after-{command_id}", "phase": "after_mutation", "command_id": command_id}
        )

    plan: dict[str, Any] = {
        "plan_version": supervisor.RUN_PLAN_VERSION,
        "run_plan_id": "",
        "mutation_head_sha": mutation_head_sha,
        "reviewed_remote_ref": supervisor.EXPECTED_REVIEWED_REMOTE_REF,
        "database": database,
        "repo_path": repo,
        "operator_attestation": {
            "sole_db_user_during_window": True,
            "database_audit_proof": False,
            "trust_limit": "discrete observations; no absolute direct-SQL bypass proof",
        },
        "commands": commands,
        "captures": captures,
        "checkpoints": checkpoints,
    }
    plan["run_plan_id"] = supervisor.run_plan_id(plan)
    return plan


def author_and_validate(**kwargs: Any) -> tuple[dict[str, Any], bytes, str]:
    """Build the plan, validate it with the real supervisor gate, canonicalize it."""

    plan = build_run_plan(**kwargs)
    # Fail closed here rather than at the one-shot replay start: the supervisor
    # gate is the same contract the live unit enforces.
    validated = supervisor.validate_run_plan(plan, inherited_env={})
    if supervisor.run_plan_id(validated) != plan["run_plan_id"]:
        raise PlanAuthorError("authored run_plan_id is not stable under validation")
    canonical = supervisor._canonical(plan)
    return plan, canonical, hashlib.sha256(canonical).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutation-head-sha", required=True)
    parser.add_argument("--output", type=Path, default=None, help="write the plan here (default: stdout)")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--schema-dump-host", default=DEFAULT_SCHEMA_DUMP_HOST)
    parser.add_argument("--schema-dump-container", default=DEFAULT_SCHEMA_DUMP_CONTAINER)
    parser.add_argument("--capture-python", default=sys.executable)
    parser.add_argument("--capture-script", default=DEFAULT_CAPTURE_SCRIPT)
    parser.add_argument("--capture-psql", default="/usr/bin/psql")
    parser.add_argument("--capture-systemctl", default="/usr/bin/systemctl")
    parser.add_argument("--capture-docker", default="/usr/bin/docker")
    parser.add_argument("--capture-journalctl", default="/usr/bin/journalctl")
    parser.add_argument("--capture-git", default="/usr/bin/git")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    plan, canonical, digest = author_and_validate(
        mutation_head_sha=args.mutation_head_sha,
        repo=args.repo,
        root=args.root,
        database=args.database,
        container=args.container,
        schema_dump_host=args.schema_dump_host,
        schema_dump_container=args.schema_dump_container,
        capture_python=args.capture_python,
        capture_script=args.capture_script,
        capture_psql=args.capture_psql,
        capture_systemctl=args.capture_systemctl,
        capture_docker=args.capture_docker,
        capture_journalctl=args.capture_journalctl,
        capture_git=args.capture_git,
    )
    if args.output is not None:
        from packages.common.safe_fs import atomic_write_bytes_no_follow

        atomic_write_bytes_no_follow(args.output, canonical, mode=0o600)
        location = str(args.output)
    else:
        sys.stdout.buffer.write(canonical)
        location = "-"
    sys.stderr.write(f"run_plan_id={plan['run_plan_id']}\n")
    sys.stderr.write(f"sha256={digest}\n")
    sys.stderr.write(f"output={location}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
