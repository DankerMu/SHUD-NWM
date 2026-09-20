"""Shared fixtures, seams and literals for the state-index copyback replay suites.

Non-collectible support module (#1611): the single home of every definition the
``tests/test_scheduler_state_index_copyback_replay_*.py`` partitions share, so no
partition can drift from another's copy. Every definition below was moved
verbatim out of the pre-split ``tests/test_scheduler_state_index_copyback_replay.py``;
the module header is the only thing written for the split.

The two pytest fixtures (``fixture``, ``private_umask_fixture``) are registered by
IMPORT: each partition imports the factory name so pytest sees the fixture marker
on its own module namespace. An unused-looking import of
``private_umask_fixture_factory`` carries a ``noqa: F401`` for exactly that reason.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common import provider_atomic, safe_fs
from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.safe_fs import SafeFilesystemError
from packages.common.state_manager import publish_state_snapshot_index
from scripts import scheduler_state_index_copyback_replay as replay

PREFIX = "s3://nhms"
AUTHORITATIVE_RUN = "fcst_gfs_2026072000_model_a"
IFS_RUN = "fcst_ifs_2026072000_model_a"
HISTORICAL_RUN = "fcst_gfs_2026070500_model_a"


def _inject_alias_identity(monkeypatch: pytest.MonkeyPatch, *roots: Path) -> None:
    """Make `roots` report one filesystem identity through the guard's probe.

    Patched on the replay module's own namespace, which is where the guard
    resolves the name -- patching `packages.common.safe_fs` instead would leave
    the production call point untouched.  No portable, root-free construction
    produces two concurrently existing realpaths over one inode (see the honest
    limit in tests/test_safe_fs.py), so the alias is injected at this seam.
    """

    resolved = {root.resolve() for root in roots}
    real_probe = safe_fs.directory_identity_no_follow

    def probe(path: Path) -> tuple[int, int]:
        if Path(path).resolve() in resolved:
            return (0x1192, 0x1192)
        return real_probe(path)

    monkeypatch.setattr(replay, "directory_identity_no_follow", probe)


def _call_without_hanging(call: Any) -> Any:
    """Run `call` on a daemon thread and fail if it has not returned in 5s.

    This is a generic non-return net for the guard path, nothing narrower: if
    the guard ever stops returning -- blocks, spins, waits on any lock -- the
    suite fails in 5s instead of wedging the session.  `daemon=True` is not
    optional -- a non-daemon thread keeps the interpreter alive at exit waiting
    for exactly the thread that never finishes.  A thread stuck here goes on
    holding whatever fd it took for the rest of this pytest session.

    The two caller shapes in this file differ in whether that can actually
    happen, so the bound means different things to each.

    For the **probe-seam** caller
    (`test_replay_refuses_alias_roots_reporting_one_filesystem_identity`) the
    helper cannot reproduce the `fcntl.flock` self-deadlock that motivates the
    guard (provider_atomic.py:221 takes the blocking path without LOCK_NB).
    That deadlock is real in production, where a bind-mount alias makes two
    realpaths name one directory, so the scoped merge locks one lockfile twice.
    There the alias is injected at the probe seam, so on the real filesystem
    the two roots stay genuinely distinct directories; the provider lock is
    path-keyed (`provider_lock_path`, and the in-process gate keys on
    `os.path.abspath`), so even a regressed guard that reaches the merge takes
    two distinct lockfiles and returns.  Measured: under a string-compare
    mutant that test reds in ~0.3s on an ordinary assertion or
    `FileNotFoundError`.  Reproducing the deadlock through that seam would need
    a real bind mount, which has no portable root-free construction (see the
    honest limit in tests/test_safe_fs.py).

    For the **hardlink** caller
    (`test_replay_state_index_lock_collision_is_a_refusal_not_an_uncertain_commit`)
    it is the other way round: nothing is injected, `os.link` puts the
    destination lockfile on the source inode, so the two lock names reach one
    file and the blocking `flock` genuinely self-deadlocks.  The 5s join is a
    real tripwire there and must not be removed.  Measured under the branch-B
    deletion mutant (state_manager.py:1980-1983 -> `return`): that test and its
    `run_tree_copyback` sibling both consume the whole join budget -- `2 failed
    in 10.28s` with four hang-regression occurrences and zero
    `provider_lock_parent_unsafe`, against a `2 passed in 0.45s` pristine
    baseline.  pyproject.toml carries no `pytest-timeout` and no `addopts`, so
    this `thread.join(5.0)` is the only bound in the process: drop it and a
    regression wedges the local session and burns the CI unit-test job's full
    `timeout-minutes: 35`.
    """

    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["value"] = call()
        except BaseException as error:  # noqa: BLE001 -- re-raised on the caller's thread
            outcome["error"] = error

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(5.0)
    if thread.is_alive():
        pytest.fail("hang regression: the same-root guard did not return within 5s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.reference_root = root / "object-store"
        self.destination_root = root / "shared-object-store"
        self.receipt_root = root / "receipts"
        self.receipt_root.mkdir(mode=0o700)
        os.chmod(self.receipt_root, 0o700)
        private_store = LocalObjectStore(self.reference_root, PREFIX)
        self.archived_content = _valid_state_bytes(b"archived")
        self.fresh_content = _valid_state_bytes(b"fresh")
        self.ifs_content = _valid_state_bytes(b"ifs-fresh")
        self.cycleless_content = _valid_state_bytes(b"cycleless")
        archived_uri = private_store.write_bytes_atomic(
            "states/gfs/model_a/archived/state.cfg.ic", self.archived_content
        )
        fresh_uri = private_store.write_bytes_atomic(
            "states/gfs/model_a/fresh/state.cfg.ic", self.fresh_content
        )
        ifs_uri = private_store.write_bytes_atomic(
            "states/ifs/model_a/fresh/state.cfg.ic", self.ifs_content
        )
        cycleless_uri = private_store.write_bytes_atomic(
            "states/gfs/model_b/cycleless/state.cfg.ic", self.cycleless_content
        )
        self.archived_entry = _entry(
            state_id="archived-state",
            run_id=HISTORICAL_RUN,
            state_uri=archived_uri,
            content=self.archived_content,
            valid_time="2026-07-05T12:00:00Z",
            created_at="2026-07-05T13:00:00Z",
            cycle_id="gfs_2026070500",
        )
        self.fresh_entry = _entry(
            state_id="fresh-state",
            run_id=AUTHORITATIVE_RUN,
            state_uri=fresh_uri,
            content=self.fresh_content,
            valid_time="2026-07-20T12:00:00Z",
            created_at="2026-07-27T01:00:00Z",
            cycle_id="gfs_2026072000",
        )
        # Production replays both deterministic sources of one cycle time, so
        # the fixture carries a second cycle whose entry is also missing from
        # the shared index.
        self.ifs_entry = _entry(
            state_id="ifs-fresh-state",
            run_id=IFS_RUN,
            state_uri=ifs_uri,
            content=self.ifs_content,
            valid_time="2026-07-20T12:00:00Z",
            created_at="2026-07-27T01:00:00Z",
            cycle_id="ifs_2026072000",
            source_id="ifs",
        )
        self.cycleless_entry = {
            **_entry(
                state_id="cycleless-state",
                run_id="fcst_gfs_2026072000_model_b",
                state_uri=cycleless_uri,
                content=self.cycleless_content,
                valid_time="2026-07-20T12:00:00Z",
                created_at="2026-07-27T01:00:00Z",
                cycle_id="gfs_2026072000",
            ),
            "model_id": "model_b",
            "cycle_id": None,
            "lead_hours": None,
        }
        self.source_index = self.reference_root / "scheduler/state-index/index-last.json"
        self.destination_index = self.destination_root / "scheduler/state-index/index-last.json"
        publish_state_snapshot_index(
            [self.archived_entry, self.fresh_entry, self.ifs_entry, self.cycleless_entry],
            self.source_index,
            object_store_root=self.reference_root,
            object_store_prefix=PREFIX,
            generated_at=datetime(2026, 7, 27, 1, tzinfo=UTC),
        )
        publish_state_snapshot_index(
            [self.archived_entry],
            self.destination_index,
            object_store_root=self.destination_root,
            object_store_prefix=PREFIX,
            generated_at=datetime(2026, 7, 25, 18, tzinfo=UTC),
            verify_objects=False,
        )
        self.archived_shared_object = self.destination_root / "states/gfs/model_a/archived/state.cfg.ic"
        self.new_shared_object = self.destination_root / "states/gfs/model_a/fresh/state.cfg.ic"
        self.ifs_shared_object = self.destination_root / "states/ifs/model_a/fresh/state.cfg.ic"


@pytest.fixture(name="fixture")
def fixture_factory(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def _apply_env(monkeypatch: pytest.MonkeyPatch, fixture: Fixture) -> None:
    monkeypatch.setenv(replay.REFERENCE_ROOT_ENV, str(fixture.reference_root))
    monkeypatch.setenv(replay.DESTINATION_ROOT_ENV, str(fixture.destination_root))
    monkeypatch.setenv(replay.OBJECT_STORE_PREFIX_ENV, PREFIX)
    monkeypatch.setenv(replay.RECEIPT_ROOT_ENV, str(fixture.receipt_root))


def _fail_index_fsync_after_replace(monkeypatch: pytest.MonkeyPatch, fixture: Fixture) -> None:
    """Fail the destination index CAS the way a post-``os.replace`` fsync does.

    ``safe_fs.atomic_write_bytes_no_follow`` marks exactly this window
    ``kind="indeterminate"`` (safe_fs.py:109-123) because the replace already
    happened, and ``provider_atomic.atomic_replace_provider_bytes:317-320`` turns
    that into ``provider_replace_uncertain``.  The real bytes are written first, so
    the shared index genuinely holds the new content.  Only the index CAS goes
    through this seam; checkpoint object copies use ``state_manager``'s own import.
    """

    real_write = provider_atomic.atomic_write_bytes_no_follow

    def write_then_fail_directory_fsync(path: Path, content: bytes, **kwargs: Any) -> Any:
        written = real_write(path, content, **kwargs)
        if Path(path) == fixture.destination_index:
            raise SafeFilesystemError(
                f"Atomic replacement for {path} completed but directory fsync failed",
                kind="indeterminate",
            )
        return written

    monkeypatch.setattr(
        provider_atomic, "atomic_write_bytes_no_follow", write_then_fail_directory_fsync
    )


def _entry(
    *,
    state_id: str,
    run_id: str,
    state_uri: str,
    content: bytes,
    valid_time: str,
    created_at: str,
    cycle_id: str,
    lead_hours: int = 12,
    source_id: str = "gfs",
) -> dict[str, Any]:
    return {
        "state_id": state_id,
        "model_id": "model_a",
        "run_id": run_id,
        "source_id": source_id,
        "valid_time": valid_time,
        "state_uri": state_uri,
        "checksum": f"sha256:{sha256_bytes(content)}",
        "usable_flag": True,
        "created_at": created_at,
        "cycle_id": cycle_id,
        "lead_hours": lead_hours,
    }


def _valid_state_bytes(seed: bytes) -> bytes:
    minute = 27_000_000.0 + (int.from_bytes(seed[:4].ljust(4, b"\x00"), "big") % 1000)
    return (
        f"2\t1\t{minute:.6f}\n"
        "1\t0.1\t0.1\t0.1\t0.1\t0.1\n"
        "2\t0.1\t0.1\t0.1\t0.1\t0.1\n"
        "1\t0.5\n"
    ).encode()


@pytest.fixture(name="private_umask_fixture")
def private_umask_fixture_factory(tmp_path: Path) -> Fixture:
    """`fixture`, but built under `umask 0o077` so the lock parents come out private.

    Written for #1609/#1610, when `ensure_directory_no_follow` created the lock
    parent with a bare `os.mkdir` (`0o777 & ~umask`): under an ambient `umask 002`
    that landed 0o775 and `provider_lock_parent_unsafe` (provider_atomic.py:209-210)
    fired in fixture setup -- an error, not a failure, and nothing about the guard
    under test.  #1513 pinned that `os.mkdir` to an explicit 0o755 (safe_fs.py:68),
    so the lock-parent gate no longer depends on the ambient umask and this wrapper
    is no longer load-bearing for it (measured: the suite is green with the wrapper
    neutralized).

    It is kept because it is behavior-neutral -- `0o755 & ~0o077 == 0o700`, the
    same private mode it always produced -- and because it keeps the fixture's
    private-mode posture explicit rather than implicit in safe_fs's pin.  The
    construction sits inside the `try` so a raise cannot leak 0o077 into the rest
    of the session.
    """

    previous_umask = os.umask(0o077)
    try:
        return Fixture(tmp_path)
    finally:
        os.umask(previous_umask)
