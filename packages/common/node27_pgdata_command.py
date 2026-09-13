"""Bounded argv subprocess execution for PGDATA and retained host consumers."""

from __future__ import annotations

import os
import select
import subprocess
import time
from collections.abc import Callable, Sequence
from typing import Any

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class CommandError(RuntimeError):
    """A bounded command is unavailable, timed out, or exceeds its output limit."""


def _kill(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=1)
    except Exception:
        pass


def _close_pipes(process: subprocess.Popen[bytes]) -> None:
    for pipe in (process.stdout, process.stderr):
        if pipe is None:
            continue
        try:
            pipe.close()
        except OSError:
            pass


def run_bounded_command(
    argv: Sequence[str],
    *,
    timeout: int = 5,
    max_bytes: int = 64 * 1024,
    runner: CommandRunner | None = None,
) -> subprocess.CompletedProcess[str]:
    if runner is not None:
        try:
            result = runner(list(argv), check=False, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise CommandError("target inspector timed out") from error
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        if len(stdout.encode("utf-8")) > max_bytes or len(stderr.encode("utf-8")) > max_bytes:
            raise CommandError("target inspector output exceeds the byte ceiling")
        return result
    try:
        process = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
    except OSError as error:
        raise CommandError("target inspector unavailable") from error
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    stdout_open = process.stdout is not None
    stderr_open = process.stderr is not None
    started = time.monotonic()
    try:
        while stdout_open or stderr_open:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                _kill(process)
                raise CommandError("target inspector timed out")
            watch: list[Any] = []
            if stdout_open and process.stdout is not None:
                watch.append(process.stdout)
            if stderr_open and process.stderr is not None:
                watch.append(process.stderr)
            ready, _writers, _errors = select.select(watch, [], [], min(0.1, remaining))
            if not ready:
                if process.poll() is not None and not watch:
                    break
                continue
            for pipe in ready:
                chunk = os.read(pipe.fileno(), 4096)
                target = stdout_buf if pipe is process.stdout else stderr_buf
                if not chunk:
                    if pipe is process.stdout:
                        stdout_open = False
                    else:
                        stderr_open = False
                    continue
                if len(target) + len(chunk) > max_bytes:
                    _kill(process)
                    raise CommandError("target inspector output exceeds the byte ceiling")
                target.extend(chunk)
        returncode = process.wait(timeout=1)
    except CommandError:
        raise
    except subprocess.TimeoutExpired as error:
        _kill(process)
        raise CommandError("target inspector timed out") from error
    except OSError as error:
        _kill(process)
        raise CommandError("target inspector unavailable") from error
    finally:
        if process.poll() is None:
            _kill(process)
        _close_pipes(process)
    return subprocess.CompletedProcess(
        list(argv),
        returncode if returncode is not None else 1,
        stdout_buf.decode("utf-8", errors="replace"),
        stderr_buf.decode("utf-8", errors="replace"),
    )
