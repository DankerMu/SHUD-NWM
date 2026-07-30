#!/usr/bin/env python3
"""Serial six-basin production replay driver (#1164 change 2, design D5).

Drives one source's replay cycle by cycle, strictly serially, and maintains the
schema-versioned replacement receipt
(``schemas/production_replay_replacement_receipt.schema.json``) that makes the
controlled overwrite traceable.

Per cycle, in this order:

1. **Pre-capture** (before the cycle is submitted, while the OLD run tree is
   still intact): prior run-manifest sha256, prior output inventory digests,
   the prior terminal journal job id, and -- UNCONDITIONALLY -- the forcing
   package and model package checksums that cycle consumed.  The prior STATE
   fields come from the reset receipt's ``removed_entries``: the state scope was
   cleared before the replay started, so reading the live index would silently
   yield nothing.  A reset receipt that cannot supply them is a hard error, not
   a default.
2. **Pre-stage**: copy the NFS forcing package into the scratch object store
   when the scratch copy is missing, verifying every file's sha256 after write.
3. **Submit**: ``plan-production --cycle-time <T> --source <s> --model-id x6
   --disable-backfill --submit`` (single-pass semantics; ``--max-passes`` is
   meaningless without ``--continuous`` and is never passed).  The GFS
   2026-07-07T12 cycle switches to the pre-existing repair parameter set.
4. **Wait**: the cycle is complete only when the journal shows terminal success
   for every replay model AND the NFS state index holds one fresh successor
   entry (``valid_time == T + 12h``, ``created_at`` after the pass started) per
   model.  Timeout or failure halts the sequence with the interruption recorded;
   nothing is skipped automatically.
5. **Post-capture**: new manifest sha256, new state checksum, ``init_mode`` /
   ``quality`` / ``packaged_ic_checksum``, plus the river-network-version and
   variable key-consistency assertion.  The first replayed cycle MUST show
   ``init_mode=3`` + ``quality=packaged_calibrated_state`` + a non-empty
   ``packaged_ic_checksum``; anything else halts the driver.

Dry run is the default: it performs the census, the pre-capture, and the staging
plan, and submits nothing.  ``--execute`` runs the real sequence, and refuses to
start unless the effective environment disables retention
(``NHMS_RETENTION_ENABLED=false``) -- an enabled retention pass would delete the
very historical cycles being replayed.

Resumption (``--resume-from <receipt>``) never blind-skips: a cycle is skipped
only when the state index still holds the recorded new state entry AND its
checksum matches the receipt row.

Exit codes: ``0`` success (or dry run), ``2`` refusal before any submission,
``3`` halted mid-sequence with the interruption recorded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    read_bytes_limited_no_follow,
)
from packages.common.source_identity import normalize_source_id
from scripts.replay_capture import (
    MAX_DIGEST_FILE_BYTES,
    MAX_RECEIPT_BYTES,
    PROBE_PRESENT,
    ReplayDriverRefused,
    _DigestUndeterminable,
    _iter_files,
    _object_key,
    _read_json,
    _run_root,
    _sha256_file,
    directory_digest,
    key_consistency,
    load_reset_removed_entries,
    manifest_summary,
    read_state_index_entries,
    run_capture,
    run_id_for,
    successor_state_entries,
)
from services.orchestrator.scheduler_replay import find_forcing_package_dir

SCHEMA_VERSION = "nhms.production_replay_replacement.v1"
_ROOT = Path(__file__).resolve().parents[1]
RECEIPT_SCHEMA_PATH = _ROOT / "schemas/production_replay_replacement_receipt.schema.json"

RETENTION_ENV = "NHMS_RETENTION_ENABLED"
REPAIR_ENV = "NHMS_SCHEDULER_REPAIR_MISSING_FORCING"
REPAIR_CYCLE_ENV = "NHMS_SCHEDULER_REPAIR_MISSING_FORCING_CYCLE_TIME"

PACKAGED_IC_QUALITY = "packaged_calibrated_state"
PACKAGED_IC_INIT_MODE = 3
SUCCESSOR_LEAD_HOURS = 12
DEFAULT_CYCLE_TIMEOUT_SECONDS = 90 * 60
DEFAULT_POLL_SECONDS = 60.0

#: The one cycle whose forcing packages are gone while its raw manifest is not:
#: it runs through the pre-existing repair surface instead of the replay branch.
REPAIR_CYCLES: frozenset[tuple[str, str]] = frozenset({("gfs", "2026070712")})

class ReplayHalted(RuntimeError):
    """The sequence stopped mid-flight; the receipt records the interruption."""

    def __init__(self, reason: str, *, receipt: Mapping[str, Any], details: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.receipt: dict[str, Any] = dict(receipt)
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"outcome": "halted", "halt_reason": self.reason, **self.details}


@dataclass(frozen=True)
class ReplayDriverConfig:
    source_id: str
    model_ids: tuple[str, ...]
    cycles: tuple[datetime, ...]
    nfs_root: Path
    scratch_root: Path
    state_index: Path
    journal_root: Path
    reset_receipt: Path
    receipt_path: Path
    registry_manifest: Path | None = None
    object_store_prefix: str = ""
    execute: bool = False
    cycle_timeout_seconds: int = DEFAULT_CYCLE_TIMEOUT_SECONDS
    poll_seconds: float = DEFAULT_POLL_SECONDS
    resume_from: Path | None = None
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))



# ---------------------------------------------------------------------------
# submission and convergence
# ---------------------------------------------------------------------------


def default_submit_pass(argv: Sequence[str], env: Mapping[str, str]) -> dict[str, Any]:
    completed = subprocess.run(  # noqa: S603 - fixed argv built below, no shell
        list(argv),
        env=dict(env),
        capture_output=True,
        text=True,
        check=False,
        cwd=str(_ROOT),
    )
    return {
        "returncode": completed.returncode,
        "stdout_tail": (completed.stdout or "")[-4000:],
        "stderr_tail": (completed.stderr or "")[-4000:],
    }


def submit_argv(config: ReplayDriverConfig, cycle_time: datetime) -> list[str]:
    argv = [
        sys.executable,
        "-m",
        "services.orchestrator.cli",
        "plan-production",
        "--source",
        config.source_id,
        "--cycle-time",
        _format_time(cycle_time),
        "--disable-backfill",
    ]
    for model_id in config.model_ids:
        argv.extend(["--model-id", model_id])
    argv.append("--submit")
    return argv


def submit_env(config: ReplayDriverConfig, cycle_time: datetime) -> dict[str, str]:
    """Environment for one cycle's pass; 070712 switches to the repair set."""

    env = dict(config.env)
    token = cycle_time.strftime("%Y%m%d%H")
    if (normalize_source_id(config.source_id).lower(), token) in REPAIR_CYCLES:
        env[REPAIR_ENV] = "1"
        env[REPAIR_CYCLE_ENV] = _format_time(cycle_time)
    else:
        env[REPAIR_ENV] = "false"
        env.pop(REPAIR_CYCLE_ENV, None)
    return env


