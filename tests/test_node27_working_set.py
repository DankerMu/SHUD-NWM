"""Exercise CLI projection using only PostgreSQL/OS boundary substitutes."""

import errno
import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import psycopg2
import psycopg2.errors
import pytest

from packages.common import node27_resource_governance_collection as collection
from packages.common.display_watermark import DISPLAY_WATERMARK_SQL
from scripts import node27_resource_governance as governance

GIB = 1024**3
RUNWAY = "PGDATA_INGEST_RUNWAY_BELOW_CRITICAL"
RETIRED = "PROJECTED_PEAK_EXCEEDS_WORKING_SET_FREE"
_SCHEMAS = {
    "river_timeseries": "hydro",
    "river_timeseries_legacy": "hydro",
    "forcing_station_timeseries": "met",
    "forcing_station_timeseries_legacy": "met",
}
_DAY0 = datetime(2026, 11, 18, tzinfo=UTC)


def _gib(value):
    """Integer bytes, as `pg_total_relation_size` returns and statvfs reports."""
    return round(value * GIB)


def _chunk(name, gib, *, day=0, days=1, start=None, end=None):
    """One row of the uncompressed-chunk statement, sized in GiB."""
    start = _DAY0 + timedelta(days=day) if start is None else start
    return {
        "hypertable_schema": _SCHEMAS[name],
        "hypertable_name": name,
        "range_start": start,
        "range_end": start + timedelta(days=days) if end is None else end,
        "total_bytes": _gib(gib),
    }


# Default rows, consistent with the fake aggregate's 600 GiB: eleven 50 GiB
# river days and ten 5 GiB forcing days, an arrival of 55 GiB/day.
DEFAULT_ROWS = [_chunk("river_timeseries", 50, day=d) for d in range(11)] + [
    _chunk("forcing_station_timeseries", 5, day=d) for d in range(10)
]
DEFAULT_RATE = 55 * GIB

# Spec scenario "Arrival is the largest uncompressed chunk of each hypertable"
# (2026-11-25): four ended river days at 41.8 GiB, today's 37.7, forecast-horizon
# days smaller; forcing at most 2.6 GiB. Arrival 41.8 + 2.6 = 44.4 GiB/day.
SCENARIO_ROWS = (
    [_chunk("river_timeseries", 41.8, day=d) for d in range(4)]
    + [_chunk("river_timeseries", 37.7, day=4), _chunk("river_timeseries", 12.0, day=5)]
    + [_chunk("river_timeseries", 3.5, day=6)]
    + [_chunk("forcing_station_timeseries", 2.6, day=d) for d in range(4)]
    + [_chunk("forcing_station_timeseries", 2.4, day=4), _chunk("forcing_station_timeseries", 0.9, day=5)]
)
SCENARIO_RATE = _gib(41.8) + _gib(2.6)


@pytest.fixture(autouse=True)
def _retired_code_is_never_emitted(monkeypatch):
    """Spec "The retired code is not emitted": every state this module builds goes through here."""
    real = governance._recommendations

    def checked(receipt, thresholds):
        recommendations = real(receipt, thresholds)
        assert RETIRED not in {item.get("code") for item in recommendations}
        return recommendations

    monkeypatch.setattr(governance, "_recommendations", checked)


def _argv(tmp_path, path, extra=()):
    return [
        "--database-url",
        "postgresql://user:secret@example/db",
        "--repo-root",
        str(tmp_path / "repo"),
        "--object-store-root",
        str(tmp_path / "objects"),
        "--pgdata-root",
        str(tmp_path),
        "--summary-path",
        str(path),
        "--quiet",
        *extra,
    ]


