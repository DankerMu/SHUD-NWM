"""The node-27 weight purge tool on a real PostgreSQL, migrations from zero (#2699 part 3).

The fake-database suite (``tests/test_node27_purge_superseded_weights.py``)
proves what the tool asks and in which order.  What it cannot prove is here:

* THE RULE AS SQL.  One model per class is seeded and the classification
  statement must put each where the change proposal puts it -- worked out by
  hand in ``EXPECTED``, not derived from the statement.
* BACKUP = DELETED ROWS.  ``COPY (DELETE ... RETURNING ...) TO STDOUT`` runs for
  real; restoring the CSV with ``COPY ... FROM STDIN`` gives back the table
  byte for byte, ``weight_id`` and NULLs included.
* THE COLUMN LIST.  The tool's constant equals ``information_schema`` in table
  order, so a later ``ADD COLUMN`` fails here instead of losing a column.
* THE WRITERS' LOCK.  A session holding the advisory lock of the forcing domain
  handoff (taken by that module's own function) makes the tool hit its
  ``lock_timeout`` and delete nothing.

Run against a disposable database (never production):

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... \\
        uv run pytest -q -rs tests/test_node27_purge_superseded_weights_integration.py

SILENT-SKIP TRAP: without both variables every case skips; read the passed count.
"""

from __future__ import annotations

import csv
import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import Json, execute_values

import scripts.node27_purge_superseded_weights as tool
from packages.common import forcing_domain_handoff_apply
from tests.integration_helpers import (
    BASIN_VERSION_ID,
    CYCLE_TIME,
    MESH_VERSION_ID,
    MODEL_ID,
    RIVER_NETWORK_VERSION_ID,
    apply_migrations_from_zero,
    seed_issue_126_data,
)

pytestmark = pytest.mark.integration

PREFIX = "it2699p"
OLD = datetime.now(UTC) - timedelta(days=90)
OLDER = OLD - timedelta(days=30)
# Before the seeded model's forecast cycle, so that one stays the latest displayable gfs run.
OUTRANKED = CYCLE_TIME - timedelta(days=30)
GFS_GRID, IFS_GRID = "it2699p_gfs_grid", "it2699p_ifs_grid"
STATIONS = (f"{PREFIX}_station_1", f"{PREFIX}_station_2")

# The two the tool may purge sort first, so a failure on the first leaves the second untouched too.
PURGE_A = f"{PREFIX}_a_superseded_two_scopes"
PURGE_B = f"{PREFIX}_b_never_ran"
IFS_CURRENT = f"{PREFIX}_current_for_ifs_only"
IN_MANIFEST = f"{PREFIX}_in_manifest"
RUN_FAILED = f"{PREFIX}_run_failed_inside"
RUN_HINDCAST = f"{PREFIX}_run_hindcast_inside"
RUN_UPDATED = f"{PREFIX}_run_updated_inside"
FORCING = f"{PREFIX}_forcing_inside"
WEIGHTS = f"{PREFIX}_weights_inside"
CREATED = f"{PREFIX}_created_inside"

#: model -> class, by hand from the change proposal.  ``MODEL_ID`` is the seeded model: it owns the newest
#: displayable gfs forecast run of the basin version (2026-05-03), which outranks ``PURGE_A``'s older one.
EXPECTED = {
    MODEL_ID: "current",
    IFS_CURRENT: "current",
    IN_MANIFEST: "in_manifest",
    RUN_FAILED: "recent_run",
    RUN_HINDCAST: "recent_run",
    RUN_UPDATED: "recent_run",
    FORCING: "recent_forcing",
    WEIGHTS: "recent_weights",
    CREATED: "recently_created",
    PURGE_A: "purgeable",
    PURGE_B: "purgeable",
}
#: model -> weight rows seeded.
ROWS = {model_id: 2 for model_id in EXPECTED} | {PURGE_A: 6, PURGE_B: 3}
ALL_ROWS_SQL = "SELECT w::text AS row FROM met.interp_weight w ORDER BY weight_id"


def _connect(database_url: str) -> Any:
    connection = psycopg2.connect(database_url)
    connection.autocommit = True
    return connection