def default_journal_probe(config: ReplayDriverConfig, cycle_time: datetime) -> dict[str, bool]:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = FileOrchestrationJournalRepository(str(config.journal_root))
    return {
        model_id: bool(
            repository.has_completed_pipeline(
                source_id=config.source_id,
                cycle_time=cycle_time,
                model_id=model_id,
            )
        )
        for model_id in config.model_ids
    }


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


def run_replay(
    config: ReplayDriverConfig,
    *,
    submit_pass: Callable[[Sequence[str], Mapping[str, str]], dict[str, Any]] = default_submit_pass,
    journal_probe: Callable[[ReplayDriverConfig, datetime], Mapping[str, bool]] = default_journal_probe,
    clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    started_at = clock()
    _assert_retention_disabled(config)
    removed_entries = load_reset_removed_entries(config.reset_receipt)
    resumed_rows = _load_resume_rows(config)

    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "outcome": "completed",
        "pass_id": f"replay-{normalize_source_id(config.source_id).lower()}-{started_at.strftime('%Y%m%dT%H%M%SZ')}",
        "executed": bool(config.execute),
        "source_id": normalize_source_id(config.source_id),
        "model_ids": list(config.model_ids),
        "started_at": _format_time(started_at),
        "finished_at": None,
        "reset_receipt": {
            "path": str(config.reset_receipt),
            "sha256": _safe_file_sha256(config.reset_receipt),
            "removed_entry_count": len(removed_entries),
        },
        "inventory_census": _inventory_census(config),
        "cycles": [cycle.strftime("%Y%m%d%H") for cycle in config.cycles],
        "rows": [],
        "interruption": None,
    }

    for cycle_time in config.cycles:
        verified = _verified_resume_skip(config, cycle_time, resumed_rows)
        if verified is not None:
            receipt["rows"].extend(verified)
            continue
        rows = [
            _pre_capture_row(config, cycle_time, model_id, removed_entries=removed_entries)
            for model_id in config.model_ids
        ]
        for row in rows:
            row["staging"] = _stage_forcing(config, cycle_time, row["model_id"])
        if not config.execute:
            for row in rows:
                row["status"] = "planned"
            receipt["rows"].extend(rows)
            continue

        pass_started_at = clock()
        submission = submit_pass(submit_argv(config, cycle_time), submit_env(config, cycle_time))
        for row in rows:
            row["submission"] = {
                "returncode": int(submission.get("returncode", 1)),
                "repair_parameter_set": submit_env(config, cycle_time).get(REPAIR_ENV) == "1",
                "submitted_at": _format_time(pass_started_at),
            }
        if int(submission.get("returncode", 1)) != 0:
            _halt(receipt, config, rows, cycle_time, reason="submission_failed", detail=submission)

        convergence = _wait_for_convergence(
            config,
            cycle_time,
            since=pass_started_at,
            journal_probe=journal_probe,
            clock=clock,
            sleep=sleep,
        )
        for row in rows:
            row["convergence"] = {
                "journal_terminal": bool(convergence["journal"].get(row["model_id"])),
                "state_entry_present": row["model_id"] in convergence["state_entries"],
                "waited_seconds": convergence["waited_seconds"],
            }
        if not convergence["converged"]:
            _halt(receipt, config, rows, cycle_time, reason="convergence_timeout", detail=convergence["detail"])

        for row in rows:
            _post_capture_row(config, cycle_time, row, state_entry=convergence["state_entries"].get(row["model_id"]))
            failure = _row_assertion_failure(row)
            if failure is not None:
                receipt["rows"].extend(rows)
                _halt(receipt, config, [], cycle_time, reason=failure, detail={"model_id": row["model_id"]})
            row["status"] = "completed"
        receipt["rows"].extend(rows)

    receipt["finished_at"] = _format_time(clock())
    _write_receipt(config.receipt_path, receipt)
    return receipt


