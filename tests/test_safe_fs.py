"""L1 evidence for `directory_identity_no_follow` (#1192).

What this file proves: the helper consumes **inode identity**, not the input
string.  Every case here runs against a real filesystem, needs no root, and is
portable to Linux (CI, node-27) as well as macOS.

HONEST LIMIT -- read before adding cases.  This layer does **not** prove that
two paths existing *at the same time* under different realpaths report one
identity.  That shape is a bind mount or a second mount point of one export,
and there is no portable, root-free construction for it: directories cannot be
hardlinked, `Path.resolve()` folds symlink aliases away, the no-follow walk
refuses a symlink final component outright, and macOS case-folding aliases are
two distinct directories on Linux.  The rename pair below is **sequential** --
one inode seen at two realpaths one after the other -- not concurrent.  The
guard-level claim is carried by the injection tests in
tests/test_scheduler_state_index_copyback_replay.py,
tests/test_run_tree_copyback.py, tests/test_tile_publisher.py and
tests/test_forcing_copyback_backfill.py, plus the POSIX same-superblock
argument recorded in the change proposal.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from packages.common.safe_fs import SafeFilesystemError, directory_identity_no_follow


def test_directory_identity_is_stable_across_different_input_strings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Normalization layer only: `~` expansion and cwd-relative resolution both
    # land on one directory. A pure-string implementation passes this too --
    # test_directory_identity_survives_rename below is what kills that one.
    home = tmp_path.resolve()
    real = home / "real"
    real.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(home)

    absolute = directory_identity_no_follow(real)
    tilde = directory_identity_no_follow(Path("~/real"))
    relative = directory_identity_no_follow(Path("real"))

    assert tilde == absolute
    assert relative == absolute


def test_directory_identity_survives_rename(tmp_path: Path) -> None:
    # The discriminating case: one inode, two genuinely different realpaths,
    # sequentially. `return (0, hash(str(path)))` -- the string implementation
    # this whole change replaces -- fails here and passes everything else.
    original = tmp_path / "before"
    original.mkdir()
    before = directory_identity_no_follow(original)

    renamed = tmp_path / "after"
    os.rename(original, renamed)

    assert directory_identity_no_follow(renamed) == before
    assert str(renamed) != str(original)


def test_directory_identity_equals_the_kernel_stat_pair(tmp_path: Path) -> None:
    # Pins that the returned pair is the kernel's, not a self-invented number.
    target = tmp_path / "dir"
    target.mkdir()
    info = os.stat(target)

    assert directory_identity_no_follow(target) == (info.st_dev, info.st_ino)


def test_directory_identity_differs_between_two_real_directories(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()

    assert directory_identity_no_follow(left) != directory_identity_no_follow(right)


def test_directory_identity_raises_for_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        directory_identity_no_follow(tmp_path / "absent")


@pytest.mark.parametrize("shape", ["final", "ancestor"])
def test_directory_identity_refuses_symlink_components(tmp_path: Path, shape: str) -> None:
    # Type only, never the message: the same final-component symlink surfaces as
    # ENOTDIR on macOS ("Path component is not a directory") and ELOOP on Linux
    # ("Path component must not be a symlink"). Asserting text reds on one of
    # the two platforms this repo runs on.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    probed = link if shape == "final" else link / "child"
    if shape == "ancestor":
        (real / "child").mkdir()

    with pytest.raises(SafeFilesystemError):
        directory_identity_no_follow(probed)
