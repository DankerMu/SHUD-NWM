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
   A staging result that is not verified halts the cycle before submission;
   ``source_absent`` is tolerated only for the repair cycles.
3. **Submit**: ``plan-production --cycle-time <T> --source <s> --model-id x6
   --disable-backfill --submit`` (single-pass semantics; ``--max-passes`` is
   meaningless without ``--continuous`` and is never passed).  The GFS
   2026-07-07T12 cycle switches to the pre-existing repair parameter set.
4. **Wait**: the cycle is complete only when, for every replay model, the NFS
   state index holds a successor entry (``valid_time == T + 12h``) whose
   checksum differs from the one the reset receipt archived AND the journal
   supplies terminal evidence -- either a completion job id that was NOT in the
   pre-pass baseline (``new_job``) or, for a model whose replaced successor AND
   terminal attribution a PRIOR pass durably observed, that same replaced
   successor (``prior_pass``).  The second form exists because the scheduler
   refuses to resubmit a model whose successor state is already present:
   demanding a new job id would deadlock every resume after a partially
   successful cycle.  It is bound to the RESUME RECEIPT, not to the live index:
   a model is prior-eligible only when the receipt this pass resumes from
   carries its row AND that row records both halves
   (``convergence.state_entry_present`` true -- the only per-model discriminator
   -- together with that pass's ``terminal_evidence``, or the older
   ``journal_terminal`` on rows that predate it -- or a finished row status), and
   the successor in the index must still carry the checksum that row recorded.
   Without ``--resume-from`` there is no prior evidence at all, so re-running an
   already-replayed cycle dead-ends in ``convergence_timeout`` rather than
   recording the replay's own output as the pre-image.  Neither leg uses a
   timestamp: production state entries carry ``created_at: null`` and the
   original run's terminal record survives the reset.  Timeout, an undecidable
   index, or failure halts the sequence with the interruption recorded; nothing
   is skipped automatically.
5. **Post-capture**: new manifest sha256, new state checksum, ``init_mode`` /
   ``quality`` / ``packaged_ic_checksum``, plus the key-consistency assertion
   over ``river_network_version_id``, the output segment count and the output
   file inventory.  The replay-sequence ORIGIN cycle (``--origin-cycle``,
   2026070500 -- not merely the first cycle of this invocation) MUST show
   ``init_mode=3`` + ``quality=packaged_calibrated_state`` + a non-empty
   ``packaged_ic_checksum``; anything else halts the driver.

Dry run is the default: it performs the census, the pre-capture, and the staging
plan, and submits nothing.  ``--execute`` runs the real sequence, and refuses to
start unless the effective environment disables retention
(``NHMS_RETENTION_ENABLED=false``) -- an enabled retention pass would delete the
very historical cycles being replayed.

Resumption (``--resume-from <receipt>``) is owned end to end by
:func:`_resume_plan` (design D5 v7), which first refuses a receipt from another
``(source, models)`` scope: row keys collide exactly across IFS and GFS, so a
foreign receipt would donate foreign pre-images under this pass's name
(round-4 B4-2).  Every row of the accepted receipt then seeds
:class:`_ReceiptRows`, the single owner of ``receipt["rows"]``; a row is replaced
only when THIS pass produces that key, so a chain of three or more attempts never
loses the pre-image of a cycle a middle attempt did not reach -- whether because
its window was narrower or because it halted (round-4 B4-1).
Disposition is then per ``(cycle, model)``: a row is eligible for the
verified-skip re-check only when it is ``completed``/``verified_skip`` AND
carries no assertion failure (round-3 B3-1), and even then the cycle is skipped
only when the state index still holds the recorded new state entry with the
recorded checksum.  Everything else re-runs with the carried prior
(``prior.prior_source = "resumed_receipt"``), so a drift or bootstrap assertion
re-fires against the ORIGINAL pre-image: the run trees carry no archive, and
re-capturing the prior from a tree the interrupted attempt already replayed
would destroy the only pre-image that ever existed (round-2 B2-1).  The receipt
records its source under ``resume_from`` so a multi-hop chain is auditable, and
the driver refuses to write its receipt to the path it resumes from.  The
replacement receipt is rewritten atomically after every cycle, so an interrupted
sequence leaves the completed rows on disk; an ``in_progress`` receipt is written
BEFORE the first submission, which doubles as the writability pre-flight.

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
    PROBE_UNDETERMINABLE,
    ReplayDriverRefused,
    _DigestUndeterminable,
    _iter_files,
    _object_key,
    _read_json,
    _sha256_file,
    census_run_cycles,
    directory_digest,
    key_consistency,
    load_reset_removed_entries,
    manifest_summary,
    read_state_index_entries,
    replaced_successor_entries,
    run_capture,
    run_id_for,
    successor_state_entries,
    terminal_completion_job_ids,
)
from services.orchestrator.scheduler_replay import find_forcing_package_dir

SCHEMA_VERSION = "nhms.production_replay_replacement.v1"
_ROOT = Path(__file__).resolve().parents[1]
RECEIPT_SCHEMA_PATH = _ROOT / "schemas/production_replay_replacement_receipt.schema.json"