def _halt(
    receipt: dict[str, Any],
    config: ReplayDriverConfig,
    rows: Sequence[dict[str, Any]],
    cycle_time: datetime,
    *,
    reason: str,
    detail: Any,
) -> None:
    for row in rows:
        row["status"] = "halted"
    receipt["rows"].extend(rows)
    receipt["outcome"] = "halted"
    receipt["interruption"] = {
        "cycle": cycle_time.strftime("%Y%m%d%H"),
        "reason": reason,
        "detail": json.loads(json.dumps(detail, default=str)) if detail is not None else None,
        "recorded_at": _format_time(datetime.now(tz=UTC)),
    }
    receipt["finished_at"] = _format_time(datetime.now(tz=UTC))
    _write_receipt(config.receipt_path, receipt)
    raise ReplayHalted(reason, receipt=receipt, details={"cycle": cycle_time.strftime("%Y%m%d%H")})


def _assert_retention_disabled(config: ReplayDriverConfig) -> None:
    value = str(config.env.get(RETENTION_ENV, "")).strip().lower()
    if value not in {"false", "0", "no", "off"}:
        raise ReplayDriverRefused(
            "retention_not_disabled",
            {"env": RETENTION_ENV, "value": config.env.get(RETENTION_ENV)},
        )


def _inventory_census(config: ReplayDriverConfig) -> dict[str, Any]:
    """On-site inventory at start time -- never a frozen survey count."""

    try:
        index_entries = read_state_index_entries(config.state_index)
    except ReplayDriverRefused:
        index_entries = []
    per_scope: list[dict[str, Any]] = []
    for model_id in config.model_ids:
        run_cycles = sorted(
            cycle.strftime("%Y%m%d%H")
            for cycle in config.cycles
            if _run_root(config.nfs_root, run_id_for(config.source_id, cycle, model_id)).is_dir()
        )
        entries = [
            entry
            for entry in index_entries
            if str(entry.get("model_id") or "") == model_id
            and normalize_source_id(str(entry.get("source_id") or "")) == normalize_source_id(config.source_id)
        ]
        per_scope.append(
            {
                "model_id": model_id,
                "source_id": normalize_source_id(config.source_id),
                "run_cycle_count": len(run_cycles),
                "run_cycles": run_cycles,
                "state_index_entry_count": len(entries),
            }
        )
    return {
        "state_index": str(config.state_index),
        "scopes": per_scope,
        "frontier_cycle": config.cycles[-1].strftime("%Y%m%d%H") if config.cycles else None,
    }


