from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from packages.common.source_identity import normalize_source_id

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN_ROOT = ROOT / ".nhms-runs" / "qhh-continuous"
MODEL_ID = "basins_qhh_shud"
TERMINAL_SUCCESS = {"frequency_done", "published", "already_done"}
RETRYABLE_STATE = {"failed", "unavailable"}
ACTIVE_STATE = {"submitted", "running"}
SLURM_TERMINAL_SUCCESS = {"COMPLETED"}
SLURM_TERMINAL_FAILURE = {"FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL"}
SLURM_ACCOUNTING_UNKNOWN = "UNKNOWN_ACCOUNTING_UNAVAILABLE"
DEFAULT_SLURM_WAIT_TIMEOUT_SECONDS = 12 * 60 * 60
DEFAULT_SLURM_SQUEUE_POLL_SECONDS = 30
DEFAULT_SLURM_ACCOUNTING_TIMEOUT_SECONDS = 300
DEFAULT_SLURM_ACCOUNTING_POLL_SECONDS = 10
SLURM_EXPORT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class CandidateCycle:
    source_id: str
    cycle_time: datetime

    @property
    def token(self) -> str:
        return self.cycle_time.strftime("%Y%m%d%H")

    @property
    def source_segment(self) -> str:
        return self.source_id.lower()

    @property
    def run_id(self) -> str:
        return f"fcst_{self.source_segment}_{self.token}_{MODEL_ID}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run qhh full chain continuously for GFS and IFS cycles.")
    parser.add_argument("--once", action="store_true", default=_env_bool("QHH_CONTINUOUS_ONCE", True))
    parser.add_argument("--dry-run", action="store_true", default=_env_bool("QHH_CONTINUOUS_DRY_RUN", False))
    parser.add_argument("--sources", default=os.getenv("QHH_CONTINUOUS_SOURCES", "gfs,IFS"))
    parser.add_argument("--lookback-hours", type=int, default=int(os.getenv("QHH_CONTINUOUS_LOOKBACK_HOURS", "48")))
    parser.add_argument(
        "--max-cycles-per-source",
        type=int,
        default=int(os.getenv("QHH_CONTINUOUS_MAX_CYCLES_PER_SOURCE", "2")),
    )
    parser.add_argument("--cycle-lag-hours", type=int, default=int(os.getenv("QHH_CONTINUOUS_CYCLE_LAG_HOURS", "6")))
    parser.add_argument("--poll-seconds", type=int, default=int(os.getenv("QHH_CONTINUOUS_POLL_SECONDS", "1800")))
    parser.add_argument("--run-root", default=os.getenv("QHH_RUN_ROOT", str(DEFAULT_RUN_ROOT)))
    parser.add_argument("--executor", choices=("local", "slurm"), default=os.getenv("QHH_CONTINUOUS_EXECUTOR", "local"))
    parser.add_argument("--slurm-partition", default=os.getenv("QHH_SLURM_PARTITION", "CPU"))
    parser.add_argument("--slurm-cpus", type=int, default=int(os.getenv("QHH_SLURM_CPUS", "8")))
    parser.add_argument("--slurm-mem", default=os.getenv("QHH_SLURM_MEM", "128G"))
    parser.add_argument("--slurm-time", default=os.getenv("QHH_SLURM_TIME", "08:00:00"))
    parser.add_argument("--slurm-wait", action="store_true", default=_env_bool("QHH_SLURM_WAIT", True))
    parser.add_argument(
        "--slurm-wait-timeout-seconds",
        type=int,
        default=int(os.getenv("QHH_SLURM_WAIT_TIMEOUT_SECONDS", str(DEFAULT_SLURM_WAIT_TIMEOUT_SECONDS))),
    )
    parser.add_argument(
        "--slurm-accounting-timeout-seconds",
        type=int,
        default=int(os.getenv("QHH_SLURM_ACCOUNTING_TIMEOUT_SECONDS", str(DEFAULT_SLURM_ACCOUNTING_TIMEOUT_SECONDS))),
    )
    args = parser.parse_args(argv)

    if args.executor == "slurm" and not args.dry_run:
        _require_slurm_reachable_database()

    run_root = Path(args.run_root).expanduser().resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    state_root = run_root / "state"
    state_root.mkdir(parents=True, exist_ok=True)

    with _exclusive_lock(state_root / "qhh-continuous.lock"):
        while True:
            summary = run_pass(args=args, run_root=run_root, state_root=state_root)
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            if args.once:
                return 0 if not _has_failed(summary) else 1
            time.sleep(max(args.poll_seconds, 60))


