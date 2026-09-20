"""`provider_atomic`'s compare-and-swap, snapshot and destination-lock contracts.

Partition (#1101 partition of the 9614-line / 315-case
tests/test_scheduler_file_provider_refresh.py). Direct coverage of the
primitive layer the refresh lanes sit on: expected-preimage CAS, the three
snapshot divergence refusals, post-read failure restoring validated previous
bytes, durable-replace uncertainty, the non-blocking and cross-process
destination lock, shared-mode publication under a private umask, the
writable-parent and post-flock revalidation gates, and the all-publishers
changed-preimage refusal.

The threaded and subprocess harnesses that exercise the same lock live in
`tests/test_scheduler_refresh_barrier_seam.py` and
`tests/test_scheduler_refresh_terminability_probes.py`.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from packages.common import provider_atomic as provider_atomic_module
from packages.common.provider_atomic import (
    ProviderAtomicError,
    ProviderPreimage,
    atomic_replace_provider_bytes,
    capture_provider_preimage,
    provider_destination_lock,
    read_provider_snapshot,
)
from packages.common.safe_fs import SafeFilesystemError
from packages.common.state_manager import publish_state_snapshot_index
from scripts import scheduler_file_provider_refresh as refresh
from services.orchestrator.scheduler_file_providers import (
    capture_scheduler_provider_preimage,
    publish_canonical_readiness_index,
    publish_scheduler_registry_manifest,
)
from tests.provider_mode_helpers import write_provider_destination


def test_provider_atomic_expected_preimage_preserves_concurrent_update(tmp_path: Path) -> None:
    destination = tmp_path / "index-last.json"
    # Seeded at SHARED_PROVIDER_MODE, not the ambient umask (#1513): under umask
    # 0002 a bare write_bytes lands 0o664 and the publish below raises
    # provider_destination_access_invalid instead of reaching the preimage check
    # this test is about.
    write_provider_destination(destination, b"old")
    expected = capture_provider_preimage(destination, max_bytes=1024)
    destination.write_bytes(b"authoritative-new")
    before = os.stat(destination)

    with pytest.raises(ProviderAtomicError) as error_info:
        atomic_replace_provider_bytes(
            destination,
            b"refresh",
            max_bytes=1024,
            expected_preimage=expected,
        )

    assert error_info.value.reason == "provider_preimage_changed"
    assert destination.read_bytes() == b"authoritative-new"
    after = os.stat(destination)
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_gid,
        after.st_size,
        after.st_mtime_ns,
    )
    assert after_identity == (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_gid,
        before.st_size,
        before.st_mtime_ns,
    )


def test_provider_snapshot_rejects_replacement_between_metadata_and_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "index-last.json"
    destination.write_bytes(b"generation-a")
    seed = os.stat(destination)
    real_read = provider_atomic_module.read_bytes_limited_no_follow
    calls = 0

    # ``read_provider_snapshot`` reads three times through this one name: the
    # ``before`` capture, the payload read, the ``after`` capture.  Firing on
    # call 2 is what puts the replacement strictly BETWEEN the two captures.
    def replace_during_payload_read(*args: object, **kwargs: object) -> bytes:
        nonlocal calls
        calls += 1
        if calls != 2:
            return real_read(*args, **kwargs)
        destination.write_bytes(b"generation-b")
        content = real_read(*args, **kwargs)
        # ABA: restore the bytes, THEN re-stamp the original mtime_ns.  The
        # order is load-bearing — writing after the utime would leave a fresh
        # mtime_ns and make `before != after` fire from the metadata disjunct
        # instead.  This restore is the deterministic stand-in for ext4's 4 ms
        # timestamp tick (CONFIG_HZ=250, measured in #1717): a same-size
        # replacement inside one tick leaves every ProviderPreimage metadata
        # field identical, so the content-digest comparison in
        # `read_provider_snapshot` is the guard's only remaining defense.
        destination.write_bytes(b"generation-a")
        os.utime(destination, ns=(seed.st_atime_ns, seed.st_mtime_ns))
        return content

    monkeypatch.setattr(
        provider_atomic_module, "read_bytes_limited_no_follow", replace_during_payload_read
    )
    with pytest.raises(ProviderAtomicError) as error_info:
        read_provider_snapshot(destination, max_bytes=1024)

    assert error_info.value.reason == "provider_preimage_changed"
    assert destination.read_bytes() == b"generation-a"
    assert calls == 3


def test_provider_snapshot_rejects_replacement_left_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "index-last.json"
    destination.write_bytes(b"generation-a")
    real_read = provider_atomic_module.read_bytes_limited_no_follow
    calls = 0

    # Unlike the ABA test above this one does NOT isolate a single disjunct:
    # with the replacement left in place, both `before != after` and the
    # content-digest comparison fire.  Its value is that the divergence rides
    # on `size`, not on mtime_ns granularity, so it is deterministic on ext4
    # and APFS alike (#1717).
    def replace_during_payload_read(*args: object, **kwargs: object) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 2:
            destination.write_bytes(b"generation-b-longer")
        return real_read(*args, **kwargs)

    monkeypatch.setattr(
        provider_atomic_module, "read_bytes_limited_no_follow", replace_during_payload_read
    )
    with pytest.raises(ProviderAtomicError) as error_info:
        read_provider_snapshot(destination, max_bytes=1024)

    assert error_info.value.reason == "provider_preimage_changed"
    assert calls == 3


def test_provider_snapshot_rejects_metadata_only_divergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1733: isolate the `before != after` disjunct of `read_provider_snapshot`.

    The ABA test above isolates the content-digest disjunct; the left-in-place test
    fires both at once.  Neither one reds when `before != after` is deleted, so the
    metadata comparison had no covering test of its own.  This one changes the
    destination's `mode` between the payload read and the second preimage capture:
    the bytes are never rewritten, so the digest still equals `before.sha256` and
    `size`/`mtime_ns` are untouched (chmod moves `st_ctime`, which
    `ProviderPreimage` does not carry).  `mode` is the divergence field because it
    needs neither privileges nor timestamp granularity -- deterministic on APFS and
    ext4 alike.
    """

    destination = tmp_path / "index-last.json"
    destination.write_bytes(b"generation-a")
    original_mode = stat.S_IMODE(os.stat(destination).st_mode)
    # Clear group/other read rather than add bits: the owner keeps read, so call 3
    # still succeeds and the guard -- not an unreadable-destination error -- is what
    # raises.  0o640 keeps the mode different even if the ambient umask already
    # produced 0o600.
    divergent_mode = 0o600 if original_mode != 0o600 else 0o640
    assert divergent_mode != original_mode
    real_read = provider_atomic_module.read_bytes_limited_no_follow
    calls = 0

    def chmod_after_payload_read(*args: object, **kwargs: object) -> bytes:
        nonlocal calls
        calls += 1
        if calls != 2:
            return real_read(*args, **kwargs)
        # Read the real payload FIRST, then diverge the metadata: the change has to
        # land strictly between the payload read and the `after` capture, so that the
        # bytes handed back are exactly the ones the digest comparison accepts.
        content = real_read(*args, **kwargs)
        os.chmod(destination, divergent_mode)
        return content

    monkeypatch.setattr(
        provider_atomic_module, "read_bytes_limited_no_follow", chmod_after_payload_read
    )
    try:
        with pytest.raises(ProviderAtomicError) as error_info:
            read_provider_snapshot(destination, max_bytes=1024)

        assert error_info.value.reason == "provider_preimage_changed"
        assert error_info.value.phase == "precommit"
        assert calls == 3
        # The bytes never moved, so the digest disjunct cannot be what fired.
        assert destination.read_bytes() == b"generation-a"
        after = os.stat(destination)
        assert stat.S_IMODE(after.st_mode) == divergent_mode
        assert after.st_size == len(b"generation-a")
    finally:
        os.chmod(destination, original_mode)


