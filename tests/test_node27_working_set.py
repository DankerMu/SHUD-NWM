"""Exercise CLI projection using only PostgreSQL/OS boundary substitutes."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import psycopg2
import pytest

from packages.common import node27_cold_governance_collection as collection
from scripts import node27_resource_governance as governance

GIB = 1024**3


def _argv(tmp_path, path, extra=()):
    return [
        "--database-url",
        "postgresql://user:secret@example/db",
        "--repo-root",
        str(tmp_path),
        "--object-store-root",
        str(tmp_path),
        "--pgdata-root",
        str(tmp_path),
        "--summary-path",
        str(path),
        "--quiet",
        *extra,
    ]


@pytest.fixture
def observations(monkeypatch):
    state = {
        "uncompressed": 600 * GIB,
        "daily": 75 * GIB,
        "home": 900 * GIB,
        "oldest": datetime(2026, 9, 1, tzinfo=UTC),
        "watermark": datetime(2026, 9, 1, tzinfo=UTC),
        "queries": [],
    }

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params=None):
            self.sql = sql
            state["queries"].append(sql)

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
            return []

    class Connection:
        def cursor(self):
            return Cursor()

        def set_session(self, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(psycopg2, "connect", lambda *args, **kwargs: Connection())
    monkeypatch.setattr(
        collection.os,
        "statvfs",
        lambda path: SimpleNamespace(
            f_blocks=2000 * GIB, f_bfree=state["home"], f_bavail=state["home"], f_frsize=1, f_fsid=1
        ),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="0\tunused\n", stderr=""),
    )
    monkeypatch.setenv("NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS", "172800")
    return state


@pytest.mark.parametrize(
    ("home", "flags", "expected_exit", "expected_code"),
    [
        (900, [], 0, None),
        (800, [], 1, "PROJECTED_PEAK_EXCEEDS_HOME_FREE"),
        (900, ["--safety-margin-bytes", str(200 * GIB)], 1, "PROJECTED_PEAK_EXCEEDS_HOME_FREE"),
        (900, ["--working-set-warn-bytes", str(500 * GIB)], 0, "WORKING_SET_ABOVE_WARNING"),
    ],
)
def test_cli_projection_and_threshold_overrides(
    observations, tmp_path, capsys, home, flags, expected_exit, expected_code
):
    observations["home"] = home * GIB
    path = tmp_path / "audit.json"
    rc = governance.main(_argv(tmp_path, path, flags))
    receipt = json.loads(path.read_text())
    stderr = capsys.readouterr().err
    assert rc == expected_exit
    assert receipt["status"] == "completed"
    assert receipt["working_set"]["projected_peak_bytes"] == 750 * GIB
    assert receipt["working_set"]["next_compressible_at"] == "2026-09-03T00:00:00+00:00"
    recommendations = {row["code"]: row["severity"] for row in receipt["recommendations"]}
    assert recommendations["DATABASE_SIZE_ABOVE_CRITICAL"] == "info"
    assert "DATABASE_SIZE_ABOVE_CRITICAL" not in governance._critical_codes(receipt)
    assert "RESOURCE_GOVERNANCE_CRITICAL:DATABASE_SIZE" not in stderr
    if expected_code:
        assert expected_code in recommendations
    if expected_exit:
        assert "RESOURCE_GOVERNANCE_CRITICAL:PROJECTED_PEAK_EXCEEDS_HOME_FREE" in stderr
        for field in ("projected_peak_bytes", "home_free_bytes", "next_compressible_at", "uncompressed_bytes"):
            assert field + "=" in stderr
    else:
        assert "RESOURCE_GOVERNANCE_CRITICAL:" not in stderr
    assert "secret" not in path.read_text()
    assert "postgresql://" not in path.read_text()


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
        assert governance._critical_codes(receipt) == []
    else:
        assert "WATERMARK_UNAVAILABLE" in governance._critical_codes(receipt)
        assert "RESOURCE_GOVERNANCE_CRITICAL:WATERMARK_UNAVAILABLE" in capsys.readouterr().err


def test_working_set_query_is_catalog_only(observations):
    collection.collect_working_set("postgresql://unused", 900 * GIB)
    sql = next(query for query in observations["queries"] if "oldest_uncompressed_range_end" in query)
    assert "FROM timescaledb_information.chunks" in sql
    assert "FROM timescaledb_information.hypertables" in sql
    assert "range_start >= CURRENT_TIMESTAMP - interval '7 days'" in sql
    assert "pg_total_relation_size" in sql
    for forbidden in ("FROM hydro.", "FROM met.", "pg_class", "pg_tables", "_timescaledb_catalog"):
        assert forbidden not in sql


def test_governance_example_ships_the_compression_lag():
    text = (Path(__file__).resolve().parents[1] / "infra/env/node27-resource-governance.example").read_text()
    assert "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS=172800" in text
    assert "same lag as the compression lane; do not invent a second lag" in text.lower()


def test_missing_compression_lag_does_not_report_working_set_unavailable(
    observations, tmp_path, monkeypatch, capsys
):
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


def test_catalog_connect_failure_reports_working_set_unavailable(
    observations, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        psycopg2,
        "connect",
        lambda *args, **kwargs: (_ for _ in ()).throw(psycopg2.OperationalError("refused")),
    )
    path = tmp_path / "audit.json"
    rc = governance.main(_argv(tmp_path, path))
    receipt = json.loads(path.read_text())
    assert receipt["working_set"]["projection_status"] == "catalog_unavailable"
    assert "WORKING_SET_UNAVAILABLE" in governance._critical_codes(receipt)
    assert rc == 1
    assert "RESOURCE_GOVERNANCE_CRITICAL:WORKING_SET_UNAVAILABLE" in capsys.readouterr().err

