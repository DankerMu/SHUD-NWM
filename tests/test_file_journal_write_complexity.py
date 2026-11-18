"""#3197: the file journal refuses, before its first byte, a record its own reader would refuse.

The JSON complexity limits (``max_json_depth`` / ``max_json_nodes``) used to be enforced
only on read, so an over-limit write appended and the next read of the cycle blocked.
These tests pin the write side against the read side of the SAME instance limits:

* depth pairs per latest-view headroom class -- ``hydro_run`` (the latest view embeds the
  row at the record's own depth) and ``pipeline_job`` / ``pipeline_event`` (the latest view
  nests them one level deeper, inside a list).  The deepest admitted record is read back
  by a FRESH repository at the limit (a fresh instance, so neither the writer's cycle-rows
  cache nor its prevalidated-bytes cache can hide a block); one level deeper is refused
  with every file of the journal tree byte-identical;
* a node-count pair on ``pipeline_job``, whose own files (jsonl line, direct row) are
  decoded directly at the instance limits;
* the derived-direct restore lane, which rewrites an existing record and stays unchecked;
* a batch lane and the historical import, both refused whole.

Expected depths come from the task 1.1 measurement recorded in
``openspec/changes/journal-write-complexity-limits/design.md``: latest-view offset 0 for
``hydro_run``/``forecast_cycle``/``forcing_version``/``model_context``, +1 for
``pipeline_job``/``pipeline_event``; jsonl line and direct row offset 0.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.contracts.time_normalization import cycle_id_for
from services.orchestrator.file_orchestration_journal import (
    FILE_JOURNAL_READ_BLOCKED_STATUS,
    FileOrchestrationJournalError,
    FileOrchestrationJournalRepository,
)
from services.orchestrator.file_orchestration_migration import import_historical_scheduler_state
from tests.test_file_orchestration_journal import _terminally_failed_cohort_master

CYCLE = datetime(2026, 6, 28, tzinfo=UTC)
CYCLE_SEGMENT = "2026062800"
#: A small depth limit, far below the module default, so a test that used the module
#: constants instead of the instance limit would admit the one-over record.
LIMIT = 12
#: Root-level JSON depth of a journal record whose payload carries a value of depth v:
#: record (1) -> payload (2) -> field value (v levels from 3 on).
RECORD_PREFIX = 2
#: The same for a pipeline_event whose ``details`` carries the value.
EVENT_DETAILS_PREFIX = 3


def _depth(value: Any) -> int:
    """JSON depth as the journal reader counts it: the root is depth 1."""

    if isinstance(value, Mapping):
        return 1 + max((_depth(item) for item in value.values()), default=0)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return 1 + max((_depth(item) for item in value), default=0)
    return 1


def _nodes(value: Any) -> int:
    """JSON node count as the journal reader counts it: every container and every scalar."""

    if isinstance(value, Mapping):
        return 1 + sum(_nodes(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return 1 + sum(_nodes(item) for item in value)
    return 1


def _nest(depth: int) -> Any:
    """A value of exactly ``depth`` levels: a scalar leaf under ``depth - 1`` mappings."""

    value: Any = "leaf"
    for _ in range(depth - 1):
        value = {"k": value}
    return value


def _tree(root: Path) -> dict[str, str]:
    """sha256 of every file under the journal root, plus every directory (marked ``dir``)."""

    if not root.exists():
        return {}
    tree: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        tree[relative] = "dir" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
    return tree


def _hydro_row(model_id: str, **extra: Any) -> dict[str, Any]:
    row = {
        "run_id": f"fcst_gfs_{CYCLE_SEGMENT}_{model_id}",
        "run_type": "forecast",
        "scenario_id": "scenario_a",
        "model_id": model_id,
        "basin_version_id": "basin_a_v1",
        "forcing_version_id": f"forc_gfs_{CYCLE_SEGMENT}_{model_id}",
        "source_id": "gfs",
        "cycle_time": CYCLE,
        "start_time": CYCLE,
        "end_time": CYCLE,
        "status": "succeeded",
        "updated_at": "2026-06-28T00:05:00Z",
    }
    row.update(extra)
    return row


def _job_row(job_id: str, model_id: str, **extra: Any) -> dict[str, Any]:
    row = {
        "job_id": job_id,
        "run_id": f"fcst_gfs_{CYCLE_SEGMENT}_{model_id}",
        "cycle_id": cycle_id_for("gfs", CYCLE),
        "job_type": "run_shud_forecast_array",
        "model_id": model_id,
        "status": "succeeded",
        "stage": "forecast",
        "retry_count": 0,
        "manual_retry_marker": False,
        "created_at": "2026-06-28T00:00:00Z",
        "updated_at": "2026-06-28T00:05:00Z",
    }
    row.update(extra)
    return row


def _event_row(event_id: int, job_id: str, details: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "entity_type": "pipeline_job",
        "entity_id": job_id,
        "event_type": "retry",
        "status_from": "failed",
        "status_to": "pending",
        "details": dict(details),
        "created_at": "2026-06-28T00:06:00Z",
    }


def _journal_lines(root: Path) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.jsonl")):
        lines.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    return lines


def _line_for(root: Path, record_type: str, **identity: Any) -> dict[str, Any]:
    matches = [
        line
        for line in _journal_lines(root)
        if line.get("record_type") == record_type
        and all(line.get("payload", {}).get(key) == value for key, value in identity.items())
    ]
    assert len(matches) == 1, matches
    return matches[0]


def _latest_path(root: Path, model_id: str) -> Path:
    return root / "latest" / "gfs" / CYCLE_SEGMENT / f"{model_id}.json"


def _fresh_reader(root: Path, **limits: int) -> FileOrchestrationJournalRepository:
    return FileOrchestrationJournalRepository(root, **limits)


def _assert_reads_at_limit(root: Path, *, model_id: str, **limits: int) -> None:
    """Every journal segment and the model's latest view decode at the limits, and the
    cycle replay through the latest view does not block."""

    reader = _fresh_reader(root, **limits)
    for path in sorted(root.rglob("*.jsonl")):
        reader._read_jsonl(path)
    reader = _fresh_reader(root, **limits)
    assert reader._read_optional_json(_latest_path(root, model_id)) is not None
    _fresh_reader(root, **limits)._cycle_rows(source_id="gfs", cycle_time=CYCLE, model_id=model_id)


def _assert_depth_refused(error: pytest.ExceptionInfo[FileOrchestrationJournalError], record_type: str) -> None:
    assert error.value.reason == "file_journal_json_depth_exceeded"
    assert error.value.field == record_type


# --- depth pair, headroom 0: hydro_run ------------------------------------------------


def test_hydro_run_at_the_depth_limit_round_trips_through_jsonl_and_latest(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    writer = FileOrchestrationJournalRepository(root, max_json_depth=LIMIT)

    writer.append_historical_hydro_run(_hydro_row("model_a", probe=_nest(LIMIT - RECORD_PREFIX)))

    line = _line_for(root, "hydro_run", model_id="model_a")
    latest = json.loads(_latest_path(root, "model_a").read_text(encoding="utf-8"))
    assert _depth(line) == LIMIT
    assert _depth(latest) == LIMIT
    _assert_reads_at_limit(root, model_id="model_a", max_json_depth=LIMIT)


def test_hydro_run_one_level_over_the_depth_limit_is_refused_with_zero_bytes(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    writer = FileOrchestrationJournalRepository(root, max_json_depth=LIMIT)
    writer.append_historical_hydro_run(_hydro_row("model_a"))
    before = _tree(root)

    with pytest.raises(FileOrchestrationJournalError) as error:
        writer.append_historical_hydro_run(_hydro_row("model_b", probe=_nest(LIMIT - RECORD_PREFIX + 1)))

    _assert_depth_refused(error, "hydro_run")
    assert _tree(root) == before
    _assert_reads_at_limit(root, model_id="model_a", max_json_depth=LIMIT)


# --- depth pair, headroom 1: pipeline_job and pipeline_event --------------------------


def test_pipeline_job_at_the_depth_limit_round_trips_through_jsonl_direct_and_latest(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    writer = FileOrchestrationJournalRepository(root, max_json_depth=LIMIT)

    writer.append_historical_pipeline_job(_job_row("job_deep", "model_a", exit_code=_nest(LIMIT - 1 - RECORD_PREFIX)))

    line = _line_for(root, "pipeline_job", job_id="job_deep")
    direct = json.loads((root / "pipeline-jobs" / "job_deep.json").read_text(encoding="utf-8"))
    latest = json.loads(_latest_path(root, "model_a").read_text(encoding="utf-8"))
    assert _depth(line) == LIMIT - 1
    assert _depth(direct) == LIMIT - 1
    assert _depth(latest) == LIMIT
    _assert_reads_at_limit(root, model_id="model_a", max_json_depth=LIMIT)
    assert _fresh_reader(root, max_json_depth=LIMIT)._read_optional_json(root / "pipeline-jobs" / "job_deep.json")
    public = _fresh_reader(root, max_json_depth=LIMIT).get_pipeline_job("job_deep")
    assert public is not None
    assert public["status"] == "succeeded"


def test_pipeline_job_one_level_over_the_depth_limit_is_refused_with_zero_bytes(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    writer = FileOrchestrationJournalRepository(root, max_json_depth=LIMIT)
    writer.append_historical_pipeline_job(_job_row("job_small", "model_a"))
    before = _tree(root)

    with pytest.raises(FileOrchestrationJournalError) as error:
        writer.append_historical_pipeline_job(_job_row("job_deep", "model_a", exit_code=_nest(LIMIT - RECORD_PREFIX)))

    _assert_depth_refused(error, "pipeline_job")
    assert _tree(root) == before
    _assert_reads_at_limit(root, model_id="model_a", max_json_depth=LIMIT)


def test_pipeline_event_at_the_depth_limit_round_trips_and_one_over_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    writer = FileOrchestrationJournalRepository(root, max_json_depth=LIMIT)
    writer.append_historical_pipeline_job(_job_row("job_a", "model_a"))

    writer.append_historical_pipeline_event(
        _event_row(101, "job_a", {"probe": _nest(LIMIT - 1 - EVENT_DETAILS_PREFIX)})
    )

    line = _line_for(root, "pipeline_event", event_id=101)
    latest = json.loads(_latest_path(root, "model_a").read_text(encoding="utf-8"))
    assert _depth(line) == LIMIT - 1
    assert _depth(latest) == LIMIT
    _assert_reads_at_limit(root, model_id="model_a", max_json_depth=LIMIT)
    before = _tree(root)

    with pytest.raises(FileOrchestrationJournalError) as error:
        writer.append_historical_pipeline_event(
            _event_row(102, "job_a", {"probe": _nest(LIMIT - EVENT_DETAILS_PREFIX)})
        )

    _assert_depth_refused(error, "pipeline_event")
    assert _tree(root) == before


# --- node-count pair: pipeline_job ----------------------------------------------------


NODE_LIMIT = 200


def _job_with_nodes(root: Path, job_id: str, total_nodes: int) -> dict[str, Any]:
    """A job row whose built record has exactly ``total_nodes`` nodes.

    The record's node count without padding is measured once on a scratch journal (the
    record's other values are scalars, so their count does not depend on their values);
    ``exit_code`` then carries a list of ``k`` scalars, which adds ``k + 1`` nodes.
    """

    calibration_root = root.parent / f"{root.name}-calibration-{job_id}"
    FileOrchestrationJournalRepository(calibration_root).append_historical_pipeline_job(
        _job_row(job_id, "model_a", exit_code=[])
    )
    base = _nodes(_line_for(calibration_root, "pipeline_job", job_id=job_id))
    padding = total_nodes - base
    assert padding > 0
    return _job_row(job_id, "model_a", exit_code=[0] * padding)


def test_pipeline_job_at_the_node_limit_is_admitted_and_its_own_files_decode(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    writer = FileOrchestrationJournalRepository(root, max_json_nodes=NODE_LIMIT)

    writer.append_historical_pipeline_job(_job_with_nodes(root, "job_full", NODE_LIMIT))

    line = _line_for(root, "pipeline_job", job_id="job_full")
    direct_path = root / "pipeline-jobs" / "job_full.json"
    assert _nodes(line) == NODE_LIMIT
    assert _nodes(json.loads(direct_path.read_text(encoding="utf-8"))) == NODE_LIMIT
    for path in sorted(root.rglob("*.jsonl")):
        _fresh_reader(root, max_json_nodes=NODE_LIMIT)._read_jsonl(path)
    assert _fresh_reader(root, max_json_nodes=NODE_LIMIT)._read_optional_json(direct_path) is not None


def test_pipeline_job_one_node_over_the_limit_is_refused_with_zero_bytes(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    writer = FileOrchestrationJournalRepository(root, max_json_nodes=NODE_LIMIT)
    writer.append_historical_pipeline_job(_job_row("job_small", "model_a"))
    before = _tree(root)

    with pytest.raises(FileOrchestrationJournalError) as error:
        writer.append_historical_pipeline_job(_job_with_nodes(root, "job_over", NODE_LIMIT + 1))

    assert error.value.reason == "file_journal_json_node_limit_exceeded"
    assert error.value.field == "pipeline_job"
    assert _tree(root) == before


# --- restore lane: unchecked by design ------------------------------------------------


def test_restore_lane_rebuilds_a_direct_row_whose_record_is_inside_the_latest_view_headroom(
    tmp_path: Path,
) -> None:
    """A canonical row at record depth LIMIT is readable from its jsonl line and its direct
    row (offset 0) although the write-side headroom for pipeline_job would refuse it; the
    restore lane rewrites existing content, so it must still rebuild the direct row."""

    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root, max_json_depth=LIMIT)
    canonical = repository._pipeline_job_row(
        _job_row("job_restore", "model_a", exit_code=_nest(LIMIT - RECORD_PREFIX))
    )
    direct_path = root / "pipeline-jobs" / "job_restore.json"

    with repository._locked_cycle_write(source_id="gfs", cycle_time=CYCLE):
        restored = repository._restore_derived_master_direct_unlocked(canonical, source_id="gfs", cycle_time=CYCLE)

    assert restored is True
    direct = json.loads(direct_path.read_text(encoding="utf-8"))
    assert _depth(direct) == LIMIT
    assert direct["payload"]["job_id"] == "job_restore"
    assert _fresh_reader(root, max_json_depth=LIMIT)._read_optional_json(direct_path) is not None


# --- batch lane: refused whole --------------------------------------------------------


def test_batch_lane_with_one_over_limit_record_writes_nothing(tmp_path: Path) -> None:
    """``mark_pipeline_job_permanently_failed`` appends a master row and an event in one
    batch; an over-limit event must refuse the batch before the master row is appended."""

    repository, record = _terminally_failed_cohort_master(tmp_path)
    job_id = str(record["job_id"])
    before = _tree(repository.root)

    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.mark_pipeline_job_permanently_failed(
            job_id,
            error_code="OUT_OF_MEMORY",
            event_details={"probe": _nest(repository.max_json_depth)},
        )

    _assert_depth_refused(error, "pipeline_event")
    assert _tree(repository.root) == before
    assert FileOrchestrationJournalRepository(repository.root).get_pipeline_job(job_id)["status"] == "failed"


# --- historical import: refused whole, before the journal root exists ---------------


def _history(event_details: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Cycle, run and two jobs first, the event carrying ``event_details`` LAST, so a
    per-row refusal would leave the earlier rows behind."""

    return {
        "forecast_cycles": [
            {
                "cycle_id": cycle_id_for("gfs", CYCLE),
                "source_id": "gfs",
                "cycle_time": CYCLE,
                "status": "forecast_running",
                "created_at": "2026-06-28T00:00:00Z",
            }
        ],
        "hydro_runs": [_hydro_row("model_a")],
        "pipeline_jobs": [_job_row("job_a", "model_a"), _job_row("job_b", "model_b")],
        "pipeline_events": [_event_row(101, "job_a", {"trigger": "manual"}), _event_row(102, "job_a", event_details)],
    }


