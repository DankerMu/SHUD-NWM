#!/usr/bin/env python3
"""One-shot backfill of the canonical precipitation mirror (#2008, design.md D6).

Mirrors every ``canonical/<storage_source>/<cycle_token>/prcp_rate_or_amount/``
directory found under ``--source-root``, plus each source's
``canonical/<storage_source>/grid/<grid_id>/``, into ``--copyback-root`` under
the identical keyspace, and prints a JSON summary to stdout.

Standard library only -- deliberately. node-22's checkout is frozen ahead of a
maintenance window, so this must run as::

    cd /scratch/frd_muziyao/NWM && \\
        /scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.canonical_precip_copyback_backfill \\
        --source-root <root> --copyback-root <root>

without triggering any environment build. The ``cd`` is mandatory, not decoration:
there is no ``scripts/__init__.py`` (PEP 420), so ``-m`` resolves the module only
because it puts the cwd on ``sys.path``. From anywhere else the interpreter exits
1 with ``ModuleNotFoundError`` -- the same exit code this script uses for
"completed but something failed", distinguishable only by the empty stdout.

It therefore imports nothing from ``services`` / ``packages`` / ``workers`` and
no third-party module. The direct
consequence is that it does **not** normalize source ids: it copies the on-disk
directory names verbatim (``gfs`` / ``IFS`` are already the storage spelling) and
discovers ``<grid_id>`` by listing ``canonical/<S>/grid/*/``. That non-sharing of
the keyspace rule with ``services/tile_publisher/publisher.py`` is a decision of
record (tasks.md 4.1-4.3 invariant matrix), not a duplicated implementation.

Unlike its sibling ``services/tile_publisher/forcing_copyback_backfill.py`` this
script *writes by default*; only ``--dry-run`` suppresses writes, because the
node-22 operation invokes it without any flag.

Exit codes: 0 = completed with no failure, 1 = completed but something failed
(the summary is still printed), 2 = unusable arguments or roots -- which
includes a ``canonical/`` under ``--source-root`` that exists but is unreadable,
not a directory, or a symlink. An *absent* ``canonical/`` is not an error: there
is simply nothing to mirror, and the run exits 0.

Path safety, stated as exactly what is enforced and nothing more:

* the three tree roots this script builds as paths -- ``canonical/``,
  ``canonical/<S>/grid/`` and ``canonical/<S>/<cycle_token>/prcp_rate_or_amount/``
  -- are ``lstat``ed and refused when they are symlinks, rather than followed out
  of ``--source-root``;
* entries *inside* a tree are a stricter rule: a symlinked file or directory
  there is recorded as a failure by ``_mirror_directory``;
* the directory names this script *discovers* by listing -- ``<S>``,
  ``<cycle_token>`` and ``<grid_id>`` -- are filtered with
  ``entry.is_dir(follow_symlinks=False)``, so a symlink there is silently
  SKIPPED, not refused, and never appears in the summary at all;
* path components under ``--copyback-root`` are NOT checked: this script
  ``mkdir -p``s into the destination and a symlinked *component* there is
  followed. The destination root is operator-supplied and anyone able to plant
  such a link already has write access to it, so this is not a privilege
  boundary; it is a gap relative to the publisher, which walks its destination
  with ``O_NOFOLLOW``. The per-file *leaf* is not in that gap: the temp name
  ``.<name>.backfill.<pid>.tmp`` is opened
  ``O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW``, so a symlink planted at it is refused
  and recorded as a failure rather than followed out of the root, and a stale
  temp from a crashed run is refused rather than written through.

Every directory the script creates under ``--copyback-root`` is chmod'ed 0o755
explicitly, and every file it promotes there is chmod'ed 0o644 on the temp name
before the ``os.replace``, because node-22 writes as one account and node-27
reads the same NFS as another and the process umask must not decide that
(``O_CREAT``'s mode argument is masked by the umask exactly as ``mkdir``'s is).
Directories the script did not create and files it did not write -- an
identical-size destination is skipped -- keep the mode they already had.
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


DIR_MODE = 0o755
FILE_MODE = 0o644


class BackfillUsageError(Exception):
    """The arguments or the roots they name cannot be used (exit 2)."""


class SymlinkedDirectoryError(OSError):
    """A directory the script was asked to descend into is a symlink.

    Subclasses ``OSError`` so every caller that already records a listing
    failure records this one too, instead of silently deep-copying whatever
    lives outside ``--source-root``.
    """


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

    A symlinked child is silently omitted, not reported: it is skipped by the
    ``follow_symlinks=False`` filter and therefore never reaches the summary.
    Raises ``FileNotFoundError`` when ``path`` is absent and ``OSError`` when it
    exists but cannot be listed (unreadable, not a directory at all, or a
    symlink -- ``os.scandir`` would happily follow that one).
    """

    _reject_symlinked_directory(path)
    names: list[str] = []
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                names.append(entry.name)
    return sorted(names)