def _pre_capture_row(
    config: ReplayDriverConfig,
    cycle_time: datetime,
    model_id: str,
    *,
    removed_entries: Mapping[tuple[str, str, str], Mapping[str, Any]],
) -> dict[str, Any]:
    run_id = run_id_for(config.source_id, cycle_time, model_id)
    capture = run_capture(config.nfs_root, run_id)
    prior_summary = manifest_summary(capture["manifest"])
    successor_valid_time = cycle_time + timedelta(hours=SUCCESSOR_LEAD_HOURS)
    prior_state = removed_entries.get(
        (model_id, normalize_source_id(config.source_id), _format_time(successor_valid_time))
    )
    forcing_dir = find_forcing_package_dir(
        config.nfs_root,
        source_id=config.source_id,
        cycle_time=cycle_time,
        model_id=model_id,
    )
    return {
        "cycle": cycle_time.strftime("%Y%m%d%H"),
        "cycle_time": _format_time(cycle_time),
        "source_id": normalize_source_id(config.source_id),
        "model_id": model_id,
        "run_id": run_id,
        "status": "planned",
        "prior": {
            "run_manifest_sha256": capture["manifest_sha256"],
            "output_inventory": capture["output_inventory"],
            "no_prior_run": bool(capture["no_prior_run"]),
            "state": dict(prior_state) if prior_state is not None else None,
            "state_source": "reset_receipt",
            "river_network_version_id": prior_summary["river_network_version_id"],
            "variable_keys": prior_summary["variable_keys"],
        },
        # Unconditional per the user instruction ("input checksums"): recorded
        # for every row, including rows with no prior run.
        "inputs": {
            "forcing_package": directory_digest(forcing_dir, label="forcing_package"),
            "model_package": directory_digest(
                _model_package_dir(config, model_id),
                label="model_package",
            ),
        },
        "staging": None,
        "submission": None,
        "convergence": None,
        "new": None,
        "key_consistency": None,
        "bootstrap_assertion": None,
    }