def _run_rows() -> list[tuple[Any, ...]]:
    """run id suffix, model, type, status, source, cycle_time, start_time, created_at, updated_at."""

    recent = datetime.now(UTC) - timedelta(days=1)
    return [
        # Displayable and old, but an older cycle than the seeded model's: superseded, not current.
        ("a_old", PURGE_A, "forecast", "succeeded", "gfs", OUTRANKED, OUTRANKED, OLD, OLD),
        # Displayable but without a cycle_time: `ORDER BY cycle_time DESC` sorts it first, so only the IS NOT NULL
        # predicate keeps it from being "the latest"; and a NULL is not inside the window.
        ("a_null", PURGE_A, "forecast", "succeeded", "gfs", None, OLDER, OLD, OLD),
        # The only displayable IFS run of the basin version, however old.
        ("ifs", IFS_CURRENT, "forecast", "published", "IFS", OLDER, OLDER, OLD, OLD),
        ("failed", RUN_FAILED, "forecast", "failed", "gfs", recent, OLDER, OLD, OLD),
        ("hindcast", RUN_HINDCAST, "hindcast", "succeeded", "gfs", None, recent, OLD, OLD),
        ("updated", RUN_UPDATED, "forecast", "superseded", "gfs", OLDER, OLDER, OLD, recent),
    ]


