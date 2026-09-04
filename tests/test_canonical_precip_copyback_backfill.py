"""Requirement-driven tests for ``scripts/canonical_precip_copyback_backfill.py`` (#2008).

Contract (canonical-precip-copyback spec, Requirement 2):

* mirrors every ``canonical/<storage_source>/<cycle_token>/prcp_rate_or_amount/``
  plus each source's ``canonical/<storage_source>/grid/*/`` under ``--source-root``
  into ``--copyback-root`` on the identical keyspace, copying the on-disk
  directory names verbatim (no source-id normalization -- the script must not
  import the publisher's ``normalize_source_id``);
* prints a JSON summary to stdout listing every cycle with copied/skipped/failed;
* writes by default, ``--dry-run`` creates no file and no directory;
* exit 0 with no failure, 1 when something failed, 2 for unusable roots;
* imports the standard library only, so node-22's frozen checkout can run it as
  ``<pinned python> -m scripts.canonical_precip_copyback_backfill`` without
  triggering an environment build;
* refuses a symlinked *tree root* (``canonical/``, ``canonical/<S>/grid/``,
  ``canonical/<S>/<cycle>/prcp_rate_or_amount/``) instead of deep-copying
  whatever lives outside ``--source-root``. That is one of three distinct rules:
  entries *inside* a tree are refused per entry, while the directory names the
  script *discovers* by listing (``<S>``, ``<cycle_token>``, ``<grid_id>``) are
  silently skipped by ``is_dir(follow_symlinks=False)``. Path *components* under
  ``--copyback-root`` are not checked at all, but the per-file leaf is: the temp
  name is opened ``O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW``, so a symlink or a stale
  temp planted there is refused and recorded rather than written through;
* leaves every directory it creates under ``--copyback-root`` group/world
  readable, and every file it promotes there ``0o644``, regardless of the
  process umask (node-22 writes as one account, node-27 reads the same NFS as
  another) -- while leaving directories it did not create and files it did not
  write at the mode they already had.
"""

from __future__ import annotations

import ast
import errno
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import canonical_precip_copyback_backfill as backfill

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "canonical_precip_copyback_backfill.py"
MODULE_NAME = "scripts.canonical_precip_copyback_backfill"


def _seed_cycle(
    source_root: Path,
    storage_source: str,
    cycle_token: str,
    *,
    leads: tuple[int, ...] = (3, 6, 9),
) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    prcp_dir = source_root / "canonical" / storage_source / cycle_token / "prcp_rate_or_amount"
    prcp_dir.mkdir(parents=True, exist_ok=True)
    for lead in leads:
        name = f"{storage_source}_{cycle_token}_prcp_rate_or_amount_f{lead:03d}.nc"
        payload = f"prcp:{storage_source}:{cycle_token}:{lead:03d}".encode("utf-8")
        (prcp_dir / name).write_bytes(payload)
        payloads[f"canonical/{storage_source}/{cycle_token}/prcp_rate_or_amount/{name}"] = payload
    return payloads


def _seed_grid(source_root: Path, storage_source: str, grid_id: str) -> dict[str, bytes]:
    grid_dir = source_root / "canonical" / storage_source / "grid" / grid_id
    grid_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"grid_id": grid_id}).encode("utf-8")
    (grid_dir / "grid.json").write_bytes(payload)
    return {f"canonical/{storage_source}/grid/{grid_id}/grid.json": payload}


def _seed_two_source_store(tmp_path: Path) -> tuple[Path, Path, dict[str, bytes]]:
    source_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    source_root.mkdir()
    copyback_root.mkdir()
    payloads: dict[str, bytes] = {}
    for storage_source, grid_id in (("gfs", "gfs_0p25"), ("IFS", "ifs_0p25")):
        for cycle_token in ("2026090200", "2026090212"):
            payloads.update(_seed_cycle(source_root, storage_source, cycle_token))
        payloads.update(_seed_grid(source_root, storage_source, grid_id))
    return source_root, copyback_root, payloads


