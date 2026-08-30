"""Production target inspector tests for Issue #1893."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.common.compressed_chunk_cold_runtime_catalog import ColdRuntimeError
from packages.common.compressed_chunk_cold_target import (
    CONTAINER_COLD_PATH,
    CONTAINER_WRITABLE_ARGV,
    HOST_COLD_PATH,
    INSPECT_OUTPUT_MAX_BYTES,
    TRUSTED_DOCKER_BIN,
    inspect_production_target,
    production_inspect_target,
    run_bounded_command,
)


def _mounts(*sources: str) -> str:
    return json.dumps([{"Destination": CONTAINER_COLD_PATH, "Source": source} for source in sources])


def _host(_path: str) -> dict[str, int | str]:
    return {"device_identity": "8:11", "mode": 0o555, "uid": 999, "gid": 999}


def _docker_runner(*, writable: bool = True, source: str = HOST_COLD_PATH):
    def runner(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        if "inspect" in argv:
            return SimpleNamespace(returncode=0, stdout=_mounts(source), stderr="")
        if list(argv) == list(CONTAINER_WRITABLE_ARGV):
            return SimpleNamespace(returncode=0 if writable else 1, stdout="", stderr="denied")
        raise AssertionError(argv)

    return runner


def test_production_inspector_reads_mount_and_container_writable() -> None:
    observed = inspect_production_target(
        runner=_docker_runner(writable=True),
        host_inspect=_host,
        docker_bin=TRUSTED_DOCKER_BIN,
    )
    assert observed.host_path == HOST_COLD_PATH
    assert observed.device_identity == "8:11"
    assert observed.writable is True
    assert observed.host_mode == 0o555


def test_host_unwritable_to_nwm_still_passes_when_container_can_write() -> None:
    observed = inspect_production_target(runner=_docker_runner(writable=True), host_inspect=_host)
    assert observed.writable is True


def test_container_unwritable_fails_even_if_host_looks_open() -> None:
    def open_host(_path: str) -> dict[str, int | str]:
        return {"device_identity": "8:11", "mode": 0o777, "uid": 0, "gid": 0}

    with pytest.raises(ColdRuntimeError, match="not writable"):
        inspect_production_target(runner=_docker_runner(writable=False), host_inspect=open_host)


def test_untrusted_absolute_docker_bin_is_refused(tmp_path: Path) -> None:
    fake = tmp_path / "fake-docker"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    with pytest.raises(ColdRuntimeError, match="trusted"):
        inspect_production_target(docker_bin=str(fake), host_inspect=_host, runner=_docker_runner())


def test_tmp_fake_docker_absolute_path_is_refused() -> None:
    fake = Path("/tmp/fake-docker")
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    try:
        with pytest.raises(ColdRuntimeError, match="trusted"):
            inspect_production_target(docker_bin=str(fake), host_inspect=_host, runner=_docker_runner())
    finally:
        fake.unlink(missing_ok=True)


def test_missing_container_is_target_identity_error() -> None:
    def runner(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        if "inspect" in argv:
            return SimpleNamespace(returncode=1, stdout="", stderr="No such container")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(ColdRuntimeError, match="could not inspect"):
        inspect_production_target(runner=runner, host_inspect=_host)


def test_malformed_mount_json_is_refused() -> None:
    def runner(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        if "inspect" in argv:
            return SimpleNamespace(returncode=0, stdout="{not-json", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(ColdRuntimeError, match="malformed"):
        inspect_production_target(runner=runner, host_inspect=_host)


def test_timeout_is_refused() -> None:
    def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="docker", timeout=5)

    with pytest.raises(ColdRuntimeError, match="timed out"):
        inspect_production_target(runner=runner, host_inspect=_host)


def test_output_cap_is_enforced() -> None:
    huge = "x" * (INSPECT_OUTPUT_MAX_BYTES + 1)

    def runner(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        if "inspect" in argv:
            return SimpleNamespace(returncode=0, stdout=huge, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(ColdRuntimeError, match="byte ceiling"):
        inspect_production_target(runner=runner, host_inspect=_host)


def test_stderr_cap_is_enforced() -> None:
    huge = "e" * (INSPECT_OUTPUT_MAX_BYTES + 1)

    def runner(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        if "inspect" in argv:
            return SimpleNamespace(returncode=1, stdout="", stderr=huge)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(ColdRuntimeError, match="byte ceiling"):
        inspect_production_target(runner=runner, host_inspect=_host)


def test_mount_mismatch_is_refused() -> None:
    with pytest.raises(ColdRuntimeError, match="bind source drifted"):
        inspect_production_target(runner=_docker_runner(source="/tmp/wrong-cold"), host_inspect=_host)


def test_path_swap_is_refused() -> None:
    def boom(_path: str) -> dict[str, int | str]:
        raise ColdRuntimeError(
            "target host path identity drifted during inspection",
            error_class="target_identity",
            stage="target_identity",
        )

    with pytest.raises(ColdRuntimeError, match="identity drifted"):
        inspect_production_target(runner=_docker_runner(), host_inspect=boom)


def test_production_inspect_target_does_not_echo_expected_values() -> None:
    payload = production_inspect_target(
        runner=_docker_runner(),
        host_inspect=_host,
        expected_host_path=HOST_COLD_PATH,
    )
    assert payload["device_identity"] == "8:11"
    assert payload["writable"] is True


def test_bounded_collector_caps_real_stdout_child() -> None:
    started = time.monotonic()
    with pytest.raises(ColdRuntimeError, match="byte ceiling"):
        run_bounded_command(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000); sys.stdout.flush()"],
            timeout=5,
        )
    assert time.monotonic() - started < 4


def test_bounded_collector_caps_real_stderr_child() -> None:
    started = time.monotonic()
    with pytest.raises(ColdRuntimeError, match="byte ceiling"):
        run_bounded_command(
            [sys.executable, "-c", "import sys; sys.stderr.write('e' * 200000); sys.stderr.flush()"],
            timeout=5,
        )
    assert time.monotonic() - started < 4


def test_bounded_collector_kills_hanging_child() -> None:
    started = time.monotonic()
    with pytest.raises(ColdRuntimeError, match="timed out"):
        run_bounded_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
    assert time.monotonic() - started < 4


def test_container_writable_argv_runs_as_postgres() -> None:
    assert CONTAINER_WRITABLE_ARGV == (
        "/usr/bin/docker",
        "exec",
        "--user",
        "postgres",
        "nhms-db",
        "test",
        "-w",
        CONTAINER_COLD_PATH,
    )


def test_host_identity_drift_after_writable_check_is_refused() -> None:
    seen = {"count": 0}

    def drifting(_path: str) -> dict[str, int | str]:
        seen["count"] += 1
        if seen["count"] == 1:
            return {"device_identity": "8:11", "mode": 0o555, "uid": 999, "gid": 999}
        return {"device_identity": "8:12", "mode": 0o555, "uid": 999, "gid": 999}

    with pytest.raises(ColdRuntimeError, match="identity drifted after writable check"):
        inspect_production_target(runner=_docker_runner(writable=True), host_inspect=drifting)
    assert seen["count"] == 2
