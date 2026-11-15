"""The root-level Slurm log lane on the scheduler workspace root (#3142).

Slurm writes ``<WORKSPACE_ROOT>/<run_id>/logs/<job>.{out,err}`` (the sbatch
``--output/--error`` directives) and the gateway writes
``<WORKSPACE_ROOT>/<cycle_id>/{manifests,array_logs}``, where ``cycle_id`` is
``cycle_id_for(source, cycle)``. Additional-root retention swept that root
``runs/``-only, so these directories were never reclaimed.

This module owns the lane's collector, its one name predicate (shared with
root-overlap classification in :mod:`services.orchestrator.retention`, so
admission cannot drift from the scan) and its recency walk. Admission -- which
root gets the lane -- stays in ``retention``. The dependency runs one way only:
``retention`` imports this module, and hands the collector the two
``retention`` collaborators it needs (the target record and the shared
frontier-exempt skip entry) as arguments, so this module imports nothing from
``retention`` and the frontier skip shape has one owner.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from services.orchestrator.run_identity import parse_run_cycle

# Target reason for a lane directory that aged out below the frontier with no
# recent modification anywhere in its tree.
SLURM_LOG_TARGET_REASON = "slurm_log_cycle_aged_out"

# Skip reason for a lane directory whose cycle aged out below the frontier but
# whose tree was modified within the window -- or could not be fully read, in
# which case recency cannot be ruled out. In-flight analysis lanes, API retries
# and forced resubmits sit below the forecast frontier, and their manifests are
# runtime inputs of array tasks while Slurm is still writing their logs.
SLURM_LOG_RECENTLY_MODIFIED_REASON = "slurm_log_recently_modified"

# The closed set ``cycle_id_for`` can emit: ``normalize_source_id`` accepts only
# GFS, IFS and ERA5 and the id is lower-cased. ``fullmatch`` (not ``match`` with
# ``$``, which tolerates a trailing newline) and ``[0-9]`` (not ``\d``, which
# matches non-ASCII digits): this predicate gates a deletion surface.
GATEWAY_CYCLE_ID_RE = re.compile(r"(gfs|ifs|era5)_([0-9]{10})")

_Target = TypeVar("_Target")


def slurm_log_lane_cycle(name: str) -> datetime | None:
    """The cycle of a root-level lane directory name, or None when not in the lane.

    In the lane: (a) a canonical run id (``parse_run_cycle``, the same shapes
    the ``runs/`` lane admits), or (b) a gateway cycle id
    ``(gfs|ifs|era5)_<YYYYMMDDHH>`` whose digits are a real ``%Y%m%d%H``. Every
    other name -- ``runs``, ``scheduler``, ``slurm-logs``, ``repairs``, probe and
    operator directories, upper-case or suffixed look-alikes -- returns None.
    """
    cycle = parse_run_cycle(name)
    if cycle is not None:
        return cycle
    matched = GATEWAY_CYCLE_ID_RE.fullmatch(name)
    if matched is None:
        return None
    try:
        return datetime.strptime(matched.group(2), "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError:
        return None


def idle_tree_size(path: Path, *, modified_since: datetime) -> int | None:
    """Regular-file byte total of ``path`` when nothing in it is recent, else None.

    One walk computes both answers. "Recent" means an ``lstat`` mtime at or after
    ``modified_since`` on the directory itself or on any non-symlink
    descendant; links are never followed and their own mtimes are ignored. The
    walk stops at the first recent entry, because the size is only needed for
    a target.

    Fails closed: ``path`` not being a real directory, or any ``OSError`` while
    walking (an unreadable subtree, an entry vanishing mid-walk) returns None,
    because an unread subtree may hold exactly the recent file the guard is
    there to see. The caller skips the directory; the next pass re-adjudicates.
    """
    threshold = modified_since.timestamp()
    try:
        root_stat = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_mtime >= threshold:
        return None
    total = 0
    pending = [os.fspath(path)]
    try:
        while pending:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    entry_stat = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(entry_stat.st_mode):
                        continue
                    if entry_stat.st_mtime >= threshold:
                        return None
                    if stat.S_ISDIR(entry_stat.st_mode):
                        pending.append(entry.path)
                    elif stat.S_ISREG(entry_stat.st_mode):
                        total += entry_stat.st_size
    except OSError:
        return None
    return total


def collect_slurm_log_targets(
    root: Path,
    cutoff: datetime,
    active_lower_bound: datetime | None,
    *,
    make_target: Callable[..., _Target],
    frontier_exempt_entry: Callable[[str, Path, datetime, Path], dict[str, Any]],
) -> tuple[list[_Target], list[dict[str, Any]]]:
    """Scan ``root``'s direct children for the Slurm log lane.

    Names are filtered before any stat (the root holds thousands of entries);
    symlinks and non-directories are never selected or followed. Adjudication
    is the ``runs/`` order plus one recency guard: window, then frontier (no
    walk), then one walk that both rules out a modification within the window
    and sizes the target. The key is the root-relative child name.
    """
    targets: list[_Target] = []
    skipped: list[dict[str, Any]] = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return targets, skipped
    for name in names:
        cycle_time = slurm_log_lane_cycle(name)
        if cycle_time is None:
            continue
        child = root / name
        if child.is_symlink() or not child.is_dir():
            continue
        if cycle_time >= cutoff:
            skipped.append({"key": name, "root": str(root), "reason": "within_retention_window"})
            continue
        if active_lower_bound is not None and cycle_time >= active_lower_bound:
            skipped.append(frontier_exempt_entry(name, child, cycle_time, root))
            continue
        size_bytes = idle_tree_size(child, modified_since=cutoff)
        if size_bytes is None:
            skipped.append(
                {
                    "key": name,
                    "path": str(child),
                    "cycle_time": cycle_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "reason": SLURM_LOG_RECENTLY_MODIFIED_REASON,
                    "root": str(root),
                }
            )
            continue
        targets.append(
            make_target(
                path=child,
                key=name,
                cycle_time=cycle_time,
                reason=SLURM_LOG_TARGET_REASON,
                size_bytes=size_bytes,
                root=root,
            )
        )
    return targets, skipped
