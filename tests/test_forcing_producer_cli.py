"""CLI-facing tests for ``workers.forcing_producer.cli``.

Closes the F-series caller-observable contract for the compressed-chunk
write guard on the forcing side. The CLI's dedicated guard arm was
previously dead code because ``ForcingProducer.produce()`` wrapped every
``Exception`` (including the guard error) into ``ForcingProductionError``
before the CLI could see it. R2/F1 added a dedicated guard arm in
``produce()`` BEFORE the generic ``except Exception``, propagating the
guard error un-wrapped so the CLI arm becomes reachable.

#1785 then split that single arm in two, subclass first, at both CLI legs
(``_click_main`` and ``_argparse_main`` — ``main()`` prefers the click leg
whenever click imports, so the argparse leg is exercised by calling it
directly):

* subclass ``CompressedChunkWriteError`` -> stderr prefix
  ``FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED:`` (a chunk really was there);
* base ``CompressedChunkGuardError`` -> stderr prefix
  ``FORCING_PRODUCE_COMPRESSED_CHUNK_GUARD_FAILED:`` (the guard could not
  certify the batch; nothing to decompress).

Both exit non-zero (click leg: ``SystemExit(1)``; argparse leg: return
code 1). A baseline test pins that ``ForcingProductionError`` still keeps
its own routing shape.
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
from workers.forcing_producer import cli as forcing_cli
from workers.forcing_producer.producer import ForcingProductionError

_ARGV = [
    "produce",
    "--source-id",
    "gfs",
    "--cycle-time",
    "2026050700",
    "--model-id",
    "demo_model",
]


def _write_error() -> CompressedChunkWriteError:
    return CompressedChunkWriteError(
        chunk_schema="_timescaledb_internal",
        chunk_name="_hyper_2_5_chunk",
        hypertable_schema="met",
        hypertable_name="forcing_station_timeseries",
    )


def _guard_error() -> CompressedChunkGuardError:
    return CompressedChunkGuardError(
        "Compressed-chunk guard lookup failed for met.forcing_station_timeseries: "
        "canceling statement due to statement timeout"
    )


class _RaisingProducer:
    """Fake ``ForcingProducer`` whose ``produce`` re-raises a caller-set error."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def produce(self, **_kwargs: Any) -> Any:
        raise self._error


def _patch_forcing_producer(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    """Stub ``ForcingProducer.from_env`` so ``cli._produce`` uses our fake."""

    def _from_env() -> _RaisingProducer:
        return _RaisingProducer(error)

    monkeypatch.setattr(forcing_cli.ForcingProducer, "from_env", _from_env)


def test_forcing_click_leg_emits_compressed_chunk_blocked_prefix_and_exit_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1785 arm 3/8 (subclass, click leg): a real compressed-chunk hit keeps
    the ``FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED:`` prefix and exit 1."""
    _patch_forcing_producer(monkeypatch, _write_error())

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with (
        pytest.raises(SystemExit) as exc_info,
        redirect_stdout(stdout_buf),
        redirect_stderr(stderr_buf),
    ):
        forcing_cli._click_main(_ARGV)
    assert exc_info.value.code == 1
    assert "FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED:" in stderr_buf.getvalue()
    assert "GUARD_FAILED" not in stderr_buf.getvalue()


def test_forcing_click_leg_emits_guard_failed_prefix_and_exit_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1785 arm 3/8 (base class, click leg): a guard-internal failure gets
    the ``FORCING_PRODUCE_COMPRESSED_CHUNK_GUARD_FAILED:`` prefix, never the
    ``_BLOCKED:`` one."""
    _patch_forcing_producer(monkeypatch, _guard_error())

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with (
        pytest.raises(SystemExit) as exc_info,
        redirect_stdout(stdout_buf),
        redirect_stderr(stderr_buf),
    ):
        forcing_cli._click_main(_ARGV)
    assert exc_info.value.code == 1
    assert "FORCING_PRODUCE_COMPRESSED_CHUNK_GUARD_FAILED:" in stderr_buf.getvalue()
    assert "FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED:" not in stderr_buf.getvalue()


def test_forcing_argparse_leg_emits_compressed_chunk_blocked_prefix_and_rc_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1785 arm 4/8 (subclass, argparse leg): the click-less fallback leg
    keeps the ``_BLOCKED:`` prefix and returns 1."""
    _patch_forcing_producer(monkeypatch, _write_error())

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        rc = forcing_cli._argparse_main(_ARGV)
    assert rc == 1
    assert "FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED:" in stderr_buf.getvalue()
    assert "GUARD_FAILED" not in stderr_buf.getvalue()


def test_forcing_argparse_leg_emits_guard_failed_prefix_and_rc_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1785 arm 4/8 (base class, argparse leg): the click-less fallback leg
    reports the guard failure as such and returns 1."""
    _patch_forcing_producer(monkeypatch, _guard_error())

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        rc = forcing_cli._argparse_main(_ARGV)
    assert rc == 1
    assert "FORCING_PRODUCE_COMPRESSED_CHUNK_GUARD_FAILED:" in stderr_buf.getvalue()
    assert "FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED:" not in stderr_buf.getvalue()


def test_forcing_cli_preserves_baseline_error_shape_on_forcing_production_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Baseline behavior: ``ForcingProductionError`` is NOT mislabeled.

    The dedicated guard arms must not shadow the pre-existing error
    propagation contract. A ``ForcingProductionError`` is not a subclass
    of ``CompressedChunkGuardError`` (they are peer ``Exception``
    subclasses), so ordering does not accidentally swallow the baseline
    error. Click's standalone mode does not catch application exceptions,
    so ``ForcingProductionError`` propagates out of ``cli.main``. The
    stderr surface MUST NOT carry either compressed-chunk prefix — the
    routing shape is the observable contract under test.
    """
    _patch_forcing_producer(
        monkeypatch,
        ForcingProductionError("model instance not found"),
    )

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with (
        pytest.raises((ForcingProductionError, SystemExit)) as exc_info,
        redirect_stdout(stdout_buf),
        redirect_stderr(stderr_buf),
    ):
        forcing_cli.main(_ARGV)
    # If click surfaces a SystemExit, exit code must not be 0.
    if isinstance(exc_info.value, SystemExit):
        assert exc_info.value.code not in (None, 0)
    # Neither compressed-chunk prefix may appear on the baseline path.
    assert "FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED:" not in stderr_buf.getvalue()
    assert (
        "FORCING_PRODUCE_COMPRESSED_CHUNK_GUARD_FAILED:" not in stderr_buf.getvalue()
    )
