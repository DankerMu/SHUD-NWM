"""Planning, liveness, and receipt contracts for scheduler-journal retention."""

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
    _bytes,
    _config,
    _cycle_lock_path,
    _fixture_path,
    _frontier,
    _job,
    _record,
    _reservation,
    _seed_cycle,
    _write_json,
    _write_jsonl,
)

SchedulerJournalRetentionConfig = retention.SchedulerJournalRetentionConfig
ReceiptReservation = retention.ReceiptReservation
FrontierReadResult = retention.FrontierReadResult
reserve_receipt = retention.reserve_receipt

def test_default_dry_run_plans_complete_gfs_cycle_without_mutation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    members = _seed_cycle(config.journal_root, continuation=True, pipeline_event=True)
    direct_job = _job("gfs", OLD_CYCLE)
    direct = _write_json(
        config.journal_root / "pipeline-jobs" / f"{direct_job['job_id']}.json",
        _record("gfs", OLD_CYCLE, direct_job),
    )
    index = _write_json(_fixture_path(config, tmp_path / "state-index.json"), {"authority": "state"})
    before = {path: _bytes(path) for path in [*members, direct, index]}

    payload = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert payload["counts"]["planned"] == 1
    row = payload["cycles"][0]
    assert row["status"] == "planned"
    assert row["member_count"] == 4
    assert set(row["members"]) == {
        "latest/gfs/2026050100/model_a.json",
        "journal/gfs/2026050100.jsonl",
        "journal/gfs/2026050100.1.jsonl",
        "pipeline-events/gfs/2026050100.jsonl",
    }
    assert not (config.archive_root / "gfs" / "2026050100").exists()
    assert {path: _bytes(path) for path in before} == before


@pytest.mark.parametrize("source_id", ["gfs", "IFS"])
def test_enforce_archives_exact_hot_cycle_and_preserves_sibling_authority(tmp_path: Path, source_id: str) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    members = _seed_cycle(config.journal_root, source_id=source_id, continuation=True, pipeline_event=True)
    direct_job = _job(source_id, OLD_CYCLE)
    direct = _write_json(
        config.journal_root / "pipeline-jobs" / f"{direct_job['job_id']}.json",
        _record(source_id, OLD_CYCLE, direct_job),
    )
    inventory = _write_json(config.journal_root / "reconcile-inventory" / "anchor.json", {"authority": "inventory"})
    index = _write_json(_fixture_path(config, tmp_path / "state-index.json"), {"authority": "state"})
    before = {path: _bytes(path) for path in [direct, inventory, index]}

    payload = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert payload["counts"]["archived"] == 1
    row = payload["cycles"][0]
    assert row["status"] == "archived"
    archive_path = config.archive_root / source_id / "2026050100" / "bundle" / archive.ARCHIVE_NAME
    manifest_path = archive_path.with_name(archive.MANIFEST_NAME)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["archive_sha256"] == hashlib.sha256(archive_path.read_bytes()).hexdigest()
    assert [member["path"] for member in manifest["members"]] == sorted(row["members"])
    assert all(not path.exists() for path in members)
    assert {path: _bytes(path) for path in before} == before


