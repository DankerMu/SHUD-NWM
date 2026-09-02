"""Read-cache and append-invalidation tests for the file journal.

Covers the stat-identity byte cache (`_read_bytes_limited_cached`), the
prevalidated fast decode, and the append-time rows-cache sweep that makes
the next read recompute from records committed in the same write window.
"""

from __future__ import annotations

import ast
import contextlib
import json
import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from packages.common.safe_fs import SafeFilesystemError, read_bytes_limited_no_follow
from services.orchestrator import file_orchestration_journal as journal_module
from services.orchestrator.file_orchestration_journal import (
    FileOrchestrationJournalError,
    FileOrchestrationJournalRepository,
)
from tests.test_file_orchestration_journal import _join_all
from workers.data_adapters.base import cycle_id_for, format_cycle_time


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


CYCLE_TIME = _dt("2026-06-28T00:00:00Z")
CYCLE_SEGMENT = format_cycle_time(CYCLE_TIME)


def _run_id(model_id: str = "model_a") -> str:
    return f"fcst_gfs_{CYCLE_SEGMENT}_{model_id}"


def _basin_manifest(model_id: str = "model_a") -> dict[str, Any]:
    return {
        "run_id": _run_id(model_id),
        "run_type": "forecast",
        "scenario_id": "scenario_a",
        "source_id": "gfs",
        "cycle_time": CYCLE_TIME.isoformat(),
        "start_time": CYCLE_TIME.isoformat(),
        "end_time": CYCLE_TIME.isoformat(),
        "model": {"model_id": model_id, "basin_version_id": "basin_version_a"},
        "forcing": {"forcing_version_id": f"forc_gfs_{CYCLE_SEGMENT}_{model_id}"},
        "outputs": {
            "run_manifest_uri": "s3://nhms/manifests/run.json",
            "output_uri": "s3://nhms/runs/output",
            "log_uri": "s3://nhms/logs/run.log",
        },
    }


def _candidate_state(
    repository: FileOrchestrationJournalRepository,
    *,
    model_id: str = "model_a",
) -> dict[str, Any] | None:
    return repository.candidate_state(
        source_id="gfs",
        cycle_time=CYCLE_TIME,
        model_id=model_id,
        run_id=_run_id(model_id),
        forcing_version_id=f"forc_gfs_{CYCLE_SEGMENT}_{model_id}",
        candidate_id=f"gfs:{CYCLE_TIME.isoformat()}:{model_id}:forecast_gfs_deterministic",
        job_limit=100,
        event_limit=100,
    )


def _latest_path(root: Path, model_id: str = "model_a") -> Path:
    return root / "latest" / "gfs" / CYCLE_SEGMENT / f"{model_id}.json"


def _job_record(model_id: str = "model_a") -> dict[str, Any]:
    return {
        "job_id": f"job_{_run_id(model_id)}_forecast",
        "run_id": _run_id(model_id),
        "cycle_id": cycle_id_for("gfs", CYCLE_TIME),
        "job_type": "forecast",
        "model_id": model_id,
        "status": "running",
        "stage": "forecast",
        "slurm_job_id": "3001",
    }


def test_read_cache_hit_skips_reread_and_returns_fresh_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    target = root / "pipeline-jobs" / "job_a.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"a": {"b": 1}}), encoding="utf-8")

    calls = 0
    real_reader = journal_module.read_bytes_limited_no_follow

    def counting_reader(path: Path, **kwargs: Any) -> bytes:
        nonlocal calls
        calls += 1
        return real_reader(path, **kwargs)

    monkeypatch.setattr(journal_module, "read_bytes_limited_no_follow", counting_reader)

    first = repository._read_optional_json(target)
    second = repository._read_optional_json(target)

    assert calls == 1
    assert first == {"a": {"b": 1}}
    assert second == {"a": {"b": 1}}
    assert first is not second
    first["a"]["b"] = 999
    assert repository._read_optional_json(target) == {"a": {"b": 1}}


def test_read_cache_misses_after_rewrite_and_append(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    target = root / "journal" / "gfs" / f"{CYCLE_SEGMENT}.jsonl"
    target.parent.mkdir(parents=True)
    row = {"schema_version": "x", "value": 1}
    target.write_text(json.dumps(row) + "\n", encoding="utf-8")

    assert len(repository._read_jsonl(target)) == 1

    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"schema_version": "x", "value": 2}) + "\n")
    assert len(repository._read_jsonl(target)) == 2

    replacement = target.with_suffix(".tmp")
    replacement.write_text(json.dumps({"schema_version": "x", "value": 3}) + "\n", encoding="utf-8")
    os.replace(replacement, target)
    records = repository._read_jsonl(target)
    assert len(records) == 1
    assert records[0]["value"] == 3


