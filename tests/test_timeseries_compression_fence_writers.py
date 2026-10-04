"""#2713: every ingest writer tries the compression fence first, and busy is not failure.

For each production writer of a compressed hypertable this pins, at its public
seam:

* the shared fence try is the FIRST statement of the write transaction
  (recording fake connection, statement order asserted);
* a busy fence rolls the transaction back before any other statement, raises
  ``IngestFenceBusy`` / reports the writer's own busy code, and does NOT mark
  the run or cycle failed;
* the node-27 ingest tick classifies the busy outcome as ``skipped`` and stays
  green. The river case is driven across the real subprocess contract: the
  real ``workers.output_parser.cli`` runs in-process and its rc + stderr are
  what the tick sees.

Fakes stand only at the driver and subprocess boundaries; the recording
connections are the existing suites' own doubles.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

import scripts.node27_autopipeline as autopipe
from packages.common import forcing_domain_handoff_apply as apply_module
from packages.common.timeseries_compression_fence import IngestFenceBusy
from tests.test_forcing_domain_handoff_apply import _fake_execute_values, _parse_complete
from tests.test_forcing_domain_handoff_apply import _FakeConnection as _ApplyConnection
from tests.test_forcing_producer import _build_producer, _build_repository
from tests.test_node27_autopipeline_handoff import (
    RUN_A,
    _command_kinds,
    _prepare_autopipe,
    _run_main,
    _set_initial_state,
)
from tests.test_output_parser import _build_parser
from tests.test_timescale_write_guard_wired import (
    _forcing_rows,
    _install_fake_psycopg2,
    _output_parser_repository,
    _patch_parser_execute_values,
    _RecordingConnection,
    _river_identity_kwargs,
    _river_rows,
)
from workers.forcing_producer import cli as forcing_cli
from workers.forcing_producer.store import PsycopgForcingRepository
from workers.output_parser import cli as output_cli
from workers.output_parser import parser as parser_module
from workers.output_parser.parser import PsycopgOutputParserRepository

_FENCE_TRY = "SELECT pg_try_advisory_xact_lock_shared(%s, %s)"
# Literal keys (independent CRC-32 of the hypertable names; see the fence suite).
_RIVER_KEY = (2713, -1964848284)
_FORCING_KEY = (2713, 431776087)
# Literal wire codes, not the constants: the tick keys on these exact values.
_PARSE_BUSY = "OUTPUT_PARSE_COMPRESSION_FENCE_BUSY"
_APPLY_BUSY = "HANDOFF_APPLY_COMPRESSION_FENCE_BUSY"
_PRODUCE_BUSY = "FORCING_PRODUCE_COMPRESSION_FENCE_BUSY"


# ---------------------------------------------------------------------------
# Output parser: upsert_river_timeseries
# ---------------------------------------------------------------------------


def test_river_replace_tries_the_fence_before_the_run_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_parser_execute_values(monkeypatch)
    connection = _RecordingConnection()

    _output_parser_repository(connection).upsert_river_timeseries(
        _river_rows(), batch_size=2, **_river_identity_kwargs()
    )

    assert connection.executions[0] == (_FENCE_TRY, _RIVER_KEY)
    assert connection.executions[1][0] == "SELECT 1 FROM hydro.hydro_run WHERE run_key = %s FOR UPDATE"


def test_a_busy_fence_rolls_the_river_replace_back_before_any_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    inserts = _patch_parser_execute_values(monkeypatch)
    connection = _RecordingConnection(ingest_fence_free=False)
    repository = PsycopgOutputParserRepository(database_url="postgres://unused")
    monkeypatch.setattr(PsycopgOutputParserRepository, "_connect", lambda _self: connection)

    with pytest.raises(IngestFenceBusy) as excinfo:
        repository.upsert_river_timeseries(_river_rows(), batch_size=2, **_river_identity_kwargs())

    assert excinfo.value.hypertable == "hydro.river_timeseries"
    assert connection.executions == [(_FENCE_TRY, _RIVER_KEY)]
    assert inserts == []
    assert (connection.commits, connection.rollbacks, connection.closed) == (0, 1, True)


def _write_rivqdown(store: Any) -> None:
    store.write_bytes_atomic(
        "runs/run_001/output/demo.rivqdown",
        b"time,seg_a,seg_b\n2026-05-01T00:00:00Z,86400,172800\n",
    )


def _busy_upsert(*_args: Any, **_kwargs: Any) -> None:
    raise IngestFenceBusy("hydro.river_timeseries")


def test_a_busy_parse_leaves_the_run_status_untouched(tmp_path: Path) -> None:
    store, parser, repository = _build_parser(tmp_path)
    _write_rivqdown(store)
    repository.upsert_river_timeseries = _busy_upsert  # type: ignore[method-assign]

    with pytest.raises(IngestFenceBusy):
        parser.parse_run("run_001")

    # Neither 'failed' (no mark_run_failed) nor 'parsed': the next tick re-parses.
    assert repository.statuses == []
    assert repository.failures == []
    assert repository.qc_results == []


def _patch_busy_parser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    store, parser, repository = _build_parser(tmp_path)
    _write_rivqdown(store)
    repository.upsert_river_timeseries = _busy_upsert  # type: ignore[method-assign]
    monkeypatch.setattr(output_cli.OutputParser, "from_env", lambda: parser)
    return repository


def _run_parser_cli(argv: list[str], *, leg: str) -> tuple[int, str]:
    stderr, stdout = io.StringIO(), io.StringIO()
    with redirect_stderr(stderr), redirect_stdout(stdout):
        if leg == "argparse":
            rc = output_cli._argparse_main(argv)
        else:
            try:
                rc = output_cli._click_main(argv)
            except SystemExit as exit_:
                rc = int(exit_.code or 0)
    return rc, stderr.getvalue()


@pytest.mark.parametrize("leg", ["click", "argparse"])
@pytest.mark.parametrize("subcommand", ["shud-output", "parse"])
def test_parser_cli_prints_the_busy_code_and_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, leg: str, subcommand: str
) -> None:
    repository = _patch_busy_parser(monkeypatch, tmp_path)

    rc, stderr = _run_parser_cli([subcommand, "--run-id", "run_001"], leg=leg)

    assert rc == 1
    assert stderr.startswith(f"{_PARSE_BUSY}: compression fence on hydro.river_timeseries")
    assert "Traceback" not in stderr
    assert repository.statuses == []


def test_the_tick_reads_the_parser_busy_code_byte_for_byte() -> None:
    assert autopipe.REASON_PARSE_COMPRESSION_FENCE_BUSY == _PARSE_BUSY
    assert parser_module.COMPRESSION_FENCE_BUSY_ERROR_CODE == _PARSE_BUSY


@pytest.mark.parametrize(
    ("stderr", "busy"),
    [
        (f"{_PARSE_BUSY}: compression fence on hydro.river_timeseries is held\n", True),
        (f"UserWarning: x\n{_PARSE_BUSY}: held\n", True),
        (f"Traceback (most recent call last):\n{_PARSE_BUSY}: held\n", False),
        ("OUTPUT_PARSE_DB_ERROR: timeout\n", False),
        ("MODEL_RIVER_FILE_MALFORMED: line 7\n", False),
        ("", False),
    ],
    ids=["bare", "after-warning", "traceback", "db-error", "deterministic", "empty"],
)
def test_only_the_cli_busy_line_reads_as_busy(stderr: str, busy: bool) -> None:
    assert autopipe._parse_fence_busy(stderr) is busy


def test_a_busy_parse_finishes_the_tick_green_as_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end through the subprocess contract: the REAL parser CLI runs
    in-process, and only its rc + stderr reach ``_process_run``."""
    repository = _patch_busy_parser(monkeypatch, tmp_path / "parser")

    def command_handler(argv: list[str], _env: dict[str, str]) -> tuple[int, str, str]:
        command = " ".join(argv)
        if "node27_ingest_run.py" in command:
            return 0, '{"status": "registered"}\n', ""
        if "workers.output_parser.cli" in command:
            index = argv.index("workers.output_parser.cli")
            subcommand, flag, run_id = argv[index + 1 :]
            assert (subcommand, flag, run_id) == ("parse", "--run-id", RUN_A)
            # The parser fixture is keyed to run_001; the busy path never
            # depends on which run it is.
            rc, stderr = _run_parser_cli([subcommand, flag, "run_001"], leg="click")
            return rc, "", stderr
        raise AssertionError(f"unexpected command: {argv}")

    object_store_root, calls, published_calls = _prepare_autopipe(
        monkeypatch, tmp_path, runs={RUN_A: True}, command_handler=command_handler
    )
    _set_initial_state(object_store_root, RUN_A, "state-a")
    declines: list[Any] = []
    monkeypatch.setattr(autopipe, "_record_recompute_decline", lambda *_a, **kw: declines.append(kw))

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 0
    assert summary["runs"]["failed"] == 0
    assert summary["runs"]["skipped_runs"] == [{"run_id": RUN_A, "reason": _PARSE_BUSY}]
    detail = summary["runs"]["details"][0]
    assert (detail["outcome"], detail["stage"]) == ("skipped", "parse")
    assert declines == []
    assert _command_kinds(calls) == ["register", "parse"]  # no coverage refresh
    assert repository.statuses == []  # the run was never marked failed


