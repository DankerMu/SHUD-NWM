from __future__ import annotations

import contextvars
import functools
import inspect
import threading
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

# ---------------------------------------------------------------------------
# #1734 D11: always-on per-entrypoint journal read attribution.
#
# The 2026-08-23 node-22 receipt left three candidate mechanisms (A full-tree
# replay, B flat-directory re-read, C absent memo) and traced none of them.
# node-22 pulls from GitHub, so a monkeypatched probe is not an option: the
# counter has to ship in the repo and be on by default. It is a dict increment
# per read — no new IO, and its own dedicated lock, which is increment-only and
# never held while acquiring anything else, so the journal's single lock order
# (`_write_lock` -> `_cache_lock`) is untouched (spec
# `pipeline-job-persistence` L550).
# ---------------------------------------------------------------------------
JOURNAL_READ_ATTRIBUTION_SCHEMA_VERSION = "nhms.file_journal.read_attribution.v1"
#: Guard against a pathological tag explosion in a single pass; the live tag
#: space is entrypoint x lane, measured on the 308-test reference fixture at
#: ~51 live tags per pass window and 72 distinct across the whole session,
#: against this cap of 256, with ``tags_dropped`` observed 0.
MAX_JOURNAL_READ_ATTRIBUTION_TAGS = 256
_JOURNAL_READ_UNATTRIBUTED = "unattributed"
_ClassT = TypeVar("_ClassT", bound=type)
_journal_read_counter_lock = threading.Lock()
_journal_read_counters: dict[str, dict[str, int]] = {}
_journal_read_counter_tags_dropped = 0
_journal_read_entrypoint: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nhms_journal_read_entrypoint",
    default=_JOURNAL_READ_UNATTRIBUTED,
)
_journal_read_lane: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nhms_journal_read_lane",
    default=_JOURNAL_READ_UNATTRIBUTED,
)


_flat_direct_job_listing_memo: contextvars.ContextVar[dict[str, list[Path]] | None] = contextvars.ContextVar(
    "nhms_flat_direct_job_listing_memo",
    default=None,
)


@contextmanager
def _flat_direct_job_listing_memo_scope() -> Iterable[None]:
    """Memoize the flat ``pipeline-jobs/`` LISTING for one read-only call.

    Scope is deliberately narrow (#1810 design D14). The listing is
    point-in-time on a live journal, which is only safe because the mutating
    paths re-read under ``_locked_cycle_write`` and compare there; a memo that
    outlived one query, or that any write path entered, would turn "at most a
    refused invocation" into a bad write. So this is a ``ContextVar`` bound to
    one call frame, never an instance cache, and it is entered from exactly one
    read-only caller.

    Only the raw, unfiltered directory listing is memoized — the per-cycle
    filename filter still runs per call, so
    ``_flat_direct_pipeline_job_paths_for_cycle`` stays the ONE definition of
    that filter.
    """

    token = _flat_direct_job_listing_memo.set({})
    try:
        yield
    finally:
        _flat_direct_job_listing_memo.reset(token)


@contextmanager
def journal_read_lane(lane: str) -> Iterable[None]:
    """Tag every journal read made in this context with its reader lane.

    The lanes are design D11's candidate mechanisms: ``full_tree_replay`` (A),
    ``direct_flat_scan`` (B) and ``cycle_replay`` (C). Nesting is honoured —
    the innermost lane wins — so a cycle replay reached from a full-tree
    fall-open is still attributed to the lane that actually opened the file.
    """

    token = _journal_read_lane.set(lane)
    try:
        yield
    finally:
        _journal_read_lane.reset(token)


def _attributed_public_method(name: str, func: Callable[..., Any]) -> Callable[..., Any]:
    """Tag one public method as the entrypoint for every read beneath it.

    OUTERMOST wins, unlike ``journal_read_entrypoint``: the public API call the
    caller actually made is the attribution answer, so a public method reached
    from another public method (``upsert_pipeline_job`` -> ``get_pipeline_job``,
    the retry service -> the repository) does not re-tag and steal its caller's
    bytes.
    """

    @functools.wraps(func)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        if _journal_read_entrypoint.get() != _JOURNAL_READ_UNATTRIBUTED:
            return func(self, *args, **kwargs)
        token = _journal_read_entrypoint.set(name)
        try:
            return func(self, *args, **kwargs)
        finally:
            _journal_read_entrypoint.reset(token)

    return wrapper