@pytest.mark.parametrize("key", ["object_store_root", "ordinary"])
def test_historical_import_holding_one_over_limit_record_writes_nothing(tmp_path: Path, key: str) -> None:
    """A runtime-root-shaped key (``*_root``: the public renderer recurses into a mapping
    under it) and an ordinary key carry an over-limit structure the same way."""

    journal_root = tmp_path / "journal"
    deep = _nest(FileOrchestrationJournalRepository(journal_root).max_json_depth)

    with pytest.raises(FileOrchestrationJournalError) as error:
        import_historical_scheduler_state(journal_root=journal_root, cutoff_time=CYCLE, **_history({key: deep}))

    _assert_depth_refused(error, "pipeline_event")
    assert not journal_root.exists()


def test_historical_import_holding_one_over_node_limit_record_writes_nothing(tmp_path: Path) -> None:
    journal_root = tmp_path / "journal"
    wide = [0] * FileOrchestrationJournalRepository(journal_root).max_json_nodes

    with pytest.raises(FileOrchestrationJournalError) as error:
        import_historical_scheduler_state(journal_root=journal_root, cutoff_time=CYCLE, **_history({"wide": wide}))

    assert error.value.reason == "file_journal_json_node_limit_exceeded"
    assert error.value.field == "pipeline_event"
    assert not journal_root.exists()


def test_historical_row_over_the_limit_only_before_redaction_still_imports(tmp_path: Path) -> None:
    """The public event renderer collapses any value under a sensitive key to
    ``[redacted]``, so the pre-scan must judge the record the writer builds, not the
    raw row."""

    journal_root = tmp_path / "journal"
    deep = _nest(FileOrchestrationJournalRepository(journal_root).max_json_depth)

    receipt = import_historical_scheduler_state(
        journal_root=journal_root,
        cutoff_time=CYCLE,
        **_history({"token": deep, "trigger": "manual"}),
    )

    assert receipt["imported_row_counts"]["pipeline_events"] == 2
    line = _line_for(journal_root, "pipeline_event", event_id=102)
    assert line["payload"]["details"]["token"] == "[redacted]"
    public = FileOrchestrationJournalRepository(journal_root).get_pipeline_job("job_a")
    assert public is not None
    assert public["status"] != FILE_JOURNAL_READ_BLOCKED_STATUS