RETENTION_ENV = "NHMS_RETENTION_ENABLED"
REPAIR_ENV = "NHMS_SCHEDULER_REPAIR_MISSING_FORCING"
REPAIR_CYCLE_ENV = "NHMS_SCHEDULER_REPAIR_MISSING_FORCING_CYCLE_TIME"

#: The replacement receipt is CROSS-NODE evidence: node-27 loads all four of them
#: off the shared NFS under a different uid, so this one writer opts out of
#: ``safe_fs``'s 0600 default (round-3 C3-1).  ``safe_fs`` itself and every other
#: receipt writer (the state-scope reset receipt included) are unchanged; the
#: runbook's §2.3.2 ``test -r`` precondition is the operator-side check.
REPLACEMENT_RECEIPT_MODE = 0o644

PACKAGED_IC_QUALITY = "packaged_calibrated_state"
PACKAGED_IC_INIT_MODE = 3
SUCCESSOR_LEAD_HOURS = 12
DEFAULT_CYCLE_TIMEOUT_SECONDS = 90 * 60
DEFAULT_POLL_SECONDS = 60.0

#: The one cycle whose forcing packages are gone while its raw manifest is not:
#: it runs through the pre-existing repair surface instead of the replay branch.
REPAIR_CYCLES: frozenset[tuple[str, str]] = frozenset({("gfs", "2026070712")})

#: Origin of the replay sequence for the six-basin scope.  The bootstrap
#: assertion is bound to THIS cycle, never to ``cycles[0]``: a Phase-2 resume
#: that starts mid-window would otherwise classify a warm cycle as the
#: packaged-IC first cycle and halt on an assertion that cannot hold
#: (round-1 B-P1-2).
DEFAULT_REPLAY_ORIGIN_CYCLE = datetime(2026, 7, 5, 0, tzinfo=UTC)

class ReceiptWriteError(RuntimeError):
    """The replacement receipt could not be written.

    Never swallowed: before the first submission it is a refusal (exit 2), and
    mid-sequence it is a typed halt (exit 3).  A sequence that keeps submitting
    with no receipt on disk is exactly the untraceable overwrite this driver
    exists to prevent (round-2 B2-2).
    """


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
    origin_cycle: datetime = DEFAULT_REPLAY_ORIGIN_CYCLE
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


def default_journal_probe(config: ReplayDriverConfig, cycle_time: datetime) -> dict[str, list[str]]:
    """Terminal completion job ids per model, read-only, never a boolean.

    ``has_completed_pipeline`` is already True before the replay pass starts --
    the reset clears the state index, not the journal, so the ORIGINAL run's
    terminal record survives.  A boolean probe therefore degenerates into "done
    immediately" (round-1 B-P2-7).  The driver baselines the recorded terminal
    job ids before submitting and treats the cycle as complete only once an id
    appears that was not in that baseline.
    """

    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from workers.data_adapters.base import cycle_id_for

    repository = FileOrchestrationJournalRepository(str(config.journal_root))
    try:
        jobs = repository.query_pipeline_jobs_by_cycle(cycle_id_for(config.source_id, cycle_time))
    except (FileOrchestrationJournalError, OSError, ValueError):
        # Unreadable journal is not evidence of completion; the driver keeps
        # waiting and halts on the timeout with the empty probe recorded.
        jobs = []
    return terminal_completion_job_ids(jobs, model_ids=config.model_ids)


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


class _ReceiptRows:
    """The receipt's rows: an ordered ``(cycle, model)``-keyed map (design D5 v7).

    Single owner of ``receipt["rows"]``.  Seeded with EVERY resume-receipt row in
    receipt order, then updated in place: a key this pass produces replaces the
    carried row AT ITS ORIGINAL POSITION, and a key nobody has seen before is
    appended in production order.  ``_halt``, the per-cycle checkpoint and the
    pre-flight write all serialize from here, so no code path can leave a
    partially-carried row list behind (round-4 B4-1).
    """

    def __init__(self, seed: Sequence[Mapping[str, Any]] = ()) -> None:
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.extend(seed)

    @staticmethod
    def _key(row: Mapping[str, Any]) -> tuple[str, str]:
        return (str(row.get("cycle") or ""), str(row.get("model_id") or ""))

    def put(self, row: Mapping[str, Any]) -> None:
        self._rows[self._key(row)] = dict(row)

    def extend(self, rows: Sequence[Mapping[str, Any]]) -> None:
        for row in rows:
            self.put(row)

    def serialize(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows.values()]