def test_reserved_unbound_and_incomplete_accepted_master_remain_live(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    reserved = _job("gfs", OLD_CYCLE, status="reserved")
    paths = _seed_cycle(config.journal_root, job=reserved)
    incomplete_cycle = OLD_CYCLE - timedelta(hours=12)
    incomplete = _job("gfs", incomplete_cycle, status="succeeded", accepted=True)
    incomplete["candidate_projections"] = []
    _seed_cycle(config.journal_root, cycle=incomplete_cycle, job=incomplete)
    # The accepted-master projection row is intentionally old authority: its
    # malformed legacy form must block only its own cycle, not weaken the
    # reserved/unbound assertion above.

    payload = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert any(row["reason"] == "live_row" for row in payload["cycles"])
    assert all(path.exists() for path in paths)


def test_released_identity_blocked_row_without_inventory_anchor_remains_live(tmp_path: Path) -> None:
    from tests.test_file_orchestration_journal import _released_identity_blocked_master

    config = _config(tmp_path, enabled=True, dry_run=False)
    # Build the real current-contract release through the owner lifecycle. Its
    # inventory anchor was removed by the release itself, while cycle journal
    # authority remains and is the only retention source needed here.
    repository, record = _released_identity_blocked_master(config.journal_root.parent)
    released = repository.get_pipeline_job(str(record["job_id"]))
    assert released is not None
    cycle_time = datetime.fromisoformat(str(released["cycle_time"]).replace("Z", "+00:00"))
    source_id = str(released["source_id"])
    hot_member = config.journal_root / "journal" / source_id / f"{cycle_time.strftime('%Y%m%d%H')}.jsonl"

    payload = retention.run_retention(
        config, now=NOW.replace(year=2027), frontier=_frontier(None), receipt_reservation=_reservation(config)
    )

    assert payload["cycles"][0]["reason"] == "live_row"
    assert hot_member.exists()
    assert not any((config.journal_root / "reconcile-inventory").glob("*.json"))


@pytest.mark.parametrize(
    ("frontier", "expected"),
    [
        (_frontier(OLD_CYCLE), "pipeline_frontier_exempt"),
        (retention.FrontierReadResult(status="unavailable", reason="receipt_stale"), "receipt_stale"),
        (_frontier(None), "planned"),
    ],
)
def test_frontier_contract_is_fail_closed_or_explicitly_allows_null(
    tmp_path: Path,
    frontier: retention.FrontierReadResult,
    expected: str,
) -> None:
    config = _config(tmp_path)
    paths = _seed_cycle(config.journal_root)

    payload = retention.run_retention(config, now=NOW, frontier=frontier)

    row = payload["cycles"][0]
    assert expected in {row["reason"], row["status"]}
    assert all(path.exists() for path in paths)
    if expected == "receipt_stale":
        assert payload["preflight_blockers"] == ["receipt_stale"]


def test_invalid_safety_window_blocks_every_cycle(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    config = retention.SchedulerJournalRetentionConfig(**{**config.__dict__, "retention_days": 1})
    paths = _seed_cycle(config.journal_root)

    payload = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert payload["preflight_blockers"] == ["scheduler_window_invalid"]
    assert payload["counts"]["archived"] == 0
    assert all(path.exists() for path in paths)


def test_discovery_budget_is_aggregate_and_one_over_blocks_all_removal(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False, max_files=8)
    first = _seed_cycle(config.journal_root, cycle=OLD_CYCLE)
    second = _seed_cycle(config.journal_root, cycle=OLD_CYCLE - timedelta(hours=12))

    payload = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert payload["discovery"]["status"] == "ok"
    assert payload["counts"]["archived"] == 2
    config = _config(tmp_path / "one-over", enabled=True, dry_run=False, max_files=8)
    first = _seed_cycle(config.journal_root, cycle=OLD_CYCLE)
    second = _seed_cycle(config.journal_root, cycle=OLD_CYCLE - timedelta(hours=12))
    _write_json(config.journal_root / "latest" / "gfs" / "2026043012" / "extra.json", {"foreign": True})

    blocked = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert blocked["discovery"]["status"] == "blocked"
    assert blocked["counts"]["archived"] == 0
    assert all(path.exists() for path in [*first, *second])


def test_malformed_symlink_nonregular_unrecognised_and_gapped_members_block(tmp_path: Path) -> None:
    for kind in ("malformed", "symlink", "fifo", "unrecognised", "gapped"):
        config = _config(tmp_path / kind, enabled=True, dry_run=False)
        paths = _seed_cycle(config.journal_root)
        stamp = "2026050100"
        if kind == "malformed":
            (config.journal_root / "journal" / "gfs" / f"{stamp}.jsonl").write_text("not-json\n", encoding="utf-8")
        elif kind == "symlink":
            target = config.journal_root / "latest" / "gfs" / stamp / "model_a.json"
            target.unlink()
            target.symlink_to("/etc/passwd")
        elif kind == "fifo":
            fifo = config.journal_root / "journal" / "gfs" / f"{stamp}.jsonl"
            fifo.unlink()
            os.mkfifo(fifo)
        elif kind == "unrecognised":
            (config.journal_root / "journal" / "gfs" / "unexpected.txt").write_text("x", encoding="utf-8")
        else:
            _write_jsonl(config.journal_root / "journal" / "gfs" / f"{stamp}.2.jsonl", [])

        payload = retention.run_retention(
            config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config)
        )

        assert payload["counts"]["archived"] == 0, kind
        assert any(path.exists() or path.is_symlink() for path in paths), kind


def test_lock_contention_skips_without_wait(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    paths = _seed_cycle(config.journal_root)
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
        payload = retention.run_retention(
            config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config)
        )
    finally:
        release.set()
        thread.join(timeout=5)

    assert payload["cycles"][0]["reason"] == "in_flight"
    assert all(path.exists() for path in paths)


def test_matching_archive_retries_partial_cleanup_and_conflict_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    paths = _seed_cycle(config.journal_root)
    repository = FileOrchestrationJournalRepository(config.journal_root)
    original = repository._remove_retention_members_unlocked
    calls = 0

    def partial(*args: Any, **kwargs: Any):
        nonlocal calls
        calls += 1
        if calls == 1:
            return type(original(*args, **kwargs))(status="partial", reason="injected")
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, "_remove_retention_members_unlocked", partial)
    monkeypatch.setattr(retention, "FileOrchestrationJournalRepository", lambda *_args, **_kwargs: repository)
    first = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))
    assert first["cycles"][0]["status"] == "partial"
    monkeypatch.setattr(repository, "_remove_retention_members_unlocked", original)

    # Simulate a real partial unlink: retain one manifest-bound member for the
    # retry rather than merely returning a partial result after full cleanup.
    _seed_cycle(config.journal_root)
    second = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))
    assert second["cycles"][0]["status"] == "archived"
    assert all(not path.exists() for path in paths)

    config = _config(tmp_path / "conflict", enabled=True, dry_run=False)
    paths = _seed_cycle(config.journal_root)
    archive_dir = config.archive_root / "gfs" / "2026050100"
    archive_dir.mkdir(parents=True)
    (archive_dir / archive.ARCHIVE_NAME).write_bytes(b"not-an-archive")
    _write_json(archive_dir / archive.MANIFEST_NAME, {"schema_version": archive.MANIFEST_SCHEMA_VERSION})
    monkeypatch.setattr(retention, "FileOrchestrationJournalRepository", FileOrchestrationJournalRepository)
    conflict = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))
    assert conflict["cycles"][0]["reason"] == "archive_conflict"
    assert all(path.exists() for path in paths)


