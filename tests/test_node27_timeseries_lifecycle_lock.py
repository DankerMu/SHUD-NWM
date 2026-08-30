"""Lifecycle mutex: no-follow, owner/mode, contention, never unlinked."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from packages.common.node27_timeseries_lifecycle_lock import (
    LifecycleLockContended,
    LifecycleLockError,
    acquire_timeseries_lifecycle_lock,
    refuse_lifecycle_lock_env_override,
    release_timeseries_lifecycle_lock,
    timeseries_lifecycle_lock,
)


def _args(**kwargs: object) -> argparse.Namespace:
    defaults: dict[str, object] = {"enforce": False, "receipt_path": None, "lock_path": None, "archive_gate": None}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_lifecycle_lock_path_is_the_fixed_tmp_mutex() -> None:
    source = Path(
        __import__("packages.common.node27_timeseries_lifecycle_lock", fromlist=["LIFECYCLE_LOCK_PATH"]).__file__
        or ""
    ).read_text(encoding="utf-8")
    assert 'Path("/tmp/nhms-node27-timeseries-lifecycle.lock")' in source


def test_acquire_creates_regular_user_owned_mode_0600_single_link(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.lock"
    fd = acquire_timeseries_lifecycle_lock(path)
    assert fd is not None
    try:
        info = os.fstat(fd)
        named = os.lstat(path)
        assert stat.S_ISREG(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_uid == os.geteuid()
        assert info.st_nlink == 1
        assert (info.st_dev, info.st_ino) == (named.st_dev, named.st_ino)
        assert path.exists()
    finally:
        release_timeseries_lifecycle_lock(fd)
    assert path.exists()


def test_release_never_unlinks_the_lock_file(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.lock"
    fd = acquire_timeseries_lifecycle_lock(path)
    assert fd is not None
    release_timeseries_lifecycle_lock(fd)
    assert path.is_file()
    again = acquire_timeseries_lifecycle_lock(path)
    assert again is not None
    release_timeseries_lifecycle_lock(again)


def test_contention_is_none_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.lock"
    held = acquire_timeseries_lifecycle_lock(path)
    assert held is not None
    try:
        contended = acquire_timeseries_lifecycle_lock(path)
        assert contended is None
    finally:
        release_timeseries_lifecycle_lock(held)


def test_context_manager_raises_contended_without_holding_second_fd(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.lock"
    with timeseries_lifecycle_lock(path) as fd:
        assert fd >= 0
        with pytest.raises(LifecycleLockContended, match="held"):
            with timeseries_lifecycle_lock(path):
                raise AssertionError("must not enter while contended")


def test_symlink_lock_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "real.lock"
    target.write_bytes(b"")
    target.chmod(0o600)
    link = tmp_path / "lifecycle.lock"
    link.symlink_to(target)
    with pytest.raises(LifecycleLockError, match="symlink|regular"):
        acquire_timeseries_lifecycle_lock(link)


def test_wrong_mode_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.lock"
    path.write_bytes(b"")
    path.chmod(0o644)
    with pytest.raises(LifecycleLockError, match="mode 0600"):
        acquire_timeseries_lifecycle_lock(path)


def test_hardlink_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.lock"
    alias = tmp_path / "alias.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    os.link(path, alias)
    with pytest.raises(LifecycleLockError, match="one hard link"):
        acquire_timeseries_lifecycle_lock(path)


def test_relative_path_is_refused(tmp_path: Path) -> None:
    with pytest.raises(LifecycleLockError, match="absolute"):
        acquire_timeseries_lifecycle_lock(Path("relative.lock"))


def test_path_fd_identity_is_rechecked_after_open(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.lock"
    fd = acquire_timeseries_lifecycle_lock(path)
    assert fd is not None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        release_timeseries_lifecycle_lock(fd)


def test_env_override_cannot_split_lifecycle_lanes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import node27_cold_residency as cold
    from scripts import node27_timeseries_compression as compression
    from scripts import node27_timeseries_decompression_replay as replay
    from scripts import node27_timeseries_retention as retention

    refuse_lifecycle_lock_env_override(
        {"NODE27_TIMESERIES_LIFECYCLE_LOCK_PATH": "/tmp/nhms-node27-timeseries-lifecycle.lock"}
    )
    foreign = [
        str(tmp_path / "split.lock"),
        str(tmp_path / "other.lock"),
        "/var/tmp/nhms-node27-timeseries-lifecycle.lock",
        "/tmp/nhms-node27-timeseries-lifecycle.lock.extra",
    ]
    for value in foreign:
        monkeypatch.setenv("NODE27_TIMESERIES_LIFECYCLE_LOCK_PATH", value)
        with pytest.raises(cold.ColdResidencyConfigError, match="cannot override"):
            cold.config_from_args(
                _args(enforce=False),
                {
                    "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
                    "NODE27_COLD_RESIDENCY_RECEIPT_PATH": str(tmp_path / "receipt.json"),
                    "NODE27_COLD_RESIDENCY_LOCK_PATH": str(tmp_path / "runner.lock"),
                    "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "100",
                    "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "1",
                    "NODE27_TIMESERIES_LIFECYCLE_LOCK_PATH": value,
                },
            )
        with pytest.raises(compression.CompressionConfigError, match="cannot override"):
            compression.config_from_args(
                _args(enforce=False, receipt_path=None, lock_path=None),
                {
                    "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
                    "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "604800",
                    "NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": "1",
                    "NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH": str(tmp_path / "receipt.json"),
                    "NODE27_TIMESERIES_COMPRESSION_LOCK_PATH": str(tmp_path / "runner.lock"),
                    "NODE27_TIMESERIES_LIFECYCLE_LOCK_PATH": value,
                },
            )
        with pytest.raises(retention.RetentionConfigError, match="cannot override"):
            retention.config_from_args(
                _args(enforce=False, receipt_path=None, lock_path=None, archive_gate=None),
                {
                    "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
                    "NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE": "disabled",
                    "NODE27_TIMESERIES_RETENTION_RECEIPT_PATH": str(tmp_path / "receipt.json"),
                    "NODE27_TIMESERIES_RETENTION_LOCK_PATH": str(tmp_path / "runner.lock"),
                    "NODE27_TIMESERIES_LIFECYCLE_LOCK_PATH": value,
                },
            )
        with pytest.raises(replay.DecompressionError, match="unsafe"):
            replay.produce_recovery_receipt(
                database_url="postgresql://user:secretpw@127.0.0.1:55432/nhms",
                mutation_head_sha="a" * 40,
                receipt_path=tmp_path / "recovery.json",
                connect=lambda _url: (_ for _ in ()).throw(AssertionError("no mutation")),
            )


def _owned_lifecycle_fd(tmp_path: Path) -> int:
    return os.open(tmp_path / "life.fd", os.O_RDWR | os.O_CREAT, 0o600)


def _retention_env(tmp_path: Path) -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
        "NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE": "disabled",
        "NODE27_TIMESERIES_RETENTION_RECEIPT_PATH": str(tmp_path / "receipt.json"),
        "NODE27_TIMESERIES_RETENTION_LOCK_PATH": str(tmp_path / "runner.lock"),
    }


def test_retention_lane_lock_error_releases_lifecycle_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts import node27_timeseries_retention as retention

    owned = _owned_lifecycle_fd(tmp_path)
    monkeypatch.setattr(retention, "acquire_timeseries_lifecycle_lock", lambda *_args, **_kwargs: owned)

    def release(fd: int) -> None:
        os.close(fd)

    monkeypatch.setattr(retention, "release_timeseries_lifecycle_lock", release)

    def boom(_path: Path) -> int | None:
        raise retention.RetentionConfigError("lock file must be a mode-0600 regular file")

    monkeypatch.setattr(retention, "acquire_lock", boom)
    for key, value in _retention_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    code = retention.main(argv=[], now=datetime(2026, 7, 11, 12, tzinfo=UTC))
    assert code == 2
    with pytest.raises(OSError):
        os.fstat(owned)
    err = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert err["code"] == retention.CODE_RETENTION_CONFIG_INVALID
    assert not Path(_retention_env(tmp_path)["NODE27_TIMESERIES_RETENTION_RECEIPT_PATH"]).exists()


def test_retention_lane_lock_contention_releases_lifecycle_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts import node27_timeseries_retention as retention

    owned = _owned_lifecycle_fd(tmp_path)
    monkeypatch.setattr(retention, "acquire_timeseries_lifecycle_lock", lambda *_args, **_kwargs: owned)

    def release(fd: int) -> None:
        os.close(fd)

    monkeypatch.setattr(retention, "release_timeseries_lifecycle_lock", release)
    monkeypatch.setattr(retention, "acquire_lock", lambda *_args, **_kwargs: None)
    env = _retention_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    code = retention.main(argv=[], now=datetime(2026, 7, 11, 12, tzinfo=UTC))
    assert code == 1
    with pytest.raises(OSError):
        os.fstat(owned)
    receipt = json.loads(Path(env["NODE27_TIMESERIES_RETENTION_RECEIPT_PATH"]).read_text(encoding="utf-8"))
    assert receipt["outcome"] == "refused"
    assert receipt["refusal_reason"] == retention.CODE_RETENTION_CONCURRENT_INVOCATION
    err = capsys.readouterr().err
    assert retention.CODE_RETENTION_CONCURRENT_INVOCATION in err
