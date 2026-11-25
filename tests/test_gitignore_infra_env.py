"""Guard: the repository `.gitignore` keeps real env files out and env templates in.

Moved unchanged from ``tests/test_two_node_docker_runtime.py`` (#3231): the
``infra/env/*`` rule outlives the docker/compose lane that file tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_gitignore_ignores_real_env_files_but_not_examples() -> None:
    ignored = subprocess.run(
        ["git", "check-ignore", "-v", "infra/env/compute.env", "infra/env/display.env", "infra/env/local.env"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert ignored.returncode == 0
    assert "infra/env/*" in ignored.stdout

    for path in ("infra/env/compute.example", "infra/env/display.example", "infra/env/README.md"):
        trackable = subprocess.run(
            ["git", "check-ignore", "-v", path],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert trackable.returncode == 1