# ---------------------------------------------------------------------------
# Forcing handoff apply: _apply_with_cursor
# ---------------------------------------------------------------------------


def test_handoff_apply_tries_the_fence_before_the_routing_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apply_module, "execute_values", _fake_execute_values)
    connection = _ApplyConnection()

    report = apply_module.apply_forcing_domain_handoff(_parse_complete(), connection=connection)

    assert report["status"] == "applied"
    statements = [(statement, params) for _mode, statement, params in connection.executions]
    assert statements[0] == (_FENCE_TRY, _FORCING_KEY)
    assert "timeseries_store" in statements[1][0]


def test_a_busy_fence_rolls_the_owned_apply_back_untouched() -> None:
    connection = _ApplyConnection(ingest_fence_free=False)

    report = apply_module.apply_forcing_domain_handoff(_parse_complete(), connection=connection)

    assert report["status"] == "failed"
    assert report["writes_performed"] is False
    [reason] = report["unavailable_reasons"]
    assert reason["code"] == _APPLY_BUSY == apply_module.REASON_APPLY_COMPRESSION_FENCE_BUSY
    assert reason["exception_type"] == "IngestFenceBusy"
    assert [statement for _mode, statement, _params in connection.executions] == [_FENCE_TRY]
    assert (connection.commits, connection.rollbacks) == (0, 1)
    assert all(rows == [] for rows in connection.tables.values())


