#!/usr/bin/env python
"""Retention cleanup for node-27-owned raw bundles, canonical precipitation
mirrors and their rendered PNG cache.

This script targets exactly three lanes, all on ONE cutoff in ONE run:

    <object-store-root>/raw/<source>/<YYYYMMDDHH>
    <object-store-root>/canonical/<storage-source>/<YYYYMMDDHH>
    <precip-cache-root>/precip/<storage-source>/<YYYYMMDDHH>

A cycle's canonical mirror directory and its PNG cache directory therefore live
and die together (issue #2011): the display API cannot keep serving rendered
precipitation for a cycle whose mirror is gone.

Each canonical cycle is removed while holding the object-store copyback batch
mutex (`packages.common.copyback_guard`), because on node-27 the object-store
root IS the shared copyback root that node-22 writers promote `canonical/` trees
into. The raw and PNG-cache lanes are not locked: no mutex writer promotes into
them.

It deliberately does not touch `canonical/<storage-source>/grid/**` (grid
definitions cannot be regenerated on node-27), anything under the precipitation
cache root outside `precip/` (the MVT tile cache is a sibling there), forcing,
runs, or published products. Production retention deletes aged directories after
safety preflight and emits bounded JSON evidence for operator review.

Two environment gates exist for staged rollout and rollback and default to the
execute-only behaviour: ``NODE27_RAW_RETENTION_ENABLED`` (default true) and
``NODE27_RAW_RETENTION_PLAN_ONLY`` (default false). The cutoff anchor is the
display watermark, not the pipeline frontier; every summary discloses that
choice and its residual risk in its ``anchor`` block (issue #1407).

``NODE27_RAW_RETENTION_LANES`` (issue #2360) selects a subset of
``raw,canonical,precip-cache``; unset means all three. node-27 runs the
canonical lane in a system unit as the copyback root's owner and the other two
in the ``nwm`` user unit, both on the same cutoff rule.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from packages.common.copyback_guard import (
    DEFAULT_RETENTION_COPYBACK_LOCK_WAIT_BUDGET_SECONDS,
    CopybackLockBudgetExhausted,
    CopybackLockError,
    acquire_copyback_batch_lock,
    copyback_lock_failure_kind,
    count_copyback_lock_failures,
    release_copyback_batch_lock,
)
from packages.common.display_watermark import fetch_display_watermark
from packages.common.source_identity import normalize_source_id

# `services.precip.constants` is deliberately stdlib-only (its own docstring
# pins that), so naming the cache env here costs no numpy/netCDF4 import.
from services.precip.constants import FILE_CACHE_DIR_ENV

SCHEMA_VERSION = "nhms.node27_raw_retention.production.v5"
DEFAULT_RETENTION_DAYS = 14
DEFAULT_SOURCES = ("gfs", "ifs")
CYCLE_NAME_LENGTH = 10

# Lane identity: (target key prefix, `planned[].reason`).
RAW_LANE_KEY = "raw"
RAW_LANE_REASON = "raw_cycle_aged_out"
CANONICAL_LANE_KEY = "canonical"
CANONICAL_LANE_REASON = "canonical_cycle_aged_out"
PRECIP_CACHE_LANE_KEY = "precip-cache"
PRECIP_CACHE_LANE_REASON = "precip_cache_aged_out"
# `NODE27_RAW_RETENTION_LANES` vocabulary (#2360): the lane key prefixes above.
# Unset selects all three, which is the pre-#2360 run byte for byte.
ALL_LANES = frozenset({RAW_LANE_KEY, CANONICAL_LANE_KEY, PRECIP_CACHE_LANE_KEY})
LANE_NOT_SELECTED_REASON = "lane_not_selected"
# The one directory name under `canonical/<S>/` that is never a cycle: grid
# definitions are not reproducible from node-27, so they are pinned out of the
# target set explicitly instead of relying on `_parse_cycle_name` alone.
GRID_DIR_NAME = "grid"

# Anchor disclosure (issue #1407 / design D4). This process keeps the display
# watermark as its cutoff anchor instead of the pipeline frontier used by the
# out-of-pass cleanup CLI: the scheduler pass receipts and journal live on
# node-22 private /scratch, which node-27 cannot reach, and publishing the
# frontier across nodes needs a shared-store write surface that is out of scope
# here. The residual risk is disclosed in every summary rather than left
# implicit.
ANCHOR_MODE = "display_watermark"
ANCHOR_DECISION = "issue-1407-keep-watermark-anchor"
ANCHOR_RESIDUAL_RISK = "backfill cycles older than watermark - retention_days are unprotected"

# #1714: default pg_stat_activity attribution for this component. libpq treats
# fallback_application_name as a default only, so an operator's explicit
# ?application_name=... in NODE27_DISPLAY_WATERMARK_DATABASE_URL still wins.
_APPLICATION_NAME = "nhms-raw-retention"


def _attributed_connect(*args: Any, **kwargs: Any) -> Any:
    """``psycopg2.connect`` with this component's #1714 identity attached.

    This runner never opens a connection itself; its ONLY database touch is
    the watermark read delegated to
    ``packages.common.display_watermark.fetch_display_watermark``. Injecting
    this callable is what keeps that delegated connection attributable in
    ``pg_stat_activity``.

    psycopg2 is imported lazily so importing this module stays possible without
    the driver, exactly as ``display_watermark`` does it.
    """
    import psycopg2  # type: ignore[import-untyped]

    return psycopg2.connect(*args, fallback_application_name=_APPLICATION_NAME, **kwargs)


@dataclass(frozen=True)
class RawRetentionConfig:
    object_store_root: Path
    retention_days: int
    sources: frozenset[str]
    summary_path: Path | None
    # Env gates (issue #1407 / design D5). Defaults reproduce the execute-only
    # behaviour byte for byte; they exist for staged rollout and rollback, and
    # deliberately have no CLI flags (the ones 9c1625ee removed stay removed).
    enabled: bool = True
    dry_run: bool = False
    # Display-side PNG cache root (`NHMS_MVT_FILE_CACHE_DIR`). Optional on
    # purpose: an unconfigured cache root is a per-lane skip, never a preflight
    # blocker, so the first production tick after deploy still prunes raw and
    # canonical instead of returning zero deletions.
    precip_cache_root: Path | None = None
    # Lane selection (#2360, env-only like the gates above). On node-27 the
    # canonical lane runs in its own system unit as the copyback root's owner
    # and the nwm user unit runs raw + precip-cache; an unselected lane is one
    # `lane_not_selected` skip and is never probed, listed or locked.
    lanes: frozenset[str] = ALL_LANES


@dataclass(frozen=True)
class RetentionTarget:
    path: Path
    key: str
    source: str
    cycle_time: datetime
    size_bytes: int
    reason: str


def _env_int(name: str, *, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _split_sources(raw: str | None) -> frozenset[str]:
    values = raw if raw not in (None, "") else ",".join(DEFAULT_SOURCES)
    sources = {item.strip().lower() for item in str(values).split(",") if item.strip()}
    return frozenset(sources or DEFAULT_SOURCES)


def _split_lanes(raw: str | None) -> tuple[frozenset[str] | None, dict[str, Any] | None]:
    """The selected lanes, or the `lanes` preflight blocker.

    Unset is all three lanes. A set value is fail-closed: exact names only, and
    a value that trims to nothing is refused rather than read as "all" -- a
    blank line in the env file must not silently widen a canonical-only unit.
    """
    if raw is None:
        return ALL_LANES, None
    names = [item.strip() for item in raw.split(",")]
    lanes = frozenset(name for name in names if name)
    if not lanes:
        return None, {"field": "lanes", "reason": "empty"}
    unknown = sorted(lanes - ALL_LANES)
    if unknown:
        return None, {"field": "lanes", "reason": "unknown_lane", "value": unknown}
    return lanes, None


def _parse_cycle_name(name: str) -> datetime | None:
    if len(name) != CYCLE_NAME_LENGTH or not name.isdigit():
        return None
    try:
        return datetime.strptime(name, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError:
        return None


def _dir_size(path: Path) -> tuple[int, OSError | None]:
    """`path`'s regular-file bytes, plus the `OSError` that stopped the walk (#2309).

    The whole traversal sits inside the `try`, not only the loop body: the
    `rglob` generator itself advances through `os.scandir`, and on the pinned
    3.11 `pathlib` swallows only `PermissionError` while iterating, so a
    non-EACCES `OSError` there (ESTALE, EIO, EMFILE) used to escape through
    `collect_targets` and kill the whole tick before any deletion and before
    the summary write. Same contract as `_iter_dirs`: a returned error means
    the caller must not plan this target -- a tree that went stale half-walked
    may be the one node-22 is rewriting. The per-file `stat` guard stays: a
    single file vanishing mid-walk is sized as 0, not a reason to retire the
    target.
    """
    total = 0
    try:
        for child in path.rglob("*"):
            try:
                if child.is_file() and not child.is_symlink():
                    total += child.stat().st_size
            except OSError:
                continue
    except OSError as error:
        return total, error
    return total, None


def _safe_resolved_dir(path: Path, *, label: str) -> tuple[Path | None, dict[str, Any] | None]:
    if not path.is_absolute():
        return None, {"field": label, "reason": "path_not_absolute", "path": str(path)}
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as error:
        return None, {
            "field": label,
            "reason": "path_unavailable",
            "path": str(path),
            "error": str(error),
            "error_type": type(error).__name__,
        }
    if resolved == Path("/"):
        return None, {"field": label, "reason": "path_is_root", "path": str(resolved)}
    if not resolved.is_dir():
        return None, {"field": label, "reason": "path_not_directory", "path": str(resolved)}
    if resolved.is_symlink():
        return None, {"field": label, "reason": "path_is_symlink", "path": str(resolved)}
    return resolved, None


def config_from_env(args: argparse.Namespace) -> tuple[RawRetentionConfig | None, list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    root_value = (
        args.object_store_root
        or os.getenv("NODE27_RAW_RETENTION_OBJECT_STORE_ROOT")
        or os.getenv("OBJECT_STORE_ROOT")
        or ""
    ).strip()
    if not root_value:
        blockers.append({"field": "object_store_root", "reason": "missing"})
        root = Path()
    else:
        root = Path(root_value)
    resolved_root, blocker = _safe_resolved_dir(root, label="object_store_root")
    if blocker is not None:
        blockers.append(blocker)

    retention_days = args.retention_days or _env_int("NODE27_RAW_RETENTION_DAYS", default=DEFAULT_RETENTION_DAYS)
    if retention_days <= 0:
        blockers.append({"field": "retention_days", "reason": "must_be_positive", "value": retention_days})

    summary_value = args.summary_path or os.getenv("NODE27_RAW_RETENTION_SUMMARY_PATH") or ""
    summary_path = Path(summary_value).expanduser() if summary_value.strip() else None
    if summary_path is not None and not summary_path.is_absolute():
        blockers.append({"field": "summary_path", "reason": "path_not_absolute", "path": str(summary_path)})

    sources = _split_sources(args.sources or os.getenv("NODE27_RAW_RETENTION_SOURCES"))
    if not sources:
        blockers.append({"field": "sources", "reason": "empty"})

    # Same env name the display API reads (`services.precip.constants` /
    # `services.tiles.mvt`); it must hold the value the display PROCESS has, not
    # the value in infra/env/display.example. Blank or unset is not a blocker --
    # the cache lane skips itself and the other two lanes still prune.
    cache_value = (os.getenv(FILE_CACHE_DIR_ENV) or "").strip()
    precip_cache_root = Path(cache_value).expanduser() if cache_value else None

    lanes, lanes_blocker = _split_lanes(os.getenv("NODE27_RAW_RETENTION_LANES"))
    if lanes_blocker is not None:
        blockers.append(lanes_blocker)

    if blockers or resolved_root is None or lanes is None:
        return None, blockers
    return (
        RawRetentionConfig(
            object_store_root=resolved_root,
            retention_days=retention_days,
            sources=sources,
            summary_path=summary_path,
            enabled=_env_flag("NODE27_RAW_RETENTION_ENABLED", default=True),
            # Deliberately not the retired NODE27_RAW_RETENTION_DRY_RUN name: that
            # one defaulted to true, so a leftover line in node-27's live config
            # would silently return production to zero deletions. Under the new
            # name any such leftover line is inert.
            dry_run=_env_flag("NODE27_RAW_RETENTION_PLAN_ONLY", default=False),
            precip_cache_root=precip_cache_root,
            lanes=lanes,
        ),
        [],
    )


def _safe_target(lane_root: Path, target: Path) -> bool:
    """True only for a real `<lane_root>/<S>/<K>` directory (never a symlink).

    The parts count is what pins every lane at exactly two levels below its own
    root, so no lane can ever reach a sibling tree or a grid definition.
    """
    try:
        relative = target.resolve(strict=True).relative_to(lane_root)
    except (OSError, ValueError):
        return False
    return len(relative.parts) == 2 and target.is_dir() and not target.is_symlink()


def _resolve_lane_root(
    root: Path, *, key: str, prefix: str
) -> tuple[Path | None, dict[str, Any] | None]:
    """The resolved lane root, or the skip entry that retires ONLY this lane.

    Absence and unsafety are symmetric across the three lanes and always local:
    making any of them a preflight blocker would zero out raw retention on the
    first production tick after deploy, before an operator has edited the env
    file -- strictly worse than not pruning a cache.

    Every probe here is wrapped, and on the repo's pinned interpreter that
    wrapping is load-bearing. On CPython 3.11-3.13 `pathlib` swallows only
    ENOENT/ENOTDIR/EBADF/ELOOP (`_IGNORED_ERRNOS`; 3.11/3.12 `pathlib.py`,
    3.13 `pathlib/_abc.py`) and re-raises the rest, so an EACCES or ESTALE on a
    lane root (a non-traversable ancestor -- e.g. a 0750 object-store tree owned
    by another uid) would without this `except OSError` escape `collect_targets`,
    which runs to completion BEFORE any deletion. That would retire all three
    lanes at once and skip the summary write entirely, leaving the
    `--summary-path` receipt silently stale.

    That errno set is version-scoped, not a property of `pathlib`. From CPython
    3.14 there is no `_IGNORED_ERRNOS`: `exists`/`is_dir`/`is_symlink` route
    through `os.path.exists`/`isdir`/`islink`, which swallow EVERY `OSError`
    (measured 3.14.2). The guard below then degrades to a no-op and an
    unreadable root falls out of `root.exists()` as `<lane>_root_missing`
    instead of `<lane>_root_unsafe` / `path_unavailable`. That is still safe --
    one per-lane skip, no wider blast radius -- but the receipt mislabels "not
    traversable" as "absent". `pyproject.toml` declares `requires-python
    >=3.11`, so the mislabel is inside the supported range; it is a documented
    limit that #2104 did NOT fix (an explicit non-goal there, as in #2099), so
    no open issue promises the label.

    Locality is the promise this function makes on every supported version, so
    an unreadable root is one lane's skip like any other. #2104 extended that
    promise past this gate: every probe here stats the lane root FROM ITS
    PARENT, so the first syscall that needs `x` on the lane root itself lives
    in `_collect_mapped_lane` / `_iter_dirs`, and those are wrapped too.
    """
    try:
        if root.is_symlink():
            return None, {
                "key": key,
                "reason": f"{prefix}_root_unsafe",
                "path": str(root),
                "detail": "path_is_symlink",
            }
        if not root.exists():
            return None, {"key": key, "reason": f"{prefix}_root_missing", "path": str(root)}
        if not root.is_dir():
            return None, {
                "key": key,
                "reason": f"{prefix}_root_unsafe",
                "path": str(root),
                "detail": "path_not_directory",
            }
        resolved, blocker = _safe_resolved_dir(root, label=f"{prefix}_root")
    except OSError as error:
        return None, _unavailable_skip(
            key=key, reason=f"{prefix}_root_unsafe", path=root, error=error
        )
    if resolved is None:
        entry: dict[str, Any] = {
            "key": key,
            "reason": f"{prefix}_root_unsafe",
            "path": str(root),
            "detail": (blocker or {}).get("reason", "path_unsafe"),
        }
        # The errno the probe saw, when the blocker carries one. Forwarded
        # rather than re-derived so `error_type` is always the exception class
        # name (#2104 item 3), never a reason string.
        for field in ("error", "error_type"):
            value = (blocker or {}).get(field)
            if value is not None:
                entry[field] = value
        return None, entry
    return resolved, None


def _unavailable_skip(*, key: str, reason: str, path: Path, error: OSError) -> dict[str, Any]:
    """The one skip shape for "a probe on this path raised" (#2104).

    Lane roots and per-source roots share it so the receipt reads the same way
    at both depths: the pinned `(reason, detail)` pair plus the errno that the
    pre-#2104 receipt dropped on the floor.
    """
    return {
        "key": key,
        "reason": reason,
        "path": str(path),
        "detail": "path_unavailable",
        "error": str(error),
        "error_type": type(error).__name__,
    }


def _collect_raw_lane(
    config: RawRetentionConfig, *, raw_root: Path, cutoff: datetime
) -> tuple[list[RetentionTarget], list[dict[str, Any]]]:
    """Raw lane: iterate the directories that exist and match them case-insensitively."""
    skipped: list[dict[str, Any]] = []
    targets: list[RetentionTarget] = []
    source_dirs, listing_error = _iter_dirs(raw_root)
    if listing_error is not None:
        skipped.append(
            _unavailable_skip(
                key=RAW_LANE_KEY,
                reason=f"{RAW_LANE_KEY}_root_unsafe",
                path=raw_root,
                error=listing_error,
            )
        )
    for source_dir in source_dirs:
        source_key = source_dir.name.lower()
        if source_key not in config.sources:
            skipped.append({"key": f"{RAW_LANE_KEY}/{source_dir.name}", "reason": "source_not_enabled"})
            continue
        cycle_dirs, source_error = _iter_dirs(source_dir)
        if source_error is not None:
            skipped.append(
                _unavailable_skip(
                    key=f"{RAW_LANE_KEY}/{source_dir.name}",
                    reason=f"{RAW_LANE_KEY}_source_unsafe",
                    path=source_dir,
                    error=source_error,
                )
            )
            continue
        for cycle_dir in cycle_dirs:
            key = f"{RAW_LANE_KEY}/{source_dir.name}/{cycle_dir.name}"
            cycle_time = _parse_cycle_name(cycle_dir.name)
            if cycle_time is None:
                skipped.append({"key": key, "reason": "unparseable_cycle_name"})
                continue
            if cycle_time >= cutoff:
                skipped.append({"key": key, "reason": "within_retention_window"})
                continue
            if not _safe_target(raw_root, cycle_dir):
                skipped.append({"key": key, "reason": "unsafe_target_path"})
                continue
            size_bytes, size_error = _dir_size(cycle_dir)
            if size_error is not None:
                skipped.append(
                    _unavailable_skip(
                        key=key,
                        reason=f"{RAW_LANE_KEY}_target_unsafe",
                        path=cycle_dir,
                        error=size_error,
                    )
                )
                continue
            targets.append(
                RetentionTarget(
                    path=cycle_dir,
                    key=key,
                    source=source_dir.name,
                    cycle_time=cycle_time,
                    size_bytes=size_bytes,
                    reason=RAW_LANE_REASON,
                )
            )
    return targets, skipped


def _collect_mapped_lane(
    config: RawRetentionConfig,
    *,
    lane_root: Path,
    key_prefix: str,
    skip_prefix: str,
    reason: str,
    cutoff: datetime,
) -> tuple[list[RetentionTarget], list[dict[str, Any]]]:
    """Canonical / PNG-cache lane: enumerate CONFIGURED sources through
    `normalize_source_id`, never the directory names found on disk.

    That direction is the constructive guarantee that no `canonical/ifs/...` or
    `precip-cache/ifs/...` path can be produced from the lower-case configured
    token: the storage spelling can only come out of the shared normalizer, so
    the mirror tree and the PNG cache tree are addressed by one identity.

    `key_prefix` spells the lane in target keys (`precip-cache`); `skip_prefix`
    is the lane's skip vocabulary (`precip_cache`), so a per-source failure
    reads `precip_cache_source_unsafe` beside `precip_cache_root_unsafe`.
    """
    skipped: list[dict[str, Any]] = []
    targets: list[RetentionTarget] = []
    for source in sorted(config.sources):
        try:
            storage_source = normalize_source_id(source)
        except ValueError:
            # `_split_sources` accepts arbitrary free text from the env file; an
            # unmappable entry retires itself, never the run.
            skipped.append({"key": f"{key_prefix}/{source}", "reason": "source_unmappable"})
            continue
        source_root = lane_root / storage_source
        # First syscall that needs `x` on the lane root itself (the lane-root
        # gate only ever stats it from its parent). Locality contract:
        # openspec spec `node27-raw-retention` -- one source's OSError retires
        # that source, never the lane and never the run.
        try:
            if source_root.is_symlink() or not source_root.is_dir():
                continue
        except OSError as error:
            skipped.append(
                _unavailable_skip(
                    key=f"{key_prefix}/{storage_source}",
                    reason=f"{skip_prefix}_source_unsafe",
                    path=source_root,
                    error=error,
                )
            )
            continue
        cycle_dirs, listing_error = _iter_dirs(source_root)
        if listing_error is not None:
            skipped.append(
                _unavailable_skip(
                    key=f"{key_prefix}/{storage_source}",
                    reason=f"{skip_prefix}_source_unsafe",
                    path=source_root,
                    error=listing_error,
                )
            )
            continue
        for cycle_dir in cycle_dirs:
            key = f"{key_prefix}/{storage_source}/{cycle_dir.name}"
            if cycle_dir.name == GRID_DIR_NAME:
                skipped.append({"key": key, "reason": "grid_definitions_preserved"})
                continue
            cycle_time = _parse_cycle_name(cycle_dir.name)
            if cycle_time is None:
                skipped.append({"key": key, "reason": "unparseable_cycle_name"})
                continue
            if cycle_time >= cutoff:
                skipped.append({"key": key, "reason": "within_retention_window"})
                continue
            if not _safe_target(lane_root, cycle_dir):
                skipped.append({"key": key, "reason": "unsafe_target_path"})
                continue
            size_bytes, size_error = _dir_size(cycle_dir)
            if size_error is not None:
                skipped.append(
                    _unavailable_skip(
                        key=key,
                        reason=f"{skip_prefix}_target_unsafe",
                        path=cycle_dir,
                        error=size_error,
                    )
                )
                continue
            targets.append(
                RetentionTarget(
                    path=cycle_dir,
                    key=key,
                    source=storage_source,
                    cycle_time=cycle_time,
                    size_bytes=size_bytes,
                    reason=reason,
                )
            )
    return targets, skipped


def collect_targets(config: RawRetentionConfig, *, now: datetime) -> tuple[list[RetentionTarget], list[dict[str, Any]]]:
    """The three lanes of one retention run, on ONE cutoff.

    Raw, canonical and PNG-cache targets are collected together so a cycle's
    mirror directory and its rendered PNGs are removed by the same tick.
    With `NODE27_RAW_RETENTION_LANES` two units split the lanes (#2360); both
    keep this one cutoff rule, so the pair still ages out on the same date.
    A lane outside `config.lanes` is recorded in its usual position and its
    root is never touched.
    """
    cutoff = now.astimezone(UTC) - timedelta(days=config.retention_days)
    skipped: list[dict[str, Any]] = []
    targets: list[RetentionTarget] = []

    if RAW_LANE_KEY not in config.lanes:
        skipped.append({"key": RAW_LANE_KEY, "reason": LANE_NOT_SELECTED_REASON})
        raw_root, raw_blocker = None, None
    else:
        raw_root, raw_blocker = _resolve_lane_root(
            config.object_store_root / "raw", key=RAW_LANE_KEY, prefix="raw"
        )
    if raw_blocker is not None:
        skipped.append(raw_blocker)
    elif raw_root is not None:
        lane_targets, lane_skipped = _collect_raw_lane(config, raw_root=raw_root, cutoff=cutoff)
        targets.extend(lane_targets)
        skipped.extend(lane_skipped)

    if CANONICAL_LANE_KEY not in config.lanes:
        skipped.append({"key": CANONICAL_LANE_KEY, "reason": LANE_NOT_SELECTED_REASON})
        canonical_root, canonical_blocker = None, None
    else:
        canonical_root, canonical_blocker = _resolve_lane_root(
            config.object_store_root / "canonical", key=CANONICAL_LANE_KEY, prefix="canonical"
        )
    if canonical_blocker is not None:
        skipped.append(canonical_blocker)
    elif canonical_root is not None:
        lane_targets, lane_skipped = _collect_mapped_lane(
            config,
            lane_root=canonical_root,
            key_prefix=CANONICAL_LANE_KEY,
            skip_prefix="canonical",
            reason=CANONICAL_LANE_REASON,
            cutoff=cutoff,
        )
        targets.extend(lane_targets)
        skipped.extend(lane_skipped)

    if PRECIP_CACHE_LANE_KEY not in config.lanes:
        # Checked before the unconfigured-root branch: an unselected lane says
        # so, whatever its root setting.
        skipped.append({"key": PRECIP_CACHE_LANE_KEY, "reason": LANE_NOT_SELECTED_REASON})
        return targets, skipped
    if config.precip_cache_root is None:
        skipped.append({"key": PRECIP_CACHE_LANE_KEY, "reason": "precip_cache_root_unconfigured"})
        return targets, skipped
    # Only ever descend into `<cache>/precip`: the MVT tile cache is a sibling
    # under the same root and must never be enumerated, let alone deleted.
    cache_root, cache_blocker = _resolve_lane_root(
        config.precip_cache_root / "precip", key=PRECIP_CACHE_LANE_KEY, prefix="precip_cache"
    )
    if cache_blocker is not None:
        skipped.append(cache_blocker)
    elif cache_root is not None:
        lane_targets, lane_skipped = _collect_mapped_lane(
            config,
            lane_root=cache_root,
            key_prefix=PRECIP_CACHE_LANE_KEY,
            skip_prefix="precip_cache",
            reason=PRECIP_CACHE_LANE_REASON,
            cutoff=cutoff,
        )
        targets.extend(lane_targets)
        skipped.extend(lane_skipped)
    return targets, skipped


def _iter_dirs(parent: Path) -> tuple[list[Path], OSError | None]:
    """`parent`'s real subdirectories, plus the `OSError` that stopped the listing.

    The comprehension is inside the `try` because `iterdir()` is not the only
    syscall here: on a readable but non-traversable directory (`0o444`)
    `iterdir()` succeeds and the first `is_dir()` raises EACCES -- on the pinned
    3.11 that escaped every caller and retired the whole run (#2104 item 2).

    The error is returned instead of swallowed so a caller can never read a
    listing that raised as an empty directory: `([], None)` means empty,
    `([], error)` means unavailable, and the two get different skip entries.
    """
    try:
        entries = sorted(parent.iterdir())
        return [entry for entry in entries if entry.is_dir() and not entry.is_symlink()], None
    except OSError as error:
        return [], error


def _target_payload(target: RetentionTarget) -> dict[str, Any]:
    return {
        "key": target.key,
        "path": str(target.path),
        "source": target.source,
        "cycle_time": target.cycle_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "size_bytes": target.size_bytes,
        "reason": target.reason,
    }


@dataclass
class _CanonicalLockBudget:
    """The pass-level acquisition budget every canonical removal draws on."""

    budget_seconds: float
    remaining_seconds: float


def _remove_canonical_under_copyback_mutex(
    target: Path, *, copyback_root: Path, budget: _CanonicalLockBudget
) -> None:
    """Hold the copyback batch mutex for exactly this one canonical `rmtree`.

    `posix`, not `flock`: this process runs on the host that exports the
    copyback root, where a local `flock` does not exclude the NFS clients'
    `flock` writers but a POSIX record lock does. This CLI is single-threaded
    and never opens the lock file elsewhere, which that primitive requires.

    Only the acquisition is charged against the pass budget, never the removal;
    once the budget is spent the entry is refused before any attempt.
    """
    if budget.remaining_seconds <= 0:
        raise CopybackLockBudgetExhausted(
            f"copyback batch lock wait budget of {budget.budget_seconds}s "
            f"is exhausted for this retention pass; {target} was not removed"
        )
    started = time.monotonic()
    try:
        fd = acquire_copyback_batch_lock(
            copyback_root, timeout_seconds=budget.remaining_seconds, primitive="posix"
        )
    finally:
        budget.remaining_seconds -= time.monotonic() - started
    try:
        shutil.rmtree(target)
    finally:
        release_copyback_batch_lock(fd, primitive="posix")


def run_retention(
    config: RawRetentionConfig,
    *,
    now: datetime,
    reference_time: datetime | None = None,
    copyback_lock_wait_budget_seconds: float | None = None,
) -> dict[str, Any]:
    """Plan and, unless gated off, execute one retention pass.

    ``copyback_lock_wait_budget_seconds`` exists for tests; ``None`` reads the
    shared module default at call time.
    """
    started_at = now.astimezone(UTC)
    reference_time = (reference_time or started_at).astimezone(UTC)
    cutoff = reference_time - timedelta(days=config.retention_days)
    base = {
        "schema_version": SCHEMA_VERSION,
        "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reference_time": reference_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "object_store_root": str(config.object_store_root),
        "raw_root": str(config.object_store_root / "raw"),
        "canonical_root": str(config.object_store_root / "canonical"),
        "precip_cache_root": (
            None if config.precip_cache_root is None else str(config.precip_cache_root)
        ),
        "sources": sorted(config.sources),
        # Which lanes this run owns (#2360): on node-27 two units write
        # summaries, and this field says which one wrote it.
        "lanes": sorted(config.lanes),
        "retention_days": config.retention_days,
        "cutoff": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "enabled": config.enabled,
        "dry_run": config.dry_run,
        "anchor": _anchor_disclosure(reference_time),
    }
    if not config.enabled:
        return {
            **base,
            "status": "disabled",
            "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "execution_mode": "disabled",
            "counts": {"planned": 0, "deleted": 0, "skipped": 0, "failed": 0},
            "planned": [],
            "deleted": [],
            "skipped": [],
            "failed": [],
            "copyback_lock_failures": count_copyback_lock_failures([]),
            "freed_bytes": 0,
        }
    targets, skipped = collect_targets(config, now=reference_time)
    planned = [_target_payload(target) for target in targets]
    deleted: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    freed_bytes = 0
    if not config.dry_run:
        budget_seconds = float(
            DEFAULT_RETENTION_COPYBACK_LOCK_WAIT_BUDGET_SECONDS
            if copyback_lock_wait_budget_seconds is None
            else copyback_lock_wait_budget_seconds
        )
        lock_budget = _CanonicalLockBudget(budget_seconds=budget_seconds, remaining_seconds=budget_seconds)
        for target, payload in zip(targets, planned, strict=True):
            try:
                if target.reason == CANONICAL_LANE_REASON:
                    _remove_canonical_under_copyback_mutex(
                        target.path, copyback_root=config.object_store_root, budget=lock_budget
                    )
                else:
                    shutil.rmtree(target.path)
            except CopybackLockError as error:
                # Timeout, unsafe/unopenable lock file, or a spent pass budget:
                # the tree is kept for the next tick. `lock_unsafe` is what a
                # canonical lane run by anyone but the copyback root's owner
                # gets, because the lock file is `0600` owned by it; on node-27
                # that lane runs in its own unit as that owner (#2360).
                failed.append(
                    {
                        **payload,
                        "error": str(error),
                        "error_type": type(error).__name__,
                        "lock_failure": copyback_lock_failure_kind(error),
                    }
                )
                continue
            except OSError as error:
                # `error_type` keeps a permission denial distinguishable from
                # other IO failures in the receipt, and since #2100 that
                # distinction is an incident signal rather than a known limit:
                # the canonical mirror is `2775` with the shared group this
                # runner belongs to (gid 1107 `nwmuser`), set by the mirror
                # producers on every directory they own and by the owner-side
                # sweep on the rest. A `PermissionError` on a canonical target
                # therefore means either the producer mode regressed or that
                # source was never swept -- a storage source added to
                # `NODE27_RAW_RETENTION_SOURCES` without its sweep fails closed
                # here by design, with zero bytes removed. Remedy and the exact
                # sweep commands: `docs/runbooks/current-production-ops.md` 5.3
                # (#2100). Nothing in this script is the fix; it reports.
                failed.append({**payload, "error": str(error), "error_type": type(error).__name__})
                continue
            deleted.append(payload)
            freed_bytes += int(payload["size_bytes"])
    finished_at = datetime.now(UTC)
    return {
        **base,
        "status": "completed",
        "finished_at": finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "execution_mode": "plan_only" if config.dry_run else "production_execute",
        "counts": {
            "planned": len(planned),
            "deleted": len(deleted),
            "skipped": len(skipped),
            "failed": len(failed),
        },
        "planned": planned,
        "deleted": deleted,
        "skipped": skipped,
        "failed": failed,
        "copyback_lock_failures": count_copyback_lock_failures(failed),
        "freed_bytes": freed_bytes,
    }


def _anchor_disclosure(reference_time: datetime) -> dict[str, Any]:
    """Say in the summary which anchor bounded this run, and what it misses."""
    return {
        "mode": ANCHOR_MODE,
        "reference_time": reference_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "frontier_active_lower_bound": None,
        "decision": ANCHOR_DECISION,
        "residual_risk": ANCHOR_RESIDUAL_RISK,
    }


def _blocked_payload(blockers: Iterable[dict[str, Any]]) -> dict[str, Any]:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "preflight_blocked",
        "execution_mode": "preflight_blocked",
        "started_at": now,
        "finished_at": now,
        "blockers": list(blockers),
        "counts": {"planned": 0, "deleted": 0, "skipped": 0, "failed": 0},
        "planned": [],
        "deleted": [],
        "skipped": [],
        "failed": [],
        "copyback_lock_failures": count_copyback_lock_failures([]),
        "freed_bytes": 0,
    }


def _write_summary(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--object-store-root")
    parser.add_argument("--retention-days", type=int)
    parser.add_argument("--sources")
    parser.add_argument("--summary-path")
    parser.add_argument("--reference-time")
    return parser


def _parse_reference_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("reference time is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("reference time must be timezone-aware")
    return parsed.astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config, blockers = config_from_env(args)
    if config is None:
        payload = _blocked_payload(blockers)
        summary_value = args.summary_path or os.getenv("NODE27_RAW_RETENTION_SUMMARY_PATH") or ""
        if summary_value.strip():
            _write_summary(Path(summary_value).expanduser(), payload)
        print(json.dumps(payload, sort_keys=True))
        return 2
    try:
        reference_time = (
            _parse_reference_time(args.reference_time)
            if args.reference_time is not None
            else fetch_display_watermark(
                os.getenv("NODE27_DISPLAY_WATERMARK_DATABASE_URL", ""),
                connect=_attributed_connect,
            )
        )
    except Exception as error:
        payload = _blocked_payload(
            [{"field": "display_watermark", "reason": type(error).__name__}]
        )
        if config.summary_path is not None:
            _write_summary(config.summary_path, payload)
        print(json.dumps(payload, sort_keys=True))
        return 2
    payload = run_retention(
        config, now=datetime.now(UTC), reference_time=reference_time
    )
    if config.summary_path is not None:
        _write_summary(config.summary_path, payload)
    print(json.dumps(payload, sort_keys=True))
    return 1 if payload["counts"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