def test_backfill_mirrors_every_cycle_and_grid_and_exits_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, copyback_root, payloads = _seed_two_source_store(tmp_path)

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    for key, payload in payloads.items():
        assert (copyback_root / key).read_bytes() == payload
    assert [(cycle["source"], cycle["cycle_token"], cycle["copied"], cycle["skipped"], cycle["failed"])
            for cycle in summary["cycles"]] == [
        ("IFS", "2026090200", 3, 0, 0),
        ("IFS", "2026090212", 3, 0, 0),
        ("gfs", "2026090200", 3, 0, 0),
        ("gfs", "2026090212", 3, 0, 0),
    ]
    assert [(grid["source"], grid["grid_id"], grid["copied"], grid["skipped"], grid["failed"])
            for grid in summary["grids"]] == [
        ("IFS", "ifs_0p25", 1, 0, 0),
        ("gfs", "gfs_0p25", 1, 0, 0),
    ]
    assert summary["totals"] == {"copied": 14, "skipped": 0, "failed": 0}
    # The script copies directory names verbatim; it never normalizes a source id.
    assert sorted(entry.name for entry in (copyback_root / "canonical").iterdir()) == ["IFS", "gfs"]


def test_backfill_reruns_skip_identically_sized_destinations(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, copyback_root, _payloads = _seed_two_source_store(tmp_path)
    assert backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)]) == 0
    capsys.readouterr()

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert summary["totals"] == {"copied": 0, "skipped": 14, "failed": 0}


def test_backfill_replaces_a_destination_of_different_size(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, copyback_root, payloads = _seed_two_source_store(tmp_path)
    assert backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)]) == 0
    capsys.readouterr()
    stale_key = "canonical/gfs/2026090212/prcp_rate_or_amount/gfs_2026090212_prcp_rate_or_amount_f003.nc"
    (copyback_root / stale_key).write_bytes(b"short")

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert summary["totals"] == {"copied": 1, "skipped": 13, "failed": 0}
    assert (copyback_root / stale_key).read_bytes() == payloads[stale_key]


