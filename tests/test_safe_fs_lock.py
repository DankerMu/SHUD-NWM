"""The shared lock-file opener (#2540): ``open_lock_file_no_follow``.

On macOS/APFS a plain ``openat(dir_fd, name, O_CREAT)`` that races a concurrent
first creator can fail with ``ENOENT`` instead of opening the winner's file.  The
opener creates exclusively first and falls back to a plain no-follow open on
``EEXIST``; an entry that vanishes between the two opens is retried a bounded
number of times.  The primitive tests below are platform-independent: the race
windows are forced by a delegating ``os.open`` fake that intercepts only the
test's own ``dir_fd``.  The threaded ``Barrier`` test drives the scheduler lease
guard-file site directly -- it has no in-process registry, so origin/master
reproduces the race there on macOS only.

Every ``services.*`` import is function-local, so this file joins no frozen
top-level importer set (``tests/test_select_ci_tests.py``).
"""

from __future__ import annotations

import errno
import os
import stat
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from packages.common.safe_fs import SafeFilesystemError
from packages.common.safe_fs_lock import open_lock_file_no_follow

_LOCK_NAME = "cycle.lock"


@pytest.fixture
def lock_dir(tmp_path: Path) -> Iterator[tuple[Path, int]]:
    directory = tmp_path / "locks"
    directory.mkdir()
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield directory, dir_fd
    finally:
        os.close(dir_fd)


def _intercept_os_open(monkeypatch: pytest.MonkeyPatch, dir_fd: int, before_real_open) -> list[str]:
    """Route this test's lock opens through ``before_real_open``; everything else is untouched.

    Returns the call log, one ``"excl"`` / ``"plain"`` entry per intercepted open.
    """

    real_open = os.open
    intercepted_dir_fd = dir_fd
    calls: list[str] = []

    def fake_open(path, flags, mode=0o777, *, dir_fd=None):  # noqa: ANN001 - os.open signature
        if dir_fd != intercepted_dir_fd or path != _LOCK_NAME:
            return real_open(path, flags, mode, dir_fd=dir_fd)
        kind = "excl" if flags & os.O_EXCL else "plain"
        calls.append(kind)
        before_real_open(kind, len(calls))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", fake_open)
    return calls


def test_a_concurrent_winner_still_yields_a_descriptor_to_its_file(lock_dir: tuple[Path, int]) -> None:
    """The winner already created the file: ``EEXIST`` on create, then the plain open succeeds."""

    directory, dir_fd = lock_dir
    winner = directory / _LOCK_NAME
    winner.write_bytes(b"")

    fd = open_lock_file_no_follow(_LOCK_NAME, dir_fd=dir_fd, mode=0o600)
    try:
        opened = os.fstat(fd)
        assert stat.S_ISREG(opened.st_mode)
        assert (opened.st_dev, opened.st_ino) == (winner.stat().st_dev, winner.stat().st_ino)
    finally:
        os.close(fd)


def test_a_first_creation_honours_the_mode(lock_dir: tuple[Path, int]) -> None:
    directory, dir_fd = lock_dir
    old_umask = os.umask(0)
    try:
        fd = open_lock_file_no_follow(_LOCK_NAME, dir_fd=dir_fd, mode=0o640)
    finally:
        os.umask(old_umask)
    os.close(fd)

    assert stat.S_IMODE((directory / _LOCK_NAME).stat().st_mode) == 0o640


