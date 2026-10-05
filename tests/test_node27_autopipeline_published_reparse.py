"""#2590: a published run's deterministic re-parse failure becomes a decline.

`mark_run_failed` never touches a `published` run (#1789 forbids demoting it),
so a rewritten product that fails to parse used to leave the run `published`,
the tick at rc=1 forever, and the #2529 residency lane blind. The autopipe now
records a `PUBLISHED_REPARSE_FAILED` decline for a DETERMINISTIC parser code
(the lane's watched source, and the end of retrying that evidence); anything
possibly transient keeps failing and retrying.

#2690: `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED` is the one `OUTPUT_PARSE_*` code
declined too -- it recurs every tick until an operator decompresses the chunk,
so retrying only kept the tick at rc=1 with the lane blind.

The unit cases fake only the subprocess boundary (`_run`) and the helpers the
fixture names; the integration case runs `_process_run`, the decline write,
the decline read and the lane's observation against PostgreSQL.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import pytest

import scripts.node27_autopipeline as autopipe
from scripts import node27_parse_failure_residency_alert as lane
from tests.test_node27_autopipeline_handoff import (
    RUN_A,
    _DeclineStore,
    _prepare_autopipe,
    _run_main,
    _set_initial_state,
)

RUN_ID = "fcst_gfs_2026092800_basin-2590"
MALFORMED = "MODEL_RIVER_FILE_MALFORMED: river output file is malformed at line 7\n"
OS_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "workers/output_parser/parser.py", line 269, in parse\n'
    "OSError: [Errno 116] Stale file handle\n"
)
CHUNK_BLOCKED = (
    "OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED: Reingest targets compressed chunk "
    "_timescaledb_internal._hyper_1_2_chunk in hydro.river_timeseries\n"
)
CHUNK_GUARD_FAILED = "OUTPUT_PARSE_COMPRESSED_CHUNK_GUARD_FAILED: catalog read timed out\n"
PSYCOPG_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "workers/output_parser/parser.py", line 298, in parse\n'
    'psycopg2.errors.ForeignKeyViolation: insert or update on table "river_timeseries" violates\n'
    'DETAIL:  Key (run_key)=(7) is not present in table "hydro_run".\n'
)


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (MALFORMED, "MODEL_RIVER_FILE_MALFORMED"),
        ("RIVQDOWN_EMPTY: no data rows\n", "RIVQDOWN_EMPTY"),
        ("UserWarning: pandas is old\n  warnings.warn(\n" + MALFORMED, "MODEL_RIVER_FILE_MALFORMED"),
        ("OUTPUT_PARSE_DB_ERROR: Output parser database operation failed: timeout\n", None),
        (CHUNK_BLOCKED, "OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED"),
        (CHUNK_GUARD_FAILED, None),
        ("OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED_AGAIN: not the block\n", None),
        ("Traceback (most recent call last):\n" + CHUNK_BLOCKED, None),
        (OS_TRACEBACK, None),
        (PSYCOPG_TRACEBACK, None),
        ("", None),
        ("something went wrong: lower case\n", None),
        ("WARNING: Ignoring invalid distribution ~umpy\nOUTPUT_PARSE_DB_ERROR: timeout\n", None),
        ("WARNING: Ignoring invalid distribution ~umpy\n" + MALFORMED, "MODEL_RIVER_FILE_MALFORMED"),
        ("DETAIL:  Key (run_key)=(7) is not present\nHINT:  retry\n", None),
        ("MANIFEST_INDEX_INVALID: Unable to safely read manifest index\n", "MANIFEST_INDEX_INVALID"),
    ],
    ids=[
        "bare-code",
        "rivqdown-empty",
        "warning-first",
        "db-error",
        "chunk-blocked",
        "guard-failed",
        "chunk-blocked-prefix-only",
        "chunk-blocked-after-traceback",
        "os-traceback",
        "psycopg-traceback-with-detail",
        "empty",
        "no-code",
        "uppercase-warning-then-transient",
        "uppercase-warning-then-deterministic",
        "libpq-detail-hint",
        "manifest-index",
    ],
)
def test_only_a_bare_parser_code_is_deterministic(stderr: str, expected: str | None) -> None:
    assert autopipe._deterministic_parse_error_code(stderr) == expected


class _Harness:
    """Fakes the subprocess boundary, the status read and the decline row write.

    `_decline_blocked_recompute` itself runs for real; `keys` are the decline
    keys `_decline_key` returns on successive calls (before parse, at decline).
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, parse_stderr: str, status: str | None) -> None:
        self.declines: list[dict[str, Any]] = []
        self.write_fails = False
        self.keys: list[tuple[str, float] | None] = [("", 100.0), ("", 100.0)]

        def fake_run(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
            if "workers.output_parser.cli" in argv:
                return 1, "", parse_stderr
            return 0, "", ""

        def fake_record(database_url: str, **kwargs: Any) -> None:
            if self.write_fails:
                raise RuntimeError("decline write refused")
            self.declines.append(kwargs)

        monkeypatch.setattr(autopipe, "_run", fake_run)
        monkeypatch.setattr(
            autopipe, "_process_forcing_stage", lambda **_kwargs: {"outcome": "degraded", "forcing_stage": {}}
        )
        monkeypatch.setattr(autopipe, "_run_status", lambda _database_url, _run_id: status)
        monkeypatch.setattr(autopipe, "_decline_key", lambda _root, _run_id: self.keys.pop(0))
        monkeypatch.setattr(autopipe, "_record_recompute_decline", fake_record)

    def process(self, tmp_path: Path) -> dict[str, Any]:
        return autopipe._process_run(
            RUN_ID, {}, object_store_root=tmp_path, database_url="postgresql://fake/db", object_store_prefix="s3://x"
        )


def test_a_published_runs_deterministic_failure_is_declined_with_its_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(monkeypatch, parse_stderr="UserWarning: x\n" + MALFORMED, status="published")

    result = harness.process(tmp_path)

    assert result["outcome"] == "declined"
    assert result["reason_code"] == autopipe.REASON_PUBLISHED_REPARSE_FAILED
    [decline] = harness.declines
    assert decline["reason_code"] == "PUBLISHED_REPARSE_FAILED"
    assert decline["detail"].startswith("MODEL_RIVER_FILE_MALFORMED: ")


def test_a_published_runs_compressed_chunk_block_is_declined_with_its_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(monkeypatch, parse_stderr=CHUNK_BLOCKED, status="published")

    result = harness.process(tmp_path)

    assert result["outcome"] == "declined"
    assert result["reason_code"] == autopipe.REASON_PUBLISHED_REPARSE_FAILED
    [decline] = harness.declines
    assert decline["reason_code"] == "PUBLISHED_REPARSE_FAILED"
    assert decline["detail"].startswith("OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED: ")
    assert "_hyper_1_2_chunk" in decline["detail"]


@pytest.mark.parametrize(
    "stderr", [OS_TRACEBACK, PSYCOPG_TRACEBACK, "OUTPUT_PARSE_DB_ERROR: timeout\n", CHUNK_GUARD_FAILED, ""]
)
def test_a_possibly_transient_failure_keeps_failing_and_retrying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    harness = _Harness(monkeypatch, parse_stderr=stderr, status="published")

    result = harness.process(tmp_path)

    assert result["outcome"] == "failed" and result["stage"] == "parse"
    assert harness.declines == []


@pytest.mark.parametrize("stderr", [MALFORMED, CHUNK_BLOCKED], ids=["malformed", "chunk-blocked"])
@pytest.mark.parametrize("status", ["failed", "succeeded", "parsed", None])
def test_only_a_published_run_is_declined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str | None, stderr: str
) -> None:
    """`failed` runs already reach the lane through their status; an unreadable status declines nothing."""
    harness = _Harness(monkeypatch, parse_stderr=stderr, status=status)

    result = harness.process(tmp_path)

    assert result["outcome"] == "failed"
    assert harness.declines == []