def _reject_symlinked_directory(path: Path) -> None:
    """Fail closed on a tree root that is itself a symlink.

    Applies to the three roots this script builds as paths (``canonical/``,
    ``canonical/<S>/grid/``, ``canonical/<S>/<cycle>/prcp_rate_or_amount/``),
    which would otherwise be followed out of ``--source-root``. It is a
    different rule from the two the rest of the script applies: a symlinked
    entry *inside* a tree is recorded as a failure by ``_mirror_directory``,
    while a symlinked *discovered* directory name (``<S>``, ``<cycle_token>``,
    ``<grid_id>``) is silently skipped by ``_sorted_child_dir_names``'s
    ``is_dir(follow_symlinks=False)`` filter. Raises ``FileNotFoundError`` when
    ``path`` is absent, so callers keep distinguishing "nothing to mirror".
    """

    if stat.S_ISLNK(path.lstat().st_mode):
        raise SymlinkedDirectoryError(f"refusing to mirror a symlinked directory: {path}")


def _ensure_target_directory(directory: Path) -> None:
    """``mkdir -p`` the destination and chmod 0o755 every directory created here.

    ``mkdir(mode=...)`` is masked by the process umask, so the mode has to be
    applied afterwards with an explicit ``chmod``; under ``umask 027`` the plain
    ``mkdir`` leaves 0o750 and node-27's reader account loses the tree.
    Pre-existing directories are left alone -- this script only owns what it
    creates.

    The chain is created one level at a time, outermost first, and each level is
    chmod'ed as soon as *that* level exists. A single ``mkdir(parents=True)``
    followed by a chmod loop would leave the ancestors at the process umask
    forever whenever the leaf fails: ``parents=True`` creates the ancestors
    first, the loop is never reached, and a later run's ``exists()`` probe no
    longer counts them as created.
    """

    missing: list[Path] = []
    probe = directory
    while not probe.exists():
        missing.append(probe)
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    for path in reversed(missing):
        try:
            path.mkdir()
        except FileExistsError:
            # Lost the race to a concurrent creator (or the probe was stale):
            # this run did not create it, so this run does not widen it. Matches
            # `mkdir(exist_ok=True)`, which re-raises when the name is not a
            # directory.
            if not path.is_dir():
                raise
            continue
        os.chmod(path, DIR_MODE)


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
    try:
        _reject_symlinked_directory(source_dir)
    except SymlinkedDirectoryError as error:
        result.failed += 1
        result.errors.append(str(error))
        return result
    except OSError as error:
        result.failed += 1
        result.errors.append(f"failed to stat {source_dir}: {error}")
        return result
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
        _ensure_target_directory(target_file.parent)
        # O_NOFOLLOW: the temp name is predictable, and `copyfile` + `chmod`
        # would follow a symlink planted there straight out of --copyback-root,
        # overwrite whatever it points at and widen its mode, while the summary
        # still counted the file `copied`. O_EXCL refuses a stale temp rather
        # than writing through it; the handler below unlinks it, so a rerun is
        # clean. The mode argument is masked by the umask exactly as `mkdir`'s
        # is, hence the explicit `fchmod`.
        descriptor = os.open(temp_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE)
        with os.fdopen(descriptor, "wb") as temp_stream:
            os.fchmod(descriptor, FILE_MODE)
            with open(source_file, "rb") as source_stream:
                shutil.copyfileobj(source_stream, temp_stream)
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
    try:
        # lstat, not exists(): a dangling symlink here is a refusal (recorded by
        # mirror_tree below), never "this cycle has no precipitation products".
        source_dir.lstat()
    except FileNotFoundError:
        # A canonical cycle directory may legitimately hold other variables only;
        # nothing to mirror is not a failure.
        entry.update({"status": "no_precip_products", "copied": 0, "skipped": 0, "failed": 0, "errors": []})
        return entry
    except OSError as error:
        entry.update(
            {
                "status": "failed",
                "copied": 0,
                "skipped": 0,
                "failed": 1,
                "errors": [f"failed to stat {source_dir}: {error}"],
            }
        )
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
    if summary.get("root_error") is not None:
        # `canonical/` exists but is unreadable, not a directory, or a symlink:
        # an unusable root, not a per-cycle failure -- no cycle or grid entry
        # even exists. Exit 2 (the summary has still been printed). An *absent*
        # `canonical/` is not a root error and stays exit 0.
        return EXIT_USAGE
    return EXIT_FAILURES if int(summary["totals"]["failed"]) else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