def test_provider_atomic_postread_failure_restores_validated_previous_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "index-last.json"
    write_provider_destination(destination, b"old")  # SHARED_PROVIDER_MODE, not the umask (#1513)
    expected = capture_provider_preimage(destination, max_bytes=1024)
    real_write = provider_atomic_module.atomic_write_bytes_no_follow
    calls = 0

    def corrupt_first(path: Path, content: bytes, **kwargs: object) -> Path:
        nonlocal calls
        calls += 1
        return real_write(path, b"corrupt" if calls == 1 else content, **kwargs)

    monkeypatch.setattr(provider_atomic_module, "atomic_write_bytes_no_follow", corrupt_first)
    with pytest.raises(ProviderAtomicError) as error_info:
        atomic_replace_provider_bytes(destination, b"new", max_bytes=1024, expected_preimage=expected)

    assert error_info.value.reason == "provider_restored_previous"
    assert destination.read_bytes() == b"old"


def test_provider_atomic_durable_replace_uncertainty_is_not_reported_as_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "index-last.json"
    write_provider_destination(destination, b"old")  # SHARED_PROVIDER_MODE, not the umask (#1513)

    def uncertain(*args: object, **kwargs: object) -> Path:
        del args, kwargs
        raise SafeFilesystemError("directory fsync failed", kind="indeterminate")

    monkeypatch.setattr(provider_atomic_module, "atomic_write_bytes_no_follow", uncertain)
    with pytest.raises(ProviderAtomicError) as error_info:
        atomic_replace_provider_bytes(destination, b"new", max_bytes=1024)

    assert error_info.value.reason == "provider_replace_uncertain"
    assert error_info.value.phase == "replace_uncertain"


