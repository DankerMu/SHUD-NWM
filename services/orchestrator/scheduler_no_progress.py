"""Cross-pass no-progress circuit evidence (issue #1118).

Observe-only. The circuit counts how many consecutive *fully observed* passes
report the same no-progress reason for the same subject and surfaces the
repeat as a `no_progress_circuit` evidence block plus one aggregated WARNING.
It never feeds a scheduling decision, a retry, or a terminal status.

Two facts shape the module:

- The production scheduler is a systemd one-shot (docs/runbooks/
  current-production-ops.md §3.1), so an in-memory counter dies with the pass.
  State lives in ``<evidence_root>/no-progress-tracker.json``, rewritten
  atomically on every enabled fully-observed pass — even when it holds zero
  entries, so ``state_reset`` only ever appears on the very first enabled pass.
- Only the complete-pass evidence write may observe. Early-exit, pre-lock,
  lock-contended and resource-limit-aborted passes carry empty candidate lists;
  observing there would clear every accumulated count. The single call site is
  scheduler_runtime.run_once's complete-pass write.

Both adapters read the *uncompacted* payload the pass just assembled — the
bounded-compaction key table (scheduler_evidence_payload.py:26-35) is a
compaction-time hoisting map, not an observation path.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.orchestrator.scheduler_evidence import (
    SchedulerEvidenceWriteError,
    open_evidence_directory,
)

log = logging.getLogger(__name__)

#: Top-level evidence key carrying the circuit block.
EVIDENCE_KEY = "no_progress_circuit"
#: Grep token for the aggregated warning (journalctl is the only channel ops
#: actually consumes today).
CIRCUIT_OPEN_LOG_TOKEN = "SCHEDULER_NO_PROGRESS_CIRCUIT_OPEN"
CIRCUIT_FAILED_LOG_TOKEN = "SCHEDULER_NO_PROGRESS_CIRCUIT_OBSERVE_FAILED"
#: Deliberately NOT prefixed ``scheduler_``: the retention script only deletes
#: ``scheduler_*.json`` pass artifacts and skips everything else as
#: ``unrecognised`` (scripts/node22_scheduler_evidence_retention.py:212-215).
STATE_FILENAME = "no-progress-tracker.json"
STATE_TMP_FILENAME = STATE_FILENAME + ".tmp"
STATE_SCHEMA_VERSION = "nhms.scheduler.no_progress_tracker.v1"
#: Open entries carried in evidence and in the warning, highest count first.
MAX_OPEN_ENTRIES = 50

CANDIDATE_ADAPTER = "candidate"
RECONCILE_ADAPTER = "reconcile"
KNOWN_ADAPTERS = (CANDIDATE_ADAPTER, RECONCILE_ADAPTER)

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True)
class NoProgressObservation:
    """One subject reporting one no-progress reason in one pass."""

    adapter: str
    subject_kind: str
    subject_id: str
    reason: str
    operator_action_required: bool = False


@dataclass(frozen=True)
class AdapterObservations:
    """What one adapter saw, plus whether its source was in the pass at all.

    ``source_present=False`` is NOT "nothing was blocked": it means the pass
    never produced this adapter's source (a failed reconcile segment, a dry
    run). Entries under an absent source are preserved untouched, otherwise one
    sacct hiccup would zero a real wedge's count.
    """

    adapter: str
    source_present: bool
    observations: tuple[NoProgressObservation, ...] = ()


@dataclass(frozen=True)
class TrackerEntry:
    """A (subject, reason) pair and its consecutive fully-observed pass count."""

    adapter: str
    subject_kind: str
    subject_id: str
    reason: str
    consecutive_passes: int
    first_pass_id: str
    last_pass_id: str
    operator_action_required: bool = False

    @property
    def subject_key(self) -> tuple[str, str]:
        return (self.subject_kind, self.subject_id)

    def to_state(self) -> dict[str, Any]:
        payload = {
            "adapter": self.adapter,
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "reason": self.reason,
            "consecutive_passes": self.consecutive_passes,
            "first_pass_id": self.first_pass_id,
            "last_pass_id": self.last_pass_id,
        }
        if self.operator_action_required:
            payload["operator_action_required"] = True
        return payload

    def to_open_entry(self) -> dict[str, Any]:
        payload = {
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "reason": self.reason,
            "consecutive_passes": self.consecutive_passes,
            "first_pass_id": self.first_pass_id,
            "last_pass_id": self.last_pass_id,
        }
        if self.operator_action_required:
            payload["operator_action_required"] = True
        return payload


def _is_row_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes)


def _non_empty_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def candidate_observations(payload: Mapping[str, Any]) -> AdapterObservations:
    """A1: blocked candidate rows with a non-empty reason.

    Row shape is ``SchedulerCandidate.to_dict`` (scheduler_types.py:97-136).
    ``skipped_candidates`` is excluded on purpose: those rows keep
    ``status="selected"`` and include successful skips such as
    ``terminal_hydro_success``, so counting them is a permanent false alarm.
    """

    rows: list[Any] = []
    source_present = False
    for key in ("candidates", "blocked_candidates"):
        value = payload.get(key)
        if not _is_row_sequence(value):
            continue
        source_present = True
        rows.extend(value)
    if not source_present:
        return AdapterObservations(CANDIDATE_ADAPTER, False)

    observations: list[NoProgressObservation] = []
    for row in rows:
        if not isinstance(row, Mapping) or row.get("status") != "blocked":
            continue
        reason = _non_empty_text(row.get("reason"))
        candidate_id = _non_empty_text(row.get("candidate_id"))
        if reason is None or candidate_id is None:
            continue
        state_evidence = row.get("state_evidence")
        # #1152 consumption: the flag rides the A1 entry as an annotation. A
        # separate adapter would collide on this very subject and reset both
        # reasons every pass, so the circuit would never open.
        operator_action_required = (
            isinstance(state_evidence, Mapping) and state_evidence.get("operator_action_required") is True
        )
        observations.append(
            NoProgressObservation(
                adapter=CANDIDATE_ADAPTER,
                subject_kind="candidate",
                subject_id=candidate_id,
                reason=f"blocked:{reason}",
                operator_action_required=operator_action_required,
            )
        )
    return AdapterObservations(CANDIDATE_ADAPTER, True, tuple(observations))


def reconcile_observations(payload: Mapping[str, Any]) -> AdapterObservations:
    """A3: reserved-unbound reconcile outcomes, keyed by action + reason class.

    Read only when the segment completed AND its ``reserved_unbound`` key is
    present: a sacct fault writes ``reserved_unbound_error`` with
    ``status="error"`` (scheduler_runtime.py:1561-1563) and a dry run / missing
    store returns ``None`` or ``status="skipped"`` — all source-absent.
    """

    reconcile = payload.get("restart_reconcile")
    if not isinstance(reconcile, Mapping) or reconcile.get("status") != "completed":
        return AdapterObservations(RECONCILE_ADAPTER, False)
    reserved = reconcile.get("reserved_unbound")
    if not isinstance(reserved, Mapping):
        return AdapterObservations(RECONCILE_ADAPTER, False)
    outcomes = reserved.get("outcomes")
    if not _is_row_sequence(outcomes):
        return AdapterObservations(RECONCILE_ADAPTER, False)

    observations: list[NoProgressObservation] = []
    for row in outcomes:
        if not isinstance(row, Mapping):
            continue
        job_id = _non_empty_text(row.get("job_id"))
        action = _non_empty_text(row.get("action"))
        if job_id is None or action is None:
            continue
        reason_class = _non_empty_text(row.get("reconciliation_reason_class"))
        observations.append(
            NoProgressObservation(
                adapter=RECONCILE_ADAPTER,
                subject_kind="job",
                subject_id=job_id,
                reason=action if reason_class is None else f"{action}:{reason_class}",
            )
        )
    return AdapterObservations(RECONCILE_ADAPTER, True, tuple(observations))


def observe_adapters(payload: Mapping[str, Any]) -> tuple[AdapterObservations, ...]:
    return (candidate_observations(payload), reconcile_observations(payload))


def merge_entries(
    entries: Sequence[TrackerEntry],
    adapter_observations: Sequence[AdapterObservations],
    *,
    pass_id: str,
) -> tuple[TrackerEntry, ...]:
    """Strictly consecutive merge, per adapter.

    Same (subject, reason) increments; a changed reason resets to one; a
    subject absent while its adapter's source is present is dropped; every
    entry of a source-absent adapter is preserved as-is. Two rows for one
    subject in one pass: the first row wins.
    """

    previous_by_adapter: dict[str, dict[tuple[str, str], TrackerEntry]] = {}
    for entry in entries:
        previous_by_adapter.setdefault(entry.adapter, {})[entry.subject_key] = entry

    merged: list[TrackerEntry] = []
    for adapter in adapter_observations:
        if not adapter.source_present:
            merged.extend(entry for entry in entries if entry.adapter == adapter.adapter)
            continue
        previous = previous_by_adapter.get(adapter.adapter, {})
        seen: set[tuple[str, str]] = set()
        for observation in adapter.observations:
            key = (observation.subject_kind, observation.subject_id)
            if key in seen:
                continue
            seen.add(key)
            prior = previous.get(key)
            if prior is not None and prior.reason == observation.reason:
                consecutive_passes = prior.consecutive_passes + 1
                first_pass_id = prior.first_pass_id
            else:
                consecutive_passes = 1
                first_pass_id = pass_id
            merged.append(
                TrackerEntry(
                    adapter=adapter.adapter,
                    subject_kind=observation.subject_kind,
                    subject_id=observation.subject_id,
                    reason=observation.reason,
                    consecutive_passes=consecutive_passes,
                    first_pass_id=first_pass_id,
                    last_pass_id=pass_id,
                    operator_action_required=observation.operator_action_required,
                )
            )
    return tuple(merged)


def open_entries(
    entries: Sequence[TrackerEntry],
    *,
    threshold: int,
) -> tuple[tuple[TrackerEntry, ...], int]:
    """Entries at or above the threshold, highest count first, capped at 50."""

    if threshold <= 0:
        return (), 0
    opened = sorted(
        (entry for entry in entries if entry.consecutive_passes >= threshold),
        key=lambda entry: (-entry.consecutive_passes, entry.subject_kind, entry.subject_id, entry.reason),
    )
    truncated = max(len(opened) - MAX_OPEN_ENTRIES, 0)
    return tuple(opened[:MAX_OPEN_ENTRIES]), truncated


def circuit_block(
    *,
    threshold: int,
    entries: Sequence[TrackerEntry],
    opened: Sequence[TrackerEntry],
    truncated: int,
    state_reset: str | None,
) -> dict[str, Any]:
    block: dict[str, Any] = {"threshold": threshold, "tracked": len(entries)}
    if state_reset is not None:
        block["state_reset"] = state_reset
    block["open"] = [entry.to_open_entry() for entry in opened]
    block["truncated"] = truncated
    return block


def _entry_from_state(row: Any) -> TrackerEntry | None:
    if not isinstance(row, Mapping):
        return None
    adapter = row.get("adapter")
    subject_kind = _non_empty_text(row.get("subject_kind"))
    subject_id = _non_empty_text(row.get("subject_id"))
    reason = _non_empty_text(row.get("reason"))
    first_pass_id = _non_empty_text(row.get("first_pass_id"))
    last_pass_id = _non_empty_text(row.get("last_pass_id"))
    consecutive_passes = row.get("consecutive_passes")
    if adapter not in KNOWN_ADAPTERS:
        return None
    if subject_kind is None or subject_id is None or reason is None:
        return None
    if first_pass_id is None or last_pass_id is None:
        return None
    if not isinstance(consecutive_passes, int) or isinstance(consecutive_passes, bool):
        return None
    if consecutive_passes < 1:
        return None
    return TrackerEntry(
        adapter=str(adapter),
        subject_kind=subject_kind,
        subject_id=subject_id,
        reason=reason,
        consecutive_passes=consecutive_passes,
        first_pass_id=first_pass_id,
        last_pass_id=last_pass_id,
        operator_action_required=row.get("operator_action_required") is True,
    )


def load_state(dir_fd: int) -> tuple[tuple[TrackerEntry, ...], str | None]:
    """Read the tracker file. Never raises: missing/corrupt both reset to empty."""

    try:
        fd = os.open(STATE_FILENAME, os.O_RDONLY | _NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        return (), "missing"
    except OSError:
        # Symlink, directory, permission fault: unusable, and not "missing".
        return (), "corrupt"
    try:
        handle = os.fdopen(fd, encoding="utf-8")
    except OSError:
        os.close(fd)
        return (), "corrupt"
    try:
        with handle:
            raw = handle.read()
    except (OSError, ValueError):
        return (), "corrupt"
    try:
        document = json.loads(raw)
    except ValueError:
        return (), "corrupt"
    if not isinstance(document, Mapping):
        return (), "corrupt"
    rows = document.get("entries")
    if not isinstance(rows, list):
        return (), "corrupt"
    entries: list[TrackerEntry] = []
    for row in rows:
        entry = _entry_from_state(row)
        if entry is None:
            return (), "corrupt"
        entries.append(entry)
    return tuple(entries), None


def write_state(dir_fd: int, entries: Sequence[TrackerEntry]) -> bool:
    """Overwrite the tracker file atomically; return whether it landed.

    ``write_new_regular_file`` (scheduler_evidence.py:888-919) is O_EXCL —
    create-or-fail — so it cannot serve a file that is rewritten every pass.
    """

    document = {
        "schema_version": STATE_SCHEMA_VERSION,
        "entries": [entry.to_state() for entry in entries],
    }
    serialized = json.dumps(document, sort_keys=True) + "\n"
    flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC | _NOFOLLOW
    try:
        fd = os.open(STATE_TMP_FILENAME, flags, 0o644, dir_fd=dir_fd)
    except OSError:
        return False
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
    except OSError:
        os.close(fd)
        _unlink_quietly(STATE_TMP_FILENAME, dir_fd)
        return False
    try:
        with handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        _unlink_quietly(STATE_TMP_FILENAME, dir_fd)
        return False
    try:
        os.replace(STATE_TMP_FILENAME, STATE_FILENAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError:
        _unlink_quietly(STATE_TMP_FILENAME, dir_fd)
        return False
    return True


def _unlink_quietly(name: str, dir_fd: int) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except OSError:
        pass


def _open_state_directory(evidence_dir: Path, workspace_root: Path) -> int | None:
    try:
        return open_evidence_directory(Path(evidence_dir), Path(workspace_root))
    except (SchedulerEvidenceWriteError, OSError):
        return None


def _log_circuit_open(block: Mapping[str, Any]) -> None:
    subjects = "; ".join(
        f"{entry['subject_kind']}={entry['subject_id']} reason={entry['reason']} "
        f"passes={entry['consecutive_passes']}"
        for entry in block["open"]
    )
    log.warning(
        "%s threshold=%s open=%s truncated=%s tracked=%s subjects=[%s]",
        CIRCUIT_OPEN_LOG_TOKEN,
        block["threshold"],
        len(block["open"]),
        block["truncated"],
        block["tracked"],
        subjects,
    )


def observe_pass(
    payload: Mapping[str, Any],
    *,
    pass_id: str,
    threshold: int,
    evidence_dir: Path,
    workspace_root: Path,
) -> dict[str, Any] | None:
    """Observe one fully-observed pass and return its circuit block.

    Returns ``None`` when the feature is disabled (``threshold <= 0``): no
    state file is read or written, no key is injected, nothing is logged.
    """

    if threshold <= 0:
        return None
    try:
        return _observe_pass(
            payload,
            pass_id=pass_id,
            threshold=threshold,
            evidence_dir=evidence_dir,
            workspace_root=workspace_root,
        )
    except Exception:  # noqa: BLE001 - observe-only evidence must never abort a pass.
        log.warning("%s pass_id=%s", CIRCUIT_FAILED_LOG_TOKEN, pass_id, exc_info=True)
        return None


def _observe_pass(
    payload: Mapping[str, Any],
    *,
    pass_id: str,
    threshold: int,
    evidence_dir: Path,
    workspace_root: Path,
) -> dict[str, Any]:
    adapters = observe_adapters(payload)
    dir_fd = _open_state_directory(evidence_dir, workspace_root)
    if dir_fd is None:
        # The evidence root itself is unusable; the pass's own write is about to
        # fail too. Count from empty rather than fail the pass.
        entries, state_reset = (), "missing"
        merged = merge_entries(entries, adapters, pass_id=pass_id)
    else:
        try:
            entries, state_reset = load_state(dir_fd)
            merged = merge_entries(entries, adapters, pass_id=pass_id)
            write_state(dir_fd, merged)
        finally:
            os.close(dir_fd)
    opened, truncated = open_entries(merged, threshold=threshold)
    block = circuit_block(
        threshold=threshold,
        entries=merged,
        opened=opened,
        truncated=truncated,
        state_reset=state_reset,
    )
    if opened:
        _log_circuit_open(block)
    return block


__all__ = [
    "CANDIDATE_ADAPTER",
    "CIRCUIT_OPEN_LOG_TOKEN",
    "EVIDENCE_KEY",
    "KNOWN_ADAPTERS",
    "MAX_OPEN_ENTRIES",
    "RECONCILE_ADAPTER",
    "STATE_FILENAME",
    "STATE_SCHEMA_VERSION",
    "STATE_TMP_FILENAME",
    "AdapterObservations",
    "NoProgressObservation",
    "TrackerEntry",
    "candidate_observations",
    "circuit_block",
    "load_state",
    "merge_entries",
    "observe_adapters",
    "observe_pass",
    "open_entries",
    "reconcile_observations",
    "write_state",
]
