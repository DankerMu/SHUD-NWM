#!/usr/bin/env python
"""Retention cleanup for the node-27 MVT tile FILE cache (issue #2032).

This runner prunes exactly three path shapes under `NHMS_MVT_FILE_CACHE_DIR`,
on ONE wall-clock cutoff, in ONE run:

    <root>/<hh>/<sha256>.pbf                      the cached tile body
    <root>/<hh>/.<sha256>.pbf.<pid>.tmp           a crashed write intermediate
    <root>/.locks/<hh>/<sha256>.lock              a tile generation lock file

`<hh>` is the first two characters of the sha256 cache key, so the enumeration
is fixed at TWO levels of exactly-`[0-9a-f]{2}` directories. Everything else
under the cache root -- `<root>/precip/**` above all, which
`scripts/node27_raw_retention.py` owns -- is unreachable BY CONSTRUCTION rather
than by a blacklist: `precip` does not match `[0-9a-f]{2}`, files at the root
are never enumerated, and nothing one level deeper is either. The summary still
names `precip_root_untouched` explicitly so an operator can assert it.

The cutoff anchor is the WALL CLOCK, deliberately not the display watermark the
raw-retention runner uses: a pruned tile is regenerated from the database on the
next request (a cache miss), whereas that runner's input is an irreplaceable
mirror. This runner therefore opens NO database connection and imports nothing
from `scripts.node27_raw_retention` (which would pull in psycopg and the
watermark read). It is stdlib-only.

Two environment gates exist for staged rollout and rollback and default to the
execute-only behaviour: ``NODE27_MVT_CACHE_RETENTION_ENABLED`` (default true)
and ``NODE27_MVT_CACHE_RETENTION_PLAN_ONLY`` (default false). Neither is named
``*_DRY_RUN`` -- that name defaulted to true, so a leftover line in a live
config would silently return production to zero deletions (issue #1407).
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "nhms.node27_mvt_cache_retention.production.v1"
DEFAULT_RETENTION_DAYS = 14

# The same env name the display API reads (`services/tiles/mvt.py`
# `MVT_FILE_CACHE_DIR_ENV`); it must hold the value the display PROCESS has.
CACHE_ROOT_ENV = "NHMS_MVT_FILE_CACHE_DIR"
LOCKS_DIR_NAME = ".locks"
PRECIP_DIR_NAME = "precip"

# The path shapes, kept in one place because they are a CROSS-PROCESS CONTRACT
# with `services/tiles/mvt.py::_file_cache_path` / `_file_cache_lock_path` /
# `_write_file_cache`. `tests/test_node27_mvt_cache_retention.py` builds real
# paths through those producers and asserts these patterns match them exactly,
# so a layout change on either side reds there instead of silently switching
# this runner off.
HEX_DIR_PATTERN = re.compile(r"^[0-9a-f]{2}$")
PBF_PATTERN = re.compile(r"^[0-9a-f]{64}\.pbf$")
TMP_PATTERN = re.compile(r"^\.[0-9a-f]{64}\.pbf\.[0-9]+\.tmp$")
LOCK_PATTERN = re.compile(r"^[0-9a-f]{64}\.lock$")

KIND_PBF = "pbf"
KIND_TMP = "tmp"
KIND_LOCK = "lock"

_TILE_LANE_PATTERNS = ((PBF_PATTERN, KIND_PBF), (TMP_PATTERN, KIND_TMP))
_LOCK_LANE_PATTERNS = ((LOCK_PATTERN, KIND_LOCK),)


@dataclass(frozen=True)
class MvtCacheRetentionConfig:
    cache_root: Path
    retention_days: int
    summary_path: Path | None
    # Rollout / rollback gates. Defaults reproduce execute-only behaviour.
    enabled: bool = True
    plan_only: bool = False
    # An explicit `--reference-time`; `None` means "the wall clock at start".
    reference_time: datetime | None = None


@dataclass(frozen=True)
class CacheTarget:
    path: Path
    kind: str
    size_bytes: int
    mtime: datetime


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_reference_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("reference time must be timezone-aware")
    return parsed.astimezone(UTC)


def _cache_root_blocker(root: Path) -> dict[str, Any] | None:
    """The first reason this path may not be enumerated, or `None`.

    Order matters. `is_symlink()` runs on the UNRESOLVED, `expanduser()`-ed path
    and BEFORE `exists()`, for two reasons: a dangling symlink answers
    `exists()` False and would otherwise be mislabelled `path_missing`, and the
    sibling's `_safe_resolved_dir` pattern (`resolve(strict=True)` then
    `is_symlink()`) is dead code -- a resolved path is never a symlink. The
    unresolved path is also what gets enumerated, which is the same view
    `services/tiles/mvt.py` has (`Path(root).expanduser()`, no resolve).

    Every probe is wrapped in `except OSError` for the reason
    `node27_raw_retention._resolve_lane_root` documents at length: on the pinned
    interpreter `pathlib` swallows only ENOENT/ENOTDIR/EBADF/ELOOP and re-raises
    the rest, so an EACCES or ESTALE here would escape preflight and skip the
    summary write entirely, leaving a silently stale receipt.
    """
    if not root.is_absolute():
        return {"field": "cache_root", "reason": "path_not_absolute", "path": str(root)}
    if root == Path("/"):
        return {"field": "cache_root", "reason": "path_is_root", "path": str(root)}
    try:
        if root.is_symlink():
            return {"field": "cache_root", "reason": "path_is_symlink", "path": str(root)}
        if not root.exists():
            return {"field": "cache_root", "reason": "path_missing", "path": str(root)}
        if not root.is_dir():
            return {"field": "cache_root", "reason": "path_not_directory", "path": str(root)}
    except OSError as error:
        return {
            "field": "cache_root",
            "reason": "path_unavailable",
            "path": str(root),
            "error": str(error),
        }
    return None


def _resolve_retention_days(args: argparse.Namespace) -> tuple[int | None, dict[str, Any] | None]:
    """Strict integer >= 1, from the CLI when given, else the env, else 14.

    DELIBERATE DEPARTURE from `node27_raw_retention._env_int`, which falls back
    to the default on an unparseable or non-positive value: here a malformed
    age is a preflight blocker, so an operator's typo cannot silently restore a
    14-day cutoff on a unit meant to run at 3. `args.retention_days is None` --
    not `or` -- is what makes `--retention-days 0` a blocker instead of falling
    through to the env default.
    """
    if args.retention_days is not None:
        raw: str | None = args.retention_days
        field = "retention_days"
    else:
        raw = os.getenv("NODE27_MVT_CACHE_RETENTION_DAYS")
        field = "NODE27_MVT_CACHE_RETENTION_DAYS"
    if raw is None or raw.strip() == "":
        return DEFAULT_RETENTION_DAYS, None
    try:
        parsed = int(raw.strip())
    except ValueError:
        return None, {"field": field, "reason": "not_an_integer", "value": raw}
    if parsed < 1:
        return None, {"field": field, "reason": "must_be_at_least_one", "value": parsed}
    return parsed, None


def _summary_sink(args: argparse.Namespace) -> Path | None:
    """Where the receipt goes: `--summary-path`, or `None` for stdout only.

    The CLI flag is the ONLY source. There is deliberately no env fallback: the
    wrapper (`scripts/node27_mvt_cache_retention_once.sh`) resolves its own
    `NODE27_MVT_CACHE_RETENTION_SUMMARY_PATH` override and always passes the
    result as `--summary-path`, so a second reader here would only add a way for
    the two to disagree. A RELATIVE path is a valid sink -- it is a file this
    process creates, not a tree it deletes from, so the absoluteness that
    `cache_root` needs buys nothing here.
    """
    value = (args.summary_path or "").strip()
    return Path(value).expanduser() if value else None


def config_from_env(
    args: argparse.Namespace,
) -> tuple[MvtCacheRetentionConfig | None, list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []

    root_value = (os.getenv(CACHE_ROOT_ENV) or "").strip()
    cache_root: Path | None = None
    if not root_value:
        blockers.append({"field": "cache_root", "reason": "missing", "env": CACHE_ROOT_ENV})
    else:
        candidate = Path(root_value).expanduser()
        blocker = _cache_root_blocker(candidate)
        if blocker is not None:
            blockers.append(blocker)
        else:
            cache_root = candidate

    retention_days, days_blocker = _resolve_retention_days(args)
    if days_blocker is not None:
        blockers.append(days_blocker)

    reference_time: datetime | None = None
    if args.reference_time is not None:
        try:
            reference_time = _parse_reference_time(args.reference_time)
        except ValueError:
            blockers.append(
                {"field": "reference_time", "reason": "not_rfc3339", "value": args.reference_time}
            )

    summary_path = _summary_sink(args)

    if blockers or cache_root is None or retention_days is None:
        return None, blockers
    return (
        MvtCacheRetentionConfig(
            cache_root=cache_root,
            retention_days=retention_days,
            summary_path=summary_path,
            enabled=_env_flag("NODE27_MVT_CACHE_RETENTION_ENABLED", default=True),
            plan_only=_env_flag("NODE27_MVT_CACHE_RETENTION_PLAN_ONLY", default=False),
            reference_time=reference_time,
        ),
        [],
    )


def _enumeration_failure(directory: Path, error: OSError) -> dict[str, Any]:
    """The `failed[]` entry for a directory this run could not READ AT ALL.

    Deliberately a FAILURE and not a skip: a directory that cannot be
    enumerated hides an unknown number of aged files, so the run neither
    deleted them nor can promise there was nothing to delete. Reporting it as a
    skip would leave `counts.failed == 0` and rc 0, and the env template's
    health criterion (`.failed | length == 0`) would stay green over a cache
    root that is silently no longer being pruned. `kind` is `None` because the
    entry names a directory, not one of the three target shapes.
    """
    return {
        "path": str(directory),
        "kind": None,
        "reason": "enumeration_unavailable",
        "error": str(error),
        "error_type": type(error).__name__,
    }


def _hex_directories(parent: Path) -> tuple[list[Path], dict[str, Any] | None]:
    """The `[0-9a-f]{2}` NON-SYMLINK subdirectories of `parent`, sorted.

    Returns `(directories, failure_or_None)`. A failure means `parent` itself
    could not be listed; the caller records it and carries on with the other
    lane, so one unreadable directory never suppresses the rest of the run.

    `follow_symlinks=False` throughout: `DirEntry.is_dir()` follows links by
    default, which would let a `<root>/ab -> /elsewhere` symlink drag the
    deletion surface out of the cache root. The PER-ENTRY `OSError` stays a
    silent skip: that is the ENOENT race of an entry vanishing mid-scan, i.e.
    exactly the concurrency this runner is built to tolerate.
    """
    found: list[Path] = []
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                if not HEX_DIR_PATTERN.match(entry.name):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        found.append(Path(entry.path))
                except OSError:
                    continue
    except OSError as error:
        return [], _enumeration_failure(parent, error)
    return sorted(found), None


def _lane_targets(
    hex_dir: Path,
    patterns: tuple[tuple[re.Pattern[str], str], ...],
    cutoff: datetime,
) -> tuple[list[CacheTarget], dict[str, Any] | None]:
    """`(aged targets, failure_or_None)` for one `<hh>` directory.

    Same split as `_hex_directories`: an unreadable `<hh>` is a `failed[]`
    entry, a single entry that vanishes or refuses `stat` mid-scan is not.
    """
    cutoff_ts = cutoff.timestamp()
    found: list[CacheTarget] = []
    try:
        with os.scandir(hex_dir) as entries:
            for entry in entries:
                kind = next((k for pattern, k in patterns if pattern.match(entry.name)), None)
                if kind is None:
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                # Regular files only: a symlink, a directory wearing a `.pbf`
                # name, a fifo or a device node is never a target.
                if not stat.S_ISREG(info.st_mode):
                    continue
                if info.st_mtime >= cutoff_ts:
                    continue
                found.append(
                    CacheTarget(
                        path=Path(entry.path),
                        kind=kind,
                        size_bytes=info.st_size,
                        mtime=datetime.fromtimestamp(info.st_mtime, UTC),
                    )
                )
    except OSError as error:
        return [], _enumeration_failure(hex_dir, error)
    return sorted(found, key=lambda target: str(target.path)), None


def _locks_root_skip(locks_root: Path) -> dict[str, Any] | None:
    """The lane-level skip entry that retires ONLY the lock lane, or `None`.

    Absence is the NORMAL state of a fresh cache root, so it is a skip and never
    a preflight blocker: the tile/intermediate lane must still prune. Unsafety
    is a skip for the opposite reason -- `os.scandir` follows a symlinked
    `.locks`, which would carry the deletion surface outside the cache root.
    """
    try:
        info = os.lstat(locks_root)
    except FileNotFoundError:
        return {"path": str(locks_root), "kind": None, "reason": "locks_root_missing"}
    except OSError as error:
        return {
            "path": str(locks_root),
            "kind": None,
            "reason": "locks_root_unsafe",
            "detail": "path_unavailable",
            "error": str(error),
        }
    if stat.S_ISLNK(info.st_mode):
        return {
            "path": str(locks_root),
            "kind": None,
            "reason": "locks_root_unsafe",
            "detail": "path_is_symlink",
        }
    if not stat.S_ISDIR(info.st_mode):
        return {
            "path": str(locks_root),
            "kind": None,
            "reason": "locks_root_unsafe",
            "detail": "path_not_directory",
        }
    return None


def collect_targets(
    root: Path, *, cutoff: datetime
) -> tuple[list[CacheTarget], list[dict[str, Any]], list[dict[str, Any]]]:
    """`(aged targets, lane-level skips, enumeration failures)`.

    Exactly two levels are ever read: `<root>/<hh>/` and `<root>/.locks/<hh>/`.
    Nothing at the root, nothing one level deeper, and nothing under a directory
    whose name is not `[0-9a-f]{2}` is enumerated -- which is why `precip/` is
    unreachable without naming it.

    A directory that cannot be listed is collected into the third list rather
    than raising: the sibling lanes must still prune, and the caller turns the
    entries into `failed[]` (rc 1).
    """
    targets: list[CacheTarget] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    def collect_lane(parent: Path, patterns: tuple[tuple[re.Pattern[str], str], ...]) -> None:
        hex_dirs, failure = _hex_directories(parent)
        if failure is not None:
            failed.append(failure)
        for hex_dir in hex_dirs:
            lane, lane_failure = _lane_targets(hex_dir, patterns, cutoff)
            if lane_failure is not None:
                failed.append(lane_failure)
            targets.extend(lane)

    collect_lane(root, _TILE_LANE_PATTERNS)

    locks_root = root / LOCKS_DIR_NAME
    lane_skip = _locks_root_skip(locks_root)
    if lane_skip is not None:
        skipped.append(lane_skip)
        return targets, skipped, failed
    collect_lane(locks_root, _LOCK_LANE_PATTERNS)
    return targets, skipped, failed


def _open_lock_fd(path: Path) -> int:
    """A read-only descriptor on the lock file, never creating or following.

    NO `O_CREAT`: a deleter must not be able to recreate what it is removing.
    `O_NOFOLLOW` makes a symlink an `ELOOP` instead of a reach outside the cache
    root, and `O_CLOEXEC` keeps the descriptor out of anything this process
    might exec. `O_NONBLOCK` is inert on a regular file -- the only shape this
    runner ever wants to delete -- and is here for the shape it does NOT want:
    opening a writer-less FIFO read-only BLOCKS FOREVER without it, and the
    systemd unit runs with `TimeoutStartSec=0` while the wrapper's `flock -n`
    would then skip every later tick at rc 0. With it the open returns at once
    and the `fstat` below classifies the FIFO as `not_regular_file`.
    Separated out as a module-level function because it is the seam the
    recreate-race test drives.
    """
    return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)


def _remove_lock_target(target: CacheTarget) -> tuple[str, OSError | None]:
    """Unlink one lock file only while holding it, and only if it is still it.

    Returns `("deleted" | "already_gone" | "lock_held" | "not_regular_file" |
    "failed", error)`.

    Order: open -> `fstat` shape check -> `flock` -> identity recheck -> unlink.
    The shape check runs BEFORE the `flock` on purpose: `flock` on a FIFO or a
    directory descriptor is not what this runner wants to reason about, and
    answering `not_regular_file` from the `fstat` alone makes the classification
    of a path replaced by a non-file deterministic.

    The identity recheck after the `flock` is what makes deleting a LIVE lock
    impossible. The window it closes: we open inode I, the holder finishes and
    unlinks I, a new miss recreates the path as inode J and takes the lock on
    it. Our non-blocking `flock` on the orphan I then SUCCEEDS, and unlinking by
    path at that point would delete J -- a lock somebody is holding right now.
    Comparing `fstat(fd)` with `lstat(path)` turns that into `already_gone`.
    """
    try:
        fd = _open_lock_fd(target.path)
    except FileNotFoundError:
        return "already_gone", None
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.EMLINK}:
            # Replaced by a symlink since collection; `O_NOFOLLOW` refused it.
            return "not_regular_file", None
        return "failed", error
    try:
        held = os.fstat(fd)
        if not stat.S_ISREG(held.st_mode):
            return "not_regular_file", None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "lock_held", None
        except OSError as error:
            return "failed", error
        try:
            current = os.lstat(target.path)
        except FileNotFoundError:
            return "already_gone", None
        except OSError as error:
            return "failed", error
        if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
            return "already_gone", None
        try:
            os.unlink(target.path)
        except FileNotFoundError:
            return "already_gone", None
        except OSError as error:
            return "failed", error
        return "deleted", None
    finally:
        # Closing releases the `flock` too; every branch above goes through here.
        os.close(fd)


def _remove_file_target(target: CacheTarget) -> tuple[str, OSError | None]:
    """Tile bodies and write intermediates are unlinked directly, never opened."""
    try:
        os.unlink(target.path)
    except FileNotFoundError:
        return "already_gone", None
    except OSError as error:
        return "failed", error
    return "deleted", None


def _target_payload(target: CacheTarget) -> dict[str, Any]:
    return {
        "path": str(target.path),
        "kind": target.kind,
        "size_bytes": target.size_bytes,
        "mtime": _rfc3339(target.mtime),
    }


def run_retention(config: MvtCacheRetentionConfig, *, now: datetime) -> dict[str, Any]:
    started_at = now.astimezone(UTC)
    reference_time = (config.reference_time or started_at).astimezone(UTC)
    cutoff = reference_time - timedelta(days=config.retention_days)
    base = {
        "schema_version": SCHEMA_VERSION,
        "started_at": _rfc3339(started_at),
        "reference_time": _rfc3339(reference_time),
        "cache_root": str(config.cache_root),
        "retention_days": config.retention_days,
        "cutoff": _rfc3339(cutoff),
        "enabled": config.enabled,
        "plan_only": config.plan_only,
        # The subtree this runner never enters; `scripts/node27_raw_retention.py`
        # owns it. Named explicitly so an operator can assert the exclusion from
        # the receipt instead of trusting the enumeration.
        "precip_root_untouched": str(config.cache_root / PRECIP_DIR_NAME),
    }
    if not config.enabled:
        return {
            **base,
            "status": "disabled",
            "finished_at": _rfc3339(datetime.now(UTC)),
            "execution_mode": "disabled",
            "counts": {"planned": 0, "deleted": 0, "skipped": 0, "failed": 0},
            "planned": [],
            "deleted": [],
            "skipped": [],
            "failed": [],
            "freed_bytes": 0,
        }

    # `failed` starts NON-EMPTY when a directory could not be enumerated: those
    # entries survive `plan_only` too, because a plan that could not read a
    # directory is as incomplete as an execution that could not.
    targets, skipped, failed = collect_targets(config.cache_root, cutoff=cutoff)
    planned = [_target_payload(target) for target in targets]
    deleted: list[dict[str, Any]] = []
    freed_bytes = 0
    if not config.plan_only:
        for target, payload in zip(targets, planned, strict=True):
            remover = _remove_lock_target if target.kind == KIND_LOCK else _remove_file_target
            outcome, error = remover(target)
            if outcome == "deleted":
                deleted.append(payload)
                freed_bytes += int(payload["size_bytes"])
                continue
            if outcome == "failed":
                failed.append(
                    {
                        "path": payload["path"],
                        "kind": payload["kind"],
                        "error": str(error),
                        "error_type": type(error).__name__,
                    }
                )
                continue
            # `already_gone` / `lock_held` / `not_regular_file` are SKIPS: a
            # concurrent display worker doing its job is not a retention
            # failure, and neither is a lock somebody currently holds.
            skipped.append({"path": payload["path"], "kind": payload["kind"], "reason": outcome})
    finished_at = datetime.now(UTC)
    return {
        **base,
        "status": "completed",
        "finished_at": _rfc3339(finished_at),
        "execution_mode": "plan_only" if config.plan_only else "production_execute",
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
        "freed_bytes": freed_bytes,
    }


def _blocked_payload(blockers: Iterable[dict[str, Any]]) -> dict[str, Any]:
    now = _rfc3339(datetime.now(UTC))
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
        "freed_bytes": 0,
    }


def _write_summary(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    # Deliberately NOT `type=int`: argparse would exit 2 with a usage message
    # and no JSON, while the spec requires a `preflight_blocked` summary on the
    # normal sink for a malformed age.
    parser.add_argument("--retention-days")
    parser.add_argument("--summary-path")
    parser.add_argument("--reference-time")
    return parser


def _emit(payload: dict[str, Any], summary_path: Path | None) -> None:
    """One sink for blocked and completed runs alike."""
    if summary_path is not None:
        _write_summary(summary_path, payload)
    print(json.dumps(payload, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config, blockers = config_from_env(args)
    if config is None:
        # Same sink a completed run would have used, or the operator's health
        # check reads yesterday's receipt and calls a blocked run healthy.
        _emit(_blocked_payload(blockers), _summary_sink(args))
        return 2
    payload = run_retention(config, now=datetime.now(UTC))
    _emit(payload, config.summary_path)
    return 1 if payload["counts"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