def test_backfill_dry_run_writes_nothing_and_reports_planned_copies(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, copyback_root, _payloads = _seed_two_source_store(tmp_path)

    exit_code = backfill.main(
        ["--source-root", str(source_root), "--copyback-root", str(copyback_root), "--dry-run"]
    )
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert summary["dry_run"] is True
    assert summary["totals"] == {"copied": 14, "skipped": 0, "failed": 0}
    # No file AND no directory was created under the copyback root.
    assert list(copyback_root.rglob("*")) == []


def test_backfill_unreadable_cycle_is_reported_and_exits_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, copyback_root, _payloads = _seed_two_source_store(tmp_path)
    broken = source_root / "canonical" / "gfs" / "2026090300"
    broken.mkdir()
    # A regular file where the product directory belongs: os.scandir raises
    # NotADirectoryError deterministically, unlike a chmod that a root-owned CI
    # runner would sail straight through.
    (broken / "prcp_rate_or_amount").write_bytes(b"not a directory")

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    failed = [cycle for cycle in summary["cycles"] if cycle["failed"]]
    assert len(failed) == 1
    assert failed[0]["source"] == "gfs"
    assert failed[0]["cycle_token"] == "2026090300"
    assert failed[0]["status"] == "failed"
    assert failed[0]["errors"]
    # The healthy cycles were still mirrored and still reported.
    assert summary["totals"]["copied"] == 14
    assert len(summary["cycles"]) == 5


def test_backfill_cycle_without_precipitation_products_is_not_a_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, copyback_root, _payloads = _seed_two_source_store(tmp_path)
    (source_root / "canonical" / "gfs" / "2026090300" / "t2m").mkdir(parents=True)

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    entry = next(cycle for cycle in summary["cycles"] if cycle["cycle_token"] == "2026090300")
    assert entry["status"] == "no_precip_products"
    assert (entry["copied"], entry["skipped"], entry["failed"]) == (0, 0, 0)


def test_backfill_refuses_to_mirror_a_symlinked_entry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, copyback_root, _payloads = _seed_two_source_store(tmp_path)
    prcp_dir = source_root / "canonical" / "gfs" / "2026090212" / "prcp_rate_or_amount"
    (prcp_dir / "linked.nc").symlink_to(prcp_dir / "gfs_2026090212_prcp_rate_or_amount_f003.nc")

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    entry = next(
        cycle for cycle in summary["cycles"] if cycle["source"] == "gfs" and cycle["cycle_token"] == "2026090212"
    )
    assert entry["failed"] == 1
    assert any("symlink" in message for message in entry["errors"])
    assert not (copyback_root / "canonical/gfs/2026090212/prcp_rate_or_amount/linked.nc").exists()


# --------------------------------------------------------------------------- #
# Symlinked tree ROOTS. The per-entry rule above only covers entries *inside* a
# tree: `canonical/`, `canonical/<S>/grid/` and
# `canonical/<S>/<cycle>/prcp_rate_or_amount/` are built as paths and would be
# followed straight out of --source-root into the shared copyback root.
# --------------------------------------------------------------------------- #
OUTSIDE_MARKER = b"outside-the-source-root"


def _seed_outside_tree(tmp_path: Path, *, relative: str, files: tuple[str, ...]) -> Path:
    """A payload directory that lives outside --source-root entirely."""

    outside = tmp_path / "outside" / relative
    outside.mkdir(parents=True)
    for name in files:
        (outside / name).write_bytes(OUTSIDE_MARKER + name.encode("utf-8"))
    return outside


def _assert_nothing_copied_from_outside(copyback_root: Path) -> None:
    leaked = [
        str(path)
        for path in copyback_root.rglob("*")
        if path.is_file() and OUTSIDE_MARKER in path.read_bytes()
    ]
    assert leaked == []


def test_backfill_refuses_a_symlinked_canonical_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    source_root.mkdir()
    copyback_root.mkdir()
    outside = _seed_outside_tree(
        tmp_path,
        relative="gfs/2026090212/prcp_rate_or_amount",
        files=("gfs_2026090212_prcp_rate_or_amount_f003.nc",),
    )
    (source_root / "canonical").symlink_to(outside.parents[2])

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    summary = json.loads(capsys.readouterr().out)

    # An unusable `canonical/` is a root problem, not a per-cycle failure.
    assert exit_code == 2
    assert summary["totals"]["failed"] > 0
    assert "refusing to mirror a symlinked directory" in summary["root_error"]
    _assert_nothing_copied_from_outside(copyback_root)


def test_backfill_refuses_a_symlinked_grid_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, copyback_root, _payloads = _seed_two_source_store(tmp_path)
    outside = _seed_outside_tree(tmp_path, relative="grid/gfs_0p25", files=("grid.json",))
    grid_root = source_root / "canonical" / "gfs" / "grid"
    shutil.rmtree(grid_root)
    grid_root.symlink_to(outside.parent)

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert summary["totals"]["failed"] > 0
    entry = next(grid for grid in summary["grids"] if grid["source"] == "gfs")
    assert entry["status"] == "failed"
    assert any("refusing to mirror a symlinked directory" in message for message in entry["errors"])
    _assert_nothing_copied_from_outside(copyback_root)


def test_backfill_refuses_a_symlinked_precipitation_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, copyback_root, _payloads = _seed_two_source_store(tmp_path)
    outside = _seed_outside_tree(
        tmp_path,
        relative="prcp_rate_or_amount",
        files=("gfs_2026090212_prcp_rate_or_amount_f003.nc",),
    )
    prcp_root = source_root / "canonical" / "gfs" / "2026090212" / "prcp_rate_or_amount"
    shutil.rmtree(prcp_root)
    prcp_root.symlink_to(outside)

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert summary["totals"]["failed"] > 0
    entry = next(
        cycle for cycle in summary["cycles"] if cycle["source"] == "gfs" and cycle["cycle_token"] == "2026090212"
    )
    assert entry["status"] == "failed"
    assert any("refusing to mirror a symlinked directory" in message for message in entry["errors"])
    _assert_nothing_copied_from_outside(copyback_root)


def test_backfill_dangling_symlinked_precipitation_root_is_a_refusal_not_absence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A dangling symlink must not read as "this cycle has no precipitation".

    The presence probe is `lstat()`, not `exists()`: `exists()` follows the link,
    finds nothing, and reports `no_precip_products` with exit 0 -- an operator
    signal that the cycle is legitimately variable-only, when in fact a symlink
    was planted where the products belong.
    """

    source_root, copyback_root, _payloads = _seed_two_source_store(tmp_path)
    prcp_root = source_root / "canonical" / "gfs" / "2026090212" / "prcp_rate_or_amount"
    shutil.rmtree(prcp_root)
    prcp_root.symlink_to(tmp_path / "outside" / "never-created")
    assert not prcp_root.exists() and prcp_root.is_symlink()

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    entry = next(
        cycle for cycle in summary["cycles"] if cycle["source"] == "gfs" and cycle["cycle_token"] == "2026090212"
    )
    assert entry["status"] == "failed"
    assert entry["status"] != "no_precip_products"
    assert entry["failed"] == 1
    assert any("refusing to mirror a symlinked directory" in message for message in entry["errors"])
    assert not (copyback_root / "canonical/gfs/2026090212/prcp_rate_or_amount").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root sails straight through a chmod-000 directory")
def test_backfill_unreadable_cycle_directory_is_recorded_not_raised(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The `lstat` probe's non-FileNotFoundError branch: recorded, never propagated."""

    source_root, copyback_root, _payloads = _seed_two_source_store(tmp_path)
    cycle_dir = source_root / "canonical" / "gfs" / "2026090300"
    (cycle_dir / "prcp_rate_or_amount").mkdir(parents=True)
    os.chmod(cycle_dir, 0o000)
    try:
        exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
        summary = json.loads(capsys.readouterr().out)
    finally:
        os.chmod(cycle_dir, 0o755)

    assert exit_code == 1
    entry = next(cycle for cycle in summary["cycles"] if cycle["cycle_token"] == "2026090300")
    assert entry["status"] == "failed"
    assert entry["failed"] == 1
    assert any("failed to stat" in message for message in entry["errors"])
    # The healthy cycles were still mirrored.
    assert summary["totals"]["copied"] == 14


def test_backfill_created_directories_stay_readable_under_a_restrictive_umask(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """node-22 writes as one account; node-27 reads the same NFS as another."""

    source_root, copyback_root, _payloads = _seed_two_source_store(tmp_path)

    previous_umask = os.umask(0o077)
    try:
        exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    finally:
        os.umask(previous_umask)
    capsys.readouterr()

    assert exit_code == 0
    created_dirs = [path for path in copyback_root.rglob("*") if path.is_dir()]
    # Intermediates (canonical/, canonical/<S>/, canonical/<S>/<cycle>/) too, not
    # just the leaves: mkdir(parents=True) creates them all at the umask.
    assert len(created_dirs) >= 10
    unreadable = [
        str(path) for path in created_dirs if stat.S_IMODE(path.stat().st_mode) & 0o055 != 0o055
    ]
    assert unreadable == []


def test_backfill_partially_created_directory_chain_stays_readable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leaf mkdir that fails must not leave its ancestors at the process umask.

    `mkdir(parents=True)` creates the ancestors first and the leaf last, so a
    trailing chmod loop is never reached when the leaf fails -- and a later clean
    rerun's `exists()` probe no longer counts those ancestors as newly created,
    so they stay unreadable forever. Each level must therefore be chmod'ed as
    soon as that level itself exists.
    """

    source_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    source_root.mkdir()
    copyback_root.mkdir()
    _seed_cycle(source_root, "gfs", "2026090212", leads=(3,))
    leaf = copyback_root / "canonical" / "gfs" / "2026090212" / "prcp_rate_or_amount"
    ancestors = [leaf.parent.parent.parent, leaf.parent.parent, leaf.parent]
    real_mkdir = os.mkdir

    def mkdir_failing_on_the_leaf(path: Any, mode: int = 0o777, *args: Any, **kwargs: Any) -> None:
        # Only once the parents exist, so pathlib's parents=True recursion has
        # already created the whole ancestor chain -- exactly the state the
        # trailing-chmod implementation leaves behind.
        if os.fspath(path) == str(leaf) and leaf.parent.is_dir():
            raise OSError(errno.ENOSPC, "No space left on device")
        real_mkdir(path, mode, *args, **kwargs)

    def assert_ancestors_readable(label: str) -> None:
        unreadable = [
            f"{label}: {path} {stat.S_IMODE(path.stat().st_mode):04o}"
            for path in ancestors
            if stat.S_IMODE(path.stat().st_mode) & 0o055 != 0o055
        ]
        assert unreadable == []

    previous_umask = os.umask(0o077)
    try:
        monkeypatch.setattr(os, "mkdir", mkdir_failing_on_the_leaf)
        first_exit = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
        first_summary = json.loads(capsys.readouterr().out)
        monkeypatch.undo()
        # The violation is complete after run 1: a rerun repairing it would not
        # make run 1's state acceptable, and it does not repair it anyway.
        assert first_exit == 1
        assert first_summary["totals"]["failed"] == 1
        assert all(path.is_dir() for path in ancestors)
        assert_ancestors_readable("after the failed run")

        second_exit = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
        second_summary = json.loads(capsys.readouterr().out)
    finally:
        os.umask(previous_umask)

    assert second_exit == 0
    assert second_summary["totals"] == {"copied": 1, "skipped": 0, "failed": 0}
    assert_ancestors_readable("after the clean rerun")


def test_backfill_refuses_a_symlink_planted_at_the_per_file_temp_name(tmp_path: Path) -> None:
    """The per-file temp name is opened `O_NOFOLLOW`, so a link there is refused.

    Destination-side path *components* are followed by design (the script only
    `mkdir -p`s into an operator-supplied root). The leaf write is a different
    matter: `copyfile` + `chmod` would follow a link planted at the predictable
    temp name, overwrite an arbitrary file outside `--copyback-root` with the
    source bytes, widen its mode to 0o644, rename the link itself into the
    mirror -- and still count the file `copied` with exit 0.

    `backfill()` is called in-process because the temp name embeds `os.getpid()`.
    """

    source_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    source_root.mkdir()
    copyback_root.mkdir()
    _seed_cycle(source_root, "gfs", "2026090212", leads=(3,))
    victim = tmp_path / "outside" / "victim.txt"
    victim.parent.mkdir()
    victim.write_bytes(b"ORIGINAL-VICTIM-CONTENT")
    os.chmod(victim, 0o600)
    target_name = "gfs_2026090212_prcp_rate_or_amount_f003.nc"
    target_dir = copyback_root / "canonical" / "gfs" / "2026090212" / "prcp_rate_or_amount"
    target_dir.mkdir(parents=True)
    planted = target_dir / f".{target_name}.backfill.{os.getpid()}.tmp"
    planted.symlink_to(victim)

    summary = backfill.backfill(source_root.resolve(), copyback_root.resolve(), dry_run=False)

    entry = next(cycle for cycle in summary["cycles"] if cycle["cycle_token"] == "2026090212")
    assert entry["status"] == "failed"
    assert entry["copied"] == 0
    assert entry["failed"] == 1
    assert summary["totals"]["failed"] == 1
    # The file outside the copyback root is untouched: bytes and mode both.
    assert victim.read_bytes() == b"ORIGINAL-VICTIM-CONTENT"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o600
    # And no escaping link was renamed into the mirror.
    assert not (target_dir / target_name).exists()
    assert not (target_dir / target_name).is_symlink()


def test_backfill_refuses_a_stale_regular_temp_file_then_recovers_on_a_rerun(tmp_path: Path) -> None:
    """`O_EXCL` refuses a stale temp instead of writing through it.

    A temp file left by a crashed run carries no guarantee about what it is; the
    run that meets it records a failure and unlinks it, so the next clean run
    mirrors the file normally.
    """

    source_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    source_root.mkdir()
    copyback_root.mkdir()
    payloads = _seed_cycle(source_root, "gfs", "2026090212", leads=(3,))
    target_name = "gfs_2026090212_prcp_rate_or_amount_f003.nc"
    target_dir = copyback_root / "canonical" / "gfs" / "2026090212" / "prcp_rate_or_amount"
    target_dir.mkdir(parents=True)
    stale_temp = target_dir / f".{target_name}.backfill.{os.getpid()}.tmp"
    stale_temp.write_bytes(b"leftover from a crashed run")

    first = backfill.backfill(source_root.resolve(), copyback_root.resolve(), dry_run=False)

    entry = next(cycle for cycle in first["cycles"] if cycle["cycle_token"] == "2026090212")
    assert entry["status"] == "failed"
    assert entry["failed"] == 1
    assert not (target_dir / target_name).exists()
    assert not stale_temp.exists()

    second = backfill.backfill(source_root.resolve(), copyback_root.resolve(), dry_run=False)

    assert second["totals"] == {"copied": 1, "skipped": 0, "failed": 0}
    key = f"canonical/gfs/2026090212/prcp_rate_or_amount/{target_name}"
    assert (copyback_root / key).read_bytes() == payloads[key]

@pytest.mark.parametrize(
    ("level", "link_at", "outside_relative", "summary_key", "summary_field", "planted_name"),
    [
        (
            "storage_source",
            "canonical/evil_source",
            "evil_source_payload/grid/gfs_0p25",
            "grids",
            "source",
            "evil_source",
        ),
        (
            "cycle_token",
            "canonical/gfs/2026090300",
            "evil_cycle_payload/prcp_rate_or_amount",
            "cycles",
            "cycle_token",
            "2026090300",
        ),
        (
            "grid_id",
            "canonical/gfs/grid/evil_grid",
            "evil_grid_payload",
            "grids",
            "grid_id",
            "evil_grid",
        ),
    ],
)
def test_backfill_silently_skips_a_symlinked_discovered_directory_name(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    level: str,
    link_at: str,
    outside_relative: str,
    summary_key: str,
    summary_field: str,
    planted_name: str,
) -> None:
    """Rule (c): `<S>`, `<cycle_token>` and `<grid_id>` are `is_dir(follow_symlinks=False)`.

    These three names are *discovered* by listing, not built as paths, so the
    tree-root `lstat` refusal never sees them. The filter is the only
    containment boundary at this level: following one of these links deep-copies
    whatever lives outside `--source-root` into the shared copyback root, with
    `failed: 0` and exit 0. A skipped name never appears in the summary at all,
    which is the observable that distinguishes "skipped" from "refused".
    """

    source_root, copyback_root, _payloads = _seed_two_source_store(tmp_path)
    outside = _seed_outside_tree(
        tmp_path,
        relative=outside_relative,
        files=("grid.json",) if level == "grid_id" else ("gfs_2026090300_prcp_rate_or_amount_f003.nc",),
    )
    link_target = outside if level != "storage_source" else outside.parents[1]
    if level == "cycle_token":
        link_target = outside.parent
    link = source_root / link_at
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(link_target)

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    summary = json.loads(capsys.readouterr().out)

    # Silently skipped: not refused, not reported, not counted.
    assert [entry for entry in summary[summary_key] if entry[summary_field] == planted_name] == []
    assert summary["totals"]["failed"] == 0
    assert exit_code == 0
    _assert_nothing_copied_from_outside(copyback_root)


def test_backfill_dangling_destination_component_is_recorded_not_widened(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`mkdir` losing to a name that already exists but is not a directory.

    The destination probe is `exists()`, which follows a dangling symlink and
    reports False, so the component is queued as missing and its `mkdir` then
    raises `FileExistsError` on the link name -- in a single-process run with no
    concurrent writer at all. `mkdir(exist_ok=True)` semantics apply: a name
    that is not a directory is re-raised, so the copy is recorded as a failure
    instead of being written through the link.
    """

    source_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    source_root.mkdir()
    copyback_root.mkdir()
    _seed_cycle(source_root, "gfs", "2026090212", leads=(3,))
    (copyback_root / "canonical" / "gfs").mkdir(parents=True)
    dangling = copyback_root / "canonical" / "gfs" / "2026090212"
    dangling.symlink_to(tmp_path / "never-created")
    assert dangling.is_symlink() and not dangling.exists()

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    entry = next(cycle for cycle in summary["cycles"] if cycle["cycle_token"] == "2026090212")
    assert entry["status"] == "failed"
    assert entry["failed"] == 1
    assert entry["copied"] == 0
    assert entry["errors"]
    # The planted name is left exactly as it was: not replaced, not written through.
    assert dangling.is_symlink() and not dangling.exists()


def test_backfill_does_not_widen_a_directory_a_concurrent_writer_created(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Directories the script did not create are left alone -- including on a lost race.

    The `exists()` probe and the `mkdir` are two syscalls; a second writer (a
    concurrent operator invocation -- the CLI takes no lock) can create a
    component in between. The probe is simulated stale for one existing
    ancestor at 0o700: this run did not create it, so this run must not chmod it
    to 0o755 on the shared NFS root.
    """

    source_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    source_root.mkdir()
    copyback_root.mkdir()
    payloads = _seed_cycle(source_root, "gfs", "2026090212", leads=(3,))
    foreign = copyback_root / "canonical" / "gfs"
    foreign.mkdir(parents=True)
    os.chmod(foreign, 0o700)
    resolved_foreign = os.fspath(foreign.resolve())
    real_exists = Path.exists

    def exists_stale_for_the_foreign_ancestor(self: Path, *args: Any, **kwargs: Any) -> bool:
        if os.fspath(self) == resolved_foreign:
            return False
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", exists_stale_for_the_foreign_ancestor)
    try:
        exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    finally:
        monkeypatch.undo()
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert summary["totals"] == {"copied": 1, "skipped": 0, "failed": 0}
    assert stat.S_IMODE(foreign.stat().st_mode) == 0o700
    # The levels this run *did* create are still chmod'ed, so the skip is
    # scoped to the foreign directory rather than disabling the rule.
    for created in (foreign / "2026090212", foreign / "2026090212" / "prcp_rate_or_amount"):
        assert stat.S_IMODE(created.stat().st_mode) & 0o055 == 0o055
    for key, payload in payloads.items():
        assert (copyback_root / key).read_bytes() == payload


def test_backfill_promoted_files_stay_readable_under_a_restrictive_umask(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The file half of the permission rule: node-27 must be able to open them.

    `copyfile`/`os.open` create the temp at `0o666 & ~umask`, so under the
    scenario's own `umask 0o077` a promoted product lands at 0o600 and node-27's
    reader account traverses group/world-readable directories to unreadable
    files. Only files this run wrote are touched: an identical-size destination
    is skipped and keeps whatever mode it already had.
    """

    source_root, copyback_root, payloads = _seed_two_source_store(tmp_path)

    previous_umask = os.umask(0o077)
    try:
        exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
        capsys.readouterr()
        assert exit_code == 0
        promoted = sorted(path for path in copyback_root.rglob("*") if path.is_file())
        assert len(promoted) == len(payloads)
        unreadable = [
            f"{path}: {stat.S_IMODE(path.stat().st_mode):04o}"
            for path in promoted
            if stat.S_IMODE(path.stat().st_mode) != 0o644
        ]
        assert unreadable == []

        # A file the script did not write is left alone: same size -> skipped.
        untouched = promoted[0]
        os.chmod(untouched, 0o600)
        rerun_exit = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
        rerun = json.loads(capsys.readouterr().out)
    finally:
        os.umask(previous_umask)

    assert rerun_exit == 0
    assert rerun["totals"] == {"copied": 0, "skipped": len(payloads), "failed": 0}
    assert stat.S_IMODE(untouched.stat().st_mode) == 0o600

def test_backfill_canonical_root_that_is_not_a_directory_exits_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    source_root.mkdir()
    copyback_root.mkdir()
    (source_root / "canonical").write_bytes(b"not a directory")

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    summary = json.loads(capsys.readouterr().out)

    # Exit 2 is "unusable root": no cycle or grid entry exists to carry a
    # failure, so clause 1's own predicate is literally false here.
    assert exit_code == 2
    assert summary["root_error"]
    assert summary["cycles"] == [] and summary["grids"] == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root sails straight through a chmod-000 directory")
def test_backfill_unreadable_canonical_root_exits_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, copyback_root, _payloads = _seed_two_source_store(tmp_path)
    canonical_root = source_root / "canonical"
    os.chmod(canonical_root, 0o000)
    try:
        exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
        summary = json.loads(capsys.readouterr().out)
    finally:
        os.chmod(canonical_root, 0o755)

    assert exit_code == 2
    assert summary["root_error"]


@pytest.mark.parametrize("missing", ["source", "copyback"])
def test_backfill_missing_root_exits_two(tmp_path: Path, missing: str) -> None:
    source_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    (copyback_root if missing == "source" else source_root).mkdir()

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])

    assert exit_code == 2


def test_backfill_overlapping_roots_exit_two(tmp_path: Path) -> None:
    source_root = tmp_path / "object-store"
    (source_root / "nested").mkdir(parents=True)

    assert backfill.main(["--source-root", str(source_root), "--copyback-root", str(source_root)]) == 2
    assert backfill.main(["--source-root", str(source_root), "--copyback-root", str(source_root / "nested")]) == 2


def test_backfill_source_root_without_canonical_tree_exits_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    source_root.mkdir()
    copyback_root.mkdir()

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert summary["cycles"] == []
    assert summary["grids"] == []
    assert summary["totals"] == {"copied": 0, "skipped": 0, "failed": 0}


def test_backfill_module_imports_only_the_standard_library() -> None:
    """node-22 runs this with a pinned interpreter and a frozen environment."""

    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"), filename=str(SCRIPT_PATH))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports would tie the script to a package"
            assert node.module is not None
            imported.add(node.module.split(".")[0])

    assert imported
    assert imported.isdisjoint({"services", "packages", "workers", "apps", "tests"})
    non_stdlib = sorted(name for name in imported if name not in sys.stdlib_module_names)
    assert non_stdlib == []


def test_backfill_runs_as_a_module_in_a_subprocess(tmp_path: Path) -> None:
    source_root, copyback_root, payloads = _seed_two_source_store(tmp_path)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            MODULE_NAME,
            "--source-root",
            str(source_root),
            "--copyback-root",
            str(copyback_root),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    summary = json.loads(completed.stdout)
    assert summary["totals"] == {"copied": 14, "skipped": 0, "failed": 0}
    for key, payload in payloads.items():
        assert (copyback_root / key).read_bytes() == payload


def test_backfill_module_launch_outside_the_repo_root_fails_with_no_summary(tmp_path: Path) -> None:
    """The documented `-m` invocation needs the repo root as cwd, and says so.

    There is no `scripts/__init__.py` (PEP 420), so `-m` resolves the module only
    because it puts the cwd on `sys.path`. Launched from anywhere else the
    interpreter exits 1 -- colliding with this script's own "completed but
    something failed" code -- so the only distinguisher an operator or a wrapper
    has is that stdout carries no JSON summary at all.
    """

    source_root, copyback_root, _payloads = _seed_two_source_store(tmp_path)
    argv = [
        sys.executable,
        "-m",
        MODULE_NAME,
        "--source-root",
        str(source_root),
        "--copyback-root",
        str(copyback_root),
    ]

    outside = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, check=False)

    assert outside.returncode == 1
    assert outside.stdout == ""
    assert "ModuleNotFoundError" in outside.stderr
    assert list(copyback_root.rglob("*")) == []
    # Same command, repo root as cwd: the difference is the cwd and nothing else.
    from_root = subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    assert from_root.returncode == 0, from_root.stderr
    assert json.loads(from_root.stdout)["totals"] == {"copied": 14, "skipped": 0, "failed": 0}

@pytest.mark.parametrize("empty", ["source", "copyback"])
def test_backfill_empty_root_argument_exits_two(tmp_path: Path, empty: str) -> None:
    """An unset env var expanded to "" must not silently mirror into the cwd."""

    source_root, copyback_root, _payloads = _seed_two_source_store(tmp_path)
    argv = [
        "--source-root",
        "" if empty == "source" else str(source_root),
        "--copyback-root",
        "" if empty == "copyback" else str(copyback_root),
    ]

    assert backfill.main(argv) == 2
