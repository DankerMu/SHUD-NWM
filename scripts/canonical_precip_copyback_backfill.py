#!/usr/bin/env python3
"""One-shot backfill of the canonical precipitation mirror (#2008, design.md D6).

Mirrors every ``canonical/<storage_source>/<cycle_token>/prcp_rate_or_amount/``
directory found under ``--source-root``, plus each source's
``canonical/<storage_source>/grid/<grid_id>/``, into ``--copyback-root`` under
the identical keyspace, and prints a JSON summary to stdout.

Standard library only -- deliberately. node-22's checkout is frozen ahead of a
maintenance window, so this must run as::

    /scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.canonical_precip_copyback_backfill \\
        --source-root <root> --copyback-root <root>

without triggering any environment build. It therefore imports nothing from
``services`` / ``packages`` / ``workers`` and no third-party module. The direct
consequence is that it does **not** normalize source ids: it copies the on-disk
directory names verbatim (``gfs`` / ``IFS`` are already the storage spelling) and
discovers ``<grid_id>`` by listing ``canonical/<S>/grid/*/``. That non-sharing of
the keyspace rule with ``services/tile_publisher/publisher.py`` is a decision of
record (tasks.md 4.1-4.3 invariant matrix), not a duplicated implementation.

Unlike its sibling ``services/tile_publisher/forcing_copyback_backfill.py`` this
script *writes by default*; only ``--dry-run`` suppresses writes, because the
node-22 operation invokes it without any flag.

Exit codes: 0 = completed with no failure, 1 = completed but something failed
(the summary is still printed), 2 = unusable arguments or roots.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_USAGE = 2

CANONICAL_DIR = "canonical"
GRID_DIR = "grid"
PRCP_DIR = "prcp_rate_or_amount"
CYCLE_TOKEN_LENGTH = 10


class BackfillUsageError(Exception):
    """The arguments or the roots they name cannot be used (exit 2)."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="canonical_precip_copyback_backfill",
        description="Mirror canonical precipitation products into the object-store copyback root.",
    )
    parser.add_argument("--source-root", required=True, help="Production object-store root (holds canonical/).")
    parser.add_argument("--copyback-root", required=True, help="Shared object-store copyback root to mirror into.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the planned copies without creating any file or directory under --copyback-root.",
    )
    return parser


def resolve_roots(source_root_arg: str, copyback_root_arg: str) -> tuple[Path, Path]:
    # An empty value (e.g. an unset NHMS_OBJECT_STORE_COPYBACK_ROOT expanded by
    # the shell) must not become `.` and silently mirror into the cwd.
    for label, raw in (("--source-root", source_root_arg), ("--copyback-root", copyback_root_arg)):
        if not raw.strip():
            raise BackfillUsageError(f"{label} must not be empty")
    source_root = Path(source_root_arg).expanduser()
    copyback_root = Path(copyback_root_arg).expanduser()
    for label, root in (("--source-root", source_root), ("--copyback-root", copyback_root)):
        if not root.is_dir():
            raise BackfillUsageError(f"{label} is not an existing directory: {root}")
    source_root = source_root.resolve()
    copyback_root = copyback_root.resolve()
    if source_root == copyback_root:
        raise BackfillUsageError(f"--source-root and --copyback-root are the same directory: {source_root}")
    if _is_relative_to(source_root, copyback_root) or _is_relative_to(copyback_root, source_root):
        raise BackfillUsageError(f"--source-root and --copyback-root must not overlap: {source_root} / {copyback_root}")
    return source_root, copyback_root


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _sorted_child_dir_names(path: Path) -> list[str]:
    """Names of the real (non-symlink) subdirectories of ``path``, sorted.

    Raises ``FileNotFoundError`` when ``path`` is absent and ``OSError`` when it
    exists but cannot be listed (unreadable, or not a directory at all).
    """

    names: list[str] = []
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                names.append(entry.name)
    return sorted(names)


