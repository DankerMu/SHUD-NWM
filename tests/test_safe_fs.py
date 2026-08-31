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

The file also carries safe_fs's **directory-mode determinism** cases (#1513) --
see the section comment below.  Those live here rather than beside the
provider_atomic coverage in tests/test_scheduler_file_provider_refresh.py
because scripts/select_ci_tests.py routes packages/common/safe_fs.py to THIS
suite and not to that one, so a safe_fs-only change would otherwise never run
them in the PR lane.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from packages.common.provider_atomic import provider_destination_lock
from packages.common.safe_fs import (
    SafeFilesystemError,
    directory_identity_no_follow,
    ensure_directory_no_follow,
    prove_named_entry_absent_durable,
    read_bytes_durable_no_follow,
    read_bytes_limited_no_follow,
    rmtree_no_follow,
    unlink_no_follow_durable,
)


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


# Directory-mode determinism (#1513).
#
# `ensure_directory_no_follow` used to call `os.mkdir` with no mode, so the
# landed permission was `0o777 & ~umask` -- a function of the ambient
# environment rather than of the code.  `provider_atomic`'s lock gate is
# fail-closed on any `0o022` bit in the lock's direct parent, so on a
# umask-0002 host (node-27, the project's backend pytest oracle) every
# safe_fs-created lock parent landed `0o775` and was refused.
#
# Both sides are pinned below on purpose: the repository's pre-existing umask
# tests all pin the STRICT side, which is precisely the coverage shape that let
# the permissive-side bug survive.  The `0o077` case is the guard against the
# tempting "fix" of adding an `fchmod` after `mkdir` -- that would clear the
# umask's influence in BOTH directions and silently widen `0o700` to `0o755`.


def test_ensure_directory_pins_its_mode_under_a_permissive_umask(tmp_path: Path) -> None:
    target = tmp_path / "permissive" / "child"

    previous_umask = os.umask(0o002)
    try:
        ensure_directory_no_follow(target)
    finally:
        os.umask(previous_umask)

    # Both components are safe_fs-created; the intermediate one matters just as
    # much, because that is the one a lock's direct parent usually is.
    for created in (target.parent, target):
        landed = stat.S_IMODE(created.stat().st_mode)
        assert landed == 0o755, f"{created} landed {landed:#o}"
        assert landed & 0o022 == 0


def test_ensure_directory_is_not_widened_under_a_restrictive_umask(tmp_path: Path) -> None:
    # The umask may further RESTRICT a safe_fs directory; it may never loosen
    # it.  0o755 & ~0o077 == 0o700, byte-identical to the mode-less behavior.
    target = tmp_path / "restrictive" / "child"

    previous_umask = os.umask(0o077)
    try:
        ensure_directory_no_follow(target)
    finally:
        os.umask(previous_umask)

    for created in (target.parent, target):
        landed = stat.S_IMODE(created.stat().st_mode)
        assert landed == 0o700, f"{created} landed {landed:#o}"


def test_provider_lock_acquires_under_a_safe_fs_parent_created_at_a_permissive_umask(
    tmp_path: Path,
) -> None:
    # The permissive-side twin of
    # tests/test_scheduler_file_provider_refresh.py's
    # test_provider_atomic_publishes_shared_mode_under_private_umask, and the
    # end-to-end shape of the bug: the gate is unchanged, the parent's mode is
    # what changed.
    destination = tmp_path / "provider" / "locks" / "manifest.json"

    previous_umask = os.umask(0o002)
    try:
        parent = ensure_directory_no_follow(destination.parent)
        with provider_destination_lock(destination):
            pass
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(parent.stat().st_mode) & 0o022 == 0


def test_ensure_directory_leaves_an_existing_directory_mode_alone(tmp_path: Path) -> None:
    # Forward-only: the helper never chmods a prefix it did not create, so
    # directories that predate this change keep their mode and no migration is
    # implied.  Callers such as
    # `state_manager._ensure_copyback_state_parent` rely on exactly this to own
    # the widening themselves.
    existing = tmp_path / "existing"
    existing.mkdir()
    os.chmod(existing, 0o775)

    ensure_directory_no_follow(existing)

    assert stat.S_IMODE(existing.stat().st_mode) == 0o775
# --- Undeterminable home directory (#1547) -----------------------------------
#
# `Path.expanduser()` throws a bare, errno-less RuntimeError when no home
# directory can be determined.  `_expand_path` is the shared prelude of every
# public entry point here, so that throw used to defeat the module's error
# contract on all of them at once: `SafeFilesystemError` IS a `RuntimeError`
# subclass but not the reverse, so `except SafeFilesystemError` callers missed
# it and `error.kind` readers got an AttributeError.

_UNKNOWN_HOME = "~nosuchuser_zz"


@pytest.mark.parametrize("entry", ["write", "read", "delete"])
def test_undeterminable_home_is_a_structured_unsafe_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
) -> None:
    # cwd is pinned explicitly because `_expand_path` anchors relative results at
    # `Path.cwd()`: a regression that kept the literal `~...` component instead
    # of refusing would otherwise create it in the repository working tree.
    monkeypatch.chdir(tmp_path)
    target = Path(_UNKNOWN_HOME) / "lane" / "leaf"
    calls = {
        "write": lambda: ensure_directory_no_follow(target),
        "read": lambda: read_bytes_limited_no_follow(target, max_bytes=1024),
        "delete": lambda: rmtree_no_follow(target),
    }

    with pytest.raises(SafeFilesystemError) as excinfo:
        calls[entry]()

    assert excinfo.value.kind == "unsafe"
    assert list(tmp_path.iterdir()) == []
    assert not list(tmp_path.glob("~*"))


