"""The node-27 weight purge tool against its own small fake database (#2699 part 3).

Everything is in ``tmp_path``: an object store with the canonical manifest, a
real env file and real receipt / backup files.  The fake database is strict:
its cursor knows exactly the statements written down in this module -- not the
tool's constants -- and fails on any other, so a second table, a ``VACUUM`` or
a reworded classification cannot pass.  Deletes happen in a per-connection
working copy that only ``commit`` makes visible.

The fake answers the classification statement with the rule worked out here in
Python from the change proposal.  That proves the tool's plumbing (what it
asks, in which order, what it does with each class); the SQL itself is run by
``tests/test_node27_purge_superseded_weights_integration.py`` on a real
PostgreSQL.
"""

from __future__ import annotations

import csv
import errno
import fcntl
import hashlib
import io
import json
import os
import re
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg2
import pytest
from psycopg2 import sql

import scripts.node27_purge_superseded_weights as tool
from packages.common import forcing_domain_handoff_apply
from workers.forcing_producer.store import PsycopgForcingRepository

PASSWORD = "s3cret-pw-7Qx"
DATABASE_URL = f"postgresql://nhms_ingest_rw:{PASSWORD}@127.0.0.1:55432/nhms"
NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
OLD = NOW - timedelta(days=90)
INSIDE = NOW - timedelta(days=1)
BV = "basins_demo_v1"

# The statements the tool may send, written out here on purpose (see the module docstring).
SERVER_VERSION_SQL = "SHOW server_version"
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '10s'"
PAIRS_SQL = "SELECT DISTINCT source_id, grid_id FROM met.interp_weight WHERE model_id = %s"
ADVISORY_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"
REMAINING_SQL = "SELECT count(*) FROM met.interp_weight WHERE model_id = %s"
COLUMNS = (
    "weight_id", "source_id", "grid_id", "model_id", "station_id", "variable", "grid_cell_id", "weight", "method",
    "created_at", "grid_signature", "active_flag", "superseded_at", "grid_snapshot_id",
)  # fmt: skip
COPY_PATTERN = re.compile(
    r"COPY \(DELETE FROM met\.interp_weight WHERE model_id = '(?P<model>[^']*)' AND \(source_id, grid_id\) IN "
    r"\((?P<pairs>.*)\) RETURNING (?P<columns>.*)\) TO STDOUT WITH CSV HEADER"
)
CLASSIFY_SQL = """
WITH cutoff AS ( SELECT now() - make_interval(days => %s) AS at ),
weights AS (
  SELECT w.model_id, count(*) AS row_count, coalesce(bool_or(w.created_at >= c.at), false) AS recent
  FROM met.interp_weight w CROSS JOIN cutoff c
  WHERE (%s::text IS NULL OR w.model_id = %s)
  GROUP BY w.model_id
),
latest AS (
  SELECT DISTINCT ON (h.basin_version_id, lower(h.source_id)) h.model_id
  FROM hydro.hydro_run h
  WHERE h.run_type = 'forecast' AND h.status IN ('succeeded', 'parsed', 'published')
    AND h.cycle_time IS NOT NULL
  ORDER BY h.basin_version_id, lower(h.source_id), h.cycle_time DESC, h.run_id DESC
),
recent_runs AS (
  SELECT DISTINCT h.model_id FROM hydro.hydro_run h CROSS JOIN cutoff c
  WHERE h.cycle_time >= c.at OR h.start_time >= c.at OR h.created_at >= c.at OR h.updated_at >= c.at
),
recent_forcing AS (
  SELECT DISTINCT f.model_id FROM met.forcing_version f CROSS JOIN cutoff c WHERE f.created_at >= c.at
)
SELECT w.model_id, w.row_count,
  CASE
    WHEN EXISTS (SELECT 1 FROM latest l WHERE l.model_id = w.model_id) THEN 'current'
    WHEN w.model_id = ANY (%s::text[]) THEN 'in_manifest'
    WHEN EXISTS (SELECT 1 FROM recent_runs r WHERE r.model_id = w.model_id) THEN 'recent_run'
    WHEN EXISTS (SELECT 1 FROM recent_forcing f WHERE f.model_id = w.model_id) THEN 'recent_forcing'
    WHEN w.recent THEN 'recent_weights'
    WHEN (m.created_at < c.at) IS NOT TRUE THEN 'recently_created'
    ELSE 'purgeable'
  END AS class
FROM weights w CROSS JOIN cutoff c
LEFT JOIN core.model_instance m ON m.model_id = w.model_id
ORDER BY w.model_id
"""
DISPLAYABLE = ("succeeded", "parsed", "published")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _render(node: Any, literals: list[str]) -> str:
    """The text of a composed statement, as psycopg2 would send it; collects what was passed as a literal."""

    if isinstance(node, sql.Composed):
        return "".join(_render(part, literals) for part in node.seq)
    if isinstance(node, sql.SQL):
        return node.string
    if isinstance(node, sql.Identifier):
        return ".".join(node.strings)
    assert isinstance(node, sql.Literal) and isinstance(node.wrapped, str), f"not a literal: {node!r}"
    literals.append(node.wrapped)
    return "'" + node.wrapped.replace("'", "''") + "'"


