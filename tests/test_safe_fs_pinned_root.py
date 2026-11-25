"""`pinned_containment_root` (#3236 B1): one root-chain open per block.

Inside a block, `stat_no_follow(path, containment_root=root)` starts its walk
from a duplicate of a handle on `root` opened once on entry; everything after
that is the code that runs outside a block.  These tests pin:

* the outcome matrix: for every tree that does not change after the block is
  entered, the result (or exception class and `kind`) is the same inside and
  outside a block, and is the class the design's outcome table names;
* the saving: the root chain is opened once per block, and only for
  `stat_no_follow` naming the pinned root;
* the handle lifetime: nothing outlives a block, in this context or in a
  copied one;
* the race table: what a change made between two calls of one block does,
  next to what the same call does outside a block;
* nesting and threads.

The chain-open count is observed by replacing
`packages.common.safe_fs._open_directory_no_follow` with a counting wrapper;
the module looks that name up at call time.
"""

from __future__ import annotations

import contextvars
import errno
import os
import shutil
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from packages.common import safe_fs
from packages.common.safe_fs import (
    _PINNED_CONTAINMENT_ROOT,
    SafeFilesystemError,
    open_file_no_follow,
    pinned_containment_root,
    read_bytes_no_follow,
    stat_no_follow,
)

Outcome = tuple[str, object]


def _identity(result: os.stat_result) -> tuple[int, int, int, int, int]:
    return (result.st_mode, result.st_ino, result.st_dev, result.st_size, result.st_mtime_ns)


def _outcome(path: Path, root: Path | None) -> Outcome:
    """Run one stat and reduce it to a comparable outcome.

    Only the call is wrapped: a block is always entered before this runs, so
    an exception escaping from entry fails the test instead of becoming an
    outcome.
    """

    try:
        return ("ok", _identity(stat_no_follow(path, containment_root=root)))
    except SafeFilesystemError as error:
        return ("SafeFilesystemError", error.kind)
    except FileNotFoundError:
        return ("FileNotFoundError", None)