def test_an_entry_unlinked_between_create_and_open_is_retried(
    lock_dir: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, dir_fd = lock_dir
    (directory / _LOCK_NAME).write_bytes(b"")

    def vanish_before_first_plain_open(kind: str, call_number: int) -> None:
        if kind == "plain" and call_number == 2:
            (directory / _LOCK_NAME).unlink()

    calls = _intercept_os_open(monkeypatch, dir_fd, vanish_before_first_plain_open)

    fd = open_lock_file_no_follow(_LOCK_NAME, dir_fd=dir_fd, mode=0o600)
    try:
        assert stat.S_ISREG(os.fstat(fd).st_mode)
    finally:
        os.close(fd)
    # excl -> EEXIST, plain -> ENOENT (unlinked), excl again -> created.
    assert calls == ["excl", "plain", "excl"]
    assert (directory / _LOCK_NAME).is_file()


def test_retry_exhaustion_re_raises_the_last_enoent(
    lock_dir: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, dir_fd = lock_dir

    def always_lose_the_race(kind: str, _call_number: int) -> None:
        if kind == "excl":
            # Another creator wins every time ...
            (directory / _LOCK_NAME).write_bytes(b"")
        else:
            # ... and unlinks it again before our plain open.
            (directory / _LOCK_NAME).unlink()

    calls = _intercept_os_open(monkeypatch, dir_fd, always_lose_the_race)

    with pytest.raises(FileNotFoundError) as excinfo:
        open_lock_file_no_follow(_LOCK_NAME, dir_fd=dir_fd, mode=0o600)

    assert excinfo.value.errno == errno.ENOENT
    assert calls == ["excl", "plain"] * 3


def test_a_symlink_is_refused_with_eloop_and_never_followed(lock_dir: tuple[Path, int]) -> None:
    directory, dir_fd = lock_dir
    target = directory / "target-that-must-not-appear"
    (directory / _LOCK_NAME).symlink_to(target)

    with pytest.raises(OSError) as excinfo:
        open_lock_file_no_follow(_LOCK_NAME, dir_fd=dir_fd, mode=0o600)

    assert excinfo.value.errno == errno.ELOOP
    assert not isinstance(excinfo.value, SafeFilesystemError)
    # Neither the exclusive create nor the plain open followed the dangling link.
    assert not target.exists()
    assert (directory / _LOCK_NAME).is_symlink()


def test_a_directory_is_refused_with_eisdir_as_the_raw_oserror(lock_dir: tuple[Path, int]) -> None:
    directory, dir_fd = lock_dir
    (directory / _LOCK_NAME).mkdir()

    with pytest.raises(OSError) as excinfo:
        open_lock_file_no_follow(_LOCK_NAME, dir_fd=dir_fd, mode=0o600)

    assert excinfo.value.errno == errno.EISDIR
    assert type(excinfo.value) is IsADirectoryError


def test_concurrent_first_openers_of_the_lease_guard_file_both_get_the_same_file(tmp_path: Path) -> None:
    """``scheduler_lease._open_regular_guard_file`` under a real first-creation race.

    Two threads meet at a ``Barrier`` and open a guard file that does not yet
    exist, once per round on a fresh name.  Before #2540 the loser of a round
    failed with ``ENOENT`` on macOS/APFS roughly every other round, so twenty
    rounds make the pre-change failure deterministic there; on Linux the race
    never surfaces and this is a pin.
    """

    from services.orchestrator.scheduler_lease import _open_regular_guard_file

    directory = tmp_path / "guards"
    directory.mkdir()
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    failures: list[BaseException] = []
    try:
        for round_number in range(20):
            guard_name = f"scheduler_{round_number:02d}.lock.guard"
            barrier = threading.Barrier(2)
            identities: list[tuple[int, int]] = []

            def open_guard() -> None:
                barrier.wait()
                try:
                    fd = _open_regular_guard_file(guard_name, dir_fd=dir_fd)
                except BaseException as error:  # noqa: BLE001 - collected and asserted below
                    failures.append(error)
                    return
                try:
                    opened = os.fstat(fd)
                    identities.append((opened.st_dev, opened.st_ino))
                finally:
                    os.close(fd)

            threads = [threading.Thread(target=open_guard) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert failures == [], f"round {round_number}: {failures!r}"
            assert len(identities) == 2 and identities[0] == identities[1]
    finally:
        os.close(dir_fd)