def _seed(database_url: str) -> None:
    apply_migrations_from_zero(database_url)
    seed_issue_126_data(database_url)
    connection = _connect(database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO met.data_source (source_id, source_name, source_type, status, native_format, "
                "adapter_name) VALUES ('IFS', 'IFS Integration', 'forecast', 'mock', 'grib2', 'ifs')"
            )
            for model_id in EXPECTED:
                if model_id == MODEL_ID:
                    continue
                # Inactive: one active model per basin version is a unique index.  The rule reads neither flag.
                cursor.execute(
                    """
                    INSERT INTO core.model_instance (
                        model_id, basin_version_id, river_network_version_id, mesh_version_id,
                        calibration_version_id, shud_code_version, model_package_uri,
                        active_flag, lifecycle_state, resource_profile, created_at
                    )
                    VALUES (%s, %s, %s, %s, 'calib-v1', 'shud-v1', 's3://nhms/models/it2699p/package/',
                            false, 'inactive', %s, %s)
                    """,
                    (
                        model_id,
                        BASIN_VERSION_ID,
                        RIVER_NETWORK_VERSION_ID,
                        MESH_VERSION_ID,
                        Json({}),
                        datetime.now(UTC) if model_id == CREATED else OLD,
                    ),
                )
            for index, station_id in enumerate(STATIONS):
                cursor.execute(
                    "INSERT INTO met.met_station (station_id, basin_version_id, station_name, geom, elevation_m) "
                    "VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, 30.1), 4490), 400.0)",
                    (station_id, BASIN_VERSION_ID, 'purge, "station"', 110.1 + index / 10),
                )
            weights = []
            for model_id, count in ROWS.items():
                for index in range(count):
                    # PURGE_A spans two scopes, one under the source id in capitals, and carries a signature on
                    # some rows only, so the backup holds NULLs and non-NULLs in the same column.
                    ifs = model_id == PURGE_A and index % 2 == 1
                    weights.append(
                        (
                            "IFS" if ifs else "gfs",
                            IFS_GRID if ifs else GFS_GRID,
                            model_id,
                            STATIONS[index % 2],
                            ("PRCP", "TEMP", "RH")[index % 3],
                            f"{PREFIX}_cell_{index}",
                            1.0 / (index + 3),
                            "idw",
                            f"sig-{index}" if ifs else None,
                            datetime.now(UTC) if model_id == WEIGHTS and index == 0 else OLD,
                        )
                    )
            execute_values(
                cursor,
                "INSERT INTO met.interp_weight (source_id, grid_id, model_id, station_id, variable, grid_cell_id, "
                "weight, method, grid_signature, created_at) VALUES %s",
                weights,
            )
            execute_values(
                cursor,
                "INSERT INTO hydro.hydro_run (run_id, run_type, scenario_id, model_id, basin_version_id, source_id, "
                "cycle_time, start_time, end_time, status, run_manifest_uri, created_at, updated_at) VALUES %s",
                [
                    (f"{PREFIX}_run_{suffix}", run_type, "it2699p_scenario", model_id, BASIN_VERSION_ID, source_id,
                     cycle_time, start_time, start_time + timedelta(hours=1), status,
                     "s3://nhms/runs/it2699p/manifest.json", created_at, updated_at)
                    for suffix, model_id, run_type, status, source_id, cycle_time, start_time, created_at, updated_at
                    in _run_rows()
                ],
            )  # fmt: skip
            # Forcing lands before any run row exists; the old version of PURGE_B protects nothing.
            execute_values(
                cursor,
                "INSERT INTO met.forcing_version (forcing_version_id, model_id, source_id, cycle_time, start_time, "
                "end_time, station_count, forcing_package_uri, created_at) VALUES %s",
                [
                    (f"{PREFIX}_forcing_new", FORCING, "gfs", OLDER, OLDER, OLD, 2, "s3://nhms/forcing/new/",
                     datetime.now(UTC)),
                    (f"{PREFIX}_forcing_old", PURGE_B, "gfs", OLDER, OLDER, OLD, 2, "s3://nhms/forcing/old/", OLD),
                ],
            )  # fmt: skip
    finally:
        connection.close()


def _all_rows(database_url: str) -> list[str]:
    connection = _connect(database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(ALL_ROWS_SQL)
            return [row[0] for row in cursor.fetchall()]
    finally:
        connection.close()


def _rows_of(database_url: str, model_id: str) -> list[str]:
    connection = _connect(database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT w::text FROM met.interp_weight w WHERE model_id = %s ORDER BY weight_id", (model_id,)
            )
            return [row[0] for row in cursor.fetchall()]
    finally:
        connection.close()


class Deployment:
    """The object store, the manifest and the env file of one run, bound to the throwaway database."""

    def __init__(self, database_url: str, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.database_url = database_url
        self.object_store = root / "store"
        self.receipt_root = self.object_store / "scheduler" / "weight-purge"
        self.env_file = root / "node27-ingest.env"
        manifest = self.object_store / "scheduler" / "registry" / "manifest-last.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"models": [{"model_id": MODEL_ID}, {"model_id": IN_MANIFEST}]}))
        self.env_file.write_text(f"DATABASE_URL={database_url}\n", encoding="utf-8")
        monkeypatch.setenv("DATABASE_URL", database_url)
        monkeypatch.setenv("OBJECT_STORE_ROOT", str(self.object_store))

    def run(self, *extra: str) -> int:
        arguments = ["--operator-id", "it2699p", "--reason", "integration", "--env-file", str(self.env_file)]
        return tool.main([*arguments, "--pause-seconds", "0", *extra])

    def run_directory(self) -> Path:
        (directory,) = list(self.receipt_root.iterdir())
        return directory


def test_the_column_list_is_the_table_in_order(throwaway_database_url: str) -> None:
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'met' "
                "AND table_name = 'interp_weight' ORDER BY ordinal_position"
            )
            columns = tuple(row[0] for row in cursor.fetchall())
    finally:
        connection.close()
    assert columns == tool.WEIGHT_COLUMNS


def test_each_seeded_model_lands_in_the_class_the_rule_gives_it(
    throwaway_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(throwaway_database_url)
    deployment = Deployment(throwaway_database_url, tmp_path, monkeypatch)
    before = _all_rows(throwaway_database_url)

    assert deployment.run() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["purgeable_models"] == [{"model_id": PURGE_A, "rows": 6}, {"model_id": PURGE_B, "rows": 3}]
    assert report["total_rows"] == 9 and report["largest_model"] == {"model_id": PURGE_A, "rows": 6}
    counts = {name: {"models": 0, "rows": 0} for name in tool.CLASSES}
    for model_id, name in EXPECTED.items():
        counts[name]["models"] += 1
        counts[name]["rows"] += ROWS[model_id]
    assert report["classes"] == counts
    assert report["server_version"]
    assert not deployment.receipt_root.exists()
    assert _all_rows(throwaway_database_url) == before

    # Model by model, and again one model at a time (the form each purge transaction uses).
    settings = tool.settings_from_arguments(
        tool._parse_args(
            ["--operator-id", "it2699p", "--reason", "integration", "--env-file", str(deployment.env_file)]
        )
    )
    manifest = tool.read_manifest(settings.manifest)
    connection = _connect(throwaway_database_url)
    try:
        with connection.cursor() as cursor:
            found = tool.classify(cursor, settings, manifest)
            assert {row["model_id"]: row["class"] for row in found} == EXPECTED
            assert {row["model_id"]: row["rows"] for row in found} == ROWS
            for model_id, name in EXPECTED.items():
                assert tool.classify(cursor, settings, manifest, model_id=model_id) == [
                    {"model_id": model_id, "rows": ROWS[model_id], "class": name}
                ]
            assert tool.classify(cursor, settings, manifest, model_id=f"{PREFIX}_no_such_model") == []
    finally:
        connection.close()


def test_apply_deletes_only_the_purgeable_models_and_the_backup_restores_them(
    throwaway_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(throwaway_database_url)
    deployment = Deployment(throwaway_database_url, tmp_path, monkeypatch)
    before = _all_rows(throwaway_database_url)
    purged_before = {model_id: _rows_of(throwaway_database_url, model_id) for model_id in (PURGE_A, PURGE_B)}
    assert (len(purged_before[PURGE_A]), len(purged_before[PURGE_B])) == (6, 3)

    assert deployment.run("--apply") == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["totals"] == {"models_purged": 2, "rows_purged": 9, "models_skipped": 0}
    after = _all_rows(throwaway_database_url)
    assert after == [row for row in before if row not in purged_before[PURGE_A] + purged_before[PURGE_B]]
    assert len(after) == len(before) - 9

    directory = deployment.run_directory()
    assert sorted(path.name for path in directory.iterdir()) == [
        "model-0001.json", "model-0002.json", "purge-receipt.json",
        f"weights-{PURGE_A}.csv", f"weights-{PURGE_B}.csv",
    ]  # fmt: skip
    for index, model_id in enumerate((PURGE_A, PURGE_B), start=1):
        backup = directory / f"weights-{model_id}.csv"
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
        with backup.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        assert tuple(rows[0]) == tool.WEIGHT_COLUMNS and len(rows) - 1 == ROWS[model_id]
        receipt = json.loads((directory / f"model-{index:04d}.json").read_text(encoding="utf-8"))
        assert (receipt["model_id"], receipt["status"], receipt["rows"]) == (model_id, "purged", ROWS[model_id])
        assert receipt["sha256"] == tool.succession.file_sha256(backup)

    # The runbook's restore: the model has no rows, so the copy alone brings back exactly what was deleted.
    columns = ", ".join(tool.WEIGHT_COLUMNS)
    connection = _connect(throwaway_database_url)
    try:
        with connection.cursor() as cursor:
            for model_id in (PURGE_A, PURGE_B):
                with (directory / f"weights-{model_id}.csv").open("rb") as handle:
                    cursor.copy_expert(f"COPY met.interp_weight ({columns}) FROM STDIN WITH CSV HEADER", handle)
    finally:
        connection.close()
    assert _all_rows(throwaway_database_url) == before

    # A rerun is a new run directory; with the rows restored it purges the same two again.
    assert deployment.run("--apply", "--max-models", "1", "--receipt-root", str(tmp_path / "second")) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["totals"]["models_purged"] == 1 and again["models_not_reached"] == [PURGE_B]
    assert _rows_of(throwaway_database_url, PURGE_A) == []
    assert _rows_of(throwaway_database_url, PURGE_B) == purged_before[PURGE_B]


def test_a_writer_holding_its_scope_lock_makes_the_tool_time_out_and_delete_nothing(
    throwaway_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(throwaway_database_url)
    deployment = Deployment(throwaway_database_url, tmp_path, monkeypatch)
    before = _all_rows(throwaway_database_url)
    writer = psycopg2.connect(throwaway_database_url)
    try:
        with writer.cursor() as cursor:
            # The domain handoff's own lock call, on the scope stored under the source id in capitals: a tool
            # that lowercased it, or spelled the key its own way, would not wait here and would delete the rows.
            forcing_domain_handoff_apply._lock_interp_weight_scope(cursor, "IFS", IFS_GRID, PURGE_A)
        assert deployment.run("--apply") == 1
    finally:
        writer.rollback()
        writer.close()
    failure = json.loads(capsys.readouterr().out)
    assert failure["failed_model"]["model_id"] == PURGE_A
    assert "LockNotAvailable" in failure["failed_model"]["error"]
    assert failure["failed_model"]["outcome"] == "rolled_back"
    assert failure["models"] == [] and failure["models_not_attempted"] == [PURGE_B]
    assert _all_rows(throwaway_database_url) == before
    assert [path.name for path in deployment.run_directory().iterdir()] == ["purge-failed.json"]

    # With the writer gone the same command purges both.
    assert deployment.run("--apply", "--receipt-root", str(tmp_path / "second")) == 0
    assert _rows_of(throwaway_database_url, PURGE_A) == [] and _rows_of(throwaway_database_url, PURGE_B) == []