def run_replay(
    config: ReplayDriverConfig,
    *,
    submit_pass: Callable[[Sequence[str], Mapping[str, str]], dict[str, Any]] = default_submit_pass,
    journal_probe: Callable[[ReplayDriverConfig, datetime], Mapping[str, Sequence[str]]] = default_journal_probe,
    clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    started_at = clock()
    _assert_retention_disabled(config)
    # BEFORE any receipt write: writing the in-progress receipt over the resume
    # source would destroy the only record of the interrupted attempt's prior
    # half (round-2 B2-1).
    _assert_receipt_path_is_not_the_resume_source(config)
    removed_entries = load_reset_removed_entries(
        config.reset_receipt,
        source_id=config.source_id,
        model_ids=config.model_ids,
    )
    resume = _resume_plan(config)
    # Single owner of the receipt's rows, seeded with the whole resume receipt:
    # a row is replaced only when THIS pass produces it (round-4 B4-1).
    rows_ledger = _ReceiptRows(resume.all_rows())

    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        # Flipped to ``completed`` only after the last cycle; every per-cycle
        # write below leaves an honest in-progress receipt behind so a crash
        # mid-sequence preserves the rows already finished (round-1 B-P1-4).
        "outcome": "in_progress",
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
        # Serialized from the ledger at every write, never appended to directly,
        # so the chain of attempts stays whole (round-4 B4-1).
        "rows": rows_ledger.serialize(),
        "interruption": None,
    }
    resume_evidence = resume.to_evidence()
    if resume_evidence is not None:
        receipt["resume_from"] = resume_evidence

    # Writability pre-flight: the first receipt write happens BEFORE the first
    # submission, so an unwritable path is a refusal with zero submissions
    # instead of a discovery made after the first cycle is already overwritten.
    try:
        _write_receipt(config.receipt_path, receipt)
    except ReceiptWriteError as error:
        raise ReplayDriverRefused(
            "receipt_path_unwritable",
            {"path": str(config.receipt_path), "error": str(error)},
        ) from error

    for cycle_time in config.cycles:
        verified = _verified_resume_skip(config, cycle_time, resume)
        if verified is not None:
            rows_ledger.extend(verified)
            _checkpoint_receipt(config, receipt, rows_ledger, cycle_time)
            continue
        prior_probe = dict(journal_probe(config, cycle_time))
        prior_terminal_jobs = {
            model_id: sorted(str(job_id) for job_id in (prior_probe.get(model_id) or []))
            for model_id in config.model_ids
        }
        resumed_rows = {
            model_id: resume.prior_row(cycle_time.strftime("%Y%m%d%H"), model_id)
            for model_id in config.model_ids
        }
        rows = [
            _pre_capture_row(
                config,
                cycle_time,
                model_id,
                removed_entries=removed_entries,
                prior_terminal_job_ids=prior_terminal_jobs[model_id],
                resumed_row=resumed_rows[model_id],
            )
            for model_id in config.model_ids
        ]
        for row in rows:
            row["staging"] = _stage_forcing(config, cycle_time, row["model_id"])
        if not config.execute:
            for row in rows:
                row["status"] = "planned"
            rows_ledger.extend(rows)
            _checkpoint_receipt(config, receipt, rows_ledger, cycle_time)
            continue

        staging_halt = _staging_halt(config, cycle_time, rows)
        if staging_halt is not None:
            _halt(receipt, rows_ledger, config, rows, cycle_time, reason=staging_halt[0], detail=staging_halt[1])

        pass_started_at = clock()
        submission = submit_pass(submit_argv(config, cycle_time), submit_env(config, cycle_time))
        for row in rows:
            row["submission"] = {
                "returncode": int(submission.get("returncode", 1)),
                "repair_parameter_set": submit_env(config, cycle_time).get(REPAIR_ENV) == "1",
                "submitted_at": _format_time(pass_started_at),
            }
        if int(submission.get("returncode", 1)) != 0:
            _halt(receipt, rows_ledger, config, rows, cycle_time, reason="submission_failed", detail=submission)

        convergence = _wait_for_convergence(
            config,
            cycle_time,
            since=pass_started_at,
            prior_terminal_jobs=prior_terminal_jobs,
            resumed_rows=resumed_rows,
            removed_entries=removed_entries,
            journal_probe=journal_probe,
            clock=clock,
            sleep=sleep,
        )
        for row in rows:
            model_id = row["model_id"]
            row["convergence"] = {
                # ``journal_terminal`` keeps its original meaning: a NEW job id.
                # How the terminal leg was actually satisfied is
                # ``terminal_evidence``.
                "journal_terminal": bool(convergence["new_terminal_jobs"].get(model_id)),
                "terminal_evidence": convergence["terminal_evidence"].get(model_id),
                "prior_eligible": model_id in convergence["prior_eligible"],
                "prior_terminal_job_ids": list(prior_terminal_jobs.get(model_id) or []),
                "new_terminal_job_ids": list(convergence["new_terminal_jobs"].get(model_id) or []),
                "state_entry_present": model_id in convergence["state_entries"],
                # Binds the row's prior evidence to the exact successor it
                # describes: a later resume accepts ``prior_pass`` only while the
                # index still holds this checksum (post-capture's
                # ``new.state_checksum`` records the same value, but a halted row
                # never reaches post-capture).
                "successor_checksum": (convergence["state_entries"].get(model_id) or {}).get("checksum"),
                "state_index_status": convergence["state_index_status"],
                "waited_seconds": convergence["waited_seconds"],
            }
        if not convergence["converged"]:
            _halt(
                receipt,
                rows_ledger,
                config,
                rows,
                cycle_time,
                reason=convergence["halt_reason"],
                detail=convergence["detail"],
            )

        for row in rows:
            _post_capture_row(config, cycle_time, row, state_entry=convergence["state_entries"].get(row["model_id"]))
            failure = _row_assertion_failure(row)
            if failure is not None:
                rows_ledger.extend(rows)
                _halt(
                    receipt,
                    rows_ledger,
                    config,
                    [],
                    cycle_time,
                    reason=failure,
                    detail={"model_id": row["model_id"]},
                )
            row["status"] = "completed"
        rows_ledger.extend(rows)
        # Atomic per-cycle checkpoint: a crash on the next cycle keeps every row
        # finished so far, instead of losing the whole sequence.
        _checkpoint_receipt(config, receipt, rows_ledger, cycle_time)

    receipt["outcome"] = "completed"
    receipt["finished_at"] = _format_time(clock())
    _checkpoint_receipt(config, receipt, rows_ledger, None)
    return receipt