def _install_public_read_attribution(cls: _ClassT) -> _ClassT:
    """Tag the whole public surface of ``cls``, by boundary rather than by list.

    Round 1 measured why this is not an enumerated list: six methods carried a
    hand-written tag, the design claimed eight, and 80.6% of a real fixture's
    bytes carried no entrypoint at all. ``query_inflight_jobs``, the
    cycle-status predicates and every write path that reads before it writes
    were all silently untagged. A list drifts; a boundary cannot.

    Generators are skipped deliberately: a tag spanning a ``yield`` can have
    its ``ContextVar`` token reset from a context that never set it (design
    D11), and an instrument must never be able to fail a read path. There are
    none on this surface today; the guard is what keeps it that way.
    """

    for name, value in list(vars(cls).items()):
        if name.startswith("_") or not inspect.isfunction(value):
            continue
        if inspect.isgeneratorfunction(value) or getattr(value, "__wrapped__", None) is not None:
            continue
        setattr(cls, name, _attributed_public_method(name, value))
    return cls


def _record_journal_read(*, byte_count: int, cached: bool) -> None:
    """Account one read-primitive call against the current ``(entrypoint, lane)``.

    ``cached`` reads are byte-cache hits: they cost no ``rchar``, so they are
    counted separately and never folded into ``bytes``, which must reconcile
    against the ``/proc`` figure the receipt reports.
    """

    tag = f"{_journal_read_entrypoint.get()}|{_journal_read_lane.get()}"
    global _journal_read_counter_tags_dropped
    with _journal_read_counter_lock:
        entry = _journal_read_counters.get(tag)
        if entry is None:
            if len(_journal_read_counters) >= MAX_JOURNAL_READ_ATTRIBUTION_TAGS:
                _journal_read_counter_tags_dropped += 1
                return
            entry = {"calls": 0, "bytes": 0, "cache_hit_calls": 0, "cache_hit_bytes": 0}
            _journal_read_counters[tag] = entry
        if cached:
            entry["cache_hit_calls"] += 1
            entry["cache_hit_bytes"] += byte_count
        else:
            entry["calls"] += 1
            entry["bytes"] += byte_count


def reset_journal_read_counters() -> None:
    """Zero the counters at scheduler pass entry so totals are per-pass."""

    global _journal_read_counter_tags_dropped
    with _journal_read_counter_lock:
        _journal_read_counters.clear()
        _journal_read_counter_tags_dropped = 0


def journal_read_attribution() -> dict[str, Any]:
    """Snapshot the counters as an evidence-shaped payload.

    Snapshotting copies every counter under the lock before returning, so the
    caller can serialise it while other threads keep reading; handing out the
    live dicts would let a row change mid-serialisation.
    """

    with _journal_read_counter_lock:
        rows = [
            {"tag": tag, **{field_name: int(value) for field_name, value in entry.items()}}
            for tag, entry in _journal_read_counters.items()
        ]
        tags_dropped = _journal_read_counter_tags_dropped
    rows.sort(key=lambda row: (-int(row["bytes"]), str(row["tag"])))
    totals = {
        "calls": sum(int(row["calls"]) for row in rows),
        "bytes": sum(int(row["bytes"]) for row in rows),
        "cache_hit_calls": sum(int(row["cache_hit_calls"]) for row in rows),
        "cache_hit_bytes": sum(int(row["cache_hit_bytes"]) for row in rows),
    }
    return {
        "schema_version": JOURNAL_READ_ATTRIBUTION_SCHEMA_VERSION,
        "github_issue": 1734,
        "interpretation": (
            "bytes are filesystem reads only (rchar-contributing); cache_hit_* are "
            "in-process byte-cache hits and cost no syscall"
        ),
        "totals": totals,
        "tags": rows,
        "tags_dropped": tags_dropped,
    }
