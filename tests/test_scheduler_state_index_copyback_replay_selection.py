"""Cycle/run-id selection, dry-run preview and destination bootstrap (#1611).

Partition of ``tests/test_scheduler_state_index_copyback_replay.py``; every case
below is a verbatim move. Shared fixtures live in
``tests/scheduler_state_index_copyback_replay_helpers.py``.
"""

from __future__ import annotations

import json

import pytest

from scripts import scheduler_state_index_copyback_replay as replay
from tests.scheduler_state_index_copyback_replay_helpers import (
    AUTHORITATIVE_RUN,
    IFS_RUN,
    Fixture,
    _apply_env,
    fixture_factory,  # noqa: F401  (registers the `fixture` fixture on this module)
)


def test_replay_dry_run_previews_without_touching_index_or_objects(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _apply_env(monkeypatch, fixture)
    index_before = fixture.destination_index.read_bytes()

    exit_code = replay.main(["--cycle", "GFS_2026072000"])

    assert exit_code == 0
    assert fixture.destination_index.read_bytes() == index_before
    assert not fixture.new_shared_object.exists()
    receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    assert receipt["schema_version"] == replay.SCHEMA_VERSION
    assert receipt["mode"] == "dry_run"
    assert receipt["requested_cycles"] == ["gfs_2026072000"]
    assert receipt["resolved_run_ids"] == [AUTHORITATIVE_RUN]
    assert receipt["destination_entry_count_before"] == 1
    assert receipt["destination_entry_count_after"] == 1
    assert receipt["preview_new_state_ids"] == ["fresh-state"]
    assert receipt["checkpoint_copied_count"] is None
    assert receipt["merge"] is None


def test_replay_enforce_publishes_missing_entries_and_is_idempotent(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _apply_env(monkeypatch, fixture)

    first = replay.main(["--cycle", "gfs_2026072000", "--enforce"])
    first_receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    entries_after_first = json.loads(fixture.destination_index.read_text(encoding="utf-8"))["entries"]
    second = replay.main(["--cycle", "gfs_2026072000", "--enforce"])
    second_receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))

    assert (first, second) == (0, 0)
    assert first_receipt["mode"] == "enforce"
    assert first_receipt["destination_entry_count_before"] == 1
    assert first_receipt["destination_entry_count_after"] == 2
    assert first_receipt["checkpoint_copied_count"] == 1
    assert first_receipt["checkpoint_reused_count"] == 0
    assert first_receipt["merge"]["published_entry_count"] == 2
    assert fixture.new_shared_object.read_bytes() == fixture.fresh_content
    # The historical entry stays published and its archived object stays gone.
    assert [entry["state_id"] for entry in entries_after_first] == ["archived-state", "fresh-state"]
    assert not fixture.archived_shared_object.exists()

    assert second_receipt["destination_entry_count_before"] == 2
    assert second_receipt["destination_entry_count_after"] == 2
    # The entry is already published byte-identically, so the repeat enforce
    # copies nothing at all (#1189 A2) instead of re-copying it as "reused".
    assert second_receipt["checkpoint_copied_count"] == 0
    assert second_receipt["checkpoint_reused_count"] == 0
    assert second_receipt["checkpoint_replaced_count"] == 0
    assert second_receipt["preview_new_state_ids"] == []
    assert json.loads(fixture.destination_index.read_text(encoding="utf-8"))["entries"] == entries_after_first