def _checkpoint_receipt(
    config: ReplayDriverConfig,
    receipt: dict[str, Any],
    rows: _ReceiptRows,
    cycle_time: datetime | None,
) -> None:
    """Write the receipt; a failure halts the sequence instead of printing."""

    receipt["rows"] = rows.serialize()
    try:
        _write_receipt(config.receipt_path, receipt)
    except ReceiptWriteError as error:
        raise ReplayHalted(
            "receipt_write_failed",
            receipt=receipt,
            details={
                "cycle": cycle_time.strftime("%Y%m%d%H") if cycle_time is not None else None,
                "path": str(config.receipt_path),
                "error": str(error),
            },
        ) from error


def _assert_receipt_path_is_not_the_resume_source(config: ReplayDriverConfig) -> None:
    if config.resume_from is None:
        return
    receipt_path = _resolved_path(config.receipt_path)
    resume_path = _resolved_path(config.resume_from)
    if receipt_path == resume_path:
        raise ReplayDriverRefused(
            "receipt_path_is_resume_source",
            {"receipt_path": str(receipt_path), "resume_from": str(resume_path)},
        )


def _resolved_path(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):  # pragma: no cover - defensive
        return path.absolute()


def _halt(
    receipt: dict[str, Any],
    rows_ledger: _ReceiptRows,
    config: ReplayDriverConfig,
    rows: Sequence[dict[str, Any]],
    cycle_time: datetime,
    *,
    reason: str,
    detail: Any,
) -> None:
    for row in rows:
        row["status"] = "halted"
    rows_ledger.extend(rows)
    # Halting is exactly where the round-3 shape lost rows: the cycles this pass
    # never reached keep the carried resume rows the ledger was seeded with.
    receipt["rows"] = rows_ledger.serialize()
    receipt["outcome"] = "halted"
    receipt["interruption"] = {
        "cycle": cycle_time.strftime("%Y%m%d%H"),
        "reason": reason,
        "detail": json.loads(json.dumps(detail, default=str)) if detail is not None else None,
        "recorded_at": _format_time(datetime.now(tz=UTC)),
    }
    receipt["finished_at"] = _format_time(datetime.now(tz=UTC))
    details: dict[str, Any] = {"cycle": cycle_time.strftime("%Y%m%d%H")}
    try:
        _write_receipt(config.receipt_path, receipt)
    except ReceiptWriteError as error:
        # The halt still stands and still exits non-zero; the operator is told
        # the receipt could not be persisted rather than left to assume it was.
        details["receipt_write_error"] = str(error)
        details["receipt_path"] = str(config.receipt_path)
    raise ReplayHalted(reason, receipt=receipt, details=details)


def _assert_retention_disabled(config: ReplayDriverConfig) -> None:
    value = str(config.env.get(RETENTION_ENV, "")).strip().lower()
    if value not in {"false", "0", "no", "off"}:
        raise ReplayDriverRefused(
            "retention_not_disabled",
            {"env": RETENTION_ENV, "value": config.env.get(RETENTION_ENV)},
        )


def _inventory_census(config: ReplayDriverConfig) -> dict[str, Any]:
    """On-site inventory at start time -- never a frozen survey count.

    The run enumeration walks the whole ``runs/`` directory rather than probing
    the requested cycles one by one, so a frontier that advanced after the
    survey (or any in-scope run outside the requested window) is visible in the
    receipt instead of being truncated away (round-1 B-P2-8).
    """

    try:
        index_entries = read_state_index_entries(config.state_index)
    except ReplayDriverRefused:
        index_entries = []
    census = census_run_cycles(config.nfs_root, source_id=config.source_id, model_ids=config.model_ids)
    per_scope: list[dict[str, Any]] = []
    for model_id in config.model_ids:
        run_cycles = list(census["cycles_by_model"].get(model_id) or [])
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
    observed = sorted({cycle for scope in per_scope for cycle in scope["run_cycles"]})
    return {
        "state_index": str(config.state_index),
        "runs_root": census["runs_root"],
        "enumeration_status": census["status"],
        "enumeration_detail": census["detail"],
        "scopes": per_scope,
        # On-site frontier: the newest run actually on disk, not an echo of the
        # requested range (which is recorded next to it for comparison).
        "frontier_cycle": observed[-1] if observed else None,
        "requested_frontier_cycle": config.cycles[-1].strftime("%Y%m%d%H") if config.cycles else None,
    }


