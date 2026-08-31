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
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
from tests.scheduler_journal_retention_fixtures import (
    NOW,
    OLD_CYCLE,
    _archive_path,
    _config,
    _direct_record,
    _frontier,
    _job,
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
