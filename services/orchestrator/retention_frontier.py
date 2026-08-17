"""Pipeline frontier read for deletion surfaces outside a scheduler pass.

A scheduler pass knows the pipeline's active lower bound from its own in-memory
state and passes it to ``run_retention`` (issue #1307). Callers outside a pass
-- the manual ``cleanup`` CLI -- have no such state, so before this module they
deleted on a pure wall-clock criterion and could re-run the #1307
"produce-then-delete" spin during catch-up.

This module recovers the bound from the most recent scheduler pass evidence
receipt (``<evidence_dir>/<pass_id>.json``, ``retention.frontier``; the block
survives evidence size compaction). It is deliberately fail-closed: every form
of "the frontier is unknown" -- missing directory, no readable receipt, no
frontier block, a pass whose retention did not run, a malformed bound, a stale
receipt, or any unexpected error -- yields ``status="unavailable"`` with a
machine-readable reason, and the caller must refuse to delete rather than fall
back to unprotected wall-clock deletion. Nothing raises out of
:func:`read_latest_pass_frontier`.

Operator note for ``pass_retention_not_run``: the fix is to make a pass emit a
frontier block again -- run one pass with ``NHMS_RETENTION_ENABLED=true`` and
``NHMS_RETENTION_DRY_RUN=true`` (the pass still deletes nothing, but records the
frontier), after which cleanup reads a fresh bound. There is deliberately no
CLI bypass flag.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .scheduler_evidence import MAX_EVIDENCE_BYTES

__all__ = (
    "DEFAULT_MAX_AGE_HOURS",
    "FRONTIER_MAX_AGE_ENV",
    "FrontierReadResult",
    "max_age_from_env",
    "read_latest_pass_frontier",
)

FRONTIER_MAX_AGE_ENV = "NHMS_RETENTION_FRONTIER_MAX_AGE_HOURS"

# Production scheduler passes run on a minutes-scale loop, so 24h without a
# receipt means the pipeline is stopped -- exactly when deletion should wait.
DEFAULT_MAX_AGE_HOURS = 24

PRE_EXECUTION_SUFFIX = ".pre_execution.json"

# Pass retention states that produce no frontier block at all
# (scheduler_runtime.py ``_run_retention``).
_RETENTION_NOT_RUN_STATUSES = frozenset({"disabled", "error"})


@dataclass(frozen=True)
class FrontierReadResult:
    """Outcome of reading the pipeline frontier from a pass receipt."""

    status: str  # "ok" | "unavailable"
    active_lower_bound: datetime | None = None
    source: str | None = None
    reason: str | None = None
    receipt_path: str | None = None
    receipt_started_at: datetime | None = None


def max_age_from_env() -> timedelta:
    """Freshness cap for the latest receipt, from env with a 24h default."""
    value = os.getenv(FRONTIER_MAX_AGE_ENV)
    hours = DEFAULT_MAX_AGE_HOURS
    if value is not None and value.strip() != "":
        try:
            parsed = int(value.strip())
        except ValueError:
            parsed = DEFAULT_MAX_AGE_HOURS
        if parsed > 0:
            hours = parsed
    return timedelta(hours=hours)


def read_latest_pass_frontier(
    evidence_dir: Path | str,
    *,
    now: datetime,
    max_age: timedelta,
) -> FrontierReadResult:
    """Read the pipeline frontier from the latest pass evidence receipt.

    Never raises: unexpected errors are wrapped as
    ``unavailable/frontier_read_error`` so a deletion surface can fail closed
    instead of crashing.
    """
    try:
        return _read_latest_pass_frontier(Path(evidence_dir), now=now, max_age=max_age)
    except Exception:  # noqa: BLE001 - the read must never escape as an exception
        return _unavailable("frontier_read_error")


def _read_latest_pass_frontier(
    evidence_dir: Path,
    *,
    now: datetime,
    max_age: timedelta,
) -> FrontierReadResult:
    if not evidence_dir.is_dir():
        return _unavailable("evidence_dir_missing")
    try:
        entries = sorted(evidence_dir.glob("*.json"))
    except OSError:
        return _unavailable("evidence_dir_missing")
    selected = _select_latest_receipt(entries)
    if selected is None:
        return _unavailable("no_readable_receipt")
    path, started_at, payload = selected
    if now.astimezone(UTC) - started_at > max_age:
        return _unavailable("receipt_stale", path=path, started_at=started_at)

    retention = payload.get("retention")
    retention_block = retention if isinstance(retention, dict) else {}
    # Adjudication order (design D1/N2): a pass whose retention was disabled or
    # errored also has no frontier block, and the operator guidance differs, so
    # the not-run form must win over the generic missing-block form.
    if str(retention_block.get("status", "")).strip().lower() in _RETENTION_NOT_RUN_STATUSES:
        return _unavailable("pass_retention_not_run", path=path, started_at=started_at)
    frontier = retention_block.get("frontier")
    if not isinstance(frontier, dict):
        return _unavailable("frontier_block_missing", path=path, started_at=started_at)

    raw_bound = frontier.get("active_lower_bound")
    if raw_bound is None:
        # Mirror the pass: it ran pure wall-clock this pass, so the CLI is not
        # stricter than the pass. The "receipt:none" label is carried by the
        # caller's payload, because retention's frontier block nulls the source
        # whenever the bound is null.
        return FrontierReadResult(
            status="ok",
            active_lower_bound=None,
            source="receipt:none",
            receipt_path=str(path),
            receipt_started_at=started_at,
        )
    bound = _parse_utc(raw_bound)
    if bound is None:
        return _unavailable("frontier_bound_invalid", path=path, started_at=started_at)
    return FrontierReadResult(
        status="ok",
        active_lower_bound=bound,
        source=f"receipt:{_source_label(frontier.get('source'))}",
        receipt_path=str(path),
        receipt_started_at=started_at,
    )


def _select_latest_receipt(entries: list[Path]) -> tuple[Path, datetime, dict[str, Any]] | None:
    """Pick the newest final receipt by its recorded ``started_at``.

    Pre-execution reservations (``<pass_id>.pre_execution.json``) are excluded:
    they carry the same ``started_at`` as the final receipt but never a
    retention block, so selecting one would push the CLI into a permanent
    forced dry-run in healthy steady state. Filenames are pass ids with no
    guaranteed ordering and mtimes survive copies, hence the receipt-internal
    time, with the filename as a deterministic tie-break.
    """
    best: tuple[datetime, str, Path, dict[str, Any]] | None = None
    for path in entries:
        if path.name.endswith(PRE_EXECUTION_SUFFIX):
            continue
        payload = _load_receipt(path)
        if payload is None:
            continue
        started_at = _parse_utc(payload.get("started_at"))
        if started_at is None:
            continue
        candidate = (started_at, path.name, path, payload)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    if best is None:
        return None
    return best[2], best[0], best[3]


def _load_receipt(path: Path) -> dict[str, Any] | None:
    """Read one receipt, or None when it is unusable as a frontier source."""
    try:
        if not path.is_file() or path.is_symlink():
            return None
        # Same bound the writer honours, so an oversized artifact cannot stall
        # the CLI on a read.
        if path.stat().st_size > MAX_EVIDENCE_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _parse_utc(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp to UTC, or None when malformed."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # Naive timestamps are read as UTC, matching retention's own bound
        # normalisation, never silently dropped.
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _source_label(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "none"


def _unavailable(
    reason: str,
    *,
    path: Path | None = None,
    started_at: datetime | None = None,
) -> FrontierReadResult:
    return FrontierReadResult(
        status="unavailable",
        reason=reason,
        receipt_path=None if path is None else str(path),
        receipt_started_at=started_at,
    )