def _pre_capture_row(
    config: ReplayDriverConfig,
    cycle_time: datetime,
    model_id: str,
    *,
    removed_entries: Mapping[tuple[str, str, str], Mapping[str, Any]],
    prior_terminal_job_ids: Sequence[str],
    resumed_row: Mapping[str, Any] | None = None,
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
    row: dict[str, Any] = {
        "cycle": cycle_time.strftime("%Y%m%d%H"),
        "cycle_time": _format_time(cycle_time),
        "source_id": normalize_source_id(config.source_id),
        "model_id": model_id,
        "run_id": run_id,
        "status": "planned",
        "prior": {
            "prior_source": "captured",
            "run_manifest_sha256": capture["manifest_sha256"],
            "output_inventory": capture["output_inventory"],
            "no_prior_run": bool(capture["no_prior_run"]),
            "state": dict(prior_state) if prior_state is not None else None,
            "state_source": "reset_receipt",
            "river_network_version_id": prior_summary["river_network_version_id"],
            "output_segment_count": prior_summary["output_segment_count"],
            # Baseline for the completion probe: the ORIGINAL run's terminal
            # records survive the state-scope reset (the journal is untouched).
            "terminal_job_ids": list(prior_terminal_job_ids),
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
    return _with_resumed_prior(row, resumed_row)


def _with_resumed_prior(
    row: dict[str, Any],
    resumed_row: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Keep the interrupted attempt's pre-image instead of re-capturing it.

    The run trees have no archive: once a cycle has been submitted the prior
    manifest and outputs are gone, so a resumed attempt that re-captures its
    prior half records the replayed tree as the "prior" and the key-consistency
    assertion degenerates into replay-versus-replay.  Any resume-receipt row for
    this (cycle, model) therefore wins, whatever its status -- the interrupted
    cycle's rows are precisely the ``halted`` ones (round-2 B2-1).
    """

    if not isinstance(resumed_row, Mapping):
        return row
    resumed_prior = resumed_row.get("prior")
    if not isinstance(resumed_prior, Mapping):
        return row
    row["prior"] = {**dict(resumed_prior), "prior_source": "resumed_receipt"}
    resumed_inputs = resumed_row.get("inputs")
    if isinstance(resumed_inputs, Mapping):
        # The input checksums belong to the same pre-image half; the forcing
        # package may have been re-staged since.
        row["inputs"] = dict(resumed_inputs)
    return row


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


def _staging_halt(
    config: ReplayDriverConfig,
    cycle_time: datetime,
    rows: Sequence[dict[str, Any]],
) -> tuple[str, dict[str, Any]] | None:
    """Typed halt for any row whose forcing package is not verifiably staged.

    A partially copied package still satisfies the scheduler's presence probe,
    so an unverified staging result must never reach a submission
    (round-1 A-P2-6/B-P2-5).  ``source_absent`` is exempt for the repair cycles
    only: GFS 2026070712 genuinely has no NFS forcing for five basins and is
    submitted through the repair parameter set, which rebuilds it.
    """

    token = cycle_time.strftime("%Y%m%d%H")
    repair_cycle = (normalize_source_id(config.source_id).lower(), token) in REPAIR_CYCLES
    offenders: list[dict[str, Any]] = []
    for row in rows:
        staging = dict(row.get("staging") or {})
        if staging.get("verified") is True:
            continue
        status = str(staging.get("status") or "unknown")
        if status == "source_absent" and repair_cycle:
            staging["repair_cycle_exemption"] = True
            row["staging"] = staging
            continue
        offenders.append(
            {
                "model_id": row["model_id"],
                "status": status,
                "detail": staging.get("detail"),
                "copied_files": staging.get("copied_files"),
            }
        )
    if not offenders:
        return None
    reason = (
        "forcing_source_absent"
        if all(offender["status"] == "source_absent" for offender in offenders)
        else "forcing_staging_unverified"
    )
    return reason, {"cycle": token, "repair_cycle": repair_cycle, "rows": offenders}


#: The two ways a pass can adjudicate the terminal leg; anything else (``None``)
#: means that pass closed nothing for the model.
TERMINAL_EVIDENCE_KINDS = frozenset({"new_job", "prior_pass"})


def _resumed_row_evidences_replacement(row: Mapping[str, Any] | None) -> bool:
    """Did a PRIOR pass durably observe BOTH halves for this (cycle, model)?

    Both, conjunctively -- the replaced successor AND terminal attribution --
    because either alone launders a failure through the resume:

    * ``state_entry_present`` is the only PER-MODEL discriminator (the checksum
      differed from the reset-archived one for THIS model), but a run can write
      its state and then die before anything adjudicates it terminal;
    * terminal attribution is not per-model discriminating on its own: the real
      attempt-1 rows carry ``journal_terminal: true`` for all six models, the
      failed one included, because the cohort's completion job carries
      ``model_id: null`` and is attributed to every requested model.

    Terminal attribution is read from whichever field the writing pass had:
    ``terminal_evidence`` when the row has it (that pass's own adjudication,
    which makes ``prior_pass`` transitive down a chain), otherwise the older
    ``journal_terminal``.  Note ``journal_terminal`` is PASS-RELATIVE: a
    zero-submission pass writes ``false`` for every row, so its receipt carries
    no usable prior evidence and a later attempt must resume from the receipt
    that does (their prior halves are identical -- rows carry verbatim, B4-1).
    """

    if not row:
        return False
    if str(row.get("status") or "") in RESUME_COMPLETED_STATUSES:
        return True
    convergence = row.get("convergence") or {}
    if convergence.get("state_entry_present") is not True:
        return False
    if "terminal_evidence" in convergence:
        return convergence.get("terminal_evidence") in TERMINAL_EVIDENCE_KINDS
    return convergence.get("journal_terminal") is True


def _resumed_successor_checksum(row: Mapping[str, Any] | None) -> str | None:
    """The successor checksum the prior pass actually saw, when it recorded one."""

    if not row:
        return None
    checksum = (row.get("convergence") or {}).get("successor_checksum")
    return str(checksum) if checksum else None


def _successor_identity_holds(recorded: str | None, entry: Mapping[str, Any]) -> bool:
    """Is the successor still the exact one the prior pass's evidence describes?

    A different checksum means something re-ran and re-published this successor
    after that observation, so the prior evidence no longer describes what is on
    disk and only a new terminal job id may close the model.  ``None`` is a row
    that predates the field: there is nothing to bind against, so its own
    evidence stands alone.
    """

    if recorded is None:
        return True
    return str(entry.get("checksum") or "") == recorded


def _wait_for_convergence(
    config: ReplayDriverConfig,
    cycle_time: datetime,
    *,
    since: datetime,
    prior_terminal_jobs: Mapping[str, Sequence[str]],
    resumed_rows: Mapping[str, Mapping[str, Any] | None],
    removed_entries: Mapping[tuple[str, str, str], Mapping[str, Any]],
    journal_probe: Callable[[ReplayDriverConfig, datetime], Mapping[str, Sequence[str]]],
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    """Wait for a REPLACED successor state entry plus terminal-job evidence.

    Neither leg uses a wall-clock gate: production state-index entries carry
    ``created_at: null`` and the journal's completion record for the original
    run survives the reset, so both "is it new?" questions are answered by
    identity instead -- a terminal job id absent from the pre-pass baseline, and
    a successor checksum different from the one the reset receipt archived
    (round-1 B-P1-1 / B-P2-7).  An unreadable index is undecidable: it never
    counts as converged and halts with its own typed reason.

    The terminal leg is satisfied two ways, because a cycle that partially
    succeeded and was then resumed can never satisfy the first one again: the
    scheduler refuses to resubmit a model whose successor state already exists,
    so no NEW job id can appear for it and the wait would deadlock forever.

    * ``new_job`` -- a terminal job id absent from this pass's pre-pass baseline;
    * ``prior_pass`` -- the model is ``prior_eligible``, its successor entry is
      currently REPLACED (checksum different from the reset-archived one), and
      that entry is still the exact one the prior pass's evidence describes
      (:func:`_successor_identity_holds`).

    ``prior_eligible`` is derived ONCE, before the first poll, from the RESUME
    RECEIPT -- never from the live index, so no observation this pass makes can
    widen it.  A model qualifies when its resumed row exists, that row evidences
    a replacement a prior pass durably observed together with terminal
    attribution (:func:`_resumed_row_evidences_replacement`), and the journal
    carries a terminal job id for the cycle (attribution; the pre-reset record
    survives, so this conjunct is weak on its own and is never the deciding one).

    Without ``--resume-from`` -- or for a model the resume receipt does not
    evidence -- ``prior_eligible`` is empty and the ONLY way to converge is a new
    terminal job id.  That is deliberate: re-running an already-replayed cycle
    with no resume chain has no pre-image to record (the run trees were already
    overwritten), so it must dead-end in ``convergence_timeout`` instead of
    writing a receipt whose "prior" half is the replay's own output.
    """

    successor_valid_time = cycle_time + timedelta(hours=SUCCESSOR_LEAD_HOURS)
    canonical_source = normalize_source_id(config.source_id)
    prior_state_entries = {
        model_id: removed_entries.get((model_id, canonical_source, _format_time(successor_valid_time)))
        for model_id in config.model_ids
    }
    prior_eligible = frozenset(
        model_id
        for model_id in config.model_ids
        if _resumed_row_evidences_replacement(resumed_rows.get(model_id))
        and (prior_terminal_jobs.get(model_id) or [])
    )
    prior_successor_checksums = {
        model_id: _resumed_successor_checksum(resumed_rows.get(model_id))
        for model_id in config.model_ids
    }
    deadline = since + timedelta(seconds=config.cycle_timeout_seconds)
    while True:
        probe = dict(journal_probe(config, cycle_time))
        new_terminal_jobs = {
            model_id: sorted(
                set(str(job_id) for job_id in (probe.get(model_id) or []))
                - set(str(job_id) for job_id in (prior_terminal_jobs.get(model_id) or []))
            )
            for model_id in config.model_ids
        }
        state_index_status = PROBE_PRESENT
        state_index_detail: str | None = None
        try:
            entries = read_state_index_entries(config.state_index)
        except ReplayDriverRefused as error:
            entries = []
            state_index_status = PROBE_UNDETERMINABLE
            state_index_detail = error.reason
        successors = successor_state_entries(
            entries,
            source_id=config.source_id,
            model_ids=config.model_ids,
            valid_time=successor_valid_time,
        )
        state_entries = (
            replaced_successor_entries(successors, prior_entries=prior_state_entries)
            if state_index_status == PROBE_PRESENT
            else {}
        )
        terminal_evidence: dict[str, str | None] = {}
        for model_id in config.model_ids:
            if new_terminal_jobs[model_id]:
                terminal_evidence[model_id] = "new_job"
            elif (
                model_id in prior_eligible
                and model_id in state_entries
                and _successor_identity_holds(
                    prior_successor_checksums.get(model_id), state_entries[model_id]
                )
            ):
                terminal_evidence[model_id] = "prior_pass"
            else:
                terminal_evidence[model_id] = None
        converged = (
            state_index_status == PROBE_PRESENT
            and all(terminal_evidence[model_id] for model_id in config.model_ids)
            and set(state_entries) == set(config.model_ids)
        )
        now = clock()
        if converged:
            return {
                "converged": True,
                "halt_reason": None,
                "new_terminal_jobs": new_terminal_jobs,
                "terminal_evidence": terminal_evidence,
                "prior_eligible": sorted(prior_eligible),
                "state_entries": state_entries,
                "state_index_status": state_index_status,
                "waited_seconds": (now - since).total_seconds(),
                "detail": None,
            }
        if now >= deadline:
            return {
                "converged": False,
                "halt_reason": (
                    "state_index_undeterminable"
                    if state_index_status == PROBE_UNDETERMINABLE
                    else "convergence_timeout"
                ),
                "new_terminal_jobs": new_terminal_jobs,
                "terminal_evidence": terminal_evidence,
                "prior_eligible": sorted(prior_eligible),
                "state_entries": state_entries,
                "state_index_status": state_index_status,
                "waited_seconds": (now - since).total_seconds(),
                "detail": {
                    "journal_terminal": sorted(
                        model_id for model_id, ids in new_terminal_jobs.items() if ids
                    ),
                    # Disjoint from ``journal_terminal``: these models satisfied
                    # the terminal leg on an earlier pass's evidence, so a
                    # timeout receipt shows which half is actually still pending.
                    "prior_satisfied": sorted(
                        model_id
                        for model_id, evidence in terminal_evidence.items()
                        if evidence == "prior_pass"
                    ),
                    "prior_eligible": sorted(prior_eligible),
                    "prior_terminal_job_ids": {
                        model_id: list(ids) for model_id, ids in prior_terminal_jobs.items()
                    },
                    "state_entries": sorted(state_entries),
                    "unreplaced_successors": sorted(set(successors) - set(state_entries)),
                    "state_index_status": state_index_status,
                    "state_index_detail": state_index_detail,
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
        "output_segment_count": summary["output_segment_count"],
    }
    row["key_consistency"] = key_consistency(row["prior"], row["new"])
    is_first_cycle = cycle_time == config.origin_cycle
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


#: Row statuses whose work is finished; only these may be verified-skipped.
RESUME_COMPLETED_STATUSES = frozenset({"completed", "verified_skip"})


@dataclass(frozen=True)
class _ResumePlan:
    """Single owner of every resume-receipt row's disposition (design D5 v6).

    ``rows`` holds EVERY row of the resume receipt, verbatim, keyed by
    ``(cycle, model)`` in receipt order.  ``skip_eligible`` is the subset that
    finished cleanly -- ``completed``/``verified_skip`` AND no assertion failure
    (round-3 B3-1) -- and is therefore allowed to reach the state-index
    checksum re-check.  Every other key re-runs with the carried prior, so a
    drift/bootstrap assertion re-judges the ORIGINAL pre-image and, if it was
    genuinely wrong, halts again instead of being skipped past.
    """

    source: Path | None
    sha256: str | None
    order: tuple[tuple[str, str], ...]
    rows: Mapping[tuple[str, str], Mapping[str, Any]]
    skip_eligible: frozenset[tuple[str, str]]

    def all_rows(self) -> list[dict[str, Any]]:
        """EVERY resume row, in receipt order, to seed this pass's row map.

        Carrying only the rows outside this pass's static window (the round-3
        shape) covers the window-narrowing branch and nothing else: a pass that
        HALTS never reaches its later in-window cycles, so their rows were
        dropped, and the next resume re-captured their "prior" from an
        already-replayed tree while labelling it ``captured`` (round-4 B4-1).
        Seeding everything and replacing per key only when this pass actually
        produces that key makes the carry a RUNTIME judgement.
        """

        return [dict(self.rows[key]) for key in self.order]

    def prior_row(self, cycle_token: str, model_id: str) -> Mapping[str, Any] | None:
        return self.rows.get((cycle_token, model_id))

    def cycle_rows_are_skip_eligible(self, cycle_token: str, model_ids: Sequence[str]) -> bool:
        return bool(model_ids) and all(
            (cycle_token, model_id) in self.skip_eligible for model_id in model_ids
        )

    def to_evidence(self) -> dict[str, Any] | None:
        if self.source is None:
            return None
        return {"path": str(self.source), "sha256": self.sha256}


def _resume_plan(config: ReplayDriverConfig) -> _ResumePlan:
    """Read the resume receipt once and decide every row's disposition."""

    if config.resume_from is None:
        return _ResumePlan(
            source=None,
            sha256=None,
            order=(),
            rows={},
            skip_eligible=frozenset(),
        )
    payload = _read_json(config.resume_from, max_bytes=MAX_RECEIPT_BYTES)
    if payload is None or payload.get("schema_version") != SCHEMA_VERSION:
        raise ReplayDriverRefused("resume_receipt_unreadable", {"path": str(config.resume_from)})
    _assert_resume_receipt_scope_covers(config, payload)
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    skip_eligible: set[tuple[str, str]] = set()
    for row in payload.get("rows") or []:
        if not isinstance(row, Mapping):
            continue
        # A later row for the same key wins, matching the receipt's append order.
        key = (str(row.get("cycle") or ""), str(row.get("model_id") or ""))
        if key not in rows:
            order.append(key)
        rows[key] = dict(row)
        if str(row.get("status") or "") in RESUME_COMPLETED_STATUSES and _row_assertion_failure(row) is None:
            skip_eligible.add(key)
        else:
            skip_eligible.discard(key)
    return _ResumePlan(
        source=config.resume_from,
        sha256=_safe_file_sha256(config.resume_from),
        order=tuple(order),
        rows=rows,
        skip_eligible=frozenset(skip_eligible),
    )


def _assert_resume_receipt_scope_covers(
    config: ReplayDriverConfig,
    payload: Mapping[str, Any],
) -> None:
    """Refuse a resume receipt that belongs to another (source, models) scope.

    Row keys are ``(cycle, model_id)`` and IFS/GFS replay the same windows with
    the same model ids from the same directory, so a receipt from the OTHER
    source collides key for key: its rows would be adopted verbatim as this
    pass's ``resumed_receipt`` priors, the real pre-image would be destroyed by
    the replay, and the key-consistency assertion would still pass because both
    sources share a river network (round-4 B4-2).  A model SUBSET is legitimate
    -- that is exactly a narrowed resume -- so only a model whose pre-image the
    receipt does not hold refuses.

    Coverage is therefore read off the ROWS, unioned with the declared
    ``model_ids`` (round-5 B5-1).  The declared field records the WRITING pass's
    own scope (``:349``), and since the full-row carry those two diverge: a
    narrowed pass declares only its narrowed models while carrying every model's
    rows verbatim.  Judging by the declaration alone refused the widen-back hop
    of the runbook's own ``forcing_source_absent`` recovery (narrow, fix, widen,
    always resuming from "上一份") even though the widened model's pre-image sat
    in the very receipt being refused -- and the workaround, resuming from an
    older receipt, is the B4-1 pre-image loss all over again.  The union keeps
    the declaration meaningful for a receipt that legitimately carries no rows
    yet (a pass refused or halted before its first cycle).
    """

    recorded_source = str(payload.get("source_id") or "")
    try:
        payload_source = normalize_source_id(recorded_source) if recorded_source else ""
    except ValueError:
        # An unrecognized source id is not a match by default: refuse.
        payload_source = ""
    config_source = normalize_source_id(config.source_id)
    payload_models = {str(model_id) for model_id in (payload.get("model_ids") or [])}
    payload_models.update(
        str(row.get("model_id") or "")
        for row in (payload.get("rows") or [])
        if isinstance(row, Mapping)
    )
    payload_models.discard("")
    missing_models = sorted({str(model_id) for model_id in config.model_ids} - payload_models)
    if payload_source == config_source and not missing_models:
        return
    raise ReplayDriverRefused(
        "resume_receipt_scope_mismatch",
        {
            "path": str(config.resume_from),
            "receipt_source_id": recorded_source or None,
            "config_source_id": config_source,
            "receipt_model_ids": sorted(payload_models),
            "missing_model_ids": missing_models,
        },
    )


def _verified_resume_skip(
    config: ReplayDriverConfig,
    cycle_time: datetime,
    plan: _ResumePlan,
) -> list[dict[str, Any]] | None:
    """Skip a cycle ONLY on positive evidence; a failed check re-runs it."""

    token = cycle_time.strftime("%Y%m%d%H")
    if not plan.cycle_rows_are_skip_eligible(token, config.model_ids):
        return None
    rows = [plan.prior_row(token, model_id) for model_id in config.model_ids]
    try:
        entries = read_state_index_entries(config.state_index)
    except ReplayDriverRefused:
        return None
    present = successor_state_entries(
        entries,
        source_id=config.source_id,
        model_ids=config.model_ids,
        valid_time=cycle_time + timedelta(hours=SUCCESSOR_LEAD_HOURS),
    )
    verified: list[dict[str, Any]] = []
    for row in rows:
        assert row is not None  # every key was proved skip-eligible above
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


def _write_receipt(path: Path, receipt: Mapping[str, Any], *, mode: int = REPLACEMENT_RECEIPT_MODE) -> None:
    content = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True, default=str).encode("utf-8") + b"\n"
    try:
        ensure_directory_no_follow(path.parent)
        atomic_write_bytes_no_follow(path, content, mode=mode)
    except (OSError, SafeFilesystemError) as error:
        raise ReceiptWriteError(str(error)) from error


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
    parser.add_argument(
        "--origin-cycle",
        default=DEFAULT_REPLAY_ORIGIN_CYCLE.strftime("%Y%m%d%H"),
        help=(
            "Replay-sequence origin whose row must show the packaged-IC bootstrap shape. "
            "Stays 2026070500 when resuming from a later --start-cycle."
        ),
    )
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
            origin_cycle=_parse_cycle_token(args.origin_cycle),
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
