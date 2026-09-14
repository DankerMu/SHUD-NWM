#!/usr/bin/env python3
"""Validate and safely launch node-27's ordinary timeseries compression runner."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

# This script is launched from systemd as a file, whose initial import path is
# ``scripts/`` rather than the checkout root.  Put the trusted checkout holding
# this script first rather than relying on a caller-controlled PYTHONPATH.
_CHECKOUT_ROOT = Path(__file__).parent.parent
sys.path[:] = [entry for entry in sys.path if entry != str(_CHECKOUT_ROOT)]
sys.path.insert(0, str(_CHECKOUT_ROOT))

from packages.common.node27_timeseries_compression_budget import (  # noqa: E402
    ASSEMBLY_COMPRESSION_BOUND_KEY,
    ASSEMBLY_COMPRESSION_STATEMENT_KEY,
    ASSEMBLY_COMPRESSION_WRAPPER_KEY,
    ASSEMBLY_MARKER_KEY,
    ASSEMBLY_SERVICE_WALL_KEY,
    CompressionBudgetError,
    ParsedCompressionEnv,
    read_compression_env_data,
)

_DEFAULT_REPO_ROOT = "/home/nwm/NWM"
_IMPORT_ORIGIN_PROBE = """
import importlib.machinery
import os
import sys

root = os.path.realpath(sys.argv[1])
script = os.path.realpath(sys.argv[2])
expected_namespace = os.path.join(root, "scripts")
search_path = list(sys.path)
if not sys.flags.safe_path:
    search_path[0] = os.path.dirname(script)