def _model_package_dir(config: ReplayDriverConfig, model_id: str) -> Path | None:
    if config.registry_manifest is None:
        return None
    payload = _read_json(config.registry_manifest, max_bytes=MAX_RECEIPT_BYTES)
    if payload is None:
        return None
    models = payload.get("models")
    if not isinstance(models, Sequence):
        return None
    for model in models:
        if not isinstance(model, Mapping) or str(model.get("model_id") or "") != model_id:
            continue
        uri = str(model.get("model_package_uri") or "")
        if not uri:
            return None
        key = _object_key(uri, config.object_store_prefix)
        if not key or key.startswith("/") or ".." in Path(key).parts:
            return None
        return config.nfs_root / key
    return None


def _stage_forcing(config: ReplayDriverConfig, cycle_time: datetime, model_id: str) -> dict[str, Any]:
    """Copy the NFS forcing package into scratch when scratch lacks it."""

    source_dir = find_forcing_package_dir(
        config.nfs_root,
        source_id=config.source_id,
        cycle_time=cycle_time,
        model_id=model_id,
    )
    if source_dir is None:
        return {"status": "source_absent", "copied_files": 0, "verified": False, "detail": None}
    relative = source_dir.relative_to(config.nfs_root)
    target_dir = config.scratch_root / relative
    if config.nfs_root == config.scratch_root:
        return {"status": "same_root", "copied_files": 0, "verified": True, "detail": None}
    source_digest = directory_digest(source_dir, label="forcing_package")
    target_digest = directory_digest(target_dir, label="forcing_package")
    if target_digest["status"] == PROBE_PRESENT and target_digest["sha256"] == source_digest["sha256"]:
        return {"status": "already_staged", "copied_files": 0, "verified": True, "detail": None}
    if not config.execute:
        return {"status": "stage_planned", "copied_files": 0, "verified": False, "detail": None}
    copied = 0
    try:
        for path in _iter_files(source_dir):
            content = read_bytes_limited_no_follow(path, max_bytes=MAX_DIGEST_FILE_BYTES)
            destination = target_dir / path.relative_to(source_dir)
            ensure_directory_no_follow(destination.parent)
            atomic_write_bytes_no_follow(destination, content, mode=0o644)
            if _sha256_file(destination) != hashlib.sha256(content).hexdigest():
                return {
                    "status": "verify_failed",
                    "copied_files": copied,
                    "verified": False,
                    "detail": str(destination),
                }
            copied += 1
    except (_DigestUndeterminable, OSError, SafeFilesystemError) as error:
        return {"status": "stage_failed", "copied_files": copied, "verified": False, "detail": str(error)}
    staged_digest = directory_digest(target_dir, label="forcing_package")
    return {
        "status": "staged" if staged_digest["sha256"] == source_digest["sha256"] else "verify_failed",
        "copied_files": copied,
        "verified": staged_digest["sha256"] == source_digest["sha256"],
        "detail": None,
    }


