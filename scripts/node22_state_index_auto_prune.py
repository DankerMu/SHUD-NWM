#!/usr/bin/env python
"""Daily node-22 state-index ``prune-retention`` auto-enforce at the scheduler pass boundary (#3121).

Flow (one run, no retries):

1. Import the repair CLI; derive the window exactly as the capacity watch does
   (``max(21, floor_hours // 24 + 1)`` from ``NHMS_SCHEDULER_CYCLE_LAG_HOURS``,
   the watch's own ``_retention_window``); validate the receipt root and the
   wait/poll settings. Nothing is touched yet.
2. Dry-run. No lane at the capacity warning threshold → ``skip``, exit 0. The
   pre-gate (both lanes ``checksum_valid`` and non-empty, every over-threshold
   lane ``removed_count > 0``) fails → ``gate_failed_precheck``, exit 1.
3. A running ``node22-run-cycle-once.sh`` wrapper (the runbook's anchored
   ``pgrep``) → ``deferred_wrapper_running``, exit 1. pgrep that cannot answer
   → exit 2. No timer is touched in steps 1-3.
4. Stop ``nhms-compute-scheduler.timer`` and
   ``nhms-scheduler-file-provider-refresh.timer`` — only the ones that are
   ``active`` — then wait (bounded) until both services are idle: ``is-active``
   ``inactive``/``failed`` and no queued job (``show -p Job --value`` empty).
5. Re-check the wrapper and both services, then a final dry-run with the same
   gate (``gate_failed``, exit 1).
6. Enforce. SIGTERM/SIGINT/SIGHUP received during enforce are only recorded;
   the repair call is never interrupted between its two lane CASes.
7. Always: start again every timer this run stopped, exactly once, with all
   three signals ignored while doing so. A timer this run did not stop is
   never started.

Environment:

* ``OBJECT_STORE_ROOT`` / ``NHMS_OBJECT_STORE_COPYBACK_ROOT`` / ``OBJECT_STORE_PREFIX``
  / ``NHMS_SCHEDULER_CYCLE_LAG_HOURS`` /
  ``NHMS_SCHEDULER_STATE_INDEX_REPAIR_ARCHIVE_ROOT`` /
  ``NHMS_SCHEDULER_STATE_INDEX_REPAIR_RECEIPT_ROOT`` — as for the repair CLI
* ``NHMS_STATE_INDEX_AUTO_PRUNE_RECEIPT_ROOT`` — existing owner-private receipt
  directory (never created here)
* ``NHMS_STATE_INDEX_AUTO_PRUNE_WAIT_SECONDS`` (default 18000) /
  ``NHMS_STATE_INDEX_AUTO_PRUNE_POLL_SECONDS`` (default 30)
* ``NHMS_NODE22_SYSTEMCTL`` (default ``systemctl --user``) /
  ``NHMS_STATE_INDEX_AUTO_PRUNE_PGREP`` (default the anchored wrapper pgrep)

Exit codes: ``0`` skip or enforced; ``1`` wrapper running, gate failed, or
still over threshold after enforce (``enforced_unprunable``); ``2`` refusal,
undeterminable unit/wrapper state, timer stop failure, wait timeout, or a
receipt that could not be written after an otherwise-0 run; ``3`` enforce
incomplete (a lane may have been replaced); ``4`` unexpected failure; ``5`` a
timer this run stopped could not be started again (overrides every other
code); ``128+n`` the run was ended by signal ``n``. The receipt JSON is printed
to stdout before the files are written.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

SCHEMA_VERSION = "nhms.scheduler.state_index_auto_prune_receipt.v1"
RECEIPT_ROOT_ENV = "NHMS_STATE_INDEX_AUTO_PRUNE_RECEIPT_ROOT"
WAIT_SECONDS_ENV = "NHMS_STATE_INDEX_AUTO_PRUNE_WAIT_SECONDS"
POLL_SECONDS_ENV = "NHMS_STATE_INDEX_AUTO_PRUNE_POLL_SECONDS"
PGREP_ENV = "NHMS_STATE_INDEX_AUTO_PRUNE_PGREP"
SYSTEMCTL_ENV = "NHMS_NODE22_SYSTEMCTL"
DEFAULT_SYSTEMCTL = "systemctl --user"
# Must stay below the refresh-timer-health stopped dwell (8 h).
DEFAULT_WAIT_SECONDS = 18000
DEFAULT_POLL_SECONDS = 30
SUBPROCESS_TIMEOUT_SECONDS = 60
# TimeoutStopSec budget after systemd's SIGTERM: remaining enforce (measured
# 14 s, x4) + one start per timer + receipt write.
ENFORCE_STOP_BUDGET_SECONDS = 60
SCHEDULER_TIMER = "nhms-compute-scheduler.timer"
SCHEDULER_SERVICE = "nhms-compute-scheduler.service"
REFRESH_TIMER = "nhms-scheduler-file-provider-refresh.timer"
REFRESH_SERVICE = "nhms-scheduler-file-provider-refresh.service"
TIMERS = (SCHEDULER_TIMER, REFRESH_TIMER)
SERVICES = (SCHEDULER_SERVICE, REFRESH_SERVICE)
# The runbook's anchored pattern (known-issues-state-index 8.12).
WRAPPER_PGREP_PATTERN = r"^(/[^ ]*/)?bash( -[^ ]+)* [^ ]*node22-run-cycle-once\.sh( |$)"
LANES = ("reference", "destination")
PREIMAGE_CHANGED = "provider_preimage_changed"
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 5
KEEP_RECEIPTS = 30
LATEST_NAME = "latest.json"
MAX_RECEIPT_BYTES = 1024 * 1024
_RECEIPT_NAME = re.compile(r"^\d{8}T\d{6}Z\.json$")
_IDLE = frozenset({"inactive", "failed"})
_RUNNING = frozenset({"active", "activating", "deactivating", "reloading"})
_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
_STATUS = {0: "ok", 1: "alert", 2: "refused", 3: "incomplete", 4: "error", 5: "timer_restore_failed"}


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _monotonic() -> float:
    return time.monotonic()


def _now() -> datetime:
    return datetime.now(tz=UTC)


class _Abort(Exception):
    """End the flow with ``exit_code`` / ``action``; the restore still runs."""

    def __init__(self, exit_code: int, action: str, reason: str, **details: object) -> None:
        super().__init__(reason)
        self.exit_code = exit_code
        self.action = action
        self.error: dict[str, object] = {"reason": reason, **details}


class _Run:
    def __init__(self) -> None:
        self.stopped: list[str] = []
        self.restored: list[str] = []
        self.restore_failed: list[str] = []
        self.waited = 0.0
        self.gate: dict[str, object] | None = None
        self.enforce: dict[str, object] | None = None
        self.signal: int | None = None
        self.deferring = False


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)


def main(argv: list[str] | None = None) -> int:
    started = _now()
    try:
        # Imported here so an import failure is exit 4, never an alert-shaped exit.
        from packages.common import safe_fs
        from packages.common.state_manager import (
            STATE_INDEX_CAPACITY_WARNING_THRESHOLD,
            minimum_state_index_retention_days,
        )
        from scripts import scheduler_state_index_repair as repair
        from scripts.node22_state_index_capacity_watch import _retention_window
    except Exception as error:  # noqa: BLE001 - any import failure is exit 4
        header = {"retention_days": None, "retention_days_source": None, "warning_threshold": None}
        return _publish(_receipt(started, _Run(), 4, "error", _unexpected(error), header), None, None)
    build_parser().parse_args(argv)
    retention_days, source = _retention_window(repair, minimum_state_index_retention_days)
    header = {
        "retention_days": retention_days,
        "retention_days_source": source,
        "warning_threshold": STATE_INDEX_CAPACITY_WARNING_THRESHOLD,
    }
    run = _Run()
    previous = _install_signal_handlers(run)
    try:
        root: Path | None = None
        exit_code, action, error = 4, "error", None
        try:
            root = repair._private_directory(
                os.getenv(RECEIPT_ROOT_ENV, ""), env=RECEIPT_ROOT_ENV, required=True, kind="receipt"
            )
            exit_code, action, error = _flow(run, repair, retention_days)
        except _Abort as abort:
            exit_code, action, error = abort.exit_code, abort.action, abort.error
        except repair.RepairCliError as refusal:
            exit_code, action, error = 2, "refused", _error(refusal)
        except SystemExit as exit_:
            exit_code, action = int(exit_.code or 0), "interrupted"
        except Exception as unexpected:  # noqa: BLE001 - a programming error must not look like an alert
            exit_code, action, error = 4, "error", _unexpected(unexpected)
        finally:
            _ignore_signals()
            _restore_timers(run)
        if run.restore_failed:
            exit_code = 5
        elif run.signal is not None:
            exit_code = 128 + run.signal
        return _publish(_receipt(started, run, exit_code, action, error, header), root, safe_fs)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _flow(run: _Run, repair: ModuleType, retention_days: int) -> tuple[int, str, dict[str, object] | None]:
    wait_seconds = _wait_setting(WAIT_SECONDS_ENV, DEFAULT_WAIT_SECONDS, integer=True)
    poll_seconds = _wait_setting(POLL_SECONDS_ENV, DEFAULT_POLL_SECONDS, integer=False)
    summary = _dry_run(repair, retention_days)
    lanes = _lanes(summary)
    if all(lane is not None for lane in lanes.values()) and not any(_over(lane) for lane in lanes.values()):
        return 0, "skip", None
    run.gate = _gate(lanes, "precheck")
    if not run.gate["passed"]:
        return 1, "gate_failed_precheck", None
    if _wrapper_running():
        return 1, "deferred_wrapper_running", None

    _freeze(run)
    _wait_idle(run, wait_seconds, poll_seconds)
    if _wrapper_running():
        return 1, "deferred_wrapper_running", {"reason": "wrapper_running_at_gate"}
    if not _services_idle():
        raise _Abort(2, "refused", "service_busy_at_gate")
    final = _lanes(_dry_run(repair, retention_days))
    run.gate = _gate(final, "final")
    if not run.gate["passed"]:
        return 1, "gate_failed", None

    # From here until the restore ignores them, signals are only recorded: an
    # exception must never land between the two lane CASes.
    run.deferring = True
    try:
        result = _repair(repair, retention_days, enforce=True)
    except repair.RepairIncompleteError as error:
        run.enforce = _enforce_block(error.summary, run.gate)
        return 3, "enforce_incomplete", _error(error)
    except repair.RepairCliError as error:
        return 2, "refused", _error(error)
    run.enforce = _enforce_block(result, run.gate)
    if any(lane["capacity_after_warning"] for lane in run.enforce["lanes"].values() if lane):
        return 1, "enforced_unprunable", None
    return 0, "enforced", None


# --- configuration ------------------------------------------------------------


def _wait_setting(env: str, default: float, *, integer: bool) -> float:
    raw = os.getenv(env, "").strip()
    if not raw:
        return default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError:
        value = -1
    if value < 0 or (not integer and value <= 0):
        raise _Abort(2, "refused", "auto_prune_setting_invalid", field=env)
    return value


# --- repair calls -------------------------------------------------------------


def _repair(repair: ModuleType, retention_days: int, *, enforce: bool) -> dict[str, object]:
    return repair.repair_state_index(
        operation="prune-retention",
        reference_root=None,
        destination_root=None,
        object_store_prefix=None,
        enforce=enforce,
        lane=None,
        state_id=None,
        run_id=None,
        model_id=None,
        source_id=None,
        valid_time=None,
        allow_missing_reference=False,
        allow_missing_destination=False,
        retention_days=retention_days,
        cycle_lag_hours=None,
    )


def _dry_run(repair: ModuleType, retention_days: int) -> dict[str, object]:
    attempts = 0
    while True:
        attempts += 1
        try:
            return _repair(repair, retention_days, enforce=False)
        except repair.RepairIncompleteError as error:
            raise _Abort(3, "dry_run_incomplete", error.reason) from error
        except repair.RepairCliError as error:
            if error.reason != PREIMAGE_CHANGED or attempts >= MAX_ATTEMPTS:
                raise _Abort(2, "refused", **_error(error)) from error
            _sleep(RETRY_DELAY_SECONDS)


def _lanes(summary: object) -> dict[str, dict[str, object] | None]:
    raw = summary.get("lanes") if isinstance(summary, dict) and isinstance(summary.get("lanes"), dict) else {}
    lanes: dict[str, dict[str, object] | None] = {}
    for name in LANES:
        lane = raw.get(name)
        ok = isinstance(lane, dict) and isinstance(lane.get("retention"), dict)
        lanes[name] = lane if ok else None
    return lanes


def _capacity(lane: dict[str, object], key: str) -> dict[str, object]:
    value = lane["retention"].get(key)
    return value if isinstance(value, dict) else {}


def _over(lane: dict[str, object] | None) -> bool:
    return lane is not None and _capacity(lane, "capacity_before").get("warning") is True


def _gate(lanes: dict[str, dict[str, object] | None], phase: str) -> dict[str, object]:
    failures: list[dict[str, str]] = []
    summary: dict[str, object] = {}
    for name, lane in lanes.items():
        if lane is None:
            failures.append({"lane": name, "reason": "lane_summary_missing"})
            summary[name] = None
            continue
        retention = lane["retention"]
        entries = retention.get("entry_count_before")
        removed = retention.get("removed_count")
        over = _over(lane)
        summary[name] = {
            "checksum_valid": lane.get("checksum_valid"),
            "entry_count_before": entries,
            "removed_count": removed,
            "removed_state_ids_sha256": retention.get("removed_state_ids_sha256"),
            "over_threshold": over,
        }
        if lane.get("checksum_valid") is not True:
            failures.append({"lane": name, "reason": "checksum_invalid"})
        if not isinstance(entries, int) or entries <= 0:
            failures.append({"lane": name, "reason": "lane_empty"})
        if over and (not isinstance(removed, int) or removed <= 0):
            failures.append({"lane": name, "reason": "over_threshold_nothing_to_prune"})
    return {"phase": phase, "passed": not failures, "failures": failures, "lanes": summary}


def _enforce_block(summary: object, gate: dict[str, object] | None) -> dict[str, object]:
    gate_lanes = (gate or {}).get("lanes") or {}
    lanes: dict[str, object] = {}
    for name, lane in _lanes(summary).items():
        if lane is None:
            lanes[name] = None
            continue
        retention = lane["retention"]
        digest = retention.get("removed_state_ids_sha256")
        gated = gate_lanes.get(name) or {}
        lanes[name] = {
            "status": lane.get("status"),
            "removed_count": retention.get("removed_count"),
            "removed_state_ids_sha256": digest,
            "digest_matches_gate": digest == gated.get("removed_state_ids_sha256"),
            "capacity_before": _capacity(lane, "capacity_before"),
            "capacity_after": _capacity(lane, "capacity_after"),
            "capacity_after_warning": _capacity(lane, "capacity_after").get("warning") is True,
        }
    status = summary.get("status") if isinstance(summary, dict) else None
    return {"status": status, "lanes": lanes}


# --- systemctl / pgrep --------------------------------------------------------


def _systemctl(*args: str, capture: bool) -> tuple[int, str]:
    """``(returncode, stripped stdout)``; ``(-1, "")`` when the call itself failed or timed out."""
    command = [*shlex.split(os.getenv(SYSTEMCTL_ENV) or DEFAULT_SYSTEMCTL), *args]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return result.returncode, (result.stdout or "").strip()


def _unit_state(unit: str) -> str:
    # The printed state, never the exit code: is-active exits 3 for inactive/failed.
    return _systemctl("is-active", unit, capture=True)[1]


def _wrapper_running() -> bool:
    override = os.getenv(PGREP_ENV)
    command = shlex.split(override) if override else ["pgrep", "-u", str(os.getuid()), "-f", WRAPPER_PGREP_PATTERN]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise _Abort(2, "refused", "wrapper_state_undeterminable", error_type=type(error).__name__) from error
    if result.returncode in (0, 1):
        return result.returncode == 0
    raise _Abort(2, "refused", "wrapper_state_undeterminable", returncode=result.returncode)


def _freeze(run: _Run) -> None:
    for timer in TIMERS:
        state = _unit_state(timer)
        if state == "active":
            run.stopped.append(timer)  # registered before stop: a failed/interrupted stop is still restored
            if _systemctl("stop", timer, capture=False)[0] != 0:
                raise _Abort(2, "refused", "timer_stop_failed", unit=timer)
            _log(f"timer_stopped unit={timer}")
        elif state in _IDLE:
            _log(f"timer_left_alone unit={timer} state={state}")
        else:
            raise _Abort(2, "refused", "timer_state_undeterminable", unit=timer, state=state[:64])


def _service_idle(service: str) -> bool:
    state = _unit_state(service)
    if state in _RUNNING:
        return False
    if state not in _IDLE:
        raise _Abort(2, "refused", "service_state_undeterminable", unit=service, state=state[:64])
    returncode, job = _systemctl("show", "-p", "Job", "--value", service, capture=True)
    if returncode != 0:
        raise _Abort(2, "refused", "service_state_undeterminable", unit=service, state=state)
    return job == ""


def _services_idle() -> bool:
    return all(_service_idle(service) for service in SERVICES)


def _wait_idle(run: _Run, wait_seconds: float, poll_seconds: float) -> None:
    start = _monotonic()
    while not _services_idle():
        run.waited = round(_monotonic() - start, 3)
        if run.waited >= wait_seconds:
            raise _Abort(2, "wait_timeout", "wait_timeout", limit_seconds=wait_seconds)
        _sleep(poll_seconds)
    run.waited = round(_monotonic() - start, 3)
    _log(f"services_idle waited={run.waited}")


def _restore_timers(run: _Run) -> None:
    for timer in dict.fromkeys(run.stopped):
        if _systemctl("start", timer, capture=False)[0] == 0:
            run.restored.append(timer)
            _log(f"timer_restored unit={timer}")
        else:
            run.restore_failed.append(timer)
            _log(f"timer_restore_failed unit={timer}; restore manually: systemctl --user start {timer}")


# --- signals ------------------------------------------------------------------


def _install_signal_handlers(run: _Run) -> dict[int, object]:
    previous = {signum: signal.getsignal(signum) for signum in _SIGNALS}

    def handler(signum: int, _frame: object) -> None:
        if run.signal is None:
            run.signal = signum
        if run.deferring:
            return
        _ignore_signals()
        raise SystemExit(128 + signum)

    for signum in _SIGNALS:
        signal.signal(signum, handler)
    return previous


def _ignore_signals() -> None:
    for signum in _SIGNALS:
        signal.signal(signum, signal.SIG_IGN)


# --- receipt ------------------------------------------------------------------


def _receipt(
    started: datetime,
    run: _Run,
    exit_code: int,
    action: str,
    error: dict[str, object] | None,
    header: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "started_at": _format_utc_seconds(started),
        "finished_at": _format_utc_seconds(_now()),
        "status": _STATUS.get(exit_code, "interrupted"),
        "action": action,
        "exit_code": exit_code,
        **header,
        "cycle_lag_hours": _cycle_lag_hours(),
        "timers_stopped": list(dict.fromkeys(run.stopped)),
        "timers_restored": run.restored,
        "timers_restore_failed": run.restore_failed,
        "waited_seconds": run.waited,
        "gate": run.gate,
        "enforce": run.enforce,
        "error": error,
        "signal": signal.Signals(run.signal).name if run.signal is not None else None,
    }


def _cycle_lag_hours() -> int | None:
    try:
        return int(os.getenv("NHMS_SCHEDULER_CYCLE_LAG_HOURS", "").strip())
    except ValueError:
        return None


def _error(error: object) -> dict[str, object]:
    details = error.details
    return {"reason": error.reason, "field": details.get("field") or details.get("env")}


def _unexpected(error: BaseException) -> dict[str, object]:
    return {"reason": "unexpected_exception", "error_type": type(error).__name__}


def _log(message: str) -> None:
    try:
        print(f"node22-state-index-auto-prune: {message}", file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def _publish(receipt: dict[str, object], root: Path | None, safe_fs: object) -> int:
    exit_code = int(receipt["exit_code"])
    try:
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True), flush=True)
    except (OSError, ValueError):
        pass
    if root is None:
        return exit_code
    try:
        _write_receipt(root, receipt, safe_fs)
    except Exception as error:  # noqa: BLE001 - any write/rotation failure is reported
        _log(f"receipt_write_failed error_type={type(error).__name__}")
        return 2 if exit_code == 0 else exit_code
    return exit_code


def _write_receipt(root: Path, receipt: dict[str, object], safe_fs: object) -> None:
    content = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    if len(content) > MAX_RECEIPT_BYTES:
        raise ValueError(f"receipt is {len(content)} bytes")
    name = str(receipt["started_at"]).replace("-", "").replace(":", "") + ".json"
    safe_fs.atomic_write_bytes_no_follow(root / name, content, containment_root=root, mode=0o600)
    safe_fs.atomic_write_bytes_no_follow(root / LATEST_NAME, content, containment_root=root, mode=0o600)
    stamped = sorted(
        entry
        for entry in os.listdir(root)
        if _RECEIPT_NAME.match(entry) and stat.S_ISREG(os.lstat(root / entry).st_mode)
    )
    for stale in stamped[:-KEEP_RECEIPTS]:
        os.unlink(root / stale)


def _format_utc_seconds(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
