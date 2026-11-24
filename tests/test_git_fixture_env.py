"""#3349: tmp fixture repositories must not inherit the caller's redirecting git environment.

The fixture builders (``_init_git`` / ``_commit_all`` and
``_round14_clean_writer_checkout``) run with ``GIT_INDEX_FILE``, ``GIT_DIR`` and
``GIT_WORK_TREE`` pointed at a sentinel. The sentinel must come out byte-identical
(or still absent), and each fixture repository must own its index and a HEAD.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.entropy_audit_helpers import _commit_all, _init_git, _write
from tests.gateway_reconcile_writer_helpers import _round14_clean_writer_checkout
from tests.git_fixture_env import (
    REDIRECTING_GIT_ENVIRONMENT,
    isolated_git_env,
    run_fixture_git,
)

_SENTINEL_VARIABLES = ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE")


def _caller_repository(root: Path) -> Path:
    """A second repository standing in for the caller, with one indexed file."""

    root.mkdir()
    run_fixture_git(root, "init", "-q")
    (root / "caller.txt").write_text("caller entry\n", encoding="utf-8")
    run_fixture_git(root, "add", "caller.txt")
    return root


def _snapshot(paths: dict[str, Path]) -> dict[str, bytes | None]:
    return {name: path.read_bytes() if path.is_file() else None for name, path in paths.items()}


def _fixture_head(repository: Path) -> str:
    return run_fixture_git(repository, "rev-parse", "HEAD", capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize("sentinel", ["existing-caller-repository", "absent-paths"])
def test_fixture_repository_helpers_ignore_the_callers_redirecting_git_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sentinel: str
) -> None:
    if sentinel == "existing-caller-repository":
        caller = _caller_repository(tmp_path / "caller")
        redirect = {
            "GIT_INDEX_FILE": caller / ".git" / "index",
            "GIT_DIR": caller / ".git",
            "GIT_WORK_TREE": caller,
        }
        watched = {"index": caller / ".git" / "index", "head": caller / ".git" / "HEAD"}
    else:
        absent = tmp_path / "absent"
        redirect = {
            "GIT_INDEX_FILE": absent / "index",
            "GIT_DIR": absent / "git-dir",
            "GIT_WORK_TREE": absent / "work-tree",
        }
        watched = {"index": absent / "index"}
    before = _snapshot(watched)
    if sentinel == "existing-caller-repository":
        assert before["index"]
    for name, value in redirect.items():
        monkeypatch.setenv(name, str(value))

    entropy_repo = tmp_path / "entropy"
    _write(entropy_repo / "docs" / "fixture.md", "entropy fixture\n")
    _init_git(entropy_repo)
    _commit_all(entropy_repo, "entropy fixture")
    writer_checkout, generation = _round14_clean_writer_checkout(tmp_path / "writer", content="A\n")

    assert _snapshot(watched) == before
    if sentinel == "absent-paths":
        assert not (tmp_path / "absent").exists()
    for repository in (entropy_repo, writer_checkout):
        assert (repository / ".git" / "index").is_file()
        assert len(_fixture_head(repository)) == 40
    assert generation == _fixture_head(writer_checkout)
    assert run_fixture_git(entropy_repo, "ls-files", capture_output=True, text=True).stdout.split() == [
        "docs/fixture.md"
    ]
    # The variables are still set for the rest of the test: only the fixture
    # subprocesses were isolated, nothing was stripped from os.environ.
    assert {name: os.environ[name] for name in redirect} == {name: str(value) for name, value in redirect.items()}


def test_isolated_git_env_drops_every_git_variable_and_ignores_global_and_system_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (*REDIRECTING_GIT_ENVIRONMENT, "GIT_CEILING_DIRECTORIES", "GIT_CONFIG_GLOBAL"):
        monkeypatch.setenv(name, "/caller/value")
    monkeypatch.setenv("NHMS_UNRELATED_VARIABLE", "kept")

    env = isolated_git_env({"GIT_AUTHOR_NAME": "overlay"})

    assert {name for name in env if name.startswith("GIT_")} == {
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_AUTHOR_NAME",
    }
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_SYSTEM"] == os.devnull
    assert env["GIT_AUTHOR_NAME"] == "overlay"
    assert env["NHMS_UNRELATED_VARIABLE"] == "kept"
    assert os.environ["GIT_INDEX_FILE"] == "/caller/value"


def test_isolated_git_environ_is_opt_in_and_removes_only_the_redirecting_variables(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Not autouse: REPO_ROOT scans in other tests must keep honouring GIT_INDEX_FILE.
    assert "isolated_git_environ" not in request.fixturenames
    for name in REDIRECTING_GIT_ENVIRONMENT:
        monkeypatch.setenv(name, "/caller/value")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", "/caller/ceiling")

    request.getfixturevalue("isolated_git_environ")

    assert set(REDIRECTING_GIT_ENVIRONMENT) == {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    }
    assert not any(name in os.environ for name in REDIRECTING_GIT_ENVIRONMENT)
    assert os.environ["GIT_CEILING_DIRECTORIES"] == "/caller/ceiling"
