"""A merged scheduler registry apply is held to its dry-run and puts back what it committed (#2738).

Drives ``scripts.node22_publish_merged_scheduler_registry`` through the shared
workspace of ``tests/merged_registry_publish_helpers.py``.  Failures of a
publish are injected at the publisher seam by a wrapper that still calls the
real publisher, or by really holding the lock the publisher needs.
"""

from __future__ import annotations

import json
import os
import signal
import threading
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import scripts.merged_registry_publish.apply as apply_module
import scripts.node22_publish_merged_scheduler_registry as tool
from packages.common.object_store import sha256_bytes
from packages.common.provider_atomic import provider_destination_lock
from services.orchestrator.scheduler_file_providers import (
    SchedulerFileProviderError,
    publish_scheduler_registry_manifest,
)
from tests.merged_registry_publish_helpers import (
    PREFIX,
    SEEDED_AT,
    SOURCES,
    Operations,
    Workspace,
    _add_case,
    _apply_failed,
    _changed,
    _cli_environment,
    _refused,
    _remove_case,
    _tree,
    no_database,  # noqa: F401  (registers the autouse fixture on this module)
    workspace_fixture,  # noqa: F401  (registers the `workspace` fixture on this module)
)

# --- apply requires its dry-run ------------------------------------------------


def test_apply_without_a_dry_run_receipt_is_refused_naming_the_expected_path(workspace: Workspace) -> None:
    message = _refused(workspace, _remove_case(workspace)[0], succession_id="s-1", apply=True)

    assert str(workspace.receipt_root / "s-1" / "publish-dry-run.json") in message


def test_apply_without_a_succession_id_is_refused(workspace: Workspace) -> None:
    assert "--apply requires --succession-id" in _refused(workspace, _remove_case(workspace)[0], apply=True)


def test_apply_with_operations_other_than_the_dry_run_recorded_is_refused(workspace: Workspace) -> None:
    workspace.run(Operations(remove=("dg_a_gfs_v1", "dg_a_ifs_v1")), succession_id="s-1")

    message = _refused(
        workspace, Operations(remove=("dg_b_gfs_v1", "dg_b_ifs_v1")), succession_id="s-1", apply=True
    )

    assert "operations differs from the dry-run receipt" in message and "dg_b_gfs_v1" in message


def test_apply_after_the_canonical_models_changed_since_the_dry_run_is_refused(workspace: Workspace) -> None:
    operations = Operations(remove=("dg_a_gfs_v1", "dg_a_ifs_v1"))
    workspace.run(operations, succession_id="s-1")
    # Another succession retired basin c in between: both manifests hold the new generation.
    workspace.seed(workspace.rows[:4], generated_at=SEEDED_AT + timedelta(hours=1))

    message = _refused(workspace, operations, succession_id="s-1", apply=True)

    assert "canonical_models_sha256_before differs from the dry-run receipt" in message


def test_apply_naming_another_provision_than_the_dry_run_planned_is_refused_before_any_backup(
    workspace: Workspace,
) -> None:
    operations, provisioned, _expected = _add_case(workspace)
    workspace.provision("p-1", provisioned)
    workspace.run(operations, succession_id="s-1", provision_succession_id="p-1")
    # A second provision of the same model ids whose rows are not the ones the dry-run planned.
    workspace.provision("p-2", [workspace.row("d", source, mesh_area_km2=999.0) for source in SOURCES])
    previous = workspace.canonical.read_bytes()

    message = _refused(workspace, operations, succession_id="s-1", provision_succession_id="p-2", apply=True)

    assert "provision_succession_id differs from the dry-run receipt" in message
    assert '"p-1"' in message and '"p-2"' in message
    assert workspace.canonical.read_bytes() == previous == workspace.mirror.read_bytes()
    assert workspace.backups() == []
    assert list((workspace.receipt_root / "s-1").glob("publish-apply*.json")) == []