@pytest.fixture
def opens(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every call of the root-chain open, in order."""

    calls: list[Path] = []
    lock = threading.Lock()
    real = safe_fs._open_directory_no_follow

    def counting(path: Path) -> int:
        with lock:
            calls.append(Path(path))
        return real(path)

    monkeypatch.setattr(safe_fs, "_open_directory_no_follow", counting)
    return calls


def _base_tree(tmp_path: Path) -> Path:
    """`top/root` with `a/f`, `a/d/`, `g` and a sibling `top/other/f`."""

    root = tmp_path / "top" / "root"
    (root / "a" / "d").mkdir(parents=True)
    (root / "a" / "f").write_bytes(b"f-bytes")
    (root / "g").write_bytes(b"g")
    (tmp_path / "top" / "other").mkdir()
    (tmp_path / "top" / "other" / "f").write_bytes(b"outside")
    return root


# --- 3.1 outcome matrix -------------------------------------------------------


def _row_regular_file(tmp_path: Path) -> tuple[Path, list[Path]]:
    root = _base_tree(tmp_path)
    return root, [root / "a" / "f"]


def _row_directory(tmp_path: Path) -> tuple[Path, list[Path]]:
    root = _base_tree(tmp_path)
    return root, [root / "a" / "d"]


def _row_file_under_root_twice(tmp_path: Path) -> tuple[Path, list[Path]]:
    root = _base_tree(tmp_path)
    return root, [root / "g", root / "g"]


def _row_absent_leaf(tmp_path: Path) -> tuple[Path, list[Path]]:
    root = _base_tree(tmp_path)
    return root, [root / "a" / "missing"]


def _row_absent_component(tmp_path: Path) -> tuple[Path, list[Path]]:
    root = _base_tree(tmp_path)
    return root, [root / "missing" / "f"]


def _row_absent_root(tmp_path: Path) -> tuple[Path, list[Path]]:
    (tmp_path / "top").mkdir()
    root = tmp_path / "top" / "root"
    return root, [root / "a" / "f"]


def _row_symlink_leaf(tmp_path: Path) -> tuple[Path, list[Path]]:
    root = _base_tree(tmp_path)
    (root / "a" / "link").symlink_to(root / "a" / "f")
    return root, [root / "a" / "link"]


def _row_symlinked_component(tmp_path: Path) -> tuple[Path, list[Path]]:
    root = _base_tree(tmp_path)
    (root / "l").symlink_to(root / "a", target_is_directory=True)
    return root, [root / "l" / "f"]


def _row_non_directory_component(tmp_path: Path) -> tuple[Path, list[Path]]:
    root = _base_tree(tmp_path)
    return root, [root / "g" / "x"]


def _row_root_is_symlink(tmp_path: Path) -> tuple[Path, list[Path]]:
    real = _base_tree(tmp_path)
    root = tmp_path / "top" / "root_link"
    root.symlink_to(real, target_is_directory=True)
    return root, [root / "a" / "f", root / "g"]


def _row_component_above_root_is_symlink(tmp_path: Path) -> tuple[Path, list[Path]]:
    _base_tree(tmp_path)
    (tmp_path / "top_link").symlink_to(tmp_path / "top", target_is_directory=True)
    root = tmp_path / "top_link" / "root"
    return root, [root / "a" / "f", root / "g"]


def _row_outside_root(tmp_path: Path) -> tuple[Path, list[Path]]:
    root = _base_tree(tmp_path)
    return root, [tmp_path / "top" / "other" / "f"]


def _row_dotdot_under_root(tmp_path: Path) -> tuple[Path, list[Path]]:
    root = _base_tree(tmp_path)
    (root / "sub").mkdir()
    return root, [root / "sub" / ".."]


#: Row -> (tree builder, the outcome class the design's outcome table names).
_ROWS: dict[str, tuple[Callable[[Path], tuple[Path, list[Path]]], str]] = {
    "regular_file": (_row_regular_file, "ok"),
    "directory": (_row_directory, "ok"),
    "file_directly_under_root_twice": (_row_file_under_root_twice, "ok"),
    "absent_leaf": (_row_absent_leaf, "FileNotFoundError"),
    "absent_component_under_root": (_row_absent_component, "FileNotFoundError"),
    "absent_root": (_row_absent_root, "FileNotFoundError"),
    "symlink_leaf": (_row_symlink_leaf, "SafeFilesystemError"),
    "symlinked_component_under_root": (_row_symlinked_component, "SafeFilesystemError"),
    "non_directory_component_under_root": (_row_non_directory_component, "SafeFilesystemError"),
    "root_is_symlink": (_row_root_is_symlink, "SafeFilesystemError"),
    "component_above_root_is_symlink": (_row_component_above_root_is_symlink, "SafeFilesystemError"),
    "path_outside_root": (_row_outside_root, "SafeFilesystemError"),
    "dotdot_under_root": (_row_dotdot_under_root, "SafeFilesystemError"),
}


@pytest.mark.parametrize("row", sorted(_ROWS))
def test_outcome_is_the_same_inside_and_outside_a_block(tmp_path: Path, row: str) -> None:
    build, expected_class = _ROWS[row]
    root, paths = build(tmp_path)

    outside = [_outcome(path, root) for path in paths]
    with pinned_containment_root(root):
        inside = [_outcome(path, root) for path in paths]

    assert inside == outside, f"row {row}: inside {inside} != outside {outside}"
    assert {outcome[0] for outcome in outside} == {expected_class}, f"row {row}: {outside}"


# --- 3.2 / 3.3 the chain is opened once, and only for stat_no_follow --------


def _five_files(tmp_path: Path) -> tuple[Path, list[Path]]:
    root = _base_tree(tmp_path)
    paths = []
    for index in range(5):
        path = root / "a" / f"f{index}"
        path.write_bytes(b"x" * index)
        paths.append(path)
    return root, paths


def test_root_chain_is_opened_once_per_block(tmp_path: Path, opens: list[Path]) -> None:
    root, paths = _five_files(tmp_path)

    for path in paths:
        stat_no_follow(path, containment_root=root)
    assert len(opens) == 5

    opens.clear()
    with pinned_containment_root(root):
        for path in paths:
            stat_no_follow(path, containment_root=root)
        assert len(opens) == 1


def test_calls_without_the_pinned_root_open_their_own_chain(tmp_path: Path, opens: list[Path]) -> None:
    root, paths = _five_files(tmp_path)
    other_root = tmp_path / "top"

    with pinned_containment_root(root):
        assert len(opens) == 1
        stat_no_follow(paths[0], containment_root=None)
        assert len(opens) == 2
        result = stat_no_follow(paths[0], containment_root=other_root)
        assert len(opens) == 3
    assert _identity(result) == _identity(os.lstat(paths[0]))


def test_call_without_a_root_ignores_a_pin_on_the_filesystem_anchor(tmp_path: Path, opens: list[Path]) -> None:
    # With no containment root the walk is anchored at the filesystem root, so
    # a pin on that same directory must still not be used (task 1.3).
    root, paths = _five_files(tmp_path)
    anchor = Path(tmp_path.anchor)
    with pinned_containment_root(anchor):
        assert _PINNED_CONTAINMENT_ROOT.get() is not None
        before = len(opens)
        result = stat_no_follow(paths[0], containment_root=None)
        assert len(opens) == before + 1
    assert _identity(result) == _identity(os.lstat(paths[0]))


def test_read_bytes_ignores_the_pin(tmp_path: Path, opens: list[Path]) -> None:
    root, paths = _five_files(tmp_path)
    with pinned_containment_root(root):
        before = len(opens)
        assert read_bytes_no_follow(paths[3], containment_root=root) == b"xxx"
        assert len(opens) == before + 1


def test_open_file_ignores_the_pin(tmp_path: Path, opens: list[Path]) -> None:
    root, paths = _five_files(tmp_path)
    with pinned_containment_root(root):
        before = len(opens)
        fd = open_file_no_follow(paths[2], containment_root=root)
        os.close(fd)
        assert len(opens) == before + 1


# --- 3.4 handle lifetime ------------------------------------------------------


def _pinned_fd() -> int:
    pin = _PINNED_CONTAINMENT_ROOT.get()
    assert pin is not None and pin.fd is not None, "the block did not pin its root"
    return pin.fd


def _assert_closed(fd: int) -> None:
    with pytest.raises(OSError) as info:
        os.fstat(fd)
    assert info.value.errno == errno.EBADF


def test_pinned_handle_is_closed_after_the_block(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    with pinned_containment_root(root):
        fd = _pinned_fd()
    _assert_closed(fd)


def test_context_variable_is_restored_after_the_block(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    before = _PINNED_CONTAINMENT_ROOT.get()
    with pinned_containment_root(root):
        assert _PINNED_CONTAINMENT_ROOT.get() is not before
    assert _PINNED_CONTAINMENT_ROOT.get() is before


def test_pinned_handle_is_closed_when_the_body_raises(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    fd = -1
    with pytest.raises(LookupError):
        with pinned_containment_root(root):
            fd = _pinned_fd()
            raise LookupError("body failure")
    _assert_closed(fd)


def test_context_variable_is_restored_when_the_body_raises(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    before = _PINNED_CONTAINMENT_ROOT.get()
    with pytest.raises(LookupError):
        with pinned_containment_root(root):
            raise LookupError("body failure")
    assert _PINNED_CONTAINMENT_ROOT.get() is before


@pytest.mark.parametrize("row", ["absent_root", "root_is_symlink", "component_above_root_is_symlink"])
def test_entry_that_cannot_open_the_root_pins_nothing(tmp_path: Path, row: str) -> None:
    root, _paths = _ROWS[row][0](tmp_path)
    with pinned_containment_root(root):
        assert _PINNED_CONTAINMENT_ROOT.get() is None


def test_entry_with_an_unexpandable_root_pins_nothing(tmp_path: Path) -> None:
    root = Path("~no_such_user_for_pinned_root_3236") / "root"
    with pinned_containment_root(root):
        assert _PINNED_CONTAINMENT_ROOT.get() is None


def test_copied_context_finds_no_usable_pin_after_the_block(tmp_path: Path, opens: list[Path]) -> None:
    root = _base_tree(tmp_path)
    target = root / "a" / "f"
    with pinned_containment_root(root):
        copied = contextvars.copy_context()
    before = len(opens)

    outcome = copied.run(_outcome, target, root)

    assert outcome == ("ok", _identity(os.lstat(target)))
    assert len(opens) == before + 1, "the copied context must open the chain itself"


def _tree_directories(top: Path) -> set[tuple[int, int]]:
    identities = {(os.lstat(top).st_dev, os.lstat(top).st_ino)}
    for current, directories, _files in os.walk(top):
        for name in directories:
            info = os.lstat(Path(current) / name)
            identities.add((info.st_dev, info.st_ino))
    return identities


def _open_descriptor_identities() -> set[tuple[int, int]]:
    identities: set[tuple[int, int]] = set()
    for entry in os.listdir("/dev/fd"):
        try:
            info = os.fstat(int(entry))
        except (OSError, ValueError):
            continue
        identities.add((info.st_dev, info.st_ino))
    return identities


def test_no_descriptor_of_the_tree_survives_many_blocks(tmp_path: Path) -> None:
    root, paths = _five_files(tmp_path)
    tree = _tree_directories(tmp_path / "top")
    assert not tree & _open_descriptor_identities(), "precondition: the tree is not open yet"

    for _ in range(200):
        with pinned_containment_root(root):
            for path in paths:
                stat_no_follow(path, containment_root=root)

    leaked = tree & _open_descriptor_identities()
    assert not leaked, f"descriptors still refer to tree directories: {leaked}"


def test_failed_duplicate_of_the_pin_is_an_io_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Task 1.2: an ``OSError`` from ``os.dup`` of the pin is ``SafeFilesystemError(kind="io")``.

    The path's parent is below the root, so the duplicate of the pin is the
    only ``os.dup`` of the call.  ``safe_fs.os`` is the real ``os`` module, so
    the replacement is undone right after the one call.
    """

    root, paths = _five_files(tmp_path)
    tree = _tree_directories(tmp_path / "top")

    def no_descriptors_left(_fd: int) -> int:
        raise OSError(errno.EMFILE, "Too many open files")

    with pinned_containment_root(root):
        with monkeypatch.context() as patch:
            patch.setattr(safe_fs.os, "dup", no_descriptors_left)
            with pytest.raises(SafeFilesystemError) as info:
                stat_no_follow(paths[0], containment_root=root)
        assert info.value.kind == "io"
        assert isinstance(info.value.__cause__, OSError) and info.value.__cause__.errno == errno.EMFILE
        assert _outcome(paths[1], root) == ("ok", _identity(os.lstat(paths[1])))
    leaked = tree & _open_descriptor_identities()
    assert not leaked, f"descriptors still refer to tree directories: {leaked}"


# --- 3.5 races: a change between two calls of one block -----------------------


def test_race_component_under_root_becomes_a_symlink(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    target = root / "a" / "f"
    with pinned_containment_root(root):
        assert _outcome(target, root)[0] == "ok"
        shutil.copytree(root / "a", tmp_path / "a_copy", symlinks=True)
        (root / "a").rename(tmp_path / "a_old")
        (root / "a").symlink_to(tmp_path / "a_copy", target_is_directory=True)
        with pytest.raises(SafeFilesystemError):
            stat_no_follow(target, containment_root=root)
    with pytest.raises(SafeFilesystemError):
        stat_no_follow(target, containment_root=root)


def test_race_root_replaced_by_another_directory(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    target = root / "a" / "f"
    with pinned_containment_root(root):
        old = _outcome(target, root)
        assert old[0] == "ok"
        root.rename(tmp_path / "top" / "root_old")
        (root / "a" / "d").mkdir(parents=True)
        target.write_bytes(b"f-bytes")
        (root / "g").write_bytes(b"g")
        with pytest.raises(SafeFilesystemError):
            stat_no_follow(target, containment_root=root)
    # Outside a block the same call describes the other directory's entry.
    replacement = _outcome(target, root)
    assert replacement == ("ok", _identity(os.lstat(target)))
    assert replacement != old


def test_race_component_above_root_becomes_a_symlink_to_itself(tmp_path: Path) -> None:
    """Stated limit: a swap above the root is not seen by the rest of the block.

    The handle was opened through the real chain on entry and the identity
    checks compare it with what the parent's path names now; a symlink above
    the root resolves to the same directories, so both checks agree and the
    values returned are the real files' stats, not a decoy's.  The first call
    of the next block cannot open the chain without following the symlink and
    is refused, as every call outside a block is.
    """

    root = _base_tree(tmp_path)
    real_top = tmp_path / "top_real"
    with pinned_containment_root(root):
        assert _outcome(root / "g", root)[0] == "ok"
        (tmp_path / "top").rename(real_top)
        (tmp_path / "top").symlink_to(real_top, target_is_directory=True)
        assert _outcome(root / "a" / "f", root) == ("ok", _identity(os.lstat(real_top / "root" / "a" / "f")))
        assert _outcome(root / "g", root) == ("ok", _identity(os.lstat(real_top / "root" / "g")))
    with pytest.raises(SafeFilesystemError):
        stat_no_follow(root / "a" / "f", containment_root=root)
    with pinned_containment_root(root):
        with pytest.raises(SafeFilesystemError):
            stat_no_follow(root / "a" / "f", containment_root=root)


def test_race_root_becomes_a_symlink_to_itself(tmp_path: Path) -> None:
    """Stated limit: the root swapped for a symlink to itself.

    A path whose parent is below the root is walked from the pinned handle and
    its parent's path resolves through the symlink to the same directory, so
    the real entry's stat is returned.  A path whose parent is the root makes
    the identity check look at the root's own path, finds a symlink, and is
    refused.
    """

    root = _base_tree(tmp_path)
    real_root = tmp_path / "top" / "root_real"
    with pinned_containment_root(root):
        assert _outcome(root / "g", root)[0] == "ok"
        root.rename(real_root)
        root.symlink_to(real_root, target_is_directory=True)
        assert _outcome(root / "a" / "f", root) == ("ok", _identity(os.lstat(real_root / "a" / "f")))
        with pytest.raises(SafeFilesystemError):
            stat_no_follow(root / "g", containment_root=root)
    with pytest.raises(SafeFilesystemError):
        stat_no_follow(root / "a" / "f", containment_root=root)


def test_race_root_renamed_away(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    target = root / "a" / "f"
    with pinned_containment_root(root):
        assert _outcome(target, root)[0] == "ok"
        root.rename(tmp_path / "top" / "root_gone")
        with pytest.raises(SafeFilesystemError) as info:
            stat_no_follow(target, containment_root=root)
        assert info.value.kind == "io"
    with pytest.raises(FileNotFoundError):
        stat_no_follow(target, containment_root=root)


# --- 3.6 nesting and threads --------------------------------------------------


def test_same_root_nesting_reuses_the_outer_pin(tmp_path: Path, opens: list[Path]) -> None:
    root, paths = _five_files(tmp_path)
    with pinned_containment_root(root):
        outer = _PINNED_CONTAINMENT_ROOT.get()
        outer_fd = _pinned_fd()
        with pinned_containment_root(root):
            assert _PINNED_CONTAINMENT_ROOT.get() is outer
            stat_no_follow(paths[0], containment_root=root)
        assert len(opens) == 1, "the inner block for the same root must reuse the outer pin"
        assert _PINNED_CONTAINMENT_ROOT.get() is outer
        assert _pinned_fd() == outer_fd, "the inner block must not clear the outer pin"
        os.fstat(outer_fd)
        assert _outcome(paths[1], root) == ("ok", _identity(os.lstat(paths[1])))
        assert len(opens) == 1, "the outer block's later stat must still use its own pin"
    assert len(opens) == 1
    _assert_closed(outer_fd)


def test_different_root_nesting_has_one_pin_at_a_time(tmp_path: Path, opens: list[Path]) -> None:
    root_a = _base_tree(tmp_path / "A")
    root_b = _base_tree(tmp_path / "B")
    with pinned_containment_root(root_a):
        with pinned_containment_root(root_b):
            assert _outcome(root_b / "a" / "f", root_b) == ("ok", _identity(os.lstat(root_b / "a" / "f")))
            assert _outcome(root_b / "g", root_b) == ("ok", _identity(os.lstat(root_b / "g")))
            assert len(opens) == 2
            assert _outcome(root_a / "a" / "f", root_a) == ("ok", _identity(os.lstat(root_a / "a" / "f")))
            assert len(opens) == 3
        assert _outcome(root_a / "g", root_a) == ("ok", _identity(os.lstat(root_a / "g")))
        assert len(opens) == 3


@pytest.mark.skipif(
    bool(getattr(sys.flags, "thread_inherit_context", False)),
    reason="this runtime lets new threads inherit the starting thread's context",
)
def test_another_thread_does_not_see_the_pin(tmp_path: Path, opens: list[Path]) -> None:
    root = _base_tree(tmp_path)
    target = root / "a" / "f"
    seen: list[Outcome] = []
    with pinned_containment_root(root):
        assert len(opens) == 1
        worker = threading.Thread(target=lambda: seen.append(_outcome(target, root)))
        worker.start()
        worker.join()
        assert len(opens) == 2, "the other thread must open the chain itself"
    assert seen == [("ok", _identity(os.lstat(target)))]