def test_a_product_promoted_during_the_parse_is_not_declined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure belongs to the evidence parsed; newer evidence must not be suppressed unparsed."""
    harness = _Harness(monkeypatch, parse_stderr=MALFORMED, status="published")
    harness.keys = [("", 100.0), ("", 160.0)]

    result = harness.process(tmp_path)

    assert result["outcome"] == "failed"
    assert harness.declines == []


def test_a_decline_that_does_not_commit_keeps_the_run_failing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _Harness(monkeypatch, parse_stderr=MALFORMED, status="published")
    harness.write_fails = True

    result = harness.process(tmp_path)

    assert result["outcome"] == "failed"
    assert "reason_code" not in result


# ---------------------------------------------------------------------------
# #2690: the tick. `main` runs for real over the handoff file's doubles; only
# the parse subprocess, the status read and the decline table are faked.
# ---------------------------------------------------------------------------


def _reparse_tick(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    parse_stderr: str,
    status: str | None,
) -> tuple[int, dict[str, Any], _DeclineStore]:
    store = _DeclineStore()

    def commands(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
        command = " ".join(argv)
        if "node27_ingest_run.py" in command:
            return 0, json.dumps({"status": "registered"}) + "\n", ""
        if "workers.output_parser.cli" in command:
            return 1, "", parse_stderr
        raise AssertionError(f"unexpected command: {argv}")

    object_store_root, _calls, _published = _prepare_autopipe(
        monkeypatch, tmp_path, runs={RUN_A: True}, command_handler=commands, decline_store=store
    )
    _set_initial_state(object_store_root, RUN_A, "state-a")
    monkeypatch.setattr(autopipe, "_run_status", lambda _database_url, _run_id: status)
    rc, summary = _run_main(capsys, object_store_root)
    return rc, summary, store


def test_a_published_runs_compressed_chunk_block_stops_failing_the_tick(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Before #2690: no decline, `failed` every tick, rc=1 until an operator decompressed."""
    rc, summary, store = _reparse_tick(monkeypatch, tmp_path, capsys, parse_stderr=CHUNK_BLOCKED, status="published")

    assert rc == 0
    assert summary["status"] == "completed"
    assert (summary["runs"]["failed"], summary["runs"]["declined"]) == (0, 1)
    assert summary["runs"]["declined_runs"] == [{"run_id": RUN_A, "reason_code": "PUBLISHED_REPARSE_FAILED"}]
    assert summary["runs"]["details"][0]["stage"] == "parse"
    [row] = store.rows
    assert (row["run_id"], row["init_state_id"], row["reason_code"]) == (RUN_A, "state-a", "PUBLISHED_REPARSE_FAILED")
    assert row["detail"].startswith("OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED: ")
    assert summary["declines_active"] == 1


