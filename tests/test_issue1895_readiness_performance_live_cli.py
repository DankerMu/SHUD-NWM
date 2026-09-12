"""CLI/injection contracts for the four-lane live performance oracle. Helpers imported from the live core suite."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from packages.common.node27_issue1895_catalog import INTERSECTING_CHUNKS_SQL
from packages.common.node27_issue1895_lanes import LANE_NAMES
from packages.common.node27_issue1895_performance_live import (
    discover_four_lanes,
    fetch_local_api,
    open_readonly_performance_connection,
)
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from scripts import node27_issue1895_performance_oracle as oracle
from tests.test_issue1895_readiness_performance_live import (
    BASIN,
    DSN,
    IDENTITIES,
    ORIGIN,
    SEGMENT,
    SHA,
    FakeConnection,
    FakeCursor,
    FakeOpener,
    _fake_collect_group,
    _fake_load_chunk,
    _plan,
    _query,
)
from tests.test_issue1895_runbook_contract import _gate, _gate_bash, _gate_lines


def _injected_lanes() -> dict[str, dict]:
    lanes: dict[str, dict] = {}
    for name, source, kind in (
        ("gfs_hot", "GFS", "hot"),
        ("ifs_hot", "IFS", "hot"),
        ("gfs_cold", "GFS", "cold"),
        ("ifs_cold", "IFS", "cold"),
    ):
        identity = IDENTITIES[source][kind]
        query = _query(source, identity["run_id"], identity["cycle_time"])
        state = "hot_uncompressed_source" if kind == "hot" else "cold_compressed_target"
        lanes[name] = {
            "name": name,
            "identity": {
                "run_id": identity["run_id"],
                "model_id": identity["model_id"],
                "basin_id": identity["basin_id"],
                "basin_version_id": identity["basin_version_id"],
                "river_network_version_id": identity["river_network_version_id"],
                "cycle_time": identity["cycle_time"],
                "source_id": source,
                "run_status": "published",
                "scenario": query["scenario"],
            },
            "query": query,
            "candidate_chunk_names": ["_hyper_1_1_chunk"],
            "candidate_count": 1,
            "tablespace": "pg_default" if kind == "hot" else "nhms_cold",
            "compressed": kind == "cold",
            "query_digest": query["query_digest"],
            "state": state,
        }
    return lanes


def _argv(receipt: Path, *, display_origin: str | None = None) -> list[str]:
    args = [
        "--receipt-path",
        str(receipt),
        "--basin-id",
        BASIN,
        "--segment-id",
        SEGMENT,
        "--head-sha",
        SHA,
        "--reviewed-sha",
        SHA,
    ]
    if display_origin is not None:
        args.extend(("--display-origin", display_origin))
    return args


def _private_display_env(path: Path, *, port: int = 8080) -> Path:
    path.write_text(f"DATABASE_URL={DSN}\nNHMS_DISPLAY_API_PORT={port}\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def test_injection_is_all_or_none() -> None:
    def sql_probe(_index: int) -> dict:
        return {"duration_ms": 1, "explain_json": _plan()}

    def api_probe(_index: int) -> dict:
        return {"duration_ms": 1, "status": 200, "body_len": 1}

    lanes = _injected_lanes()
    sql_probes = {name: sql_probe for name in LANE_NAMES}
    api_probes = {name: api_probe for name in LANE_NAMES}
    assert (
        oracle._injection_state(
            sql_probes=None,
            api_probes=None,
            lanes=None,
            connect=None,
            opener=None,
            connection=None,
        )
        == "live"
    )
    assert (
        oracle._injection_state(
            sql_probes=sql_probes,
            api_probes=api_probes,
            lanes=lanes,
            connect=None,
            opener=None,
            connection=None,
        )
        == "full-probes"
    )
    connection = FakeConnection()
    opener = FakeOpener()
    assert (
        oracle._injection_state(
            sql_probes=None,
            api_probes=None,
            lanes=None,
            connect=lambda _dsn: connection,
            opener=opener,
            connection=None,
        )
        == "live-connect"
    )
    mixed_cases = [
        dict(sql_probes=sql_probes, api_probes=None, lanes=None, connect=None, opener=opener, connection=None),
        dict(
            sql_probes=None,
            api_probes=None,
            lanes=None,
            connect=lambda _dsn: connection,
            opener=None,
            connection=None,
        ),
        dict(
            sql_probes=sql_probes,
            api_probes=api_probes,
            lanes=lanes,
            connect=None,
            opener=opener,
            connection=None,
        ),
        dict(
            sql_probes=None,
            api_probes=None,
            lanes=None,
            connect=lambda _dsn: connection,
            opener=opener,
            connection=connection,
        ),
    ]
    for kwargs in mixed_cases:
        with pytest.raises(Issue1895ReadinessError) as mixed:
            oracle._injection_state(**kwargs)
        assert mixed.value.code == "INJECTION_MIXED"


def test_default_cli_live_connect_path_sets_readonly_before_sql(tmp_path: Path) -> None:
    parent = tmp_path / "run"
    parent.mkdir()
    os.chmod(parent, 0o700)
    target = parent / "performance.json"
    env_file = tmp_path / "display.env"
    _private_display_env(env_file)
    connection = FakeConnection()
    opener = FakeOpener()
    seen_dsn: list[str] = []

    def connect(dsn: str) -> FakeConnection:
        seen_dsn.append(dsn)
        return connection

    rc = oracle.main(
        _argv(target, display_origin=ORIGIN) + ["--display-env", str(env_file)],
        connect=connect,
        opener=opener,
        load_chunk=_fake_load_chunk,
        collect_group=_fake_collect_group,
        environ={"NHMS_DISPLAY_API_PORT": "8080"},
    )
    assert rc == 0
    assert seen_dsn == [DSN]
    assert connection.executed[0][0] == "set_session"
    assert connection.executed[0][1] == {"readonly": True, "autocommit": False}
    assert connection.explains == 84
    assert connection.rolled_back is True
    assert connection.closed is True
    document = json.loads(target.read_text(encoding="utf-8"))
    encoded = json.dumps(document)
    assert document["status"] == "PASS"
    assert document["readonly"] == {"transaction_read_only": True, "current_user": "nhms_display_ro"}
    assert set(document["lanes"]) == set(LANE_NAMES)
    assert document["lanes"]["gfs_hot"]["sql"]["accepted_count"] == 20
    assert document["lanes"]["ifs_cold"]["api"]["accepted_count"] == 20
    assert document["lanes"]["gfs_hot"]["candidate_chunk_names"] == ["_hyper_1_1_chunk"]
    assert document["lanes"]["ifs_hot"]["candidate_chunk_names"] == ["_hyper_1_1_chunk"]
    assert document["lanes"]["gfs_cold"]["candidate_chunk_names"] == ["_hyper_1_1_chunk_cold"]
    assert document["lanes"]["ifs_cold"]["candidate_chunk_names"] == ["_hyper_1_1_chunk_cold"]
    assert document["lanes"]["gfs_hot"]["sql"]["plan_metrics"][0]["decompressed_count"] == 0
    assert document["lanes"]["gfs_cold"]["sql"]["plan_metrics"][0]["decompressed_count"] == 1
    assert "/forecast-series" in document["lanes"]["gfs_hot"]["api"]["path"]
    assert document["identity"]["basin_id"] == BASIN
    assert document["identity"]["segment_id"] == SEGMENT
    assert "secret" not in encoded
    assert "postgresql://" not in encoded
    marker = parent / "performance.json.commit"
    assert marker.is_file()
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert oct(marker.stat().st_mode & 0o777) == "0o600"


def test_cli_uses_private_display_env_port_not_ambient_port(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "run"
    parent.mkdir()
    os.chmod(parent, 0o700)
    target = parent / "performance.json"
    env_file = _private_display_env(tmp_path / "display.env", port=18080)
    connection = FakeConnection()
    opener = FakeOpener()
    rc = oracle.main(
        _argv(target) + ["--display-env", str(env_file)],
        connect=lambda _dsn: connection,
        opener=opener,
        load_chunk=_fake_load_chunk,
        collect_group=_fake_collect_group,
        environ={"NHMS_DISPLAY_API_PORT": "19090"},
    )
    assert rc == 0
    assert opener.requests
    assert all(request.full_url.startswith("http://127.0.0.1:18080/") for request in opener.requests)
    assert json.loads(target.read_text(encoding="utf-8"))["identity"]["api_origin"] == "http://127.0.0.1:18080"


def test_cli_defaults_missing_private_display_env_port_without_ambient_override(tmp_path: Path) -> None:
    parent = tmp_path / "run"
    parent.mkdir()
    os.chmod(parent, 0o700)
    target = parent / "performance.json"
    env_file = tmp_path / "display.env"
    env_file.write_text(f"DATABASE_URL={DSN}\n", encoding="utf-8")
    os.chmod(env_file, 0o600)
    connection = FakeConnection()
    opener = FakeOpener()

    rc = oracle.main(
        _argv(target) + ["--display-env", str(env_file)],
        connect=lambda _dsn: connection,
        opener=opener,
        load_chunk=_fake_load_chunk,
        collect_group=_fake_collect_group,
        environ={"NHMS_DISPLAY_API_PORT": "19090"},
    )

    assert rc == 0
    assert opener.requests
    assert all(request.full_url.startswith("http://127.0.0.1:8080/") for request in opener.requests)
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["identity"]["api_origin"] == "http://127.0.0.1:8080"


@pytest.mark.parametrize(
    "contents",
    (
        f"DATABASE_URL={DSN}\nNHMS_DISPLAY_API_PORT='18080'\n",
        f"DATABASE_URL={DSN}\nNHMS_DISPLAY_API_PORT=18080\nNHMS_DISPLAY_API_PORT=18081\n",
        f"DATABASE_URL={DSN}\nNHMS_DISPLAY_API_PORT=08080\n",
    ),
)
def test_cli_refuses_invalid_private_display_env_port_before_connect(
    tmp_path: Path,
    contents: str,
) -> None:
    parent = tmp_path / "run"
    parent.mkdir()
    os.chmod(parent, 0o700)
    target = parent / "performance.json"
    env_file = tmp_path / "display.env"
    env_file.write_text(contents, encoding="utf-8")
    os.chmod(env_file, 0o600)
    connection_attempted = False

    def unexpected_connect(_dsn: str) -> FakeConnection:
        nonlocal connection_attempted
        connection_attempted = True
        raise AssertionError("invalid port must refuse before a DSN connection")

    assert (
        oracle.main(
            _argv(target) + ["--display-env", str(env_file)],
            connect=unexpected_connect,
            opener=FakeOpener(),
            load_chunk=_fake_load_chunk,
            collect_group=_fake_collect_group,
        )
        == 1
    )
    assert connection_attempted is False
    assert not target.exists()
    assert not (parent / "performance.json.commit").exists()


def test_cli_refuses_display_origin_conflicting_with_display_env(tmp_path: Path) -> None:
    parent = tmp_path / "run"
    parent.mkdir()
    os.chmod(parent, 0o700)
    env_file = _private_display_env(tmp_path / "display.env", port=18080)
    assert (
        oracle.main(_argv(parent / "performance.json", display_origin=ORIGIN) + ["--display-env", str(env_file)]) == 1
    )


def test_cli_failure_still_closes_and_redacts_connect_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parent = tmp_path / "run"
    parent.mkdir()
    os.chmod(parent, 0o700)
    target = parent / "performance.json"
    env_file = tmp_path / "display.env"
    _private_display_env(env_file)
    connection = FakeConnection(fail_after=0)
    opener = FakeOpener()
    rc = oracle.main(
        _argv(target, display_origin=ORIGIN) + ["--display-env", str(env_file)],
        connect=lambda _dsn: connection,
        opener=opener,
        load_chunk=_fake_load_chunk,
        collect_group=_fake_collect_group,
        environ={"NHMS_DISPLAY_API_PORT": "8080"},
    )
    assert rc == 1
    assert connection.rolled_back is True
    assert connection.closed is True
    assert not target.exists()
    assert not (parent / "performance.json.commit").exists()
    err = capsys.readouterr().err
    assert "postgresql://" not in err
    assert "secret" not in err
    assert DSN not in err


def test_connect_exception_does_not_leak_dsn(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    parent = tmp_path / "run"
    parent.mkdir()
    os.chmod(parent, 0o700)
    target = parent / "performance.json"
    env_file = tmp_path / "display.env"
    _private_display_env(env_file)

    def connect(dsn: str) -> FakeConnection:
        raise RuntimeError(f"could not connect using {dsn}")

    rc = oracle.main(
        _argv(target, display_origin=ORIGIN) + ["--display-env", str(env_file)],
        connect=connect,
        opener=FakeOpener(),
        load_chunk=_fake_load_chunk,
        collect_group=_fake_collect_group,
        environ={"NHMS_DISPLAY_API_PORT": "8080"},
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "SQL_CONNECT_FAILED" in err
    assert DSN not in err
    assert "postgresql://" not in err


def test_empty_candidate_set_is_illegal() -> None:
    class EmptyChunks(FakeConnection):
        def cursor(self) -> FakeCursor:
            cursor = super().cursor()
            original = cursor.execute

            def execute(sql: object, params: object = None) -> None:
                original(sql, params)
                if "timescaledb_information.chunks" in str(sql):
                    cursor._rows = []

            cursor.execute = execute  # type: ignore[method-assign]
            return cursor

    empty = EmptyChunks()
    open_readonly_performance_connection(DSN, connect=lambda _dsn: empty)
    with pytest.raises(Issue1895ReadinessError) as caught:
        discover_four_lanes(
            empty,
            basin_id=BASIN,
            segment_id=SEGMENT,
            origin=ORIGIN,
            opener=FakeOpener(),
            load_chunk=_fake_load_chunk,
            collect_group=_fake_collect_group,
        )
    assert caught.value.code == "LANE_EMPTY"


def test_non_localhost_origin_is_refused() -> None:
    query = _query("GFS", "run-gfs-hot", "2026-08-01T00:00:00Z")
    with pytest.raises(Issue1895ReadinessError) as caught:
        fetch_local_api(
            origin="http://example.invalid:8080",
            path=query["api_path"],
            query=query["api_query"],
            segment_id=SEGMENT,
            issue_time=query["issue_time"],
            scenario=query["scenario"],
            source=query["source"],
            window_start=query["window_start"],
            window_end=query["window_end"],
            opener=FakeOpener(),
        )
    assert caught.value.code == "INPUT_ORIGIN_INVALID"


def test_g7_runbook_invokes_live_cli_and_binds_current_pass_receipt() -> None:
    g7 = _gate("G7")
    lines = " ".join(_gate_lines("G7"))
    assert "scripts/node27_issue1895_performance_oracle.py" in g7
    assert '--receipt-path "$PERF_RECEIPT"' in lines
    assert '--basin-id "$BASIN_ID"' in lines
    assert '--segment-id "$SEGMENT_ID"' in lines
    assert "--display-env /home/nwm/NWM/infra/env/display.env" in lines
    assert "--window-start" not in lines
    assert "date -ud '-30 days'" not in g7
    assert "PERFORMANCE_LIVE_DISABLED" not in g7
    assert "--database-url" not in g7
    assert "scripts/node27_issue1895_performance_bind.py" in g7
    assert "PERF_COMMIT" in g7
    assert '--bracket "$PERF_BRACKET"' in lines
    assert "forecast-series" in g7 or "scripts/node27_issue1895_performance_oracle.py" in g7
    assert "http://127.0.0.1:" in g7
    assert "json.load(open(path))" not in g7
    assert "assert_report_within_command_bracket" not in g7
    for _opening, body in _gate_bash("G7"):
        if "performance_bind.py" in body:
            assert "--bracket" in body
            assert "os.stat(path)" not in body
    for _opening, body in _gate_bash("G7"):
        assert "Shared Read Buffers:" not in body
        assert "pgrep -f" not in body
    assert INTERSECTING_CHUNKS_SQL


@pytest.mark.parametrize("state", ["wide", "parent_drift"])
def test_performance_cli_refuses_actual_cold_admission_without_publication(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    state: str,
) -> None:
    class AdmissionConnection(FakeConnection):
        def cursor(self):
            cursor = super().cursor()
            original = cursor.execute

            def execute(sql, params=None):
                original(sql, params)
                if "FROM pg_attribute" in str(sql) and params == ("hydro", "river_timeseries"):
                    if state == "wide":
                        cursor._rows.append(
                            dict(
                                cursor._rows[0],
                                attnum=99,
                                attname="run_id",
                                type_name="text",
                                typtype="b",
                            )
                        )
                    else:
                        cursor._rows[-1] = dict(
                            cursor._rows[-1],
                            parent_oid=cursor._rows[-1]["parent_oid"] + 1,
                        )

            cursor.execute = execute
            return cursor

    parent = tmp_path / "run"
    parent.mkdir(mode=0o700)
    target = parent / "performance.json"
    env_file = _private_display_env(tmp_path / "display.env")
    connection = AdmissionConnection()
    rc = oracle.main(
        _argv(target, display_origin=ORIGIN) + ["--display-env", str(env_file)],
        connect=lambda _dsn: connection,
        opener=FakeOpener(),
        load_chunk=_fake_load_chunk,
        collect_group=_fake_collect_group,
        environ={"NHMS_DISPLAY_API_PORT": "8080"},
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("LANE_CATALOG_FAILED:")
    assert "Traceback" not in captured.err
    assert list(parent.iterdir()) == []
    assert connection.closed