def test_provider_destination_lock_contender_is_nonblocking(tmp_path: Path) -> None:
    destination = tmp_path / "manifest-last.json"
    with provider_destination_lock(destination, blocking=False):
        with pytest.raises(ProviderAtomicError) as error_info:
            with provider_destination_lock(destination, blocking=False):
                pass
    assert error_info.value.reason == "provider_already_running"


def test_provider_destination_lock_still_excludes_cross_process_contender(tmp_path: Path) -> None:
    destination = tmp_path / "manifest-last.json"
    repository = Path(__file__).resolve().parents[1]
    contender = """
import sys
from pathlib import Path
from packages.common.provider_atomic import ProviderAtomicError, provider_destination_lock

try:
    with provider_destination_lock(Path(sys.argv[1]), blocking=False):
        print("unexpectedly-acquired")
except ProviderAtomicError as error:
    print(error.reason)
"""

    with provider_destination_lock(destination):
        result = subprocess.run(
            [sys.executable, "-c", contender, str(destination)],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )

    assert result.stdout.strip() == "provider_already_running"


def test_provider_atomic_publishes_shared_mode_under_private_umask(tmp_path: Path) -> None:
    destination = tmp_path / "manifest-last.json"
    previous_umask = os.umask(0o077)
    try:
        atomic_replace_provider_bytes(destination, b"shared", max_bytes=1024)
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o644
    assert destination.stat().st_uid == os.geteuid()