def test_replay_resolves_flat_cycle_id_and_skips_entries_without_one(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _apply_env(monkeypatch, fixture)

    exit_code = replay.main(["--cycle", "gfs_2026072000", "--enforce"])

    receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    # The source index also holds a historical entry, an entry of another cycle
    # and a cycle-less entry; none may be resolved into the authoritative set.
    assert receipt["source_entry_count"] == 4
    assert receipt["resolved_run_ids"] == [AUTHORITATIVE_RUN]
    assert receipt["matched_source_entry_count"] == 1
    published = json.loads(fixture.destination_index.read_text(encoding="utf-8"))["entries"]
    assert [entry["state_id"] for entry in published] == ["archived-state", "fresh-state"]


def test_replay_enforce_honors_every_repeated_cycle_flag(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The production recovery is `--cycle gfs_... --cycle ifs_...`; honouring
    # only the last flag would silently leave half the backlog behind.
    _apply_env(monkeypatch, fixture)

    exit_code = replay.main(
        ["--cycle", "gfs_2026072000", "--cycle", "IFS_2026072000", "--enforce"]
    )

    receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert receipt["requested_cycles"] == ["gfs_2026072000", "ifs_2026072000"]
    assert receipt["resolved_run_ids"] == sorted([AUTHORITATIVE_RUN, IFS_RUN])
    assert receipt["matched_source_entry_count"] == 2
    assert receipt["destination_entry_count_before"] == 1
    assert receipt["destination_entry_count_after"] == 3
    assert receipt["checkpoint_copied_count"] == 2
    published = json.loads(fixture.destination_index.read_text(encoding="utf-8"))["entries"]
    assert sorted(entry["state_id"] for entry in published) == [
        "archived-state",
        "fresh-state",
        "ifs-fresh-state",
    ]
    assert fixture.new_shared_object.read_bytes() == fixture.fresh_content
    assert fixture.ifs_shared_object.read_bytes() == fixture.ifs_content


def test_replay_enforce_refuses_missing_destination_index(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A wrong destination root (typo, unmounted NFS stub) must not be
    # bootstrapped into a fake canonical index holding only this replay.
    _apply_env(monkeypatch, fixture)
    empty_destination = fixture.root / "empty-shared-object-store"
    empty_destination.mkdir()
    monkeypatch.setenv(replay.DESTINATION_ROOT_ENV, str(empty_destination))

    exit_code = replay.main(["--cycle", "gfs_2026072000", "--enforce"])

    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert exit_code == 2
    assert payload["status"] == "refused"
    assert payload["reason"] == "destination_index_missing"
    assert not (empty_destination / "scheduler").exists()
    assert not (empty_destination / "states").exists()
    assert list(empty_destination.iterdir()) == []
    assert not (fixture.receipt_root / "latest.json").exists()


def test_replay_enforce_bootstraps_destination_index_when_allowed(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _apply_env(monkeypatch, fixture)
    empty_destination = fixture.root / "empty-shared-object-store"
    empty_destination.mkdir()
    monkeypatch.setenv(replay.DESTINATION_ROOT_ENV, str(empty_destination))

    exit_code = replay.main(["--cycle", "gfs_2026072000", "--enforce", "--allow-bootstrap"])

    receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert receipt["destination_index_existed"] is False
    assert receipt["allow_bootstrap"] is True
    assert receipt["destination_entry_count_before"] == 0
    assert receipt["destination_entry_count_after"] == 1
    assert receipt["checkpoint_copied_count"] == 1
    published = json.loads(
        (empty_destination / "scheduler/state-index/index-last.json").read_text(encoding="utf-8")
    )
    assert [entry["state_id"] for entry in published["entries"]] == ["fresh-state"]
    assert (empty_destination / "states/gfs/model_a/fresh/state.cfg.ic").read_bytes() == (
        fixture.fresh_content
    )


def test_replay_dry_run_previews_against_missing_destination_index(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _apply_env(monkeypatch, fixture)
    empty_destination = fixture.root / "empty-shared-object-store"
    empty_destination.mkdir()
    monkeypatch.setenv(replay.DESTINATION_ROOT_ENV, str(empty_destination))

    exit_code = replay.main(["--cycle", "gfs_2026072000"])

    receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert receipt["destination_index_existed"] is False
    assert receipt["destination_entry_count_before"] == 0
    assert receipt["preview_new_state_ids"] == ["fresh-state"]
    assert list(empty_destination.iterdir()) == []


def test_replay_run_ids_selection_requires_source_index_entries(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _apply_env(monkeypatch, fixture)
    index_before = fixture.destination_index.read_bytes()

    ok = replay.main(["--run-ids", f"{AUTHORITATIVE_RUN},{AUTHORITATIVE_RUN}", "--enforce"])
    assert ok == 0
    assert json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))[
        "resolved_run_ids"
    ] == [AUTHORITATIVE_RUN]

    index_after_ok = fixture.destination_index.read_bytes()
    refused = replay.main(["--run-ids", "unknown-run", "--enforce"])

    assert refused == 2
    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["reason"] == "run_ids_absent_from_source_index"
    assert payload["missing_run_ids"] == ["unknown-run"]
    assert fixture.destination_index.read_bytes() == index_after_ok
    assert index_before != index_after_ok


def test_replay_empty_cycle_resolution_fails_closed_without_writes(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _apply_env(monkeypatch, fixture)
    index_before = fixture.destination_index.read_bytes()

    exit_code = replay.main(["--cycle", "gfs_2026072012", "--enforce"])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["reason"] == "cycles_absent_from_source_index"
    assert payload["unresolved_cycles"] == ["gfs_2026072012"]
    assert fixture.destination_index.read_bytes() == index_before
    assert not fixture.new_shared_object.exists()
    assert not (fixture.receipt_root / "latest.json").exists()
