"""Isolated git environment for tmp fixture repositories (non-collectible module, #3349).

A test that builds its own git repository under ``tmp_path`` must not inherit the
caller's ``GIT_INDEX_FILE`` / ``GIT_DIR`` / ``GIT_WORK_TREE`` (and friends): git
honours them over the cwd, so the fixture's ``git add`` would write into the
caller's index and its ``git ls-files`` would answer from it.

Scope is deliberately narrow. Tests that scan the real checkout (``REPO_ROOT``)
must keep honouring the caller's ``GIT_INDEX_FILE`` -- the evidence procedure
that runs ``git ls-files`` scanners under a temporary index depends on it -- so
nothing here is session-wide or autouse:

* ``isolated_git_env`` / ``run_fixture_git`` isolate one git subprocess.
* ``isolated_git_environ`` (registered as a non-autouse fixture in
  ``tests/conftest.py``) removes the redirecting variables from ``os.environ``
  for the tests that run scanners/writers in-process on a tmp root, or that
  start children inheriting ``os.environ`` there.

The filename does not start with ``test_``: pytest must not collect it.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from packages.common.node27_pgdata_workload_io import REDIRECTING_GIT_ENVIRONMENT

__all__ = [
    "REDIRECTING_GIT_ENVIRONMENT",
    "isolated_git_env",
    "isolated_git_environ",
    "run_fixture_git",
]


def isolated_git_env(overlay: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return ``os.environ`` without any ``GIT_*`` variable and with no global/system config.

    ``overlay`` is applied last (for example a fixture's author/committer identity).
    """

    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    if overlay:
        env.update(overlay)
    return env


def run_fixture_git(
    root: Path | str,
    *args: str,
    env_overlay: Mapping[str, str] | None = None,
    check: bool = True,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """Run ``git *args`` in the tmp fixture repository ``root`` under ``isolated_git_env``.

    Remaining keyword arguments go to ``subprocess.run`` unchanged, so each call
    site keeps its own capture/text handling.
    """

    return subprocess.run(
        ["git", *args],
        cwd=root,
        env=isolated_git_env(env_overlay),
        check=check,
        **kwargs,
    )


@pytest.fixture
def isolated_git_environ(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove the repository-redirecting ``GIT_*`` variables from ``os.environ`` for one test."""

    for name in REDIRECTING_GIT_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    yield