def test_undeterminable_home_refusal_is_not_a_bare_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The directionality pin: a caller that only knows the module's own error
    # type still catches this, which is what makes the change a pure narrowing.
    monkeypatch.chdir(tmp_path)
    caught: SafeFilesystemError | None = None
    try:
        ensure_directory_no_follow(Path(_UNKNOWN_HOME) / "lane")
    except SafeFilesystemError as error:
        caught = error

    assert caught is not None
    assert isinstance(caught, RuntimeError)
    assert type(caught) is not RuntimeError
    assert list(tmp_path.iterdir()) == []


def test_environment_file_lane_reports_configuration_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Escape surface A: the refresh CLI catches SafeFilesystemError already, so
    # the narrowed throw turns an operator-facing traceback into its own
    # structured rejection with no change on that side.
    from scripts import scheduler_file_provider_refresh as refresh

    monkeypatch.chdir(tmp_path)

    with pytest.raises(refresh.RefreshError) as excinfo:
        refresh._apply_environment_file(Path(_UNKNOWN_HOME) / "scheduler.env")

    assert excinfo.value.reason == "configuration_invalid"
    assert list(tmp_path.iterdir()) == []


def test_met_evidence_lane_reports_a_structured_evidence_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Escape surface B, spot check.  The end-to-end closure of #1622 is covered
    # by tests/test_production_met_validation.py, which exercises
    # `ProductionMetConfig.from_env` and `validate_met` directly; this spot
    # check pins the writer's own containment-gate refusal.
    from services.production_closure.met_validation import (
        EvidenceWriter,
        ProductionMetValidationError,
    )

    monkeypatch.chdir(tmp_path)
    evidence_root = Path(_UNKNOWN_HOME) / "evidence"
    writer = EvidenceWriter(evidence_root, evidence_root / "met")

    with pytest.raises(ProductionMetValidationError) as excinfo:
        writer.prepare()

    assert excinfo.value.error_code.startswith("PRODUCTION_MET_EVIDENCE_")
    assert list(tmp_path.iterdir()) == []


def test_unlink_no_follow_durable_fsyncs_parent(tmp_path: Path) -> None:
    target = tmp_path / "sidecar.json"
    target.write_text("x", encoding="utf-8")
    unlink_no_follow_durable(target)
    assert not target.exists()


def test_unlink_no_follow_durable_fsync_failure_is_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "sidecar.json"
    target.write_text("x", encoding="utf-8")

    def boom(_fd: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr("packages.common.safe_fs.os.fsync", boom)
    with pytest.raises(SafeFilesystemError) as raised:
        unlink_no_follow_durable(target)
    assert raised.value.kind == "indeterminate"


def test_unlink_no_follow_durable_parent_identity_failure_is_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "sidecar.json"
    target.write_text("x", encoding="utf-8")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise SafeFilesystemError("parent changed")

    monkeypatch.setattr("packages.common.safe_fs._verify_fd_matches_path", boom)
    with pytest.raises(SafeFilesystemError) as raised:
        unlink_no_follow_durable(target)
    assert raised.value.kind == "indeterminate"


def test_durable_absence_proof_fsyncs_and_rechecks_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "sidecar.json"
    calls: list[int] = []
    original_fsync = os.fsync

    def record_fsync(fd: int) -> None:
        calls.append(fd)
        original_fsync(fd)

    monkeypatch.setattr("packages.common.safe_fs.os.fsync", record_fsync)
    prove_named_entry_absent_durable(target)
    assert len(calls) == 1


def test_durable_absence_proof_parent_recheck_failure_is_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "sidecar.json"
    calls = {"count": 0}
    safe_fs = __import__("packages.common.safe_fs", fromlist=["_verify_fd_matches_path"])
    original_verify = safe_fs._verify_fd_matches_path

    def fail_after_fsync(*args: object, **kwargs: object) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise SafeFilesystemError("parent changed")
        original_verify(*args, **kwargs)

    monkeypatch.setattr("packages.common.safe_fs._verify_fd_matches_path", fail_after_fsync)
    with pytest.raises(SafeFilesystemError) as raised:
        prove_named_entry_absent_durable(target)
    assert raised.value.kind == "indeterminate"


def test_durable_read_fsyncs_and_returns_the_proven_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "receipt.json"
    target.write_bytes(b"{\"receipt\":true}\n")
    calls: list[int] = []
    original_fsync = os.fsync

    def record_fsync(fd: int) -> None:
        calls.append(fd)
        original_fsync(fd)

    monkeypatch.setattr("packages.common.safe_fs.os.fsync", record_fsync)
    assert read_bytes_durable_no_follow(target, max_bytes=1024) == b"{\"receipt\":true}\n"
    assert len(calls) == 1


def test_durable_read_parent_recheck_failure_is_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "receipt.json"
    target.write_text("{}\n", encoding="utf-8")
    calls = {"count": 0}
    safe_fs = __import__("packages.common.safe_fs", fromlist=["_verify_fd_matches_path"])
    original_verify = safe_fs._verify_fd_matches_path

    def fail_after_fsync(*args: object, **kwargs: object) -> None:
        calls["count"] += 1
        if calls["count"] == 3:
            raise SafeFilesystemError("parent changed")
        original_verify(*args, **kwargs)

    monkeypatch.setattr("packages.common.safe_fs._verify_fd_matches_path", fail_after_fsync)
    with pytest.raises(SafeFilesystemError) as raised:
        read_bytes_durable_no_follow(target, max_bytes=1024)
    assert raised.value.kind == "indeterminate"
