"""#1955: the one journal-root authority seam on the remaining operator write lanes.

``verify_journal_root_authority`` shipped with a deliberately narrow scope
(demotion + scheduler + census).  Seven further lanes still built a
``FileOrchestrationJournalRepository`` straight from an operator-supplied
``--journal-root``, and none of them is read-only: ``safe_fs`` anchors a
relative path on ``Path.cwd()`` and ``Path("")`` is ``Path(".")``, so a blank or
relative root silently retargets a write lane at the process working directory,
while a symlinked ancestor degrades every read to a blocked row whose diagnostic
never names the symlink.

The sharpest instance was ``_rollback_execution_lock``, which resolved the root
through symlinks: the lock landed on the realpath while the repository was built
on the alias, and a blank root created
``.reconcile-inventory-rollback-execution.lock`` in whatever directory the
operator happened to be standing in.

Every case here therefore asserts three things together -- the typed one-line
refusal, a non-zero exit, and zero bytes anywhere the lane could have written --
plus the mirror-image success case, because a seam that refuses everything is
not an improvement either: a legitimate ``~/journal`` root must still work, and
every later filesystem decision the lane makes (lock path, glob, ``relative_to``
and the receipt's own ``journal_root`` field) must be derived from the SAME
expanded path the repository reads.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.orchestrator import cli as cli_module
from services.orchestrator.file_orchestration_journal import (
    RELEASED_RESERVATION_RECOVERY_COMMAND,
    FileOrchestrationJournalError,
    FileOrchestrationJournalRepository,
)
from services.orchestrator.file_orchestration_migration import (
    ROLLBACK_EXECUTION_LOCK_NAME,
    import_historical_scheduler_state,
)
from services.orchestrator.journal_root_authority import JOURNAL_ROOT_INVALID_MESSAGE
from tests.test_file_orchestration_journal import _released_identity_blocked_master

_ENTRYPOINTS = ("click", "argparse")
_TYPED_LINE = f"FILE_JOURNAL_INVALID_ROOT: {JOURNAL_ROOT_INVALID_MESSAGE}"

#: ``~<user>`` for a user that cannot have a passwd entry: ``Path.expanduser``
#: has nothing to look up and raises a bare ``RuntimeError``.  Unlike ``~/...``
#: this shape ignores ``HOME``, so the case is deterministic under any
#: environment the suite runs in.
UNEXPANDABLE_TILDE_ROOT = "~nhms-no-such-user-7f3a/journal"

#: The four shapes #1955's acceptance names.  ``alias_ancestor`` is the one the
#: db-free preflight passes, so it is the one that used to reach the repository.
#: The two "nothing is there" shapes below are deliberately NOT here: they are
#: parametrized only where refusing them is the contract (see ``missing`` on the
#: rollback lanes and ``missing_under_alias`` on the import lane).
_INVALID_SHAPES = ("blank", "relative", "unexpandable", "alias_ancestor")


def _invalid_root(shape: str, tmp_path: Path) -> str:
    """Build one hostile ``--journal-root`` value, with a real tree behind it.

    The four ``_INVALID_SHAPES`` are built so that the refusal cannot pass for
    the uninteresting reason that the directory is simply missing: the relative
    target exists under the cwd and the alias resolves to a real journal root.
    The two extra shapes are the exception and say so in their own names --
    ``missing`` is a plain absent root (which the rollback lanes must refuse
    rather than create, the #1955 behaviour change) and ``missing_under_alias``
    is an absent root behind a symlinked ancestor (the one input that separates
    design D2's "only a ``FileNotFoundError`` refusal may be answered by
    creating" from "anything absent may be created").
    """

    if shape == "blank":
        return ""
    if shape == "relative":
        return "relative/journal"
    if shape == "unexpandable":
        return UNEXPANDABLE_TILDE_ROOT
    if shape == "missing":
        return str(tmp_path / "absent" / "journal")
    real_base = tmp_path / "real"
    if shape == "missing_under_alias":
        # The ancestor exists and is reached through a symlink; the root itself
        # does not exist.  ``alias`` is what the no-follow walk meets first.
        (real_base / "scheduler").mkdir(parents=True)
    else:
        (real_base / "scheduler" / "journal").mkdir(parents=True)
    alias_base = tmp_path / "alias"
    alias_base.symlink_to(real_base, target_is_directory=True)
    return str(alias_base / "scheduler" / "journal")


def _hostile_cwd(shape: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty working directory the lane must never write into."""

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    if shape == "relative":
        (cwd / "relative" / "journal").mkdir(parents=True)
    monkeypatch.chdir(cwd)
    return cwd


def _tree(root: Path) -> dict[str, bytes | None]:
    """Every directory and file under ``root``; ``None`` marks a directory."""

    snapshot: dict[str, bytes | None] = {}
    for current, directories, files in os.walk(root, followlinks=False):
        for name in directories:
            path = Path(current) / name
            snapshot[str(path.relative_to(root))] = None
        for name in files:
            path = Path(current) / name
            snapshot[str(path.relative_to(root))] = path.read_bytes()
    return snapshot


def _invoke(entrypoint: str, args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    if entrypoint == "click":
        try:
            code = cli_module._click_main(args)
        except SystemExit as exit_error:
            code = int(exit_error.code or 0)
    else:
        code = cli_module._argparse_main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _assert_typed_refusal(code: int, out: str, err: str, *, configured: str) -> None:
    """EF-3: exactly one typed line, exit 2, no traceback, no path.

    Exit 2 exactly, not merely non-zero: the runbook pins 2 for every one of
    these lanes and the demotion template an operator copies pins an exact code,
    so a lane that drifted to 1 would still pass a ``!= 0`` assertion while
    breaking the documented contract.
    """

    assert code == 2
    assert out.strip() == ""
    assert err.strip() == _TYPED_LINE
    assert "Traceback" not in err
    if configured.strip():
        assert configured not in err


def _recovery_args(root: Path | str) -> list[str]:
    return [RELEASED_RESERVATION_RECOVERY_COMMAND, "--journal-root", str(root)]


def _prepare_args(root: Path | str, workspace: Path) -> list[str]:
    return [
        "prepare-file-journal-rollback",
        "--journal-root",
        str(root),
        "--workspace-root",
        str(workspace),
        "--scheduler-state",
        "stopped",
        "--active-scheduler-processes",
        "0",
        "--checked-at",
        "2026-09-14T12:00:00Z",
        "--checked-by",
        "node-22-operator",
        "--target-writer-generation",
        "a" * 40,
    ]


def _launch_args(root: Path | str, workspace: Path, writer_root: Path) -> list[str]:
    return [
        "launch-file-journal-rollback-writer",
        "--journal-root",
        str(root),
        "--workspace-root",
        str(workspace),
        "--receipt-id",
        "receipt-that-does-not-exist",
        "--writer-repository-root",
        str(writer_root),
        "--",
        "plan-production",
        "--submit",
    ]


def _rollforward_args(root: Path | str, workspace: Path) -> list[str]:
    return [
        "complete-file-journal-rollforward",
        "--journal-root",
        str(root),
        "--workspace-root",
        str(workspace),
        "--preparation-receipt-id",
        "receipt-that-does-not-exist",
    ]


def _rollback_args(command: str, root: Path | str, tmp_path: Path) -> list[str]:
    workspace = tmp_path / f"workspace-{command}"
    workspace.mkdir(parents=True, exist_ok=True)
    if command == "prepare":
        return _prepare_args(root, workspace)
    if command == "launch":
        writer_root = tmp_path / f"writer-{command}"
        writer_root.mkdir(parents=True, exist_ok=True)
        return _launch_args(root, workspace, writer_root)
    return _rollforward_args(root, workspace)


_ROLLBACK_COMMANDS = ("prepare", "launch", "rollforward")


# ---------------------------------------------------------------------------
# 1. The released-reservation recovery lane (#1955 task 1.1, 1.6, 1.8)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
@pytest.mark.parametrize("shape", _INVALID_SHAPES)
def test_recovery_command_refuses_a_hostile_root_with_one_typed_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    shape: str,
) -> None:
    """The recovery lane is not read-only: ``--attest`` writes through this root."""

    configured = _invalid_root(shape, tmp_path)
    cwd = _hostile_cwd(shape, tmp_path, monkeypatch)
    before = _tree(cwd)

    code, out, err = _invoke(entrypoint, _recovery_args(configured), capsys)

    _assert_typed_refusal(code, out, err, configured=configured)
    assert _tree(cwd) == before


@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_recovery_command_lists_from_the_tilde_expanded_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    """#1955 acceptance: a legitimate ``~/journal`` root still works.

    A regression pin, not a red test: ``safe_fs._expand_path`` already expanded a
    leading ``~`` before anchoring, which is exactly why the seam must return the
    tilde-expanded, **un-resolved** path -- parity with that prelude.  What this
    asserts is that the lane still reads the same expanded tree the seam
    returned, WITH content in it, so "listed" is not the vacuous zero an empty
    tree would produce from any root.
    """

    home = tmp_path / "home"
    home.mkdir(parents=True)
    # Seed the production wedge (#1748) INSIDE ``$HOME/journal`` so that
    # "listed" is evidence of reading the expanded tree; an empty tree would
    # list zero rows from any root, including the wrong one.
    repository, record = _released_identity_blocked_master(home)
    expanded_root = home / "journal"
    assert repository.root == expanded_root
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)

    code, out, err = _invoke(entrypoint, _recovery_args("~/journal"), capsys)

    assert err == ""
    assert code == 0
    receipt = json.loads(out)
    assert receipt["decision"] == "listed"
    assert receipt["wedged_count"] == 1
    assert receipt["wedged"][0]["job_id"] == str(record["job_id"])


# ---------------------------------------------------------------------------
# 2. The three rollback lanes (#1955 tasks 1.2, 1.4, 1.6, 1.8, 1.9)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
@pytest.mark.parametrize("command", _ROLLBACK_COMMANDS)
@pytest.mark.parametrize("shape", (*_INVALID_SHAPES, "missing"))
def test_rollback_commands_refuse_a_hostile_root_and_take_no_lock(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    command: str,
    shape: str,
) -> None:
    """Task 1.9: no ``.reconcile-inventory-rollback-execution.lock`` anywhere.

    The lock is taken BEFORE the repository is built, so verifying only at the
    constructor would leave the lock itself on an unverified root -- and a blank
    root would still create the lock file in the process working directory,
    because ``Path("") / NAME`` resolves under ``cwd``.

    ``missing`` pins the one BEHAVIOUR CHANGE of #1955 on these three lanes.
    Before it, ``_rollback_execution_lock`` ran ``ensure_directory_no_follow``
    and so CREATED an absent journal root on the way to taking its lock; now
    verification runs first and an absent root is refused like any other invalid
    one, with nothing created.  That is the fail-closed direction -- a rollback
    is a recovery over an EXISTING journal, so an absent root means the operator
    named the wrong tree, and inventing it would hide the typo behind an empty
    successful-looking rollback.
    """

    configured = _invalid_root(shape, tmp_path)
    cwd = _hostile_cwd(shape, tmp_path, monkeypatch)
    before = _tree(cwd)

    code, out, err = _invoke(entrypoint, _rollback_args(command, configured, tmp_path), capsys)

    _assert_typed_refusal(code, out, err, configured=configured)
    assert _tree(cwd) == before
    assert list(tmp_path.rglob(ROLLBACK_EXECUTION_LOCK_NAME)) == []
    if shape == "missing":
        # Not one component of the absent root was created -- neither the root
        # nor the parent `ensure_directory_no_follow` used to mkdir on the way.
        assert not Path(configured).exists()
        assert not Path(configured).parent.exists()


@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_rollback_prepare_and_rollforward_lock_inside_the_tilde_expanded_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    """Task 1.8a: lock path and repository name the same expanded tree.

    ``_rollback_execution_lock`` used to ``.resolve()`` the configured root,
    which both defeated the subsequent no-follow check and split the lock's tree
    from the repository's.  Here the lock must land under the expanded root the
    repository reads.
    """

    home = tmp_path / "home"
    expanded_root = home / "journal"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    expanded_root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    # The migration marker the rollback lanes require is written by the first
    # repository read, exactly as the existing rollback suite does it.
    assert FileOrchestrationJournalRepository(expanded_root).query_reserved_unbound_jobs() == []

    code, out, err = _invoke(entrypoint, _prepare_args("~/journal", workspace), capsys)

    assert err == ""
    assert code == 0
    receipt = json.loads(out)
    assert receipt["status"] == "prepared"
    assert (expanded_root / ROLLBACK_EXECUTION_LOCK_NAME).is_file()
    # No lock anywhere else: not on a resolved alias, not in the cwd.
    assert [path.parent for path in tmp_path.rglob(ROLLBACK_EXECUTION_LOCK_NAME)] == [expanded_root]

    code, out, err = _invoke(
        entrypoint,
        [
            "complete-file-journal-rollforward",
            "--journal-root",
            "~/journal",
            "--workspace-root",
            str(workspace),
            "--preparation-receipt-id",
            receipt["receipt_id"],
        ],
        capsys,
    )

    assert err == ""
    assert code == 0
    assert json.loads(out)["preparation_receipt_id"] == receipt["receipt_id"]
    assert [path.parent for path in tmp_path.rglob(ROLLBACK_EXECUTION_LOCK_NAME)] == [expanded_root]


@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_rollback_launch_locks_inside_the_tilde_expanded_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    """Task 1.8a for the launch lane, which also builds its scheduler config.

    The lane is stopped at the writer-generation gate on purpose: what is under
    test is that the verified root reached the lock and the config, not the old
    writer's exec boundary.  A refusal at that gate is a DIFFERENT typed token,
    which is itself the evidence that the root verified.
    """

    home = tmp_path / "home"
    expanded_root = home / "journal"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    expanded_root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    assert FileOrchestrationJournalRepository(expanded_root).query_reserved_unbound_jobs() == []

    code, out, err = _invoke(
        entrypoint,
        _launch_args("~/journal", workspace, tmp_path / "no-such-writer-checkout"),
        capsys,
    )

    assert code == 2
    assert out.strip() == ""
    assert err.strip() == "file_journal_rollback_writer_generation_unresolvable"
    assert "Traceback" not in err
    assert [path.parent for path in tmp_path.rglob(ROLLBACK_EXECUTION_LOCK_NAME)] == [expanded_root]


# ---------------------------------------------------------------------------
# 3. The create-capable import lane (#1955 task 1.3, 1.10 / design D2)
# ---------------------------------------------------------------------------
_IMPORT_CYCLE_TIME = datetime(2026, 6, 28, tzinfo=UTC)


def test_import_lane_creates_a_fresh_root_through_the_no_follow_creator(tmp_path: Path) -> None:
    """Design D2: the one create-capable lane still creates, then verifies."""

    from tests.test_file_orchestration_migration import _historical_rows

    journal_root = tmp_path / "journal"
    assert not journal_root.exists()

    receipt = import_historical_scheduler_state(
        journal_root=journal_root,
        cutoff_time=_IMPORT_CYCLE_TIME,
        **_historical_rows(_IMPORT_CYCLE_TIME),
    )

    assert journal_root.is_dir()
    assert receipt["imported_row_counts"]["pipeline_jobs"] == 3
    assert FileOrchestrationJournalRepository(journal_root).get_pipeline_job("job_model_a_permanent") is not None


def test_import_lane_creates_under_a_tilde_root_it_expanded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 1.8a: the created tree is the expanded one, never a ``~`` literal."""

    from tests.test_file_orchestration_migration import _historical_rows

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)

    import_historical_scheduler_state(
        journal_root="~/journal",
        cutoff_time=_IMPORT_CYCLE_TIME,
        **_historical_rows(_IMPORT_CYCLE_TIME),
    )

    assert (home / "journal").is_dir()
    assert list(tmp_path.glob("~*")) == []


@pytest.mark.parametrize("shape", (*_INVALID_SHAPES, "missing_under_alias"))
def test_import_lane_refuses_a_hostile_root_without_creating_anything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    """Only ``FileNotFoundError`` may be answered by creating; nothing else.

    ``missing_under_alias`` is the sharpest input design D2 has, and the one the
    other four shapes cannot reach: the root is ABSENT, so a lane that answered
    "absent" with "create it" would pass every other case here, and it sits
    behind a symlinked ancestor, which is the shape the db-free preflight waves
    through.  The no-follow walk meets the symlink before it can report the leaf
    missing, so the refusal is not a ``FileNotFoundError`` and the creator is
    never reached -- nothing appears behind the alias.
    """

    from services.orchestrator.chain_types import OrchestratorError
    from tests.test_file_orchestration_migration import _historical_rows

    configured = _invalid_root(shape, tmp_path)
    cwd = _hostile_cwd(shape, tmp_path, monkeypatch)
    before = _tree(cwd)

    with pytest.raises(OrchestratorError) as caught:
        import_historical_scheduler_state(
            journal_root=configured,
            cutoff_time=_IMPORT_CYCLE_TIME,
            **_historical_rows(_IMPORT_CYCLE_TIME),
        )

    assert caught.value.error_code == "FILE_JOURNAL_INVALID_ROOT"
    assert caught.value.details["setting"] == "--journal-root"
    assert _tree(cwd) == before
    if shape == "missing_under_alias":
        # Neither through the alias nor on the realpath it points at.
        assert not Path(configured).exists()
        assert not (tmp_path / "real" / "scheduler" / "journal").exists()
        assert _tree(tmp_path / "real") == {"scheduler": None}


# ---------------------------------------------------------------------------
# 4. The two operator scripts (#1955 task 1.5, 1.8, 1.8a)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("shape", _INVALID_SHAPES)
def test_manual_retry_script_returns_a_typed_non_zero_from_main(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    """The script has no wrapping handler, so ``main()`` itself must be typed."""

    from scripts.node22_manual_retry_failed_runs import main

    configured = _invalid_root(shape, tmp_path)
    cwd = _hostile_cwd(shape, tmp_path, monkeypatch)
    before = _tree(cwd)

    code = main(
        [
            "--journal-root",
            configured,
            "--run-id",
            "fcst_gfs_2026082300_dg_abc",
            "--reason",
            "forcing backfilled",
            "--requested-by",
            "operator",
        ]
    )

    captured = capsys.readouterr()
    _assert_typed_refusal(code, captured.out, captured.err, configured=configured)
    assert _tree(cwd) == before


def test_manual_retry_script_receipt_names_the_expanded_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 1.8a: every later use of the root is the verified (expanded) value."""

    from scripts.node22_manual_retry_failed_runs import main

    home = tmp_path / "home"
    expanded_root = home / "journal"
    expanded_root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)

    code = main(
        [
            "--journal-root",
            "~/journal",
            "--run-id",
            "fcst_gfs_2026082300_dg_abc",
            "--reason",
            "forcing backfilled",
            "--requested-by",
            "operator",
        ]
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert code == 0
    receipt = json.loads(captured.out)
    assert receipt["journal_root"] == str(expanded_root)
    assert receipt["runs"][0]["preview"]["decision"] == "refused"


@pytest.mark.parametrize("apply_mode", [False, True])
@pytest.mark.parametrize("shape", _INVALID_SHAPES)
def test_placeholder_repair_script_refuses_before_the_dry_run_glob(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    apply_mode: bool,
) -> None:
    """The script globs the raw root even without ``--apply``.

    Verifying only ahead of the repository would leave the dry-run half reading
    the wrong tree and reporting a clean receipt over it.
    """

    from scripts.ops.node22_repair_placeholder_hydro_uris import main

    configured = _invalid_root(shape, tmp_path)
    cwd = _hostile_cwd(shape, tmp_path, monkeypatch)
    object_store = tmp_path / "object-store"
    object_store.mkdir()
    before = _tree(cwd)

    argv = [
        "--journal-root",
        configured,
        "--object-store-root",
        str(object_store),
    ]
    if apply_mode:
        argv.append("--apply")
    monkeypatch.setattr("sys.argv", ["node22_repair_placeholder_hydro_uris.py", *argv])

    code = main()

    captured = capsys.readouterr()
    _assert_typed_refusal(code, captured.out, captured.err, configured=configured)
    assert _tree(cwd) == before


def test_placeholder_repair_script_globs_the_expanded_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 1.8a: the glob AND ``relative_to`` use the same expanded root.

    ``relative_to`` against the unexpanded ``~/journal`` literal raises
    ``ValueError`` for every legitimate tilde root, so a lane that expands for
    the glob but not for the report is still broken.
    """

    from scripts.ops.node22_repair_placeholder_hydro_uris import main

    home = tmp_path / "home"
    expanded_root = home / "journal"
    latest = expanded_root / "latest" / "gfs" / "2026062800" / "model_a.json"
    latest.parent.mkdir(parents=True)
    latest.write_text(
        json.dumps(
            {
                "hydro_run": {
                    "run_id": "fcst_gfs_2026062800_model_a",
                    "log_uri": "[object-uri]",
                    "source_id": "gfs",
                    "cycle_time": "2026-06-28T00:00:00Z",
                    "model_id": "model_a",
                }
            }
        ),
        encoding="utf-8",
    )
    object_store = tmp_path / "object-store"
    object_store.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "node22_repair_placeholder_hydro_uris.py",
            "--journal-root",
            "~/journal",
            "--object-store-root",
            str(object_store),
        ],
    )

    code = main()

    captured = capsys.readouterr()
    assert captured.err == ""
    receipt = json.loads(captured.out)
    # The run manifest is absent on the object store, so the polluted row is
    # SKIPPED -- which is exactly the branch that proves the glob found it and
    # that ``relative_to`` resolved against the same expanded root.
    assert code == 1
    assert receipt["skipped"] == [
        {
            "latest": "latest/gfs/2026062800/model_a.json",
            "run_id": "fcst_gfs_2026062800_model_a",
            "fields": "log_uri",
            "reason": "run_manifest_missing_on_disk",
        }
    ]


def test_rollback_execution_lock_is_derived_from_the_root_it_is_given(tmp_path: Path) -> None:
    """Design D3: no ``.resolve()``, so the lock cannot drift onto a realpath.

    ``resolve()`` follows symlinks, which made the subsequent
    ``ensure_directory_no_follow`` unfalsifiable -- it could only ever be handed
    a realpath -- and split the lock's tree from the repository's.
    """

    from services.orchestrator.file_orchestration_migration import _rollback_execution_lock

    real_root = tmp_path / "real" / "journal"
    real_root.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "real", target_is_directory=True)
    aliased_root = alias / "journal"

    with pytest.raises(FileOrchestrationJournalError) as caught:
        with _rollback_execution_lock(aliased_root):
            pass

    assert caught.value.reason == "file_journal_rollback_execution_lock_unavailable"
    assert not (real_root / ROLLBACK_EXECUTION_LOCK_NAME).exists()


# ---------------------------------------------------------------------------
# 5. migrate-scheduler-state: the refusal it INHERITS renders typed too
# ---------------------------------------------------------------------------
#: The create-capable lane's own failure code, raised when the root is legal but
#: cannot be created.  Distinct from the invalid-root refusal on purpose: the
#: operator's remedy is a permission, not a different path.
_WRITE_FAILED_LINE = "FILE_JOURNAL_WRITE_FAILED: failed to create file orchestration journal root"


def _forwarding_export(**kwargs: str) -> dict[str, object]:
    """Stand in for the Postgres export, forwarding ONLY the journal root.

    The point of the substitution is to keep the test db-free while leaving the
    journal-root half genuine: the real ``import_historical_scheduler_state``
    runs, so the refusal under test is the one production inherits rather than a
    hand-raised imitation.  It never returns on an invalid root.
    """

    return import_historical_scheduler_state(journal_root=kwargs["journal_root"])


@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
@pytest.mark.parametrize("shape", _INVALID_SHAPES)
def test_migrate_scheduler_state_renders_the_inherited_refusal_typed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    shape: str,
) -> None:
    """The shipped spec says EVERY entrypoint of these lanes renders the code.

    This lane never calls the seam itself -- it inherits the refusal through
    ``import_historical_scheduler_state``.  ``OrchestratorError`` is a
    ``RuntimeError`` subclass, so the lane's pre-existing
    ``except (RuntimeError, ValueError)`` arm caught it and echoed
    ``str(error)``, which is the message ALONE: exit 2 but no
    ``FILE_JOURNAL_INVALID_ROOT:`` in front of it, so the one token an operator
    greps for was missing on this lane and this lane only.
    """

    monkeypatch.setattr(cli_module, "export_scheduler_state_from_postgres", _forwarding_export)
    configured = _invalid_root(shape, tmp_path)
    cwd = _hostile_cwd(shape, tmp_path, monkeypatch)
    before = _tree(cwd)

    code, out, err = _invoke(
        entrypoint,
        [
            "migrate-scheduler-state",
            "--database-url",
            "postgresql://nwm@localhost:55433/nhms",
            "--journal-root",
            configured,
        ],
        capsys,
    )

    _assert_typed_refusal(code, out, err, configured=configured)
    assert _tree(cwd) == before


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root ignores the directory mode, so the parent cannot be made unwritable",
)
@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_migrate_scheduler_state_reports_an_uncreatable_root_without_leaking_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    """The create-capable lane's creation failure is typed, and carries no path.

    A legal-but-absent root under an unwritable parent is the ordinary
    permission mistake.  ``ensure_directory_no_follow`` reports it as a
    ``SafeFilesystemError`` naming the FULL target path -- and that is a
    ``RuntimeError`` subclass, so an unwrapped call had the CLI echo the
    operator's journal root straight onto stderr, which every other refusal on
    these lanes is asserted never to do.  ``safe_fs`` can also re-raise a bare
    ``OSError`` here, which the ``(RuntimeError, ValueError)`` arm would not
    catch at all.
    """

    monkeypatch.setattr(cli_module, "export_scheduler_state_from_postgres", _forwarding_export)
    parent = tmp_path / "unwritable"
    parent.mkdir()
    configured = str(parent / "journal")
    monkeypatch.chdir(tmp_path)
    parent.chmod(0o555)
    try:
        code, out, err = _invoke(
            entrypoint,
            [
                "migrate-scheduler-state",
                "--database-url",
                "postgresql://nwm@localhost:55433/nhms",
                "--journal-root",
                configured,
            ],
            capsys,
        )
    finally:
        parent.chmod(0o755)

    assert code == 2
    assert out.strip() == ""
    assert err.strip() == _WRITE_FAILED_LINE
    assert "Traceback" not in err
    assert configured not in err
    assert not Path(configured).exists()
