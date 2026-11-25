"""One cycle's decoded segment records are shared between its models (#3236 B2).

On a miss of ``_cycle_rows`` outside a write window, the decoded records of
``journal/<source>`` and ``pipeline-events/<source>`` for the cycle come from
``_cycle_segment_records_cache`` when the entry was stored under the
fingerprint the same call computed, and are read and stored otherwise.  These
tests pin that every model still reads what a cold repository reads (rows and
errors), that each segment directory is decoded once per cycle while its files
do not change, and the rules for faults, the write-window owner, failing
directories, the bound, aliasing, threads and read attribution.

Expected values come from a new repository instance reading the same tree
(the cold oracle), never from the instance under test.  ``_read_cycle_segments``
is counted through an instance attribute: the shared helper looks it up on the
instance at call time.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common.contracts.time_normalization import cycle_id_for, format_cycle_time
from services.orchestrator import file_orchestration_journal as journal_module
from services.orchestrator import public_evidence as pe
from services.orchestrator import scheduler as scheduler_module
from services.orchestrator.file_journal_read_attribution import (
    journal_read_attribution,
    reset_journal_read_counters,
)
from services.orchestrator.file_orchestration_journal import (
    FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
    FileOrchestrationJournalError,
    FileOrchestrationJournalRepository,
)
from tests.test_file_orchestration_journal_read_cache import (
    CYCLE_SEGMENT,
    CYCLE_TIME,
    _basin_manifest,
    _job_record,
)
from tests.test_production_scheduler import _model, _write_db_free_raw_manifest_fixture
from tests.test_scheduler_backfill import (
    _IDENTITY_CYCLE_TIMES,
    _IDENTITY_NOW,
    _build_scheduler,
    _completion_status,
    _dt,
    _seed_completed_journal_cycle,
    _set_db_free_cold_start_env,
)

MODELS = ("m1", "m2", "m3", "m4", "m5")
SURFACES = ("journal", "pipeline-events")
_FAULT = journal_module._FINGERPRINT_CONTAINMENT_FAULT
_CYCLE_ID = cycle_id_for("gfs", CYCLE_TIME)
_MASTER_JOB_ID = f"job_cycle_gfs_{CYCLE_SEGMENT}_forecast"


# --- fixture journal ---------------------------------------------------------------


def _case_folds(tmp_path: Path) -> bool:
    probe = tmp_path / "case_probe"
    probe.mkdir()
    folds = (tmp_path / "CASE_PROBE").exists()
    probe.rmdir()
    return folds


def _event_record(
    event_id: int,
    *,
    entity_type: str,
    entity_id: str,
    model_id: str | None = None,
    source_spelling: str = "gfs",
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
        "sequence": event_id,
        "record_type": "pipeline_event",
        "source_id": source_spelling,
        "cycle_time": CYCLE_TIME.isoformat(),
        "payload": {
            "event_id": event_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": "status_change",
            "status_from": "running",
            "status_to": "succeeded",
            "created_at": "2026-06-28T00:05:00Z",
            "details": {"stage": "publish", "summary": {"published": 1, "kinds": ["tiles"]}},
        },
    }
    if model_id is not None:
        record["model_id"] = model_id
    return record


def _append_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def _master_job(status: str) -> dict[str, Any]:
    return {
        "job_id": _MASTER_JOB_ID,
        "run_id": f"cycle_gfs_{CYCLE_SEGMENT}",
        "cycle_id": _CYCLE_ID,
        "candidate_id": f"cycle_gfs_{CYCLE_SEGMENT}",
        "job_type": "run_shud_forecast_array",
        "stage": "forecast",
        "status": status,
        "model_id": None,
        "slurm_job_id": "4001",
    }


def _build_plain(root: Path) -> None:
    """Five models with latest views, model and model-less journal records,
    pipeline events (per model and model-less) and by-cycle direct jobs."""

    writer = FileOrchestrationJournalRepository(root)
    writer.ensure_forecast_cycle(source_id="gfs", cycle_time=CYCLE_TIME)
    for model_id in MODELS:
        writer.create_hydro_run_from_basin({"source_id": "gfs"}, _basin_manifest(model_id))
        writer.upsert_pipeline_job(_job_record(model_id))
    # The cohort master: two model-less journal records.
    writer.upsert_pipeline_job(_master_job("running"))
    writer.upsert_pipeline_job(_master_job("succeeded"))
    events = [
        _event_record(
            200 + index,
            entity_type="pipeline_job",
            entity_id=_job_record(model_id)["job_id"],
            model_id=model_id,
        )
        for index, model_id in enumerate(MODELS)
    ]
    events.append(_event_record(300, entity_type="pipeline_job", entity_id=_MASTER_JOB_ID))
    events.append(_event_record(301, entity_type="forecast_cycle", entity_id=_CYCLE_ID))
    _append_jsonl(root / "pipeline-events" / "gfs" / f"{CYCLE_SEGMENT}.jsonl", events)
    # Two direct records move into the by-cycle partition.
    by_cycle = root / "pipeline-jobs" / "by-cycle" / "gfs" / CYCLE_SEGMENT
    by_cycle.mkdir(parents=True)
    for job_id in (_MASTER_JOB_ID, _job_record("m5")["job_id"]):
        (root / "pipeline-jobs" / f"{job_id}.json").rename(by_cycle / f"{job_id}.json")


def _split_into_rollover(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    half = len(lines) // 2
    path.write_text("".join(lines[:half]), encoding="utf-8")
    path.with_name(f"{CYCLE_SEGMENT}.1.jsonl").write_text("".join(lines[half:]), encoding="utf-8")


def _build_rollover(root: Path) -> None:
    _build_plain(root)
    for surface in SURFACES:
        _split_into_rollover(root / surface / "gfs" / f"{CYCLE_SEGMENT}.jsonl")


def _build_both_spellings(root: Path) -> None:
    _build_plain(root)
    for surface, first_id in (("journal", 400), ("pipeline-events", 500)):
        _append_jsonl(
            root / surface / "GFS" / f"{CYCLE_SEGMENT}.jsonl",
            [
                _event_record(
                    first_id,
                    entity_type="pipeline_job",
                    entity_id=_job_record("m2")["job_id"],
                    model_id="m2",
                    source_spelling="GFS",
                ),
                _event_record(first_id + 1, entity_type="forecast_cycle", entity_id=_CYCLE_ID, source_spelling="GFS"),
            ],
        )


def _build_empty(root: Path) -> None:
    root.mkdir(parents=True)


#: variant -> (builder, needs two distinct spelling directories)
_VARIANTS: dict[str, tuple[Callable[[Path], None], bool]] = {
    "plain": (_build_plain, False),
    "rollover": (_build_rollover, False),
    "both_spellings": (_build_both_spellings, True),
    "empty_cycle": (_build_empty, False),
}


def _variant_root(tmp_path: Path, variant: str) -> Path:
    build, needs_two_directories = _VARIANTS[variant]
    if needs_two_directories and _case_folds(tmp_path):
        pytest.skip("filesystem folds case: the two spellings are one directory here (runs on node-27)")
    root = tmp_path.resolve() / "journal"
    build(root)
    return root


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return _variant_root(tmp_path, "plain")


# --- helpers -----------------------------------------------------------------------


def _rows(repository: FileOrchestrationJournalRepository, model_id: str | None, **kwargs: Any) -> Any:
    cycle_time = kwargs.pop("cycle_time", CYCLE_TIME)
    return repository._cycle_rows(source_id="gfs", cycle_time=cycle_time, model_id=model_id, **kwargs)


def _sharing_nothing(repository: FileOrchestrationJournalRepository) -> FileOrchestrationJournalRepository:
    """Replace the shared helper on this instance by a stand-in that always reads.

    The cold oracle must not run the code under test: a key collision in the
    helper would otherwise contaminate the expected rows exactly as it does the
    rows under test.
    """

    def always_read(*, surface: str, source_segment: str, cycle_segment: str, **_kwargs: Any) -> Any:
        return repository._read_cycle_segments(repository.root / surface / source_segment, cycle_segment)

    repository._shared_cycle_segment_records = always_read  # type: ignore[method-assign]
    return repository


def _cold(root: Path, model_id: str | None) -> Any:
    return _rows(_sharing_nothing(FileOrchestrationJournalRepository(root)), model_id)


def _source_segments(root: Path) -> tuple[str, ...]:
    return journal_module._cycle_read_source_segments(source_id="gfs", source_segment_override=None, root=root)


def _key(surface: str, source_segment: str = "gfs", cycle_segment: str = CYCLE_SEGMENT) -> tuple[str, str, str]:
    return (surface, source_segment, cycle_segment)


def _cycle_keys(root: Path) -> set[tuple[str, str, str]]:
    """Every shared-cache key one default read of the cycle touches: two
    surfaces per source segment (``gfs`` and ``GFS`` stay two segments on a
    case-sensitive filesystem even when one of them is absent)."""

    return {_key(surface, segment) for segment in _source_segments(root) for surface in SURFACES}


def _entries(repository: FileOrchestrationJournalRepository) -> dict[Any, Any]:
    # The entry objects themselves: holding them keeps a replaced entry alive,
    # so a new entry can never reuse its id.
    with repository._cache_lock:
        return dict(repository._cycle_segment_records_cache)


def _same_entries(left: dict[Any, Any], right: dict[Any, Any]) -> bool:
    return left.keys() == right.keys() and all(left[key] is right[key] for key in left)


def _count_segment_reads(
    repository: FileOrchestrationJournalRepository, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, str, str]]:
    """Each call of ``_read_cycle_segments`` as ``(surface, source segment, cycle segment)``."""

    calls: list[tuple[str, str, str]] = []
    real = repository._read_cycle_segments

    def counting(directory: Path, cycle_segment: str) -> list[dict[str, Any]]:
        relative = Path(directory).relative_to(repository.root)
        calls.append((relative.parts[0], relative.parts[1], cycle_segment))
        return real(directory, cycle_segment)

    monkeypatch.setattr(repository, "_read_cycle_segments", counting)
    return calls


def _raises(read: Callable[[], Any]) -> FileOrchestrationJournalError:
    with pytest.raises(FileOrchestrationJournalError) as caught:
        read()
    return caught.value


def _same_error(left: FileOrchestrationJournalError, right: FileOrchestrationJournalError) -> None:
    assert type(left) is type(right)
    assert (left.reason, left.field) == (right.reason, right.field)


# --- 3.1 equivalence ---------------------------------------------------------------


@pytest.mark.parametrize("variant", sorted(_VARIANTS))
def test_every_model_reads_what_a_cold_repository_reads(tmp_path: Path, variant: str) -> None:
    root = _variant_root(tmp_path, variant)
    order: list[str | None] = [*MODELS, None]
    cold = {model_id: _cold(root, model_id) for model_id in order}
    if variant != "empty_cycle":
        assert all(cold[model_id].pipeline_jobs for model_id in order), "the fixture must populate every model"
        assert cold["m1"].pipeline_events and cold[None].pipeline_events, "the fixture must carry events"
    if variant == "both_spellings":
        # Guard against a silent fall-back to the plain tree: both spellings are
        # read, and the model-less events written under GFS (journal 401,
        # pipeline-events 501) reach the cycle-wide rows.
        assert _source_segments(root) == ("gfs", "GFS")
        event_ids = {str(event.get("event_id")) for event in cold[None].pipeline_events}
        assert {"401", "501"} <= event_ids, sorted(event_ids)

    for sequence in (order, list(reversed(order))):
        repository = FileOrchestrationJournalRepository(root)
        for round_index in range(2):  # the second round is all hits of the rows cache
            for model_id in sequence:
                assert _rows(repository, model_id) == cold[model_id], (variant, round_index, model_id)


# --- 3.2 one decode per cycle and surface ------------------------------------------


def test_one_decode_per_cycle_and_surface(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FileOrchestrationJournalRepository(root)
    reads = _count_segment_reads(repository, monkeypatch)
    fingerprints = 0
    real_fingerprint = repository._cycle_rows_source_fingerprint

    def counting_fingerprint(**kwargs: Any) -> Any:
        nonlocal fingerprints
        fingerprints += 1
        return real_fingerprint(**kwargs)

    monkeypatch.setattr(repository, "_cycle_rows_source_fingerprint", counting_fingerprint)

    for model_id in (*MODELS, None):
        _rows(repository, model_id)

    segments = _source_segments(root)
    assert Counter(reads) == dict.fromkeys(_cycle_keys(root), 1), (
        f"{len(reads)} segment reads for {len(segments)} source segment(s)"
    )
    assert fingerprints == 6, "every read computes its fingerprint"


# --- 3.3 a changed file is decoded again -------------------------------------------


def test_a_changed_file_is_decoded_again(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FileOrchestrationJournalRepository(root)
    _rows(repository, "m1")
    _rows(repository, "m2")
    segments = _source_segments(root)
    before = repository._cycle_rows_source_fingerprint(source_segments=segments, cycle_segment=CYCLE_SEGMENT)

    journal_file = root / "journal" / "gfs" / f"{CYCLE_SEGMENT}.jsonl"
    records = [json.loads(line) for line in journal_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    (m3_job,) = [
        record for record in records if record["record_type"] == "pipeline_job" and record.get("model_id") == "m3"
    ]
    appended = json.loads(json.dumps(m3_job))
    appended["sequence"] = max(int(record["sequence"]) for record in records) + 1
    appended["payload"]["status"] = "succeeded"
    appended["payload"]["finished_at"] = "2026-06-28T01:00:00Z"
    _append_jsonl(journal_file, [appended])

    reads = _count_segment_reads(repository, monkeypatch)
    rows = _rows(repository, "m3")
    assert Counter(reads) == dict.fromkeys(_cycle_keys(root), 1)
    assert rows.pipeline_jobs[m3_job["job_id"]]["status"] == "succeeded"
    assert rows == _cold(root, "m3")
    now = repository._cycle_rows_source_fingerprint(source_segments=segments, cycle_segment=CYCLE_SEGMENT)
    assert now != before
    after = _entries(repository)
    assert set(after) == _cycle_keys(root)
    assert after[_key("journal")][0] == now, "the entry is not stored under the changed file's fingerprint"


# --- 3.4 fault ---------------------------------------------------------------------


def test_a_containment_fault_is_never_served_from_the_shared_records(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = FileOrchestrationJournalRepository(root)
    _rows(repository, "m1")
    stored = _entries(repository)
    assert set(stored) == _cycle_keys(root)

    events = root / "pipeline-events" / "gfs"
    copy = tmp_path / "events_copy"
    shutil.copytree(events, copy)
    events.rename(tmp_path / "events_original")
    events.symlink_to(copy, target_is_directory=True)

    reads = _count_segment_reads(repository, monkeypatch)
    warm = _raises(lambda: _rows(repository, "m2"))
    cold = _raises(lambda: _cold(root, "m2"))
    _same_error(warm, cold)
    assert _key("journal") in reads, "the stored journal entry was served to a faulted read"
    assert not any(entry[0] is _FAULT for entry in _entries(repository).values()), "an entry under the fault marker"
    assert _same_entries(_entries(repository), stored), "the faulted read must take and store nothing"

    events.unlink()
    copy.rename(events)
    assert _rows(repository, "m3") == _cold(root, "m3")
    after = _entries(repository)
    assert set(after) == set(stored)
    assert after[_key("pipeline-events")] is not stored[_key("pipeline-events")], "the restored tree did not store"
    assert not any(entry[0] is _FAULT for entry in after.values())


def test_a_fault_outside_both_surfaces_stores_neither_decode(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The by-cycle direct-jobs partition is read after both surfaces, so both
    # decodes complete under a fingerprint that is the containment-fault marker.
    partition = root / "pipeline-jobs" / "by-cycle" / "gfs" / CYCLE_SEGMENT
    copy = tmp_path / "by_cycle_copy"
    shutil.copytree(partition, copy)
    partition.rename(tmp_path / "by_cycle_original")
    partition.symlink_to(copy, target_is_directory=True)

    repository = FileOrchestrationJournalRepository(root)
    assert (
        repository._cycle_rows_source_fingerprint(source_segments=_source_segments(root), cycle_segment=CYCLE_SEGMENT)
        is _FAULT
    )
    reads = _count_segment_reads(repository, monkeypatch)
    for count, model_id in enumerate(("m1", "m2"), start=1):
        warm = _raises(lambda model_id=model_id: _rows(repository, model_id))
        _same_error(warm, _raises(lambda model_id=model_id: _cold(root, model_id)))
        assert Counter(reads) == dict.fromkeys(_cycle_keys(root), count), "both surfaces are decoded on every read"
        assert _entries(repository) == {}, "a faulted read stored a decode"


# --- 3.5 the owner does not share --------------------------------------------------


def test_the_write_window_owner_reads_for_itself(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FileOrchestrationJournalRepository(root)
    _rows(repository, "m1")
    stored = _entries(repository)
    assert set(stored) == _cycle_keys(root)

    reads = _count_segment_reads(repository, monkeypatch)
    with repository._locked_cycle_write(source_id="gfs", cycle_time=CYCLE_TIME):
        _rows(repository, "m2")
        assert Counter(reads) == dict.fromkeys(_cycle_keys(root), 1)
        _rows(repository, "m3")
        assert Counter(reads) == dict.fromkeys(_cycle_keys(root), 2)
        assert _same_entries(_entries(repository), stored), "the owner took, stored or cleared an entry"
    assert _same_entries(_entries(repository), stored)

    reads.clear()
    assert _rows(repository, "m4") == _cold(root, "m4")
    assert reads == [], "after the window an unchanged cycle is a shared hit again"


# --- 3.6 a failing directory leaves no entry ---------------------------------------


def _segment_gap(directory: Path) -> str:
    base = directory / f"{CYCLE_SEGMENT}.jsonl"
    base.rename(directory / f"{CYCLE_SEGMENT}.1.jsonl")
    return "file_journal_segment_gap"


def _segment_limit(directory: Path) -> str:
    base = directory / f"{CYCLE_SEGMENT}.jsonl"
    for index in (1, 2, 3):
        shutil.copyfile(base, directory / f"{CYCLE_SEGMENT}.{index}.jsonl")
    return "file_journal_segment_limit_exceeded"


def _malformed_line(directory: Path) -> str:
    with (directory / f"{CYCLE_SEGMENT}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    return "file_journal_malformed_json"


_FAILURES: dict[str, Callable[[Path], str]] = {
    "segment_gap": _segment_gap,
    "segment_limit": _segment_limit,
    "malformed_json": _malformed_line,
}


@pytest.mark.parametrize("failure", sorted(_FAILURES))
def test_a_failing_journal_directory_fails_every_model_and_leaves_no_entry(
    root: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    reason = _FAILURES[failure](root / "journal" / "gfs")
    repository = FileOrchestrationJournalRepository(root)
    reads = _count_segment_reads(repository, monkeypatch)
    for count, model_id in enumerate(("m1", "m2", "m3"), start=1):
        warm = _raises(lambda model_id=model_id: _rows(repository, model_id))
        _same_error(warm, _raises(lambda model_id=model_id: _cold(root, model_id)))
        assert warm.reason == reason
        assert Counter(reads)[_key("journal")] == count, f"{model_id} did not read the failing directory"
    assert _key("journal") not in _entries(repository)


def test_a_failing_events_directory_keeps_the_decoded_journal_entry(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _malformed_line(root / "pipeline-events" / "gfs")
    repository = FileOrchestrationJournalRepository(root)
    reads = _count_segment_reads(repository, monkeypatch)

    first = _raises(lambda: _rows(repository, "m1"))
    _same_error(first, _raises(lambda: _cold(root, "m1")))
    assert first.reason == "file_journal_malformed_json"
    second = _raises(lambda: _rows(repository, "m2"))
    _same_error(second, _raises(lambda: _cold(root, "m2")))
    # The journal surface is decoded first; its entry is served to the next model.
    assert reads == [_key("journal"), _key("pipeline-events"), _key("pipeline-events")]
    entries = _entries(repository)
    assert set(entries) == {_key("journal")}
    fresh = FileOrchestrationJournalRepository(root)._read_cycle_segments(root / "journal" / "gfs", CYCLE_SEGMENT)
    assert entries[_key("journal")][1] == fresh


def test_a_decode_error_drops_the_older_entry_of_its_directory(root: Path) -> None:
    repository = FileOrchestrationJournalRepository(root)
    _rows(repository, "m1")
    stored = _entries(repository)
    assert set(stored) == _cycle_keys(root)

    # The appended line moves the fingerprint (size) and makes the journal
    # surface undecodable; the journal surface is read first, so the events
    # surface is not reached by the failing read.
    _malformed_line(root / "journal" / "gfs")
    error = _raises(lambda: _rows(repository, "m2"))
    _same_error(error, _raises(lambda: _cold(root, "m2")))
    assert error.reason == "file_journal_malformed_json"

    after = _entries(repository)
    assert _key("journal") not in after, "the older entry of the failing directory survived its read error"
    # Every other directory keeps its older entry untouched: none of them was
    # looked up by the failing read, and an older entry is never served to a
    # call whose fingerprint differs.
    assert _same_entries(after, {key: entry for key, entry in stored.items() if key != _key("journal")})


# --- 3.7 bound and key -------------------------------------------------------------


def test_the_cache_keeps_the_newest_eight_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path.resolve() / "journal"
    cycles = [CYCLE_TIME + timedelta(hours=6 * index) for index in range(5)]
    writer = FileOrchestrationJournalRepository(root)
    for cycle_time in cycles:
        writer.ensure_forecast_cycle(source_id="gfs", cycle_time=cycle_time)

    repository = FileOrchestrationJournalRepository(root)
    reads = _count_segment_reads(repository, monkeypatch)
    for cycle_time in cycles:
        _rows(repository, "m1", cycle_time=cycle_time, source_segment_override="gfs")
    assert len(reads) == 10, "ten inserts"

    entries = _entries(repository)
    expected = {_key(surface, cycle_segment=format_cycle_time(cycle)) for cycle in cycles[1:] for surface in SURFACES}
    assert set(entries) == expected, "the first cycle's two entries go, the other four cycles' stay"
    newest = format_cycle_time(cycles[-1])
    assert entries[_key("journal", cycle_segment=newest)] is not entries[_key("pipeline-events", cycle_segment=newest)]
    assert entries[_key("journal", cycle_segment=newest)][1], "the journal entry holds the cycle's records"
    assert entries[_key("pipeline-events", cycle_segment=newest)][1] == []


# --- 3.8 aliasing guard ------------------------------------------------------------

_SCORED_MODELS = MODELS
_TARGET = "2026-05-21T00:00:00Z"


def _publish_job(cycle_time: datetime, model_id: str) -> dict[str, Any]:
    stamp = format_cycle_time(cycle_time)
    return {
        "job_id": f"job_fcst_gfs_{stamp}_{model_id}_publish",
        "run_id": f"fcst_gfs_{stamp}_{model_id}",
        "cycle_id": cycle_id_for("gfs", cycle_time),
        "job_type": "publish",
        "stage": "publish",
        "status": "succeeded",
        "model_id": model_id,
        "submitted_at": "2026-05-21T01:00:00Z",
        "created_at": "2026-05-21T01:00:00Z",
    }


def _publish_events(cycle_time: datetime) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for index, model_id in enumerate(_SCORED_MODELS):
        record = _event_record(
            100 + index,
            entity_type="pipeline_job",
            entity_id=_publish_job(cycle_time, model_id)["job_id"],
            model_id=model_id,
        )
        record["cycle_time"] = cycle_time.isoformat()
        events.append(record)
    shared = _event_record(150, entity_type="forecast_cycle", entity_id=cycle_id_for("gfs", cycle_time))
    shared["cycle_time"] = cycle_time.isoformat()
    events.append(shared)
    return events


def _scoring_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, journal_root: Path, with_events: bool
) -> Any:
    target = _dt(_TARGET)
    for model_id in _SCORED_MODELS:
        _seed_completed_journal_cycle(
            journal_root,
            cycle_time=_TARGET,
            init_state_id=None,
            model_id=model_id,
            jobs=[_publish_job(target, model_id)],
        )
    if with_events:
        latest = json.loads(
            (journal_root / "latest" / "gfs" / format_cycle_time(target) / "m1.json").read_text(encoding="utf-8")
        )
        assert all(event["sequence"] > latest["replay"]["latest_sequence"] for event in _publish_events(target))
        events_file = journal_root / "pipeline-events" / "gfs" / f"{format_cycle_time(target)}.jsonl"
        _append_jsonl(events_file, _publish_events(target))
    scheduler = _build_scheduler(
        tmp_path,
        db_free=True,
        now=_dt(_IDENTITY_NOW),
        cycle_times=list(_IDENTITY_CYCLE_TIMES),
        backfill_enabled=True,
        max_cycles_per_source=len(_IDENTITY_CYCLE_TIMES),
        active_repository=FileOrchestrationJournalRepository(journal_root),
        models=[_model(model_id, f"basin_{model_id}") for model_id in _SCORED_MODELS],
    )
    monkeypatch.setattr(scheduler, "_strict_warm_start_for_candidate", lambda _candidate, _cycle: None)
    monkeypatch.setattr(
        scheduler, "_successor_warm_start_state_for_candidate", lambda _candidate, _cycle: {"ready": True}
    )
    return scheduler


def _shared_records_match_a_fresh_decode(repository: FileOrchestrationJournalRepository) -> list[str]:
    """Every entry whose records differ from a new instance's decode of the same directory."""

    fresh = FileOrchestrationJournalRepository(repository.root)
    return [
        str(key)
        for key, (_fingerprint, records) in _entries(repository).items()
        if records != fresh._read_cycle_segments(repository.root / key[0] / key[1], key[2])
    ]