def test_non_regular_cycle_lock_blocks_one_cycle_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator.chain_types import OrchestratorError

    config = _config(tmp_path, enabled=True, dry_run=False)
    earlier = datetime(2026, 4, 1, tzinfo=UTC)
    later = datetime(2026, 5, 1, 12, tzinfo=UTC)
    earlier_members = _seed_cycle(config.journal_root, cycle=earlier)
    failing_members = _seed_cycle(config.journal_root)
    later_members = _seed_cycle(config.journal_root, cycle=later)
    lock_path = _cycle_lock_path(config.journal_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.mkdir()

    payload = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))
    rows = {row["cycle_time"]: row for row in payload["cycles"]}
    assert rows["2026-04-01T00:00:00Z"]["status"] == "archived"
    assert rows["2026-05-01T00:00:00Z"]["status"] == "blocked"
    assert rows["2026-05-01T00:00:00Z"]["reason"] == "cycle_lock_unavailable"
    assert rows["2026-05-01T12:00:00Z"]["status"] == "archived"
    assert all(not path.exists() for path in [*earlier_members, *later_members])
    assert all(path.exists() for path in failing_members)

    def fail_root(self: FileOrchestrationJournalRepository) -> None:
        raise OrchestratorError(
            "FILE_JOURNAL_WRITE_FAILED",
            "failed to create file orchestration journal root",
            {"surface": "journal_root"},
        )

    monkeypatch.setattr(FileOrchestrationJournalRepository, "_ensure_root_unlocked", fail_root)
    global_payload = retention.run_retention(
        config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config)
    )
    assert global_payload["schema_version"] == retention.SCHEMA_VERSION
    assert global_payload["cycles"] or global_payload["preflight_blockers"]
    assert "failed to create file orchestration journal root" not in json.dumps(global_payload)


# --- #2384: one journal-root decision (verify_journal_root_authority), the
# retention/restore ``journal_root_*`` vocabulary kept as a translation.

_UNEXPANDABLE = "~nosuchuser_zz"


def _real_base(tmp_path: Path) -> Path:
    # macOS tmp_path sits under the /var -> /private/var symlink, which every
    # no-follow walk refuses; the shapes below need a symlink-free base.
    return Path(os.path.realpath(tmp_path))


