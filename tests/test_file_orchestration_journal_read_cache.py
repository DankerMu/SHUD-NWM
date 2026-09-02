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
from collections.abc import Callable, Iterator
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

#: Design D1's fault-token table: the token is decided by whichever hardened
#: reader the forced recompute sends through the tampered path FIRST, so it is
#: a property of (leg, lane), not of the leg alone.  Keyed by leg, then by the
#: `model_id` the public read carries (`"model_a"` = the model-scoped read,
#: `None` = the cross-model read `list_stage_statuses` takes by default and the
#: chain forecast trigger uses).  Only the latest-directory leg is
#: lane-dependent: its model-scoped read resolves ONE path through
#: `_read_optional_json`, while the cross-model read lists the directory
#: through `_iter_discovered_files`.
_FINGERPRINT_PARENT_TOKENS: dict[str, dict[str | None, str]] = {
    "journal_segment_slot": {
        "model_a": "file_journal_unreadable",
        None: "file_journal_unreadable",
    },
    "pipeline_events_segment_slot": {
        "model_a": "file_journal_unreadable",
        None: "file_journal_unreadable",
    },
    "latest_scandir_parent": {
        "model_a": "file_journal_unreadable",
        None: "file_journal_unsafe_scanned_entry",
    },
    "by_cycle_direct_partition": {
        "model_a": "file_journal_unsafe_scanned_entry",
        None: "file_journal_unsafe_scanned_entry",
    },
    "flat_direct_root": {
        "model_a": "file_journal_unsafe_scanned_entry",
        None: "file_journal_unsafe_scanned_entry",
    },
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


def _read_cycle(
    repository: FileOrchestrationJournalRepository, *, model_id: str | None = "model_a"
) -> list[dict[str, Any]]:
    return repository.list_stage_statuses(
        source_id="gfs", cycle_time=_TAMPER_CYCLE, model_id=model_id
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


@pytest.mark.parametrize("model_id", ["model_a", None], ids=["model_scoped", "cross_model"])
@pytest.mark.parametrize("leg", sorted(_FINGERPRINT_PARENTS))
def test_every_fingerprint_parent_leg_fails_loud_after_a_symlink_swap(
    tmp_path: Path, leg: str, model_id: str | None
) -> None:
    """#1567 D1: all five stat legs of the fingerprint family, in both lanes.

    The issue names only the segment slots, but the same fingerprint also stats
    the event-log slots, the `latest/<source>/<cycle>` scandir directory, the
    by-cycle direct partition and the flat `pipeline-jobs` root.  Every one of
    them is tampered here in turn, through both public read lanes
    (`model_id="model_a"` and the default cross-model `model_id=None`).

    Expected: a fail-loud blocked row on the warm instance carrying exactly the
    token design D1's table names for that (leg, lane) cell, and the cold
    instance on the same tree carrying the same one.  The token is NOT a
    property of the leg: it is whichever hardened reader the forced recompute
    sends through the tampered path first, which is why the `latest` leg
    reports `file_journal_unreadable` model-scoped and
    `file_journal_unsafe_scanned_entry` cross-model.
    """

    relative, child = _FINGERPRINT_PARENTS[leg]
    expected_token = _FINGERPRINT_PARENT_TOKENS[leg][model_id]
    root = tmp_path / "journal"
    _empty_cycle_tree(root)
    repository = FileOrchestrationJournalRepository(root)
    assert _read_cycle(repository, model_id=model_id) == []

    _swap_for_symlinked_decoy(root, relative, tmp_path / f"decoy_{leg}", child=child)

    warm_rows = _read_cycle(repository, model_id=model_id)
    cold_rows = _read_cycle(FileOrchestrationJournalRepository(root), model_id=model_id)
    assert _stage_status_code(warm_rows) != "EMPTY", f"leg {leg} still fails open"
    assert warm_rows[0]["file_journal"]["status"] == "blocked"
    assert _stage_status_code(warm_rows) == expected_token, (
        f"leg {leg} / model_id={model_id!r}: warm token drifted from design D1's table"
    )
    assert _stage_status_code(cold_rows) == expected_token, (
        f"leg {leg} / model_id={model_id!r}: cold token drifted from design D1's table"
    )
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


@pytest.mark.parametrize("leg", sorted(_FINGERPRINT_PARENTS))
def test_fingerprint_that_observed_a_containment_fault_is_never_stored(
    tmp_path: Path, leg: str
) -> None:
    """#1567 D1: a marker-carrying fingerprint neither hits nor is stored.

    If a marker-carrying fingerprint WERE stored, the next read would compute
    the same marker, compare equal, and serve the rows computed under the
    tamper — the same hole in a new shape.  The oracle is the cache dict, and
    every one of the five stat legs must reach it, not just the segment slot.

    Why white-box at all: reverting `_containment_stat_signature` to the bare
    `_stat_signature` on the two direct-partition call sites leaves the PUBLIC
    read of both direct legs unchanged (measured: the decoy swap moves the
    bare stat tuple anyway, so the recompute happens for the wrong reason and
    lands on the same token) — only this marker assertion goes red for them.

    `by_cycle_direct_partition` runs its HARD variant on purpose: neither the
    real `pipeline-jobs/by-cycle/gfs` nor the decoy holds a `<cycle>` child, so
    a bare stat returns `None` before AND after the swap.  #1567 left that cell
    open — the forced recompute was served the pre-tamper `[]` from
    `_direct_jobs_cycle_cache`, a second cache that still fingerprinted with
    bare stats — and #1941 D1 closes it by taking both of that cache's
    signature legs through `_containment_stat_signature` and never storing a
    faulted signature.  So the hard variant now asserts the public token too,
    in BOTH lanes (`model_a` and the cross-model `model_id=None`, which share
    the direct cache's `(source_id, cycle_segment)` key), plus `after ==
    before` on `_direct_jobs_cycle_cache` itself.
    `test_every_fingerprint_parent_leg_fails_loud_after_a_symlink_swap` pins
    the public tokens for the ordinary variants.
    """

    relative, child = _FINGERPRINT_PARENTS[leg]
    hard_variant = leg == "by_cycle_direct_partition"
    root = tmp_path / "journal"
    _empty_cycle_tree(root)
    if hard_variant:
        (root / "pipeline-jobs" / "by-cycle" / "gfs" / _TAMPER_SEGMENT).rmdir()
        child = None
    repository = FileOrchestrationJournalRepository(root)
    assert _read_cycle(repository) == []
    before = {key: entry[0] for key, entry in repository._cycle_rows_cache.items()}
    assert before, "the warm read must have stored a legal entry"
    direct_before = {key: entry[0] for key, entry in repository._direct_jobs_cycle_cache.items()}
    assert direct_before, "the warm read must have stored a direct-jobs entry"

    _swap_for_symlinked_decoy(root, relative, tmp_path / f"decoy_{leg}", child=child)

    faulted = repository._cycle_rows_source_fingerprint(
        source_segments=("gfs",), cycle_segment=_TAMPER_SEGMENT
    )
    assert faulted is journal_module._FINGERPRINT_CONTAINMENT_FAULT, (
        f"leg {leg} did not carry the containment marker into the fingerprint"
    )

    rows = _read_cycle(repository)
    assert _stage_status_code(rows) == _FINGERPRINT_PARENT_TOKENS[leg]["model_a"]
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
    # Repeating the read proves the point directly: never a hit on the marker.
    assert repository._cycle_rows_source_fingerprint(
        source_segments=("gfs",), cycle_segment=_TAMPER_SEGMENT
    ) is journal_module._FINGERPRINT_CONTAINMENT_FAULT
    assert (
        _stage_status_code(_read_cycle(repository)) == _FINGERPRINT_PARENT_TOKENS[leg]["model_a"]
    )
    # The cross-model lane shares the direct cache's `(source_id, cycle_segment)`
    # key with the model-scoped one but misses `_cycle_rows_cache`, so it is the
    # lane that would still have been served the pre-#1941 warm `[]`.
    assert (
        _stage_status_code(_read_cycle(repository, model_id=None))
        == _FINGERPRINT_PARENT_TOKENS[leg][None]
    )
    # #1941 D1: the same never-stored rule on the direct-jobs cycle cache.
    direct_after = {key: entry[0] for key, entry in repository._direct_jobs_cycle_cache.items()}
    assert direct_after == direct_before, "the faulted direct read must store nothing"
    # The store guard is pinned on its own by
    # `test_direct_cache_signature_only_fault_is_never_stored_and_never_hits`.


# --- #1941: the direct-jobs cycle cache under containment (design D1) --------
#
# The residual #1567 left open.  `_direct_pipeline_job_records_for_cycle_cached`
# signed its listing with bare `_stat_signature` on `pipeline-jobs` and on
# `pipeline-jobs/by-cycle/<source>/<cycle>`.  In the HARD variant — the
# per-source partition swapped for a symlink to a decoy that, like the
# original, holds no `<cycle>` child — both legs sign `(sig, None)` before AND
# after the swap, so a warm instance kept serving `[]` from that cache while a
# cold one raised `file_journal_unsafe_scanned_entry`.  The owner fast path
# shared the hole: its directory probe forced a `_cycle_rows` recompute, but
# the recompute consulted the same warm direct cache.


def _hard_variant_tree(root: Path) -> None:
    """The empty cycle tree WITHOUT the `by-cycle/gfs/<cycle>` child.

    That missing child is what makes the variant hard: a bare absence stat of
    `<cycle>` compares equal across the partition swap, so only a
    containment-aware signature can tell the two trees apart.
    """

    _empty_cycle_tree(root)
    (root / "pipeline-jobs" / "by-cycle" / "gfs" / _TAMPER_SEGMENT).rmdir()


def _swap_by_cycle_for_hard_decoy(root: Path, decoy: Path) -> None:
    """Replace `pipeline-jobs/by-cycle/gfs` with a symlink to a childless decoy."""

    _swap_for_symlinked_decoy(root, "pipeline-jobs/by-cycle/gfs", decoy, child=None)


def test_cold_instance_fails_loud_on_the_by_cycle_hard_variant(tmp_path: Path) -> None:
    """#1941 D1 — the cold answer every warm/owner/retention row is compared to.

    Green on master by construction: a fresh repository has nothing cached, so
    it always recomputes and always reaches the tampered partition.  Pinning it
    here makes "warm == cold" a statement about a measured cold value rather
    than about whatever the recompute happens to do.

    Input: the hard-variant tree, tampered BEFORE the first read.  Expected:
    `file_journal_unsafe_scanned_entry` in both lanes and an empty
    `_direct_jobs_cycle_cache` — the faulted read stores nothing.
    """

    root = tmp_path / "journal"
    _hard_variant_tree(root)
    _swap_by_cycle_for_hard_decoy(root, tmp_path / "decoy_by_cycle")
    expected = _FINGERPRINT_PARENT_TOKENS["by_cycle_direct_partition"]

    repository = FileOrchestrationJournalRepository(root)
    for model_id in ("model_a", None):
        rows = _read_cycle(repository, model_id=model_id)
        assert _stage_status_code(rows) == expected[model_id], (
            f"cold read for model_id={model_id!r} did not fail loud"
        )
        assert rows[0]["file_journal"]["status"] == "blocked"
    assert not repository._direct_jobs_cycle_cache, (
        "a cold read that faulted must leave the direct-jobs cache empty"
    )


def test_cycle_write_window_owner_hit_under_the_by_cycle_hard_variant_fails_loud(
    tmp_path: Path,
) -> None:
    """#1941 D1 — the owner lane's cell of the hard variant.

    The owner computes no source-file fingerprint, but
    `pipeline-jobs/by-cycle/<source>/<cycle>` IS in its directory probe list,
    so the swap already forced a recompute before this change.  The recompute
    was then served the pre-tamper `[]` out of `_direct_jobs_cycle_cache` — the
    probe found the fault and the answer was `[]` anyway.

    Input: two in-window `_cycle_rows` reads with the partition swapped for the
    childless decoy between them.  Expected: the second raises
    `file_journal_unsafe_scanned_entry`, the cold read's token.  Master returns
    the cached empty rows instead.
    """

    root = tmp_path / "journal"
    _hard_variant_tree(root)
    repository = FileOrchestrationJournalRepository(root)

    with repository._locked_cycle_write(source_id="gfs", cycle_time=_TAMPER_CYCLE):
        first = repository._cycle_rows(
            source_id="gfs", cycle_time=_TAMPER_CYCLE, model_id="model_a"
        )
        assert not first.pipeline_jobs
        assert repository._direct_jobs_cycle_cache, (
            "the in-window read must have populated the direct-jobs cache"
        )

        _swap_by_cycle_for_hard_decoy(root, tmp_path / "decoy_by_cycle")

        assert repository._cycle_directories_probe_faulted(
            source_segments=("gfs",), cycle_segment=_TAMPER_SEGMENT
        ), "the swapped partition is a directory the owner probe covers"
        with pytest.raises(FileOrchestrationJournalError) as caught:
            repository._cycle_rows(source_id="gfs", cycle_time=_TAMPER_CYCLE, model_id="model_a")
    assert caught.value.reason == "file_journal_unsafe_scanned_entry"
    assert repository._cycle_write_owner is None


@pytest.mark.parametrize(
    ("build_tree", "by_cycle_child_absent"),
    [(_empty_cycle_tree, False), (_hard_variant_tree, True)],
    ids=["with_cycle_child", "without_cycle_child"],
)
def test_untouched_empty_by_cycle_partition_still_hits_the_direct_cache(
    tmp_path: Path,
    build_tree: Callable[[Path], None],
    by_cycle_child_absent: bool,
) -> None:
    """#1941 D1 — genuine absence still caches, which is why the cache exists.

    The guard has to be read across two public reads that both MISS
    `_cycle_rows_cache` yet share the direct cache's key: `_cycle_rows` keys on
    `model_id`, the direct cache keys on `(source_id, cycle_segment)`.  Two
    same-`model_id` reads would be answered by the outer cycle-rows hit before
    the direct cache is consulted at all, and would count one recompute even if
    the direct cache never hit.

    Both untouched shapes the spec scenario names are covered: the tree WITH a
    real `by-cycle/gfs/<cycle>` child, and the hard-variant tree WITHOUT it —
    the common production shape for a cycle that has no direct jobs, whose
    by-cycle leg must sign `None` (genuine absence under real directories) and
    not the containment marker.  What is unpinned without the second parameter
    is the HIT, not the store: a marker-for-absence regression already fails
    the stored-signature assertions of the never-stored tests, but a lookup
    that refused to serve a `None` leg would leave every such cycle
    recomputing its full direct scan for ever, with no test noticing.

    Input: an untouched tree whose real `by-cycle/gfs` partition holds no
    records; one read with `model_id="model_a"`, one with `model_id=None`.
    Expected: `_iter_direct_pipeline_job_records_for_cycle` runs exactly once
    and both reads are `[]`.
    """

    root = tmp_path / "journal"
    build_tree(root)
    repository = FileOrchestrationJournalRepository(root)

    calls: list[str | None] = []
    real_iter = repository._iter_direct_pipeline_job_records_for_cycle

    def counting_iter(*, source_id: str, cycle_time: datetime, model_id: str | None) -> Any:
        calls.append(model_id)
        return real_iter(source_id=source_id, cycle_time=cycle_time, model_id=model_id)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(repository, "_iter_direct_pipeline_job_records_for_cycle", counting_iter)
    try:
        assert _read_cycle(repository, model_id="model_a") == []
        assert _read_cycle(repository, model_id=None) == []
    finally:
        monkeypatch.undo()

    assert len(calls) == 1, (
        f"the second read must be served from the direct-jobs cache, saw {calls}"
    )
    keys = set(repository._direct_jobs_cycle_cache)
    assert keys == {("gfs", _TAMPER_SEGMENT)}, keys
    stored_signature = repository._direct_jobs_cycle_cache[("gfs", _TAMPER_SEGMENT)][0]
    if by_cycle_child_absent:
        assert stored_signature[1] is None, (
            "a missing `<cycle>` child under a real partition is genuine absence and "
            f"must sign None, got {stored_signature[1]!r}"
        )
    else:
        assert isinstance(stored_signature[1], tuple), stored_signature[1]


def test_direct_cache_signature_only_fault_is_never_stored_and_never_hits(
    tmp_path: Path,
) -> None:
    """#1941 D1 matrix row 2b — the STORE guard, discriminated on its own.

    The never-stored tests above tamper the tree, so their recompute raises
    before the store line is reached: `after == before` holds there whether or
    not `if not faulted:` guards the store.  This test separates the two by
    faulting the SIGNATURE only — `_containment_stat_signature` is patched to
    report the containment marker for the `pipeline-jobs` flat root while the
    tree underneath stays real, so the recompute succeeds and returns `[]` and
    the store line IS reached.  (That leg also feeds
    `_cycle_rows_source_fingerprint`, so the outer `_cycle_rows_cache` stores
    nothing either while the patch is in place — which is what keeps both of
    the last two reads reaching the direct cache.)

    Design D1's two guards: the store guard and the `not faulted` clause on the
    lookup.  Either one alone closes the hole, because
    `_FINGERPRINT_CONTAINMENT_FAULT` is a singleton that defines no `__eq__`,
    so `(x, FAULT) == (x, FAULT)` is True BY IDENTITY — a stored marker entry
    would compare equal on the next read and serve rows computed while the
    stat was faulting.  This test discriminates the mutation "drop `if not
    faulted:` around the store" (the marker reaches the cache — the emptiness
    assertion goes red) and the mutation "drop BOTH guards" (the second read
    hits the stored marker entry — the recompute count goes red at 1).  It does
    NOT discriminate "drop the lookup clause alone": given the store guard no
    marker is ever in the cache, so that clause is belt-and-braces and its
    mutant stays green here, by design.

    Input: a clean `_empty_cycle_tree`; two reads (`model_a`, then `None`)
    under the patched signature, then the patch is removed and two more.
    Expected: the faulted pair recomputes twice and stores nothing; the read
    after the patch is removed recomputes once more and stores; the read after
    that is a hit.
    """

    root = tmp_path / "journal"
    _empty_cycle_tree(root)
    repository = FileOrchestrationJournalRepository(root)

    faulting_path = root / "pipeline-jobs"
    real_signature = repository._containment_stat_signature

    def faulting_signature(path: Path) -> Any:
        if path == faulting_path:
            return journal_module._FINGERPRINT_CONTAINMENT_FAULT
        return real_signature(path)

    calls: list[str | None] = []
    real_iter = repository._iter_direct_pipeline_job_records_for_cycle

    def counting_iter(*, source_id: str, cycle_time: datetime, model_id: str | None) -> Any:
        calls.append(model_id)
        return real_iter(source_id=source_id, cycle_time=cycle_time, model_id=model_id)

    counting_patch = pytest.MonkeyPatch()
    counting_patch.setattr(repository, "_iter_direct_pipeline_job_records_for_cycle", counting_iter)
    signature_patch = pytest.MonkeyPatch()
    signature_patch.setattr(repository, "_containment_stat_signature", faulting_signature)
    try:
        assert _read_cycle(repository, model_id="model_a") == []
        assert _read_cycle(repository, model_id=None) == []
        assert len(calls) == 2, (
            f"a faulted signature must never be a hit, saw {calls}"
        )
        assert not repository._direct_jobs_cycle_cache, (
            "a faulted signature must never be stored, saw "
            f"{repository._direct_jobs_cycle_cache}"
        )
        assert not repository._cycle_rows_cache, (
            "the same faulted leg feeds the cycle-rows fingerprint, so the outer cache "
            "must be empty too — otherwise the reads below would never reach the direct cache"
        )

        signature_patch.undo()

        assert _read_cycle(repository, model_id="model_a") == []
        assert len(calls) == 3, f"the unfaulted read must recompute, saw {calls}"
        assert set(repository._direct_jobs_cycle_cache) == {("gfs", _TAMPER_SEGMENT)}, (
            "an unfaulted signature must be stored"
        )

        assert _read_cycle(repository, model_id=None) == []
        assert len(calls) == 3, (
            f"the stored unfaulted signature must be a hit, saw {calls}"
        )
    finally:
        signature_patch.undo()
        counting_patch.undo()


def test_retention_inspection_reports_the_hard_variant_as_blocked(tmp_path: Path) -> None:
    """#1941 D1 matrix row 6 — the destructive-operation predicate fails closed.

    `_inspect_retention_cycle_unlocked` reads `_cycle_rows(model_id=None)`
    BEFORE its own direct call and catches `FileOrchestrationJournalError` into
    a `blocked` row.  On master that read was served the warm direct `[]`, no
    row blocked rollback quiescence, and the window reported the slice
    `eligible` on a listing the tree no longer backs.

    The warm cell must be built INSIDE one window: `open_retention_cycle` wipes
    all three caches on entry and on exit, so a pre-window warmup would make
    the second inspection a cold read.  The fresh-instance comparison is taken
    after the window has exited — a second repository opening the same cycle
    while the first still holds it gets `status == "busy"`.

    Expected: the second in-window `inspect()` is `blocked` /
    `file_journal_unsafe_scanned_entry`, field for field what a fresh instance
    reports on the same tree.  `inspect()` never calls `remove_members`, so the
    status IS the assertion.
    """

    root = tmp_path / "journal"
    _hard_variant_tree(root)
    repository = FileOrchestrationJournalRepository(root)

    with repository.open_retention_cycle(source_id="gfs", cycle_time=_TAMPER_CYCLE) as window:
        assert window.status == "locked"
        warm = window.inspect()
        assert warm.status == "eligible", (
            f"the pre-tamper inspection must be a legal empty slice, got {warm}"
        )
        assert repository._direct_jobs_cycle_cache, (
            "the in-window inspection must have populated the direct-jobs cache"
        )

        _swap_by_cycle_for_hard_decoy(root, tmp_path / "decoy_by_cycle")

        blocked = window.inspect()

    assert blocked.status == "blocked"
    assert blocked.reason == "file_journal_unsafe_scanned_entry"

    fresh = FileOrchestrationJournalRepository(root)
    with fresh.open_retention_cycle(source_id="gfs", cycle_time=_TAMPER_CYCLE) as cold_window:
        assert cold_window.status == "locked"
        cold = cold_window.inspect()
    assert blocked == cold, "the warm inspection must equal a cold one field for field"


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


def test_cycle_write_window_owner_hit_does_not_see_a_leaf_swap_stated_limit(
    tmp_path: Path,
) -> None:
    """#1567 D1b STATED LIMIT: the owner probe is directory-only, by design.

    This test pins a limit, not a fix.  The owner's fast path computes no
    source-file fingerprint (that is what D1b exists to skip); it only
    containment-stats the five DIRECTORIES that feed the cycle, and that probe
    is FAULT-ONLY — it asks whether a directory is still reachable under
    containment, never whether its contents changed.  The limit is therefore
    ANY leaf-level change beneath the probed directories: a symlink swap, or a
    plain file added, replaced or removed.  The symlink swap exercised below
    (`journal/gfs/<cycle>.jsonl`, one of five measured leaf cells) is one
    instance of that class, not the whole of it.  Whichever shape it takes, the
    owner keeps serving its pre-tamper cached rows while a cold instance on the
    same tree raises.  That narrows the pre-PR state (the owner did no tamper
    detection at all) instead of widening it.

    #1942 ruled this limit PERMANENT (design D3, option B): the fast path is
    not going to grow a leaf probe, so this test pins a settled behaviour, not
    a pending flip.  The cost is the reason — the five swappable leaf cells are
    exactly the files the source-file fingerprint stats, so a leaf probe IS the
    fingerprint under another name.  Measured: the five-directory probe already
    costs 191 Python-level `os.*` calls, the full fingerprint 414, and a warm
    cache-hit public read 422 against 20 before the containment work.  DEPTH
    CAVEAT: those three figures were taken at a 14-component realpath root, and
    `_open_directory_no_follow` re-walks from `/`, so they are linear in root
    depth; this PR measured a 334-call warm hit at a 9-component root.  The two
    sets of absolute call counts are comparable only among themselves, at their
    own depth, and NOT across depths.  Option A would put probe + fingerprint on every in-window
    hit, i.e. collapse the fast path into the non-owner path and keep the
    probe's cost on top.  Option C — comparing the `(mtime_ns, size, ino)`
    tuples the probe has ALREADY computed for those five directories, at zero
    extra `os.*` calls — was priced in design D3 and not adopted: the shared
    flat `pipeline-jobs` root turns other cycles' writes into owner recomputes,
    and a parent tuple does not move for an in-place append to an existing
    `.jsonl` leaf, so C would close the swap/add/remove cells but not the
    append cell at an unmeasured hit-rate cost.  The exposure stays bounded to
    the window: the owner's next append invalidates every reachable key for the
    pair, and the first read after the window revalidates under the full
    containment-aware fingerprint, which sees the swapped leaf and fails loud.
    The cold and non-owner lanes never had it.

    Input: an empty tree; two in-window `_cycle_rows` reads with
    `journal/gfs/<cycle>.jsonl` replaced by a symlink to a decoy regular file
    between them.  Expected: the second read returns the cached pre-tamper rows
    with no raise, the directory probe reports no fault, and a fresh cold
    repository on the same tree raises `file_journal_unreadable`.
    """

    root = tmp_path / "journal"
    _empty_cycle_tree(root)
    decoy_leaf = tmp_path / "decoy_leaf.jsonl"
    decoy_leaf.write_text("", encoding="utf-8")
    repository = FileOrchestrationJournalRepository(root)

    with repository._locked_cycle_write(source_id="gfs", cycle_time=_TAMPER_CYCLE):
        first = repository._cycle_rows(
            source_id="gfs", cycle_time=_TAMPER_CYCLE, model_id="model_a"
        )
        assert not first.pipeline_jobs
        assert repository._cycle_rows_cache, "the in-window read must have cached an entry"

        leaf = root / "journal" / "gfs" / f"{_TAMPER_SEGMENT}.jsonl"
        assert not leaf.exists()
        leaf.symlink_to(decoy_leaf)

        # The stated limit: no raise, and the same rows the owner cached before
        # the swap.
        second = repository._cycle_rows(
            source_id="gfs", cycle_time=_TAMPER_CYCLE, model_id="model_a"
        )
        assert second == first
        assert not repository._cycle_directories_probe_faulted(
            source_segments=("gfs",), cycle_segment=_TAMPER_SEGMENT
        ), "the probe is directory-only; a leaf swap must not register as a fault"

        # The contrast that makes the limit a limit: a cold reader on the very
        # same tree fails loud on that leaf.
        with pytest.raises(FileOrchestrationJournalError) as caught:
            FileOrchestrationJournalRepository(root)._cycle_rows(
                source_id="gfs", cycle_time=_TAMPER_CYCLE, model_id="model_a"
            )
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
