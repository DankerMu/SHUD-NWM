"""CLI-facing tests for ``workers.forcing_producer.cli``.

Closes the F-series caller-observable contract for the compressed-chunk
write guard on the forcing side. The CLI's dedicated
``except CompressedChunkGuardError`` arm was previously dead code because
``ForcingProducer.produce()`` wrapped every ``Exception`` (including the
guard error) into ``ForcingProductionError`` before the CLI could see it.
R2/F1 added a dedicated ``except CompressedChunkGuardError`` in
``produce()`` BEFORE the generic ``except Exception``, propagating the
guard error un-wrapped so the CLI arm becomes reachable.

Tests:

* Test 1: guard error propagates to CLI stderr with the
  ``FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED:`` prefix and exit code 1.
* Test 2: ``ForcingProductionError`` propagates with non-zero exit so
  baseline behavior is preserved.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

import pytest

from packages.common.timescale_write_guard import CompressedChunkGuardError
from workers.forcing_producer import cli as forcing_cli
from workers.forcing_producer.producer import ForcingProductionError


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


def test_forcing_cli_emits_compressed_chunk_blocked_prefix_and_exit_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard error from ``produce`` reaches the CLI arm intact.

    Asserts:
    - stderr contains ``"FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED:"``.
    - CLI exit code is 1.
    """
    _patch_forcing_producer(
        monkeypatch,
        CompressedChunkGuardError(
            "Reingest targets compressed chunk _timescaledb_internal._hyper_1_1_chunk"
        ),
    )

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    argv = [
        "produce",
        "--source-id",
        "gfs",
        "--cycle-time",
        "2026050700",
        "--model-id",
        "demo_model",
    ]
    with (
        pytest.raises(SystemExit) as exc_info,
        redirect_stdout(stdout_buf),
        redirect_stderr(stderr_buf),
    ):
        forcing_cli.main(argv)
    assert exc_info.value.code == 1
    assert "FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED:" in stderr_buf.getvalue()


def test_forcing_cli_preserves_baseline_error_shape_on_forcing_production_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Baseline behavior: ``ForcingProductionError`` is NOT mislabeled.

    The new dedicated guard arm must not shadow the pre-existing error
    propagation contract. A ``ForcingProductionError`` is not a subclass
    of ``CompressedChunkGuardError`` (they are peer ``Exception``
    subclasses), so ordering does not accidentally swallow the baseline
    error. Click's standalone mode does not catch application exceptions,
    so ``ForcingProductionError`` propagates out of ``cli.main``. The
    stderr surface MUST NOT carry the compressed-chunk prefix — the
    routing shape is the observable contract under test.
    """
    _patch_forcing_producer(
        monkeypatch,
        ForcingProductionError("model instance not found"),
    )

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    argv = [
        "produce",
        "--source-id",
        "gfs",
        "--cycle-time",
        "2026050700",
        "--model-id",
        "demo_model",
    ]
    with (
        pytest.raises((ForcingProductionError, SystemExit)) as exc_info,
        redirect_stdout(stdout_buf),
        redirect_stderr(stderr_buf),
    ):
        forcing_cli.main(argv)
    # If click surfaces a SystemExit, exit code must not be 0.
    if isinstance(exc_info.value, SystemExit):
        assert exc_info.value.code not in (None, 0)
    # Compressed-chunk prefix MUST NOT appear on the baseline path.
    assert (
        "FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED:" not in stderr_buf.getvalue()
    )