@pytest.fixture
def scoring_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[list[object]]:
    _set_db_free_cold_start_env(monkeypatch, tmp_path)
    raw_root = Path(os.environ["NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT"])
    _write_db_free_raw_manifest_fixture({"object_store_root": raw_root}, cycle_time=_dt(_TARGET))
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ENABLED", "true")
    states: list[object] = []
    decide: Callable[..., object] = scheduler_module._candidate_state_decision

    def spy(candidate: object, state: object, **kwargs: object) -> object:
        states.append(state)
        return decide(candidate, state, **kwargs)

    monkeypatch.setattr(scheduler_module, "_candidate_state_decision", spy)
    yield states
    pe._clear_sanitiser_caches()


def test_completion_scoring_leaves_the_shared_records_as_decoded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scoring_env: list[object]
) -> None:
    states = scoring_env
    control = _scoring_scheduler(
        tmp_path, monkeypatch, journal_root=(tmp_path / "control_journal").resolve(), with_events=False
    )
    control_verdict = _completion_status(control, _TARGET)
    assert len(states) == len(_SCORED_MODELS), "the strict/successor branch did not read candidate states"

    journal_root = (tmp_path / "journal").resolve()
    scheduler = _scoring_scheduler(tmp_path, monkeypatch, journal_root=journal_root, with_events=True)
    states.clear()
    assert _completion_status(scheduler, _TARGET) == control_verdict == "complete"
    assert len(states) == len(_SCORED_MODELS), "candidate_state did not run for every model"

    repository = scheduler.active_repository
    entries = _entries(repository)
    events_key = _key("pipeline-events", cycle_segment=format_cycle_time(_dt(_TARGET)))
    assert events_key in entries and entries[events_key][1], "scoring did not go through the shared records"
    assert _shared_records_match_a_fresh_decode(repository) == []

    # Positive control: a write into ``details`` of an event in returned rows
    # reaches the shared records, and the oracle sees it.
    rows = repository._cycle_rows(source_id="gfs", cycle_time=_dt(_TARGET), model_id="m1")
    (event,) = [event for event in rows.pipeline_events if event.get("entity_type") == "forecast_cycle"]
    event["details"]["summary"]["published"] = 999
    assert _shared_records_match_a_fresh_decode(repository) == [str(events_key)]