def _wait_for_convergence(
    config: ReplayDriverConfig,
    cycle_time: datetime,
    *,
    since: datetime,
    journal_probe: Callable[[ReplayDriverConfig, datetime], Mapping[str, bool]],
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    successor_valid_time = cycle_time + timedelta(hours=SUCCESSOR_LEAD_HOURS)
    deadline = since + timedelta(seconds=config.cycle_timeout_seconds)
    journal: Mapping[str, bool] = {}
    state_entries: dict[str, dict[str, Any]] = {}
    while True:
        journal = dict(journal_probe(config, cycle_time))
        try:
            entries = read_state_index_entries(config.state_index)
        except ReplayDriverRefused:
            entries = []
        state_entries = successor_state_entries(
            entries,
            source_id=config.source_id,
            model_ids=config.model_ids,
            valid_time=successor_valid_time,
            since=since,
        )
        converged = all(journal.get(model_id) for model_id in config.model_ids) and set(state_entries) == set(
            config.model_ids
        )
        now = clock()
        if converged:
            return {
                "converged": True,
                "journal": journal,
                "state_entries": state_entries,
                "waited_seconds": (now - since).total_seconds(),
                "detail": None,
            }
        if now >= deadline:
            return {
                "converged": False,
                "journal": journal,
                "state_entries": state_entries,
                "waited_seconds": (now - since).total_seconds(),
                "detail": {
                    "journal_terminal": sorted(model for model, done in journal.items() if done),
                    "state_entries": sorted(state_entries),
                    "successor_valid_time": _format_time(successor_valid_time),
                },
            }
        sleep(config.poll_seconds)


def _post_capture_row(
    config: ReplayDriverConfig,
    cycle_time: datetime,
    row: dict[str, Any],
    *,
    state_entry: Mapping[str, Any] | None,
) -> None:
    capture = run_capture(config.nfs_root, row["run_id"])
    summary = manifest_summary(capture["manifest"])
    row["new"] = {
        "run_manifest_sha256": capture["manifest_sha256"],
        "output_inventory": capture["output_inventory"],
        "state_id": (state_entry or {}).get("state_id"),
        "state_checksum": (state_entry or {}).get("checksum"),
        "init_mode": summary["init_mode"],
        "quality": summary["quality"],
        "packaged_ic_checksum": summary["packaged_ic_checksum"],
        "river_network_version_id": summary["river_network_version_id"],
        "variable_keys": summary["variable_keys"],
    }
    row["key_consistency"] = key_consistency(row["prior"], row["new"])
    is_first_cycle = bool(config.cycles) and cycle_time == config.cycles[0]
    if not is_first_cycle:
        row["bootstrap_assertion"] = {"required": False, "status": "not_required", "detail": None}
        return
    satisfied = (
        summary["init_mode"] == PACKAGED_IC_INIT_MODE
        and str(summary["quality"] or "") == PACKAGED_IC_QUALITY
        and bool(str(summary["packaged_ic_checksum"] or "").strip())
    )
    row["bootstrap_assertion"] = {
        "required": True,
        "status": "satisfied" if satisfied else "violated",
        "detail": None
        if satisfied
        else (
            f"init_mode={summary['init_mode']} quality={summary['quality']} "
            f"packaged_ic_checksum={'set' if summary['packaged_ic_checksum'] else 'empty'}"
        ),
    }


def _row_assertion_failure(row: Mapping[str, Any]) -> str | None:
    bootstrap = row.get("bootstrap_assertion") or {}
    if bootstrap.get("status") == "violated":
        return "first_cycle_bootstrap_assertion_failed"
    consistency = row.get("key_consistency") or {}
    if consistency.get("status") == "drift":
        return "key_consistency_drift"
    return None


def _load_resume_rows(config: ReplayDriverConfig) -> dict[tuple[str, str], dict[str, Any]]:
    if config.resume_from is None:
        return {}
    payload = _read_json(config.resume_from, max_bytes=MAX_RECEIPT_BYTES)
    if payload is None or payload.get("schema_version") != SCHEMA_VERSION:
        raise ReplayDriverRefused("resume_receipt_unreadable", {"path": str(config.resume_from)})
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for row in payload.get("rows") or []:
        if not isinstance(row, Mapping) or row.get("status") != "completed":
            continue
        rows[(str(row.get("cycle") or ""), str(row.get("model_id") or ""))] = dict(row)
    return rows


def _verified_resume_skip(
    config: ReplayDriverConfig,
    cycle_time: datetime,
    resumed_rows: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[dict[str, Any]] | None:
    """Skip a cycle ONLY on positive evidence; a failed check re-runs it."""

    if not resumed_rows:
        return None
    token = cycle_time.strftime("%Y%m%d%H")
    rows = [resumed_rows.get((token, model_id)) for model_id in config.model_ids]
    if any(row is None for row in rows):
        return None
    try:
        entries = read_state_index_entries(config.state_index)
    except ReplayDriverRefused:
        return None
    present = successor_state_entries(
        entries,
        source_id=config.source_id,
        model_ids=config.model_ids,
        valid_time=cycle_time + timedelta(hours=SUCCESSOR_LEAD_HOURS),
        since=None,
    )
    verified: list[dict[str, Any]] = []
    for row in rows:
        assert row is not None  # narrowed above
        entry = present.get(str(row.get("model_id") or ""))
        recorded = (row.get("new") or {}).get("state_checksum")
        if entry is None or not recorded or str(entry.get("checksum") or "") != str(recorded):
            return None
        verified.append({**dict(row), "status": "verified_skip"})
    return verified


def _safe_file_sha256(path: Path) -> str | None:
    try:
        return _sha256_file(path)
    except (FileNotFoundError, OSError, SafeFilesystemError):
        return None


def _write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    content = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True, default=str).encode("utf-8") + b"\n"
    try:
        ensure_directory_no_follow(path.parent)
        atomic_write_bytes_no_follow(path, content, mode=0o600)
    except (OSError, SafeFilesystemError) as error:  # pragma: no cover - operator-visible best effort
        print(f"receipt write failed: {error}", file=sys.stderr)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------


def cycles_between(start: datetime, end: datetime, *, hours: Sequence[int] = (0, 12)) -> tuple[datetime, ...]:
    resolved: list[datetime] = []
    current = start
    while current <= end:
        if current.hour in set(hours):
            resolved.append(current)
        current += timedelta(hours=1)
    return tuple(resolved)


def _parse_cycle_token(token: str) -> datetime:
    try:
        return datetime.strptime(token.strip(), "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError as error:
        raise ReplayDriverRefused("cycle_token_invalid", {"token": token}) from error


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--model-id", action="append", required=True, dest="model_ids")
    parser.add_argument("--start-cycle", default="2026070500")
    parser.add_argument("--end-cycle", default="2026072100")
    parser.add_argument("--nfs-root", type=Path, required=True)
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument("--state-index", type=Path, required=True)
    parser.add_argument("--journal-root", type=Path, required=True)
    parser.add_argument("--reset-receipt", type=Path, required=True)
    parser.add_argument("--receipt-path", type=Path, required=True)
    parser.add_argument("--registry-manifest", type=Path, default=None)
    parser.add_argument("--object-store-prefix", default=os.getenv("OBJECT_STORE_PREFIX", ""))
    parser.add_argument("--resume-from", type=Path, default=None)
    parser.add_argument("--cycle-timeout-seconds", type=int, default=DEFAULT_CYCLE_TIMEOUT_SECONDS)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the real replay; without it the driver plans and submits nothing.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        config = ReplayDriverConfig(
            source_id=args.source,
            model_ids=tuple(args.model_ids),
            cycles=cycles_between(_parse_cycle_token(args.start_cycle), _parse_cycle_token(args.end_cycle)),
            nfs_root=args.nfs_root,
            scratch_root=args.scratch_root,
            state_index=args.state_index,
            journal_root=args.journal_root,
            reset_receipt=args.reset_receipt,
            receipt_path=args.receipt_path,
            registry_manifest=args.registry_manifest,
            object_store_prefix=args.object_store_prefix,
            execute=bool(args.execute),
            cycle_timeout_seconds=int(args.cycle_timeout_seconds),
            poll_seconds=float(args.poll_seconds),
            resume_from=args.resume_from,
        )
        receipt = run_replay(config)
    except ReplayHalted as error:
        print(json.dumps(error.receipt, ensure_ascii=False, sort_keys=True, default=str))
        print(json.dumps(error.to_dict(), ensure_ascii=False, sort_keys=True, default=str), file=sys.stderr)
        return 3
    except ReplayDriverRefused as error:
        print(json.dumps(error.to_dict(), ensure_ascii=False, sort_keys=True, default=str), file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