def _journal_root_shape(base: Path, shape: str) -> str:
    if shape == "missing":
        return str(base / "missing")
    if shape == "deep_missing":
        return str(base / "a" / "b" / "c")
    if shape == "leaf_symlink":
        (base / "real").mkdir()
        (base / "link").symlink_to(base / "real", target_is_directory=True)
        return str(base / "link")
    if shape == "dangling_symlink":
        (base / "dangling").symlink_to(base / "nowhere", target_is_directory=True)
        return str(base / "dangling")
    if shape == "regular_file":
        (base / "file").write_text("x", encoding="utf-8")
        return str(base / "file")
    if shape == "under_file":
        (base / "file").write_text("x", encoding="utf-8")
        return str(base / "file" / "child")
    if shape == "symlinked_ancestor":
        (base / "real" / "journal").mkdir(parents=True)
        (base / "linkdir").symlink_to(base / "real", target_is_directory=True)
        return str(base / "linkdir" / "journal")
    if shape == "dotdot_component":
        (base / "a").mkdir()
        (base / "journal").mkdir()
        return str(base / "a" / ".." / "journal")
    if shape == "relative":
        return "relative/journal"
    if shape == "permission_denied_ancestor":
        locked = base / "locked"
        (locked / "journal").mkdir(parents=True)
        locked.chmod(0o000)
        return str(locked / "journal")
    raise AssertionError(shape)


# Expected reasons are the pre-#2384 vocabulary of the retention preflight,
# restated from its published receipt contract (design D4), not derived by
# calling either implementation.
_JOURNAL_ROOT_PARITY = [
    ("missing", "journal_root_unavailable"),
    ("deep_missing", "journal_root_unavailable"),
    ("leaf_symlink", "journal_root_symlink"),
    ("dangling_symlink", "journal_root_symlink"),
    ("regular_file", "journal_root_not_directory"),
    ("under_file", "journal_root_unavailable"),
    ("symlinked_ancestor", "journal_root_unsafe"),
    ("dotdot_component", "journal_root_unsafe"),
    ("relative", "journal_root_not_absolute"),
    ("permission_denied_ancestor", "journal_root_unavailable"),
]


def _restore_permissions(base: Path) -> None:
    locked = base / "locked"
    if locked.exists():
        locked.chmod(0o755)


@pytest.mark.parametrize("entry", ["retention", "restore"])
@pytest.mark.parametrize("shape,reason", _JOURNAL_ROOT_PARITY)
def test_journal_root_refusal_vocabulary_is_unchanged_at_both_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    reason: str,
    entry: str,
) -> None:
    if shape == "permission_denied_ancestor" and os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")
    base = _real_base(tmp_path)
    try:
        journal_root = _journal_root_shape(base, shape)
        if entry == "retention":
            monkeypatch.setenv("NHMS_SCHEDULER_ALLOWED_ROOTS", str(base))
            config, blockers = retention.config_from_env(
                entrypoint.build_parser().parse_args(["--journal-root", journal_root])
            )
            assert config is None
            assert blockers == [reason]
        else:
            with pytest.raises(retention.RetentionFailure) as refused:
                restore.verify_and_restore(
                    journal_root=journal_root,
                    archive_root=base / "archive",
                    source_id="gfs",
                    cycle="2026050100",
                    stage_root=base / "stage",
                )
            assert refused.value.reason == reason
    finally:
        _restore_permissions(base)


def _retention_cli_env(monkeypatch: pytest.MonkeyPatch, base: Path) -> dict[str, Path]:
    roots = {name: base / name for name in ("journal", "archive", "evidence")}
    roots["journal"].mkdir()
    roots["evidence"].mkdir()
    for name, value in {
        "NHMS_SCHEDULER_ALLOWED_ROOTS": str(base),
        "NHMS_SCHEDULER_JOURNAL_ROOT": str(roots["journal"]),
        "NHMS_SCHEDULER_JOURNAL_ARCHIVE_ROOT": str(roots["archive"]),
        "NHMS_SCHEDULER_EVIDENCE_ROOT": str(roots["evidence"]),
        "NHMS_SCHEDULER_LOOKBACK_HOURS": "96",
        "NHMS_SCHEDULER_CYCLE_LAG_HOURS": "16",
        "NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC": "0,12",
    }.items():
        monkeypatch.setenv(name, value)
    return roots


def test_unexpandable_journal_root_is_a_typed_retention_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _retention_cli_env(monkeypatch, _real_base(tmp_path))
    config, blockers = retention.config_from_env(
        entrypoint.build_parser().parse_args(["--journal-root", f"{_UNEXPANDABLE}/journal"])
    )
    assert config is None
    assert blockers == ["journal_root_not_absolute"]

    monkeypatch.setenv("NHMS_SCHEDULER_JOURNAL_ROOT", f"{_UNEXPANDABLE}/journal")
    config, blockers = retention.config_from_env(entrypoint.build_parser().parse_args([]))
    assert config is None
    assert blockers == ["journal_root_not_absolute"]