def _is_cycle_token(name: str) -> bool:
    return len(name) == CYCLE_TOKEN_LENGTH and name.isdigit()


class _TreeResult:
    def __init__(self) -> None:
        self.copied = 0
        self.skipped = 0
        self.failed = 0
        self.errors: list[str] = []

    def as_dict(self) -> dict[str, Any]:
        return {"copied": self.copied, "skipped": self.skipped, "failed": self.failed, "errors": list(self.errors)}


def mirror_tree(source_dir: Path, target_dir: Path, *, dry_run: bool) -> _TreeResult:
    """Mirror one directory tree file by file; identical-size destinations are skipped."""

    result = _TreeResult()
    _mirror_directory(source_dir, target_dir, dry_run=dry_run, result=result)
    return result


def _mirror_directory(source_dir: Path, target_dir: Path, *, dry_run: bool, result: _TreeResult) -> None:
    try:
        with os.scandir(source_dir) as scan:
            entries = sorted(scan, key=lambda entry: entry.name)
    except OSError as error:
        result.failed += 1
        result.errors.append(f"failed to list {source_dir}: {error}")
        return

    for entry in entries:
        source_entry = Path(entry.path)
        target_entry = target_dir / entry.name
        try:
            entry_stat = entry.stat(follow_symlinks=False)
        except OSError as error:
            result.failed += 1
            result.errors.append(f"failed to stat {source_entry}: {error}")
            continue
        if stat.S_ISLNK(entry_stat.st_mode):
            result.failed += 1
            result.errors.append(f"refusing to mirror symlink: {source_entry}")
            continue
        if stat.S_ISDIR(entry_stat.st_mode):
            _mirror_directory(source_entry, target_entry, dry_run=dry_run, result=result)
            continue
        if not stat.S_ISREG(entry_stat.st_mode):
            result.failed += 1
            result.errors.append(f"not a regular file: {source_entry}")
            continue
        _mirror_file(source_entry, target_entry, source_size=entry_stat.st_size, dry_run=dry_run, result=result)


def _mirror_file(
    source_file: Path,
    target_file: Path,
    *,
    source_size: int,
    dry_run: bool,
    result: _TreeResult,
) -> None:
    try:
        target_stat: os.stat_result | None = target_file.lstat()
    except FileNotFoundError:
        target_stat = None
    except OSError as error:
        result.failed += 1
        result.errors.append(f"failed to stat {target_file}: {error}")
        return

    if target_stat is not None and stat.S_ISREG(target_stat.st_mode) and target_stat.st_size == source_size:
        result.skipped += 1
        return
    if dry_run:
        # --dry-run creates neither a file nor a directory under the copyback root.
        result.copied += 1
        return

    temp_file = target_file.parent / f".{target_file.name}.backfill.{os.getpid()}.tmp"
    try:
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_file, temp_file)
        os.chmod(temp_file, 0o644)
        os.replace(temp_file, target_file)
    except OSError as error:
        try:
            os.unlink(temp_file)
        except OSError:
            pass
        result.failed += 1
        result.errors.append(f"failed to copy {source_file} -> {target_file}: {error}")
        return
    result.copied += 1