spec = importlib.machinery.PathFinder.find_spec("scripts", search_path)
locations = (
    []
    if spec is None or spec.submodule_search_locations is None
    else [os.path.realpath(path) for path in spec.submodule_search_locations]
)
valid = (
    spec is not None
    and spec.origin is None
    and locations
    and all(path == expected_namespace for path in locations)
)
raise SystemExit(0 if valid else 1)
"""

_LANE_OPTIONS = {
    "repo_root": "NODE27_TIMESERIES_COMPRESSION_REPO_ROOT",
    "python": "NODE27_TIMESERIES_COMPRESSION_PYTHON",
    "script": "NODE27_TIMESERIES_COMPRESSION_SCRIPT",
    "entrypoint": "node27_timeseries_compression.py",
    "entrypoint_failure": "compression entrypoint is unavailable or a symlink",
}


class _ArgumentParser(argparse.ArgumentParser):
    """Keep command-line errors stable and free of supplied input text."""

    def error(self, _message: str) -> None:
        self.exit(2, "node27-timeseries-budget-preflight: invalid arguments\n")


class _LaunchError(RuntimeError):
    """A stable, non-secret refusal while constructing a child launch."""


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--compression-env", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--lane", choices=("compression",))
    action.add_argument("--check", action="store_true")
    action.add_argument("--launch", choices=("compression",))
    parser.add_argument("--format", choices=("wall", "assembly"), default="wall")
    return parser


def _parse_args(argv: Sequence[str] | None) -> tuple[argparse.Namespace, list[str]]:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    runner_args: list[str] = []
    if "--" in raw_args:
        separator = raw_args.index("--")
        runner_args = raw_args[separator + 1 :]
        raw_args = raw_args[:separator]
    args = _parser().parse_args(raw_args)
    if runner_args and args.launch is None:
        _parser().error("runner arguments require launch mode")
    return args, runner_args


def _output_for_lane(*, output_format: str, values: tuple[int, int, int, int]) -> str:
    wrapper_wall, _service_wall, _statement, _bound = values
    if output_format == "assembly":
        return ",".join(str(value) for value in values)
    return str(wrapper_wall)


def _failure(reason: str) -> int:
    print(json.dumps({"status": "failed", "reason": reason}, separators=(",", ":")), file=sys.stderr)
    return 1


def _absolute_path(raw: str, *, failure: str) -> Path:
    try:
        path = Path(raw)
    except (TypeError, ValueError):
        raise _LaunchError(failure) from None
    if "\x00" in str(path) or not path.is_absolute():
        raise _LaunchError(failure)
    return path


def _runner_root(own_env: Mapping[str, str], caller_env: Mapping[str, str]) -> Path:
    root_key = _LANE_OPTIONS["repo_root"]
    raw = own_env[root_key] if root_key in own_env else caller_env.get(root_key)
    root = _absolute_path(raw or _DEFAULT_REPO_ROOT, failure="repository root must be absolute")
    if ":" in str(root):
        raise _LaunchError("repository root must not contain a path-list delimiter")
    return root


def _runner_paths(
    *,
    own_env: Mapping[str, str],
    caller_env: Mapping[str, str],
) -> tuple[Path, Path, Path]:
    root = _runner_root(own_env, caller_env)
    caller_python = caller_env.get(_LANE_OPTIONS["python"]) or None
    caller_script = caller_env.get(_LANE_OPTIONS["script"]) or None
    # A lane-file Python or script assignment remains inert child data; it never
    # selects the executable or entrypoint for this launch.
    python_raw = caller_python or str(root / ".venv/bin/python")
    script_raw = caller_script or str(root / "scripts" / _LANE_OPTIONS["entrypoint"])
    python_bin = _absolute_path(python_raw, failure="wrapper paths must be absolute")
    script = _absolute_path(script_raw, failure="wrapper paths must be absolute")
    try:
        python_available = python_bin.is_file() and os.access(python_bin, os.X_OK)
    except (OSError, ValueError):
        python_available = False
    if not python_available:
        raise _LaunchError("python executable is unavailable")
    try:
        script_mode = os.lstat(script).st_mode
    except (OSError, ValueError):
        raise _LaunchError(_LANE_OPTIONS["entrypoint_failure"]) from None
    if stat.S_ISLNK(script_mode) or not stat.S_ISREG(script_mode):
        raise _LaunchError(_LANE_OPTIONS["entrypoint_failure"])
    return root, python_bin, script


def _child_environment(
    *,
    parsed: ParsedCompressionEnv,
    caller_env: Mapping[str, str],
    runner_root: Path,
) -> dict[str, str]:
    own_env = parsed.compression_env
    child_env = dict(caller_env)
    child_env.update(own_env)
    for key in (_LANE_OPTIONS["python"], _LANE_OPTIONS["script"]):
        caller_value = caller_env.get(key)
        if caller_value:
            child_env[key] = caller_value

    caller_pythonpath = caller_env.get("PYTHONPATH", "")
    child_env["PYTHONPATH"] = (
        f"{runner_root}:{caller_pythonpath}" if caller_pythonpath else str(runner_root)
    )
    wrapper_wall, service_wall, statement_timeout, bound = parsed.resolved.assembly_values()
    child_env.update(
        {
            ASSEMBLY_MARKER_KEY: "1",
            ASSEMBLY_COMPRESSION_WRAPPER_KEY: str(wrapper_wall),
            ASSEMBLY_SERVICE_WALL_KEY: str(service_wall),
            ASSEMBLY_COMPRESSION_STATEMENT_KEY: str(statement_timeout),
            ASSEMBLY_COMPRESSION_BOUND_KEY: str(bound),
        }
    )
    return child_env


def _verify_scripts_import_origin(*, python_bin: Path, root: Path, script: Path, child_env: Mapping[str, str]) -> None:
    try:
        probe = subprocess.run(
            [str(python_bin), "-c", _IMPORT_ORIGIN_PROBE, str(root), str(script)],
            env=dict(child_env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        raise _LaunchError("python executable is unavailable") from None
    if probe.returncode != 0:
        raise _LaunchError("scripts import origin is outside repository root")


def _launch(*, parsed: ParsedCompressionEnv, caller_env: Mapping[str, str], runner_args: Sequence[str]) -> int:
    own_env = parsed.compression_env
    root, python_bin, script = _runner_paths(own_env=own_env, caller_env=caller_env)
    child_env = _child_environment(
        parsed=parsed,
        caller_env=caller_env,
        runner_root=root,
    )
    _verify_scripts_import_origin(
        python_bin=python_bin,
        root=root,
        script=script,
        child_env=child_env,
    )
    if not os.path.isfile("/usr/bin/timeout") or not os.access("/usr/bin/timeout", os.X_OK):
        raise _LaunchError("timeout launcher is unavailable")
    wall = parsed.resolved.budget.wrapper_wall_seconds
    timeout_args = [
        "/usr/bin/timeout",
        "--signal=TERM",
        "--kill-after=30s",
        f"{wall}s",
        str(python_bin),
        str(script),
        *runner_args,
    ]
    try:
        os.execve("/usr/bin/timeout", timeout_args, child_env)
    except OSError:
        return _failure("timeout launcher is unavailable")
    raise AssertionError("os.execve unexpectedly returned")


def main(argv: Sequence[str] | None = None) -> int:
    args, runner_args = _parse_args(argv)
    try:
        compression_path = Path(args.compression_env)
    except (TypeError, ValueError):
        if args.launch:
            return _failure("compression budget preflight failed")
        print("node27-timeseries-budget-preflight: invalid compression env", file=sys.stderr)
        return 1
    if not compression_path.is_absolute():
        if args.launch:
            return _failure("compression budget preflight failed")
        print("node27-timeseries-budget-preflight: invalid compression env", file=sys.stderr)
        return 1
    try:
        parsed = read_compression_env_data(compression_path)
    except CompressionBudgetError:
        if args.launch:
            return _failure("compression budget preflight failed")
        print("node27-timeseries-budget-preflight: invalid compression env", file=sys.stderr)
        return 1
    if args.check:
        return 0
    if args.launch:
        try:
            return _launch(
                parsed=parsed,
                caller_env=dict(os.environ),
                runner_args=runner_args,
            )
        except _LaunchError as error:
            return _failure(str(error))
    assert args.lane == "compression"
    print(_output_for_lane(output_format=args.format, values=parsed.resolved.assembly_values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
