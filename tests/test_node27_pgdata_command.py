"""Real child-process bounds at the PGDATA command owner."""

import sys
import time

import pytest

from packages.common.node27_pgdata_command import CommandError, run_bounded_command


def test_bounded_collector_caps_real_stdout_child() -> None:
    started = time.monotonic()
    with pytest.raises(CommandError, match="byte ceiling"):
        run_bounded_command(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000); sys.stdout.flush()"],
            timeout=5,
        )
    assert time.monotonic() - started < 4


def test_bounded_collector_caps_real_stderr_child() -> None:
    started = time.monotonic()
    with pytest.raises(CommandError, match="byte ceiling"):
        run_bounded_command(
            [sys.executable, "-c", "import sys; sys.stderr.write('e' * 200000); sys.stderr.flush()"],
            timeout=5,
        )
    assert time.monotonic() - started < 4


def test_bounded_collector_kills_hanging_child() -> None:
    started = time.monotonic()
    with pytest.raises(CommandError, match="timed out"):
        run_bounded_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
    assert time.monotonic() - started < 4
