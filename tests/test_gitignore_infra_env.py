"""Guard: the repository `.gitignore` keeps real env files out and env templates in.

Moved from ``tests/test_two_node_docker_runtime.py`` (#3231): the ``infra/env/*``
rule outlives the docker/compose lane that file tested.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _check_ignore(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "check-ignore", *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_gitignore_ignores_real_env_files_but_not_examples() -> None:
    ignored = _check_ignore("-v", "infra/env/compute.env", "infra/env/display.env", "infra/env/local.env")
    assert ignored.returncode == 0
    assert "infra/env/*" in ignored.stdout

    # `--no-index` makes git answer from the rules alone: without it a tracked file is
    # reported as not ignored whatever `.gitignore` says. The last path is untracked and
    # stands for a template that does not exist yet.
    for path in (
        "infra/env/display.example",
        "infra/env/compute.scheduler-dbfree.env.example",
        "infra/env/README.md",
        "infra/env/new-role.example",
    ):
        trackable = _check_ignore("--no-index", "-q", path)
        assert trackable.returncode == 1, path
        # The path is kept by a negation rule of the root `.gitignore`, not merely unmatched.
        source, _line, pattern = _check_ignore("--no-index", "-v", path).stdout.split(":", 2)
        assert source == ".gitignore", path
        assert pattern.startswith("!infra/env/"), path
