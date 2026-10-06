"""What the provision step accepts as ``--output-registry`` before it does anything (#2753).

A second file beside ``test_provision_direct_grid_dry_run_and_receipt.py``,
whose workspace fixture and helpers it reuses: the scheme of the destination,
and the directory check of ``packages.common.provider_atomic`` held against the
destination lock it predicts.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import scripts.provision_direct_grid_scheduler_registry as provision
from packages.common.provider_atomic import (
    ProviderAtomicError,
    provider_destination_lock,
    provider_destination_parent_problem,
)
from tests.test_provision_direct_grid_dry_run_and_receipt import (
    PREFIX,
    Workspace,
    _refused_having_done_nothing,
    workspace,  # noqa: F401  (the fixture)
)

# --- the scheme of the destination ------------------------------------------


@pytest.mark.parametrize(("registry", "scheme"), [("a:b/registry.json", "a"), ("file:///x/registry.json", "file")])
@pytest.mark.parametrize("flags", [(), ("--apply",)], ids=["dry-run", "apply"])
def test_an_output_registry_with_a_scheme_the_publisher_does_not_support_is_refused(
    workspace: Workspace,  # noqa: F811
    registry: str,
    scheme: str,
    flags: tuple[str, ...],
) -> None:
    message = _refused_having_done_nothing(workspace, "--succession-id", "s-1", *flags, "--output-registry", registry)

    assert registry in message and f"scheme {scheme!r}" in message and "plain path" in message
    assert workspace.database.connections == []
    assert not workspace.receipt_dir.exists()


def test_an_s3_output_registry_is_not_refused_by_the_directory_check(workspace: Workspace) -> None:  # noqa: F811
    registry = f"{PREFIX}/scheduler/direct-grid-candidates/registry.json"

    assert provision.main(workspace.argv("--succession-id", "s-1", "--output-registry", registry)) == 0

    assert len(workspace.database.connections) == 1
    assert workspace.receipt("s-1", "dry-run")["output_registry"]["path"] == registry


# --- the directory check against the lock it predicts -----------------------


def _lock_succeeds(directory: Path) -> bool:
    try:
        with provider_destination_lock(directory / "registry.json"):
            return True
    except ProviderAtomicError:
        return False


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (0o755, None),
        (0o775, "mode 0775"),
        pytest.param(
            0o555, "is not writable", marks=pytest.mark.skipif(os.geteuid() == 0, reason="root ignores modes")
        ),
    ],
    ids=["0755", "0775", "0555"],
)
def test_the_directory_check_agrees_with_the_destination_lock(tmp_path: Path, mode: int, expected: str | None) -> None:
    directory = tmp_path / "out"
    directory.mkdir()
    directory.chmod(mode)

    problem = provider_destination_parent_problem(directory)

    if expected is None:
        assert problem is None
    else:
        assert problem is not None and str(directory) in problem and expected in problem
    assert _lock_succeeds(directory) == (problem is None)
    directory.chmod(0o755)


def test_the_directory_check_names_a_symlinked_component_and_the_lock_refuses_it_too(tmp_path: Path) -> None:
    (tmp_path / "real" / "sub").mkdir(parents=True)
    (tmp_path / "real").chmod(0o755)
    (tmp_path / "real" / "sub").chmod(0o755)
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)

    for directory in (tmp_path / "link", tmp_path / "link" / "sub"):
        problem = provider_destination_parent_problem(directory)

        assert problem is not None and str(tmp_path / "link") in problem and "symlink" in problem
        assert not _lock_succeeds(directory)
    # The same directories reached without the link pass, and so does the lock.
    for directory in (tmp_path / "real", tmp_path / "real" / "sub"):
        assert provider_destination_parent_problem(directory) is None
        assert _lock_succeeds(directory)
