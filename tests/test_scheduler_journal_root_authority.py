"""#1943: the db-free scheduler verifies its journal root at construction.

A symlink in ANY ancestor of ``NHMS_SCHEDULER_JOURNAL_ROOT`` passes the db-free
required-path preflight (which only judges the leaf and its direct parent) and
then makes every hardened journal read a blocked row with a diagnostic that
names neither the symlink nor the remedy.  These tests pin both halves: the
preflight still says "no blocker" (the trap is real and unchanged -- #1627 owns
that lane), and ``ProductionScheduler.from_env`` now refuses typed with
``FILE_JOURNAL_INVALID_ROOT`` before any repository read.

Every ``from_env`` case builds its environment from the complete db-free
fixture ``tests.test_production_scheduler._set_db_free_scheduler_env``: the
factory is only reached when the whole db-free preflight passes, so a partial
environment would return ``active_repository=None`` and test nothing.  The
first test in this file is the non-vacuity control for exactly that.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from packages.common.safe_fs import verify_directory_no_follow
from services.orchestrator import cli as cli_module
from services.orchestrator import journal_root_authority, operator_reserved_demotion
from services.orchestrator.chain_types import OrchestratorError
from services.orchestrator.journal_root_authority import (
    JOURNAL_ROOT_INVALID_MESSAGE,
    verify_journal_root_authority,
)
from services.orchestrator.scheduler import ProductionScheduler, ProductionSchedulerConfig
from services.orchestrator.scheduler_config.db_free import _db_free_path_check
from services.orchestrator.scheduler_core import _db_free_orchestration_repository_from_config
from tests.provider_mode_helpers import make_directory_with_explicit_mode
from tests.test_production_scheduler import _set_db_free_scheduler_env

_EXPECTED_STDERR = f"FILE_JOURNAL_INVALID_ROOT: {JOURNAL_ROOT_INVALID_MESSAGE}"


def _tree_snapshot(root: Path) -> dict[str, bytes | None]:
    """Every directory and file under ``root``, without following symlinks."""

    snapshot: dict[str, bytes | None] = {}
    for current, directories, files in os.walk(root, followlinks=False):
        for name in directories:
            path = Path(current) / name
            snapshot[str(path.relative_to(root))] = None
        for name in files:
            path = Path(current) / name
            snapshot[str(path.relative_to(root))] = path.read_bytes()
    return snapshot


def _alias_ancestor_root(monkeypatch: Any, tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """A journal root reached through an explicit ``alias -> real`` symlink.

    The symlink sits TWO levels above the root, so neither the leaf nor its
    direct parent is a link: that is what makes ``_db_free_path_check`` pass
    while every no-follow read fails.  The link is explicit rather than relying
    on the platform's ``/var`` alias, so the case is true on Linux too.

    Both the alias and the real chain live under the fixture's
    ``workspace_root``, which is an allowed root, so containment adjudication is
    untouched by this test.
    """

    _roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path)
    workspace_root = Path(os.environ["WORKSPACE_ROOT"])
    real_base = workspace_root / "real"
    make_directory_with_explicit_mode(real_base)
    real_root = real_base / "scheduler" / "journal"
    make_directory_with_explicit_mode(real_root.parent)
    make_directory_with_explicit_mode(real_root)
    alias_base = workspace_root / "alias"
    alias_base.symlink_to(real_base, target_is_directory=True)
    alias_root = alias_base / "scheduler" / "journal"
    monkeypatch.setenv("NHMS_SCHEDULER_JOURNAL_ROOT", str(alias_root))
    del paths
    return alias_root, real_root, alias_base, real_base


# ---------------------------------------------------------------------------
# Non-vacuity control: the fixture really does reach the factory
# ---------------------------------------------------------------------------
def test_db_free_from_env_reaches_the_repository_factory_on_a_real_root(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """Control: with the complete db-free fixture the factory IS reached.

    ``from_env`` returns ``active_repository=None`` for every preflight-blocked
    environment, so without this control the alias test below could pass for the
    wrong reason (a blocked preflight, not the new refusal).
    """

    _roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path)
    config = ProductionSchedulerConfig()

    assert config.db_free_runtime_preflight()["status"] != "blocked"

    scheduler = ProductionScheduler.from_env(config)

    assert scheduler.active_repository is not None
    assert scheduler.active_repository.root == Path(str(paths["NHMS_SCHEDULER_JOURNAL_ROOT"]))


# ---------------------------------------------------------------------------
# The alias-ancestor trap: preflight PASS, construction refusal
# ---------------------------------------------------------------------------
def test_alias_ancestor_root_passes_db_free_path_check(monkeypatch: Any, tmp_path: Path) -> None:
    """Pin of the trap itself: the preflight lane reports no blocker.

    ``_db_free_path_check`` is deliberately unchanged (#1627 adjudicates that
    lane's realpath/ENOENT family).  If this assertion ever flips, the #1943
    factory seam stops being the thing under test here.
    """

    alias_root, real_root, _alias_base, _real_base = _alias_ancestor_root(monkeypatch, tmp_path)
    workspace_root = Path(os.environ["WORKSPACE_ROOT"])

    check, blocker = _db_free_path_check(
        "NHMS_SCHEDULER_JOURNAL_ROOT",
        str(alias_root),
        kind="directory",
        allowed_roots=(workspace_root,),
    )

    assert blocker is None
    assert check["exists"] is True
    assert check["contained"] is True
    assert real_root.is_dir()


def test_alias_ancestor_root_refused_typed_by_from_env(monkeypatch: Any, tmp_path: Path) -> None:
    """The whole point of #1943: refusal at construction, naming the remedy."""

    alias_root, real_root, alias_base, real_base = _alias_ancestor_root(monkeypatch, tmp_path)
    config = ProductionSchedulerConfig()
    assert config.db_free_runtime_preflight()["status"] != "blocked", (
        "the alias root must survive the db-free preflight, otherwise this test would pass for the wrong reason"
    )
    before_real = _tree_snapshot(real_base)

    with pytest.raises(OrchestratorError) as caught:
        ProductionScheduler.from_env(config)

    error = caught.value
    assert error.error_code == "FILE_JOURNAL_INVALID_ROOT"
    assert error.message == JOURNAL_ROOT_INVALID_MESSAGE
    assert error.details["error_type"]
    assert error.details["setting"] == "NHMS_SCHEDULER_JOURNAL_ROOT"
    assert error.details["journal_root"] == str(alias_root)
    assert "readlink -f" in error.message
    assert "real directory" in error.message
    # The constant carries no path, traceback or module name.
    assert str(alias_root) not in error.message
    assert "journal_root_authority" not in error.message
    # Zero bytes on either side of the alias.
    assert _tree_snapshot(real_base) == before_real
    assert list(real_root.iterdir()) == []
    assert list((alias_base / "scheduler" / "journal").iterdir()) == []


def test_realpath_root_at_production_depth_constructs(monkeypatch: Any, tmp_path: Path) -> None:
    """node-22's shape: six real components verify to themselves and construct."""

    _set_db_free_scheduler_env(monkeypatch, tmp_path)
    workspace_root = Path(os.environ["WORKSPACE_ROOT"])
    deep_root = workspace_root
    for segment in ("scratch", "frd", "nhms-prod", "workspace", "scheduler", "journal"):
        deep_root = deep_root / segment
        make_directory_with_explicit_mode(deep_root)
    assert len(deep_root.relative_to(workspace_root).parts) == 6
    monkeypatch.setenv("NHMS_SCHEDULER_JOURNAL_ROOT", str(deep_root))
    config = ProductionSchedulerConfig()

    scheduler = ProductionScheduler.from_env(config)

    verified = verify_journal_root_authority(deep_root, setting="NHMS_SCHEDULER_JOURNAL_ROOT")
    assert scheduler.active_repository is not None
    assert scheduler.active_repository.root == verified
    # Un-resolved: a root that already is a realpath verifies to itself.
    assert verified == verify_directory_no_follow(deep_root)
    assert str(verified) == str(deep_root)


@pytest.mark.parametrize("shape", ["symlink_leaf", "symlink_loop"])
def test_symlink_leaf_and_loop_roots_are_refused_with_the_same_code(
    monkeypatch: Any,
    tmp_path: Path,
    shape: str,
) -> None:
    """A root that is itself a link, and a root in a symlink loop, both refuse.

    Pinned at the repository factory rather than at ``from_env``: unlike the
    alias-ancestor shape, a symlinked LEAF and a symlink loop are exactly what
    ``_db_free_path_check`` does catch, so ``from_env`` short-circuits into the
    preflight-blocked branch (#1627's redacted blocker) and never reaches the
    factory.  The assertion below is the one that would hold for those shapes if
    the preflight ever stopped catching them; the preflight's ownership of them
    today is pinned by the companion test.
    """

    _set_db_free_scheduler_env(monkeypatch, tmp_path)
    workspace_root = Path(os.environ["WORKSPACE_ROOT"])
    if shape == "symlink_leaf":
        target = workspace_root / "journal-target"
        make_directory_with_explicit_mode(target)
        root = workspace_root / "journal-link"
        root.symlink_to(target, target_is_directory=True)
    else:
        loop_a = workspace_root / "loop-a"
        loop_b = workspace_root / "loop-b"
        loop_a.symlink_to(loop_b)
        loop_b.symlink_to(loop_a)
        root = loop_a
    monkeypatch.setenv("NHMS_SCHEDULER_JOURNAL_ROOT", str(root))
    config = ProductionSchedulerConfig()
    assert config.db_free_runtime_preflight()["status"] == "blocked"

    with pytest.raises(OrchestratorError) as caught:
        _db_free_orchestration_repository_from_config(config)

    assert caught.value.error_code == "FILE_JOURNAL_INVALID_ROOT"
    assert caught.value.message == JOURNAL_ROOT_INVALID_MESSAGE
    assert caught.value.details["error_type"]
    assert caught.value.details["setting"] == "NHMS_SCHEDULER_JOURNAL_ROOT"


@pytest.mark.parametrize("shape", ["missing", "symlink_leaf", "symlink_loop"])
def test_preflight_blocked_db_free_pass_builds_no_repository(
    monkeypatch: Any,
    tmp_path: Path,
    shape: str,
) -> None:
    """A blocked db-free pass must not verify -- or even touch -- the root.

    ``from_env``'s blocked branches used to pass ``active_repository=None``,
    which ``__init__`` reads as "not supplied" and answers by calling the
    db-free factory with the very value the preflight had just rejected.  That
    was inert while the factory was a bare constructor; with the #1943
    verification in it, the blocked lane would raise (and for a missing root,
    raise ``TypeError`` out of ``Path(None)``) instead of returning the redacted
    blocker.  The sentinel closes that; this pins it for all three shapes.
    """

    _set_db_free_scheduler_env(monkeypatch, tmp_path)
    workspace_root = Path(os.environ["WORKSPACE_ROOT"])
    if shape == "missing":
        monkeypatch.delenv("NHMS_SCHEDULER_JOURNAL_ROOT", raising=False)
    elif shape == "symlink_leaf":
        target = workspace_root / "blocked-target"
        make_directory_with_explicit_mode(target)
        link = workspace_root / "blocked-link"
        link.symlink_to(target, target_is_directory=True)
        monkeypatch.setenv("NHMS_SCHEDULER_JOURNAL_ROOT", str(link))
    else:
        loop_a = workspace_root / "blocked-loop-a"
        loop_b = workspace_root / "blocked-loop-b"
        loop_a.symlink_to(loop_b)
        loop_b.symlink_to(loop_a)
        monkeypatch.setenv("NHMS_SCHEDULER_JOURNAL_ROOT", str(loop_a))
    config = ProductionSchedulerConfig()
    assert config.db_free_runtime_preflight()["status"] == "blocked"

    scheduler = ProductionScheduler.from_env(config)

    assert scheduler.active_repository is None


# ---------------------------------------------------------------------------
# The refusal reaches the operator on both CLI entrypoints
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_plan_production_surfaces_the_refusal_as_code_message_exit_1(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
) -> None:
    """Under the node-22 oneshot unit this stderr line is what the operator reads."""

    alias_root, real_root, alias_base, real_base = _alias_ancestor_root(monkeypatch, tmp_path)
    before_real = _tree_snapshot(real_base)
    args = ["plan-production", "--dry-run"]

    if entrypoint == "click":
        with pytest.raises(SystemExit) as excinfo:
            cli_module._click_main(args)
        assert excinfo.value.code == 1
    else:
        assert cli_module._argparse_main(args) == 1

    captured = capsys.readouterr()
    assert captured.err.strip() == _EXPECTED_STDERR
    assert captured.out.strip() == ""
    assert "Traceback" not in captured.err
    assert str(alias_root) not in captured.err
    # Zero bytes under either side of the alias.
    assert _tree_snapshot(real_base) == before_real
    assert list(real_root.iterdir()) == []
    assert list((alias_base / "scheduler" / "journal").iterdir()) == []


# ---------------------------------------------------------------------------
# One seam, two callers
# ---------------------------------------------------------------------------
def test_demotion_and_scheduler_share_one_root_authority_seam() -> None:
    """Import identity: there is no second copy of the check anywhere."""

    assert (
        operator_reserved_demotion.verify_journal_root_authority is journal_root_authority.verify_journal_root_authority
    )