def test_apply_after_the_provision_receipt_was_rewritten_since_the_dry_run_is_refused_before_any_backup(
    workspace: Workspace,
) -> None:
    operations, provisioned, _expected = _add_case(workspace)
    workspace.provision("s-1", provisioned)
    planned = workspace.run(operations, succession_id="s-1")
    # The same provision succession id, re-provisioned in place: another registry, another receipt.
    workspace.provision("s-1", [workspace.row("d", source, mesh_area_km2=999.0) for source in SOURCES])
    previous = workspace.canonical.read_bytes()

    message = _refused(workspace, operations, succession_id="s-1", apply=True)

    assert "provision_apply_receipt differs from the dry-run receipt" in message
    assert planned["provision_apply_receipt"]["sha256"] in message
    assert workspace.canonical.read_bytes() == previous == workspace.mirror.read_bytes()
    assert workspace.backups() == []
    assert list((workspace.receipt_root / "s-1").glob("publish-apply*.json")) == []


def test_a_renewal_that_only_changes_generated_at_between_dry_run_and_apply_does_not_block_the_apply(
    workspace: Workspace,
) -> None:
    operations, _provisioned, expected_rows = _remove_case(workspace)
    planned = workspace.run(operations, succession_id="s-1")
    workspace.seed(workspace.rows, generated_at=SEEDED_AT + timedelta(hours=1))
    assert sha256_bytes(workspace.canonical.read_bytes()) != planned["canonical"]["sha256_before"]

    receipt = workspace.run(operations, succession_id="s-1", apply=True)

    assert receipt["outcome"] == "published" and workspace.models(workspace.mirror) == expected_rows
    assert receipt["canonical_models_sha256_before"] == planned["canonical_models_sha256_before"]


def test_a_second_apply_of_a_published_succession_is_refused_before_any_write(workspace: Workspace) -> None:
    operations = _remove_case(workspace)[0]
    workspace.plan_then_apply(operations, "s-1")

    message = _refused(workspace, operations, succession_id="s-1", apply=True)

    assert "publish-apply.json already exists" in message


def test_apply_while_the_provider_refresh_lock_is_held_is_refused_before_any_backup(workspace: Workspace) -> None:
    operations = _remove_case(workspace)[0]
    workspace.run(operations, succession_id="s-1")

    with provider_destination_lock(workspace.refresh_lock, blocking=False):
        message = _refused(workspace, operations, succession_id="s-1", apply=True)

    assert "provider refresh lock" in message and "provider_already_running" in message
    assert workspace.backups() == [] and workspace.failed_receipts("s-1") == []
    # Released again: the same succession then publishes.
    assert workspace.run(operations, succession_id="s-1", apply=True)["outcome"] == "published"


def test_apply_without_a_configured_provider_refresh_lock_is_refused(workspace: Workspace) -> None:
    operations = _remove_case(workspace)[0]
    workspace.run(operations, succession_id="s-1")

    message = _refused(workspace, operations, succession_id="s-1", apply=True, refresh_lock=None)

    assert "NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK is required" in message