def run_pass(*, args: argparse.Namespace, run_root: Path, state_root: Path) -> dict[str, Any]:
    sources = [normalize_source_id(item.strip()) for item in args.sources.split(",") if item.strip()]
    candidates = _candidate_cycles(
        sources=sources,
        lookback_hours=args.lookback_hours,
        max_cycles_per_source=args.max_cycles_per_source,
        cycle_lag_hours=args.cycle_lag_hours,
    )
    pass_started_at = _now_iso()
    results: list[dict[str, Any]] = []
    for candidate in candidates:
        state_file = _state_file(state_root, candidate)
        current_state = _read_json(state_file)
        skip_reason = _skip_reason(candidate, current_state, executor=args.executor)
        if skip_reason is not None:
            result = {
                "source_id": candidate.source_id,
                "cycle_time": candidate.token,
                "run_id": candidate.run_id,
                "status": current_state.get("status", "already_done"),
                "reason": skip_reason,
            }
            if current_state.get("slurm_job_id"):
                result["slurm_job_id"] = current_state["slurm_job_id"]
            results.append(result)
            continue
        if args.dry_run:
            result = {
                "source_id": candidate.source_id,
                "cycle_time": candidate.token,
                "run_id": candidate.run_id,
                "status": "planned",
                "reason": "dry run",
            }
            results.append(result)
            continue
        if args.executor == "slurm":
            result = _submit_slurm_cycle(candidate, run_root=run_root, state_file=state_file, args=args)
        else:
            result = _run_cycle(candidate, run_root=run_root, state_file=state_file)
        results.append(result)

    summary = {
        "status": "completed_with_failures" if any(item["status"] == "failed" for item in results) else "completed",
        "pass_started_at": pass_started_at,
        "pass_finished_at": _now_iso(),
        "run_root": str(run_root),
        "candidate_count": len(candidates),
        "results": results,
    }
    (state_root / "qhh-continuous-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def _candidate_cycles(
    *,
    sources: list[str],
    lookback_hours: int,
    max_cycles_per_source: int,
    cycle_lag_hours: int,
) -> list[CandidateCycle]:
    latest = datetime.now(UTC) - timedelta(hours=max(cycle_lag_hours, 0))
    earliest = latest - timedelta(hours=max(lookback_hours, 0))
    cycles: list[CandidateCycle] = []
    cycle_hours = (0, 6, 12, 18)
    for source_id in sources:
        source_cycles: list[CandidateCycle] = []
        cursor = datetime(latest.year, latest.month, latest.day, tzinfo=UTC)
        while cursor >= earliest - timedelta(days=1):
            for hour in cycle_hours:
                cycle_time = cursor.replace(hour=hour)
                if earliest <= cycle_time <= latest:
                    source_cycles.append(CandidateCycle(source_id=source_id, cycle_time=cycle_time))
            cursor -= timedelta(days=1)
        source_cycles.sort(key=lambda item: item.cycle_time, reverse=True)
        cycles.extend(source_cycles[: max(max_cycles_per_source, 1)])
    return cycles


