"""Archive publication, restore, and resource contracts for scheduler-journal retention."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts import node22_scheduler_journal_retention as entrypoint
from services.orchestrator import scheduler_journal_archive as archive
from services.orchestrator import scheduler_journal_restore as restore
from services.orchestrator import scheduler_journal_retention as retention
from services.orchestrator.file_orchestration_journal import (
    FileJournalRetentionMember,
    FileOrchestrationJournalRepository,
)
from tests.scheduler_journal_retention_fixtures import (
    NOW,
    OLD_CYCLE,
    _archive_path,
    _config,
    _cycle_lock_path,
    _direct_record,
    _frontier,
    _install_cli_env,
    _job,
    _later_frontier,
    _manifest_path,
    _reservation,
    _seed_cycle,
    _write_json,
)
from workers.data_adapters.base import cycle_id_for

SchedulerJournalRetentionConfig = retention.SchedulerJournalRetentionConfig
ReceiptReservation = retention.ReceiptReservation
FrontierReadResult = retention.FrontierReadResult
reserve_receipt = retention.reserve_receipt

def test_verify_stage_restore_is_no_clobber_and_preserves_public_query_parity(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    paths = _seed_cycle(config.journal_root, continuation=True, pipeline_event=True)
    repository = FileOrchestrationJournalRepository(config.journal_root)
    before = repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", OLD_CYCLE))

    enforced = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))
    assert enforced["counts"]["archived"] == 1
    staged = restore.verify_and_restore(
        journal_root=config.journal_root,
        archive_root=config.archive_root,
        source_id="gfs",
        cycle="2026050100",
        stage_root=tmp_path / "stage",
    )

    assert staged["status"] == "restored"
    assert {path.relative_to(config.journal_root).as_posix() for path in paths} == set(staged["restored_paths"])
    after = FileOrchestrationJournalRepository(config.journal_root).query_pipeline_jobs_by_cycle(
        cycle_id_for("gfs", OLD_CYCLE)
    )
    assert after == before
    with pytest.raises(retention.RetentionFailure, match="restore_clobber"):
        restore.verify_and_restore(
            journal_root=config.journal_root,
            archive_root=config.archive_root,
            source_id="gfs",
            cycle="2026050100",
            stage_root=tmp_path / "second-stage",
        )


def test_verify_restore_cli_reports_structured_refusal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result = entrypoint.main(
        [
            "verify-restore",
            "--journal-root",
            str(tmp_path / "missing-journal"),
            "--archive-root",
            str(tmp_path / "missing-archive"),
            "--source-id",
            "gfs",
            "--cycle",
            "2026050100",
            "--stage-root",
            str(tmp_path / "stage"),
        ]
    )

    assert result == 2
    assert json.loads(capsys.readouterr().out) == {
        "reason": "journal_root_unavailable",
        "schema_version": retention.SCHEMA_VERSION,
        "status": "blocked",
    }


def test_verify_restore_cli_restores_archive_and_returns_structured_success(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    members = _seed_cycle(config.journal_root, continuation=True, pipeline_event=True)
    assert retention.run_retention(
        config,
        now=NOW,
        frontier=_frontier(),
        receipt_reservation=_reservation(config),
    )["counts"]["archived"] == 1

    result = entrypoint.main(
        [
            "verify-restore",
            "--journal-root",
            str(config.journal_root),
            "--archive-root",
            str(config.archive_root),
            "--source-id",
            "gfs",
            "--cycle",
            "2026050100",
            "--stage-root",
            str(tmp_path / "stage"),
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "restored"
    assert payload["source_id"] == "gfs"
    assert payload["cycle_time"] == "2026-05-01T00:00:00Z"
    assert set(payload["restored_paths"]) == {
        path.relative_to(config.journal_root).as_posix() for path in members
    }


def test_flat_direct_live_authority_retains_hot_slice(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    members = _seed_cycle(config.journal_root)
    active = _job("gfs", OLD_CYCLE, status="running")
    _write_json(
        config.journal_root / "pipeline-jobs" / f"{active['job_id']}.json",
        _direct_record(active),
    )

    payload = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    row = payload["cycles"][0]
    assert row["status"] == "skipped"
    assert row["reason"] == "live_row"
    assert row["member_count"] == len(members)
    assert all(path.exists() for path in members)


def test_current_accepted_terminal_master_with_incomplete_projections_retains_its_own_cycle(
    tmp_path: Path,
) -> None:
    from tests.test_file_orchestration_journal import _cancelled_cohort_master

    # This helper appends a legal current-contract cancelled master through the
    # journal's own outgoing validator and write sequence. It begins bound and
    # accepted, but deliberately has no task projections, so the liveness
    # assertion cannot be accidentally satisfied by a separate live row.
    repository, record = _cancelled_cohort_master(tmp_path)
    master = repository.get_pipeline_job(str(record["job_id"]))
    assert master is not None
    assert master["status"] == "cancelled"
    assert master["submit_outcome"] == "accepted"
    assert master["candidate_projections"] == []
    config = _config(tmp_path, enabled=True, dry_run=False)

    payload = retention.run_retention(
        config,
        now=NOW.replace(year=2027),
        frontier=_frontier(None),
        receipt_reservation=_reservation(config),
    )

    own = next(row for row in payload["cycles"] if row["cycle_time"] == "2026-07-20T00:00:00Z")
    assert own["status"] == "skipped"
    assert own["reason"] == "live_row"


def test_partial_cleanup_adopts_original_manifest_after_frontier_changes(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    members = _seed_cycle(config.journal_root, continuation=True, pipeline_event=True)

    first = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert first["counts"]["archived"] == 1
    archive = _archive_path(config)
    original_archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    restored = restore.verify_and_restore(
        journal_root=config.journal_root,
        archive_root=config.archive_root,
        source_id="gfs",
        cycle="2026050100",
        stage_root=tmp_path / "stage",
    )
    one_removed = config.journal_root / restored["restored_paths"][0]
    one_removed.unlink()

    retry = retention.run_retention(
        config,
        now=NOW,
        receipt_reservation=_reservation(config),
        frontier=retention.FrontierReadResult(
            status="ok",
            active_lower_bound=datetime(2026, 6, 1, tzinfo=UTC),
            source="receipt:scheduler_pass",
            receipt_path="/receipts/new-pass.json",
            receipt_started_at=NOW + timedelta(minutes=1),
        ),
    )

    assert retry["counts"]["archived"] == 1
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == original_archive_digest
    assert all(not path.exists() for path in members)


def test_partial_cleanup_refuses_new_or_changed_hot_member(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    _seed_cycle(config.journal_root)
    assert (
        retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))[
            "counts"
        ]["archived"]
        == 1
    )
    restore.verify_and_restore(
        journal_root=config.journal_root,
        archive_root=config.archive_root,
        source_id="gfs",
        cycle="2026050100",
        stage_root=tmp_path / "stage",
    )
    changed = config.journal_root / "journal" / "gfs" / "2026050100.jsonl"
    original = changed.read_bytes()
    changed.write_bytes(b"x" * len(original))

    retry = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert retry["cycles"][0]["status"] == "blocked"
    # The owner parser rejects the altered authority before an archive retry can
    # reach unlink; either route is a fail-closed zero-removal outcome.
    assert retry["cycles"][0]["reason"] in {"file_journal_malformed_json", "member_identity_changed"}
    assert changed.exists()


def test_same_size_replacement_before_unlink_refuses_hot_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    members = _seed_cycle(config.journal_root)
    original_remove = FileOrchestrationJournalRepository._remove_retention_members_unlocked

    def swap_before_unlink(self: FileOrchestrationJournalRepository, **kwargs: Any):
        target = self.root / members[0].relative_to(config.journal_root)
        payload = target.read_bytes()
        target.write_bytes(b"x" * len(payload))
        return original_remove(self, **kwargs)

    monkeypatch.setattr(FileOrchestrationJournalRepository, "_remove_retention_members_unlocked", swap_before_unlink)
    payload = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert payload["cycles"][0]["status"] == "blocked"
    assert payload["cycles"][0]["reason"] == "member_identity_changed"
    assert all(path.exists() for path in members)


def test_case_alias_member_is_rejected_before_archive_publication_or_hot_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    hot_members = _seed_cycle(config.journal_root)
    alias_path = config.journal_root / "latest" / "GFS" / "2026050100" / "model_alias.json"
    alias_path.parent.mkdir(parents=True, exist_ok=True)
    alias_path.write_bytes(hot_members[0].read_bytes())
    alias_member = FileJournalRetentionMember(
        relative_path=alias_path.relative_to(config.journal_root).as_posix(),
        size_bytes=alias_path.stat().st_size,
    )
    members = tuple(
        FileJournalRetentionMember(
            relative_path=path.relative_to(config.journal_root).as_posix(),
            size_bytes=path.stat().st_size,
        )
        for path in hot_members
    ) + (alias_member,)
    calls = 0

    def archive_tool_must_not_run(**_kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        raise AssertionError("case-alias member reached archive toolchain")

    monkeypatch.setattr(archive, "_run_archive_toolchain", archive_tool_must_not_run)
    repository = FileOrchestrationJournalRepository(config.journal_root)
    assert repository.query_pipeline_jobs_by_cycle(cycle_id_for("GFS", OLD_CYCLE))

    def publish_reason() -> str:
        with pytest.raises(retention.RetentionFailure) as raised:
            archive.publish_archive(
                config,
                source_id="gfs",
                cycle_time=OLD_CYCLE,
                now=NOW,
                frontier=_frontier(),
                members=members,
            )
        return raised.value.reason

    assert publish_reason() == "archive_manifest_identity_mismatch"
    assert publish_reason() == "archive_manifest_identity_mismatch"
    cycle_archive_root = config.archive_root / "gfs" / "2026050100"
    assert calls == 0
    assert not cycle_archive_root.exists()
    assert not (cycle_archive_root / archive._PUBLICATION_MARKER).exists()
    assert not _archive_path(config).exists()
    assert not _manifest_path(config).exists()
    assert all(path.exists() for path in [*hot_members, alias_path])


def test_incomplete_bundle_is_not_adopted_or_unlinked(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    members = _seed_cycle(config.journal_root)
    incomplete = config.archive_root / "gfs" / "2026050100" / "bundle"
    incomplete.mkdir(parents=True)
    (incomplete / archive.ARCHIVE_NAME).write_bytes(b"incomplete")

    payload = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert payload["cycles"][0]["reason"] == "archive_conflict"
    assert all(path.exists() for path in members)


@pytest.mark.parametrize("published", [archive.ARCHIVE_NAME, archive.MANIFEST_NAME])
def test_marker_bound_partial_bundle_is_completed_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    published: str,
) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    members = _seed_cycle(config.journal_root)
    original_move = archive.move_regular_file_no_follow_exclusive
    moved_names: list[str] = []

    def fail_after_requested_publish(*args: Any, **kwargs: Any) -> Path:
        moved = original_move(*args, **kwargs)
        moved_names.append(str(args[3]))
        if str(args[3]) == published:
            raise retention.RetentionFailure("archive_publish_failed")
        return moved

    monkeypatch.setattr(archive, "move_regular_file_no_follow_exclusive", fail_after_requested_publish)
    first = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert first["cycles"][0]["reason"] == "archive_publish_failed"
    assert published in moved_names
    bundle = _archive_path(config).parent
    assert (bundle / published).exists()
    assert all(path.exists() for path in members)

    monkeypatch.setattr(archive, "move_regular_file_no_follow_exclusive", original_move)
    second = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert second["counts"]["archived"] == 1
    assert all(not path.exists() for path in members)


def test_empty_bundle_publication_reservation_is_completed_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    members = _seed_cycle(config.journal_root)
    original_publish = archive._publish_bundle_directory
    calls = 0

    def fail_after_reservation(**kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            archive.write_bytes_no_follow_exclusive(
                kwargs["bundle_root"] / archive._PUBLICATION_MARKER,
                archive._publication_payload(manifest=kwargs["manifest"]),
                containment_root=kwargs["archive_root"],
                require_durable_create=True,
            )
            raise retention.RetentionFailure("archive_publish_failed")
        original_publish(**kwargs)

    monkeypatch.setattr(archive, "_publish_bundle_directory", fail_after_reservation)
    first = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))
    assert first["cycles"][0]["reason"] == "archive_publish_failed"
    assert all(path.exists() for path in members)

    monkeypatch.setattr(archive, "_publish_bundle_directory", original_publish)
    second = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert second["counts"]["archived"] == 1
    assert all(not path.exists() for path in members)


def test_archive_publication_failure_leaves_no_hot_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    members = _seed_cycle(config.journal_root)

    def fail_publish(**_kwargs: Any) -> None:
        raise retention.RetentionFailure("archive_publish_failed")

    monkeypatch.setattr(archive, "_publish_bundle_directory", fail_publish)
    payload = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert payload["cycles"][0]["status"] == "blocked"
    assert payload["cycles"][0]["reason"] == "archive_publish_failed"
    assert all(path.exists() for path in members)


def test_direct_enforcement_requires_a_durable_receipt_intent(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    members = _seed_cycle(config.journal_root)

    payload = retention.run_retention(config, now=NOW, frontier=_frontier())

    assert payload["preflight_blockers"] == ["receipt_reservation_required"]
    assert payload["counts"]["archived"] == 0
    assert not config.archive_root.exists()
    assert all(path.exists() for path in members)


def test_receipt_reservation_failure_prevents_archive_and_hot_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    members = _seed_cycle(config.journal_root)

    def fail_reservation(*_args: Any, **_kwargs: Any) -> retention.ReceiptReservation:
        raise retention.RetentionFailure("receipt_reservation_failed")

    for name, value in {
        "NHMS_SCHEDULER_ALLOWED_ROOTS": str(config.journal_root.parent),
        "NHMS_SCHEDULER_JOURNAL_ROOT": str(config.journal_root),
        "NHMS_SCHEDULER_JOURNAL_ARCHIVE_ROOT": str(config.archive_root),
        "NHMS_SCHEDULER_EVIDENCE_ROOT": str(config.evidence_root),
        "NHMS_SCHEDULER_LOOKBACK_HOURS": "96",
        "NHMS_SCHEDULER_CYCLE_LAG_HOURS": "16",
        "NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC": "0,12",
        "NHMS_SCHEDULER_JOURNAL_RETENTION_ENABLED": "true",
        "NHMS_SCHEDULER_JOURNAL_RETENTION_DRY_RUN": "false",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(retention, "reserve_receipt", fail_reservation)
    result = entrypoint.main(
        [
            "--journal-root",
            str(config.journal_root),
            "--archive-root",
            str(config.archive_root),
            "--evidence-root",
            str(config.evidence_root),
        ]
    )

    assert result == 2
    assert not (_archive_path(config)).exists()
    assert all(path.exists() for path in members)


def test_cli_returns_nonzero_for_partial_or_blocked_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    _seed_cycle(config.journal_root)
    for name, value in {
        "NHMS_SCHEDULER_ALLOWED_ROOTS": str(config.journal_root.parent),
        "NHMS_SCHEDULER_JOURNAL_ROOT": str(config.journal_root),
        "NHMS_SCHEDULER_JOURNAL_ARCHIVE_ROOT": str(config.archive_root),
        "NHMS_SCHEDULER_EVIDENCE_ROOT": str(config.evidence_root),
        "NHMS_SCHEDULER_LOOKBACK_HOURS": "96",
        "NHMS_SCHEDULER_CYCLE_LAG_HOURS": "16",
        "NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC": "0,12",
        "NHMS_SCHEDULER_JOURNAL_RETENTION_ENABLED": "true",
        "NHMS_SCHEDULER_JOURNAL_RETENTION_DRY_RUN": "false",
    }.items():
        monkeypatch.setenv(name, value)

    def partial(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "schema_version": retention.SCHEMA_VERSION,
            "preflight_blockers": [],
            "counts": {"blocked": 0, "partial": 1},
        }

    monkeypatch.setattr(retention, "run_retention", partial)
    assert entrypoint.main([]) == 2


def test_restore_refuses_unsafe_source_member_stage_and_busy_lock(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    _seed_cycle(config.journal_root)
    assert (
        retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))[
            "counts"
        ]["archived"]
        == 1
    )
    with pytest.raises(retention.RetentionFailure, match="source_id_invalid"):
        restore.verify_and_restore(
            journal_root=config.journal_root,
            archive_root=config.archive_root,
            source_id="../gfs",
            cycle="2026050100",
            stage_root=tmp_path / "stage-a",
        )
    with pytest.raises(retention.RetentionFailure, match="stage_root_is_root"):
        restore.verify_and_restore(
            journal_root=config.journal_root,
            archive_root=config.archive_root,
            source_id="gfs",
            cycle="2026050100",
            stage_root="/",
        )
    manifest_path = _manifest_path(config)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["members"][0]["path"] = "latest/IFS/2026050100/foreign.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(retention.RetentionFailure, match="archive_verification_failed"):
        restore.verify_and_restore(
            journal_root=config.journal_root,
            archive_root=config.archive_root,
            source_id="gfs",
            cycle="2026050100",
            stage_root=tmp_path / "stage-b",
        )


def test_restore_busy_lock_refuses_without_writes(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    _seed_cycle(config.journal_root)
    assert (
        retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))[
            "counts"
        ]["archived"]
        == 1
    )
    repository = FileOrchestrationJournalRepository(config.journal_root)
    entered = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with repository.open_retention_cycle(source_id="gfs", cycle_time=OLD_CYCLE) as window:
            assert window.status == "locked"
            entered.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert entered.wait(timeout=5)
    try:
        with pytest.raises(retention.RetentionFailure, match="in_flight"):
            restore.verify_and_restore(
                journal_root=config.journal_root,
                archive_root=config.archive_root,
                source_id="gfs",
                cycle="2026050100",
                stage_root=tmp_path / "stage",
            )
    finally:
        release.set()
        thread.join(timeout=5)
    assert not (tmp_path / "stage").exists()
    assert not any(config.journal_root.glob("latest/gfs/2026050100/*.json"))


def test_archive_and_manifest_symlinks_are_refused_without_restoring(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    _seed_cycle(config.journal_root)
    assert (
        retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))[
            "counts"
        ]["archived"]
        == 1
    )
    archive = _archive_path(config)
    archive.unlink()
    archive.symlink_to("/etc/passwd")

    with pytest.raises(retention.RetentionFailure, match="archive_verification_failed"):
        restore.verify_and_restore(
            journal_root=config.journal_root,
            archive_root=config.archive_root,
            source_id="gfs",
            cycle="2026050100",
            stage_root=tmp_path / "stage",
        )


def test_cycle_resource_limits_block_before_archive_tool_or_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    _seed_cycle(config.journal_root, continuation=True)
    config = retention.SchedulerJournalRetentionConfig(
        **{**config.__dict__, "max_cycle_members": 2, "max_cycle_bytes": 10_000, "max_archive_bytes": 10_000}
    )
    calls = 0

    def never_run(*_args: Any, **_kwargs: Any) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(archive, "_run_archive_toolchain", never_run)
    payload = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert payload["cycles"][0]["reason"] == "archive_member_limit_exceeded"
    assert calls == 0
    assert not (_archive_path(config)).exists()


@pytest.mark.parametrize("limit_delta", [0, -1])
def test_cycle_byte_limit_accepts_boundary_and_blocks_one_over_before_archive_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_delta: int,
) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    members = _seed_cycle(config.journal_root)
    expected_bytes = sum(path.stat().st_size for path in members)
    config = retention.SchedulerJournalRetentionConfig(
        **{**config.__dict__, "max_cycle_bytes": expected_bytes + limit_delta}
    )
    calls = 0
    original_run = archive._run_archive_toolchain

    def record_archive_tool(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        original_run(*args, **kwargs)

    monkeypatch.setattr(archive, "_run_archive_toolchain", record_archive_tool)
    payload = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    if limit_delta == 0:
        assert payload["counts"]["archived"] == 1
        assert calls == 1
        assert all(not path.exists() for path in members)
    else:
        assert payload["cycles"][0]["reason"] == "archive_cycle_byte_limit_exceeded"
        assert calls == 0
        assert all(path.exists() for path in members)


def test_archive_byte_limit_accepts_exact_output_and_blocks_one_over_without_hot_removal(
    tmp_path: Path,
) -> None:
    def seeded_config(name: str) -> tuple[retention.SchedulerJournalRetentionConfig, list[Path]]:
        config = _config(tmp_path / name, enabled=True, dry_run=False)
        members = _seed_cycle(config.journal_root)
        for member in members:
            os.utime(member, (0, 0))
        return config, members

    probe_config, _probe_members = seeded_config("probe")
    probe = retention.run_retention(
        probe_config,
        now=NOW,
        frontier=_frontier(),
        receipt_reservation=_reservation(probe_config),
    )
    assert probe["counts"]["archived"] == 1
    archive_bytes = _archive_path(probe_config).stat().st_size
    assert archive_bytes > 1

    exact_config, exact_members = seeded_config("exact")
    exact_config = retention.SchedulerJournalRetentionConfig(
        **{**exact_config.__dict__, "max_archive_bytes": archive_bytes}
    )
    exact = retention.run_retention(
        exact_config,
        now=NOW,
        frontier=_frontier(),
        receipt_reservation=_reservation(exact_config),
    )
    assert exact["counts"]["archived"] == 1
    assert all(not path.exists() for path in exact_members)

    over_config, over_members = seeded_config("one-over")
    over_config = retention.SchedulerJournalRetentionConfig(
        **{**over_config.__dict__, "max_archive_bytes": archive_bytes - 1}
    )
    over = retention.run_retention(
        over_config,
        now=NOW,
        frontier=_frontier(),
        receipt_reservation=_reservation(over_config),
    )
    assert over["cycles"][0]["reason"] == "archive_byte_limit_exceeded"
    assert not _archive_path(over_config).exists()
    assert all(path.exists() for path in over_members)


def test_direct_config_over_hard_archive_cap_blocks_before_archive_or_hot_removal(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    members = _seed_cycle(config.journal_root)
    config = retention.SchedulerJournalRetentionConfig(
        **{**config.__dict__, "max_archive_bytes": retention.MAX_ARCHIVE_BYTES + 1}
    )

    payload = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert payload["preflight_blockers"] == ["archive_limit_invalid"]
    assert payload["counts"]["archived"] == 0
    assert not _archive_path(config).exists()
    assert all(path.exists() for path in members)


def test_config_from_env_requires_scheduler_timing_and_strict_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    (root / "journal").mkdir(parents=True)
    (root / "evidence").mkdir()
    monkeypatch.setenv("NHMS_SCHEDULER_ALLOWED_ROOTS", str(root))
    monkeypatch.setenv("NHMS_SCHEDULER_JOURNAL_ROOT", str(root / "journal"))
    monkeypatch.setenv("NHMS_SCHEDULER_JOURNAL_ARCHIVE_ROOT", str(root / "archive"))
    monkeypatch.setenv("NHMS_SCHEDULER_EVIDENCE_ROOT", str(root / "evidence"))
    monkeypatch.setenv("NHMS_SCHEDULER_LOOKBACK_HOURS", "96")
    monkeypatch.setenv("NHMS_SCHEDULER_CYCLE_LAG_HOURS", "16")
    monkeypatch.setenv("NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC", "0,12")

    config, blockers = retention.config_from_env(entrypoint.build_parser().parse_args([]))

    assert config is not None
    assert blockers == []
    monkeypatch.setenv(
        "NHMS_SCHEDULER_JOURNAL_RETENTION_MAX_ARCHIVE_BYTES",
        str(retention.MAX_ARCHIVE_BYTES + 1),
    )
    capped, blockers = retention.config_from_env(entrypoint.build_parser().parse_args([]))
    assert capped is None
    assert blockers == ["nhms_scheduler_journal_retention_archive_limit_invalid"]
    monkeypatch.delenv("NHMS_SCHEDULER_JOURNAL_RETENTION_MAX_ARCHIVE_BYTES")
    monkeypatch.setenv("NHMS_SCHEDULER_LOOKBACK_HOURS", "")
    missing, blockers = retention.config_from_env(entrypoint.build_parser().parse_args([]))
    assert missing is None
    assert blockers == ["nhms_scheduler_lookback_hours_missing"]


def _interrupt_after_marker(
    monkeypatch: pytest.MonkeyPatch,
    published: str | None,
) -> tuple[Any, Any]:
    original_publish = archive._publish_bundle_directory
    original_move = archive.move_regular_file_no_follow_exclusive

    def fail_after_reservation(**kwargs: Any) -> None:
        archive.write_bytes_no_follow_exclusive(
            kwargs["bundle_root"] / archive._PUBLICATION_MARKER,
            archive._publication_payload(manifest=kwargs["manifest"]),
            containment_root=kwargs["archive_root"],
            require_durable_create=True,
        )
        raise retention.RetentionFailure("archive_publish_failed")

    def fail_after_requested_publish(*args: Any, **kwargs: Any) -> Path:
        moved = original_move(*args, **kwargs)
        if str(args[3]) == published:
            raise retention.RetentionFailure("archive_publish_failed")
        return moved

    if published is None:
        monkeypatch.setattr(archive, "_publish_bundle_directory", fail_after_reservation)
    else:
        monkeypatch.setattr(archive, "move_regular_file_no_follow_exclusive", fail_after_requested_publish)
    return original_publish, original_move


@pytest.mark.parametrize("published", [None, archive.ARCHIVE_NAME, archive.MANIFEST_NAME])
def test_marker_bound_retry_keeps_original_provenance_when_later_frontier_differs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    published: str | None,
) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    members = _seed_cycle(config.journal_root)
    original_publish, original_move = _interrupt_after_marker(monkeypatch, published)
    first = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))
    assert first["cycles"][0]["reason"] == "archive_publish_failed"
    marker = config.archive_root / "gfs" / "2026050100" / archive._PUBLICATION_MARKER
    original = json.loads(marker.read_text(encoding="utf-8"))["manifest"]
    assert original["created_at"] == "2026-08-31T12:00:00Z"
    assert original["frontier"]["receipt_path"] == "/receipts/pass.json"
    monkeypatch.setattr(archive, "_publish_bundle_directory", original_publish)
    monkeypatch.setattr(archive, "move_regular_file_no_follow_exclusive", original_move)

    second = retention.run_retention(
        config,
        now=NOW + timedelta(hours=1),
        frontier=_later_frontier(),
        receipt_reservation=_reservation(config),
    )

    final = json.loads(_manifest_path(config).read_text(encoding="utf-8"))
    assert second["counts"]["archived"] == 1
    assert all(not path.exists() for path in members)
    assert final["created_at"] == original["created_at"]
    assert final["frontier"] == original["frontier"]
    assert final["archive_sha256"] == original["archive_sha256"]
    assert final["members"] == original["members"]


@pytest.mark.parametrize("published", [None, archive.ARCHIVE_NAME, archive.MANIFEST_NAME])
def test_marker_bound_stable_identity_mismatch_conflicts_without_hot_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    published: str | None,
) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    members = _seed_cycle(config.journal_root)
    original_publish, original_move = _interrupt_after_marker(monkeypatch, published)
    first = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))
    assert first["cycles"][0]["reason"] == "archive_publish_failed"
    marker = config.archive_root / "gfs" / "2026050100" / archive._PUBLICATION_MARKER
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["manifest"]["members"][0]["sha256"] = "0" * 64
    marker.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    monkeypatch.setattr(archive, "_publish_bundle_directory", original_publish)
    monkeypatch.setattr(archive, "move_regular_file_no_follow_exclusive", original_move)

    second = retention.run_retention(
        config,
        now=NOW + timedelta(hours=1),
        frontier=_later_frontier(),
        receipt_reservation=_reservation(config),
    )

    assert second["cycles"][0]["reason"] == "archive_conflict"
    assert all(path.exists() for path in members)
    assert not _manifest_path(config).exists() or published == archive.MANIFEST_NAME


def test_complete_pair_retry_with_later_frontier_still_adopts_residual_subset(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    members = _seed_cycle(config.journal_root, continuation=True, pipeline_event=True)
    first = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))
    assert first["counts"]["archived"] == 1
    original = json.loads(_manifest_path(config).read_text(encoding="utf-8"))
    restored = restore.verify_and_restore(
        journal_root=config.journal_root,
        archive_root=config.archive_root,
        source_id="gfs",
        cycle="2026050100",
        stage_root=tmp_path / "stage",
    )
    (config.journal_root / restored["restored_paths"][0]).unlink()

    retry = retention.run_retention(
        config,
        now=NOW + timedelta(hours=1),
        frontier=_later_frontier(),
        receipt_reservation=_reservation(config),
    )

    assert retry["counts"]["archived"] == 1
    assert json.loads(_manifest_path(config).read_text(encoding="utf-8")) == original
    assert all(not path.exists() for path in members)


def test_restore_cli_maps_non_regular_lock_to_structured_refusal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    members = _seed_cycle(config.journal_root)
    assert retention.run_retention(
        config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config)
    )["counts"]["archived"] == 1
    lock_path = _cycle_lock_path(config.journal_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists() or lock_path.is_symlink():
        lock_path.unlink()
    lock_path.mkdir()
    stage = tmp_path / "stage"

    result = entrypoint.main(
        [
            "verify-restore",
            "--journal-root",
            str(config.journal_root),
            "--archive-root",
            str(config.archive_root),
            "--source-id",
            "gfs",
            "--cycle",
            "2026050100",
            "--stage-root",
            str(stage),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 2
    assert payload == {
        "reason": "cycle_lock_unavailable",
        "schema_version": retention.SCHEMA_VERSION,
        "status": "blocked",
    }
    assert not stage.exists()
    assert all(not path.exists() for path in members)


@pytest.mark.parametrize("enabled,dry_run", [(False, True), (True, True)])
def test_default_and_dry_run_cli_receipt_lock_failure_and_continue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    enabled: bool,
    dry_run: bool,
) -> None:
    config = _config(tmp_path, enabled=enabled, dry_run=dry_run)
    earlier = datetime(2026, 4, 1, tzinfo=UTC)
    later = datetime(2026, 5, 1, 12, tzinfo=UTC)
    earlier_members = _seed_cycle(config.journal_root, cycle=earlier)
    failing_members = _seed_cycle(config.journal_root)
    later_members = _seed_cycle(config.journal_root, cycle=later)
    lock_path = _cycle_lock_path(config.journal_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.mkdir()
    _install_cli_env(monkeypatch, config, enabled=enabled, dry_run=dry_run)
    monkeypatch.setattr(retention, "read_latest_pass_frontier", lambda *_args, **_kwargs: _frontier())

    result = entrypoint.main([])
    payload = json.loads(capsys.readouterr().out)
    receipt = json.loads(Path(payload["receipt_path"]).read_text(encoding="utf-8"))
    rows = {row["cycle_time"]: row for row in payload["cycles"]}

    assert result == 2
    assert payload["receipt_path"]
    assert receipt["receipt_status"] == "final"
    assert rows["2026-04-01T00:00:00Z"]["status"] == "planned"
    assert rows["2026-05-01T00:00:00Z"] == {
        **rows["2026-05-01T00:00:00Z"],
        "status": "blocked",
        "reason": "cycle_lock_unavailable",
    }
    assert rows["2026-05-01T12:00:00Z"]["status"] == "planned"
    assert "failed to acquire" not in capsys.readouterr().out
    assert all(path.exists() for path in [*earlier_members, *failing_members, *later_members])


def test_enforce_cli_finalizes_receipt_and_continues_after_cycle_lock_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    earlier = datetime(2026, 4, 1, tzinfo=UTC)
    later = datetime(2026, 5, 1, 12, tzinfo=UTC)
    earlier_members = _seed_cycle(config.journal_root, cycle=earlier)
    failing_members = _seed_cycle(config.journal_root)
    later_members = _seed_cycle(config.journal_root, cycle=later)
    lock_path = _cycle_lock_path(config.journal_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.mkdir()
    _install_cli_env(monkeypatch, config, enabled=True, dry_run=False)
    monkeypatch.setattr(retention, "read_latest_pass_frontier", lambda *_args, **_kwargs: _frontier())

    result = entrypoint.main([])
    payload = json.loads(capsys.readouterr().out)
    receipt = json.loads(Path(payload["receipt_path"]).read_text(encoding="utf-8"))
    rows = {row["cycle_time"]: row for row in payload["cycles"]}

    assert result == 2
    assert receipt["receipt_status"] == "final"
    assert rows["2026-04-01T00:00:00Z"]["status"] == "archived"
    assert rows["2026-05-01T00:00:00Z"]["status"] == "blocked"
    assert rows["2026-05-01T00:00:00Z"]["reason"] == "cycle_lock_unavailable"
    assert rows["2026-05-01T12:00:00Z"]["status"] == "archived"
    assert all(not path.exists() for path in [*earlier_members, *later_members])
    assert all(path.exists() for path in failing_members)
    assert "FILE_JOURNAL_WRITE_FAILED" not in Path(payload["receipt_path"]).read_text(encoding="utf-8")