@pytest.mark.parametrize(
    ("parse_stderr", "status"),
    [
        (CHUNK_GUARD_FAILED, "published"),
        ("OUTPUT_PARSE_DB_ERROR: timeout\n", "published"),
        (CHUNK_BLOCKED, "failed"),
        (CHUNK_BLOCKED, "parsed"),
        (CHUNK_BLOCKED, None),
    ],
    ids=["guard-failed", "db-error", "blocked-failed-run", "blocked-parsed-run", "blocked-status-unreadable"],
)
def test_every_other_compressed_chunk_shape_still_fails_the_tick(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    parse_stderr: str,
    status: str | None,
) -> None:
    rc, summary, store = _reparse_tick(monkeypatch, tmp_path, capsys, parse_stderr=parse_stderr, status=status)

    assert rc == 1
    assert (summary["runs"]["failed"], summary["runs"]["declined"]) == (1, 0)
    assert store.rows == []


# ---------------------------------------------------------------------------
# PostgreSQL: the whole path, write -> suppress -> watched -> reopen.
# ---------------------------------------------------------------------------


def _seed_published_run(database_url: str, run_id: str, *, parsed_at: datetime) -> None:
    from tests.integration_helpers import apply_migrations_from_zero

    apply_migrations_from_zero(database_url)
    connection = psycopg2.connect(database_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO core.basin (basin_id, basin_name) VALUES ('b-2590', 'b')")
            cursor.execute(
                "INSERT INTO core.basin_version (basin_version_id, basin_id, version_label, geom, active_flag) "
                "VALUES ('bv-2590', 'b-2590', 'v1', ST_SetSRID(ST_GeomFromText("
                "'MULTIPOLYGON(((99 37, 99 39, 101 39, 101 37, 99 37)))'), 4490), true)"
            )
            cursor.execute(
                "INSERT INTO core.river_network_version (river_network_version_id, basin_version_id, "
                "version_label, segment_count) VALUES ('rnv-2590', 'bv-2590', 'v1', 1)"
            )
            cursor.execute(
                "INSERT INTO core.model_instance (model_id, basin_version_id, river_network_version_id, "
                "mesh_version_id, calibration_version_id, shud_code_version, model_package_uri, active_flag, "
                "lifecycle_state) "
                "VALUES ('m-2590', 'bv-2590', 'rnv-2590', 'mesh', 'cal', '1.0', 's3://nhms/m', true, 'active')"
            )
            cursor.execute(
                "INSERT INTO hydro.hydro_run (run_id, run_type, scenario_id, model_id, basin_version_id, "
                "cycle_time, start_time, end_time, status, run_manifest_uri, parsed_at) "
                "VALUES (%s, 'forecast', 'sc', 'm-2590', 'bv-2590', now(), now(), now(), 'published', "
                "'s3://m', %s)",
                (run_id, parsed_at),
            )
    finally:
        connection.close()


def _rewrite_product(object_store_root: Path, run_id: str, mtime: float) -> Path:
    product = object_store_root / "runs" / run_id / "output" / "basin.rivqdown"
    product.parent.mkdir(parents=True, exist_ok=True)
    product.write_text("# rewritten\n", encoding="utf-8")
    os.utime(product, (mtime, mtime))
    return product


def _fetch(database_url: str, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    connection = psycopg2.connect(database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()
    finally:
        connection.close()


def _declined(database_url: str, object_store_root: Path) -> set[str]:
    connection = psycopg2.connect(database_url)
    try:
        with connection.cursor() as cursor:
            return autopipe._declined_runs(cursor, [RUN_ID], object_store_root)
    finally:
        connection.rollback()
        connection.close()


@pytest.mark.integration
def test_a_published_reparse_failure_is_recorded_suppressed_watched_and_reopened(
    throwaway_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parsed_at = datetime.now(UTC) - timedelta(hours=1)
    _seed_published_run(throwaway_database_url, RUN_ID, parsed_at=parsed_at)
    object_store_root = tmp_path / "object-store"
    product = _rewrite_product(object_store_root, RUN_ID, datetime.now(UTC).timestamp())
    stderr = {"value": MALFORMED}

    def fake_run(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
        if "workers.output_parser.cli" in argv:
            return 1, "", stderr["value"]
        return 0, "", ""  # register: the upsert keeps the status (#1789)

    monkeypatch.setattr(autopipe, "_run", fake_run)

    def process() -> dict[str, Any]:
        return autopipe._process_run(
            RUN_ID,
            {},
            object_store_root=object_store_root,
            database_url=throwaway_database_url,
            object_store_prefix="s3://nhms",
        )

    # A possibly transient failure writes nothing and keeps retrying.
    stderr["value"] = "OUTPUT_PARSE_DB_ERROR: Output parser database operation failed: timeout\n"
    assert process()["outcome"] == "failed"
    assert _fetch(throwaway_database_url, "SELECT count(*) FROM ops.ingest_recompute_decline", ()) == [(0,)]
    assert _declined(throwaway_database_url, object_store_root) == set()

    stderr["value"] = MALFORMED
    result = process()

    assert result["outcome"] == "declined"
    [(reason, detail, declined_at)] = _fetch(
        throwaway_database_url,
        "SELECT reason_code, detail, declined_at FROM ops.ingest_recompute_decline WHERE run_id = %s",
        (RUN_ID,),
    )
    assert reason == "PUBLISHED_REPARSE_FAILED"
    assert detail.startswith("MODEL_RIVER_FILE_MALFORMED: ")
    # Never demoted, never re-stamped.
    assert _fetch(
        throwaway_database_url, "SELECT status, parsed_at FROM hydro.hydro_run WHERE run_id = %s", (RUN_ID,)
    ) == [("published", parsed_at)]
    # The next tick does not retry the same evidence ...
    assert _declined(throwaway_database_url, object_store_root) == {RUN_ID}
    # ... and the residency lane now watches it, with the parser's code.
    config = lane.config_from_env({"DATABASE_URL": throwaway_database_url})
    [watched] = lane.default_observe(config, datetime.now(UTC) - timedelta(hours=6))
    assert (watched.run_id, watched.first_error_code, watched.classification) == (
        RUN_ID,
        "MODEL_RIVER_FILE_MALFORMED",
        "published_reparse",
    )
    assert watched.updated_at == declined_at

    # New evidence (a repaired product) reopens the run.
    os.utime(product, (product.stat().st_mtime + 60, product.stat().st_mtime + 60))
    assert _declined(throwaway_database_url, object_store_root) == set()


@pytest.mark.integration
def test_a_published_compressed_chunk_block_is_recorded_suppressed_and_watched(
    throwaway_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2690 against PostgreSQL: the guard failure writes nothing, the block is declined once."""
    parsed_at = datetime.now(UTC) - timedelta(hours=1)
    _seed_published_run(throwaway_database_url, RUN_ID, parsed_at=parsed_at)
    object_store_root = tmp_path / "object-store"
    _rewrite_product(object_store_root, RUN_ID, datetime.now(UTC).timestamp())
    stderr = {"value": CHUNK_GUARD_FAILED}

    def fake_run(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
        if "workers.output_parser.cli" in argv:
            return 1, "", stderr["value"]
        return 0, "", ""

    monkeypatch.setattr(autopipe, "_run", fake_run)

    def process() -> dict[str, Any]:
        return autopipe._process_run(
            RUN_ID,
            {},
            object_store_root=object_store_root,
            database_url=throwaway_database_url,
            object_store_prefix="s3://nhms",
        )

    assert process()["outcome"] == "failed"
    assert _fetch(throwaway_database_url, "SELECT count(*) FROM ops.ingest_recompute_decline", ()) == [(0,)]
    assert _declined(throwaway_database_url, object_store_root) == set()

    stderr["value"] = CHUNK_BLOCKED
    assert process()["outcome"] == "declined"

    [(reason, detail)] = _fetch(
        throwaway_database_url,
        "SELECT reason_code, detail FROM ops.ingest_recompute_decline WHERE run_id = %s",
        (RUN_ID,),
    )
    assert reason == "PUBLISHED_REPARSE_FAILED"
    assert detail.startswith("OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED: ")
    assert _fetch(
        throwaway_database_url, "SELECT status, parsed_at FROM hydro.hydro_run WHERE run_id = %s", (RUN_ID,)
    ) == [("published", parsed_at)]
    # The next tick does not retry the same evidence: decompressing changes none of it.
    assert _declined(throwaway_database_url, object_store_root) == {RUN_ID}
    config = lane.config_from_env({"DATABASE_URL": throwaway_database_url})
    [watched] = lane.default_observe(config, datetime.now(UTC) - timedelta(hours=6))
    assert (watched.run_id, watched.first_error_code, watched.classification) == (
        RUN_ID,
        "OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED",
        "published_reparse",
    )