class FakeDatabase:
    """Committed tables, every statement received, and the failures a test injects."""

    def __init__(self) -> None:
        self.models: dict[str, datetime] = {}  # model_id -> core.model_instance.created_at
        self.weights: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.forcing: list[dict[str, Any]] = []
        self.connections: list[FakeConnection] = []
        self.statements: list[tuple[str, Any]] = []
        self.events: list[str] = []
        self.on_lock: Callable[[str], None] | None = None
        self.after_copy: Callable[[str], None] | None = None
        self.commit_error: BaseException | None = None
        self.connect_error: BaseException | None = None
        self.next_weight_id = 1000

    def connect(self, dsn: str, **_options: Any) -> FakeConnection:
        assert dsn == DATABASE_URL
        if self.connect_error is not None:
            raise self.connect_error
        self.connections.append(FakeConnection(self))
        return self.connections[-1]

    def add_model(
        self, model_id: str, rows: int = 2, *, created: datetime = OLD, weights_created: datetime = OLD,
        scopes: tuple[tuple[str, str], ...] = (("gfs", "gfs_0p25"),),
    ) -> None:  # fmt: skip
        self.models[model_id] = created
        for index in range(rows):
            self.insert_weight(model_id, *scopes[index % len(scopes)], created_at=weights_created)

    def insert_weight(self, model_id: str, source_id: str, grid_id: str, *, created_at: datetime = OLD) -> None:
        """A committed row, visible at once to every open transaction (READ COMMITTED)."""

        self.next_weight_id += 1
        weight_id = self.next_weight_id
        row = {
            "weight_id": weight_id, "source_id": source_id, "grid_id": grid_id, "model_id": model_id,
            "station_id": f"station, \"{weight_id}\"", "variable": "PRCP", "grid_cell_id": f"cell_{weight_id}",
            "weight": 0.25, "method": "idw", "created_at": created_at, "grid_signature": None, "active_flag": True,
            "superseded_at": None, "grid_snapshot_id": None,
        }  # fmt: skip
        self.weights.append(row)

    def add_run(self, model_id: str, run_id: str, **fields: Any) -> None:
        row = {
            "run_id": run_id, "model_id": model_id, "run_type": "forecast", "status": "succeeded",
            "basin_version_id": BV, "source_id": "gfs", "cycle_time": OLD, "start_time": OLD, "created_at": OLD,
            "updated_at": OLD,
        }  # fmt: skip
        self.runs.append({**row, **fields})

    def rows_of(self, model_id: str) -> list[dict[str, Any]]:
        return [row for row in self.weights if row["model_id"] == model_id]

    def classes(self, weights: list[dict[str, Any]], days: int, only: str | None, manifest: list[str]) -> list[Any]:
        """The rule of the change proposal, first match wins; a NULL timestamp is never inside the window."""

        cutoff = NOW - timedelta(days=days)

        def inside(value: datetime | None) -> bool:
            return value is not None and value >= cutoff

        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for run in self.runs:
            if run["run_type"] != "forecast" or run["status"] not in DISPLAYABLE or run["cycle_time"] is None:
                continue
            key = (run["basin_version_id"], run["source_id"].lower())
            best = latest.get(key)
            if best is None or (run["cycle_time"], run["run_id"]) > (best["cycle_time"], best["run_id"]):
                latest[key] = run
        current = {run["model_id"] for run in latest.values()}
        result = []
        for model_id in sorted({row["model_id"] for row in weights}):
            if only is not None and model_id != only:
                continue
            mine = [row for row in weights if row["model_id"] == model_id]
            runs = [run for run in self.runs if run["model_id"] == model_id]
            if model_id in current:
                name = "current"
            elif model_id in manifest:
                name = "in_manifest"
            elif any(inside(run[c]) for run in runs for c in ("cycle_time", "start_time", "created_at", "updated_at")):
                name = "recent_run"
            elif any(inside(row["created_at"]) for row in self.forcing if row["model_id"] == model_id):
                name = "recent_forcing"
            elif any(inside(row["created_at"]) for row in mine):
                name = "recent_weights"
            elif model_id not in self.models or not self.models[model_id] < cutoff:
                name = "recently_created"
            else:
                name = "purgeable"
            result.append((model_id, len(mine), name))
        return result