def test_provider_lock_rejects_writable_parent_and_preserves_body_errors(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    with pytest.raises(ProviderAtomicError) as error_info:
        with provider_destination_lock(unsafe / "manifest.json"):
            pass
    assert error_info.value.reason == "provider_lock_parent_unsafe"

    # The 0o775 case (#1513) sits between the two above and is the one that
    # actually bites: it is what a bare `mkdir` lands under umask 0002, so it
    # arrives by accident rather than by an explicit 0o777. The gate stays
    # FAIL-CLOSED on it regardless of who created the directory -- the fix for
    # #1513 pins the creators' modes, it does not relax this check (design D3).
    group_writable = tmp_path / "group-writable"
    group_writable.mkdir(mode=0o775)
    group_writable.chmod(0o775)
    group_writable_destination = group_writable / "manifest.json"
    with pytest.raises(ProviderAtomicError) as group_error_info:
        with provider_destination_lock(group_writable_destination):
            pass
    assert group_error_info.value.reason == "provider_lock_parent_unsafe"
    assert group_error_info.value.phase == "precommit"
    # Refused before the lock inode exists, not after.
    assert not provider_atomic_module.provider_lock_path(group_writable_destination).exists()

    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    with provider_destination_lock(shared / "manifest.json"):
        pass

    with pytest.raises(SafeFilesystemError):
        with provider_destination_lock(tmp_path / "manifest.json"):
            raise SafeFilesystemError("body failure")


def test_provider_lock_revalidates_lock_path_after_flock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "manifest.json"
    lock_path = provider_atomic_module.provider_lock_path(destination)
    real_flock = provider_atomic_module.fcntl.flock
    swapped = False

    def swap_after_lock(fd: int, flags: int) -> None:
        nonlocal swapped
        real_flock(fd, flags)
        if flags & provider_atomic_module.fcntl.LOCK_EX and not swapped:
            swapped = True
            replacement = tmp_path / "replacement.lock"
            replacement.write_bytes(b"")
            os.replace(replacement, lock_path)

    monkeypatch.setattr(provider_atomic_module.fcntl, "flock", swap_after_lock)
    with pytest.raises(ProviderAtomicError) as error_info:
        with provider_destination_lock(destination):
            pass

    assert error_info.value.reason == "provider_lock_changed"


def test_provider_postreplace_read_exception_is_not_precommit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "manifest-last.json"
    write_provider_destination(destination, b"old")  # SHARED_PROVIDER_MODE, not the umask (#1513)
    real_capture = provider_atomic_module.capture_provider_preimage
    calls = 0

    def fail_after_replace(*args: object, **kwargs: object) -> ProviderPreimage:
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise SafeFilesystemError("post-replace read failed")
        return real_capture(*args, **kwargs)

    monkeypatch.setattr(provider_atomic_module, "capture_provider_preimage", fail_after_replace)
    with pytest.raises(ProviderAtomicError) as error_info:
        atomic_replace_provider_bytes(destination, b"new", max_bytes=1024)

    assert error_info.value.phase == "replace_uncertain"
    assert error_info.value.reason == "provider_postread_failed"


@pytest.mark.parametrize("provider", ["registry", "readiness", "state"])
def test_all_provider_publishers_reject_changed_expected_preimage(tmp_path: Path, provider: str) -> None:
    destination = tmp_path / f"{provider}.json"
    generated = refresh.datetime(2026, 7, 14, 12, tzinfo=refresh.UTC)
    if provider == "registry":
        def publisher(expected=None):
            return publish_scheduler_registry_manifest(
                [], destination, generated_at=generated, expected_preimage=expected
            )
    elif provider == "readiness":
        def publisher(expected=None):
            return publish_canonical_readiness_index(
                [], destination, generated_at=generated, expected_preimage=expected
            )
    else:
        def publisher(expected=None):
            return publish_state_snapshot_index(
                [], destination, generated_at=generated, expected_preimage=expected
            )
    previous_umask = os.umask(0o077)
    try:
        publisher()
    finally:
        os.umask(previous_umask)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o644
    expected = capture_scheduler_provider_preimage(destination)
    authoritative = json.loads(destination.read_text())
    authoritative["extra_authoritative_field"] = "new"
    destination.write_text(json.dumps(authoritative))
    preserved = destination.read_bytes()

    with pytest.raises(Exception) as error_info:
        publisher(expected)

    assert getattr(error_info.value, "reason", "") == "provider_preimage_changed"
    assert destination.read_bytes() == preserved