def test_a_busy_fence_on_the_caller_owned_path_rolls_back_to_the_savepoint() -> None:
    connection = _ApplyConnection(ingest_fence_free=False)
    cursor = connection.cursor()

    report = apply_module.apply_forcing_domain_handoff(_parse_complete(), cursor=cursor)

    assert report["unavailable_reasons"][0]["code"] == _APPLY_BUSY
    statements = [statement.lower() for _mode, statement, _params in connection.executions]
    assert statements[0].startswith("savepoint ")
    assert statements[1] == _FENCE_TRY.lower()
    assert statements[2].startswith("rollback to savepoint ")
    assert (connection.commits, connection.rollbacks) == (0, 0)  # the caller owns the transaction


def test_a_busy_forcing_apply_finishes_the_tick_green_as_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    busy_report = apply_module.apply_forcing_domain_handoff(
        _parse_complete(), connection=_ApplyConnection(ingest_fence_free=False)
    )
    object_store_root, calls, published_calls = _prepare_autopipe(
        monkeypatch, tmp_path, runs={RUN_A: True}, apply_reports={RUN_A: busy_report}
    )
    _set_initial_state(object_store_root, RUN_A, "state-a")
    declines: list[Any] = []
    monkeypatch.setattr(autopipe, "_record_recompute_decline", lambda *_a, **kw: declines.append(kw))

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 0
    assert summary["runs"]["failed"] == 0
    assert summary["runs"]["skipped_runs"] == [{"run_id": RUN_A, "reason": _APPLY_BUSY}]
    detail = summary["runs"]["details"][0]
    assert detail["outcome"] == "skipped"
    assert detail["forcing_stage"]["reason_codes"] == [_APPLY_BUSY]
    assert declines == []
    assert _command_kinds(calls) == ["register"]  # not parsed this tick; retried next tick
    assert published_calls == []


# ---------------------------------------------------------------------------
# Forcing producer store: replace_forcing_timeseries
# ---------------------------------------------------------------------------


def test_forcing_store_tries_the_fence_before_the_routing_read(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _RecordingConnection()
    _install_fake_psycopg2(monkeypatch, connection)

    PsycopgForcingRepository(database_url="postgres://unused").replace_forcing_timeseries("fv_a", _forcing_rows())

    assert connection.executions[0] == (_FENCE_TRY, _FORCING_KEY)
    assert "timeseries_store" in connection.executions[1][0]
    assert connection.commits == 1


def test_a_busy_fence_rolls_the_forcing_store_replace_back(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _RecordingConnection(ingest_fence_free=False)
    inserts = _install_fake_psycopg2(monkeypatch, connection)

    with pytest.raises(IngestFenceBusy):
        PsycopgForcingRepository(database_url="postgres://unused").replace_forcing_timeseries(
            "fv_a", _forcing_rows()
        )

    assert connection.executions == [(_FENCE_TRY, _FORCING_KEY)]
    assert inserts == []
    assert (connection.commits, connection.rollbacks) == (0, 1)


def test_a_busy_forcing_replace_does_not_mark_the_cycle_failed(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path)
    producer = _build_producer(tmp_path, repository, store)

    def _busy(*_args: Any, **_kwargs: Any) -> None:
        raise IngestFenceBusy("met.forcing_station_timeseries")

    repository.replace_forcing_timeseries = _busy  # type: ignore[method-assign]

    with pytest.raises(IngestFenceBusy):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert not any(update.get("status") == "failed_forcing" for update in repository.cycle_updates)


class _BusyProducer:
    def produce(self, **_kwargs: Any) -> Any:
        raise IngestFenceBusy("met.forcing_station_timeseries")


@pytest.mark.parametrize("leg", ["click", "argparse"])
def test_forcing_cli_prints_the_busy_code_and_exits_non_zero(monkeypatch: pytest.MonkeyPatch, leg: str) -> None:
    monkeypatch.setattr(forcing_cli.ForcingProducer, "from_env", lambda: _BusyProducer())
    argv = ["produce", "--source-id", "gfs", "--cycle-time", "2026050700", "--model-id", "demo_model"]
    stderr = io.StringIO()
    with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
        if leg == "argparse":
            rc = forcing_cli._argparse_main(argv)
        else:
            with pytest.raises(SystemExit) as exit_:
                forcing_cli._click_main(argv)
            rc = exit_.value.code

    assert rc == 1
    assert stderr.getvalue().startswith(f"{_PRODUCE_BUSY}: compression fence on met.forcing_station_timeseries")