# --- 3.9 threads -------------------------------------------------------------------


def test_threads_reading_different_models_of_one_cycle(root: Path) -> None:
    models = MODELS[:4]
    cold = {model_id: _cold(root, model_id) for model_id in models}
    for _round in range(50):
        repository = FileOrchestrationJournalRepository(root)
        barrier = threading.Barrier(len(models))
        results: dict[str, Any] = {}
        errors: list[BaseException] = []

        def read(model_id: str, repository: FileOrchestrationJournalRepository = repository,
                 barrier: threading.Barrier = barrier, results: dict[str, Any] = results,
                 errors: list[BaseException] = errors) -> None:
            try:
                barrier.wait(timeout=10)
                results[model_id] = _rows(repository, model_id)
            except BaseException as error:  # noqa: BLE001 - every escape is a failure
                errors.append(error)

        threads = [threading.Thread(target=read, args=(model_id,)) for model_id in models]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert results == cold


# --- 3.10 attribution --------------------------------------------------------------


def _attributed_reads(root: Path, monkeypatch: pytest.MonkeyPatch, *, share: bool) -> dict[str, Any]:
    repository = FileOrchestrationJournalRepository(root)
    if not share:
        assert callable(repository._shared_cycle_segment_records)
        _sharing_nothing(repository)
    reset_journal_read_counters()
    for model_id in MODELS:
        _rows(repository, model_id)
    snapshot = journal_read_attribution()
    reset_journal_read_counters()
    return snapshot


def test_read_attribution_drops_byte_cache_hits_and_keeps_file_reads(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = _attributed_reads(root, monkeypatch, share=True)
    unshared = _attributed_reads(root, monkeypatch, share=False)
    assert shared["totals"]["cache_hit_calls"] < unshared["totals"]["cache_hit_calls"]
    assert shared["totals"]["calls"] == unshared["totals"]["calls"]
    for snapshot in (shared, unshared):
        for field_name in ("calls", "bytes", "cache_hit_calls", "cache_hit_bytes"):
            assert sum(row[field_name] for row in snapshot["tags"]) == snapshot["totals"][field_name]
