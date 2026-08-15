"""Receipt, schema, flock and config coverage for the #1339 backfill runner.

Split out of ``tests/test_node27_river_identity_backfill.py`` (which keeps the
batching/guard/shortfall behaviour) so neither module needs a large-file-guard
exemption. Shared cursor fakes live in ``tests/river_identity_backfill_fakes``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from scripts import node27_river_identity_backfill as backfill
from tests.river_identity_backfill_fakes import (
    NOW,
    UNCOMPRESSED_GUARD,
    FakeConnection,
    chunk,
    config,
    env,
    inventory_row,
    load_schema,
)
from tests.river_identity_backfill_fakes import (
    args as _args,
)

_TIMER_STATE = {
    "observed": True,
    "is_active": "inactive",
    "is_enabled": "masked",
    "unavailable_reason": None,
}


def _build_receipt(
    tmp_path: Path, chunks: list[backfill.ChunkRow], **cfg: Any
) -> dict[str, Any]:
    connection = FakeConnection(
        [
            (
                # Unique to _CHUNK_INVENTORY_SQL; must be matched before the
                # guard's own timescaledb_information.chunks lookup below.
                "pg_total_relation_size",
                lambda s, p: {"fetchall": [inventory_row(c) for c in chunks]},
            ),
            UNCOMPRESSED_GUARD,
            ("t.run_key IS NOT NULL", lambda s, p: {"fetchone": (0,)}),
            ("SELECT count(*)", lambda s, p: {"fetchone": (0,)}),
        ]
    )
    return backfill.build_receipt(
        config(tmp_path, **cfg),
        now_utc=NOW,
        connect=lambda _url: connection,
        head_sha="0" * 40,
        timer_state=dict(_TIMER_STATE),
    )


# ---------------------------------------------------------------------------
# 1. Receipt: schema conformance
# ---------------------------------------------------------------------------


def test_dry_run_receipt_validates_against_the_published_schema(tmp_path: Path) -> None:
    receipt = _build_receipt(
        tmp_path,
        [chunk("terminal", days_old=30), chunk("frozen", days_old=30, compressed=True),
         chunk("active", days_old=0)],
    )

    jsonschema.validate(receipt, load_schema())
    assert receipt["outcome"] == "clean"
    assert receipt["mode"] == "dry-run"
    assert receipt["totals"]["updated_rows"] == 0
    assert receipt["totals"]["chunks_skipped_compressed"] == 1
    assert receipt["totals"]["chunks_skipped_active"] == 1


def test_skipped_chunks_report_pending_rows_as_null_rather_than_a_fabricated_zero(
    tmp_path: Path,
) -> None:
    """0 is a measurement. A skipped chunk was never counted, and reporting 0
    for it is the exact claim an operator would read as "nothing left here"."""
    receipt = _build_receipt(
        tmp_path,
        [chunk("frozen", days_old=30, compressed=True), chunk("active", days_old=0),
         chunk("terminal", days_old=30)],
    )

    jsonschema.validate(receipt, load_schema())
    by_state = {c["state"]: c for c in receipt["chunks"]}
    assert by_state["skipped_compressed"]["pending_rows"] is None
    assert by_state["skipped_active"]["pending_rows"] is None
    # The chunk that WAS measured still reports a number.
    assert by_state["skipped_no_pending"]["pending_rows"] == 0


def test_stop_stage_enum_contains_only_stages_the_runner_can_emit() -> None:
    """A dead enum value is a documented behaviour that does not exist. Budget
    exhaustion is chunk state 'deferred'; the compression timer is observed,
    never enforced."""
    schema = load_schema()
    stages = set(schema["$defs"]["stop"]["properties"]["stage"]["enum"])

    assert stages == {
        "shortfall",
        "duration_wall",
        "ingest_not_quiescent",
        "compressed_chunk_guard",
    }

    source = Path(backfill.__file__).read_text(encoding="utf-8")
    for stage in stages:
        assert f'"{stage}"' in source, f"schema advertises unreachable stop stage {stage}"


def test_receipt_records_the_compression_timer_state(tmp_path: Path) -> None:
    """D6: an enforce window that left the timer running is a recoverable-only-
    by-decompressing-200GB mistake. The receipt has to show which it was."""
    receipt = _build_receipt(tmp_path, [chunk("terminal", days_old=30)])

    assert receipt["compression_timer"]["is_enabled"] == "masked"


def test_receipt_reports_bloat_against_measured_disk_headroom(tmp_path: Path) -> None:
    receipt = _build_receipt(tmp_path, [chunk("terminal", days_old=30, total_bytes=4096)])

    disk = receipt["disk"]
    assert disk["estimated_bloat_bytes"] == 4096
    assert disk["mount"] == "/home"
    assert disk["headroom_sufficient"] in {True, False, None}


def test_refused_lock_receipt_carries_no_chunk_inventory(tmp_path: Path) -> None:
    receipt = backfill.build_refused_lock_receipt(
        config(tmp_path), now_utc=NOW, head_sha="a" * 40
    )

    jsonschema.validate(receipt, load_schema())
    assert receipt["outcome"] == "refused_lock"
    assert receipt["chunks"] == []
    assert receipt["cursor"] == {}


def test_failed_receipt_without_provenance_omits_head_sha(tmp_path: Path) -> None:
    receipt = backfill.build_failed_receipt(
        config(tmp_path), now_utc=NOW, stage="runner", head_sha=None
    )

    jsonschema.validate(receipt, load_schema())
    assert receipt["provenance_state"] == "unavailable"
    assert "head_sha" not in receipt


def test_schema_rejects_a_stopped_receipt_that_hides_why_it_stopped() -> None:
    """A halted run that looks like a finished run is the failure this schema
    rule exists to prevent."""
    bad = {
        "schema_version": "1.0",
        "generated_at": "2026-08-15T12:00:00Z",
        "mode": "enforce",
        "outcome": "stopped",
    }

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, load_schema())


def test_schema_rejects_a_dry_run_that_claims_to_have_updated_rows() -> None:
    bad = {
        "schema_version": "1.0",
        "head_sha": "0" * 40,
        "generated_at": "2026-08-15T12:00:00Z",
        "now_utc": "2026-08-15T12:00:00Z",
        "hypertable": "hydro.river_timeseries",
        "mode": "dry-run",
        "outcome": "clean",
        "bounds": {
            "batch_pages": 1,
            "duration_wall_ms": 1000,
            "batch_sleep_ms": 0,
            "max_batches": 1,
            "lag_seconds": 1,
        },
        "totals": {
            "candidate_rows": 5,
            "updated_rows": 5,
            "pending_rows": 0,
            "batches_run": 1,
            "batches_halved": 0,
            "unmatched_rows": 0,
            "unmappable_rows": 0,
            "chunks_skipped_compressed": 0,
            "chunks_skipped_active": 0,
        },
    }

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, load_schema())


# ---------------------------------------------------------------------------
# 2. Bounded budget and stop shapes at the receipt level
# ---------------------------------------------------------------------------


def test_per_invocation_batch_budget_defers_the_remainder(tmp_path: Path) -> None:
    target = chunk("c1", days_old=30, pages=100)
    connection = FakeConnection(
        [
            UNCOMPRESSED_GUARD,
            ("t.run_key IS NOT NULL", lambda s, p: {"fetchone": (0,)}),
            ("UPDATE ONLY", lambda s, p: {"rowcount": 4}),
            ("SELECT count(*)", lambda s, p: {"fetchone": (4,)}),
        ]
    )
    settings = config(tmp_path, enforce=True, batch_pages=10, max_batches=2, batch_sleep_ms=0)

    descriptor = backfill.process_chunk(connection, target, settings, cursor_budget=[2])

    assert descriptor["batches_run"] == 2
    assert descriptor["state"] == "deferred"
    assert descriptor["next_page"] == 20


def test_a_refused_final_sweep_is_a_stopped_receipt_not_an_empty_failure_shell(
    tmp_path: Path,
) -> None:
    """The refusal happens before any chunk is touched, but the run still
    discovered a full inventory. Letting the stop escape to the generic
    failure handler throws that away and reports outcome 'failed' with no
    chunks and no totals — unreadable as an operational record."""
    active = chunk("active", days_old=0)
    samples = iter([1000, 1042])
    connection = FakeConnection(
        [
            ("pg_total_relation_size", lambda s, p: {"fetchall": [inventory_row(active)]}),
            ("pg_stat_all_tables", lambda s, p: {"fetchone": (next(samples),)}),
            UNCOMPRESSED_GUARD,
            ("t.run_key IS NOT NULL", lambda s, p: {"fetchone": (0,)}),
            ("SELECT count(*)", lambda s, p: {"fetchone": (0,)}),
        ]
    )

    receipt = backfill.build_receipt(
        config(tmp_path, enforce=True, final_sweep=True, batch_sleep_ms=0),
        now_utc=NOW,
        connect=lambda _url: connection,
        head_sha="0" * 40,
        timer_state=dict(_TIMER_STATE),
        sleep=lambda _s: None,
    )

    jsonschema.validate(receipt, load_schema())
    assert receipt["outcome"] == "stopped"
    assert receipt["stop"]["stage"] == "ingest_not_quiescent"
    assert receipt["chunks"] and receipt["chunks"][0]["state"] == "stopped"
    assert receipt["chunks"][0]["pending_rows"] is None
    assert receipt["totals"]["updated_rows"] == 0


def _stopping_connection(target: backfill.ChunkRow, *, guard: Any = None) -> FakeConnection:
    """Three planned batches; the third under-updates and is rolled back."""
    calls: list[int] = []

    def update_handler(sql: str, params: Any) -> Any:
        calls.append(len(calls))
        return {"rowcount": 1 if len(calls) == 3 else 2}

    return FakeConnection(
        [
            ("pg_total_relation_size", lambda s, p: {"fetchall": [inventory_row(target)]}),
            guard or UNCOMPRESSED_GUARD,
            ("t.run_key IS NOT NULL", lambda s, p: {"fetchone": (0,)}),
            ("NOT EXISTS (SELECT 1 FROM hydro.hydro_run", lambda s, p: {"fetchone": (1,)}),
            ("NOT IN (SELECT unnest", lambda s, p: {"fetchone": (0,)}),
            ("UPDATE ONLY", update_handler),
            ("SELECT count(*) FROM ONLY", lambda s, p: {"fetchone": (7,)}),
            ("SELECT count(*)", lambda s, p: {"fetchone": (2,)}),
        ]
    )


def test_a_shortfall_stopped_receipt_totals_only_the_rows_that_persisted(
    tmp_path: Path,
) -> None:
    """totals.updated_rows is a rows-in-the-table claim.

    Folding the rolled-back batch into it publishes rows that are not there,
    and breaks the property the integration test relies on — that successive
    invocations partition the table exactly.
    """
    target = chunk("c1", days_old=30, pages=12)
    connection = _stopping_connection(target)

    receipt = backfill.build_receipt(
        config(tmp_path, enforce=True, batch_pages=4, max_batches=10, batch_sleep_ms=0),
        now_utc=NOW,
        connect=lambda _url: connection,
        head_sha="0" * 40,
        timer_state=dict(_TIMER_STATE),
    )

    jsonschema.validate(receipt, load_schema())
    assert receipt["outcome"] == "stopped"
    assert receipt["stop"]["stage"] == "shortfall"
    assert connection.commits == 2
    assert receipt["totals"]["updated_rows"] == 4
    assert receipt["totals"]["candidate_rows"] == 4
    # Attempts, not commits — this is what the budget spent.
    assert receipt["totals"]["batches_run"] == 3
    # The failed batch is still fully described, just not counted as progress.
    assert receipt["stop"]["updated_rows"] == 1
    assert receipt["stop"]["candidate_rows"] == 2


def test_a_guard_error_produces_a_stopped_receipt_not_a_bare_failure_shell(
    tmp_path: Path,
) -> None:
    """A guard trip mid-chunk must not erase the invocation's own record.

    build_failed_receipt keeps no chunks, no totals and no cursor, and stamps
    mutation_state="failed_before_mutation" — three false statements once a
    batch has committed.
    """
    target = chunk("c1", days_old=30, pages=12)
    guard_calls: list[int] = []

    def guard_handler(sql: str, params: Any) -> Any:
        guard_calls.append(len(guard_calls))
        return {"fetchone": (len(guard_calls) > 2,)}

    connection = _stopping_connection(
        target,
        guard=("SELECT is_compressed FROM timescaledb_information.chunks", guard_handler),
    )

    receipt = backfill.build_receipt(
        config(tmp_path, enforce=True, batch_pages=4, max_batches=10, batch_sleep_ms=0),
        now_utc=NOW,
        connect=lambda _url: connection,
        head_sha="0" * 40,
        timer_state=dict(_TIMER_STATE),
    )

    jsonschema.validate(receipt, load_schema())
    assert receipt["outcome"] == "stopped"
    assert receipt["stop"]["stage"] == "compressed_chunk_guard"
    assert "failure" not in receipt
    assert receipt["chunks"][0]["state"] == "stopped"
    assert receipt["totals"]["updated_rows"] == 2  # the one committed batch
    assert receipt["cursor"][target.key] == 4  # resume where the guard tripped


def test_a_lock_refusal_carries_the_previous_cursor_instead_of_wiping_it(
    tmp_path: Path,
) -> None:
    """flock contention is a routine exit-0 event on overlapping ticks.

    Publishing an empty cursor over a real one would rewind every chunk to
    page 0 on the next run — a full 460M-row rescan caused by two timers
    overlapping by a second.
    """
    settings = config(tmp_path)
    prior = {
        "schema_version": backfill.SCHEMA_VERSION,
        "cursor": {"_timescaledb_internal._hyper_3_55": 4321},
    }
    settings.receipt_path.write_text(json.dumps(prior), encoding="utf-8")

    receipt = backfill.build_refused_lock_receipt(settings, now_utc=NOW, head_sha="a" * 40)

    jsonschema.validate(receipt, load_schema())
    assert receipt["outcome"] == "refused_lock"
    assert receipt["chunks"] == []
    assert receipt["cursor"] == {"_timescaledb_internal._hyper_3_55": 4321}

    # End to end: the refusal is published, and the cursor survives the round
    # trip so the next invocation still resumes.
    backfill.publish_receipt(settings, receipt)
    assert backfill.load_persisted_cursor(settings.receipt_path) == {
        "_timescaledb_internal._hyper_3_55": 4321
    }


def test_a_failed_receipt_deliberately_carries_no_cursor() -> None:
    """Spec-blessed loss: after an unclassified crash the runner cannot vouch
    for how far it got, and the NULL sentinel makes a full rescan identical in
    outcome. Reconstructing a cursor here would be a guess dressed as state."""
    receipt = backfill.build_failed_receipt(
        None, now_utc=NOW, stage="runner", head_sha="0" * 40
    )

    jsonschema.validate(receipt, load_schema())
    assert "cursor" not in receipt
    assert "DELIBERATELY absent from a failed receipt" in (
        load_schema()["properties"]["cursor"]["description"]
    )


# ---------------------------------------------------------------------------
# 3. flock single-instance mutex
# ---------------------------------------------------------------------------


def test_second_holder_is_refused_the_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "runner.lock"

    first = backfill.acquire_lock(lock_path)
    assert first is not None
    try:
        assert backfill.acquire_lock(lock_path) is None
    finally:
        os.close(first)

    # Released: a later invocation gets it.
    third = backfill.acquire_lock(lock_path)
    assert third is not None
    os.close(third)


def test_lock_file_is_created_mode_0600(tmp_path: Path) -> None:
    lock_path = tmp_path / "runner.lock"
    fd = backfill.acquire_lock(lock_path)
    try:
        assert oct(lock_path.stat().st_mode & 0o777) == "0o600"
    finally:
        os.close(fd)


def test_lock_path_must_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(backfill.BackfillConfigError):
        backfill.acquire_lock(Path("relative.lock"))


def test_contended_lock_publishes_a_refusal_receipt_and_touches_no_database(
    tmp_path: Path,
) -> None:
    values = env(tmp_path)
    for key, value in values.items():
        os.environ[key] = value
    holder = backfill.acquire_lock(tmp_path / "runner.lock")
    assert holder is not None

    def _explode(_url: str) -> Any:  # pragma: no cover - must never run
        raise AssertionError("lock contention must be decided before any DB call")

    try:
        exit_code = backfill.main([], now_utc=NOW, connect=_explode)
    finally:
        os.close(holder)
        for key in values:
            os.environ.pop(key, None)

    assert exit_code == 0
    receipt = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    jsonschema.validate(receipt, load_schema())
    assert receipt["outcome"] == "refused_lock"


# ---------------------------------------------------------------------------
# 4. Config fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"NODE27_RIVER_IDENTITY_BACKFILL_RECEIPT_PATH": None}, "receipt path must be set"),
        ({"NODE27_RIVER_IDENTITY_BACKFILL_RECEIPT_PATH": "relative.json"}, "must be absolute"),
        ({"NODE27_RIVER_IDENTITY_BACKFILL_LOCK_PATH": None}, "lock path must be set"),
        ({"DATABASE_URL": None}, "DATABASE_URL must be set"),
        ({"NODE27_RIVER_IDENTITY_BACKFILL_BATCH_PAGES": "0"}, "must be >= 1"),
        ({"NODE27_RIVER_IDENTITY_BACKFILL_DURATION_WALL_MS": "10"}, "must be >= 1000"),
    ],
)
def test_config_fails_closed_on_bad_shape(
    tmp_path: Path, override: dict[str, str | None], expected: str
) -> None:
    with pytest.raises(backfill.BackfillConfigError, match=expected):
        backfill.config_from_args(_args(), env(tmp_path, **override))


def test_receipt_and_lock_paths_must_be_disjoint(tmp_path: Path) -> None:
    shared = str(tmp_path / "same.json")
    with pytest.raises(backfill.BackfillConfigError):
        backfill.config_from_args(
            _args(),
            env(
                tmp_path,
                NODE27_RIVER_IDENTITY_BACKFILL_RECEIPT_PATH=shared,
                NODE27_RIVER_IDENTITY_BACKFILL_LOCK_PATH=shared,
            ),
        )


def test_stderr_diagnostics_never_leak_the_dsn_password(capsys: pytest.CaptureFixture[str]) -> None:
    backfill._emit("failed", "boom", dsn="postgresql://user:secretpw@127.0.0.1:55432/nhms")

    captured = capsys.readouterr().err
    assert "secretpw" not in captured
    assert "127.0.0.1:55432" in captured