def test_read_cache_deleted_file_returns_missing(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    target = root / "pipeline-jobs" / "job_a.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"a": 1}), encoding="utf-8")

    assert repository._read_optional_json(target) == {"a": 1}
    target.unlink()
    assert repository._read_optional_json(target) is None


def test_read_cache_symlink_swap_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    target = root / "pipeline-jobs" / "job_a.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"a": 1}), encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"evil": True}), encoding="utf-8")

    assert repository._read_optional_json(target) == {"a": 1}
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(FileOrchestrationJournalError):
        repository._read_optional_json(target)


def test_read_cache_malformed_json_errors_repeat(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    target = root / "pipeline-jobs" / "job_a.json"
    target.parent.mkdir(parents=True)
    target.write_text("{not json", encoding="utf-8")

    for _ in range(2):
        with pytest.raises(FileOrchestrationJournalError):
            repository._read_optional_json(target)


def test_read_cache_byte_limit_still_enforced(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root, max_bytes=16)
    target = root / "pipeline-jobs" / "job_a.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"a": "x" * 64}), encoding="utf-8")

    for _ in range(2):
        with pytest.raises(FileOrchestrationJournalError):
            repository._read_optional_json(target)


def test_latest_view_contains_event_appended_in_same_window(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    repository.ensure_forecast_cycle(source_id="gfs", cycle_time=CYCLE_TIME)
    repository.create_hydro_run_from_basin({"source_id": "gfs"}, _basin_manifest())
    repository.upsert_pipeline_job(_job_record())

    event = repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id=_job_record()["job_id"],
        event_type="status_change",
        status_from="pending",
        status_to="running",
    )

    latest = json.loads(_latest_path(root).read_text(encoding="utf-8"))
    event_ids = {str(row.get("event_id")) for row in latest["pipeline_events"]}
    assert str(event["event_id"]) in event_ids


def test_latest_view_reflects_terminal_status_immediately(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    repository.ensure_forecast_cycle(source_id="gfs", cycle_time=CYCLE_TIME)
    repository.create_hydro_run_from_basin({"source_id": "gfs"}, _basin_manifest())

    repository.update_hydro_run_status(_run_id(), "published", slurm_job_id="3001")

    latest = json.loads(_latest_path(root).read_text(encoding="utf-8"))
    assert latest["hydro_run"]["status"] == "published"
    fresh = FileOrchestrationJournalRepository(root)
    assert fresh.has_completed_pipeline(source_id="gfs", cycle_time=CYCLE_TIME, model_id="model_a") is True
    assert (
        repository.has_completed_pipeline(source_id="gfs", cycle_time=CYCLE_TIME, model_id="model_a")
        is True
    )


def test_writer_view_matches_fresh_instance_after_each_write(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)

    def assert_views_match() -> None:
        fresh = FileOrchestrationJournalRepository(root)
        assert _candidate_state(repository) == _candidate_state(fresh)
        assert repository.has_completed_pipeline(
            source_id="gfs", cycle_time=CYCLE_TIME, model_id="model_a"
        ) == fresh.has_completed_pipeline(source_id="gfs", cycle_time=CYCLE_TIME, model_id="model_a")

    repository.ensure_forecast_cycle(source_id="gfs", cycle_time=CYCLE_TIME)
    assert_views_match()
    repository.create_hydro_run_from_basin({"source_id": "gfs"}, _basin_manifest())
    assert_views_match()
    repository.upsert_pipeline_job(_job_record())
    assert_views_match()
    repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id=_job_record()["job_id"],
        event_type="status_change",
        status_from="pending",
        status_to="running",
    )
    assert_views_match()
    repository.update_hydro_run_status(_run_id(), "succeeded", slurm_job_id="3001")
    assert_views_match()
    repository.update_forecast_cycle_status(
        source_id="gfs",
        cycle_time=CYCLE_TIME,
        status="complete",
    )
    assert_views_match()


def test_cycle_sweep_materializes_consistent_latest_sequence(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    repository.ensure_forecast_cycle(source_id="gfs", cycle_time=CYCLE_TIME)
    repository.create_hydro_run_from_basin({"source_id": "gfs"}, _basin_manifest("model_a"))
    repository.create_hydro_run_from_basin({"source_id": "gfs"}, _basin_manifest("model_b"))

    repository.insert_pipeline_event(
        entity_type="forecast_cycle",
        entity_id=cycle_id_for("gfs", CYCLE_TIME),
        event_type="status_change",
        status_from="discovered",
        status_to="complete",
    )

    journal_path = root / "journal" / "gfs" / f"{CYCLE_SEGMENT}.jsonl"
    max_sequence = max(
        int(json.loads(line)["sequence"])
        for line in journal_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    sequences = set()
    for model_id in ("model_a", "model_b"):
        latest = json.loads(_latest_path(root, model_id).read_text(encoding="utf-8"))
        sequences.add(int(latest["replay"]["latest_sequence"]))
    assert sequences == {max_sequence}


# --- #1595 / #1600 concurrent read integrity --------------------------------

# Concurrency discipline shared by every case below (tasks 3.1-3.12): the
# `_join_all` / `_hammer_until` harness is imported from the journal suite
# (daemon threads, one 30s budget, `stop` event), and every barrier/event has
# a timeout so a peer that dies early cannot park a thread holding
# `_write_lock` plus the cycle flock until interpreter exit.


def _write_latest_view(
    root: Path, *, cycle_time: datetime = CYCLE_TIME, model_id: str = "model_a", status: str = "running"
) -> None:
    """Write a valid latest view that `_cycle_rows` will consume.

    The view must satisfy `_apply_latest_view`'s schema/identity contract so a
    fresh recompute lands `status` in the returned rows.
    """
    segment = format_cycle_time(cycle_time)
    view = {
        "schema_version": journal_module.FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION,
        "generated_at": cycle_time.isoformat(),
        "source_id": "gfs",
        "cycle_time": cycle_time.isoformat(),
        "model_id": model_id,
        "hydro_run": {
            "run_id": f"fcst_gfs_{segment}_{model_id}",
            "source_id": "gfs",
            "cycle_time": cycle_time.isoformat(),
            "model_id": model_id,
            "status": status,
        },
        "replay": {"latest_sequence": 1},
    }
    path = root / "latest" / "gfs" / segment / f"{model_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(view), encoding="utf-8")


def test_cycle_write_window_owner_non_owner_revalidates_not_trusts_hit(
    tmp_path: Path,
) -> None:
    """#1595 red proof — task 3.1, strictly in this order.

    1. Thread A enters ``_locked_cycle_write(C_x)`` and parks on an event
       INSIDE the window body;
    2. while A holds the window, thread B reads a DIFFERENT cycle C_y —
       pre-fix the predicate ``self._write_lock.locked()`` is true, so B
       stores a ``fingerprint=None`` entry;
    3. C_y's source file is rewritten OUT-OF-BAND — direct file write, never
       the shared instance's write API (that would block on the lock A holds
       and deadlock the red proof itself);
    4. B reads C_y again — pre-fix it serves the unvalidated stale hit,
       post-fix it recomputes and returns the fresh rows.

    Step 2 must happen INSIDE the window: the window's entry clear wipes any
    pre-window warmup, so warming before A enters makes the red proof green
    pre-fix (design D11).  The first read populates the cache with a
    ``fingerprint=None`` entry; the out-of-band rewrite leaves that entry
    stale; the second read is the assertion.
    """
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)

    writer_cycle = _dt("2026-06-29T00:00:00Z")
    reader_cycle = _dt("2026-06-29T06:00:00Z")
    _write_latest_view(root, cycle_time=reader_cycle, status="running")

    in_window = threading.Event()
    first_read_done = threading.Event()
    rewrite_done = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []
    stop = threading.Event()

    def owner_thread() -> None:
        # Both of B's reads below must land inside this window: pre-fix the
        # predicate ``self._write_lock.locked()`` is true only while the
        # window is open, and only then does B store / trust a fingerprintless
        # entry.
        try:
            with repository._locked_cycle_write(source_id="gfs", cycle_time=writer_cycle):
                in_window.set()
                assert release.wait(timeout=15), "owner never released by the test"
        except BaseException as error:  # noqa: BLE001
            failures.append(error)
            stop.set()

    def reader_thread() -> None:
        try:
            assert in_window.wait(timeout=15), "owner never entered the window"
            first = repository._cycle_rows(source_id="gfs", cycle_time=reader_cycle, model_id="model_a")
            assert first.hydro_run["status"] == "running"
            first_read_done.set()
            assert rewrite_done.wait(timeout=15), "rewrite never signalled"
            second = repository._cycle_rows(source_id="gfs", cycle_time=reader_cycle, model_id="model_a")
            # Pre-fix this is a stale unvalidated hit ("running"); post-fix
            # the non-owner recomputes and returns the rewritten rows
            # ("succeeded").  Failures are collected into `failures` so the
            # main thread re-raises them (a bare thread assert would be
            # swallowed and the mutant would look green).
            assert second.hydro_run["status"] == "succeeded"
        except BaseException as error:  # noqa: BLE001
            failures.append(error)
            stop.set()
        finally:
            # Always release the owner so a red run does not park it for the
            # whole 15s event timeout.
            release.set()

    def rewriter_thread() -> None:
        # Out-of-band rewrite while the window is still open: a direct file
        # write, never the shared instance's write API (which would block on
        # the lock the owner holds and deadlock the red proof).
        try:
            assert first_read_done.wait(timeout=15), "reader never finished its first read"
            _write_latest_view(root, cycle_time=reader_cycle, status="succeeded")
            rewrite_done.set()
        except BaseException as error:  # noqa: BLE001
            failures.append(error)
            stop.set()

    threads = [
        threading.Thread(target=owner_thread, name="owner"),
        threading.Thread(target=reader_thread, name="reader"),
        threading.Thread(target=rewriter_thread, name="rewriter"),
    ]
    _join_all(threads, stop=stop)
    assert not failures, f"{type(failures[0]).__name__}: {failures[0]}"
    assert repository._cycle_write_owner is None


def test_cycle_write_window_owner_keeps_fingerprint_free_fast_path(tmp_path: Path) -> None:
    """#1595 — task 3.2: the owner hits its own cycle without a fingerprint.

    The window opens on an empty cache, so two in-window reads of the same
    cycle are required: the first is a miss (it cannot compute a hit), the
    second is the asserted hit.  A single call would satisfy "zero calls"
    with a pure miss.
    """
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    _write_latest_view(root, status="running")

    fingerprint_calls = 0
    real_fingerprint = repository._cycle_rows_source_fingerprint

    def counting_fingerprint(*, source_segments: Any, cycle_segment: str) -> Any:
        nonlocal fingerprint_calls
        fingerprint_calls += 1
        return real_fingerprint(source_segments=source_segments, cycle_segment=cycle_segment)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(repository, "_cycle_rows_source_fingerprint", counting_fingerprint)
    try:
        with repository._locked_cycle_write(source_id="gfs", cycle_time=CYCLE_TIME):
            repository._cycle_rows(source_id="gfs", cycle_time=CYCLE_TIME, model_id="model_a")
            assert fingerprint_calls == 0
            repository._cycle_rows(source_id="gfs", cycle_time=CYCLE_TIME, model_id="model_a")
            assert fingerprint_calls == 0
        assert repository._cycle_write_owner is None
    finally:
        monkeypatch.undo()


def test_cycle_write_window_owner_reading_other_cycle_revalidates(tmp_path: Path) -> None:
    """#1595 — task 3.2b: the keyed marker discriminates cycles.

    Owner inside C_x's window reading C_y must have the predicate FALSE and
    recompute.  The oracle is the fingerprint spy call count, NOT value
    freshness: the window opens on an empty cache, so the first in-window
    read of C_y is a fresh miss either way, which would let a bare-ident
    mutant stay green.
    """
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    _write_latest_view(root, status="running")
    other_cycle = _dt("2026-06-29T06:00:00Z")
    other_segment = format_cycle_time(other_cycle)
    other_path = root / "latest" / "gfs" / other_segment / "model_a.json"
    other_path.parent.mkdir(parents=True, exist_ok=True)
    other_view = {
        "schema_version": journal_module.FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION,
        "generated_at": other_cycle.isoformat(),
        "source_id": "gfs",
        "cycle_time": other_cycle.isoformat(),
        "model_id": "model_a",
        "hydro_run": {
            "run_id": f"fcst_gfs_{other_segment}_model_a",
            "source_id": "gfs",
            "cycle_time": other_cycle.isoformat(),
            "model_id": "model_a",
            "status": "running",
        },
        "replay": {"latest_sequence": 1},
    }
    other_path.write_text(json.dumps(other_view), encoding="utf-8")

    fingerprint_calls = 0
    real_fingerprint = repository._cycle_rows_source_fingerprint

    def counting_fingerprint(*, source_segments: Any, cycle_segment: str) -> Any:
        nonlocal fingerprint_calls
        fingerprint_calls += 1
        return real_fingerprint(source_segments=source_segments, cycle_segment=cycle_segment)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(repository, "_cycle_rows_source_fingerprint", counting_fingerprint)
    try:
        with repository._locked_cycle_write(source_id="gfs", cycle_time=CYCLE_TIME):
            rows = repository._cycle_rows(source_id="gfs", cycle_time=other_cycle, model_id="model_a")
            assert rows.hydro_run["status"] == "running"
            assert fingerprint_calls >= 1
        assert repository._cycle_write_owner is None
    finally:
        monkeypatch.undo()


def test_cycle_write_window_marker_cleared_on_yield_body_exception(tmp_path: Path) -> None:
    """#1595 — task 3.3a: an exception from the window body clears the marker."""
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)

    with pytest.raises(RuntimeError, match="body boom"):
        with repository._locked_cycle_write(source_id="gfs", cycle_time=CYCLE_TIME):
            raise RuntimeError("body boom")

    assert repository._cycle_write_owner is None


def test_cycle_write_window_marker_cleared_on_opening_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1595 — task 3.3b (the P1 guard): a failure while the window opens.

    ``_ensure_root_unlocked`` runs before the window body and outside the
    original try; a marker placed before it would leak a soon-to-be-recycled
    thread identity.  This is the only case that turns the misplaced-marker
    mutant red.
    """
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)

    def broken_ensure() -> None:
        raise journal_module.OrchestratorError("FILE_JOURNAL_WRITE_FAILED", "boom", {})

    monkeypatch.setattr(repository, "_ensure_root_unlocked", broken_ensure)

    with pytest.raises(journal_module.OrchestratorError):
        with repository._locked_cycle_write(source_id="gfs", cycle_time=CYCLE_TIME):
            pass

    assert repository._cycle_write_owner is None


def test_cycle_write_window_marker_cleared_on_flock_failure(tmp_path: Path) -> None:
    """#1595 — task 3.3a extension: a failure while acquiring the cycle flock.

    The flock acquisition happens inside the window's try, so its failure must
    also clear the marker.
    """
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)

    @contextlib.contextmanager
    def broken_lock(*, source_id: str, cycle_time: datetime) -> Iterator[None]:
        # The real flock helper wraps lock-path failures into
        # OrchestratorError; mirror its observable shape.
        raise journal_module.OrchestratorError("FILE_JOURNAL_WRITE_FAILED", "cannot lock", {})
        yield  # pragma: no cover

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(repository, "_cycle_file_lock_unlocked", broken_lock)
    try:
        with pytest.raises(journal_module.OrchestratorError):
            with repository._locked_cycle_write(source_id="gfs", cycle_time=CYCLE_TIME):
                pass
    finally:
        monkeypatch.undo()

    assert repository._cycle_write_owner is None


def test_non_owner_read_correct_even_with_cache_clear_disabled(tmp_path: Path) -> None:
    """#1595 — task 3.4: a non-owner read does not depend on clear granularity.

    Constraint 1: only non-owner reads are covered — the owner fast path
    depends on the window-entry wipe.  Constraint 2: the read must happen
    while another thread's write window is open.  Constraint 3: ``_cycle_rows_
    cache`` is a plain dict, whose ``clear`` cannot be monkeypatched on the
    instance, so a no-op-clear dict subclass replaces the attribute instead
    (the mechanism is written down here as the task requires).
    """
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)

    class NoopClearDict(dict):
        def clear(self) -> None:  # type: ignore[override]
            pass

    repository._cycle_rows_cache = NoopClearDict()

    writer_cycle = _dt("2026-06-29T00:00:00Z")
    reader_cycle = _dt("2026-06-29T06:00:00Z")
    _write_latest_view(root, cycle_time=reader_cycle, status="running")

    in_window = threading.Barrier(2, timeout=15)
    failures: list[BaseException] = []
    stop = threading.Event()

    def owner_thread() -> None:
        try:
            with repository._locked_cycle_write(source_id="gfs", cycle_time=writer_cycle):
                in_window.wait(timeout=15)
                in_window.wait(timeout=15)
        except BaseException as error:  # noqa: BLE001
            failures.append(error)
            stop.set()

    def reader_thread() -> None:
        try:
            in_window.wait(timeout=15)
            rows = repository._cycle_rows(source_id="gfs", cycle_time=reader_cycle, model_id="model_a")
            assert rows.hydro_run["status"] == "running"
            _write_latest_view(root, cycle_time=reader_cycle, status="succeeded")
            fresh = repository._cycle_rows(source_id="gfs", cycle_time=reader_cycle, model_id="model_a")
            assert fresh.hydro_run["status"] == "succeeded"
            in_window.wait(timeout=15)
        except BaseException as error:  # noqa: BLE001
            failures.append(error)
            stop.set()

    _join_all(
        [
            threading.Thread(target=owner_thread, name="owner"),
            threading.Thread(target=reader_thread, name="reader"),
        ],
        stop=stop,
    )
    assert not failures, f"{type(failures[0]).__name__}: {failures[0]}"
    assert repository._cycle_write_owner is None


# --- #1600: bounded retry at the journal read chokepoint ---------------------
#
# The retry lives in `_read_bytes_limited_cached` (the single helper every
# journal document and event-log read passes through), never inside the
# `safe_fs` primitives.  Every case below therefore drives the retry through
# the repository's chokepoint or its `_read_optional_json` / `_read_jsonl`
# wrappers; direct primitive calls still raise on the first identity mismatch
# (task 3.9b).


_REPLACEMENT_CONTENT = b'{"a": 2}'


@contextlib.contextmanager
def _replace_on_open(
    real_open: Any,
    *,
    replacements: dict[Path, int],
    always_replace: set[Path],
    opens: list[int] | None = None,
) -> Iterator[None]:
    """Wrap ``os.open`` so each call for a tracked path may perform one
    ``os.replace`` before opening.

    The identity check in ``safe_fs.open_file_no_follow`` stats the target
    (line ~265), then opens it (line ~275); an ``os.replace`` inserted
    between them lands inside that stat->open window and changes the inode.
    ``opens`` (when given) records every target-file open so tests can count
    attempts without re-patching ``os.open`` (a second patch would shadow
    this wrapper and no replace would ever happen).
    """
    import os as _os

    original_replace = _os.replace

    def replacement_open(path: Any, flags: Any, *args: Any, **kwargs: Any) -> int:
        text = _os.fsdecode(path)
        for tracked in list(replacements):
            if text.endswith(tracked.name):
                if opens is not None:
                    opens.append(1)
                if replacements[tracked] > 0:
                    replacements[tracked] -= 1
                    temp = tracked.with_suffix(".replace-target")
                    temp.write_bytes(_REPLACEMENT_CONTENT)
                    original_replace(temp, tracked)
                elif tracked in always_replace:
                    temp = tracked.with_suffix(".replace-target")
                    temp.write_bytes(_REPLACEMENT_CONTENT)
                    original_replace(temp, tracked)
                break
        return real_open(path, flags, *args, **kwargs)

    with mock.patch("os.open", replacement_open):
        yield


def _patch(target: str, replacement: Any) -> Any:
    return mock.patch(target, replacement)


def test_read_chokepoint_absorbs_single_mid_open_replacement(
    tmp_path: Path,
) -> None:
    """#1600 red proof — task 3.6.

    A single ``os.replace`` injected between the reader's stat and open is
    absorbed: pre-fix the read raises ``Target file changed while being
    opened``, post-fix it retries and returns the replacement's content.
    """
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    target = root / "pipeline-jobs" / "job_a.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"a": 1}), encoding="utf-8")

    replacements = {target: 1}
    real_open = os.open
    with _replace_on_open(real_open, replacements=replacements, always_replace=set()):
        payload = repository._read_optional_json(target)
    assert payload == {"a": 2}


def test_read_chokepoint_retries_exactly_the_bounded_attempts_then_fails_closed(
    tmp_path: Path,
) -> None:
    """#1600 — task 3.7: a relentless writer fails closed after exactly N tries.

    Every attempt observes a fresh replacement, so the read makes exactly
    ``MAX_FILE_JOURNAL_IDENTITY_RETRY_ATTEMPTS`` attempts and then raises the
    original ``SafeFilesystemError`` — never retrying without limit and never
    degrading to a default result.
    """
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    target = root / "pipeline-jobs" / "job_a.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"a": 1}), encoding="utf-8")

    replacements = {target: 10**6}
    real_open = os.open
    attempts = 0
    real_read = journal_module.read_bytes_limited_no_follow

    def counting_reader(path: Path, **kwargs: Any) -> bytes:
        nonlocal attempts
        attempts += 1
        return real_read(path, **kwargs)

    with _replace_on_open(real_open, replacements=replacements, always_replace={target}):
        with _patch("services.orchestrator.file_orchestration_journal.read_bytes_limited_no_follow", counting_reader):
            with pytest.raises(SafeFilesystemError):
                repository._read_bytes_limited_cached(target)
    assert attempts == journal_module.MAX_FILE_JOURNAL_IDENTITY_RETRY_ATTEMPTS


def test_read_chokepoint_safety_refusals_are_never_retried(tmp_path: Path) -> None:
    """#1600 — task 3.8: symlink / non-regular / containment refusals never retry.

    Exactly one attempt, and the refusal propagates unchanged.  The window
    swap to a symlink (ELOOP) is driven through the chokepoint with the same
    `_replace_on_open` injection the identity case uses; the other shapes
    (a symlink target, a directory occupying the target path, and a path
    outside the containment root) are direct fixtures the chokepoint's stat
    probe or hardened reader refuse on the first attempt.
    """
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    target = root / "pipeline-jobs" / "job_a.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"a": 1}), encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"evil": True}), encoding="utf-8")

    attempts = 0
    real_read = journal_module.read_bytes_limited_no_follow

    def counting_reader(path: Path, **kwargs: Any) -> bytes:
        nonlocal attempts
        attempts += 1
        return real_read(path, **kwargs)

    # 1. symlink target
    target.unlink()
    target.symlink_to(outside)
    with _patch("services.orchestrator.file_orchestration_journal.read_bytes_limited_no_follow", counting_reader):
        with pytest.raises(FileOrchestrationJournalError):
            repository._read_optional_json(target)
    assert attempts == 1

    # 2. window swap to a symlink (ELOOP path, design D4 table row 2): the
    # chokepoint's stat probe sees a regular file, then the injected swap
    # makes the open hit O_NOFOLLOW ELOOP.  Still exactly one attempt.
    target.unlink()
    target.write_text(json.dumps({"a": 2}), encoding="utf-8")

    def swap_to_symlink(real_open: Any) -> Any:
        original_replace = os.replace

        def swapped_open(path: Any, flags: Any, *args: Any, **kwargs: Any) -> int:
            if os.fsdecode(path).endswith(target.name):
                temp = target.with_suffix(".swap-target")
                temp.write_text("not a symlink yet", encoding="utf-8")
                os.symlink(outside, temp)
                original_replace(temp, target)
            return real_open(path, flags, *args, **kwargs)

        return swapped_open

    attempts = 0
    with _patch("services.orchestrator.file_orchestration_journal.read_bytes_limited_no_follow", counting_reader):
        with mock.patch("os.open", swap_to_symlink(os.open)):
            with pytest.raises(FileOrchestrationJournalError):
                repository._read_optional_json(target)
    assert attempts == 1

    # 3. containment violation
    attempts = 0
    with _patch("services.orchestrator.file_orchestration_journal.read_bytes_limited_no_follow", counting_reader):
        with pytest.raises(SafeFilesystemError):
            repository._read_bytes_limited_cached(outside)
    assert attempts == 1

    # 4. a directory occupying the target path: the chokepoint's stat probe
    # sees a non-regular object and the hardened reader refuses it on the
    # first attempt with the same stable journal error (task 3.8).
    target.unlink()
    target.mkdir()
    attempts = 0
    with _patch("services.orchestrator.file_orchestration_journal.read_bytes_limited_no_follow", counting_reader):
        with pytest.raises(FileOrchestrationJournalError) as caught:
            repository._read_optional_json(target)
    assert attempts == 1
    assert caught.value.reason == "file_journal_unreadable"


def test_read_chokepoint_retry_selects_on_kind_not_message(tmp_path: Path) -> None:
    """#1600 — task 3.9: a matching message with the wrong kind is not retried."""
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    target = root / "pipeline-jobs" / "job_a.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"a": 1}), encoding="utf-8")

    attempts = 0

    def throwing_reader(path: Path, **kwargs: Any) -> bytes:
        nonlocal attempts
        attempts += 1
        raise SafeFilesystemError("Target file changed while being opened", kind="unsafe")

    with _patch("services.orchestrator.file_orchestration_journal.read_bytes_limited_no_follow", throwing_reader):
        with pytest.raises(SafeFilesystemError) as excinfo:
            repository._read_bytes_limited_cached(target)
    assert attempts == 1
    assert excinfo.value.kind == "unsafe"


def test_read_chokepoint_passes_same_message_kind_identity_changed_retries(tmp_path: Path) -> None:
    """#1600 — the positive twin of the field-not-message case.

    A message that does NOT match the historical text but carries the right
    kind IS retried (the selector reads the field alone).
    """
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    target = root / "pipeline-jobs" / "job_a.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"a": 1}), encoding="utf-8")

    attempts = 0

    def flipping_reader(path: Path, **kwargs: Any) -> bytes:
        nonlocal attempts
        attempts += 1
        if attempts < journal_module.MAX_FILE_JOURNAL_IDENTITY_RETRY_ATTEMPTS:
            raise SafeFilesystemError("inode moved", kind="identity_changed")
        return b'{"a": 9}'

    with _patch("services.orchestrator.file_orchestration_journal.read_bytes_limited_no_follow", flipping_reader):
        payload = repository._read_bytes_limited_cached(target)
    assert payload[0] == b'{"a": 9}'
    assert attempts == journal_module.MAX_FILE_JOURNAL_IDENTITY_RETRY_ATTEMPTS


def test_safe_fs_primitive_itself_does_not_retry(tmp_path: Path) -> None:
    """#1600 — task 3.9b: the primitive raises on the first identity mismatch.

    Direct ``read_bytes_limited_no_follow`` / ``open_file_no_follow`` calls
    see exactly one ``os.open``; there is no second attempt.
    """
    root = tmp_path / "journal"
    target = root / "pipeline-jobs" / "job_a.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"a": 1}), encoding="utf-8")

    opens: list[int] = []
    real_open = os.open

    replacements = {target: 1}
    with _replace_on_open(real_open, replacements=replacements, always_replace=set(), opens=opens):
        with pytest.raises(SafeFilesystemError) as excinfo:
            read_bytes_limited_no_follow(target, max_bytes=1024, containment_root=root)
    assert excinfo.value.kind == "identity_changed"
    assert len(opens) == 1, f"primitive opened the target {len(opens)} times; it must not retry"


# --- structural guards -------------------------------------------------------


def test_cycle_write_owner_assignments_confined_to_init_and_window() -> None:
    """#1595 — task 3.11: AST guard on `_cycle_write_owner` writes.

    Assignment may appear only in `__init__` (to None) and in
    `_locked_cycle_write`.  Covers Assign, AnnAssign, AugAssign and
    setattr(self, ...) forms.
    """
    source_path = Path(journal_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    sites: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if _is_owner_target(target):
                    sites.append((node.lineno, "Assign"))
        elif isinstance(node, ast.AnnAssign) and _is_owner_target(node.target):
            sites.append((node.lineno, "AnnAssign"))
        elif isinstance(node, ast.AugAssign) and _is_owner_target(node.target):
            sites.append((node.lineno, "AugAssign"))
        elif isinstance(node, ast.Call) and _is_setattr_owner(node):
            sites.append((node.lineno, "setattr"))

    allowed = {"__init__", "_locked_cycle_write"}
    assert sites, "no _cycle_write_owner write sites found — guard is vacuous"
    for lineno, form in sites:
        fn = _enclosing_function_for_line(tree, lineno)
        assert fn in allowed, (
            f"_cycle_write_owner written at {source_path}:{lineno} ({form}) in {fn!r}; "
            "allowed only in __init__ and _locked_cycle_write"
        )


def _is_owner_target(target: ast.AST) -> bool:
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "_cycle_write_owner"
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    )


def _is_setattr_owner(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "setattr"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "self"
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "_cycle_write_owner"
    )


def _enclosing_function_for_line(tree: ast.AST, lineno: int) -> str | None:
    best: tuple[int, str] | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = node.end_lineno or node.lineno
            if node.lineno <= lineno <= end:
                if best is None or node.lineno > best[0]:
                    best = (node.lineno, node.name)
    return best[1] if best else None


def test_write_lock_is_plain_lock_not_rlock(tmp_path: Path) -> None:
    """#1595 — task 3.12: `_write_lock` is a non-reentrant Lock.

    The plain set/clear marker logic depends on non-reentrancy: an RLock
    would let a nested `_locked_cycle_write` clear the marker early.
    """
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    # The underlying type is the interpreter's primitive `lock`; the
    # reentrancy probe is the discriminator an RLock would fail: acquiring
    # twice from one thread deadlocks on a Lock but succeeds on an RLock.
    assert not repository._write_lock.acquire(blocking=False) or True
    repository._write_lock.release()
    assert repository._write_lock.acquire(blocking=False)
    try:
        assert not repository._write_lock.acquire(blocking=False), (
            "_write_lock is reentrant; the plain set/clear owner marker is unsafe"
        )
    finally:
        repository._write_lock.release()
    assert repository._cycle_write_owner is None


def test_cold_instance_walks_fingerprint_path(tmp_path: Path) -> None:
    """#1595 — task 1.3 note: `None == tuple` is always false, so a cold
    instance (owner None) always revalidates."""
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    assert repository._cycle_write_owner is None

    _write_latest_view(root, cycle_time=CYCLE_TIME, status="running")
    fingerprint_calls = 0
    real_fingerprint = repository._cycle_rows_source_fingerprint

    def counting_fingerprint(*, source_segments: Any, cycle_segment: str) -> Any:
        nonlocal fingerprint_calls
        fingerprint_calls += 1
        return real_fingerprint(source_segments=source_segments, cycle_segment=cycle_segment)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(repository, "_cycle_rows_source_fingerprint", counting_fingerprint)
    try:
        repository._cycle_rows(source_id="gfs", cycle_time=CYCLE_TIME, model_id="model_a")
    finally:
        monkeypatch.undo()
    assert fingerprint_calls >= 1


# --- #1567: containment-aware cycle-rows fingerprint (design D1 / D1b) -------
#
# The hole: `_cycle_rows_source_fingerprint` stat'ed its source files with a
# bare `os.stat(follow_symlinks=False)`, which does not follow the FINAL
# component but does follow symlinked PARENTS.  A real empty directory and a
# `symlink -> empty decoy` therefore fingerprinted identically, so a long-lived
# instance that cached a legal `[]` before a tamper kept serving `[]` after it
# while a cold instance on the same tree reported `file_journal_unreadable`.


_TAMPER_CYCLE = _dt("2026-06-28T00:00:00Z")
_TAMPER_SEGMENT = format_cycle_time(_TAMPER_CYCLE)

#: Every parent that feeds the cycle-rows fingerprint, relative to the journal
#: root.  Values are (path to replace with a symlink, extra directory the decoy
#: must hold so the tampered tree stays a plausible "same shape" decoy).
_FINGERPRINT_PARENTS: dict[str, tuple[str, str | None]] = {
    "journal_segment_slot": ("journal/gfs", None),
    "pipeline_events_segment_slot": ("pipeline-events/gfs", None),
    "latest_scandir_parent": ("latest/gfs", None),
    "by_cycle_direct_partition": ("pipeline-jobs/by-cycle/gfs", _TAMPER_SEGMENT),
    "flat_direct_root": ("pipeline-jobs", None),
}


def _empty_cycle_tree(root: Path) -> None:
    """A real, fully initialized journal tree that holds no records at all."""

    for relative in (
        "journal/gfs",
        "pipeline-events/gfs",
        f"latest/gfs/{_TAMPER_SEGMENT}",
        f"pipeline-jobs/by-cycle/gfs/{_TAMPER_SEGMENT}",
        "pipeline-jobs",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)


def _swap_for_symlinked_decoy(root: Path, relative: str, decoy: Path, *, child: str | None) -> None:
    """Replace one real parent directory with a symlink to an empty decoy."""

    decoy.mkdir(parents=True, exist_ok=True)
    if child is not None:
        (decoy / child).mkdir(exist_ok=True)
    target = root / relative
    import shutil

    shutil.rmtree(target)
    target.symlink_to(decoy)


def _stage_status_code(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "EMPTY"
    return str(rows[0].get("error_code") or "ROWS")


def _read_cycle(repository: FileOrchestrationJournalRepository) -> list[dict[str, Any]]:
    return repository.list_stage_statuses(
        source_id="gfs", cycle_time=_TAMPER_CYCLE, model_id="model_a"
    )


def test_warm_cache_entry_does_not_survive_journal_parent_symlink_tamper(tmp_path: Path) -> None:
    """#1567 red proof: a cached legal `[]` must not outlive an ancestor swap.

    Input: a real, empty journal tree; one warm read populates the cycle-rows
    cache with a legal `[]`; then `journal/gfs` is replaced by a symlink to an
    empty decoy directory.  Expected: the SAME instance's next public read
    reports the `file_journal_unreadable` blocked row, not the cached `[]`.
    """

    root = tmp_path / "journal"
    _empty_cycle_tree(root)
    repository = FileOrchestrationJournalRepository(root)

    warm = _read_cycle(repository)
    assert warm == []
    assert repository._cycle_rows_cache, "the warm read must have populated the cache"

    _swap_for_symlinked_decoy(root, "journal/gfs", tmp_path / "decoy", child=None)

    blocked = _read_cycle(repository)
    assert _stage_status_code(blocked) == "file_journal_unreadable"
    assert blocked[0]["file_journal"]["status"] == "blocked"


def test_cold_and_warm_instance_agree_on_a_tampered_tree(tmp_path: Path) -> None:
    """#1567: one tree, one answer — the warm instance matches a cold one."""

    root = tmp_path / "journal"
    _empty_cycle_tree(root)
    warm_repository = FileOrchestrationJournalRepository(root)
    assert _read_cycle(warm_repository) == []

    _swap_for_symlinked_decoy(root, "journal/gfs", tmp_path / "decoy", child=None)

    cold_repository = FileOrchestrationJournalRepository(root)
    warm_rows = _read_cycle(warm_repository)
    cold_rows = _read_cycle(cold_repository)
    assert _stage_status_code(warm_rows) == "file_journal_unreadable"
    assert _stage_status_code(warm_rows) == _stage_status_code(cold_rows)
    assert warm_rows[0]["file_journal"]["reason"] == cold_rows[0]["file_journal"]["reason"]


@pytest.mark.parametrize("leg", sorted(_FINGERPRINT_PARENTS))
def test_every_fingerprint_parent_leg_fails_loud_after_a_symlink_swap(
    tmp_path: Path, leg: str
) -> None:
    """#1567 D1: all five stat legs of the fingerprint family, not just one.

    The issue names only the segment slots, but the same fingerprint also stats
    the event-log slots, the `latest/<source>/<cycle>` scandir directory, the
    by-cycle direct partition and the flat `pipeline-jobs` root.  Every one of
    them is tampered here in turn; the expected output for each is a fail-loud
    blocked row on the warm instance that is identical to the cold instance's.
    """

    relative, child = _FINGERPRINT_PARENTS[leg]
    root = tmp_path / "journal"
    _empty_cycle_tree(root)
    repository = FileOrchestrationJournalRepository(root)
    assert _read_cycle(repository) == []

    _swap_for_symlinked_decoy(root, relative, tmp_path / f"decoy_{leg}", child=child)

    warm_rows = _read_cycle(repository)
    cold_rows = _read_cycle(FileOrchestrationJournalRepository(root))
    assert _stage_status_code(warm_rows) != "EMPTY", f"leg {leg} still fails open"
    assert warm_rows[0]["file_journal"]["status"] == "blocked"
    assert _stage_status_code(warm_rows) == _stage_status_code(cold_rows)


def test_untouched_empty_directory_stays_a_cacheable_legal_empty_read(tmp_path: Path) -> None:
    """#1567 D1: genuine absence still fingerprints as absence.

    Input: the same real, empty tree, read twice with nothing tampered.
    Expected: `[]` both times, and the second read opens no file at all — the
    containment probe must not turn an empty directory into a cache miss.
    """

    root = tmp_path / "journal"
    _empty_cycle_tree(root)
    repository = FileOrchestrationJournalRepository(root)
    assert _read_cycle(repository) == []

    reads: list[Path] = []
    real_read_json = FileOrchestrationJournalRepository._read_optional_json
    real_read_jsonl = FileOrchestrationJournalRepository._read_jsonl

    def record_json(self: Any, path: Path) -> Any:
        reads.append(path)
        return real_read_json(self, path)

    def record_jsonl(self: Any, path: Path, **kwargs: Any) -> Any:
        reads.append(path)
        return real_read_jsonl(self, path, **kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(FileOrchestrationJournalRepository, "_read_optional_json", record_json)
    monkeypatch.setattr(FileOrchestrationJournalRepository, "_read_jsonl", record_jsonl)
    try:
        assert _read_cycle(repository) == []
    finally:
        monkeypatch.undo()
    assert reads == [], f"second read was not served from the cache: {reads}"


def test_fingerprint_that_observed_a_containment_fault_is_never_stored(tmp_path: Path) -> None:
    """#1567 D1: a marker-carrying fingerprint neither hits nor is stored.

    If a marker-carrying fingerprint WERE stored, the next read would compute
    the same marker, compare equal, and serve the rows computed under the
    tamper — the same hole in a new shape.  The oracle is the cache dict.
    """

    root = tmp_path / "journal"
    _empty_cycle_tree(root)
    repository = FileOrchestrationJournalRepository(root)
    assert _read_cycle(repository) == []
    before = {key: entry[0] for key, entry in repository._cycle_rows_cache.items()}
    assert before, "the warm read must have stored a legal entry"

    _swap_for_symlinked_decoy(root, "journal/gfs", tmp_path / "decoy", child=None)

    faulted = repository._cycle_rows_source_fingerprint(
        source_segments=("gfs",), cycle_segment=_TAMPER_SEGMENT
    )
    assert faulted is journal_module._FINGERPRINT_CONTAINMENT_FAULT

    assert _stage_status_code(_read_cycle(repository)) == "file_journal_unreadable"
    after = {key: entry[0] for key, entry in repository._cycle_rows_cache.items()}
    # The pre-tamper entry is a legally stored one and simply stops being
    # reachable (the fault forces a miss).  What must never happen is a NEW
    # entry whose stored fingerprint carries the marker: the next read would
    # recompute the same marker, compare equal, and serve the tampered rows.
    assert after == before, "the faulted read must store nothing"
    assert all(
        fingerprint is not journal_module._FINGERPRINT_CONTAINMENT_FAULT
        for fingerprint in after.values()
    )
    # Repeating the read proves the point directly: still fail-loud, never a hit.
    assert _stage_status_code(_read_cycle(repository)) == "file_journal_unreadable"


def test_cycle_write_window_owner_hit_under_a_tampered_parent_fails_loud(tmp_path: Path) -> None:
    """#1567 D1b: the owner's fingerprint-free fast path is not a tamper hole.

    Input: the owner reads its own cycle twice inside its window (the first is
    a miss, the second would be an unvalidated hit), with `journal/gfs` swapped
    for a symlink to an empty decoy between them.  Expected: the second read
    recomputes and raises `file_journal_unreadable` — the cold read's outcome.
    """

    root = tmp_path / "journal"
    _empty_cycle_tree(root)
    repository = FileOrchestrationJournalRepository(root)

    with repository._locked_cycle_write(source_id="gfs", cycle_time=_TAMPER_CYCLE):
        first = repository._cycle_rows(
            source_id="gfs", cycle_time=_TAMPER_CYCLE, model_id="model_a"
        )
        assert not first.pipeline_jobs
        _swap_for_symlinked_decoy(root, "journal/gfs", tmp_path / "decoy", child=None)
        with pytest.raises(FileOrchestrationJournalError) as caught:
            repository._cycle_rows(source_id="gfs", cycle_time=_TAMPER_CYCLE, model_id="model_a")
    assert caught.value.reason == "file_journal_unreadable"
    assert repository._cycle_write_owner is None


# --- #1658: the window-EXIT clear is scoped to the window's own keys ---------


def test_cycle_write_window_exit_clear_keeps_other_cycles_entries(tmp_path: Path) -> None:
    """#1658 red proof — design D2.

    Cohort X opens a write window; INSIDE it (the entry wipe is global, so a
    pre-window warmup would go red for the wrong reason) an entry for a
    different cycle Y is populated on the same instance, and a synthetic base
    key for X is injected so "base key included" is not vacuous.  X's window
    body appends a JOURNAL record for X — deliberately not a flat
    `pipeline-jobs/<job_id>.json` direct, whose write would change the shared
    flat-root stat that every cycle's fingerprint carries (design D2's stated
    limit) and make Y miss for the wrong reason.

    Expected after the window exits: Y's entry is still cached and Y's next
    read opens no file at all, while every key for X's own
    `(source_id, cycle_segment)` — the base key included — is gone.
    """

    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    writer_cycle = _dt("2026-06-29T00:00:00Z")
    reader_cycle = _dt("2026-06-29T06:00:00Z")
    writer_segment = format_cycle_time(writer_cycle)
    reader_segment = format_cycle_time(reader_cycle)
    _write_latest_view(root, cycle_time=reader_cycle, status="running")
    _write_latest_view(root, cycle_time=writer_cycle, status="running")

    base_key = ("gfs", writer_segment, None, None)
    with repository._locked_cycle_write(source_id="gfs", cycle_time=writer_cycle):
        # X's own derived entry, populated inside the window.
        repository._cycle_rows(source_id="gfs", cycle_time=writer_cycle, model_id="model_a")
        # Y's entry, populated inside the window by the same thread: a
        # non-owner read for that cycle, so it revalidates and stores a real
        # fingerprint.
        repository._cycle_rows(source_id="gfs", cycle_time=reader_cycle, model_id="model_a")
        # No current reader produces the legacy base key, so "the exit sweep
        # includes the base key" would be untestable without injecting one.
        with repository._cache_lock:
            repository._cycle_rows_cache[base_key] = (None, journal_module._CycleRows())
        record = journal_module._journal_record_for_write(
            "pipeline_event",
            {
                "event_id": 1,
                "entity_type": "pipeline_job",
                "entity_id": f"job_cycle_gfs_{writer_segment}_forecast",
                "event_type": "status_change",
                "status_from": "reserved",
                "status_to": "queued",
                "message": None,
                "details": {},
                "created_at": "2026-06-29T00:00:00Z",
            },
            source_id="gfs",
            cycle_time=writer_cycle,
            model_id=None,
            sequence=1,
        )
        repository._append_journal_record_unlocked(
            source_id="gfs", cycle_time=writer_cycle, record=record
        )
        with repository._cache_lock:
            repository._cycle_rows_cache[base_key] = (None, journal_module._CycleRows())
        surviving_key = next(
            key for key in repository._cycle_rows_cache if key[1] == reader_segment
        )

    remaining = set(repository._cycle_rows_cache)
    assert surviving_key in remaining, "the exit clear evicted another cycle's entry"
    assert base_key not in remaining, "the exit sweep must include the base key"
    assert not [key for key in remaining if key[0] == "gfs" and key[1] == writer_segment], (
        "the window's own prefix must be gone"
    )

    reads: list[Path] = []
    real_read_json = FileOrchestrationJournalRepository._read_optional_json
    real_read_jsonl = FileOrchestrationJournalRepository._read_jsonl

    def record_json(self: Any, path: Path) -> Any:
        reads.append(path)
        return real_read_json(self, path)

    def record_jsonl(self: Any, path: Path, **kwargs: Any) -> Any:
        reads.append(path)
        return real_read_jsonl(self, path, **kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(FileOrchestrationJournalRepository, "_read_optional_json", record_json)
    monkeypatch.setattr(FileOrchestrationJournalRepository, "_read_jsonl", record_jsonl)
    try:
        rows = repository._cycle_rows(
            source_id="gfs", cycle_time=reader_cycle, model_id="model_a"
        )
    finally:
        monkeypatch.undo()
    assert rows.hydro_run["status"] == "running"
    assert reads == [], f"Y's entry did not survive the window exit: {reads}"


def test_cycle_write_window_entry_clear_is_still_global(tmp_path: Path) -> None:
    """#1658 D2: only the EXIT clear is narrowed; the ENTRY clear is not.

    The owner bypasses fingerprint validation for every hit, so a pre-window
    entry another process had already invalidated would be trusted.  That makes
    the entry wipe a correctness precondition, not a tunable.
    """

    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    writer_cycle = _dt("2026-06-29T00:00:00Z")
    other_cycle = _dt("2026-06-29T06:00:00Z")
    _write_latest_view(root, cycle_time=other_cycle, status="running")

    repository._cycle_rows(source_id="gfs", cycle_time=other_cycle, model_id="model_a")
    assert repository._cycle_rows_cache

    with repository._locked_cycle_write(source_id="gfs", cycle_time=writer_cycle):
        assert repository._cycle_rows_cache == {}, (
            "the window-entry clear must stay global (design D2)"
        )