class FakeConnection:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.autocommit = True
        self.readonly: bool | None = None
        self.session: dict[str, Any] = {}
        self.deleted: set[int] = set()  # weight ids deleted in the open transaction
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.sent = 0

    def set_session(self, **options: Any) -> None:
        assert self.sent == 0, "set_session after a statement"
        self.session = options
        self.readonly = options.get("readonly")

    def visible(self) -> list[dict[str, Any]]:
        return [row for row in self.database.weights if row["weight_id"] not in self.deleted]

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.database.events.append("commit")
        if self.database.commit_error is not None:
            raise self.database.commit_error
        self.database.weights = self.visible()
        self.deleted = set()
        self.commits += 1

    def rollback(self) -> None:
        self.deleted = set()
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.database = connection.database
        self.result: list[tuple[Any, ...]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def _record(self, statement: str, parameters: Any) -> None:
        assert not self.connection.closed and self.connection.autocommit is False
        self.connection.sent += 1
        self.database.statements.append((statement, parameters))

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
        assert isinstance(statement, str)
        self._record(statement, parameters)
        flat = _flat(statement)
        if flat == SERVER_VERSION_SQL:
            self.result = [("15.13 (Ubuntu 15.13-1.pgdg22.04+1)",)]
        elif flat == LOCK_TIMEOUT_SQL:
            self.database.events.append("lock_timeout")
        elif flat == PAIRS_SQL:
            pairs = {
                (r["source_id"], r["grid_id"]) for r in self.connection.visible() if r["model_id"] == parameters[0]
            }
            self.result = sorted(pairs, reverse=True)  # not the order the tool must lock in
        elif flat == ADVISORY_LOCK_SQL:
            self.database.events.append(f"lock:{parameters[0]}")
            if self.database.on_lock is not None:
                self.database.on_lock(parameters[0])
        elif flat == REMAINING_SQL:
            self.result = [(sum(1 for r in self.connection.visible() if r["model_id"] == parameters[0]),)]
        elif flat == _flat(CLASSIFY_SQL):
            days, only, again, manifest = parameters
            assert only == again and isinstance(manifest, list)
            self.database.events.append(f"classify:{only}")
            self.result = self.database.classes(self.connection.visible(), days, only, manifest)
        else:
            raise AssertionError(f"a statement this tool must not send: {flat}")

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.result)

    def fetchone(self) -> tuple[Any, ...]:
        return self.result[0]

    def copy_expert(self, statement: Any, file: Any) -> None:
        assert isinstance(statement, sql.Composed), "the COPY must be composed with psycopg2.sql, not formatted"
        literals: list[str] = []
        text = _render(statement, literals)
        self._record(text, None)
        match = COPY_PATTERN.fullmatch(text)
        assert match, f"a COPY this tool must not send: {text}"
        assert match["columns"] == ", ".join(COLUMNS)
        pairs = re.findall(r"\('([^']*)', '([^']*)'\)", match["pairs"])
        # Every value in the statement came in as a literal: the model id, then each pair.
        assert literals == [match["model"], *(value for pair in pairs for value in pair)]
        if self.connection.readonly:
            raise psycopg2.errors.ReadOnlySqlTransaction("cannot execute DELETE in a read-only transaction")
        self.database.events.append(f"copy:{match['model']}")
        doomed = [
            row for row in self.connection.visible()
            if row["model_id"] == match["model"] and (row["source_id"], row["grid_id"]) in pairs
        ]  # fmt: skip
        self.connection.deleted |= {row["weight_id"] for row in doomed}
        file.write(csv_bytes(doomed))
        if self.database.after_copy is not None:
            self.database.after_copy(match["model"])


def csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    """Rows as ``COPY ... WITH CSV HEADER`` writes them: NULL is the empty unquoted field."""

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow(["" if row[c] is None else ("t" if row[c] is True else row[c]) for c in COLUMNS])
    return buffer.getvalue().encode("utf-8")


class Space:
    def __init__(self, root: Path, database: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
        self.root, self.database, self.capsys = root, database, capsys
        self.object_store = root / "store"
        self.manifest = self.object_store / "scheduler" / "registry" / "manifest-last.json"
        self.receipt_root = self.object_store / "scheduler" / "weight-purge"
        self.env_file = root / "env" / "node27-ingest.env"
        self.sleeps: list[float] = []
        self.on_sleep: Callable[[], None] | None = None
        self.out = self.err = ""

    def write_manifest(self, model_ids: list[str]) -> None:
        self.manifest.write_text(json.dumps({"models": [{"model_id": m} for m in model_ids]}), encoding="utf-8")

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep()

    def run(self, *extra: str) -> int:
        arguments = ["--operator-id", "ops-test", "--reason", "owner decision 2026-10-07", "--env-file"]
        status = tool.main([*arguments, str(self.env_file), *extra], sleep=self._sleep)
        self.out, self.err = self.capsys.readouterr()
        return status

    def report(self) -> dict[str, Any]:
        return json.loads(self.out)

    def run_directory(self) -> Path:
        (directory,) = sorted(self.receipt_root.iterdir())
        assert re.fullmatch(r"purge-\d{8}T\d{6}Z", directory.name)
        return directory

    def receipt(self, name: str) -> dict[str, Any]:
        return json.loads((self.run_directory() / name).read_text(encoding="utf-8"))

    def files_written(self) -> list[str]:
        """Everything under the root except the two inputs, relative."""

        inputs = {self.manifest, self.env_file}
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file() and p not in inputs)

    def assert_no_secret_anywhere(self) -> None:
        texts = [self.out, self.err, *(p.read_text(encoding="utf-8") for p in self.receipt_root.rglob("*.json"))]
        assert not [text for text in texts if PASSWORD in text or DATABASE_URL in text]


LEGACY, OLD_A, OLD_B = "basins_legacy_shud", "dg_old_a", "dg_old_b"
LEGACY_SCOPES = (("gfs", "gfs_0p25"), ("IFS", "ifs_0p25"))


