"""Exercise CLI projection using only PostgreSQL/OS boundary substitutes."""

import errno
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import jsonschema
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
        "queries": [],
        "target_path": str(tmp_path.resolve()),
        "target_failure": None,
        "target_samples": 0,
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
    real_stat = Path.stat
    directory_stat = tmp_path.stat()

    def stat(path, *args, **kwargs):
        # Keep lstat identity real for no-follow receipt publication.
        if not kwargs.get("follow_symlinks", True):
            return real_stat(path, *args, **kwargs)
        if str(path) in {"/", "/home", "/data/GHDC", state["target_path"]}:
            if str(path) == state["target_path"] and state["target_failure"] == "missing":
                raise FileNotFoundError(errno.ENOENT, "password=os-secret", str(path))
            return directory_stat
        return real_stat(path, *args, **kwargs)

    def statvfs(path):
        is_target = str(path) == state["target_path"]
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
    ("home", "flags", "expected_exit", "expected_code"),
    [
        (900, [], 0, None),
        (800, [], 1, "PROJECTED_PEAK_EXCEEDS_WORKING_SET_FREE"),
        (900, ["--safety-margin-bytes", str(200 * GIB)], 1, "PROJECTED_PEAK_EXCEEDS_WORKING_SET_FREE"),
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
        assert "RESOURCE_GOVERNANCE_CRITICAL:PROJECTED_PEAK_EXCEEDS_WORKING_SET_FREE" in stderr
        for field in ("projected_peak_bytes", "working_set_free_bytes", "next_compressible_at", "uncompressed_bytes"):
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
    collection.collect_working_set("postgresql://unused", {})
    sql = next(query for query in observations["queries"] if "oldest_uncompressed_range_end" in query)
    assert "FROM timescaledb_information.chunks" in sql
    assert "FROM timescaledb_information.hypertables" in sql
    assert "range_start >= CURRENT_TIMESTAMP - interval '7 days'" in sql
    assert "pg_total_relation_size" in sql
    for forbidden in ("FROM hydro.", "FROM met.", "pg_class", "pg_tables", "_timescaledb_catalog"):
        assert forbidden not in sql


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
    assert "WORKING_SET_UNAVAILABLE" in governance._critical_codes(receipt)
    assert rc == 1
    assert "RESOURCE_GOVERNANCE_CRITICAL:WORKING_SET_UNAVAILABLE" in capsys.readouterr().err
    assert "db-secret" not in path.read_text()


@pytest.mark.parametrize(
    ("home", "target", "expected_exit"),
    [(800, 900, 0), (900, 800, 1), (900, 850, 0)],
    ids=("destination-fits-home-does-not", "destination-full-home-fits", "equality-fits"),
)
def test_cli_destination_capacity_not_home(observations, tmp_path, capsys, home, target, expected_exit):
    observations.update(home=home * GIB, target=target * GIB)
    path = tmp_path / "audit.json"
    rc = governance.main(_argv(tmp_path, path))
    receipt = json.loads(path.read_text())
    codes = governance._critical_codes(receipt)
    assert rc == expected_exit
    assert codes == (["PROJECTED_PEAK_EXCEEDS_WORKING_SET_FREE"] if expected_exit else [])
    working = receipt["working_set"]
    assert working["projected_peak_bytes"] == 750 * GIB
    assert working["working_set_free_bytes"] == target * GIB
    assert "home_free_bytes" not in working
    binding = working["working_set_filesystem"]
    assert binding["path"] == str(tmp_path.resolve())
    assert binding["status"] == "ok"
    assert binding["blockers"] == []
    stderr = capsys.readouterr().err
    if expected_exit:
        assert "RESOURCE_GOVERNANCE_CRITICAL:PROJECTED_PEAK_EXCEEDS_WORKING_SET_FREE" in stderr
        assert str(750 * GIB) in stderr
        assert str(800 * GIB) in stderr
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
    stderr = capsys.readouterr().err
    for code in codes:
        assert "RESOURCE_GOVERNANCE_CRITICAL:" + code in stderr
    for secret in ("os-secret", "driver-secret", "du-secret"):
        assert secret not in path.read_text() + stderr


@pytest.mark.parametrize("target", ["/home/nwm/nhms-pgdata", "/data/GHDC/nhms-primary/pgdata", "/srv/db-alias"])
def test_cli_configured_paths_and_duplicate_device_labels(observations, tmp_path, target):
    observations.update(target_path=target, target=900 * GIB, home=800 * GIB)
    path = tmp_path / "audit.json"
    assert governance.main(_argv(tmp_path, path, ["--pgdata-root", target])) == 0
    receipt = json.loads(path.read_text())
    binding = receipt["working_set"]["working_set_filesystem"]
    assert binding["path"] == target
    assert binding["status"] == "ok"
    assert binding["blockers"] == []
    assert receipt["working_set"]["working_set_free_bytes"] == 900 * GIB
    assert receipt["filesystem"]["filesystems"]["home"]["device_identity"] == binding["device_identity"]


def test_cli_optional_cold_receipt_uses_destination_shape(observations, tmp_path):
    summary = tmp_path / "audit.json"
    cold = tmp_path / "cold.json"
    assert governance.main(_argv(tmp_path, summary, ["--cold-governance-receipt-path", str(cold)])) == 0
    receipt = json.loads(cold.read_text())
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas/node27_cold_governance_receipt.schema.json").read_text()
    )
    jsonschema.validate(receipt, schema)
    working = receipt["working_set"]
    assert working["working_set_free_bytes"] == 900 * GIB
    assert working["working_set_filesystem"]["path"] == str(tmp_path.resolve())
    assert "home_free_bytes" not in working


def test_current_comparator_never_falls_back_to_historical_home():
    receipt = {
        "working_set": {
            "projection_status": "ok",
            "uncompressed_bytes": 600 * GIB,
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
    observations.update(target=800 * GIB)
    summary = tmp_path / "audit.json"
    assert governance.main(_argv(tmp_path, summary)) == 1
    diagnostic = capsys.readouterr().err
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


def test_secret_bearing_target_evidence_is_redacted_at_both_receipt_sinks(observations, tmp_path, capsys):
    target = tmp_path / "password=target-secret"
    target.mkdir()
    observations.update(target_path=str(target.resolve()), target=800 * GIB)
    summary = tmp_path / "audit.json"
    cold = tmp_path / "cold.json"
    assert (
        governance.main(
            _argv(
                tmp_path,
                summary,
                [
                    "--pgdata-root",
                    str(target),
                    "--cold-governance-receipt-path",
                    str(cold),
                ],
            )
        )
        == 1
    )
    for text in (summary.read_text(), cold.read_text(), capsys.readouterr().err):
        assert "target-secret" not in text
    binding = json.loads(summary.read_text())["working_set"]["working_set_filesystem"]
    assert "[redacted]" in binding["path"]
