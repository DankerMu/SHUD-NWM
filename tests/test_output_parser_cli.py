"""CLI-facing tests for ``workers.output_parser.cli``.

Parallel of ``tests/test_forcing_producer_cli.py`` for the parser side of
the compressed-chunk write guard. The parser's ``parse_run`` propagates
the guard exception un-wrapped, so these tests lock the CLI arms as
reachable and assert the caller-observable stderr prefix + exit code
contract.

#1785 split the single base-class arm into two, subclass first, at all
four parser-CLI arms (``shud-output`` and ``parse`` subcommands × the
``_click_main`` and ``_argparse_main`` legs — ``main()`` prefers the click
leg whenever click imports, so the argparse leg is exercised by calling it
directly):

* subclass ``CompressedChunkWriteError`` -> ``OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED:``
* base ``CompressedChunkGuardError`` -> ``OUTPUT_PARSE_COMPRESSED_CHUNK_GUARD_FAILED:``

Click leg exits via ``SystemExit(1)``; argparse leg returns 1.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

import pytest

from packages.common.timescale_write_guard import (
    CompressedChunkGuardError,
    CompressedChunkWriteError,
)
from workers.output_parser import cli as output_cli
from workers.output_parser.parser import OutputParsingError

SUBCOMMANDS = ["shud-output", "parse"]


def _argv(subcommand: str) -> list[str]:
    return [subcommand, "--run-id", "run_001"]


def _write_error() -> CompressedChunkWriteError:
    return CompressedChunkWriteError(
        chunk_schema="_timescaledb_internal",
        chunk_name="_hyper_1_1_chunk",
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
    )


def _guard_error() -> CompressedChunkGuardError:
    return CompressedChunkGuardError(
        "Compressed-chunk guard lookup failed for hydro.river_timeseries: "
        "canceling statement due to statement timeout"
    )


class _RaisingParser:
    """Fake ``OutputParser`` whose ``parse_run`` re-raises a caller-set error."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def parse_run(self, _run_id: str) -> Any:
        raise self._error


def _patch_output_parser(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    """Stub ``OutputParser.from_env`` so ``cli._parse`` uses our fake."""

    def _from_env() -> _RaisingParser:
        return _RaisingParser(error)

    monkeypatch.setattr(output_cli.OutputParser, "from_env", _from_env)


@pytest.mark.parametrize("subcommand", SUBCOMMANDS)
def test_output_click_leg_emits_compressed_chunk_blocked_prefix_and_exit_1(
    monkeypatch: pytest.MonkeyPatch, subcommand: str
) -> None:
    """#1785 arms 5-6/8 (subclass, click leg): a real compressed-chunk hit
    keeps the ``OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED:`` prefix and exit 1."""
    _patch_output_parser(monkeypatch, _write_error())

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with (
        pytest.raises(SystemExit) as exc_info,
        redirect_stdout(stdout_buf),
        redirect_stderr(stderr_buf),
    ):
        output_cli._click_main(_argv(subcommand))
    assert exc_info.value.code == 1
    assert "OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED:" in stderr_buf.getvalue()
    assert "GUARD_FAILED" not in stderr_buf.getvalue()


@pytest.mark.parametrize("subcommand", SUBCOMMANDS)
def test_output_click_leg_emits_guard_failed_prefix_and_exit_1(
    monkeypatch: pytest.MonkeyPatch, subcommand: str
) -> None:
    """#1785 arms 5-6/8 (base class, click leg): a guard-internal failure gets
    the ``OUTPUT_PARSE_COMPRESSED_CHUNK_GUARD_FAILED:`` prefix, never the
    ``_BLOCKED:`` one."""
    _patch_output_parser(monkeypatch, _guard_error())

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with (
        pytest.raises(SystemExit) as exc_info,
        redirect_stdout(stdout_buf),
        redirect_stderr(stderr_buf),
    ):
        output_cli._click_main(_argv(subcommand))
    assert exc_info.value.code == 1
    assert "OUTPUT_PARSE_COMPRESSED_CHUNK_GUARD_FAILED:" in stderr_buf.getvalue()
    assert "OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED:" not in stderr_buf.getvalue()


@pytest.mark.parametrize("subcommand", SUBCOMMANDS)
def test_output_argparse_leg_emits_compressed_chunk_blocked_prefix_and_rc_1(
    monkeypatch: pytest.MonkeyPatch, subcommand: str
) -> None:
    """#1785 arms 7-8/8 (subclass, argparse leg): the click-less fallback leg
    keeps the ``_BLOCKED:`` prefix and returns 1."""
    _patch_output_parser(monkeypatch, _write_error())

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        rc = output_cli._argparse_main(_argv(subcommand))
    assert rc == 1
    assert "OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED:" in stderr_buf.getvalue()
    assert "GUARD_FAILED" not in stderr_buf.getvalue()


@pytest.mark.parametrize("subcommand", SUBCOMMANDS)
def test_output_argparse_leg_emits_guard_failed_prefix_and_rc_1(
    monkeypatch: pytest.MonkeyPatch, subcommand: str
) -> None:
    """#1785 arms 7-8/8 (base class, argparse leg): the click-less fallback leg
    reports the guard failure as such and returns 1."""
    _patch_output_parser(monkeypatch, _guard_error())

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        rc = output_cli._argparse_main(_argv(subcommand))
    assert rc == 1
    assert "OUTPUT_PARSE_COMPRESSED_CHUNK_GUARD_FAILED:" in stderr_buf.getvalue()
    assert "OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED:" not in stderr_buf.getvalue()


def test_output_parser_cli_preserves_baseline_error_shape_on_output_parsing_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Baseline behavior: ``OutputParsingError`` still exits non-zero.

    The guard arms must not shadow the pre-existing error propagation
    contract (routes via ``{error_code}: {message}``). Neither
    compressed-chunk prefix may appear.
    """
    _patch_output_parser(
        monkeypatch,
        OutputParsingError("RIVQDOWN_NOT_FOUND", "No .rivqdown file found"),
    )

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with (
        pytest.raises(SystemExit) as exc_info,
        redirect_stdout(stdout_buf),
        redirect_stderr(stderr_buf),
    ):
        output_cli.main(_argv("shud-output"))
    assert exc_info.value.code == 1
    assert "RIVQDOWN_NOT_FOUND" in stderr_buf.getvalue()
    assert "OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED:" not in stderr_buf.getvalue()
    assert "OUTPUT_PARSE_COMPRESSED_CHUNK_GUARD_FAILED:" not in stderr_buf.getvalue()