@pytest.fixture()
def space(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Space:
    """One model per class, and three purgeable ones (the legacy model has two scopes)."""

    database = FakeDatabase()
    built = Space(tmp_path, database, capsys)
    built.manifest.parent.mkdir(parents=True)
    built.write_manifest(["dg_current", "dg_manifest"])
    built.env_file.parent.mkdir()
    built.env_file.write_text(f"# test copy\nDATABASE_URL={DATABASE_URL}\nAUTOPIPE_RUN_WORKERS=2\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(built.object_store))
    monkeypatch.setattr(psycopg2, "connect", database.connect)
    database.add_model("dg_current", 3)
    database.add_run("dg_current", "run_current", cycle_time=OLD + timedelta(days=5))
    database.add_model("dg_manifest", 2)
    database.add_model("dg_recent_run", 2)
    database.add_run("dg_recent_run", "run_failed", status="failed", cycle_time=INSIDE)
    database.add_model("dg_recent_forcing", 1)
    database.forcing.append({"model_id": "dg_recent_forcing", "created_at": INSIDE})
    database.add_model("dg_recent_weights", 2, weights_created=INSIDE)
    database.add_model("dg_recently_created", 1, created=INSIDE)
    # Still `active` in production, with an old run that an even older cycle of the current model outranks.
    database.add_model(LEGACY, 5, scopes=LEGACY_SCOPES)
    database.add_run(LEGACY, "run_legacy", cycle_time=OLD)
    database.add_model(OLD_A, 2)
    database.add_model(OLD_B, 4)
    return built


PROTECTED = ("dg_current", "dg_manifest", "dg_recent_run", "dg_recent_forcing", "dg_recent_weights",
             "dg_recently_created")  # fmt: skip


def test_dry_run_reports_every_class_and_writes_nothing(space: Space) -> None:
    before = [dict(row) for row in space.database.weights]
    assert space.run() == 0
    report = space.report()
    assert report["dry_run"] is True
    assert report["server_version"].startswith("15.13")
    assert report["classes"] == {
        "current": {"models": 1, "rows": 3},
        "in_manifest": {"models": 1, "rows": 2},
        "recent_run": {"models": 1, "rows": 2},
        "recent_forcing": {"models": 1, "rows": 1},
        "recent_weights": {"models": 1, "rows": 2},
        "recently_created": {"models": 1, "rows": 1},
        "purgeable": {"models": 3, "rows": 11},
    }
    assert report["purgeable_models"] == [
        {"model_id": LEGACY, "rows": 5}, {"model_id": OLD_A, "rows": 2}, {"model_id": OLD_B, "rows": 4},
    ]  # fmt: skip
    assert report["total_rows"] == 11
    assert report["largest_model"] == {"model_id": LEGACY, "rows": 5}
    assert report["thresholds"]["min_idle_days"] == 30
    assert report["manifest"]["sha256"] == hashlib.sha256(space.manifest.read_bytes()).hexdigest()
    (connection,) = space.database.connections
    assert connection.readonly is True and connection.session["isolation_level"] == "READ COMMITTED"
    assert connection.commits == 0 and connection.closed
    sent = [_flat(statement) for statement, _parameters in space.database.statements]
    assert sent == [SERVER_VERSION_SQL, _flat(CLASSIFY_SQL)]
    assert not [statement for statement in sent if "DELETE" in statement.upper()]
    assert space.database.statements[1][1] == (30, None, None, ["dg_current", "dg_manifest"])
    # No file, no directory: not the receipt root, not the lock file.
    assert space.files_written() == []
    assert not space.receipt_root.exists() and not Path(f"{space.env_file}.weight-purge-lock").exists()
    assert space.database.weights == before
    assert "DRY-RUN" in space.err
    space.assert_no_secret_anywhere()


def test_classification_reads_only_the_four_small_tables() -> None:
    relations = set(re.findall(r"(?:FROM|JOIN)\s+([a-z_]+\.[a-z_]+)", tool.CLASSIFY_SQL))
    assert relations == {"met.interp_weight", "hydro.hydro_run", "met.forcing_version", "core.model_instance"}
    assert "now() - make_interval(days => %s)" in tool.CLASSIFY_SQL
    assert "timeseries" not in tool.CLASSIFY_SQL
    assert tool.WEIGHT_COLUMNS == COLUMNS
    assert tool.CLASSES == ("current", "in_manifest", "recent_run", "recent_forcing", "recent_weights",
                            "recently_created", "purgeable")  # fmt: skip


def _case_current_for_one_source(db: FakeDatabase) -> None:
    db.add_run("m", "run_ifs_m", source_id="IFS")  # the only IFS run; gfs belongs to another model
    db.add_run("other", "run_gfs_other", cycle_time=OLD + timedelta(days=3))


def _case_superseded_by_a_newer_run(db: FakeDatabase) -> None:
    db.add_run("m", "run_m")
    db.add_run("other", "run_other", cycle_time=OLD + timedelta(days=3))


CASES: tuple[tuple[str, Callable[[FakeDatabase], None], str], ...] = (
    ("current for one source only", _case_current_for_one_source, "current"),
    ("legacy model with an old outranked run", _case_superseded_by_a_newer_run, "purgeable"),
    ("no run at all", lambda db: None, "purgeable"),
    ("only a failed run", lambda db: db.add_run("m", "r", status="failed", cycle_time=INSIDE), "recent_run"),
    ("only a hindcast run", lambda db: db.add_run("m", "r", run_type="hindcast", start_time=INSIDE), "recent_run"),
    ("only updated_at inside", lambda db: db.add_run("m", "r", status="superseded", updated_at=INSIDE), "recent_run"),
    ("an old displayable run without cycle_time", lambda db: db.add_run("m", "r", cycle_time=None), "purgeable"),
    ("forcing inside, no run", lambda db: db.forcing.append({"model_id": "m", "created_at": INSIDE}),
     "recent_forcing"),
    ("old forcing only", lambda db: db.forcing.append({"model_id": "m", "created_at": OLD}), "purgeable"),
    ("weights created yesterday", lambda db: db.insert_weight("m", "gfs", "gfs_0p25", created_at=INSIDE),
     "recent_weights"),
    ("model created yesterday, no run", lambda db: db.models.update(m=INSIDE), "recently_created"),
)  # fmt: skip


@pytest.mark.parametrize(("change", "expected"), [case[1:] for case in CASES], ids=[case[0] for case in CASES])
def test_class_of_one_model(space: Space, change: Callable[[FakeDatabase], None], expected: str) -> None:
    database = space.database
    database.models, database.weights, database.runs, database.forcing = {}, [], [], []
    database.add_model("m", 2)
    database.add_model("other", 1)
    change(database)
    assert space.run() == 0
    report = space.report()
    assert report["classes"][expected]["models"] >= 1
    assert ("m" in [model["model_id"] for model in report["purgeable_models"]]) is (expected == "purgeable")
    # Classes are exclusive: exactly the two seeded models are counted.
    assert sum(counts["models"] for counts in report["classes"].values()) == 2
    if expected != "purgeable":
        assert space.run("--apply") == 0
        assert len(database.rows_of("m")) >= 2 and "copy:m" not in database.events


def _break_manifest(content: str | None) -> Callable[[Space], list[str]]:
    def change(space: Space) -> list[str]:
        if content is None:
            space.manifest.unlink()
        else:
            space.manifest.write_text(content, encoding="utf-8")
        return []

    return change


def _other_database(space: Space) -> list[str]:
    space.env_file.write_text("DATABASE_URL=postgresql://nhms_ingest_rw:other@127.0.0.1:55432/scratch\n")
    return []


def _quoted_database(space: Space) -> list[str]:
    space.env_file.write_text(f'DATABASE_URL="{DATABASE_URL}"\n')
    return []


REFUSALS: tuple[tuple[str, Callable[[Space], list[str]], str], ...] = (
    ("manifest missing", _break_manifest(None), "cannot be read"),
    ("manifest not JSON", _break_manifest("{not json"), "is not JSON"),
    ("manifest without models", _break_manifest('{"generated_at": "x"}'), "no models list"),
    ("manifest with an empty models list", _break_manifest('{"models": []}'), "no models list"),
    ("manifest row without model_id", _break_manifest('{"models": [{"model_id": "a"}, {"basin_id": "b"}]}'),
     "a row without a model_id (models[1])"),
    ("manifest row with an empty model_id", _break_manifest('{"models": [{"model_id": ""}]}'), "without a model_id"),
    ("another database", _other_database, "is not the one in"),
    ("quoted DATABASE_URL line", _quoted_database, "non-empty and unquoted"),
    ("window below the forcing retention", lambda space: ["--min-idle-days", "20"], "is below 21"),
    ("empty reason", lambda space: ["--reason", " "], "--reason must not be empty"),
    ("negative pause", lambda space: ["--pause-seconds", "-1"], "--pause-seconds"),
    ("zero cap", lambda space: ["--max-models", "0"], "--max-models"),
)  # fmt: skip


@pytest.mark.parametrize("mode", [(), ("--apply",)], ids=["dry-run", "apply"])
@pytest.mark.parametrize(("change", "message"), [case[1:] for case in REFUSALS], ids=[case[0] for case in REFUSALS])
def test_refused_before_the_database_is_opened(
    space: Space, change: Callable[[Space], list[str]], message: str, mode: tuple[str, ...]
) -> None:
    extra = change(space)
    assert space.run(*extra, *mode) == 1
    assert message in space.err and "Nothing was written." in space.err
    assert space.out == ""
    assert space.database.connections == []
    assert space.files_written() == [] and not space.receipt_root.exists()
    space.assert_no_secret_anywhere()


def test_apply_purges_every_purgeable_model_under_the_writers_locks(space: Space) -> None:
    database = space.database
    expected = {model: csv_bytes(database.rows_of(model)) for model in (LEGACY, OLD_A, OLD_B)}
    untouched = [dict(row) for row in database.weights if row["model_id"] in PROTECTED]
    assert space.run("--apply") == 0

    # Per model: lock_timeout, the writers' locks in pair order, the rule again, the one COPY, the commit.
    unit = "\x1f"
    assert database.events == [
        "classify:None",
        "lock_timeout",
        f"lock:met.interp_weight:IFS{unit}ifs_0p25{unit}{LEGACY}",  # as stored: not lowercased
        f"lock:met.interp_weight:gfs{unit}gfs_0p25{unit}{LEGACY}",
        f"classify:{LEGACY}", f"copy:{LEGACY}", "commit",
        "lock_timeout", f"lock:met.interp_weight:gfs{unit}gfs_0p25{unit}{OLD_A}",
        f"classify:{OLD_A}", f"copy:{OLD_A}", "commit",
        "lock_timeout", f"lock:met.interp_weight:gfs{unit}gfs_0p25{unit}{OLD_B}",
        f"classify:{OLD_B}", f"copy:{OLD_B}", "commit",
    ]  # fmt: skip
    (connection,) = database.connections
    assert connection.readonly is False and connection.session["isolation_level"] == "READ COMMITTED"
    assert connection.commits == 3 and connection.closed
    copies = [statement for statement, parameters in database.statements if parameters is None]
    assert copies[0] == (
        f"COPY (DELETE FROM met.interp_weight WHERE model_id = '{LEGACY}' AND (source_id, grid_id) IN "
        f"(('IFS', 'ifs_0p25'), ('gfs', 'gfs_0p25')) RETURNING {', '.join(COLUMNS)}) TO STDOUT WITH CSV HEADER"
    )
    assert len(copies) == 3

    assert [row for row in database.weights if row["model_id"] in (LEGACY, OLD_A, OLD_B)] == []
    assert database.weights == untouched

    directory = space.run_directory()
    for index, (model_id, rows) in enumerate(((LEGACY, 5), (OLD_A, 2), (OLD_B, 4)), start=1):
        backup = directory / f"weights-{model_id}.csv"
        assert backup.read_bytes() == expected[model_id]
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
        assert space.receipt(f"model-{index:04d}.json") == {
            "model_id": model_id,
            "status": "purged",
            "rows": rows,
            "backup": str(backup),
            "sha256": hashlib.sha256(expected[model_id]).hexdigest(),
            "class": "purgeable",
        }
    summary = space.receipt("purge-receipt.json")
    assert summary["schema_version"] == "nhms.weight_purge.receipt.v1"
    assert (summary["operator_id"], summary["reason"]) == ("ops-test", "owner decision 2026-10-07")
    assert summary["thresholds"] == {"min_idle_days": 30, "pause_seconds": 2.0, "max_models": None}
    assert summary["manifest_at_start"]["sha256"] == hashlib.sha256(space.manifest.read_bytes()).hexdigest()
    assert summary["classes_at_start"]["purgeable"] == {"models": 3, "rows": 11}
    assert summary["classes_at_start"]["current"] == {"models": 1, "rows": 3}
    assert summary["totals"] == {"models_purged": 3, "rows_purged": 11, "models_skipped": 0}
    assert [model["model_id"] for model in summary["models"]] == [LEGACY, OLD_A, OLD_B]
    assert summary["models_not_reached"] == [] and summary["outcome"] == "completed"
    assert space.report()["receipt"] == str(directory / "purge-receipt.json")
    assert sorted(path.name for path in directory.iterdir()) == [
        "model-0001.json", "model-0002.json", "model-0003.json", "purge-receipt.json",
        f"weights-{LEGACY}.csv", f"weights-{OLD_A}.csv", f"weights-{OLD_B}.csv",
    ]  # fmt: skip
    # Between models, not after the last.
    assert space.sleeps == [2.0, 2.0]
    space.assert_no_secret_anywhere()

    # A rerun starts a new run directory and finds nothing left to purge.
    assert space.run("--apply", "--receipt-root", str(space.root / "second")) == 0
    assert space.report()["totals"] == {"models_purged": 0, "rows_purged": 0, "models_skipped": 0}


def test_max_models_stops_after_the_cap_and_names_the_rest(space: Space) -> None:
    assert space.run("--apply", "--max-models", "1", "--pause-seconds", "0.5") == 0
    summary = space.receipt("purge-receipt.json")
    assert [model["model_id"] for model in summary["models"]] == [LEGACY]
    assert summary["models_not_reached"] == [OLD_A, OLD_B]
    assert summary["thresholds"]["max_models"] == 1
    assert space.database.rows_of(LEGACY) == []
    assert len(space.database.rows_of(OLD_A)) == 2 and len(space.database.rows_of(OLD_B)) == 4
    assert space.sleeps == []
    assert not (space.run_directory() / "model-0002.json").exists()


def test_a_model_that_gains_a_run_before_its_transaction_is_skipped(space: Space) -> None:
    database = space.database

    def new_run(key: str) -> None:
        # A producer registered a run of the model while the tool was waiting for its lock.
        if key.endswith(OLD_A) and not any(run["run_id"] == "run_new" for run in database.runs):
            database.add_run(OLD_A, "run_new", status="running", cycle_time=None, created_at=NOW)

    database.on_lock = new_run
    assert space.run("--apply") == 0
    assert space.receipt("model-0002.json") == {
        "model_id": OLD_A, "status": "skipped", "rows": 2, "backup": None, "sha256": None, "class": "recent_run",
    }  # fmt: skip
    assert len(database.rows_of(OLD_A)) == 2
    assert f"copy:{OLD_A}" not in database.events
    assert not (space.run_directory() / f"weights-{OLD_A}.csv").exists()
    # The run continues.
    assert database.rows_of(OLD_B) == [] and space.receipt("model-0003.json")["status"] == "purged"
    assert space.receipt("purge-receipt.json")["totals"] == {
        "models_purged": 2, "rows_purged": 9, "models_skipped": 1,
    }  # fmt: skip


def test_a_model_the_manifest_gains_before_its_transaction_is_skipped(space: Space) -> None:
    # node-22 rewrites the manifest during the first pause.
    space.on_sleep = lambda: space.write_manifest(["dg_current", "dg_manifest", OLD_A])
    assert space.run("--apply") == 0
    assert space.receipt("model-0002.json")["class"] == "in_manifest"
    assert space.receipt("model-0002.json")["status"] == "skipped"
    assert len(space.database.rows_of(OLD_A)) == 2 and space.database.rows_of(OLD_B) == []
    recheck = [p for s, p in space.database.statements if _flat(s) == _flat(CLASSIFY_SQL) and p[1] == OLD_A]
    assert recheck == [(30, OLD_A, OLD_A, ["dg_current", "dg_manifest", OLD_A])]


def test_a_manifest_that_turns_malformed_stops_the_run_before_the_next_model(space: Space) -> None:
    space.on_sleep = lambda: space.manifest.write_text('{"models": [{"basin_id": "x"}]}', encoding="utf-8")
    assert space.run("--apply") == 1
    failed = space.receipt("purge-failed.json")
    assert failed["failed_model"]["model_id"] == OLD_A and "without a model_id" in failed["failed_model"]["error"]
    assert [model["model_id"] for model in failed["models"]] == [LEGACY]
    assert failed["models_not_attempted"] == [OLD_B]
    assert len(space.database.rows_of(OLD_A)) == 2 and len(space.database.rows_of(OLD_B)) == 4
    assert "lock_timeout" not in space.database.events[space.database.events.index("commit") + 1 :]


def test_rows_left_after_the_delete_roll_the_model_back_and_stop_the_run(space: Space) -> None:
    database = space.database

    def writer_slips_in(model_id: str) -> None:
        if model_id == OLD_A:
            database.insert_weight(OLD_A, "IFS", "ifs_0p25")  # a scope the tool did not lock

    database.after_copy = writer_slips_in
    assert space.run("--apply") == 1
    directory = space.run_directory()
    failed = space.receipt("purge-failed.json")
    assert failed["schema_version"] == "nhms.weight_purge.failure.v1" and failed["outcome"] == "failed"
    assert failed["failed_model"]["model_id"] == OLD_A
    assert failed["failed_model"]["outcome"] == "rolled_back" and failed["failed_model"]["backup_kept"] is None
    assert "1 met.interp_weight rows" in failed["failed_model"]["error"]
    assert [model["model_id"] for model in failed["models"]] == [LEGACY]
    assert failed["models_not_attempted"] == [OLD_B]
    # Rolled back: the two rows are back, plus the writer's; the backup is gone; the next model was not touched.
    assert len(database.rows_of(OLD_A)) == 3 and len(database.rows_of(OLD_B)) == 4
    assert not (directory / f"weights-{OLD_A}.csv").exists()
    assert (directory / f"weights-{LEGACY}.csv").exists() and database.rows_of(LEGACY) == []
    assert not (directory / "purge-receipt.json").exists() and not (directory / "model-0002.json").exists()
    assert f"classify:{OLD_B}" not in database.events
    assert "FAILED" in space.err and space.report()["failure_receipt"] == str(directory / "purge-failed.json")
    space.assert_no_secret_anywhere()


def test_lock_timeout_on_the_third_model_leaves_the_first_two_purged(space: Space) -> None:
    database = space.database

    def held(key: str) -> None:
        if key.endswith(OLD_B):
            raise psycopg2.errors.LockNotAvailable(f"canceling statement due to lock timeout ({DATABASE_URL})")

    database.on_lock = held
    assert space.run("--apply") == 1
    directory = space.run_directory()
    assert space.receipt("model-0001.json")["status"] == "purged"
    assert space.receipt("model-0002.json")["status"] == "purged"
    assert database.rows_of(LEGACY) == [] and database.rows_of(OLD_A) == []
    assert len(database.rows_of(OLD_B)) == 4
    failed = space.receipt("purge-failed.json")
    assert failed["failed_model"]["model_id"] == OLD_B and failed["models_not_attempted"] == []
    assert "LockNotAvailable" in failed["failed_model"]["error"] and "lock timeout" in failed["failed_model"]["error"]
    assert failed["totals"] == {"models_purged": 2, "rows_purged": 7, "models_skipped": 0}
    assert not (directory / f"weights-{OLD_B}.csv").exists() and not (directory / "model-0003.json").exists()
    assert f"copy:{OLD_B}" not in database.events
    # Whatever the driver put in its message, the connection string is not in a receipt or on a stream.
    space.assert_no_secret_anywhere()


def test_a_model_id_that_cannot_be_rendered_fails_without_a_statement(space: Space) -> None:
    bad = "bad id'; DROP TABLE met.interp_weight; --"
    space.database.add_model(bad, 2)  # sorts before every other purgeable model
    assert space.run() == 0
    assert space.report()["model_ids_an_apply_cannot_render"] == [bad]
    space.database.statements.clear()
    assert space.run("--apply") == 1
    failed = space.receipt("purge-failed.json")
    assert failed["failed_model"]["model_id"] == bad and "does not match" in failed["failed_model"]["error"]
    assert failed["models"] == [] and failed["models_not_attempted"] == [LEGACY, OLD_A, OLD_B]
    # Only the classification of the whole table was sent: nothing for this model, nothing after it.
    assert [_flat(s) for s, _p in space.database.statements] == [SERVER_VERSION_SQL, _flat(CLASSIFY_SQL)]
    assert len(space.database.rows_of(bad)) == 2 and len(space.database.rows_of(LEGACY)) == 5
    assert [path.name for path in space.run_directory().iterdir()] == ["purge-failed.json"]


@pytest.mark.parametrize("model_id", ["", "a" * 129, "dg_a b", "dg_a'b", "dg/a", "dg_a\n", "模型"])
def test_model_ids_outside_the_pattern_are_not_rendered(model_id: str) -> None:
    assert not tool.MODEL_ID_PATTERN.fullmatch(model_id)
    assert tool.MODEL_ID_PATTERN.fullmatch("dg_09fce53ac1464cc52f57e1900d09d89b")
    assert tool.MODEL_ID_PATTERN.fullmatch("basins_lh_gl_shud") and tool.MODEL_ID_PATTERN.fullmatch("A.b-c_" * 21)


def test_a_second_instance_is_refused_while_the_lock_is_held(space: Space) -> None:
    descriptor = os.open(f"{space.env_file}.weight-purge-lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert space.run("--apply") == 1
        assert "another instance" in space.err and "Nothing was written." in space.err
        assert space.database.connections == [] and not space.receipt_root.exists()
        # A dry-run takes no lock and is not held up by one.
        assert space.run() == 0
    finally:
        os.close(descriptor)
    assert space.run("--apply") == 0


def test_a_lock_that_cannot_be_taken_is_refused(space: Space, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_descriptor: int, _operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(tool.fcntl, "flock", broken)
    assert space.run("--apply") == 1
    assert "cannot take the lock" in space.err and "another instance" not in space.err
    assert space.database.connections == [] and not space.receipt_root.exists()


def test_a_commit_that_raises_keeps_the_backup_and_reports_the_outcome_as_unknown(space: Space) -> None:
    database = space.database
    expected = csv_bytes(database.rows_of(LEGACY))
    database.commit_error = psycopg2.OperationalError("server closed the connection unexpectedly")
    assert space.run("--apply") == 1
    directory = space.run_directory()
    backup = directory / f"weights-{LEGACY}.csv"
    assert backup.read_bytes() == expected
    failed = space.receipt("purge-failed.json")
    assert failed["failed_model"]["model_id"] == LEGACY
    assert failed["failed_model"]["outcome"] == "unknown" and failed["failed_model"]["backup_kept"] == str(backup)
    assert "is not known" in failed["failed_model"]["error"]
    assert failed["models"] == [] and failed["models_not_attempted"] == [OLD_A, OLD_B]
    assert sorted(path.name for path in directory.iterdir()) == ["purge-failed.json", backup.name]
    assert f"classify:{OLD_A}" not in database.events


def test_an_existing_run_directory_is_refused_and_left_alone(space: Space, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool, "utc_stamp", lambda: "20261007T120000Z")
    directory = space.receipt_root / "purge-20261007T120000Z"
    directory.mkdir(parents=True)
    assert space.run("--apply") == 1
    assert "cannot be created" in space.err and "Nothing was written." in space.err
    assert list(directory.iterdir()) == [] and len(space.database.rows_of(LEGACY)) == 5


def test_a_connection_error_does_not_leak_the_connection_string(space: Space) -> None:
    space.database.connect_error = psycopg2.OperationalError(f'connection to "{DATABASE_URL}" failed: {PASSWORD}')
    for mode in ((), ("--apply",)):
        assert space.run(*mode) == 1
        assert "The database refused (connecting)" in space.err and "<redacted>" in space.err
        space.assert_no_secret_anywhere()
    assert not space.receipt_root.exists()


def test_the_lock_key_is_the_one_both_weight_writers_take(monkeypatch: pytest.MonkeyPatch) -> None:
    source_id, grid_id, model_id = "IFS", "ifs_0p25", "dg_09fce53ac1464cc52f57e1900d09d89b"
    expected = "met.interp_weight:IFS\x1fifs_0p25\x1fdg_09fce53ac1464cc52f57e1900d09d89b"
    sent: dict[str, tuple[str, Any]] = {}

    class Recorder:
        def execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
            sent["handoff"] = (_flat(statement), parameters)

    forcing_domain_handoff_apply._lock_interp_weight_scope(Recorder(), source_id, grid_id, model_id)

    def replace_values(_self: Any, lock_statement: str, lock_parameters: tuple[Any, ...], *_rest: Any) -> None:
        sent["producer"] = (_flat(lock_statement), lock_parameters)

    monkeypatch.setattr(PsycopgForcingRepository, "_replace_values", replace_values)
    # One weight as the producer hands it over; the store reads attributes only.
    weight = SimpleNamespace(
        source_id=source_id, grid_id=grid_id, model_id=model_id, station_id="station_1", variable="PRCP",
        grid_cell_id="cell_1", weight=1.0, method="idw", grid_signature=None,
    )  # fmt: skip
    PsycopgForcingRepository("postgresql://unused").upsert_interp_weights([weight])
    assert sent["handoff"] == sent["producer"] == (ADVISORY_LOCK_SQL, (expected,))
    assert (tool.ADVISORY_LOCK_SQL, (tool.lock_key(source_id, grid_id, model_id),)) == sent["producer"]
