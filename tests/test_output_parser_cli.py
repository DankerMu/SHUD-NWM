"""CLI-facing tests for ``workers.output_parser.cli``.

Parallel of ``tests/test_forcing_producer_cli.py`` for the parser side of
the compressed-chunk write guard. The parser's ``parse_run`` already
propagates ``CompressedChunkGuardError`` un-wrapped, so this test locks
the CLI arm as reachable and asserts the caller-observable stderr
prefix + exit code contract:

* stderr contains ``"OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED:"``.
* exit code == 1.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

import pytest

from packages.common.timescale_write_guard import CompressedChunkGuardError
from workers.output_parser import cli as output_cli
from workers.output_parser.parser import OutputParsingError


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


def test_output_parser_cli_emits_compressed_chunk_blocked_prefix_and_exit_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard error from ``parse_run`` reaches the CLI arm intact."""
    _patch_output_parser(
        monkeypatch,
        CompressedChunkGuardError(
            "Reingest targets compressed chunk _timescaledb_internal._hyper_1_1_chunk"
        ),
    )

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    argv = ["shud-output", "--run-id", "run_001"]
    with (
        pytest.raises(SystemExit) as exc_info,
        redirect_stdout(stdout_buf),
        redirect_stderr(stderr_buf),
    ):
        output_cli.main(argv)
    assert exc_info.value.code == 1
    assert "OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED:" in stderr_buf.getvalue()


def test_output_parser_cli_preserves_baseline_error_shape_on_output_parsing_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Baseline behavior: ``OutputParsingError`` still exits non-zero.

    The new guard arm must not shadow the pre-existing error propagation
    contract (routes via ``{error_code}: {message}``). Compressed-chunk
    prefix MUST NOT appear.
    """
    _patch_output_parser(
        monkeypatch,
        OutputParsingError("RIVQDOWN_NOT_FOUND", "No .rivqdown file found"),
    )

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    argv = ["shud-output", "--run-id", "run_001"]
    with (
        pytest.raises(SystemExit) as exc_info,
        redirect_stdout(stdout_buf),
        redirect_stderr(stderr_buf),
    ):
        output_cli.main(argv)
    assert exc_info.value.code == 1
    assert "RIVQDOWN_NOT_FOUND" in stderr_buf.getvalue()
    assert (
        "OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED:" not in stderr_buf.getvalue()
    )