@pytest.fixture
def observations(monkeypatch, tmp_path):
    state = {
        "uncompressed": 600 * GIB,
        "daily": 75 * GIB,
        "home": 900 * GIB,
        "oldest": datetime(2026, 9, 1, tzinfo=UTC),
        "watermark": datetime(2026, 9, 1, tzinfo=UTC),
        # None: DEFAULT_ROWS while the aggregate saw uncompressed bytes, else no row.
        "rows": None,
        "rows_failure": None,
        "queries": [],
        "connections": [],
        "target_path": str(tmp_path.resolve()),
        "target_failure": None,
        "target_samples": 0,
    }

    class Cursor:
        def __init__(self, log):
            self.log = log

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params=None):
            self.sql = sql
            state["queries"].append(sql)
            self.log.append(sql)
            if sql == collection.UNCOMPRESSED_CHUNKS_SQL and state["rows_failure"] is not None:
                raise state["rows_failure"]

        def fetchone(self):
            if "oldest_uncompressed_range_end" in self.sql:
                return {
                    "uncompressed_bytes": state["uncompressed"],
                    "daily_ingest_bytes": state["daily"],
                    "oldest_uncompressed_range_end": state["oldest"],
                }
            return (state["watermark"],)

        def fetchall(self):
            if "FROM pg_database" in self.sql:
                return [{"datname": "nhms", "bytes": 1000 * GIB}]
            if self.sql == collection.UNCOMPRESSED_CHUNKS_SQL:
                if state["rows"] is not None:
                    return [dict(row) for row in state["rows"]]
                return [dict(row) for row in DEFAULT_ROWS] if state["uncompressed"] else []
            return []

    class Connection:
        def __init__(self, kwargs):
            self.log = []
            state["connections"].append({"kwargs": kwargs, "queries": self.log})

        def cursor(self):
            return Cursor(self.log)

        def set_session(self, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(psycopg2, "connect", lambda *args, **kwargs: Connection(kwargs))
    real_stat = Path.stat
    directory_stat = tmp_path.stat()

    def aliases(*paths):
        # Production resolves symlinks (macOS /home -> /System/Volumes/Data/home);
        # evaluated per call because tests rewrite target_path after setup.
        # realpath is resolve(strict=False) without its loop-check Path.stat(),
        # which would recurse into this fake.
        return {form for p in paths for form in (p, os.path.realpath(p))}

    def stat(path, *args, **kwargs):
        # Keep lstat identity real for no-follow receipt publication.
        if not kwargs.get("follow_symlinks", True):
            return real_stat(path, *args, **kwargs)
        if str(path) in aliases("/", "/home", state["target_path"]):
            if str(path) in aliases(state["target_path"]) and state["target_failure"] == "missing":
                raise FileNotFoundError(errno.ENOENT, "password=os-secret", str(path))
            return directory_stat
        return real_stat(path, *args, **kwargs)

    def statvfs(path):
        is_target = str(path) in aliases(state["target_path"])
        identity = 1
        if is_target:
            state["target_samples"] += 1
            if state["target_failure"] == "statvfs":
                raise OSError("password=os-secret postgresql://user:driver-secret@example/db")
            if state["target_failure"] == "identity":
                identity = None
            if state["target_failure"] == "conflict" and state["target_samples"] > 1:
                identity = 2
        free = state.get("target", state["home"]) if is_target else state["home"]
        return SimpleNamespace(f_blocks=2000 * GIB, f_bfree=free + 10 * GIB, f_bavail=free, f_frsize=1, f_fsid=identity)

    def run(args, **kwargs):
        if args[0] == "du" and args[-1] == state["target_path"] and state["target_failure"] == "du":
            return SimpleNamespace(returncode=1, stdout="", stderr="password=du-secret")
        return SimpleNamespace(returncode=0, stdout="0\tunused\n", stderr="")

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(collection.os, "statvfs", statvfs)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS", "172800")
    return state


@pytest.mark.parametrize(
    ("home", "flags", "margin", "expected_exit", "expected_code"),
    [
        (900, [], 100, 0, None),
        # The retired rule fired here (projected peak 750 GiB > 800 - 100 GiB);
        # 700 GiB above the margin at 55 GiB/day is 12.7 days of runway.
        (800, [], 100, 0, None),
        (900, ["--safety-margin-bytes", str(760 * GIB)], 760, 1, RUNWAY),
        (900, ["--runway-critical-days", "15"], 100, 1, RUNWAY),
        (900, ["--runway-critical-days", "14"], 100, 0, None),
        (900, ["--working-set-warn-bytes", str(500 * GIB)], 100, 0, "WORKING_SET_ABOVE_WARNING"),
    ],
    ids=("fits", "retired-rule-state-fits", "margin-override", "days-override-fires", "days-override-fits", "warn"),
)
def test_cli_projection_and_threshold_overrides(
    observations, tmp_path, capsys, home, flags, margin, expected_exit, expected_code
):
    """Now: the runway comparison against the default rows (55 GiB/day), its two
    overrides, and that the projection is still computed and stored as before."""
    observations["home"] = home * GIB
    path = tmp_path / "audit.json"
    rc = governance.main(_argv(tmp_path, path, flags))
    receipt = json.loads(path.read_text())
    stderr = capsys.readouterr().err
    assert rc == expected_exit
    assert receipt["status"] == "completed"
    working = receipt["working_set"]
    assert working["projected_peak_bytes"] == 750 * GIB
    assert working["next_compressible_at"] == "2026-09-03T00:00:00+00:00"
    assert working["daily_arrival_bytes"] == DEFAULT_RATE
    assert working["ingest_runway_days"] == pytest.approx((home - margin) / 55)
    recommendations = {row["code"]: row["severity"] for row in receipt["recommendations"]}
    assert recommendations["DATABASE_SIZE_ABOVE_CRITICAL"] == "info"
    assert "DATABASE_SIZE_ABOVE_CRITICAL" not in governance._critical_codes(receipt)
    assert "RESOURCE_GOVERNANCE_CRITICAL:DATABASE_SIZE" not in stderr
    if expected_code:
        assert expected_code in recommendations
    assert governance._critical_codes(receipt) == ([RUNWAY] if expected_exit else [])
    if expected_exit:
        assert "RESOURCE_GOVERNANCE_CRITICAL:" + RUNWAY in stderr
        for field in (
            "ingest_runway_days",
            "daily_arrival_bytes",
            "working_set_free_bytes",
            "safety_margin_bytes",
            "uncompressed_bytes",
        ):
            assert field + "=" in stderr
        assert f"safety_margin_bytes={margin * GIB}" in stderr
    else:
        assert "RESOURCE_GOVERNANCE_CRITICAL:" not in stderr
    assert "secret" not in path.read_text()
    assert "postgresql://" not in path.read_text()


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf", "-inf", "three"])
def test_runway_critical_days_refuses_non_positive_or_non_finite(observations, tmp_path, capsys, raw):
    path = tmp_path / "audit.json"
    with pytest.raises(SystemExit) as error:
        governance.main(_argv(tmp_path, path, ["--runway-critical-days", raw]))
    assert error.value.code == 2
    assert "runway-critical-days" in capsys.readouterr().err
    assert not path.exists()


def test_runway_critical_days_defaults_to_three_and_accepts_fractions():
    assert governance.AuditThresholds().runway_critical_days == 3.0
    assert governance.config_from_args(governance.build_parser().parse_args([])).thresholds.runway_critical_days == 3
    args = governance.build_parser().parse_args(["--runway-critical-days", "2.5"])
    assert governance.config_from_args(args).thresholds.runway_critical_days == 2.5


@pytest.mark.parametrize("empty", [True, False])
def test_empty_set_and_missing_watermark(observations, tmp_path, capsys, empty):
    observations["watermark"] = None
    if empty:
        observations.update(uncompressed=0, oldest=None)
    path = tmp_path / "audit.json"
    rc = governance.main(_argv(tmp_path, path))
    receipt = json.loads(path.read_text())
    working = receipt["working_set"]
    assert rc == (0 if empty else 1)
    assert working["projection_status"] == ("no_uncompressed_chunk" if empty else "watermark_unavailable")
    if empty:
        assert working["next_compressible_at"] is None
        assert working["projected_peak_bytes"] == 0
        assert working["daily_arrival_bytes"] == 0.0
        assert working["ingest_runway_days"] is None
        assert governance._critical_codes(receipt) == []
    else:
        assert working["daily_arrival_bytes"] == DEFAULT_RATE
        assert working["ingest_runway_days"] == pytest.approx(800 / 55)
        assert governance._critical_codes(receipt) == ["WATERMARK_UNAVAILABLE"]
        assert "RESOURCE_GOVERNANCE_CRITICAL:WATERMARK_UNAVAILABLE" in capsys.readouterr().err


_CATALOG_SOURCES = {"timescaledb_information.chunks", "timescaledb_information.hypertables"}


def test_working_set_query_is_catalog_only(observations):
    """Spec "No row scan", extended (#3379) from the one statement found by a
    marker to every statement the working-set collection executes: the catalog
    connection runs exactly the timeout, the aggregate and the row statement,
    each catalog-only; the only other statement is the watermark's own read."""
    collection.collect_working_set("postgresql://unused", {})
    catalog = [c for c in observations["connections"] if "cursor_factory" in c["kwargs"]]
    others = [c for c in observations["connections"] if "cursor_factory" not in c["kwargs"]]
    assert len(catalog) == 1
    assert catalog[0]["queries"] == [
        "SET LOCAL statement_timeout = '20s'",
        collection.WORKING_SET_SQL,
        collection.UNCOMPRESSED_CHUNKS_SQL,
    ]
    assert [c["queries"] for c in others] == [["SET LOCAL statement_timeout = '5s'", DISPLAY_WATERMARK_SQL]]
    assert observations["queries"] == catalog[0]["queries"] + others[0]["queries"]
    for sql in catalog[0]["queries"][1:]:
        assert "FROM timescaledb_information.chunks" in sql
        assert "FROM timescaledb_information.hypertables" in sql
        assert "pg_total_relation_size" in sql
        assert set(re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w.]*)", sql)) == _CATALOG_SOURCES
        for forbidden in ("FROM hydro.", "FROM met.", "pg_class", "pg_tables", "_timescaledb_catalog"):
            assert forbidden not in sql
    assert "range_start >= CURRENT_TIMESTAMP - interval '7 days'" in collection.WORKING_SET_SQL
    assert "WHERE NOT is_compressed" in collection.UNCOMPRESSED_CHUNKS_SQL
    assert "sum(" not in collection.UNCOMPRESSED_CHUNKS_SQL.lower()
    assert "max(" not in collection.UNCOMPRESSED_CHUNKS_SQL.lower()


