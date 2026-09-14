"""PGDATA workload IO/boundary refusals: typed receipt, DSN, publication, identity."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.forecast_store import ForecastStoreError
from packages.common.node27_pgdata_workload import capture_workload_query, measure_workload
from packages.common.node27_pgdata_workload_io import (
    encode_evidence,
    open_readonly_connection,
    publish_measurement_output,
    read_private_dsn_file,
)
from packages.common.node27_pgdata_workload_measure import prove_authoritative_run_identity
from packages.common.node27_pgdata_workload_query import (
    query_digest,
    record_explicit_cycle_curve,
    validate_river_series_response,
)
from packages.common.node27_pgdata_workload_types import PgdataWorkloadError
from packages.common.safe_fs import SafeFilesystemError
from tests.test_node27_pgdata_workload import (
    BV,
    ISSUE,
    MODEL,
    RNV,
    RUN,
    SEGMENT,
    SHA,
    TS_SEGMENT,
    WINDOW_END,
    _assert_code,
    _Connection,
    _Cursor,
    _named_parameters,
    _named_sql,
    _ok_series,
    _plan,
)


def _api_kwargs() -> dict[str, Any]:
    return {
        "segment_id": SEGMENT,
        "issue_time": ISSUE,
        "scenario": "forecast_gfs_deterministic",
        "source": "GFS",
        "window_start": ISSUE,
        "window_end": WINDOW_END,
    }


def _private_dir(tmp_path: Path) -> Path:
    parent = tmp_path / "run"
    parent.mkdir()
    os.chmod(parent, 0o700)
    return parent


def _write_dsn(parent: Path, text: str | bytes, *, name: str = "reader.dsn") -> Path:
    path = parent / name
    path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    os.chmod(path, 0o600)
    return path


def _identity_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "run_id": RUN,
        "model_id": MODEL,
        "source_id": "GFS",
        "cycle_time": datetime(2026, 8, 1, tzinfo=UTC),
        "scenario_id": "forecast_gfs_deterministic",
    }
    row.update(overrides)
    return row


def _captured() -> dict[str, Any]:
    return capture_workload_query(
        basin_version_id=BV,
        segment_id=SEGMENT,
        river_network_version_id=RNV,
        issue_time=ISSUE,
        run_id=RUN,
        model_id=MODEL,
        source="GFS",
    )


def _measure_kwargs(connection: _Connection, captured: dict[str, Any]) -> dict[str, Any]:
    plan = _plan()

    def sql_probe(_index: int) -> dict[str, Any]:
        return {"duration_ms": 10, "explain_json": plan}

    def api_probe(_index: int) -> dict[str, Any]:
        return {"duration_ms": 20, "status": 200, "body_len": 32, "content_digest": "ab" * 32}

    return {
        "connection": connection,
        "origin": "http://127.0.0.1:18080",
        "captured": captured,
        "evidence_kind": "isolated",
        "reviewed_sha": SHA,
        "sql_probe": sql_probe,
        "api_probe": api_probe,
    }


class _IdentityCursor(_Cursor):
    def __init__(self, rows: list[Any] | None = None) -> None:
        super().__init__(rows=rows or [_identity_row()])

    def fetchall(self) -> list[Any]:
        last = self.calls[-1][0] if self.calls else ""
        if "timescaledb_information.chunks" in last:
            return [
                {
                    "hypertable_schema": "hydro",
                    "hypertable_name": "river_timeseries",
                    "chunk_schema": "_timescaledb_internal",
                    "chunk_name": "_hyper_1_1_chunk",
                    "range_start": ISSUE,
                    "range_end": WINDOW_END,
                    "is_compressed": False,
                    "compressed_schema": None,
                    "compressed_name": None,
                }
            ]
        if "hydro.hydro_run" in last:
            return list(self.rows)
        return []


def test_published_query_parameters_are_typed_canonical_and_digest_recomputable() -> None:
    captured = _captured()
    native = captured["parameters"]
    document = measure_workload(**_measure_kwargs(_Connection(_IdentityCursor()), captured))
    decoded = json.loads(encode_evidence(document))
    typed = decoded["query"]["parameters"]
    members = typed["mapping"]
    assert set(typed) == {"mapping"}
    assert typed.get("scenario_tokens") is None
    assert members == {
        "basin_version_id": BV,
        "end_time": {"datetime": WINDOW_END},
        "issue_time": {"datetime": ISSUE},
        "model_id": MODEL,
        "river_network_version_id": RNV,
        "river_segment_id": TS_SEGMENT,
        "run_id": RUN,
        "scenario_ids": {"sequence": ["forecast_gfs_deterministic"]},
        "scenario_tokens": {"sequence": ["forecast_gfs_deterministic"]},
    }
    independent = hashlib.sha256(
        json.dumps(
            {
                "sql": " ".join(str(decoded["query"]["sql"]).split()),
                "parameters": decoded["query"]["parameters"],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert independent == decoded["query"]["query_digest"] == captured["query_digest"]
    assert query_digest(sql=captured["sql"], parameters=native) == captured["query_digest"]

    reordered = {key: native[key] for key in reversed(tuple(native))}
    assert tuple(reordered) != tuple(native)
    reordered_captured = {**captured, "parameters": reordered}
    reordered_decoded = json.loads(
        encode_evidence(measure_workload(**_measure_kwargs(_Connection(_IdentityCursor()), reordered_captured)))
    )
    assert reordered_decoded["query"]["parameters"] == typed
    assert reordered_decoded["query"]["query_digest"] == decoded["query"]["query_digest"]
    assert query_digest(sql=captured["sql"], parameters=reordered) == captured["query_digest"]


def test_open_readonly_connection_preserves_typed_dsn_refusals() -> None:
    def boom(_dsn: str) -> Any:
        raise AssertionError("connect must not run after typed DSN refusal")

    _assert_code(
        lambda: open_readonly_connection(
            "host=10.0.0.9 port=5432 dbname=nhms user=nhms_display_ro password=secret",
            connect=boom,
        ),
        "DSN_HOST_INVALID",
    )
    _assert_code(
        lambda: open_readonly_connection(
            "host=127.0.0.1 port=5432 dbname=nhms user=nhms_ingest_rw password=secret",
            connect=boom,
        ),
        "DSN_ROLE_INVALID",
    )
    _assert_code(
        lambda: open_readonly_connection(
            "host=127.0.0.1 port=5432 dbname=nhms user=nhms_display_ro password=secret options=-cstatement_timeout=1",
            connect=boom,
        ),
        "DSN_OPTIONS_INVALID",
    )
    _assert_code(
        lambda: open_readonly_connection(
            "host=127.0.0.1 port=5432 dbname=nhms user=nhms_display_ro",
            connect=boom,
        ),
        "DSN_INCOMPLETE",
    )
    _assert_code(lambda: open_readonly_connection("not a dsn", connect=boom), "DSN_INVALID")

    class BrokenConnect:
        def __call__(self, _dsn: str) -> Any:
            raise OSError("libpq failed with password=super-secret")

    with pytest.raises(PgdataWorkloadError) as refused:
        open_readonly_connection(
            "host=127.0.0.1 port=5432 dbname=nhms user=nhms_display_ro password=super-secret",
            connect=BrokenConnect(),
        )
    assert refused.value.code == "SQL_CONNECT_FAILED"
    assert "super-secret" not in str(refused.value)
    assert "password=" not in str(refused.value)


def test_invalid_utf8_private_dsn_file_is_typed(tmp_path: Path) -> None:
    parent = _private_dir(tmp_path)
    path = _write_dsn(parent, b"host=127.0.0.1 port=5432 dbname=nhms user=nhms_display_ro password=\xff")
    with pytest.raises(PgdataWorkloadError) as refused:
        read_private_dsn_file(path)
    assert refused.value.code == "DSN_FILE_INVALID"
    assert "\\xff" not in str(refused.value)
    assert "password=" not in str(refused.value)


def test_malformed_api_scalars_use_existing_field_codes() -> None:
    kwargs = _api_kwargs()
    extra_status = {**_ok_series(), "status": []}
    _assert_code(lambda: validate_river_series_response(extra_status, **kwargs), "API_BODY_KEYS_INVALID")
    invalid_status = {**_ok_series(), "status": "invalid"}
    _assert_code(lambda: validate_river_series_response(invalid_status, **kwargs), "API_BODY_KEYS_INVALID")
    wrong_segment = {**_ok_series(), "segment_id": ["not-the-pin"]}
    _assert_code(lambda: validate_river_series_response(wrong_segment, **kwargs), "API_SEGMENT_MISMATCH")
    issue_list = {**_ok_series(), "issue_time": []}
    _assert_code(lambda: validate_river_series_response(issue_list, **kwargs), "API_ISSUE_TIME_MISMATCH")
    cycle_object = json.loads(json.dumps(_ok_series()))
    cycle_object["series"][0]["cycle_time"] = {"t": 1}
    _assert_code(lambda: validate_river_series_response(cycle_object, **kwargs), "API_CYCLE_MISMATCH")
    run_list = json.loads(json.dumps(_ok_series()))
    run_list["series"][0]["run_id"] = []
    _assert_code(
        lambda: validate_river_series_response(run_list, **kwargs, run_id=RUN),
        "API_RUN_MISMATCH",
    )
    model_list = json.loads(json.dumps(_ok_series()))
    model_list["series"][0]["model_id"] = []
    _assert_code(
        lambda: validate_river_series_response(model_list, **kwargs, model_id=MODEL),
        "API_MODEL_MISMATCH",
    )
    source_list = json.loads(json.dumps(_ok_series()))
    source_list["series"][0]["source_id"] = []
    _assert_code(lambda: validate_river_series_response(source_list, **kwargs), "API_SOURCE_INVALID")
    variable_list = json.loads(json.dumps(_ok_series()))
    variable_list["series"][0]["variable"] = ["q_down"]
    _assert_code(lambda: validate_river_series_response(variable_list, **kwargs), "API_VARIABLE_INVALID")
    zero_run = json.loads(json.dumps(_ok_series()))
    zero_run["series"][0]["run_id"] = 0
    _assert_code(
        lambda: validate_river_series_response(zero_run, **kwargs, run_id=RUN),
        "API_RUN_MISMATCH",
    )
    empty_run = json.loads(json.dumps(_ok_series()))
    empty_run["series"][0]["run_id"] = ""
    assert validate_river_series_response(empty_run, **kwargs, run_id=RUN)["point_count"] == 1
    null_run = json.loads(json.dumps(_ok_series()))
    null_run["series"][0]["run_id"] = None
    assert validate_river_series_response(null_run, **kwargs, run_id=RUN)["point_count"] == 1


def test_publish_is_mode_0600_from_first_write_and_does_not_clobber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = _private_dir(tmp_path)
    output = parent / "workload.json"
    document = {"status": "PASS"}
    expected = encode_evidence(document)
    modes_during_write: list[int] = []
    real_write = os.write

    def observe_write(fd: int, data: bytes) -> int:
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode):
            modes_during_write.append(stat.S_IMODE(info.st_mode))
        return real_write(fd, data)

    original_umask = os.umask(0o022)
    try:
        monkeypatch.setattr("packages.common.safe_fs_publication.os.write", observe_write, raising=False)
        monkeypatch.setattr("packages.common.safe_fs.os.write", observe_write)
        publish_measurement_output(output, document)
        first_write_modes = list(modes_during_write)
    finally:
        os.umask(original_umask)

    assert first_write_modes
    assert all(mode == 0o600 for mode in first_write_modes)
    assert output.read_bytes() == expected
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    original = output.read_bytes()
    _assert_code(lambda: publish_measurement_output(output, {"status": "FAIL"}), "OUTPUT_EXISTS")
    assert output.read_bytes() == original == expected
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_pre_publish_write_failure_leaves_no_partial_final(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = _private_dir(tmp_path)
    output = parent / "workload.json"
    document = {"status": "PASS"}
    expected = encode_evidence(document)
    real_write = os.write
    written_prefix = {"count": 0}

    def short_then_enospc(fd: int, data: bytes) -> int:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return real_write(fd, data)
        if written_prefix["count"] == 0:
            prefix = bytes(data[:1])
            if len(prefix) == 0:
                raise OSError(errno.ENOSPC, "No space left on device")
            written = real_write(fd, prefix)
            written_prefix["count"] = written
            return written
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("packages.common.safe_fs_publication.os.write", short_then_enospc, raising=False)
    monkeypatch.setattr("packages.common.safe_fs.os.write", short_then_enospc)
    _assert_code(lambda: publish_measurement_output(output, document), "OUTPUT_PUBLISH_INDETERMINATE")
    assert written_prefix["count"] > 0
    assert not output.exists()
    monkeypatch.undo()
    publish_measurement_output(output, document)
    assert output.read_bytes() == expected
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_pre_publish_fsync_failure_leaves_no_partial_final(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = _private_dir(tmp_path)
    output = parent / "workload.json"
    real_fsync = os.fsync

    def fail_file_fsync(fd: int) -> None:
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode):
            raise OSError(5, "Input/output error")
        real_fsync(fd)

    monkeypatch.setattr("packages.common.safe_fs_publication.os.fsync", fail_file_fsync, raising=False)
    monkeypatch.setattr("packages.common.safe_fs.os.fsync", fail_file_fsync)
    _assert_code(lambda: publish_measurement_output(output, {"status": "PASS"}), "OUTPUT_PUBLISH_INDETERMINATE")
    assert not output.exists()


def test_pre_publish_move_io_failure_unlinks_owned_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = _private_dir(tmp_path)
    output = parent / "workload.json"
    document = {"status": "PASS"}
    expected = encode_evidence(document)
    staged: dict[str, Any] = {}

    def fail_move(parent_dir: Path, name: str, dest_parent: Path, dest_name: str, **_kwargs: Any) -> Path:
        del dest_parent, dest_name
        stage = Path(parent_dir) / name
        info = os.stat(stage)
        staged["path"] = stage
        staged["dev"] = info.st_dev
        staged["ino"] = info.st_ino
        staged["bytes"] = stage.read_bytes()
        raise SafeFilesystemError("exclusive-move io failed", kind="io")

    monkeypatch.setattr(
        "packages.common.node27_pgdata_workload_io.move_regular_file_no_follow_exclusive",
        fail_move,
        raising=False,
    )
    _assert_code(lambda: publish_measurement_output(output, document), "OUTPUT_PUBLISH_FAILED")
    assert not output.exists()
    assert staged["bytes"] == expected
    assert not staged["path"].exists()
    remaining = {path.stat().st_ino for path in parent.iterdir()}
    assert staged["ino"] not in remaining
    monkeypatch.undo()
    publish_measurement_output(output, document)
    assert output.read_bytes() == expected
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_indeterminate_publication_preserves_foreign_and_staged_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = _private_dir(tmp_path)
    output = parent / "workload.json"
    document = {"status": "PASS"}
    expected = encode_evidence(document)
    foreign = parent / "foreign.json"
    foreign.write_text("keep-me", encoding="utf-8")
    os.chmod(foreign, 0o600)
    foreign_info = foreign.stat()
    staged: dict[str, Any] = {}

    def indeterminate_move(parent_dir: Path, name: str, dest_parent: Path, dest_name: str, **_kwargs: Any) -> Path:
        del dest_parent, dest_name
        stage = Path(parent_dir) / name
        info = os.stat(stage)
        staged["path"] = stage
        staged["dev"] = info.st_dev
        staged["ino"] = info.st_ino
        staged["bytes"] = stage.read_bytes()
        raise SafeFilesystemError("exclusive-move proof failed", kind="indeterminate")

    monkeypatch.setattr(
        "packages.common.node27_pgdata_workload_io.move_regular_file_no_follow_exclusive",
        indeterminate_move,
        raising=False,
    )
    _assert_code(
        lambda: publish_measurement_output(output, document),
        "OUTPUT_PUBLISH_INDETERMINATE",
    )
    assert not output.exists()
    assert foreign.read_text(encoding="utf-8") == "keep-me"
    assert (foreign.stat().st_dev, foreign.stat().st_ino) == (foreign_info.st_dev, foreign_info.st_ino)
    stage = staged["path"]
    surviving = os.stat(stage)
    assert (surviving.st_dev, surviving.st_ino) == (staged["dev"], staged["ino"])
    assert stage.read_bytes() == staged["bytes"] == expected


def test_zero_and_two_captured_primaries_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = {
        "basin_version_id": BV,
        "segment_id": SEGMENT,
        "river_network_version_id": RNV,
        "issue_time": ISSUE,
        "run_id": RUN,
        "model_id": MODEL,
        "source": "GFS",
    }

    def empty_series(self: Any, **_kwargs: Any) -> dict[str, Any]:
        del self
        return {"segment_id": SEGMENT, "issue_time": ISSUE, "unit": "m3/s", "series": []}

    monkeypatch.setattr(
        "packages.common.node27_pgdata_workload_query._CaptureForecastStore.forecast_series",
        empty_series,
    )
    _assert_code(lambda: record_explicit_cycle_curve(**kwargs), "QUERY_PRIMARY_INVALID")

    def duplicate_primary(self: Any, **_kwargs: Any) -> dict[str, Any]:
        cursor = self._capture_cursor
        cursor.execute(_named_sql(), _named_parameters())
        cursor.execute(_named_sql(), _named_parameters())
        return {"segment_id": SEGMENT, "issue_time": ISSUE, "unit": "m3/s", "series": []}

    monkeypatch.setattr(
        "packages.common.node27_pgdata_workload_query._CaptureForecastStore.forecast_series",
        duplicate_primary,
    )
    _assert_code(lambda: record_explicit_cycle_curve(**kwargs), "QUERY_PRIMARY_INVALID")


def test_recorded_primary_tolerates_run_not_published_and_refuses_other_store_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = {
        "basin_version_id": BV,
        "segment_id": SEGMENT,
        "river_network_version_id": RNV,
        "issue_time": ISSUE,
        "run_id": RUN,
        "model_id": MODEL,
        "source": "GFS",
    }

    def capture_then_unpublished(self: Any, **_call: Any) -> dict[str, Any]:
        self._capture_cursor.execute(_named_sql(), _named_parameters())
        raise ForecastStoreError(
            status_code=404,
            code="RUN_NOT_PUBLISHED",
            message="No published forecast exists for issue_time 2026-08-01T00:00:00Z.",
        )

    monkeypatch.setattr(
        "packages.common.node27_pgdata_workload_query._CaptureForecastStore.forecast_series",
        capture_then_unpublished,
    )
    recorded = record_explicit_cycle_curve(**kwargs)
    expected_parameters = _named_parameters()
    assert recorded["run_id"] == RUN
    assert recorded["model_id"] == MODEL
    assert recorded["source"] == "GFS"
    assert recorded["scenario"] == "forecast_gfs_deterministic"
    assert recorded["issue_time"] == ISSUE
    assert recorded["parameters"]["run_id"] == expected_parameters["run_id"]
    assert recorded["parameters"]["model_id"] == expected_parameters["model_id"]
    assert recorded["parameters"]["issue_time"] == expected_parameters["issue_time"]
    assert recorded["parameters"]["scenario_tokens"] == expected_parameters["scenario_tokens"]
    assert recorded["parameters"]["scenario_ids"] == expected_parameters["scenario_ids"]
    assert recorded["query_digest"] == query_digest(sql=recorded["sql"], parameters=recorded["parameters"])

    def other_store_error(self: Any, **_kwargs: Any) -> dict[str, Any]:
        del self
        raise ForecastStoreError(
            status_code=500,
            code="SOURCE_NOT_FOUND",
            message="basin missing",
        )

    monkeypatch.setattr(
        "packages.common.node27_pgdata_workload_query._CaptureForecastStore.forecast_series",
        other_store_error,
    )
    _assert_code(lambda: record_explicit_cycle_curve(**kwargs), "QUERY_RECORD_FAILED")


def test_authoritative_identity_refuses_two_rows_and_distinct_mismatches() -> None:
    captured = _captured()
    two_rows = _Connection(_IdentityCursor(rows=[_identity_row(), _identity_row(run_id="other")]))
    _assert_code(lambda: prove_authoritative_run_identity(two_rows, captured=captured), "SQL_IDENTITY_MISSING")
    run_mismatch = _Connection(_IdentityCursor(rows=[_identity_row(run_id="other-run")]))
    _assert_code(lambda: prove_authoritative_run_identity(run_mismatch, captured=captured), "SQL_IDENTITY_MISMATCH")
    model_mismatch = _Connection(_IdentityCursor(rows=[_identity_row(model_id="other-model")]))
    _assert_code(lambda: prove_authoritative_run_identity(model_mismatch, captured=captured), "SQL_IDENTITY_MISMATCH")
    source_mismatch = _Connection(_IdentityCursor(rows=[_identity_row(source_id="IFS")]))
    _assert_code(lambda: prove_authoritative_run_identity(source_mismatch, captured=captured), "SQL_IDENTITY_MISMATCH")
    scenario_mismatch = _Connection(
        _IdentityCursor(rows=[_identity_row(scenario_id="forecast_ifs_deterministic")])
    )
    _assert_code(
        lambda: prove_authoritative_run_identity(scenario_mismatch, captured=captured),
        "SQL_IDENTITY_MISMATCH",
    )