def backfill(source_root: Path, copyback_root: Path, *, dry_run: bool) -> dict[str, Any]:
    cycles: list[dict[str, Any]] = []
    grids: list[dict[str, Any]] = []
    canonical_root = source_root / CANONICAL_DIR

    try:
        storage_sources = _sorted_child_dir_names(canonical_root)
    except FileNotFoundError:
        storage_sources = []
    except OSError as error:
        return _summary(
            source_root,
            copyback_root,
            dry_run=dry_run,
            cycles=[],
            grids=[],
            root_error=f"failed to list {canonical_root}: {error}",
        )

    for storage_source in storage_sources:
        source_dir = canonical_root / storage_source
        grids.extend(_backfill_grids(source_root, copyback_root, storage_source, dry_run=dry_run))
        try:
            child_names = _sorted_child_dir_names(source_dir)
        except OSError as error:
            cycles.append(
                {
                    "source": storage_source,
                    "cycle_token": None,
                    "status": "failed",
                    "copied": 0,
                    "skipped": 0,
                    "failed": 1,
                    "errors": [f"failed to list {source_dir}: {error}"],
                }
            )
            continue
        for cycle_token in child_names:
            if not _is_cycle_token(cycle_token):
                continue
            cycles.append(
                _backfill_cycle(source_root, copyback_root, storage_source, cycle_token, dry_run=dry_run)
            )

    return _summary(source_root, copyback_root, dry_run=dry_run, cycles=cycles, grids=grids, root_error=None)


def _backfill_cycle(
    source_root: Path,
    copyback_root: Path,
    storage_source: str,
    cycle_token: str,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    relative = Path(CANONICAL_DIR) / storage_source / cycle_token / PRCP_DIR
    source_dir = source_root / relative
    entry: dict[str, Any] = {"source": storage_source, "cycle_token": cycle_token}
    if not source_dir.exists():
        # A canonical cycle directory may legitimately hold other variables only;
        # nothing to mirror is not a failure.
        entry.update({"status": "no_precip_products", "copied": 0, "skipped": 0, "failed": 0, "errors": []})
        return entry
    result = mirror_tree(source_dir, copyback_root / relative, dry_run=dry_run)
    entry.update({"status": "failed" if result.failed else "ok", **result.as_dict()})
    return entry


def _backfill_grids(
    source_root: Path,
    copyback_root: Path,
    storage_source: str,
    *,
    dry_run: bool,
) -> list[dict[str, Any]]:
    grid_root = source_root / CANONICAL_DIR / storage_source / GRID_DIR
    try:
        grid_ids = _sorted_child_dir_names(grid_root)
    except FileNotFoundError:
        return []
    except OSError as error:
        return [
            {
                "source": storage_source,
                "grid_id": None,
                "status": "failed",
                "copied": 0,
                "skipped": 0,
                "failed": 1,
                "errors": [f"failed to list {grid_root}: {error}"],
            }
        ]
    entries: list[dict[str, Any]] = []
    for grid_id in grid_ids:
        relative = Path(CANONICAL_DIR) / storage_source / GRID_DIR / grid_id
        result = mirror_tree(source_root / relative, copyback_root / relative, dry_run=dry_run)
        entries.append(
            {
                "source": storage_source,
                "grid_id": grid_id,
                "status": "failed" if result.failed else "ok",
                **result.as_dict(),
            }
        )
    return entries


def _summary(
    source_root: Path,
    copyback_root: Path,
    *,
    dry_run: bool,
    cycles: list[dict[str, Any]],
    grids: list[dict[str, Any]],
    root_error: str | None,
) -> dict[str, Any]:
    totals = {"copied": 0, "skipped": 0, "failed": 0}
    for entry in (*cycles, *grids):
        for field in ("copied", "skipped", "failed"):
            totals[field] += int(entry[field])
    if root_error is not None:
        totals["failed"] += 1
    summary: dict[str, Any] = {
        "source_root": str(source_root),
        "copyback_root": str(copyback_root),
        "dry_run": dry_run,
        "cycles": cycles,
        "grids": grids,
        "totals": totals,
    }
    if root_error is not None:
        summary["root_error"] = root_error
    return summary


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source_root, copyback_root = resolve_roots(args.source_root, args.copyback_root)
    except BackfillUsageError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_USAGE

    summary = backfill(source_root, copyback_root, dry_run=bool(args.dry_run))
    # stdout carries the JSON summary and nothing else.
    print(json.dumps(summary, indent=2, sort_keys=True))
    return EXIT_FAILURES if int(summary["totals"]["failed"]) else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
