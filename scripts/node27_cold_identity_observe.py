#!/usr/bin/env python3
"""Read-only host identity observation for the node-27 #1895 rollout.

The rollout needs two host identities that the shipped owners produce and no
operator may re-derive by hand:

* ``installer`` — the mount identity ``major:minor:mount-id:source`` that
  :func:`packages.common.node27_cold_tablespace_host.inspect_host_path` returns
  for the *absent* production child, consumed only by the #1894 installer's
  ``--expected-device-identity``; and
* ``runner`` — the descriptor identity ``st_dev:st_ino`` that
  :func:`packages.common.compressed_chunk_cold_target.inspect_host_path` returns
  for the *created* directory, consumed only by
  ``NODE27_COLD_RESIDENCY_DEVICE_IDENTITY``.

The two formats differ on purpose and the values may differ.  This helper is a
pure reader: it imports the two shipped observers, never reimplements them,
never touches Docker, the database, or any target preflight, never imports a
probe-private module, and never creates, changes, or removes a path.  Each mode
calls exactly one observer, so an observer rename fails this command rather than
silently substituting another.  It also refuses a dirty worktree or an
unobservable HEAD, because an identity captured off unreviewed code cannot
authorize an install.

Output is one line on stdout: ``<lane> <identity>`` for the caller to capture
into a shell variable.  A refusal prints a stable class/stage pair to stderr and
exits non-zero, so an empty or wrong-format value can never reach installer argv
or the runner env.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_INSTALLER_PATTERN = re.compile(r"^[0-9]+:[0-9]+:[0-9]+:.+$")
_RUNNER_PATTERN = re.compile(r"^[0-9]+:[0-9]+$")
_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")

LANE_INSTALLER = "installer"
LANE_RUNNER = "runner"


class IdentityObserveError(Exception):
    """A stable, non-secret refusal of one identity observation."""

    def __init__(self, message: str, *, error_class: str = "identity", stage: str = "observe") -> None:
        super().__init__(message)
        self.error_class = error_class
        self.stage = stage


def observe_head(repo_root: Path | None = None) -> tuple[str | None, bool, bool]:
    """Exact HEAD, HEAD-observed, worktree-dirty (same shape as the runner)."""

    root = Path(__file__).resolve().parents[1] if repo_root is None else repo_root
    try:
        parsed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        unstaged = subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], cwd=root, check=False, timeout=10)
        staged = subprocess.run(["git", "diff", "--quiet", "--cached", "--"], cwd=root, check=False, timeout=10)
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        raise IdentityObserveError(
            "cannot bind the observation to repository HEAD",
            error_class="head",
            stage="freeze_head",
        ) from error
    head_sha = parsed.stdout.strip()
    observed = parsed.returncode == 0 and _HEAD_RE.fullmatch(head_sha) is not None
    dirty = bool(unstaged.returncode or staged.returncode or (untracked.stdout or "").strip())
    if not observed:
        return None, False, dirty
    return head_sha, True, dirty


def observe_installer_identity(
    *,
    head_sha: str,
    host_path: str | None = None,
) -> str:
    """Mount identity of the installer's contracted host path via the owner.

    With no ``host_path`` the shipped production identity contract supplies it,
    so the operator cannot point the observer at an arbitrary path.  A caller
    that passes a path must pass exactly the contracted one.
    """

    del head_sha
    from packages.common.node27_cold_tablespace_host import (
        PRODUCTION_IDENTITY,
        ColdHostError,
        inspect_host_path,
    )

    identity = PRODUCTION_IDENTITY
    path = identity.host_path if host_path is None else Path(host_path)
    if path != identity.host_path:
        raise IdentityObserveError(
            "host path must equal the immutable installer contract",
            error_class="path_alias",
            stage="observe",
        )
    try:
        observed = inspect_host_path(path, identity=identity)
    except (ColdHostError, OSError, RuntimeError) as error:
        raise IdentityObserveError(
            f"installer host path observation failed ({type(error).__name__})",
            error_class="host_path",
            stage="observe",
        ) from error
    device_identity = str(observed.get("device_identity") or "")
    if not _INSTALLER_PATTERN.fullmatch(device_identity):
        raise IdentityObserveError(
            "installer mount identity has the wrong format",
            error_class="device_identity",
            stage="observe",
        )
    # The installer consumes only the identity of the *absent* production child:
    # a pre-existing directory is already someone else's target and must refuse
    # rather than be reported as the pre-install mount identity.
    if observed.get("exists") is not False:
        raise IdentityObserveError(
            "installer host path already exists",
            error_class="device_identity",
            stage="observe",
        )
    if observed.get("is_symlink") is not False:
        raise IdentityObserveError(
            "installer host path must not be a symlink",
            error_class="path_alias",
            stage="observe",
        )
    return device_identity


def observe_runner_identity(
    *,
    head_sha: str,
    host_path: str | None = None,
) -> str:
    """Descriptor identity of the created cold directory via the target owner."""

    del head_sha
    # The runner host path constant lives on the target inspector owner, not the
    # movement runtime module: importing the latter would load the runtime's
    # target-preflight re-exports and movement machinery, which this pure reader
    # must never touch.
    from packages.common.compressed_chunk_cold_target import (
        HOST_COLD_PATH,
        ColdRuntimeError,
        inspect_host_path,
    )

    path = str(HOST_COLD_PATH) if host_path is None else host_path
    if path != str(HOST_COLD_PATH):
        raise IdentityObserveError(
            "host path must equal the immutable runner contract",
            error_class="path_alias",
            stage="observe",
        )
    try:
        identity = inspect_host_path(path)
    except (ColdRuntimeError, OSError, RuntimeError) as error:
        raise IdentityObserveError(
            f"runner host path observation failed ({type(error).__name__})",
            error_class="host_path",
            stage="observe",
        ) from error
    device_identity = str(identity.device_identity)
    if not _RUNNER_PATTERN.fullmatch(device_identity):
        raise IdentityObserveError(
            "runner descriptor identity has the wrong format",
            error_class="device_identity",
            stage="observe",
        )
    return device_identity


_LANES = {
    LANE_INSTALLER: observe_installer_identity,
    LANE_RUNNER: observe_runner_identity,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only cold-tablespace host identity observation (#1895).")
    parser.add_argument("lane", choices=(LANE_INSTALLER, LANE_RUNNER))
    parser.add_argument("--host-path", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--repo-root", default=None, help=argparse.SUPPRESS)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    head_observer: Any = None,
) -> int:
    args = _parser().parse_args(argv)
    observer = _LANES[args.lane]
    try:
        head_fn = observe_head if head_observer is None else head_observer
        head_sha, observed, dirty = (
            head_fn() if head_observer is not None else head_fn(Path(args.repo_root) if args.repo_root else None)
        )
        if not observed:
            raise IdentityObserveError(
                "repository HEAD is not observable",
                error_class="head",
                stage="freeze_head",
            )
        if dirty:
            raise IdentityObserveError(
                "worktree differs from repository HEAD",
                error_class="head",
                stage="freeze_head",
            )
        identity = observer(head_sha=str(head_sha), host_path=args.host_path)
    except IdentityObserveError as error:
        print(f"{args.lane}: {error.error_class} ({error.stage})", file=sys.stderr)
        return 2
    except (TypeError, ValueError, KeyError):
        print(f"{args.lane}: identity (observation)", file=sys.stderr)
        return 2
    print(f"{args.lane} {identity}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