def _run_cycle(candidate: CandidateCycle, *, run_root: Path, state_file: Path) -> dict[str, Any]:
    _write_state(
        state_file,
        {
            "status": "running",
            "source_id": candidate.source_id,
            "cycle_time": candidate.token,
            "run_id": candidate.run_id,
            "started_at": _now_iso(),
        },
    )
    env = os.environ.copy()
    env.update(
        {
            "QHH_RUN_ROOT": str(run_root),
            "QHH_SOURCE_ID": candidate.source_id,
            "QHH_CYCLE_TIME": candidate.token,
            "QHH_RUN_ID": candidate.run_id,
            "QHH_SKIP_COMPLETED": env.get("QHH_SKIP_COMPLETED", "1"),
        }
    )
    command = [str(ROOT / "scripts" / "run_qhh_cycle.sh")]
    started = time.monotonic()
    completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
    elapsed_seconds = round(time.monotonic() - started, 3)
    state = _read_json(state_file)
    if completed.returncode == 0:
        status = str(state.get("status") or "completed")
        result = {
            "source_id": candidate.source_id,
            "cycle_time": candidate.token,
            "run_id": candidate.run_id,
            "status": status,
            "returncode": completed.returncode,
            "elapsed_seconds": elapsed_seconds,
        }
        if not state:
            _write_state(state_file, {**result, "finished_at": _now_iso()})
        return result

    result = {
        "source_id": candidate.source_id,
        "cycle_time": candidate.token,
        "run_id": candidate.run_id,
        "status": "failed",
        "returncode": completed.returncode,
        "elapsed_seconds": elapsed_seconds,
    }
    _write_state(state_file, {**result, "finished_at": _now_iso()})
    return result


def _submit_slurm_cycle(
    candidate: CandidateCycle,
    *,
    run_root: Path,
    state_file: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    slurm_root = run_root / "slurm-logs" / candidate.source_segment / candidate.token
    slurm_root.mkdir(parents=True, exist_ok=True)
    env_exports = _slurm_exports(candidate, run_root)
    env_file = _write_slurm_env_file(slurm_root / "qhh-cycle.env", env_exports)
    command = [
        "sbatch",
        "--parsable",
        "--partition",
        str(args.slurm_partition),
        "--cpus-per-task",
        str(args.slurm_cpus),
        "--mem",
        str(args.slurm_mem),
        "--time",
        str(args.slurm_time),
        "--job-name",
        f"qhh_{candidate.source_segment}_{candidate.token}",
        "--output",
        str(slurm_root / "%j.out"),
        "--error",
        str(slurm_root / "%j.err"),
        "--export",
        _format_slurm_export({"QHH_SLURM_ENV_FILE": str(env_file)}),
        str(ROOT / "scripts" / "run_qhh_cycle.sbatch"),
    ]
    started = time.monotonic()
    submitted = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)
    if submitted.returncode != 0:
        result = {
            "source_id": candidate.source_id,
            "cycle_time": candidate.token,
            "run_id": candidate.run_id,
            "status": "failed",
            "reason": "sbatch submission failed",
            "returncode": submitted.returncode,
            "stderr": submitted.stderr.strip(),
        }
        _write_state(state_file, {**result, "finished_at": _now_iso()})
        return result

    job_id = submitted.stdout.strip().split(";", 1)[0]
    _write_state(
        state_file,
        {
            "status": "submitted",
            "source_id": candidate.source_id,
            "cycle_time": candidate.token,
            "run_id": candidate.run_id,
            "slurm_job_id": job_id,
            "submitted_at": _now_iso(),
            "slurm_log_dir": str(slurm_root),
        },
    )
    if not args.slurm_wait:
        return {
            "source_id": candidate.source_id,
            "cycle_time": candidate.token,
            "run_id": candidate.run_id,
            "status": "submitted",
            "slurm_job_id": job_id,
        }

    status = _wait_for_slurm_job(
        job_id,
        wait_timeout_seconds=args.slurm_wait_timeout_seconds,
        accounting_timeout_seconds=args.slurm_accounting_timeout_seconds,
    )
    elapsed_seconds = round(time.monotonic() - started, 3)
    state = _read_json(state_file)
    if status == "COMPLETED":
        result_status = str(state.get("status") or "completed")
        return {
            "source_id": candidate.source_id,
            "cycle_time": candidate.token,
            "run_id": candidate.run_id,
            "status": result_status,
            "slurm_status": status,
            "slurm_job_id": job_id,
            "elapsed_seconds": elapsed_seconds,
        }
    result = {
        "source_id": candidate.source_id,
        "cycle_time": candidate.token,
        "run_id": candidate.run_id,
        "status": "failed",
        "slurm_status": status,
        "slurm_job_id": job_id,
        "elapsed_seconds": elapsed_seconds,
    }
    _write_state(state_file, {**result, "finished_at": _now_iso()})
    return result