def test_unexpandable_journal_root_cli_prints_preflight_blocked_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _retention_cli_env(monkeypatch, _real_base(tmp_path))

    result = entrypoint.main(["--journal-root", f"{_UNEXPANDABLE}/journal"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert result == 2
    assert payload["status"] == "preflight_blocked"
    assert payload["preflight_blockers"] == ["journal_root_not_absolute"]
    assert captured.err == ""


@pytest.mark.parametrize(
    "field,override",
    [
        ("allowed_root", {"NHMS_SCHEDULER_ALLOWED_ROOTS": f"{_UNEXPANDABLE}/allowed"}),
        ("archive_root", {"NHMS_SCHEDULER_JOURNAL_ARCHIVE_ROOT": f"{_UNEXPANDABLE}/archive"}),
        ("evidence_root", {"NHMS_SCHEDULER_EVIDENCE_ROOT": f"{_UNEXPANDABLE}/evidence"}),
    ],
)
def test_unexpandable_non_journal_retention_roots_are_their_own_not_absolute_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    override: dict[str, str],
) -> None:
    _retention_cli_env(monkeypatch, _real_base(tmp_path))
    for name, value in override.items():
        monkeypatch.setenv(name, value)

    config, blockers = retention.config_from_env(entrypoint.build_parser().parse_args([]))

    assert config is None
    assert blockers == [f"{field}_not_absolute"]


def _verify_restore_argv(base: Path, **overrides: str) -> list[str]:
    values = {
        "journal_root": str(base / "journal"),
        "archive_root": str(base / "archive"),
        "stage_root": str(base / "stage"),
        **overrides,
    }
    return [
        "verify-restore",
        "--journal-root",
        values["journal_root"],
        "--archive-root",
        values["archive_root"],
        "--source-id",
        "gfs",
        "--cycle",
        "2026050100",
        "--stage-root",
        values["stage_root"],
    ]


@pytest.mark.parametrize(
    "override,reason",
    [
        ({"journal_root": f"{_UNEXPANDABLE}/journal"}, "journal_root_not_absolute"),
        ({"archive_root": f"{_UNEXPANDABLE}/archive"}, "archive_root_not_absolute"),
        ({"stage_root": f"{_UNEXPANDABLE}/x"}, "stage_root_not_absolute"),
    ],
)
def test_verify_restore_cli_reports_unexpandable_roots_as_typed_refusals(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    override: dict[str, str],
    reason: str,
) -> None:
    base = _real_base(tmp_path)
    (base / "journal").mkdir()
    (base / "archive").mkdir()

    result = entrypoint.main(_verify_restore_argv(base, **override))

    captured = capsys.readouterr()
    assert result == 2
    assert json.loads(captured.out) == {
        "reason": reason,
        "schema_version": retention.SCHEMA_VERSION,
        "status": "blocked",
    }
    assert captured.err == ""
    assert not (base / "stage").exists()


def test_journal_root_decision_belongs_to_the_authority_at_both_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _real_base(tmp_path)
    roots = _retention_cli_env(monkeypatch, base)
    (base / "archive").mkdir()
    settings: list[str] = []
    # Reached through the retention module: the error type its adapter catches.
    OrchestratorError = retention.OrchestratorError

    def refuse(journal_root: str | Path, *, setting: str) -> Path:
        settings.append(setting)
        raise OrchestratorError(
            "FILE_JOURNAL_INVALID_ROOT",
            "refused",
            {"error_type": "SafeFilesystemError", "journal_root": str(journal_root), "setting": setting},
        )

    monkeypatch.setattr(retention, "verify_journal_root_authority", refuse)

    # The root is a real directory: only the authority's refusal can block it.
    config, blockers = retention.config_from_env(entrypoint.build_parser().parse_args([]))
    assert config is None
    assert blockers == ["journal_root_unsafe"]
    config, blockers = retention.config_from_env(
        entrypoint.build_parser().parse_args(["--journal-root", str(roots["journal"])])
    )
    assert config is None
    assert blockers == ["journal_root_unsafe"]
    with pytest.raises(retention.RetentionFailure) as refused:
        restore.verify_and_restore(
            journal_root=roots["journal"],
            archive_root=base / "archive",
            source_id="gfs",
            cycle="2026050100",
            stage_root=base / "stage",
        )
    assert refused.value.reason == "journal_root_unsafe"
    assert isinstance(refused.value.__cause__, OrchestratorError)
    assert settings == ["NHMS_SCHEDULER_JOURNAL_ROOT", "--journal-root", "--journal-root"]