def test_missing_compression_lag_does_not_report_working_set_unavailable(observations, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS", raising=False)
    path = tmp_path / "audit.json"
    rc = governance.main(_argv(tmp_path, path))
    receipt = json.loads(path.read_text())
    working = receipt["working_set"]
    assert working["projection_status"] == "ok"
    assert working["next_compressible_at"] == "2026-09-03T00:00:00+00:00"
    assert "WORKING_SET_UNAVAILABLE" not in governance._critical_codes(receipt)
    assert rc == 0
    assert "RESOURCE_GOVERNANCE_CRITICAL:" not in capsys.readouterr().err


def test_catalog_connect_failure_reports_working_set_unavailable(observations, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        psycopg2,
        "connect",
        lambda *args, **kwargs: (_ for _ in ()).throw(psycopg2.OperationalError("secret password=db-secret")),
    )
    path = tmp_path / "audit.json"
    rc = governance.main(_argv(tmp_path, path))
    receipt = json.loads(path.read_text())
    assert receipt["working_set"]["projection_status"] == "catalog_unavailable"
    assert _catalog_fields(receipt["working_set"]) == dict.fromkeys(_CATALOG_FIELDS)
    assert governance._critical_codes(receipt) == ["WORKING_SET_UNAVAILABLE"]
    assert rc == 1
    assert "RESOURCE_GOVERNANCE_CRITICAL:WORKING_SET_UNAVAILABLE" in capsys.readouterr().err
    assert "db-secret" not in path.read_text()


_CATALOG_FIELDS = (
    "uncompressed_bytes",
    "daily_ingest_bytes",
    "daily_arrival_bytes",
    "next_compressible_at",
    "watermark",
    "projected_peak_bytes",
    "ingest_runway_days",
)


def _catalog_fields(working):
    return {name: working[name] for name in _CATALOG_FIELDS}


@pytest.mark.parametrize(
    ("home", "target", "expected_exit"),
    [(200, 900, 0), (900, 260, 1), (900, 265, 0)],
    ids=("destination-fits-home-does-not", "destination-short-home-fits", "equality-fits"),
)
def test_cli_destination_capacity_not_home(observations, tmp_path, capsys, home, target, expected_exit):
    """Now: the runway is judged on the configured PGDATA filesystem, never on
    /home (at 55 GiB/day the firing line is 100 + 3 x 55 = 265 GiB free)."""
    observations.update(home=home * GIB, target=target * GIB)
    path = tmp_path / "audit.json"
    rc = governance.main(_argv(tmp_path, path))
    receipt = json.loads(path.read_text())
    codes = governance._critical_codes(receipt)
    assert rc == expected_exit
    assert codes == ([RUNWAY] if expected_exit else [])
    working = receipt["working_set"]
    assert working["projected_peak_bytes"] == 750 * GIB
    assert working["working_set_free_bytes"] == target * GIB
    assert working["ingest_runway_days"] == pytest.approx((target - 100) / 55)
    assert "home_free_bytes" not in working
    binding = working["working_set_filesystem"]
    assert binding["path"] == str(tmp_path.resolve())
    assert binding["status"] == "ok"
    assert binding["blockers"] == []
    stderr = capsys.readouterr().err
    if expected_exit:
        assert "RESOURCE_GOVERNANCE_CRITICAL:" + RUNWAY in stderr
        assert f"working_set_free_bytes={260 * GIB}" in stderr
        assert f"daily_arrival_bytes={float(DEFAULT_RATE)}" in stderr
        assert f"ingest_runway_days={working['ingest_runway_days']}" in stderr
        assert binding["path"] in stderr
        assert binding["device_identity"] in stderr


@pytest.mark.parametrize("empty", [False, True], ids=("populated", "empty"))
@pytest.mark.parametrize("failure", ["missing", "statvfs", "identity", "conflict", "du"])
def test_cli_missing_capacity_and_usage_fail_independently(observations, tmp_path, capsys, failure, empty):
    target = tmp_path / "pgdata-target"
    target.mkdir()
    observations.update(target_path=str(target.resolve()), target_failure=failure)
    if empty:
        observations.update(uncompressed=0, oldest=None)
    path = tmp_path / "audit.json"
    assert governance.main(_argv(tmp_path, path, ["--pgdata-root", str(target)])) == 1
    receipt = json.loads(path.read_text())
    working = receipt["working_set"]
    codes = governance._critical_codes(receipt)
    assert working["projection_status"] == ("no_uncompressed_chunk" if empty else "ok")
    binding = working["working_set_filesystem"]
    if failure == "du":
        assert codes == ["PGDATA_USAGE_UNAVAILABLE"]
        assert binding["status"] == "ok"
        assert working["working_set_free_bytes"] == 900 * GIB
    else:
        assert "WORKING_SET_FILESYSTEM_UNAVAILABLE" in codes
        assert binding["status"] == ("ambiguous" if failure == "conflict" else "unavailable")
        assert working["working_set_free_bytes"] is None
        assert binding["blockers"]
        if failure == "missing":
            assert "PGDATA_USAGE_UNAVAILABLE" in codes
    if failure == "du" and not empty:
        assert working["ingest_runway_days"] == pytest.approx(800 / 55)
    else:
        assert working["ingest_runway_days"] is None
    stderr = capsys.readouterr().err
    for code in codes:
        assert "RESOURCE_GOVERNANCE_CRITICAL:" + code in stderr
    # #3379: the filesystem criticals name what is missing (free bytes, usage
    # status) and the filesystem object, not the projection.
    usage_status = receipt["filesystem"]["path_sizes"]["pgdata_root"]["status"]
    capacity_line = f"working-set capacity: working_set_free_bytes={working['working_set_free_bytes']} "
    assert capacity_line + f"pgdata_usage_status={usage_status}" in stderr.splitlines()
    assert "working-set filesystem: " + json.dumps(binding, sort_keys=True) in stderr.splitlines()
    assert "projected_peak_bytes" not in stderr
    for secret in ("os-secret", "driver-secret", "du-secret"):
        assert secret not in path.read_text() + stderr


@pytest.mark.parametrize("target", ["/home/nwm/nhms-pgdata", "/home/nwm/nhms-primary/pgdata", "/srv/db-alias"])
def test_cli_configured_paths_and_duplicate_device_labels(observations, tmp_path, target):
    observations.update(target_path=target, target=900 * GIB, home=800 * GIB)
    path = tmp_path / "audit.json"
    assert governance.main(_argv(tmp_path, path, ["--pgdata-root", target])) == 0
    receipt = json.loads(path.read_text())
    binding = receipt["working_set"]["working_set_filesystem"]
    assert binding["path"] == str(Path(target).resolve(strict=False))
    assert binding["status"] == "ok"
    assert binding["blockers"] == []
    assert receipt["working_set"]["working_set_free_bytes"] == 900 * GIB
    assert receipt["filesystem"]["filesystems"]["home"]["device_identity"] == binding["device_identity"]


def test_cli_ordinary_receipt_ignores_retired_cold_environment(observations, tmp_path, monkeypatch):
    summary = tmp_path / "audit.json"
    cold = tmp_path / "cold.json"
    monkeypatch.setenv("NODE27_COLD_GOVERNANCE_RECEIPT_PATH", str(cold))
    assert governance.main(_argv(tmp_path, summary)) == 0
    receipt = json.loads(summary.read_text())
    assert receipt["schema_version"] == "nhms.node27_resource_governance.audit.v1"
    assert receipt["execution_mode"] == "read_only_audit"
    assert receipt["status"] == "completed"
    assert receipt["working_set"]["projected_peak_bytes"] == 750 * GIB
    assert receipt["working_set"]["working_set_free_bytes"] == 900 * GIB
    # #3059: the retired 14T cold volume is no longer sampled (it only re-measured `/`).
    assert "cold" not in receipt["filesystem"]["filesystems"]
    assert {"root", "home"} <= set(receipt["filesystem"]["filesystems"])
    assert receipt["postgres"]["maintenance_output"] == {"status": "ok", "summary": {}, "rows": []}
    assert "cold_tablespace_governance" not in receipt
    assert {"cold_tablespace", "cold_relation_by_tablespace", "external_pg_tblspc_targets"}.isdisjoint(
        receipt["postgres"]
    )
    assert not cold.exists()


def test_current_comparator_never_falls_back_to_historical_home():
    receipt = {
        "working_set": {
            "projection_status": "ok",
            "uncompressed_bytes": 600 * GIB,
            "daily_arrival_bytes": float(DEFAULT_RATE),
            "projected_peak_bytes": 750 * GIB,
            "home_free_bytes": 900 * GIB,
        },
        "filesystem": {"path_sizes": {"pgdata_root": {"status": "ok", "bytes": 0}}},
    }
    codes = {
        row["code"]
        for row in governance._recommendations(receipt, governance.AuditThresholds())
        if row["severity"] == "critical"
    }
    assert codes == {"WORKING_SET_FILESYSTEM_UNAVAILABLE"}


def test_capacity_stderr_survives_wrapper_and_onfailure_mail(observations, tmp_path, capsys, monkeypatch):
    """Spec "Alert body names the numbers that matter". Now: the runway
    critical's lines (230 GiB free, 44.4 GiB/day) reach the mail through the
    real wrapper and alert handler, lead with the runway code, not database size."""
    observations.update(target=_gib(230), rows=SCENARIO_ROWS, uncompressed=_gib(373.5))
    summary = tmp_path / "audit.json"
    assert governance.main(_argv(tmp_path, summary)) == 1
    diagnostic = capsys.readouterr().err
    working = json.loads(summary.read_text())["working_set"]
    binding = working["working_set_filesystem"]
    lines = diagnostic.splitlines()
    assert lines[0] == "RESOURCE_GOVERNANCE_CRITICAL:" + RUNWAY
    assert lines[1] == (
        f"ingest runway: ingest_runway_days={working['ingest_runway_days']} "
        f"daily_arrival_bytes={float(SCENARIO_RATE)} working_set_free_bytes={_gib(230)} "
        f"safety_margin_bytes={100 * GIB} uncompressed_bytes={_gib(373.5)}"
    )
    assert lines[2] == "working-set filesystem: " + json.dumps(binding, sort_keys=True)
    assert working["ingest_runway_days"] == pytest.approx(130 / 44.4, rel=1e-9)
    assert binding["path"] == str(tmp_path.resolve()) and binding["device_identity"]
    assert "DATABASE_SIZE" not in diagnostic
    assert "secret" not in diagnostic and "postgresql://" not in diagnostic
    monkeypatch.undo()
    captured = tmp_path / "cli-stderr"
    captured.write_text(diagnostic)
    repo = tmp_path / "wrapper-repo"
    python = repo / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\ncat "$CAPTURED_STDERR" >&2\nexit 1\n')
    python.chmod(0o700)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, body in {
        "flock": "exit 0",
        "journalctl": 'cat "$JOURNAL_FILE"',
        "sendmail": 'cat > "$MAIL_FILE"',
    }.items():
        executable = binaries / name
        executable.write_text("#!/bin/sh\n" + body + "\n")
        executable.chmod(0o700)
    env_file = tmp_path / "governance.env"
    env_file.write_text("DATABASE_URL=postgresql://unused/db\n")
    env_file.chmod(0o600)
    env = {
        **os.environ,
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "CAPTURED_STDERR": str(captured),
        "NODE27_RESOURCE_GOVERNANCE_REPO": str(repo),
        "NODE27_RESOURCE_GOVERNANCE_ENV_FILE": str(env_file),
        "NODE27_RESOURCE_GOVERNANCE_LOG_ROOT": str(tmp_path / "logs"),
        "NODE27_RESOURCE_GOVERNANCE_BOOTSTRAP_LOG": str(tmp_path / "bootstrap.log"),
    }
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    wrapped = subprocess.run(
        ["bash", str(scripts / "node27_resource_governance_once.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert wrapped.returncode == 1
    assert diagnostic in wrapped.stderr
    assert diagnostic in (tmp_path / "logs/resource-governance.log").read_text()
    journal = tmp_path / "journal"
    journal.write_text(wrapped.stderr)
    mail = tmp_path / "mail"
    env.update(
        {
            "JOURNAL_FILE": str(journal),
            "MAIL_FILE": str(mail),
            "NHMS_FRONTIER_SENDMAIL": str(binaries / "sendmail"),
            "NHMS_ALERT_EMAIL_TO": "ops@example.test",
            "NHMS_ALERT_EMAIL_FROM": "alerts@example.test",
        }
    )
    alerted = subprocess.run(
        ["bash", str(scripts / "node27_unit_failure_alert_once.sh"), "nhms-node27-resource-governance.service"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert alerted.returncode == 0
    assert diagnostic in mail.read_text()


def test_secret_bearing_target_evidence_is_redacted_in_summary_and_diagnostics(observations, tmp_path, capsys):
    target = tmp_path / "password=target-secret"
    target.mkdir()
    observations.update(target_path=str(target.resolve()), target=240 * GIB)
    summary = tmp_path / "audit.json"
    assert (
        governance.main(
            _argv(
                tmp_path,
                summary,
                [
                    "--pgdata-root",
                    str(target),
                ],
            )
        )
        == 1
    )
    stderr = capsys.readouterr().err
    # Now: the runway critical (140 GiB above the margin < 3 x 55 GiB) carries the path.
    assert stderr.splitlines()[0] == "RESOURCE_GOVERNANCE_CRITICAL:" + RUNWAY
    assert "working-set filesystem: " in stderr and "[redacted]" in stderr
    for text in (summary.read_text(), stderr):
        assert "target-secret" not in text
    binding = json.loads(summary.read_text())["working_set"]["working_set_filesystem"]
    assert "[redacted]" in binding["path"]


# ---------------------------------------------------------------------------
# #3379: the critical is the ingest runway of the PGDATA device
# ---------------------------------------------------------------------------


def _audit(observations, tmp_path, extra=()):
    path = tmp_path / "audit.json"
    rc = governance.main(_argv(tmp_path, path, extra))
    return rc, json.loads(path.read_text())


def test_six_days_of_runway_is_not_critical(observations, tmp_path, capsys):
    """Spec "Six days of runway is not critical": 373.5 GiB uncompressed is more
    than the 288.7 GiB above the margin (the retired rule fired), /home is short,
    the database is 1000 GiB."""
    observations.update(
        target=_gib(388.7), home=200 * GIB, rows=SCENARIO_ROWS, uncompressed=_gib(373.5), daily=_gib(31.9)
    )
    rc, receipt = _audit(observations, tmp_path)
    working = receipt["working_set"]
    assert rc == 0
    assert working["projection_status"] == "ok"
    assert working["projected_peak_bytes"] > working["working_set_free_bytes"] - 100 * GIB
    assert working["ingest_runway_days"] == pytest.approx(6.5, abs=0.01)
    assert governance._critical_codes(receipt) == []
    codes = {row["code"]: row["severity"] for row in receipt["recommendations"]}
    assert codes["DATABASE_SIZE_ABOVE_CRITICAL"] == "info"
    assert codes["HOME_FREE_BELOW_WARNING"] == "warning"
    assert capsys.readouterr().err == ""


def test_less_than_three_days_of_runway_is_critical(observations, tmp_path, capsys):
    """Spec "Less than three days of runway is critical" (230 GiB free, 2.9 days)."""
    observations.update(target=_gib(230), home=900 * GIB, rows=SCENARIO_ROWS, uncompressed=_gib(373.5))
    rc, receipt = _audit(observations, tmp_path)
    assert rc == 1
    assert governance._critical_codes(receipt) == [RUNWAY]
    assert receipt["working_set"]["ingest_runway_days"] == pytest.approx(2.93, abs=0.01)
    assert "RESOURCE_GOVERNANCE_CRITICAL:" + RUNWAY in capsys.readouterr().err.splitlines()


# The successful catalog statuses, as the spec names them.
_OBSERVED_STATUSES = ("ok", "no_uncompressed_chunk", "watermark_unavailable")


def _runway_receipt(free, rate, status="ok", **working):
    return {
        "working_set": {
            "projection_status": status,
            "working_set_filesystem": {"status": "ok", "path": "/pg", "device_identity": "8:1:1", "blockers": []},
            "working_set_free_bytes": free,
            "uncompressed_bytes": 1,
            "projected_peak_bytes": 1,
            "daily_arrival_bytes": rate,
            **working,
        },
        "filesystem": {"path_sizes": {"pgdata_root": {"status": "ok", "bytes": 1}}},
    }


def _critical(receipt, thresholds=None):
    return [
        row["code"]
        for row in governance._recommendations(receipt, thresholds or governance.AuditThresholds())
        if row["severity"] == "critical"
    ]


@pytest.mark.parametrize(
    ("delta", "expected"), [(-1, [RUNWAY]), (0, []), (1, [])], ids=("one-byte-below", "equal-fits", "one-byte-above")
)
@pytest.mark.parametrize("status", _OBSERVED_STATUSES)
def test_runway_boundary_is_strict(status, delta, expected):
    """Spec "Exactly three days fits" and task 4.3, for every successful catalog
    status: an integer-byte rate keeps 3 x rate exact."""
    rate = _gib(44.4)
    free = 100 * GIB + 3 * rate + delta
    receipt = _runway_receipt(free, float(rate), status)
    codes = _critical(receipt)
    if status == "watermark_unavailable":
        assert codes == ["WATERMARK_UNAVAILABLE", *expected]
    else:
        assert codes == expected
    days = governance._ingest_runway_days(receipt["working_set"], governance.AuditThresholds())
    assert days == pytest.approx(3.0)


@pytest.mark.parametrize(
    ("state", "free", "expected"),
    [
        ("empty", 90, [RUNWAY]),
        ("empty", 150, []),
        ("legacy-only", 90, [RUNWAY]),
        ("legacy-only", 150, []),
    ],
)
def test_zero_rate_leaves_the_margin_as_the_floor(observations, tmp_path, state, free, expected):
    """Spec "A zero rate leaves the margin as the floor": no uncompressed chunk
    (status no_uncompressed_chunk), or only a `_legacy` one (status ok)."""
    if state == "empty":
        observations.update(uncompressed=0, oldest=None)
    else:
        observations.update(uncompressed=_gib(300), rows=[_chunk("river_timeseries_legacy", 300, days=3)])
    observations["target"] = free * GIB
    rc, receipt = _audit(observations, tmp_path)
    working = receipt["working_set"]
    assert working["projection_status"] == ("no_uncompressed_chunk" if state == "empty" else "ok")
    assert working["daily_arrival_bytes"] == 0.0
    assert working["ingest_runway_days"] is None
    assert governance._critical_codes(receipt) == expected
    assert rc == (1 if expected else 0)


_BAD_RATES = [
    pytest.param(governance._MISSING, id="absent"),
    pytest.param(None, id="null"),
    pytest.param(-1.0, id="negative"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="inf"),
    pytest.param(True, id="bool"),
    pytest.param("44", id="string"),
]


@pytest.mark.parametrize("capacity", ["valid", "invalid"])
@pytest.mark.parametrize("status", _OBSERVED_STATUSES)
@pytest.mark.parametrize("rate", _BAD_RATES)
def test_missing_rate_is_a_fault_not_a_pass(rate, status, capacity):
    """Spec "A missing rate is a fault, not a pass", in every successful
    catalog status and independently of capacity validity."""
    receipt = _runway_receipt(10 * GIB, rate, status)
    if rate is governance._MISSING:
        del receipt["working_set"]["daily_arrival_bytes"]
    if capacity == "invalid":
        receipt["working_set"]["working_set_free_bytes"] = None
    rows = governance._recommendations(receipt, governance.AuditThresholds())
    unavailable = [row for row in rows if row["code"] == "WORKING_SET_UNAVAILABLE"]
    assert len(unavailable) == 1 and unavailable[0]["severity"] == "critical"
    assert "daily_arrival_bytes" in unavailable[0]["action"]
    assert RUNWAY not in {row["code"] for row in rows}
    assert governance._ingest_runway_days(receipt["working_set"], governance.AuditThresholds()) is None


@pytest.mark.parametrize("rate", [pytest.param(None, id="absent"), -GIB, float("inf")])
def test_missing_rate_exits_one_through_the_audit(observations, tmp_path, capsys, monkeypatch, rate):
    real = governance.collect_working_set

    def without_rate(*args, **kwargs):
        sample = real(*args, **kwargs)
        assert sample["projection_status"] == "ok"
        if rate is None:
            del sample["daily_arrival_bytes"]
        else:
            sample["daily_arrival_bytes"] = rate
        return sample

    monkeypatch.setattr(governance, "collect_working_set", without_rate)
    rc, receipt = _audit(observations, tmp_path)
    assert rc == 1
    assert governance._critical_codes(receipt) == ["WORKING_SET_UNAVAILABLE"]
    assert receipt["working_set"]["ingest_runway_days"] is None
    assert "RESOURCE_GOVERNANCE_CRITICAL:WORKING_SET_UNAVAILABLE" in capsys.readouterr().err


def test_runway_comparison_does_not_wait_for_the_watermark(observations, tmp_path, capsys):
    """Spec "The comparison does not wait for the watermark"."""
    observations.update(watermark=None, target=_gib(230), rows=SCENARIO_ROWS, uncompressed=_gib(373.5))
    rc, receipt = _audit(observations, tmp_path)
    working = receipt["working_set"]
    assert rc == 1
    assert working["projection_status"] == "watermark_unavailable"
    assert working["projected_peak_bytes"] is None
    assert working["ingest_runway_days"] == pytest.approx(130 / 44.4, rel=1e-9)
    assert governance._critical_codes(receipt) == ["WATERMARK_UNAVAILABLE", RUNWAY]
    stderr = capsys.readouterr().err
    assert "RESOURCE_GOVERNANCE_CRITICAL:WATERMARK_UNAVAILABLE" in stderr
    assert "RESOURCE_GOVERNANCE_CRITICAL:" + RUNWAY in stderr


@pytest.mark.parametrize(
    ("rows", "uncompressed", "expected"),
    [
        pytest.param(SCENARIO_ROWS, 373.5, SCENARIO_RATE, id="largest-chunk-per-hypertable"),
        pytest.param([_chunk("forcing_station_timeseries", 70, days=7)], 70, 10 * GIB, id="seven-day-chunk"),
        pytest.param(
            [row for row in SCENARIO_ROWS if row["hypertable_name"] == "river_timeseries"],
            300,
            _gib(41.8),
            id="hypertable-without-uncompressed-chunk",
        ),
        pytest.param(
            [*SCENARIO_ROWS, _chunk("river_timeseries_legacy", 300, days=3)],
            373.5 + 300,
            SCENARIO_RATE,
            id="legacy-chunk",
        ),
    ],
)
def test_receipt_records_the_arrival_rate(observations, tmp_path, rows, uncompressed, expected):
    """Spec scenarios "Arrival is the largest uncompressed chunk of each
    hypertable" (44.4 GiB next to the 31.9 GiB trailing mean), "per day whatever
    the chunk interval", "A hypertable with no uncompressed chunk contributes
    nothing", "A legacy chunk does not enter the rate"."""
    observations.update(rows=rows, uncompressed=_gib(uncompressed), daily=_gib(31.9))
    rc, receipt = _audit(observations, tmp_path)
    working = receipt["working_set"]
    assert rc == 0
    assert working["projection_status"] == "ok"
    assert working["daily_arrival_bytes"] == pytest.approx(expected, abs=1)
    assert working["daily_ingest_bytes"] == _gib(31.9)
    assert working["uncompressed_bytes"] == _gib(uncompressed)


@pytest.mark.parametrize(
    "fault",
    [
        "no-row",
        "null-start",
        "null-end",
        "zero-interval",
        "negative-interval",
        "legacy-null-interval",
        "null-size",
        "rows-statement-fails",
    ],
)
def test_inconsistent_or_failed_arrival_read_leaves_no_partial_working_set(observations, tmp_path, capsys, fault):
    """Spec "Rows that contradict the aggregate are a fault" and "A failed
    arrival read leaves no partial working set": the aggregate succeeded, yet
    every catalog field is null and the audit emits WORKING_SET_UNAVAILABLE."""
    start = _DAY0
    bad = {
        "no-row": [],
        "null-start": [*DEFAULT_ROWS, {**DEFAULT_ROWS[0], "range_start": None}],
        "null-end": [*DEFAULT_ROWS, {**DEFAULT_ROWS[0], "range_end": None}],
        "zero-interval": [*DEFAULT_ROWS, _chunk("river_timeseries", 1, start=start, end=start)],
        "negative-interval": [
            *DEFAULT_ROWS,
            _chunk("river_timeseries", 1, start=start, end=start - timedelta(hours=1)),
        ],
        "legacy-null-interval": [*DEFAULT_ROWS, {**_chunk("river_timeseries_legacy", 300), "range_end": None}],
        "null-size": [*DEFAULT_ROWS, {**DEFAULT_ROWS[0], "total_bytes": None}],
        "rows-statement-fails": None,
    }[fault]
    if bad is None:
        observations["rows_failure"] = psycopg2.errors.QueryCanceled("password=rows-secret")
    else:
        observations["rows"] = bad
    rc, receipt = _audit(observations, tmp_path)
    working = receipt["working_set"]
    assert working["projection_status"] == "catalog_unavailable"
    assert _catalog_fields(working) == dict.fromkeys(_CATALOG_FIELDS)
    assert working["working_set_free_bytes"] == 900 * GIB
    assert governance._critical_codes(receipt) == ["WORKING_SET_UNAVAILABLE"]
    assert rc == 1
    assert collection.UNCOMPRESSED_CHUNKS_SQL in observations["queries"]
    assert DISPLAY_WATERMARK_SQL not in observations["queries"]
    assert "rows-secret" not in json.dumps(receipt) + capsys.readouterr().err


def test_malformed_aggregate_leaves_no_partial_working_set(observations, tmp_path):
    observations["uncompressed"] = "not-a-number"
    rc, receipt = _audit(observations, tmp_path)
    assert receipt["working_set"]["projection_status"] == "catalog_unavailable"
    assert _catalog_fields(receipt["working_set"]) == dict.fromkeys(_CATALOG_FIELDS)
    assert rc == 1


# Pure rate function on plain rows (task 4.2).


def test_rate_is_the_per_hypertable_maximum_summed():
    rows = [
        _chunk("river_timeseries", 10, day=0),
        _chunk("river_timeseries", 20, day=1),
        _chunk("forcing_station_timeseries", 3, day=0),
        _chunk("forcing_station_timeseries", 1, day=1),
    ]
    # Not the sum (34), not the global maximum (20).
    assert collection.daily_arrival_bytes(rows) == 23 * GIB


def test_rate_normalises_by_the_chunk_interval_in_fractional_days():
    assert collection.daily_arrival_bytes([_chunk("river_timeseries", 70, days=7)]) == 10 * GIB
    half_day = _chunk("river_timeseries", 2, start=_DAY0, end=_DAY0 + timedelta(hours=12))
    assert collection.daily_arrival_bytes([half_day]) == 4 * GIB
    # The larger chunk per day wins, not the larger chunk: 70/7 < 12/1.
    assert collection.daily_arrival_bytes([_chunk("river_timeseries", 70, days=7), _chunk("river_timeseries", 12)]) == (
        12 * GIB
    )


def test_rate_of_the_scenario_rows_is_44_4_gib():
    assert collection.daily_arrival_bytes(SCENARIO_ROWS) == pytest.approx(44.4 * GIB, abs=1)


def test_rate_without_rows_or_without_canonical_rows_is_zero():
    assert collection.daily_arrival_bytes([]) == 0.0
    river_only = [_chunk("river_timeseries", 41.8)]
    assert collection.daily_arrival_bytes(river_only) == _gib(41.8)
    legacy = [_chunk("river_timeseries_legacy", 300, days=3), _chunk("forcing_station_timeseries_legacy", 9)]
    assert collection.daily_arrival_bytes(legacy) == 0.0
    assert collection.daily_arrival_bytes([*river_only, *legacy]) == _gib(41.8)


@pytest.mark.parametrize(
    "row",
    [
        {**_chunk("river_timeseries", 1), "range_start": None},
        {**_chunk("river_timeseries", 1), "range_end": None},
        _chunk("river_timeseries", 1, start=_DAY0, end=_DAY0),
        _chunk("river_timeseries", 1, start=_DAY0, end=_DAY0 - timedelta(days=1)),
        {**_chunk("river_timeseries_legacy", 1), "range_end": None},
        {**_chunk("river_timeseries", 1), "total_bytes": None},
        {**_chunk("river_timeseries", 1), "total_bytes": -1},
        {**_chunk("river_timeseries", 1), "total_bytes": True},
    ],
    ids=(
        "null-start",
        "null-end",
        "zero-interval",
        "negative-interval",
        "legacy-null",
        "null-size",
        "negative-size",
        "bool-size",
    ),
)
def test_rate_refuses_a_faulty_row(row):
    with pytest.raises(ValueError):
        collection.daily_arrival_bytes([_chunk("river_timeseries", 41.8), row])


def test_canonical_set_comes_from_the_discovery_module():
    from packages.common import node27_timeseries_discovery as discovery

    assert collection.CANONICAL_HYPERTABLES is discovery.CANONICAL_HYPERTABLES
    assert {pair[1] for pair in discovery.LEGACY_HYPERTABLES} == {
        "river_timeseries_legacy",
        "forcing_station_timeseries_legacy",
    }


def test_retired_code_is_absent_from_the_audit_source():
    """Spec "The retired code is not emitted": the autouse wrapper covers the
    states this suite builds; the sources cannot produce it at all."""
    for module in (governance, collection):
        assert RETIRED not in Path(module.__file__).read_text(encoding="utf-8")