def _slurm_exports(candidate: CandidateCycle, run_root: Path) -> dict[str, str]:
    inherited = {
        key: value
        for key, value in os.environ.items()
        if _slurm_env_allowed(key)
        and not _slurm_env_sensitive(key)
        and not key.startswith("QHH_CONTINUOUS_")
        and _slurm_export_value_allowed(value)
    }
    inherited.update(
        {
            "QHH_REPO_ROOT": str(ROOT),
            "QHH_RUN_ROOT": str(run_root),
            "WORKSPACE_ROOT": str(run_root),
            "OBJECT_STORE_ROOT": os.getenv("OBJECT_STORE_ROOT", str(run_root)),
            "OBJECT_STORE_PREFIX": os.getenv("OBJECT_STORE_PREFIX", "s3://nhms"),
            "QHH_SOURCE_ID": candidate.source_id,
            "QHH_CYCLE_TIME": candidate.token,
            "QHH_RUN_ID": candidate.run_id,
            "QHH_AUTO_START_PG": "0",
            "PATH": os.environ.get("PATH", ""),
        }
    )
    if "DATABASE_URL" in os.environ:
        inherited["DATABASE_URL"] = os.environ["DATABASE_URL"]
    return {key: value for key, value in inherited.items() if value != ""}


def _format_slurm_export(values: Mapping[str, str]) -> str:
    assignments = [f"{key}={value}" for key, value in values.items()]
    return ",".join(assignments)