def test_apply_with_a_relative_provider_refresh_lock_is_refused_before_anything_is_created(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = _remove_case(workspace)[0]
    workspace.run(operations, succession_id="s-1")
    # Relative to the working directory this would be the very lock the refresh runner takes.
    monkeypatch.chdir(workspace.root)
    before = _tree(workspace.root)

    with pytest.raises(tool.MergedRegistryPublishError) as raised:
        workspace.run(operations, succession_id="s-1", apply=True, refresh_lock="provider-refresh/refresh")

    assert "NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK must be an absolute path" in str(raised.value)
    assert "provider-refresh/refresh" in str(raised.value)
    assert _changed(before, _tree(workspace.root)) == set()
    assert list(workspace.refresh_lock.parent.iterdir()) == []


# --- a failed apply puts back what it committed ---------------------------------


def _publisher(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Workspace,
    *,
    before: Callable[[Path, dict[str, Any]], None] | None = None,
    after: Callable[[Path], None] | None = None,
) -> None:
    """Wrap the real publisher: ``before`` / ``after`` run around a publish to a real manifest only."""

    def wrapped(models: Any, destination: Any, **kwargs: Any) -> dict[str, Any]:
        path = Path(destination)
        real = path in {workspace.canonical, workspace.mirror}
        if real and before is not None:
            before(path, kwargs)
        result = publish_scheduler_registry_manifest(models, destination, **kwargs)
        if real and after is not None:
            after(path)
        return result

    monkeypatch.setattr(apply_module, "publish_scheduler_registry_manifest", wrapped)


def _another_writer(workspace: Workspace, path: Path) -> bytes:
    """Rewrite ``path`` as a concurrent writer would: the same rows under a later ``generated_at``."""

    publish_scheduler_registry_manifest(
        workspace.rows,
        path,
        object_store_root=workspace.store,
        object_store_prefix=PREFIX,
        generated_at=SEEDED_AT + timedelta(hours=2),
    )
    return path.read_bytes()


def test_a_canonical_manifest_changed_between_read_and_publish_is_refused_and_neither_is_written(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = _remove_case(workspace)[0]
    workspace.run(operations, succession_id="s-1")
    previous = workspace.mirror.read_bytes()
    concurrent: list[bytes] = []

    def change_canonical(path: Path, _kwargs: dict[str, Any]) -> None:
        if path == workspace.canonical:
            concurrent.append(_another_writer(workspace, path))

    _publisher(monkeypatch, workspace, before=change_canonical)

    error = _apply_failed(workspace, operations, "s-1")

    assert error.receipt["outcome"] == "refused" and "provider_preimage_changed" in error.receipt["reason"]
    # The compare-and-swap kept the other writer's bytes; this run wrote neither manifest.
    assert workspace.canonical.read_bytes() == concurrent[0] != previous
    assert workspace.mirror.read_bytes() == previous
    assert error.receipt["canonical"]["sha256_after"] == sha256_bytes(concurrent[0])
    assert "changed neither manifest" in str(error)


def test_a_mirror_publish_that_fails_after_the_canonical_write_restores_the_canonical_manifest_and_can_be_retried(
    workspace: Workspace,
) -> None:
    operations, _provisioned, expected_rows = _remove_case(workspace)
    workspace.run(operations, succession_id="s-1")
    previous = workspace.canonical.read_bytes()

    # Somebody holds the mirror's own destination lock: the canonical publish
    # commits, then the mirror publish is really refused by the atomic writer.
    with provider_destination_lock(workspace.mirror, blocking=False):
        error = _apply_failed(workspace, operations, "s-1")

    assert error.receipt["outcome"] == "rolled_back" and "provider_already_running" in error.receipt["reason"]
    assert workspace.canonical.read_bytes() == previous == workspace.mirror.read_bytes()
    assert error.receipt["canonical"]["sha256_after"] == sha256_bytes(previous)
    assert [path.read_bytes() for path in workspace.backups()] == [previous, previous]
    assert "--apply rolled_back" in str(error) and "can be retried" in str(error)

    receipt = workspace.run(operations, succession_id="s-1", apply=True)

    assert receipt["outcome"] == "published" and workspace.models(workspace.canonical) == expected_rows
    assert workspace.mirror.read_bytes() == workspace.canonical.read_bytes()
    assert len(workspace.failed_receipts("s-1")) == 1 and len(workspace.backups()) == 4


def test_a_read_back_that_differs_restores_both_manifests(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = _remove_case(workspace)[0]
    workspace.run(operations, succession_id="s-1")
    previous = workspace.canonical.read_bytes()

    def other_generation(path: Path, kwargs: dict[str, Any]) -> None:
        if path == workspace.mirror:
            kwargs["generated_at"] += timedelta(seconds=1)

    _publisher(monkeypatch, workspace, before=other_generation)

    error = _apply_failed(workspace, operations, "s-1")

    assert error.receipt["outcome"] == "rolled_back" and "read-back" in error.receipt["reason"]
    assert workspace.canonical.read_bytes() == previous == workspace.mirror.read_bytes()


def test_a_mirror_publish_that_raises_after_its_commit_restores_the_mirror_too(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = _remove_case(workspace)[0]
    workspace.run(operations, succession_id="s-1")
    previous = workspace.canonical.read_bytes()
    written: list[bytes] = []

    def raise_after_commit(path: Path) -> None:
        if path == workspace.mirror:
            written.append(path.read_bytes())
            raise RuntimeError("after the mirror commit")

    _publisher(monkeypatch, workspace, after=raise_after_commit)

    error = _apply_failed(workspace, operations, "s-1")

    assert written and written[0] != previous
    assert error.receipt["outcome"] == "rolled_back" and "after the mirror commit" in error.receipt["reason"]
    assert workspace.canonical.read_bytes() == previous == workspace.mirror.read_bytes()


def test_a_canonical_manifest_rewritten_by_someone_else_is_not_overwritten_at_restore_time(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = _remove_case(workspace)[0]
    workspace.run(operations, succession_id="s-1")
    previous = workspace.mirror.read_bytes()
    concurrent: list[bytes] = []

    def rewrite_canonical_then_fail(path: Path, _kwargs: dict[str, Any]) -> None:
        if path == workspace.mirror:
            concurrent.append(_another_writer(workspace, workspace.canonical))
            raise SchedulerFileProviderError("provider_replace_failed", field="destination")

    _publisher(monkeypatch, workspace, before=rewrite_canonical_then_fail)

    error = _apply_failed(workspace, operations, "s-1")

    assert error.receipt["outcome"] == "inconsistent"
    assert workspace.canonical.read_bytes() == concurrent[0]
    assert workspace.mirror.read_bytes() == previous
    assert error.receipt["canonical"]["sha256_after"] == sha256_bytes(concurrent[0])
    assert error.receipt["mirror"]["sha256_after"] == sha256_bytes(previous)
    backups = [str(path) for path in workspace.backups()]
    assert len(backups) == 2 and all(path in str(error) for path in backups)
    assert sorted(backups) == sorted(error.receipt[name]["backup_path"] for name in ("canonical", "mirror"))
    assert "restore both from this run's backups" in str(error) and "publish the mirror" in str(error)


def test_a_manifest_changed_without_a_commit_this_run_observed_is_left_alone_and_reported_inconsistent(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = _remove_case(workspace)[0]
    workspace.run(operations, succession_id="s-1")
    previous = workspace.canonical.read_bytes()

    def lose_the_commit_token(path: Path, kwargs: dict[str, Any]) -> None:
        # What a failure between the writer's commit and the publisher's return looks like.
        if path == workspace.mirror:
            kwargs["commit_observer"] = lambda _preimage: None

    def fail(path: Path) -> None:
        if path == workspace.mirror:
            raise SchedulerFileProviderError("provider_lock_release_failed", field="destination")

    _publisher(monkeypatch, workspace, before=lose_the_commit_token, after=fail)

    error = _apply_failed(workspace, operations, "s-1")

    assert error.receipt["outcome"] == "inconsistent"
    # The canonical manifest, which this run did commit, is restored; the mirror is never guessed at.
    assert workspace.canonical.read_bytes() == previous
    assert workspace.mirror.read_bytes() != previous
    assert "not by a commit this run observed" in error.receipt["reason"]


def test_a_published_apply_whose_receipt_cannot_be_written_rolls_nothing_back_and_exits_non_zero(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    operations, _provisioned, expected_rows = _remove_case(workspace)
    workspace.run(operations, succession_id="s-1")
    apply_receipt = workspace.receipt_root / "s-1" / "publish-apply.json"

    def occupy_the_receipt(path: Path) -> None:
        if path == workspace.mirror:
            apply_receipt.mkdir()

    _publisher(monkeypatch, workspace, after=occupy_the_receipt)
    _cli_environment(monkeypatch, workspace)

    removes = ["--remove", "dg_a_ifs_v1", "--remove", "dg_a_gfs_v1"]
    status = tool.main([*removes, "--operator-id", "op", "--succession-id", "s-1", "--apply"])

    message = capsys.readouterr().err
    assert status == 1
    assert workspace.models(workspace.canonical) == expected_rows
    published = workspace.canonical.read_bytes()
    assert workspace.mirror.read_bytes() == published
    assert "complete but unreceipted" in message and "Nothing was rolled back" in message
    assert sha256_bytes(published) in message and json.loads(published)["generated_at"] in message
    assert len(workspace.backups()) == 2 and all(str(path) in message for path in workspace.backups())
    assert workspace.failed_receipts("s-1") == []


# --- termination signals during an apply ----------------------------------------


@pytest.fixture
def signal_handlers() -> Iterator[dict[int, Any]]:
    """Harmless handlers for SIGTERM and SIGHUP, so that a signal the tool does not catch fails the test only."""

    received: list[int] = []

    def harmless(signum: int, _frame: Any) -> None:
        received.append(signum)

    handlers: dict[int, Any] = {"received": received, "handler": harmless}  # type: ignore[dict-item]
    previous = {number: signal.signal(number, harmless) for number in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield handlers
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP])
def test_a_termination_signal_between_the_canonical_commit_and_the_mirror_publish_restores_the_canonical_manifest(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, signal_handlers: dict[Any, Any], number: signal.Signals
) -> None:
    operations = _remove_case(workspace)[0]
    workspace.run(operations, succession_id="s-1")
    previous = workspace.canonical.read_bytes()
    committed: list[bytes] = []
    publishes: list[Path] = []

    def terminate_after_the_canonical_commit(path: Path) -> None:
        if path == workspace.canonical:
            committed.append(path.read_bytes())
            os.kill(os.getpid(), number)

    _publisher(
        monkeypatch,
        workspace,
        before=lambda path, _kwargs: publishes.append(path),
        after=terminate_after_the_canonical_commit,
    )

    error = _apply_failed(workspace, operations, "s-1")

    # The canonical manifest really held the new generation when the signal came.
    assert committed and committed[0] != previous
    assert publishes == [workspace.canonical]
    assert error.receipt["outcome"] == "rolled_back" and number.name in error.receipt["reason"]
    assert workspace.canonical.read_bytes() == previous == workspace.mirror.read_bytes()
    assert error.receipt["canonical"]["sha256_after"] == sha256_bytes(previous)
    # What stops the process is not an ``Exception``: a handler catching only that would not have restored.
    cause = error.__cause__
    assert isinstance(cause, BaseException) and not isinstance(cause, Exception)
    # The handlers are the ones that were there before the call, and they never saw the signal.
    assert [signal.getsignal(item) for item in (signal.SIGTERM, signal.SIGHUP)] == [signal_handlers["handler"]] * 2
    assert signal_handlers["received"] == []
    # The same succession then publishes.
    monkeypatch.undo()
    assert workspace.run(operations, succession_id="s-1", apply=True)["outcome"] == "published"


def test_a_hangup_that_is_ignored_on_entry_as_under_nohup_does_not_stop_the_apply(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, signal_handlers: dict[Any, Any]
) -> None:
    operations, _provisioned, expected_rows = _remove_case(workspace)
    workspace.run(operations, succession_id="s-1")
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    sent: list[Path] = []

    def hang_up_after_the_canonical_commit(path: Path) -> None:
        if path == workspace.canonical:
            sent.append(path)
            os.kill(os.getpid(), signal.SIGHUP)

    _publisher(monkeypatch, workspace, after=hang_up_after_the_canonical_commit)

    receipt = workspace.run(operations, succession_id="s-1", apply=True)

    assert sent == [workspace.canonical]
    assert receipt["outcome"] == "published" and workspace.models(workspace.mirror) == expected_rows
    assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
    assert signal.getsignal(signal.SIGTERM) == signal_handlers["handler"]


def test_a_published_apply_leaves_the_signal_handlers_it_found(
    workspace: Workspace, signal_handlers: dict[Any, Any]
) -> None:
    receipt = workspace.plan_then_apply(_remove_case(workspace)[0], "s-1")

    assert receipt["outcome"] == "published"
    assert [signal.getsignal(item) for item in (signal.SIGTERM, signal.SIGHUP)] == [signal_handlers["handler"]] * 2


def test_a_refused_apply_leaves_the_signal_handlers_it_found(
    workspace: Workspace, signal_handlers: dict[Any, Any]
) -> None:
    operations = _remove_case(workspace)[0]
    workspace.run(operations, succession_id="s-1")

    with provider_destination_lock(workspace.canonical, blocking=False):
        assert _apply_failed(workspace, operations, "s-1").receipt["outcome"] == "refused"

    assert [signal.getsignal(item) for item in (signal.SIGTERM, signal.SIGHUP)] == [signal_handlers["handler"]] * 2


def test_a_dry_run_installs_no_signal_handler(workspace: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(signal, "signal", lambda *_args: pytest.fail("a dry-run installed a signal handler"))

    assert workspace.run(_remove_case(workspace)[0], succession_id="s-1")["outcome"] == "planned"


def test_an_apply_outside_the_main_thread_publishes_without_installing_signal_handlers(
    workspace: Workspace, signal_handlers: dict[Any, Any]
) -> None:
    operations = _remove_case(workspace)[0]
    workspace.run(operations, succession_id="s-1")
    outcome: list[Any] = []

    def apply() -> None:
        try:
            outcome.append(workspace.run(operations, succession_id="s-1", apply=True)["outcome"])
        except BaseException as error:  # noqa: BLE001 - reported by the assertion below
            outcome.append(error)

    thread = threading.Thread(target=apply)
    thread.start()
    thread.join()

    assert outcome == ["published"]
    assert [signal.getsignal(item) for item in (signal.SIGTERM, signal.SIGHUP)] == [signal_handlers["handler"]] * 2


# --- a backup that could not be written ------------------------------------------


def test_a_mirror_backup_that_cannot_be_written_is_refused_reporting_only_the_backup_that_exists(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations, _provisioned, expected_rows = _remove_case(workspace)
    workspace.run(operations, succession_id="s-1")
    previous = workspace.canonical.read_bytes()
    write_backup = apply_module._write_backup
    partial: list[Path] = []

    def fail_on_the_mirror(path: Path, content: bytes) -> None:
        if path.parent != workspace.mirror.parent:
            write_backup(path, content)
            return
        # The disk fills while the mirror backup is being written.
        path.write_bytes(content[:10])
        partial.append(path)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(apply_module, "_write_backup", fail_on_the_mirror)

    error = _apply_failed(workspace, operations, "s-1")

    assert error.receipt["outcome"] == "refused" and "No space left on device" in error.receipt["reason"]
    assert workspace.canonical.read_bytes() == previous == workspace.mirror.read_bytes()
    (canonical_backup,) = workspace.backups()
    assert canonical_backup.parent == workspace.canonical.parent and canonical_backup.read_bytes() == previous
    assert error.receipt["canonical"]["backup_path"] == str(canonical_backup)
    assert error.receipt["mirror"]["backup_path"] is None
    assert len(partial) == 1 and not partial[0].exists()
    assert str(canonical_backup) in str(error) and str(partial[0]) not in str(error)

    monkeypatch.undo()
    receipt = workspace.run(operations, succession_id="s-1", apply=True)

    assert receipt["outcome"] == "published" and workspace.models(workspace.mirror) == expected_rows
    assert len(workspace.backups()) == 3


def test_a_canonical_backup_that_cannot_be_written_reports_no_backup_at_all(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = _remove_case(workspace)[0]
    workspace.run(operations, succession_id="s-1")
    previous = workspace.canonical.read_bytes()

    def fail(path: Path, content: bytes) -> None:
        path.write_bytes(content[:10])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(apply_module, "_write_backup", fail)

    error = _apply_failed(workspace, operations, "s-1")

    assert error.receipt["outcome"] == "refused"
    assert workspace.canonical.read_bytes() == previous == workspace.mirror.read_bytes()
    assert workspace.backups() == []
    assert (error.receipt["canonical"]["backup_path"], error.receipt["mirror"]["backup_path"]) == (None, None)
    assert "No backup was written" in str(error) and ".bak-" not in str(error)
