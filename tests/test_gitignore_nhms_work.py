"""Guard: scheduler work files under `.nhms-work/` are ignored by the repo `.gitignore`.

Contract: openspec/changes/node22-config-hygiene task 4.1 and spec
`node22-env-hygiene` scenario "Scheduler work files do not appear in status".
`core.excludesFile=/dev/null` keeps a developer's global excludes from masking
a missing repository rule.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

NESTED_WORK_FILE = ".nhms-work/scheduler/basins-file-registry-publish/basins-inventory.json"


def _check_ignore(path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.excludesFile=/dev/null", "check-ignore", "-v", path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def _assert_inside_git_checkout() -> None:
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0 and probe.stdout.strip() == "true", (
        f"{REPO_ROOT} is not a git checkout; this guard must run inside one "
        f"(rc={probe.returncode}, stdout={probe.stdout!r}, stderr={probe.stderr!r})"
    )


def test_nested_scheduler_work_file_is_ignored_by_repo_gitignore() -> None:
    _assert_inside_git_checkout()
    result = _check_ignore(NESTED_WORK_FILE)
    assert result.returncode == 0, (
        f"expected {NESTED_WORK_FILE} to be ignored "
        f"(rc={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r})"
    )
    # `-v` format: "<source>:<linenum>:<pattern>\t<pathname>"; line number is not pinned.
    match_info, _, matched_path = result.stdout.rstrip("\n").partition("\t")
    source, _, rest = match_info.partition(":")
    _linenum, _, pattern = rest.partition(":")
    assert source == ".gitignore", result.stdout
    assert pattern == ".nhms-work/", result.stdout
    assert matched_path == NESTED_WORK_FILE, result.stdout


@pytest.mark.parametrize("sibling", [".nhms-work.md", ".nhms-workx/y"])
def test_same_prefix_siblings_are_not_ignored(sibling: str) -> None:
    _assert_inside_git_checkout()
    result = _check_ignore(sibling)
    # 1 == "not ignored"; 128 is a git error and must not pass as "not ignored".
    assert result.returncode == 1, (
        f"expected {sibling} to be not ignored with exit 1 "
        f"(rc={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r})"
    )