def _write_slurm_env_file(path: Path, values: Mapping[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"export {key}={shlex.quote(value)}" for key, value in sorted(values.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _wait_for_slurm_job(
    job_id: str,
    *,
    wait_timeout_seconds: int = DEFAULT_SLURM_WAIT_TIMEOUT_SECONDS,
    accounting_timeout_seconds: int = DEFAULT_SLURM_ACCOUNTING_TIMEOUT_SECONDS,
    squeue_poll_seconds: int = DEFAULT_SLURM_SQUEUE_POLL_SECONDS,
    accounting_poll_seconds: int = DEFAULT_SLURM_ACCOUNTING_POLL_SECONDS,
) -> str:
    wait_deadline = time.monotonic() + max(wait_timeout_seconds, 1)
    accounting_deadline: float | None = None
    signals = (signal.SIGINT, signal.SIGTERM)
    previous_handlers = {signum: signal.getsignal(signum) for signum in signals}

    def cancel_and_raise(signum: int, _frame: Any) -> None:
        _cancel_slurm_job(job_id)
        raise SystemExit(f"cancelled by signal {signum}; requested scancel for Slurm job {job_id}")

    for signum in signals:
        signal.signal(signum, cancel_and_raise)
    try:
        while time.monotonic() < wait_deadline:
            running = subprocess.run(
                ["squeue", "-h", "-j", job_id, "-o", "%T"],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            if running.returncode == 0 and running.stdout.strip():
                time.sleep(min(max(squeue_poll_seconds, 1), max(wait_deadline - time.monotonic(), 0.0)))
                continue
            accounting = subprocess.run(
                ["sacct", "-n", "-P", "-j", job_id, "-o", "State"],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            if accounting.returncode != 0:
                return SLURM_ACCOUNTING_UNKNOWN
            states = [
                line.split("|", 1)[0].strip().split()[0]
                for line in accounting.stdout.splitlines()
                if line.strip()
            ]
            if states:
                for state in states:
                    if state in SLURM_TERMINAL_FAILURE:
                        return state
                if all(state in SLURM_TERMINAL_SUCCESS for state in states):
                    return "COMPLETED"
                return states[0]
            if accounting_deadline is None:
                accounting_deadline = time.monotonic() + max(accounting_timeout_seconds, 1)
            if time.monotonic() >= accounting_deadline:
                return SLURM_ACCOUNTING_UNKNOWN
            time.sleep(min(max(accounting_poll_seconds, 1), max(accounting_deadline - time.monotonic(), 0.0)))
        return "UNKNOWN_WAIT_TIMEOUT"
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def _cancel_slurm_job(job_id: str) -> None:
    subprocess.run(["scancel", job_id], cwd=ROOT, check=False, capture_output=True, text=True)


def _slurm_env_allowed(key: str) -> bool:
    if SLURM_EXPORT_NAME_RE.fullmatch(key) is None:
        return False
    if key == "PATH":
        return True
    return key.startswith(("QHH_", "GFS_", "IFS_", "FORCING_", "OBJECT_STORE_", "SHUD_", "NHMS_"))


def _slurm_env_sensitive(key: str) -> bool:
    upper = key.upper()
    return any(token in upper for token in ("PASSWORD", "SECRET", "TOKEN", "KEY", "CREDENTIAL"))


def _slurm_export_value_allowed(value: str) -> bool:
    return "," not in value and "\n" not in value and "\r" not in value


def _require_slurm_reachable_database() -> None:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit(
            "QHH_CONTINUOUS_EXECUTOR=slurm requires DATABASE_URL to be set to a compute-node reachable endpoint."
        )
    parsed = urlparse(database_url)
    host = parsed.hostname or ""
    if parsed.scheme not in {"postgresql", "postgres"} or not host:
        raise SystemExit(
            "QHH_CONTINUOUS_EXECUTOR=slurm requires a valid PostgreSQL DATABASE_URL reachable from compute nodes; "
            f"got {database_url!r}."
        )
    if host in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit(
            "QHH_CONTINUOUS_EXECUTOR=slurm requires DATABASE_URL reachable from compute nodes; "
            f"got host {host!r}. Use a cluster-reachable production PostgreSQL endpoint, or intentionally run "
            "scripts/local_pg.sh in safe helper mode with QHH_LOCAL_PG_ALLOW_REMOTE=1 and a non-default APP_PASSWORD."
        )


def _skip_reason(candidate: CandidateCycle, state: dict[str, Any], *, executor: str) -> str | None:
    status = str(state.get("status") or "")
    if status in TERMINAL_SUCCESS:
        return "state file already terminal"
    if status in ACTIVE_STATE and executor == "slurm":
        job_id = str(state.get("slurm_job_id") or "")
        if job_id and _slurm_job_is_active(job_id):
            return f"slurm job {job_id} is still active"
    if status in RETRYABLE_STATE and os.getenv("QHH_CONTINUOUS_RETRY_FAILED", "1") != "1":
        return "state file retry disabled"
    if not state:
        return None
    if state.get("source_id") == candidate.source_id and state.get("cycle_time") == candidate.token:
        return "state file already terminal" if status in TERMINAL_SUCCESS else None
    return None


def _should_skip(candidate: CandidateCycle, state: dict[str, Any]) -> bool:
    return _skip_reason(candidate, state, executor=os.getenv("QHH_CONTINUOUS_EXECUTOR", "local")) is not None


def _slurm_job_is_active(job_id: str) -> bool:
    running = subprocess.run(
        ["squeue", "-h", "-j", job_id, "-o", "%T"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if running.returncode == 0 and running.stdout.strip():
        return True
    accounting = subprocess.run(
        ["sacct", "-n", "-P", "-j", job_id, "-o", "State"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    terminal = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED"}
    states = [line.split("|", 1)[0].strip().split()[0] for line in accounting.stdout.splitlines() if line.strip()]
    return bool(states) and all(state not in terminal for state in states)


def _state_file(state_root: Path, candidate: CandidateCycle) -> Path:
    return state_root / "cycles" / candidate.source_segment / f"{candidate.token}.json"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"status": "failed", "reason": "state json is invalid", "state_path": str(path)}


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"qhh continuous runner is already active: {path}", file=sys.stderr)
        raise SystemExit(1)
    return handle


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _has_failed(summary: dict[str, Any]) -> bool:
    return any(item.get("status") == "failed" for item in summary.get("results", []))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
